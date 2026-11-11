"""Strict, value-free public trace contracts for one Agent Run."""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import unquote

from pydantic import Field, field_validator, model_validator

from app.contracts import RuntimeAgentRunResponse, StrictModel

logger = logging.getLogger("ai_native_bank.trace")


TraceStage = Literal[
    "request.accepted",
    "input_rail.started",
    "input_rail.completed",
    "action_rail.started",
    "action_rail.completed",
    "output_rail.started",
    "output_rail.completed",
    "security_gate.decided",
    "user_agent.started",
    "user_agent.committed",
    "user_agent.rolled_back",
    "intent_graph.retrieved",
    "memory.user_context_recalled",
    "memory.task_context_recalled",
    "memory.candidates_built",
    "memory.governance_decided",
    "memory.persistence_completed",
    "user_agent.completed",
    "delegation.created",
    "business_agent.started",
    "business_skill.started",
    "business_skill.completed",
    "business_agent.completed",
    "capability.invocation_started",
    "capability.invocation_completed",
    "plan.validated",
    "confirmation.checkpoint_created",
    "run.awaiting_confirmation",
    "run.completed",
    "run.failed",
]
TraceStatus = Literal["accepted", "running", "completed", "awaiting_confirmation", "failed"]
SafeMetric = str | int | float | bool | None

ID_PATTERN = r"^[A-Za-z0-9_.:-]+$"
FIELD_PATTERN = r"^[A-Za-z0-9_.:-]+$"
MEMORY_GROUP_KEY_PATTERN = r"^[A-Za-z0-9_.:-]+$"
# Aligned with frontend traceText (tools/ai-native-bank-trace.js)
_TRACE_SECRET_MARKERS = (
    "confirmationtoken", "tokenhash", "authorization", "accesstoken", "accesskey",
    "refreshtoken", "apikey", "bearer", "password", "secret", "systemprompt",
    "rawresponse", "modelraw",
)
_TRACE_INTERNAL_ID_PREFIXES = (
    "acp", "bwf", "conv", "abr", "dlg", "trace", "checkpoint", "grant",
    "card", "payee", "execution", "order", "task", "transfer",
)
_BWF_HEX_PATTERN = re.compile(r"(?:^|[^a-z0-9])bwf_[a-f0-9]{24}(?:$|[^a-z0-9])", re.IGNORECASE)
_UNBOUNDED_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{32,}")

# URL-like text detection — aligned with frontend containsTraceUrlLikeText
_TRACE_URL_PROTOCOL_RE = re.compile(r"(?:https?|ftp|file|data|javascript|vbscript|ws|wss):", re.IGNORECASE)
_TRACE_URL_PROTOCOL_RELATIVE_RE = re.compile(r"//")
_TRACE_URL_WWW_RE = re.compile(r"www\.", re.IGNORECASE)
_TRACE_URL_IPV4_RE = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?")
_TRACE_URL_LOCALHOST_RE = re.compile(r"localhost(?::\d{1,5})?", re.IGNORECASE)
_TRACE_URL_EMAIL_RE = re.compile(r"[\w.%+-]+@(?:[\w-]+\.)+[a-zA-Z]{2,63}", re.IGNORECASE)
_IDEOGRAPHIC_DOMAIN_RE = re.compile(r"[\w-]+(?:\u3002[\w-]+)+", re.UNICODE)
_COMMON_IDN_TLDS = frozenset({
    "中国", "公司", "网络", "网址", "在线", "商店", "中文网", "我爱你", "移动",
    "集团", "世界", "信息", "餐厅", "游戏", "企业", "娱乐", "香港", "台湾", "澳门", "政务",
})
_PERCENT_ENCODE_RE = re.compile(r"(?:%[0-9a-fA-F]{2})+")
_FULLWIDTH_DOT = "\uff0e"
_HALFWIDTH_KATAKANA_DOT = "\uff61"
_CJK_FULL_STOP = "\u3002"


