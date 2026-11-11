"""Deterministic, fail-closed resolution for graph-declared wealth holdings."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Literal


HoldingReferenceState = Literal["selected", "ambiguous", "empty"]

_MAX_VISIBLE_OPTIONS = 8
_INTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_HOLDING_STATUSES = frozenset({"HOLDING"})
_UNSAFE_MARKERS = (
    "productid", "accountid", "忽略", "无视", "系统提示", "提示词",
    "指令", "密钥", "密码", "验证码", "token", "secret", "prompt",
)


class HoldingReferenceResolutionError(ValueError):
    """Raised when provider holding data cannot be projected safely."""


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


def _safe_identifier(value: Any) -> str | None:
    normalized = _canonical(value).strip()
    return normalized if _INTERNAL_ID_RE.fullmatch(normalized) else None


def _safe_holding_name(value: Any) -> str:
    normalized = " ".join(_canonical(value).split()).strip()
    compact = _compact(normalized)
    if (
        not normalized
        or len(normalized) > 80
        or any(marker in compact for marker in _UNSAFE_MARKERS)
    ):
        raise HoldingReferenceResolutionError("holding name is invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class HoldingOption:
    holding_id: str = field(repr=False)
    name: str
    category: str
    reply_message: str
    segments: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class HoldingReferenceResolution:
    state: HoldingReferenceState
    options: tuple[HoldingOption, ...]
    holding_id: str | None = field(default=None, repr=False)
    semantic_reference: str | None = None
    total_count: int = 0
    options_truncated: bool = False


def _active_holdings(holdings: Any) -> tuple[HoldingOption, ...]:
    if not isinstance(holdings, list):
        raise HoldingReferenceResolutionError("holdings must be a list")
    options: list[HoldingOption] = []
    seen_ids: set[str] = set()
    for holding in holdings:
        if not isinstance(holding, dict):
            raise HoldingReferenceResolutionError("holding record must be an object")
        holding_id = _safe_identifier(holding.get("id"))
        if holding_id is None or holding_id in seen_ids:
            raise HoldingReferenceResolutionError("holding identity is invalid")
        seen_ids.add(holding_id)
        status = _canonical(holding.get("status")).strip()
        if status not in _HOLDING_STATUSES:
            continue
        name = _safe_holding_name(holding.get("productName"))
        category = _canonical(holding.get("category")).strip()
        if not category or len(category) > 40:
            raise HoldingReferenceResolutionError("holding category is invalid")
        # Provider names embed bracketed brand aliases (e.g. 天天利（龙钱宝）).
        # Segment the name on punctuation so a distinctive alias can match a
        # short user reference without hard-coding any product wording.
        segments = tuple(
            segment for segment in re.findall(r"[^\W_]+", name, re.UNICODE)
            if len(_compact(segment)) >= 3
        )
        options.append(HoldingOption(
            holding_id=holding_id,
            name=name,
            category=category,
            reply_message=f"选择赎回持仓：{name}",
            segments=segments,
        ))
    return tuple(options)


def resolve_holding_reference(
    source_message: str | None,
    holdings: Any,
) -> HoldingReferenceResolution:
    """Resolve one customer-owned holding by its provider-owned product name."""

    if source_message is not None and not isinstance(source_message, str):
        raise HoldingReferenceResolutionError("source message must be text or null")
    options = _active_holdings(holdings)
    if not options:
        return HoldingReferenceResolution("empty", (), total_count=0)
    message = _canonical(source_message)
    compact = _compact(message)
    unsafe = any(marker in compact for marker in _UNSAFE_MARKERS)
    matches = tuple(
        option for option in options
        if _compact(option.name) in compact
        or any(_compact(segment) in compact for segment in option.segments)
    )
    if not unsafe and len(matches) == 1:
        selected = matches[0]
        return HoldingReferenceResolution(
            "selected",
            (selected,),
            holding_id=selected.holding_id,
            semantic_reference=selected.name,
            total_count=len(options),
        )
    visible = options[:_MAX_VISIBLE_OPTIONS]
    return HoldingReferenceResolution(
        "ambiguous",
        visible,
        total_count=len(options),
        options_truncated=len(options) > len(visible),
    )


__all__ = [
    "HoldingOption", "HoldingReferenceResolution",
    "HoldingReferenceResolutionError", "resolve_holding_reference",
]
