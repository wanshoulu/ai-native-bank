"""Strict public and A2A contracts for the bank's dual DeepAgent runtime."""

from __future__ import annotations

import unicodedata
import re
import math
from datetime import datetime
from urllib.parse import unquote
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class LLMTolerantModel(BaseModel):
    """LLM 生成 JSON 的容错基类。

    qwen3.7-flash 等轻量模型偶发用 camelCase 提交字段或多投未知字段，
    StrictModel extra=forbid 会直接拒绝。此基类：
    - alias_generator=to_camel: task_ids → taskIds，两种写法都能接受
    - extra=ignore: 未知字段静默丢弃，不触发 ValidationError
    - populate_by_name=True: snake_case 字段名仍然可用于内部构造
    """
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        alias_generator=to_camel,
    )


class FrozenStrictModel(StrictModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


MAX_AGENT_SESSION_ID_LENGTH = 160


_USER_TEXT_INTERNAL_MARKERS = (
    "confirmationtoken", "tokenhash", "authorizationtoken", "accesstoken",
    "refreshtoken", "systemprompt", "模型原文", "accountid", "cardid",
    "payeeid", "capabilityid", "intentid", "taskid", "stepid", "agentrunid",
    "businessagentrunid", "delegationid", "traceid", "requestid",
    "conversationid", "errorcode", "failedstage", "risklevel",
    "executionreadiness", "clarificationrequired", "needsslots",
    "needsclarification", "awaitingconfirmation", "navigationonly",
    "bank", "acct", "credit", "payee",
)


def _canonical_user_text(value: str) -> str:
    characters: list[str] = []
    for character in unicodedata.normalize("NFKC", value):
        if unicodedata.category(character) == "Nd":
            try:
                characters.append(str(unicodedata.decimal(character)))
                continue
            except (TypeError, ValueError):
                pass
        characters.append(character)
    return "".join(characters).casefold()


def _compact_user_text(value: str) -> str:
    return "".join(
        character
        for character in _canonical_user_text(value)
        if unicodedata.category(character)[0] not in {"P", "Z", "S", "M", "C"}
    )


def _decoded_security_forms(value: str) -> tuple[str, ...]:
    forms: list[str] = []
    current = _canonical_user_text(value)
    for _ in range(4):
        if current not in forms:
            forms.append(current)
        decoded = _canonical_user_text(unquote(current))
        if decoded == current:
            break
        current = decoded
    return tuple(forms)


def _longest_digit_chain(value: str) -> int:
    """账号形态检测：数字组经装饰性分隔符（标点/空白/符号/不可见字符）去除后，
    被不超过 2 个字母衔接的相邻数字组仍视为同一串，返回最长串长度。
    收益率/费率等金融文本数字密集但呈短串分布（组间有真实文字），不会误判。"""
    best = 0
    current = 0
    gap = 0
    in_digits = False
    for character in value:
        if character.isdigit():
            current += 1
            best = max(best, current)
            gap = 0
            in_digits = True
        elif in_digits:
            gap += 1
            if gap > 2:
                current = 0
                in_digits = False
    return best


def _validate_user_visible_texts(values: list[str], error_message: str) -> None:
    for value in values:
        for security_form in _decoded_security_forms(value):
            compact = _compact_user_text(security_form)
            if any(marker in compact for marker in _USER_TEXT_INTERNAL_MARKERS):
                raise ValueError(error_message)
            if _longest_digit_chain(compact) >= 15:
                raise ValueError(error_message)


class PageContext(StrictModel):
    tab: str | None = Field(default=None, min_length=1, max_length=64)
    menu_key: str | None = Field(default=None, alias="menuKey", min_length=1, max_length=128)


class Readiness(StrictModel):
    status: Literal["ready"]
    environment: str = Field(min_length=1, max_length=32)
    blockers: list[str] = Field(default_factory=list, max_length=0)


class CapabilityProjection(StrictModel):
    id: str = Field(pattern=r"^bank\.[a-z0-9.-]+$", max_length=128)
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    exposure: Literal["model-callable", "user-action"]
    side_effect: str = Field(alias="sideEffect", min_length=1, max_length=32)
    risk_level: Literal["L0", "L1", "L2", "L3"] = Field(alias="riskLevel")
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    output_schema: dict[str, Any] = Field(alias="outputSchema")
    possible_errors: list[str] = Field(alias="possibleErrors", max_length=64)
    readiness: Readiness


class ConversationSlot(StrictModel):
    name: str = Field(pattern=r"^[a-z][A-Za-z0-9]{0,63}$")
    value: str | int | float | bool
    source: Literal["user_explicit", "user_selection", "context", "intent_default"]
    source_intent_id: str = Field(
        alias="sourceIntentId", pattern=r"^[a-z0-9.-]+$", max_length=160
    )
    updated_at: str = Field(alias="updatedAt", min_length=20, max_length=40)

    @model_validator(mode="after")
    def reject_internal_slot_names(self) -> "ConversationSlot":
        if re.search(r"(?:Id|ID|Token|Secret|Credential|Grant)$", self.name):
            raise ValueError("conversation slots cannot contain internal identifiers")
        return self


class ConversationContextGoal(StrictModel):
    task_id: str = Field(alias="taskId", min_length=1, max_length=96)
    intent_id: str = Field(
        alias="intentId", pattern=r"^[a-z0-9.-]+$", min_length=1, max_length=160
    )
    status: Literal["provisional", "needs_slots", "ready"]
    slots: list[ConversationSlot] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_goal_slots(self) -> "ConversationContextGoal":
        names = [item.name for item in self.slots]
        if len(names) != len(set(names)):
            raise ValueError("conversation goal slot names must be unique")
        if any(item.source_intent_id != self.intent_id for item in self.slots):
            raise ValueError("conversation goal slots cannot cross intents")
        return self


class ConversationContextAmbiguity(StrictModel):
    type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$", max_length=64)
    task_ids: list[str] = Field(alias="taskIds", min_length=2, max_length=16)
    options: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]] = Field(
        min_length=2, max_length=8
    )