def _decoded_security_forms(value: str) -> list[str]:
    """Generate all URL-decoded variants of a value.

    Mirrors frontend decodedSecurityForms: NFKC normalize, lowercase,
    replace fullwidth dots, then up to 8 rounds of percent-decoding.
    Raises ValueError on invalid encoding or excessive encoding layers.
    """
    def _normalize(s: str) -> str:
        s = unicodedata.normalize("NFKC", s).casefold()
        return s.replace(_FULLWIDTH_DOT, ".").replace(_HALFWIDTH_KATAKANA_DOT, ".")

    forms: list[str] = []
    current = _normalize(value)
    for pass_num in range(8):
        if current not in forms:
            forms.append(current)
        if not _PERCENT_ENCODE_RE.search(current):
            break
        try:
            decoded = unquote(current, errors="strict")
        except (UnicodeDecodeError, ValueError):
            raise ValueError("trace text contains invalid encoding")
        decoded = _normalize(decoded)
        if decoded == current:
            break
        current = decoded
        if current not in forms:
            forms.append(current)
        if pass_num == 7:
            try:
                probe = unquote(current, errors="strict")
            except (UnicodeDecodeError, ValueError):
                raise ValueError("trace text contains invalid encoding")
            probe = _normalize(probe)
            if probe != current:
                raise ValueError("trace text has excessive encoding layers")
    return forms


def _contains_ideographic_bare_domain(value: str) -> bool:
    """Check for CJK internationalized domain names using 。 separator."""
    for match in _IDEOGRAPHIC_DOMAIN_RE.finditer(value):
        start, end = match.start(), match.end()
        before = value[start - 1] if start > 0 else ""
        after = value[end] if end < len(value) else ""
        if before == _CJK_FULL_STOP or after == _CJK_FULL_STOP:
            continue
        matched = match.group()
        labels = matched.split(_CJK_FULL_STOP)
        tld = labels[-1]
        if re.search(r"[a-z0-9]", matched, re.IGNORECASE) or tld in _COMMON_IDN_TLDS:
            return True
    return False


def _contains_trace_url_like_text(forms: list[str]) -> bool:
    """Check if any decoded form contains URL-like text.

    Mirrors frontend containsTraceUrlLikeText.
    """
    for candidate in forms:
        if _TRACE_URL_PROTOCOL_RE.search(candidate):
            return True
        if _TRACE_URL_PROTOCOL_RELATIVE_RE.search(candidate):
            return True
        if _TRACE_URL_WWW_RE.search(candidate):
            return True
        if _TRACE_URL_IPV4_RE.search(candidate):
            return True
        if _TRACE_URL_LOCALHOST_RE.search(candidate):
            return True
        if _TRACE_URL_EMAIL_RE.search(candidate):
            return True
        if _contains_ideographic_bare_domain(candidate):
            return True
    return False


def _is_safe_trace_text(
    value: str,
    max_length: int | None = None,
    *,
    strict: bool = True,
) -> str | None:
    """Single authoritative safe-text validator for memory trace text.

    Returns normalized text if safe, None if unsafe.
    Mirrors frontend traceText: NFKC, control chars, URL-like text across all
    decoded forms, sensitive markers, internal IDs, long credentials, payment
    card digits. This is the single source of truth — memory_runtime.py
    imports and delegates to this function.

    When strict=False (used for memory fact display values from the trusted
    memory-control service), skips URL-like-text detection and long-credential
    check — these are overly aggressive for internal memory content that
    legitimately contains long English identifiers (e.g. predicate names like
    ``dislikes_steady_growth_wealth_products``). The frontend achieves the same
    flexibility via its ``allowedLongId`` whitelist parameter.
    """
    if not isinstance(value, str) or not value:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        return None
    if max_length is not None and len(normalized) > max_length:
        return None
    if any(unicodedata.category(c).startswith("C") for c in normalized):
        return None
    if "<" in normalized or ">" in normalized:
        return None

    canonical = normalized.casefold()
    try:
        forms = _decoded_security_forms(canonical)
    except ValueError:
        return None

    if strict and _contains_trace_url_like_text(forms):
        return None

    for form in forms:
        if sum(1 for c in form if unicodedata.category(c) == "Nd") >= 15:
            return None
        if strict and _UNBOUNDED_CREDENTIAL.search(form):
            return None
        compact = "".join(
            c for c in form
            if not unicodedata.category(c).startswith(("P", "Z", "S", "M", "C"))
        )
        for marker in _TRACE_SECRET_MARKERS:
            if marker in compact:
                return None
        for prefix in _TRACE_INTERNAL_ID_PREFIXES:
            if (prefix + "private") in compact or (prefix + "internal") in compact:
                return None
        if _BWF_HEX_PATTERN.search(form):
            return None

    return normalized


