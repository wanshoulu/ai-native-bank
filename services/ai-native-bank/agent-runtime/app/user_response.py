"""Deterministic, user-safe response projection for the mobile bank runtime."""

from __future__ import annotations

import unicodedata
import re
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from .balance_resolution import BalanceResolution
from .card_reference_resolution import CardReferenceResolution
from .product_reference_resolution import ProductReferenceResolution
from .holding_reference_resolution import HoldingReferenceResolution
from .contracts import (
    ConfirmationView,
    IntentTask,
    TaskProgress,
    UserFacingResult,
    UserResponse,
)
from .faq import FaqEntry, FaqError, FaqMatch, MockFaqRepository
from .intent_graph import IntentGraphError, IntentGraphRepository
from .slot_contract import SlotEvaluation


_ACTION_LABELS = {
    "home.account.overview": "查看账户总览",
    "home.details.overview": "查询账户明细",
    "home.transfer.apply": "发起转账",
    "home.transfer.overview": "查询转账记录",
    "credit.card-manage.activation": "卡片激活",
    "credit.card-manage.loss-report": "卡片挂失",
    "credit.card-manage.limit": "额度管理",
    "credit.card-manage.transactions": "查询信用卡明细",
}

_CLARIFICATION_QUESTIONS = {
    "account_balance": "请说明要查询的银行卡名称或尾号后四位。",
    "account_transaction": "请说明要查询的账户和时间范围。",
    "credit_card": "请说明要办理的信用卡名称或尾号后四位。",
    "credit_card_limit": "请说明要调整的信用卡和目标额度。",
    "credit_card_security": "请选择要设置的信用卡，并说明需要调整的安全设置。",
    "credit_card_transaction": "请说明要查询的信用卡和时间范围。",
    "credit_card_bill": "请直接告诉我本次要还多少元；金额不能超过本期未还金额。",
    "bill": "请说明要查询或办理的账单及时间范围。",
    "repayment": "请说明要还款的信用卡和还款金额。",
    "loan": "请说明要查询或办理的贷款类型。",
    "deposit": "请说明要查询或办理的存款产品。",
    "wealth_advice": "请告诉我本次计划投入金额和预计持有时间；投资目标如需调整也可以一起说明。",
}

_NOTICE_MESSAGES = {
    "unsupported": ("暂不支持", "当前智能服务暂不支持这项请求。"),
    "blocked": (
        "暂时无法办理",
        "该请求未通过业务规则校验，请核对信息后重试。",
    ),
    "clarification_required": (
        "需要明确业务",
        "请说明需要办理的具体业务或对象。",
    ),
    "faq_uncovered": (
        "知识暂未覆盖",
        "当前业务知识尚未覆盖这个问题，我不会猜测答案。您可以进入相关功能查看。",
    ),
    "runtime_failed": (
        "请求未完成",
        "智能服务暂时无法完成本次请求，请稍后重试。",
    ),
}

_BUSINESS_TASK_CHOICES = {
    "account_balance": ("先查询账户余额", "查询账户余额"),
    "account_transaction": ("先查询账户明细", "查询账户明细"),
    "transfer_order": ("先办理转账", "发起转账"),
    "credit_card": ("先办理信用卡业务", "办理信用卡业务"),
}


class UserResponseProjectionError(ValueError):
    """Raised when source data cannot be projected without leaking or guessing."""


def _security_compact(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character)[0] not in {"P", "Z", "S", "M", "C"}
    )


def _recognized_transfer_text(payee: object, amount_minor: object) -> str:
    if isinstance(payee, str) and payee.strip() and (
        isinstance(amount_minor, int) and not isinstance(amount_minor, bool)
    ):
        amount = Decimal(amount_minor) / Decimal(100)
        return f"已识别向{payee.strip()}转账 {amount:.2f} 元，"
    if isinstance(payee, str) and payee.strip():
        return f"已识别收款人为{payee.strip()}，"
    if isinstance(amount_minor, int) and not isinstance(amount_minor, bool):
        amount = Decimal(amount_minor) / Decimal(100)
        return f"已识别转账金额为 {amount:.2f} 元，"
    return ""


