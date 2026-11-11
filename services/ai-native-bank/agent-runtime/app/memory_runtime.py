"""Governed memory adapter for the bank Agent Runtime.

Intent policy is server-owned and deny-by-default. Models receive only the
bounded context pack returned by the memory control plane; they never choose a
customer scope or write directly to the memory store.
"""

from __future__ import annotations

import json
import logging
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx

from app.trace import _is_safe_trace_text


logger = logging.getLogger(__name__)

MEMORY_TYPES = {"user_profile", "semantic_memory", "episodic_memory", "variable", "summary", "unknown"}
# Trace 契约对外使用 UI 词表（Node 网关 agent-trace 校验同表）。OpenJiuwen
# 原生 memoryType 在投影边界翻译；无对应展示类型的记忆按隐藏处理，避免
# 整条事件流被下游契约校验拒绝。
TRACE_MEMORY_KINDS = {
    "user_profile": "semantic",
    "semantic_memory": "semantic",
    "episodic_memory": "episodic",
    "summary": "episodic",
    "variable": "working",
}
SENSITIVITIES = {"L0", "L1", "L2", "L3"}
SAFE_ID = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SOURCE_LABELS = {
    "user_explicit": "用户明确设置",
    "canonical_run": "成功业务运行",
    "agent_observation": "智能体观察",
    "model_inference": "模型推断",
    "knowledge_graph": "知识图谱事实",
}
PURPOSE_LABELS = {
    "service_personalization": "个性化偏好",
    "service_continuity": "服务连续性",
    "monthly_fund_planning": "本月资金安排",
    "cashflow_explanation": "资金变化解释",
    "wealth_guidance": "理财服务偏好",
    "consent_governance": "授权与同意管理",
}
OMISSION_LABELS = {
    "expired": "已过期记忆",
    "revoked": "已撤销记忆",
    "rejected": "未通过治理",
    "contested": "存在争议",
    "purpose": "用途不匹配",
    "sensitivity": "超出敏感级别",
    "temporal": "时效不匹配",
    "policy": "策略过滤",
    "tokenBudget": "超出召回预算",
}
RESTRICTED_MEMORY = "受限记忆"


def _safe_memory_default_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -(2**53) < value < 2**53
    if isinstance(value, str):
        return _safe_trace_text(value, 128) is not None
    return False


class MemoryGatewayError(RuntimeError):
    """A value-free memory dependency failure."""


RecallOutcome = Literal["applied", "empty", "not_applicable", "degraded"]


@dataclass(frozen=True)
class MemoryRecallResult:
    """Bounded recall outcome shared by both recall stages.

    Context packs remain internal Agent input. ``trace_detail`` is the only
    public projection and never copies raw values or internal references.
    """

    outcome: RecallOutcome
    context_packs: tuple[dict[str, Any], ...] = ()
    fact_count: int = 0
    conflict_count: int = 0
    omission_count: int = 0
    requires_core_refresh_count: int = 0
    requires_confirmation_count: int = 0
    policy_version: str | None = None

    def trace_metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "outcome": self.outcome,
            "contextPackCount": len(self.context_packs),
            "factCount": self.fact_count,
            "conflictCount": self.conflict_count,
            "omissionCount": self.omission_count,
            "requiresCoreRefreshCount": self.requires_core_refresh_count,
            "requiresConfirmationCount": self.requires_confirmation_count,
        }
        if self.policy_version is not None:
            metrics["policyVersion"] = self.policy_version
        return metrics

    def trace_detail(self, *, scope: Literal["user", "task"]) -> dict[str, Any]:
        if scope not in {"user", "task"}:
            raise ValueError("memory recall trace scope invalid")
        if self.outcome == "degraded":
            return _empty_trace_detail(scope, "degraded")
        if self.outcome in {"empty", "not_applicable"}:
            return _empty_trace_detail(scope, "empty")

        groups: list[dict[str, Any]] = []
        conflict_count = 0
        omissions: dict[str, int] = {}
        hidden_count = 0
        visible_count = 0
        duplicate_count = 0
        seen_signatures: set[str] = set()
        for index, wrapper in enumerate(self.context_packs[:8], start=1):
            pack = wrapper.get("contextPack")
            if not isinstance(pack, dict):
                hidden_count += 1
                continue
            if scope == "user":
                key = str(wrapper.get("purpose") or f"user_{index}")
                label = PURPOSE_LABELS.get(key, "用户记忆")
            else:
                key = f"task_{index}"
                label = wrapper.get("intentLabel") or f"任务记忆 {index}"
            safe_key = key if SAFE_ID.fullmatch(key) and len(key) <= 64 else f"group_{index}"
            safe_label = _safe_trace_text(label, 80)
            facts = pack.get("facts", [])
            if not isinstance(facts, list):
                hidden_count += 1
                continue
            if not safe_label:
                hidden_count += len(facts)
                continue
            items: list[dict[str, Any]] = []
            for fact in facts[:20 - visible_count]:
                item = _project_trace_fact(fact)
                if item is None:
                    hidden_count += 1
                    continue
                sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if sig in seen_signatures:
                    duplicate_count += 1
                    continue
                seen_signatures.add(sig)
                items.append(item)
                visible_count += 1
            if items:
                groups.append({"key": safe_key, "label": safe_label, "items": items})
            conflict_count += min(len(pack.get("conflicts", [])), 12 - conflict_count)
            raw_omissions = pack.get("omissions", {})
            if isinstance(raw_omissions, dict):
                for reason in OMISSION_LABELS:
                    count = raw_omissions.get(reason, 0)
                    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                        omissions[reason] = omissions.get(reason, 0) + count

        hidden_count = max(hidden_count, self.fact_count - visible_count - duplicate_count)
        notices = [
            {"label": OMISSION_LABELS[key], "count": count}
            for key, count in omissions.items()
        ][:9]
        if hidden_count:
            notices.append({"label": "展示策略隐藏", "count": hidden_count})
        detail_status = "available" if visible_count else "redacted"
        return {
            "schemaVersion": "1.0",
            "scope": scope,
            "detailStatus": detail_status,
            "groups": groups,
            "conflicts": (
                [{"label": "存在争议记忆", "count": conflict_count}]
                if conflict_count else []
            ),
            "omissions": notices,
        }


