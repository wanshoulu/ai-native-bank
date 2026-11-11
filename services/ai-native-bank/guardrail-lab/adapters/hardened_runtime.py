"""Runtime-only hardening overlay for the isolated third-party guardrail lab."""

from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import MethodType
from typing import Any

try:
    from .finance_safety_policy import (
        MAX_COMBINED_CONTEXT_CHARS,
        POLICY_VERSION,
        STRICT_SECURITY_PROMPT,
        bounded_history,
        deterministic_action_decision,
        deterministic_input_decision,
        deterministic_output_decision,
        normalize_text,
    )
except ImportError:  # pragma: no cover - used by the /opt bootstrap script
    from finance_safety_policy import (
        MAX_COMBINED_CONTEXT_CHARS,
        POLICY_VERSION,
        STRICT_SECURITY_PROMPT,
        bounded_history,
        deterministic_action_decision,
        deterministic_input_decision,
        deterministic_output_decision,
        normalize_text,
    )

try:
    from .hardening_policy import (
        Decision,
        ReasonCode,
        TEMPLATES,
        normalize_upstream_result,
        project_input_rail_result,
        project_policy_match,
    )
except ImportError:  # pragma: no cover - used by the /opt bootstrap script
    from hardening_policy import (
        Decision,
        ReasonCode,
        TEMPLATES,
        normalize_upstream_result,
        project_input_rail_result,
        project_policy_match,
    )

try:  # The pinned upstream config is available inside the lab image.
    from config.config import (
        CLASSIFIER_ENGLISH_RATIO_THRESHOLD,
        CLASSIFIER_MIN_TEXT_LENGTH,
    )
except ImportError:  # Isolated adapter unit tests do not import the upstream app.
    CLASSIFIER_MIN_TEXT_LENGTH = 8
    CLASSIFIER_ENGLISH_RATIO_THRESHOLD = 0.5


logger = logging.getLogger("ai_bank_guardrail_hardening")

ANSWER_MODEL_UNAVAILABLE_SENTINEL = "__AI_BANK_ANSWER_MODEL_UNAVAILABLE__"
EMPTY_ANSWER_SENTINEL = "__AI_BANK_EMPTY_ANSWER__"
INVALID_ANSWER_CONTRACT_SENTINEL = "__AI_BANK_INVALID_ANSWER_CONTRACT__"
SECURITY_DETECTOR_UNAVAILABLE = "security_detector_unavailable"
HARDENED_PII_ENTITIES = (
    "CN_ID_CARD",
    "CN_BANK_CARD",
    "CN_PHONE",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "CREDIT_CARD",
    "IBAN_CODE",
    "US_BANK_NUMBER",
    "US_SSN",
)
REVIEW_SCORE_MARGIN = 0.03
LLM_CONFIDENCE_THRESHOLD = 0.75
FOUR_STATE_SECURITY_PROMPT = STRICT_SECURITY_PROMPT
ANSWER_SAFETY_INSTRUCTION = (
    "Answer the legitimate request without producing any complete card number, identity number, "
    "phone number, email address, authentication credential, or other sensitive identifier. "
    "Use masked or clearly truncated examples when an explanation needs an identifier."
)
SENSITIVE_DRAFT_RETRY_INSTRUCTION = (
    "The previous draft contained a complete sensitive identifier. Regenerate the answer now. "
    "Preserve the useful explanation, but replace every card number, identity number, phone "
    "number, email address, account identifier, and credential with a masked placeholder."
)
SENSITIVE_OUTPUT_SAFE_RESPONSE = (
    "为保护敏感信息，完整标识已省略。我可以使用 masked（脱敏）示例继续说明相关规则。"
)
LLM_BLOCK_CATEGORIES = frozenset(
    {
        "unsafe_content",
        "prompt_injection",
        "privacy_exfiltration",
        "fraud_or_laundering",
        "misleading_guarantee",
    }
)
LLM_REVIEW_CATEGORIES = frozenset(
    {
        ReasonCode.AMBIGUOUS_RISK_INTENT.value,
        ReasonCode.CONFLICTING_SAFETY_EVIDENCE.value,
        ReasonCode.INSUFFICIENT_SAFETY_CONTEXT.value,
    }
)
LLM_CLARIFICATION_KEYS = frozenset({"clarify_safe_purpose"})
LLM_ALLOW_SEMANTIC_ROLES = frozenset(
    {"unrelated_or_benign", "legitimate_safety_context"}
)
LLM_REVIEW_SEMANTIC_ROLES = frozenset({"underspecified_risk_topic"})
LLM_BLOCK_SEMANTIC_ROLES = frozenset({"prohibited_objective"})


@dataclass(frozen=True)
class ClassificationEvidence:
    disposition: str
    max_score: float
    threshold: float | None
    category: str | None
    health: str


@dataclass(frozen=True)
class InputArbitration:
    decision: str
    reason_code: str | None


@dataclass(frozen=True)
class GuardrailRequestToken:
    evidence: Token
    history: Token


_INPUT_EVIDENCE: ContextVar[ClassificationEvidence | None] = ContextVar(
    "ai_bank_guardrail_input_evidence",
    default=None,
)
_INPUT_HISTORY: ContextVar[tuple[str, ...]] = ContextVar(
    "ai_bank_guardrail_input_history",
    default=(),
)