class ConversationContext(StrictModel):
    pending_intent_id: str | None = Field(
        default=None, alias="pendingIntentId", pattern=r"^[a-z0-9.-]+$", max_length=160
    )
    slots: list[ConversationSlot] = Field(default_factory=list, max_length=16)
    pending_envelope_id: str | None = Field(
        default=None, alias="pendingEnvelopeId", min_length=1, max_length=96
    )
    envelope_revision: int | None = Field(
        default=None, alias="envelopeRevision", ge=1
    )
    intent_shape: Literal[
        "single_intent", "multi_intent", "same_intent_multi_task",
        "ambiguous_intent", "non_task",
    ] | None = Field(default=None, alias="intentShape")
    goals: list[ConversationContextGoal] = Field(default_factory=list, max_length=16)
    shared_slots: list[ConversationSlot] = Field(
        default_factory=list, alias="sharedSlots", max_length=16
    )
    ambiguities: list[ConversationContextAmbiguity] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_owner_scoped_intent_context(self) -> "ConversationContext":
        if self.goals:
            if self.pending_intent_id is not None or self.slots:
                raise ValueError("multi-goal context cannot contain legacy state")
            if self.pending_envelope_id is None or self.intent_shape is None:
                raise ValueError("multi-goal context requires envelope and shape")
            if self.envelope_revision is None:
                self.envelope_revision = 1
            goal_ids = [item.task_id for item in self.goals]
            if len(goal_ids) != len(set(goal_ids)):
                raise ValueError("conversation goal task ids must be unique")
            known = set(goal_ids)
            for ambiguity in self.ambiguities:
                if len(ambiguity.task_ids) != len(set(ambiguity.task_ids)):
                    raise ValueError("conversation ambiguity task ids must be unique")
                if not set(ambiguity.task_ids).issubset(known):
                    raise ValueError("conversation ambiguity must reference known goals")
            shared_names = [item.name for item in self.shared_slots]
            if len(shared_names) != len(set(shared_names)):
                raise ValueError("conversation shared slot names must be unique")
            goal_intents = {item.intent_id for item in self.goals}
            if any(item.source_intent_id not in goal_intents for item in self.shared_slots):
                raise ValueError("shared slots must belong to a pending goal intent")
            return self
        if (
            self.pending_envelope_id is not None
            or self.envelope_revision is not None
            or self.intent_shape is not None
        ):
            raise ValueError("empty multi-goal context cannot carry workflow identity")
        if self.shared_slots or self.ambiguities:
            raise ValueError("empty multi-goal context cannot carry workflow payload")
        if self.slots and self.pending_intent_id is None:
            raise ValueError("conversation slots require a pending intent")
        names = [item.name for item in self.slots]
        if len(names) != len(set(names)):
            raise ValueError("conversation slot names must be unique")
        if any(item.source_intent_id != self.pending_intent_id for item in self.slots):
            raise ValueError("conversation slots cannot cross intents")
        return self


class ConversationStateSlot(StrictModel):
    name: str = Field(pattern=r"^[a-z][A-Za-z0-9]{0,63}$", max_length=64)
    value: str | int | float | bool
    source: Literal["user_explicit", "user_selection", "context", "intent_default"]

    @model_validator(mode="after")
    def validate_scalar_bound(self) -> "ConversationStateSlot":
        if re.search(r"(?:Id|ID|Token|Secret|Credential|Grant)$", self.name):
            raise ValueError("conversation state cannot contain internal slot names")
        if isinstance(self.value, str) and not (1 <= len(self.value) <= 240):
            raise ValueError("conversation state value is out of bounds")
        if isinstance(self.value, str) and (
            self.value.casefold().startswith(("acct_", "payee_", "credit_"))
            or "bank." in self.value.casefold()
        ):
            raise ValueError("conversation state cannot contain internal identifiers")
        return self


class PendingBusinessGoal(StrictModel):
    task_id: str = Field(alias="taskId", min_length=1, max_length=96)
    intent_id: str = Field(
        alias="intentId", pattern=r"^[a-z0-9.-]+$", min_length=1, max_length=160
    )
    slots: list[ConversationStateSlot] = Field(default_factory=list, max_length=16)
    status: Literal["provisional", "needs_slots", "ready"]

    @model_validator(mode="after")
    def validate_unique_slots(self) -> "PendingBusinessGoal":
        names = [item.name for item in self.slots]
        if len(names) != len(set(names)):
            raise ValueError("business goal slot names must be unique")
        return self


class ConversationStateAmbiguity(StrictModel):
    type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$", max_length=64)
    task_ids: list[str] = Field(alias="taskIds", min_length=2, max_length=16)
    options: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]] = Field(
        min_length=2, max_length=8
    )

    @model_validator(mode="after")
    def validate_unique_values(self) -> "ConversationStateAmbiguity":
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("conversation ambiguity task ids must be unique")
        if len(self.options) != len(set(self.options)):
            raise ValueError("conversation ambiguity options must be unique")
        return self


class ConversationState(StrictModel):
    schema_version: Literal["1.0", "2.0"] = Field(alias="schemaVersion")
    pending_intent_id: str | None = Field(
        default=None, alias="pendingIntentId",
        pattern=r"^[a-z0-9.-]+$", max_length=160,
    )
    status: Literal["retain", "clear"]
    slots: list[ConversationStateSlot] = Field(default_factory=list, max_length=16)
    pending_envelope_id: str | None = Field(
        default=None, alias="pendingEnvelopeId", min_length=1, max_length=96
    )
    envelope_revision: int | None = Field(
        default=None, alias="envelopeRevision", ge=1
    )
    intent_shape: Literal[
        "single_intent", "multi_intent", "same_intent_multi_task",
        "ambiguous_intent", "non_task",
    ] | None = Field(default=None, alias="intentShape")
    goals: list[PendingBusinessGoal] = Field(default_factory=list, max_length=16)
    shared_slots: list[ConversationStateSlot] = Field(
        default_factory=list, alias="sharedSlots", max_length=16
    )
    ambiguities: list[ConversationStateAmbiguity] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ConversationState":
        if self.schema_version == "1.0":
            if (
                self.pending_envelope_id is not None
                or self.envelope_revision is not None
                or self.intent_shape is not None
            ):
                raise ValueError("v1 conversation state cannot contain v2 identity")
            if self.goals or self.shared_slots or self.ambiguities:
                raise ValueError("v1 conversation state cannot contain v2 payload")
        else:
            if self.pending_intent_id is not None or self.slots:
                raise ValueError("v2 conversation state cannot contain v1 payload")
            goal_ids = [item.task_id for item in self.goals]
            if len(goal_ids) != len(set(goal_ids)):
                raise ValueError("business goal task ids must be unique")
            known = set(goal_ids)
            if any(not set(item.task_ids).issubset(known) for item in self.ambiguities):
                raise ValueError("conversation ambiguity must reference known goals")
            shared_names = [item.name for item in self.shared_slots]
            if len(shared_names) != len(set(shared_names)):
                raise ValueError("shared conversation slot names must be unique")
            if self.status == "retain" and (
                self.pending_envelope_id is None
                or self.intent_shape is None
                or not self.goals
            ):
                raise ValueError("retained v2 state requires envelope, shape and goals")
            if self.status == "retain" and self.envelope_revision is None:
                self.envelope_revision = 1
            if self.status == "clear" and (
                self.pending_envelope_id is not None
                or self.envelope_revision is not None
                or self.intent_shape is not None
                or self.goals
                or self.shared_slots
                or self.ambiguities
            ):
                raise ValueError("cleared v2 state cannot carry workflow context")
            return self
        names = [item.name for item in self.slots]
        if len(names) != len(set(names)):
            raise ValueError("conversation state slot names must be unique")
        if self.status == "retain" and (self.pending_intent_id is None or not self.slots):
            raise ValueError("retained state requires a pending intent and slots")
        if self.status == "clear" and (self.pending_intent_id is not None or self.slots):
            raise ValueError("cleared state cannot carry slots")
        return self


