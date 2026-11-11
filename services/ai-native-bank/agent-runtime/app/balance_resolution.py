"""Deterministic, fail-closed account resolution for balance queries."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from urllib.parse import unquote
from typing import Any, Literal

from .contracts import CapabilityPlan, IntentTask


BalanceResolutionState = Literal["selected", "ambiguous", "empty"]

_MAX_VISIBLE_OPTIONS = 8
_INTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_STATUS_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
_ACCOUNT_STATUSES = frozenset({
    "ACTIVE", "INACTIVE", "LOST", "FROZEN", "CLOSED", "DORMANT", "BLOCKED",
})
_TAIL_EVIDENCE_RE = re.compile(
    r"(?:尾号|末四位|卡号后四位)\s*(?:(?:是|为)\s*)?(?:[:：-]\s*)?([0-9]{4})(?![0-9])"
)
_SAFE_LABEL_RE = re.compile(r"^[\u3400-\u9fffA-Za-z0-9·・ -]{1,20}$")
_GENERIC_ACCOUNT_EVIDENCE_RE = re.compile(
    r"[\u3400-\u9fffA-Za-z0-9]{1,16}(?:卡|账户|存折)"
)
_ACCOUNT_SELECTION_COMPACT_RE = re.compile(r"^尾号[0-9]{4}的?账户$")
# 证据分词会把"用尾号6217的账户"这类带引导词的整串吞进 token，全串精确
# 白名单（_ACCOUNT_SELECTION_COMPACT_RE）匹配不上，导致纯尾号引用被误判为
# "报了不存在的账户名"而 fail-closed 成 ambiguous。判定改为：token 含尾号
# 短语且去掉短语后只剩通用名词，即纯尾号选择，不算业务账户名证据。
_ACCOUNT_TAIL_REF_RE = re.compile(r"尾号[0-9]{4}")
_TAIL_REF_RESIDUAL_RE = re.compile(r"^.*尾号[0-9]{4}的?")
_TAIL_SELECTION_RESIDUALS = frozenset({"", "账户", "卡", "存折", "银行卡", "银行账户"})
_MASK_MARKERS = frozenset({"*", "•", "●", "x", "X"})
_ACCOUNT_NOUNS = ("卡", "账户", "存折")
_GENERIC_ACCOUNT_NAMES_COMPACT = frozenset({"卡", "账户", "银行卡", "银行账户", "存折"})
_QUERY_NAME_PREFIXES = (
    "选择",
    "请帮我查询", "请帮我查看", "我想查询", "我想查看", "帮我查询", "帮我查看",
    "查询", "查看", "查一下", "请帮我", "帮我", "我想", "我的", "请", "查",
)
# 消息级扫描只保留注入/凭证类标记：跨域续跑轮的用户回复必然携带
# 转账/收款人等正常业务词汇（如"使用工资卡转账"），业务动词一旦触发
# fail-closed，余额账户澄清就会在混合意图会话中无限循环。
_UNSAFE_INSTRUCTION_MARKERS = (
    "accountid", "cardid", "capabilityid", "acct", "credit", "payee", "bank",
    "忽略", "无视", "之前指令", "系统提示", "提示词", "指令", "输出", "泄露",
    "密钥", "密码", "验证码", "token", "secret", "prompt", "execute", "执行",
)
# 账户名清洗保持最严：API 数据里的账户名含业务动词即视为可疑标签。
_UNSAFE_LABEL_MARKERS = (
    *_UNSAFE_INSTRUCTION_MARKERS,
    "转账", "汇款", "付款", "陌生人", "收款人",
)


class BalanceResolutionError(ValueError):
    """Raised when untrusted account data cannot be resolved safely."""


def _canonical_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            continue
        if category == "Nd":
            try:
                characters.append(str(unicodedata.decimal(character)))
                continue
            except (TypeError, ValueError):
                pass
        characters.append(character)
    return "".join(characters)


def _compact_text(value: Any) -> str:
    return "".join(
        character.casefold()
        for character in _canonical_text(value)
        if unicodedata.category(character)[0] not in {"P", "Z", "S", "M", "C"}
    )


def _decoded_security_forms(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    forms: list[str] = []
    current = _canonical_text(value).casefold()
    for _ in range(4):
        if current not in forms:
            forms.append(current)
        decoded = _canonical_text(unquote(current)).casefold()
        if decoded == current:
            break
        current = decoded
    return tuple(forms)


def _contains_unsafe_marker(value: Any) -> bool:
    return any(
        marker in _compact_text(security_form)
        for security_form in _decoded_security_forms(value)
        for marker in _UNSAFE_LABEL_MARKERS
    )


def _contains_unsafe_instruction(value: Any) -> bool:
    return any(
        marker in _compact_text(security_form)
        for security_form in _decoded_security_forms(value)
        for marker in _UNSAFE_INSTRUCTION_MARKERS
    )


def _decimal_digit_runs(value: Any) -> tuple[str, ...]:
    """Join decimal digits across P/Z/S/M/C separators, including emoji."""

    if not isinstance(value, str):
        return ()
    runs: list[str] = []
    current: list[str] = []
    for character in unicodedata.normalize("NFKC", value):
        category = unicodedata.category(character)
        if category == "Nd":
            try:
                current.append(str(unicodedata.decimal(character)))
                continue
            except (TypeError, ValueError):
                pass
        if category[0] in {"P", "Z", "S", "M", "C"}:
            continue
        if current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return tuple(runs)


def _contains_pan(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return any(
        sum(
            1
            for character in unicodedata.normalize("NFKC", security_form)
            if unicodedata.category(character) == "Nd"
        ) >= 15
        for security_form in _decoded_security_forms(value)
    )


def _validated_internal_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    identifier = _canonical_text(value).strip()
    if not _INTERNAL_ID_RE.fullmatch(identifier) or _contains_pan(identifier):
        return None
    return identifier


def _safe_label(value: Any) -> str:
    if not isinstance(value, str):
        return "银行账户"
    normalized = " ".join(_canonical_text(value).split()).strip()
    compact = _compact_text(normalized)
    if (
        not _SAFE_LABEL_RE.fullmatch(normalized)
        or not any(noun in normalized for noun in _ACCOUNT_NOUNS)
        or compact in _GENERIC_ACCOUNT_NAMES_COMPACT
        or _contains_pan(normalized)
        or _contains_unsafe_marker(normalized)
    ):
        return "银行账户"
    return normalized


def _safe_tail(masked_number: Any) -> str | None:
    if not isinstance(masked_number, str) or _contains_pan(masked_number):
        return None
    normalized = _canonical_text(masked_number).strip()
    digits = "".join(_decimal_digit_runs(masked_number))
    if len(digits) != 4:
        return None
    if "尾号" not in normalized and not any(marker in normalized for marker in _MASK_MARKERS):
        return None
    return digits


@dataclass(frozen=True, slots=True)
class AccountOption:
    """Internal identity plus display-safe label/tail; replies never reuse the label."""

    account_id: str = field(repr=False)
    label: str
    tail: str
    reply_message: str | None = None

    def __post_init__(self) -> None:
        if _validated_internal_id(self.account_id) != self.account_id:
            raise BalanceResolutionError("account_id must be an internal identifier")
        if _safe_label(self.label) != self.label:
            raise BalanceResolutionError("account label is not safe for user projection")
        if not re.fullmatch(r"[0-9]{4}", self.tail):
            raise BalanceResolutionError("account tail must contain four ASCII digits")
        allowed_replies = {f"选择尾号{self.tail}的账户"}
        if self.reply_message is not None and self.reply_message not in allowed_replies:
            raise BalanceResolutionError("account reply is outside the fixed safe templates")

    @property
    def selectable(self) -> bool:
        return self.reply_message is not None


@dataclass(frozen=True, slots=True)
class BalanceResolution:
    state: BalanceResolutionState
    options: tuple[AccountOption, ...]
    account_id: str | None = field(default=None, repr=False)
    total_count: int = 0
    options_truncated: bool = False
    selection_degraded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.options, (tuple, list)):
            raise BalanceResolutionError("options must be a tuple or list")
        object.__setattr__(self, "options", tuple(self.options))
        if len(self.options) > _MAX_VISIBLE_OPTIONS:
            raise BalanceResolutionError("too many visible account options")
        if any(not isinstance(option, AccountOption) for option in self.options):
            raise BalanceResolutionError("options must contain AccountOption values")
        if len({option.account_id for option in self.options}) != len(self.options):
            raise BalanceResolutionError("account options must be unique")
        if not isinstance(self.total_count, int) or isinstance(self.total_count, bool):
            raise BalanceResolutionError("total_count must be an integer")
        if self.total_count < len(self.options):
            raise BalanceResolutionError("total_count cannot be smaller than visible options")
        if not isinstance(self.selection_degraded, bool):
            raise BalanceResolutionError("selection_degraded must be boolean")

        if self.state == "selected":
            if len(self.options) != 1 or self.account_id != self.options[0].account_id:
                raise BalanceResolutionError("selected resolution requires one matching option")
            if self.options_truncated:
                raise BalanceResolutionError("selected resolution is not a truncated choice list")
            if self.selection_degraded:
                raise BalanceResolutionError("selected resolution cannot have degraded choices")
        elif self.state == "ambiguous":
            if not self.options or self.account_id is not None:
                raise BalanceResolutionError("ambiguous resolution requires safe options and no account id")
            if self.options_truncated != (self.total_count > len(self.options)):
                raise BalanceResolutionError("ambiguous truncation metadata is inconsistent")
            if any(not option.selectable for option in self.options):
                object.__setattr__(self, "selection_degraded", True)
        elif self.state == "empty":
            if (
                self.options or self.account_id is not None or self.total_count != 0
                or self.options_truncated or self.selection_degraded
            ):
                raise BalanceResolutionError("empty resolution cannot contain account data")
        else:
            raise BalanceResolutionError("unknown balance resolution state")


@dataclass(frozen=True, slots=True)
class _Candidate:
    option: AccountOption
    match_name: str


@dataclass(frozen=True, slots=True)
class _RawCandidate:
    account_id: str
    label: str
    tail: str
    match_name: str


def _active_candidates(accounts: Any) -> tuple[_Candidate, ...]:
    if not isinstance(accounts, list):
        raise BalanceResolutionError("accounts must be a list")
    raw_candidates: list[_RawCandidate] = []
    seen_ids: set[str] = set()
    for account in accounts:
        if not isinstance(account, dict):
            raise BalanceResolutionError("account record must be an object")
        account_id = _validated_internal_id(account.get("id"))
        if account_id is None:
            raise BalanceResolutionError("account record has an invalid internal id")
        if account_id in seen_ids:
            raise BalanceResolutionError("account record id is duplicated")
        seen_ids.add(account_id)

        status = account.get("status")
        if (
            not isinstance(status, str)
            or not _STATUS_RE.fullmatch(status)
            or status not in _ACCOUNT_STATUSES
        ):
            raise BalanceResolutionError("account status must be a known uppercase enum")
        if status != "ACTIVE":
            continue
        if not isinstance(account.get("name"), str):
            raise BalanceResolutionError("active account name must be text")
        tail = _safe_tail(account.get("maskedNo"))
        if tail is None:
            raise BalanceResolutionError("active account masked number is invalid")
        original_name = " ".join(_canonical_text(account["name"]).split()).strip()
        label = _safe_label(original_name)
        compact_name = _compact_text(original_name)
        match_name = (
            compact_name
            if label == original_name and compact_name not in _GENERIC_ACCOUNT_NAMES_COMPACT
            else ""
        )
        raw_candidates.append(_RawCandidate(account_id, label, tail, match_name))

    tail_counts: dict[str, int] = {}
    for candidate in raw_candidates:
        tail_counts[candidate.tail] = tail_counts.get(candidate.tail, 0) + 1

    candidates: list[_Candidate] = []
    for candidate in raw_candidates:
        reply_message: str | None = None
        if tail_counts[candidate.tail] == 1:
            reply_message = f"选择尾号{candidate.tail}的账户"
        option = AccountOption(
            candidate.account_id, candidate.label, candidate.tail, reply_message
        )
        candidates.append(_Candidate(option, candidate.match_name))
    return tuple(candidates)


def _selected(candidate: _Candidate, total_count: int) -> BalanceResolution:
    return BalanceResolution(
        state="selected",
        options=(candidate.option,),
        account_id=candidate.option.account_id,
        total_count=total_count,
    )


def _ambiguous(candidates: tuple[_Candidate, ...]) -> BalanceResolution:
    visible = tuple(candidate.option for candidate in candidates[:_MAX_VISIBLE_OPTIONS])
    return BalanceResolution(
        state="ambiguous",
        options=visible,
        account_id=None,
        total_count=len(candidates),
        options_truncated=len(candidates) > len(visible),
        selection_degraded=any(not candidate.option.selectable for candidate in candidates),
    )


def _is_tail_selection_token(token: str) -> bool:
    if not _ACCOUNT_TAIL_REF_RE.search(token):
        return False
    residual = _TAIL_REF_RESIDUAL_RE.sub("", token)
    return residual in _TAIL_SELECTION_RESIDUALS


def _name_evidence(
    compact_message: str, candidates: tuple[_Candidate, ...]
) -> tuple[bool, tuple[_Candidate, ...]]:
    business_name_present = False
    for match in _GENERIC_ACCOUNT_EVIDENCE_RE.finditer(compact_message):
        token = match.group(0)
        stripped = token
        for prefix in _QUERY_NAME_PREFIXES:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
                break
        if _ACCOUNT_SELECTION_COMPACT_RE.fullmatch(stripped):
            continue
        if _is_tail_selection_token(stripped):
            continue
        if stripped and stripped not in _GENERIC_ACCOUNT_NAMES_COMPACT:
            business_name_present = True
            break
    matches = tuple(
        candidate
        for candidate in candidates
        if candidate.match_name and candidate.match_name in compact_message
    )
    return business_name_present or bool(matches), matches


def _tail_evidence(
    message: str, candidates: tuple[_Candidate, ...]
) -> tuple[bool, tuple[_Candidate, ...]]:
    if _contains_pan(message):
        return True, ()
    tails = set(_TAIL_EVIDENCE_RE.findall(_canonical_text(message)))
    matches = tuple(candidate for candidate in candidates if candidate.option.tail in tails)
    return bool(tails), matches


def resolve_balance_account(source_message: str | None, accounts: Any) -> BalanceResolution:
    """Resolve explicit evidence by intersection; fail closed on malformed API data."""

    if source_message is not None and not isinstance(source_message, str):
        raise BalanceResolutionError("source_message must be text or null")
    candidates = _active_candidates(accounts)
    if not candidates:
        return BalanceResolution(state="empty", options=(), account_id=None, total_count=0)

    if _contains_pan(source_message) or _contains_unsafe_instruction(source_message):
        return _ambiguous(candidates)

    message = _canonical_text(source_message)
    compact_message = _compact_text(message)
    has_name, name_matches = _name_evidence(compact_message, candidates)
    has_tail, tail_matches = _tail_evidence(message, candidates)

    if has_name or has_tail:
        if (has_name and not name_matches) or (has_tail and not tail_matches):
            return _ambiguous(candidates)
        matches = set(candidates)
        if has_name:
            matches.intersection_update(name_matches)
        if has_tail:
            matches.intersection_update(tail_matches)
        if len(matches) != 1:
            return _ambiguous(candidates)
        return _selected(next(iter(matches)), len(candidates))

    if len(candidates) == 1:
        return _selected(candidates[0], 1)
    return _ambiguous(candidates)


def build_resolved_balance_plan(
    plan: CapabilityPlan,
    task: IntentTask,
    resolution: BalanceResolution,
) -> CapabilityPlan:
    """Build the exact read-only balance plan from a proven selected resolution."""

    if not isinstance(plan, CapabilityPlan) or not isinstance(task, IntentTask):
        raise BalanceResolutionError("plan and task must use runtime contracts")
    if (
        task.intent_id != "ccb.account.balance.query"
        or task.action != "QUERY"
        or task.business_object != "account_balance"
        or task.risk_level != "L1"
    ):
        raise BalanceResolutionError("task is outside the balance query contract")
    if not isinstance(resolution, BalanceResolution) or resolution.state != "selected":
        raise BalanceResolutionError("a selected balance resolution is required")
    account_id = resolution.options[0].account_id
    return CapabilityPlan.model_validate(
        {
            "schema_version": "1.0",
            "business_agent_run_id": plan.business_agent_run_id,
            "delegation_id": plan.delegation_id,
            "status": "planned",
            "steps": [
                {
                    "step_id": "step_accounts",
                    "task_id": task.task_id,
                    "capability_id": "bank.account.list",
                    "input_bindings": {},
                    "requires_user_confirmation": False,
                    "depends_on": [],
                },
                {
                    "step_id": "step_balance",
                    "task_id": task.task_id,
                    "capability_id": "bank.account.balance.get",
                    "input_bindings": {"accountId": account_id},
                    "requires_user_confirmation": False,
                    "depends_on": ["step_accounts"],
                },
            ],
        }
    )