TRACE_METRIC_KEYS: dict[str, frozenset[str]] = {
    "request.accepted": frozenset({"messageLength"}),
    "input_rail.started": frozenset({"parallelGroup"}),
    "input_rail.completed": frozenset(
        {"decision", "reasonCode", "policyVersion", "parallelGroup"}
    ),
    "action_rail.started": frozenset({"actionCount"}),
    "action_rail.completed": frozenset(
        {"decision", "reasonCode", "policyVersion", "actionCount"}
    ),
    "output_rail.started": frozenset(),
    "output_rail.completed": frozenset(
        {"decision", "reasonCode", "policyVersion"}
    ),
    "security_gate.decided": frozenset(
        {"decision", "reasonCode", "policyVersion", "permitType"}
    ),
    "user_agent.started": frozenset({"capabilityCount"}),
    "user_agent.committed": frozenset({"permitType", "envelopeRevision"}),
    "user_agent.rolled_back": frozenset({"reasonCode"}),
    "intent_graph.retrieved": frozenset({"candidateCount", "graphVersion"}),
    "memory.user_context_recalled": frozenset({
        "outcome", "contextPackCount", "factCount", "conflictCount",
        "omissionCount", "requiresCoreRefreshCount",
        "requiresConfirmationCount", "policyVersion", "intentId",
    }),
    "memory.task_context_recalled": frozenset({
        "outcome", "contextPackCount", "factCount", "conflictCount",
        "omissionCount", "requiresCoreRefreshCount",
        "requiresConfirmationCount", "policyVersion", "taskCount",
        "inputDefaultCount",
    }),
    "memory.candidates_built": frozenset({
        "candidateCount", "canonicalEpisodeCount", "userPreferenceCount",
    }),
    "memory.governance_decided": frozenset({
        "addCount", "mergeCount", "supersedeCount", "contestedCount", "rejectCount",
    }),
    "memory.persistence_completed": frozenset({
        "persistedCount", "pendingRetryCount", "deadLetterCount",
    }),
    "user_agent.completed": frozenset(
        {"intentShape", "taskCount", "candidateConstraint", "userRepairCount", "intentSummary", "slotSummary", "ambiguityType"}
    ),
    "delegation.created": frozenset({"taskCount", "dependencyCount"}),
    "business_agent.started": frozenset({
        "capabilityCount", "loopIteration", "todoRevision", "delegatedTaskCount"
    }),
    "business_skill.started": frozenset({
        "skillId", "skillVersion", "skillCount", "capabilityCount", "providerCount"
    }),
    "business_skill.completed": frozenset({
        "skillId", "skillVersion", "skillCount", "outcome", "stepCount"
    }),
    "business_agent.completed": frozenset(
        {
            "planStatus", "stepCount", "slotResolution", "businessRepairCount",
            "ambiguityType", "loopIteration", "todoRevision", "currentTodoStatus",
            "delegatedTaskCount", "coveredTaskCount",
        }
    ),
    "capability.invocation_started": frozenset({"capabilityId"}),
    "capability.invocation_completed": frozenset({"capabilityId", "outcome"}),
    "plan.validated": frozenset({"validated", "confirmationCount"}),
    "confirmation.checkpoint_created": frozenset({
        "capabilityId", "riskLevel", "checkpointStatus", "grantCount"
    }),
    "run.awaiting_confirmation": frozenset({
        "status", "taskCount", "stepCount", "checkpointStatus", "grantCount"
    }),
    "run.completed": frozenset({
        "status", "taskCount", "stepCount", "grantCount", "succeededCount",
        "failedCount",
    }),
    "run.failed": frozenset({"failedStage"}),
}