class SafetyContext(FrozenStrictModel):
    """Bounded, already-redacted user turns used only by the safety Rail."""

    recent_user_messages: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
    ] = Field(alias="recentUserMessages", default_factory=list, max_length=6)


class GuidedEntryGoal(FrozenStrictModel):
    goal_id: str = Field(alias="goalId", pattern=r"^goal_[A-Za-z0-9_.:-]+$", max_length=160)
    scenario_id: str = Field(alias="scenarioId", pattern=r"^[a-z0-9-]+$", max_length=96)
    scenario_version: str = Field(alias="scenarioVersion", pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    recommendation_id: str = Field(alias="recommendationId", pattern=r"^nba_[A-Za-z0-9_.:-]+$", max_length=160)
    target_intent_id: str = Field(alias="targetIntentId", pattern=r"^[a-z0-9.-]+$", max_length=160)
    mode: Literal["guided"]
    context_refs: list[Annotated[str, StringConstraints(min_length=1, max_length=160)]] = Field(alias="contextRefs", min_length=1, max_length=16)
    workflow_template_id: str = Field(alias="workflowTemplateId", pattern=r"^[a-z0-9-]+-v[0-9]+$", max_length=128)
    trusted_inputs: dict[str, str | int | float | bool] = Field(
        default_factory=dict, alias="trustedInputs", max_length=16
    )

    @field_validator("trusted_inputs")
    @classmethod
    def validate_trusted_inputs(
        cls, value: dict[str, str | int | float | bool]
    ) -> dict[str, str | int | float | bool]:
        for key, item in value.items():
            if not re.fullmatch(r"[a-z][a-zA-Z0-9_]{0,63}", key):
                raise ValueError("guided trusted input name is invalid")
            if isinstance(item, str):
                if not item or len(item) > 160 or re.search(r"[<>]", item):
                    raise ValueError("guided trusted input text is invalid")
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("guided trusted input number is invalid")
        return value


FocusText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=240),
]


class RecommendationFocusMetric(FrozenStrictModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=48)]
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=96)]

    @model_validator(mode="after")
    def validate_visible_text(self):
        _validate_user_visible_texts([self.label, self.value], "focus metric text is invalid")
        return self


class RecommendationFocusItem(FrozenStrictModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    detail: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)] | None = None
    amount: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=96)] | None = None

    @model_validator(mode="after")
    def validate_visible_text(self):
        _validate_user_visible_texts(
            [item for item in (self.title, self.detail, self.amount) if item],
            "focus item text is invalid",
        )
        return self


class RecommendationFocusContext(FrozenStrictModel):
    title: FocusText
    headline: FocusText
    metrics: list[RecommendationFocusMetric] = Field(default_factory=list, max_length=12)
    items: list[RecommendationFocusItem] = Field(default_factory=list, max_length=16)
    insights: list[FocusText] = Field(default_factory=list, max_length=8)
    suggestion: FocusText | None = None

    @model_validator(mode="after")
    def validate_visible_text(self):
        _validate_user_visible_texts(
            [self.title, self.headline, *self.insights, *([self.suggestion] if self.suggestion else [])],
            "focus context text is invalid",
        )
        return self


class RecommendationFocus(FrozenStrictModel):
    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    recommendation_id: str = Field(alias="recommendationId", pattern=r"^nba_[A-Za-z0-9_.:-]+$", max_length=160)
    mode: Literal["consult"]
    scenario_id: str = Field(alias="scenarioId", pattern=r"^[a-z0-9-]+$", max_length=96)
    scenario_version: str = Field(alias="scenarioVersion", pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    target_intent_id: str = Field(alias="targetIntentId", pattern=r"^[a-z0-9.-]+$", max_length=160)
    context_digest: str = Field(alias="contextDigest", pattern=r"^[a-f0-9]{64}$")
    expires_at: datetime = Field(alias="expiresAt")
    context: RecommendationFocusContext


class InternalIntentEvaluateRequest(StrictModel):
    """评测阶梯只读请求（评估观测面，不触发规划与业务执行）。"""

    message: str = Field(min_length=1, max_length=2000)
    page_context: PageContext | None = Field(
        default=None, alias="pageContext"
    )
    plannable_capability_ids: list[str] = Field(
        default_factory=list, alias="plannableCapabilityIds", max_length=256
    )


class RuntimeAgentRunRequest(StrictModel):
    agent_run_id: str = Field(alias="agentRunId", min_length=1, max_length=96)
    conversation_id: str = Field(alias="conversationId", min_length=1, max_length=96)
    request_id: str = Field(alias="requestId", min_length=1, max_length=128)
    trace_id: str = Field(alias="traceId", min_length=1, max_length=128)
    customer_id: str = Field(alias="customerId", min_length=1, max_length=96)
    actor_type: Literal["agent"] = Field(alias="actorType")
    message: str = Field(min_length=1, max_length=2000)
    page_context: PageContext = Field(alias="pageContext", default_factory=PageContext)
    conversation_context: ConversationContext = Field(
        alias="conversationContext", default_factory=ConversationContext
    )
    safety_context: SafetyContext = Field(
        alias="safetyContext", default_factory=SafetyContext
    )
    entry_goal: GuidedEntryGoal | None = Field(default=None, alias="entryGoal")
    conversation_focus: RecommendationFocus | None = Field(
        default=None, alias="conversationFocus"
    )
    executable_capabilities: list[CapabilityProjection] = Field(alias="executableCapabilities", max_length=64)
    plannable_capabilities: list[CapabilityProjection] = Field(alias="plannableCapabilities", min_length=1, max_length=64)


SafeAgentSessionId = Annotated[
    str,
    StringConstraints(
        min_length=7,
        max_length=MAX_AGENT_SESSION_ID_LENGTH,
        pattern=(
            r"^(?:user:[A-Za-z0-9][A-Za-z0-9_.-]{0,95}"
            r"|business:[A-Za-z0-9][A-Za-z0-9_.-]{0,95}:"
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95})$"
        ),
    ),
]


