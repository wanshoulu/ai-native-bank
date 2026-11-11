"""Immutable intent graph loading and bounded deterministic recall."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple

from .text_utils import normalize as _normalize

# --- Lexical recall scoring constants ---
_SCORE_ALIAS_EXACT = 110
_SCORE_ALIAS_CONTAINS = 75
_SCORE_RECALL_TERMS = 90
_SCORE_RECALL_TERM_SETS = 100
_SCORE_PAGE_MENU = 35
_SCORE_PAGE_TAB = 12
_SCORE_NEGATIVE_EXAMPLE = -160
_SCORE_DISCARD_THRESHOLD = 60

# --- Graph traversal expansion constants ---
# Edge types traversed for 2-hop intent-to-intent sibling expansion.
# Weight is applied to the seed candidate's lexical score to derive
# the expansion candidate's score, capped by _GRAPH_EXPAND_MAX_SCORE.
_GRAPH_EXPAND_EDGE_WEIGHTS: Mapping[str, float] = MappingProxyType({
    "offers": 0.65,       # menu→intent: sibling intents under same menu (strongest)
    "TARGETS": 0.60,      # intent→business-object: intents on same domain object
    "HANDLED_BY": 0.55,   # intent→skill: intents handled by same orchestrator
})
_GRAPH_EXPAND_MAX_SCORE = 80
_GRAPH_EXPAND_MAX_PER_SEED = 2
_GRAPH_EXPAND_MAX_TOTAL = 4


class IntentGraphError(RuntimeError):
    """The configured graph is invalid or incompatible with a runtime request."""


@dataclass(frozen=True)
class SlotDefinition:
    slot_id: str
    name: str
    label: str
    type: str
    required: bool
    extraction_keys: tuple[str, ...]
    resolved_field: str
    resolver_capability_id: str | None = None
    normalizer: str | None = None
    default_value: Any | None = None

    def to_prompt_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "slotId": self.slot_id,
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "extractionKeys": list(self.extraction_keys),
            "resolvedField": self.resolved_field,
        }
        if self.resolver_capability_id is not None:
            result["resolverCapabilityId"] = self.resolver_capability_id
        if self.normalizer is not None:
            result["normalizer"] = self.normalizer
        if self.default_value is not None:
            result["defaultValue"] = self.default_value
        return result


@dataclass(frozen=True)
class ExecutionBinding:
    capability_id: str
    requires_confirmation: bool


@dataclass(frozen=True)
class IntentSlotContract:
    version: str
    slots: tuple[SlotDefinition, ...]
    clarification_order: tuple[str, ...]
    execution_binding: ExecutionBinding

    def slot(self, name: str) -> SlotDefinition:
        for item in self.slots:
            if item.name == name:
                return item
        raise KeyError(name)

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "slots": [item.to_prompt_dict() for item in self.slots],
            "clarificationOrder": list(self.clarification_order),
            "executionBinding": {
                "capabilityId": self.execution_binding.capability_id,
                "requiresConfirmation": self.execution_binding.requires_confirmation,
            },
        }


@dataclass(frozen=True)
class SkillBinding:
    skill_id: str
    version: str
    digest: str

    def to_prompt_dict(self) -> dict[str, str]:
        return {"skillId": self.skill_id, "version": self.version, "digest": self.digest}


@dataclass(frozen=True)
class TaskPolicy:
    supported_shapes: tuple[str, ...]
    mode: str
    max_tasks: int

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "supportedShapes": list(self.supported_shapes),
            "mode": self.mode,
            "maxTasks": self.max_tasks,
        }


@dataclass(frozen=True)
class ContextPolicy:
    retain_on_clarification: bool = True
    allow_intent_switch: bool = True
    resumable: bool = True


def _project_context_policy(value: Any) -> ContextPolicy:
    if not isinstance(value, Mapping):
        return ContextPolicy()
    return ContextPolicy(
        retain_on_clarification=bool(value.get("retainOnClarification", True)),
        allow_intent_switch=bool(value.get("allowIntentSwitch", True)),
        resumable=bool(value.get("resumable", True)),
    )


class IntentProjection(NamedTuple):
    intent_id: str
    title: str
    aliases: list[str]
    description: str = ""


@dataclass(frozen=True)
class IntentCandidate:
    intent_id: str
    title: str
    action: str
    business_object: str
    execution_mode: str
    risk_level: str
    required_slots: list[str]
    capability_bindings: list[str]
    score: int
    evidence: list[str]
    rule_version: str
    semantic_priority: int = 0
    slot_contract: IntentSlotContract | None = None
    skill_binding: SkillBinding | None = None
    task_policy: TaskPolicy | None = None
    context_policy: ContextPolicy | None = None
    response_policy: str | None = None
    business_object_id: str | None = None
    orchestration_mode: str | None = None
    lifecycle: str | None = None
    readiness: str | None = None
    fulfillment_kind: str | None = None
    confusable_intents: tuple[str, ...] = ()
    disambiguation_hints: str | None = None

    def to_prompt_dict(self) -> dict[str, Any]:
        result = {
            "intent_id": self.intent_id,
            "title": self.title,
            "action": self.action,
            "business_object": self.business_object,
            "execution_mode": self.execution_mode,
            "risk_level": self.risk_level,
            "required_slots": list(self.required_slots),
            "capability_bindings": list(self.capability_bindings),
            "score": self.score,
            "evidence": list(self.evidence),
            "rule_version": self.rule_version,
        }
        if self.confusable_intents:
            result["confusableIntents"] = list(self.confusable_intents)
        if self.disambiguation_hints is not None:
            result["disambiguationHints"] = self.disambiguation_hints
        if self.slot_contract is not None:
            result["slotContract"] = self.slot_contract.to_prompt_dict()
        if self.skill_binding is not None:
            result["skillBinding"] = self.skill_binding.to_prompt_dict()
        if self.task_policy is not None:
            result["taskPolicy"] = self.task_policy.to_prompt_dict()
        if self.response_policy is not None:
            result["responsePolicy"] = self.response_policy
        if self.business_object_id is not None:
            result["businessObjectId"] = self.business_object_id
        if self.orchestration_mode is not None:
            result["orchestrationMode"] = self.orchestration_mode
        if self.lifecycle is not None:
            result["lifecycle"] = self.lifecycle
        if self.readiness is not None:
            result["readiness"] = self.readiness
        if self.fulfillment_kind is not None:
            result["fulfillmentKind"] = self.fulfillment_kind
        return result


class IntentGraphRepository:
    __slots__ = (
        "_payload",
        "_path",
        "_intents",
        "_intents_by_id",
        "_intent_action_keys",
        "_action_keys",
        "_adjacency",
        "_reverse_adjacency",
        "graph_id",
        "version",
        "digest",
        "menu_count",
        "_sealed",
    )

    def __init__(self, payload: dict[str, Any], path: Path):
        frozen_payload = _deep_freeze(payload)
        intents = tuple(
            node for node in frozen_payload["nodes"] if node["type"] == "intent"
        )
        intents_by_id = MappingProxyType({node["id"]: node for node in intents})
        intent_action_keys = MappingProxyType({
            intent_id: self._action_key(intent)
            for intent_id, intent in intents_by_id.items()
        })
        object.__setattr__(self, "_payload", frozen_payload)
        object.__setattr__(self, "_path", Path(path))
        object.__setattr__(self, "_intents", intents)
        object.__setattr__(self, "_intents_by_id", intents_by_id)
        object.__setattr__(self, "_intent_action_keys", intent_action_keys)
        object.__setattr__(self, "_action_keys", frozenset(intent_action_keys.values()))
        object.__setattr__(self, "graph_id", frozen_payload["graphId"])
        object.__setattr__(self, "version", frozen_payload["version"])
        object.__setattr__(self, "digest", frozen_payload["digest"])
        object.__setattr__(
            self,
            "menu_count",
            sum(1 for node in frozen_payload["nodes"] if node["type"] == "menu"),
        )
        # Build forward and reverse adjacency lists from edges for graph traversal.
        # Structure: {node_id: {edge_type: frozenset(neighbor_ids)}}
        forward: dict[str, dict[str, set[str]]] = {}
        reverse: dict[str, dict[str, set[str]]] = {}
        for edge in frozen_payload["edges"]:
            edge_type = edge["type"]
            src = edge["from"]
            dst = edge["to"]
            forward.setdefault(src, {}).setdefault(edge_type, set()).add(dst)
            reverse.setdefault(dst, {}).setdefault(edge_type, set()).add(src)
        object.__setattr__(
            self,
            "_adjacency",
            MappingProxyType({
                src: MappingProxyType({
                    et: frozenset(targets) for et, targets in edges.items()
                })
                for src, edges in forward.items()
            }),
        )
        object.__setattr__(
            self,
            "_reverse_adjacency",
            MappingProxyType({
                dst: MappingProxyType({
                    et: frozenset(sources) for et, sources in edges.items()
                })
                for dst, edges in reverse.items()
            }),
        )
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("intent graph repository is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def load(cls, path: str | Path) -> "IntentGraphRepository":
        graph_path = Path(path)
        try:
            payload = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IntentGraphError("intent graph cannot be loaded") from exc
        _validate_graph(payload)
        return cls(payload, graph_path)

    def recall(
        self,
        message: str,
        *,
        page_context: dict[str, Any] | None = None,
        limit: int = 8,
        graph_expand: bool = True,
    ) -> list[IntentCandidate]:
        normalized = _normalize(message)
        if not normalized:
            return []
        context = page_context or {}
        tab = str(context.get("tab") or "").strip().lower()
        menu_key = str(context.get("menuKey") or context.get("menu_key") or "").strip().lower()
        candidates: list[IntentCandidate] = []
        for intent in self._intents:
            if intent.get("lifecycle", "active") != "active":
                continue
            score = 0
            evidence: list[str] = []
            # alias 打分 — 遍历所有别名取最高分，exact(110) 优先于 contains(75)
            alias_score = 0
            for alias in intent.get("aliases", []):
                alias_normalized = _normalize(alias)
                if len(alias_normalized) >= 2 and (
                    alias_normalized in normalized or normalized in alias_normalized
                ):
                    if alias_normalized == normalized:
                        alias_score = _SCORE_ALIAS_EXACT
                        break
                    elif alias_score < _SCORE_ALIAS_CONTAINS:
                        alias_score = _SCORE_ALIAS_CONTAINS
            if alias_score > 0:
                score += alias_score
                evidence.append("alias")
            for term in intent.get("recallTerms", []):
                term_normalized = _normalize(term)
                if len(term_normalized) >= 2 and term_normalized in normalized:
                    score += _SCORE_RECALL_TERMS
                    evidence.append("graph_term")
                    break
            for term_set in intent.get("recallTermSets", []):
                if all(_normalize(term) in normalized for term in term_set):
                    score += _SCORE_RECALL_TERM_SETS
                    evidence.append("graph_term_set")
                    break
            if menu_key and intent.get("menuKey", "").lower() == menu_key:
                score += _SCORE_PAGE_MENU
                evidence.append("page_menu")
            elif tab and intent.get("domain", "").lower() == tab:
                score += _SCORE_PAGE_TAB
                evidence.append("page_tab")
            if any(_normalize(item) in normalized for item in intent.get("negativeExamples", [])):
                score += _SCORE_NEGATIVE_EXAMPLE
                evidence.append("negative_example")
            if score < _SCORE_DISCARD_THRESHOLD:
                continue
            candidates.append(self._candidate(intent, score=score, evidence=evidence))
        maximum = min(max(int(limit), 1), 8)
        if graph_expand and candidates:
            candidates = self._graph_expand(candidates)
        # Use graph-authored semantic priority as the first-order disambiguator
        # only after its compound term set matched; it never activates from
        # page context or a generic alias alone.
        return sorted(
            candidates,
            key=lambda item: (-item.semantic_priority, -item.score, item.intent_id),
        )[:maximum]

    def _sibling_intents(self, intent_id: str, edge_type: str) -> frozenset[str]:
        """Find sibling intent IDs via 2-hop traversal through *edge_type*.

        Traverses both directions: forward-then-reverse (intent is the
        ``from`` endpoint) and reverse-then-forward (intent is the ``to``
        endpoint), collecting all intent nodes reachable in exactly two
        hops through an intermediate node connected by *edge_type*.
        """
        siblings: set[str] = set()
        # Path A: intent --(forward)--> intermediate --(reverse)--> sibling
        for inter_id in self._adjacency.get(intent_id, {}).get(edge_type, frozenset()):
            for sib in self._reverse_adjacency.get(inter_id, {}).get(edge_type, frozenset()):
                if sib != intent_id:
                    siblings.add(sib)
        # Path B: intent --(reverse)--> intermediate --(forward)--> sibling
        for inter_id in self._reverse_adjacency.get(intent_id, {}).get(edge_type, frozenset()):
            for sib in self._adjacency.get(inter_id, {}).get(edge_type, frozenset()):
                if sib != intent_id:
                    siblings.add(sib)
        return frozenset(siblings)

    def _graph_expand(
        self, candidates: list[IntentCandidate]
    ) -> list[IntentCandidate]:
        """Expand lexical recall candidates via graph traversal.

        For each seed candidate, performs 2-hop BFS through expandable
        edge types to discover sibling intents that were not caught by
        lexical matching.  Expansion scores are derived from the seed's
        lexical score multiplied by an edge-type weight, capped to stay
        below direct compound matches.
        """
        if not candidates:
            return candidates
        existing_ids: set[str] = {c.intent_id for c in candidates}
        expanded: list[IntentCandidate] = []
        total_added = 0
        for seed in candidates:
            if total_added >= _GRAPH_EXPAND_MAX_TOTAL:
                break
            added_for_seed = 0
            for edge_type, weight in _GRAPH_EXPAND_EDGE_WEIGHTS.items():
                if added_for_seed >= _GRAPH_EXPAND_MAX_PER_SEED:
                    break
                for sibling_id in self._sibling_intents(seed.intent_id, edge_type):
                    if added_for_seed >= _GRAPH_EXPAND_MAX_PER_SEED:
                        break
                    if sibling_id in existing_ids:
                        continue
                    intent = self._intents_by_id.get(sibling_id)
                    if intent is None or intent.get("lifecycle", "active") != "active":
                        continue
                    expansion_score = min(
                        int(seed.score * weight), _GRAPH_EXPAND_MAX_SCORE
                    )
                    if expansion_score < _SCORE_DISCARD_THRESHOLD:
                        continue
                    expanded.append(self._candidate(
                        intent,
                        score=expansion_score,
                        evidence=[f"graph_expand:{edge_type}"],
                    ))
                    existing_ids.add(sibling_id)
                    added_for_seed += 1
                    total_added += 1
        return candidates + expanded

    def iter_active_intents(self) -> list[IntentProjection]:
        """Active intent projections in stable id order (semantic index input)."""
        projections = [
            IntentProjection(
                intent_id=str(intent["id"]),
                title=str(intent.get("title") or ""),
                aliases=[str(alias) for alias in intent.get("aliases", [])],
                description=str(intent.get("description") or ""),
            )
            for intent in self._intents
            if intent.get("lifecycle", "active") == "active"
        ]
        projections.sort(key=lambda item: item.intent_id)
        return projections

    def candidate_for_intent(
        self, intent_id: str, *, score: int, evidence: list[str]
    ) -> IntentCandidate:
        intent = self._intents_by_id.get(intent_id)
        if intent is None:
            raise IntentGraphError("FAQ intent binding is missing")
        return self._candidate(intent, score=score, evidence=evidence)

    @property
    def path(self) -> Path:
        """Source graph file location (semantic index sits beside it)."""
        return self._path

    @property
    def action_keys(self) -> frozenset[str]:
        return self._action_keys

    def action_key_for_intent(self, intent_id: str) -> str:
        action_key = self._intent_action_keys.get(intent_id)
        if action_key is None:
            raise IntentGraphError("navigation intent binding is missing")
        return action_key

    def _candidate(
        self, intent: Mapping[str, Any], *, score: int, evidence: list[str]
    ) -> IntentCandidate:
        return IntentCandidate(
            intent_id=intent["id"],
            title=intent["title"],
            action=intent["action"],
            business_object=intent["businessObject"],
            execution_mode=intent["executionMode"],
            risk_level=intent["riskLevel"],
            required_slots=list(intent["requiredSlots"]),
            capability_bindings=list(intent["capabilityBindings"]),
            score=score,
            evidence=list(dict.fromkeys(evidence)),
            rule_version=f"v{self.version}.{self.digest[7:19]}",
            semantic_priority=(
                int(intent.get("semanticPriority", 0))
                if "graph_term_set" in evidence else 0
            ),
            slot_contract=_project_slot_contract(intent.get("slotContract")),
            skill_binding=_project_skill_binding(intent.get("skillBinding")),
            task_policy=_project_task_policy(intent.get("taskPolicy")),
            context_policy=_project_context_policy(intent.get("contextPolicy")),
            response_policy=intent.get("responsePolicy"),
            business_object_id=intent.get("businessObjectId"),
            orchestration_mode=_project_orchestration_mode(
                intent.get("orchestrationMode")
            ),
            lifecycle=intent.get("lifecycle"),
            readiness=intent.get("readiness"),
            fulfillment_kind=intent.get("fulfillmentKind"),
            confusable_intents=tuple(intent.get("confusableIntents") or ()),
            disambiguation_hints=(
                str(intent["disambiguationHints"])
                if intent.get("disambiguationHints") else None
            ),
        )

    @staticmethod
    def _action_key(intent: Mapping[str, Any]) -> str:
        source = intent.get("source")
        if not isinstance(source, Mapping):
            raise IntentGraphError("navigation intent binding is missing")
        parts = (source.get("channel"), source.get("menuId"), source.get("actionId"))
        if any(not isinstance(item, str) or not item for item in parts):
            raise IntentGraphError("navigation intent binding is missing")
        return ".".join(parts)

    @staticmethod
    def validate_capability_bindings(
        candidates: list[IntentCandidate], available_capability_ids: set[str]
    ) -> None:
        missing = sorted(
            {
                capability_id
                for candidate in candidates
                for capability_id in candidate.capability_bindings
                if capability_id not in available_capability_ids
            }
        )
        if missing:
            raise IntentGraphError("runtime capability binding missing: " + ",".join(missing))


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _project_slot_contract(value: Any) -> IntentSlotContract | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise IntentGraphError("intent graph slot contract is invalid")
    try:
        slots = tuple(
            SlotDefinition(
                slot_id=item["slotId"],
                name=item["name"],
                label=item["label"],
                type=item["type"],
                required=item["required"],
                extraction_keys=tuple(item["extractionKeys"]),
                resolved_field=item["resolvedField"],
                resolver_capability_id=item.get("resolverCapabilityId"),
                normalizer=item.get("normalizer"),
                default_value=item.get("defaultValue"),
            )
            for item in value["slots"]
        )
        binding = value["executionBinding"]
        return IntentSlotContract(
            version=value["version"],
            slots=slots,
            clarification_order=tuple(value["clarificationOrder"]),
            execution_binding=ExecutionBinding(
                capability_id=binding["capabilityId"],
                requires_confirmation=binding["requiresConfirmation"],
            ),
        )
    except (KeyError, TypeError) as exc:
        raise IntentGraphError("intent graph slot contract is invalid") from exc


def _project_skill_binding(value: Any) -> SkillBinding | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise IntentGraphError("intent graph skill binding is invalid")
    try:
        return SkillBinding(
            skill_id=value["skillId"], version=value["version"], digest=value["digest"]
        )
    except (KeyError, TypeError) as exc:
        raise IntentGraphError("intent graph skill binding is invalid") from exc


def _project_task_policy(value: Any) -> TaskPolicy | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise IntentGraphError("intent graph task policy is invalid")
    try:
        return TaskPolicy(
            supported_shapes=tuple(value["supportedShapes"]),
            mode=value["mode"],
            max_tasks=int(value["maxTasks"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise IntentGraphError("intent graph task policy is invalid") from exc


def _project_orchestration_mode(value: Any) -> str | None:
    """V4.1 → V2 适配器：将 V4.1 的 single/parallel/sequential 映射到 V2 runtime 可接受的 deterministic/agent。"""
    if value is None:
        return None
    # V4.1 orchestration_mode → V2 runtime orchestration_mode
    v41_to_v2 = {
        "single": "deterministic",
        "parallel": "deterministic",  # 并行由 taskPolicy.maxTasks 控制，orchestration_mode 仍为 deterministic
        "parallel-safe": "deterministic",  # TuGraph 数据中使用的变体，语义等同 parallel
        "sequential": "deterministic",
        "deterministic": "deterministic",
        "agent": "agent",
    }
    mapped = v41_to_v2.get(value)
    if mapped is None:
        raise IntentGraphError("intent graph orchestration mode is invalid")
    return mapped


def _validate_graph(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise IntentGraphError("intent graph schema is invalid")
    required = {
        "schemaVersion", "graphId", "version", "bank", "appSurface",
        "publishedAt", "nodes", "edges", "digest",
    }
    if not required.issubset(set(payload)):
        raise IntentGraphError("intent graph schema is invalid")
    if payload["schemaVersion"] != "2.0":
        raise IntentGraphError("intent graph schema version is invalid")
    if not isinstance(payload["nodes"], list) or not isinstance(payload["edges"], list):
        raise IntentGraphError("intent graph schema is invalid")
    copy = json.loads(json.dumps(payload, ensure_ascii=False))
    supplied = copy.pop("digest", "")
    canonical = json.dumps(copy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if supplied != expected:
        raise IntentGraphError("intent graph digest is invalid")
    node_ids: set[str] = set()
    menu_count = 0
    for node in payload["nodes"]:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            raise IntentGraphError("intent graph node schema is invalid")
        if node["id"] in node_ids:
            raise IntentGraphError("intent graph node id is duplicated")
        node_ids.add(node["id"])
        if node.get("type") == "menu":
            menu_count += 1
        if node.get("type") == "intent":
            for key in (
                "title", "action", "businessObject", "executionMode", "riskLevel",
                "requiredSlots", "capabilityBindings", "aliases", "recallTerms",
                "recallTermSets", "negativeExamples",
                "semanticPriority",
            ):
                if key not in node:
                    raise IntentGraphError("intent graph intent schema is invalid")
            if not isinstance(node["title"], str) or not 1 <= len(node["title"]) <= 80:
                raise IntentGraphError("intent graph intent title is invalid")
            if (
                not isinstance(node["semanticPriority"], int)
                or isinstance(node["semanticPriority"], bool)
                or not 0 <= node["semanticPriority"] <= 100
            ):
                raise IntentGraphError("intent graph semantic priority is invalid")
            if node["executionMode"] == "knowledge-only" and (
                node["requiredSlots"] != [] or node["capabilityBindings"] != []
            ):
                raise IntentGraphError("knowledge-only intent isolation is invalid")
            if node.get("readiness") == "ready":
                if node["executionMode"] == "read-execute":
                    # 纯查询直接绑定 capability 走 MCP，不要求 skill/slot 契约。
                    if not node.get("capabilityBindings"):
                        raise IntentGraphError("read-execute intent lacks capability binding")
                elif node["executionMode"] != "knowledge-only":
                    # knowledge-only 就绪意图按上方隔离规则本就不携带执行
                    # 契约（无槽位/无绑定/无技能），执行契约键只对执行型
                    # 就绪意图强制；对其要求 skillBinding 等键与隔离不变
                    # 量自相矛盾，会把整个知识面误判为 schema 非法。
                    for key in (
                        "skillBinding", "slotContract", "businessObjectId",
                        "taskPolicy", "contextPolicy", "responsePolicy",
                    ):
                        if key not in node:
                            raise IntentGraphError("ready intent contract is incomplete")
    if menu_count < 1:
        raise IntentGraphError("intent graph has no menu nodes")
    for edge in payload["edges"]:
        if edge.get("from") not in node_ids or edge.get("to") not in node_ids:
            raise IntentGraphError("intent graph edge binding is invalid")