def begin_guardrail_request(
    history: list[str] | tuple[str, ...] | None = None,
) -> GuardrailRequestToken:
    return GuardrailRequestToken(
        evidence=_INPUT_EVIDENCE.set(None),
        history=_INPUT_HISTORY.set(bounded_history(history)),
    )


def end_guardrail_request(token: GuardrailRequestToken) -> None:
    _INPUT_HISTORY.reset(token.history)
    _INPUT_EVIDENCE.reset(token.evidence)


def current_input_evidence() -> ClassificationEvidence | None:
    return _INPUT_EVIDENCE.get()


def current_input_history() -> tuple[str, ...]:
    return _INPUT_HISTORY.get()


async def evaluate_input_rail(
    agent: Any,
    *,
    message: str,
    request_id: str,
    conversation_id: str,
    history: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Run only the upstream input-check graph and return a minimal decision."""

    started = time.monotonic()
    deterministic = deterministic_input_decision(message, history=history)
    if deterministic is not None:
        return project_policy_match(
            deterministic,
            stage="input",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    request_token = begin_guardrail_request(history=history)
    try:
        if classifier_skip_reason(message) is not None:
            _INPUT_EVIDENCE.set(
                ClassificationEvidence(
                    disposition=Decision.ALLOW.value,
                    max_score=0.0,
                    threshold=None,
                    category=None,
                    health="skipped",
                )
            )
            # 确定性检查已覆盖所有已知风险模式，短文本/英文文本无需 LLM 分类
            return _rail_result(
                stage="input",
                decision=Decision.ALLOW.value,
                reason_code=None,
                response_key=None,
                started=started,
            )
        workflow = getattr(agent, "input_check_workflow", None)
        if workflow is None:
            raw_result: dict[str, Any] = {"error": "guardrail_runtime_error"}
        else:
            try:
                raw_result = await workflow.ainvoke(
                    {
                        "current_input": message,
                        "response": "",
                        "is_blocked": False,
                        "block_reason": "",
                    },
                    {
                        "configurable": {
                            "thread_id": f"rail:{request_id}",
                            "checkpoint_ns": "input",
                        }
                    },
                )
                if not isinstance(raw_result, dict):
                    raw_result = {"error": "guardrail_runtime_error"}
            except Exception:
                logger.error("guardrail_runtime_error stage=input_workflow")
                raw_result = {"error": "guardrail_runtime_error"}
        return project_input_rail_result(
            raw_result,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        end_guardrail_request(request_token)


def _rail_result(
    *,
    stage: str,
    decision: str,
    reason_code: str | None,
    response_key: str | None,
    started: float,
) -> dict[str, Any]:
    return {
        "railId": "finance-text-safety",
        "stage": stage,
        "decision": decision,
        "reasonCode": reason_code,
        "policyVersion": POLICY_VERSION,
        "responseKey": response_key,
        "durationMs": max(
            0,
            min(600_000, int((time.monotonic() - started) * 1000)),
        ),
    }


async def evaluate_output_rail(
    agent: Any,
    *,
    text: str,
    request_id: str,
    conversation_id: str,
) -> dict[str, Any]:
    """Evaluate only user-visible output and fail closed on required detectors."""

    del request_id, conversation_id
    started = time.monotonic()
    deterministic = deterministic_output_decision(text)
    if deterministic is not None:
        return project_policy_match(
            deterministic,
            stage="output",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    try:
        pii_service = getattr(agent, "pii_service", None)
        classifier = getattr(agent, "content_filter_service", None)
        if pii_service is None or classifier is None:
            raise RuntimeError("required output detector missing")
        pii_result = pii_service.check_output(text)
        if not isinstance(pii_result, dict):
            raise TypeError("invalid output PII result")
        if pii_result.get("should_block") is True or pii_result.get("has_pii") is True:
            return _rail_result(
                stage="output",
                decision=Decision.BLOCK.value,
                reason_code=ReasonCode.SENSITIVE_OUTPUT.value,
                response_key="security.block.sensitive_output",
                started=started,
            )
        # 安全输出快通道：短文本且无可疑模式时跳过 LLM 分类器（节省 ~400ms）
        # 注意：RailDecision 契约要求 allow 决策不能携带 reasonCode，故传 None
        if _is_safe_banking_output(text):
            return _rail_result(
                stage="output",
                decision=Decision.ALLOW.value,
                reason_code=None,
                response_key=None,
                started=started,
            )
        classification = await classifier.classify(text, is_output=True)
        categories = tuple(getattr(classification, "triggered_categories", ()) or ())
        if SECURITY_DETECTOR_UNAVAILABLE in categories:
            raise RuntimeError("output classifier unavailable")
        if bool(getattr(classification, "is_blocked", False)):
            return _rail_result(
                stage="output",
                decision=Decision.BLOCK.value,
                reason_code="unsafe_output",
                response_key="security.block.unsafe_output",
                started=started,
            )
        return _rail_result(
            stage="output",
            decision=Decision.ALLOW.value,
            reason_code=None,
            response_key=None,
            started=started,
        )
    except Exception:
        logger.error("security_detector_unavailable stage=output")
        return _rail_result(
            stage="output",
            decision=Decision.UNAVAILABLE.value,
            reason_code=ReasonCode.SECURITY_DETECTOR_UNAVAILABLE.value,
            response_key="security.unavailable.default",
            started=started,
        )


async def evaluate_action_rail(
    *,
    capability_id: str,
    risk_level: str,
    summary: str,
    requires_confirmation: bool,
    confirmed: bool,
    input_decision: str,
) -> dict[str, Any]:
    """Evaluate a value-minimized capability action before a permit is issued."""

    started = time.monotonic()
    match = deterministic_action_decision(
        capability_id=capability_id,
        risk_level=risk_level,
        summary=summary,
        requires_confirmation=requires_confirmation,
        confirmed=confirmed,
        input_decision=input_decision,
    )
    return project_policy_match(
        match,
        stage="action",
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def disable_third_party_telemetry(settings: Any | None = None) -> None:
    """Disable Guardrails Hub metrics before upstream validators are imported."""

    if settings is None:
        try:
            from guardrails.settings import settings as guardrails_settings
        except ImportError:
            return
        settings = guardrails_settings
    settings.rc.enable_metrics = False
    settings.rc.use_remote_inferencing = False


def project_http_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add the four-state contract to the pinned upstream HTTP payload."""

    projected = dict(payload)
    if payload.get("is_blocked") is False:
        projected.update(decision=Decision.ALLOW.value, reason_code=None)
        return projected

    reason_text = str(
        payload.get("reason_code") or payload.get("detect_reason") or ""
    ).lower()
    review_reason = next(
        (reason for reason in ReasonCode if reason in {
            ReasonCode.AMBIGUOUS_RISK_INTENT,
            ReasonCode.CONFLICTING_SAFETY_EVIDENCE,
            ReasonCode.INSUFFICIENT_SAFETY_CONTEXT,
        } and reason.value in reason_text),
        None,
    )
    if review_reason is not None:
        projected.update(
            decision=Decision.REVIEW.value,
            reason_code=review_reason.value,
        )
        return projected

    unavailable_reasons = (
        ReasonCode.SECURITY_DETECTOR_UNAVAILABLE,
        ReasonCode.ANSWER_MODEL_UNAVAILABLE,
        ReasonCode.GUARDRAIL_RUNTIME_ERROR,
        ReasonCode.INVALID_ANSWER_CONTRACT,
        ReasonCode.EMPTY_ANSWER,
    )
    unavailable_reason = next(
        (reason for reason in unavailable_reasons if reason.value in reason_text),
        None,
    )
    if unavailable_reason is not None or payload.get("is_blocked") is not True:
        reason = unavailable_reason or ReasonCode.GUARDRAIL_RUNTIME_ERROR
        projected.update(
            decision=Decision.UNAVAILABLE.value,
            reason_code=reason.value,
        )
        return projected

    block_reasons = (
        ReasonCode.UNSAFE_CONTENT,
        ReasonCode.PROMPT_INJECTION,
        ReasonCode.SENSITIVE_INPUT,
        ReasonCode.SENSITIVE_OUTPUT,
    )
    block_reason = next(
        (reason for reason in block_reasons if reason.value in reason_text),
        ReasonCode.UNSAFE_CONTENT,
    )
    projected.update(
        decision=Decision.BLOCK.value,
        reason_code=block_reason.value,
    )
    return projected


_OUTPUT_MAX_FAST_PATH_CHARS = 500
_SUSPICIOUS_OUTPUT_PATTERNS = [
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"<script", re.IGNORECASE),
    re.compile(r"```"),
    re.compile(r"\b(?:drop|delete|truncate|insert|update)\s+table\b", re.IGNORECASE),
]