class TraceError(StrictModel):
    code: str = Field(pattern=r"^[a-z0-9_]+$", min_length=1, max_length=96)
    message: str = Field(min_length=1, max_length=200)


class SafeTraceTask(StrictModel):
    id: str = Field(pattern=ID_PATTERN, min_length=1, max_length=96)
    intent_id: str = Field(alias="intentId", pattern=ID_PATTERN, min_length=1, max_length=160)
    action: Literal["ASK", "QUERY", "NAVIGATE", "DO", "CHANGE", "CANCEL", "HELP"]
    business_object: str = Field(
        alias="businessObject", pattern=ID_PATTERN, min_length=1, max_length=128
    )
    readiness: Literal[
        "ready", "needs_slots", "needs_clarification", "navigation_only", "unsupported"
    ]
    risk: Literal["L0", "L1", "L2", "L3"]
    dependencies: list[str] = Field(default_factory=list, max_length=32)
    slot_names: list[str] = Field(alias="slotNames", default_factory=list, max_length=64)


class SafeTraceStep(StrictModel):
    id: str = Field(pattern=ID_PATTERN, min_length=1, max_length=96)
    task_id: str = Field(alias="taskId", pattern=ID_PATTERN, min_length=1, max_length=96)
    capability_id: str = Field(
        alias="capabilityId", pattern=r"^bank\.[a-z0-9.-]+$", min_length=1, max_length=128
    )
    input_keys: list[str] = Field(alias="inputKeys", default_factory=list, max_length=64)
    requires_confirmation: bool = Field(alias="requiresConfirmation")
    dependencies: list[str] = Field(default_factory=list, max_length=32)


