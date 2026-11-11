"""Deterministic semantic-slot normalization and completeness evaluation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

from app.contracts import SlotValue
from app.intent_graph import IntentSlotContract, SlotDefinition


SlotState = Literal[
    "missing", "extracted", "resolved", "ambiguous", "invalid", "optional_absent"
]


@dataclass(frozen=True)
class SlotAssessment:
    name: str
    label: str
    state: SlotState
    semantic_value: object | None = None
    resolved_value: object | None = None


@dataclass(frozen=True)
class SlotEvaluation:
    assessments: tuple[SlotAssessment, ...]
    semantic_values: Mapping[str, object]
    resolved_values: Mapping[str, object]
    missing: tuple[str, ...]
    ambiguous: tuple[str, ...]
    invalid: tuple[str, ...]
    recognized: tuple[str, ...]
    optional_absent: tuple[str, ...]
    readiness: Literal["ready", "needs_slots", "needs_clarification"]

    def assessment(self, name: str) -> SlotAssessment:
        for item in self.assessments:
            if item.name == name:
                return item
        raise KeyError(name)


@dataclass(frozen=True)
class ReferenceResolution:
    state: Literal["missing", "resolved", "ambiguous"]
    value: object | None = None


def resolve_reference(matches: Sequence[object]) -> ReferenceResolution:
    if not matches:
        return ReferenceResolution("missing")
    if len(matches) != 1:
        return ReferenceResolution("ambiguous")
    return ReferenceResolution("resolved", matches[0])


_PROVIDER_REFERENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_CHINESE_MONTHS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}


def _most_recent_year_month(month: int, reference_date: date) -> str:
    year = (
        reference_date.year
        if month <= reference_date.month
        else reference_date.year - 1
    )
    return f"{year:04d}-{month:02d}"


def _extract_year_month_candidates(
    value: str, *, reference_date: date
) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKC", value).strip()
    candidates: list[str] = []
    if re.search(r"(?:本月|这个月|当月)", text):
        candidates.append(f"{reference_date.year:04d}-{reference_date.month:02d}")
    if re.search(r"(?:上月|上个月)", text):
        year = reference_date.year
        month = reference_date.month - 1
        if month == 0:
            year -= 1
            month = 12
        candidates.append(f"{year:04d}-{month:02d}")

    explicit = [
        f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
        for match in re.finditer(
            r"(?<!\d)(20\d{2})\s*(?:年|[-/.])\s*(0?[1-9]|1[0-2])\s*月?",
            text,
        )
    ]
    candidates.extend(explicit)
    if not explicit:
        candidates.extend(
            _most_recent_year_month(int(match.group(1)), reference_date)
            for match in re.finditer(r"(?<!\d)(0?[1-9]|1[0-2])\s*月", text)
        )
        candidates.extend(
            _most_recent_year_month(
                _CHINESE_MONTHS[match.group(1)], reference_date
            )
            for match in re.finditer(
                r"(十一|十二|十|[一二三四五六七八九])月", text
            )
        )
    return tuple(dict.fromkeys(candidates))


def normalize_year_month(
    value: object, *, reference_date: date | None = None
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidates = _extract_year_month_candidates(
        value, reference_date=reference_date or date.today()
    )
    return candidates[0] if len(candidates) == 1 else None


def extract_normalized_message_slots(
    contract: IntentSlotContract,
    message: str,
    *,
    reference_date: date | None = None,
) -> dict[str, SlotValue]:
    """Extract unambiguous values for graph-declared normalizers only."""

    result: dict[str, SlotValue] = {}
    for definition in contract.slots:
        normalized: object | None = None
        if definition.normalizer == "year_month":
            normalized = normalize_year_month(
                message, reference_date=reference_date
            )
        elif definition.normalizer == "cny_amount_minor":
            normalized = _extract_cny_amount(message)
        elif definition.normalizer == "duration_months":
            normalized = _normalize_duration_months(message)
        elif definition.normalizer == "investment_goal":
            normalized = _normalize_investment_goal(message)
        elif definition.normalizer == "wealth_product_scope":
            normalized = _normalize_wealth_product_scope(message)
        if normalized is not None:
            result[definition.name] = SlotValue(
                value=normalized,
                source="user_explicit",
                confidence=1,
            )
    return result


def normalize_account_reference(value: object) -> str | None:
    """account_reference 槽的平台级取值归一（声明契约的形态校验）。

    合法引用形态：尾号XXXX / 尾号 XXXX / XXXX的账户|卡。抽不出合法
    引用的输入（人名、任意叙述）不是账户引用，不得作为该槽位取值
    驱动结构重构或升级。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = unicodedata.normalize("NFKC", value)
    match = re.search(r"尾号\s*(\d{4})", text)
    if match:
        return f"尾号{match.group(1)}"
    match = re.search(r"(\d{4})\s*的\s*(?:账户|卡)", text)
    if match:
        return f"尾号{match.group(1)}"
    return None


