"""Deterministic, fail-closed resolution for graph-declared card references."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Literal


CardReferenceState = Literal["selected", "ambiguous", "empty"]

_MAX_VISIBLE_OPTIONS = 8
_INTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_SAFE_LABEL_RE = re.compile(r"^[\u3400-\u9fffA-Za-z0-9·・ -]{1,40}$")
_TAIL_RE = re.compile(r"(?:尾号|末四位|卡号后四位)\s*(?:是|为)?\s*[:：-]?\s*(\d{4})(?!\d)")
_UNSAFE_MARKERS = (
    "cardid", "accountid", "credit_", "忽略", "无视", "系统提示", "提示词",
    "指令", "密钥", "密码", "验证码", "token", "secret", "prompt", "执行",
)


class CardReferenceResolutionError(ValueError):
    """Raised when provider card data cannot be projected safely."""


def _canonical(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )


def _compact(value: Any) -> str:
    return "".join(
        character.casefold()
        for character in _canonical(value)
        if unicodedata.category(character)[0] not in {"P", "Z", "S", "M", "C"}
    )


def _contains_pan(value: Any) -> bool:
    return sum(character.isdecimal() for character in _canonical(value)) >= 15


def _safe_identifier(value: Any) -> str | None:
    normalized = _canonical(value).strip()
    if not _INTERNAL_ID_RE.fullmatch(normalized) or _contains_pan(normalized):
        return None
    return normalized


def _safe_product_name(value: Any) -> str:
    normalized = " ".join(_canonical(value).split()).strip()
    compact = _compact(normalized)
    if (
        not _SAFE_LABEL_RE.fullmatch(normalized)
        or "卡" not in normalized
        or _contains_pan(normalized)
        or any(marker in compact for marker in _UNSAFE_MARKERS)
    ):
        return "信用卡"
    return normalized


def _safe_tail(value: Any) -> str | None:
    if not isinstance(value, str) or _contains_pan(value):
        return None
    normalized = _canonical(value)
    digits = "".join(character for character in normalized if character.isdecimal())
    if len(digits) != 4 or not any(marker in normalized for marker in ("*", "•", "●", "x", "X", "尾号")):
        return None
    return digits


@dataclass(frozen=True, slots=True)
class CardOption:
    card_id: str = field(repr=False)
    label: str
    tail: str
    reply_message: str | None = None

    @property
    def semantic_reference(self) -> str:
        return f"{self.label} · 尾号{self.tail}"

    @property
    def selectable(self) -> bool:
        return self.reply_message is not None


@dataclass(frozen=True, slots=True)
class CardReferenceResolution:
    state: CardReferenceState
    options: tuple[CardOption, ...]
    card_id: str | None = field(default=None, repr=False)
    semantic_reference: str | None = None
    total_count: int = 0
    options_truncated: bool = False
    selection_degraded: bool = False


def _active_cards(cards: Any) -> tuple[CardOption, ...]:
    if not isinstance(cards, list):
        raise CardReferenceResolutionError("cards must be a list")
    rows: list[tuple[str, str, str]] = []
    seen_ids: set[str] = set()
    for card in cards:
        if not isinstance(card, dict):
            raise CardReferenceResolutionError("card record must be an object")
        card_id = _safe_identifier(card.get("id"))
        if card_id is None or card_id in seen_ids:
            raise CardReferenceResolutionError("card record identity is invalid")
        seen_ids.add(card_id)
        status = card.get("status")
        if not isinstance(status, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,31}", status):
            raise CardReferenceResolutionError("card status is invalid")
        if status != "ACTIVE":
            continue
        tail = _safe_tail(card.get("maskedNo"))
        if tail is None:
            raise CardReferenceResolutionError("active card mask is invalid")
        rows.append((card_id, _safe_product_name(card.get("productName")), tail))
    tail_counts = {tail: sum(1 for _, _, item_tail in rows if item_tail == tail) for _, _, tail in rows}
    return tuple(
        CardOption(
            card_id=card_id,
            label=label,
            tail=tail,
            reply_message=(f"选择尾号{tail}的信用卡" if tail_counts[tail] == 1 else None),
        )
        for card_id, label, tail in rows
    )


def resolve_card_reference(source_message: str | None, cards: Any) -> CardReferenceResolution:
    """Resolve one active card by safe name/tail evidence, otherwise return choices."""

    if source_message is not None and not isinstance(source_message, str):
        raise CardReferenceResolutionError("source message must be text or null")
    options = _active_cards(cards)
    if not options:
        return CardReferenceResolution("empty", (), total_count=0)
    message = _canonical(source_message)
    compact = _compact(message)
    unsafe = _contains_pan(message) or any(marker in compact for marker in _UNSAFE_MARKERS)
    tails = set(_TAIL_RE.findall(message))
    name_matches = tuple(
        option for option in options
        if _compact(option.label) and _compact(option.label) in compact
    )
    has_name = bool(name_matches)
    matches = tuple(options)
    if tails:
        matches = tuple(option for option in matches if option.tail in tails)
    if has_name:
        matches = tuple(option for option in matches if option in name_matches)
    explicit = bool(tails or has_name)
    if not unsafe and ((explicit and len(matches) == 1) or (not explicit and len(options) == 1)):
        selected = matches[0] if explicit else options[0]
        return CardReferenceResolution(
            "selected", (selected,), card_id=selected.card_id,
            semantic_reference=selected.semantic_reference, total_count=len(options),
        )
    visible = options[:_MAX_VISIBLE_OPTIONS]
    return CardReferenceResolution(
        "ambiguous", visible, total_count=len(options),
        options_truncated=len(options) > len(visible),
        selection_degraded=any(not option.selectable for option in visible),
    )


__all__ = [
    "CardOption", "CardReferenceResolution", "CardReferenceResolutionError",
    "resolve_card_reference",
]