class AgentSessionReleaseRequest(StrictModel):
    session_ids: list[SafeAgentSessionId] = Field(
        alias="sessionIds",
        min_length=1,
        max_length=32,
    )

    @field_validator("session_ids")
    @classmethod
    def reject_ambiguous_session_ids(cls, value: list[str]) -> list[str]:
        if any(".." in session_id for session_id in value):
            raise ValueError("agent session id is invalid")
        return value


class CapabilityGrantRef(FrozenStrictModel):
    grant_id: str = Field(alias="grantId", pattern=r"^acg_[A-Za-z0-9_-]+$", max_length=100)
    task_id: str = Field(alias="taskId", pattern=r"^[A-Za-z0-9_.:-]+$", max_length=96)
    capability_id: str = Field(alias="capabilityId", pattern=r"^bank\.[a-z0-9.-]+$", max_length=128)
    expires_at: str = Field(alias="expiresAt", min_length=20, max_length=40)


class BusinessWorkflowResumeRequest(StrictModel):
    resume_kind: Literal["confirmation", "clarification"] = Field(
        default="confirmation", alias="resumeKind"
    )
    agent_run_id: str = Field(alias="agentRunId", min_length=1, max_length=96)
    conversation_id: str = Field(alias="conversationId", min_length=1, max_length=96)
    customer_id: str = Field(alias="customerId", min_length=1, max_length=96)
    checkpoint_id: str | None = Field(
        default=None, alias="checkpointId",
        pattern=r"^acp_[A-Za-z0-9_-]+$", max_length=96,
    )
    task_id: str | None = Field(
        default=None, alias="taskId", pattern=r"^[A-Za-z0-9_.:-]+$", max_length=96
    )
    clarification_message: str | None = Field(
        default=None, alias="clarificationMessage", min_length=1, max_length=240
    )
    grants: list[CapabilityGrantRef] = Field(default_factory=list, max_length=16)
    expected_workflow_revision: int | None = Field(
        default=None, alias="expectedWorkflowRevision", ge=1
    )
    conversation_context: ConversationContext | None = Field(
        default=None, alias="conversationContext"
    )
    executable_capability_ids: list[str] = Field(
        default_factory=list, alias="executableCapabilityIds", max_length=64
    )

    @model_validator(mode="after")
    def validate_unique_grants(self) -> "BusinessWorkflowResumeRequest":
        grant_ids = [item.grant_id for item in self.grants]
        if len(grant_ids) != len(set(grant_ids)):
            raise ValueError("resume grants must have unique grant ids")
        if len(self.executable_capability_ids) != len(set(self.executable_capability_ids)):
            raise ValueError("resume executable capability ids must be unique")
        if any(
            not re.fullmatch(r"bank\.[a-z0-9.-]{1,123}", item)
            for item in self.executable_capability_ids
        ):
            raise ValueError("resume executable capability id is invalid")
        if (self.expected_workflow_revision is None) != (self.conversation_context is None):
            raise ValueError("native resume context must be complete")
        if self.resume_kind == "confirmation":
            if self.checkpoint_id is None or not self.grants:
                raise ValueError("confirmation resume requires checkpoint and grants")
            if self.task_id is not None or self.clarification_message is not None:
                raise ValueError("confirmation resume cannot carry clarification task")
        else:
            if self.expected_workflow_revision is None or self.conversation_context is None:
                raise ValueError("clarification resume requires native workflow context")
            if self.task_id is None or self.checkpoint_id is not None or self.grants:
                raise ValueError("clarification resume requires only the active task")
            if self.clarification_message and re.search(
                r"\d(?:[\s-]?\d){14,}", _canonical_user_text(self.clarification_message)
            ):
                raise ValueError("clarification message cannot contain a full account number")
        return self


class BusinessWorkflowReceipt(FrozenStrictModel):
    grant_id: str = Field(alias="grantId", pattern=r"^acg_[A-Za-z0-9_-]+$", max_length=100)
    task_id: str = Field(alias="taskId", pattern=r"^[A-Za-z0-9_.:-]+$", max_length=96)
    capability_id: str = Field(alias="capabilityId", pattern=r"^bank\.[a-z0-9.-]+$", max_length=128)
    status: Literal["SUCCEEDED", "FAILED"]
    output_keys: list[str] = Field(alias="outputKeys", default_factory=list, max_length=32)
    error_code: str | None = Field(default=None, alias="errorCode", pattern=r"^[a-z0-9_]+$", max_length=96)


class BusinessWorkflowResumeResponse(StrictModel):
    schema_version: Literal["1.0", "2.0"] = Field(alias="schemaVersion")
    agent_run_id: str = Field(alias="agentRunId", min_length=1, max_length=96)
    business_workflow_id: str = Field(alias="businessWorkflowId", min_length=1, max_length=160)
    status: Literal[
        "succeeded", "partial_success", "failed",
        "awaiting_confirmation", "clarification_required",
    ]
    receipts: list[BusinessWorkflowReceipt] = Field(max_length=16)
    workflow_revision: int | None = Field(
        default=None, alias="workflowRevision", ge=1
    )
    active_goal_task_id: str | None = Field(
        default=None, alias="activeGoalTaskId", max_length=96
    )
    task_progress: TaskProgress | None = Field(default=None, alias="taskProgress")
    user_response: UserResponse | None = Field(default=None, alias="userResponse")
    user_responses: tuple[UserResponse, ...] | None = Field(
        default=None, alias="userResponses", min_length=1, max_length=8
    )
    user_result: UserFacingResult | None = Field(default=None, alias="userResult")
    confirmation: ConfirmationView | None = None
    clarification_form: ClarificationForm | None = Field(
        default=None, alias="clarificationForm"
    )
    conversation_state: ConversationState | None = Field(
        default=None, alias="conversationState"
    )

    @model_validator(mode="after")
    def validate_native_resume_projection(self) -> "BusinessWorkflowResumeResponse":
        if self.schema_version == "1.0":
            if not self.receipts:
                raise ValueError("legacy resume requires receipts")
            if any(item is not None for item in (
                self.workflow_revision, self.active_goal_task_id,
                self.task_progress, self.user_response, self.user_responses,
                self.user_result,
                self.confirmation,
                self.clarification_form,
                self.conversation_state,
            )):
                raise ValueError("legacy resume cannot carry loop projection")
            return self
        if self.workflow_revision is None:
            raise ValueError("native resume requires workflow revision")
        if self.conversation_state is None:
            raise ValueError("native resume requires conversation state")
        if self.user_response is not None and self.user_responses is not None:
            raise ValueError("resume accepts either userResponse or userResponses")
        terminal_response = (
            self.user_responses[-1]
            if self.user_responses is not None
            else self.user_response
        )
        if self.user_responses is not None and any(
            item.type != "result" or item.actions
            for item in self.user_responses[:-1]
        ):
            raise ValueError("resume response prefixes must be non-actionable results")
        if self.status in {"awaiting_confirmation", "clarification_required"}:
            if self.active_goal_task_id is None or self.task_progress is None or terminal_response is None:
                raise ValueError("non-terminal resume requires active task projection")
        if (self.status == "awaiting_confirmation") != (self.confirmation is not None):
            raise ValueError("resume confirmation status conflict")
        if self.clarification_form is not None and self.status != "clarification_required":
            raise ValueError("resume clarification form status conflict")
        return self


