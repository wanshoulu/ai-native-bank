"""Strict loading and deterministic matching for governed Mock FAQ data."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .text_utils import normalize as _normalize

_REQUIRED_ENTRY_COUNT = 12


_TOP_LEVEL_FIELDS = {"schemaVersion", "version", "scope", "entries"}
_ENTRY_FIELDS = {
    "id",
    "intentId",
    "questionPatterns",
    "answer",
    "recommendedActionKeys",
}
_SEMVER = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_FAQ_ID = re.compile(r"^faq\.[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_INTENT_ID = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$")
_ACTION_KEY = re.compile(r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*$")


class FaqError(RuntimeError):
    """The configured FAQ is invalid or cannot be loaded."""


@dataclass(frozen=True, slots=True)
class FaqEntry:
    id: str
    version: str
    intent_id: str
    question_patterns: tuple[str, ...]
    answer: str
    scope: str
    recommended_action_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FaqMatch:
    entry: FaqEntry
    pattern: str


@dataclass(frozen=True, slots=True)
class FaqAnalysis:
    matches: tuple[FaqMatch, ...]
    residual_semantics: str
    fully_covered: bool


@dataclass(frozen=True, slots=True)
class MockFaqRepository:
    version: str
    scope: str
    entries: tuple[FaqEntry, ...]
    _entries_by_id: Mapping[str, FaqEntry] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        immutable_entries = tuple(self.entries)
        if any(type(entry) is not FaqEntry for entry in immutable_entries):
            raise FaqError("FAQ repository entries are invalid")
        entries_by_id = {entry.id: entry for entry in immutable_entries}
        if len(entries_by_id) != len(immutable_entries):
            raise FaqError("FAQ entry id is duplicated")
        object.__setattr__(self, "entries", immutable_entries)
        object.__setattr__(self, "_entries_by_id", MappingProxyType(entries_by_id))

    @property
    def entries_by_id(self) -> Mapping[str, FaqEntry]:
        return self._entries_by_id

    def canonical_entry(self, entry_id: str, *, version: str) -> FaqEntry:
        entry = self._entries_by_id.get(entry_id)
        if entry is None or entry.version != version:
            raise FaqError("FAQ canonical entry is missing")
        return entry

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        valid_action_keys: AbstractSet[str],
    ) -> "MockFaqRepository":
        faq_path = Path(path)
        try:
            payload = json.loads(
                faq_path.read_text(encoding="utf-8"),
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FaqError("FAQ cannot be loaded") from exc

        version, scope, entries = _validate_payload(payload, valid_action_keys)
        return cls(version, scope, entries)

    def match(self, message: str) -> FaqEntry | None:
        analysis = self.analyze(message)
        if not analysis.matches:
            return None
        query = _normalize(message)
        return sorted(
            analysis.matches,
            key=lambda item: (
                -(200 if query == _normalize(item.pattern) else len(_normalize(item.pattern))),
                item.entry.id,
            ),
        )[0].entry

    def analyze(self, message: str) -> FaqAnalysis:
        query = _normalize(message)
        if not query:
            return FaqAnalysis((), "", False)
        located: list[tuple[int, int, FaqMatch]] = []
        for entry in self.entries:
            entry_matches: list[tuple[int, int, str]] = []
            for pattern in entry.question_patterns:
                normalized_pattern = _normalize(pattern)
                start = query.find(normalized_pattern)
                if start >= 0:
                    entry_matches.append(
                        (start, start + len(normalized_pattern), pattern)
                    )
            if entry_matches:
                start, end, pattern = sorted(
                    entry_matches,
                    key=lambda item: (-(item[1] - item[0]), item[0], item[2]),
                )[0]
                located.append((start, end, FaqMatch(entry, pattern)))
        located.sort(key=lambda item: (item[0], item[2].entry.id))
        retained = [True] * len(query)
        for start, end, _match in located:
            for index in range(start, end):
                retained[index] = False
        residual = "".join(
            character for index, character in enumerate(query) if retained[index]
        )
        residual = _strip_semantic_wrappers(residual)
        return FaqAnalysis(
            tuple(item[2] for item in located),
            residual,
            bool(located) and not residual,
        )


def _validate_payload(
    payload: Any,
    valid_action_keys: AbstractSet[str],
) -> tuple[str, str, tuple[FaqEntry, ...]]:
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_FIELDS:
        raise FaqError("FAQ schema is invalid")
    if payload["schemaVersion"] != "1.0":
        raise FaqError("FAQ schema version is invalid")

    version = payload["version"]
    if not isinstance(version, str) or _SEMVER.fullmatch(version) is None:
        raise FaqError("FAQ version is invalid")

    scope = payload["scope"]
    if not isinstance(scope, str) or not 1 <= len(scope.strip()) <= 120:
        raise FaqError("FAQ scope is invalid")
    scope = scope.strip()

    raw_entries = payload["entries"]
    if not isinstance(raw_entries, list) or len(raw_entries) != _REQUIRED_ENTRY_COUNT:
        raise FaqError("FAQ must contain exactly 12 entries")

    entries: list[FaqEntry] = []
    ids: set[str] = set()
    for raw_entry in raw_entries:
        entry = _validate_entry(raw_entry, version, scope, valid_action_keys)
        if entry.id in ids:
            raise FaqError("FAQ entry id is duplicated")
        ids.add(entry.id)
        entries.append(entry)
    return version, scope, tuple(entries)


def _validate_entry(
    payload: Any,
    version: str,
    scope: str,
    valid_action_keys: AbstractSet[str],
) -> FaqEntry:
    if not isinstance(payload, dict) or set(payload) != _ENTRY_FIELDS:
        raise FaqError("FAQ entry schema is invalid")

    entry_id = payload["id"]
    if not isinstance(entry_id, str) or _FAQ_ID.fullmatch(entry_id) is None:
        raise FaqError("FAQ entry id is invalid")

    intent_id = payload["intentId"]
    if not isinstance(intent_id, str) or _INTENT_ID.fullmatch(intent_id) is None:
        raise FaqError("FAQ intent id is invalid")

    raw_patterns = payload["questionPatterns"]
    if not isinstance(raw_patterns, list) or not 1 <= len(raw_patterns) <= 8:
        raise FaqError("FAQ question pattern is invalid")
    patterns: list[str] = []
    normalized_patterns: set[str] = set()
    for pattern in raw_patterns:
        if not isinstance(pattern, str) or not 1 <= len(pattern.strip()) <= 120:
            raise FaqError("FAQ question pattern is invalid")
        normalized = _normalize(pattern)
        if not normalized or normalized in normalized_patterns:
            raise FaqError("FAQ question pattern is invalid")
        normalized_patterns.add(normalized)
        patterns.append(pattern.strip())

    answer = payload["answer"]
    if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 600:
        raise FaqError("FAQ answer is invalid")
    answer = answer.strip()

    raw_action_keys = payload["recommendedActionKeys"]
    if not isinstance(raw_action_keys, list) or not 1 <= len(raw_action_keys) <= 8:
        raise FaqError("FAQ recommended action is invalid")
    action_keys: list[str] = []
    for action_key in raw_action_keys:
        if (
            not isinstance(action_key, str)
            or _ACTION_KEY.fullmatch(action_key) is None
            or action_key not in valid_action_keys
            or action_key in action_keys
        ):
            raise FaqError("FAQ recommended action is invalid")
        action_keys.append(action_key)

    return FaqEntry(
        id=entry_id,
        version=version,
        intent_id=intent_id,
        question_patterns=tuple(patterns),
        answer=answer,
        scope=scope,
        recommended_action_keys=tuple(action_keys),
    )


def _strip_semantic_wrappers(value: str) -> str:
    result = value.replace("一下", "")
    prefixes = (
        "请问", "麻烦", "帮我", "帮忙", "我想问", "想问", "问下",
        "然后", "并且", "另外", "顺便", "同时", "以及", "还有", "再", "和",
    )
    suffixes = ("谢谢", "谢了", "呢", "呀", "啊", "吗", "吧")
    changed = True
    while changed and result:
        changed = False
        for token in prefixes:
            if result.startswith(token):
                result = result[len(token):]
                changed = True
                break
        for token in suffixes:
            if result.endswith(token):
                result = result[:-len(token)]
                changed = True
                break
    return result


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FaqError(f"FAQ duplicate JSON key: {key}")
        result[key] = value
    return result