class SafeAgentRunView(StrictModel):
    id: str = Field(pattern=ID_PATTERN, min_length=1, max_length=96)
    conversation_id: str = Field(
        alias="conversationId", pattern=ID_PATTERN, min_length=1, max_length=96
    )
    delegation_id: str = Field(
        alias="delegationId", pattern=ID_PATTERN, min_length=1, max_length=96
    )
    status: Literal["planned", "succeeded", "awaiting_confirmation", "clarification_required", "navigation_only", "unsupported", "blocked"]
    intent_shape: Literal[
        "single_intent", "multi_intent", "same_intent_multi_task", "ambiguous_intent", "non_task"
    ] = Field(alias="intentShape")
    tasks: list[SafeTraceTask] = Field(default_factory=list, max_length=16)
    steps: list[SafeTraceStep] = Field(default_factory=list, max_length=64)
    predicted_next_intents: list[str] = Field(
        alias="predictedNextIntents", default_factory=list, max_length=8
    )
    requires_confirmation: bool = Field(alias="requiresConfirmation")

    @model_validator(mode="after")
    def validate_graphs(self) -> "SafeAgentRunView":
        task_ids = [item.id for item in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("trace task id must be unique")
        known_tasks = set(task_ids)
        for task in self.tasks:
            if task.id in task.dependencies or not set(task.dependencies).issubset(known_tasks):
                raise ValueError("trace task dependency is invalid")
        step_ids = [item.id for item in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("trace step id must be unique")
        known_steps = set(step_ids)
        for step in self.steps:
            if step.task_id not in known_tasks:
                raise ValueError("trace step task is invalid")
            if step.id in step.dependencies or not set(step.dependencies).issubset(known_steps):
                raise ValueError("trace step dependency is invalid")
        return self


class SafeSecurityRunView(StrictModel):
    kind: Literal["security_gate"] = "security_gate"
    id: str = Field(pattern=ID_PATTERN, min_length=1, max_length=96)
    conversation_id: str = Field(
        alias="conversationId", pattern=ID_PATTERN, min_length=1, max_length=96
    )
    status: Literal["blocked", "unavailable", "clarification_required"]


def _validate_memory_trace_text(value: str) -> str:
    """Validate memory trace text using the single authoritative validator.

    Delegates to _is_safe_trace_text — the same function memory_runtime.py
    imports. This eliminates duplicate validation logic and ensures both
    Pydantic model validation and runtime projection use identical rules.
    """
    result = _is_safe_trace_text(value)
    if result is None:
        raise ValueError("memory trace text is unsafe")
    return result


class SafeMemoryRecallItem(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    display_value: str = Field(alias="displayValue", min_length=1, max_length=160)
    # UI 词表：与 Node 网关 agent-trace 校验一致；OpenJiuwen 原生类型在
    # memory_runtime 投影边界已翻译。
    memory_kind: Literal["working", "semantic", "episodic", "procedural"] = Field(alias="memoryKind")
    source_label: str = Field(alias="sourceLabel", min_length=1, max_length=80)
    valid_from: datetime = Field(alias="validFrom")
    application_status: Literal[
        "applied", "not_applied_conflict", "not_applied_policy"
    ] = Field(alias="applicationStatus")
    controls: list[Literal[
        "current", "requires_core_refresh", "requires_confirmation"
    ]] = Field(default_factory=list, max_length=3)

    @field_validator("label", "display_value", "source_label")
    @classmethod
    def validate_safe_text(cls, value: str) -> str:
        return _validate_memory_trace_text(value)

    @model_validator(mode="after")
    def validate_controls(self) -> "SafeMemoryRecallItem":
        if len(self.controls) != len(set(self.controls)):
            raise ValueError("memory recall controls must be unique")
        if self.application_status == "applied" and "current" not in self.controls:
            raise ValueError("applied memory recall must be current")
        return self


class SafeMemoryRecallGroup(StrictModel):
    key: str = Field(pattern=MEMORY_GROUP_KEY_PATTERN, min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)
    items: list[SafeMemoryRecallItem] = Field(default_factory=list, max_length=20)

    @field_validator("label")
    @classmethod
    def validate_safe_label(cls, value: str) -> str:
        return _validate_memory_trace_text(value)


class SafeMemoryRecallNotice(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    count: int = Field(ge=1, le=100_000)

    @field_validator("label")
    @classmethod
    def validate_safe_label(cls, value: str) -> str:
        return _validate_memory_trace_text(value)


class SafeMemoryRecallDetail(StrictModel):
    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    scope: Literal["user", "task"]
    detail_status: Literal["available", "empty", "redacted", "degraded"] = Field(alias="detailStatus")
    groups: list[SafeMemoryRecallGroup] = Field(default_factory=list, max_length=8)
    conflicts: list[SafeMemoryRecallNotice] = Field(default_factory=list, max_length=12)
    omissions: list[SafeMemoryRecallNotice] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_detail_state(self) -> "SafeMemoryRecallDetail":
        keys = [group.key for group in self.groups]
        if len(keys) != len(set(keys)):
            raise ValueError("memory recall group keys must be unique")
        item_count = sum(len(group.items) for group in self.groups)
        if item_count > 20:
            raise ValueError("memory recall item budget exceeded")
        if self.detail_status == "available" and item_count == 0:
            raise ValueError("available memory recall requires visible items")
        if self.detail_status in {"empty", "degraded"} and (
            self.groups or self.conflicts or self.omissions
        ):
            raise ValueError("empty memory recall state cannot carry details")
        return self


class SafeIntentGraphCandidate(StrictModel):
    intent_id: str = Field(alias="intentId", pattern=ID_PATTERN, min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=80)
    action: Literal["ASK", "QUERY", "NAVIGATE", "DO", "CHANGE", "CANCEL", "HELP"]
    business_object: str = Field(
        alias="businessObject", pattern=ID_PATTERN, min_length=1, max_length=128
    )
    execution_mode: Literal[
        "read-execute", "confirm-required", "navigation-only", "knowledge-only", "unavailable"
    ] = Field(alias="executionMode")
    capability_bindings: list[str] = Field(
        alias="capabilityBindings", default_factory=list, max_length=32
    )
    score: int = Field(ge=0, le=1000)
    evidence: list[str] = Field(default_factory=list, max_length=16)
    constraint: Literal["required", "allowed"]

    @field_validator("title")
    @classmethod
    def validate_safe_title(cls, value: str) -> str:
        return _validate_memory_trace_text(value)


class SafeIntentGraphDetail(StrictModel):
    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    graph_version: str = Field(
        alias="graphVersion", pattern=r"^\d+\.\d+\.\d+$", min_length=1, max_length=32
    )
    candidates: list[SafeIntentGraphCandidate] = Field(default_factory=list, max_length=8)


class TraceSummary(StrictModel):
    title: str = Field(min_length=1, max_length=128)
    input: list[str] = Field(default_factory=list, max_length=32)
    output: list[str] = Field(default_factory=list, max_length=32)
    metrics: dict[str, SafeMetric] = Field(default_factory=dict, max_length=32)
    memory_recall: SafeMemoryRecallDetail | None = Field(default=None, alias="memoryRecall")
    intent_graph_detail: SafeIntentGraphDetail | None = Field(default=None, alias="intentGraphDetail")
    result: SafeAgentRunView | SafeSecurityRunView | None = None


class AgentTraceEvent(StrictModel):
    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    sequence: int = Field(ge=1, le=32)
    timestamp: datetime
    stage: TraceStage
    status: TraceStatus
    duration_ms: int | None = Field(default=None, alias="durationMs", ge=0, le=600_000)
    agent_run_id: str = Field(
        alias="agentRunId", pattern=ID_PATTERN, min_length=1, max_length=96
    )
    trace_id: str = Field(alias="traceId", pattern=ID_PATTERN, min_length=1, max_length=128)
    summary: TraceSummary
    error: TraceError | None = None

    @model_validator(mode="after")
    def validate_stage_state(self) -> "AgentTraceEvent":
        if self.stage == "request.accepted" and self.status != "accepted":
            raise ValueError("request.accepted requires accepted status")
        if self.stage.endswith(".started") and self.status != "running":
            raise ValueError("started stage requires running status")
        if self.stage == "run.failed":
            if self.status != "failed" or self.error is None:
                raise ValueError("run.failed requires a safe error")
        elif self.status == "failed" or self.error is not None:
            raise ValueError("only run.failed can carry an error")
        elif self.stage == "run.awaiting_confirmation":
            if self.status != "awaiting_confirmation":
                raise ValueError("awaiting confirmation stage requires awaiting status")
        elif self.stage != "request.accepted" and self.stage not in {
            "input_rail.started", "user_agent.started", "business_agent.started",
            "business_skill.started", "capability.invocation_started",
            "action_rail.started", "output_rail.started"
        }:
            if self.status != "completed":
                raise ValueError("completed stage requires completed status")
        if self.summary.result is not None and self.stage not in {"run.completed", "run.awaiting_confirmation"}:
            raise ValueError("only terminal run stages can carry a result")
        memory_stages = {"memory.user_context_recalled", "memory.task_context_recalled"}
        if (self.summary.memory_recall is not None) != (self.stage in memory_stages):
            raise ValueError("memory recall detail must match a memory recall stage")
        if self.summary.memory_recall is not None:
            expected_scope = "user" if self.stage == "memory.user_context_recalled" else "task"
            if self.summary.memory_recall.scope != expected_scope:
                raise ValueError("memory recall scope does not match stage")
            visible_count = sum(
                len(group.items) for group in self.summary.memory_recall.groups
            )
            fact_count = self.summary.metrics.get("factCount")
            if not isinstance(fact_count, int) or isinstance(fact_count, bool) or fact_count < visible_count:
                raise ValueError("memory recall count is inconsistent")
        intent_graph_stages = {"intent_graph.retrieved"}
        if (self.summary.intent_graph_detail is not None) != (self.stage in intent_graph_stages):
            raise ValueError("intent graph detail must match an intent graph stage")
        if self.summary.intent_graph_detail is not None:
            candidate_count = self.summary.metrics.get("candidateCount")
            actual_count = len(self.summary.intent_graph_detail.candidates)
            # trace detail 截断到 8 项（SafeIntentGraphDetail.candidates
            # max_length=8），允许 candidateCount >= actual_count（截断场景）。
            if not isinstance(candidate_count, int) or isinstance(candidate_count, bool) or candidate_count < actual_count:
                raise ValueError("intent graph candidate count is inconsistent")
        return self


def project_safe_run(
    result: RuntimeAgentRunResponse,
) -> SafeAgentRunView | SafeSecurityRunView:
    envelope = result.intent_task_envelope
    plan = result.capability_plan
    if envelope is None or plan is None:
        if result.status not in {"blocked", "unavailable", "clarification_required"}:
            logger.warning(
                "project_safe_run: envelope/plan missing for status=%s agent_run_id=%s; "
                "degrading to unavailable",
                result.status,
                result.agent_run_id,
            )
            return SafeSecurityRunView(
                id=result.agent_run_id,
                conversationId=result.conversation_id,
                status="unavailable",
            )
        return SafeSecurityRunView(
            id=result.agent_run_id,
            conversationId=result.conversation_id,
            status=result.status,
        )
    return SafeAgentRunView(
        id=result.agent_run_id,
        conversationId=result.conversation_id,
        delegationId=envelope.delegation_id,
        status=result.status,
        intentShape=envelope.intent_shape,
        tasks=[
            SafeTraceTask(
                id=task.task_id,
                intentId=task.intent_id,
                action=task.action,
                businessObject=task.business_object,
                readiness=task.execution_readiness,
                risk=task.risk_level,
                dependencies=list(task.dependencies),
                slotNames=sorted(task.slots),
            )
            for task in envelope.tasks
        ],
        steps=[
            SafeTraceStep(
                id=step.step_id,
                taskId=step.task_id,
                capabilityId=step.capability_id,
                inputKeys=sorted(step.input_bindings),
                requiresConfirmation=step.requires_user_confirmation,
                dependencies=list(step.depends_on),
            )
            for step in plan.steps
        ],
        predictedNextIntents=list(envelope.predicted_next_intents),
        requiresConfirmation=any(step.requires_user_confirmation for step in plan.steps),
    )


def build_trace_event(
    *,
    sequence: int,
    stage: TraceStage,
    status: TraceStatus,
    agent_run_id: str,
    trace_id: str,
    title: str,
    input_fields: list[str],
    output_fields: list[str],
    metrics: dict[str, SafeMetric],
    duration_ms: int | None = None,
    memory_recall: dict | SafeMemoryRecallDetail | None = None,
    intent_graph_detail: dict | SafeIntentGraphDetail | None = None,
    result: SafeAgentRunView | SafeSecurityRunView | None = None,
    error: TraceError | None = None,
) -> AgentTraceEvent:
    unknown_metrics = set(metrics).difference(TRACE_METRIC_KEYS[stage])
    if unknown_metrics:
        raise ValueError("trace metrics are not allowed for stage")
    return AgentTraceEvent(
        schemaVersion="1.0",
        sequence=sequence,
        timestamp=datetime.now(timezone.utc),
        stage=stage,
        status=status,
        durationMs=duration_ms,
        agentRunId=agent_run_id,
        traceId=trace_id,
        summary=TraceSummary(
            title=title,
            input=input_fields,
            output=output_fields,
            metrics=metrics,
            memoryRecall=memory_recall,
            intentGraphDetail=intent_graph_detail,
            result=result,
        ),
        error=error,
    )