class SlotValue(LLMTolerantModel):
    value: Any
    source: Literal["user_explicit", "user_selection", "context", "intent_default"]
    confidence: float = Field(ge=0, le=1)


class IntentTask(LLMTolerantModel):
    task_id: str = Field(min_length=1, max_length=96)
    intent_id: str = Field(min_length=1, max_length=160)
    action: Literal["ASK", "QUERY", "NAVIGATE", "DO", "CHANGE", "CANCEL", "HELP", "TRANSFER", "REPAY"]
    business_object: str = Field(min_length=1, max_length=128)
    slots: dict[str, SlotValue] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list, max_length=32)
    execution_readiness: Literal["ready", "needs_slots", "needs_clarification", "navigation_only", "unsupported"]
    risk_level: Literal["L0", "L1", "L2", "L3"]
    task_status: Literal[
        "pending",
        "needs_slots",
        "needs_clarification",
        "ready",
        "awaiting_confirmation",
        "running",
        "succeeded",
        "failed",
        "skipped",
        "completed",
        "cancelled",
    ] = "pending"


class UserMemoryCandidateHint(LLMTolerantModel):
    # 语义级谓词：不再限制为预定义枚举，LLM 可基于用户表达语义生成
    # snake_case 谓词名。已知谓词仍保留值域校验，未知谓词接受标量值。
    predicate: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$", max_length=64)
    value: str | bool | int
    evidence_kind: Literal["user_explicit"]
    consent_granted: Literal[True]

    @model_validator(mode="after")
    def validate_value_for_predicate(self) -> "UserMemoryCandidateHint":
        # 已注册谓词仍校验值域；未注册谓词接受任何标量值
        allowed: dict[str, set[str | bool]] = {
            "preferred_language": {"zh-CN", "en-US"},
            "response_style": {"concise", "detailed"},
            "recommendation_opt_out": {True, False},
            "spending_explanation_preference": {"category_first", "change_first", "detail_first"},
            "investment_goal_preference": {"capital_preservation", "steady_growth", "long_term_growth"},
        }
        if self.predicate in allowed:
            if self.value not in allowed[self.predicate] or (
                self.predicate == "recommendation_opt_out" and not isinstance(self.value, bool)
            ):
                raise ValueError("memory hint value is not allowed for predicate")
            return self
        # 未注册谓词：接受任意 str/bool/int 标量值
        return self


IntentShape = Literal[
    "single_intent",
    "multi_intent",
    "same_intent_multi_task",
    "ambiguous_intent",
    "non_task",
]


class IntentAmbiguity(LLMTolerantModel):
    type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$", max_length=64)
    task_ids: list[str] = Field(default_factory=list, max_length=16)
    options: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]] = Field(
        min_length=2, max_length=8
    )

    @model_validator(mode="after")
    def validate_unique_values(self) -> "IntentAmbiguity":
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("ambiguity task_ids must be unique")
        if len(self.options) != len(set(self.options)):
            raise ValueError("ambiguity options must be unique")
        return self


class IntentTaskEnvelope(LLMTolerantModel):
    """Model-visible cross-field invariants.

    single_intent requires exactly one task.
    multi_intent and same_intent_multi_task require at least two tasks.
    ambiguous_intent and non_task require tasks=[].
    missing slots do not make an intent ambiguous; keep the recognized intent task and
    use execution_readiness=needs_slots or needs_clarification.
    """

    schema_version: Literal["1.0"] = "1.0"
    revision: int = Field(default=1, ge=1)
    delegation_id: str = Field(min_length=1, max_length=96)
    trace_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=96)
    source_agent_run_id: str = Field(min_length=1, max_length=96)
    intent_shape: IntentShape
    tasks: list[IntentTask] = Field(default_factory=list, max_length=16)
    ambiguities: list[IntentAmbiguity] = Field(default_factory=list, max_length=8)
    excluded_entities: list[str] = Field(default_factory=list, max_length=16)
    shared_slots: dict[str, SlotValue] = Field(default_factory=dict)
    predicted_next_intents: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("excluded_entities")
    @classmethod
    def _validate_excluded_entities(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item) > 64:
                raise ValueError(
                    "excluded_entities entries must be non-empty strings (<=64 chars)"
                )
        return value

    @model_validator(mode="after")
    def validate_task_graph(self) -> "IntentTaskEnvelope":
        count = len(self.tasks)
        if self.intent_shape == "single_intent" and count != 1:
            raise ValueError("single_intent requires exactly one task")
        if self.intent_shape in {"multi_intent", "same_intent_multi_task"} and count < 2:
            raise ValueError("multi-task intent requires at least two tasks")
        if self.intent_shape in {"ambiguous_intent", "non_task"} and count:
            raise ValueError("ambiguous/non-task envelopes cannot contain executable tasks")
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task_id must be unique")
        known = set(task_ids)
        for task in self.tasks:
            if task.task_id in task.dependencies or not set(task.dependencies).issubset(known):
                raise ValueError("task dependency must reference another task")
        for ambiguity in self.ambiguities:
            if ambiguity.task_ids and not set(ambiguity.task_ids).issubset(known):
                raise ValueError("ambiguity must reference known tasks")
        return self


class CapabilityPlanStep(StrictModel):
    step_id: str = Field(min_length=1, max_length=96)
    task_id: str = Field(min_length=1, max_length=96)
    capability_id: str = Field(pattern=r"^bank\.[a-z0-9.-]+$", max_length=128)
    input_bindings: dict[str, Any] = Field(default_factory=dict)
    requires_user_confirmation: bool
    depends_on: list[str] = Field(default_factory=list, max_length=32)


class CapabilityPlan(StrictModel):
    schema_version: Literal["1.0"]
    business_agent_run_id: str = Field(min_length=1, max_length=96)
    delegation_id: str = Field(min_length=1, max_length=96)
    status: Literal["planned", "clarification_required", "navigation_only", "unsupported", "blocked"]
    steps: list[CapabilityPlanStep] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_step_graph(self) -> "CapabilityPlan":
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step_id must be unique")
        known = set(step_ids)
        for step in self.steps:
            if step.step_id in step.depends_on or not set(step.depends_on).issubset(known):
                raise ValueError("step dependency must reference another step")
        if self.status == "planned" and not self.steps:
            raise ValueError("planned status requires steps")
        if self.status in {"navigation_only", "unsupported"} and self.steps:
            raise ValueError("non-executable plan cannot contain steps")
        return self