def _safe_transfer_account_actions(
    invocations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    for invocation in invocations:
        if not isinstance(invocation, dict) or invocation.get("capability_id") != "bank.account.list":
            continue
        output = invocation.get("output")
        if isinstance(output, dict) and isinstance(output.get("accounts"), list):
            accounts = output["accounts"]
            break

    projected: list[tuple[str, str]] = []
    seen_tails: set[str] = set()
    for account in accounts:
        if not isinstance(account, dict) or account.get("status") != "ACTIVE":
            continue
        label = account.get("name")
        masked_no = account.get("maskedNo")
        if not isinstance(label, str) or not re.fullmatch(
            r"[\u4e00-\u9fffA-Za-z0-9·（）() -]{1,20}", label
        ):
            continue
        if re.search(r"指令|密码|令牌|验证码|bank", label, re.IGNORECASE):
            continue
        if not isinstance(masked_no, str):
            continue
        match = re.search(r"(?<!\d)(\d{4})$", masked_no.strip())
        if match is None or match.group(1) in seen_tails:
            continue
        seen_tails.add(match.group(1))
        projected.append((label.strip(), match.group(1)))
    return [
        {
            "id": f"transfer-account-option-{index}",
            "label": label,
            "description": f"尾号 {tail}",
            "kind": "reply",
            "interaction": "submit",
            "payload": {"message": f"使用{label}尾号{tail}转账"},
        }
        for index, (label, tail) in enumerate(projected[:8], 1)
    ]


def _missing_transfer_payees(
    requested_payees: list[str],
    invocations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[str]:
    discovered: list[dict[str, Any]] | None = None
    for invocation in reversed(invocations):
        if not isinstance(invocation, dict) or invocation.get("capability_id") != "bank.payee.list":
            continue
        output = invocation.get("output")
        if isinstance(output, dict) and isinstance(output.get("payees"), list):
            discovered = output["payees"]
            break
    if discovered is None:
        return []

    active = [
        item for item in discovered
        if isinstance(item, dict) and item.get("status") == "ACTIVE"
    ]
    missing: list[str] = []
    for requested in requested_payees:
        normalized = unicodedata.normalize("NFKC", requested).strip().casefold()
        tail_match = re.search(r"(?<!\d)(\d{4})$", normalized)
        matched = any(
            isinstance(item.get("name"), str)
            and unicodedata.normalize("NFKC", item["name"]).strip().casefold() == normalized
            or (
                tail_match is not None
                and isinstance(item.get("maskedNo"), str)
                and item["maskedNo"].strip().endswith(tail_match.group(1))
            )
            for item in active
        )
        if not matched:
            missing.append(requested.strip())
    return missing


class UserResponseProjector:
    """Project validated runtime facts into the six public response strategies.

    The projector never interprets model prose as a navigation target and never
    carries execution credentials into the public response contract.
    """

    def __init__(
        self,
        intent_graph: IntentGraphRepository,
        faq_repository: MockFaqRepository | None = None,
    ) -> None:
        if faq_repository is not None and not isinstance(
            faq_repository, MockFaqRepository
        ):
            raise TypeError("faq_repository must be a MockFaqRepository")
        self._intent_graph = intent_graph
        self._faq_repository = faq_repository

    @staticmethod
    def _response(
        type_name: str,
        title: str,
        body: str,
        *,
        items: tuple[str, ...] | list[str] = (),
        actions: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        source: dict[str, str] | None = None,
        schema_version: str = "1.0",
        task_progress: TaskProgress | None = None,
    ) -> UserResponse:
        try:
            return UserResponse.model_validate(
                {
                    "schemaVersion": schema_version,
                    "type": type_name,
                    "title": title,
                    "body": body,
                    "items": list(items),
                    "actions": list(actions),
                    "source": source,
                    "taskProgress": (
                        task_progress.model_dump(by_alias=True)
                        if task_progress is not None else None
                    ),
                }
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise UserResponseProjectionError(
                "source cannot be projected as a safe user response"
            ) from exc

    @staticmethod
    def missing_transfer_payees(
        requested_payees: list[str],
        successful_invocations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> list[str]:
        return _missing_transfer_payees(requested_payees, successful_invocations)

    def project_transfer_form(
        self,
        *,
        progress: TaskProgress,
        payee_name: str,
        from_account_label: str,
        amount_minor: int,
    ) -> UserResponse:
        if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
            raise UserResponseProjectionError("transfer form amount is invalid")
        amount = Decimal(amount_minor) / Decimal(100)
        return self._response(
            "clarification",
            f"补充第{progress.current}笔收款信息",
            f"已保留当前转账信息，请补充{payee_name}的收款账号。",
            items=(
                f"收款人：{payee_name}",
                f"转出账户：{from_account_label}",
                f"金额：{amount:.2f}元",
            ),
            schema_version="1.1",
            task_progress=progress,
        )

    def project_transfer_full_form(
        self,
        *,
        payee_name: str,
        from_account_label: str = "",
        amount_minor: int = 0,
    ) -> UserResponse:
        items: list[str] = []
        if payee_name:
            items.append(f"收款人：{payee_name}")
        if from_account_label:
            items.append(f"转出账户：{from_account_label}")
        if (
            isinstance(amount_minor, int)
            and not isinstance(amount_minor, bool)
            and amount_minor > 0
        ):
            amount = Decimal(amount_minor) / Decimal(100)
            items.append(f"金额：{amount:.2f}元")
        return self._response(
            "clarification",
            "补全转账信息",
            f"请补充{payee_name or '收款人'}的收款信息并确认转账金额。",
            items=tuple(items),
            schema_version="1.1",
        )

    def _navigation_action(
        self,
        action_key: str,
        *,
        action_id: str,
    ) -> dict[str, Any]:
        if not isinstance(action_key, str) or action_key not in self._intent_graph.action_keys:
            raise UserResponseProjectionError("navigation target is not registered")
        parts = action_key.split(".")
        if len(parts) != 3 or any(not part for part in parts):
            raise UserResponseProjectionError("navigation target is not registered")
        channel, feature_id, target_action_id = parts
        return {
            "id": action_id,
            "label": _ACTION_LABELS.get(action_key, "打开相关服务"),
            "description": None,
            "kind": "navigate",
            "payload": {
                "channel": channel,
                "featureId": feature_id,
                "actionId": target_action_id,
            },
        }

    def project_balance(
        self,
        resolution: BalanceResolution,
    ) -> UserResponse | None:
        """Project balance discovery; selected accounts continue to real execution."""

        if not isinstance(resolution, BalanceResolution):
            raise UserResponseProjectionError(
                "balance projection requires a verified resolution"
            )
        if resolution.state == "selected":
            return None
        if resolution.state == "empty":
            return self._response(
                "notice",
                "暂无可用账户",
                "当前没有可查询余额的账户，请先查看账户状态。",
            )

        selectable = tuple(
            option for option in resolution.options if option.selectable
        )
        if resolution.selection_degraded or not selectable:
            return self.project_navigation(
                "请到账户页选择",
                "当前账户无法安全生成快捷选项，请进入账户总览选择要查询的账户。",
                "home.account.overview",
            )

        actions = [
            {
                "id": f"account-option-{index}",
                "label": option.label,
                "description": f"尾号 {option.tail}",
                "kind": "reply",
                "interaction": "submit",
                "payload": {"message": option.reply_message},
            }
            for index, option in enumerate(selectable[:8], 1)
        ]
        body = "您想查询哪张卡的余额？"
        if resolution.options_truncated:
            body += " 仅显示前 8 张可选账户。"
        return self._response(
            "clarification",
            "选择查询账户",
            body,
            actions=actions,
        )

    def project_slot_resolution(
        self,
        *,
        slot_type: str,
        slot_label: str,
        resolution: object,
    ) -> UserResponse:
        """Project a graph-declared semantic slot without inspecting intent ids."""

        if slot_type == "product_reference" and isinstance(
            resolution, ProductReferenceResolution
        ):
            if resolution.state == "selected":
                raise UserResponseProjectionError(
                    "resolved slot does not require a clarification card"
                )
            if resolution.state == "empty":
                return self._response(
                    "notice",
                    "暂无可申购产品",
                    "当前没有符合条件且处于在售状态的理财产品，请稍后再试。",
                )
            body = f"请选择当前任务需要申购的{slot_label}。"
            if resolution.options_truncated:
                body += " 仅显示前 8 款可选产品。"
            return self._response(
                "clarification",
                f"请选择{slot_label}",
                body,
                actions=[
                    {
                        "id": f"product-option-{index}",
                        "label": option.name,
                        "description": f"{option.risk_level} · {option.category}",
                        "kind": "reply",
                        "interaction": "submit",
                        "payload": {"message": option.reply_message},
                    }
                    for index, option in enumerate(resolution.options[:8], 1)
                ],
            )

        if slot_type == "holding_reference" and isinstance(
            resolution, HoldingReferenceResolution
        ):
            if resolution.state == "selected":
                raise UserResponseProjectionError(
                    "resolved slot does not require a clarification card"
                )
            if resolution.state == "empty":
                return self._response(
                    "notice",
                    "暂无可赎回持仓",
                    "当前没有可赎回的理财持仓，请先确认持仓状态。",
                )
            body = f"请选择当前任务需要赎回的{slot_label}。"
            if resolution.options_truncated:
                body += " 仅显示前 8 笔可选持仓。"
            return self._response(
                "clarification",
                f"请选择{slot_label}",
                body,
                actions=[
                    {
                        "id": f"holding-option-{index}",
                        "label": option.name,
                        "description": f"{option.category}",
                        "kind": "reply",
                        "interaction": "submit",
                        "payload": {"message": option.reply_message},
                    }
                    for index, option in enumerate(resolution.options[:8], 1)
                ],
            )

        if slot_type == "card_reference" and isinstance(
            resolution, CardReferenceResolution
        ):
            if resolution.state == "selected":
                raise UserResponseProjectionError(
                    "resolved slot does not require a clarification card"
                )
            if resolution.state == "empty":
                return self._response(
                    "notice",
                    "暂无可用信用卡",
                    "当前没有可用于该业务的信用卡，请先查看信用卡状态。",
                )
            selectable = tuple(
                option for option in resolution.options if option.selectable
            )
            if resolution.selection_degraded or not selectable:
                return self.project_navigation(
                    "请到信用卡页选择",
                    "当前信用卡无法安全生成快捷选项，请进入信用卡总览选择。",
                    "credit.card.overview",
                )
            body = f"请选择当前任务使用的{slot_label}。"
            if resolution.options_truncated:
                body += " 仅显示前 8 张可选信用卡。"
            return self._response(
                "clarification",
                f"请选择{slot_label}",
                body,
                actions=[
                    {
                        "id": f"card-option-{index}",
                        "label": option.label,
                        "description": f"尾号 {option.tail}",
                        "kind": "reply",
                        "interaction": "submit",
                        "payload": {"message": option.reply_message},
                    }
                    for index, option in enumerate(selectable[:8], 1)
                ],
            )
        if slot_type != "account_reference" or not isinstance(
            resolution, BalanceResolution
        ):
            return self._response(
                "clarification",
                f"请补充{slot_label}",
                f"请补充当前任务所需的{slot_label}。",
            )
        if resolution.state == "selected":
            raise UserResponseProjectionError(
                "resolved slot does not require a clarification card"
            )
        if resolution.state == "empty":
            return self._response(
                "notice",
                "暂无可用账户",
                "当前没有可用于该业务的账户，请先查看账户状态。",
            )
        selectable = tuple(
            option for option in resolution.options if option.selectable
        )
        if resolution.selection_degraded or not selectable:
            return self.project_navigation(
                "请到账户页选择",
                "当前账户无法安全生成快捷选项，请进入账户总览选择。",
                "home.account.overview",
            )
        actions = [
            {
                "id": f"account-option-{index}",
                "label": option.label,
                "description": f"尾号 {option.tail}",
                "kind": "reply",
                "interaction": "submit",
                "payload": {"message": option.reply_message},
            }
            for index, option in enumerate(selectable[:8], 1)
        ]
        body = f"请选择当前任务使用的{slot_label}。"
        if resolution.options_truncated:
            body += " 仅显示前 8 个可选账户。"
        return self._response(
            "clarification",
            f"请选择{slot_label}",
            body,
            actions=actions,
        )

    def project_legacy_result(self, result: UserFacingResult) -> UserResponse:
        if not isinstance(result, UserFacingResult):
            raise UserResponseProjectionError(
                "result projection requires a validated legacy result"
            )
        items = list(result.items[:20])
        if result.primary_action and len(items) < 20:
            items.append(result.primary_action)
        return self._response(
            "result",
            result.title,
            result.summary,
            items=items,
        )

    def project_result_clarification(self, result: UserFacingResult) -> UserResponse | None:
        """Turn a validated non-terminal read result into reply choices.

        Returns ``None`` when the result has no reply actions — e.g. a
        read-only bill inquiry in a multi-task envelope where the
        clarification is driven by a pending follow-up task, not by the
        result itself needing user input.  The caller is expected to
        fall back to :meth:`project_legacy_result` in that case.
        """
        if not isinstance(result, UserFacingResult):
            raise UserResponseProjectionError(
                "clarification projection requires a validated result"
            )
        actions = [
            {
                "id": f"result-followup-{index}",
                "label": item.label,
                "description": None,
                "kind": "reply",
                "interaction": item.interaction,
                "payload": {"message": item.message},
                **(
                    {
                        "form": item.form.model_dump(
                            by_alias=True, exclude_none=True
                        )
                    }
                    if item.form is not None
                    else {}
                ),
            }
            for index, item in enumerate(result.actions[:8], 1)
            if item.message
        ]
        if not actions:
            return None
        return self._response(
            "clarification",
            result.title,
            result.summary,
            items=list(result.items[:20]),
            actions=actions,
        )

    def project_result_with_continue(self, result: UserFacingResult) -> UserResponse:
        """Project a read result as a clarification with a continue action.

        Used in multi-task envelopes where one task's result is ready but
        a pending follow-up task remains.  The result is shown as a
        clarification card with a "继续" reply action so the user can
        proceed to the next task.
        """
        if not isinstance(result, UserFacingResult):
            raise UserResponseProjectionError(
                "result-with-continue requires a validated result"
            )
        actions = [
            {
                "id": "result-continue",
                "label": "继续下一项任务",
                "description": None,
                "kind": "reply",
                "interaction": "submit",
                "payload": {"message": "继续"},
            }
        ]
        return self._response(
            "clarification",
            result.title,
            result.summary,
            items=list(result.items[:20]),
            actions=actions,
        )

    def project_faq(self, entry: FaqEntry) -> UserResponse:
        if self._faq_repository is None:
            raise UserResponseProjectionError("governed FAQ repository is unavailable")
        if type(entry) is not FaqEntry:
            raise UserResponseProjectionError("FAQ entry is not governed")
        try:
            canonical = self._faq_repository.canonical_entry(
                entry.id, version=entry.version
            )
        except (FaqError, TypeError, ValueError) as exc:
            raise UserResponseProjectionError("FAQ entry is not governed") from exc
        if entry is not canonical:
            raise UserResponseProjectionError("FAQ entry is not canonical")
        actions = [
            self._navigation_action(action_key, action_id=f"open-service-{index}")
            for index, action_key in enumerate(canonical.recommended_action_keys, 1)
        ]
        return self._response(
            "answer",
            "业务说明",
            canonical.answer,
            actions=actions,
            source={
                "id": canonical.id,
                "version": canonical.version,
                "label": "Mock 业务知识",
            },
        )

    def project_faq_choices(
        self,
        matches: tuple[FaqMatch, ...] | list[FaqMatch],
        business_tasks: tuple[IntentTask, ...] | list[IntentTask] = (),
    ) -> UserResponse:
        """Split composite requests without inventing FAQ wording or task targets."""

        if self._faq_repository is None:
            raise UserResponseProjectionError("governed FAQ repository is unavailable")
        actions: list[dict[str, Any]] = []
        seen_messages: set[str] = set()
        for match in matches:
            if type(match) is not FaqMatch:
                raise UserResponseProjectionError("FAQ match is not governed")
            try:
                canonical = self._faq_repository.canonical_entry(
                    match.entry.id, version=match.entry.version
                )
            except (FaqError, TypeError, ValueError) as exc:
                raise UserResponseProjectionError("FAQ match is not governed") from exc
            if (
                match.entry is not canonical
                or match.pattern not in canonical.question_patterns
            ):
                raise UserResponseProjectionError("FAQ match is not canonical")
            if match.pattern in seen_messages:
                continue
            seen_messages.add(match.pattern)
            actions.append(
                {
                    "id": f"faq-choice-{len(actions) + 1}",
                    "label": f"先看问题 {len(actions) + 1}",
                    "description": match.pattern,
                    "kind": "reply",
                    "interaction": "submit",
                    "payload": {"message": match.pattern},
                }
            )

        for task in business_tasks:
            if not isinstance(task, IntentTask):
                raise UserResponseProjectionError(
                    "business choice must use the runtime contract"
                )
            label, message = _BUSINESS_TASK_CHOICES.get(
                task.business_object,
                ("先办理其他业务", "继续办理其他业务"),
            )
            if message in seen_messages:
                continue
            seen_messages.add(message)
            actions.append(
                {
                    "id": f"business-choice-{len(actions) + 1}",
                    "label": label,
                    "description": None,
                    "kind": "reply",
                    "interaction": "submit",
                    "payload": {"message": message},
                }
            )

        if len(actions) < 2:
            raise UserResponseProjectionError(
                "composite clarification requires at least two choices"
            )
        if len(actions) > 8:
            raise UserResponseProjectionError(
                "composite clarification exceeds the safe choice limit"
            )
        return self._response(
            "clarification",
            "请先选择一项",
            "这次请求包含多项内容，请选择先处理哪一项。",
            actions=actions,
        )

    def project_confirmation(
        self,
        confirmation: ConfirmationView,
        *,
        task_progress: TaskProgress | None = None,
    ) -> UserResponse:
        if not isinstance(confirmation, ConfirmationView):
            raise UserResponseProjectionError(
                "confirmation projection requires a validated view"
            )
        visible_summary = "\n".join(
            (confirmation.summary.title, confirmation.summary.description)
        )
        compact_summary = _security_compact(visible_summary)
        protected_values = (
            confirmation.checkpoint_id,
            confirmation.capability_id,
            confirmation.confirmation_token,
            *(
                value
                for item in confirmation.confirmations
                for value in (item.checkpoint_id, item.confirmation_token)
            ),
        )
        if any(
            protected and _security_compact(protected) in compact_summary
            for protected in protected_values
        ):
            raise UserResponseProjectionError(
                "confirmation summary contains protected execution data"
            )
        items = list(confirmation.summary.items)
        if (
            confirmation.summary.task_count > 1
            and confirmation.summary.total_amount_minor is not None
        ):
            total = Decimal(confirmation.summary.total_amount_minor) / Decimal(100)
            items.append(f"合计：{total:.2f}元")
        return self._response(
            "confirmation",
            confirmation.summary.title,
            confirmation.summary.description,
            items=items,
            actions=[
                {
                    "id": "confirm-action",
                    "label": "确认",
                    "description": None,
                    "kind": "confirm",
                    "payload": {},
                },
                {
                    "id": "dismiss-action",
                    "label": "取消",
                    "description": None,
                    "kind": "dismiss",
                    "payload": {},
                },
            ],
            schema_version="1.1" if task_progress is not None else "1.0",
            task_progress=task_progress,
        )

    def project_credit_card_repayment_choice(
        self,
        *,
        bill_state: str = "payment_due",
        product_name: str,
        masked_card: str,
        outstanding_minor: int,
        minimum_minor: int,
        due_label: str,
        account_label: str,
        account_available_minor: int,
    ) -> UserResponse:
        """Project only server-verified bill facts into the repayment choice step."""

        if (
            bill_state != "payment_due"
            or not product_name or len(product_name) > 120
            or not re.fullmatch(r"\*{2,12}\d{4}", masked_card.replace(" ", ""))
            or not isinstance(outstanding_minor, int) or isinstance(outstanding_minor, bool)
            or outstanding_minor <= 0
            or not isinstance(minimum_minor, int) or isinstance(minimum_minor, bool)
            or minimum_minor <= 0 or minimum_minor > outstanding_minor
            or not due_label or len(due_label) > 40
            or not account_label or len(account_label) > 120
            or not isinstance(account_available_minor, int)
            or isinstance(account_available_minor, bool) or account_available_minor < 0
        ):
            raise UserResponseProjectionError(
                "credit-card repayment choice facts are invalid"
            )
        money = lambda value: f"{Decimal(value) / Decimal(100):,.2f}元"
        return self._response(
            "clarification",
            "请选择本次还款金额",
            f"已核对{product_name} · 尾号{masked_card.replace(' ', '')[-4:]}的本期账单。",
            items=[
                f"本期未还：{money(outstanding_minor)}",
                f"最低还款：{money(minimum_minor)}",
                f"到期日：{due_label}",
                f"付款账户：{account_label}",
                f"账户可用：{money(account_available_minor)}",
            ],
            actions=[
                {
                    "id": "repay-full",
                    "label": f"全额还款 {money(outstanding_minor)}",
                    "description": "还清本期未还金额",
                    "kind": "reply",
                    "interaction": "submit",
                    "payload": {"message": "全额还款"},
                },
                {
                    "id": "repay-minimum",
                    "label": f"最低还款 {money(minimum_minor)}",
                    "description": "剩余未还部分将按约定计息",
                    "kind": "reply",
                    "interaction": "submit",
                    "payload": {"message": "最低还款"},
                },
                {
                    "id": "repay-custom",
                    "label": "输入其他金额",
                    "description": "直接告诉我本次还款金额",
                    "kind": "reply",
                    "interaction": "compose",
                    "payload": {"message": "我要输入其他还款金额"},
                },
            ],
        )

    def project_credit_card_repayment_settled(
        self,
        *,
        product_name: str,
        masked_card: str,
        due_label: str,
        continue_next: bool = False,
        task_progress: TaskProgress | None = None,
    ) -> UserResponse:
        """Project a verified zero-balance bill as a successful no-op result."""

        compact_card = masked_card.replace(" ", "") if isinstance(masked_card, str) else ""
        if (
            not isinstance(product_name, str) or not product_name or len(product_name) > 120
            or not re.fullmatch(r"\*{2,12}\d{4}", compact_card)
            or not isinstance(due_label, str) or not due_label or len(due_label) > 40
        ):
            raise UserResponseProjectionError(
                "settled credit-card bill facts are invalid"
            )
        actions = []
        if continue_next:
            actions.append({
                "id": "continue-next-task",
                "label": "继续办理下一项",
                "description": None,
                "kind": "reply",
                "interaction": "submit",
                "payload": {"message": "继续办理下一项"},
            })
        return self._response(
            "result",
            "本期信用卡账单已结清",
            f"{product_name} · 尾号{compact_card[-4:]}本期未还金额为 0.00 元，无需重复还款。",
            items=[f"到期日：{due_label}", "账单状态：已结清"],
            actions=actions,
            schema_version="1.1" if task_progress is not None else "1.0",
            task_progress=task_progress,
        )

    def project_navigation(
        self,
        title: str,
        body: str,
        action_key: str,
    ) -> UserResponse:
        return self._response(
            "navigation",
            title,
            body,
            actions=[self._navigation_action(action_key, action_id="open-service")],
        )

    def project_navigation_for_intent(self, intent_id: str) -> UserResponse:
        try:
            action_key = self._intent_graph.action_key_for_intent(intent_id)
        except (IntentGraphError, TypeError, ValueError) as exc:
            raise UserResponseProjectionError(
                "navigation intent is not registered"
            ) from exc
        return self.project_navigation(
            "打开相关服务",
            "已找到对应服务入口，您可以进入页面继续操作。",
            action_key,
        )

    def project_non_task_guidance(self) -> UserResponse:
        """Invite the user to start a meaningful request without business-slot language."""

        suggestions = (
            ("查账户余额", "查账户余额"),
            ("发起转账", "发起转账"),
            ("查信用卡账单", "查信用卡账单"),
            ("查询账户明细", "查询账户明细"),
        )
        return self._response(
            "notice",
            "请告诉我您想办理什么",
            "您可以直接说出想查询的问题或需要办理的业务，也可以选择一个常用服务。",
            actions=[
                {
                    "id": f"non-task-suggestion-{index}",
                    "label": label,
                    "description": None,
                    "kind": "reply",
                    "interaction": "submit",
                    "payload": {"message": message},
                }
                for index, (label, message) in enumerate(suggestions, 1)
            ],
        )

    def project_clarification(
        self,
        task: IntentTask | None,
        *,
        slot_evaluation: SlotEvaluation | None = None,
        successful_invocations: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> UserResponse:
        if task is not None and not isinstance(task, IntentTask):
            raise UserResponseProjectionError(
                "clarification task must use the runtime contract"
            )
        if task is not None and task.business_object == "transfer_order":
            if not isinstance(slot_evaluation, SlotEvaluation):
                return self._response(
                    "clarification", "请补充办理信息", "请说明收款人、转出账户和转账金额。"
                )
            return self._project_transfer_clarification(
                slot_evaluation, successful_invocations
            )
        business_object = task.business_object if task is not None else ""
        body = _CLARIFICATION_QUESTIONS.get(
            business_object,
            "请说明需要办理的具体业务对象和要求。",
        )
        title = "请补充投资安排" if business_object == "wealth_advice" else "请补充办理信息"
        return self._response("clarification", title, body)

    def project_amount_scope_clarification(
        self,
        amount_value: object,
        recipient_count: int,
    ) -> UserResponse:
        if not 2 <= recipient_count <= 16:
            raise UserResponseProjectionError("amount scope requires multiple recipients")
        try:
            amount = Decimal(str(amount_value))
        except Exception as exc:
            raise UserResponseProjectionError("amount scope value is invalid") from exc
        if not amount.is_finite() or amount <= 0 or amount > Decimal("50000"):
            raise UserResponseProjectionError("amount scope value is outside safe bounds")
        amount_label = format(amount.normalize(), "f")
        count_label = {2: "两", 3: "三", 4: "四"}.get(
            recipient_count, str(recipient_count)
        )
        return self._response(
            "clarification",
            "请确认转账金额",
            f"{amount_label}元是每人{amount_label}元，还是{count_label}人合计{amount_label}元？",
            actions=[
                {
                    "id": "amount-scope-per-task",
                    "label": f"每人{amount_label}元",
                    "description": f"将分别向{recipient_count}位收款人转账",
                    "kind": "reply",
                    "interaction": "submit",
                    "payload": {"message": f"每人{amount_label}元"},
                },
                {
                    "id": "amount-scope-total",
                    "label": f"{count_label}人合计{amount_label}元",
                    "description": "下一步分别确认每笔金额",
                    "kind": "reply",
                    "interaction": "submit",
                    "payload": {"message": f"{count_label}人合计{amount_label}元"},
                },
            ],
        )

    def _project_transfer_clarification(
        self,
        evaluation: SlotEvaluation,
        invocations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> UserResponse:
        payee = evaluation.semantic_values.get("payee")
        amount_minor = evaluation.resolved_values.get("amountMinor")
        recognized = _recognized_transfer_text(payee, amount_minor)

        if "payee" in evaluation.ambiguous:
            return self._response(
                "clarification", "请选择收款人", "匹配到多个收款人，请补充姓名或银行卡尾号。"
            )
        if "payee" in evaluation.missing:
            return self._response(
                "clarification", "请补充收款人", "请告诉我收款人姓名或银行卡尾号。"
            )
        if "fromAccount" in evaluation.ambiguous:
            body = f"{recognized}匹配到多个转出账户，请选择一个。"
            return self._response(
                "clarification", "请选择转出账户", body,
                actions=_safe_transfer_account_actions(invocations),
            )
        if "fromAccount" in evaluation.missing:
            body = f"{recognized}请选择转出账户。"
            return self._response(
                "clarification", "请选择转出账户", body,
                actions=_safe_transfer_account_actions(invocations),
            )
        if "amount" in evaluation.invalid:
            return self._response(
                "clarification", "请重新输入金额", "请输入大于 0 且不超过 50,000.00 元的有效转账金额。"
            )
        if "amount" in evaluation.missing:
            return self._response(
                "clarification", "请补充转账金额", f"{recognized}请输入转账金额。"
            )
        return self._response(
            "clarification", "请核对转账信息", "请核对收款人、转出账户和转账金额。"
        )

    def project_multi_transfer_clarification(
        self,
        evaluations: list[SlotEvaluation] | tuple[SlotEvaluation, ...],
        successful_invocations: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> UserResponse:
        if len(evaluations) < 2:
            raise UserResponseProjectionError("multi transfer clarification requires multiple goals")
        payees = [item.semantic_values.get("payee") for item in evaluations]
        amounts = [item.resolved_values.get("amountMinor") for item in evaluations]
        if not all(isinstance(item, str) and item.strip() for item in payees):
            return self._response(
                "clarification", "请补充收款人", "请分别补充每笔转账的收款人。"
            )
        payee_label = "、".join(str(item).strip() for item in payees)
        if len(set(amounts)) == 1 and isinstance(amounts[0], int):
            amount = Decimal(amounts[0]) / Decimal(100)
            recognized = f"已识别将分别向{payee_label}各转账 {amount:.2f} 元，"
        else:
            recognized = f"已识别将分别向{payee_label}办理转账，"
        if any("amount" in item.invalid for item in evaluations):
            return self._response(
                "clarification",
                "请分别输入有效金额",
                f"请分别告诉我向{payee_label}转账的金额，每笔金额需大于0且不超过50,000.00元。",
            )
        if any("amount" in item.missing for item in evaluations):
            return self._response(
                "clarification",
                "请分别输入转账金额",
                f"合计金额不会自动均分，请分别告诉我向{payee_label}转账的金额。",
            )
        if any("fromAccount" in item.ambiguous for item in evaluations):
            return self._response(
                "clarification", "请选择转出账户", recognized + "匹配到多个转出账户，请选择一个。",
                actions=_safe_transfer_account_actions(successful_invocations),
            )
        if any("fromAccount" in item.missing for item in evaluations):
            return self._response(
                "clarification", "请选择转出账户", recognized + "请选择共同使用的转出账户。",
                actions=_safe_transfer_account_actions(successful_invocations),
            )
        missing_payees = _missing_transfer_payees(
            [str(item).strip() for item in payees], successful_invocations
        )
        if missing_payees:
            return self._response(
                "clarification",
                "未找到收款人",
                "未在当前收款人列表中找到"
                + "、".join(missing_payees)
                + "，请先添加收款人，或补充正确的姓名或银行卡尾号。",
            )
        return self._response(
            "clarification", "请核对转账信息", recognized + "请核对每笔信息。"
        )

    def project_notice(
        self,
        status: str,
        *,
        recommended_action_key: str | None = None,
    ) -> UserResponse:
        title, body = _NOTICE_MESSAGES.get(status, _NOTICE_MESSAGES["runtime_failed"])
        actions: list[dict[str, Any]] = []
        if recommended_action_key is not None:
            actions.append(
                self._navigation_action(
                    recommended_action_key,
                    action_id="open-related-service",
                )
            )
        return self._response("notice", title, body, actions=actions)

    def project_notice_for_intent(
        self,
        status: str,
        intent_id: str,
        *,
        include_actions: bool = True,
    ) -> UserResponse:
        # 优先从意图图谱获取功能标题，让"暂不支持"文案说清楚是哪项
        # 功能不支持，而不是笼统的"这项请求"。标题获取失败时回退到
        # 固定文案，不阻塞通知投影。
        intent_title: str | None = None
        try:
            candidate = self._intent_graph.candidate_for_intent(
                intent_id, score=0, evidence=["notice_projection"]
            )
            intent_title = candidate.title
        except (IntentGraphError, TypeError, ValueError):
            pass
        # action_key 可选：mock-only 意图可能未注册导航入口，不应因此
        # 让通知投影整体失败。作为 prefix 使用时（include_actions=False）
        # 跳过 action_key 查找，因为 prefix 契约要求无 actions。
        actions: list[dict[str, Any]] = []
        if include_actions:
            action_key: str | None = None
            try:
                action_key = self._intent_graph.action_key_for_intent(intent_id)
            except (IntentGraphError, TypeError, ValueError):
                pass
            if action_key is not None:
                actions.append(
                    self._navigation_action(action_key, action_id="open-related-service")
                )
        if intent_title is not None and status == "unsupported":
            title = "暂不支持"
            body = f"{intent_title}功能暂不支持，已为您继续处理其他请求。"
        else:
            title, body = _NOTICE_MESSAGES.get(
                status, _NOTICE_MESSAGES["runtime_failed"]
            )
        return self._response("notice", title, body, actions=actions)