def extract_clarification_answer_slots(
    contract: IntentSlotContract,
    message: str,
    current_slot_names: frozenset[str],
) -> dict[str, SlotValue]:
    """Extract account references from a clarification answer.

    When a user answers a pending clarification (e.g. "选择尾号6217的账户"),
    the lightweight model often fails to fill the corresponding slot.
    This platform-level fallback extracts the account reference and assigns
    it to the *currently-clarified* ``account_reference`` slot only —
    i.e. the first missing one in ``clarification_order``.

    Filling all missing ``account_reference`` slots would wrongly assign
    the same value to semantically distinct slots (e.g. payee and
    fromAccount).
    """
    if not isinstance(message, str) or not message.strip():
        return {}
    account_ref = normalize_account_reference(message)
    if account_ref is None:
        return {}
    # Only fill the first missing account_reference slot in clarification
    # order, matching the one the user is actually answering.
    for slot_name in contract.clarification_order:
        if slot_name in current_slot_names:
            continue
        try:
            definition = contract.slot(slot_name)
        except KeyError:
            continue
        if definition.type != "account_reference":
            continue
        # Skip if the slot is already filled via an extraction key.
        if any(key in current_slot_names for key in definition.extraction_keys):
            continue
        return {
            definition.name: SlotValue(
                value=account_ref,
                source="user_explicit",
                confidence=1,
            )
        }
    return {}