class UserResponseActionPayload(FrozenStrictModel):
    message: str | None = Field(default=None, min_length=1, max_length=240)
    channel: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,31}$")
    feature_id: str | None = Field(default=None, alias="featureId", pattern=r"^[a-z][a-z0-9-]{0,63}$")
    action_id: str | None = Field(default=None, alias="actionId", pattern=r"^[a-z][a-z0-9-]{0,63}$")


class UserResponseActionFormField(FrozenStrictModel):
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,31}$")
    label: str = Field(min_length=1, max_length=40)
    type: Literal["text", "number", "select"]
    required: bool = True
    options: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    placeholder: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def validate_options(self) -> "UserResponseActionFormField":
        if self.type == "select" and len(self.options) < 2:
            raise ValueError("select field requires at least two options")
        if self.type != "select" and self.options:
            raise ValueError("options are only valid for select fields")
        return self


class UserResponseActionForm(FrozenStrictModel):
    fields: tuple[UserResponseActionFormField, ...] = Field(min_length=1, max_length=6)


class UserResponseAction(FrozenStrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=160)
    kind: Literal["reply", "navigate", "confirm", "dismiss"]
    interaction: Literal["submit", "compose"] | None = None
    payload: UserResponseActionPayload = Field(default_factory=UserResponseActionPayload)
    form: UserResponseActionForm | None = None

    @model_validator(mode="after")
    def validate_payload_for_kind(self) -> "UserResponseAction":
        payload_fields = self.payload.model_fields_set
        if self.kind != "reply" and self.interaction is not None:
            raise ValueError("interaction is only valid for reply actions")
        if self.kind != "reply" and self.form is not None:
            raise ValueError("form is only valid for reply actions")
        if self.form is not None and self.interaction != "compose":
            raise ValueError("form requires a compose interaction")
        if self.kind == "reply":
            if not self.payload.message:
                raise ValueError("reply action requires message")
            if payload_fields != {"message"}:
                raise ValueError("reply action only accepts message")
        if self.kind == "navigate":
            if not all((self.payload.channel, self.payload.feature_id, self.payload.action_id)):
                raise ValueError("navigate action requires registered target")
            if payload_fields != {"channel", "feature_id", "action_id"}:
                raise ValueError("navigate action only accepts registered target")
        if self.kind in {"confirm", "dismiss"} and payload_fields:
            raise ValueError("confirm/dismiss actions require an empty payload")
        return self


class UserResponseSource(FrozenStrictModel):
    id: str = Field(pattern=r"^faq\.[a-z0-9.-]+$", max_length=128)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=32)
    label: Literal["Mock 业务知识"]


UserResponseItem = Annotated[str, Field(min_length=1, max_length=240)]


class TaskProgress(FrozenStrictModel):
    mode: Literal["sequential"]
    current: int = Field(ge=1, le=16)
    total: int = Field(ge=1, le=16)
    completed: int = Field(ge=0, le=16)
    failed: int = Field(ge=0, le=16)
    skipped: int = Field(ge=0, le=16)

    @model_validator(mode="after")
    def validate_counts(self) -> "TaskProgress":
        terminal = self.completed + self.failed + self.skipped
        if self.current > self.total or terminal >= self.total:
            raise ValueError("task progress is inconsistent")
        return self


class ClarificationFieldOption(FrozenStrictModel):
    label: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=60)


class ClarificationField(FrozenStrictModel):
    name: Literal["payeeName", "accountNo", "bankName", "purpose", "savePayee", "amount", "fromAccount"]
    label: str = Field(min_length=1, max_length=40)
    type: Literal["text", "bank_account", "bank_name", "boolean", "amount", "select"]
    required: bool
    value: str | bool
    editable: bool
    options: tuple[ClarificationFieldOption, ...] | None = None

    @model_validator(mode="after")
    def reject_sensitive_prefill(self) -> "ClarificationField":
        if self.name == "accountNo" and self.value != "":
            raise ValueError("bank account cannot be prefilled")
        if self.name == "savePayee" and not isinstance(self.value, bool):
            raise ValueError("savePayee must be boolean")
        if self.name != "savePayee" and not isinstance(self.value, str):
            raise ValueError("text clarification field must contain text")
        return self


