"""Local deterministic lexicon-matching Rail for the general safety word list.

The general lexicon is stored as data files (one category per ``.txt``, one
term per line) and is matched locally with a pure-Python Aho-Corasick automaton
before any semantic / LLM rail runs.  No third-party corpus is embedded in code;
terms live in ``data/ai-native-bank/guardrail/lexicon/*.txt``.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any

from app.safety_gate import RailDecision, aggregate_rail_decisions

logger = logging.getLogger("ai_native_bank.agent_runtime.lexicon_rail")

POLICY_VERSION = "general-lexicon-v1"
RAIL_ID = "lexicon-safety"
_NON_WORD = r"0-9a-z\u4e00-\u9fff"


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\u200b-\u200d\ufeff]", "", value)


class _AhoCorasick:
    """Zero-dependency Aho-Corasick automaton for substring matching."""

    def __init__(self) -> None:
        self._goto: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._output: list[set[int]] = [set()]
        self._terms: list[str] = []

    def add(self, term: str) -> None:
        state = 0
        for char in term:
            nxt = self._goto[state].get(char)
            if nxt is None:
                nxt = len(self._goto)
                self._goto[state][char] = nxt
                self._goto.append({})
                self._fail.append(0)
                self._output.append(set())
            state = nxt
        self._output[state].add(len(self._terms))
        self._terms.append(term)

    def build(self) -> None:
        queue: deque[int] = deque()
        for nxt in self._goto[0].values():
            self._fail[nxt] = 0
            queue.append(nxt)
        while queue:
            state = queue.popleft()
            for char, nxt in self._goto[state].items():
                queue.append(nxt)
                fallback = self._fail[state]
                while fallback and char not in self._goto[fallback]:
                    fallback = self._fail[fallback]
                self._fail[nxt] = self._goto[fallback].get(char, 0)
                self._output[nxt] |= self._output[self._fail[nxt]]

    def first_match(self, text: str) -> str | None:
        state = 0
        for char in text:
            while state and char not in self._goto[state]:
                state = self._fail[state]
            state = self._goto[state].get(char, 0)
            if self._output[state]:
                return self._terms[next(iter(self._output[state]))]
        return None


def _decision(
    *,
    stage: str,
    disposition: str,
    started: float,
) -> RailDecision:
    duration_ms = max(0, min(600_000, int((time.monotonic() - started) * 1000)))
    if disposition == "allow":
        return RailDecision(
            railId=RAIL_ID,
            stage=stage,
            decision="allow",
            reasonCode=None,
            policyVersion=POLICY_VERSION,
            responseKey=None,
            durationMs=duration_ms,
        )
    if disposition == "review":
        return RailDecision(
            railId=RAIL_ID,
            stage=stage,
            decision="review",
            reasonCode="lexicon_match",
            policyVersion=POLICY_VERSION,
            responseKey=None,
            durationMs=duration_ms,
        )
    return RailDecision(
        railId=RAIL_ID,
        stage=stage,
        decision="block",
        reasonCode="lexicon_violation",
        policyVersion=POLICY_VERSION,
        responseKey="security.block.lexicon_violation",
        durationMs=duration_ms,
    )


class LexiconInputRail:
    """Advisory input lexicon with independent strict output enforcement."""

    input_advisory = True

    def __init__(self, lexicon_dir: str) -> None:
        self._lexicon_dir = Path(lexicon_dir)
        self._automaton = _AhoCorasick()
        self._short_terms: list[str] = []
        self._short_pattern: re.Pattern[str] | None = None
        self._total_terms = 0
        self._categories: list[str] = []
        self._load()

    def _load(self) -> None:
        if not self._lexicon_dir.is_dir():
            logger.warning("lexicon dir missing: %s", self._lexicon_dir)
            return
        short: set[str] = set()
        for path in sorted(self._lexicon_dir.glob("*.txt")):
            category = path.stem
            count = 0
            for raw in path.read_text(encoding="utf-8").splitlines():
                term = _normalize(raw)
                if not term:
                    continue
                self._total_terms += 1
                count += 1
                # Short single-character terms are matched with word-boundary
                # guards to avoid false positives inside normal words (e.g. 操/逼/靠).
                # Two-character terms use substring matching (e.g. 傻逼/垃圾).
                if len(term) >= 2:
                    self._automaton.add(term)
                else:
                    short.add(term)
            if count:
                self._categories.append(category)
        self._automaton.build()
        self._short_terms = sorted(short)
        if self._short_terms:
            alternation = "|".join(re.escape(term) for term in self._short_terms)
            self._short_pattern = re.compile(
                rf"(?<![{_NON_WORD}])(?:{alternation})(?![{_NON_WORD}])"
            )
        logger.info(
            "lexicon rail loaded: %d terms, %d short, categories=%s",
            self._total_terms,
            len(self._short_terms),
            self._categories,
        )

    def _match(self, text: str) -> str | None:
        normalized = _normalize(text)
        hit = self._automaton.first_match(normalized)
        if hit is not None:
            return hit
        if self._short_pattern is not None:
            match = self._short_pattern.search(normalized)
            if match is not None:
                return match.group(0)
        return None

    async def evaluate(
        self,
        *,
        message: str,
        request_id: str,
        conversation_id: str,
    ) -> RailDecision:
        return await self.evaluate_input(
            message=message,
            request_id=request_id,
            conversation_id=conversation_id,
        )

    async def evaluate_input(
        self,
        *,
        message: str,
        request_id: str,
        conversation_id: str,
        history: list[str] | tuple[str, ...] | None = None,
    ) -> RailDecision:
        del request_id, conversation_id, history
        started = time.monotonic()
        disposition = "review" if self._match(message) else "allow"
        return _decision(stage="input", disposition=disposition, started=started)

    async def evaluate_output(
        self,
        *,
        text: str,
        request_id: str,
        conversation_id: str,
    ) -> RailDecision:
        del request_id, conversation_id
        started = time.monotonic()
        disposition = "block" if self._match(text) else "allow"
        return _decision(stage="output", disposition=disposition, started=started)

    async def evaluate_action(
        self,
        *,
        capability_id: str,
        risk_level: str,
        summary: str,
        requires_confirmation: bool,
        confirmed: bool,
        input_decision: str,
    ) -> RailDecision:
        del capability_id, risk_level, summary, requires_confirmation, confirmed, input_decision
        started = time.monotonic()
        return _decision(stage="action", disposition="allow", started=started)

    async def aclose(self) -> None:
        return None


class ChainedInputRail:
    """Compose multiple Rails, short-circuiting once a block is reached."""

    def __init__(self, rails: list[Any]) -> None:
        self._rails = list(rails)

    async def evaluate(
        self,
        *,
        message: str,
        request_id: str,
        conversation_id: str,
    ) -> RailDecision:
        return await self.evaluate_input(
            message=message,
            request_id=request_id,
            conversation_id=conversation_id,
        )

    async def evaluate_input(
        self,
        *,
        message: str,
        request_id: str,
        conversation_id: str,
        history: list[str] | tuple[str, ...] | None = None,
    ) -> RailDecision:
        advisory_decisions: list[RailDecision] = []
        authoritative_decisions: list[RailDecision] = []
        for rail in self._rails:
            evaluator = getattr(rail, "evaluate_input", None)
            if evaluator is not None:
                decision = await evaluator(
                    message=message,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    history=history,
                )
            else:
                decision = await rail.evaluate(
                    message=message,
                    request_id=request_id,
                    conversation_id=conversation_id,
                )
            if getattr(rail, "input_advisory", False):
                advisory_decisions.append(decision)
                if decision.decision == "unavailable":
                    return decision
                continue
            authoritative_decisions.append(decision)
            if decision.decision == "block":
                return decision
        if authoritative_decisions:
            return aggregate_rail_decisions(authoritative_decisions)
        if advisory_decisions:
            return aggregate_rail_decisions(advisory_decisions)
        if not self._rails:
            return self._allow_input()
        return self._allow_input()

    async def evaluate_output(
        self,
        *,
        text: str,
        request_id: str,
        conversation_id: str,
    ) -> RailDecision:
        decisions: list[RailDecision] = []
        for rail in self._rails:
            evaluator = getattr(rail, "evaluate_output", None)
            if evaluator is None:
                continue
            decision = await evaluator(
                text=text,
                request_id=request_id,
                conversation_id=conversation_id,
            )
            decisions.append(decision)
            if decision.decision == "block":
                return decision
        return aggregate_rail_decisions(decisions) if decisions else self._allow_output()

    async def evaluate_action(
        self,
        *,
        capability_id: str,
        risk_level: str,
        summary: str,
        requires_confirmation: bool,
        confirmed: bool,
        input_decision: str,
    ) -> RailDecision:
        decisions: list[RailDecision] = []
        for rail in self._rails:
            evaluator = getattr(rail, "evaluate_action", None)
            if evaluator is None:
                continue
            decision = await evaluator(
                capability_id=capability_id,
                risk_level=risk_level,
                summary=summary,
                requires_confirmation=requires_confirmation,
                confirmed=confirmed,
                input_decision=input_decision,
            )
            decisions.append(decision)
            if decision.decision == "block":
                return decision
        return aggregate_rail_decisions(decisions) if decisions else self._allow_action()

    @staticmethod
    def _allow_input() -> RailDecision:
        return RailDecision(
            railId=RAIL_ID,
            stage="input",
            decision="allow",
            reasonCode=None,
            policyVersion=POLICY_VERSION,
            responseKey=None,
            durationMs=0,
        )

    @staticmethod
    def _allow_output() -> RailDecision:
        return RailDecision(
            railId=RAIL_ID,
            stage="output",
            decision="allow",
            reasonCode=None,
            policyVersion=POLICY_VERSION,
            responseKey=None,
            durationMs=0,
        )

    @staticmethod
    def _allow_action() -> RailDecision:
        return RailDecision(
            railId=RAIL_ID,
            stage="action",
            decision="allow",
            reasonCode=None,
            policyVersion=POLICY_VERSION,
            responseKey=None,
            durationMs=0,
        )

    async def aclose(self) -> None:
        for rail in self._rails:
            closer = getattr(rail, "aclose", None)
            if closer is not None:
                await closer()