def _extract_cny_amount(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = unicodedata.normalize("NFKC", value).replace(",", "").replace("，", "")
    number = r"(\d+(?:\.\d{1,2})?)"
    # 一般金额表述的两类锚点：关键词引出（允许无单位，如"金额为3000"）
    # 与币种单位/符号紧随（如"每人100元""¥300"）。多值文本（如"给张三
    # 转100元，给李四转200元"）产出口径不一致的候选，返回 None 交回澄清。
    anchored: tuple[tuple[str, bool], ...] = (
        (
            rf"(?:计划(?:投资|投入)?|投资|投入|金额)\s*(?:为|是|约)?\s*[¥￥]?\s*"
            rf"{number}\s*(万|千|元|块钱?|圆)?",
            True,
        ),
        (rf"{number}\s*(万|千|元|块钱?|圆)", True),
        (rf"[¥￥]\s*{number}", False),
    )
    candidates: set[Decimal] = set()
    for pattern, has_unit in anchored:
        for match in re.finditer(pattern, text):
            amount = Decimal(match.group(1))
            if has_unit:
                unit = match.group(2)
                if unit == "万":
                    amount *= 10000
                elif unit == "千":
                    amount *= 1000
            if amount <= 0:
                continue
            candidates.add(amount)
    if len(candidates) != 1:
        return None
    return format(candidates.pop(), "f")


def _normalize_duration_months(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 600 else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = unicodedata.normalize("NFKC", value)
    if "半年" in text:
        return 6
    month_matches = [int(item) for item in re.findall(r"(?<!\d)(\d{1,3})\s*个?月", text)]
    year_matches = [int(item) * 12 for item in re.findall(r"(?<!\d)(\d{1,2})\s*年", text)]
    candidates = list(dict.fromkeys(month_matches + year_matches))
    return candidates[0] if len(candidates) == 1 and 1 <= candidates[0] <= 600 else None


def _normalize_investment_goal(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = unicodedata.normalize("NFKC", value).strip().lower()
    if text in {"capital_preservation", "steady_growth", "long_term_growth"}:
        return text
    matches = []
    if re.search(r"保本|保守|本金安全", text):
        matches.append("capital_preservation")
    if re.search(r"稳健|稳中求进|平衡", text):
        matches.append("steady_growth")
    if re.search(r"长期增值|长期增长|成长", text):
        matches.append("long_term_growth")
    unique = tuple(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _normalize_wealth_product_scope(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = unicodedata.normalize("NFKC", value).strip().lower()
    if text in {"wealth_only", "wealth_and_conservative_alternatives"}:
        return text
    if re.search(r"只(?:要|看|推荐)?理财|仅限理财|不要存款", text):
        return "wealth_only"
    if re.search(r"可以.*存款|包含.*存款|保守替代|稳健替代", text):
        return "wealth_and_conservative_alternatives"
    return None


def with_provider_resolved_reference(
    evaluation: SlotEvaluation,
    definition: SlotDefinition,
    *,
    semantic_value: str,
    resolved_value: str,
) -> SlotEvaluation:
    """Attach a resolver-owned identifier to an evaluated semantic reference.

    The ordinary evaluator intentionally treats user/model supplied reference
    values as semantic text only.  This helper is the separate trust boundary
    used *after* a server-side capability has selected exactly one entity.
    """

    semantic_value = semantic_value.strip()
    resolved_value = resolved_value.strip()
    if definition.resolver_capability_id is None:
        raise ValueError("slot does not declare a provider resolver")
    if definition.type not in {"account_reference", "card_reference", "product_reference", "holding_reference", "party_reference"}:
        raise ValueError("slot is not a supported reference type")
    if not semantic_value or len(semantic_value) > 120:
        raise ValueError("semantic reference is invalid")
    if not _PROVIDER_REFERENCE_ID.fullmatch(resolved_value):
        raise ValueError("provider reference identifier is invalid")

    found = False
    assessments: list[SlotAssessment] = []
    for item in evaluation.assessments:
        if item.name != definition.name:
            assessments.append(item)
            continue
        found = True
        assessments.append(replace(
            item,
            state="resolved",
            semantic_value=semantic_value,
            resolved_value=resolved_value,
        ))
    if not found:
        raise ValueError("slot assessment is missing")

    semantic_values = dict(evaluation.semantic_values)
    semantic_values[definition.name] = semantic_value
    resolved_values = dict(evaluation.resolved_values)
    resolved_values[definition.resolved_field] = resolved_value
    recognized = list(evaluation.recognized)
    if definition.name not in recognized:
        recognized.append(definition.name)

    missing = tuple(item.name for item in assessments if item.state == "missing")
    ambiguous = tuple(item.name for item in assessments if item.state == "ambiguous")
    invalid = tuple(item.name for item in assessments if item.state == "invalid")
    optional_absent = tuple(
        item.name for item in assessments if item.state == "optional_absent"
    )
    readiness: Literal["ready", "needs_slots", "needs_clarification"] = "ready"
    if ambiguous or invalid:
        readiness = "needs_clarification"
    elif missing:
        readiness = "needs_slots"
    return SlotEvaluation(
        assessments=tuple(assessments),
        semantic_values=MappingProxyType(semantic_values),
        resolved_values=MappingProxyType(resolved_values),
        missing=missing,
        ambiguous=ambiguous,
        invalid=invalid,
        recognized=tuple(recognized),
        optional_absent=optional_absent,
        readiness=readiness,
    )


def evaluate_amount(
    value: object,
    *,
    already_minor: bool = False,
    name: str = "amount",
    label: str = "金额",
) -> SlotAssessment:
    if isinstance(value, bool):
        return SlotAssessment(name, label, "invalid")
    normalized_value = value
    if isinstance(value, str) and not already_minor:
        normalized_value = value.strip().replace(",", "").replace("，", "")
        normalized_value = re.sub(
            r"^(?:CNY|RMB|CN¥|￥|¥)\s*",
            "",
            normalized_value,
            flags=re.IGNORECASE,
        )
        normalized_value = re.sub(
            r"\s*(?:CNY|RMB|人民币|元|块钱?|圆)$",
            "",
            normalized_value,
            flags=re.IGNORECASE,
        ).strip()
    try:
        amount = Decimal(str(normalized_value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return SlotAssessment(name, label, "invalid")
    if not amount.is_finite() or amount <= 0:
        return SlotAssessment(name, label, "invalid")
    if already_minor:
        if amount != amount.to_integral_value():
            return SlotAssessment(name, label, "invalid")
        amount_minor = int(amount)
        semantic_value = format(amount / 100, "f")
    else:
        if amount.as_tuple().exponent < -2:
            return SlotAssessment(name, label, "invalid")
        amount_minor = int(amount * 100)
        semantic_value = format(amount, "f")
    # This is a representational bound only. Product limits belong to the
    # capability policy/core transaction system, never to the generic slot layer.
    if amount_minor <= 0 or amount_minor > 1_000_000_000_000_000:
        return SlotAssessment(name, label, "invalid")
    return SlotAssessment(
        name, label, "resolved",
        semantic_value=semantic_value,
        resolved_value=amount_minor,
    )


class SlotContractEvaluator:
    """Evaluate a graph contract without model or network side effects."""

    def __init__(self, *, reference_date: date | None = None) -> None:
        self._reference_date = reference_date or date.today()

    def evaluate(
        self,
        *,
        contract: IntentSlotContract,
        current_slots: Mapping[str, SlotValue],
        conversation_slots: Mapping[str, SlotValue],
    ) -> SlotEvaluation:
        assessments: list[SlotAssessment] = []
        semantic_values: dict[str, object] = {}
        resolved_values: dict[str, object] = {}
        recognized: list[str] = []

        for definition in contract.slots:
            selected, selected_key, from_default = _select_value(
                definition, current_slots, conversation_slots
            )
            assessment = _assess(
                definition,
                selected,
                selected_key,
                reference_date=self._reference_date,
            )
            assessments.append(assessment)
            if assessment.semantic_value is not None:
                semantic_values[definition.name] = assessment.semantic_value
            if assessment.resolved_value is not None:
                resolved_values[definition.resolved_field] = assessment.resolved_value
            if not from_default and assessment.state in {"extracted", "resolved"}:
                recognized.append(definition.name)

        missing = tuple(item.name for item in assessments if item.state == "missing")
        ambiguous = tuple(item.name for item in assessments if item.state == "ambiguous")
        invalid = tuple(item.name for item in assessments if item.state == "invalid")
        optional_absent = tuple(
            item.name for item in assessments if item.state == "optional_absent"
        )
        readiness: Literal["ready", "needs_slots", "needs_clarification"] = "ready"
        if ambiguous or invalid:
            readiness = "needs_clarification"
        elif missing:
            readiness = "needs_slots"
        return SlotEvaluation(
            assessments=tuple(assessments),
            semantic_values=MappingProxyType(semantic_values),
            resolved_values=MappingProxyType(resolved_values),
            missing=missing,
            ambiguous=ambiguous,
            invalid=invalid,
            recognized=tuple(recognized),
            optional_absent=optional_absent,
            readiness=readiness,
        )


def _select_value(
    definition: SlotDefinition,
    current: Mapping[str, SlotValue],
    remembered: Mapping[str, SlotValue],
) -> tuple[SlotValue | None, str | None, bool]:
    keys = (definition.name, *definition.extraction_keys)
    for key in keys:
        value = current.get(key)
        if value is not None:
            return value, key, False
    for key in keys:
        value = remembered.get(key)
        if value is not None:
            return value, key, False
    if definition.default_value is not None:
        return SlotValue(
            value=definition.default_value, source="intent_default", confidence=1
        ), definition.name, True
    return None, None, False


def _assess(
    definition: SlotDefinition,
    selected: SlotValue | None,
    selected_key: str | None,
    *,
    reference_date: date,
) -> SlotAssessment:
    if selected is None:
        state: SlotState = "missing" if definition.required else "optional_absent"
        return SlotAssessment(definition.name, definition.label, state)
    # 付款账户（account_reference）涉及资金出口，必须由用户显式确认或
    # 选择。LLM agent 从上下文/默认值自动设定的 fromAccount 不得直接评估
    # 为 resolved，否则会跳过用户对付款账户的确认，导致资金从用户未指
    # 定的账户转出。此处强制要求 source ∈ {user_explicit, user_selection}，
    # 否则标记为 ambiguous 交回澄清。
    if (
        definition.type == "account_reference"
        and selected.source not in ("user_explicit", "user_selection")
    ):
        return SlotAssessment(definition.name, definition.label, "ambiguous")
    value = selected.value
    if definition.normalizer == "year_month":
        normalized = normalize_year_month(
            value, reference_date=reference_date
        )
        if normalized is None:
            return SlotAssessment(definition.name, definition.label, "invalid")
        return SlotAssessment(
            definition.name,
            definition.label,
            "resolved",
            semantic_value=normalized,
            resolved_value=normalized,
        )
    if definition.normalizer == "duration_months":
        normalized_duration = _normalize_duration_months(value)
        if normalized_duration is None:
            return SlotAssessment(definition.name, definition.label, "invalid")
        return SlotAssessment(
            definition.name, definition.label, "resolved",
            semantic_value=normalized_duration, resolved_value=normalized_duration,
        )
    if definition.normalizer == "investment_goal":
        normalized_goal = _normalize_investment_goal(value)
        if normalized_goal is None:
            return SlotAssessment(definition.name, definition.label, "invalid")
        return SlotAssessment(
            definition.name, definition.label, "resolved",
            semantic_value=normalized_goal, resolved_value=normalized_goal,
        )
    if definition.normalizer == "wealth_product_scope":
        normalized_scope = _normalize_wealth_product_scope(value)
        if normalized_scope is None:
            return SlotAssessment(definition.name, definition.label, "invalid")
        return SlotAssessment(
            definition.name, definition.label, "resolved",
            semantic_value=normalized_scope, resolved_value=normalized_scope,
        )
    if definition.type == "money":
        return evaluate_amount(
            value,
            already_minor=selected_key == definition.resolved_field,
            name=definition.name,
            label=definition.label,
        )
    if definition.type == "currency":
        if not isinstance(value, str) or len(value.strip()) != 3 or not value.strip().isalpha():
            return SlotAssessment(definition.name, definition.label, "invalid")
        normalized = value.strip().upper()
        return SlotAssessment(
            definition.name, definition.label, "resolved",
            semantic_value=normalized, resolved_value=normalized,
        )
    if definition.type == "text":
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 40:
            return SlotAssessment(definition.name, definition.label, "invalid")
        clean = value.strip()
        return SlotAssessment(
            definition.name, definition.label, "resolved",
            semantic_value=clean, resolved_value=clean,
        )
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        return SlotAssessment(definition.name, definition.label, "invalid")
    clean = value.strip()
    return SlotAssessment(
        definition.name, definition.label, "extracted", semantic_value=clean
    )