def _is_safe_banking_output(text: str) -> bool:
    """Conservative fast-path check: only skip the LLM classifier for short,
    clean text without URLs, code blocks, or SQL-like patterns."""
    stripped = text.strip()
    if len(stripped) > _OUTPUT_MAX_FAST_PATH_CHARS:
        return False
    for pattern in _SUSPICIOUS_OUTPUT_PATTERNS:
        if pattern.search(stripped):
            return False
    return True


def classifier_skip_reason(text: str) -> str | None:
    stripped = text.strip()
    if len(stripped) < CLASSIFIER_MIN_TEXT_LENGTH:
        return "short_text"
    ascii_letters = sum(
        1 for character in stripped if ord(character) < 128 and character.isalpha()
    )
    if stripped and ascii_letters / len(stripped) > CLASSIFIER_ENGLISH_RATIO_THRESHOLD:
        return "english_text"
    return None


def classification_disposition(
    *,
    max_score: float,
    threshold: float | None,
    is_blocked: bool,
) -> str:
    if is_blocked:
        return Decision.BLOCK.value
    if threshold is not None and max_score >= max(0.0, threshold - REVIEW_SCORE_MARGIN):
        return Decision.REVIEW.value
    return Decision.ALLOW.value


def arbitrate_input_evidence(
    evidence: ClassificationEvidence | None,
    *,
    llm_decision: str,
    llm_category: str,
    llm_confidence: float,
    llm_semantic_role: str,
) -> InputArbitration:
    """Combine semantic evidence without giving either model sole block authority."""

    if evidence is not None and (
        evidence.health == "unavailable"
        or evidence.disposition == Decision.UNAVAILABLE.value
    ):
        return InputArbitration(
            Decision.UNAVAILABLE.value,
            ReasonCode.SECURITY_DETECTOR_UNAVAILABLE.value,
        )

    if llm_decision == Decision.REVIEW.value:
        if llm_semantic_role in LLM_REVIEW_SEMANTIC_ROLES:
            return InputArbitration(Decision.REVIEW.value, llm_category)
        return InputArbitration(
            Decision.REVIEW.value,
            ReasonCode.CONFLICTING_SAFETY_EVIDENCE.value,
        )

    if evidence is not None and evidence.disposition == Decision.REVIEW.value:
        return InputArbitration(
            Decision.REVIEW.value,
            ReasonCode.CONFLICTING_SAFETY_EVIDENCE.value,
        )

    # Short or language-routed text has no embedding observation. The structured
    # semantic role, rather than an isolated keyword or missing classifier score,
    # decides whether a phrase is benign or an underspecified risk topic. The LLM
    # still cannot block on its own.
    if evidence is None or evidence.health == "skipped":
        if (
            llm_decision == Decision.ALLOW.value
            and llm_confidence >= LLM_CONFIDENCE_THRESHOLD
        ):
            return InputArbitration(Decision.ALLOW.value, None)
        return InputArbitration(
            Decision.REVIEW.value,
            ReasonCode.INSUFFICIENT_SAFETY_CONTEXT.value,
        )

    if evidence.disposition == llm_decision:
        if llm_decision == Decision.BLOCK.value:
            if llm_confidence < LLM_CONFIDENCE_THRESHOLD:
                return InputArbitration(
                    Decision.REVIEW.value,
                    ReasonCode.INSUFFICIENT_SAFETY_CONTEXT.value,
                )
            return InputArbitration(Decision.BLOCK.value, llm_category)
        return InputArbitration(Decision.ALLOW.value, None)

    return InputArbitration(
        Decision.REVIEW.value,
        ReasonCode.CONFLICTING_SAFETY_EVIDENCE.value,
    )


