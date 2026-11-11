"""Deterministic, fail-closed resolution for graph-declared party references.

A ``party_reference`` slot (e.g. ``payee``) carries a semantic name that must
be resolved to a provider-assigned identifier via a resolver capability such
as ``bank.payee.list``.  This module mirrors the contract used by
``card_reference_resolution`` so the graph reference chain can handle
``party_reference`` slots without special-casing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Literal


PartyReferenceState = Literal["selected", "ambiguous", "empty"]

_MAX_VISIBLE_OPTIONS = 8
_INTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_SAFE_NAME_RE = re.compile(r"^[\u3400-\u9fffA-Za-z0-9·・\-\u00b7 ]{1,60}$")
_UNSAFE_MARKERS = (
    "payeeid", "accountid", "cardid", "忽略", "无视", "系统提示", "提示词",
    "指令", "密钥", "密码", "验证码", "token", "secret", "prompt", "执行",
)


class PartyReferenceResolutionError(ValueError):
    """Raised when provider party data cannot be projected safely."""


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
    if not _INTERNAL_ID_RE.fullmatch(normalized):
        return None
    return normalized


def _safe_name(value: Any) -> str:
    normalized = " ".join(_canonical(value).split()).strip()
    if (
        not _SAFE_NAME_RE.fullmatch(normalized)
        or any(marker in _compact(normalized) for marker in _UNSAFE_MARKERS)
    ):
        return "收款方"
    return normalized


@dataclass(frozen=True, slots=True)
class PartyOption:
    party_id: str = field(repr=False)
    name: str
    reply_message: str | None = None

    @property
    def semantic_reference(self) -> str:
        return self.name

    @property
    def selectable(self) -> bool:
        return self.reply_message is not None


@dataclass(frozen=True, slots=True)
class PartyReferenceResolution:
    state: PartyReferenceState
    options: tuple[PartyOption, ...]
    party_id: str | None = field(default=None, repr=False)
    semantic_reference: str | None = None
    total_count: int = 0
    options_truncated: bool = False
    selection_degraded: bool = False


def _active_parties(parties: Any) -> tuple[PartyOption, ...]:
    if not isinstance(parties, list):
        raise PartyReferenceResolutionError("parties must be a list")
    rows: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for party in parties:
        if not isinstance(party, dict):
            raise PartyReferenceResolutionError("party record must be an object")
        party_id = _safe_identifier(party.get("id"))
        if party_id is None or party_id in seen_ids:
            raise PartyReferenceResolutionError("party record identity is invalid")
        seen_ids.add(party_id)
        status = party.get("status")
        if not isinstance(status, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,31}", status):
            raise PartyReferenceResolutionError("party status is invalid")
        if status != "ACTIVE":
            continue
        rows.append((party_id, _safe_name(party.get("name"))))
    name_counts: dict[str, int] = {}
    for _, name in rows:
        name_counts[name] = name_counts.get(name, 0) + 1
    return tuple(
        PartyOption(
            party_id=party_id,
            name=name,
            reply_message=(f"选择{name}" if name_counts[name] == 1 else None),
        )
        for party_id, name in rows
    )


def resolve_party_reference(
    source_message: str | None,
    parties: Any,
) -> PartyReferenceResolution:
    """Resolve one active party by name evidence, otherwise return choices."""

    if source_message is not None and not isinstance(source_message, str):
        raise PartyReferenceResolutionError("source message must be text or null")
    options = _active_parties(parties)
    if not options:
        return PartyReferenceResolution("empty", (), total_count=0)
    message = _canonical(source_message)
    compact = _compact(message)
    unsafe = any(marker in compact for marker in _UNSAFE_MARKERS)
    name_matches = tuple(
        option for option in options
        if _compact(option.name) and _compact(option.name) in compact
    )
    has_name = bool(name_matches)
    matches = tuple(options)
    if has_name:
        matches = name_matches
    explicit = has_name
    if not unsafe and ((explicit and len(matches) == 1) or (not explicit and len(options) == 1)):
        selected = matches[0] if explicit else options[0]
        return PartyReferenceResolution(
            "selected", (selected,), party_id=selected.party_id,
            semantic_reference=selected.semantic_reference, total_count=len(options),
        )
    visible = options[:_MAX_VISIBLE_OPTIONS]
    return PartyReferenceResolution(
        "ambiguous", visible, total_count=len(options),
        options_truncated=len(options) > len(visible),
        selection_degraded=any(not option.selectable for option in visible),
    )


__all__ = [
    "PartyOption", "PartyReferenceResolution", "PartyReferenceResolutionError",
    "resolve_party_reference",
]
