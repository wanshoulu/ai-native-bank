"""Stable safety decisions and user-facing responses for the isolated lab.

The upstream API exposes only a boolean block flag. This adapter projects it
to a four-state decision so ambiguity and dependency failures are neither
allowed nor misreported as user attacks.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

try:
    from .finance_safety_policy import POLICY_VERSION, PolicyMatch
except ImportError:  # pragma: no cover - used by the /opt bootstrap script
    from finance_safety_policy import POLICY_VERSION, PolicyMatch


class Decision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REVIEW = "review"
    UNAVAILABLE = "unavailable"


class ReasonCode(str, Enum):
    UNSAFE_CONTENT = "unsafe_content"
    PROMPT_INJECTION = "prompt_injection"
    SENSITIVE_INPUT = "sensitive_input"
    SENSITIVE_OUTPUT = "sensitive_output"
    AMBIGUOUS_RISK_INTENT = "ambiguous_risk_intent"
    CONFLICTING_SAFETY_EVIDENCE = "conflicting_safety_evidence"
    INSUFFICIENT_SAFETY_CONTEXT = "insufficient_safety_context"
    INVALID_ANSWER_CONTRACT = "invalid_answer_contract"
    EMPTY_ANSWER = "empty_answer"
    SECURITY_DETECTOR_UNAVAILABLE = "security_detector_unavailable"
    ANSWER_MODEL_UNAVAILABLE = "answer_model_unavailable"
    GUARDRAIL_RUNTIME_ERROR = "guardrail_runtime_error"


TEMPLATES: dict[ReasonCode, str] = {
    ReasonCode.UNSAFE_CONTENT: (
        "抱歉，我无法协助处理这类请求。"
        "您可以换一种合规、安全的方式描述需要的金融帮助。"
    ),
    ReasonCode.PROMPT_INJECTION: (
        "抱歉，我无法提供或绕过内部安全设置。"
        "请直接描述您需要办理或咨询的金融问题。"
    ),
    ReasonCode.SENSITIVE_INPUT: (
        "为保护您的信息安全，请遮盖完整卡号、证件号、手机号等敏感信息后重新提问。"
    ),
    ReasonCode.SENSITIVE_OUTPUT: (
        "当前回答可能包含敏感信息，已停止展示。"
        "请换一种不涉及完整个人信息的方式查询。"
    ),
    ReasonCode.AMBIGUOUS_RISK_INTENT: (
        "为了安全判断您的需求，请补充说明办理目的和预期结果，且不要提供完整卡号、证件号或验证码。"
    ),
    ReasonCode.CONFLICTING_SAFETY_EVIDENCE: (
        "为了安全判断您的需求，请补充说明办理目的和预期结果，且不要提供完整卡号、证件号或验证码。"
    ),
    ReasonCode.INSUFFICIENT_SAFETY_CONTEXT: (
        "为了安全判断您的需求，请补充说明办理目的和预期结果，且不要提供完整卡号、证件号或验证码。"
    ),
    ReasonCode.INVALID_ANSWER_CONTRACT: "智能服务返回异常，请稍后重试。",
    ReasonCode.EMPTY_ANSWER: "智能服务未能生成有效回答，请稍后重试。",
    ReasonCode.SECURITY_DETECTOR_UNAVAILABLE: "安全检查暂时不可用，请稍后重试。",
    ReasonCode.ANSWER_MODEL_UNAVAILABLE: "智能服务暂时不可用，请稍后重试。",
    ReasonCode.GUARDRAIL_RUNTIME_ERROR: "安全服务暂时无法完成本次请求，请稍后重试。",
}

REVIEW_REASONS = frozenset(
    {
        ReasonCode.AMBIGUOUS_RISK_INTENT,
        ReasonCode.CONFLICTING_SAFETY_EVIDENCE,
        ReasonCode.INSUFFICIENT_SAFETY_CONTEXT,
    }
)


def review_result(
    reason: ReasonCode,
    conversation_id: str,
) -> dict[str, Any]:
    if reason not in REVIEW_REASONS:
        raise ValueError("review_result requires a review reason")
    response = TEMPLATES[reason]
    return {
        "response": response,
        "is_blocked": True,
        "is_attack": False,
        "decision": Decision.REVIEW.value,
        "reason_code": reason.value,
        "block_reason": reason.value,
        "blocked_by": "guardrail_review",
        "detected_by": "guardrail_review",
        "violated_rules": [],
        "response_message": response,
        "conversation_id": conversation_id,
        "detection_summary": None,
    }


def unavailable_result(
    reason: ReasonCode,
    conversation_id: str,
) -> dict[str, Any]:
    if reason not in {
        ReasonCode.SECURITY_DETECTOR_UNAVAILABLE,
        ReasonCode.ANSWER_MODEL_UNAVAILABLE,
        ReasonCode.GUARDRAIL_RUNTIME_ERROR,
        ReasonCode.INVALID_ANSWER_CONTRACT,
        ReasonCode.EMPTY_ANSWER,
    }:
        raise ValueError("unavailable_result requires an availability reason")
    response = TEMPLATES[reason]
    return {
        "response": response,
        "is_blocked": True,
        "is_attack": False,
        "decision": Decision.UNAVAILABLE.value,
        "reason_code": reason.value,
        "block_reason": reason.value,
        "blocked_by": "system_guardrail",
        "detected_by": "system_guardrail",
        "violated_rules": [],
        "response_message": response,
        "conversation_id": conversation_id,
        "detection_summary": None,
    }


def _blocked_reason(result: Mapping[str, Any]) -> ReasonCode:
    reason_text = str(result.get("block_reason", "")).lower()
    blocked_by = str(result.get("blocked_by", "")).lower()
    if "output" in reason_text and blocked_by == "pii":
        return ReasonCode.SENSITIVE_OUTPUT
    if blocked_by == "pii":
        return ReasonCode.SENSITIVE_INPUT
    if "prompt_injection" in reason_text:
        return ReasonCode.PROMPT_INJECTION
    return ReasonCode.UNSAFE_CONTENT


def _review_reason(result: Mapping[str, Any]) -> ReasonCode | None:
    candidates = " ".join(
        str(result.get(key, "")).lower()
        for key in ("decision", "reason_code", "block_reason")
    )
    for reason in REVIEW_REASONS:
        if reason.value in candidates:
            return reason
    return None


def normalize_upstream_result(
    result: Mapping[str, Any],
    conversation_id: str,
) -> dict[str, Any]:
    """Normalize an upstream result without exposing detector internals."""

    reason_text = str(result.get("block_reason", "")).lower()
    response_text = str(result.get("response", ""))
    if result.get("error"):
        return unavailable_result(ReasonCode.GUARDRAIL_RUNTIME_ERROR, conversation_id)
    if (
        ReasonCode.ANSWER_MODEL_UNAVAILABLE.value in reason_text
        or response_text == "__AI_BANK_ANSWER_MODEL_UNAVAILABLE__"
    ):
        return unavailable_result(ReasonCode.ANSWER_MODEL_UNAVAILABLE, conversation_id)
    if response_text == "__AI_BANK_EMPTY_ANSWER__":
        return unavailable_result(ReasonCode.EMPTY_ANSWER, conversation_id)
    if response_text == "__AI_BANK_INVALID_ANSWER_CONTRACT__":
        return unavailable_result(ReasonCode.INVALID_ANSWER_CONTRACT, conversation_id)
    if ReasonCode.SECURITY_DETECTOR_UNAVAILABLE.value in reason_text:
        return unavailable_result(
            ReasonCode.SECURITY_DETECTOR_UNAVAILABLE,
            conversation_id,
        )

    review_reason = _review_reason(result)
    if review_reason is not None:
        return review_result(review_reason, conversation_id)

    if result.get("is_blocked") is not True:
        if not response_text.strip():
            return unavailable_result(ReasonCode.EMPTY_ANSWER, conversation_id)
        return {
            "response": response_text,
            "is_blocked": False,
            "is_attack": False,
            "decision": Decision.ALLOW.value,
            "reason_code": None,
            "block_reason": "",
            "blocked_by": None,
            "detected_by": None,
            "violated_rules": [],
            "response_message": None,
            "conversation_id": conversation_id,
            "detection_summary": None,
        }

    reason = _blocked_reason(result)
    response = TEMPLATES[reason]
    is_attack = reason not in {
        ReasonCode.SENSITIVE_INPUT,
        ReasonCode.SENSITIVE_OUTPUT,
    }
    detected_by = str(result.get("blocked_by") or "guardrail")
    return {
        "response": response,
        "is_blocked": True,
        "is_attack": is_attack,
        "decision": Decision.BLOCK.value,
        "reason_code": reason.value,
        "block_reason": reason.value,
        "blocked_by": detected_by,
        "detected_by": detected_by,
        "violated_rules": [],
        "response_message": response,
        "conversation_id": conversation_id,
        "detection_summary": None,
    }


def _input_rail_response_key(
    decision: str,
    reason_code: str | None,
) -> str | None:
    if decision == Decision.BLOCK.value:
        if reason_code == ReasonCode.PROMPT_INJECTION.value:
            return "security.block.prompt_injection"
        if reason_code == ReasonCode.SENSITIVE_INPUT.value:
            return "security.block.sensitive_input"
        return "security.block.default"
    if decision == Decision.UNAVAILABLE.value:
        return "security.unavailable.default"
    return None


def project_policy_match(
    match: PolicyMatch | None,
    *,
    stage: str,
    duration_ms: int,
) -> dict[str, Any]:
    """Project an owned policy decision to the shared value-free Rail contract."""

    decision = Decision.ALLOW.value if match is None else match.decision
    return {
        "railId": "finance-text-safety",
        "stage": stage,
        "decision": decision,
        "reasonCode": None if match is None else match.reason_code,
        "policyVersion": POLICY_VERSION,
        "responseKey": None if match is None else match.response_key,
        "durationMs": max(0, min(600_000, int(duration_ms))),
    }


def project_input_rail_result(
    result: Mapping[str, Any],
    *,
    duration_ms: int,
) -> dict[str, Any]:
    """Project input-only workflow state to the minimal framework Rail contract."""

    if result.get("error"):
        normalized = unavailable_result(
            ReasonCode.GUARDRAIL_RUNTIME_ERROR,
            conversation_id="rail-input",
        )
    elif result.get("is_blocked") is True:
        normalized = normalize_upstream_result(result, conversation_id="rail-input")
    else:
        normalized = {
            "decision": Decision.ALLOW.value,
            "reason_code": None,
        }
    decision = str(normalized["decision"])
    reason_code = normalized.get("reason_code")
    return {
        "railId": "finance-text-safety",
        "stage": "input",
        "decision": decision,
        "reasonCode": reason_code,
        "policyVersion": POLICY_VERSION,
        "responseKey": _input_rail_response_key(decision, reason_code),
        "durationMs": max(0, min(600_000, int(duration_ms))),
    }
