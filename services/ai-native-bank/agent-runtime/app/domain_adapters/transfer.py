"""Transfer compatibility behavior isolated from the generic dual-agent runtime.

The OpenJiuwen Skill remains the business method authority. This adapter only
projects deterministic legacy UI/state details while those contracts migrate to
graph response policies.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from app.contracts import IntentAmbiguity, IntentTaskEnvelope, SlotValue
from app.intent_shape import DISTRIBUTION_MARKERS, TOTAL_MARKERS
from app.slot_contract import evaluate_amount


def message_amount_scope(message: str | None) -> str | None:
    """Classify the declared amount-scope wording of the current message.

    与形状层（intent_shape）同一词表与优先级：合计类标记优先于分摊类
    标记——「每人100元，总共200元」声明的是总额校验口径，而非分摊。
    """

    text = message or ""
    if any(marker in text for marker in TOTAL_MARKERS):
        return "total"
    if any(marker in text for marker in DISTRIBUTION_MARKERS):
        return "per_task"
    return None


def _message_declares_amount_scope(message: str | None) -> bool:
    return message_amount_scope(message) is not None


_DECLARED_SOURCES = frozenset({"user_explicit", "user_selection"})


def _declared_amount(
    slot: SlotValue | None, *, is_minor: bool, allow_inherited: bool = False
) -> Decimal | None:
    """Resolve an amount only if the user declared it in the current turn.

    继承自会话上下文（source=context/intent_default）的金额口径在先前
    轮次已经确定（声明过分摊或经澄清），本轮不得重新追问，否则多轮
    任务（如补充收款账户）会被重复的金额澄清卡卡死。replan 拆分例外：
    平台把用户单一请求重实例化为 N 个任务，继承金额正是用户原话申报
    的那份（只是连续性重建盖了 context 戳），分摊口径构造上不可能已
    澄清，allow_inherited 时视作用户声明。
    """

    if not isinstance(slot, SlotValue):
        return None
    if slot.source not in _DECLARED_SOURCES and not (
        allow_inherited and slot.source == "context"
    ):
        return None
    assessment = evaluate_amount(slot.value, already_minor=is_minor)
    if assessment.state != "resolved":
        return None
    return Decimal(str(assessment.semantic_value))


def _message_amount_value(message: str | None) -> Decimal | None:
    """Extract a declared amount from the user message as fallback.

    LLM 意图引擎偶尔将金额标记为 intent_default 而非 user_explicit，
    导致 _declared_amount 的来源检查失败。用户原消息中的金额是用户
    声明的真实口径，从消息中提取作为回退。与 project_ambiguity 的
    message 回退逻辑一致。
    """
    if not message:
        return None
    match = re.search(r'(\d+(?:\.\d{1,2})?)', message)
    if match:
        assessment = evaluate_amount(match.group(1))
        if assessment.state == "resolved":
            return Decimal(str(assessment.semantic_value))
    return None


def derive_ambiguities(*, tasks, message, shared_slots=None, replan_split=False):
    """Derive task-identity-scoped ambiguities from evaluated task content.

    域不变量：本轮用户声明的一个金额对应多个转账任务且原文未声明
    分摊口径时，"每人还是合计"未定，必须澄清。派生只依赖任务本身
    （槽位/共享槽位）及其来源，不依赖任务顺序或信封构成，因此单意图、
    同意图多任务、跨域多意图三种信封下的行为一致；上下文继承的金额
    不派生。金额不可解析时交由常规缺槽澄清，不派生。replan_split 仅由
    同意图结构重组采纳路径传入（见 _declared_amount 例外说明）。

    message 回退：LLM 意图引擎可能将金额标记为 intent_default 而非
    user_explicit，导致 _declared_amount 的来源检查失败。用户原消息
    中的金额是用户声明的真实口径，从消息中提取作为回退，与
    project_ambiguity 的回退逻辑一致。
    """

    if len(tasks) < 2 or _message_declares_amount_scope(message):
        return []
    task_amounts = [
        _declared_amount(
            task.slots.get("amount") or task.slots.get("amountMinor"),
            is_minor=task.slots.get("amountMinor") is not None,
            allow_inherited=replan_split,
        )
        for task in tasks
    ]
    if all(value is not None for value in task_amounts):
        values = [value for value in task_amounts if value is not None]
        if any(item != values[0] for item in values[1:]):
            return []
    else:
        shared = shared_slots.get("amount") if isinstance(shared_slots, dict) else None
        if _declared_amount(shared, is_minor=False) is None:
            # 金额非本轮用户声明或不可解析：LLM 可能标记为 intent_default
            # 而非 user_explicit，从用户原消息中提取作为回退。
            if _message_amount_value(message) is None:
                return []
    return [
        IntentAmbiguity.model_validate({
            "type": "amount_scope",
            "task_ids": [task.task_id for task in tasks],
            "options": ["per_task", "total"],
        })
    ]


def ambiguity_is_projectable(ambiguity) -> bool:
    """域内可投影性判定：退化歧义不应让 run 崩成 blocked。

    派生侧从不产出引用任务数 <2 的 amount_scope（derive_ambiguities
    的域不变量）；模型 planner 偶发声明单任务 amount_scope——"每人还是
    合计"对单一收款人无意义，无法安全投影澄清卡。交由上层跳过该歧义
    落回常规槽位澄清，与其他投影垃圾的机械归一同一原则。

    本域投影契约只实现了 amount_scope（project_ambiguity 对其他类型
    直接抛 "unsupported transfer ambiguity"）。模型 planner 偶发声明
    其他类型（如 payee_ambiguity，options 携带中文收款人名）——没有
    下游消费方，同样跳过落回常规槽位澄清，而不是让投影抛错把整轮
    崩成 blocked。其他域不受影响（business_profile 对无适配器域仍
    保持 fail-closed）。
    """

    if ambiguity.type == "amount_scope":
        return len(ambiguity.task_ids or ()) >= 2
    return False


def project_ambiguity(
    envelope: IntentTaskEnvelope,
    ambiguity: IntentAmbiguity,
    projector: Any,
    *,
    message: str | None = None,
):
    if ambiguity.type != "amount_scope":
        raise ValueError("unsupported transfer ambiguity")
    shared = envelope.shared_slots.get("amount")
    shared_minor = envelope.shared_slots.get("amountMinor")
    value = None
    if shared is not None:
        assessment = evaluate_amount(shared.value)
        if assessment.state == "resolved":
            value = assessment.semantic_value
    if value is None and shared_minor is not None:
        assessment = evaluate_amount(shared_minor.value, already_minor=True)
        if assessment.state == "resolved":
            value = assessment.semantic_value
    if value is None:
        values = []
        # 跨域信封里只看本歧义引用的转账任务，其他域任务没有金额槽位是正常的。
        referenced = set(ambiguity.task_ids)
        for task in envelope.tasks:
            if task.task_id not in referenced:
                continue
            slot = task.slots.get("amount") or task.slots.get("amountMinor")
            if slot is None:
                continue
            assessment = evaluate_amount(
                slot.value,
                already_minor=slot is task.slots.get("amountMinor"),
            )
            if assessment.state != "resolved":
                continue
            values.append(Decimal(str(assessment.semantic_value)))
        if values and all(item == values[0] for item in values[1:]):
            value = values[0]
    # Fallback: extract amount from the original user message when slots
    # are empty (LLM intent engine didn't fill amount into slots for
    # multi-task envelopes).
    if value is None and message:
        match = re.search(r'(\d+(?:\.\d{1,2})?)', message)
        if match:
            assessment = evaluate_amount(match.group(1))
            if assessment.state == "resolved":
                value = assessment.semantic_value
    if value is None:
        return None
    return projector.project_amount_scope_clarification(value, len(ambiguity.task_ids))


def withhold_shared_slot(
    *,
    name: str,
    context_ambiguities: tuple[IntentAmbiguity, ...],
    message: str,
) -> bool:
    unresolved = any(item.type == "amount_scope" for item in context_ambiguities)
    explicitly_per_task = bool(re.search(r"每人|每个|各|分别", message))
    return name == "amount" and unresolved and not explicitly_per_task


_SCOPE_PENDING_AMOUNT_SLOTS = frozenset({"amount", "amountMinor"})


def withhold_scope_inherited_amount_slot(
    *, name: str, context_ambiguities, message: str
) -> bool:
    """合计口径声明使歧义继承的每笔金额槽位失效。

    域不变量：待消解 amount_scope 下，任务槽位/共享槽位/本轮消息中的
    金额都是"未定口径时的申报值"。用户声明合计后，这些金额是总额而非
    每笔份额——声明的投影语义要求逐笔追问（project_multi_transfer_
    clarification 明确"合计金额不会自动均分"），不自动均分。因此歧义按
    口径已明消解，同时继承金额槽位失效，落入常规缺槽澄清。仅当本轮
    消息声明合计口径且存在待消解歧义时生效；分摊口径（每人/各）下
    金额本就是每笔份额，照常生效。
    """

    if name not in _SCOPE_PENDING_AMOUNT_SLOTS:
        return False
    if message_amount_scope(message) != "total":
        return False
    return any(
        item.type == "amount_scope" and len(item.task_ids or ()) >= 2
        for item in context_ambiguities
    )


def ambiguity_is_resolved(
    ambiguity: IntentAmbiguity,
    *,
    context_ambiguities: tuple[IntentAmbiguity, ...],
    message: str,
    tasks: list[Any],
) -> bool:
    if ambiguity.type != "amount_scope":
        return False
    source = next(
        (item for item in context_ambiguities if item.type == "amount_scope"),
        None,
    )
    scope = message_amount_scope(message)
    if source is None or scope is None:
        return False
    if scope == "total":
        # 合计口径一经声明即消解：每笔金额由 withhold_scope_inherited_
        # amount_slot 失效并走常规缺槽澄清逐笔追问，不自动均分。
        return True
    resolved = {
        task.task_id for task in tasks
        if isinstance(task.slots.get("amountMinor"), SlotValue)
    }
    return set(source.task_ids).issubset(resolved)