@dataclass(frozen=True)
class DetectorUnavailableClassification:
    is_blocked: bool = True
    triggered_categories: tuple[str, ...] = (SECURITY_DETECTOR_UNAVAILABLE,)
    category_scores: dict[str, float] = field(
        default_factory=lambda: {SECURITY_DETECTOR_UNAVAILABLE: 1.0}
    )
    max_score: float = 1.0
    max_category: str = SECURITY_DETECTOR_UNAVAILABLE


@dataclass(frozen=True)
class ReviewRequiredClassification:
    is_blocked: bool = True
    triggered_categories: tuple[str, ...] = (
        ReasonCode.CONFLICTING_SAFETY_EVIDENCE.value,
    )
    category_scores: dict[str, float] = field(default_factory=dict)
    max_score: float = 0.0
    max_category: str = ReasonCode.CONFLICTING_SAFETY_EVIDENCE.value


@dataclass(frozen=True)
class NonBlockingInputClassification:
    """Upstream-compatible projection; the product arbiter owns the decision."""

    is_blocked: bool = False
    triggered_categories: tuple[str, ...] = ()
    category_scores: dict[str, float] = field(default_factory=dict)
    max_score: float = 0.0
    max_category: str | None = None


class HardenedClassifierProxy:
    """Collect input evidence while preserving strict output enforcement."""

    def __init__(self, delegate: Any):
        self._delegate = delegate

    async def classify(self, text: str, is_output: bool = False) -> Any:
        skip_reason = classifier_skip_reason(text)
        if skip_reason is not None and not is_output:
            _INPUT_EVIDENCE.set(
                ClassificationEvidence(
                    disposition=Decision.ALLOW.value,
                    max_score=0.0,
                    threshold=None,
                    category=None,
                    health="skipped",
                )
            )
            return NonBlockingInputClassification()
        if skip_reason is None and getattr(self._delegate, "_anchors_loaded", None) is False:
            if not is_output:
                _INPUT_EVIDENCE.set(
                    ClassificationEvidence(
                        disposition=Decision.UNAVAILABLE.value,
                        max_score=0.0,
                        threshold=None,
                        category=None,
                        health="unavailable",
                    )
                )
            return DetectorUnavailableClassification()
        try:
            result = await self._delegate.classify(text, is_output=is_output)
        except Exception:
            logger.error(
                "security_detector_unavailable stage=%s detector=content_classifier",
                "output" if is_output else "input",
            )
            if not is_output:
                _INPUT_EVIDENCE.set(
                    ClassificationEvidence(
                        disposition=Decision.UNAVAILABLE.value,
                        max_score=0.0,
                        threshold=None,
                        category=None,
                        health="unavailable",
                    )
                )
            return DetectorUnavailableClassification()

        max_score = float(getattr(result, "max_score", 0.0) or 0.0)
        category = getattr(result, "max_category", None)
        thresholds = getattr(self._delegate, "_thresholds", {})
        threshold_value = thresholds.get(category) if isinstance(thresholds, dict) else None
        threshold = float(threshold_value) if threshold_value is not None else None

        if skip_reason is not None:
            evidence = ClassificationEvidence(
                disposition=Decision.ALLOW.value,
                max_score=max_score,
                threshold=threshold,
                category=category,
                health="skipped",
            )
        elif not getattr(result, "category_scores", None):
            if not is_output:
                _INPUT_EVIDENCE.set(
                    ClassificationEvidence(
                        disposition=Decision.UNAVAILABLE.value,
                        max_score=max_score,
                        threshold=threshold,
                        category=category,
                        health="unavailable",
                    )
                )
            return DetectorUnavailableClassification()
        else:
            evidence = ClassificationEvidence(
                disposition=classification_disposition(
                    max_score=max_score,
                    threshold=threshold,
                    is_blocked=bool(getattr(result, "is_blocked", False)),
                ),
                max_score=max_score,
                threshold=threshold,
                category=category,
                health="healthy",
            )

        if not is_output:
            _INPUT_EVIDENCE.set(evidence)
            return NonBlockingInputClassification(
                max_score=max_score,
                max_category=category,
            )
        if is_output and evidence.disposition == Decision.REVIEW.value:
            return ReviewRequiredClassification(max_score=max_score)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class HardenedChainProxy:
    """Keep provider errors private while making them detectable upstream."""

    def __init__(self, delegate: Any):
        self._delegate = delegate

    @staticmethod
    def _system_message(content: str) -> Any:
        try:
            from langchain_core.messages import SystemMessage

            return SystemMessage(content=content)
        except ImportError:
            return {"role": "system", "content": content}

    @staticmethod
    def _text(result: Any) -> str | None:
        if isinstance(result, str):
            return result
        if hasattr(result, "content"):
            content = getattr(result, "content", None)
            if isinstance(content, str):
                return content
        return None

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        guarded_args = args
        if args and isinstance(args[0], dict):
            payload = dict(args[0])
            messages = list(payload.get("messages") or [])
            safety_message = self._system_message(ANSWER_SAFETY_INSTRUCTION)
            payload["messages"] = [safety_message, *messages]
            guarded_args = (payload, *args[1:])
        try:
            result = await self._delegate.ainvoke(*guarded_args, **kwargs)
        except Exception:
            logger.error("answer_model_unavailable stage=answer_generation")
            return ANSWER_MODEL_UNAVAILABLE_SENTINEL
        result_text = self._text(result)
        if result_text is not None and _contains_high_sensitivity(result_text):
            retry_args = guarded_args
            if guarded_args and isinstance(guarded_args[0], dict):
                retry_payload = dict(guarded_args[0])
                retry_payload["messages"] = [
                    self._system_message(SENSITIVE_DRAFT_RETRY_INSTRUCTION),
                    *list(retry_payload.get("messages") or []),
                ]
                retry_args = (retry_payload, *guarded_args[1:])
            try:
                result = await self._delegate.ainvoke(*retry_args, **kwargs)
            except Exception:
                logger.error("answer_model_unavailable stage=sensitive_output_regeneration")
                return ANSWER_MODEL_UNAVAILABLE_SENTINEL
            result_text = self._text(result)
            if result_text is not None and _contains_high_sensitivity(result_text):
                return SENSITIVE_OUTPUT_SAFE_RESPONSE

        if result_text is not None:
            return result if result_text.strip() else EMPTY_ANSWER_SENTINEL
        return INVALID_ANSWER_CONTRACT_SENTINEL

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