def _empty_trace_detail(scope: str, status: str) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "scope": scope,
        "detailStatus": status,
        "groups": [],
        "conflicts": [],
        "omissions": [],
    }


def _safe_trace_text(value: Any, max_length: int) -> str | None:
    """Delegate to the single authoritative validator in trace.py.

    Memory fact display values come from the trusted memory-control service
    and may legitimately contain long English identifiers (predicate names,
    preference keys). Use strict=False to skip URL-like-text detection and
    long-credential check, mirroring the frontend's allowedLongId flexibility.
    """
    if not isinstance(value, str):
        return None
    return _is_safe_trace_text(value, max_length=max_length, strict=False)


def _project_trace_fact(fact: Any) -> dict[str, Any] | None:
    if not isinstance(fact, dict):
        return None
    label = _safe_trace_text(fact.get("safeLabel", "记忆事实"), 80)
    # 优先使用 safeValueSummary，回退到 content（OpenJiuwen 原生内容）
    raw_summary = fact.get("safeValueSummary")
    if raw_summary is None:
        raw_summary = fact.get("content", "")
    display_value = _safe_trace_text(raw_summary, 160)
    memory_kind = fact.get("memoryType")
    source_label = SOURCE_LABELS.get(fact.get("sourceAuthority", "agent_observation"))
    valid_from = fact.get("validFrom")
    if (
        not label or not display_value
        or label == RESTRICTED_MEMORY or display_value == RESTRICTED_MEMORY
        or memory_kind not in MEMORY_TYPES or source_label is None
        or not HttpMemoryGateway._is_datetime(valid_from)
    ):
        return None
    trace_kind = TRACE_MEMORY_KINDS.get(memory_kind)
    if trace_kind is None:
        return None
    controls = ["current"]
    if fact.get("requiresCoreRefresh") is True:
        controls.append("requires_core_refresh")
    if fact.get("requiresConfirmation") is True:
        controls.append("requires_confirmation")
    return {
        "label": label,
        "displayValue": display_value,
        "memoryKind": trace_kind,
        "sourceLabel": source_label,
        "validFrom": valid_from,
        "applicationStatus": "applied",
        "controls": controls,
    }