class ClarificationForm(FrozenStrictModel):
    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    clarification_id: str = Field(
        alias="clarificationId", pattern=r"^acl_[A-Za-z0-9_-]+$", max_length=96
    )
    submission_token: str = Field(
        alias="submissionToken", min_length=32, max_length=256, repr=False
    )
    expires_at: str = Field(alias="expiresAt", min_length=20, max_length=40)
    kind: Literal["transfer_payee", "transfer_full"]
    fields: tuple[ClarificationField, ...] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def validate_unique_fields(self) -> "ClarificationForm":
        names = [item.name for item in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("clarification fields are invalid")
        if self.kind == "transfer_full":
            required = {"payeeName", "accountNo", "amount"}
            if not required.issubset(names):
                raise ValueError("clarification fields are invalid")
        elif "accountNo" not in names or "payeeName" not in names:
            raise ValueError("clarification fields are invalid")
        return self


class UserResponse(FrozenStrictModel):
    schema_version: Literal["1.0", "1.1"] = Field(alias="schemaVersion")
    type: Literal["result", "clarification", "answer", "confirmation", "navigation", "notice"]
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=600)
    items: tuple[UserResponseItem, ...] = Field(default_factory=tuple, max_length=20)
    actions: tuple[UserResponseAction, ...] = Field(default_factory=tuple, max_length=8)
    source: UserResponseSource | None = None
    task_progress: TaskProgress | None = Field(default=None, alias="taskProgress")

    @model_validator(mode="after")
    def validate_strategy_and_safe_text(self) -> "UserResponse":
        if self.type == "clarification" and any(item.kind != "reply" for item in self.actions):
            raise ValueError("clarification choices must be replies")
        if self.type == "navigation" and (
            not self.actions or any(item.kind != "navigate" for item in self.actions)
        ):
            raise ValueError("navigation requires navigate actions")
        if self.type == "answer" and self.source is None:
            raise ValueError("answer requires governed source")
        if self.type != "answer" and self.source is not None:
            raise ValueError("only answer can carry source")
        if self.task_progress is not None and (
            self.schema_version != "1.1"
            or self.type not in {"clarification", "confirmation", "result"}
        ):
            raise ValueError("task progress requires a v1.1 workflow response")
        if self.schema_version == "1.0" and self.task_progress is not None:
            raise ValueError("v1.0 response cannot carry task progress")
        action_ids = [action.id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("user response action ids must be unique")
        values = [self.title, self.body, *self.items]
        for action in self.actions:
            values.extend(item for item in (action.label, action.description, action.payload.message) if item)
        _validate_user_visible_texts(values, "user response exposes internal data")
        return self


class SafetyClarificationSubmission(FrozenStrictModel):
    """Value-free clarification generated by the User DeepAgent on review."""

    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_safe_text(self) -> "SafetyClarificationSubmission":
        _validate_user_visible_texts(
            [self.title, self.body],
            "safety clarification exposes internal data",
        )
        return self


class UserFacingItemMeta(FrozenStrictModel):
    label: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_safe_text(self) -> "UserFacingItemMeta":
        _validate_user_visible_texts(
            [self.label, self.value], "item meta contains forbidden text"
        )
        return self


class UserFacingItemAction(FrozenStrictModel):
    label: str = Field(min_length=1, max_length=40)
    message: str | None = Field(default=None, max_length=240)
    detail: str | None = Field(default=None, max_length=2000)
    # submit = 确定性单选项（单击即发送）；compose = 开放输入请求，
    # message 仅是可编辑草稿，必须经用户确认/修改后才能发送。
    interaction: Literal["submit", "compose"] = "submit"
    form: "UserResponseActionForm | None" = None

    @model_validator(mode="after")
    def validate_safe_text(self) -> "UserFacingItemAction":
        values: list[str] = [self.label]
        if self.message:
            values.append(self.message)
        if self.detail:
            values.append(self.detail)
        _validate_user_visible_texts(
            values, "item action contains forbidden text"
        )
        return self


class UserFacingItemCard(FrozenStrictModel):
    id: str | None = Field(default=None, max_length=80)
    title: str = Field(min_length=1, max_length=120)
    subtitle: str | None = Field(default=None, max_length=80)
    metas: tuple[UserFacingItemMeta, ...] = Field(default_factory=tuple, max_length=8)
    action: UserFacingItemAction | None = Field(default=None)

    @model_validator(mode="after")
    def validate_safe_text(self) -> "UserFacingItemCard":
        values: list[str] = [self.title]
        if self.subtitle:
            values.append(self.subtitle)
        if self.id:
            values.append(self.id)
        for meta in self.metas:
            values.extend([meta.label, meta.value])
        if self.action:
            values.append(self.action.label)
            if self.action.message:
                values.append(self.action.message)
            if self.action.detail:
                values.append(self.action.detail)
        _validate_user_visible_texts(
            values, "item card contains forbidden text"
        )
        return self


class UserFacingPlanAllocation(FrozenStrictModel):
    product_id: str = Field(
        alias="productId", pattern=r"^prod_[A-Za-z0-9_-]{1,63}$"
    )
    product_name: str = Field(alias="productName", min_length=1, max_length=120)
    product_category: str = Field(alias="productCategory", min_length=1, max_length=40)
    product_category_label: str = Field(
        alias="productCategoryLabel", min_length=1, max_length=40
    )
    portfolio_role: str = Field(alias="portfolioRole", min_length=1, max_length=40)
    portfolio_role_label: str = Field(
        alias="portfolioRoleLabel", min_length=1, max_length=40
    )
    amount_minor: int = Field(alias="amountMinor", ge=100)
    amount: str = Field(min_length=1, max_length=40)
    ratio_basis_points: int = Field(alias="ratioBasisPoints", ge=1, le=10000)
    risk_level: str = Field(alias="riskLevel", min_length=1, max_length=20)
    liquidity: str = Field(min_length=1, max_length=240)
    suitability: str = Field(min_length=1, max_length=240)
    rate_note: str | None = Field(default=None, alias="rateNote", max_length=400)
    detail: str | None = Field(default=None, max_length=2000)
    product_action_label: str = Field(
        alias="productActionLabel", min_length=1, max_length=40
    )

    @model_validator(mode="after")
    def validate_safe_text(self) -> "UserFacingPlanAllocation":
        values = [
            self.product_name, self.product_category, self.product_category_label,
            self.portfolio_role, self.portfolio_role_label, self.amount,
            self.risk_level, self.liquidity, self.suitability,
            self.product_action_label,
        ]
        if self.rate_note:
            values.append(self.rate_note)
        if self.detail:
            values.append(self.detail)
        _validate_user_visible_texts(values, "plan allocation contains forbidden text")
        return self


class UserFacingPlan(FrozenStrictModel):
    id: str = Field(pattern=r"^plan_[A-Za-z0-9_-]{1,63}$")
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=240)
    total_amount_minor: int = Field(alias="totalAmountMinor", ge=100)
    total_amount: str = Field(alias="totalAmount", min_length=1, max_length=40)
    is_primary: bool = Field(alias="isPrimary")
    allocations: tuple[UserFacingPlanAllocation, ...] = Field(
        min_length=1, max_length=5
    )
    rationale: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    select_label: str = Field(alias="selectLabel", min_length=1, max_length=40)
    select_message: str = Field(alias="selectMessage", min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_amounts_and_safe_text(self) -> "UserFacingPlan":
        if sum(item.amount_minor for item in self.allocations) != self.total_amount_minor:
            raise ValueError("user-facing plan amount mismatch")
        values = [
            self.title, self.summary, self.total_amount,
            self.select_label, self.select_message, *self.rationale,
        ]
        _validate_user_visible_texts(values, "user-facing plan contains forbidden text")
        return self


class UserFacingResult(FrozenStrictModel):
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=240)
    items: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    item_cards: tuple[UserFacingItemCard, ...] = Field(
        default_factory=tuple, max_length=20, alias="itemCards"
    )
    plans: tuple[UserFacingPlan, ...] = Field(default_factory=tuple, max_length=3)
    primary_action: str | None = Field(default=None, alias="primaryAction", max_length=80)
    actions: tuple[UserFacingItemAction, ...] = Field(default_factory=tuple, max_length=6)

    @model_validator(mode="after")
    def validate_safe_text(self) -> "UserFacingResult":
        values = [self.title, self.summary, *self.items]
        if self.primary_action:
            values.append(self.primary_action)
        for value in values:
            if len(value) > 240:
                raise ValueError("user-facing result contains an unmasked account number")
        _validate_user_visible_texts(
            values, "user-facing result contains forbidden text"
        )
        if self.plans and sum(1 for plan in self.plans if plan.is_primary) != 1:
            raise ValueError("user-facing result requires exactly one primary plan")
        return self