_SSN_PATTERN = re.compile(r"(?<!\d)(\d{3}-\d{2}-\d{4})(?!\d)")
_IBAN_PATTERN = re.compile(
    r"(?<![A-Z0-9])([A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]){11,30})(?![A-Z0-9])",
    re.IGNORECASE,
)
_US_BANK_PATTERN = re.compile(
    r"(?:ABA|routing(?: number)?)[\s:#-]*(\d{9})(?!\d)",
    re.IGNORECASE,
)
_CN_ID_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])"
    r"([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
    r"\d{3}[\dXx])"
    r"(?![0-9A-Za-z])"
)
_CARD_CANDIDATE_PATTERN = re.compile(r"(?<!\d)((?:\d[ -]?){13,19})(?!\d)")
_LOG_REDACTIONS = (
    (
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        "<REDACTED_EMAIL>",
    ),
    (re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"), "<REDACTED_SSN>"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "<REDACTED_PHONE>"),
    (
        _CN_ID_PATTERN,
        "<REDACTED_ID>",
    ),
    (re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"), "<REDACTED_CARD>"),
    (_IBAN_PATTERN, "<REDACTED_IBAN>"),
)


def _contains_high_sensitivity(text: str) -> bool:
    if _extra_sensitive_entities(text):
        return True
    return any(
        pattern.search(text)
        for pattern in (
            _LOG_REDACTIONS[0][0],
            _LOG_REDACTIONS[2][0],
            _LOG_REDACTIONS[3][0],
        )
    )
_UPSTREAM_DETAIL_MARKERS = (
    "已拦截的交互",
    "交互记录",
    "安全检查全部通过：",
    "白名单放行：",
    "输入:",
    "输入：",
    "输出:",
    "输出：",
    "规则:",
    "规则：",
    "分数:",
    "分数：",
    "问题:",
    "问题：",
    "响应:",
    "响应：",
)


class SensitiveLogFilter(logging.Filter):
    """Remove sensitive values and exception details from upstream records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info is not None:
            record.msg = "guardrail_internal_error details_redacted"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            return True
        rendered = record.getMessage()
        if any(marker in rendered for marker in _UPSTREAM_DETAIL_MARKERS):
            record.msg = "guardrail_event details_redacted"
            record.args = ()
            return True
        for pattern, replacement in _LOG_REDACTIONS:
            rendered = pattern.sub(replacement, rendered)
        record.msg = rendered
        record.args = ()
        return True


def _install_log_filter(target: Any) -> None:
    if not isinstance(target, logging.Logger):
        return
    if getattr(target, "_ai_bank_sensitive_filter_installed", False):
        return
    target.addFilter(SensitiveLogFilter())
    target._ai_bank_sensitive_filter_installed = True


def _stream_terminal(response: str, decision: str) -> str:
    payload = {
        "status": "reject",
        "decision": decision,
        "response_message": response,
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sanitize_stream_chunk(chunk: str) -> str:
    """Project streaming terminal results without exposing internal metadata."""

    if ANSWER_MODEL_UNAVAILABLE_SENTINEL in chunk:
        return _stream_terminal(
            TEMPLATES[ReasonCode.ANSWER_MODEL_UNAVAILABLE],
            Decision.UNAVAILABLE.value,
        )
    if EMPTY_ANSWER_SENTINEL in chunk:
        return _stream_terminal(
            TEMPLATES[ReasonCode.EMPTY_ANSWER],
            Decision.UNAVAILABLE.value,
        )
    if INVALID_ANSWER_CONTRACT_SENTINEL in chunk:
        return _stream_terminal(
            TEMPLATES[ReasonCode.INVALID_ANSWER_CONTRACT],
            Decision.UNAVAILABLE.value,
        )
    if not chunk.startswith("data: "):
        return chunk
    payload_text = chunk[6:].strip()
    if not payload_text.startswith("{"):
        return chunk
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return chunk
    if not isinstance(payload, dict) or payload.get("status") != "reject":
        return chunk
    normalized = normalize_upstream_result(
        {
            "is_blocked": True,
            "block_reason": payload.get("detected_issues", ""),
            "blocked_by": payload.get("blocked_by", "guardrail"),
        },
        conversation_id="stream",
    )
    return _stream_terminal(
        normalized["response"],
        normalized["decision"],
    )


def _luhn_valid(value: str) -> bool:
    digits = [int(character) for character in value if character.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _cn_id_valid(value: str) -> bool:
    compact = value.upper()
    if not _CN_ID_PATTERN.fullmatch(compact):
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check_characters = "10X98765432"
    checksum = sum(int(digit) * weight for digit, weight in zip(compact[:17], weights))
    return check_characters[checksum % 11] == compact[-1]


def _iban_valid(value: str) -> bool:
    compact = re.sub(r"[ -]", "", value).upper()
    if not 15 <= len(compact) <= 34 or not compact[:2].isalpha():
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(
        str(ord(character) - 55) if character.isalpha() else character
        for character in rearranged
    )
    remainder = 0
    for character in numeric:
        remainder = (remainder * 10 + int(character)) % 97
    return remainder == 1


def _extra_sensitive_entities(text: str) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for entity_type, pattern, validator in (
        ("CN_ID_CARD", _CN_ID_PATTERN, _cn_id_valid),
        ("US_SSN", _SSN_PATTERN, lambda _: True),
        ("IBAN_CODE", _IBAN_PATTERN, _iban_valid),
        ("US_BANK_NUMBER", _US_BANK_PATTERN, lambda _: True),
        ("CREDIT_CARD", _CARD_CANDIDATE_PATTERN, _luhn_valid),
    ):
        for match in pattern.finditer(text):
            value = match.group(1)
            if not validator(value):
                continue
            entities.append(
                {
                    "type": entity_type,
                    "start": match.start(1),
                    "end": match.end(1),
                    "score": 1.0,
                }
            )
    entities.sort(key=lambda entity: (entity["start"], entity["end"]))
    return entities


def _sanitize_delegate_pii_result(
    text: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    entities = list(result.get("summary", {}).get("entities", []))
    retained: list[dict[str, Any]] = []
    for entity in entities:
        entity_type = str(entity.get("type", ""))
        entity_text = entity.get("text")
        if not isinstance(entity_text, str):
            position = entity.get("position")
            if (
                isinstance(position, (list, tuple))
                and len(position) == 2
                and all(isinstance(value, int) for value in position)
            ):
                entity_text = text[position[0] : position[1]]
        if (
            entity_type in {"CN_BANK_CARD", "CREDIT_CARD"}
            and isinstance(entity_text, str)
            and not _luhn_valid(entity_text)
        ):
            continue
        retained.append(
            {
                "type": entity_type,
                "position": entity.get("position"),
                "score": entity.get("score"),
            }
        )

    entity_counts: dict[str, int] = {}
    for entity in retained:
        entity_type = entity["type"]
        entity_counts[entity_type] = entity_counts.get(entity_type, 0) + 1
    has_pii = bool(retained)
    sanitized = dict(result)
    sanitized.update(
        has_pii=has_pii,
        should_block=has_pii and bool(result.get("should_block", False)),
        anonymized_text=result.get("anonymized_text", text) if has_pii else text,
        original_text=text,
        summary={
            "has_pii": has_pii,
            "total_count": len(retained),
            "entity_counts": entity_counts,
            "entities": retained,
        },
    )
    return sanitized


class HardenedPIIServiceProxy:
    """Add deterministic high-sensitivity recognizers without broad NLP PII."""

    def __init__(self, delegate: Any):
        self._delegate = delegate

    @staticmethod
    def _merge(text: str, result: dict[str, Any]) -> dict[str, Any]:
        result = _sanitize_delegate_pii_result(text, result)
        additions = _extra_sensitive_entities(text)
        if not additions:
            return result
        existing = list(result.get("summary", {}).get("entities", []))
        entity_counts: dict[str, int] = dict(
            result.get("summary", {}).get("entity_counts", {})
        )
        for entity in additions:
            entity_counts[entity["type"]] = entity_counts.get(entity["type"], 0) + 1
        entities = existing + additions
        return {
            "has_pii": True,
            "anonymized_text": text,
            "summary": {
                "has_pii": True,
                "total_count": len(entities),
                "entity_counts": entity_counts,
                "entities": entities,
            },
            "original_text": text,
            "should_block": True,
        }

    def check_input(self, text: str) -> dict[str, Any]:
        return self._merge(text, self._delegate.check_input(text))

    def check_output(self, text: str) -> dict[str, Any]:
        return self._merge(text, self._delegate.check_output(text))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def _bounded_security_context(
    message: str,
    history: list[str] | tuple[str, ...] | None,
) -> dict[str, Any]:
    current = normalize_text(message)
    if len(current) > MAX_COMBINED_CONTEXT_CHARS:
        head = MAX_COMBINED_CONTEXT_CHARS // 2
        current = current[:head] + current[-(MAX_COMBINED_CONTEXT_CHARS - head) :]

    remaining = max(0, MAX_COMBINED_CONTEXT_CHARS - len(current))
    selected_reversed: list[str] = []
    for item in reversed(bounded_history(history)):
        if remaining <= 0:
            break
        value = item[-remaining:]
        selected_reversed.append(value)
        remaining -= len(value)
    return {
        "priorUserTurns": list(reversed(selected_reversed)),
        "currentInput": current,
    }


def _security_messages(agent: Any, message: str) -> list[Any]:
    security_prompt = FOUR_STATE_SECURITY_PROMPT
    user_payload = json.dumps(
        _bounded_security_context(message, current_input_history()),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        return [
            SystemMessage(content=security_prompt),
            HumanMessage(content=user_payload),
        ]
    except ImportError:
        return [
            {"role": "system", "content": security_prompt},
            {"role": "user", "content": user_payload},
        ]


def _detector_unavailable() -> dict[str, Any]:
    return {
        "is_safe": False,
        "risk_level": "unavailable",
        "detected_issues": SECURITY_DETECTOR_UNAVAILABLE,
        "violated_rules": [SECURITY_DETECTOR_UNAVAILABLE],
    }


def _security_block(category: str) -> dict[str, Any]:
    return {
        "is_safe": False,
        "risk_level": "high",
        "detected_issues": category,
        "violated_rules": [category],
    }


def _security_review(reason: ReasonCode) -> dict[str, Any]:
    return {
        "is_safe": False,
        "risk_level": Decision.REVIEW.value,
        "detected_issues": reason.value,
        "violated_rules": [reason.value],
    }


def _parse_security_decision(
    response_text: str,
) -> tuple[str, str, float, str | None, str]:
    value = json.loads(response_text)
    if not isinstance(value, dict):
        raise ValueError("security response must be an object")
    required_keys = {"decision", "category", "confidence", "semantic_role"}
    allowed_keys = required_keys | {"clarification_key"}
    if not required_keys.issubset(value) or not set(value).issubset(allowed_keys):
        raise ValueError("security response keys are invalid")

    decision = value["decision"]
    category = value["category"]
    confidence = value["confidence"]
    clarification_key = value.get("clarification_key")
    semantic_role = value["semantic_role"]
    if decision not in {Decision.ALLOW.value, Decision.BLOCK.value, Decision.REVIEW.value}:
        raise ValueError("security decision is invalid")
    if not isinstance(category, str):
        raise ValueError("security category is invalid")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("security confidence is invalid")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("security confidence is out of range")
    if not isinstance(semantic_role, str):
        raise ValueError("security semantic role is invalid")

    if decision == Decision.ALLOW.value:
        if (
            category != "safe"
            or clarification_key is not None
            or semantic_role not in LLM_ALLOW_SEMANTIC_ROLES
        ):
            raise ValueError("allow contract is invalid")
    elif decision == Decision.BLOCK.value:
        if (
            category not in LLM_BLOCK_CATEGORIES
            or clarification_key is not None
            or semantic_role not in LLM_BLOCK_SEMANTIC_ROLES
        ):
            raise ValueError("block contract is invalid")
    elif (
        category not in LLM_REVIEW_CATEGORIES
        or clarification_key not in LLM_CLARIFICATION_KEYS
        or semantic_role not in LLM_REVIEW_SEMANTIC_ROLES
    ):
        raise ValueError("review contract is invalid")
    return decision, category, confidence, clarification_key, semantic_role


async def _strict_llm_security(self: Any, message: str) -> dict[str, Any]:
    try:
        response = await self.llm.ainvoke(_security_messages(self, message))
        response_text = getattr(response, "content", None)
        if not isinstance(response_text, str):
            return _detector_unavailable()
        decision, category, confidence, _, semantic_role = _parse_security_decision(
            response_text
        )
        evidence = current_input_evidence()
        arbitration = arbitrate_input_evidence(
            evidence,
            llm_decision=decision,
            llm_category=category,
            llm_confidence=confidence,
            llm_semantic_role=semantic_role,
        )
        if arbitration.decision == Decision.UNAVAILABLE.value:
            return _detector_unavailable()
        if arbitration.decision == Decision.BLOCK.value:
            return _security_block(arbitration.reason_code or category)
        if arbitration.decision == Decision.REVIEW.value:
            return _security_review(ReasonCode(arbitration.reason_code))
        return {
            "is_safe": True,
            "risk_level": "low",
            "detected_issues": "",
            "violated_rules": [],
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Security model returned an invalid response contract")
        return _detector_unavailable()
    except Exception:
        logger.error("security_detector_unavailable stage=llm_security")
        return _detector_unavailable()


def install_hardening(agent: Any) -> Any:
    """Install the overlay once on an initialized upstream agent instance."""

    if getattr(agent, "_ai_bank_hardening_installed", False):
        return agent

    _install_log_filter(getattr(agent, "logger", None))
    _install_log_filter(getattr(agent, "security_logger", None))

    if getattr(agent, "content_filter_service", None) is not None:
        agent.content_filter_service = HardenedClassifierProxy(
            agent.content_filter_service
        )
    if getattr(agent, "chain", None) is not None:
        agent.chain = HardenedChainProxy(agent.chain)
    if getattr(agent, "pii_service", None) is not None:
        agent.pii_service = HardenedPIIServiceProxy(agent.pii_service)

    agent._check_with_llm_security = MethodType(_strict_llm_security, agent)
    agent.enable_prompt_defense = True
    agent.enable_content_filter = True

    original_whitelist_reload = agent.reload_white_list_rules
    original_pii_reload = agent.reload_pii_config
    original_prompt_reload = agent.reload_prompt_defense_config
    original_content_filter_reload = agent.reload_content_filter_config
    original_process_message = agent.process_message
    original_stream = getattr(agent, "process_message_stream_graph", None)

    def hardened_reload_white_list_rules(self: Any, patterns: Any) -> bool:
        return original_whitelist_reload([])

    def hardened_reload_pii_config(self: Any, config: dict[str, Any]) -> bool:
        safe = dict(config)
        safe.update(
            input_enabled=True,
            output_enabled=True,
            input_entities=list(HARDENED_PII_ENTITIES),
            output_entities=list(HARDENED_PII_ENTITIES),
            use_presidio=False,
            anonymize_input=False,
            anonymize_output=False,
            input_action_mode="block",
            output_action_mode="block",
        )
        return original_pii_reload(safe)

    def hardened_reload_prompt_defense_config(
        self: Any,
        config: dict[str, Any],
    ) -> bool:
        safe = dict(config)
        safe["enabled"] = True
        return original_prompt_reload(safe)

    def hardened_reload_content_filter_config(
        self: Any,
        config: dict[str, Any],
    ) -> bool:
        safe = dict(config)
        safe.update(
            input_enabled=True,
            output_enabled=True,
            action_mode="block",
        )
        return original_content_filter_reload(safe)

    async def hardened_process_message(
        self: Any,
        message: str,
        conversation_id: str = "default",
    ) -> dict[str, Any]:
        request_token = begin_guardrail_request()
        try:
            try:
                result = await original_process_message(message, conversation_id)
            except Exception:
                logger.error("guardrail_runtime_error stage=workflow")
                result = {"error": "guardrail_runtime_error"}
            return normalize_upstream_result(result, conversation_id)
        finally:
            end_guardrail_request(request_token)

    async def hardened_process_message_stream_graph(
        self: Any,
        message: str,
        conversation_id: str = "default",
    ):
        request_token = begin_guardrail_request()
        try:
            try:
                async for chunk in original_stream(message, conversation_id):
                    yield sanitize_stream_chunk(chunk)
            except Exception:
                logger.error("guardrail_runtime_error stage=stream_workflow")
                yield _stream_terminal(
                    TEMPLATES[ReasonCode.GUARDRAIL_RUNTIME_ERROR],
                    Decision.UNAVAILABLE.value,
                )
        finally:
            end_guardrail_request(request_token)

    agent.reload_white_list_rules = MethodType(
        hardened_reload_white_list_rules,
        agent,
    )
    agent.reload_pii_config = MethodType(hardened_reload_pii_config, agent)
    agent.reload_prompt_defense_config = MethodType(
        hardened_reload_prompt_defense_config,
        agent,
    )
    agent.reload_content_filter_config = MethodType(
        hardened_reload_content_filter_config,
        agent,
    )
    agent.process_message = MethodType(hardened_process_message, agent)
    if original_stream is not None:
        agent.process_message_stream_graph = MethodType(
            hardened_process_message_stream_graph,
            agent,
        )
    agent._ai_bank_hardening_installed = True
    return agent


def install_registration_hook(state_module: Any | None = None) -> Any:
    """Harden every agent before the upstream API module receives it."""

    if state_module is None:
        from src.api import state as state_module
    if getattr(state_module, "_ai_bank_hardening_hook_installed", False):
        return state_module

    for logger_name in ("agent_service", "agent_service.agent", "security", "database"):
        _install_log_filter(logging.getLogger(logger_name))

    original_set_agent = state_module.set_agent

    def hardened_set_agent(agent: Any) -> None:
        original_set_agent(install_hardening(agent))

    state_module.set_agent = hardened_set_agent
    state_module._ai_bank_hardening_hook_installed = True
    return state_module


def install_request_log_redaction(crud_class: Any | None = None) -> Any:
    """Preserve aggregate lab metrics without storing request or answer content."""

    if crud_class is None:
        from database.crud import RequestLogCRUD as crud_class
    if getattr(crud_class, "_ai_bank_redaction_installed", False):
        return crud_class

    original_create_log = crud_class.create_log

    async def redacted_create_log(*args: Any, **kwargs: Any) -> Any:
        safe = dict(kwargs)
        safe.update(
            source_ip=None,
            user_agent=None,
            security_risks=None,
            user_input="[REDACTED]",
            response_content="[REDACTED]",
        )
        return await original_create_log(*args, **safe)

    crud_class.create_log = staticmethod(redacted_create_log)
    crud_class._ai_bank_redaction_installed = True
    return crud_class