class UserMemoryPolicyRegistry:
    """Purpose-bound user-stage recall and preference-candidate allowlist."""

    _READ_PURPOSES = {
        "service_personalization": {
            "memoryTypes": {"user_profile", "semantic_memory"},
            "sensitivityCeilings": {"L0", "L1"},
        },
        "service_continuity": {
            "memoryTypes": {"episodic_memory", "summary"},
            "sensitivityCeilings": {"L0", "L1", "L2"},
        },
        "wealth_guidance": {
            "memoryTypes": {"user_profile", "semantic_memory", "summary"},
            "sensitivityCeilings": {"L0", "L1", "L2"},
        },
        "session_context": {
            "memoryTypes": {"variable"},
            "sensitivityCeilings": {"L0", "L1"},
        },
    }
    # 可写谓词 → 允许的候选 purpose（镜像 control-plane allowedPurposes，
    # 加载期即拒绝策略 JSON 与控制面漂移）。
    _WRITABLE_PREDICATE_PURPOSES = {
        "preferred_language": {"service_personalization"},
        "response_style": {"service_personalization"},
        "recommendation_opt_out": {"service_personalization"},
        "monthly_living_budget": {"wealth_guidance"},
        "emergency_reserve_preference": {"wealth_guidance"},
        "spending_explanation_preference": {"service_personalization"},
        "investment_goal_preference": {"wealth_guidance"},
        "investment_horizon_months": {"wealth_guidance"},
        "risk_preference": {"wealth_guidance"},
        "preferred_wealth_product_type": {"wealth_guidance"},
        "monthly_spending_pattern": {"wealth_guidance"},
        "salary_allocation_habit": {"wealth_guidance"},
        "frequent_operation_pattern": {"service_personalization"},
        "preferred_service_channel": {"service_personalization"},
        "preferred_repayment_timing": {"service_personalization"},
        "utility_payment_habit": {"service_personalization"},
        "points_usage_preference": {"service_personalization"},
        "dislikes_high_risk_investments": {"wealth_guidance"},
        "dislikes_steady_growth_products": {"wealth_guidance"},
        "dislikes_steady_growth_wealth_products": {"wealth_guidance"},
    }

    def __init__(self, document: dict[str, Any]):
        expected = {"schemaVersion", "policyVersion", "defaults", "reads", "writeHints"}
        consents = document.get("consents")
        if consents is not None:
            expected.add("consents")
        if set(document) != expected:
            raise ValueError("invalid user memory policy shape")
        if document.get("schemaVersion") != "1.0":
            raise ValueError("unsupported user memory policy schema")
        if not SAFE_ID.fullmatch(str(document.get("policyVersion", ""))):
            raise ValueError("invalid user memory policy version")
        defaults = document.get("defaults")
        if not isinstance(defaults, dict) or defaults.get("read") is not None:
            raise ValueError("user memory read policy must deny by default")
        default_write_hint = defaults.get("writeHint")
        if default_write_hint is not None:
            self._validate_write_hint_shape(default_write_hint)

        reads = document.get("reads")
        if not isinstance(reads, dict) or set(reads) - set(self._READ_PURPOSES):
            raise ValueError("invalid user memory read purposes")
        # session_context (variable) 走独立 KV 路径，不纳入 text-pack 预算
        total_items = 0
        total_tokens = 0
        for purpose, policy in reads.items():
            self._validate_read_policy(purpose, policy)
            if purpose == "session_context":
                continue
            total_items += policy["maxItems"]
            total_tokens += policy["maxTokens"]
        if total_items > 15 or total_tokens > 1500:
            raise ValueError("user memory recall budget exceeded")

        write_hints = document.get("writeHints")
        if not isinstance(write_hints, dict) or not write_hints:
            raise ValueError("invalid user memory write hints")
        for predicate, policy in write_hints.items():
            self._validate_write_hint(predicate, policy)
        if consents is not None:
            self._validate_consents(consents)
        self._document = document

    @classmethod
    def load(cls, path: str | Path) -> "UserMemoryPolicyRegistry":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def version(self) -> str:
        return self._document["policyVersion"]

    def read_policy(self, purpose: str) -> dict[str, Any] | None:
        policy = self._document["reads"].get(purpose)
        return json.loads(json.dumps(policy)) if policy else None

    def write_hint(self, predicate: str) -> dict[str, Any] | None:
        policy = self._document["writeHints"].get(predicate)
        if policy is None:
            default = self._document.get("defaults", {}).get("writeHint")
            return json.loads(json.dumps(default)) if default else None
        return json.loads(json.dumps(policy))

    @classmethod
    def _validate_read_policy(cls, purpose: str, policy: Any) -> None:
        # queryHint 是可选的语义融合字段，用于增强向量检索
        required_fields = {"memoryTypes", "predicates", "maxItems", "maxTokens", "sensitivityCeiling"}
        if not isinstance(policy, dict) or not required_fields.issubset(set(policy)):
            raise ValueError("invalid user memory read policy shape")
        # 允许 queryHint 作为可选字段，但不容许其他未知字段
        allowed_fields = required_fields | {"queryHint"}
        if not set(policy).issubset(allowed_fields):
            raise ValueError("invalid user memory read policy shape")
        binding = cls._READ_PURPOSES[purpose]
        memory_types = policy["memoryTypes"]
        if not isinstance(memory_types, list) or not memory_types or (
            len(memory_types) != len(set(memory_types))
            or not set(memory_types).issubset(binding["memoryTypes"])
        ):
            raise ValueError("invalid user memory types")
        predicates = policy["predicates"]
        # 语义融合模式：predicates 可以为空列表（依赖 queryHint + 向量搜索）
        # 也可以保留谓词作为可选提示（不作为硬过滤白名单）
        if not isinstance(predicates, list) or len(predicates) > 30 or (
            len(predicates) != len(set(predicates))
            or not all(SAFE_ID.fullmatch(str(item)) for item in predicates)
        ):
            raise ValueError("invalid user memory predicates")
        # queryHint 校验：非空字符串
        hint = policy.get("queryHint")
        if hint is not None and (not isinstance(hint, str) or not hint.strip() or len(hint) > 200):
            raise ValueError("invalid user memory queryHint")
        # session_context 允许更多条目（KV 对象，非 text-pack）
        max_items_limit = 20 if purpose == "session_context" else 6
        if not isinstance(policy["maxItems"], int) or isinstance(policy["maxItems"], bool) or not 1 <= policy["maxItems"] <= max_items_limit:
            raise ValueError("invalid user memory item budget")
        if not isinstance(policy["maxTokens"], int) or isinstance(policy["maxTokens"], bool) or not 64 <= policy["maxTokens"] <= 560:
            raise ValueError("invalid user memory token budget")
        if policy["sensitivityCeiling"] not in binding["sensitivityCeilings"]:
            raise ValueError("invalid user memory sensitivity ceiling")

    @classmethod
    def _validate_write_hint_shape(cls, policy: Any) -> None:
        if not isinstance(policy, dict) or set(policy) != {
            "candidateType", "purpose", "requiresConsent"
        }:
            raise ValueError("invalid user memory write hint shape")
        if (
            policy["candidateType"] != "user_preference"
            or policy["requiresConsent"] is not True
        ):
            raise ValueError("invalid user memory write hint binding")

    @classmethod
    def _validate_write_hint(cls, predicate: str, policy: Any) -> None:
        cls._validate_write_hint_shape(policy)
        # 已注册谓词校验 purpose 绑定；未注册谓词接受任意 purpose
        if predicate in cls._WRITABLE_PREDICATE_PURPOSES:
            if policy["purpose"] not in cls._WRITABLE_PREDICATE_PURPOSES[predicate]:
                raise ValueError("invalid user memory write hint binding")

    @classmethod
    def _validate_consents(cls, consents: Any) -> None:
        if not isinstance(consents, dict) or not consents:
            raise ValueError("invalid user memory consent kinds")
        covered: set[str] = set()
        for kind, policy in consents.items():
            if (
                not SAFE_ID.fullmatch(str(kind))
                or not isinstance(policy, dict)
                or set(policy) != {"predicates"}
            ):
                raise ValueError("invalid user memory consent kind shape")
            predicates = policy["predicates"]
            if not isinstance(predicates, list) or not predicates or (
                len(predicates) != len(set(predicates))
                or not all(SAFE_ID.fullmatch(str(item)) for item in predicates)
            ):
                raise ValueError("invalid user memory consent predicates")
            overlap = covered.intersection(predicates)
            if overlap:
                raise ValueError(
                    f"predicate governed by multiple consent kinds: {sorted(overlap)}"
                )
            covered.update(predicates)

    def consent_predicates(self, kind: str) -> tuple[str, ...]:
        policy = self._document.get("consents", {}).get(kind)
        return tuple(policy["predicates"]) if policy else ()