class ConfirmationSummary(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=200)
    risk_level: Literal["L0", "L1", "L2", "L3"] = Field(alias="riskLevel")
    items: list[str] = Field(default_factory=list, max_length=16)
    task_count: int = Field(default=1, alias="taskCount", ge=1, le=16)
    total_amount_minor: int | None = Field(
        default=None, alias="totalAmountMinor", ge=1, le=800_000_000
    )
    # Frontend rendering hints — not validated by gateway, only used by
    # the UI to render structured transfer confirmation cards.
    transfer_details: list[dict[str, Any]] | None = Field(
        default=None, alias="transferDetails"
    )
    from_account: dict[str, Any] | None = Field(
        default=None, alias="fromAccount"
    )

    @model_validator(mode="after")
    def validate_batch_summary(self) -> "ConfirmationSummary":
        # A batch displays one line per independently confirmed task.  A
        # single transaction may display several verified detail lines
        # (object, amount, funding account and post-transaction balances).
        if self.task_count > 1 and len(self.items) != self.task_count:
            raise ValueError("confirmation requires one item per displayed task")
        if self.task_count > 1 and self.total_amount_minor is None:
            raise ValueError("batch confirmation requires total amount")
        _validate_user_visible_texts(
            [self.title, self.description, *self.items],
            "confirmation summary exposes internal data",
        )
        return self


class ConfirmationCredential(StrictModel):
    checkpoint_id: str = Field(
        alias="checkpointId", pattern=r"^acp_[A-Za-z0-9_-]+$", max_length=96
    )
    confirmation_token: str = Field(
        alias="confirmationToken", min_length=32, max_length=256, repr=False
    )


class ConfirmationView(StrictModel):
    checkpoint_id: str = Field(alias="checkpointId", pattern=r"^acp_[A-Za-z0-9_-]+$", max_length=96)
    capability_id: str = Field(alias="capabilityId", pattern=r"^bank\.[a-z0-9.-]+$", max_length=128)
    confirmation_token: str = Field(alias="confirmationToken", min_length=32, max_length=256, repr=False)
    expires_at: str = Field(alias="expiresAt", min_length=20, max_length=40)
    summary: ConfirmationSummary
    confirmations: list[ConfirmationCredential] = Field(
        default_factory=list, max_length=16, repr=False
    )

    @model_validator(mode="after")
    def validate_batch_credentials(self) -> "ConfirmationView":
        if not self.confirmations:
            if self.summary.task_count != 1:
                raise ValueError("batch confirmation requires credentials")
            return self
        checkpoint_ids = [item.checkpoint_id for item in self.confirmations]
        if len(checkpoint_ids) != len(set(checkpoint_ids)):
            raise ValueError("confirmation checkpoint ids must be unique")
        if len(self.confirmations) != self.summary.task_count:
            raise ValueError("confirmation credential count must match task count")
        if (
            self.confirmations[0].checkpoint_id != self.checkpoint_id
            or self.confirmations[0].confirmation_token != self.confirmation_token
        ):
            raise ValueError("primary confirmation credential mismatch")
        return self


class RuntimeAgentRunResponse(StrictModel):
    agent_run_id: str = Field(alias="agentRunId")
    conversation_id: str = Field(alias="conversationId")
    status: Literal["planned", "succeeded", "awaiting_confirmation", "clarification_required", "navigation_only", "unsupported", "blocked", "unavailable"]
    intent_task_envelope: IntentTaskEnvelope | None = Field(
        default=None, alias="intentTaskEnvelope"
    )
    capability_plan: CapabilityPlan | None = Field(
        default=None, alias="capabilityPlan"
    )
    user_result: UserFacingResult | None = Field(default=None, alias="userResult")
    user_response: UserResponse | None = Field(default=None, alias="userResponse")
    user_responses: tuple[UserResponse, ...] | None = Field(
        default=None,
        alias="userResponses",
        min_length=1,
        max_length=8,
    )
    confirmation: ConfirmationView | None = None
    clarification_form: ClarificationForm | None = Field(
        default=None, alias="clarificationForm"
    )
    conversation_state: ConversationState | None = Field(
        default=None, alias="conversationState"
    )
    memory_candidate_hints: tuple[UserMemoryCandidateHint, ...] = Field(
        default=(), exclude=True, repr=False, max_length=5
    )

    @model_validator(mode="after")
    def validate_terminal_projection(self) -> "RuntimeAgentRunResponse":
        has_agent_artifacts = (
            self.intent_task_envelope is not None
            and self.capability_plan is not None
        )
        if self.status == "unavailable":
            if has_agent_artifacts or any((
                self.intent_task_envelope is not None,
                self.capability_plan is not None,
                self.conversation_state is not None,
            )):
                raise ValueError("unavailable run cannot carry agent artifacts")
        elif not has_agent_artifacts:
            # blocked/clarification_required 允许携带 conversation_state，
            # 以便安全拦截或澄清只拒绝"本次请求"而不清空进行中的会话。
            if self.status not in {"blocked", "clarification_required"} or any((
                self.intent_task_envelope is not None,
                self.capability_plan is not None,
            )):
                raise ValueError("run status requires complete agent artifacts")
        if self.user_response is not None and self.user_responses is not None:
            raise ValueError("run accepts either userResponse or userResponses")
        terminal_response = (
            self.user_responses[-1]
            if self.user_responses is not None
            else self.user_response
        )
        if self.user_responses is not None and any(
            item.type not in ("result", "notice") or item.actions
            for item in self.user_responses[:-1]
        ):
            raise ValueError("ordered response prefixes must be non-actionable results or notices")
        if (
            self.status == "succeeded"
            and self.user_result is None
            and terminal_response is None
        ):
            raise ValueError("succeeded run requires user response")
        if terminal_response is not None:
            expected = {
                "succeeded": {"result", "answer"},
                "clarification_required": {"clarification"},
                "awaiting_confirmation": {"confirmation"},
                "navigation_only": {"navigation"},
                "unsupported": {"notice"},
                "blocked": {"notice"},
                "unavailable": {"notice"},
            }.get(self.status)
            if expected and terminal_response.type not in expected:
                raise ValueError("run status conflicts with user response")
        if self.status == "awaiting_confirmation" and self.confirmation is None:
            raise ValueError("awaiting_confirmation requires confirmation")
        if self.status != "succeeded" and self.user_result is not None:
            raise ValueError("only succeeded run can carry userResult")
        if self.status != "awaiting_confirmation" and self.confirmation is not None:
            raise ValueError("only awaiting_confirmation can carry confirmation")
        if self.clarification_form is not None and (
            self.status != "clarification_required"
            or terminal_response is None
            or terminal_response.type != "clarification"
            or terminal_response.schema_version != "1.1"
        ):
            raise ValueError("clarification form requires a v1.1 clarification response")
        return self


# Resolve resume projections declared before the user-facing response models.
BusinessWorkflowResumeResponse.model_rebuild()