class IntentMemoryPolicyRegistry:
    def __init__(self, document: dict[str, Any]):
        if document.get("schemaVersion") != "1.0":
            raise ValueError("unsupported intent memory policy schema")
        if not SAFE_ID.fullmatch(str(document.get("policyVersion", ""))):
            raise ValueError("invalid intent memory policy version")
        defaults = document.get("defaults")
        if not isinstance(defaults, dict) or defaults != {"read": None, "write": None}:
            raise ValueError("intent memory policies must deny by default")
        default_read = document.get("defaultRead")
        if default_read is not None:
            self._validate_read_shape(default_read)
        self._default_read = default_read
        domain_patterns = document.get("domainPatterns")
        if domain_patterns is not None:
            if not isinstance(domain_patterns, dict) or len(domain_patterns) > 20:
                raise ValueError("invalid domainPatterns map")
            for prefix, policy in domain_patterns.items():
                self._validate_read_shape(policy)
        self._domain_patterns = domain_patterns or {}
        intents = document.get("intents")
        if not isinstance(intents, dict) or len(intents) > 299:
            raise ValueError("invalid intent memory policy map")
        for intent_id, policy in intents.items():
            self._validate_intent_policy(intent_id, policy)
        self._document = document

    @classmethod
    def load(cls, path: str | Path) -> "IntentMemoryPolicyRegistry":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def version(self) -> str:
        return self._document["policyVersion"]

    def read_policy(self, intent_id: str) -> dict[str, Any] | None:
        policy = self._document["intents"].get(intent_id, {}).get("read")
        if policy:
            return json.loads(json.dumps(policy))
        for prefix, pattern_policy in self._domain_patterns.items():
            if intent_id.startswith(prefix):
                return json.loads(json.dumps(pattern_policy))
        if self._default_read is not None:
            return json.loads(json.dumps(self._default_read))
        return None

    def capability_input_defaults(
        self, intent_id: str, context_pack: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Project policy-declared preference facts into optional tool inputs.

        The mapping is registry-owned, values must already have survived the
        governed memory query, only advisory-usage facts are eligible (so
        context_only / display_only memories can never become tool inputs),
        and only bounded scalar values are projected. Current-turn tool
        arguments remain authoritative and may override these defaults at
        invocation time.
        """
        policy = self.read_policy(intent_id)
        if policy is None or not isinstance(context_pack, dict):
            return {}
        bindings = policy.get("capabilityInputBindings", {})
        facts = context_pack.get("facts", [])
        if not isinstance(bindings, dict) or not isinstance(facts, list):
            return {}
        values = {
            item.get("predicate"): item.get("value")
            for item in facts
            if isinstance(item, dict)
            and isinstance(item.get("predicate"), str)
            and str(item.get("usage", "")).startswith("advisory")
            and _safe_memory_default_value(item.get("value"))
        }
        projected: dict[str, dict[str, Any]] = {}
        for capability_id, mapping in bindings.items():
            capability_defaults = {
                input_field: values[predicate]
                for predicate, input_field in mapping.items()
                if predicate in values
            }
            if capability_defaults:
                projected[capability_id] = capability_defaults
        return projected

    def write_policy(self, intent_id: str, status: str) -> dict[str, Any] | None:
        policy = self._document["intents"].get(intent_id, {}).get("write")
        if not policy or status not in policy["statuses"]:
            return None
        return json.loads(json.dumps(policy))

    @staticmethod
    def _validate_read_shape(read: Any) -> None:
        required = {
            "purpose", "memoryTypes", "predicates", "maxItems", "maxTokens",
            "sensitivityCeiling",
        }
        optional = {"capabilityInputBindings", "graphQueryKeywords", "queryHint"}
        if not required.issubset(read) or set(read) - required - optional:
            raise ValueError("invalid memory read policy shape")
        if not SAFE_ID.fullmatch(str(read["purpose"])):
            raise ValueError("invalid memory purpose")
        if not isinstance(read["memoryTypes"], list) or not (
            1 <= len(read["memoryTypes"]) <= 6
            and len(set(read["memoryTypes"])) == len(read["memoryTypes"])
            and set(read["memoryTypes"]).issubset(MEMORY_TYPES)
        ):
            raise ValueError("invalid memory types")
        if not isinstance(read["predicates"], list) or not (
            len(read["predicates"]) <= 30
            and len(set(read["predicates"])) == len(read["predicates"])
            and all(SAFE_ID.fullmatch(str(item)) for item in read["predicates"])
        ):
            raise ValueError("invalid memory predicates")
        if not isinstance(read["maxItems"], int) or not 1 <= read["maxItems"] <= 20:
            raise ValueError("invalid memory item budget")
        if not isinstance(read["maxTokens"], int) or not 64 <= read["maxTokens"] <= 2000:
            raise ValueError("invalid memory token budget")
        if read["sensitivityCeiling"] not in SENSITIVITIES:
            raise ValueError("invalid memory sensitivity ceiling")
        bindings = read.get("capabilityInputBindings", {})
        if not isinstance(bindings, dict) or len(bindings) > 12:
            raise ValueError("invalid memory capability input bindings")
        for capability_id, mapping in bindings.items():
            if (
                not SAFE_ID.fullmatch(str(capability_id))
                or not str(capability_id).startswith("bank.")
                or not isinstance(mapping, dict)
                or not 1 <= len(mapping) <= 12
            ):
                raise ValueError("invalid memory capability input bindings")
            for predicate, input_field in mapping.items():
                if (
                    (read["predicates"] and predicate not in read["predicates"])
                    or not SAFE_ID.fullmatch(str(input_field))
                ):
                    raise ValueError("invalid memory capability input bindings")
        graph_keywords = read.get("graphQueryKeywords")
        if graph_keywords is not None:
            if (
                not isinstance(graph_keywords, list)
                or not 1 <= len(graph_keywords) <= 6
                or not all(
                    isinstance(k, str) and 1 <= len(k) <= 20
                    for k in graph_keywords
                )
            ):
                raise ValueError("invalid graph query keywords")
        hint = read.get("queryHint")
        if hint is not None and (
            not isinstance(hint, str) or not hint.strip() or len(hint) > 200
        ):
            raise ValueError("invalid memory queryHint")

    @staticmethod
    def _validate_intent_policy(intent_id: str, policy: Any) -> None:
        if not SAFE_ID.fullmatch(intent_id) or not isinstance(policy, dict):
            raise ValueError("invalid intent memory policy")
        if set(policy) - {"read", "write"}:
            raise ValueError("unknown intent memory policy field")
        read = policy.get("read")
        if read is not None:
            IntentMemoryPolicyRegistry._validate_read_shape(read)
        write = policy.get("write")
        if write is not None:
            required = {"statuses", "candidateType", "predicate", "purpose"}
            if set(write) != required:
                raise ValueError("invalid memory write policy shape")
            if write["statuses"] != ["succeeded"]:
                raise ValueError("only successful runs may write memory")
            if write["candidateType"] != "canonical_service_episode":
                raise ValueError("runtime may only append canonical episodes")
            if not all(SAFE_ID.fullmatch(str(write[key])) for key in ("predicate", "purpose")):
                raise ValueError("invalid memory write binding")


class KnowledgeGraphGateway:
    """知识图谱检索网关 — 调用 ai-harbor /api/knowledge-qa/graph/* API。

    非关键路径：任何异常均向上传播为空结果，不影响 LTM 向量检索。
    """

    def __init__(
        self,
        *,
        base_url: str,
        graph_id: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("knowledge graph base URL is invalid")
        self._base_url = base_url.rstrip("/")
        self._graph_id = graph_id
        self._client = client or httpx.AsyncClient()
        self._timeout = max(0.5, min(float(timeout_seconds), 8.0))

    async def search_entities(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """实体搜索 — POST /api/knowledge-qa/tools/search_entities"""
        resp = await self._client.post(
            f"{self._base_url}/api/knowledge-qa/tools/search_entities",
            json={"graphId": self._graph_id, "query": query, "topK": top_k},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("entities", []) if isinstance(data, dict) else []

    async def expand_subgraph(self, node_ids: list[str], hops: int = 2) -> dict[str, Any]:
        """子图扩展 — POST /api/knowledge-qa/tools/expand_subgraph"""
        resp = await self._client.post(
            f"{self._base_url}/api/knowledge-qa/tools/expand_subgraph",
            json={"graphId": self._graph_id, "nodeIds": node_ids, "hops": hops},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def query_relations(self, node_id: str) -> list[dict[str, Any]]:
        """关系查询 — POST /api/knowledge-qa/tools/query_relations"""
        resp = await self._client.post(
            f"{self._base_url}/api/knowledge-qa/tools/query_relations",
            json={
                "graphId": self._graph_id,
                "nodeId": node_id,
                "direction": "both",
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("relations", []) if isinstance(data, dict) else []


class HttpMemoryGateway:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        policies: IntentMemoryPolicyRegistry,
        user_policies: UserMemoryPolicyRegistry | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 5.0,
        clock: Callable[[], datetime] | None = None,
        mode: str = "governed_write",
        user_mode: str | None = None,
        kg_gateway: KnowledgeGraphGateway | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("memory control URL is invalid")
        if not 16 <= len(service_token) <= 512 or any(char.isspace() for char in service_token):
            raise ValueError("memory service token is invalid")
        if mode not in {"off", "shadow", "read_only", "governed_write"}:
            raise ValueError("memory mode is invalid")
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._policies = policies
        self._user_policies = user_policies
        self._client = client or httpx.AsyncClient()
        self._timeout_seconds = max(0.2, min(float(timeout_seconds), 10.0))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._mode = mode
        self._user_mode = mode if user_mode is None else user_mode
        if self._user_mode not in {"off", "shadow", "read_only", "governed_write"}:
            raise ValueError("user memory mode is invalid")
        self._kg_gateway = kg_gateway

    async def recall_user_context(
        self, *, tenant_id: str, customer_id: str, agent_domain: str,
        agent_run_id: str, query: str | None = None,
        intent_id: str | None = None,
    ) -> MemoryRecallResult:
        if self._user_mode == "off" or self._user_policies is None:
            return MemoryRecallResult(outcome="not_applicable")
        # 根据 intent_id 从意图策略获取 purpose 和检索参数，实现意图感知的记忆召回；
        # 无 intent_id 时回退到遍历所有已配置 purpose（保持向后兼容）。
        if intent_id is not None:
            intent_policy = self._policies.read_policy(intent_id)
            if intent_policy is None:
                return MemoryRecallResult(
                    outcome="not_applicable",
                    policy_version=self._user_policies.version,
                )
            purpose = intent_policy["purpose"]
            # 优先使用意图策略的检索参数（memoryTypes/queryHint/maxItems 等），
            # 同时用用户策略校验 sensitivityCeiling 上限，确保安全边界不被突破。
            user_policy = self._user_policies.read_policy(purpose)
            policy = dict(intent_policy)
            if user_policy is not None:
                user_ceiling = user_policy.get("sensitivityCeiling")
                if user_ceiling is not None:
                    policy["sensitivityCeiling"] = user_ceiling
            policies_to_query = [(purpose, policy)]
        else:
            policies_to_query = [
                (p, self._user_policies.read_policy(p))
                for p in ("service_personalization", "service_continuity", "wealth_guidance")
                if self._user_policies.read_policy(p) is not None
            ]
        async def _recall_one(
            purpose: str, policy: dict[str, Any],
        ) -> dict[str, Any] | None:
            task_id = f"user-context:{agent_run_id}:{purpose}"
            try:
                pack = await self._query_context(
                    tenant_id=tenant_id, customer_id=customer_id,
                    agent_domain=agent_domain, task_id=task_id,
                    purpose=purpose, policy=policy, query=query,
                )
                return {"purpose": purpose, "contextPack": pack}
            except MemoryGatewayError:
                logger.warning("memory recall failed for purpose=%s", purpose, exc_info=True)
                return None

        valid_policies = [
            (p, pol) for p, pol in policies_to_query if pol is not None
        ]
        results = await asyncio.gather(
            *[_recall_one(p, pol) for p, pol in valid_policies]
        )
        packs: list[dict[str, Any]] = [r for r in results if r is not None]
        if not packs:
            return MemoryRecallResult(
                outcome="degraded", policy_version=self._user_policies.version
            )
        if self._user_mode == "shadow":
            return MemoryRecallResult(
                outcome="not_applicable", policy_version=self._user_policies.version
            )
        return _summarize_recall(packs, self._user_policies.version)

    async def recall(
        self, *, tenant_id: str, customer_id: str, agent_domain: str,
        task_id: str, intent_id: str, query: str | None = None,
    ) -> dict[str, Any] | None:
        if self._mode == "off":
            return None
        policy = self._policies.read_policy(intent_id)
        if policy is None:
            return None
        pack = await self._query_context(
            tenant_id=tenant_id, customer_id=customer_id,
            agent_domain=agent_domain, task_id=task_id,
            purpose=policy["purpose"], policy=policy, query=query,
        )
        return pack

    async def recall_variables(
        self, *, tenant_id: str, customer_id: str, agent_domain: str,
        agent_run_id: str,
    ) -> dict[str, str]:
        """从 memory-control 获取用户工作记忆变量（LTM variable store）。

        工作记忆是跨轮次的会话级全局变量，如当前选中的账户、最近查询的日期等。
        """
        if self._user_mode == "off":
            return {}
        payload = {
            "schemaVersion": "1.0",
            "taskId": f"variables:{agent_run_id}",
            "scope": {
                "tenantId": tenant_id,
                "customerId": customer_id,
                "agentDomain": agent_domain,
                "memorySpace": "customer-service",
                "purpose": "session_context",
            },
        }
        try:
            result = await self._post("/v1/variables:query", payload)
        except MemoryGatewayError:
            return {}
        return result.get("variables", {})

    async def submit_variables(
        self, *, tenant_id: str, customer_id: str, agent_domain: str,
        agent_run_id: str, variables: dict[str, str],
    ) -> dict[str, Any]:
        """写入工作记忆变量到 memory-control（LTM variable store）。

        在 agent-run 终端记忆记录阶段调用，将本轮会话提取的关键变量持久化。
        """
        if self._user_mode == "off" or not variables:
            return {"status": "skipped"}
        payload = {
            "schemaVersion": "1.0",
            "scope": {
                "tenantId": tenant_id,
                "customerId": customer_id,
                "agentDomain": agent_domain,
                "memorySpace": "customer-service",
                "purpose": "session_context",
            },
            "variables": variables,
            "sourceAgentRunId": agent_run_id,
        }
        try:
            result = await self._post("/v1/variables:submit", payload)
        except MemoryGatewayError:
            return {"status": "error"}
        return result

    @property
    def task_policy_version(self) -> str:
        return self._policies.version

    def capability_input_defaults(
        self, intent_id: str, context_pack: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        return self._policies.capability_input_defaults(intent_id, context_pack)

    async def _query_context(
        self, *, tenant_id: str, customer_id: str, agent_domain: str,
        task_id: str, purpose: str, policy: dict[str, Any],
        query: str | None = None,
    ) -> dict[str, Any]:
        # 语义融合：将 queryHint 与用户 query 组合，增强向量检索的语义匹配
        # queryHint 提供 purpose 级别的语义上下文，使得即使无用户 query 也能召回相关记忆
        hint = policy.get("queryHint", "")
        if hint:
            effective_query = f"{query or ''} {hint}".strip()
        else:
            effective_query = query or ""
        payload = {
            "schemaVersion": "1.0",
            "taskId": task_id,
            "scope": {
                "tenantId": tenant_id,
                "customerId": customer_id,
                "agentDomain": agent_domain,
                "memorySpace": "customer-service",
                "purpose": purpose,
            },
            "memoryTypes": policy["memoryTypes"],
            "predicates": policy["predicates"],
            "temporal": {"mode": "current", "asOf": self._clock().isoformat()},
            "maxItems": policy["maxItems"],
            "maxTokens": policy["maxTokens"],
            "sensitivityCeiling": policy["sensitivityCeiling"],
        }
        if effective_query:
            payload["query"] = effective_query
        result = await self._post("/v1/context:query", payload)
        self._validate_context_pack(result, task_id, policy)
        return result

    async def _query_knowledge_graph(
        self, policy: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """K2-T4: 从策略 graphQueryKeywords 发起知识图谱检索，返回 fact 列表。"""
        if self._kg_gateway is None:
            return []
        keywords = policy.get("graphQueryKeywords")
        if not isinstance(keywords, list) or not keywords:
            return []
        query = " ".join(str(k) for k in keywords)
        try:
            entities = await self._kg_gateway.search_entities(query, top_k=5)
            if not entities:
                return []
            # 对 top-3 实体扩展 2-hop 子图，获取关系信息
            top_ids = [str(e.get("id", "")) for e in entities[:3] if e.get("id")]
            relations_map: dict[str, list[dict[str, Any]]] = {}
            if top_ids:
                subgraph = await self._kg_gateway.expand_subgraph(top_ids, hops=2)
                sub_nodes = subgraph.get("nodes", [])
                sub_edges = subgraph.get("edges", [])
                node_label_map = {
                    str(n.get("id", "")): n.get("label", n.get("id", ""))
                    for n in sub_nodes if isinstance(n, dict)
                }
                for edge in sub_edges:
                    if not isinstance(edge, dict):
                        continue
                    src = str(edge.get("source", edge.get("from", "")))
                    tgt = str(edge.get("target", edge.get("to", "")))
                    etype = str(edge.get("type", "RELATED_TO"))
                    for nid in top_ids:
                        if src == nid and tgt != nid:
                            relations_map.setdefault(nid, []).append({
                                "type": etype,
                                "target": node_label_map.get(tgt, tgt),
                                "direction": "outgoing",
                            })
                        elif tgt == nid and src != nid:
                            relations_map.setdefault(nid, []).append({
                                "type": etype,
                                "target": node_label_map.get(src, src),
                                "direction": "incoming",
                            })
            return self._entities_to_facts(entities, relations_map)
        except Exception:
            return []

    def _entities_to_facts(
        self,
        entities: list[dict[str, Any]],
        relations_map: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """将图谱实体 + 关系转换为 memory context fact 格式。"""
        now = self._clock().isoformat()
        facts: list[dict[str, Any]] = []
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            ent_id = str(ent.get("id", ""))
            label = str(ent.get("label", ent_id))
            ent_type = str(ent.get("type", ""))
            props = ent.get("properties", "")
            content_parts: list[str] = [label]
            if ent_type:
                content_parts.append(f"类型:{ent_type}")
            if props:
                content_parts.append(str(props))
            rels = relations_map.get(ent_id, [])
            if rels:
                rel_strs = [
                    f"{r['type']}-{r['direction']}-{r['target']}"
                    for r in rels[:5]
                ]
                content_parts.append("关系: " + "; ".join(rel_strs))
            content = " | ".join(content_parts)
            facts.append({
                "memId": f"kg:{ent_id}" if ent_id else "kg:unknown",
                "content": content,
                "memoryType": "semantic_memory",
                "score": min(
                    float(ent.get("score", 1)) / 10.0, 1.0
                ) if ent.get("score") is not None else 0.5,
                "timestamp": now,
                "sensitivity": "L0",
                "safeLabel": label if label else "知识图谱事实",
                "safeValueSummary": content[:160],
                "displayType": "text",
                "display": None,
                "validFrom": now,
                "sourceAuthority": "knowledge_graph",
                "confidence": 0.9,
                "evidenceRef": f"graph:{ent_id}" if ent_id else "graph:unknown",
                "usage": "advisory",
                "requiresCoreRefresh": False,
                "requiresConfirmation": False,
                "predicate": None,
                "value": None,
            })
        return facts

    def _merge_kg_facts(
        self,
        pack: dict[str, Any],
        kg_facts: list[dict[str, Any]],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        """将知识图谱事实合并到 LTM context pack，不超过 maxItems 上限。"""
        merged = dict(pack)
        existing = list(merged.get("facts", []))
        remaining = policy["maxItems"] - len(existing)
        if remaining <= 0:
            return pack
        kg_facts = kg_facts[:remaining]
        merged["facts"] = existing + kg_facts
        # 每条 KG 事实约 40 token
        extra_tokens = len(kg_facts) * 40
        merged["estimatedTokens"] = min(
            merged.get("estimatedTokens", 0) + extra_tokens,
            policy["maxTokens"],
        )
        return merged

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self._base_url + path,
                headers={"Authorization": "Bearer " + self._service_token},
                json=payload,
                timeout=self._timeout_seconds,
            )
            if response.status_code >= 400:
                logger.warning(
                    "memory_control_error path=%s status=%s body=%s payload=%s",
                    path, response.status_code, response.text[:1000],
                    json.dumps(payload, ensure_ascii=False, default=str)[:1000],
                )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise MemoryGatewayError("memory control unavailable") from exc
        if not isinstance(result, dict):
            raise MemoryGatewayError("memory control response invalid")
        return result

    @staticmethod
    def _validate_context_pack(
        result: dict[str, Any], task_id: str, policy: dict[str, Any]
    ) -> None:
        expected_top = {
            "schemaVersion", "taskId", "queryTime", "policyVersion", "mode",
            "facts", "conflicts", "omissions", "estimatedTokens",
            "deniedPredicates",
        }
        if set(result) != expected_top:
            raise MemoryGatewayError("memory context response shape invalid")
        denied_predicates = result.get("deniedPredicates")
        if not isinstance(denied_predicates, list) or len(denied_predicates) > 64 \
                or not all(isinstance(p, str) and SAFE_ID.fullmatch(p) for p in denied_predicates):
            raise MemoryGatewayError("memory context denied predicates invalid")
        facts = result.get("facts")
        if result.get("schemaVersion") != "1.0" or result.get("taskId") != task_id:
            raise MemoryGatewayError("memory context response binding invalid")
        if result.get("mode") not in {"shadow", "read_only", "governed_write"}:
            raise MemoryGatewayError("memory context response mode invalid")
        if not HttpMemoryGateway._is_datetime(result.get("queryTime")):
            raise MemoryGatewayError("memory context response time invalid")
        if not SAFE_ID.fullmatch(str(result.get("policyVersion", ""))):
            raise MemoryGatewayError("memory context response policy version invalid")
        if not isinstance(facts, list) or len(facts) > policy["maxItems"]:
            raise MemoryGatewayError("memory context response budget invalid")
        allowed_predicates = set(policy["predicates"])
        # 允许的全部 fact 字段 — 适配 OpenJiuwen 原生结构
        fact_allowed_keys = {
            "memId", "content", "memoryType", "score", "timestamp",
            "sensitivity", "safeLabel", "safeValueSummary", "displayType",
            "validFrom", "sourceAuthority", "confidence", "evidenceRef",
            "usage", "requiresCoreRefresh", "requiresConfirmation",
            "display", "predicate", "value",
        }
        # 必填字段（OpenJiuwen 原生 + 时间戳）
        fact_required_keys = {"memId", "content", "memoryType", "validFrom"}
        display_types = {"text", "currency", "integer", "boolean", "date", "enum", "masked_reference"}
        authorities = {"user_explicit", "canonical_run", "agent_observation", "model_inference", "knowledge_graph"}
        for item in facts:
            if not isinstance(item, dict) or not set(item).issubset(fact_allowed_keys):
                raise MemoryGatewayError("memory context fact shape invalid")
            missing = fact_required_keys - set(item)
            if missing:
                raise MemoryGatewayError("memory context fact missing required fields")
            # 语义融合模式：predicates 为空时不做谓词白名单校验，
            # 依赖 memory-control 侧的治理过滤（敏感度天花板 + 记忆类型 + 用途过滤）
            predicate = item.get("predicate")
            if allowed_predicates and predicate is not None and predicate not in allowed_predicates:
                raise MemoryGatewayError("memory context response policy invalid")
            display_type = item.get("displayType", "text")
            if display_type not in display_types:
                raise MemoryGatewayError("memory context response policy invalid")
            if item["memoryType"] not in MEMORY_TYPES:
                raise MemoryGatewayError("memory context record metadata invalid")
            sensitivity = item.get("sensitivity", "L0")
            if sensitivity not in SENSITIVITIES:
                raise MemoryGatewayError("memory context record metadata invalid")
            safe_label = item.get("safeLabel", "记忆事实")
            safe_value = item.get("safeValueSummary")
            if safe_value is None:
                # 回退到 content 字段（OpenJiuwen 原生内容）
                safe_value = item.get("content", "")
            if _safe_trace_text(safe_label, 80) is None or _safe_trace_text(safe_value, 160) is None:
                raise MemoryGatewayError("memory context display projection invalid")
            if item.get("sourceAuthority", "agent_observation") not in authorities:
                raise MemoryGatewayError("memory context source invalid")
            evidence_ref = str(item.get("evidenceRef", "memref_unknown"))
            usage_val = str(item.get("usage", "advisory"))
            if not SAFE_ID.fullmatch(evidence_ref) or not SAFE_ID.fullmatch(usage_val):
                raise MemoryGatewayError("memory context reference invalid")
            if not HttpMemoryGateway._is_datetime(item["validFrom"]):
                raise MemoryGatewayError("memory context fact time invalid")
            confidence = item.get("confidence", 1.0)
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise MemoryGatewayError("memory context confidence invalid")
            if not isinstance(item.get("requiresCoreRefresh", False), bool) or not isinstance(item.get("requiresConfirmation", False), bool):
                raise MemoryGatewayError("memory context control flag invalid")
            # value 可选 — 存在时校验类型安全
            # 语义记忆（business knowledge）的 value 字段包含完整知识内容，
            # 可能超过 120 字符，放宽到 2000 字符以适配业务知识场景
            scalar = item.get("value")
            if scalar is not None:
                if isinstance(scalar, str):
                    if not scalar or len(scalar) > 2000 or "<" in scalar or ">" in scalar:
                        raise MemoryGatewayError("memory context value invalid")
                elif isinstance(scalar, bool):
                    pass
                elif isinstance(scalar, (int, float)):
                    if isinstance(scalar, float) and not __import__("math").isfinite(scalar):
                        raise MemoryGatewayError("memory context value invalid")
                else:
                    raise MemoryGatewayError("memory context value invalid")
        conflicts = result.get("conflicts")
        if not isinstance(conflicts, list) or len(conflicts) > 12:
            raise MemoryGatewayError("memory context conflicts invalid")
        for item in conflicts:
            if not isinstance(item, dict) or set(item) != {"predicate", "reason", "count"}:
                raise MemoryGatewayError("memory context conflict shape invalid")
            if item["predicate"] not in allowed_predicates or item["reason"] not in {
                "same_authority_overlap", "contested_assertion"
            } or not isinstance(item["count"], int) or isinstance(item["count"], bool) or not 1 <= item["count"] <= 20:
                raise MemoryGatewayError("memory context conflict invalid")
        omission_allowed_keys = {
            "expired", "revoked", "rejected", "contested", "purpose",
            "sensitivity", "temporal", "policy", "tokenBudget", "engineError",
        }
        omissions = result.get("omissions")
        if (
            not isinstance(omissions, dict)
            or not set(omissions).issubset(omission_allowed_keys)
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in omissions.values()
            )
        ):
            raise MemoryGatewayError("memory context omissions invalid")
        estimated = result.get("estimatedTokens")
        if not isinstance(estimated, int) or isinstance(estimated, bool) or not 0 <= estimated <= policy["maxTokens"]:
            raise MemoryGatewayError("memory context response token budget invalid")
        forbidden_scope_fields = {"tenantId", "customerId", "customer_id", "tenant_id"}
        if forbidden_scope_fields.intersection(result):
            raise MemoryGatewayError("memory context response leaks scope")

    @staticmethod
    def _is_datetime(value: Any) -> bool:
        if not isinstance(value, str) or len(value) > 40:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None


def _summarize_recall(
    packs: list[dict[str, Any]], policy_version: str
) -> MemoryRecallResult:
    facts = [
        fact for wrapper in packs for fact in wrapper["contextPack"]["facts"]
    ]
    return MemoryRecallResult(
        outcome="applied" if facts else "empty",
        context_packs=tuple(packs),
        fact_count=len(facts),
        conflict_count=sum(
            len(wrapper["contextPack"]["conflicts"]) for wrapper in packs
        ),
        omission_count=sum(
            sum(wrapper["contextPack"]["omissions"].values()) for wrapper in packs
        ),
        requires_core_refresh_count=sum(
            1 for item in facts if item.get("requiresCoreRefresh")
        ),
        requires_confirmation_count=sum(
            1 for item in facts if item.get("requiresConfirmation")
        ),
        policy_version=policy_version,
    )


def summarize_context_pack(pack: dict[str, Any]) -> dict[str, int]:
    """Return value-free counts suitable for agent context and public Trace."""
    facts = pack["facts"]
    return {
        "factCount": len(facts),
        "conflictCount": len(pack["conflicts"]),
        "omissionCount": sum(pack["omissions"].values()),
        "requiresCoreRefreshCount": sum(1 for item in facts if item.get("requiresCoreRefresh")),
        "requiresConfirmationCount": sum(1 for item in facts if item.get("requiresConfirmation")),
    }
