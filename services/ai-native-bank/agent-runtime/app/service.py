"""Two-Agent orchestration with a deliberately narrow A2A boundary."""

from __future__ import annotations

import uuid
import hashlib
import copy
import json
import time
import logging
import os
import re
import secrets
import asyncio
import weakref
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from pydantic import ValidationError

from app.contracts import (
    BusinessWorkflowReceipt,
    BusinessWorkflowResumeRequest,
    BusinessWorkflowResumeResponse,
    CapabilityPlan,
    ClarificationForm,
    ConversationContext,
    ConversationState,
    ConfirmationView,
    IntentTaskEnvelope,
    IntentTask,
    PageContext,
    RuntimeAgentRunRequest,
    RuntimeAgentRunResponse,
    SafetyClarificationSubmission,
    SlotValue,
    TaskProgress,
    UserFacingResult,
    UserResponse,
    UserMemoryCandidateHint,
)
from app.input_rail import DisabledInputRail
from app.safety_gate import (
    ClarificationPermit,
    ExecutionPermit,
    PermitValidationError,
    RailDecision,
)
from app.rails import (
    IntentConstraint,
    graph_bound_discovery_applies,
    validate_intent_constraints,
    validate_graph_bound_discovery,
)
from app.intent_graph import IntentCandidate, IntentGraphError, IntentGraphRepository
from app.semantic_recall import SemanticRecallIndex
from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4
from app.subgraph_reasoning_v4 import SubgraphReasoningV4, _STRONG_MATCH_THRESHOLD
from app.llm_judge_v4 import LLMJudgeV4
from app.task_continuity import ContinuityDecision, FrameworkTaskContinuity
from app.faq import FaqAnalysis, MockFaqRepository
from app.config import RuntimeSettings
from app.node_gateway import (
    AgentCapabilityInvocationError,
    AgentCapabilityInvoker,
)
from app.user_result import project_user_result
from app.deepagent_factory import SessionRevisionConflict
from app import model_capture
from app.account_reference_resolution import (
    AccountReferenceResolution,
    AccountReferenceResolutionError,
    resolve_account_reference,
)
from app.card_reference_resolution import (
    CardReferenceResolution,
    CardReferenceResolutionError,
    resolve_card_reference,
)
from app.product_reference_resolution import (
    ProductReferenceResolution,
    ProductReferenceResolutionError,
    resolve_product_reference,
)
from app.holding_reference_resolution import (
    HoldingReferenceResolution,
    HoldingReferenceResolutionError,
    resolve_holding_reference,
)
from app.party_reference_resolution import (
    PartyReferenceResolution,
    PartyReferenceResolutionError,
    resolve_party_reference,
)
from app.task_slot_runtime import (
    TaskSlotDecision,
    TaskSlotRuntimeError,
    active_task_capability_ids,
    build_graph_action_plan,
    build_graph_read_plan,
    decide_active_task,
    runtime_capability_phase,
)
from app.user_response import UserResponseProjectionError, UserResponseProjector
from app.trace import AgentTraceEvent, TraceError, build_trace_event, project_safe_run
from app.slot_contract import (
    extract_clarification_answer_slots,
    extract_normalized_message_slots,
    normalize_account_reference,
    SlotContractEvaluator,
    SlotEvaluation,
    with_provider_resolved_reference,
)
from app.intent_shape import IntentShapeAssessment, analyze_intent_shape
from app.intent_lifecycle import next_envelope_revision
from app.business_loop import (
    AUTHORITATIVE_NODE_WORKFLOW_KEY,
    BusinessLoopOutcome,
    ReplanTaskProposal,
    TodoSnapshotItem,
    business_session_id,
    business_workflow_id,
    normalize_terminal_loop_submission,
    todo_belongs_to_goal,
    validate_authoritative_todo_snapshot,
    validate_goal_coverage,
)
from app.business_todo import GoalState, select_active_goal, sequential_progress, is_intent_task_in_play
from app.business_orchestrator import (
    BusinessLoopAdvance,
    BusinessLoopCoordinator,
    BusinessLoopInvariantError,
    GuidedWorkflowPlan,
    TaskInvocation,
    goal_states_from_todos,
    plan_obligation_snapshot,
    plan_guided_goal,
)
from app.todo_ledger import TodoLedger, TodoLedgerSnapshot
from app.business_profile import (
    all_use_transfer_compatibility_adapter,
    derive_domain_ambiguities,
    domain_ambiguity_is_projectable,
    domain_ambiguity_is_resolved,
    project_domain_ambiguity,
    should_withhold_scope_inherited_slot,
    should_withhold_shared_slot,
    uses_transfer_compatibility_adapter,
)
from app.memory_runtime import (
    HttpMemoryGateway, MemoryGatewayError, MemoryRecallResult,
    summarize_context_pack,
)
from app.memory_candidates import (
    MemoryWriteSummary,
    build_canonical_episode_candidates,
    build_user_preference_candidates,
)
from app.memory_write_gateway import MemoryWriteGateway, MemoryWriteGatewayError


logger = logging.getLogger("ai_native_bank.agent_runtime.service")

# 超强匹配保护阈值:词法 top1 分数 >= 此值时,LLM 重排后仍保留原 top1
# ——避免 LLM 把多关键词强匹配(如 3+ 关键词命中)的 top1 错误排走
_SUPER_STRONG_MATCH_THRESHOLD = 145


def _select_judge_candidates(candidates: list, limit: int = 12) -> list:
    """Bound the judge input without starving an appended recall channel.

    Scores are comparable within each source only: semantic/subgraph scores
    encode source ranks, rather than calibrated lexical relevance. Rank each
    source stably, then share the budget in lexical/subgraph/vector order.
    The original lexical head stays first; repeated intent IDs use one slot.
    """
    channels: dict[str, list] = {"lexical": [], "subgraph": [], "vector": []}
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.intent_id in seen:
            continue
        seen.add(candidate.intent_id)
        evidence = candidate.evidence
        if any(ev == "semantic_recall" for ev in evidence):
            channel = "vector"
        elif any(ev.startswith(("subgraph:", "graph_expand:")) for ev in evidence):
            channel = "subgraph"
        else:
            channel = "lexical"
        channels[channel].append(candidate)
    for rows in channels.values():
        rows.sort(key=lambda candidate: -candidate.score)

    selected = []
    rank = 0
    while len(selected) < limit:
        added = False
        for rows in channels.values():
            if rank < len(rows):
                selected.append(rows[rank])
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
        rank += 1
    return selected


def _report_graph_usage(constraint_count: int) -> None:
    """Fire-and-forget：意图图谱召回命中即视为一次图谱知识消费。"""
    if constraint_count <= 0:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    from app.resource_registry import _USAGE_TASKS, _post_usage_event

    task = loop.create_task(_post_usage_event({
        "carrier": "graph",
        "caller": "ai-native-bank",
        "query": "intent-recall",
        "hits": constraint_count,
    }))
    _USAGE_TASKS.add(task)
    task.add_done_callback(_USAGE_TASKS.discard)


_SAFE_RESTORE_ERROR_TYPES = frozenset({
    "AgentCapabilityInvocationError",
    "AgentContractError",
    "ValidationError",
    "ValueError",
})


def _safe_restore_error_type(exc: BaseException) -> str:
    error_type = type(exc).__name__
    return error_type if error_type in _SAFE_RESTORE_ERROR_TYPES else "Error"


def _authoritative_node_todo_restore(
    workflow: dict[str, Any],
    *,
    resume_user_reply: bool = False,
) -> tuple[dict[str, Any], TodoLedgerSnapshot]:
    try:
        items = validate_authoritative_todo_snapshot(workflow["todoSnapshot"])
        revision = workflow["todoRevision"]
        expected_digest = workflow["todoDigest"]
    except Exception:
        raise AgentContractError(
            "persisted workflow todo snapshot is invalid"
        ) from None
    computed_digest = TodoLedger.digest(items)
    if expected_digest != computed_digest:
        raise AgentContractError("persisted workflow todo digest is invalid")
    snapshot = TodoLedgerSnapshot(
        revision=revision,
        digest=computed_digest,
        items=items,
    )
    if resume_user_reply:
        snapshot = _migrate_legacy_platform_clarification_todo(workflow, snapshot)
        computed_digest = snapshot.digest
        items = snapshot.items
    return ({
        "todoRevision": revision,
        "todoSnapshot": [
            {
                "id": item.id,
                "status": item.status,
                "dependsOn": list(item.depends_on),
            }
            for item in items
        ],
        "todoDigest": computed_digest,
    }, snapshot)


def _migrate_legacy_platform_clarification_todo(
    workflow: dict[str, Any],
    snapshot: TodoLedgerSnapshot,
) -> TodoLedgerSnapshot:
    """Reopen the real single-goal Todo when resuming legacy clarifications.

    Older result-driven clarification persistence appended a platform-only
    ``<task>:awaiting_clarification`` Todo after the business agent had already
    completed its real Todo. The marker carried no executable semantics and
    survived the user's reply, so the authoritative ledger could disagree with
    a correctly completed business-agent outcome. Migrate only that exact,
    mechanically identifiable legacy shape; native model-authored clarification
    Todos remain untouched.
    """

    active_goal = workflow.get("activeGoalTaskId")
    active_todo = workflow.get("activeTodoId")
    if (
        workflow.get("status") != "awaiting_clarification"
        or not isinstance(active_goal, str)
        or active_todo != f"{active_goal}:awaiting_clarification"
        or len(workflow.get("delegatedTaskIds", [])) != 1
    ):
        return snapshot
    remaining = tuple(item for item in snapshot.items if item.id != active_todo)
    candidates = [item for item in remaining if item.status != "cancelled"]
    if not candidates:
        return snapshot
    reopen_id = candidates[-1].id
    migrated = tuple(
        TodoSnapshotItem(
            id=item.id,
            status=("in_progress" if item.id == reopen_id else item.status),
            depends_on=list(item.depends_on),
        )
        for item in remaining
    )
    return TodoLedgerSnapshot(
        revision=snapshot.revision,
        digest=TodoLedger.digest(migrated),
        items=migrated,
    )


def _correct_todo_regression(
    restored: TodoLedgerSnapshot,
    snapshot: TodoLedgerSnapshot,
) -> TodoLedgerSnapshot:
    """Prevent terminal Todo items from regressing to non-terminal states.

    DeepAgent may internally reset already-completed or cancelled Todos to
    ``in_progress`` when it re-enters for a confirmation resume. The platform
    owns the authoritative Todo ledger, so any terminal Todo that appears
    non-terminal in the freshly loaded snapshot is corrected back to its
    authoritative status. This keeps the platform's goal computation
    consistent with the business loop's outcome without modifying DeepAgent's
    internal behaviour.
    """
    restored_status = {item.id: item.status for item in restored.items}
    corrected: list[TodoSnapshotItem] = []
    regressed_count = 0
    for item in snapshot.items:
        authoritative = restored_status.get(item.id)
        if (
            authoritative in {"completed", "cancelled"}
            and item.status not in {"completed", "cancelled"}
        ):
            corrected.append(TodoSnapshotItem(
                id=item.id,
                status=authoritative,
                depends_on=list(item.depends_on),
            ))
            regressed_count += 1
        else:
            corrected.append(item)
    if regressed_count == 0:
        return snapshot
    items = tuple(corrected)
    logger.warning(
        "Corrected terminal Todo regression from authoritative snapshot "
        "regressed_count=%d",
        regressed_count,
    )
    return TodoLedgerSnapshot(
        revision=snapshot.revision,
        digest=TodoLedger.digest(items),
        items=items,
    )


def _require_legitimate_goal_narrowing(
    workflow: dict[str, Any],
    delegated_task_ids: list[str],
) -> None:
    """接续委托允许合法收缩：只允许丢弃台账中已终态的目标。

    已交付结果的任务不再作为待办 goal 下发，接续 envelope 因此可能少于
    持久化工作流的委托集合。只要接续没有发明新目标、且每个被丢弃的目标
    在持久化 Todo 台账中全部终态（completed/cancelled，即结果已交付或已
    取消），就视为同一工作流的合法延续；其余情况仍按目标漂移拒绝。
    """
    persisted_ids = [str(item) for item in workflow.get("delegatedTaskIds", [])]
    incoming_ids = [str(item) for item in delegated_task_ids]
    if not persisted_ids or not incoming_ids:
        raise AgentContractError("persisted workflow delegated goals changed")
    persisted_set = set(persisted_ids)
    if any(item not in persisted_set for item in incoming_ids):
        raise AgentContractError("persisted workflow delegated goals changed")
    dropped = [item for item in persisted_ids if item not in set(incoming_ids)]
    if not dropped:
        return
    try:
        items = validate_authoritative_todo_snapshot(workflow["todoSnapshot"])
    except Exception:
        raise AgentContractError(
            "persisted workflow todo snapshot is invalid"
        ) from None
    for task_id in dropped:
        matching = [
            item for item in items
            if todo_belongs_to_goal(item.id, task_id, persisted_ids)
        ]
        if not matching or any(
            item.status not in {"completed", "cancelled"}
            for item in matching
        ):
            raise AgentContractError(
                "persisted workflow delegated goals changed"
            )


def _scoped_persist_todo_items(
    items: Any,
    delegated_task_ids: list[str],
) -> Any:
    """持久化台账按当前委托集收窄：已交付目标的 Todo 不随快照下发。

    运行时会话内的权威台账保留全量（收缩守卫、回归纠正依赖它）；Node 侧
    归属契约要求快照 ⊆ 当前委托。合法收缩在恢复/接续入口由
    _require_legitimate_goal_narrowing 先行验证，此处只保证持久化投影
    与委托声明一致。单目标委托沿用 Node 的通配语义，不做收窄。
    """
    if len(delegated_task_ids) == 1:
        return tuple(items)

    def _belongs(todo_id: str, task_id: str) -> bool:
        return (
            todo_id == task_id
            or todo_id.startswith(task_id + ":")
            or todo_id.startswith(task_id + ".")
        )

    scoped = tuple(
        item for item in items
        if any(_belongs(item.id, task_id) for task_id in delegated_task_ids)
    )
    if not scoped:
        raise AgentContractError("persist todo snapshot scoped empty")
    return scoped


_TRANSFER_DISPLAY_FACT_KEYS = frozenset({
    "fromAccountName",
    "fromAccountMaskedNo",
    "payeeName",
    "payeeMaskedNo",
    "payeeBankName",
})


def _transfer_capability_input(bindings: dict[str, Any]) -> dict[str, Any]:
    """Strip display-only facts so input passes the capability gateway inputSchema.

    capability gateway 用 AJV 校验 input，``additionalProperties: false`` 会拒绝
    展示字段。这些字段仅供 ``_batch_transfer_summary`` 渲染，不参与能力调用，
    因此在调用 ``create_action_checkpoint`` 前必须剥离。
    """

    return {
        key: value for key, value in bindings.items()
        if key not in _TRANSFER_DISPLAY_FACT_KEYS
    }


def _batch_transfer_summary(
    envelope: IntentTaskEnvelope,
    confirmation_steps: list,
) -> dict[str, Any]:
    """Build a user-visible batch summary from validated task and step facts only."""

    # 序号回退类 badcase 的第一现场：envelope 实际携带的任务族 vs 参与确认的步骤。
    logger.info(
        "Batch transfer summary envelope=%s steps=%s",
        [(item.task_id, item.intent_id) for item in envelope.tasks],
        [step.task_id for step in confirmation_steps],
    )
    tasks = {item.task_id: item for item in envelope.tasks}
    items: list[str] = []
    transfer_details: list[dict[str, Any]] = []
    total_amount_minor = 0
    for step in confirmation_steps:
        if step.capability_id != "bank.transfer.order.create":
            raise AgentContractError("batch confirmation only supports transfer orders")
        task = tasks.get(step.task_id)
        if task is None:
            raise AgentContractError("batch confirmation references unknown task")
        # 优先用 step.input_bindings 的 display facts（来自 bank.payee.list
        # 解析的脱敏卡号/银行名），回退到 task.slots 的 payee name。仅用
        # payee name 而无卡号/银行名时，用户无法在确认前核对对手方。
        payee_name = step.input_bindings.get("payeeName")
        if not isinstance(payee_name, str) or not payee_name.strip():
            payee_slot = task.slots.get("payeeName") or task.slots.get("payee")
            payee_name = payee_slot.value if payee_slot is not None else None
        payee_masked = step.input_bindings.get("payeeMaskedNo")
        payee_bank = step.input_bindings.get("payeeBankName")
        amount_minor = step.input_bindings.get("amountMinor")
        if (
            not isinstance(payee_name, str)
            or not re.fullmatch(r"[\u4e00-\u9fffA-Za-z·（）() -]{1,32}", payee_name.strip())
            or not isinstance(amount_minor, int)
            or isinstance(amount_minor, bool)
            or amount_minor <= 0
        ):
            raise AgentContractError("batch confirmation display facts are invalid")
        total_amount_minor += amount_minor
        amount = Decimal(amount_minor) / Decimal(100)
        # 单笔确认卡（本批次仅 1 步）时序号恒为 1，与卡片标题「请确认1笔
        # 转账」保持一致；多笔批量确认卡时用任务族序位（envelope 声明序中
        # 的位置），让用户在一次性确认多笔时能看到 1. 2. 3. 的批次编号。
        if len(confirmation_steps) == 1:
            sequence = 1
        else:
            sequence = [
                item.task_id
                for item in envelope.tasks
                if item.intent_id == task.intent_id
            ].index(step.task_id) + 1
        # 拼接收款人卡号/银行名（如有），让用户在确认前核对对手方
        payee_parts = [payee_name.strip()]
        payee_tail = None
        if isinstance(payee_bank, str) and payee_bank.strip():
            payee_parts.append(payee_bank.strip())
        if isinstance(payee_masked, str) and payee_masked.strip():
            tail = re.sub(r"\s", "", payee_masked)[-4:]
            if tail:
                payee_parts.append(f"尾号{tail}")
                payee_tail = tail
        payee_label = " · ".join(payee_parts)
        items.append(f"{sequence}. {payee_label}  {amount:.2f}元")
        transfer_details.append({
            "sequence": sequence,
            "payeeName": payee_name.strip(),
            "payeeBank": payee_bank.strip() if isinstance(payee_bank, str) and payee_bank.strip() else None,
            "payeeTail": payee_tail,
            "amount": f"{amount:.2f}",
            "amountMinor": amount_minor,
        })
    # 确认卡必须暴露付款账户：LLM agent 可能用 intent_default 静默选定
    # 工资卡，用户需在确认前看到付款账户尾号以核对资金来源。所有步骤
    # 共享同一付款账户时附在 description，避免每笔 items 重复。
    first_step = confirmation_steps[0]
    from_account_name = first_step.input_bindings.get("fromAccountName")
    from_account_masked = first_step.input_bindings.get("fromAccountMaskedNo")
    description = "请核对以下转账信息。"
    from_account_info = None
    if isinstance(from_account_name, str) and from_account_name.strip():
        from_parts = [from_account_name.strip()]
        from_tail = None
        if isinstance(from_account_masked, str) and from_account_masked.strip():
            from_tail = re.sub(r"\s", "", from_account_masked)[-4:]
            if from_tail:
                from_parts.append(f"尾号{from_tail}")
        description = "付款账户：" + " · ".join(from_parts) + "。请核对以下转账信息。"
        from_account_info = {
            "name": from_account_name.strip(),
            "tail": from_tail,
        }
    # 单笔确认无需合计字段：避免下游误渲染"合计：xxx元"误导用户。
    # 多笔确认的 totalAmountMinor 是本批次参与确认步骤的金额之和，
    # 由 user_response.py 在 task_count > 1 时追加合计行。
    return {
        "title": f"请确认{len(items)}笔转账",
        "description": description,
        "riskLevel": max(
            (tasks[item.task_id].risk_level for item in confirmation_steps),
            default="L2",
        ),
        "items": items,
        "taskCount": len(items),
        "totalAmountMinor": total_amount_minor if len(items) > 1 else None,
        "transferDetails": transfer_details,
        "fromAccount": from_account_info,
    }


def _gateway_safe_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Strip display-only keys before sending to checkpoint gateway.

    The gateway validates summary keys against a fixed allowlist
    (title, description, riskLevel, items, taskCount, totalAmountMinor).
    transferDetails and fromAccount are frontend rendering hints that
    must not enter the gateway payload.
    """
    return {
        k: v for k, v in summary.items()
        if k not in ("transferDetails", "fromAccount")
    }


def _graph_action_confirmation_contract(
    task: IntentTask,
    candidate: IntentCandidate,
    evaluation: SlotEvaluation,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project a ready graph action into private input and a public summary.

    The graph owns the semantic-to-provider field mapping.  This keeps workflow
    resume domain-neutral while ensuring identifiers remain only in checkpoint
    input and never cross into the user-visible confirmation summary.
    """

    contract = candidate.slot_contract
    if (
        contract is None
        or not contract.execution_binding.requires_confirmation
        or task.action not in {"DO", "CHANGE"}
    ):
        raise AgentContractError("graph action lacks a confirmation contract")
    if evaluation.readiness != "ready":
        raise AgentContractError("graph action slots are not ready")

    checkpoint_input: dict[str, Any] = {}
    display_items: list[str] = []
    for definition in contract.slots:
        resolved_value = evaluation.resolved_values.get(definition.resolved_field)
        if definition.required and resolved_value is None:
            raise AgentContractError("graph action binding is incomplete")
        if resolved_value is None:
            continue
        checkpoint_input[definition.resolved_field] = resolved_value

        if definition.type == "money":
            if (
                not isinstance(resolved_value, int)
                or isinstance(resolved_value, bool)
                or resolved_value <= 0
            ):
                raise AgentContractError("graph action amount is invalid")
            display_value = f"\u00a5{Decimal(resolved_value) / Decimal(100):,.2f}"
        else:
            semantic_value = evaluation.semantic_values.get(definition.name)
            if semantic_value is None:
                continue
            display_value = str(semantic_value).strip()
            if not display_value or len(display_value) > 100:
                raise AgentContractError("graph action display value is invalid")
        display_items.append(f"{definition.label}\uff1a{display_value}")

    if not checkpoint_input or not display_items:
        raise AgentContractError("graph action confirmation is empty")
    action_label = candidate.title.rsplit("\u00b7", 1)[-1].strip()
    if not action_label or len(action_label) > 60:
        raise AgentContractError("graph action title is invalid")
    return checkpoint_input, {
        "title": f"\u8bf7\u786e\u8ba4{action_label}",
        "description": "\u8bf7\u6838\u5bf9\u4ee5\u4e0b\u529e\u7406\u4fe1\u606f\uff0c\u786e\u8ba4\u540e\u63d0\u4ea4\u3002",
        "riskLevel": task.risk_level,
        "items": display_items,
        "taskCount": 1,
    }


def _retain_graph_execution_context(
    envelope: IntentTaskEnvelope,
    evaluations: dict[str, SlotEvaluation],
    repository: IntentGraphRepository,
    evaluator: SlotContractEvaluator,
    successful_invocations: list[dict[str, Any]],
) -> tuple[IntentTaskEnvelope, dict[str, SlotEvaluation]]:
    """Retain validated non-reference inputs used by a resumable query.

    A provider may request one more preference after already accepting defaults
    for other fields.  Those accepted inputs are workflow context, not durable
    memory and not provider identifiers; retaining them prevents the next turn
    from asking for information the service has already used.
    """

    next_tasks: list[IntentTask] = []
    next_evaluations = dict(evaluations)
    for task in envelope.tasks:
        candidate = repository.candidate_for_intent(
            task.intent_id, score=0, evidence=["execution_context"]
        )
        contract = candidate.slot_contract
        matching = [
            item for item in successful_invocations
            if item.get("capability_id")
            == (
                contract.execution_binding.capability_id
                if contract is not None else None
            )
            and isinstance(item.get("input"), dict)
        ]
        if contract is None or not matching:
            next_tasks.append(task)
            continue
        used_input = matching[-1]["input"]
        current_slots = dict(task.slots)
        for definition in contract.slots:
            # Provider identifiers are intentionally re-resolved on every
            # continuation and must never be persisted as semantic context.
            if definition.resolver_capability_id is not None:
                continue
            if any(
                key in current_slots
                for key in (definition.name, *definition.extraction_keys)
            ):
                continue
            if definition.resolved_field not in used_input:
                continue
            value = used_input[definition.resolved_field]
            if definition.type == "money":
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value <= 0
                ):
                    continue
                value = format(Decimal(value) / Decimal(100), "f")
            current_slots[definition.name] = SlotValue(
                value=value, source="context", confidence=1
            )
        current_evaluation = evaluator.evaluate(
            contract=contract,
            current_slots=current_slots,
            conversation_slots={},
        )
        next_evaluations[task.task_id] = current_evaluation
        next_tasks.append(task.model_copy(update={
            "slots": current_slots,
            "execution_readiness": current_evaluation.readiness,
        }))
    return envelope.model_copy(update={"tasks": next_tasks}), next_evaluations


def _guided_obligation_confirmation_plan(
    envelope: IntentTaskEnvelope,
    guided_plan: GuidedWorkflowPlan,
) -> CapabilityPlan:
    """Project platform-owned obligation facts into confirmation-only steps."""

    if len(envelope.tasks) != 1 or not guided_plan.todos:
        raise AgentContractError("guided obligation confirmation plan is invalid")
    task_id = envelope.tasks[0].task_id
    return CapabilityPlan.model_validate({
        "schema_version": "1.0",
        "business_agent_run_id": "bar_guided_" + uuid.uuid4().hex[:16],
        "delegation_id": envelope.delegation_id,
        "status": "planned",
        "steps": [
            {
                "step_id": f"step_obligation_{index}",
                "task_id": task_id,
                "capability_id": item.capability_id,
                "input_bindings": item.input_bindings,
                "requires_user_confirmation": True,
                "depends_on": [],
            }
            for index, item in enumerate(guided_plan.todos, 1)
        ],
    })


def _guided_obligation_summary(
    guided_plan: GuidedWorkflowPlan,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    facts = {
        item.get("id"): item
        for item in snapshot.get("items", [])
        if isinstance(item, dict)
    }
    labels = {
        "bank.credit-card.repayment.create": "信用卡还款",
        "bank.loan.repayment.create": "贷款还款",
        "bank.utility.payment.create": "生活缴费",
    }
    items: list[str] = []
    total_amount_minor = 0
    for index, todo in enumerate(guided_plan.todos, 1):
        fact = facts.get(todo.item_id, {})
        amount = fact.get("amount", {}) if isinstance(fact, dict) else {}
        amount_minor = amount.get("amountMinor") if isinstance(amount, dict) else None
        if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
            raise AgentContractError("guided obligation display facts are invalid")
        total_amount_minor += amount_minor
        display_name = fact.get("displayName") if isinstance(fact, dict) else None
        due_at = fact.get("dueAt") if isinstance(fact, dict) else None
        customer_label = (
            display_name.strip()
            if isinstance(display_name, str) and display_name.strip()
            else labels[todo.capability_id]
        )
        due_text = ""
        if isinstance(due_at, str) and due_at.strip():
            try:
                parsed_date = due_at[:10]
                due_text = f" · 到期日 {parsed_date}"
            except Exception:
                due_text = ""
        items.append(
            f"{index}. {customer_label}  ¥{Decimal(amount_minor) / Decimal(100):.2f}{due_text}"
        )
    return {
        "title": f"请确认{len(items)}项待还事项",
        "description": "请核对账单、金额和付款影响；确认后才会执行实际扣款。",
        "riskLevel": "L2",
        "items": items,
        "taskCount": len(items),
        # 单笔确认不设 totalAmountMinor，避免前端双重渲染"合计"。
        "totalAmountMinor": total_amount_minor if len(items) > 1 else None,
    }


def _general_obligation_summary(
    confirmation_steps: list,
) -> dict[str, Any]:
    """Build a confirmation summary for non-transfer obligation capabilities."""
    labels = {
        "bank.credit-card.repayment.create": "信用卡还款",
        "bank.loan.repayment.create": "贷款还款",
        "bank.utility.payment.create": "生活缴费",
    }
    items: list[str] = []
    total_amount_minor = 0
    for index, step in enumerate(confirmation_steps, 1):
        label = labels.get(step.capability_id, step.capability_id)
        amount_minor = step.input_bindings.get("amountMinor")
        if isinstance(amount_minor, int) and not isinstance(amount_minor, bool) and amount_minor > 0:
            total_amount_minor += amount_minor
            items.append(f"{index}. {label}  ¥{Decimal(amount_minor) / Decimal(100):.2f}")
        else:
            items.append(f"{index}. {label}")
    return {
        "title": f"请确认{len(items)}项待还事项",
        "description": "请核对事项和金额；确认后才会执行实际扣款。",
        "riskLevel": "L2",
        "items": items,
        "taskCount": len(items),
        # 单笔确认不设 totalAmountMinor，避免前端双重渲染"合计"。
        "totalAmountMinor": total_amount_minor if len(items) > 1 else None,
    }


def _guided_credit_card_repayment_facts(
    invocations: list[dict[str, Any]],
    trusted_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the three read results used by the guided repayment choice."""

    card_id = trusted_inputs.get("cardId")
    account_id = trusted_inputs.get("fromAccountId")
    by_capability = {
        item.get("capability_id"): item.get("output")
        for item in invocations if isinstance(item, dict)
    }
    bill = by_capability.get("bank.credit-card.bill.get")
    accounts_output = by_capability.get("bank.account.list")
    balance = by_capability.get("bank.account.balance.get")
    accounts = accounts_output.get("accounts") if isinstance(accounts_output, dict) else None
    account = next(
        (
            item for item in accounts
            if isinstance(item, dict) and item.get("id") == account_id
        ),
        None,
    ) if isinstance(accounts, list) else None
    outstanding = bill.get("outstanding") if isinstance(bill, dict) else None
    minimum = bill.get("minimumPayment") if isinstance(bill, dict) else None
    available = balance.get("available") if isinstance(balance, dict) else None
    outstanding_minor = outstanding.get("amountMinor") if isinstance(outstanding, dict) else None
    minimum_minor = minimum.get("amountMinor") if isinstance(minimum, dict) else None
    available_minor = available.get("amountMinor") if isinstance(available, dict) else None
    bill_status = bill.get("billStatus") if isinstance(bill, dict) else None
    if (
        not isinstance(bill, dict) or bill.get("cardId") != card_id
        or bill_status not in {"PAYMENT_DUE", "SETTLED"}
        or not isinstance(account, dict) or account.get("status") != "ACTIVE"
        or not isinstance(balance, dict) or balance.get("accountId") != account_id
        or not isinstance(outstanding_minor, int) or isinstance(outstanding_minor, bool)
        or not isinstance(minimum_minor, int) or isinstance(minimum_minor, bool)
        or not isinstance(available_minor, int) or isinstance(available_minor, bool)
        or available_minor < 0
    ):
        raise AgentContractError("guided credit-card repayment facts are invalid")
    if (
        bill_status == "PAYMENT_DUE"
        and (
            outstanding_minor <= 0
            or minimum_minor <= 0
            or minimum_minor > outstanding_minor
        )
    ):
        raise AgentContractError("guided credit-card repayment facts are invalid")
    if (
        bill_status == "SETTLED"
        and (outstanding_minor != 0 or minimum_minor != 0)
    ):
        raise AgentContractError("guided credit-card repayment facts are invalid")
    due_day = bill.get("dueDay")
    if not isinstance(due_day, int) or isinstance(due_day, bool) or not 1 <= due_day <= 31:
        raise AgentContractError("guided credit-card repayment due day is invalid")
    return {
        "bill_state": "settled" if bill_status == "SETTLED" else "payment_due",
        "product_name": bill.get("productName"),
        "masked_card": bill.get("maskedNo"),
        "outstanding_minor": outstanding_minor,
        "minimum_minor": minimum_minor,
        "due_label": f"每月{due_day}日",
        "account_label": f"{account.get('name')} · 尾号{str(account.get('maskedNo', ''))[-4:]}",
        "account_available_minor": available_minor,
    }


def _guided_credit_card_safe_slots(facts: Mapping[str, Any]) -> dict[str, str]:
    """Build resumable, user-visible references without persisting core IDs."""

    product_name = facts.get("product_name")
    masked_card = facts.get("masked_card")
    account_label = facts.get("account_label")
    if (
        not isinstance(product_name, str) or not product_name
        or not isinstance(masked_card, str)
        or not re.fullmatch(r"\*{2,12}\d{4}", masked_card.replace(" ", ""))
        or not isinstance(account_label, str) or not account_label
    ):
        raise AgentContractError("guided credit-card safe references are invalid")
    return {
        "card": f"{product_name} · 尾号{masked_card.replace(' ', '')[-4:]}",
        "fromAccount": account_label,
    }


def _credit_card_repayment_confirmation_summary(
    facts: Mapping[str, Any], amount_minor: int
) -> dict[str, Any]:
    """Build the exact, masked confirmation summary required by the Skill."""

    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise AgentContractError("credit-card repayment amount is invalid")
    outstanding_minor = facts.get("outstanding_minor")
    available_minor = facts.get("account_available_minor")
    if (
        not isinstance(outstanding_minor, int)
        or not isinstance(available_minor, int)
        or amount_minor > outstanding_minor
        or amount_minor > available_minor
    ):
        raise AgentContractError("credit-card repayment exceeds current facts")
    product_name = facts.get("product_name")
    masked_card = facts.get("masked_card")
    account_label = facts.get("account_label")
    if not all(isinstance(item, str) and item for item in (
        product_name, masked_card, account_label
    )):
        raise AgentContractError("credit-card repayment summary is incomplete")
    money = lambda value: f"{Decimal(value) / Decimal(100):,.2f}元"
    return {
        "title": "请确认信用卡还款",
        "description": "请核对以下信息；点击确认后才会实际扣款。",
        "riskLevel": "L2",
        "items": [
            f"信用卡：{product_name} · 尾号{masked_card.replace(' ', '')[-4:]}",
            f"还款金额：{money(amount_minor)}",
            f"付款账户：{account_label}",
            f"扣款后账户预计可用：{money(available_minor - amount_minor)}",
            f"还款后信用卡剩余未还：{money(outstanding_minor - amount_minor)}",
        ],
        "taskCount": 1,
        # 单笔确认不设 totalAmountMinor，避免前端双重渲染"合计"。
        "totalAmountMinor": None,
    }


class AgentRunner(Protocol):
    async def run(self, payload: dict[str, Any], *, session_id: str) -> dict[str, Any] | None:
        """Run one Agent and return only its trusted structured submission."""


class AgentContractError(ValueError):
    """Raised when an Agent does not satisfy the trusted output contract."""


def _elapsed_ms(started: float) -> int:
    return max(0, min(600_000, int((time.monotonic() - started) * 1000)))


def _effective_rail_decision(
    decision: RailDecision,
    mode: str,
) -> RailDecision:
    if mode != "shadow" or decision.decision == "allow":
        return decision
    return RailDecision(
        railId=decision.rail_id,
        stage=decision.stage,
        decision="allow",
        reasonCode=None,
        policyVersion=f"shadow-{decision.policy_version}"[:32],
        responseKey=None,
        durationMs=decision.duration_ms,
    )


def _memory_task_done_callback(task: asyncio.Task) -> None:
    """Log unhandled exceptions from fire-and-forget memory recording."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("Background memory recording failed", exc_info=True)


def _spawn_memory_recording(
    service,
    request: RuntimeAgentRunRequest,
    result: RuntimeAgentRunResponse,
    session_variables: dict[str, str],
) -> None:
    """Start memory recording as a background task so it does not block
    the terminal SSE event."""
    hints = [item.model_dump() for item in result.memory_candidate_hints]
    task = asyncio.create_task(
        service._record_terminal_memories(
            request, result, hints,
            session_variables=session_variables,
        )
    )
    task.add_done_callback(_memory_task_done_callback)


def _unavailable_rail_decision(stage: str) -> RailDecision:
    return RailDecision(
        railId="finance-text-safety",
        stage=stage,
        decision="unavailable",
        reasonCode="guardrail_runtime_error",
        policyVersion="unavailable",
        responseKey="security.unavailable.default",
        durationMs=0,
    )


def _strongest_rail_decision(decisions: list[RailDecision]) -> RailDecision:
    if not decisions:
        raise ValueError("at least one Rail decision is required")
    priority = {"allow": 0, "review": 1, "unavailable": 2, "block": 3}
    return max(decisions, key=lambda item: priority[item.decision])


def _user_visible_response_text(result: RuntimeAgentRunResponse) -> str:
    """Extract only displayed prose; action payloads and internal artifacts are excluded."""

    responses = [
        *([result.user_response] if result.user_response is not None else []),
        *(list(result.user_responses) if result.user_responses is not None else []),
    ]
    values: list[str] = []
    for response in responses:
        values.extend([response.title, response.body])
        values.extend(response.items)
        for action in response.actions:
            values.extend([action.label, action.description])
    return "\n".join(value for value in values if isinstance(value, str) and value)[:8000]


# 安全拦截文案目录：按策略层输出的 reason_code 映射到用户可读说明。
# 策略（finance_safety_policy）负责分类，运行时只按分类查表，避免把
# 所有 block 都笼统归为"金融安全风险"。新增分类只需在此追加一条。
_SECURITY_BLOCK_MESSAGES: dict[str, str] = {
    "unsafe_content": "检测到本次内容可能涉及违规或不当信息，暂不继续办理。请换一种方式说明您的需求，或联系人工客服。",
    "prompt_injection": "检测到本次内容可能包含指令干扰，暂不继续办理。请正常描述您的业务需求。",
    "privacy_exfiltration": "检测到本次内容可能涉及他人隐私信息获取，暂不继续办理。请仅办理与您本人相关的业务。",
    "financial_control_evasion": "检测到本次内容可能涉及规避金融监管，暂不继续办理。请换一种方式说明您的需求。",
    "fraud_or_laundering": "检测到本次内容可能存在欺诈或洗钱风险，暂不继续办理。请换一种方式说明您的需求，或联系人工客服。",
    "fraud_enablement": "检测到本次内容可能涉及协助欺诈，暂不继续办理。",
    "sensitive_output": "检测到本次内容可能涉及敏感信息，暂不继续办理。",
    "internal_instruction_leak": "检测到本次内容可能存在内部信息泄露风险，暂不继续办理。",
    "unsafe_output": "检测到本次内容可能存在安全风险，暂不继续办理。",
    "lexicon_violation": "检测到本次内容可能包含不当信息，暂不继续办理。请换一种方式说明您的需求，或联系人工客服。",
    "input_not_authorized": "当前操作未获授权，暂不继续办理。",
}
_DEFAULT_BLOCK_MESSAGE = "检测到本次内容可能存在金融安全风险，暂不继续办理。请换一种方式说明您的需求，或联系人工客服。"


def _project_conversation_state(request: RuntimeAgentRunRequest) -> ConversationState:
    """保留请求已携带的会话上下文，使安全拦截不丢失进行中的工作流。

    安全拦截只应拒绝"本次请求"，而不应清空会话。这里把请求里的
    ConversationContext 投影为响应里的 ConversationState；若本就无上下文，
    则返回 clear 状态以显式告知前端会话已重置。
    """

    context = request.conversation_context
    if context.goals:
        return ConversationState.model_validate({
            "schemaVersion": "2.0",
            "pendingEnvelopeId": context.pending_envelope_id,
            "envelopeRevision": context.envelope_revision or 1,
            "intentShape": context.intent_shape,
            "goals": [
                {
                    "taskId": goal.task_id,
                    "intentId": goal.intent_id,
                    "status": goal.status,
                    "slots": [
                        {"name": slot.name, "value": slot.value, "source": slot.source}
                        for slot in goal.slots
                    ],
                }
                for goal in context.goals
            ],
            "sharedSlots": [
                {"name": slot.name, "value": slot.value, "source": slot.source}
                for slot in context.shared_slots
            ],
            "ambiguities": [
                {"type": ambiguity.type, "taskIds": ambiguity.task_ids, "options": ambiguity.options}
                for ambiguity in context.ambiguities
            ],
            "status": "retain",
        })
    if context.pending_intent_id is not None and context.slots:
        return ConversationState.model_validate({
            "schemaVersion": "1.0",
            "pendingIntentId": context.pending_intent_id,
            "status": "retain",
            "slots": [
                {"name": slot.name, "value": slot.value, "source": slot.source}
                for slot in context.slots
            ],
        })
    return ConversationState.model_validate({
        "schemaVersion": "1.0",
        "pendingIntentId": None,
        "status": "clear",
        "slots": [],
    })


def _security_terminal_response(
    request: RuntimeAgentRunRequest,
    decision: RailDecision,
) -> RuntimeAgentRunResponse:
    if decision.decision == "block":
        status = "blocked"
        title = "请求暂未处理"
        body = _SECURITY_BLOCK_MESSAGES.get(decision.reason_code, _DEFAULT_BLOCK_MESSAGE)
    elif decision.decision == "unavailable":
        status = "unavailable"
        title = "智能服务暂时不可用"
        body = "安全检查暂时无法完成，本次请求未进入办理流程，请稍后重试。"
    elif decision.decision == "review":
        status = "clarification_required"
        title = "请补充必要信息"
        body = "为了安全地理解您的需求，请补充业务背景和希望办理的具体事项。"
    else:
        raise AgentContractError("allow is not a terminal security decision")
    return RuntimeAgentRunResponse(
        agentRunId=request.agent_run_id,
        conversationId=request.conversation_id,
        status=status,
        conversationState=(
            None if status == "unavailable"
            else _project_conversation_state(request)
        ),
        userResponse=UserResponse(
            schemaVersion="1.0",
            type="clarification" if status == "clarification_required" else "notice",
            title=title,
            body=body,
            items=(),
            actions=(),
        ),
    )


def _security_review_response(
    request: RuntimeAgentRunRequest,
    submission: SafetyClarificationSubmission,
) -> RuntimeAgentRunResponse:
    """Deliver the validated clarification produced by the User DeepAgent."""
    return RuntimeAgentRunResponse(
        agentRunId=request.agent_run_id,
        conversationId=request.conversation_id,
        status="clarification_required",
        userResponse=UserResponse(
            schemaVersion="1.0",
            type="clarification",
            title=submission.title,
            body=submission.body,
            items=(),
            actions=(),
        ),
    )


def _slim_slot_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """精简 slot_contract 字典，移除 LLM 不需要的内部映射字段。

    保留 LLM 槽位提取所需的核心字段（name/label/type/required/extractionKeys），
    移除 resolvedField/resolverCapabilityId/normalizer/defaultValue 等运行期内部字段。
    预期将每个 slot 定义从 ~200-400 字符缩减到 ~80-120 字符。
    """
    return {
        "version": contract.get("version"),
        "slots": [
            {
                "name": s.get("name"),
                "label": s.get("label"),
                "type": s.get("type"),
                "required": s.get("required"),
                "extractionKeys": s.get("extractionKeys", []),
            }
            for s in contract.get("slots", [])
            if isinstance(s, dict)
        ],
        "clarificationOrder": contract.get("clarificationOrder", []),
        "executionBinding": contract.get("executionBinding"),
    }


class DualDeepAgentService:
    def __init__(
        self,
        user_runner: AgentRunner,
        business_runner: AgentRunner,
        intent_graph: IntentGraphRepository | None = None,
        capability_invoker: AgentCapabilityInvoker | None = None,
        faq_repository: MockFaqRepository | None = None,
        input_rail: Any | None = None,
        input_rail_mode: str = "off",
        permit_ttl_seconds: int = 30,
        memory_gateway: HttpMemoryGateway | None = None,
        memory_write_gateway: MemoryWriteGateway | None = None,
        memory_policies: Any | None = None,
        memory_user_policies: Any | None = None,
    ):
        if input_rail_mode not in {"off", "shadow", "enforce"}:
            raise ValueError("input Rail mode is invalid")
        if not 5 <= permit_ttl_seconds <= 120:
            raise ValueError("permit ttl is invalid")
        self._user_runner = user_runner
        self._business_runner = business_runner
        self._capability_invoker = capability_invoker
        self._intent_graph = intent_graph or IntentGraphRepository.load(
            RuntimeSettings.from_env().intent_graph_path
        )
        self._faq_repository = faq_repository
        self._input_rail = input_rail or DisabledInputRail()
        self._input_rail_mode = input_rail_mode
        self._permit_ttl_seconds = permit_ttl_seconds
        self._memory_gateway = memory_gateway
        self._memory_write_gateway = memory_write_gateway
        self._memory_policies = memory_policies
        self._memory_user_policies = memory_user_policies
        self._slot_evaluator = SlotContractEvaluator()
        self._user_response_projector = UserResponseProjector(
            self._intent_graph, self._faq_repository
        )
        self._semantic_recall = SemanticRecallIndex(self._intent_graph)

        # V4 四层召回组件：仅在 V4 图谱时初始化
        # 词法召回(adapter.recall) 已由 V4GraphAdapter 提供
        # 子图推理 + embedding 语义召回 + LLM 裁决 为新增层
        self._v4_semantic: SemanticRecallV4 | None = None
        self._v4_subgraph: SubgraphReasoningV4 | None = None
        self._v4_judge: LLMJudgeV4 | None = None
        if isinstance(self._intent_graph, V4GraphAdapter):
            _settings = RuntimeSettings.from_env()
            _recall_cfg = _settings.recall_settings
            _tugraph_cfg = None
            if _settings.intent_graph_source == "tugraph" and _settings.tugraph_password:
                _tugraph_cfg = {
                    "base_url": _settings.tugraph_rest_url,
                    "user": _settings.tugraph_user,
                    "password": _settings.tugraph_password,
                    "database": _settings.tugraph_database,
                }
            # V4GraphAdapter 在 IntentGraphRepository.load 中创建，
            # 这里通过 apply 方法注入 recall_settings
            if hasattr(self._intent_graph, "apply_recall_settings"):
                self._intent_graph.apply_recall_settings(_recall_cfg)
            self._v4_semantic = SemanticRecallV4(
                self._intent_graph, tugraph_config=_tugraph_cfg, recall_settings=_recall_cfg
            )
            self._v4_subgraph = SubgraphReasoningV4(
                self._intent_graph, recall_settings=_recall_cfg
            )
            self._v4_judge = LLMJudgeV4(recall_settings=_recall_cfg)
            logger.info(
                "V4 四层召回组件已初始化 (semantic=%s, tugraph=%s, subgraph=%s, judge=%s)",
                self._v4_semantic.enabled,
                self._v4_semantic._tugraph_enabled,
                self._v4_subgraph.enabled,
                self._v4_judge.enabled,
            )
        self._task_continuity = FrameworkTaskContinuity(RuntimeSettings.from_env())
        # 卡死澄清升级预算（workflow_id -> 已用次数）：结构化抽取是卡死
        # 签名上的一次性模型调用，预算防止抽取失灵时无限升级。
        self._stuck_clarification_budget: dict[str, int] = {}
        self._run_timeout_seconds = RuntimeSettings.from_env().run_timeout_seconds
        self._precompleted_resume_timeout_seconds = (
            RuntimeSettings.from_env().precompleted_resume_timeout_seconds
        )
        # conversation_id -> (envelope_revision, offered payloads, target task_id)。
        # 见 _register_offered_replies：系统给出的 reply action 续接语义由
        # 构造保证，下一轮精确命中可免 LLM 裁决直接走权威续答。
        self._offered_reply_registry: dict[
            str, tuple[int, frozenset[str], str]
        ] = {}
        self._prewarm_task: asyncio.Task | None = None
        self._conversation_lock_index = asyncio.Lock()
        self._conversation_locks: weakref.WeakValueDictionary[
            str, asyncio.Lock
        ] = weakref.WeakValueDictionary()

    async def _conversation_lock(self, conversation_id: str) -> asyncio.Lock:
        async with self._conversation_lock_index:
            lock = self._conversation_locks.get(conversation_id)
            if lock is None:
                lock = asyncio.Lock()
                self._conversation_locks[conversation_id] = lock
            return lock

    @property
    def intent_graph_identity(self) -> tuple[str, str]:
        return self._intent_graph.version, self._intent_graph.digest

    def prewarm_semantic_recall(self) -> None:
        """启动预热语义索引；失败静默降级（运行时按需重建）。"""
        # V4 语义索引预热（TuGraph 模式登录缓存 token，文件模式构建内存索引）
        if self._v4_semantic is not None and self._v4_semantic.enabled:
            self._prewarm_task = asyncio.get_running_loop().create_task(
                asyncio.to_thread(self._v4_semantic.prewarm)
            )
            return
        # V2 回退
        if not self._semantic_recall._settings.enabled:
            return
        self._prewarm_task = asyncio.get_running_loop().create_task(
            self._semantic_recall.prewarm()
        )

    async def probe_intent_recall(
        self,
        message: str,
        page_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """只读召回探针（评估观测面）：返回四层召回候选投影，
        不执行任何规划或业务动作。

        V4 四层链路：词法 → 子图推理 → embedding 语义 → LLM 裁决
        - 子图/语义候选追加到尾部，不参与 top1 排序竞争
        - single_intent / non_task: 不触发 LLM，不按分数排序
        - ambiguous / multi_intent: 触发 LLM 重排序（候选内裁决，不截断）
        """
        # ── 第一层：词法召回 ──
        candidates = self._intent_graph.recall(
            message, page_context=page_context or {}
        )

        # V4 四层链路
        if self._v4_subgraph is not None:
            candidates = await self._v4_probe_pipeline(
                message, candidates, page_context
            )
        elif not candidates or candidates[0].score < _SCORE_WEAK_RECALL_THRESHOLD:
            # V2 回退：弱召回时用 V2 语义扩召
            semantic = await self._semantic_recall.recall(message)
            known = {item.intent_id for item in candidates}
            candidates = [
                *candidates,
                *(item for item in semantic if item.intent_id not in known),
            ]

        return [
            {
                "intentId": item.intent_id,
                "title": item.title,
                "action": item.action,
                "businessObject": item.business_object,
                "executionMode": item.execution_mode,
                "riskLevel": item.risk_level,
                "score": item.score,
                "evidence": list(item.evidence),
                **(
                    {"disambiguationHints": item.disambiguation_hints}
                    if item.disambiguation_hints else {}
                ),
            }
            for item in candidates
        ]

    async def _v4_probe_pipeline(
        self,
        message: str,
        lexical_candidates: list,
        page_context: dict[str, Any] | None = None,
    ) -> list:
        """V4 四层召回探针：词法 → 子图 → 语义 → LLM 裁决

        扩召回候选追加到尾部，不参与排序竞争——保持词法精确命中在 top1。
        """
        candidates = list(lexical_candidates)

        # ── 第二层：子图推理召回 ──
        # 词法强匹配(top1≥_STRONG_MATCH_THRESHOLD)时不扩召回——避免引入噪音
        if candidates and self._v4_subgraph and self._v4_subgraph.enabled:
            top1_score = max(c.score for c in candidates)
            if top1_score < _STRONG_MATCH_THRESHOLD:
                expanded = self._v4_subgraph.expand_recall(candidates)
                if expanded:
                    candidates = candidates + list(expanded)

        # ── 第三层：embedding 语义召回 ──
        # 始终执行语义召回，候选追加到尾部不参与排序竞争
        # 确保 alias contains 等弱词法匹配时语义相关意图也能进入候选
        if self._v4_semantic and self._v4_semantic.enabled:
            if not candidates:
                semantic_candidates = await asyncio.to_thread(
                    self._v4_semantic.recall, message, set()
                )
            else:
                known_ids = {c.intent_id for c in candidates}
                semantic_candidates = await asyncio.to_thread(
                    self._v4_semantic.recall, message, known_ids
                )
            if semantic_candidates:
                candidates = candidates + list(semantic_candidates)

        if not candidates:
            return []

        # ── 第四层：LLM 裁决 ──
        # non_task: 不触发 LLM，不按分数排序
        # single_intent: 弱召回(top1 < _STRONG_MATCH_THRESHOLD)或存在强竞争
        #   候选(top2 >= 60)时触发 LLM 救回——避免词法强匹配但语义偏离的 case
        # ambiguous / multi_intent: 触发 LLM 重排序（候选内裁决，不截断）
        if self._v4_judge and self._v4_judge.enabled:
            shape_assessment = analyze_intent_shape(message)
            shape = shape_assessment.shape

            should_judge = False
            if shape == "single_intent" and candidates:
                # 保留词法优先顺序——扩召回候选追加到尾部,不参与 top1 竞争
                # (sorted 会把语义/子图高分候选挤掉词法 top1,违背设计意图)
                sorted_cands = list(candidates)
                top1_score = sorted_cands[0].score if sorted_cands else 0
                top2_score = (
                    sorted_cands[1].score
                    if len(sorted_cands) > 1 else 0
                )
                should_judge = (
                    top1_score < _STRONG_MATCH_THRESHOLD
                    or top2_score >= 60
                )
            elif shape not in ("single_intent", "non_task"):
                should_judge = True
                sorted_cands = list(candidates)

            if should_judge and sorted_cands:
                judge_candidates = _select_judge_candidates(sorted_cands)
                subgraph_contexts = self._v4_subgraph.build_contexts(
                    judge_candidates
                )
                judged = await asyncio.to_thread(
                    self._v4_judge.judge,
                    message, judge_candidates,
                    shape,
                    subgraph_contexts,
                )
                if judged:
                    judged_order = {
                        item["intent_id"]: item["confidence"]
                        for item in judged
                    }
                    # 超强匹配保护:原 top1 分数 >= 145 时,
                    # LLM 重排后仍强制保留原 top1 为 top1——避免 LLM
                    # 把多关键词强匹配的 top1 错误排走(实测干扰 case
                    # V4-129/130/138 的 top1 分数 = 220/145/145)
                    original_top1 = sorted_cands[0]
                    super_strong = original_top1.score >= _SUPER_STRONG_MATCH_THRESHOLD
                    judged_candidates = []
                    remaining = []
                    for c in sorted_cands:
                        if c.intent_id in judged_order:
                            judged_candidates.append(
                                (c, judged_order[c.intent_id])
                            )
                        else:
                            remaining.append(c)
                    judged_candidates.sort(key=lambda x: -x[1])
                    candidates = (
                        [c for c, _ in judged_candidates] + remaining
                    )
                    if super_strong:
                        # 把原 top1 强制提到第一位
                        candidates = (
                            [original_top1]
                            + [c for c in candidates
                               if c.intent_id != original_top1.intent_id]
                        )

        # 限制候选数
        return candidates[:12]

    async def evaluate_intent_ladder(
        self,
        message: str,
        page_context: dict[str, Any] | None = None,
        plannable_capability_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """只读评测阶梯（评估观测面）：完整复刻生产意图识别确定性链路
        （形态判定 → FAQ → 图谱召回 → 语义拓宽 → 权威收敛），不触发
        DeepAgent 规划与业务执行。"""
        graph_version, graph_digest = self.intent_graph_identity
        capability_ids = list(dict.fromkeys(
            item for item in (plannable_capability_ids or []) if item
        ))
        # 评测端点未提供能力列表时，使用图谱中所有能力 ID，避免能力校验阻断意图召回
        if not capability_ids:
            capability_ids = list(getattr(
                self._intent_graph, "all_capability_ids", frozenset()
            ))
        request = _EvalProbeRequest(
            message=message,
            page_context=page_context or {},
            plannable_capabilities=[
                _EvalProbeCapability(id=item) for item in capability_ids
            ],
        )

        t0 = time.monotonic()
        shape_assessment = analyze_intent_shape(request.message)
        shape_ms = round((time.monotonic() - t0) * 1000, 1)

        t1 = time.monotonic()
        constraints = recall_intent_constraints(
            request, self._intent_graph, self._faq_repository, shape_assessment
        )
        recall_ms = round((time.monotonic() - t1) * 1000, 1)

        # Preserve pre-widening evidence: the widening function mutates the list.
        initial_constraints = list(constraints)
        initial_count = len(constraints)
        recall_channels = {
            "lexical": {"status": "completed", "candidates": [], "reason": "图谱阶梯返回的词法来源候选。"},
            "vector": {"status": "skipped", "candidates": [], "contributedCount": 0,
                       "reason": "词法/图谱强命中，本次未调用向量拓宽。"},
            "subgraph": {"status": "completed", "candidates": [], "reason": "图谱阶梯返回的关系扩展候选。"},
        }
        t2 = time.monotonic()
        best_score = max((item.score for item in constraints), default=0)
        widened = False
        if best_score < _SCORE_WEAK_RECALL_THRESHOLD:
            widened_constraints = await self._widen_intent_constraints(
                request, constraints, recall_channels=recall_channels
            )
            widened = len(widened_constraints) > initial_count
            constraints = widened_constraints
        widen_ms = round((time.monotonic() - t2) * 1000, 1)

        # ── P1-1：非业务输入（闲聊/寒暄/纯社交）拒识短路 ──
        # Codex P1-1：旧实现对"你好"/"在吗"等非业务输入也会返回业务 FAQ 候选，
        # 评测者会误判为"闲聊被错命中业务意图"。这里在 widen 完成后做一次拒识校验：
        #   - shape 为 non_task（analyze_intent_shape 判定为非任务输入）且最强召回分数
        #     低于 _SCORE_IDLE_CHAT_REJECT_THRESHOLD（80，词法强匹配线）→ 视为闲聊噪音，
        #     直接 idle_chat 出口，不返回业务候选/FAQ/widen/judge。
        #   - shape 仍为 single_intent 但 top1 分数低于 50（远低于强匹配线）→ 同样视为
        #     "图谱没真正命中、只是别名噪音"，归 idle_chat 但保留候选作为证据（评测者可见，
        #     不参与权威收敛）。
        idle_chat_short_circuit = False
        post_widen_top_score = max((item.score for item in constraints), default=0)
        if shape_assessment.shape == "non_task" and post_widen_top_score < _SCORE_IDLE_CHAT_REJECT_THRESHOLD:
            idle_chat_short_circuit = True
            # 清空候选：让评测页 hero 直接走 idle_chat 出口，不显示业务候选
            constraints = []
            recall_channels["lexical"]["status"] = "skipped"
            recall_channels["lexical"]["reason"] = (
                f"非任务输入（shape={shape_assessment.shape}）且最强召回分数 "
                f"{post_widen_top_score} 低于拒识阈值 {_SCORE_IDLE_CHAT_REJECT_THRESHOLD}，视为闲聊噪音拒识。"
            )
            recall_channels["lexical"]["candidates"] = []
            recall_channels["subgraph"]["status"] = "skipped"
            recall_channels["subgraph"]["reason"] = "非任务输入，子图扩召回未触发。"
            recall_channels["subgraph"]["candidates"] = []
            recall_channels["vector"]["status"] = "skipped"
            recall_channels["vector"]["reason"] = "非任务输入，向量拓宽未触发。"

        # ── 第四层：LLM 裁决 ──
        # 对齐 probe_intent_recall 的 _v4_probe_pipeline 第四层：
        # multi_intent / ambiguous 触发 LLM 重排序，single_intent 弱召回时也触发
        judge_results = None
        judge_ms = 0.0
        if (
            self._v4_judge
            and self._v4_judge.enabled
            and constraints
            and shape_assessment.shape not in ("non_task",)
            and not idle_chat_short_circuit  # P1-1：闲聊拒识短路后不触发 LLM 裁决
        ):
            t3 = time.monotonic()
            candidates_for_judge = [
                self._intent_graph.candidate_for_intent(
                    item.intent_id, score=item.score, evidence=list(item.evidence)
                )
                for item in constraints
                if item.intent_id
            ]
            candidates_for_judge = [c for c in candidates_for_judge if c is not None]
            if candidates_for_judge:
                shape = shape_assessment.shape
                sorted_cands = list(candidates_for_judge)
                should_judge = False
                if shape == "single_intent" and sorted_cands:
                    top1_score = sorted_cands[0].score if sorted_cands else 0
                    top2_score = (
                        sorted_cands[1].score
                        if len(sorted_cands) > 1 else 0
                    )
                    should_judge = (
                        top1_score < _STRONG_MATCH_THRESHOLD
                        or top2_score >= 60
                    )
                elif shape not in ("single_intent", "non_task"):
                    should_judge = True

                if should_judge:
                    judge_candidates = _select_judge_candidates(sorted_cands)
                    subgraph_contexts = (
                        self._v4_subgraph.build_contexts(judge_candidates)
                        if self._v4_subgraph and self._v4_subgraph.enabled
                        else []
                    )
                    judged = await asyncio.to_thread(
                        self._v4_judge.judge,
                        message,
                        judge_candidates,
                        shape,
                        subgraph_contexts,
                    )
                    if judged:
                        judge_results = [
                            {
                                "intentId": item["intent_id"],
                                "confidence": item["confidence"],
                            }
                            for item in judged
                        ]
            judge_ms = round((time.monotonic() - t3) * 1000, 1)

        # P1-1：闲聊拒识短路时不返回业务 FAQ 候选——避免评测者误以为"闲聊被命中业务意图"
        faq_analysis = (
            self._faq_repository.analyze(message)
            if self._faq_repository is not None and not idle_chat_short_circuit
            else None
        )
        for key, prefixes in (("lexical", ("alias", "graph_term", "graph_term_set", "page_menu", "page_tab")),
                              ("subgraph", ("graph_expand:", "subgraph:"))):
            rows = initial_constraints if key == "lexical" else constraints
            seen = set()
            for item in rows:
                if item.intent_id in seen:
                    continue
                if any(any(ev == prefix or ev.startswith(prefix) for prefix in prefixes) for ev in item.evidence):
                    seen.add(item.intent_id)
                    recall_channels[key]["candidates"].append(_project_intent_constraint(item))
            # ── P1-4：根据实际命中回填 lexical/subgraph 状态 ──
            # Codex P1-4：旧实现把 lexical/subgraph 默认标 completed，即使无候选也会显示"已执行"，
            # 评测者无法区分"通道跑了但没命中"vs"通道没跑"。这里按 candidates 数量回填 status：
            #   - 有候选 → completed（真实命中）
            #   - 无候选 + initial_count>0 → completed_unobserved（命中但未拆通道，证据前缀不在白名单）
            #   - 无候选 + initial_count=0 → skipped（图谱阶梯未召回任何候选）
            if not idle_chat_short_circuit:
                cand_count = len(recall_channels[key]["candidates"])
                if cand_count > 0:
                    recall_channels[key]["status"] = "completed"
                    recall_channels[key]["contributedCount"] = cand_count
                elif initial_count > 0 and key == "lexical":
                    recall_channels[key]["status"] = "completed_unobserved"
                    recall_channels[key]["reason"] = (
                        f"图谱阶梯返回 {initial_count} 个候选，但证据前缀不在词法白名单"
                        f"（alias/graph_term/page_menu 等）；候选已进入约束池，未拆通道展示。"
                    )
                else:
                    recall_channels[key]["status"] = "skipped"
                    recall_channels[key]["reason"] = (
                        "图谱阶梯未召回任何候选，本通道未命中。" if key == "lexical"
                        else "子图扩召回未触发或未拉入新候选（top1 可能已强匹配，不触发弱召回扩拉）。"
                    )
        return {
            "schemaVersion": "1.0",
            "recallChannels": recall_channels,
            "graphVersion": graph_version,
            "graphDigest": graph_digest,
            "shape": {
                "shape": shape_assessment.shape,
                "taskCount": shape_assessment.expected_task_count,
            },
            "constraints": [_project_intent_constraint(item) for item in constraints],
            "judgeResults": judge_results,
            # 评测观测面：LLM 裁决器运行时状态（key 是否加载成功 + 实际调用的模型名），
            # 供前端/适配器给出准确的未触发原因，而非猜测「可能 API key 未配置」
            "judgeEnabled": bool(self._v4_judge and self._v4_judge.enabled),
            "judgeModel": (self._v4_judge.model_name if self._v4_judge else ""),
            "faq": {
                "matchedIntentIds": [
                    item.entry.intent_id
                    for item in (faq_analysis.matches if faq_analysis else [])
                ],
                "fullyCovered": bool(faq_analysis.fully_covered) if faq_analysis else False,
            },
            "timings": {
                "shapeMs": shape_ms,
                "recallMs": recall_ms,
                "widenMs": widen_ms,
                "judgeMs": judge_ms,
            },
            "widened": widened,
            # P1-1：闲聊拒识短路标记——供适配器/前端识别"非业务输入"出口，
            # 不显示业务候选/FAQ hero，直接走 idle_chat 出口。
            "idleChatShortCircuit": idle_chat_short_circuit,
            "resultType": "idle_chat" if idle_chat_short_circuit else None,
        }

    async def _widen_intent_constraints(
        self,
        request: RuntimeAgentRunRequest,
        constraints: list[IntentConstraint],
        recall_channels: dict | None = None,
    ) -> list[IntentConstraint]:
        """分层召回（对齐 Harbor 意图引擎）：词法/图谱召回为主通道；弱召回时
        用向量语义召回扩充候选。语义候选 constraint 恒为 allowed（分值低于词法
        精确别名），只作为可解释证据进入候选裁决，永不改判权威意图。

        V4 升级：优先使用 V4 子图推理 + embedding 语义召回；追加到尾部不排序。
        """
        # V4 优先：子图推理召回 + embedding 语义召回
        # 语义召回始终执行——候选追加到尾部不参与排序竞争，保持词法精确命中在 top1
        # 子图扩召回受 subgraph_enabled 开关控制（默认 True，可经 AI_BANK_RECALL_SUBGRAPH_ENABLED=false 关闭）
        if self._v4_subgraph is not None and self._v4_semantic is not None:
            # 从 constraint 构建临时候选列表用于子图扩召回
            known_ids = {item.intent_id for item in constraints}
            available = {item.id for item in request.plannable_capabilities}

            # 子图扩召回：沿 confusableIntents 边拉入混淆意图（受 enabled 开关 + 弱匹配阈值双重控制）
            if self._v4_subgraph.enabled:
                lexical_candidates = [
                    self._intent_graph.candidate_for_intent(
                        item.intent_id, score=item.score, evidence=list(item.evidence)
                    )
                    for item in constraints
                    if item.intent_id
                ]
                # 子图扩召回只在弱匹配时触发
                if lexical_candidates:
                    top1_score = max(c.score for c in lexical_candidates)
                    if top1_score < _STRONG_MATCH_THRESHOLD:
                        expanded = self._v4_subgraph.expand_recall(lexical_candidates)
                        for candidate in (expanded or []):
                            if candidate.intent_id in known_ids:
                                continue
                            if candidate.execution_mode != "knowledge-only" and not set(
                                candidate.capability_bindings
                            ).issubset(available):
                                continue
                            constraints.append(_intent_constraint_from_candidate(
                                candidate,
                                required_shape="single_intent",
                                expected_task_count=1,
                                authoritative=False,
                                required=False,
                            ))
                            known_ids.add(candidate.intent_id)

            # embedding 语义召回（始终执行，不受 subgraph_enabled 影响）
            if self._v4_semantic.enabled:
                vector_trace = recall_channels["vector"] if recall_channels is not None else None
                vector_started = time.monotonic()
                if vector_trace is None:
                    semantic_candidates = await asyncio.to_thread(
                        self._v4_semantic.recall, request.message, known_ids
                    )
                else:
                    semantic_candidates = await asyncio.to_thread(
                        self._v4_semantic.recall, request.message, known_ids, diagnostics=vector_trace
                    )
                    vector_trace["timingMs"] = round((time.monotonic() - vector_started) * 1000, 1)
                before_vector_count = len(constraints)
                for candidate in (semantic_candidates or []):
                    if candidate.intent_id in known_ids:
                        continue
                    if candidate.execution_mode != "knowledge-only" and not set(
                        candidate.capability_bindings
                    ).issubset(available):
                        continue
                    constraints.append(_intent_constraint_from_candidate(
                        candidate,
                        required_shape="single_intent",
                        expected_task_count=1,
                        authoritative=False,
                        required=False,
                    ))
                    known_ids.add(candidate.intent_id)

                if recall_channels is not None:
                    recall_channels["vector"]["contributedCount"] = len(constraints) - before_vector_count
            elif recall_channels is not None:
                recall_channels["vector"].update(status="disabled", reason="向量服务未配置，本次未执行。")

            return constraints  # 不排序——追加到尾部，保持词法精确命中在 top1

        # V2 回退
        if recall_channels is not None:
            recall_channels["vector"].update(status="unobserved", reason="旧向量适配器未提供逐路执行诊断。")
        semantic = await self._semantic_recall.recall(request.message)
        if not semantic:
            return constraints
        known_ids = {item.intent_id for item in constraints}
        available = {item.id for item in request.plannable_capabilities}
        widened = list(constraints)
        for candidate in semantic:
            if candidate.intent_id in known_ids:
                continue
            if candidate.execution_mode != "knowledge-only" and not set(
                candidate.capability_bindings
            ).issubset(available):
                continue
            widened.append(_intent_constraint_from_candidate(
                candidate,
                required_shape="single_intent",
                expected_task_count=1,
                authoritative=False,
                required=False,
            ))
        return widened  # 不排序——追加到尾部，保持词法精确命中在 top1

    def _describe_continuity_goal(self, goal: Any) -> str:
        """把会话目标映射为框架 Task 的自然语言描述（意图标题+状态+已知槽位+待补槽位）。

        该描述进入 IntentRecognizer 的任务列表上下文，是框架裁决"本轮输入
        是续答还是新任务、续答补给哪个任务"的关键事实来源。待补槽位取自
        图谱声明的槽位契约（人类可读 label）——没有它，自由文本续答（如
        一个收款人姓名）在多个等待中的任务间无从归属，会被粘性活跃目标
        吞掉；描述尽力而为，取不到标题时退回 intent_id。
        """
        title = goal.intent_id
        missing_labels: list[str] = []
        try:
            candidate = self._intent_graph.candidate_for_intent(
                goal.intent_id, score=0, evidence=["continuity_seed"]
            )
            if candidate.title:
                title = candidate.title
            contract = candidate.slot_contract
            if contract is not None:
                filled = {slot.name for slot in goal.slots if slot.value}
                missing_labels = [
                    definition.label or definition.name
                    for definition in contract.slots
                    if definition.required
                    and definition.name not in filled
                    and not (set(definition.extraction_keys) & filled)
                ]
        except Exception:
            pass
        state_label = "等待用户补充信息" if goal.status == "needs_slots" else "办理中"
        slot_parts = [
            f"{slot.name}={slot.value}" for slot in goal.slots if slot.value
        ]
        slot_text = f"；已知：{'，'.join(slot_parts)}" if slot_parts else ""
        missing_text = f"；待补：{'、'.join(missing_labels)}" if missing_labels else ""
        return f"{title}（{state_label}{slot_text}{missing_text}）"

    async def _recall_task_memories(
        self,
        request: RuntimeAgentRunRequest,
        envelope: IntentTaskEnvelope,
    ) -> MemoryRecallResult:
        """Return task-bound context packs; dependency failures are non-fatal."""
        if self._memory_gateway is None:
            return MemoryRecallResult(outcome="not_applicable")
        async def _recall_one(task):
            pack = await self._memory_gateway.recall(
                tenant_id="bank",
                customer_id=request.customer_id,
                agent_domain="ai-native-bank-business",
                task_id=task.task_id,
                intent_id=task.intent_id,
                query=request.message,
            )
            return task, pack

        raw_results = await asyncio.gather(
            *[_recall_one(task) for task in envelope.tasks],
            return_exceptions=True,
        )
        for idx, result in enumerate(raw_results):
            if isinstance(result, MemoryGatewayError):
                task = envelope.tasks[idx]
                logger.warning(
                    "Memory recall unavailable task_id=%s intent_id=%s",
                    task.task_id,
                    task.intent_id,
                    exc_info=result,
                )
                return MemoryRecallResult(
                    outcome="degraded",
                    policy_version=getattr(self._memory_gateway, "task_policy_version", None),
                )
            if isinstance(result, BaseException):
                raise result
        packs: list[dict[str, Any]] = []
        for task, pack in raw_results:
            if pack is not None:
                try:
                    intent_label = self._intent_graph.candidate_for_intent(
                        task.intent_id, score=0, evidence=["memory_policy"]
                    ).title
                except IntentGraphError:
                    intent_label = "业务任务"
                packs.append({
                    "taskId": task.task_id,
                    "intentId": task.intent_id,
                    "intentLabel": intent_label,
                    "outcome": "applied" if pack["facts"] else "empty",
                    "contextPack": pack,
                    "summary": summarize_context_pack(pack),
                })
        if not packs:
            return MemoryRecallResult(
                outcome="not_applicable",
                policy_version=getattr(self._memory_gateway, "task_policy_version", None),
            )
        facts = [fact for wrapper in packs for fact in wrapper["contextPack"]["facts"]]
        summaries = [wrapper["summary"] for wrapper in packs]
        return MemoryRecallResult(
            outcome="applied" if facts else "empty",
            context_packs=tuple(packs), fact_count=len(facts),
            conflict_count=sum(item["conflictCount"] for item in summaries),
            omission_count=sum(item["omissionCount"] for item in summaries),
            requires_core_refresh_count=sum(item["requiresCoreRefreshCount"] for item in summaries),
            requires_confirmation_count=sum(item["requiresConfirmationCount"] for item in summaries),
            policy_version=getattr(self._memory_gateway, "task_policy_version", None),
        )

    async def _record_terminal_memories(
        self,
        request: RuntimeAgentRunRequest,
        result: RuntimeAgentRunResponse,
        user_memory_hints: list[dict[str, Any]] | None = None,
        session_variables: dict[str, str] | None = None,
    ) -> MemoryWriteSummary:
        """Submit bounded candidates through the platform outbox.

        Semantic memory (user preferences) and working memory (session
        variables) are recorded regardless of run status — a failed run
        may still carry valuable user-intent signals.  Episodic memory is
        only recorded for succeeded runs.
        """
        if (
            self._memory_write_gateway is None
            or self._memory_policies is None
            or result.intent_task_envelope is None
        ):
            return MemoryWriteSummary()
        candidates: list[dict[str, Any]] = []
        # Episodic memory: only for succeeded runs
        if result.status == "succeeded":
            candidates.extend(build_canonical_episode_candidates(
                envelope=result.intent_task_envelope,
                policies=self._memory_policies,
                tenant_id="bank", customer_id=request.customer_id,
                agent_domain="ai-native-bank-business", agent_run_id=request.agent_run_id,
                status=result.status, occurred_at=datetime.now(timezone.utc),
                user_message=request.message,
            ))
        # Semantic memory: always (even for failed/blocked runs)
        if self._memory_user_policies is not None and user_memory_hints:
            candidates.extend(build_user_preference_candidates(
                hints=user_memory_hints, policies=self._memory_user_policies,
                tenant_id="bank", customer_id=request.customer_id,
                agent_domain="ai-native-bank-user", agent_run_id=request.agent_run_id,
                occurred_at=datetime.now(timezone.utc),
            ))
        if not candidates:
            return MemoryWriteSummary()
        try:
            receipts = await self._memory_write_gateway.submit({
                "agentRunId": request.agent_run_id,
                "customerId": request.customer_id,
                "candidates": candidates,
            })
        except MemoryWriteGatewayError:
            logger.warning("Memory command enqueue unavailable")
            return MemoryWriteSummary.from_results(candidates)

        # 工作记忆变量写入（LTM variable store）
        if session_variables and self._memory_gateway is not None and hasattr(
            self._memory_gateway, "submit_variables"
        ):
            try:
                await self._memory_gateway.submit_variables(
                    tenant_id="bank", customer_id=request.customer_id,
                    agent_domain="ai-native-bank-user",
                    agent_run_id=request.agent_run_id,
                    variables=session_variables,
                )
            except MemoryGatewayError:
                logger.warning("Variable write unavailable")

        return MemoryWriteSummary.from_results(candidates, receipts)

    async def _record_failure_semantic_memories(
        self,
        request: RuntimeAgentRunRequest,
        envelope: IntentTaskEnvelope | None,
        user_memory_hints: list[dict[str, Any]] | None = None,
        session_variables: dict[str, str] | None = None,
    ) -> None:
        """Record semantic + working memory on hard-failure paths.

        When the run fails before reaching ``finish()`` (e.g. workflow
        persistence or response projection failure), user preferences
        expressed during the conversation are still valuable and must not
        be lost.  Episodic memory is skipped — a failed run is not a
        canonical episode.
        """
        if (
            self._memory_write_gateway is None
            or self._memory_policies is None
            or envelope is None
        ):
            return
        candidates: list[dict[str, Any]] = []
        if self._memory_user_policies is not None and user_memory_hints:
            candidates.extend(build_user_preference_candidates(
                hints=user_memory_hints, policies=self._memory_user_policies,
                tenant_id="bank", customer_id=request.customer_id,
                agent_domain="ai-native-bank-user", agent_run_id=request.agent_run_id,
                occurred_at=datetime.now(timezone.utc),
            ))
        if candidates:
            try:
                await self._memory_write_gateway.submit({
                    "agentRunId": request.agent_run_id,
                    "customerId": request.customer_id,
                    "candidates": candidates,
                })
            except MemoryWriteGatewayError:
                logger.warning("Memory command enqueue unavailable on failure path")
        if session_variables and self._memory_gateway is not None and hasattr(
            self._memory_gateway, "submit_variables"
        ):
            try:
                await self._memory_gateway.submit_variables(
                    tenant_id="bank", customer_id=request.customer_id,
                    agent_domain="ai-native-bank-user",
                    agent_run_id=request.agent_run_id,
                    variables=session_variables,
                )
            except MemoryGatewayError:
                logger.warning("Variable write unavailable on failure path")

    async def _recall_user_memories(
        self,
        request: RuntimeAgentRunRequest,
        *,
        intent_id: str | None = None,
    ) -> MemoryRecallResult:
        if self._memory_gateway is None or not hasattr(
            self._memory_gateway, "recall_user_context"
        ):
            return MemoryRecallResult(outcome="not_applicable")
        try:
            result = await self._memory_gateway.recall_user_context(
                tenant_id="bank", customer_id=request.customer_id,
                agent_domain="ai-native-bank-user", agent_run_id=request.agent_run_id,
                query=request.message,
                intent_id=intent_id,
            )
        except MemoryGatewayError:
            logger.warning("User memory recall unavailable")
            return MemoryRecallResult(outcome="degraded")
        if not isinstance(result, MemoryRecallResult):
            logger.warning("User memory recall returned invalid result")
            return MemoryRecallResult(outcome="degraded")
        return result

    async def run(self, request: RuntimeAgentRunRequest) -> RuntimeAgentRunResponse:
        result: RuntimeAgentRunResponse | None = None
        async for _event, candidate in self._execute(request):
            if isinstance(candidate, RuntimeAgentRunResponse):
                result = candidate
        if result is None:
            raise AgentContractError("agent run did not produce a final result")
        return result

    async def resume_business_workflow(
        self,
        business_workflow_id: str,
        request: BusinessWorkflowResumeRequest,
    ) -> BusinessWorkflowResumeResponse:
        if self._capability_invoker is None:
            raise AgentContractError("authorized capability invoker is unavailable")
        if request.resume_kind == "confirmation" and hasattr(
            self._input_rail, "evaluate_action"
        ):
            try:
                confirmed_decisions = [
                    await self._input_rail.evaluate_action(
                        capability_id=item.capability_id,
                        risk_level="L3",
                        summary=f"confirmed capability {item.capability_id}",
                        requires_confirmation=True,
                        confirmed=True,
                        input_decision="allow",
                    )
                    for item in request.grants
                ]
                confirmed_decision = _effective_rail_decision(
                    _strongest_rail_decision(confirmed_decisions),
                    self._input_rail_mode,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                confirmed_decision = _unavailable_rail_decision("action")
            if confirmed_decision.decision != "allow":
                raise AgentContractError("confirmed action failed safety policy")
        if (
            request.expected_workflow_revision is not None
            and request.conversation_context is not None
            and hasattr(self._business_runner, "load_todo_ledger")
        ):
            return await self._resume_native_business_workflow(
                business_workflow_id, request
            )
        grant_payload = [
            item.model_dump(by_alias=True) for item in request.grants
        ]
        session_id = business_session_id(
            request.customer_id, business_workflow_id
        )
        payload = {
            "business_workflow_id": business_workflow_id,
            "confirmation_resume": {
                "checkpoint_id": request.checkpoint_id,
                "grants": grant_payload,
            },
        }
        try:
            with self._capability_invoker.activate_authorized(
                request.agent_run_id,
                business_workflow_id,
                grant_payload,
            ) as context:
                await self._business_runner.run(
                    payload, session_id=session_id
                )
                receipts = list(context.receipts)
        except Exception as exc:
            raise AgentContractError("business workflow resume failed") from exc
        expected_grants = {item.grant_id for item in request.grants}
        actual_grants = {
            item.get("grantId") for item in receipts if isinstance(item, dict)
        }
        if actual_grants != expected_grants:
            raise AgentContractError("business workflow did not consume every grant")
        safe_receipts = []
        for item in receipts:
            status = item.get("status")
            result = item.get("result")
            safe_receipts.append(BusinessWorkflowReceipt.model_validate({
                "grantId": item.get("grantId"),
                "taskId": item.get("taskId"),
                "capabilityId": item.get("capabilityId"),
                "status": status,
                "outputKeys": sorted(result) if isinstance(result, dict) else [],
                "errorCode": item.get("errorCode"),
            }))
        succeeded = sum(item.status == "SUCCEEDED" for item in safe_receipts)
        response_status = (
            "succeeded" if succeeded == len(safe_receipts)
            else "partial_success" if succeeded else "failed"
        )
        if hasattr(self._business_runner, "todo_snapshot"):
            snapshot = self._business_runner.todo_snapshot(session_id)
            uncovered = validate_goal_coverage(
                {item.task_id for item in request.grants},
                {item.id for item in snapshot},
            )
            if uncovered:
                raise AgentContractError("resumed todo goal coverage conflict")
            if snapshot:
                await self._capability_invoker.persist_agent_workflow(
                    agent_run_id=request.agent_run_id,
                    business_workflow_id=business_workflow_id,
                    status=(
                        "completed" if response_status == "succeeded"
                        else "partially_completed" if response_status == "partial_success"
                        else "failed"
                    ),
                    delegated_task_ids=[item.task_id for item in request.grants],
                    expected_revision=0,
                    envelope_revision=1,
                    workflow_revision=1,
                    todo_revision=1,
                    todo_snapshot=[{
                        "id": item.id,
                        "status": item.status,
                        "dependsOn": list(item.depends_on),
                    } for item in snapshot],
                    todo_digest=TodoLedger.digest(tuple(snapshot)),
                    active_goal_task_id=None,
                    active_todo_id=None,
                    suspension_reason=None,
                    last_outcome={
                        "status": (
                            "completed" if response_status == "succeeded"
                            else "partially_completed" if response_status == "partial_success"
                            else "failed"
                        )
                    },
                    pending_checkpoint_ids=[],
                )
        return BusinessWorkflowResumeResponse.model_validate({
            "schemaVersion": "1.0",
            "agentRunId": request.agent_run_id,
            "businessWorkflowId": business_workflow_id,
            "status": response_status,
            "receipts": [
                item.model_dump(by_alias=True, exclude_none=True)
                for item in safe_receipts
            ],
        })

    async def _resume_native_business_workflow(
        self,
        business_workflow_id: str,
        request: BusinessWorkflowResumeRequest,
    ) -> BusinessWorkflowResumeResponse:
        assert self._capability_invoker is not None
        assert request.expected_workflow_revision is not None
        assert request.conversation_context is not None
        try:
            workflow = await self._capability_invoker.load_agent_workflow(
                agent_run_id=request.agent_run_id,
                business_workflow_id=business_workflow_id,
            )
        except Exception as exc:
            logger.warning(
                "Native business workflow restore rejected code=node_workflow_restore_invalid error_type=%s",
                _safe_restore_error_type(exc),
            )
            raise AgentContractError(
                "native business workflow restore failed"
            ) from None
        if workflow["workflowRevision"] != request.expected_workflow_revision:
            raise AgentContractError("business workflow revision changed")
        if request.resume_kind == "confirmation":
            if workflow["activeGoalTaskId"] not in {
                item.task_id for item in request.grants
            }:
                raise AgentContractError("confirmation does not belong to active goal")
        elif workflow["activeGoalTaskId"] != request.task_id:
            raise AgentContractError("clarification does not belong to active goal")
        session_id = business_session_id(request.customer_id, business_workflow_id)
        try:
            authoritative_restore, restored = _authoritative_node_todo_restore(
                workflow
            )
        except Exception as exc:
            logger.warning(
                "Native business workflow restore rejected code=node_workflow_restore_invalid error_type=%s",
                _safe_restore_error_type(exc),
            )
            raise AgentContractError(
                "native business workflow restore failed"
            ) from None
        envelope = _envelope_from_conversation_context(
            request, self._intent_graph
        )
        # 任务族序位（确认卡条目号/成功卡第N笔）依赖 resume envelope 的完整性；
        # 目标收缩会直接表现为编号回退，此日志是序列类问题的第一现场。
        logger.info(
            "Workflow resume envelope tasks=%s ids=%s kind=%s",
            len(envelope.tasks),
            [item.task_id for item in envelope.tasks],
            request.resume_kind,
        )
        resume_task = next(
            item for item in envelope.tasks
            if item.task_id == workflow["activeGoalTaskId"]
        )
        current_delegated = [
            item.task_id for item in envelope.tasks
            if is_intent_task_in_play(item.task_status)
        ]
        if any(
            task_id not in set(current_delegated)
            for task_id in (
                str(item) for item in workflow.get("delegatedTaskIds", [])
            )
        ):
            # 恢复路径与消息路径共用同一收缩守卫：只允许丢弃台账中全部
            # 终态的目标；通过后持久化投影按当前委托集收窄。
            _require_legitimate_goal_narrowing(workflow, current_delegated)
        resume_evaluations: dict[str, SlotEvaluation] = {}
        resume_decision: TaskSlotDecision | None = None
        executable_ids = set(request.executable_capability_ids)
        execution_allowlist = set(executable_ids)
        if request.resume_kind == "clarification":
            resume_view = type("ResumeSlotView", (), {
                "conversation_context": request.conversation_context,
                "message": request.clarification_message or "",
            })()
            resumed_envelope, resume_evaluations = _apply_slot_contracts(
                _single_task_envelope(envelope, resume_task),
                resume_view,
                self._intent_graph,
                self._slot_evaluator,
            )
            resumed_task = resumed_envelope.tasks[0]
            envelope = envelope.model_copy(update={
                "tasks": [
                    resumed_task if item.task_id == resumed_task.task_id else item
                    for item in envelope.tasks
                ]
            })
            resume_task = resumed_task
            candidate = self._intent_graph.candidate_for_intent(
                resume_task.intent_id,
                score=0,
                evidence=["workflow_resume_slot_runtime"],
            )
            resume_decision = decide_active_task(
                task=resume_task,
                candidate=candidate,
                evaluation=resume_evaluations[resume_task.task_id],
                intent_shape=envelope.intent_shape,
            )
            execution_allowlist = set(
                active_task_capability_ids(
                    resume_task, candidate, resume_decision
                ).intersection(executable_ids)
            )
            if (
                resume_task.intent_id == "ccb.home.repayment.repay"
                and resume_decision.readiness == "execute"
            ):
                evaluation = resume_evaluations[resume_task.task_id]
                bindings = dict(evaluation.resolved_values)
                card_id = bindings.get("cardId")
                account_id = bindings.get("fromAccountId")
                amount_minor = bindings.get("amountMinor")
                if (
                    not isinstance(card_id, str)
                    or not isinstance(account_id, str)
                    or not isinstance(amount_minor, int)
                    or isinstance(amount_minor, bool)
                    or amount_minor <= 0
                ):
                    raise AgentContractError(
                        "guided credit-card repayment selection is invalid"
                    )
                read_invocations: list[dict[str, Any]] = []
                for capability_id, capability_input in (
                    ("bank.credit-card.bill.get", {"cardId": card_id}),
                    ("bank.account.list", {}),
                    ("bank.account.balance.get", {"accountId": account_id}),
                ):
                    read_invocations = _merge_invocations(
                        read_invocations,
                        await self._invoke_read_once(
                            request.agent_run_id,
                            capability_id,
                            capability_input,
                            executable_ids,
                        ),
                    )
                facts = _guided_credit_card_repayment_facts(
                    read_invocations,
                    {"cardId": card_id, "fromAccountId": account_id},
                )
                if amount_minor > facts["outstanding_minor"]:
                    raise AgentContractError(
                        "guided credit-card repayment exceeds current bill"
                    )
                if amount_minor > facts["account_available_minor"]:
                    raise AgentContractError(
                        "guided credit-card repayment exceeds account balance"
                    )
                summary = _credit_card_repayment_confirmation_summary(
                    facts, amount_minor
                )
                with self._capability_invoker.activate(
                    request.agent_run_id, executable_ids, executable_ids
                ):
                    raw_checkpoint = await self._capability_invoker.create_action_checkpoint(
                        "bank.credit-card.repayment.create",
                        {
                            "cardId": card_id,
                            "fromAccountId": account_id,
                            "amountMinor": amount_minor,
                        },
                        summary,
                        business_workflow_id=business_workflow_id,
                        task_id=resume_task.task_id,
                    )
                confirmation = ConfirmationView.model_validate({
                    **raw_checkpoint,
                    "summary": summary,
                })
                items = tuple(restored.items)
                snapshot = TodoLedgerSnapshot(
                    revision=workflow["todoRevision"] + 1,
                    digest=TodoLedger.digest(items),
                    items=items,
                )
                outcome = BusinessLoopOutcome.model_validate({
                    "schema_version": "2.0",
                    "business_workflow_id": business_workflow_id,
                    "status": "awaiting_confirmation",
                    "covered_task_ids": [item.task_id for item in envelope.tasks],
                    "workflow_revision": workflow["workflowRevision"] + 1,
                    "todo_revision": workflow["todoRevision"] + 1,
                    "active_goal_task_id": resume_task.task_id,
                    "active_todo_id": workflow["activeTodoId"],
                    "next_action": "confirm_credit_card_repayment",
                })
                await self._persist_native_resume_workflow(
                    request, envelope, business_workflow_id, workflow, outcome,
                    snapshot, status="awaiting_confirmation",
                    pending_checkpoint_ids=[confirmation.checkpoint_id],
                )
                progress = TaskProgress(
                    mode="sequential", current=1, total=1,
                    completed=0, failed=0, skipped=0,
                )
                response = self._user_response_projector.project_confirmation(
                    confirmation, task_progress=progress
                )
                return BusinessWorkflowResumeResponse.model_validate({
                    "schemaVersion": "2.0",
                    "agentRunId": request.agent_run_id,
                    "businessWorkflowId": business_workflow_id,
                    "status": "awaiting_confirmation",
                    "receipts": [],
                    "workflowRevision": outcome.workflow_revision,
                    "activeGoalTaskId": resume_task.task_id,
                    "taskProgress": progress.model_dump(by_alias=True),
                    "userResponse": response.model_dump(
                        by_alias=True, exclude_none=True
                    ),
                    "confirmation": confirmation.model_dump(
                        by_alias=True, exclude_none=True
                    ),
                    "conversationState": _build_conversation_state(
                        "awaiting_confirmation", envelope, resume_evaluations
                    ).model_dump(by_alias=True),
                })
        grant_payload = [item.model_dump(by_alias=True) for item in request.grants]
        payload: dict[str, Any] = {
            "business_workflow_id": business_workflow_id,
            "intent_task_envelope": envelope.model_dump(),
            AUTHORITATIVE_NODE_WORKFLOW_KEY: authoritative_restore,
            "workflow_control": {
                "expected_workflow_revision": workflow["workflowRevision"],
                "next_workflow_revision": workflow["workflowRevision"] + 1,
                "expected_todo_revision": workflow["todoRevision"],
                "previous_active_goal_task_id": workflow["activeGoalTaskId"],
                "execution_policy": "sequential",
            },
        }
        if request.resume_kind == "confirmation":
            payload["confirmation_resume"] = {
                "checkpoint_id": request.checkpoint_id,
                "grants": grant_payload,
            }
        else:
            payload["clarification_resume"] = {
                "task_id": request.task_id,
                "status": "accepted",
                "message": request.clarification_message or "",
            }
            payload["slot_resolution_context"] = {
                "source_message": request.clarification_message or "",
            }
            payload["slot_assessment"] = _slot_assessment_payload(
                resume_evaluations
            )
            fit_contract = self._clarification_fit_contract(resume_task)
            if fit_contract is not None:
                payload["clarification_fit_contract"] = fit_contract
        precompleted_invocations: list[dict[str, Any]] = []
        if (
            request.resume_kind == "clarification"
            and resume_task.action == "QUERY"
            and resume_decision is not None
            and resume_decision.readiness == "resolve"
            and resume_decision.slot_type == "account_reference"
            and resume_decision.resolver_capability_id is not None
        ):
            resolver_invocations = await self._invoke_read_once(
                request.agent_run_id,
                resume_decision.resolver_capability_id,
                {},
                executable_ids,
            )
            accounts = _strict_invocation_list(
                resolver_invocations,
                resume_decision.resolver_capability_id,
                "accounts",
            )
            resolution = resolve_account_reference(
                request.clarification_message, accounts
            )
            if (
                resolution.state == "selected"
                and resume_decision.resolved_field is not None
            ):
                query_invocations = await self._invoke_read_once(
                    request.agent_run_id,
                    resume_decision.execution_capability_id,
                    {
                        resume_decision.resolved_field:
                            resolution.account_id
                    },
                    executable_ids,
                )
                precompleted_invocations = _merge_invocations(
                    resolver_invocations, query_invocations
                )
                execution_call = next(
                    item for item in precompleted_invocations
                    if item.get("capability_id")
                    == resume_decision.execution_capability_id
                )
                execution_output = execution_call.get("output", {})
                payload["authoritative_task_completion"] = {
                    "task_id": resume_task.task_id,
                    "capability_id": resume_decision.execution_capability_id,
                    "result_state": "succeeded",
                    "output_keys": sorted(execution_output),
                }
                payload["authoritative_completion_policy"] = (
                    "advance_then_suspend"
                )
                execution_allowlist = set()
        payload["runtime_capability_allowlist"] = sorted(execution_allowlist)
        submission: Any = None
        runner_error: Exception | None = None
        receipts: list[dict[str, Any]] = []
        try:
            with self._capability_invoker.activate(
                request.agent_run_id, execution_allowlist, execution_allowlist
            ) as read_context:
                if request.resume_kind == "confirmation":
                    with self._capability_invoker.activate_authorized(
                        request.agent_run_id, business_workflow_id, grant_payload
                    ) as authorized_context:
                        try:
                            submission = await self._business_runner.run(
                                payload, session_id=session_id
                            )
                        except Exception as exc:
                            runner_error = exc
                        finally:
                            # A confirmed grant is a platform-owned execution
                            # instruction. DeepAgent may consume it through its
                            # tool, but confirmed execution must not depend on
                            # the model producing another valid turn. Dispatch
                            # only omitted grants; the Node gateway still owns
                            # one-shot authorization, immutable input and
                            # idempotency. The resulting receipt is then used to
                            # reconcile the Todo ledger and workflow outcome.
                            for grant in request.grants:
                                if (
                                    grant.grant_id
                                    not in authorized_context.consumed_grant_ids
                                ):
                                    await self._capability_invoker.invoke_authorized_capability(
                                        grant.grant_id, grant.task_id
                                    )
                            # A capability receipt is an authoritative execution
                            # fact and must survive a later model/output failure.
                            receipts = list(authorized_context.receipts)
                else:
                    # 机械轮：澄清应答已被声明契约完全消解时，终局提交由平台
                    # 从权威状态合成，不再唤醒业务智能体（省 3-6 次模型往返，
                    # 中转链单次调用 p50 即 5-8s）。两类机械形态：
                    # a) 代查回执已落地（account_reference 澄清）——submission
                    #    置空，走下方回执权威收编分支（已在生产验证）结算台账
                    #    并推进指针；
                    # b) 活动任务全部槽位就绪待确认（readiness=execute 且为
                    #    转账适配域）——合成占位 awaiting_clarification，下游
                    #    按活动任务 readiness 改写为确认卡（与授权恢复轮的
                    #    占位改写同一机制）。
                    mechanical_transfer_execute = (
                        resume_decision.readiness == "execute"
                        and uses_transfer_compatibility_adapter(
                            self._intent_graph, resume_task
                        )
                    )
                    if precompleted_invocations or mechanical_transfer_execute:
                        if precompleted_invocations:
                            submission = None
                        else:
                            delegated_ids = tuple(
                                item.task_id for item in envelope.tasks
                            )
                            active_todo_id = next(
                                (
                                    item.id for item in restored.items
                                    if item.status not in {"completed", "cancelled"}
                                    and todo_belongs_to_goal(
                                        item.id,
                                        resume_task.task_id,
                                        delegated_ids,
                                    )
                                ),
                                resume_task.task_id,
                            )
                            submission = {
                                "schema_version": "2.0",
                                "business_workflow_id": business_workflow_id,
                                "status": "awaiting_clarification",
                                "covered_task_ids": [
                                    item.task_id for item in envelope.tasks
                                ],
                                "workflow_revision": (
                                    workflow["workflowRevision"] + 1
                                ),
                                "todo_revision": workflow["todoRevision"] + 1,
                                "active_goal_task_id": resume_task.task_id,
                                "active_todo_id": active_todo_id,
                                "next_action": "collect_required_slots",
                            }
                    else:
                        run_call = self._business_runner.run(
                            payload, session_id=session_id
                        )
                        submission = await run_call
                successful_invocations = _merge_invocations(
                    precompleted_invocations,
                    list(read_context.successful_invocations),
                )
        except Exception as exc:
            logger.warning(
                "Native business workflow resume rejected code=native_workflow_resume_failed error_type=%s",
                _safe_restore_error_type(exc),
            )
            raise AgentContractError(
                "native business workflow resume failed"
            ) from None
        expected_grants = {item.grant_id for item in request.grants}
        if (
            request.resume_kind == "confirmation"
            and {item.get("grantId") for item in receipts} != expected_grants
        ):
            raise AgentContractError("business workflow did not consume every grant")
        safe_receipts = [BusinessWorkflowReceipt.model_validate({
            "grantId": item.get("grantId"),
            "taskId": item.get("taskId"),
            "capabilityId": item.get("capabilityId"),
            "status": item.get("status"),
            "outputKeys": sorted(item.get("result", {}))
            if isinstance(item.get("result"), dict) else [],
            "errorCode": item.get("errorCode"),
        }) for item in receipts]
        grant_refs = {item.grant_id: item for item in request.grants}
        receipts_match_grants = all(
            item.status == "SUCCEEDED"
            and item.task_id == grant_refs[item.grant_id].task_id
            and item.capability_id == grant_refs[item.grant_id].capability_id
            for item in safe_receipts
        )
        outcome_error: Exception | None = runner_error
        outcome: BusinessLoopOutcome | None = None
        if outcome_error is None:
            try:
                outcome = BusinessLoopOutcome.model_validate(
                    normalize_terminal_loop_submission(submission)
                )
            except Exception as exc:
                # precompleted_invocations 路径有意将 submission 置空，
                # 下游 reconcile（line ~2653）从权威回执重建 outcome。
                # 不记为 warning，避免误导排障方向。
                if not precompleted_invocations:
                    logger.warning(
                        "BusinessLoopOutcome validation failed: %s | submission=%s",
                        exc, submission,
                    )
                outcome_error = exc
        if outcome is not None and outcome.business_workflow_id != business_workflow_id:
            raise AgentContractError("native business workflow identity conflict")
        # Workflow and Todo revisions are platform-owned CAS clocks. The model
        # describes progress, but cannot choose or replay their persisted values.
        if outcome is not None:
            outcome = outcome.model_copy(update={
                "workflow_revision": workflow["workflowRevision"] + 1,
                "todo_revision": workflow["todoRevision"] + 1,
            })
        # The agent frequently invents task IDs that don't match the envelope's
        # delegated task IDs. Discard the outcome so the reconciliation paths
        # (which derive active_goal_task_id from the authoritative todo ledger)
        # can take over. Without this, persistence crashes at the delegated_task_ids
        # membership check in node_gateway.persist_agent_workflow.
        if outcome is not None:
            envelope_task_ids = {item.task_id for item in envelope.tasks}
            if (
                outcome.active_goal_task_id is not None
                and outcome.active_goal_task_id not in envelope_task_ids
            ):
                logger.warning(
                    "Discarding agent outcome: active_goal_task_id=%s not in "
                    "envelope task_ids=%s",
                    outcome.active_goal_task_id,
                    sorted(envelope_task_ids),
                )
                outcome = None
                outcome_error = AgentContractError(
                    "active goal task id not in envelope"
                )
        graph_read_progress_is_reconcilable = False
        if (
            outcome is None
            and request.resume_kind == "clarification"
            and not precompleted_invocations
            and successful_invocations
            and not receipts
        ):
            resume_candidate = self._intent_graph.candidate_for_intent(
                resume_task.intent_id,
                score=0,
                evidence=["workflow_resume_reconciliation"],
            )
            graph_bound_ids = set(resume_candidate.capability_bindings)
            confirmation_capability_id = (
                resume_candidate.slot_contract.execution_binding.capability_id
                if resume_candidate.slot_contract is not None
                and resume_candidate.slot_contract.execution_binding.requires_confirmation
                else None
            )
            invoked_ids = {
                item.get("capability_id") for item in successful_invocations
            }
            graph_read_progress_is_reconcilable = (
                None not in invoked_ids
                and invoked_ids.issubset(graph_bound_ids)
                and invoked_ids.issubset(executable_ids)
                and confirmation_capability_id not in invoked_ids
            )
        if (
            outcome is None
            and not precompleted_invocations
            and (
                request.resume_kind != "confirmation"
                or not receipts_match_grants
            )
            and not graph_read_progress_is_reconcilable
            and runner_error is not None
        ):
            logger.warning(
                "Native workflow resume gate closed runner_error=%r reconcilable=%s invoked=%s",
                runner_error,
                graph_read_progress_is_reconcilable,
                [item.get("capability_id") for item in successful_invocations],
            )
            raise AgentContractError("native business workflow resume failed") from outcome_error

        snapshot = self._business_runner.load_todo_ledger(
            session_id, revision=workflow["todoRevision"] + 1
        )
        snapshot = _correct_todo_regression(restored, snapshot)
        if receipts_match_grants:
            # 授权回执是不可变执行事实：台账以恢复的权威快照为基准确定性
            # 重放（回执任务标记完成），模型本轮对台账的重建/改名不参与
            # 判定，避免 id 集错位把已成功的执行整体作废。
            snapshot = _todo_snapshot_after_receipts(
                envelope, restored, safe_receipts, workflow["todoRevision"] + 1
            )
        if validate_goal_coverage(
            (item.task_id for item in envelope.tasks),
            (item.id for item in snapshot.items),
        ):
            # 覆盖冲突的根因是两侧 id 集错位（信封重建 vs todo 台账），
            # 不记集合无法定位；观察日志先行，修复方向取决于真实集合差异。
            logger.warning(
                "Native workflow todo coverage conflict envelope_tasks=%s ledger_ids=%s ledger_revision=%s",
                [item.task_id for item in envelope.tasks],
                [item.id for item in snapshot.items],
                workflow["todoRevision"],
            )
            raise AgentContractError("native business workflow todo coverage conflict")
        goals = goal_states_from_todos(envelope, snapshot.items)
        if request.resume_kind == "confirmation" and receipts_match_grants:
            # 授权恢复轮的进度指针由权威台账推导：活动目标 = 首个未完成
            # 目标，全部完成则终态。模型在授权执行后提交的 outcome 不参与
            # 判定（其台账视图可能与权威台账错位，指针不可信）。占位状态
            # awaiting_clarification 由下游按活动任务 readiness 改写为
            # 确认卡或澄清卡。
            selected_after_grants = select_active_goal(
                goals, workflow["activeGoalTaskId"]
            )
            next_todo_id = None
            if selected_after_grants is not None:
                next_todo = next(
                    (
                        item for item in snapshot.items
                        if item.status not in {"completed", "cancelled"}
                        and todo_belongs_to_goal(
                            item.id,
                            selected_after_grants.task_id,
                            (task.task_id for task in envelope.tasks),
                        )
                    ),
                    None,
                )
                next_todo_id = (
                    next_todo.id
                    if next_todo is not None
                    else selected_after_grants.task_id
                )
            outcome = BusinessLoopOutcome.model_validate({
                "schema_version": "2.0",
                "business_workflow_id": business_workflow_id,
                "status": (
                    "completed"
                    if selected_after_grants is None
                    else "awaiting_clarification"
                ),
                "covered_task_ids": [item.task_id for item in envelope.tasks],
                "workflow_revision": workflow["workflowRevision"] + 1,
                "todo_revision": workflow["todoRevision"] + 1,
                "active_goal_task_id": (
                    selected_after_grants.task_id
                    if selected_after_grants is not None else None
                ),
                "active_todo_id": next_todo_id,
                "next_action": (
                    None
                    if selected_after_grants is None
                    else "collect_required_slots"
                ),
            })
        if (
            outcome is not None
            and outcome.status == "replan_required"
            and request.resume_kind == "clarification"
            and outcome.active_goal_task_id == resume_task.task_id
        ):
            adopted_response = await self._adopt_replan_outcome(
                request, envelope, business_workflow_id, workflow,
                outcome, resume_task, goals, session_id,
            )
            if adopted_response is not None:
                return adopted_response
            # 提案不可采纳（跨意图/图谱校验失败）：降级为普通澄清续跑，
            # 控制流落入下方既有 awaiting_clarification 机制。
            outcome = outcome.model_copy(update={
                "status": "awaiting_clarification",
                "proposed_tasks": [],
            })
        if outcome is None:
            if precompleted_invocations:
                # 代查回执是不可变执行事实：本轮恢复任务的查询已确定性
                # 成功，模型终局提交损坏与否不改变该事实。台账按回执
                # 结算后接管推进，而不是要求"台账已完成"——台账推进本
                # 依赖 outcome，以旧台账状态否决权威回执是循环死锁。
                execution_receipt = next(
                    (
                        item for item in precompleted_invocations
                        if item.get("capability_id")
                        == resume_decision.execution_capability_id
                    ),
                    None,
                )
                if execution_receipt is None:
                    logger.warning(
                        "Native workflow resume completion reconcile failed "
                        "resume_task=%s goals=%s outcome_error=%r",
                        resume_task.task_id,
                        [(item.task_id, item.status) for item in goals],
                        outcome_error,
                    )
                    raise AgentContractError(
                        "native business workflow resume failed"
                    ) from outcome_error
                delegated_ids = tuple(item.task_id for item in envelope.tasks)
                settled_items = tuple(
                    item.model_copy(update={"status": "completed"})
                    if item.status not in {"completed", "cancelled"}
                    and todo_belongs_to_goal(
                        item.id, resume_task.task_id, delegated_ids
                    )
                    else item
                    for item in snapshot.items
                )
                snapshot = TodoLedgerSnapshot(
                    revision=snapshot.revision,
                    digest=TodoLedger.digest(settled_items),
                    items=settled_items,
                )
                goals = goal_states_from_todos(envelope, snapshot.items)
                selected_after_completion = select_active_goal(
                    goals, workflow["activeGoalTaskId"]
                )
                selected_after_completion_id = (
                    selected_after_completion.task_id
                    if selected_after_completion is not None else None
                )
                logger.warning(
                    "Reconciled query continuation from authoritative result "
                    "and Todo ledger error_type=%s",
                    type(outcome_error).__name__,
                )
                outcome = BusinessLoopOutcome.model_validate({
                    "schema_version": "2.0",
                    "business_workflow_id": business_workflow_id,
                    "status": (
                        "completed"
                        if selected_after_completion_id is None
                        else "awaiting_clarification"
                    ),
                    "covered_task_ids": [
                        item.task_id for item in envelope.tasks
                    ],
                    "workflow_revision": workflow["workflowRevision"] + 1,
                    "todo_revision": workflow["todoRevision"] + 1,
                    "active_goal_task_id": selected_after_completion_id,
                    "active_todo_id": selected_after_completion_id,
                    "next_action": (
                        None
                        if selected_after_completion_id is None
                        else "collect_required_slots"
                    ),
                })
            elif graph_read_progress_is_reconcilable:
                selected_after_read = select_active_goal(
                    goals, workflow["activeGoalTaskId"]
                )
                if (
                    selected_after_read is None
                    or selected_after_read.task_id != resume_task.task_id
                    or selected_after_read.status in {"succeeded", "cancelled"}
                ):
                    raise AgentContractError(
                        "native business workflow resume failed"
                    ) from outcome_error
                logger.warning(
                    "Reconciled clarification from graph-bound read results "
                    "and Todo ledger error_type=%s",
                    type(outcome_error).__name__,
                )
                outcome = BusinessLoopOutcome.model_validate({
                    "schema_version": "2.0",
                    "business_workflow_id": business_workflow_id,
                    "status": "awaiting_clarification",
                    "covered_task_ids": [
                        item.task_id for item in envelope.tasks
                    ],
                    "workflow_revision": workflow["workflowRevision"] + 1,
                    "todo_revision": workflow["todoRevision"] + 1,
                    "active_goal_task_id": resume_task.task_id,
                    "active_todo_id": workflow["activeTodoId"],
                    "next_action": "collect_required_slots",
                })
            elif runner_error is None:
                selected_after_agent_run = select_active_goal(
                    goals, workflow["activeGoalTaskId"]
                )
                selected_after_agent_run_id = (
                    selected_after_agent_run.task_id
                    if selected_after_agent_run is not None else None
                )
                logger.warning(
                    "Reconciled native workflow from Todo ledger "
                    "(agent completed without submission) error_type=%s",
                    type(outcome_error).__name__,
                )
                outcome = BusinessLoopOutcome.model_validate({
                    "schema_version": "2.0",
                    "business_workflow_id": business_workflow_id,
                    "status": (
                        "completed"
                        if selected_after_agent_run_id is None
                        else "awaiting_clarification"
                    ),
                    "covered_task_ids": [
                        item.task_id for item in envelope.tasks
                    ],
                    "workflow_revision": workflow["workflowRevision"] + 1,
                    "todo_revision": workflow["todoRevision"] + 1,
                    "active_goal_task_id": selected_after_agent_run_id,
                    "active_todo_id": selected_after_agent_run_id,
                    "next_action": (
                        None
                        if selected_after_agent_run_id is None
                        else "collect_required_slots"
                    ),
                })
            else:
                granted_task_ids = {item.task_id for item in request.grants}
                ungranted_work_was_incomplete = any(
                    item.status not in {"completed", "cancelled"}
                    and not any(
                        todo_belongs_to_goal(
                            item.id,
                            task_id,
                            (task.task_id for task in envelope.tasks),
                        )
                        for task_id in granted_task_ids
                    )
                    for item in restored.items
                )
                if ungranted_work_was_incomplete or any(
                    item.status not in {"succeeded", "cancelled"}
                    for item in goals
                ):
                    raise AgentContractError(
                        "native business workflow resume failed"
                    ) from outcome_error
                logger.warning(
                    "Reconciled terminal native workflow from authoritative "
                    "receipt and Todo ledger error_type=%s",
                    type(outcome_error).__name__,
                )
                outcome = BusinessLoopOutcome.model_validate({
                    "schema_version": "2.0",
                    "business_workflow_id": business_workflow_id,
                    "status": "completed",
                    "covered_task_ids": [
                        item.task_id for item in envelope.tasks
                    ],
                    "workflow_revision": workflow["workflowRevision"] + 1,
                    "todo_revision": workflow["todoRevision"] + 1,
                    "active_goal_task_id": None,
                    "active_todo_id": None,
                    "next_action": None,
                })
        completed_resume_response = None
        resume_goal = next(
            (item for item in goals if item.task_id == resume_task.task_id),
            None,
        )
        if (
            request.resume_kind == "clarification"
            and resume_task.action == "QUERY"
            and resume_decision is not None
            and resume_decision.readiness == "resolve"
            and resume_decision.slot_type == "account_reference"
            and resume_decision.resolver_capability_id is not None
        ):
            task_invocations = list(successful_invocations)
            try:
                if not any(
                    item.get("capability_id")
                    == resume_decision.resolver_capability_id
                    for item in task_invocations
                ):
                    resolver_invocations = await self._invoke_read_once(
                        request.agent_run_id,
                        resume_decision.resolver_capability_id,
                        {},
                        executable_ids,
                    )
                    task_invocations = _merge_invocations(
                        task_invocations, resolver_invocations
                    )
                accounts = _strict_invocation_list(
                    task_invocations,
                    resume_decision.resolver_capability_id,
                    "accounts",
                )
                resolution = resolve_account_reference(
                    request.clarification_message, accounts
                )
                if resolution.state != "selected":
                    # 用户尚未选定账户（ambiguous/empty）是正常业务态：
                    # 多任务续跑的第一轮交付结果卡片，用户点"继续"后
                    # 到这里，应回账户选择澄清而不是 422。工作流保持
                    # awaiting_clarification，槽位评估与 Todo 不变。
                    clarification_response = (
                        self._user_response_projector.project_balance(
                            resolution
                        )
                    )
                    progress = TaskProgress.model_validate(
                        sequential_progress(goals, resume_task.task_id)
                    )
                    if clarification_response.type == "clarification":
                        await self._persist_native_resume_workflow(
                            request, envelope, business_workflow_id, workflow,
                            outcome, snapshot,
                            status="awaiting_clarification",
                            pending_checkpoint_ids=[],
                        )
                        return BusinessWorkflowResumeResponse.model_validate({
                            "schemaVersion": "2.0",
                            "agentRunId": request.agent_run_id,
                            "businessWorkflowId": business_workflow_id,
                            "status": "clarification_required",
                            "receipts": [],
                            "workflowRevision": outcome.workflow_revision,
                            "activeGoalTaskId": resume_task.task_id,
                            "taskProgress": progress.model_dump(by_alias=True),
                            "userResponse": clarification_response.model_dump(
                                by_alias=True, exclude_none=True
                            ),
                            "conversationState": _build_conversation_state(
                                "clarification_required", envelope,
                                resume_evaluations,
                            ).model_dump(by_alias=True),
                        })
                    # empty/degraded：没有可安全生成的账户选项，任务
                    # 无法继续，按失败交付 notice（网关 resume 契约要求
                    # clarification_required 必须配 clarification 类型）。
                    await self._persist_native_resume_workflow(
                        request, envelope, business_workflow_id, workflow,
                        outcome, snapshot,
                        status="failed",
                        pending_checkpoint_ids=[],
                    )
                    return BusinessWorkflowResumeResponse.model_validate({
                        "schemaVersion": "2.0",
                        "agentRunId": request.agent_run_id,
                        "businessWorkflowId": business_workflow_id,
                        "status": "failed",
                        "receipts": [],
                        "workflowRevision": outcome.workflow_revision,
                        "activeGoalTaskId": None,
                        "taskProgress": progress.model_dump(by_alias=True),
                        "userResponse": clarification_response.model_dump(
                            by_alias=True, exclude_none=True
                        ),
                        "conversationState": _build_conversation_state(
                            "failed", envelope, resume_evaluations,
                        ).model_dump(by_alias=True),
                    })
                if resume_decision.resolved_field is None:
                    raise AccountReferenceResolutionError(
                        "resumed query reference is unresolved"
                    )
                if not any(
                    item.get("capability_id")
                    == resume_decision.execution_capability_id
                    for item in task_invocations
                ):
                    query_invocations = await self._invoke_read_once(
                        request.agent_run_id,
                        resume_decision.execution_capability_id,
                        {
                            resume_decision.resolved_field:
                                resolution.account_id
                        },
                        executable_ids,
                    )
                    task_invocations = _merge_invocations(
                        task_invocations, query_invocations
                    )
                execution_calls = [
                    item for item in task_invocations
                    if item.get("capability_id")
                    == resume_decision.execution_capability_id
                ]
                if len(execution_calls) != 1:
                    raise AccountReferenceResolutionError(
                        "resumed query execution is missing"
                    )
                completed_result = project_user_result(execution_calls)
                if completed_result is None:
                    raise AccountReferenceResolutionError(
                        "resumed query result is unavailable"
                    )
                completed_resume_response = (
                    self._user_response_projector.project_legacy_result(
                        completed_result
                    )
                )
            except Exception as exc:
                raise AgentContractError(
                    "resumed query did not produce a trusted result"
                ) from exc
            if resume_goal is None or resume_goal.status != "succeeded":
                if (
                    outcome.active_goal_task_id != resume_task.task_id
                    and (
                        resume_goal is None
                        or resume_goal.status not in {"ready", "in_progress"}
                    )
                ):
                    # 多任务续跑时模型可能把执行焦点推进到后续任务，台账里
                    # 查询任务仍处活跃态。上方确定性分支已拿到可信查询结果
                    # ——这正是该分支存在的修复场景；若还要求漫游 outcome
                    # 预先认可修复，修复就只在单任务（无需修复）时可达。
                    # 权威执行事实优先于模型记账（与本文件既有 reconcile
                    # 同一原则），放行续跑；续跑后的台账 CAS 与一致性校验
                    # 仍然兜底。
                    raise AgentContractError(
                        "resumed query todo did not reach a reconcilable state"
                    )
                execution_call = next(
                    item for item in task_invocations
                    if item.get("capability_id")
                    == resume_decision.execution_capability_id
                )
                execution_output = execution_call.get("output", {})
                continuation_payload = {
                    "business_workflow_id": business_workflow_id,
                    "loop_iteration": 2,
                    "intent_task_envelope": envelope.model_dump(),
                    "loop_feedback": {
                        "previous_status": outcome.status,
                        "previous_next_action": outcome.next_action,
                        "active_goal_task_id": resume_task.task_id,
                        "active_todo_id": outcome.active_todo_id,
                        "todo_revision": outcome.todo_revision,
                        "todo_digest": snapshot.digest,
                    },
                    "authoritative_task_completion": {
                        "task_id": resume_task.task_id,
                        "capability_id": resume_decision.execution_capability_id,
                        "result_state": "succeeded",
                        "output_keys": sorted(execution_output),
                    },
                    "workflow_control": {
                        "expected_workflow_revision": workflow[
                            "workflowRevision"
                        ],
                        "next_workflow_revision": workflow[
                            "workflowRevision"
                        ] + 1,
                        "expected_todo_revision": outcome.todo_revision,
                        "previous_active_goal_task_id": resume_task.task_id,
                        "execution_policy": "sequential",
                    },
                }
                try:
                    with self._capability_invoker.activate(
                        request.agent_run_id, set(), set()
                    ):
                        continuation_submission = await self._business_runner.run(
                            continuation_payload, session_id=session_id
                        )
                    outcome = BusinessLoopOutcome.model_validate(
                        normalize_terminal_loop_submission(
                            continuation_submission
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "Native business query continuation rejected "
                        "code=native_query_continuation_failed error_type=%s",
                        _safe_restore_error_type(exc),
                    )
                    raise AgentContractError(
                        "native business query continuation failed"
                    ) from None
                if outcome.business_workflow_id != business_workflow_id:
                    raise AgentContractError(
                        "native business workflow identity conflict"
                    )
                outcome = outcome.model_copy(update={
                    "workflow_revision": workflow["workflowRevision"] + 1,
                })
                snapshot = self._business_runner.load_todo_ledger(
                    session_id, revision=outcome.todo_revision
                )
                snapshot = _correct_todo_regression(restored, snapshot)
                if validate_goal_coverage(
                    (item.task_id for item in envelope.tasks),
                    (item.id for item in snapshot.items),
                ):
                    raise AgentContractError(
                        "native business workflow todo coverage conflict"
                    )
                goals = goal_states_from_todos(envelope, snapshot.items)
                resume_goal = next(
                    (
                        item for item in goals
                        if item.task_id == resume_task.task_id
                    ),
                    None,
                )
                if resume_goal is None or resume_goal.status != "succeeded":
                    raise AgentContractError(
                        "native business query continuation did not complete todo"
                    )
        if (
            outcome.workflow_revision != workflow["workflowRevision"] + 1
            or outcome.todo_revision <= workflow["todoRevision"]
        ):
            raise AgentContractError("native business workflow outcome conflict")
        outcome = outcome.model_copy(update={
            "covered_task_ids": [item.task_id for item in envelope.tasks]
        })
        selected = select_active_goal(goals, workflow["activeGoalTaskId"])
        selected_id = selected.task_id if selected is not None else None
        if outcome.active_goal_task_id != selected_id:
            raise AgentContractError("native business workflow active goal conflict")
        completed_user_result: UserFacingResult | None = None
        if (
            outcome.status == "completed"
            and resume_task.action == "QUERY"
            and successful_invocations
        ):
            # A completed query resume must still hand the user the trusted
            # projection of what was queried. The DeepAgent's own final text is
            # not transportable, so project from the authoritative invocation
            # output, exactly like the account-reference completion above. The
            # rich projection (item cards) travels as userResult while the
            # legacy text projection keeps the session turn readable.
            completed_result = project_user_result(
                list(successful_invocations)
            )
            if completed_result is not None:
                completed_user_result = completed_result
                if completed_resume_response is None:
                    completed_resume_response = (
                        self._user_response_projector.project_legacy_result(
                            completed_result
                        )
                    )
        if outcome.status == "completed":
            await self._persist_native_resume_workflow(
                request, envelope, business_workflow_id, workflow, outcome,
                snapshot, status="completed", pending_checkpoint_ids=[],
            )
            return BusinessWorkflowResumeResponse.model_validate({
                "schemaVersion": "2.0", "agentRunId": request.agent_run_id,
                "businessWorkflowId": business_workflow_id, "status": "succeeded",
                "receipts": [item.model_dump(by_alias=True) for item in safe_receipts],
                "workflowRevision": outcome.workflow_revision,
                "userResponse": (
                    completed_resume_response.model_dump(
                        by_alias=True, exclude_none=True
                    )
                    if completed_resume_response else None
                ),
                "userResult": (
                    completed_user_result.model_dump(by_alias=True, exclude_none=True)
                    if completed_user_result else None
                ),
                "conversationState": _build_conversation_state(
                    "succeeded", envelope, {}
                ).model_dump(by_alias=True),
            })

        pointer_id = outcome.active_goal_task_id
        if pointer_id is None or pointer_id not in {
            item.task_id for item in envelope.tasks
        }:
            # 终局提交的活动指针是不可信回显（normalize 已清洗越界指针）。
            # 权威指针从已验证台账推导；台账无活动目标时本轮恢复任务即
            # 活动任务。
            authoritative_active = select_active_goal(
                goals, workflow["activeGoalTaskId"]
            )
            pointer_id = (
                authoritative_active.task_id
                if authoritative_active is not None
                else resume_task.task_id
            )
        active_task = next(
            item for item in envelope.tasks if item.task_id == pointer_id
        )
        resume_view = type("ResumeView", (), {
            "conversation_context": request.conversation_context,
            "message": request.clarification_message or "",
        })()
        envelope, evaluations = _apply_slot_contracts(
            envelope, resume_view, self._intent_graph, self._slot_evaluator
        )
        active_task = next(
            item for item in envelope.tasks if item.task_id == active_task.task_id
        )
        # --- fromAccount 同族传播（envelope 级）---
        # 批量转账中付款账户是用户对整批操作的一次性选择。envelope 从
        # conversation_context 重建，如果前一轮 conversation_state 未携带
        # fromAccount（例如 ConversationStateSlot 验证器拒绝内部 ID，或
        # evaluation.recognized 未包含 fromAccount），_sibling_from_accounts
        # 传播失效。此处作为 envelope 级最后一道防线：直接从同族任务复制。
        _active_fa = active_task.slots.get("fromAccount")
        if _active_fa is None or getattr(_active_fa, "source", None) not in (
            "user_explicit", "user_selection"
        ):
            for _sibling in envelope.tasks:
                if _sibling.task_id == active_task.task_id:
                    continue
                _sibling_fa = _sibling.slots.get("fromAccount")
                if _sibling_fa is not None and getattr(
                    _sibling_fa, "source", None
                ) in ("user_explicit", "user_selection"):
                    active_task = active_task.model_copy(update={
                        "slots": {**active_task.slots, "fromAccount": _sibling_fa}
                    })
                    envelope = envelope.model_copy(update={
                        "tasks": [
                            active_task if item.task_id == active_task.task_id else item
                            for item in envelope.tasks
                        ]
                    })
                    logger.info(
                        "fromAccount propagated from sibling task %s to %s value=%s",
                        _sibling.task_id, active_task.task_id, _sibling_fa.value,
                    )
                    break
        # --- fromAccount 从 conversation_context 恢复（context 级）---
        # 当 envelope 中无同族任务可复制（如批量转账首笔已完成、envelope
        # 仅含第二笔）且 _sibling_from_accounts 传播失效时，从原始请求的
        # conversation_context.shared_slots 和 conversation_context.goals
        # 中恢复 fromAccount。这是批量转账跨轮次传播的最后一道防线。
        _active_fa = active_task.slots.get("fromAccount")
        if _active_fa is None or getattr(_active_fa, "source", None) not in (
            "user_explicit", "user_selection"
        ):
            _ctx = getattr(request, "conversation_context", None)
            _recovered_fa: SlotValue | None = None
            if _ctx is not None:
                for _slot in getattr(_ctx, "shared_slots", []) or []:
                    if (
                        _slot.name == "fromAccount"
                        and getattr(_slot, "source", None) in ("user_explicit", "user_selection")
                        and _slot.value
                    ):
                        _recovered_fa = SlotValue(
                            value=_slot.value,
                            source=_slot.source,
                            confidence=1,
                        )
                        break
                if _recovered_fa is None:
                    for _goal in getattr(_ctx, "goals", []) or []:
                        for _slot in getattr(_goal, "slots", []) or []:
                            if (
                                _slot.name == "fromAccount"
                                and getattr(_slot, "source", None) in ("user_explicit", "user_selection")
                                and _slot.value
                            ):
                                _recovered_fa = SlotValue(
                                    value=_slot.value,
                                    source=_slot.source,
                                    confidence=1,
                                )
                                break
                        if _recovered_fa is not None:
                            break
            if _recovered_fa is not None:
                active_task = active_task.model_copy(update={
                    "slots": {**active_task.slots, "fromAccount": _recovered_fa}
                })
                envelope = envelope.model_copy(update={
                    "tasks": [
                        active_task if item.task_id == active_task.task_id else item
                        for item in envelope.tasks
                    ]
                })
                logger.info(
                    "fromAccount recovered from conversation_context: value=%s source=%s",
                    _recovered_fa.value, _recovered_fa.source,
                )
        current_envelope = _single_task_envelope(envelope, active_task)
        request_view = type("ResumeRequestView", (), {
            "agent_run_id": request.agent_run_id,
            "message": request.clarification_message or "",
            "conversation_context": getattr(request, "conversation_context", None),
        })()
        constraints = [_constraint_from_intent_candidate(
            self._intent_graph.candidate_for_intent(
                active_task.intent_id, score=0, evidence=["workflow_resume"]
            )
        )]
        progress = TaskProgress.model_validate(
            sequential_progress(goals, active_task.task_id)
        )
        confirmation = None
        clarification_form = None
        pending_checkpoint_ids: list[str] = []
        graph_reference_resolution: (
            AccountReferenceResolution | CardReferenceResolution | ProductReferenceResolution | HoldingReferenceResolution | PartyReferenceResolution | None
        ) = None
        repayment_facts: dict[str, Any] | None = None
        active_candidate = self._intent_graph.candidate_for_intent(
            active_task.intent_id,
            score=0,
            evidence=["workflow_resume_active_task"],
        )
        active_decision = decide_active_task(
            task=active_task,
            candidate=active_candidate,
            evaluation=evaluations[active_task.task_id],
            intent_shape=envelope.intent_shape,
        )
        if (
            not uses_transfer_compatibility_adapter(self._intent_graph, active_task)
            and active_decision.readiness == "resolve"
        ):
            try:
                (
                    active_task,
                    active_evaluation,
                    active_decision,
                    graph_reference_resolution,
                    successful_invocations,
                ) = await self._resolve_graph_reference_chain(
                    agent_run_id=request.agent_run_id,
                    message=(
                        request.clarification_message or ""
                        if resume_task.task_id == active_task.task_id
                        else ""
                    ),
                    task=active_task,
                    candidate=active_candidate,
                    evaluation=evaluations[active_task.task_id],
                    intent_shape=envelope.intent_shape,
                    executable_ids=executable_ids,
                    successful_invocations=successful_invocations,
                )
                evaluations[active_task.task_id] = active_evaluation
                envelope = envelope.model_copy(update={
                    "tasks": [
                        active_task if item.task_id == active_task.task_id else item
                        for item in envelope.tasks
                    ]
                })
                current_envelope = _single_task_envelope(envelope, active_task)
            except (
                AccountReferenceResolutionError,
                CardReferenceResolutionError,
                ProductReferenceResolutionError,
                HoldingReferenceResolutionError,
                PartyReferenceResolutionError,
                AgentCapabilityInvocationError,
                AgentContractError,
                TaskSlotRuntimeError,
            ) as exc:
                logger.warning(
                    "Native business reference resolution rejected "
                    "code=native_reference_resolution_failed error_type=%s reason=%s",
                    _safe_restore_error_type(exc),
                    str(exc),
                )
                raise AgentContractError(
                    "native business reference resolution failed"
                ) from exc
        if (
            active_candidate.response_policy == "credit_card_repayment"
            and (
                active_decision.readiness == "execute"
                or (
                    active_decision.readiness == "clarify"
                    and active_decision.slot_name == "amount"
                )
            )
        ):
            bindings = evaluations[active_task.task_id].resolved_values
            card_id = bindings.get("cardId")
            account_id = bindings.get("fromAccountId")
            if not isinstance(card_id, str) or not isinstance(account_id, str):
                raise AgentContractError(
                    "credit-card repayment references are unresolved"
                )
            for capability_id, capability_input in (
                ("bank.credit-card.bill.get", {"cardId": card_id}),
                ("bank.account.balance.get", {"accountId": account_id}),
            ):
                if not any(
                    item.get("capability_id") == capability_id
                    and item.get("input") == capability_input
                    for item in successful_invocations
                ):
                    successful_invocations = _merge_invocations(
                        successful_invocations,
                        await self._invoke_read_once(
                            request.agent_run_id,
                            capability_id,
                            capability_input,
                            executable_ids,
                        ),
                    )
            repayment_facts = _guided_credit_card_repayment_facts(
                successful_invocations,
                {"cardId": card_id, "fromAccountId": account_id},
            )
            if repayment_facts["bill_state"] == "settled":
                delegated_ids = tuple(item.task_id for item in envelope.tasks)
                settled_items = tuple(
                    item.model_copy(update={"status": "completed"})
                    if todo_belongs_to_goal(
                        item.id, active_task.task_id, delegated_ids
                    ) and item.status not in {"completed", "cancelled"}
                    else item
                    for item in snapshot.items
                )
                snapshot = TodoLedgerSnapshot(
                    revision=snapshot.revision,
                    digest=TodoLedger.digest(settled_items),
                    items=settled_items,
                )
                goals = goal_states_from_todos(envelope, snapshot.items)
                next_goal = select_active_goal(goals, active_task.task_id)
                next_goal_id = next_goal.task_id if next_goal is not None else None
                next_todo_id = None
                if next_goal_id is not None:
                    next_todo = next(
                        (
                            item for item in snapshot.items
                            if item.status not in {"completed", "cancelled"}
                            and todo_belongs_to_goal(
                                item.id, next_goal_id, delegated_ids
                            )
                        ),
                        None,
                    )
                    if next_todo is None:
                        raise AgentContractError(
                            "settled repayment continuation todo is unavailable"
                        )
                    next_todo_id = next_todo.id
                outcome = outcome.model_copy(update={
                    "status": (
                        "completed" if next_goal_id is None
                        else "awaiting_clarification"
                    ),
                    "active_goal_task_id": next_goal_id,
                    "active_todo_id": next_todo_id,
                    "next_action": (
                        None if next_goal_id is None else "collect_required_slots"
                    ),
                })
                progress = (
                    TaskProgress.model_validate(
                        sequential_progress(goals, next_goal_id)
                    )
                    if next_goal_id is not None else None
                )
                settled_response = (
                    self._user_response_projector
                    .project_credit_card_repayment_settled(
                        product_name=repayment_facts["product_name"],
                        masked_card=repayment_facts["masked_card"],
                        due_label=repayment_facts["due_label"],
                        continue_next=next_goal_id is not None,
                        task_progress=progress,
                    )
                )
                terminal = next_goal_id is None
                await self._persist_native_resume_workflow(
                    request, envelope, business_workflow_id, workflow, outcome,
                    snapshot,
                    status=("completed" if terminal else "awaiting_clarification"),
                    pending_checkpoint_ids=[],
                )
                response_status = "succeeded" if terminal else "clarification_required"
                return BusinessWorkflowResumeResponse.model_validate({
                    "schemaVersion": "2.0",
                    "agentRunId": request.agent_run_id,
                    "businessWorkflowId": business_workflow_id,
                    "status": response_status,
                    "receipts": [],
                    "workflowRevision": outcome.workflow_revision,
                    "activeGoalTaskId": next_goal_id,
                    "taskProgress": (
                        progress.model_dump(by_alias=True)
                        if progress is not None else None
                    ),
                    "userResponse": settled_response.model_dump(
                        by_alias=True, exclude_none=True
                    ),
                    "conversationState": _build_conversation_state(
                        response_status, envelope, evaluations
                    ).model_dump(by_alias=True),
                })
        # 批量转账顺序推进：确认执行完上一笔（grants 属于上一任务）后
        # 活动任务已切到下一笔。下一笔即便槽位全就绪，也必须以预填补
        # 录表单（含转出账户下拉/澄清核对）交回用户提交后才执行，与首
        # 笔的办理方式完全一致，不得静默生成确认检查点跳过用户对每一
        # 笔的过目与选择。
        advanced_batch_task = (
            request.resume_kind == "confirmation"
            and bool(request.grants)
            and all(
                item.task_id != active_task.task_id for item in request.grants
            )
        )
        # 防重复扣款：确认恢复轮中活动任务仍是刚被授权执行的任务（grants
        # 属于它）时，说明台账结算滞后而资金已按授权回执执行。此时绝不能
        # 再对该任务签发第二张确认检查点（用户点击会导致同一笔转账再次
        # 扣款），也不得返回可提交的补录表单。返回安全澄清提示，等待台账
        # 对账追平后由用户继续后续任务。
        duplicate_executed_task = (
            request.resume_kind == "confirmation"
            and bool(request.grants)
            and not advanced_batch_task
            and uses_transfer_compatibility_adapter(self._intent_graph, active_task)
        )
        if duplicate_executed_task:
            logger.warning(
                "Suppress duplicate transfer checkpoint for just-executed task=%s",
                active_task.task_id,
            )
            resolution = None
            user_response = self._user_response_projector.project_clarification(None)
            response_status = "clarification_required"
            workflow_status = "awaiting_clarification"
        elif uses_transfer_compatibility_adapter(self._intent_graph, active_task) and not advanced_batch_task:
            resolution, added = await self._resolve_transfer_confirmation(
                request_view, current_envelope,
                {active_task.task_id: evaluations[active_task.task_id]},
                constraints, executable_ids, successful_invocations,
            )
            successful_invocations = _merge_invocations(
                successful_invocations, added
            )
        else:
            resolution = None
        if resolution is not None:
            plan = _confirmation_plan_from_resolution(
                current_envelope, active_task.task_id, resolution
            )
            # 条目序号取全量 envelope 供多笔批量确认卡编号；单笔确认卡
            # 由 _batch_transfer_summary 内部判断 len(steps)==1 时恒为 1。
            summary = _batch_transfer_summary(envelope, plan.steps)
            with self._capability_invoker.activate(
                request.agent_run_id,
                executable_ids,
                executable_ids | {"bank.transfer.order.create"},
            ):
                try:
                    checkpoint = await self._capability_invoker.create_action_checkpoint(
                        "bank.transfer.order.create", resolution["resolved_input"],
                        _gateway_safe_summary(summary),
                        business_workflow_id=business_workflow_id,
                        task_id=active_task.task_id,
                    )
                except AgentCapabilityInvocationError as exc:
                    logger.warning(
                        "Native business checkpoint creation rejected code=%s",
                        exc.code,
                    )
                    raise AgentContractError(
                        "native business checkpoint creation failed"
                    ) from exc
            confirmation = ConfirmationView.model_validate({
                **checkpoint, "summary": summary,
            })
            pending_checkpoint_ids = [confirmation.checkpoint_id]
            user_response = self._user_response_projector.project_confirmation(
                confirmation, task_progress=progress
            )
            response_status = "awaiting_confirmation"
            workflow_status = "awaiting_confirmation"
        elif uses_transfer_compatibility_adapter(
            self._intent_graph, active_task
        ) and not duplicate_executed_task:
            evaluation = evaluations[active_task.task_id]
            transfer_resolution: dict[str, Any] | None = None
            if active_decision.readiness == "execute" and not advanced_batch_task:
                # 槽位全就绪的顺序确认：与前一轮确认卡同一确定性解析链，
                # 生成下一笔的本人确认检查点；补录表单只服务无法解析的
                # 收款人，不得劫持已就绪任务。
                try:
                    transfer_resolution, transfer_resolution_invocations = (
                        await self._resolve_transfer_confirmation(
                            request,
                            current_envelope,
                            {active_task.task_id: evaluation},
                            constraints,
                            executable_ids,
                            successful_invocations,
                        )
                    )
                    successful_invocations = _merge_invocations(
                        successful_invocations, transfer_resolution_invocations
                    )
                except Exception:
                    logger.exception("Transfer resume entity resolution failed")
                    transfer_resolution = None
            # When readiness != "execute" (e.g. payee considered missing because
            # bank.payee.list was never invoked or returned stale data), ensure
            # both account and payee lists are available with fresh data, then
            # retry resolution. Without this, the payee is always "missing" and
            # _build_active_transfer_form returns the same transfer_payee form
            # repeatedly — a death loop on the 2nd transfer.
            if transfer_resolution is None:
                for capability_id in ("bank.account.list", "bank.payee.list"):
                    try:
                        extra_invocations = await self._invoke_read_once(
                            request.agent_run_id,
                            capability_id,
                            {},
                            executable_ids,
                        )
                    except Exception:
                        logger.warning(
                            "%s invocation failed for form options",
                            capability_id,
                        )
                        continue
                    if not extra_invocations:
                        continue
                    # Replace stale invocations for this capability_id+input
                    # so _invocation_items returns fresh data downstream.
                    successful_invocations = [
                        item for item in successful_invocations
                        if not (
                            item.get("capability_id") == capability_id
                            and item.get("input") == {}
                        )
                    ] + extra_invocations
                # 顺序推进的下一笔：列表只为表单选项预填服务，不做确认
                # 解析（advanced_batch_task），保持返回预填表单给用户核
                # 对提交；其余场景按原契约重试解析。
                if not advanced_batch_task:
                    try:
                        transfer_resolution, transfer_resolution_invocations = (
                            await self._resolve_transfer_confirmation(
                                request,
                                current_envelope,
                                {active_task.task_id: evaluation},
                                constraints,
                                executable_ids,
                                successful_invocations,
                            )
                        )
                        successful_invocations = _merge_invocations(
                            successful_invocations, transfer_resolution_invocations
                        )
                    except Exception:
                        logger.debug(
                            "Transfer resolution retry failed after list invocations"
                        )
                        transfer_resolution = None
            if transfer_resolution is not None:
                transfer_plan = _confirmation_plan_from_resolution(
                    current_envelope, active_task.task_id, transfer_resolution
                )
                transfer_step = transfer_plan.steps[0]
                transfer_summary = _batch_transfer_summary(
                    envelope, transfer_plan.steps
                )
                try:
                    with self._capability_invoker.activate(
                        request.agent_run_id,
                        executable_ids,
                        executable_ids | {transfer_step.capability_id},
                    ):
                        raw_transfer_checkpoint = (
                            await self._capability_invoker.create_action_checkpoint(
                                transfer_step.capability_id,
                                _transfer_capability_input(transfer_step.input_bindings),
                                _gateway_safe_summary(transfer_summary),
                                business_workflow_id=business_workflow_id,
                                task_id=active_task.task_id,
                            )
                        )
                except AgentCapabilityInvocationError as exc:
                    raise AgentContractError(
                        "native transfer checkpoint creation failed"
                    ) from exc
                confirmation = ConfirmationView.model_validate({
                    **raw_transfer_checkpoint,
                    "summary": transfer_summary,
                })
                pending_checkpoint_ids = [confirmation.checkpoint_id]
                user_response = self._user_response_projector.project_confirmation(
                    confirmation, task_progress=progress
                )
                response_status = "awaiting_confirmation"
                workflow_status = "awaiting_confirmation"
                outcome = outcome.model_copy(update={
                    "status": "awaiting_confirmation",
                    "next_action": "confirm_transfer_order",
                })
            else:
                # bank.account.list and bank.payee.list already invoked in
                # the retry block above; just build the form.
                user_response, clarification_form = _build_active_transfer_form(
                    self._user_response_projector, active_task, evaluation,
                    successful_invocations, progress,
                    force_form=advanced_batch_task,
                )
                response_status = "clarification_required"
                workflow_status = "awaiting_clarification"
        elif graph_reference_resolution is not None:
            user_response = self._user_response_projector.project_slot_resolution(
                slot_type=active_decision.slot_type or "",
                slot_label=active_decision.slot_label or "业务对象",
                resolution=graph_reference_resolution,
            )
            response_status = (
                "clarification_required"
                if user_response.type == "clarification" else "blocked"
            )
            workflow_status = "awaiting_clarification"
        elif (
            repayment_facts is not None
            and active_decision.readiness == "clarify"
            and active_decision.slot_name == "amount"
        ):
            user_response = (
                self._user_response_projector.project_credit_card_repayment_choice(
                    **repayment_facts
                )
            )
            response_status = "clarification_required"
            workflow_status = "awaiting_clarification"
        elif (
            repayment_facts is not None
            and active_decision.readiness == "execute"
        ):
            bindings = dict(evaluations[active_task.task_id].resolved_values)
            amount_minor = bindings.get("amountMinor")
            card_id = bindings.get("cardId")
            account_id = bindings.get("fromAccountId")
            if (
                not isinstance(amount_minor, int)
                or isinstance(amount_minor, bool)
                or not isinstance(card_id, str)
                or not isinstance(account_id, str)
            ):
                raise AgentContractError(
                    "credit-card repayment execution bindings are incomplete"
                )
            summary = _credit_card_repayment_confirmation_summary(
                repayment_facts, amount_minor
            )
            try:
                with self._capability_invoker.activate(
                    request.agent_run_id,
                    executable_ids,
                    executable_ids | {active_decision.execution_capability_id},
                ):
                    raw_checkpoint = await self._capability_invoker.create_action_checkpoint(
                        active_decision.execution_capability_id,
                        {
                            "cardId": card_id,
                            "fromAccountId": account_id,
                            "amountMinor": amount_minor,
                        },
                        summary,
                        business_workflow_id=business_workflow_id,
                        task_id=active_task.task_id,
                    )
            except AgentCapabilityInvocationError as exc:
                raise AgentContractError(
                    "native credit-card checkpoint creation failed"
                ) from exc
            confirmation = ConfirmationView.model_validate({
                **raw_checkpoint,
                "summary": summary,
            })
            pending_checkpoint_ids = [confirmation.checkpoint_id]
            user_response = self._user_response_projector.project_confirmation(
                confirmation, task_progress=progress
            )
            response_status = "awaiting_confirmation"
            workflow_status = "awaiting_confirmation"
        elif (
            active_task.action in {"DO", "CHANGE"}
            and active_decision.readiness == "execute"
            and active_candidate.slot_contract is not None
            and active_candidate.slot_contract.execution_binding.requires_confirmation
        ):
            checkpoint_input, summary = _graph_action_confirmation_contract(
                active_task,
                active_candidate,
                evaluations[active_task.task_id],
            )
            try:
                with self._capability_invoker.activate(
                    request.agent_run_id,
                    executable_ids,
                    executable_ids | {active_decision.execution_capability_id},
                ):
                    raw_checkpoint = await self._capability_invoker.create_action_checkpoint(
                        active_decision.execution_capability_id,
                        checkpoint_input,
                        summary,
                        business_workflow_id=business_workflow_id,
                        task_id=active_task.task_id,
                    )
            except AgentCapabilityInvocationError as exc:
                raise AgentContractError(
                    "native graph action checkpoint creation failed"
                ) from exc
            confirmation = ConfirmationView.model_validate({
                **raw_checkpoint,
                "summary": summary,
            })
            pending_checkpoint_ids = [confirmation.checkpoint_id]
            user_response = self._user_response_projector.project_confirmation(
                confirmation, task_progress=progress
            )
            response_status = "awaiting_confirmation"
            workflow_status = "awaiting_confirmation"
            outcome = outcome.model_copy(update={
                "status": "awaiting_confirmation",
                "next_action": "confirm_graph_action",
            })
        else:
            evaluation = evaluations[active_task.task_id]
            user_response = self._user_response_projector.project_clarification(
                active_task,
                slot_evaluation=evaluation,
                successful_invocations=successful_invocations,
            )
            if user_response is None:
                raise AgentContractError(
                    "resumed active task response is unavailable"
                )
            response_status = "clarification_required"
            workflow_status = "awaiting_clarification"
        await self._persist_native_resume_workflow(
            request, envelope, business_workflow_id, workflow, outcome, snapshot,
            status=workflow_status, pending_checkpoint_ids=pending_checkpoint_ids,
        )
        return BusinessWorkflowResumeResponse.model_validate({
            "schemaVersion": "2.0", "agentRunId": request.agent_run_id,
            "businessWorkflowId": business_workflow_id, "status": response_status,
            "receipts": [item.model_dump(by_alias=True) for item in safe_receipts],
            "workflowRevision": outcome.workflow_revision,
            "activeGoalTaskId": active_task.task_id,
            "taskProgress": progress.model_dump(by_alias=True),
            "userResponse": (
                None
                if completed_resume_response is not None
                else user_response.model_dump(by_alias=True, exclude_none=True)
            ),
            "userResponses": (
                [
                    completed_resume_response.model_dump(
                        by_alias=True, exclude_none=True
                    ),
                    user_response.model_dump(by_alias=True, exclude_none=True),
                ]
                if completed_resume_response is not None
                else None
            ),
            "confirmation": (
                confirmation.model_dump(by_alias=True, exclude_none=True)
                if confirmation else None
            ),
            "clarificationForm": (
                clarification_form.model_dump(by_alias=True, exclude_none=True)
                if clarification_form else None
            ),
            "conversationState": _build_conversation_state(
                response_status, envelope, evaluations
            ).model_dump(by_alias=True),
        })

    def _clarification_fit_contract(
        self, resume_task: IntentTask
    ) -> dict[str, Any] | None:
        """澄清答案装填约束的声明式投影（零模型调用）。

        解析器背书的必需槽位在语义上是单实体引用。把该约束在澄清恢复
        轮显式声明给业务智能体，是模型判断"答案装不进契约、须申报
        replan"的确定依据；不声明就要求模型自行判断槽位容量是不可靠的。
        """
        try:
            candidate = self._intent_graph.candidate_for_intent(
                resume_task.intent_id, score=0, evidence=["clarification_fit"]
            )
            contract = candidate.slot_contract
            if contract is None:
                return None
            resolved_names = {
                name for name, slot in resume_task.slots.items()
                if isinstance(slot, SlotValue)
            }
            # 已解析槽位不进契约：全就绪时契约必须整体退场（返回 None），
            # 否则"待澄清"的声明框架会诱导模型对已就绪任务重复追问。
            single_value_slots = [
                {"name": item.name, "label": item.label, "type": item.type}
                for item in contract.slots
                if item.required
                and item.resolver_capability_id is not None
                and item.name not in resolved_names
            ]
            if not single_value_slots:
                return None
            return {
                "taskId": resume_task.task_id,
                "singleValueSlots": single_value_slots,
                "rule": (
                    "以下槽位每个只能承接一个实体取值："
                    + "、".join(item["label"] for item in single_value_slots)
                    + "。若用户本轮澄清答案包含多个并列实体（如两个收款人姓名）或无法"
                    "作为单值承接，禁止拆分或新增 Todo、禁止用原始答案调用解析器；必须"
                    "通过 submit_business_loop_outcome 提交 status=replan_required 与"
                    " proposed_tasks（每个实体一项，intent_id 与活动 Goal 相同，槽位名"
                    "→单实体值）。只有单一实体答案才允许填入槽位。"
                ),
            }
        except Exception:
            return None

    async def _adopt_replan_outcome(
        self,
        request: RuntimeAgentRunRequest,
        envelope: IntentTaskEnvelope,
        business_workflow_id: str,
        workflow: dict[str, Any],
        outcome: BusinessLoopOutcome,
        resume_task: IntentTask,
        goals: tuple[GoalState, ...],
        session_id: str,
    ) -> BusinessWorkflowResumeResponse | None:
        """同意图结构重构申报的确定性采納（编排器握手）。

        业务智能体在澄清相位遇到无法装进已声明槽位契约的答案（如单收款人
        槽位收到两个收款人）时，以 replan_required 申报重构后的同意图任务
        集。本方法零模型调用：跨意图拒绝 → 图谱候选校验 → 新任务确定性
        构建（结构标识从活动任务继承，槽位=活动任务槽位+提案覆盖）→ 信封
        合并并整体重校验 → 槽位契约全量重评 → 域适配器派生歧义并投影澄清
        卡 → 权威 Todo 重播种 → 持久化。任一步失败返回 None，由调用方降级
        为普通澄清；每次 resume 至多采納一次（本分支 return 即结束）。
        """
        try:
            proposals = list(outcome.proposed_tasks)
            if not proposals:
                return None
            merged, evaluations, active_task_id = _build_replan_envelope(
                request, envelope, resume_task, proposals,
                self._intent_graph, self._slot_evaluator,
            )
            candidate = self._intent_graph.candidate_for_intent(
                resume_task.intent_id, score=0, evidence=["replan_adoption"]
            )
            known_task_ids = {task.task_id for task in merged.tasks}
            message = request.clarification_message or request.message or ""
            ambiguity = next(
                (
                    item for item in derive_domain_ambiguities(
                        self._intent_graph, merged.tasks, message, (),
                        derive_kwargs={"replan_split": True},
                    )
                    if set(item.task_ids or ()) <= known_task_ids
                    and domain_ambiguity_is_projectable(self._intent_graph, merged, item)
                ),
                None,
            )
            if ambiguity is None:
                logger.warning(
                    "Replan adoption has no projectable ambiguity; demoting"
                    " intent=%s tasks=%s",
                    resume_task.intent_id,
                    [task.task_id for task in merged.tasks],
                )
                return None
            ambiguity_response = project_domain_ambiguity(
                self._intent_graph,
                merged,
                ambiguity,
                self._user_response_projector,
                message=message,
            )
            if (
                ambiguity_response is None
                or ambiguity_response.type != "clarification"
            ):
                return None
            old_status_by_id = {item.task_id: item.status for item in goals}
            seed_snapshot = self._business_runner.seed_todo_ledger(session_id, [
                {
                    "id": task.task_id,
                    "status": (
                        "in_progress"
                        if task.task_id == active_task_id
                        else "completed"
                        if old_status_by_id.get(task.task_id) == "succeeded"
                        else "pending"
                    ),
                    "dependsOn": [],
                    "title": candidate.title,
                }
                for task in merged.tasks
            ])
            adopted_outcome = outcome.model_copy(update={
                "status": "awaiting_clarification",
                "active_goal_task_id": active_task_id,
                "active_todo_id": active_task_id,
                "proposed_tasks": [],
            })
            progress = TaskProgress.model_validate(
                sequential_progress(
                    goal_states_from_todos(merged, seed_snapshot.items),
                    active_task_id,
                )
            )
            response = BusinessWorkflowResumeResponse.model_validate({
                "schemaVersion": "2.0",
                "agentRunId": request.agent_run_id,
                "businessWorkflowId": business_workflow_id,
                "status": "clarification_required",
                "receipts": [],
                "workflowRevision": adopted_outcome.workflow_revision,
                "activeGoalTaskId": active_task_id,
                "taskProgress": progress.model_dump(by_alias=True),
                "userResponse": ambiguity_response.model_dump(
                    by_alias=True, exclude_none=True
                ),
                "conversationState": _build_conversation_state(
                    "clarification_required", merged, evaluations,
                ).model_dump(by_alias=True),
            })
            await self._persist_native_resume_workflow(
                request, merged, business_workflow_id, workflow,
                adopted_outcome, seed_snapshot,
                status="awaiting_clarification",
                pending_checkpoint_ids=[],
            )
            logger.warning(
                "Replan proposal adopted: intent=%s tasks=%s ambiguity=%s",
                intent_id,
                [task.task_id for task in merged.tasks],
                ambiguity.type,
            )
            return response
        except Exception:
            logger.warning(
                "Replan adoption failed; demoting to plain clarification",
                exc_info=True,
            )
            return None

    async def _adopt_replan_message_entry(
        self,
        request: RuntimeAgentRunRequest,
        envelope: IntentTaskEnvelope,
        outcome: BusinessLoopOutcome,
        workflow_id: str,
        persisted_revision: int,
        old_goals: tuple[GoalState, ...],
        session_id: str,
    ) -> (
        RuntimeAgentRunResponse
        | tuple[
            IntentTaskEnvelope,
            BusinessLoopOutcome,
            dict[str, SlotEvaluation],
            Any,
            tuple[TodoSnapshotItem, ...],
        ]
        | None
    ):
        """消息入口轮的 replan 采納（与 resume 路径同一套确定性握手）。

        有可投影歧义：重播种台账 → 以新结构持久化 → 返回澄清卡响应
        （每人/合计），调用方收束本轮。
        无可投影歧义：返回就地改写五元组（新信封、降级 outcome、新任务
        评估、图谱候选、新台账快照），既有机制在正确结构上继续推进。
        构建失败：返回 None，调用方降级为普通澄清。
        """
        try:
            active_task = next(
                (
                    task for task in envelope.tasks
                    if task.task_id == outcome.active_goal_task_id
                ),
                None,
            )
            if active_task is None or not outcome.proposed_tasks:
                return None
            merged, evaluations, active_task_id = _build_replan_envelope(
                request, envelope, active_task, list(outcome.proposed_tasks),
                self._intent_graph, self._slot_evaluator,
            )
            candidate = self._intent_graph.candidate_for_intent(
                active_task.intent_id, score=0, evidence=["replan_adoption"]
            )
            known_task_ids = {task.task_id for task in merged.tasks}
            message = request.message or ""
            ambiguity = next(
                (
                    item for item in derive_domain_ambiguities(
                        self._intent_graph, merged.tasks, message, (),
                        derive_kwargs={"replan_split": True},
                    )
                    if set(item.task_ids or ()) <= known_task_ids
                    and domain_ambiguity_is_projectable(self._intent_graph, merged, item)
                ),
                None,
            )
            adopted_outcome = outcome.model_copy(update={
                "status": "awaiting_clarification",
                "active_goal_task_id": active_task_id,
                "active_todo_id": active_task_id,
                "proposed_tasks": [],
                "covered_task_ids": [task.task_id for task in merged.tasks],
                "workflow_revision": persisted_revision + 1,
            })
            old_status_by_id = {item.task_id: item.status for item in old_goals}
            seed_snapshot = self._business_runner.seed_todo_ledger(session_id, [
                {
                    "id": task.task_id,
                    "status": (
                        "in_progress"
                        if task.task_id == active_task_id
                        else "completed"
                        if old_status_by_id.get(task.task_id) == "succeeded"
                        else "pending"
                    ),
                    "dependsOn": [],
                    "title": candidate.title,
                }
                for task in merged.tasks
            ])
            if ambiguity is not None:
                try:
                    card = project_domain_ambiguity(
                        self._intent_graph,
                        merged,
                        ambiguity,
                        self._user_response_projector,
                        message=message,
                    )
                except Exception:
                    logger.warning(
                        "Replan ambiguity projection failed; advancing structure",
                        exc_info=True,
                    )
                    card = None
                if card is not None and card.type == "clarification":
                    await self._persist_native_resume_workflow(
                        request, merged, workflow_id,
                        {"workflowRevision": persisted_revision},
                        adopted_outcome, seed_snapshot,
                        status="awaiting_clarification",
                        pending_checkpoint_ids=[],
                    )
                    logger.warning(
                        "Replan proposal adopted with ambiguity card:"
                        " intent=%s tasks=%s ambiguity=%s",
                        active_task.intent_id,
                        [task.task_id for task in merged.tasks],
                        ambiguity.type,
                    )
                    return RuntimeAgentRunResponse(
                        agentRunId=request.agent_run_id,
                        conversationId=request.conversation_id,
                        status="clarification_required",
                        intentTaskEnvelope=merged,
                        capabilityPlan=_empty_plan(
                            merged, "clarification_required", "skipped"
                        ),
                        userResponse=card,
                        conversationState=_build_conversation_state(
                            "clarification_required", merged, evaluations,
                        ),
                    )
            logger.warning(
                "Replan proposal adopted in place: intent=%s tasks=%s",
                active_task.intent_id,
                [task.task_id for task in merged.tasks],
            )
            return (
                merged,
                adopted_outcome,
                evaluations,
                candidate,
                seed_snapshot.items,
            )
        except Exception:
            logger.warning(
                "Message-entry replan adoption failed; demoting",
                exc_info=True,
            )
            return None

    async def _escalate_stuck_clarification(
        self,
        request: RuntimeAgentRunRequest,
        envelope: IntentTaskEnvelope,
        outcome: BusinessLoopOutcome,
        workflow_id: str,
        workflow_persisted_revision: int,
        goal_states: tuple[GoalState, ...],
        business_session: str,
        restored_workflow: dict[str, Any] | None,
        active_task: IntentTask,
        fit_contract: dict[str, Any] | None,
    ) -> RuntimeAgentRunResponse | tuple | None:
        """卡死澄清的确定性升级（编排器握手，模型申报失败时的兜底）。

        触发签名完全确定：用户正在回答澄清（恢复态 awaiting_clarification）、
        本轮业务循环仍以 awaiting_clarification 结束、答案非空——即用户已
        作答而循环仍无法装填槽位。常态路径（一次作答即可推进）零额外调用。

        动作：一次结构化抽取判断答案含几个实体（复用连续性层已注册模型）；
        任一单值槽位抽到 ≥2 个实体时，按实体数合成 replan 提案，走与模型
        申报完全相同的确定性采纳握手（图谱候选校验、槽位契约重评、台账
        重播种、持久化）。抽取失败/单实体/提案不可采纳一律 fail-open 回
        既有澄清重问。
        """
        if fit_contract is None:
            logger.debug(
                "Stuck escalation skip: no fit contract workflow=%s", workflow_id
            )
            return None
        logger.debug(
            "Stuck escalation checked workflow=%s restored_status=%s"
            " outcome_status=%s message_len=%s budget=%s",
            workflow_id,
            (restored_workflow or {}).get("status"),
            outcome.status,
            len((request.message or "").strip()),
            self._stuck_clarification_budget.get(workflow_id, 0),
        )
        if restored_workflow is None or restored_workflow.get("status") != (
            "awaiting_clarification"
        ):
            return None
        message = (request.message or "").strip()
        if not message:
            return None
        used = self._stuck_clarification_budget.get(workflow_id, 0)
        if used >= 3:
            return None
        try:
            raw = await self._task_continuity.extract_structured(
                system=(
                    "你是银行业务的实体抽取器。给定单值槽位契约（每个槽位只能承接"
                    "一个实体取值）和用户的一条澄清答案，判断答案实际包含几个实体值。"
                    '只输出 JSON，禁止解释：{"slots": {"<槽位名>": ["<实体值1>", "<实体值2>"]}}。'
                    "规则：单实体答案数组只有一个元素；多个并列实体（如两位收款人姓名"
                    "连写、顿号/和/与连接）拆成多个元素；答案与槽位无关或无法抽取时"
                    "输出 {\"slots\": {}}。实体值必须原样保留用户用词，禁止改写、翻译、"
                    "增删字或按单字切分。"
                ),
                message=(
                    "槽位契约："
                    + json.dumps(
                        fit_contract.get("singleValueSlots") or [],
                        ensure_ascii=False,
                    )
                    + "\n用户澄清答案："
                    + message
                ),
            )
        except Exception:
            # 调用失败不消耗预算：预算限制的是抽取行为本身，不是基础设施抖动。
            logger.warning(
                "Stuck clarification extraction call failed workflow=%s",
                workflow_id,
                exc_info=True,
            )
            return None
        self._stuck_clarification_budget[workflow_id] = used + 1
        if not raw:
            return None
        try:
            text = raw.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:]
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                return None
            parsed = json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            logger.warning(
                "Stuck clarification extraction unparseable workflow=%s", workflow_id
            )
            return None
        slots = parsed.get("slots") if isinstance(parsed, dict) else None
        if not isinstance(slots, dict):
            return None
        contract_slots = {
            item.get("name")
            for item in (fit_contract.get("singleValueSlots") or [])
            if isinstance(item, dict) and item.get("name")
        }
        slot_contract = None
        try:
            slot_contract = self._intent_graph.candidate_for_intent(
                active_task.intent_id, score=0, evidence=["stuck_escalation"]
            ).slot_contract
        except Exception:
            slot_contract = None
        proposals: list[ReplanTaskProposal] = []
        for slot_name, values in slots.items():
            if slot_name not in contract_slots or not isinstance(values, list):
                continue
            values = [
                value.strip()
                for value in values
                if isinstance(value, str) and value.strip()
            ]
            # 提案值必须通过槽位声明契约的形态校验才能驱动结构重构：
            # 类型化槽位（account_reference）抽不出合法取值即整组放弃，
            # 交给既有澄清重问；否则人名等无关实体会被灌进槽位并炮制
            # 出重复业务动作的重组提案。
            if slot_contract is not None:
                try:
                    definition = slot_contract.slot(slot_name)
                except KeyError:
                    definition = None
                if definition is not None and definition.type == "account_reference":
                    values = [
                        normalized
                        for value in values
                        if (normalized := normalize_account_reference(value)) is not None
                    ]
            if len(values) < 2 or len(values) > 8:
                continue
            proposals = [
                ReplanTaskProposal(
                    intent_id=active_task.intent_id, slots={slot_name: value}
                )
                for value in values
            ]
            break
        if not proposals:
            return None
        logger.warning(
            "Stuck clarification escalated to deterministic replan workflow=%s"
            " task=%s slot_entities=%s",
            workflow_id,
            active_task.task_id,
            [proposal.slots for proposal in proposals],
        )
        escalated = outcome.model_copy(update={
            "status": "replan_required",
            "proposed_tasks": proposals,
        })
        return await self._adopt_replan_message_entry(
            request, envelope, escalated,
            workflow_id, workflow_persisted_revision,
            goal_states, business_session,
        )

    async def _persist_native_resume_workflow(
        self, request, envelope, workflow_id, workflow, outcome, snapshot, *,
        status, pending_checkpoint_ids,
    ) -> None:
        assert self._capability_invoker is not None
        delegated_task_ids = [item.task_id for item in envelope.tasks]
        scoped_items = _scoped_persist_todo_items(
            snapshot.items, delegated_task_ids
        )
        await self._capability_invoker.persist_agent_workflow(
            agent_run_id=request.agent_run_id,
            business_workflow_id=workflow_id,
            status=status,
            delegated_task_ids=delegated_task_ids,
            expected_revision=workflow["workflowRevision"],
            envelope_revision=envelope.revision,
            workflow_revision=outcome.workflow_revision,
            todo_revision=outcome.todo_revision,
            todo_snapshot=[{
                "id": item.id, "status": item.status,
                "dependsOn": list(item.depends_on),
            } for item in scoped_items],
            todo_digest=TodoLedger.digest(scoped_items),
            active_goal_task_id=outcome.active_goal_task_id,
            active_todo_id=outcome.active_todo_id,
            suspension_reason=(status if status.startswith("awaiting_") else None),
            last_outcome=outcome.model_dump(),
            pending_checkpoint_ids=pending_checkpoint_ids,
        )

    async def stream(self, request: RuntimeAgentRunRequest) -> AsyncIterator[AgentTraceEvent]:
        async for event, _candidate in self._execute(request):
            yield event

    async def stream_with_result(
        self, request: RuntimeAgentRunRequest
    ) -> AsyncIterator[tuple[AgentTraceEvent, RuntimeAgentRunResponse | None]]:
        """Stream value-free Trace events and expose the mobile result on a separate channel."""
        async for event, candidate in self._execute(request):
            yield event, candidate if isinstance(candidate, RuntimeAgentRunResponse) else None

    async def _execute(
        self, request: RuntimeAgentRunRequest
    ) -> AsyncIterator[
        tuple[AgentTraceEvent, RuntimeAgentRunResponse | None]
    ]:
        conversation_lock = await self._conversation_lock(
            request.conversation_id
        )
        async with conversation_lock:
            scope_token = model_capture.set_run_scope(request.agent_run_id)
            run_started = time.monotonic()
            last_sequence = 0
            final_response: RuntimeAgentRunResponse | None = None
            try:
                # run 级硬超时：模型/框架层偶发出现预算外循环（llm_call_start
                # 持续无 end 的僵尸 run，与真实请求抢模型中转容量）。无论循环
                # 出在deepagent 迭代、工具重试还是客户端层，超时统一强杀本
                # run，burn 上限 = timeout × 调用频率。
                async with asyncio.timeout(self._run_timeout_seconds):
                    async for item in self._execute_serialized(request):
                        last_sequence = item[0].sequence
                        if isinstance(item[1], RuntimeAgentRunResponse):
                            final_response = item[1]
                        yield item
                    self._register_offered_replies(
                        request.conversation_id, final_response
                    )
            except TimeoutError:
                logger.warning(
                    "Agent run exceeded hard timeout run_id=%s timeout_s=%s elapsed_ms=%s",
                    request.agent_run_id,
                    self._run_timeout_seconds,
                    _elapsed_ms(run_started),
                )
                yield (
                    build_trace_event(
                        sequence=last_sequence + 1,
                        stage="run.failed",
                        status="failed",
                        agent_run_id=request.agent_run_id,
                        trace_id=request.trace_id,
                        title="智能服务处理超时",
                        input_fields=[],
                        output_fields=["errorCode"],
                        metrics={"failedStage": "hard_timeout"},
                        duration_ms=_elapsed_ms(run_started),
                        error=TraceError(
                            code="agent_run_timeout",
                            message="智能服务处理超时，请稍后重试",
                        ),
                    ),
                    _safe_failed_response(
                        request,
                        self._user_response_projector,
                        "agent_run_timeout",
                    ),
                )
            except Exception:
                logger.exception(
                    "Agent run unhandled exception agent_run_id=%s trace_id=%s",
                    request.agent_run_id,
                    request.trace_id,
                )
                yield (
                    build_trace_event(
                        sequence=last_sequence + 1,
                        stage="run.failed",
                        status="failed",
                        agent_run_id=request.agent_run_id,
                        trace_id=request.trace_id,
                        title="智能服务运行失败",
                        input_fields=[],
                        output_fields=["errorCode"],
                        metrics={"failedStage": "unhandled_exception"},
                        duration_ms=_elapsed_ms(run_started),
                        error=TraceError(
                            code="agent_runtime_failed",
                            message="智能服务运行遇到内部错误",
                        ),
                    ),
                    _safe_failed_response(
                        request,
                        self._user_response_projector,
                        "agent_runtime_failed",
                    ),
                )
            finally:
                model_capture.clear_run_scope(scope_token)

    def _register_offered_replies(
        self,
        conversation_id: str,
        response: RuntimeAgentRunResponse | None,
    ) -> None:
        """登记澄清卡向用户展示的确定性回复选项。

        系统自己给出的 reply action 载荷（金额口径、账户尾号选择等）其
        "续接既有任务"语义由构造保证——下一轮精确命中的输入无需再经
        IntentRecognizer 做 LLM 裁决，直接按续答进入权威重建 fast path。
        登记以 envelope_revision 为时效键：换轮/换信封即失配，未命中时
        回退 LLM 裁决，行为与不登记完全一致。进程重启丢表亦同理回退。
        """
        if response is None or response.status != "clarification_required":
            return
        envelope = response.intent_task_envelope
        if envelope is None or envelope.revision is None:
            return
        actions = response.user_response.actions if response.user_response else ()
        payloads = frozenset(
            action.payload.message
            for action in actions
            if action.kind == "reply" and action.payload.message
        )
        if not payloads:
            return
        target = next(
            (
                task.task_id
                for task in envelope.tasks
                if task.task_status != "completed"
            ),
            envelope.tasks[0].task_id if envelope.tasks else None,
        )
        if target is None:
            return
        self._offered_reply_registry[conversation_id] = (
            envelope.revision,
            payloads,
            target,
        )

    async def _execute_serialized(
        self, request: RuntimeAgentRunRequest
    ) -> AsyncIterator[
        tuple[AgentTraceEvent, RuntimeAgentRunResponse | None]
    ]:
        sequence = 0
        run_started = time.monotonic()

        def event(
            *,
            stage: str,
            status: str,
            title: str,
            input_fields: list[str],
            output_fields: list[str],
            metrics: dict[str, str | int | float | bool | None],
            duration_ms: int | None = None,
            memory_recall=None,
            intent_graph_detail=None,
            result=None,
            error: TraceError | None = None,
        ) -> AgentTraceEvent:
            nonlocal sequence
            sequence += 1
            return build_trace_event(
                sequence=sequence,
                stage=stage,
                status=status,
                agent_run_id=request.agent_run_id,
                trace_id=request.trace_id,
                title=title,
                input_fields=input_fields,
                output_fields=output_fields,
                metrics=metrics,
                duration_ms=duration_ms,
                memory_recall=memory_recall,
                intent_graph_detail=intent_graph_detail,
                result=result,
                error=error,
            )

        def failed(
            code: str, message: str, internal: AgentContractError
        ) -> tuple[AgentTraceEvent, RuntimeAgentRunResponse]:
            logger.warning("Agent run failed code=%s detail=%s", code, internal)
            return (
                event(
                    stage="run.failed",
                    status="failed",
                    title="智能服务运行失败",
                    input_fields=[],
                    output_fields=["errorCode"],
                    metrics={"failedStage": code},
                    duration_ms=_elapsed_ms(run_started),
                    error=TraceError(code=code, message=message),
                ),
                _safe_failed_response(
                    request, self._user_response_projector, code
                ),
            )

        # 工作记忆变量容器：user agent 运行后从 raw_envelope 提取，
        # finish 闭包引用以在终端记忆阶段提交。
        extracted_variables: dict[str, str] = {}

        async def finish(
            result: RuntimeAgentRunResponse,
            *,
            task_count: int,
            step_count: int,
        ):
            """Apply the output Rail to every normal user-visible terminal path."""

            output_text = _user_visible_response_text(result)
            if output_text and hasattr(self._input_rail, "evaluate_output"):
                output_started = time.monotonic()
                yield (
                    event(
                        stage="output_rail.started",
                        status="running",
                        title="输出安全检查已启动",
                        input_fields=["userResponse"],
                        output_fields=[],
                        metrics={},
                    ),
                    None,
                )
                try:
                    output_decision = await self._input_rail.evaluate_output(
                        text=output_text,
                        request_id=request.request_id,
                        conversation_id=request.conversation_id,
                    )
                    if not isinstance(output_decision, RailDecision):
                        raise TypeError("output Rail returned an invalid decision")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    output_decision = _unavailable_rail_decision("output")
                output_decision = _effective_rail_decision(
                    output_decision,
                    self._input_rail_mode,
                )
                yield (
                    event(
                        stage="output_rail.completed",
                        status="completed",
                        title="输出安全检查已完成",
                        input_fields=["userResponse"],
                        output_fields=["decision"],
                        metrics={
                            "decision": output_decision.decision,
                            "reasonCode": output_decision.reason_code,
                            "policyVersion": output_decision.policy_version,
                        },
                        duration_ms=_elapsed_ms(output_started),
                    ),
                    None,
                )
                if output_decision.decision != "allow":
                    terminal = _security_terminal_response(request, output_decision)
                    yield (
                        event(
                            stage="security_gate.decided",
                            status="completed",
                            title="输出安全门禁已阻止原始回复",
                            input_fields=["railDecision"],
                            output_fields=["status"],
                            metrics={
                                "decision": output_decision.decision,
                                "reasonCode": output_decision.reason_code,
                                "policyVersion": output_decision.policy_version,
                                "permitType": None,
                            },
                            duration_ms=_elapsed_ms(run_started),
                        ),
                        None,
                    )
                    yield (
                        event(
                            stage="run.completed",
                            status="completed",
                            title="智能服务安全结束",
                            input_fields=[],
                            output_fields=["status"],
                            metrics={
                                "status": terminal.status,
                                "taskCount": 0,
                                "stepCount": 0,
                            },
                            duration_ms=_elapsed_ms(run_started),
                            result=project_safe_run(terminal),
                        ),
                        terminal,
                    )
                    return
            try:
                safe_result = project_safe_run(result)
            except Exception:
                logger.exception(
                    "Terminal response projection failed agent_run_id=%s status=%s "
                    "envelope=%s plan=%s",
                    result.agent_run_id,
                    result.status,
                    result.intent_task_envelope is not None,
                    result.capability_plan is not None,
                )
                _spawn_memory_recording(
                    self, request, result, extracted_variables,
                )
                internal = AgentContractError("terminal response projection failed")
                yield failed(
                    "user_response_projection_failed",
                    "用户响应未通过安全投影",
                    internal,
                )
                return
            # Memory recording is fire-and-forget: it must not delay the
            # terminal SSE event. The memory gateway has its own error
            # handling and retry logic.
            _spawn_memory_recording(
                self, request, result, extracted_variables,
            )
            awaiting_confirmation = result.status == "awaiting_confirmation"
            yield (
                event(
                    stage=(
                        "run.awaiting_confirmation"
                        if awaiting_confirmation
                        else "run.completed"
                    ),
                    status=(
                        "awaiting_confirmation"
                        if awaiting_confirmation
                        else "completed"
                    ),
                    title=(
                        "智能服务等待本人确认"
                        if awaiting_confirmation
                        else "智能服务运行完成"
                    ),
                    input_fields=[],
                    output_fields=["status", "tasks", "steps"],
                    metrics={
                        "status": result.status,
                        "taskCount": task_count,
                        "stepCount": step_count,
                        **(
                            {"checkpointStatus": "pending", "grantCount": 0}
                            if awaiting_confirmation
                            else {}
                        ),
                    },
                    duration_ms=_elapsed_ms(run_started),
                    result=safe_result,
                ),
                result,
            )

        yield (
            event(
                stage="request.accepted",
                status="accepted",
                title="请求已接入",
                input_fields=["message", "pageContext"],
                output_fields=["agentRunId", "traceId"],
                metrics={"messageLength": len(request.message)},
            ),
            None,
        )

        parallel_group = "input-safety-and-intent"
        input_evaluator = getattr(self._input_rail, "evaluate_input", None)
        if input_evaluator is None:
            input_evaluator = self._input_rail.evaluate
        input_kwargs = {
            "message": request.message,
            "request_id": request.request_id,
            "conversation_id": request.conversation_id,
        }
        if hasattr(self._input_rail, "evaluate_input"):
            input_kwargs["history"] = request.safety_context.recent_user_messages
        rail_task = asyncio.create_task(input_evaluator(**input_kwargs))
        yield (
            event(
                stage="input_rail.started",
                status="running",
                title="输入安全检查已启动",
                input_fields=["message"],
                output_fields=[],
                metrics={"parallelGroup": parallel_group},
            ),
            None,
        )

        yield (
            event(
                stage="user_agent.started",
                status="running",
                title="用户智能体开始识别",
                input_fields=["message", "pageContext", "capabilitySummaries"],
                output_fields=[],
                metrics={"capabilityCount": len(request.plannable_capabilities)},
            ),
            None,
        )

        user_started = time.monotonic()
        shape_assessment = analyze_intent_shape(request.message)
        # current_safe_revision 只依赖 user_session_id，与意图图谱召回无依赖
        # 提前启动，与 _widen_intent_constraints 并行执行（预估节省 20-100ms）
        user_session_id = f"user:{request.conversation_id}"
        revision_task: asyncio.Task | None = None
        if hasattr(self._user_runner, "current_safe_revision"):
            revision_task = asyncio.create_task(
                self._user_runner.current_safe_revision(user_session_id)
            )
        # 工作记忆变量召回与意图图谱召回无依赖关系，提前启动并行执行
        variable_recall_task: asyncio.Task | None = None
        if self._memory_gateway is not None and hasattr(
            self._memory_gateway, "recall_variables"
        ):
            variable_recall_task = asyncio.create_task(
                self._memory_gateway.recall_variables(
                    tenant_id="bank", customer_id=request.customer_id,
                    agent_domain="ai-native-bank-user",
                    agent_run_id=request.agent_run_id,
                )
            )
        # 框架任务连续性裁决（openjiuwen TaskManager + IntentRecognizer）：
        # 续接期先判定任务操作。CREATE_TASK 在框架语义中即"中断当前执行中
        # 任务、执行新任务"——判定为跳转时剥离续接上下文，本轮按全新意图
        # 规划（旧工作流由 Node 侧既有 suspendSupersededWorkflow 挂起，旧
        # 目标不再进入新信封）。其余操作维持现有续接路径。框架异常时按无
        # 裁决处理（fail-open），不阻塞业务。
        continuity_supplement = False
        if (
            request.conversation_context.goals
            and request.conversation_context.pending_envelope_id is not None
        ):
            offered = self._offered_reply_registry.pop(request.conversation_id, None)
            if (
                offered is not None
                and request.conversation_context.envelope_revision is not None
                and offered[0] == request.conversation_context.envelope_revision
                and request.message in offered[1]
            ):
                # 用户原样点击/输入了系统上轮给出的确定性回复选项——续接语义
                # 由构造成立，免 LLM 裁决（省一整段串行识别时延）。后续
                # supplement 门仍要求 target ∈ goals，失配自然回退常规裁决。
                decision = ContinuityDecision(
                    switched=False,
                    operation="supplement_task",
                    confidence=1.0,
                    target_description=None,
                    target_task_id=offered[2],
                    clarification_prompt=None,
                    task_count=len(request.conversation_context.goals),
                )
                logger.info(
                    "Task continuity short-circuit on offered reply revision=%s target=%s",
                    offered[0],
                    offered[2],
                )
            else:
                try:
                    # decide 局部超时：模型中转偶发单调用挂起（实测 p50 5s、
                    # 尾部可达 129s）。裁决超时按异常同一出口 fail-open 回退
                    # 常规续接路径，把尾部截断而不是让整轮等待。
                    decision = await asyncio.wait_for(
                        self._task_continuity.decide(
                            conversation_id=request.conversation_id,
                            message=request.message,
                            goals=request.conversation_context.goals,
                            describe_goal=self._describe_continuity_goal,
                        ),
                        timeout=30,
                    )
                except Exception:
                    logger.warning(
                        "Task continuity decision unavailable;"
                        " fail-open to pending-goal supplement",
                        exc_info=True,
                    )
                    # 裁决异常与 none/unknown_task 同一 fail-open 语义：
                    # 待办在等澄清时，用户消息默认按首个待补目标的补充
                    # 继续。掉回完整意图重规划会让答句被重新分类成新意
                    # 图，并整轮烧穿时延（实测 180s 超时报不可用）。
                    decision = None
                    if request.conversation_context.goals:
                        supplemented_target = next(
                            (
                                goal for goal in request.conversation_context.goals
                                if getattr(goal, "status", None) == "needs_slots"
                            ),
                            request.conversation_context.goals[0],
                        )
                        decision = ContinuityDecision(
                            switched=False,
                            operation="supplement_task",
                            confidence=0.0,
                            target_description=None,
                            target_task_id=supplemented_target.task_id,
                            clarification_prompt=None,
                            task_count=len(request.conversation_context.goals),
                        )
            if (
                decision is not None
                and not decision.switched
                and decision.operation in ("supplement_task", "resume_task")
                and decision.target_task_id is not None
                and any(
                    goal.task_id == decision.target_task_id
                    for goal in request.conversation_context.goals
                )
            ):
                # 框架判定本条消息是既有任务的补充/继续——规划阶段直接以
                # 持久化目标权威重建 envelope，跳过模型二次分类。
                continuity_supplement = True
            degenerate_switch = False
            if (
                decision is not None
                and decision.switched
                and decision.operation == "create_task"
                and decision.target_task_id is None
            ):
                description = (decision.target_description or "").strip()
                echo_target = next(
                    (
                        goal for goal in request.conversation_context.goals
                        if description
                        and _continuity_description_echoes(
                            description, self._describe_continuity_goal(goal)
                        )
                    ),
                    None,
                )
                # CREATE_TASK 语义要求指明一个"新"任务；目标与新任务标识
                # 均缺失（描述为空、仅复述原文，或回显既有目标自身的描
                # 述）的跳转判定是退化结论——指向既有任务的"新建"不是跳
                # 转。放行会把"确认"这类续答交给意图引擎从零重分类，幻觉
                # 出无关意图并空耗整轮时延；携带真实新任务标识的跳转不受
                # 影响。与 operation=none 的 fail-open 出口同一语义降级为
                # 补充续接。
                degenerate_switch = (
                    not description
                    or description == request.message.strip()
                    or echo_target is not None
                )
                if degenerate_switch and echo_target is not None:
                    decision = ContinuityDecision(
                        switched=False,
                        operation="supplement_task",
                        confidence=decision.confidence,
                        target_description=None,
                        target_task_id=echo_target.task_id,
                        clarification_prompt=decision.clarification_prompt,
                        task_count=len(request.conversation_context.goals),
                    )
                    # 已直接落到回显目标的补充续接，交由上方
                    # supplement/resume 门放行；清掉退化标记避免下方
                    # none 出口二次改写覆盖掉回显目标。
                    degenerate_switch = False
            if (
                decision is not None
                and (
                    (
                        not decision.switched
                        and decision.operation in ("none", "unknown_task")
                    )
                    or degenerate_switch
                )
                and request.conversation_context.goals
            ):
                # 裁决器未识别出任何任务操作（operation=none）、给出无法
                # 判定（unknown_task——模块契约即"维持现有续接行为"）或跳
                # 转判定退化（degenerate_switch）时，与裁决异常的 fail-open
                # 出口保持同一语义：续接期消息默认按待办目标的补充继续处
                # 理。整轮回落意图重规划会让"每人100元"这类答句被重新分
                # 类成新意图，与图谱约束冲突后 blocked，待办任务的澄清链
                # 路则永远收不到用户输入。
                supplemented_target = next(
                    (
                        goal for goal in request.conversation_context.goals
                        if getattr(goal, "status", None) == "needs_slots"
                    ),
                    request.conversation_context.goals[0],
                )
                decision = ContinuityDecision(
                    switched=False,
                    operation="supplement_task",
                    confidence=decision.confidence,
                    target_description=None,
                    target_task_id=supplemented_target.task_id,
                    clarification_prompt=decision.clarification_prompt,
                    task_count=len(request.conversation_context.goals),
                )
                continuity_supplement = True
            if decision is not None and decision.switched:
                request = request.model_copy(update={
                    "conversation_context": (
                        request.conversation_context.model_copy(update={
                            "goals": [],
                            "pending_intent_id": None,
                            "pending_envelope_id": None,
                            "envelope_revision": None,
                            "intent_shape": None,
                            "slots": [],
                            "shared_slots": [],
                            "ambiguities": [],
                        })
                    ),
                })
        try:
            intent_constraints = await asyncio.to_thread(
                recall_intent_constraints,
                request, self._intent_graph, self._faq_repository, shape_assessment,
            )
        except IntentGraphError as exc:
            rail_task.cancel()
            if variable_recall_task is not None:
                variable_recall_task.cancel()
            if revision_task is not None:
                revision_task.cancel()
            await asyncio.gather(rail_task, return_exceptions=True)
            internal = AgentContractError(str(exc))
            yield failed("intent_graph_incompatible", "意图图谱与能力目录不兼容", internal)
            return
        intent_constraints = await self._widen_intent_constraints(
            request, intent_constraints
        )
        if intent_constraints:
            _report_graph_usage(len(intent_constraints))
        bound_ids = {
            capability_id
            for constraint in intent_constraints
            if constraint.execution_mode != "knowledge-only"
            for capability_id in constraint.capability_bindings
        }
        bound_plannable = [
            item for item in request.plannable_capabilities if item.id in bound_ids
        ]
        bound_executable = [
            item for item in request.executable_capabilities if item.id in bound_ids
        ]
        executable_ids = {item.id for item in bound_executable}
        guided_workflow_plan = None
        if request.entry_goal is not None:
            try:
                guided_candidate = self._intent_graph.candidate_for_intent(
                    request.entry_goal.target_intent_id,
                    score=_SCORE_SERVER_GUIDED_ENTRY,
                    evidence=["server_guided_entry"],
                )
                if guided_candidate.skill_binding is None:
                    raise BusinessLoopInvariantError(
                        "guided goal intent has no active skill binding"
                    )
                guided_workflow_plan = plan_guided_goal(request.entry_goal)
                guided_capabilities = {
                    item.capability_id for item in guided_workflow_plan.todos
                }
                if not guided_capabilities or not guided_capabilities.issubset(
                    set(guided_candidate.capability_bindings)
                ) or not guided_capabilities.issubset(bound_ids):
                    raise BusinessLoopInvariantError(
                        "guided goal capabilities are outside the registered intent"
                    )
            except (IntentGraphError, BusinessLoopInvariantError) as exc:
                rail_task.cancel()
                if variable_recall_task is not None:
                    variable_recall_task.cancel()
                if revision_task is not None:
                    revision_task.cancel()
                await asyncio.gather(rail_task, return_exceptions=True)
                internal = AgentContractError(str(exc))
                yield failed(
                    "guided_entry_invalid",
                    "推荐办理入口与当前业务能力不匹配",
                    internal,
                )
                return
        candidate_free_non_executable = (
            request.entry_goal is None
            and request.conversation_focus is None
            and not intent_constraints
        )
        # 工作记忆变量召回（已在意图图谱召回前提前启动，此处收集结果）
        recalled_variables: dict[str, str] = {}
        if variable_recall_task is not None:
            try:
                recalled_variables = await variable_recall_task
            except MemoryGatewayError:
                logger.warning("Variable recall unavailable")
                recalled_variables = {}
            except asyncio.CancelledError:
                recalled_variables = {}
        yield (
            event(
                stage="intent_graph.retrieved",
                status="completed",
                title="建行业务意图图谱召回完成",
                input_fields=["message", "pageContext"],
                output_fields=["candidateCount", "capabilityBindings"],
                metrics={
                    # candidateCount 必须与 intentGraphDetail.candidates 列表
                    # 长度一致（BFF validateTraceEvent 校验
                    # metrics.candidateCount === candidates.length）。
                    # V4 四层召回可能产生 9+ 候选，trace detail 截断到 8 项，
                    # 此处同步取截断后长度；完整召回数量由 _report_graph_usage
                    # 单独上报，不进入 BFF trace 校验链路。
                    "candidateCount": min(len(intent_constraints), 8),
                    "graphVersion": self._intent_graph.version,
                },
                intent_graph_detail={
                    "schemaVersion": "1.0",
                    "graphVersion": self._intent_graph.version,
                    # trace detail 最多 8 项（SafeIntentGraphDetail.candidates
                    # max_length=8），V4 四层召回（词法+子图+语义向量）可能产生
                    # 超过 8 个候选；trace 仅用于可观测性，截断不影响 LLM 裁决
                    # 输入（user_payload.intent_candidates 保留全部候选）。
                    "candidates": [
                        {
                            "intentId": c.intent_id,
                            "title": c.title,
                            "action": c.action,
                            "businessObject": c.business_object,
                            "executionMode": c.execution_mode,
                            "capabilityBindings": list(c.capability_bindings),
                            "score": c.score,
                            "evidence": list(c.evidence),
                            "constraint": c.constraint,
                        }
                        for c in intent_constraints[:8]
                    ],
                },
            ),
            None,
        )
        user_payload = {
            "message": request.message,
            "entry_goal": (
                request.entry_goal.model_dump(by_alias=True)
                if request.entry_goal is not None else None
            ),
            "conversation_focus": (
                request.conversation_focus.model_dump(
                    by_alias=True, exclude_none=True, mode="json"
                )
                if request.conversation_focus is not None else None
            ),
            "page_context": request.page_context.model_dump(exclude_none=True),
            "conversation_context": request.conversation_context.model_dump(
                by_alias=True, exclude_none=True
            ),
            "capability_summaries": [
                {
                    "id": item.id,
                    "title": item.title,
                    "description": (
                        item.description[:120] + "..."
                        if item.description and len(item.description) > 120
                        else item.description
                    ),
                    "risk_level": item.risk_level,
                }
                for item in bound_plannable
            ],
            "intent_candidates": [
                {
                    "intent_id": item.intent_id,
                    "title": item.title,
                    "action": item.action,
                    "business_object": item.business_object,
                    "required_shape": item.required_shape,
                    "expected_task_count": item.expected_task_count,
                    "shape_authoritative": item.shape_authoritative,
                    "supported_shapes": list(item.supported_shapes),
                    "unresolved_slot_readiness": item.unresolved_slot_readiness,
                    "execution_mode": item.execution_mode,
                    "required_slots": list(item.required_slots),
                    "capability_bindings": list(item.capability_bindings),
                    "score": item.score,
                    "evidence": list(item.evidence),
                    "constraint": item.constraint,
                    "source": item.source,
                    "disambiguation_hints": item.disambiguation_hints,
                    "slot_contract": (
                        _slim_slot_contract(item.slot_contract)
                        if item.slot_contract else None
                    ),
                }
                for item in intent_constraints
            ],
            "intent_shape_assessment": shape_assessment.to_prompt_dict(),
        }
        if recalled_variables:
            user_payload["memory_variables"] = dict(recalled_variables)
        # user_session_id 和 revision_task 已在意图图谱召回前提前启动
        projected_session_revision = (
            request.conversation_context.envelope_revision or 0
        )
        try:
            if revision_task is not None:
                expected_session_revision = await revision_task
            else:
                expected_session_revision = projected_session_revision
        except SessionRevisionConflict:
            rail_task.cancel()
            await asyncio.gather(rail_task, return_exceptions=True)
            internal = AgentContractError(
                "canonical user session revision is invalid"
            )
            yield failed(
                "agent_session_revision_conflict",
                "会话状态发生冲突，请重新发起",
                internal,
            )
            return
        envelope_revision = next_envelope_revision(
            expected_session_revision or None
        )
        attempt_session_id = None

        async def run_user_speculation():
            if request.entry_goal is not None:
                return _guided_entry_submission(request, self._intent_graph)
            if continuity_supplement:
                try:
                    submission = _authoritative_continuation_submission(
                        request, self._intent_graph
                    )
                    logger.info(
                        "Continuity authoritative envelope engaged tasks=%s",
                        len(submission["tasks"]),
                    )
                    return submission
                except Exception:
                    logger.warning(
                        "Continuity envelope build rejected; falling back to model planning",
                        exc_info=True,
                    )
            if candidate_free_non_executable or (
                shape_assessment.authoritative
                and shape_assessment.shape == "non_task"
            ):
                return _authoritative_non_task_submission(request)
            # openjiuwen 不可用时，从意图图谱召回结果构造降级 envelope
            if self._user_runner is None:
                if intent_constraints:
                    best = max(intent_constraints, key=lambda c: c.score)
                    return _authoritative_single_intent_submission(request, best)
                return _authoritative_non_task_submission(request)
            # 权威直通已关闭：词法召回可能锁定错误候选（消歧信息不足时），
            # 始终让 LLM 基于多路召回证据做最终裁决。
            # 保留 _authoritative_single_intent_submission 供后续恢复快速通道使用。
            if hasattr(self._user_runner, "run_attempt"):
                return await self._user_runner.run_attempt(
                    user_payload,
                    canonical_session_id=user_session_id,
                    request_id=request.request_id,
                )
            return await self._user_runner.run(
                user_payload,
                session_id=user_session_id,
            )

        user_attempt_task = asyncio.create_task(run_user_speculation())
        try:
            try:
                observed_rail_decision = await rail_task
                if not isinstance(observed_rail_decision, RailDecision):
                    raise TypeError("input Rail returned an invalid decision")
            except asyncio.CancelledError:
                user_attempt_task.cancel()
                await asyncio.gather(user_attempt_task, return_exceptions=True)
                raise
            except Exception:
                observed_rail_decision = RailDecision(
                    railId="finance-text-safety",
                    decision="unavailable",
                    reasonCode="guardrail_runtime_error",
                    policyVersion="unavailable",
                    responseKey="security.unavailable.default",
                    durationMs=0,
                )
            yield (
                event(
                    stage="input_rail.completed",
                    status="completed",
                    title="输入安全检查已完成",
                    input_fields=["message"],
                    output_fields=["decision"],
                    metrics={
                        "decision": observed_rail_decision.decision,
                        "reasonCode": observed_rail_decision.reason_code,
                        "policyVersion": observed_rail_decision.policy_version,
                        "parallelGroup": parallel_group,
                    },
                    duration_ms=observed_rail_decision.duration_ms,
                ),
                None,
            )
            rail_decision = _effective_rail_decision(
                observed_rail_decision,
                self._input_rail_mode,
            )
            if rail_decision.decision == "block" or (
                rail_decision.decision == "unavailable"
                and not candidate_free_non_executable
            ):
                if not user_attempt_task.done():
                    user_attempt_task.cancel()
                await asyncio.gather(user_attempt_task, return_exceptions=True)
                if hasattr(self._user_runner, "attempt_session_id"):
                    attempt_session_id = self._user_runner.attempt_session_id(
                        user_session_id,
                        request.request_id,
                    )
                if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                    await self._user_runner.rollback_attempt(attempt_session_id)
                terminal = _security_terminal_response(request, rail_decision)
                yield (
                    event(
                        stage="user_agent.rolled_back",
                        status="completed",
                        title="用户智能体推测结果已丢弃",
                        input_fields=[],
                        output_fields=[],
                        metrics={"reasonCode": rail_decision.reason_code},
                    ),
                    None,
                )
                yield (
                    event(
                        stage="security_gate.decided",
                        status="completed",
                        title="安全门禁已阻止后续办理",
                        input_fields=["railDecision"],
                        output_fields=["status"],
                        metrics={
                            "decision": rail_decision.decision,
                            "reasonCode": rail_decision.reason_code,
                            "policyVersion": rail_decision.policy_version,
                            "permitType": None,
                        },
                        duration_ms=_elapsed_ms(run_started),
                    ),
                    None,
                )
                yield (
                    event(
                        stage="run.completed",
                        status="completed",
                        title="智能服务安全结束",
                        input_fields=[],
                        output_fields=["status"],
                        metrics={
                            "status": terminal.status,
                            "taskCount": 0,
                            "stepCount": 0,
                        },
                        duration_ms=_elapsed_ms(run_started),
                        result=project_safe_run(terminal),
                    ),
                    terminal,
                )
                return

            attempt_result = await user_attempt_task
            if hasattr(attempt_result, "attempt_session_id"):
                attempt_session_id = attempt_result.attempt_session_id
                attempt_submission = attempt_result.submission
            else:
                attempt_submission = attempt_result
        except asyncio.TimeoutError:
            logger.error(
                "Business agent iteration timed out workflow_id=%s session_id=%s",
                workflow_id,
                business_session,
            )
            internal = AgentContractError("business agent iteration timed out")
            yield failed("agent_runtime_timeout", "业务智能体执行超时", internal)
            return
        except Exception:
            if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                await self._user_runner.rollback_attempt(attempt_session_id)
            internal = AgentContractError("user agent execution failed")
            yield failed("agent_runtime_failed", "用户智能体执行失败", internal)
            return
        finally:
            for pending_task in (rail_task, user_attempt_task):
                if not pending_task.done():
                    pending_task.cancel()
            await asyncio.gather(rail_task, user_attempt_task, return_exceptions=True)
        if not continuity_supplement and (
            candidate_free_non_executable or (
                shape_assessment.authoritative
                and shape_assessment.shape == "non_task"
            )
        ):
            raw_envelope = _authoritative_non_task_submission(request)
        else:
            try:
                raw_envelope = attempt_submission
            except Exception:
                internal = AgentContractError("user agent execution failed")
                yield failed("agent_runtime_failed", "用户智能体执行失败", internal)
                return
        user_repair_count = 0
        user_memory_hints: list[dict[str, Any]] = []
        allowed_intent_ids = frozenset(
            item.intent_id for item in intent_constraints
        )
        try:
            user_memory_hints = _parse_user_memory_candidate_hints(raw_envelope)
            extracted_variables = _parse_memory_variables(raw_envelope)
            envelope = _parse_envelope_submission(
                raw_envelope, request, shape_assessment,
                allowed_intent_ids=allowed_intent_ids,
                intent_graph=self._intent_graph,
            )
            envelope = envelope.model_copy(
                update={"revision": envelope_revision}
            )
        except AgentContractError as internal:
            user_repair_count = 1
            user_memory_hints = []
            extracted_variables = {}
            repair_payload = {
                **user_payload,
                "repair_feedback": {
                    "attempt": 1,
                    "max_attempts": 1,
                    "codes": ["submission_contract_invalid"],
                    "detail": str(internal),
                    "allowed_intent_ids": sorted(allowed_intent_ids),
                },
            }
            try:
                repaired_envelope = await self._user_runner.run(
                    repair_payload,
                    session_id=attempt_session_id or user_session_id,
                )
                envelope = _parse_envelope_submission(
                    repaired_envelope, request, shape_assessment,
                    allowed_intent_ids=allowed_intent_ids,
                    intent_graph=self._intent_graph,
                )
                envelope = envelope.model_copy(
                    update={"revision": envelope_revision}
                )
            except Exception as repair_error:
                if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                    await self._user_runner.rollback_attempt(attempt_session_id)
                repair_internal = (
                    repair_error
                    if isinstance(repair_error, AgentContractError)
                    else AgentContractError("user agent repair execution failed")
                )
                yield failed(
                    "user_agent_contract_violation",
                    "用户意图结果未通过契约校验",
                    repair_internal,
                )
                return

        envelope, slot_evaluations = _apply_slot_contracts(
            envelope, request, self._intent_graph, self._slot_evaluator
        )
        logger.info(
            "User slot contracts evaluated tasks=%s",
            [
                {
                    "intent_id": task.intent_id,
                    "readiness": evaluation.readiness,
                    "missing": list(evaluation.missing),
                    "ambiguous": list(evaluation.ambiguous),
                    "invalid": list(evaluation.invalid),
                    "resolved_fields": sorted(evaluation.resolved_values),
                }
                for task in envelope.tasks
                if (evaluation := slot_evaluations.get(task.task_id)) is not None
            ],
        )

        # 先补挂丢失的保留目标，再做强约束校验：校验以会话目标集为权威
        # 期望（expected_task_count=intent 注入候选），丢目标的信封在补挂前
        # 必然 intent_id_mismatch/基数不符，修复循环无从得知目标本应存在。
        envelope, slot_evaluations = _reanchor_retained_goals(
            envelope,
            slot_evaluations,
            request,
            self._intent_graph,
            self._slot_evaluator,
        )

        if (
            envelope.intent_shape == "non_task"
            and not envelope.tasks
            and intent_constraints
            and all("pending_context" in item.evidence for item in intent_constraints)
            and request.conversation_context is not None
            and request.conversation_context.goals
            and request.conversation_context.pending_envelope_id is not None
        ):
            # 待澄清期间的权威锁定仅来自 pending_context。此时模型对
            # "你好" 等输入给出的 non_task 判定是正确的：正确终态是重述
            # 挂起的澄清问题，而不是让契约校验把对话逼入 runtime_failed。
            pending_envelope = _envelope_from_conversation_context(
                request, self._intent_graph
            )
            pending_clarification = self._user_response_projector.project_clarification(
                pending_envelope.tasks[0]
            )
            pending_plan = _empty_plan(
                pending_envelope, "clarification_required", "skipped"
            )
            pending_result = RuntimeAgentRunResponse(
                agentRunId=request.agent_run_id,
                conversationId=request.conversation_id,
                status="clarification_required",
                intentTaskEnvelope=pending_envelope,
                capabilityPlan=pending_plan,
                userResponse=pending_clarification,
                conversationState=_project_conversation_state(request),
            )
            yield (
                event(
                    stage="user_agent.completed",
                    status="completed",
                    title="用户意图识别完成（待澄清）",
                    input_fields=["message", "pageContext"],
                    output_fields=["intentShape", "taskCount"],
                    metrics={
                        "intentShape": "non_task",
                        "taskCount": 0,
                        "intentSummary": "non_task",
                        "candidateConstraint": "not_applicable",
                        "userRepairCount": user_repair_count,
                    },
                    duration_ms=_elapsed_ms(user_started),
                ),
                None,
            )
            yield (
                event(
                    stage="business_agent.completed",
                    status="completed",
                    title="等待用户澄清，业务智能体未启动",
                    input_fields=["tasks"],
                    output_fields=["planStatus", "stepCount"],
                    metrics={
                        "planStatus": "clarification_required",
                        "stepCount": 0,
                        "businessRepairCount": 0,
                    },
                ),
                None,
            )
            async for item in finish(
                pending_result,
                task_count=len(pending_envelope.tasks),
                step_count=0,
            ):
                yield item
            return

        intent_violations = validate_intent_constraints(envelope, intent_constraints)
        # mock-only intent 尚不具备真实执行能力，LLM 输出的 action 可能与
        # 图谱不一致（如 LLM 输出 NAVIGATE 而图谱是 QUERY），这是预期的。
        # 跳过 action_mismatch 等约束冲突，让后续 mock-only 检查逻辑返回"暂不支持"。
        has_mock_only_constraint = any(
            item.readiness == "mock-only" for item in intent_constraints
        )
        if intent_violations and not has_mock_only_constraint:
            logger.warning(
                "User agent constraint conflict violations=%s shape=%s tasks=%s constraints=%s",
                list(intent_violations),
                envelope.intent_shape,
                [
                    {
                        "intentId": task.intent_id,
                        "action": task.action,
                        "readiness": task.execution_readiness,
                    }
                    for task in envelope.tasks
                ],
                [
                    {
                        "intentId": item.intent_id,
                        "shape": item.required_shape,
                        "taskCount": item.expected_task_count,
                        "authoritative": item.shape_authoritative,
                        "score": item.score,
                        "evidence": item.evidence,
                    }
                    for item in intent_constraints
                ],
            )
            if user_repair_count >= 1:
                internal = AgentContractError(
                    "user agent candidate constraint conflict after bounded repair: "
                    + ",".join(intent_violations)
                )
                shape_conflict = bool({
                    "intent_shape_mismatch", "task_cardinality_mismatch"
                }.intersection(intent_violations))
                if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                    await self._user_runner.rollback_attempt(attempt_session_id)
                yield failed(
                    "user_agent_shape_conflict" if shape_conflict else "user_agent_candidate_conflict",
                    "用户意图结果与权威候选不一致",
                    internal,
                )
                return
            user_repair_count = 1
            user_memory_hints = []
            repair_payload = {
                **user_payload,
                "repair_feedback": {
                    "attempt": 1,
                    "max_attempts": 1,
                    "codes": list(intent_violations),
                },
            }
            try:
                repaired_envelope = await self._user_runner.run(
                    repair_payload,
                    session_id=attempt_session_id or user_session_id,
                )
            except Exception:
                if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                    await self._user_runner.rollback_attempt(attempt_session_id)
                internal = AgentContractError("user agent repair execution failed")
                yield failed("agent_runtime_failed", "用户智能体执行失败", internal)
                return
            try:
                envelope = _parse_envelope_submission(
                    repaired_envelope, request, shape_assessment,
                    intent_graph=self._intent_graph,
                )
                envelope = envelope.model_copy(
                    update={"revision": envelope_revision}
                )
            except AgentContractError as internal:
                if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                    await self._user_runner.rollback_attempt(attempt_session_id)
                yield failed(
                    "user_agent_contract_violation",
                    "用户意图结果未通过契约校验",
                    internal,
                )
                return
            envelope, slot_evaluations = _apply_slot_contracts(
                envelope, request, self._intent_graph, self._slot_evaluator
            )
            intent_violations = validate_intent_constraints(
                envelope, intent_constraints
            )
            if intent_violations and not has_mock_only_constraint:
                internal = AgentContractError(
                    "user agent candidate constraint conflict: "
                    + ",".join(intent_violations)
                )
                shape_conflict = bool({
                    "intent_shape_mismatch", "task_cardinality_mismatch"
                }.intersection(intent_violations))
                if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                    await self._user_runner.rollback_attempt(attempt_session_id)
                yield failed(
                    "user_agent_shape_conflict" if shape_conflict else "user_agent_candidate_conflict",
                    "用户意图结果与权威候选不一致",
                    internal,
                )
                return

        if envelope.intent_shape == "non_task":
            response = self._user_response_projector.project_non_task_guidance()
            plan = _empty_plan(envelope, "unsupported", "skipped")
            result = RuntimeAgentRunResponse(
                agentRunId=request.agent_run_id,
                conversationId=request.conversation_id,
                status="unsupported",
                intentTaskEnvelope=envelope,
                capabilityPlan=plan,
                userResponse=response,
            )
            yield (
                event(
                    stage="user_agent.completed",
                    status="completed",
                    title="用户意图识别完成",
                    input_fields=["message", "pageContext"],
                    output_fields=["intentShape", "taskCount"],
                    metrics={
                        "intentShape": "non_task",
                        "taskCount": 0,
                        "intentSummary": "non_task",
                        "candidateConstraint": "not_applicable",
                        "userRepairCount": user_repair_count,
                    },
                    duration_ms=_elapsed_ms(user_started),
                ),
                None,
            )
            if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                await self._user_runner.rollback_attempt(attempt_session_id)
            yield (
                event(
                    stage="security_gate.decided",
                    status="completed",
                    title="本次请求无可执行业务",
                    input_fields=["railDecision", "intentSummary"],
                    output_fields=["status"],
                    metrics={
                        "decision": rail_decision.decision,
                        "reasonCode": rail_decision.reason_code,
                        "policyVersion": rail_decision.policy_version,
                        "permitType": None,
                    },
                ),
                None,
            )
            yield (
                event(
                    stage="business_agent.completed",
                    status="completed",
                    title="无需启动业务智能体",
                    input_fields=["tasks"],
                    output_fields=["planStatus", "stepCount"],
                    metrics={
                        "planStatus": "unsupported",
                        "stepCount": 0,
                        "businessRepairCount": 0,
                    },
                ),
                None,
            )
            async for item in finish(result, task_count=0, step_count=0):
                yield item
            return

        if rail_decision.decision == "review":
            clarification_permit = ClarificationPermit.issue(
                request_id=request.request_id,
                conversation_id=request.conversation_id,
                session_revision=expected_session_revision,
                rail_decision=rail_decision,
                ttl_seconds=self._permit_ttl_seconds,
            )
            clarification_permit.validate_binding(
                request_id=request.request_id,
                conversation_id=request.conversation_id,
                session_revision=expected_session_revision,
                required_scope="user_agent.clarify",
            )
            try:
                if not hasattr(self._user_runner, "run_safety_clarification"):
                    raise AgentContractError(
                        "user agent safety clarification is unavailable"
                    )
                raw_clarification = await self._user_runner.run_safety_clarification(
                    {
                        "safety_review": {
                            "reason_code": rail_decision.reason_code,
                            "intent_shape": envelope.intent_shape,
                            "intent_summary": _intent_summary(
                                envelope, intent_constraints
                            ),
                        }
                    },
                    session_id=attempt_session_id or user_session_id,
                )
                clarification = SafetyClarificationSubmission.model_validate(
                    raw_clarification
                )
                terminal = _security_review_response(request, clarification)
            except Exception:
                # The safety decision remains fail-closed for execution, but
                # user-facing wording is not part of the security authority.
                # A model/contract failure here must therefore fall back to a
                # stable platform clarification instead of becoming a false
                # service outage.
                terminal = _security_terminal_response(request, rail_decision)
            yield (
                event(
                    stage="user_agent.completed",
                    status="completed",
                    title="用户意图识别与安全澄清已完成",
                    input_fields=["message", "pageContext"],
                    output_fields=["intentShape", "taskCount"],
                    metrics={
                        "intentShape": envelope.intent_shape,
                        "taskCount": len(envelope.tasks),
                        "intentSummary": _intent_summary(
                            envelope, intent_constraints
                        ),
                        "candidateConstraint": (
                            "matched"
                            if len(intent_constraints) == 1
                            else "not_applicable"
                        ),
                        "userRepairCount": user_repair_count,
                        **(
                            {"ambiguityType": envelope.ambiguities[0].type}
                            if envelope.ambiguities
                            else {}
                        ),
                        **(
                            {"slotSummary": slot_summary}
                            if (
                                slot_summary := _slot_summary(
                                    envelope,
                                    slot_evaluations,
                                    intent_constraints,
                                )
                            )
                            else {}
                        ),
                    },
                    duration_ms=_elapsed_ms(user_started),
                ),
                None,
            )
            if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                await self._user_runner.rollback_attempt(attempt_session_id)
            yield (
                event(
                    stage="user_agent.rolled_back",
                    status="completed",
                    title="用户智能体推测结果已转为安全澄清",
                    input_fields=[],
                    output_fields=[],
                    metrics={"reasonCode": rail_decision.reason_code},
                ),
                None,
            )
            yield (
                event(
                    stage="security_gate.decided",
                    status="completed",
                    title="安全门禁要求补充信息",
                    input_fields=["railDecision", "intentSummary"],
                    output_fields=["status"],
                    metrics={
                        "decision": rail_decision.decision,
                        "reasonCode": rail_decision.reason_code,
                        "policyVersion": rail_decision.policy_version,
                        "permitType": "clarification",
                    },
                ),
                None,
            )
            yield (
                event(
                    stage="run.completed",
                    status="completed",
                    title="智能服务等待补充信息",
                    input_fields=[],
                    output_fields=["status"],
                    metrics={
                        "status": terminal.status,
                        "taskCount": 0,
                        "stepCount": 0,
                    },
                    duration_ms=_elapsed_ms(run_started),
                    result=project_safe_run(terminal),
                ),
                terminal,
            )
            return

        execution_permit = ExecutionPermit.issue(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            session_revision=expected_session_revision,
            rail_decision=rail_decision,
            ttl_seconds=self._permit_ttl_seconds,
        )
        try:
            execution_permit.validate_binding(
                request_id=request.request_id,
                conversation_id=request.conversation_id,
                session_revision=expected_session_revision,
                required_scope="user_agent.commit",
            )
        except PermitValidationError:
            if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                await self._user_runner.rollback_attempt(attempt_session_id)
            internal = AgentContractError("execution permit is invalid")
            yield failed("execution_permit_invalid", "安全授权已失效", internal)
            return
        try:
            if hasattr(self._user_runner, "commit_safe_state"):
                await self._user_runner.commit_safe_state(
                    {"intent_task_envelope": envelope.model_dump()},
                    canonical_session_id=user_session_id,
                    expected_revision=expected_session_revision,
                    request_id=request.request_id,
                )
            if attempt_session_id and hasattr(self._user_runner, "release_attempt"):
                await self._user_runner.release_attempt(attempt_session_id)
        except SessionRevisionConflict:
            if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                await self._user_runner.rollback_attempt(attempt_session_id)
            internal = AgentContractError("safe user state revision conflict")
            yield failed(
                "agent_session_revision_conflict",
                "会话状态发生冲突，请重新发起",
                internal,
            )
            return
        except Exception as exc:
            if attempt_session_id and hasattr(self._user_runner, "rollback_attempt"):
                await self._user_runner.rollback_attempt(attempt_session_id)
            logger.warning(
                "User session commit failed "
                "code=agent_session_commit_failed error_type=%s",
                type(exc).__name__,
            )
            internal = AgentContractError("safe user state commit failed")
            yield failed("agent_session_commit_failed", "会话状态提交失败", internal)
            return
        yield (
            event(
                stage="security_gate.decided",
                status="completed",
                title="安全门禁已放行",
                input_fields=["railDecision"],
                output_fields=["executionPermit"],
                metrics={
                    "decision": rail_decision.decision,
                    "reasonCode": rail_decision.reason_code,
                    "policyVersion": rail_decision.policy_version,
                    "permitType": "execution",
                },
            ),
            None,
        )
        yield (
            event(
                stage="user_agent.committed",
                status="completed",
                title="用户智能体安全状态已提交",
                input_fields=["intentShape", "tasks"],
                output_fields=["envelopeRevision"],
                metrics={
                    "permitType": "execution",
                    "envelopeRevision": envelope.revision,
                },
            ),
            None,
        )

        constraint_by_id = {
            item.intent_id: item for item in intent_constraints
        }
        knowledge_task_ids = {
            task.task_id
            for task in envelope.tasks
            if constraint_by_id.get(task.intent_id) is not None
            and constraint_by_id[task.intent_id].execution_mode == "knowledge-only"
        }
        all_tasks_knowledge_only = bool(envelope.tasks) and len(
            knowledge_task_ids
        ) == len(envelope.tasks)

        yield (
            event(
                stage="user_agent.completed",
                status="completed",
                title="用户意图识别完成",
                input_fields=["message", "pageContext"],
                output_fields=["intentShape", "taskCount"],
                metrics={
                    "intentShape": envelope.intent_shape,
                    "taskCount": len(envelope.tasks),
                    "intentSummary": _intent_summary(envelope, intent_constraints),
                    "candidateConstraint": (
                        "matched" if len(intent_constraints) == 1 else "not_applicable"
                    ),
                    "userRepairCount": user_repair_count,
                    **(
                        {"ambiguityType": envelope.ambiguities[0].type}
                        if envelope.ambiguities else {}
                    ),
                    **(
                        {"slotSummary": slot_summary}
                        if (
                            slot_summary := _slot_summary(
                                envelope, slot_evaluations, intent_constraints
                            )
                        )
                        else {}
                    ),
                },
                duration_ms=_elapsed_ms(user_started),
            ),
            None,
        )
        try:
            execution_permit.validate_binding(
                request_id=request.request_id,
                conversation_id=request.conversation_id,
                session_revision=expected_session_revision,
                required_scope="a2a.delegate",
            )
            if self._capability_invoker is not None:
                self._capability_invoker.consume_delegation_permit(
                    execution_permit,
                    request_id=request.request_id,
                    conversation_id=request.conversation_id,
                    session_revision=expected_session_revision,
                )
        except (PermitValidationError, AgentCapabilityInvocationError):
            internal = AgentContractError("A2A execution permit is invalid")
            yield failed("execution_permit_invalid", "安全授权已失效", internal)
            return
        yield (
            event(
                stage="delegation.created",
                status="completed",
                title="A2A 任务委派已建立",
                input_fields=["tasks"],
                output_fields=["delegationId", "dependencies"],
                metrics={
                    "taskCount": len(envelope.tasks),
                    "dependencyCount": sum(len(item.dependencies) for item in envelope.tasks),
                },
            ),
            None,
        )

        ambiguity = None
        # 当所有任务的槽位都已就绪（无缺失/歧义/无效）时，用户已回答
        # 之前的 domain ambiguity（如 amount_scope），不应再展示旧
        # ambiguity，而应直接进入业务确认流程。
        _all_tasks_ready = all(
            (evaluation := slot_evaluations.get(task.task_id)) is not None
            and evaluation.readiness == "ready"
            and not evaluation.missing
            and not evaluation.ambiguous
            and not evaluation.invalid
            for task in envelope.tasks
        )
        if envelope.ambiguities and not _all_tasks_ready:
            # 模型偶发把不存在的任务 id 写进 ambiguity.task_ids（如占位的
            # task_transfer），或声明语义退化的歧义（如单任务 amount_scope，
            # "每人还是合计"对单一收款人无意义）。不可投影的歧义无法向
            # 用户呈现，机械跳过并落入正常槽位澄清，不让整个 run 崩成
            # blocked——与编排器对投影垃圾的处理同一原则。
            known_task_ids = {task.task_id for task in envelope.tasks}
            ambiguity = next(
                (
                    item for item in envelope.ambiguities
                    if set(item.task_ids or ()) <= known_task_ids
                    and domain_ambiguity_is_projectable(
                        self._intent_graph, envelope, item
                    )
                ),
                None,
            )
            if ambiguity is None:
                logger.warning(
                    "Task ambiguities all dangling or degenerate; skipped projection task_ids=%s",
                    [item.task_ids for item in envelope.ambiguities],
                )
        if ambiguity is not None:
            try:
                ambiguity_response = project_domain_ambiguity(
                    self._intent_graph,
                    envelope,
                    ambiguity,
                    self._user_response_projector,
                    message=request.message,
                )
                if ambiguity_response is None:
                    raise UserResponseProjectionError("ambiguity has no safe projection")
                plan = _empty_plan(envelope, "clarification_required", "skipped")
                result = RuntimeAgentRunResponse(
                    agentRunId=request.agent_run_id,
                    conversationId=request.conversation_id,
                    status="clarification_required",
                    intentTaskEnvelope=envelope,
                    capabilityPlan=plan,
                    userResponse=ambiguity_response,
                    conversationState=_build_conversation_state(
                        "clarification_required", envelope, slot_evaluations
                    ),
                )
            except Exception:
                # 单条歧义投影失败不拖垮整轮 run：该声明按不可投影跳过，
                # 落入正常槽位澄清/业务流（与上方 dangling/degenerate 跳过
                # 同一原则）。根因日志保留原始抛点——上层通用错误不携带
                # 原始校验信息，排障需要真实抛点。
                logger.exception(
                    "task ambiguity projection failed; skipped ambiguity type=%s task_ids=%s",
                    getattr(ambiguity, "type", None),
                    getattr(ambiguity, "task_ids", None),
                )
            else:
                yield (
                    event(
                        stage="business_agent.completed",
                        status="completed",
                        title="等待用户澄清，业务智能体未启动",
                        input_fields=["tasks", "ambiguities"],
                        output_fields=["planStatus", "stepCount"],
                        metrics={
                            "planStatus": "clarification_required",
                            "stepCount": 0,
                            "ambiguityType": ambiguity.type,
                            "businessRepairCount": 0,
                        },
                    ),
                    None,
                )
                # Domain ambiguity（如 amount_scope）在业务智能体启动前返回，
                # 但必须持久化 workflow（awaiting_clarification）才能让
                # business-reply 端点找到工作流并恢复执行。否则 409 死循环。
                if (
                    self._capability_invoker is not None
                    and hasattr(self._capability_invoker, "persist_agent_workflow")
                    and envelope.tasks
                ):
                    try:
                        wf_id = business_workflow_id(
                            request.customer_id,
                            request.conversation_id,
                            envelope.delegation_id,
                        )
                        active_task_id = envelope.tasks[0].task_id
                        todo_items = tuple(
                            TodoSnapshotItem(
                                id=task.task_id,
                                status=(
                                    "in_progress"
                                    if task.task_id == active_task_id
                                    else "pending"
                                ),
                                depends_on=[],
                            )
                            for task in envelope.tasks
                        )
                        ambiguity_outcome = BusinessLoopOutcome(
                            business_workflow_id=wf_id,
                            status="awaiting_clarification",
                            covered_task_ids=[
                                task.task_id for task in envelope.tasks
                            ],
                            workflow_revision=1,
                            todo_revision=1,
                            active_goal_task_id=active_task_id,
                            active_todo_id=active_task_id,
                            next_action="awaiting_clarification",
                        )
                        ambiguity_snapshot = TodoLedgerSnapshot(
                            revision=1,
                            digest=TodoLedger.digest(todo_items),
                            items=todo_items,
                        )
                        await self._persist_native_resume_workflow(
                            request, envelope, wf_id,
                            {"workflowRevision": 0},
                            ambiguity_outcome, ambiguity_snapshot,
                            status="awaiting_clarification",
                            pending_checkpoint_ids=[],
                        )
                    except Exception:
                        logger.warning(
                            "Domain ambiguity workflow persistence failed;"
                            " business-reply may not resume ambiguity_type=%s",
                            getattr(ambiguity, "type", None),
                            exc_info=True,
                        )
                async for item in finish(
                    result,
                    task_count=len(envelope.tasks),
                    step_count=0,
                ):
                    yield item
                return

        if envelope.intent_shape == "ambiguous_intent":
            response_status = (
                "clarification_required"
                if envelope.intent_shape == "ambiguous_intent"
                else "unsupported"
            )
            response = (
                self._user_response_projector.project_clarification(None)
                if response_status == "clarification_required"
                else self._user_response_projector.project_non_task_guidance()
            )
            plan = _empty_plan(envelope, response_status, "skipped")
            result = RuntimeAgentRunResponse(
                agentRunId=request.agent_run_id,
                conversationId=request.conversation_id,
                status=response_status,
                intentTaskEnvelope=envelope,
                capabilityPlan=plan,
                userResponse=response,
            )
            yield (
                event(
                    stage="business_agent.completed",
                    status="completed",
                    title="无需启动业务智能体",
                    input_fields=["tasks"],
                    output_fields=["planStatus", "stepCount"],
                    metrics={
                        "planStatus": response_status,
                        "stepCount": 0,
                        "businessRepairCount": 0,
                    },
                ),
                None,
            )
            async for item in finish(result, task_count=0, step_count=0):
                yield item
            return

        faq_analysis = (
            self._faq_repository.analyze(request.message)
            if self._faq_repository is not None
            else None
        )
        is_composite = bool(
            knowledge_task_ids
            and (
                len(envelope.tasks) > 1
                or (faq_analysis is not None and len(faq_analysis.matches) > 1)
            )
        )
        if is_composite:
            try:
                composite_response = self._user_response_projector.project_faq_choices(
                    faq_analysis.matches if faq_analysis is not None else (),
                    [
                        task
                        for task in envelope.tasks
                        if task.task_id not in knowledge_task_ids
                    ],
                )
                plan = _empty_plan(envelope, "clarification_required", "skipped")
                result = RuntimeAgentRunResponse(
                    agentRunId=request.agent_run_id,
                    conversationId=request.conversation_id,
                    status="clarification_required",
                    intentTaskEnvelope=envelope,
                    capabilityPlan=plan,
                    userResponse=composite_response,
                )
            except Exception:
                logger.exception(
                    "Composite response projection failed "
                    "confirmation=%s guided_cc=%s slot_res=%s",
                    confirmation is not None,
                    guided_credit_card_facts is not None,
                    slot_reference_resolution is not None,
                )
                internal = AgentContractError("composite response projection failed")
                yield failed(
                    "user_response_projection_failed",
                    "用户响应未通过安全投影",
                    internal,
                )
                return
            yield (
                event(
                    stage="business_agent.completed",
                    status="completed",
                    title="等待用户选择，业务智能体未启动",
                    input_fields=["tasks"],
                    output_fields=["planStatus", "stepCount"],
                    metrics={
                        "planStatus": "clarification_required",
                        "stepCount": 0,
                        "slotResolution": "clarification",
                        "businessRepairCount": 0,
                    },
                ),
                None,
            )
            async for item in finish(
                result,
                task_count=len(envelope.tasks),
                step_count=0,
            ):
                yield item
            return

        task_constraints = [
            constraint_by_id.get(task.intent_id) for task in envelope.tasks
        ]
        # mock-only intent 尚不具备真实执行能力。标记 mock-only task 为
        # skipped（execution_readiness=unsupported），让非 mock-only task
        # 继续走正常流程。finish 函数会检测 skipped task 并追加"暂不支持"
        # 通知到 user_responses，用户既能看到暂不支持提示，又能继续完成
        # 其他可用功能。
        mock_only_task_ids = {
            task.task_id
            for task in envelope.tasks
            if constraint_by_id.get(task.intent_id) is not None
            and constraint_by_id[task.intent_id].readiness == "mock-only"
        }
        if mock_only_task_ids:
            for task in envelope.tasks:
                if task.task_id in mock_only_task_ids:
                    task.task_status = "skipped"
                    task.execution_readiness = "unsupported"
            all_tasks_mock_only = len(mock_only_task_ids) == len(envelope.tasks)
            if all_tasks_mock_only:
                # 全部 mock-only：直接返回 unsupported
                first_mock_task = next(
                    task for task in envelope.tasks
                    if task.task_id in mock_only_task_ids
                )
                try:
                    unsupported_response = (
                        self._user_response_projector.project_notice_for_intent(
                            "unsupported", first_mock_task.intent_id
                        )
                    )
                    plan = _empty_plan(
                        envelope, "unsupported", "mock_only"
                    )
                    result = RuntimeAgentRunResponse(
                        agentRunId=request.agent_run_id,
                        conversationId=request.conversation_id,
                        status="unsupported",
                        intentTaskEnvelope=envelope,
                        capabilityPlan=plan,
                        userResponse=unsupported_response,
                        conversationState=_build_conversation_state(
                            "unsupported", envelope, slot_evaluations
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Mock-only response projection failed "
                        "envelope_status=%s plan_status=%s tasks=%s",
                        envelope.status if envelope else None,
                        plan.status if plan else None,
                        len(envelope.tasks) if envelope else 0,
                    )
                    internal = AgentContractError(
                        "mock-only response projection failed"
                    )
                    yield failed(
                        "user_response_projection_failed",
                        "当前服务入口暂时无法安全展示",
                        internal,
                    )
                    return
                yield (
                    event(
                        stage="business_agent.completed",
                        status="completed",
                        title="当前任务暂不支持",
                        input_fields=["tasks"],
                        output_fields=["planStatus", "stepCount"],
                        metrics={
                            "planStatus": "unsupported",
                            "stepCount": 0,
                            "businessRepairCount": 0,
                        },
                    ),
                    None,
                )
                async for item in finish(result, task_count=1, step_count=0):
                    yield item
                return
            # 部分 mock-only：标记 skipped 后让非 mock-only task 继续走流程，
            # finish 函数会追加"暂不支持"通知。
        if (
            len(envelope.tasks) == 1
            and task_constraints[0] is not None
            and task_constraints[0].execution_mode == "navigation-only"
        ):
            try:
                navigation_response = (
                    self._user_response_projector.project_navigation_for_intent(
                        envelope.tasks[0].intent_id
                    )
                )
                plan = _empty_plan(
                    envelope, "navigation_only", "navigation_only"
                )
                result = RuntimeAgentRunResponse(
                    agentRunId=request.agent_run_id,
                    conversationId=request.conversation_id,
                    status="navigation_only",
                    intentTaskEnvelope=envelope,
                    capabilityPlan=plan,
                    userResponse=navigation_response,
                    conversationState=_build_conversation_state(
                        "navigation_only", envelope, slot_evaluations
                    ),
                )
            except Exception:
                logger.exception(
                    "Navigation-only response projection failed "
                    "envelope_status=%s plan_status=%s tasks=%s",
                    envelope.status if envelope else None,
                    plan.status if plan else None,
                    len(envelope.tasks) if envelope else 0,
                )
                internal = AgentContractError(
                    "navigation-only response projection failed"
                )
                yield failed(
                    "user_response_projection_failed",
                    "当前服务入口暂时无法安全展示",
                    internal,
                )
                return
            yield (
                event(
                    stage="business_agent.completed",
                    status="completed",
                    title="当前任务无需启动业务智能体",
                    input_fields=["tasks"],
                    output_fields=["planStatus", "stepCount"],
                    metrics={
                        "planStatus": "navigation_only",
                        "stepCount": 0,
                        "businessRepairCount": 0,
                    },
                ),
                None,
            )
            async for item in finish(result, task_count=1, step_count=0):
                yield item
            return

        # Knowledge-only and unavailable intents have no slot contract;
        # they must not reach decide_active_task() which requires one.
        if (
            len(envelope.tasks) == 1
            and task_constraints[0] is not None
            and task_constraints[0].execution_mode
            in ("knowledge-only", "unavailable")
        ):
            try:
                if (
                    task_constraints[0].execution_mode == "knowledge-only"
                    and _is_single_canonical_faq(
                        envelope, knowledge_task_ids, faq_analysis
                    )
                ):
                    knowledge_response = (
                        self._user_response_projector.project_faq(
                            faq_analysis.matches[0].entry
                        )
                    )
                    response_status = "succeeded"
                elif (
                    task_constraints[0].execution_mode == "knowledge-only"
                    and _is_uncovered_single_faq(
                        envelope,
                        knowledge_task_ids,
                        faq_analysis,
                        intent_constraints,
                    )
                ):
                    knowledge_response = (
                        self._user_response_projector.project_notice_for_intent(
                            "faq_uncovered", envelope.tasks[0].intent_id
                        )
                    )
                    response_status = "unsupported"
                else:
                    knowledge_response = (
                        self._user_response_projector.project_non_task_guidance()
                    )
                    response_status = "navigation_only"
                plan = _empty_plan(
                    envelope, "navigation_only", "knowledge_only"
                )
                result = RuntimeAgentRunResponse(
                    agentRunId=request.agent_run_id,
                    conversationId=request.conversation_id,
                    status=response_status,
                    intentTaskEnvelope=envelope,
                    capabilityPlan=plan,
                    userResponse=knowledge_response,
                    conversationState=_build_conversation_state(
                        response_status, envelope, slot_evaluations
                    ),
                )
            except Exception:
                logger.exception(
                    "Knowledge-only response projection failed "
                    "response_status=%s plan_status=%s knowledge_tasks=%s",
                    response_status,
                    plan.status if plan else None,
                    len(knowledge_task_ids),
                )
                internal = AgentContractError(
                    "knowledge-only response projection failed"
                )
                yield failed(
                    "user_response_projection_failed",
                    "当前服务入口暂时无法安全展示",
                    internal,
                )
                return
            yield (
                event(
                    stage="business_agent.completed",
                    status="completed",
                    title="当前任务无需启动业务智能体",
                    input_fields=["tasks"],
                    output_fields=["planStatus", "stepCount"],
                    metrics={
                        "planStatus": response_status,
                        "stepCount": 0,
                        "businessRepairCount": 0,
                    },
                ),
                None,
            )
            async for item in finish(result, task_count=1, step_count=0):
                yield item
            return

        candidate_by_task = {
            task.task_id: self._intent_graph.candidate_for_intent(
                task.intent_id, score=0, evidence=["task_slot_runtime"]
            )
            for task in envelope.tasks
        }
        decision_by_task: dict[str, TaskSlotDecision] = {}
        try:
            for task in envelope.tasks:
                # mock-only task 已被标记为 skipped，无需 slot 决策，
                # 否则会因缺少 slot evaluation 或 candidate 而出错。
                if task.task_status == "skipped":
                    continue
                evaluation = slot_evaluations.get(task.task_id)
                if evaluation is None:
                    # 纯查询（read-execute 无槽位契约）不需要 slot evaluation，
                    # 直接创建 decision 走 MCP 读路径。
                    candidate = candidate_by_task[task.task_id]
                    if candidate.execution_mode == "read-execute" and candidate.slot_contract is None:
                        if task.action != "QUERY" or len(candidate.capability_bindings) != 1:
                            raise TaskSlotRuntimeError(
                                "read-execute intent must bind exactly one query capability"
                            )
                        decision_by_task[task.task_id] = TaskSlotDecision(
                            task_id=task.task_id,
                            slot_name=None,
                            slot_type=None,
                            slot_label=None,
                            resolver_capability_id=None,
                            resolved_field=None,
                            execution_capability_id=candidate.capability_bindings[0],
                            response_policy="query_result",
                            readiness="execute",
                        )
                    continue
                candidate = candidate_by_task[task.task_id]
                if (
                    candidate.slot_contract is None
                    and candidate.execution_mode
                    in ("knowledge-only", "unavailable")
                ):
                    continue
                decision_by_task[task.task_id] = decide_active_task(
                    task=task,
                    candidate=candidate,
                    evaluation=evaluation,
                    intent_shape=envelope.intent_shape,
                )
        except TaskSlotRuntimeError as exc:
            internal = AgentContractError(str(exc))
            yield failed(
                "task_slot_contract_invalid",
                "当前业务槽位契约不可用",
                internal,
            )
            return
        # mock-only task 已被标记为 skipped，不应作为活动任务。选取第一个
        # 非 skipped task 作为初始活动任务，否则 initial_decision 为 None，
        # graph_reference_resolvable 等条件全部失效，业务循环会在
        # needs_slots 时误启动并因能力白名单不完整而失败。
        initial_active_task = next(
            (task for task in envelope.tasks if is_intent_task_in_play(task.task_status)),
            envelope.tasks[0],
        )
        initial_decision = decision_by_task.get(initial_active_task.task_id)
        if initial_decision is not None and initial_active_task.action == "QUERY":
            required_read_capabilities = {initial_decision.execution_capability_id}
            if initial_decision.readiness == "resolve":
                if initial_decision.resolver_capability_id is None:
                    required_read_capabilities.clear()
                else:
                    required_read_capabilities.add(
                        initial_decision.resolver_capability_id
                    )
            if not required_read_capabilities.issubset(executable_ids):
                internal = AgentContractError(
                    "active query capabilities are not executable for this run"
                )
                yield failed(
                    "query_capability_unavailable",
                    "查询能力暂时不可用",
                    internal,
                )
                return
        yield (
            event(
                stage="business_agent.started",
                status="running",
                title="业务智能体开始规划",
                input_fields=["intentTaskEnvelope", "capabilities"],
                output_fields=[],
                metrics={"capabilityCount": len(bound_plannable)},
            ),
            None,
        )

        business_started = time.monotonic()
        workflow_id = business_workflow_id(
            request.customer_id,
            request.conversation_id,
            envelope.delegation_id,
        )
        business_session = business_session_id(
            request.customer_id, workflow_id
        )
        workflow_persisted_revision = 0
        # Node 侧台账版本（恢复时取得）。模型循环 outcome 的 todo_revision
        # 只反映它本轮会话内的认知，澄清轮原样透传会小于存量值，被 Node
        # 单调性校验 409 打成整轮失败；持久化时按存量值托底。
        stored_todo_revision = 0
        restored_workflow: dict[str, Any] | None = None
        authoritative_restore: dict[str, Any] | None = None
        native_loop_mode = hasattr(self._business_runner, "load_todo_ledger")
        primary_intent_id = envelope.tasks[0].intent_id if envelope.tasks else None
        # Start memory recall in parallel with workflow restoration
        _recall_task_mem = asyncio.ensure_future(
            self._recall_task_memories(request, envelope)
        )
        _recall_user_mem = asyncio.ensure_future(
            self._recall_user_memories(request, intent_id=primary_intent_id)
        )
        if (
            native_loop_mode
            and _should_restore_business_workflow(
                request.conversation_context, envelope
            )
            and self._capability_invoker is not None
            and hasattr(self._business_runner, "restore_todo_ledger")
        ):
            try:
                try:
                    restored_workflow = (
                        await self._capability_invoker.load_agent_workflow(
                            agent_run_id=request.agent_run_id,
                            business_workflow_id=workflow_id,
                        )
                    )
                except AgentCapabilityInvocationError as exc:
                    if exc.code != "agent_workflow_not_found":
                        raise
                    # 澄清期间尚未发生业务委托：无工作流可恢复，按全新委托继续。
                    restored_workflow = None
                if restored_workflow is not None:
                    # 第二轮（续跑）envelope 从会话上下文重新构造，不保留
                    # 第一轮的 skipped 状态。这里根据持久化 workflow 的
                    # delegatedTaskIds 反向标记：不在委托集中的 task 视为
                    # skipped（mock-only），让后续所有 task_status != "skipped"
                    # 过滤逻辑一致生效。
                    persisted_delegated_ids = set(
                        str(item) for item in restored_workflow.get("delegatedTaskIds", [])
                    )
                    if persisted_delegated_ids:
                        for task in envelope.tasks:
                            if (
                                task.task_id not in persisted_delegated_ids
                                and is_intent_task_in_play(task.task_status)
                            ):
                                task.task_status = "skipped"
                                task.execution_readiness = "unsupported"
                    delegated_task_ids = [
                        item.task_id for item in envelope.tasks
                        if is_intent_task_in_play(item.task_status)
                    ]
                    if restored_workflow["delegatedTaskIds"] != delegated_task_ids:
                        # 接续 envelope 可能只覆盖未交付的剩余目标（已交付
                        # 任务被会话状态过滤），合法收缩放行，漂移仍拒绝。
                        _require_legitimate_goal_narrowing(
                            restored_workflow, delegated_task_ids
                        )
                    if restored_workflow["status"] not in {
                        "running", "awaiting_clarification", "waiting_external"
                    }:
                        raise AgentContractError(
                            "persisted workflow cannot continue from chat"
                        )
                    authoritative_restore, _restored_snapshot = (
                        _authoritative_node_todo_restore(
                            restored_workflow,
                            resume_user_reply=True,
                        )
                    )
                    workflow_persisted_revision = restored_workflow["workflowRevision"]
                    stored_todo_revision = restored_workflow["todoRevision"]
            except Exception as exc:
                logger.warning(
                    "Business workflow restoration rejected code=node_workflow_restore_invalid error_type=%s msg=%s",
                    _safe_restore_error_type(exc),
                    str(exc)[:200],
                )
                # 优雅降级必须保持 CAS 时钟诚实：工作流已存在时，恢复被拒
                # 否定的只是"从聊天续跑"的当前合法性，不是它的持久化事实。
                # revision 若归零，后续 checkpoint 持久化 expected=0 对
                # stored>=1 必然 409，把可恢复的对话直接打成整轮失败
                # （智能体执行损失）。保留真实 revision 让降级后的
                # persist 以 stored+1 原子顶替。
                if restored_workflow is not None:
                    workflow_persisted_revision = restored_workflow["workflowRevision"]
                    stored_todo_revision = restored_workflow["todoRevision"]
                else:
                    workflow_persisted_revision = 0
                # 优雅降级：恢复失败时不中断用户请求，按全新工作流继续执行。
                restored_workflow = None
                authoritative_restore = None
        task_memory, user_memory = await asyncio.gather(
            _recall_task_mem, _recall_user_mem
        )
        memory_input_defaults_by_task: dict[
            str, dict[str, dict[str, Any]]
        ] = {}
        memory_default_projector = getattr(
            self._memory_gateway, "capability_input_defaults", None
        )
        if callable(memory_default_projector) and task_memory.outcome == "applied":
            for wrapper in task_memory.context_packs:
                task_id = wrapper.get("taskId")
                intent_id = wrapper.get("intentId")
                context_pack = wrapper.get("contextPack")
                if not all(isinstance(item, str) for item in (task_id, intent_id)):
                    continue
                if not isinstance(context_pack, dict):
                    continue
                defaults = memory_default_projector(intent_id, context_pack)
                if defaults:
                    memory_input_defaults_by_task[task_id] = defaults
        memory_input_default_count = sum(
            len(values)
            for capabilities in memory_input_defaults_by_task.values()
            for values in capabilities.values()
        )

        def memory_defaults_for(task_id: str) -> dict[str, dict[str, Any]] | None:
            matched = memory_input_defaults_by_task.get(task_id)
            if matched is not None:
                return matched
            if len(envelope.tasks) == 1 and len(memory_input_defaults_by_task) == 1:
                return next(iter(memory_input_defaults_by_task.values()))
            return None
        if self._memory_gateway is not None:
            yield (
                event(
                    stage="memory.task_context_recalled",
                    status="completed",
                    title="任务记忆按策略召回完成",
                    input_fields=["intentTaskEnvelope"],
                    output_fields=["memoryContext"],
                    metrics={
                        **task_memory.trace_metrics(),
                        "taskCount": len(envelope.tasks),
                        "inputDefaultCount": memory_input_default_count,
                    },
                    memory_recall=task_memory.trace_detail(scope="task"),
                ),
                None,
            )
        # 用户记忆召回已与任务记忆召回并行执行
        if self._memory_gateway is not None:
            yield (
                event(
                    stage="memory.user_context_recalled",
                    status="completed",
                    title="用户记忆按意图策略召回",
                    input_fields=["intentTaskEnvelope"],
                    output_fields=["memoryContext"],
                    metrics={
                        **user_memory.trace_metrics(),
                        "intentId": primary_intent_id,
                    },
                    memory_recall=user_memory.trace_detail(scope="user"),
                ),
                None,
            )
        business_payload = {
            "business_workflow_id": workflow_id,
            "intent_task_envelope": envelope.model_dump(),
            "slot_resolution_context": {
                "source_message": request.message,
                "page_context": request.page_context.model_dump(exclude_none=True),
            },
            "capabilities": [
                item.model_dump(by_alias=True) for item in bound_plannable
            ],
        }
        if task_memory.outcome == "applied":
            business_payload["memory_context_packs"] = list(task_memory.context_packs)
        if user_memory.outcome == "applied":
            business_payload["user_memory_context_packs"] = list(
                user_memory.context_packs
            )
        if guided_workflow_plan is not None:
            business_payload["guided_workflow_plan"] = {
                "target_intent_id": guided_workflow_plan.target_intent_id,
                "workflow_template_id": request.entry_goal.workflow_template_id,
                "todos": [
                    {
                        "item_id": item.item_id,
                        "capability_id": item.capability_id,
                        "requires_confirmation": item.requires_confirmation,
                        "input_bindings": copy.deepcopy(item.input_bindings),
                    }
                    for item in guided_workflow_plan.todos
                ],
            }
        if authoritative_restore is not None:
            business_payload[AUTHORITATIVE_NODE_WORKFLOW_KEY] = (
                authoritative_restore
            )
        if slot_evaluations:
            business_payload["slot_assessment"] = _slot_assessment_payload(
                slot_evaluations
            )
        initial_runtime_phase = runtime_capability_phase(initial_decision)
        if initial_runtime_phase is not None:
            business_payload["runtime_capability_phase"] = initial_runtime_phase
        # 不按 readiness=="clarify" 门控：连续性层补录（supplement）等路径
        # 同样会把多实体答案送进单实体槽位（如"张帅李四"→单个 payee 槽），
        # 装填约束必须全程在业务载荷里，模型才有 replan 申报的确定依据。
        fit_contract = self._clarification_fit_contract(initial_active_task)
        if fit_contract is not None:
            business_payload["clarification_fit_contract"] = fit_contract
        elif slot_evaluations and all(
            evaluation.readiness == "ready"
            and not evaluation.missing
            and not evaluation.ambiguous
            and not evaluation.invalid
            for evaluation in slot_evaluations.values()
        ):
            # 与澄清契约互补的执行声明：槽位契约（图谱权威）判定全部就绪时
            # 显式声明"禁止澄清、直接执行"。弱模型否则会对已解析槽位重复
            # 追问（如对 payee 已就绪的任务再问收款人）。
            business_payload["execution_directive"] = {
                "allTasksReady": True,
                "rule": (
                    "槽位契约判定所有任务的必需槽位已就绪且无未决歧义。"
                    "禁止提交 status=awaiting_clarification，禁止向用户追问任何"
                    "槽位；立即按执行绑定调用能力完成任务，并通过"
                    " submit_business_loop_outcome 提交执行结果。"
                ),
            }
        skill_trace_view = None
        skill_trace_started = None
        try:
            capabilities_by_task = {
                task.task_id: frozenset(
                    active_task_capability_ids(
                        task,
                        candidate_by_task[task.task_id],
                        decision_by_task[task.task_id],
                    ).intersection(executable_ids)
                    if decision_by_task.get(task.task_id) is not None
                    else set(candidate_by_task[task.task_id].capability_bindings)
                    .intersection(executable_ids)
                )
                for task in envelope.tasks
            }
            # 多任务场景下，LLM 可能在当前活跃任务的迭代中预读取后续任务
            # 所需的能力（如查余额迭代中预读收款方列表）。per-task allowlist
            # 会阻止这种合法的跨任务预读，导致 node_gateway 拒绝调用、
            # 业务循环 Todo 覆盖率校验失败。使用所有任务能力并集放宽边界。
            all_task_capabilities = set().union(
                *capabilities_by_task.values()
            ) if capabilities_by_task else set()
            active_before_loop = (
                restored_workflow["activeGoalTaskId"]
                if restored_workflow is not None
                else initial_active_task.task_id
            )
            business_execution_allowlist = all_task_capabilities
            loop_advance = None
            deterministic_guided_read = bool(
                guided_workflow_plan is not None
                and guided_workflow_plan.todos
                and all(
                    not item.requires_confirmation
                    for item in guided_workflow_plan.todos
                )
            )
            # 纯查询（无槽位契约）直接走 MCP 读能力，不启动业务智能体/skill。
            executable_by_id = {item.id: item for item in bound_executable}
            deterministic_read_inputs: dict[str, dict[str, Any]] = {}
            for task in envelope.tasks:
                decision = decision_by_task.get(task.task_id)
                evaluation = slot_evaluations.get(task.task_id)
                capability = (
                    executable_by_id.get(decision.execution_capability_id)
                    if decision is not None else None
                )
                if (
                    task.action != "QUERY"
                    or candidate_by_task[task.task_id].execution_mode
                    != "read-execute"
                    or candidate_by_task[task.task_id].orchestration_mode
                    == "agent"
                    or decision is None
                    or decision.readiness != "execute"
                    or evaluation is None
                    or capability is None
                ):
                    continue
                properties = capability.input_schema.get("properties", {})
                required = capability.input_schema.get("required", [])
                if not isinstance(properties, dict) or not isinstance(required, list):
                    continue
                read_input = {
                    key: copy.deepcopy(evaluation.resolved_values[key])
                    for key in properties
                    if key in evaluation.resolved_values
                }
                if all(key in read_input for key in required):
                    deterministic_read_inputs[task.task_id] = read_input
            # Fully-bound atomic reads use graph-normalized inputs. The model
            # cannot substitute another period, account, card, or provider
            # argument after the platform has validated the task contract.
            deterministic_read = bool(
                envelope.tasks
                and len(deterministic_read_inputs) == len(envelope.tasks)
            )
            if not deterministic_read and not deterministic_guided_read:
                execution_view_resolver = getattr(
                    self._business_runner, "execution_view_for_payload", None
                )
                if callable(execution_view_resolver):
                    from app.resource_registry import ResourceRegistryError

                    try:
                        resolved_view = execution_view_resolver(business_payload)
                    except ResourceRegistryError as exc:
                        if exc.code != "skill_binding_missing":
                            raise
                        # 委派前最后一道契约防线：注册表没有任何 Skill 声明受理
                        # 该意图时，正确终态是让用户补充缺失的必填引用，
                        # 而不是以 runtime_failed 告终。
                        unresolved_task = envelope.tasks[0]
                        clarification = self._user_response_projector.project_clarification(
                            unresolved_task,
                            slot_evaluation=slot_evaluations.get(
                                unresolved_task.task_id
                            ),
                        )
                        plan = _empty_plan(
                            envelope, "clarification_required", "skill_binding_missing"
                        )
                        result = RuntimeAgentRunResponse(
                            agentRunId=request.agent_run_id,
                            conversationId=request.conversation_id,
                            status="clarification_required",
                            intentTaskEnvelope=envelope,
                            capabilityPlan=plan,
                            userResponse=clarification,
                            conversationState=_build_conversation_state(
                                "clarification_required", envelope, slot_evaluations
                            ),
                        )
                        logger.warning(
                            "Delegation blocked without skill binding; "
                            "projecting clarification intent=%s",
                            unresolved_task.intent_id,
                        )
                        yield (
                            event(
                                stage="business_agent.completed",
                                status="completed",
                                title="等待用户补充信息，业务智能体未启动",
                                input_fields=["tasks"],
                                output_fields=["planStatus", "stepCount"],
                                metrics={
                                    "planStatus": "clarification_required",
                                    "stepCount": 0,
                                    "businessRepairCount": 0,
                                },
                            ),
                            None,
                        )
                        async for item in finish(
                            result,
                            task_count=len(envelope.tasks),
                            step_count=0,
                        ):
                            yield item
                        return
                    if resolved_view.skill_ids:
                        skill_trace_view = resolved_view
                if skill_trace_view is not None:
                    skill_trace_started = time.monotonic()
                    primary_skill = skill_trace_view.skills[0]
                    yield (
                        event(
                            stage="business_skill.started",
                            status="running",
                            title="业务 Skill 执行视图已装载",
                            input_fields=[],
                            output_fields=[],
                            metrics={
                                "skillId": primary_skill.id,
                                "skillVersion": primary_skill.version,
                                "skillCount": len(skill_trace_view.skill_ids),
                                "capabilityCount": len(skill_trace_view.capability_ids),
                                "providerCount": len(skill_trace_view.providers),
                            },
                        ),
                        None,
                    )
            # 当活动槽位 readiness="resolve" 时，槽位只能由 graph reference
            # resolver chain 通过 capability 调用解析（如 party_reference →
            # bank.payee.list → resolve_party_reference），LLM 无法解析引用
            # 类型。跳过业务智能体 LLM 循环，让 resolver chain 直接运行。
            # 多任务信封（如 查余额+转账）同样适用：链只作用于当前活动
            # 任务，其余任务保持 pending 由台账权威推进，LLM 循环对引用
            # 解析同样无能为力，只会空耗迭代预算。
            graph_reference_resolvable = (
                envelope.tasks
                and initial_decision is not None
                and initial_decision.readiness == "resolve"
                and initial_decision.slot_type in {
                    "account_reference", "card_reference", "product_reference",
                    "holding_reference", "party_reference",
                }
                and initial_decision.resolver_capability_id is not None
                and not (
                    request.entry_goal is not None
                    and request.entry_goal.target_intent_id
                    == "ccb.home.repayment.repay"
                )
            )
            if native_loop_mode and deterministic_read:
                read_invocations: list[dict[str, Any]] = []
                read_task_invocations: list[TaskInvocation] = []
                read_todos = tuple(
                    TodoSnapshotItem(
                        id=task.task_id, status="completed", depends_on=[]
                    )
                    for task in envelope.tasks
                )
                for task in envelope.tasks:
                    decision = decision_by_task[task.task_id]
                    completed = await self._invoke_read_once(
                        request.agent_run_id,
                        decision.execution_capability_id,
                        deterministic_read_inputs[task.task_id],
                        executable_ids,
                    )
                    read_invocations = _merge_invocations(
                        read_invocations, completed
                    )
                    read_task_invocations.extend(
                        TaskInvocation(
                            task_id=task.task_id,
                            capability_id=item["capability_id"],
                            input=copy.deepcopy(item["input"]),
                            output=copy.deepcopy(item["output"]),
                        )
                        for item in completed
                    )
                read_snapshot = TodoLedgerSnapshot(
                    revision=1,
                    digest=TodoLedger.digest(read_todos),
                    items=read_todos,
                )
                read_outcome = BusinessLoopOutcome.model_validate({
                    "schema_version": "2.0",
                    "business_workflow_id": workflow_id,
                    "workflow_revision": workflow_persisted_revision + 1,
                    "status": "completed",
                    "covered_task_ids": [task.task_id for task in envelope.tasks],
                    "active_goal_task_id": None,
                    "active_todo_id": None,
                    "todo_revision": 1,
                    "next_action": None,
                })
                loop_advance = BusinessLoopAdvance(
                    outcome=read_outcome,
                    snapshot=read_snapshot,
                    successful_invocations=tuple(read_invocations),
                    task_invocations=tuple(read_task_invocations),
                    completed_task_ids=tuple(
                        task.task_id for task in envelope.tasks
                    ),
                )
                raw_plan = read_outcome.model_dump()
                successful_invocations = read_invocations
            elif native_loop_mode and deterministic_guided_read:
                guided_invocations: list[dict[str, Any]] = []
                guided_task_invocations: list[TaskInvocation] = []
                guided_task_id = envelope.tasks[0].task_id
                guided_todos = tuple(
                    TodoSnapshotItem(
                        id=item.item_id, status="completed", depends_on=[]
                    )
                    for item in guided_workflow_plan.todos
                )
                for todo in guided_workflow_plan.todos:
                    completed = await self._invoke_read_once(
                        request.agent_run_id,
                        todo.capability_id,
                        copy.deepcopy(todo.input_bindings),
                        executable_ids,
                    )
                    guided_invocations = _merge_invocations(
                        guided_invocations, completed
                    )
                    guided_task_invocations.extend(
                        TaskInvocation(
                            task_id=guided_task_id,
                            capability_id=item["capability_id"],
                            input=copy.deepcopy(item["input"]),
                            output=copy.deepcopy(item["output"]),
                        )
                        for item in completed
                    )
                guided_snapshot = TodoLedgerSnapshot(
                    revision=1,
                    digest=TodoLedger.digest(guided_todos),
                    items=guided_todos,
                )
                guided_outcome = BusinessLoopOutcome.model_validate({
                    "schema_version": "2.0",
                    "business_workflow_id": workflow_id,
                    "workflow_revision": workflow_persisted_revision + 1,
                    "status": "completed",
                    "covered_task_ids": [guided_task_id],
                    "active_goal_task_id": None,
                    "active_todo_id": None,
                    "todo_revision": 1,
                    "next_action": None,
                })
                loop_advance = BusinessLoopAdvance(
                    outcome=guided_outcome,
                    snapshot=guided_snapshot,
                    successful_invocations=tuple(guided_invocations),
                    task_invocations=tuple(guided_task_invocations),
                    completed_task_ids=(guided_task_id,),
                )
                raw_plan = guided_outcome.model_dump()
                successful_invocations = guided_invocations
            elif native_loop_mode and graph_reference_resolvable:
                # readiness="resolve" 的槽位只能由 resolver chain 解析（如
                # party_reference → bank.payee.list → resolve_party_reference）。
                # 构造确定性 outcome 让下游 resolver chain 直接运行，跳过
                # 业务智能体 LLM 循环（省 4+ 次 LLM 调用）。
                resolvable_task_id = initial_active_task.task_id
                # 台账必须覆盖全部 delegated 任务（持久化网关 + goal 推导
                # 都以完整台账为准）：已交付结果的任务按信封状态标记
                # completed，活动任务 in_progress，其余 pending。
                resolvable_todos = tuple(
                    TodoSnapshotItem(
                        id=task.task_id,
                        status=(
                            "completed"
                            if task.task_status == "completed"
                            else "in_progress"
                            if task.task_id == resolvable_task_id
                            else "pending"
                        ),
                        depends_on=[],
                    )
                    for task in envelope.tasks
                )
                resolvable_snapshot = TodoLedgerSnapshot(
                    revision=max(1, workflow_persisted_revision + 1),
                    digest=TodoLedger.digest(resolvable_todos),
                    items=resolvable_todos,
                )
                resolvable_outcome = BusinessLoopOutcome.model_validate({
                    "schema_version": "2.0",
                    "business_workflow_id": workflow_id,
                    "workflow_revision": workflow_persisted_revision + 1,
                    "status": "awaiting_clarification",
                    "covered_task_ids": [task.task_id for task in envelope.tasks],
                    "active_goal_task_id": resolvable_task_id,
                    "active_todo_id": resolvable_task_id,
                    "todo_revision": max(1, workflow_persisted_revision + 1),
                    "next_action": "resolve_graph_reference",
                })
                loop_advance = BusinessLoopAdvance(
                    outcome=resolvable_outcome,
                    snapshot=resolvable_snapshot,
                    successful_invocations=(),
                    task_invocations=(),
                    completed_task_ids=(),
                )
                raw_plan = resolvable_outcome.model_dump()
                successful_invocations = []
            elif native_loop_mode:
                coordinator = BusinessLoopCoordinator(
                    self._business_runner,
                    max_iterations=max(1, min(8, int(os.getenv("AI_BANK_BUSINESS_LOOP_ITERATIONS", "4")))),
                )

                async def run_loop_iteration(payload, *, session_id):
                    iteration_active_task_id = (
                        payload.get("loop_feedback", {}).get(
                            "active_goal_task_id"
                        )
                        or active_before_loop
                    )
                    iteration_payload = dict(payload)
                    phase = runtime_capability_phase(
                        decision_by_task.get(iteration_active_task_id)
                    )
                    if phase is not None:
                        iteration_payload["runtime_capability_phase"] = phase
                    return await self._run_business(
                        iteration_payload,
                        session_id=session_id,
                        agent_run_id=request.agent_run_id,
                        execution_allowlist=all_task_capabilities,
                        input_defaults=memory_defaults_for(iteration_active_task_id),
                    )

                if restored_workflow is None:
                    # 全新工作流尚无 Node 侧权威 Todo 快照，planning 阶段模型
                    # 可能已向该会话台账写入自造 id 的待办；持久化网关要求
                    # todo id ∈ delegated 集，循环起点先按 delegated 集权威
                    # 初始化。content 取图谱 title 写成原生格式——裸 id 台账
                    # 会被模型整体重写为自造 id 重新触发 coverage 冲突。
                    # 已有权威快照的续跑路径由 restore_authoritative 流程对齐。
                    try:
                        # skipped task（mock-only）不进入业务智能体台账，
                        # 否则编排器会把它当作未完成 goal 标记为 failed。
                        delegated_tasks = [
                            task for task in envelope.tasks
                            if is_intent_task_in_play(task.task_status)
                        ]
                        delegated_ids = {task.task_id for task in delegated_tasks}
                        current_ledger = self._business_runner.load_todo_ledger(
                            business_session, revision=1
                        )
                        if {item.id for item in current_ledger.items} != delegated_ids:
                            self._business_runner.seed_todo_ledger(
                                business_session,
                                [
                                    {
                                        "id": task.task_id,
                                        "status": (
                                            "in_progress" if index == 0 else "pending"
                                        ),
                                        "dependsOn": [],
                                        "title": (
                                            candidate_by_task[task.task_id].title
                                            if candidate_by_task.get(task.task_id)
                                            is not None
                                            else task.task_id
                                        ),
                                    }
                                    for index, task in enumerate(delegated_tasks)
                                ],
                            )
                            logger.warning(
                                "Business workflow fresh Todo ledger seeded from"
                                " delegated goals session=%s foreign=%s",
                                business_session,
                                [item.id for item in current_ledger.items],
                            )
                    except Exception:
                        logger.warning(
                            "Business workflow fresh Todo ledger seed skipped",
                            exc_info=True,
                        )
                loop_advance = await coordinator.advance(
                    envelope=envelope,
                    workflow_id=workflow_id,
                    session_id=business_session,
                    initial_payload=business_payload,
                    run_iteration=run_loop_iteration,
                    expected_workflow_revision=workflow_persisted_revision,
                    previous_active_task_id=(
                        restored_workflow["activeGoalTaskId"]
                        if restored_workflow is not None else None
                    ),
                    expected_todo_revision=(
                        restored_workflow["todoRevision"]
                        if restored_workflow is not None else 0
                    ),
                    capabilities_by_task=capabilities_by_task,
                )
                raw_plan = loop_advance.outcome.model_dump()
                successful_invocations = list(loop_advance.successful_invocations)
                # 过时澄清门：槽位契约（图谱权威）判定全部就绪而循环仍提交
                # 澄清——澄清与权威状态矛盾（弱模型对已解析槽位重复追问）。
                # 仅当本轮零能力调用（无副作用可重放）时，带修复反馈重驱一次
                # 循环；常规路径零额外模型调用。
                logger.debug(
                    "Stale clarification gate check status=%s invocations=%s evals=%s",
                    loop_advance.outcome.status,
                    len(successful_invocations),
                    {
                        task_id: (
                            evaluation.readiness,
                            sorted(evaluation.missing),
                            sorted(evaluation.ambiguous),
                            sorted(evaluation.invalid),
                        )
                        for task_id, evaluation in slot_evaluations.items()
                    } if slot_evaluations else {},
                )
                if (
                    loop_advance.outcome.status == "awaiting_clarification"
                    and not successful_invocations
                    and slot_evaluations
                    and all(
                        evaluation.readiness == "ready"
                        and not evaluation.missing
                        and not evaluation.ambiguous
                        and not evaluation.invalid
                        for evaluation in slot_evaluations.values()
                    )
                ):
                    logger.warning(
                        "Stale clarification gate engaged tasks=%s",
                        sorted(slot_evaluations),
                    )
                    repair_payload = {
                        **business_payload,
                        "repair_feedback": {
                            "attempt": 1,
                            "max_attempts": 1,
                            "codes": ["stale_clarification"],
                            "active_goal_task_id": loop_advance.outcome.active_goal_task_id,
                            "active_todo_id": loop_advance.outcome.active_todo_id,
                            "rule": (
                                "此前提交的澄清已过时：槽位契约判定所有任务必需槽位"
                                "已就绪且无未决歧义，禁止再次澄清或追问；立即按执行"
                                "绑定调用能力完成任务并提交执行结果。"
                            ),
                        },
                        "workflow_control": {
                            "expected_workflow_revision": workflow_persisted_revision,
                            "next_workflow_revision": workflow_persisted_revision + 1,
                            "execution_policy": "sequential",
                        },
                    }
                    try:
                        loop_advance = await coordinator.advance(
                            envelope=envelope,
                            workflow_id=workflow_id,
                            session_id=business_session,
                            initial_payload=repair_payload,
                            run_iteration=run_loop_iteration,
                            expected_workflow_revision=workflow_persisted_revision,
                            previous_active_task_id=(
                                restored_workflow["activeGoalTaskId"]
                                if restored_workflow is not None else None
                            ),
                            expected_todo_revision=(
                                restored_workflow["todoRevision"]
                                if restored_workflow is not None else 0
                            ),
                            capabilities_by_task=capabilities_by_task,
                        )
                        raw_plan = loop_advance.outcome.model_dump()
                        successful_invocations = list(
                            loop_advance.successful_invocations
                        )
                    except Exception:
                        logger.warning(
                            "Stale clarification re-drive failed", exc_info=True
                        )
            else:
                raw_plan, successful_invocations = await self._run_business(
                    business_payload,
                    session_id=business_session,
                    agent_run_id=request.agent_run_id,
                    execution_allowlist=business_execution_allowlist,
                    input_defaults=memory_defaults_for(active_before_loop),
                )
        except BusinessLoopInvariantError as exc:
            logger.warning(
                "Business loop invariant violated workflow_id=%s session_id=%s "
                "error=%s",
                workflow_id,
                business_session,
                str(exc)[:200],
            )
            await self._record_failure_semantic_memories(
                request, envelope, user_memory_hints,
                session_variables=extracted_variables,
            )
            internal = AgentContractError(str(exc))
            yield failed(
                "business_loop_invariant",
                "业务处理暂时无法完成，请重新描述您的需求后重试",
                internal,
            )
            return
        except Exception:
            logger.exception(
                "Business agent execution failed workflow_id=%s session_id=%s",
                workflow_id,
                business_session,
            )
            await self._record_failure_semantic_memories(
                request, envelope, user_memory_hints,
                session_variables=extracted_variables,
            )
            internal = AgentContractError("business agent execution failed")
            yield failed("agent_runtime_failed", "业务智能体执行失败", internal)
            return
        if skill_trace_view is not None:
            primary_skill = skill_trace_view.skills[0]
            yield (
                event(
                    stage="business_skill.completed",
                    status="completed",
                    title="业务 Skill 已完成本轮编排",
                    input_fields=[],
                    output_fields=[],
                    metrics={
                        "skillId": primary_skill.id,
                        "skillVersion": primary_skill.version,
                        "skillCount": len(skill_trace_view.skill_ids),
                        "outcome": "applied",
                        "stepCount": len(successful_invocations),
                    },
                    duration_ms=(
                        _elapsed_ms(skill_trace_started)
                        if skill_trace_started is not None else None
                    ),
                ),
                None,
            )
        todo_snapshot = loop_advance.snapshot.items if loop_advance is not None else ()
        uncovered = ()
        if not todo_snapshot and hasattr(self._business_runner, "todo_snapshot"):
            try:
                todo_snapshot = self._business_runner.todo_snapshot(business_session)
            except Exception:
                internal = AgentContractError("business agent todo snapshot is invalid")
                yield failed(
                    "business_agent_todo_loop_unavailable",
                    "业务智能体任务循环暂时不可用",
                    internal,
                )
                return
            if len(envelope.tasks) > 1 and not todo_snapshot:
                internal = AgentContractError("business agent did not create native todos")
                yield failed(
                    "business_agent_todo_loop_unavailable",
                    "业务智能体任务循环暂时不可用",
                    internal,
                )
                return
            uncovered = (
                validate_goal_coverage(
                    {item.task_id for item in envelope.tasks},
                    {item.id for item in todo_snapshot},
                )
                if todo_snapshot else ()
            )
            if len(envelope.tasks) > 1 and uncovered:
                internal = AgentContractError(
                    "business agent todo goal coverage conflict: " + ",".join(uncovered)
                )
                yield failed(
                    "business_agent_todo_goal_conflict",
                    "业务智能体未覆盖全部业务目标",
                    internal,
                )
                return
        if all_tasks_knowledge_only and successful_invocations:
            internal = AgentContractError(
                "knowledge-only agent produced a capability invocation"
            )
            yield failed(
                "knowledge_only_capability_violation",
                "知识问答未通过能力隔离校验",
                internal,
            )
            return
        native_loop_outcome = loop_advance.outcome if loop_advance is not None else None
        _replan_goal_states = tuple(
            GoalState(
                task_id=task.task_id,
                sequence=index,
                status=(
                    "succeeded"
                    if task.task_status == "succeeded"
                    else "pending"
                ),
            )
            for index, task in enumerate(envelope.tasks, 1)
        )
        adopted = None
        if (
            native_loop_outcome is not None
            and native_loop_outcome.status == "replan_required"
        ):
            adopted = await self._adopt_replan_message_entry(
                request, envelope, native_loop_outcome,
                workflow_id, workflow_persisted_revision,
                _replan_goal_states,
                business_session,
            )
        elif (
            native_loop_outcome is not None
            and native_loop_outcome.status == "awaiting_clarification"
        ):
            # 卡死升级：用户作答而循环仍无法装填槽位（如轻量模型把两个
            # 收款人当单值反复追问）——结构化抽取 + 确定性重组，签名见
            # _escalate_stuck_clarification。
            adopted = await self._escalate_stuck_clarification(
                request, envelope, native_loop_outcome,
                workflow_id, workflow_persisted_revision,
                _replan_goal_states, business_session,
                restored_workflow,
                initial_active_task,
                business_payload.get("clarification_fit_contract"),
            )
        if isinstance(adopted, RuntimeAgentRunResponse):
            yield (
                event(
                    stage="business_agent.completed",
                    status="completed",
                    title="业务结构重组已采納，等待用户澄清",
                    input_fields=["tasks"],
                    output_fields=["status"],
                    metrics={
                        "coveredTaskCount": (
                            len(adopted.intent_task_envelope.tasks)
                            if adopted.intent_task_envelope is not None else 0
                        )
                    },
                ),
                adopted,
            )
            return
        if adopted is not None:
            # 就地采納：本轮所有下游簿记（计划、持久化、投影）改在
            # 重构后的结构上进行。
            (
                envelope,
                native_loop_outcome,
                replan_evaluations,
                replan_candidate,
                todo_snapshot,
            ) = adopted
            slot_evaluations.update(replan_evaluations)
            for replan_task_id in replan_evaluations:
                candidate_by_task.setdefault(replan_task_id, replan_candidate)
                if replan_task_id not in decision_by_task:
                    try:
                        decision_by_task[replan_task_id] = decide_active_task(
                            task=next(
                                task for task in envelope.tasks
                                if task.task_id == replan_task_id
                            ),
                            candidate=replan_candidate,
                            evaluation=replan_evaluations[replan_task_id],
                            intent_shape=envelope.intent_shape,
                        )
                    except Exception:
                        logger.warning(
                            "Replan task decision build failed task=%s",
                            replan_task_id,
                            exc_info=True,
                        )
        elif native_loop_outcome is not None and (
            native_loop_outcome.status == "replan_required"
        ):
            # 提案不可采纳：降级为普通澄清续跑（既有重问机制）。
            native_loop_outcome = native_loop_outcome.model_copy(update={
                "status": "awaiting_clarification",
                "proposed_tasks": [],
            })
        guided_confirmation_plan: CapabilityPlan | None = None
        guided_confirmation_summary: dict[str, Any] | None = None
        guided_credit_card_facts: dict[str, Any] | None = None
        if (
            native_loop_outcome is not None
            and native_loop_outcome.status in {
                "completed",
                "awaiting_clarification",
                "awaiting_confirmation",
            }
            and len(envelope.tasks) == 1
            and envelope.tasks[0].intent_id in {
                "ccb.obligation.repay-all", "ccb.obligation.pay-selected"
            }
        ):
            discovery_calls = [
                item for item in successful_invocations
                if item.get("capability_id") == "bank.obligation.list"
            ]
            if not discovery_calls:
                try:
                    successful_invocations = _merge_invocations(
                        successful_invocations,
                        await self._invoke_read_once(
                            request.agent_run_id,
                            "bank.obligation.list",
                            {},
                            executable_ids,
                        ),
                    )
                    discovery_calls = [
                        item for item in successful_invocations
                        if item.get("capability_id") == "bank.obligation.list"
                    ]
                except AgentCapabilityInvocationError:
                    yield failed(
                        "obligation_discovery_failed",
                        "待还事项暂时无法查询",
                        AgentContractError(
                            "obligation discovery capability invocation failed"
                        ),
                    )
                    return
            if len(discovery_calls) != 1 or not isinstance(
                discovery_calls[0].get("output"), dict
            ):
                internal = AgentContractError(
                    "guided obligation discovery result is unavailable"
                )
                yield failed(
                    "completed_query_result_invalid",
                    "待还事项查询未产生可信结果",
                    internal,
                )
                return
            obligation_snapshot = discovery_calls[0]["output"]
            try:
                selected_obligation_id = (
                    request.entry_goal.trusted_inputs.get("obligationId")
                    if request.entry_goal is not None
                    and request.entry_goal.target_intent_id
                    in {"ccb.obligation.repay-all", "ccb.obligation.pay-selected"}
                    else None
                )
                selected_obligation_type = (
                    request.entry_goal.trusted_inputs.get("obligationType")
                    if request.entry_goal is not None
                    and request.entry_goal.target_intent_id
                    == "ccb.obligation.pay-selected"
                    else None
                )
                expanded_guided_plan = plan_obligation_snapshot(
                    obligation_snapshot,
                    selected_obligation_id=selected_obligation_id,
                    selected_obligation_type=selected_obligation_type,
                    target_intent_id=envelope.tasks[0].intent_id,
                )
                if expanded_guided_plan.todos:
                    guided_confirmation_plan = _guided_obligation_confirmation_plan(
                        envelope, expanded_guided_plan
                    )
                    guided_confirmation_summary = _guided_obligation_summary(
                        expanded_guided_plan, obligation_snapshot
                    )
                    task_id = envelope.tasks[0].task_id
                    expanded_todos = tuple(
                        TodoSnapshotItem(
                            id=item.item_id,
                            status=("in_progress" if index == 0 else "pending"),
                            depends_on=[],
                        )
                        for index, item in enumerate(expanded_guided_plan.todos)
                    )
                    todo_snapshot = tuple(todo_snapshot) + expanded_todos
                    native_loop_outcome = BusinessLoopOutcome.model_validate({
                        "schema_version": "2.0",
                        "business_workflow_id": workflow_id,
                        "workflow_revision": native_loop_outcome.workflow_revision,
                        "status": "awaiting_confirmation",
                        "covered_task_ids": [task_id],
                        "active_goal_task_id": task_id,
                        "active_todo_id": expanded_todos[0].id,
                        "todo_revision": native_loop_outcome.todo_revision + 1,
                        "next_action": "confirm_guided_obligations",
                    })
            except (BusinessLoopInvariantError, AgentContractError) as exc:
                yield failed(
                    "capability_plan_invalid",
                    "待还事项确认计划未通过安全校验",
                    AgentContractError(str(exc)),
                )
                return
        if (
            native_loop_outcome is not None
            and native_loop_outcome.status == "completed"
            and request.entry_goal is not None
            and request.entry_goal.target_intent_id == "ccb.home.repayment.repay"
        ):
            try:
                guided_credit_card_facts = _guided_credit_card_repayment_facts(
                    successful_invocations, request.entry_goal.trusted_inputs
                )
                task_id = envelope.tasks[0].task_id
                guided_task = envelope.tasks[0]
                guided_slots = dict(guided_task.slots)
                for name, value in _guided_credit_card_safe_slots(
                    guided_credit_card_facts
                ).items():
                    guided_slots[name] = SlotValue(
                        value=value, source="context", confidence=1
                    )
                guided_task = guided_task.model_copy(update={
                    "slots": guided_slots,
                    "execution_readiness": "needs_slots",
                })
                envelope = envelope.model_copy(update={"tasks": [guided_task]})
                guided_candidate = candidate_by_task[task_id]
                if guided_candidate.slot_contract is None:
                    raise AgentContractError(
                        "guided credit-card slot contract is unavailable"
                    )
                slot_evaluations[task_id] = self._slot_evaluator.evaluate(
                    contract=guided_candidate.slot_contract,
                    current_slots=guided_slots,
                    conversation_slots={},
                )
                choice_todo = TodoSnapshotItem(
                    id="repayment-choice", status="in_progress", depends_on=[]
                )
                todo_snapshot = tuple(todo_snapshot) + (choice_todo,)
                native_loop_outcome = BusinessLoopOutcome.model_validate({
                    "schema_version": "2.0",
                    "business_workflow_id": workflow_id,
                    "workflow_revision": native_loop_outcome.workflow_revision,
                    "status": "awaiting_clarification",
                    "covered_task_ids": [task_id],
                    "active_goal_task_id": task_id,
                    "active_todo_id": choice_todo.id,
                    "todo_revision": native_loop_outcome.todo_revision + 1,
                    "next_action": "choose_credit_card_repayment_amount",
                })
            except AgentContractError as exc:
                yield failed(
                    "completed_query_result_invalid",
                    "信用卡账单核对未产生可信结果",
                    AgentContractError(str(exc)),
                )
                return
        if loop_advance is not None and len(envelope.tasks) == 1:
            query_task = envelope.tasks[0]
            query_decision = decision_by_task.get(query_task.task_id)
            trusted_query_calls = (
                [
                    item for item in loop_advance.task_invocations
                    if item.task_id == query_task.task_id
                    and query_decision is not None
                    and item.capability_id
                    == query_decision.execution_capability_id
                ]
                if query_task.action == "QUERY"
                and query_decision is not None
                and query_decision.readiness == "execute"
                else []
            )
            if (
                len(trusted_query_calls) == 1
                and native_loop_outcome is not None
                and native_loop_outcome.status != "completed"
            ):
                # A platform-observed graph-bound read is authoritative. A
                # model lifecycle projection cannot turn that exact result
                # into a failed or unnecessary suspended query.
                todo_snapshot = tuple(
                    TodoSnapshotItem(
                        id=item.id,
                        status=(
                            "cancelled"
                            if item.status == "cancelled"
                            else "completed"
                        ),
                        depends_on=list(item.depends_on),
                    )
                    for item in todo_snapshot
                )
                native_loop_outcome = native_loop_outcome.model_copy(update={
                    "status": "completed",
                    "active_goal_task_id": None,
                    "active_todo_id": None,
                    "next_action": None,
                })
                loop_advance = BusinessLoopAdvance(
                    outcome=native_loop_outcome,
                    snapshot=TodoLedgerSnapshot(
                        revision=loop_advance.snapshot.revision,
                        digest=TodoLedger.digest(todo_snapshot),
                        items=todo_snapshot,
                    ),
                    successful_invocations=loop_advance.successful_invocations,
                    task_invocations=loop_advance.task_invocations,
                    completed_task_ids=(query_task.task_id,),
                )
        completed_task_result_invocations: dict[str, list[dict[str, Any]]] = {}
        if loop_advance is not None:
            logger.debug(
                "Completed-result rebuild context "
                "successful_invocation_caps=%s capabilities_by_task=%s",
                [item.get("capability_id") for item in successful_invocations],
                {
                    key: sorted(value)
                    for key, value in capabilities_by_task.items()
                },
            )
            for completed_task_id in loop_advance.completed_task_ids:
                task = next(
                    (
                        item for item in envelope.tasks
                        if item.task_id == completed_task_id
                    ),
                    None,
                )
                decision = decision_by_task.get(completed_task_id)
                task_invocations = [
                    {
                        "capability_id": item.capability_id,
                        "input": item.input,
                        "output": item.output,
                    }
                    for item in loop_advance.task_invocations
                    if item.task_id == completed_task_id
                ]
                if not task_invocations:
                    # 台账判定该目标已完成，但本轮迭代没有它的调用记录：
                    # 能力调用发生在槽位解析阶段或更早轮次，记录在
                    # successful_invocations。按该目标的能力白名单重建，
                    # 保证已完成查询的结果卡片不丢失；确实无记录时才跳过。
                    allowed_caps = capabilities_by_task.get(
                        completed_task_id, frozenset()
                    )
                    rebuilt: list[dict[str, Any]] = []
                    seen_calls: set[tuple[str, str]] = set()
                    for item in successful_invocations:
                        capability_id = item.get("capability_id")
                        input_payload = item.get("input")
                        output_payload = item.get("output")
                        if (
                            capability_id not in allowed_caps
                            or not isinstance(input_payload, dict)
                            or not isinstance(output_payload, dict)
                        ):
                            continue
                        dedupe_key = (
                            capability_id,
                            json.dumps(input_payload, sort_keys=True, default=str),
                        )
                        if dedupe_key in seen_calls:
                            continue
                        seen_calls.add(dedupe_key)
                        rebuilt.append({
                            "capability_id": capability_id,
                            "input": input_payload,
                            "output": output_payload,
                        })
                    if decision is not None:
                        # 结果投影取最后一条调用，执行能力的输出才是
                        # 该任务的最终结果，固定排在末尾。
                        rebuilt.sort(
                            key=lambda item: (
                                1
                                if item["capability_id"]
                                == decision.execution_capability_id
                                else 0
                            )
                        )
                    task_invocations = rebuilt
                if not task_invocations:
                    # 恢复轮：该目标在更早一轮已交付完成并经台账对账翻平，
                    # 本轮没有它的调用记录，也没有需要重构的结果，直接跳过；
                    # 否则会被当成不可信的已完成查询误判失败。
                    continue
                if task is None or decision is None or task.action != "QUERY":
                    completed_task_result_invocations[completed_task_id] = (
                        task_invocations
                    )
                    continue
                try:
                    if (
                        decision.readiness == "resolve"
                        and decision.slot_type == "account_reference"
                        and decision.resolver_capability_id is not None
                    ):
                        resolver_calls = [
                            item for item in task_invocations
                            if item.get("capability_id")
                            == decision.resolver_capability_id
                        ]
                        if len(resolver_calls) != 1 or resolver_calls[0].get(
                            "input"
                        ) != {}:
                            raise AccountReferenceResolutionError(
                                "completed query resolver invocation is invalid"
                            )
                        accounts = _strict_invocation_list(
                            task_invocations,
                            decision.resolver_capability_id,
                            "accounts",
                        )
                        resolution = resolve_account_reference(
                            request.message, accounts
                        )
                        if resolution.state != "selected":
                            raise AccountReferenceResolutionError(
                                "completed query reference is unresolved"
                            )
                        if decision.resolved_field is None:
                            raise AccountReferenceResolutionError(
                                "completed query binding is missing"
                            )
                        query_invocations = await self._invoke_read_once(
                            request.agent_run_id,
                            decision.execution_capability_id,
                            {decision.resolved_field: resolution.account_id},
                            executable_ids,
                        )
                        task_invocations = _merge_invocations(
                            task_invocations, query_invocations
                        )
                        successful_invocations = _merge_invocations(
                            successful_invocations, query_invocations
                        )
                    execution_calls = [
                        item for item in task_invocations
                        if item.get("capability_id")
                        == decision.execution_capability_id
                    ]
                    if len(execution_calls) != 1:
                        raise AccountReferenceResolutionError(
                            "completed query execution is missing"
                        )
                    completed_task_result_invocations[completed_task_id] = (
                        task_invocations
                    )
                except Exception:
                    logger.exception(
                        "Completed query reconciliation failed task_id=%s",
                        completed_task_id,
                    )
                    internal = AgentContractError(
                        "completed query did not produce a trusted result"
                    )
                    yield failed(
                        "completed_query_result_invalid",
                        "\u5df2\u5b8c\u6210\u67e5\u8be2\u672a\u4ea7\u751f\u53ef\u4fe1\u7ed3\u679c",
                        internal,
                    )
                    return
        confirmation_resolution = None
        if guided_confirmation_plan is not None:
            plan = guided_confirmation_plan
        elif guided_credit_card_facts is not None:
            plan = _empty_plan(
                envelope,
                "clarification_required",
                "choose_credit_card_repayment_amount",
            )
        elif native_loop_outcome is not None:
            active_task = next(
                (
                    item for item in envelope.tasks
                    if item.task_id == native_loop_outcome.active_goal_task_id
                ),
                None,
            )
            if active_task is None:
                if (
                    native_loop_outcome.status == "completed"
                    and native_loop_outcome.active_goal_task_id is None
                    and native_loop_outcome.active_todo_id is None
                ):
                    # A completed authoritative Todo ledger intentionally has
                    # no active task. Keep its already executed read results
                    # and project them below instead of treating the cleared
                    # terminal pointer as a contract failure.
                    completed_steps = [
                        {
                            "step_id": f"native_completed_{index}",
                            "task_id": item.task_id,
                            "capability_id": item.capability_id,
                            "input_bindings": item.input,
                            "requires_user_confirmation": False,
                            "depends_on": [],
                        }
                        for index, item in enumerate(
                            loop_advance.task_invocations, 1
                        )
                    ] if loop_advance is not None else []
                    if completed_steps:
                        plan = CapabilityPlan.model_validate({
                            "schema_version": "1.0",
                            "business_agent_run_id": "native_loop_terminal",
                            "delegation_id": envelope.delegation_id,
                            "status": "planned",
                            "steps": completed_steps,
                        })
                    elif all_tasks_knowledge_only:
                        plan = _empty_plan(
                            envelope, "navigation_only", "native_loop_completed"
                        )
                    else:
                        internal = AgentContractError(
                            "completed business loop has no trusted invocation"
                        )
                        yield failed(
                            "completed_query_result_invalid",
                            "已完成查询未产生可信结果",
                            internal,
                        )
                        return
                else:
                    internal = AgentContractError(
                        "business loop active task is unavailable"
                    )
                    yield failed(
                        "business_agent_contract_violation",
                        "业务循环活动任务无效",
                        internal,
                    )
                    return
            else:
                current_envelope = _single_task_envelope(envelope, active_task)
                if uses_transfer_compatibility_adapter(self._intent_graph, active_task):
                    try:
                        confirmation_resolution, resolution_invocations = (
                            await self._resolve_transfer_confirmation(
                                request,
                                current_envelope,
                                {active_task.task_id: slot_evaluations[active_task.task_id]},
                                intent_constraints,
                                {item.id for item in bound_executable},
                                successful_invocations,
                            )
                        )
                        successful_invocations = _merge_invocations(
                            successful_invocations, resolution_invocations
                        )
                    except Exception:
                        logger.exception("Transfer loop entity resolution failed")
                        confirmation_resolution = None
                    plan = (
                        _confirmation_plan_from_resolution(
                            current_envelope, active_task.task_id, confirmation_resolution
                        )
                        if confirmation_resolution is not None
                        else _empty_plan(
                            current_envelope,
                            "clarification_required",
                            native_loop_outcome.next_action or "collect_slots",
                        )
                    )
                else:
                    mapped_status = {
                        "completed": "navigation_only" if all_tasks_knowledge_only else "clarification_required",
                        "awaiting_clarification": "clarification_required",
                        "awaiting_confirmation": "clarification_required",
                        "waiting_external": "clarification_required",
                        "partially_completed": "clarification_required",
                        "failed": "blocked",
                    }.get(native_loop_outcome.status, "clarification_required")
                    plan = _empty_plan(
                        current_envelope,
                        mapped_status,
                        native_loop_outcome.next_action or "loop_outcome",
                    )
        else:
            try:
                plan = _parse_plan_submission(raw_plan, envelope)
            except AgentContractError as internal:
                yield failed(
                    "business_agent_contract_violation", "业务能力计划未通过契约校验", internal
                )
                return

        runtime_active_task = next(
            (
                task for task in envelope.tasks
                if native_loop_outcome is not None
                and task.task_id == native_loop_outcome.active_goal_task_id
            ),
            initial_active_task,
        )
        active_slot_decision = decision_by_task.get(runtime_active_task.task_id)
        slot_reference_resolution: (
            AccountReferenceResolution | CardReferenceResolution | ProductReferenceResolution | HoldingReferenceResolution | PartyReferenceResolution | None
        ) = None
        account_reference_resolution: AccountReferenceResolution | None = None
        if (
            active_slot_decision is not None
            and active_slot_decision.readiness == "resolve"
            and active_slot_decision.slot_type in {
                "account_reference", "card_reference", "product_reference",
                "holding_reference", "party_reference",
            }
            and active_slot_decision.resolver_capability_id is not None
            and not (
                request.entry_goal is not None
                and request.entry_goal.target_intent_id
                == "ccb.home.repayment.repay"
            )
        ):
            try:
                active_candidate = candidate_by_task[runtime_active_task.task_id]
                (
                    runtime_active_task,
                    active_evaluation,
                    active_slot_decision,
                    slot_reference_resolution,
                    successful_invocations,
                ) = await self._resolve_graph_reference_chain(
                    agent_run_id=request.agent_run_id,
                    message=request.message,
                    task=runtime_active_task,
                    candidate=active_candidate,
                    evaluation=slot_evaluations[runtime_active_task.task_id],
                    intent_shape=envelope.intent_shape,
                    executable_ids=executable_ids,
                    successful_invocations=successful_invocations,
                )
                slot_evaluations[runtime_active_task.task_id] = active_evaluation
                logger.debug(
                    "Reference chain resolved task=%s readiness=%s decision=%s"
                    " missing=%s invalid=%s invocations=%s",
                    runtime_active_task.task_id,
                    active_evaluation.readiness,
                    active_slot_decision.readiness if active_slot_decision else None,
                    sorted(active_evaluation.missing),
                    sorted(active_evaluation.invalid),
                    len(successful_invocations),
                )
                envelope = envelope.model_copy(update={
                    "tasks": [
                        runtime_active_task
                        if item.task_id == runtime_active_task.task_id else item
                        for item in envelope.tasks
                    ]
                })
                if isinstance(slot_reference_resolution, AccountReferenceResolution):
                    account_reference_resolution = slot_reference_resolution
            except (
                AccountReferenceResolutionError,
                CardReferenceResolutionError,
                ProductReferenceResolutionError,
                HoldingReferenceResolutionError,
                PartyReferenceResolutionError,
            ):
                internal = AgentContractError(
                    "reference resolver returned invalid data"
                )
                yield failed(
                    "slot_resolver_data_invalid",
                    "业务对象发现结果未通过安全校验",
                    internal,
                )
                return
            except (AgentCapabilityInvocationError, AgentContractError):
                internal = AgentContractError(
                    "reference resolver capability invocation failed"
                )
                yield failed(
                    "slot_resolver_invocation_failed",
                    "业务对象发现服务暂时不可用",
                    internal,
                )
                return
            except (TaskSlotRuntimeError, ValueError, TypeError) as exc:
                logger.exception(
                    "slot_resolver_policy_violation detail=%s type=%s",
                    exc, type(exc).__name__,
                )
                internal = AgentContractError(
                    f"reference resolver policy failed: {exc}"
                )
                yield failed(
                    "slot_resolver_policy_violation",
                    "业务对象发现未通过安全校验",
                    internal,
                )
                return

        if (
            active_slot_decision is not None
            and active_slot_decision.readiness == "clarify"
            and active_slot_decision.slot_name == "amount"
            and candidate_by_task[runtime_active_task.task_id].response_policy
            == "credit_card_repayment"
        ):
            try:
                bindings = slot_evaluations[
                    runtime_active_task.task_id
                ].resolved_values
                card_id = bindings.get("cardId")
                account_id = bindings.get("fromAccountId")
                if not isinstance(card_id, str) or not isinstance(account_id, str):
                    raise AgentContractError(
                        "credit-card repayment references are unresolved"
                    )
                for capability_id, capability_input in (
                    ("bank.credit-card.bill.get", {"cardId": card_id}),
                    ("bank.account.balance.get", {"accountId": account_id}),
                ):
                    if not any(
                        item.get("capability_id") == capability_id
                        and item.get("input") == capability_input
                        for item in successful_invocations
                    ):
                        successful_invocations = _merge_invocations(
                            successful_invocations,
                            await self._invoke_read_once(
                                request.agent_run_id,
                                capability_id,
                                capability_input,
                                executable_ids,
                            ),
                        )
                guided_credit_card_facts = _guided_credit_card_repayment_facts(
                    successful_invocations,
                    {"cardId": card_id, "fromAccountId": account_id},
                )
            except (AgentCapabilityInvocationError, AgentContractError):
                yield failed(
                    "credit_card_repayment_context_unavailable",
                    "信用卡账单或付款账户暂时无法核对",
                    AgentContractError(
                        "credit-card repayment context is unavailable"
                    ),
                )
                return

        business_repair_count = 0
        planned_multi_adapter_repair = bool(
            not native_loop_mode
            and
            plan.status == "planned"
            and len(envelope.tasks) > 1
            and all_use_transfer_compatibility_adapter(self._intent_graph, envelope)
            and _validate_multi_task_plan_coverage(plan, envelope)
        )
        if all_use_transfer_compatibility_adapter(self._intent_graph, envelope) and (
            plan.status == "clarification_required" or planned_multi_adapter_repair
        ):
            try:
                confirmation_resolution, resolution_invocations = (
                    await self._resolve_transfer_confirmation(
                        request,
                        envelope,
                        slot_evaluations,
                        intent_constraints,
                        {item.id for item in bound_executable},
                        successful_invocations,
                    )
                )
                successful_invocations = _merge_invocations(
                    successful_invocations, resolution_invocations
                )
            except Exception:
                logger.exception("Transfer confirmation entity resolution failed")
                await self._record_failure_semantic_memories(
                    request, envelope, user_memory_hints,
                    session_variables=extracted_variables,
                )
                internal = AgentContractError("confirmation entity resolution failed")
                yield failed("agent_runtime_failed", "交易实体解析失败", internal)
                return
        if planned_multi_adapter_repair:
            ordered_evaluations = [
                slot_evaluations[item.task_id] for item in envelope.tasks
            ]
            requested_payees = [
                str(item.semantic_values.get("payee", "")).strip()
                for item in ordered_evaluations
            ]
            missing_payees = self._user_response_projector.missing_transfer_payees(
                requested_payees, successful_invocations
            )
            if (
                missing_payees
                or any(item.readiness != "ready" for item in ordered_evaluations)
            ):
                plan = _empty_plan(
                    envelope, "clarification_required", "multi_transfer_slots"
                )
        plan_violations = list(validate_graph_bound_discovery(
            plan,
            envelope,
            bound_executable,
            intent_constraints,
            [item.get("capability_id", "") for item in successful_invocations],
        ))
        if not native_loop_mode:
            plan_violations.extend(_validate_multi_task_plan_coverage(plan, envelope))
        if confirmation_resolution is not None and not native_loop_mode:
            plan_violations.append("confirmation_resolution_required")
        if plan_violations and native_loop_mode:
            # Native BusinessLoopOutcome/Todo state has its own bounded repair
            # protocol in BusinessLoopCoordinator. Never send that v2 outcome
            # through the legacy CapabilityPlan v1 repair parser.
            internal = AgentContractError(
                "native business loop discovery conflict: "
                + ",".join(plan_violations)
            )
            yield failed(
                "business_agent_readonly_discovery_conflict",
                "业务智能体查询步骤未通过契约校验",
                internal,
            )
            return
        if plan_violations:
            business_repair_count = 1
            repair_payload = {
                **business_payload,
                "repair_feedback": {
                    "attempt": 1,
                    "max_attempts": 1,
                    "codes": list(plan_violations),
                },
            }
            if confirmation_resolution is not None:
                repair_payload["confirmation_resolution"] = confirmation_resolution
            try:
                completed_capabilities = {
                    item.get("capability_id")
                    for item in successful_invocations
                    if isinstance(item.get("capability_id"), str)
                }
                repaired_plan, repair_invocations = await self._run_business(
                    repair_payload,
                    session_id=business_session,
                    agent_run_id=request.agent_run_id,
                    execution_allowlist=(
                        set()
                        if account_reference_resolution is not None
                        else {
                            item.id for item in bound_executable
                            if item.id not in completed_capabilities
                        }
                    ),
                )
                successful_invocations = _merge_invocations(
                    successful_invocations, repair_invocations
                )
            except Exception:
                await self._record_failure_semantic_memories(
                    request, envelope, user_memory_hints,
                    session_variables=extracted_variables,
                )
                internal = AgentContractError("business agent repair execution failed")
                yield failed("agent_runtime_failed", "业务智能体执行失败", internal)
                return
            try:
                plan = _parse_plan_submission(repaired_plan, envelope)
            except AgentContractError as internal:
                yield failed(
                    "business_agent_contract_violation",
                    "业务能力计划未通过契约校验",
                    internal,
                )
                return
            plan_violations = list(validate_graph_bound_discovery(
                plan,
                envelope,
                bound_executable,
                intent_constraints,
                [item.get("capability_id", "") for item in successful_invocations],
            ))
            plan_violations.extend(_validate_multi_task_plan_coverage(plan, envelope))
            if confirmation_resolution is not None:
                plan_violations.extend(
                    _validate_confirmation_resolution(plan, confirmation_resolution)
                )
            if plan_violations:
                internal = AgentContractError(
                    "business agent readonly discovery conflict: "
                    + ",".join(plan_violations)
                )
                yield failed(
                    "business_agent_readonly_discovery_conflict",
                    "业务计划未满足只读账户发现策略",
                    internal,
                )
                return

        yield (
            event(
                stage="business_agent.completed",
                status="completed",
                title="业务能力规划完成",
                input_fields=["tasks", "capabilities"],
                output_fields=["planStatus", "stepCount"],
                metrics={
                    "planStatus": plan.status,
                    "stepCount": len(plan.steps),
                    "slotResolution": (
                        "read_only_discovery"
                        if graph_bound_discovery_applies(envelope, bound_executable, intent_constraints)
                        else "clarification"
                        if plan.status == "clarification_required"
                        else "not_required"
                    ),
                    "businessRepairCount": business_repair_count,
                    **({
                        "loopIteration": business_repair_count + 1,
                        "todoRevision": business_repair_count + 1,
                        "currentTodoStatus": (
                            "awaiting_clarification"
                            if plan.status == "clarification_required"
                            else "awaiting_confirmation"
                            if any(item.requires_user_confirmation for item in plan.steps)
                            else "in_progress"
                        ),
                        "delegatedTaskCount": len(envelope.tasks),
                        "coveredTaskCount": len(envelope.tasks) - len(uncovered),
                    } if todo_snapshot else {}),
                },
                duration_ms=_elapsed_ms(business_started),
            ),
            None,
        )

        if (
            native_loop_outcome is not None
            and runtime_active_task.action == "DO"
            and active_slot_decision is not None
            and plan.status != "planned"
            and not uses_transfer_compatibility_adapter(
                self._intent_graph, runtime_active_task
            )
        ):
            action_capability = next(
                (
                    item for item in bound_plannable
                    if item.id == active_slot_decision.execution_capability_id
                ),
                None,
            )
            if action_capability is not None:
                resolved_references: dict[str, object] = {}
                if (
                    account_reference_resolution is not None
                    and account_reference_resolution.state == "selected"
                    and active_slot_decision.resolved_field is not None
                ):
                    resolved_references[active_slot_decision.resolved_field] = (
                        account_reference_resolution.account_id
                    )
                try:
                    graph_action_plan = build_graph_action_plan(
                        plan,
                        runtime_active_task,
                        active_slot_decision,
                        slot_evaluations[runtime_active_task.task_id],
                        action_capability,
                        resolved_references,
                    )
                except TaskSlotRuntimeError as exc:
                    yield failed(
                        "capability_plan_invalid",
                        "能力计划未通过安全校验",
                        AgentContractError(str(exc)),
                    )
                    return
                if graph_action_plan is not None:
                    plan = graph_action_plan

        validation_started = time.monotonic()
        try:
            self._validate_plan(plan, envelope, intent_constraints)
        except AgentContractError as exc:
            yield failed("capability_plan_invalid", "能力计划未通过安全校验", exc)
            return

        # A resolved account reference only turns directly into an executable
        # read step for QUERY tasks (for example balance lookup).  For DO tasks
        # it is an input to the confirmation plan; treating the final mutation
        # as a read capability both violates the executable allowlist and
        # bypasses the confirmation boundary.
        query_account_id = None
        if (
            runtime_active_task.action == "QUERY"
            and active_slot_decision is not None
            and active_slot_decision.readiness == "execute"
            and active_slot_decision.resolved_field is not None
        ):
            resolved_candidate = slot_evaluations[
                runtime_active_task.task_id
            ].resolved_values.get(active_slot_decision.resolved_field)
            if isinstance(resolved_candidate, str):
                query_account_id = resolved_candidate
        if query_account_id is not None:
            try:
                plan = build_graph_read_plan(
                    plan,
                    runtime_active_task,
                    active_slot_decision,
                    query_account_id,
                )
                self._validate_plan(plan, envelope, intent_constraints)
                if self._capability_invoker is None:
                    raise AccountReferenceResolutionError(
                        "account query capability is unavailable"
                    )
                already_executed = any(
                    item.get("capability_id")
                    == active_slot_decision.execution_capability_id
                    for item in successful_invocations
                )
                if not already_executed:
                    query_invocations = await self._invoke_read_once(
                        request.agent_run_id,
                        active_slot_decision.execution_capability_id,
                        {active_slot_decision.resolved_field: query_account_id},
                        executable_ids,
                    )
                    successful_invocations = _merge_invocations(
                        successful_invocations, query_invocations
                    )
            except AccountReferenceResolutionError as exc:
                internal = AgentContractError("balance account data is invalid")
                yield failed(
                    "balance_account_data_invalid",
                    "账户查询结果未通过安全校验",
                    internal,
                )
                return
            except AgentCapabilityInvocationError:
                internal = AgentContractError(
                    "balance capability invocation failed"
                )
                yield failed(
                    "balance_capability_invocation_failed",
                    "余额查询服务暂时不可用",
                    internal,
                )
                return
            except AgentContractError as exc:
                yield failed("capability_plan_invalid", "能力计划未通过安全校验", exc)
                return

            # 引用解析链已交付活动查询任务的执行结果时，按台账权威推进：
            # 已交付任务标记 completed，活动 goal 推进到下一个待办任务。
            # 结果卡片经 multi-task prefix 投影作为已完成前缀交付，后续
            # 任务澄清由其自身槽位链路产出。已交付结果的任务若不推进，
            # 每个续办轮都会重入 resolve 链重刷结果卡，后续任务永远得
            # 不到澄清（多任务续办在余额卡上无限循环的根因）。
            if (
                loop_advance is not None
                and native_loop_outcome is not None
                and native_loop_outcome.active_goal_task_id
                == runtime_active_task.task_id
                and any(
                    item.get("capability_id")
                    == active_slot_decision.execution_capability_id
                    for item in successful_invocations
                )
            ):
                followup_task = next(
                    (
                        item for item in envelope.tasks
                        if item.task_id != runtime_active_task.task_id
                        and item.task_status not in ("completed", "skipped")
                    ),
                    None,
                )
                if followup_task is not None:
                    delivered_task_id = runtime_active_task.task_id
                    advanced_items = []
                    for item in loop_advance.snapshot.items:
                        if item.id == delivered_task_id:
                            status = "completed"
                        elif (
                            item.id == followup_task.task_id
                            and item.status != "completed"
                        ):
                            status = "in_progress"
                        else:
                            status = item.status
                        advanced_items.append(
                            item
                            if item.status == status
                            else item.model_copy(update={"status": status})
                        )
                    advanced_snapshot = TodoLedgerSnapshot(
                        revision=loop_advance.snapshot.revision + 1,
                        digest=TodoLedger.digest(advanced_items),
                        items=tuple(advanced_items),
                    )
                    advanced_outcome = native_loop_outcome.model_copy(update={
                        "active_goal_task_id": followup_task.task_id,
                        "active_todo_id": followup_task.task_id,
                        "status": "awaiting_clarification",
                        "next_action": "collect_required_slots",
                        "todo_revision": loop_advance.snapshot.revision + 1,
                    })
                    delivered_invocations = [
                        item for item in successful_invocations
                        if item.get("capability_id")
                        == active_slot_decision.execution_capability_id
                        and isinstance(item.get("input"), dict)
                        and isinstance(item.get("output"), dict)
                    ]
                    loop_advance = BusinessLoopAdvance(
                        outcome=advanced_outcome,
                        snapshot=advanced_snapshot,
                        successful_invocations=loop_advance.successful_invocations,
                        task_invocations=tuple([
                            *loop_advance.task_invocations,
                            *(
                                TaskInvocation(
                                    task_id=delivered_task_id,
                                    capability_id=item["capability_id"],
                                    input=copy.deepcopy(item["input"]),
                                    output=copy.deepcopy(item["output"]),
                                )
                                for item in delivered_invocations
                            ),
                        ]),
                        completed_task_ids=tuple(
                            [*loop_advance.completed_task_ids, delivered_task_id]
                        ),
                    )
                    native_loop_outcome = advanced_outcome
                    todo_snapshot = advanced_snapshot.items
                    envelope = envelope.model_copy(update={
                        "tasks": [
                            item.model_copy(update={"task_status": "completed"})
                            if item.task_id == delivered_task_id
                            else item
                            for item in envelope.tasks
                        ]
                    })
                    runtime_active_task = next(
                        item for item in envelope.tasks
                        if item.task_id == followup_task.task_id
                    )
                    active_slot_decision = decision_by_task.get(
                        runtime_active_task.task_id
                    )
                    plan = _empty_plan(
                        envelope, "clarification_required", "collect_required_slots"
                    )
                    slot_reference_resolution = None
                    # 推进后的活动任务若命中转账适配器且实体可权威解析
                    # （收款人在目录中、转出账户唯一、金额有效），直接按
                    # 简历路径同一机制产出确认计划，让下游确认检查点链路
                    # 生成"请确认转账"卡片；实体不可解析时保持常规槽位澄
                    # 清，由用户补录。与 2738 行 resume 路径遵循同一契约。
                    if uses_transfer_compatibility_adapter(
                        self._intent_graph, runtime_active_task
                    ) and self._capability_invoker is not None:
                        try:
                            (
                                transfer_resolution,
                                transfer_added,
                            ) = await self._resolve_transfer_confirmation(
                                request,
                                _single_task_envelope(envelope, runtime_active_task),
                                {
                                    runtime_active_task.task_id: slot_evaluations[
                                        runtime_active_task.task_id
                                    ]
                                },
                                intent_constraints,
                                {item.id for item in bound_executable},
                                successful_invocations,
                            )
                            if transfer_resolution is not None:
                                successful_invocations = _merge_invocations(
                                    successful_invocations, transfer_added
                                )
                                confirmation_plan = _confirmation_plan_from_resolution(
                                    envelope,
                                    runtime_active_task.task_id,
                                    transfer_resolution,
                                )
                                self._validate_plan(
                                    confirmation_plan, envelope, intent_constraints
                                )
                                plan = confirmation_plan
                            else:
                                # 确认解析未成（如收款人不在目录）：无法唯一
                                # 命中的实体槽位不能继续按已满足处理，否则澄
                                # 清投影会落进"请核对转账信息"死胡同卡（槽位
                                # 看似就绪却无事实可确认）。仅在实体目录已发
                                # 现（门内通过、走到了实体匹配）时，移除失效
                                # 的语义值并降级 missing，交回常规补充澄清；
                                # 门被约束/绑定关闭时保持原状，由门日志定位。
                                merged = _merge_invocations(
                                    successful_invocations, transfer_added
                                )
                                successful_invocations = merged
                                accounts = _invocation_items(
                                    merged, "bank.account.list", "accounts"
                                )
                                payees = _invocation_items(
                                    merged, "bank.payee.list", "payees"
                                )
                                if accounts and payees:
                                    task_view = _single_task_envelope(
                                        envelope, runtime_active_task
                                    ).tasks[0]
                                    entity_slot_keys = {
                                        "fromAccount": (
                                            "fromAccount", "fromAccountName",
                                            "fromAccountReference",
                                        ),
                                        "payee": (
                                            "payee", "payeeName", "payeeReference",
                                        ),
                                    }
                                    unresolved_fields = [
                                        field
                                        for field, keys in entity_slot_keys.items()
                                        if _unique_named_match(
                                            (
                                                accounts
                                                if field == "fromAccount"
                                                else payees
                                            ),
                                            _slot_values(task_view.slots, keys),
                                            request.message,
                                        )
                                        is None
                                    ]
                                    if unresolved_fields:
                                        dropped_keys = {
                                            key
                                            for field in unresolved_fields
                                            for key in entity_slot_keys[field]
                                            if key in task_view.slots
                                        }
                                        updated_task = runtime_active_task.model_copy(
                                            update={
                                                "slots": {
                                                    key: value
                                                    for key, value in (
                                                        runtime_active_task.slots.items()
                                                    )
                                                    if key not in dropped_keys
                                                },
                                                "execution_readiness": "needs_slots",
                                            }
                                        )
                                        envelope = envelope.model_copy(update={
                                            "tasks": [
                                                updated_task
                                                if item.task_id == updated_task.task_id
                                                else item
                                                for item in envelope.tasks
                                            ]
                                        })
                                        runtime_active_task = updated_task
                                        evaluation = slot_evaluations[
                                            updated_task.task_id
                                        ]
                                        slot_evaluations[updated_task.task_id] = (
                                            replace(
                                                evaluation,
                                                missing=tuple([
                                                    *evaluation.missing,
                                                    *unresolved_fields,
                                                ]),
                                                readiness="needs_slots",
                                            )
                                        )
                        except AgentContractError as exc:
                            logger.warning(
                                "Transfer confirmation plan unavailable after"
                                " ledger advance task=%s detail=%s",
                                runtime_active_task.task_id,
                                exc,
                            )
                    logger.warning(
                        "Delivered query result advanced authoritative ledger "
                        "completed=%s next_active=%s",
                        delivered_task_id,
                        runtime_active_task.task_id,
                    )

        try:
            if query_account_id is None:
                successful_invocations = await self._execute_ready_read_steps(
                    plan,
                    request.agent_run_id,
                    {item.id for item in bound_executable},
                    successful_invocations,
                )
        except Exception:
            await self._record_failure_semantic_memories(
                request, envelope, user_memory_hints,
                session_variables=extracted_variables,
            )
            internal = AgentContractError("validated read capability execution failed")
            yield failed("agent_runtime_failed", "只读原子能力执行失败", internal)
            return

        for invocation in successful_invocations:
            capability_id = invocation.get("capability_id", "")
            yield (
                event(
                    stage="capability.invocation_started",
                    status="running",
                    title="原子查询能力开始调用",
                    input_fields=[], output_fields=[],
                    metrics={"capabilityId": capability_id},
                ), None,
            )
            yield (
                event(
                    stage="capability.invocation_completed",
                    status="completed",
                    title="原子查询能力调用完成",
                    input_fields=[], output_fields=["outputKeys"],
                    metrics={"capabilityId": capability_id, "outcome": "succeeded"},
                ), None,
            )

        yield (
            event(
                stage="plan.validated",
                status="completed",
                title="能力计划校验通过",
                input_fields=["capabilityPlan"],
                output_fields=["validated", "confirmationCount"],
                metrics={
                    "validated": True,
                    "confirmationCount": sum(
                        1 for item in plan.steps if item.requires_user_confirmation
                    ),
                },
                duration_ms=_elapsed_ms(validation_started),
            ),
            None,
        )

        response_status = plan.status
        user_result = None
        user_response = None
        user_responses = None
        confirmation = None
        clarification_form = None
        confirmation_steps = [
            step for step in plan.steps if step.requires_user_confirmation
        ]
        if confirmation_steps and hasattr(self._input_rail, "evaluate_action"):
            action_started = time.monotonic()
            yield (
                event(
                    stage="action_rail.started",
                    status="running",
                    title="高风险业务动作安全检查已启动",
                    input_fields=["capabilityPlan"],
                    output_fields=[],
                    metrics={"actionCount": len(confirmation_steps)},
                ),
                None,
            )
            capability_by_id = {item.id: item for item in bound_plannable}
            action_decisions: list[RailDecision] = []
            try:
                async def _evaluate_action(step):
                    capability = capability_by_id.get(step.capability_id)
                    if capability is None:
                        raise AgentContractError("action capability is unavailable")
                    return await self._input_rail.evaluate_action(
                        capability_id=step.capability_id,
                        risk_level=capability.risk_level,
                        summary=f"{capability.title} {capability.description}",
                        requires_confirmation=True,
                        confirmed=False,
                        input_decision=rail_decision.decision,
                    )
                action_decisions = list(await asyncio.gather(
                    *[_evaluate_action(step) for step in confirmation_steps],
                ))
                action_decision = _strongest_rail_decision(action_decisions)
            except asyncio.CancelledError:
                raise
            except Exception:
                action_decision = _unavailable_rail_decision("action")
            action_decision = _effective_rail_decision(
                action_decision,
                self._input_rail_mode,
            )
            yield (
                event(
                    stage="action_rail.completed",
                    status="completed",
                    title="高风险业务动作安全检查已完成",
                    input_fields=["capabilityPlan"],
                    output_fields=["decision"],
                    metrics={
                        "decision": action_decision.decision,
                        "reasonCode": action_decision.reason_code,
                        "policyVersion": action_decision.policy_version,
                        "actionCount": len(confirmation_steps),
                    },
                    duration_ms=_elapsed_ms(action_started),
                ),
                None,
            )
            if action_decision.decision in {"block", "unavailable"}:
                terminal = _security_terminal_response(request, action_decision)
                yield (
                    event(
                        stage="security_gate.decided",
                        status="completed",
                        title="动作安全门禁已阻止后续办理",
                        input_fields=["railDecision"],
                        output_fields=["status"],
                        metrics={
                            "decision": action_decision.decision,
                            "reasonCode": action_decision.reason_code,
                            "policyVersion": action_decision.policy_version,
                            "permitType": None,
                        },
                        duration_ms=_elapsed_ms(run_started),
                    ),
                    None,
                )
                yield (
                    event(
                        stage="run.completed",
                        status="completed",
                        title="智能服务安全结束",
                        input_fields=[],
                        output_fields=["status"],
                        metrics={"status": terminal.status, "taskCount": 0, "stepCount": 0},
                        duration_ms=_elapsed_ms(run_started),
                        result=project_safe_run(terminal),
                    ),
                    terminal,
                )
                return
        confirmation_progress = None
        envelope_task_by_id = {task.task_id: task for task in envelope.tasks}
        if (
            len(envelope.tasks) > 1
            and confirmation_steps
            and all(
                step.task_id in envelope_task_by_id
                and uses_transfer_compatibility_adapter(
                    self._intent_graph, envelope_task_by_id[step.task_id]
                )
                for step in confirmation_steps
            )
        ):
            todo_status_by_id = {
                item.id: item.status for item in todo_snapshot
            }
            status_map = {
                "pending": "ready",
                "in_progress": "in_progress",
                "completed": "succeeded",
                "cancelled": "cancelled",
            }
            goals = tuple(
                GoalState(
                    task_id=task.task_id,
                    sequence=index,
                    status=status_map.get(
                        todo_status_by_id.get(task.task_id, "pending"),
                        "pending",
                    ),
                )
                for index, task in enumerate(envelope.tasks, 1)
            )
            active_goal = select_active_goal(
                goals,
                previous_active_task_id=None,
            )
            if active_goal is None:
                raise AgentContractError(
                    "sequential transfer has no active goal"
                )
            current_steps = [
                step for step in confirmation_steps
                if step.task_id == active_goal.task_id
            ]
            if len(current_steps) != 1:
                raise AgentContractError(
                    "active transfer goal requires one confirmation step"
                )
            confirmation_steps = current_steps
            confirmation_progress = TaskProgress.model_validate(
                sequential_progress(goals, active_goal.task_id)
            )
        if plan.status == "planned" and confirmation_steps and self._capability_invoker:
            batch_capability_ids = {
                step.capability_id for step in confirmation_steps
            }
            obligation_capability_ids = {
                "bank.credit-card.repayment.create",
                "bank.loan.repayment.create",
                "bank.utility.payment.create",
            }
            if len(confirmation_steps) > 1 and not (
                batch_capability_ids == {"bank.transfer.order.create"}
                or batch_capability_ids.issubset(obligation_capability_ids)
            ):
                internal = AgentContractError("batch confirmation contains incompatible capabilities")
                yield failed("capability_plan_invalid", "确认计划未通过安全校验", internal)
                return
            try:
                if todo_snapshot:
                    active_task_id, active_todo_id = _active_workflow_ids(
                        envelope, todo_snapshot, native_loop_outcome
                    )
                    scoped_todo_items = _scoped_persist_todo_items(
                        todo_snapshot, [item.task_id for item in envelope.tasks]
                    )
                    todo_digest = TodoLedger.digest(scoped_todo_items)
                    persisted = await self._capability_invoker.persist_agent_workflow(
                        agent_run_id=request.agent_run_id,
                        business_workflow_id=workflow_id,
                        status="running",
                        delegated_task_ids=[item.task_id for item in envelope.tasks],
                        expected_revision=workflow_persisted_revision,
                        envelope_revision=envelope.revision,
                        workflow_revision=workflow_persisted_revision + 1,
                        todo_revision=(
                            max(
                                native_loop_outcome.todo_revision,
                                stored_todo_revision,
                            )
                            if native_loop_outcome is not None else 1
                        ),
                        todo_snapshot=[{
                            "id": item.id,
                            "status": item.status,
                            "dependsOn": list(item.depends_on),
                        } for item in scoped_todo_items],
                        todo_digest=todo_digest,
                        active_goal_task_id=active_task_id,
                        active_todo_id=active_todo_id,
                        suspension_reason=None,
                        last_outcome={"status": "running"},
                        pending_checkpoint_ids=[],
                    )
                    # 网关在 CAS 冲突时会以 Node 当前 revision 顶替重试，
                    # 盲目 +1 会与真实落库 revision 脱节；以返回值为准。
                    workflow_persisted_revision = int(
                        persisted.get("workflowRevision", workflow_persisted_revision + 1)
                    )
                with self._capability_invoker.activate(
                    request.agent_run_id,
                    {item.id for item in bound_executable},
                    {item.id for item in bound_plannable},
                ):
                    async def _create_checkpoint(step):
                        capability = next(
                            (
                                item for item in bound_plannable
                                if item.id == step.capability_id
                            ),
                            None,
                        )
                        if capability is None:
                            raise AgentContractError(
                                "confirmation capability is not plannable"
                            )
                        return await self._capability_invoker.create_action_checkpoint(
                            step.capability_id,
                            _transfer_capability_input(step.input_bindings),
                            {
                                "title": "请确认" + capability.title,
                                "description": capability.description,
                                "riskLevel": capability.risk_level,
                            },
                            business_workflow_id=workflow_id,
                            task_id=step.task_id,
                        )
                    raw_confirmations = list(await asyncio.gather(
                        *[_create_checkpoint(step) for step in confirmation_steps],
                    ))
                if confirmation_progress is not None:
                    confirmation = ConfirmationView.model_validate({
                        **raw_confirmations[0],
                        "summary": _batch_transfer_summary(
                            envelope, confirmation_steps
                        ),
                    })
                elif len(raw_confirmations) == 1:
                    confirmation = ConfirmationView.model_validate({
                        **raw_confirmations[0],
                        **({"summary": guided_confirmation_summary}
                           if guided_confirmation_summary is not None else {}),
                    })
                else:
                    primary = raw_confirmations[0]
                    confirmation = ConfirmationView.model_validate({
                        **primary,
                        "confirmations": [
                            {
                                "checkpointId": item["checkpointId"],
                                "confirmationToken": item["confirmationToken"],
                            }
                            for item in raw_confirmations
                        ],
                        "summary": (
                            guided_confirmation_summary
                            or (
                                _batch_transfer_summary(envelope, confirmation_steps)
                                if batch_capability_ids == {"bank.transfer.order.create"}
                                else _general_obligation_summary(confirmation_steps)
                            )
                        ),
                    })
                response_status = "awaiting_confirmation"
                yield (
                    event(
                        stage="confirmation.checkpoint_created",
                        status="completed",
                        title=(
                            "批次确认检查点已创建"
                            if len(confirmation_steps) > 1
                            else "本人确认检查点已创建"
                        ),
                        input_fields=[], output_fields=["checkpointStatus"],
                        metrics={
                            **({
                                "capabilityId": confirmation_steps[0].capability_id,
                                "riskLevel": confirmation.summary.risk_level,
                            } if len(confirmation_steps) == 1 else {}),
                            "checkpointStatus": "pending",
                            "grantCount": 0,
                        },
                    ), None,
                )
            except Exception:
                logger.exception(
                    "Confirmation checkpoint creation failed workflow_id=%s task_ids=%s",
                    workflow_id,
                    [item.task_id for item in confirmation_steps],
                )
                await self._record_failure_semantic_memories(
                    request, envelope, user_memory_hints,
                    session_variables=extracted_variables,
                )
                internal = AgentContractError("confirmation checkpoint creation failed")
                yield failed("checkpoint_creation_failed", "确认检查点创建失败", internal)
                return
        elif plan.status == "planned" and not confirmation_steps:
            if (
                account_reference_resolution is not None
                and account_reference_resolution.state != "selected"
            ):
                user_result = None
            else:
                try:
                    result_invocations = (
                        [
                            item for item in successful_invocations
                            if active_slot_decision is not None
                            and item.get("capability_id")
                            == active_slot_decision.execution_capability_id
                        ]
                        if account_reference_resolution is not None
                        and account_reference_resolution.state == "selected"
                        else successful_invocations
                    )
                    user_result = project_user_result(result_invocations)
                except Exception:
                    internal = AgentContractError("user result projection failed")
                    yield failed(
                        "user_result_projection_failed",
                        "查询结果未通过安全投影",
                        internal,
                    )
                    return
            requires_user_result = (
                account_reference_resolution is None
                or account_reference_resolution.state == "selected"
            ) and (
                bool(successful_invocations) or bool(plan.steps) and all(
                    step.capability_id in {item.id for item in bound_executable}
                    for step in plan.steps
                )
            )
            if user_result is not None:
                # 多目标运行的部分完成轮：结果已交付但信封里仍有本轮未覆盖的
                # pending 任务（如 查余额+转账 里余额已查、转账待续）。按
                # succeeded 收尾会把续办目标静默清空，客户永远无法继续办理；
                # 必须降级为结果+澄清，让会话状态把待办保留到下一轮。
                covered_task_ids = {step.task_id for step in plan.steps}
                has_pending_followup = any(
                    task.task_status
                    in {"pending", "needs_slots", "awaiting_confirmation", "needs_clarification"}
                    and task.task_id not in covered_task_ids
                    for task in envelope.tasks
                )
                response_status = (
                    "clarification_required"
                    if (
                        _successful_result_requires_clarification(
                            successful_invocations
                        )
                        or has_pending_followup
                    )
                    else "succeeded"
                )
            elif requires_user_result:
                response_status = "blocked"

        if response_status == "clarification_required" and successful_invocations:
            envelope, slot_evaluations = _retain_graph_execution_context(
                envelope,
                slot_evaluations,
                self._intent_graph,
                self._slot_evaluator,
                successful_invocations,
            )
            runtime_active_task = next(
                (
                    item for item in envelope.tasks
                    if item.task_id == runtime_active_task.task_id
                ),
                runtime_active_task,
            )

        try:
            if confirmation is not None:
                user_response = self._user_response_projector.project_confirmation(
                    confirmation,
                    task_progress=confirmation_progress,
                )
            elif guided_credit_card_facts is not None:
                user_response = (
                    self._user_response_projector
                    .project_credit_card_repayment_choice(
                        **guided_credit_card_facts
                    )
                )
                response_status = "clarification_required"
            elif (
                slot_reference_resolution is not None
                and slot_reference_resolution.state != "selected"
            ):
                if (
                    runtime_active_task is not None
                    and runtime_active_task.business_object == "transfer_order"
                ):
                    # 转账缺槽位（含 payee 实体未提取，如"给老婆打钱"
                    # 的"老婆"未识别到 semantic_values["payee"]）时直接
                    # 出卡片，不走 slot_resolution 的"请补充当前任务所需的
                    # 收款人"提问降级，让用户在卡片里一次补全所有缺的槽位。
                    transfer_evaluation = slot_evaluations.get(
                        runtime_active_task.task_id
                    )
                    if transfer_evaluation is not None:
                        single_progress = TaskProgress(
                            mode="sequential",
                            current=1,
                            total=1,
                            completed=0,
                            failed=0,
                            skipped=0,
                        )
                        user_response, clarification_form = (
                            _build_active_transfer_form(
                                self._user_response_projector,
                                runtime_active_task,
                                transfer_evaluation,
                                successful_invocations,
                                single_progress,
                            )
                        )
                        response_status = "clarification_required"
                    else:
                        if active_slot_decision is None:
                            raise UserResponseProjectionError(
                                "account resolution has no active slot decision"
                            )
                        user_response = self._user_response_projector.project_slot_resolution(
                            slot_type=active_slot_decision.slot_type or "",
                            slot_label=active_slot_decision.slot_label or "账户",
                            resolution=slot_reference_resolution,
                        )
                        if user_response is None:
                            raise UserResponseProjectionError(
                                "unresolved balance response is missing"
                            )
                        response_status = {
                            "clarification": "clarification_required",
                            "navigation": "navigation_only",
                            "notice": "blocked",
                        }[user_response.type]
                else:
                    if active_slot_decision is None:
                        raise UserResponseProjectionError(
                            "account resolution has no active slot decision"
                        )
                    user_response = self._user_response_projector.project_slot_resolution(
                        slot_type=active_slot_decision.slot_type or "",
                        slot_label=active_slot_decision.slot_label or "账户",
                        resolution=slot_reference_resolution,
                    )
                    if user_response is None:
                        raise UserResponseProjectionError(
                            "unresolved balance response is missing"
                        )
                    response_status = {
                        "clarification": "clarification_required",
                        "navigation": "navigation_only",
                        "notice": "blocked",
                    }[user_response.type]
            elif _is_single_canonical_faq(
                envelope, knowledge_task_ids, faq_analysis
            ):
                user_response = self._user_response_projector.project_faq(
                    faq_analysis.matches[0].entry
                )
                response_status = "succeeded"
            elif _is_uncovered_single_faq(
                envelope, knowledge_task_ids, faq_analysis, intent_constraints
            ):
                user_response = (
                    self._user_response_projector.project_notice_for_intent(
                        "faq_uncovered", envelope.tasks[0].intent_id
                    )
                )
                response_status = "unsupported"
            elif user_result is not None:
                if response_status == "clarification_required":
                    clarification_response = (
                        self._user_response_projector
                        .project_result_clarification(user_result)
                    )
                    if clarification_response is not None:
                        user_response = clarification_response
                        user_result = None
                    else:
                        user_response = (
                            self._user_response_projector
                            .project_result_with_continue(user_result)
                        )
                        user_result = None
                else:
                    user_response = self._user_response_projector.project_legacy_result(
                        user_result
                    )
            elif response_status == "blocked" and plan.status == "planned":
                user_response = self._user_response_projector.project_notice(
                    "runtime_failed"
                )
            elif plan.status == "navigation_only" and envelope.tasks:
                user_response = (
                    self._user_response_projector.project_navigation_for_intent(
                        envelope.tasks[0].intent_id
                    )
                )
                response_status = "navigation_only"
            elif plan.status == "clarification_required":
                task = runtime_active_task if envelope.tasks else None
                # 跨域信封（如 查余额+转账）里，收款人补录/多笔核对属于转账域
                # 的投影逻辑；按图谱 Skill 绑定圈定转账子集判定，不能要求整个
                # 信封同域，否则续办轮会跳过收款人目录校验而陷入核对循环。
                transfer_tasks = [
                    item for item in envelope.tasks
                    if uses_transfer_compatibility_adapter(self._intent_graph, item)
                ]
                if (
                    len(transfer_tasks) > 1
                    and all(item.task_id in slot_evaluations for item in transfer_tasks)
                ):
                    ordered_evaluations = [
                        slot_evaluations[item.task_id] for item in transfer_tasks
                    ]
                    requested_payees = [
                        str(item.semantic_values.get("payee", "")).strip()
                        for item in ordered_evaluations
                    ]
                    missing_payees = self._user_response_projector.missing_transfer_payees(
                        requested_payees,
                        successful_invocations,
                    )
                    if native_loop_outcome is not None:
                        missing_index = next(
                            (
                                index for index, item in enumerate(transfer_tasks)
                                if item.task_id == native_loop_outcome.active_goal_task_id
                                and requested_payees[index] in missing_payees
                            ),
                            None,
                        )
                    else:
                        missing_index = next(
                            (
                                index
                                for index, payee_name in enumerate(requested_payees)
                                if payee_name in missing_payees
                            ),
                            None,
                        )
                    if missing_index is not None:
                        evaluation = ordered_evaluations[missing_index]
                        amount_minor = evaluation.resolved_values.get("amountMinor")
                        from_account = str(
                            evaluation.semantic_values.get("fromAccount", "")
                        ).strip()
                    if (
                        missing_index is not None
                        and isinstance(amount_minor, int)
                        and not isinstance(amount_minor, bool)
                        and amount_minor > 0
                        and from_account
                    ):
                        payee_name = requested_payees[missing_index]
                        progress = TaskProgress(
                            mode="sequential",
                            current=missing_index + 1,
                            total=len(transfer_tasks),
                            completed=0,
                            failed=0,
                            skipped=0,
                        )
                        user_response = self._user_response_projector.project_transfer_form(
                            progress=progress,
                            payee_name=payee_name,
                            from_account_label=_format_transfer_account_label(
                                from_account
                            ),
                            amount_minor=amount_minor,
                        )
                        purpose = evaluation.semantic_values.get("purpose")
                        clarification_form = ClarificationForm.model_validate({
                            "schemaVersion": "1.0",
                            "clarificationId": f"acl_{uuid.uuid4().hex}",
                            "submissionToken": secrets.token_urlsafe(32),
                            "expiresAt": (
                                datetime.now(timezone.utc) + timedelta(minutes=5)
                            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                            "kind": "transfer_payee",
                            "fields": [
                                {
                                    "name": "payeeName",
                                    "label": "收款人",
                                    "type": "text",
                                    "required": True,
                                    "value": payee_name,
                                    "editable": False,
                                },
                                {
                                    "name": "accountNo",
                                    "label": "收款账号",
                                    "type": "bank_account",
                                    "required": True,
                                    "value": "",
                                    "editable": True,
                                },
                                {
                                    "name": "bankName",
                                    "label": "收款银行",
                                    "type": "bank_name",
                                    "required": True,
                                    "value": "中国建设银行",
                                    "editable": True,
                                },
                                {
                                    "name": "purpose",
                                    "label": "用途/备注（选填）",
                                    "type": "text",
                                    "required": False,
                                    "value": purpose.strip() if isinstance(purpose, str) else "",
                                    "editable": True,
                                },
                                {
                                    "name": "savePayee",
                                    "label": "保存为常用收款人",
                                    "type": "boolean",
                                    "required": False,
                                    "value": False,
                                    "editable": True,
                                },
                            ],
                        })
                    else:
                        user_response = self._user_response_projector.project_multi_transfer_clarification(
                            ordered_evaluations,
                            successful_invocations,
                        )
                else:
                    evaluation = (
                        slot_evaluations.get(task.task_id) if task is not None else None
                    )
                    # 单笔转账缺槽位时直接出卡片（与 resume 路径一致），
                    # 把已提取的槽位填进卡片、缺的槽位让用户在卡片里一次补全，
                    # 而不是逐个提问。即使 payee 实体未提取（如"给老婆打钱"
                    # 的"老婆"未被识别到 semantic_values["payee"]），转账
                    # 意图明确，也应出 transfer_full 卡片让用户在卡片里填
                    # payee 等所有缺的槽位。非转账任务则保持提问降级。
                    if (
                        task is not None
                        and task.business_object == "transfer_order"
                        and evaluation is not None
                    ):
                        single_progress = TaskProgress(
                            mode="sequential",
                            current=1,
                            total=1,
                            completed=0,
                            failed=0,
                            skipped=0,
                        )
                        user_response, clarification_form = _build_active_transfer_form(
                            self._user_response_projector,
                            task,
                            evaluation,
                            successful_invocations,
                            single_progress,
                        )
                    else:
                        user_response = self._user_response_projector.project_clarification(
                            task,
                            slot_evaluation=evaluation,
                            successful_invocations=successful_invocations,
                        )
                response_status = "clarification_required"
            elif plan.status in {"unsupported", "blocked"}:
                if plan.status == "unsupported" and envelope.tasks:
                    user_response = (
                        self._user_response_projector.project_notice_for_intent(
                            "unsupported", envelope.tasks[0].intent_id
                        )
                    )
                else:
                    user_response = self._user_response_projector.project_notice(plan.status)
                response_status = plan.status
        except Exception:
            logger.exception(
                "User response projection failed "
                "plan_status=%s response_status=%s confirmation=%s "
                "guided_cc=%s slot_res=%s user_result=%s "
                "envelope_tasks=%s active_task=%s slot_evals=%s",
                plan.status if plan else None,
                response_status,
                confirmation is not None,
                guided_credit_card_facts is not None,
                slot_reference_resolution is not None,
                user_result is not None,
                len(envelope.tasks) if envelope else 0,
                runtime_active_task.task_id if runtime_active_task else None,
                {k: type(v).__name__ for k, v in slot_evaluations.items()} if slot_evaluations else {},
            )
            await self._record_failure_semantic_memories(
                request, envelope, user_memory_hints,
                session_variables=extracted_variables,
            )
            internal = AgentContractError("user response projection failed")
            yield failed(
                "user_response_projection_failed",
                "用户响应未通过安全投影",
                internal,
            )
            return

        logger.debug(
            "Prefix gate check loop_advance_set=%s user_response=%s response_status=%s",
            loop_advance is not None,
            getattr(user_response, "type", None),
            response_status,
        )
        if (
            loop_advance is not None
            and user_response is not None
            and response_status != "succeeded"
        ):
            prefix_responses = []
            completed_ids = set(loop_advance.completed_task_ids)
            logger.debug(
                "Multi-task prefix projection envelope_tasks=%s completed_ids=%s "
                "recorded_invocations=%s loop_invocation_orders=%s "
                "response_status=%s user_response_type=%s",
                [item.task_id for item in envelope.tasks],
                sorted(completed_ids),
                {k: [i.get("capability_id") for i in v] for k, v in completed_task_result_invocations.items()},
                {
                    item.task_id: [
                        inv.capability_id
                        for inv in loop_advance.task_invocations
                        if inv.task_id == item.task_id
                    ]
                    for item in envelope.tasks
                },
                response_status,
                getattr(user_response, "type", None),
            )
            for task in envelope.tasks:
                if task.task_id not in completed_ids:
                    continue
                task_invocations = completed_task_result_invocations.get(
                    task.task_id,
                    [
                        {
                            "capability_id": item.capability_id,
                            "input": item.input,
                            "output": item.output,
                        }
                        for item in loop_advance.task_invocations
                        if item.task_id == task.task_id
                    ],
                )
                if not task_invocations:
                    continue
                if _successful_result_requires_clarification(task_invocations):
                    # The result belongs to the same unfinished goal and is
                    # already represented by the clarification response. Do
                    # not duplicate it as a completed-result prefix. Prefixes
                    # are reserved for genuinely completed sibling goals.
                    continue
                completed_result = project_user_result(task_invocations)
                logger.warning(
                    "Multi-task prefix task=%s projected=%s",
                    task.task_id,
                    getattr(completed_result, "kind", None),
                )
                if completed_result is not None:
                    prefix_responses.append(
                        self._user_response_projector.project_legacy_result(
                            completed_result
                        )
                    )
            logger.info(
                "Multi-task prefix outcome prefixes=%s",
                [getattr(item, "type", None) for item in prefix_responses],
            )
            if prefix_responses:
                user_responses = tuple([*prefix_responses, user_response])
                user_response = None

        # 部分 mock-only 场景：skipped task（execution_readiness=unsupported）
        # 需要追加"暂不支持"通知到 user_responses，让用户既看到暂不支持提示，
        # 又能继续完成非 mock-only 任务。通知插入在主响应之前。
        # 注意：prefix 必须无 actions（契约约束），故 project_notice 不带
        # recommended_action_key——不生成"打开相关服务"按钮，主响应中的
        # 澄清/结果才是用户可操作的入口。
        skipped_notices: list[UserResponse] = []
        for task in envelope.tasks:
            if task.task_status == "skipped":
                # 导航/入口类任务被跳过时不打扰用户：识别器常把业务
                # 请求附带关联到不可执行的入口意图（如"财富全景·申购
                # 理财"导航入口），这类跳过对用户没有信息量，且"暂不
                # 支持"文案会让用户误以为整个业务办不了——真正的业务
                # 任务仍在主响应中正常继续（如理财产品选择卡）。
                if getattr(task, "action", None) == "NAVIGATE":
                    continue
                try:
                    notice = (
                        self._user_response_projector.project_notice_for_intent(
                            "unsupported", task.intent_id, include_actions=False
                        )
                    )
                    if notice is not None:
                        skipped_notices.append(notice)
                except Exception:
                    logger.exception(
                        "Skipped task notice projection failed "
                        "intent=%s task_id=%s",
                        task.intent_id,
                        task.task_id,
                    )
        if skipped_notices:
            if user_responses is not None:
                # user_responses = (prefix..., main); 在 main 前插入 notices
                *prefix_parts, main_response = user_responses
                user_responses = tuple(
                    [*prefix_parts, *skipped_notices, main_response]
                )
            elif user_response is not None:
                user_responses = tuple([*skipped_notices, user_response])
                user_response = None

        if (
            todo_snapshot
            and self._capability_invoker is not None
            and hasattr(self._capability_invoker, "persist_agent_workflow")
        ):
            workflow_status = {
                "clarification_required": "awaiting_clarification",
                "awaiting_confirmation": "awaiting_confirmation",
                "succeeded": "completed",
                "planned": "running",
                "blocked": "failed",
                "unsupported": "failed",
                "navigation_only": "completed",
            }[response_status]
            if (
                native_loop_outcome is not None
                and loop_advance is not None
                and workflow_status
                in {"awaiting_clarification", "awaiting_confirmation"}
            ):
                reconciled = _reconcile_delivered_goal_todos(
                    envelope=envelope,
                    todo_snapshot=todo_snapshot,
                    outcome=native_loop_outcome,
                    covered_task_ids={
                        step.task_id for step in plan.steps
                    },
                    task_invocations=loop_advance.task_invocations,
                    excluded_task_ids={
                        step.task_id for step in plan.steps
                        if step.requires_user_confirmation
                    },
                )
                if reconciled is not None:
                    todo_snapshot, native_loop_outcome = reconciled
            try:
                if workflow_status in {"completed", "failed"}:
                    active_task_id = None
                    active_todo_id = None
                else:
                    try:
                        active_task_id, active_todo_id = _active_workflow_ids(
                            envelope, todo_snapshot, native_loop_outcome
                        )
                    except AgentContractError:
                        if len(envelope.tasks) != 1:
                            raise
                        active_task_id = envelope.tasks[0].task_id
                        reusable_todos = [
                            item for item in todo_snapshot
                            if item.status != "cancelled"
                        ]
                        if reusable_todos:
                            active_todo_id = reusable_todos[-1].id
                            todo_snapshot = tuple(
                                TodoSnapshotItem(
                                    id=item.id,
                                    status=(
                                        "in_progress"
                                        if item.id == active_todo_id
                                        else item.status
                                    ),
                                    depends_on=list(item.depends_on),
                                )
                                for item in todo_snapshot
                            )
                        else:
                            active_todo_id = (
                                f"{active_task_id}:{workflow_status}"
                            )
                            todo_snapshot = tuple(todo_snapshot) + (
                                TodoSnapshotItem(
                                    id=active_todo_id,
                                    status="in_progress",
                                    depends_on=[],
                                ),
                            )
                        if native_loop_outcome is not None:
                            native_loop_outcome = native_loop_outcome.model_copy(
                                update={
                                    "status": workflow_status,
                                    "active_goal_task_id": active_task_id,
                                    "active_todo_id": active_todo_id,
                                    "todo_revision": (
                                        native_loop_outcome.todo_revision + 1
                                    ),
                                    "next_action": workflow_status,
                                }
                            )
                # skipped task（mock-only）不应作为持久化委托目标——它们
                # 不在业务智能体台账中，混入会触发 _scoped_persist_todo_items
                # 的 "scoped empty" 断言。这里与 Todo ledger 初始化、
                # business_orchestrator 的过滤保持一致。
                active_envelope_tasks = [
                    item for item in envelope.tasks
                    if is_intent_task_in_play(item.task_status)
                ]
                scoped_todo_items = _scoped_persist_todo_items(
                    todo_snapshot or [],
                    [item.task_id for item in active_envelope_tasks],
                )
                persisted = await self._capability_invoker.persist_agent_workflow(
                    agent_run_id=request.agent_run_id,
                    business_workflow_id=workflow_id,
                    status=workflow_status,
                    delegated_task_ids=[
                        item.task_id for item in active_envelope_tasks
                    ],
                    expected_revision=workflow_persisted_revision,
                    envelope_revision=envelope.revision,
                    workflow_revision=workflow_persisted_revision + 1,
                    todo_revision=(
                        max(
                            native_loop_outcome.todo_revision,
                            stored_todo_revision,
                        )
                        if native_loop_outcome is not None else 1
                    ),
                    todo_snapshot=[
                        {
                            "id": item.id,
                            "status": item.status,
                            "dependsOn": list(item.depends_on),
                        }
                        for item in scoped_todo_items
                    ],
                    todo_digest=TodoLedger.digest(scoped_todo_items),
                    active_goal_task_id=active_task_id,
                    active_todo_id=active_todo_id,
                    suspension_reason=(
                        workflow_status
                        if workflow_status in {
                            "awaiting_clarification", "awaiting_confirmation", "waiting_external"
                        }
                        else None
                    ),
                    last_outcome=(
                        native_loop_outcome.model_dump()
                        if native_loop_outcome is not None
                        else {"status": workflow_status}
                    ),
                    pending_checkpoint_ids=(
                        (
                            [
                                item.checkpoint_id
                                for item in confirmation.confirmations
                            ]
                            if confirmation.confirmations
                            else [confirmation.checkpoint_id]
                        )
                        if confirmation is not None else []
                    ),
                )
                # 网关在 CAS 冲突时会以 Node 当前 revision 顶替重试，
                # 盲目 +1 会与真实落库 revision 脱节；以返回值为准。
                workflow_persisted_revision = int(
                    persisted.get("workflowRevision", workflow_persisted_revision + 1)
                )
            except AgentCapabilityInvocationError as exc:
                if exc.code != "agent_workflow_revision_conflict":
                    logger.exception(
                        "Business workflow persistence failed workflow_id=%s status=%s",
                        workflow_id,
                        workflow_status,
                    )
                    await self._record_failure_semantic_memories(
                        request, envelope, user_memory_hints,
                        session_variables=extracted_variables,
                    )
                    internal = AgentContractError("business workflow persistence failed")
                    yield failed(
                        "business_workflow_persistence_failed",
                        "业务智能体任务状态保存失败",
                        internal,
                    )
                    return
                # CAS 双重冲突的兜底：本轮投影与用户结果已完整成形，工作流
                # 行暂时落后不影响下一轮恢复（恢复按 Node 当前 revision
                # 重读）。把已可交付的轮次整体作废的代价远大于工作流行
                # 短暂落后，记日志降级放行。
                logger.warning(
                    "Business workflow persistence degraded on revision conflict"
                    " workflow_id=%s status=%s",
                    workflow_id,
                    workflow_status,
                )
            except Exception:
                logger.exception(
                    "Business workflow persistence failed workflow_id=%s status=%s",
                    workflow_id,
                    workflow_status,
                )
                await self._record_failure_semantic_memories(
                    request, envelope, user_memory_hints,
                    session_variables=extracted_variables,
                )
                internal = AgentContractError("business workflow persistence failed")
                yield failed(
                    "business_workflow_persistence_failed",
                    "业务智能体任务状态保存失败",
                    internal,
                )
                return

        try:
            # 已交付结果的任务（台账 succeeded 且有可信调用背书）在任何
            # 响应状态下都不应再作为待办 goal 下发：澄清轮里的前缀结果
            # 交付后，若 goal 仍留在会话状态，后续意图轮会把它合并进新
            # envelope，造成"已答复的问题被反复续跑"。
            delivered_task_ids = frozenset(completed_task_result_invocations)
            if loop_advance is not None:
                delivered_task_ids |= frozenset(
                    loop_advance.completed_task_ids
                )
            result = RuntimeAgentRunResponse(
                agentRunId=request.agent_run_id,
                conversationId=request.conversation_id,
                status=response_status,
                intentTaskEnvelope=envelope,
                capabilityPlan=plan,
                userResult=user_result,
                userResponse=user_response,
                userResponses=user_responses,
                confirmation=confirmation,
                clarificationForm=clarification_form,
                conversationState=_build_conversation_state(
                    response_status,
                    envelope,
                    slot_evaluations,
                    covered_task_ids=(
                        # awaiting_confirmation/clarification_required 的活
                        # 跃任务尚未向用户交付最终结果——即便 DeepAgent
                        # 编排中调用了中间查询能力（如风险测评、持仓查询）
                        # 使台账标记 goal=succeeded，该任务仍需用户补充
                        # slot 或确认，不应从 goals 中排除。
                        # 只排除已交付结果的前缀任务（多目标场景中先完成
                        # 的查询），活跃 task 通过 active_goal_task_id
                        # 钉定保留；succeeded 路径有独立的 task_status
                        # 门控，不受影响。
                        delivered_task_ids
                        - (
                            {native_loop_outcome.active_goal_task_id}
                            if native_loop_outcome is not None
                            and native_loop_outcome.active_goal_task_id
                            else set()
                        )
                        if response_status in {
                            "awaiting_confirmation",
                            "clarification_required",
                        }
                        else frozenset(
                            step.task_id for step in plan.steps
                        )
                        | delivered_task_ids
                    ),
                ),
                memory_candidate_hints=tuple(user_memory_hints),
            )
        except Exception:
            logger.exception(
                "Terminal response projection failed workflow_id=%s status=%s",
                workflow_id,
                response_status,
            )
            await self._record_failure_semantic_memories(
                request, envelope, user_memory_hints,
                session_variables=extracted_variables,
            )
            internal = AgentContractError("terminal response projection failed")
            yield failed(
                "user_response_projection_failed",
                "用户响应未通过安全投影",
                internal,
            )
            return

        async for item in finish(
            result,
            task_count=len(envelope.tasks),
            step_count=len(plan.steps),
        ):
            yield item

    def _validate_plan(
        self,
        plan: CapabilityPlan,
        envelope: IntentTaskEnvelope,
        constraints: list[IntentConstraint],
    ) -> None:
        """校验计划必须使用与规划时完全相同的约束集（含语义扩召候选），
        否则 allowlist 会出现规划可见、校验不可见的候选偏移。"""
        constraint_by_id = {item.intent_id: item for item in constraints}
        knowledge_task_ids = {
            task.task_id
            for task in envelope.tasks
            if constraint_by_id.get(task.intent_id) is not None
            and constraint_by_id[task.intent_id].execution_mode == "knowledge-only"
        }
        if any(step.task_id in knowledge_task_ids for step in plan.steps):
            raise AgentContractError(
                "knowledge-only task cannot contain capability steps"
            )
        if knowledge_task_ids and len(knowledge_task_ids) == len(envelope.tasks) and (
            plan.status != "navigation_only" or plan.steps
        ):
            raise AgentContractError(
                "knowledge-only intent requires an empty navigation plan"
            )
        allowed_capabilities = {
            capability_id
            for constraint in constraints
            for capability_id in constraint.capability_bindings
        }
        task_ids = {task.task_id for task in envelope.tasks}
        for step in plan.steps:
            if step.capability_id not in allowed_capabilities:
                raise AgentContractError("capability allowlist violation")
            if step.task_id not in task_ids:
                raise AgentContractError("plan references an unknown task")
        _reject_sensitive_keys(plan.model_dump())

    async def _run_business(
        self,
        payload: dict[str, Any],
        *,
        session_id: str,
        agent_run_id: str,
        execution_allowlist: set[str],
        input_defaults: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        runtime_payload = dict(payload)
        runtime_payload["runtime_capability_allowlist"] = sorted(
            execution_allowlist
        )
        try:
            iteration_timeout = float(
                os.getenv("AI_BANK_BUSINESS_ITERATION_TIMEOUT_SECONDS", "90")
            )
        except ValueError:
            iteration_timeout = 90.0
        iteration_timeout = max(1.0, min(120.0, iteration_timeout))

        async def run_iteration():
            return await self._business_runner.run(
                runtime_payload, session_id=session_id
            )

        if self._capability_invoker is None:
            return await asyncio.wait_for(
                run_iteration(), timeout=iteration_timeout
            ), []
        with self._capability_invoker.activate(
            agent_run_id, execution_allowlist, input_defaults=input_defaults
        ) as context:
            result = await asyncio.wait_for(
                run_iteration(), timeout=iteration_timeout
            )
            return result, list(context.successful_invocations)

    async def _resolve_graph_reference_chain(
        self,
        *,
        agent_run_id: str,
        message: str,
        task: IntentTask,
        candidate: IntentCandidate,
        evaluation: SlotEvaluation,
        intent_shape: str,
        executable_ids: set[str],
        successful_invocations: list[dict[str, Any]],
    ) -> tuple[
        IntentTask,
        SlotEvaluation,
        TaskSlotDecision,
        AccountReferenceResolution | CardReferenceResolution | ProductReferenceResolution | HoldingReferenceResolution | PartyReferenceResolution | None,
        list[dict[str, Any]],
    ]:
        """Resolve graph-declared references until human input is required.

        Provider identifiers live only in the private runtime envelope for the
        current turn. A masked semantic reference is retained so the next turn
        can safely re-resolve the same customer-owned entity.
        """

        contract = candidate.slot_contract
        if contract is None:
            raise TaskSlotRuntimeError("task graph contract is incomplete")
        current_task = task
        current_evaluation = evaluation
        invocations = list(successful_invocations)
        trusted_references: dict[str, tuple[str, str]] = {}
        for _ in range(len(contract.slots) + 1):
            decision = decide_active_task(
                task=current_task,
                candidate=candidate,
                evaluation=current_evaluation,
                intent_shape=intent_shape,
            )
            if decision.readiness != "resolve":
                # A ready query decision intentionally contains no pending
                # slot. Preserve the single provider-resolved binding from
                # this chain so the generic read-plan builder can execute the
                # graph-declared query without reinterpreting user language.
                resolved_definitions = [
                    definition for definition in contract.slots
                    if definition.resolver_capability_id is not None
                    and definition.resolved_field
                    in current_evaluation.resolved_values
                ]
                if decision.readiness == "execute" and len(resolved_definitions) == 1:
                    definition = resolved_definitions[0]
                    decision = TaskSlotDecision(
                        task_id=decision.task_id,
                        slot_name=definition.name,
                        slot_type=definition.type,
                        slot_label=definition.label,
                        resolver_capability_id=definition.resolver_capability_id,
                        resolved_field=definition.resolved_field,
                        execution_capability_id=decision.execution_capability_id,
                        response_policy=decision.response_policy,
                        readiness=decision.readiness,
                    )
                return current_task, current_evaluation, decision, None, invocations
            if decision.resolver_capability_id is None or decision.slot_name is None:
                raise TaskSlotRuntimeError("reference resolver binding is incomplete")
            resolver_calls = [
                item for item in invocations
                if item.get("capability_id") == decision.resolver_capability_id
            ]
            if any(item.get("input") != {} for item in resolver_calls):
                raise TaskSlotRuntimeError(
                    "reference resolver input is outside the graph contract"
                )
            if len(resolver_calls) > 1:
                raise TaskSlotRuntimeError("reference resolver cardinality is invalid")
            if not resolver_calls:
                discovered = await self._invoke_read_once(
                    agent_run_id,
                    decision.resolver_capability_id,
                    {},
                    executable_ids,
                )
                invocations = _merge_invocations(invocations, discovered)
            semantic = current_evaluation.semantic_values.get(decision.slot_name)
            if decision.slot_type == "account_reference":
                records = _strict_invocation_list(
                    invocations, decision.resolver_capability_id, "accounts"
                )
                resolution = _resolve_reference_evidence(
                    resolve_account_reference, semantic, message, records
                )
                if resolution.state != "selected":
                    return current_task, current_evaluation, decision, resolution, invocations
                option = resolution.options[0]
                resolved_value = resolution.account_id
                semantic_value = f"{option.label} · 尾号{option.tail}"
            elif decision.slot_type == "card_reference":
                records = _strict_invocation_list(
                    invocations, decision.resolver_capability_id, "cards"
                )
                resolution = _resolve_reference_evidence(
                    resolve_card_reference, semantic, message, records
                )
                if resolution.state != "selected":
                    return current_task, current_evaluation, decision, resolution, invocations
                resolved_value = resolution.card_id
                semantic_value = resolution.semantic_reference
            elif decision.slot_type == "product_reference":
                records = _strict_invocation_list(
                    invocations, decision.resolver_capability_id, "products"
                )
                resolution = _resolve_reference_evidence(
                    resolve_product_reference, semantic, message, records
                )
                if resolution.state != "selected":
                    return current_task, current_evaluation, decision, resolution, invocations
                resolved_value = resolution.product_id
                semantic_value = resolution.semantic_reference
            elif decision.slot_type == "holding_reference":
                records = _strict_invocation_list(
                    invocations, decision.resolver_capability_id, "holdings"
                )
                resolution = _resolve_reference_evidence(
                    resolve_holding_reference, semantic, message, records
                )
                if resolution.state != "selected":
                    return current_task, current_evaluation, decision, resolution, invocations
                resolved_value = resolution.holding_id
                semantic_value = resolution.semantic_reference
            elif decision.slot_type == "party_reference":
                records = _strict_invocation_list(
                    invocations, decision.resolver_capability_id, "payees"
                )
                resolution = _resolve_reference_evidence(
                    resolve_party_reference, semantic, message, records
                )
                if resolution.state != "selected":
                    return current_task, current_evaluation, decision, resolution, invocations
                resolved_value = resolution.party_id
                semantic_value = resolution.semantic_reference
            else:
                raise TaskSlotRuntimeError("reference slot type is unsupported")
            if (
                decision.resolved_field is None
                or not isinstance(resolved_value, str)
                or not isinstance(semantic_value, str)
            ):
                raise TaskSlotRuntimeError("resolved reference is incomplete")
            next_slots = dict(current_task.slots)
            next_slots[decision.slot_name] = SlotValue(
                value=semantic_value, source="context", confidence=1
            )
            next_slots[decision.resolved_field] = SlotValue(
                value=resolved_value, source="context", confidence=1
            )
            trusted_references[decision.slot_name] = (
                semantic_value,
                resolved_value,
            )
            current_task = current_task.model_copy(update={"slots": next_slots})
            current_evaluation = self._slot_evaluator.evaluate(
                contract=contract,
                current_slots=next_slots,
                conversation_slots={},
            )
            try:
                # Re-evaluation intentionally distrusts every raw ``*Id``.
                # Re-attach only references resolved by provider calls in this
                # invocation, including earlier slots in the same chain.
                for slot_name, trusted in trusted_references.items():
                    definition = contract.slot(slot_name)
                    current_evaluation = with_provider_resolved_reference(
                        current_evaluation,
                        definition,
                        semantic_value=trusted[0],
                        resolved_value=trusted[1],
                    )
            except (KeyError, ValueError) as exc:
                raise TaskSlotRuntimeError(
                    "provider reference could not be attached to the slot contract"
                ) from exc
            current_task = current_task.model_copy(update={
                "execution_readiness": current_evaluation.readiness
            })
        raise TaskSlotRuntimeError("reference resolution did not converge")

    async def _invoke_read_once(
        self,
        agent_run_id: str,
        capability_id: str,
        input_value: dict[str, Any],
        executable_ids: set[str],
    ) -> list[dict[str, Any]]:
        if self._capability_invoker is None:
            raise AgentContractError("capability invoker is unavailable")
        if capability_id not in executable_ids:
            raise AgentContractError("capability is not executable for this run")
        with self._capability_invoker.activate(
            agent_run_id,
            {capability_id},
            call_budgets={capability_id: 1},
        ) as context:
            await self._capability_invoker.invoke_read_capability(
                capability_id, input_value
            )
            return list(context.successful_invocations)

    async def _execute_ready_read_steps(
        self,
        plan: CapabilityPlan,
        agent_run_id: str,
        execution_allowlist: set[str],
        successful_invocations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self._capability_invoker is None or plan.status != "planned":
            return successful_invocations
        pending = [
            step for step in plan.steps
            if not step.requires_user_confirmation
            and step.capability_id in execution_allowlist
            and not _contains_binding_reference(step.input_bindings)
            and not any(
                item.get("capability_id") == step.capability_id
                and item.get("input") == step.input_bindings
                for item in successful_invocations
            )
        ]
        if not pending:
            return successful_invocations
        with self._capability_invoker.activate(agent_run_id, execution_allowlist) as context:
            for step in pending:
                await self._capability_invoker.invoke_read_capability(
                    step.capability_id, step.input_bindings
                )
            return successful_invocations + list(context.successful_invocations)

    async def _resolve_transfer_confirmation(
        self,
        request: RuntimeAgentRunRequest,
        envelope: IntentTaskEnvelope,
        slot_evaluations: dict[str, SlotEvaluation],
        constraints: list[IntentConstraint],
        execution_allowlist: set[str],
        successful_invocations: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        if self._capability_invoker is None or not envelope.tasks:
            return None, []
        required_bindings = {
            "bank.account.list", "bank.payee.list", "bank.payee.validate",
            "bank.transfer.order.create",
        }
        constraint_by_intent = {item.intent_id: item for item in constraints}
        # 五个静默 return None 出口的可观测性：逐门给出关闭原因，否则
        # 确认卡缺失时无从定位是哪道门拦下（约束缺失/模式不符/绑定不全/
        # 实体不可解析/校验失败表现完全相同）。
        gate_reasons: list[str] = []
        for task in envelope.tasks:
            constraint = constraint_by_intent.get(task.intent_id)
            if not uses_transfer_compatibility_adapter(self._intent_graph, task):
                gate_reasons.append(f"{task.task_id}:adapter_not_applicable")
            elif slot_evaluations.get(task.task_id) is None:
                gate_reasons.append(f"{task.task_id}:slot_evaluation_missing")
            elif constraint is None:
                gate_reasons.append(f"{task.task_id}:constraint_missing")
            elif constraint.execution_mode != "confirm-required":
                gate_reasons.append(
                    f"{task.task_id}:execution_mode={constraint.execution_mode}"
                )
            elif not required_bindings.issubset(
                set(constraint.capability_bindings)
            ):
                gate_reasons.append(
                    f"{task.task_id}:constraint_bindings_missing="
                    + ",".join(sorted(
                        required_bindings.difference(
                            constraint.capability_bindings
                        )
                    ))
                )
        allowlist_missing = required_bindings.difference(
            {"bank.transfer.order.create"}
        ).difference(execution_allowlist)
        if gate_reasons or allowlist_missing:
            logger.warning(
                "Transfer confirmation gate closed reasons=[%s] allowlist_missing=%s",
                " ".join(gate_reasons),
                ",".join(sorted(allowlist_missing)),
            )
            return None, []

        resolution_invocations: list[dict[str, Any]] = []
        combined_invocations = list(successful_invocations)
        # Always re-invoke bank.account.list and bank.payee.list to get fresh
        # data. The LLM agent's invocations from read_context may be stale or
        # incomplete. Replacing ensures _invocation_items returns current data.
        for capability_id in ("bank.account.list", "bank.payee.list"):
            try:
                discovered = await self._invoke_read_once(
                    request.agent_run_id, capability_id, {}, execution_allowlist
                )
            except Exception:
                logger.warning(
                    "%s invocation failed during transfer resolution",
                    capability_id,
                )
                continue
            if not discovered:
                continue
            # Remove stale invocations for this capability_id+input so the
            # fresh result is used by _invocation_items downstream.
            combined_invocations = [
                item for item in combined_invocations
                if not (
                    item.get("capability_id") == capability_id
                    and item.get("input") == {}
                )
            ] + discovered
            resolution_invocations = (
                [
                    item for item in resolution_invocations
                    if not (
                        item.get("capability_id") == capability_id
                        and item.get("input") == {}
                    )
                ] + discovered
            )

        if len(envelope.tasks) != 1:
            return None, resolution_invocations

        task = envelope.tasks[0]
        evaluation = slot_evaluations[task.task_id]

        # fromAccount（付款账户）涉及资金出口，必须经用户显式选择后方可进入确认。
        # 允许两种合法来源：
        #   1. user_selection — 用户通过卡片/表单 select 选择（source 由网关注入）
        #   2. user_explicit — 用户在消息中显式提及（如点击 action 按钮后发送
        #      "使用工资卡尾号0001转账"），但需额外校验 fromAccount 值确实出现在
        #      用户消息中，防止 LLM 将上下文提取的 fromAccount 误标为 user_explicit
        #      绕过 _assess 的 account_reference 守卫。
        from_account_slot = task.slots.get("fromAccount")
        from_account_source = (
            from_account_slot.source if from_account_slot is not None else None
        )
        from_account_value = (
            str(from_account_slot.value).strip()
            if from_account_slot is not None else ""
        )
        _from_account_validated = False
        if from_account_source == "user_selection":
            _from_account_validated = True
        elif from_account_source == "user_explicit" and from_account_value:
            msg_text = str(request.message or "")
            msg_digits = re.sub(r"\D", "", msg_text)
            fa_digits = re.sub(r"\D", "", from_account_value)
            fa_last4 = fa_digits[-4:] if len(fa_digits) >= 4 else fa_digits
            mentioned = (
                (from_account_value in msg_text)
                or (fa_last4 and fa_last4 in msg_digits)
                or any(
                    kw in msg_text
                    for kw in ("工资卡", "储蓄卡", "信用卡")
                    if kw in from_account_value
                )
            )
            if mentioned:
                _from_account_validated = True
            else:
                logger.warning(
                    "Transfer confirmation: fromAccount source=user_explicit"
                    " but value '%s' not found in user message; trying fallback recovery",
                    from_account_value,
                )

        if not _from_account_validated:
            # Fallback: 批量转账第二笔及后续任务可能因 conversation_context
            # 往返丢失 fromAccount 的 source，或 fromAccount 通过 shared_slots
            # 从第一笔传播但用户当前消息（如"继续"）不包含账户值。尝试通过
            # 账户列表（银行权威数据）验证 fromAccount 值并恢复 source；若仅
            # 有单一账户则自动选择。用户已在第一笔确认了同一付款账户，同批
            # 继承无需二次追问。
            _fallback_accounts = _invocation_items(
                combined_invocations, "bank.account.list", "accounts"
            )
            _recovered = False
            if from_account_value and _fallback_accounts:
                _matched = _unique_named_match(
                    _fallback_accounts,
                    _slot_values(task.slots, ("fromAccount", "fromAccountName", "fromAccountReference")),
                    from_account_value,
                )
                if _matched is not None:
                    from_account_slot = SlotValue(
                        value=from_account_value,
                        source="user_selection",
                        confidence=1,
                    )
                    task = task.model_copy(update={
                        "slots": {**task.slots, "fromAccount": from_account_slot}
                    })
                    _recovered = True
                    logger.info(
                        "fromAccount recovered by value match: %s",
                        from_account_value,
                    )
            if not _recovered and _fallback_accounts and len(_fallback_accounts) == 1:
                from_account_slot = SlotValue(
                    value=_fallback_accounts[0].get("name", ""),
                    source="user_selection",
                    confidence=1,
                )
                task = task.model_copy(update={
                    "slots": {**task.slots, "fromAccount": from_account_slot}
                })
                _recovered = True
                logger.info(
                    "fromAccount auto-selected from single account: %s",
                    from_account_slot.value,
                )
            # context 级恢复：当 task.slots 完全缺失 fromAccount 且有多账户
            # 时，从 request.conversation_context（shared_slots / goals）中
            # 恢复用户已选择的 fromAccount。批量转账跨轮次场景中，
            # _sibling_from_accounts 传播和 envelope 级复制可能均已失效。
            if not _recovered:
                _ctx = getattr(request, "conversation_context", None)
                if _ctx is not None:
                    _ctx_fa: SlotValue | None = None
                    for _slot in getattr(_ctx, "shared_slots", []) or []:
                        if (
                            _slot.name == "fromAccount"
                            and getattr(_slot, "source", None) in ("user_explicit", "user_selection")
                            and _slot.value
                        ):
                            _ctx_fa = SlotValue(
                                value=_slot.value, source=_slot.source, confidence=1,
                            )
                            break
                    if _ctx_fa is None:
                        for _goal in getattr(_ctx, "goals", []) or []:
                            for _slot in getattr(_goal, "slots", []) or []:
                                if (
                                    _slot.name == "fromAccount"
                                    and getattr(_slot, "source", None) in ("user_explicit", "user_selection")
                                    and _slot.value
                                ):
                                    _ctx_fa = SlotValue(
                                        value=_slot.value, source=_slot.source, confidence=1,
                                    )
                                    break
                            if _ctx_fa is not None:
                                break
                    if _ctx_fa is not None:
                        from_account_slot = _ctx_fa
                        task = task.model_copy(update={
                            "slots": {**task.slots, "fromAccount": from_account_slot}
                        })
                        _recovered = True
                        logger.info(
                            "fromAccount recovered from conversation_context in"
                            " _resolve_transfer_confirmation: value=%s source=%s",
                            _ctx_fa.value, _ctx_fa.source,
                        )
            if not _recovered:
                logger.warning(
                    "Transfer confirmation skipped: fromAccount not"
                    " user-selected source=%s accounts=%d",
                    from_account_source or "missing",
                    len(_fallback_accounts) if _fallback_accounts else 0,
                )
                return None, resolution_invocations

        accounts = _invocation_items(
            combined_invocations, "bank.account.list", "accounts"
        )
        payees = _invocation_items(
            combined_invocations, "bank.payee.list", "payees"
        )
        payee_refs = _slot_values(task.slots, ("payee", "payeeName", "payeeReference"))
        account = _unique_named_match(
            accounts,
            _slot_values(task.slots, ("fromAccount", "fromAccountName", "fromAccountReference")),
            request.message,
        )
        payee = _unique_named_match(
            payees,
            payee_refs,
            request.message,
        )
        amount_minor = evaluation.resolved_values.get("amountMinor")
        currency = evaluation.resolved_values.get("currency")
        purpose = evaluation.resolved_values.get("purpose")
        if (
            account is None or payee is None
            or not isinstance(amount_minor, int) or isinstance(amount_minor, bool)
            or amount_minor <= 0
            or not isinstance(currency, str) or not currency
        ):
            logger.warning(
                "Transfer confirmation entities unresolved account=%s payee=%s"
                " amount_minor=%s currency=%s",
                account is not None,
                payee is not None,
                amount_minor if isinstance(amount_minor, int) else "invalid",
                currency,
            )
            return None, resolution_invocations

        with self._capability_invoker.activate(
            request.agent_run_id, execution_allowlist
        ) as context:
            validation = await self._capability_invoker.invoke_read_capability(
                "bank.payee.validate", {"payeeId": payee["id"]}
            )
            added_invocations = _merge_invocations(
                resolution_invocations, list(context.successful_invocations)
            )
        if validation.get("valid") is not True or validation.get("status") != "ACTIVE":
            logger.warning(
                "Transfer confirmation payee validation rejected valid=%s status=%s",
                validation.get("valid"),
                validation.get("status"),
            )
            return None, added_invocations
        resolved_input = {
            "fromAccountId": account["id"],
            "payeeId": payee["id"],
            "amountMinor": amount_minor,
            "currency": currency,
        }
        if isinstance(purpose, str) and purpose.strip():
            resolved_input["purpose"] = purpose.strip()
        # 展示字段（收款人卡号/银行名/付款账户尾号）不参与能力调用，仅供
        # _batch_transfer_summary 渲染。capability gateway 用 AJV 校验 input，
        # additionalProperties: false 会拒绝这些字段（422），因此必须与
        # resolved_input 分离，通过 display_facts 单独携带，由
        # _confirmation_plan_from_resolution 合并进 input_bindings。
        display_facts: dict[str, Any] = {}
        if isinstance(account.get("name"), str):
            display_facts["fromAccountName"] = account["name"]
        if isinstance(account.get("maskedNo"), str):
            display_facts["fromAccountMaskedNo"] = account["maskedNo"]
        if isinstance(payee.get("name"), str):
            display_facts["payeeName"] = payee["name"]
        if isinstance(payee.get("maskedNo"), str):
            display_facts["payeeMaskedNo"] = payee["maskedNo"]
        if isinstance(payee.get("bankName"), str):
            display_facts["payeeBankName"] = payee["bankName"]
        return {
            "code": "graph_entity_resolution_available",
            "intent_id": task.intent_id,
            "capability_id": "bank.transfer.order.create",
            "resolved_input": resolved_input,
            "display_facts": display_facts,
            "requires_user_confirmation": True,
            "evidence_capability_ids": [
                "bank.account.list", "bank.payee.list", "bank.payee.validate",
            ],
        }, added_invocations


def _slot_value(slots: dict[str, Any], name: str) -> Any:
    item = slots.get(name)
    return item.value if item is not None else None


def _continuity_description_echoes(claimed: str, existing: str) -> bool:
    """跳转判定的"新任务描述"是否只是既有目标描述的回显。

    识别器偶发把续答误判为 CREATE_TASK，且把播种时喂给它的目标描述
    （意图标题+状态+已知槽位）原样或截取回填为"新任务描述"。归一化
    （去空白）后互为包含即视为回显——指向既有任务的"新建"是续接，
    不是跳转。
    """
    a = "".join(claimed.split())
    b = "".join(existing.split())
    return bool(a) and bool(b) and (a in b or b in a)


def _format_transfer_account_label(value: str) -> str:
    """Keep only a human label and an optional four-digit tail for display."""

    normalized = re.sub(r"\s+", "", value)
    tail_match = re.search(r"(?:尾号)?(\d{4})$", normalized)
    tail = tail_match.group(1) if tail_match else None
    label = re.sub(r"(?:尾号)?\d{4}$", "", normalized).strip(" ·-")
    if not label:
        label = "已选账户"
    return f"{label} · 尾号{tail}" if tail else label


def _slot_values(slots: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    return [
        str(value)
        for name in names
        if (value := _slot_value(slots, name)) is not None
    ]


def _invocation_items(
    invocations: list[dict[str, Any]], capability_id: str, output_key: str
) -> list[dict[str, Any]]:
    for invocation in reversed(invocations):
        if invocation.get("capability_id") != capability_id:
            continue
        output = invocation.get("output")
        items = output.get(output_key) if isinstance(output, dict) else None
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _resolve_reference_evidence(resolver, semantic, message, records):
    """Resolve one semantic reference from model evidence with message fallback.

    模型抽取的语义值优先；其无法唯一选中时回退用户原话再解析一次。
    低质量占位语义值（如「账户」）不应遮蔽消息中的决定性证据（如「尾号6217」）；
    两个证据源都无法唯一选中才按歧义返回，解析器 fail-closed 语义保持不变。
    """

    source = semantic if isinstance(semantic, str) and semantic.strip() else None
    resolution = resolver(source if source is not None else message, records)
    if resolution.state == "selected" or source is None or source == message:
        return resolution
    return resolver(message, records)


def _strict_invocation_list(
    invocations: list[dict[str, Any]], capability_id: str, output_key: str
) -> list[Any] | None:
    """Return a trusted list while preserving missing invocation versus empty output."""

    for invocation in reversed(invocations):
        if invocation.get("capability_id") != capability_id:
            continue
        output = invocation.get("output")
        if not isinstance(output, dict) or output_key not in output:
            raise AccountReferenceResolutionError("capability output is missing the expected list")
        items = output[output_key]
        if not isinstance(items, list):
            raise AccountReferenceResolutionError("capability output list is invalid")
        return items
    return None


def _empty_plan(
    envelope: IntentTaskEnvelope,
    status: str,
    run_suffix: str,
) -> CapabilityPlan:
    return CapabilityPlan.model_validate(
        {
            "schema_version": "1.0",
            "business_agent_run_id": (
                f"bar_{run_suffix}_{uuid.uuid4().hex[:16]}"
            ),
            "delegation_id": envelope.delegation_id,
            "status": status,
            "steps": [],
        }
    )


def _single_task_envelope(
    envelope: IntentTaskEnvelope,
    task: Any,
) -> IntentTaskEnvelope:
    projected = task.model_dump()
    # 单任务投影中依赖必然悬空（契约要求 dependencies 引用 envelope 内
    # 其他任务）；依赖语义属于多任务 envelope，投影时去除。
    projected["dependencies"] = []
    return IntentTaskEnvelope.model_validate({
        **envelope.model_dump(),
        "intent_shape": "single_intent",
        "tasks": [projected],
        "ambiguities": [],
    })


def _confirmation_plan_from_resolution(
    envelope: IntentTaskEnvelope,
    task_id: str,
    resolution: dict[str, Any],
) -> CapabilityPlan:
    capability_id = resolution.get("capability_id")
    resolved_input = resolution.get("resolved_input")
    if (
        capability_id != "bank.transfer.order.create"
        or not isinstance(resolved_input, dict)
        or resolution.get("requires_user_confirmation") is not True
    ):
        raise AgentContractError("business loop confirmation resolution is invalid")
    # input_bindings 合并 resolved_input（严格匹配 inputSchema 的能力入参）
    # 与 display_facts（收款人卡号/银行名/付款账户尾号等展示字段）。
    # 展示字段仅供 _batch_transfer_summary 渲染确认卡，不得进入
    # create_action_checkpoint 的 input——capability gateway 用 AJV 校验
    # input，additionalProperties: false 会拒绝额外字段（422）。
    input_bindings = dict(resolved_input)
    display_facts = resolution.get("display_facts")
    if isinstance(display_facts, dict):
        input_bindings.update(display_facts)
    return CapabilityPlan.model_validate({
        "schema_version": "1.0",
        "business_agent_run_id": "bar_loop_" + uuid.uuid4().hex[:16],
        "delegation_id": envelope.delegation_id,
        "status": "planned",
        "steps": [{
            "step_id": task_id + ":confirm",
            "task_id": task_id,
            "capability_id": capability_id,
            "input_bindings": input_bindings,
            "requires_user_confirmation": True,
            "depends_on": [],
        }],
    })


def _todo_snapshot_after_receipts(
    envelope: IntentTaskEnvelope,
    restored: TodoLedgerSnapshot,
    receipts: list[BusinessWorkflowReceipt],
    revision: int,
) -> TodoLedgerSnapshot:
    """授权回执的确定性台账重放。

    确认恢复轮的授权回执是不可变执行事实。台账必须以恢复的权威快照为
    基准、按回执把对应目标置为完成，而不是采信模型本轮对台账的改写：
    轻量模型在授权执行后可能整表重建 Todo（语义化自命名），id 集与信封
    错位会让覆盖校验把已成功的执行整体作废。
    """
    succeeded_ids = {
        item.task_id for item in receipts if item.status == "SUCCEEDED"
    }
    delegated_ids = tuple(task.task_id for task in envelope.tasks)

    def _settled(item: TodoSnapshotItem) -> TodoSnapshotItem:
        if item.status in {"completed", "cancelled"}:
            return item
        if any(
            todo_belongs_to_goal(item.id, task_id, delegated_ids)
            for task_id in succeeded_ids
        ):
            return item.model_copy(update={"status": "completed"})
        return item

    items = tuple(_settled(item) for item in restored.items)
    return TodoLedgerSnapshot(
        revision=revision,
        digest=TodoLedger.digest(items),
        items=items,
    )


def _reconcile_delivered_goal_todos(
    *,
    envelope: IntentTaskEnvelope,
    todo_snapshot: tuple,
    outcome: BusinessLoopOutcome,
    covered_task_ids: set[str],
    task_invocations: tuple,
    excluded_task_ids: frozenset[str] | set[str] = frozenset(),
) -> tuple[tuple, BusinessLoopOutcome] | None:
    """多目标部分完成轮的 Todo 台账对账。

    业务模型在暂停（等待澄清/确认）前，往往只把已交付结果的目标推进到
    in_progress；若不对账，恢复轮会把它当作未完成重跑（如 查余额+转账
    中余额被反复查询）。仅当目标满足三个机械可证的条件时才翻平：
    模型声明本轮覆盖、存在该目标的权威调用记录、且结果不需要补充用户
    输入。等待本人确认的目标不算已交付（动作尚未获授权），必须排除，
    否则确认恢复轮的归属校验会失败。活动指针随之前移到下一个未完成
    目标，恢复轮的能力边界因此直接指向后续目标，而不是停留在已交付
    目标上。
    """
    if not todo_snapshot or not covered_task_ids:
        return None
    invocations_by_task: dict[str, list[dict[str, Any]]] = {}
    for entry in task_invocations:
        invocations_by_task.setdefault(entry.task_id, []).append({
            "capability_id": entry.capability_id,
            "input": entry.input,
            "output": entry.output,
        })

    def _owner(item_id: str) -> str | None:
        for task in envelope.tasks:
            if (
                item_id == task.task_id
                or item_id.startswith(task.task_id + ":")
                or item_id.startswith(task.task_id + ".")
            ):
                return task.task_id
        return None

    delivered = {
        task.task_id
        for task in envelope.tasks
        if task.task_id in covered_task_ids
        and task.task_id not in excluded_task_ids
        and invocations_by_task.get(task.task_id)
        and not _successful_result_requires_clarification(
            invocations_by_task[task.task_id]
        )
    }
    remaining = [
        task for task in envelope.tasks if task.task_id not in delivered
    ]
    if not delivered or not remaining:
        return None
    next_task = remaining[0]
    next_items = [
        item for item in todo_snapshot
        if _owner(item.id) == next_task.task_id and item.status != "cancelled"
    ]
    if not next_items:
        return None
    reconciled = tuple(
        item
        if _owner(item.id) not in delivered or item.status == "cancelled"
        else TodoSnapshotItem(
            id=item.id,
            status="completed",
            depends_on=list(item.depends_on),
        )
        for item in todo_snapshot
    )
    return reconciled, outcome.model_copy(update={
        "active_goal_task_id": next_task.task_id,
        "active_todo_id": next_items[0].id,
    })


def _active_workflow_ids(
    envelope: IntentTaskEnvelope,
    todo_snapshot: tuple,
    outcome: BusinessLoopOutcome | None,
) -> tuple[str, str]:
    if outcome is not None:
        if outcome.active_goal_task_id is None or outcome.active_todo_id is None:
            raise AgentContractError("active workflow ids are unavailable")
        return outcome.active_goal_task_id, outcome.active_todo_id
    for task in envelope.tasks:
        matching = [
            item for item in todo_snapshot
            if item.id == task.task_id
            or item.id.startswith(task.task_id + ":")
            or item.id.startswith(task.task_id + ".")
        ]
        if matching:
            active = next(
                (item for item in matching if item.status == "in_progress"),
                matching[0],
            )
            return task.task_id, active.id
    raise AgentContractError("active workflow todo is unavailable")


def _constraint_from_intent_candidate(candidate: IntentCandidate) -> IntentConstraint:
    return IntentConstraint.model_validate({
        "intent_id": candidate.intent_id,
        "title": candidate.title,
        "action": candidate.action,
        "business_object": candidate.business_object,
        "required_shape": "single_intent",
        "expected_task_count": 1,
        "unresolved_slot_readiness": (
            "needs_slots" if candidate.required_slots else None
        ),
        "execution_mode": candidate.execution_mode,
        "readiness": candidate.readiness or "planned",
        "required_slots": candidate.required_slots,
        "capability_bindings": candidate.capability_bindings,
        "score": candidate.score,
        "evidence": candidate.evidence,
        "source": "tool_local_graph",
        "constraint": "required",
        "rule_version": candidate.rule_version,
        "slot_contract": (
            candidate.slot_contract.to_prompt_dict()
            if candidate.slot_contract is not None else None
        ),
    })


def _envelope_from_conversation_context(
    request: BusinessWorkflowResumeRequest,
    repository: IntentGraphRepository,
) -> IntentTaskEnvelope:
    context = request.conversation_context
    if (
        context is None or not context.goals
        or context.pending_envelope_id is None
        or context.intent_shape is None
    ):
        raise AgentContractError("native resume conversation context is unavailable")
    tasks = []
    for goal in context.goals:
        candidate = repository.candidate_for_intent(
            goal.intent_id, score=0, evidence=["workflow_resume"]
        )
        tasks.append({
            "task_id": goal.task_id,
            "intent_id": goal.intent_id,
            "action": candidate.action,
            "business_object": candidate.business_object,
            "slots": {
                item.name: {
                    "value": item.value,
                    "source": item.source,
                    "confidence": 1,
                }
                for item in goal.slots
            },
            "dependencies": [],
            "execution_readiness": {
                "ready": "ready",
                "needs_slots": "needs_slots",
                "provisional": "needs_clarification",
            }[goal.status],
            "risk_level": candidate.risk_level,
            "task_status": {
                "ready": "ready",
                "needs_slots": "needs_clarification",
                "provisional": "pending",
            }[goal.status],
        })
    # 已交付任务被过滤后，剩余 goal 数可能少于原始委托。intent_shape
    # 必须按实际任务数推导，否则 multi_intent + 单任务会触发契约校验
    # 失败；歧义项同样只能引用仍存在的任务。
    shape = context.intent_shape
    if len(tasks) == 1:
        shape = "single_intent"
    elif len(tasks) >= 2 and shape not in {
        "multi_intent", "same_intent_multi_task"
    }:
        shape = "multi_intent"
    known_task_ids = {task["task_id"] for task in tasks}
    ambiguities = [
        item for item in context.ambiguities
        if set(item.task_ids).issubset(known_task_ids)
    ]
    envelope = IntentTaskEnvelope.model_validate({
        "schema_version": "1.0",
        "revision": context.envelope_revision or 1,
        "delegation_id": context.pending_envelope_id,
        "trace_id": "resume_" + request.agent_run_id,
        "conversation_id": request.conversation_id,
        "source_agent_run_id": request.agent_run_id,
        "intent_shape": shape,
        "tasks": tasks,
        "ambiguities": [item.model_dump() for item in ambiguities],
        "shared_slots": {
            item.name: {
                "value": item.value,
                "source": item.source,
                "confidence": 1,
            }
            for item in context.shared_slots
        },
        "predicted_next_intents": [],
    })
    return envelope


def _build_active_transfer_form(
    projector: UserResponseProjector,
    task: Any,
    evaluation: SlotEvaluation,
    invocations: list[dict[str, Any]],
    progress: TaskProgress,
    force_form: bool = False,
) -> tuple[Any, ClarificationForm | None]:
    payee_name = str(evaluation.semantic_values.get("payee", "")).strip()
    missing = projector.missing_transfer_payees([payee_name], invocations)
    amount_minor_raw = evaluation.resolved_values.get("amountMinor")
    # 防御负数/float/bool：原始 amountMinor 可能是 float（来自 JSON 解析），
    # 也可能是负数或 bool（异常输入）。统一收敛为非负 int 分，非法值视为缺失。
    has_amount = (
        isinstance(amount_minor_raw, (int, float))
        and not isinstance(amount_minor_raw, bool)
        and amount_minor_raw > 0
    )
    amount_minor = int(round(amount_minor_raw)) if has_amount else 0
    from_account = str(evaluation.semantic_values.get("fromAccount", "")).strip()
    # fromAccount 需要用户显式选择时（值为空、ambiguous 或 missing），
    # 必须走 clarificationForm（含 fromAccount select 下拉框）路径，
    # 通过 /agent-runs/{runId}/clarifications 端点提交以保持会话状态。
    # 若走 project_clarification → action buttons 路径，用户点击按钮后会
    # 发送新消息到 /agent-runs，LLM 会丢失前序交互上下文（如 amount_scope
    # 选择结果），导致循环回到 amount_scope 提问。
    from_account_needs_selection = (
        not from_account
        or "fromAccount" in evaluation.ambiguous
        or "fromAccount" in evaluation.missing
    )
    # 只有 payee.list 查询过且 payee 在常用列表里（不缺），且 fromAccount
    # 已有值（不需要用户选择）时，才走提问降级（action buttons）；
    # payee.list 未调用时无法判断 payee 是否已有，按缺收款人出卡片，
    # 让用户在卡片里补 accountNo/amount 等槽位（避免逐个提问倒退）。
    has_payee_list = any(
        isinstance(inv, dict) and inv.get("capability_id") == "bank.payee.list"
        for inv in invocations
    )
    if (
        has_payee_list
        and payee_name not in missing
        and not from_account_needs_selection
        and not force_form
    ):
        return projector.project_clarification(
            task, slot_evaluation=evaluation, successful_invocations=invocations
        ), None
    purpose = evaluation.semantic_values.get("purpose")
    purpose_text = purpose.strip() if isinstance(purpose, str) else ""
    # 从 bank.payee.list 查询结果中查找匹配的收款人，回填已知卡号/银行名
    payee_items = _invocation_items(invocations, "bank.payee.list", "payees")
    matched_payee = _unique_named_match(
        payee_items, [payee_name] if payee_name else [], payee_name
    )
    payee_account_no = ""
    payee_bank_name = "中国建设银行"
    if matched_payee:
        payee_account_no = str(matched_payee.get("maskedNo", ""))
        if matched_payee.get("bankName"):
            payee_bank_name = str(matched_payee["bankName"])
    if has_amount and from_account:
        response = projector.project_transfer_form(
            progress=progress,
            payee_name=payee_name,
            from_account_label=_format_transfer_account_label(from_account),
            amount_minor=amount_minor,
        )
        form = ClarificationForm.model_validate({
            "schemaVersion": "1.0",
            "clarificationId": f"acl_{uuid.uuid4().hex}",
            "submissionToken": secrets.token_urlsafe(32),
            "expiresAt": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "kind": "transfer_payee",
            "fields": [
                {"name": "payeeName", "label": "收款人", "type": "text",
                 "required": True, "value": payee_name, "editable": not bool(payee_name)},
                {"name": "accountNo", "label": "收款账号", "type": "bank_account",
                 "required": True, "value": payee_account_no, "editable": not bool(payee_account_no)},
                {"name": "bankName", "label": "收款银行", "type": "bank_name",
                 "required": True, "value": payee_bank_name, "editable": not bool(matched_payee)},
                {"name": "amount", "label": "转账金额（元）", "type": "amount",
                 "required": True,
                 "value": f"{Decimal(amount_minor) / Decimal(100):.2f}",
                 "editable": False},
                {"name": "purpose", "label": "用途/备注（选填）", "type": "text",
                 "required": False, "value": purpose_text, "editable": True},
                {"name": "savePayee", "label": "保存为常用收款人", "type": "boolean",
                 "required": False, "value": False, "editable": True},
            ],
        })
        return response, form
    # Build fromAccount options from bank.account.list invocation results.
    # When fromAccount is ambiguous (source=context), semantic_values won't
    # contain it, so we provide a select field for the user to choose.
    account_items = _invocation_items(invocations, "bank.account.list", "accounts")
    from_account_options: list[dict[str, str]] = []
    for account in account_items:
        masked_no = str(account.get("maskedNo", ""))
        digits = re.sub(r"\D", "", masked_no)
        last_four = digits[-4:] if len(digits) >= 4 else ""
        if not last_four:
            continue
        account_name = str(account.get("name", "")).strip() or "账户"
        from_account_options.append({
            "label": f"{account_name} · 尾号{last_four}",
            "value": f"尾号{last_four}",
        })
    response = projector.project_transfer_full_form(
        payee_name=payee_name,
        from_account_label=_format_transfer_account_label(from_account) if from_account else "",
        amount_minor=amount_minor if has_amount else 0,
    )
    amount_value = (
        f"{Decimal(amount_minor) / Decimal(100):.2f}" if has_amount else ""
    )
    fields: list[dict[str, Any]] = []
    if from_account_options:
        fields.append({
            "name": "fromAccount", "label": "转出账户", "type": "select",
            "required": True, "value": from_account, "editable": True,
            "options": from_account_options,
        })
    fields.extend([
        {"name": "payeeName", "label": "收款人", "type": "text",
         "required": True, "value": payee_name, "editable": not bool(payee_name)},
        {"name": "accountNo", "label": "收款账号", "type": "bank_account",
         "required": True, "value": payee_account_no, "editable": not bool(payee_account_no)},
        {"name": "bankName", "label": "收款银行", "type": "bank_name",
         "required": True, "value": payee_bank_name, "editable": not bool(matched_payee)},
        {"name": "amount", "label": "转账金额（元）", "type": "amount",
         "required": True, "value": amount_value, "editable": not has_amount},
        {"name": "purpose", "label": "用途/备注（选填）", "type": "text",
         "required": False, "value": purpose_text, "editable": True},
        {"name": "savePayee", "label": "保存为常用收款人", "type": "boolean",
         "required": False, "value": False, "editable": True},
    ])
    form = ClarificationForm.model_validate({
        "schemaVersion": "1.0",
        "clarificationId": f"acl_{uuid.uuid4().hex}",
        "submissionToken": secrets.token_urlsafe(32),
        "expiresAt": (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "kind": "transfer_full",
        "fields": fields,
    })
    return response, form


def _safe_failed_response(
    request: RuntimeAgentRunRequest,
    projector: UserResponseProjector,
    _internal_code: str,
) -> RuntimeAgentRunResponse:
    envelope = IntentTaskEnvelope.model_validate(
        {
            "schema_version": "1.0",
            "delegation_id": "dlg_failed_" + uuid.uuid4().hex[:16],
            "trace_id": request.trace_id,
            "conversation_id": request.conversation_id,
            "source_agent_run_id": request.agent_run_id,
            "intent_shape": "non_task",
            "tasks": [],
            "predicted_next_intents": [],
        }
    )
    return RuntimeAgentRunResponse(
        agentRunId=request.agent_run_id,
        conversationId=request.conversation_id,
        status="blocked",
        intentTaskEnvelope=envelope,
        capabilityPlan=_empty_plan(envelope, "blocked", "failed"),
        userResponse=projector.project_notice("runtime_failed"),
    )


def _authoritative_non_task_submission(
    request: RuntimeAgentRunRequest,
) -> dict[str, Any]:
    """Build a model-free User Agent submission for input with no semantic signal."""

    return {
        "schema_version": "1.0",
        "delegation_id": "dlg_signal_gate",
        "trace_id": request.trace_id,
        "conversation_id": request.conversation_id,
        "source_agent_run_id": request.agent_run_id,
        "intent_shape": "non_task",
        "tasks": [],
        "predicted_next_intents": [],
    }


def _authoritative_continuation_submission(
    request: RuntimeAgentRunRequest,
    repository: Any,
) -> dict[str, Any]:
    """框架判定续答/恢复时，以持久化会话目标权威重建 envelope。

    openjiuwen IntentRecognizer 判定 SUPPLEMENT/RESUME 且目标命中种子任务时，
    本条消息是对既有任务的补充信息——envelope 直接取自会话状态（目标在首次
    规划时已经过图谱契约校验），不再让模型对碎片化应答做二次分类。goals
    范围与 pending envelope 按构造一致，delegation_id 由既有 pending-scope
    规则回写为原 envelope id，业务工作流据此确定性恢复。
    """

    context = request.conversation_context
    tasks: list[dict[str, Any]] = []
    for goal in context.goals:
        candidate = repository.candidate_for_intent(
            goal.intent_id, score=0, evidence=["continuity_supplement"]
        )
        needs_slots = goal.status == "needs_slots" or bool(candidate.required_slots)
        slots = {
            slot.name: {"value": slot.value, "source": slot.source, "confidence": 1}
            for slot in goal.slots
            if slot.value not in ("", None)
        }
        tasks.append({
            "task_id": goal.task_id,
            "intent_id": goal.intent_id,
            "action": candidate.action,
            "business_object": candidate.business_object,
            "slots": slots,
            "dependencies": [],
            "execution_readiness": "needs_slots" if needs_slots else "ready",
            "risk_level": candidate.risk_level,
            "task_status": "needs_clarification" if needs_slots else "ready",
        })
    return {
        "schema_version": "1.0",
        "delegation_id": "dlg_continuity",
        "trace_id": request.trace_id,
        "conversation_id": request.conversation_id,
        "source_agent_run_id": request.agent_run_id,
        # shape 必须与当前 task 数一致：持久化的 intent_shape 描述的是原
        # envelope（多任务），随着任务完成 goals 会缩到 1，multi_intent+单
        # task 过不了 envelope 校验，会把权威续接 envelope 整体推进修复轮，
        # 修复模型对碎片应答做二次分类后产出 non_task，续接链路断掉。与
        # submit_intent_task 归一化的 shape 机械推断同一原则。
        "intent_shape": (
            "single_intent" if len(tasks) == 1
            else context.intent_shape or "multi_intent"
        ),
        "tasks": tasks,
        "ambiguities": [
            {
                "type": ambiguity.type,
                "task_ids": ambiguity.task_ids,
                "options": list(ambiguity.options),
            }
            for ambiguity in context.ambiguities
        ],
        "shared_slots": {
            slot.name: {"value": slot.value, "source": slot.source, "confidence": 1}
            for slot in context.shared_slots
            if slot.value not in ("", None)
        },
        "predicted_next_intents": [],
    }


def _authoritative_single_intent_submission(
    request: RuntimeAgentRunRequest,
    constraint: IntentConstraint,
) -> dict[str, Any]:
    """Build a model-free User Agent submission for an authoritative graph recall.

    When the intent graph lexically recalls a single candidate with a score
    above the authority threshold and clear separation from other candidates,
    the LLM adjudication step is skipped to save ~400ms of latency.
    """

    task_id = "task_graph_" + hashlib.sha256(
        f"{request.agent_run_id}:{constraint.intent_id}".encode("utf-8")
    ).hexdigest()[:20]
    has_required_slots = bool(constraint.required_slots)
    return {
        "schema_version": "1.0",
        "delegation_id": "dlg_graph_authority",
        "trace_id": request.trace_id,
        "conversation_id": request.conversation_id,
        "source_agent_run_id": request.agent_run_id,
        "intent_shape": "single_intent",
        "tasks": [{
            "task_id": task_id,
            "intent_id": constraint.intent_id,
            "action": constraint.action,
            "business_object": constraint.business_object,
            "slots": {},
            "dependencies": [],
            "execution_readiness": (
                "needs_slots" if has_required_slots else "ready"
            ),
            "risk_level": constraint.risk_level,
            "task_status": (
                "needs_clarification" if has_required_slots else "ready"
            ),
        }],
        "ambiguities": [],
        "shared_slots": {},
        "predicted_next_intents": [],
    }


def _guided_entry_submission(
    request: RuntimeAgentRunRequest,
    repository: IntentGraphRepository,
) -> dict[str, Any]:
    """Create the trusted User-Agent envelope for a server-issued recommendation."""

    if request.entry_goal is None:
        raise AgentContractError("guided entry goal is unavailable")
    candidate = repository.candidate_for_intent(
        request.entry_goal.target_intent_id,
        score=_SCORE_SERVER_GUIDED_ENTRY,
        evidence=["server_guided_entry"],
    )
    if candidate.skill_binding is None:
        raise AgentContractError("guided entry intent has no skill binding")
    task_id = "task_guided_" + hashlib.sha256(
        request.entry_goal.goal_id.encode("utf-8")
    ).hexdigest()[:20]
    trusted_task_slots = {}
    if request.entry_goal.target_intent_id == "ccb.home.repayment.repay":
        for field in ("cardId", "fromAccountId"):
            value = request.entry_goal.trusted_inputs.get(field)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) is None
            ):
                raise AgentContractError(
                    "guided credit-card repayment context is unavailable"
                )
            trusted_task_slots[field] = {
                "value": value,
                "source": "context",
                "confidence": 1,
            }
    return {
        "schema_version": "1.0",
        "delegation_id": request.entry_goal.goal_id[:96],
        "trace_id": request.trace_id,
        "conversation_id": request.conversation_id,
        "source_agent_run_id": request.agent_run_id,
        "intent_shape": "single_intent",
        "tasks": [{
            "task_id": task_id,
            "intent_id": candidate.intent_id,
            "action": candidate.action,
            "business_object": candidate.business_object,
            "slots": trusted_task_slots,
            "dependencies": [],
            "execution_readiness": (
                "needs_slots" if candidate.required_slots else "ready"
            ),
            "risk_level": candidate.risk_level,
            "task_status": (
                "needs_clarification" if candidate.required_slots else "ready"
            ),
        }],
        "ambiguities": [],
        "shared_slots": {},
        "predicted_next_intents": [],
    }


def _is_single_canonical_faq(
    envelope: IntentTaskEnvelope,
    knowledge_task_ids: set[str],
    faq_analysis: FaqAnalysis | None,
) -> bool:
    return bool(
        len(envelope.tasks) == 1
        and envelope.tasks[0].task_id in knowledge_task_ids
        and faq_analysis is not None
        and faq_analysis.fully_covered
        and len(faq_analysis.matches) == 1
        and faq_analysis.matches[0].entry.intent_id == envelope.tasks[0].intent_id
    )


def _is_uncovered_single_faq(
    envelope: IntentTaskEnvelope,
    knowledge_task_ids: set[str],
    faq_analysis: FaqAnalysis | None,
    constraints: list[IntentConstraint],
) -> bool:
    if not (
        len(envelope.tasks) == 1
        and envelope.tasks[0].task_id in knowledge_task_ids
        and faq_analysis is not None
        and not faq_analysis.matches
    ):
        return False
    # The User DeepAgent has selected a graph-bound knowledge-only task, but
    # the governed FAQ repository has no canonical answer. Never guess.
    return True


def _normalized_reference(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _unique_named_match(
    items: list[dict[str, Any]], references: list[str], source_message: str
) -> dict[str, Any] | None:
    # 证据分层：任务槽位引用（含尾号）是权威证据，先单独裁决；
    # 消息全文子串只是无引用命中时的兜底。两证据平铺合并会在
    # “张三李四”这类连写消息里把多个收款人同时拉进同一匹配集，
    # 让本已由槽位唯一确定的任务被误判为歧义。
    normalized_message = _normalized_reference(source_message)
    normalized_references = {_normalized_reference(item) for item in references if item}
    reference_matches = []
    message_matches = []
    for item in items:
        identifier = item.get("id")
        if not (isinstance(identifier, str) and identifier):
            continue
        name = _normalized_reference(item.get("name"))
        masked = _normalized_reference(item.get("maskedNo"))
        tail = "".join(character for character in masked if character.isdigit())[-4:]
        if name and name in normalized_references:
            reference_matches.append(item)
        elif tail and any(tail in value for value in normalized_references):
            reference_matches.append(item)
        elif name and name in normalized_message:
            message_matches.append(item)
    if len(reference_matches) == 1:
        return reference_matches[0]
    if not reference_matches and len(message_matches) == 1:
        return message_matches[0]
    return None


def _merge_invocations(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result = list(first)
    for item in second:
        if not any(
            current.get("capability_id") == item.get("capability_id")
            and current.get("input") == item.get("input")
            for current in result
        ):
            result.append(item)
    return result


def _successful_result_requires_clarification(
    successful_invocations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> bool:
    """Recognize a structured read result that explicitly needs user input.

    This depends on provider fields, never rendered copy or a capability id.
    A successful read can still be non-terminal when it returns a bounded list
    of missing preferences together with a concrete continuation action.
    """
    for invocation in successful_invocations:
        if not isinstance(invocation, dict):
            continue
        output = invocation.get("output")
        if not isinstance(output, dict):
            continue
        missing = output.get("missingPreferenceFields")
        actions = output.get("requiredActions")
        if (
            output.get("recommendationStatus") == "preferences_required"
            and isinstance(missing, list)
            and 1 <= len(missing) <= 16
            and all(isinstance(item, str) and item for item in missing)
            and isinstance(actions, list)
            and bool(actions)
        ):
            return True
    return False


def _validate_multi_task_plan_coverage(
    plan: CapabilityPlan, envelope: IntentTaskEnvelope
) -> tuple[str, ...]:
    if (
        plan.status != "planned"
        or len(envelope.tasks) < 2
    ):
        return ()
    covered_task_ids = {step.task_id for step in plan.steps}
    if any(task.task_id not in covered_task_ids for task in envelope.tasks):
        return ("multi_transfer_plan_coverage_incomplete",)
    return ()


def _validate_confirmation_resolution(
    plan: CapabilityPlan, resolution: dict[str, Any]
) -> tuple[str, ...]:
    if plan.status != "planned" or len(plan.steps) != 1:
        return ("confirmation_resolution_required",)
    step = plan.steps[0]
    resolved_input = resolution.get("resolved_input")
    if (
        step.capability_id != resolution["capability_id"]
        or not isinstance(resolved_input, dict)
        or not resolved_input.items() <= step.input_bindings.items()
        or step.requires_user_confirmation is not True
    ):
        return ("confirmation_resolution_mismatch",)
    return ()


def _parse_envelope_submission(
    raw_envelope: dict[str, Any] | None,
    request: RuntimeAgentRunRequest,
    shape_assessment: IntentShapeAssessment | None = None,
    allowed_intent_ids: frozenset[str] | set[str] | None = None,
    intent_graph: Any | None = None,
) -> IntentTaskEnvelope:
    if not isinstance(raw_envelope, dict):
        raise AgentContractError("user agent did not produce an intent submission")
    _reject_sensitive_keys(raw_envelope)
    raw_envelope = {
        key: value for key, value in raw_envelope.items()
        if key not in ("memory_candidate_hints", "memory_variables")
    }
    # LLM sometimes wraps envelope fields inside an "envelope" key instead
    # of submitting them at the top level. Unwrap it before validation.
    nested = raw_envelope.get("envelope")
    if isinstance(nested, dict) and any(
        key in nested for key in ("schema_version", "intent_shape", "tasks")
    ):
        raw_envelope = {**raw_envelope, **nested}
        raw_envelope.pop("envelope", None)
    # 权威候选边界：task 的 intent_id 只能来自本轮召回候选集（含接续
    # 上下文注入的 pending 意图）。轻量模型可能发明形似合法的意图 id
    #（如 ccb.transfer.fund），若不在提交边界拦截，会在后续槽位契约
    # 阶段以无关错误崩溃。此处拒绝并交给有界修复轮纠正。
    if allowed_intent_ids is not None:
        raw_tasks = raw_envelope.get("tasks")
        if isinstance(raw_tasks, list) and raw_tasks:
            unknown = sorted({
                item.get("intent_id")
                for item in raw_tasks
                if isinstance(item, dict)
                and item.get("intent_id") not in allowed_intent_ids
            } - {None})
            if unknown:
                raise AgentContractError(
                    "user agent intent submission is invalid: "
                    f"unknown intent ids {unknown}; intent_ids must come "
                    "from intent_candidates"
                )
    # Shape analysis is evidence only. The User DeepAgent owns semantic
    # ambiguities and their task bindings through the validated envelope.
    # An envelope-level ambiguity always relates at least two tasks.  Models
    # occasionally attach a stale ambiguity object to an otherwise valid
    # single-task submission; canonicalize that structurally impossible field
    # instead of failing the whole user turn after a bounded repair.
    raw_tasks = raw_envelope.get("tasks")
    raw_shape = raw_envelope.get("intent_shape")
    raw_ambiguities = raw_envelope.get("ambiguities")
    if (
        raw_shape == "single_intent"
        and isinstance(raw_tasks, list)
        and len(raw_tasks) == 1
        and isinstance(raw_ambiguities, list)
        and raw_ambiguities
    ):
        raw_envelope = {**raw_envelope, "ambiguities": []}
    # LLM 偶尔输出 'needs slots' 而非 'needs_slots'（空格代替下划线），
    # 导致 Pydantic Literal 校验失败并触发额外修复 LLM 调用（3-8s）。
    # 在验证前自动修正，避免不必要的修复轮次。
    raw_tasks = raw_envelope.get("tasks")
    if isinstance(raw_tasks, list):
        for _task_item in raw_tasks:
            if not isinstance(_task_item, dict):
                continue
            for _field in ("task_status", "execution_readiness"):
                _val = _task_item.get(_field)
                if isinstance(_val, str) and " " in _val:
                    _task_item[_field] = _val.replace(" ", "_")
    # 模型偶发在 ambiguities.options 里填入自由文本（如中文收款人名），
    # 违反 options 的标识符 schema（^[a-z][a-z0-9_.-]{0,127}$）并让整封
    # 提交校验失败。选项是投影层的枚举 token（如 per_task/total），自由
    # 文本选项没有任何下游消费方（与 ambiguity_is_projectable 同一判定
    # 原则）——机械剔除携带非法选项的歧义条目，而不是让有界修复轮重
    # 试同样的幻觉输出。
    raw_ambiguities = raw_envelope.get("ambiguities")
    if isinstance(raw_ambiguities, list) and raw_ambiguities:
        _option_pattern = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
        _valid_ambiguities = [
            item
            for item in raw_ambiguities
            if not (
                isinstance(item, dict)
                and isinstance(item.get("options"), list)
                and item["options"]
                and any(
                    not isinstance(option, str)
                    or not _option_pattern.match(option)
                    for option in item["options"]
                )
            )
        ]
        if len(_valid_ambiguities) != len(raw_ambiguities):
            raw_envelope = {**raw_envelope, "ambiguities": _valid_ambiguities}
    normalized_envelope = {
        **raw_envelope,
        "delegation_id": "dlg_pending",
        "trace_id": request.trace_id,
        "conversation_id": request.conversation_id,
        "source_agent_run_id": request.agent_run_id,
    }
    try:
        envelope = IntentTaskEnvelope.model_validate(normalized_envelope)
    except ValidationError as exc:
        raise AgentContractError(
            "user agent intent submission is invalid: " + _validation_summary(exc)
        ) from exc
    pending_envelope_id = request.conversation_context.pending_envelope_id
    continues_pending_scope = _matches_pending_goal_scope(
        envelope, request.conversation_context
    )
    envelope = envelope.model_copy(
        update={
            "delegation_id": (
                pending_envelope_id
                if pending_envelope_id is not None and continues_pending_scope
                else "dlg_" + uuid.uuid4().hex
            ),
            "trace_id": request.trace_id,
            "conversation_id": request.conversation_id,
            "source_agent_run_id": request.agent_run_id,
        }
    )
    if intent_graph is not None:
        cardinality_violations = _entity_cardinality_violations(
            intent_graph, envelope, shape_assessment
        )
        if cardinality_violations:
            raise AgentContractError(
                "user agent intent submission violates entity cardinality: "
                + "; ".join(cardinality_violations)
            )
    _reject_sensitive_keys(envelope.model_dump())
    return envelope


def _entity_cardinality_violations(
    repository: Any,
    envelope: IntentTaskEnvelope,
    shape_assessment: IntentShapeAssessment | None,
) -> tuple[str, ...]:
    """家族实体基数契约：任务分解权在框架，实体引用不得被静默收窄。

    图谱为意图声明 party_reference 单值槽（每次执行恰一个办理对象）时，
    形状层声明的多个实体必须逐实体实例化，或由信封 excludedEntities
    显式排除；缺失或重复均为契约违约，框架拒绝静默采纳，交由有界修复
    轮补齐。判定只依赖图谱槽位契约与形状层实体证据，不含业务专名。
    """

    if shape_assessment is None:
        return ()
    entities = [
        item for item in shape_assessment.entities
        if isinstance(item, str) and item.strip()
    ]
    entity_count = max(
        len(entities), int(shape_assessment.entity_count_hint or 0)
    )
    if entity_count <= 1:
        return ()
    excluded = {
        _normalized_reference(item) for item in envelope.excluded_entities
    }
    party_families: list[tuple[str, list[IntentTask], tuple[Any, ...]]] = []
    for intent_id in dict.fromkeys(task.intent_id for task in envelope.tasks):
        family_tasks = [
            task for task in envelope.tasks if task.intent_id == intent_id
        ]
        try:
            candidate = repository.candidate_for_intent(
                intent_id, score=0, evidence=["entity_cardinality_contract"]
            )
        except IntentGraphError:
            continue
        contract = candidate.slot_contract
        if contract is None:
            continue
        party_slots = tuple(
            item for item in contract.slots if item.type == "party_reference"
        )
        if not party_slots:
            continue
        party_families.append((intent_id, family_tasks, party_slots))
    if not party_families:
        return ()
    logger.info(
        "Entity cardinality gate families=%s entities=%s count=%s excluded=%s",
        [intent_id for intent_id, _tasks, _slots in party_families],
        list(entities),
        entity_count,
        sorted(excluded),
    )
    violations: list[str] = []
    if entities:
        required = [
            item for item in entities
            if _normalized_reference(item) not in excluded
        ]
        for entity in required:
            normalized_entity = _normalized_reference(entity)
            covering = 0
            for _intent_id, family_tasks, party_slots in party_families:
                for task in family_tasks:
                    if _task_covers_party_entity(task, party_slots, normalized_entity):
                        covering += 1
            if covering == 0:
                violations.append(
                    f"entity_cardinality_uncovered:{entity}"
                    "(消息声明的办理对象未被任何任务实例化；为该对象提交独立 task，"
                    "或在 envelope.excludedEntities 声明排除)"
                )
            elif covering > 1:
                violations.append(
                    f"entity_cardinality_duplicated:{entity}"
                    f"(同一办理对象被 {covering} 个任务重复实例化；恰好一个 task 承接)"
                )
    else:
        for intent_id, family_tasks, _party_slots in party_families:
            if len(family_tasks) < entity_count:
                violations.append(
                    f"entity_cardinality_mismatch:{intent_id}"
                    f":expected>={entity_count},actual={len(family_tasks)}"
                    "(消息声明多个办理对象，须逐对象实例化任务或声明排除)"
                )
    return tuple(violations)


def _task_covers_party_entity(task: IntentTask, party_slots: tuple[Any, ...], normalized_entity: str) -> bool:
    for definition in party_slots:
        candidate_values = [task.slots.get(definition.name)]
        if definition.resolved_field:
            candidate_values.append(task.slots.get(definition.resolved_field))
        for slot_value in candidate_values:
            if slot_value is None:
                continue
            raw = getattr(slot_value, "value", slot_value)
            if normalized_entity in _normalized_reference(raw):
                return True
    return False


def _parse_user_memory_candidate_hints(
    raw_submission: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_submission, dict):
        return []
    raw_hints = raw_submission.get("memory_candidate_hints", [])
    if not isinstance(raw_hints, list) or len(raw_hints) > 5:
        raise AgentContractError("user memory candidate hints are invalid")
    try:
        return [
            UserMemoryCandidateHint.model_validate(item).model_dump()
            for item in raw_hints
        ]
    except ValidationError as exc:
        raise AgentContractError("user memory candidate hints are invalid") from exc


def _parse_memory_variables(
    raw_submission: dict[str, Any] | None,
) -> dict[str, str]:
    """从 user agent 原始响应中提取工作记忆变量（键值对）。

    user agent 可在响应中返回 memory_variables 字段，包含本轮会话中
    需要持久化的工作记忆变量（如当前选中的账户、最近查询的日期等）。
    """
    if not isinstance(raw_submission, dict):
        return {}
    raw_vars = raw_submission.get("memory_variables", {})
    if not isinstance(raw_vars, dict) or len(raw_vars) > 20:
        return {}
    safe_vars: dict[str, str] = {}
    for name, value in raw_vars.items():
        if not isinstance(name, str) or len(name) > 128 or not name.strip():
            continue
        if not isinstance(value, str) or len(value) > 500:
            continue
        safe_vars[name.strip()] = value
    return safe_vars


def _matches_pending_goal_scope(
    envelope: IntentTaskEnvelope, conversation_context: Any
) -> bool:
    """Reuse workflow identity only when the User Agent retained the same goals."""
    if conversation_context.pending_envelope_id is None:
        return False
    if conversation_context.goals:
        pending_goals = {
            (goal.task_id, goal.intent_id) for goal in conversation_context.goals
        }
        current_goals = {
            (task.task_id, task.intent_id) for task in envelope.tasks
        }
        return bool(current_goals) and current_goals == pending_goals
    return bool(
        conversation_context.pending_intent_id
        and len(envelope.tasks) == 1
        and envelope.tasks[0].intent_id
        == conversation_context.pending_intent_id
    )


def _parse_plan_submission(
    raw_plan: dict[str, Any] | None,
    envelope: IntentTaskEnvelope,
) -> CapabilityPlan:
    if not isinstance(raw_plan, dict):
        raise AgentContractError(
            "business agent did not produce a capability plan submission"
        )
    try:
        plan = CapabilityPlan.model_validate(raw_plan)
    except ValidationError as exc:
        raise AgentContractError(
            "business agent capability plan submission is invalid: "
            + _validation_summary(exc)
        ) from exc
    plan = plan.model_copy(
        update={
            "business_agent_run_id": "bar_" + uuid.uuid4().hex,
            "delegation_id": envelope.delegation_id,
        }
    )
    _reject_sensitive_keys(plan.model_dump())
    return plan


def _reject_sensitive_keys(value: Any) -> None:
    forbidden = {
        "password",
        "otp",
        "confirmation_token",
        "authorization_token",
        "access_token",
        "card_number",
        "account_number",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in forbidden:
                raise AgentContractError("sensitive data is forbidden in agent output")
            _reject_sensitive_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_keys(item)


def _contains_binding_reference(value: Any) -> bool:
    if isinstance(value, dict):
        reference_keys = {"slot", "fromStep", "from_step", "fromSlot", "from_slot"}
        return bool(reference_keys.intersection(value)) or any(
            _contains_binding_reference(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_binding_reference(item) for item in value)
    return False


def _should_restore_business_workflow(
    conversation_context: Any, envelope: IntentTaskEnvelope
) -> bool:
    """Restore only a started workflow that still owns the current goals."""

    if conversation_context.pending_envelope_id is None:
        return False
    # pending_envelope_id 非空即上一轮已委托并持久化过工作流。goal 全部
    # provisional 不是"未开始"：待裁决歧义覆盖的目标就是以 provisional
    # 落库的（见 _build_conversation_state），把它当未开始会让本轮跳过
    # 恢复、以 expected_revision=0 对已存在工作流行 CAS，必然 409 打废
    # 整轮。
    started = bool(
        conversation_context.goals
    ) or any(
        goal.status != "provisional"
        for goal in conversation_context.goals
    )
    return started and _matches_pending_goal_scope(
        envelope, conversation_context
    )


def _intent_constraint_from_candidate(
    item: IntentCandidate,
    *,
    required_shape: str,
    expected_task_count: int,
    authoritative: bool,
    required: bool,
) -> IntentConstraint:
    return IntentConstraint.model_validate(
        {
            "intent_id": item.intent_id,
            "title": item.title,
            "action": item.action,
            "business_object": item.business_object,
            "required_shape": required_shape,
            "expected_task_count": expected_task_count,
            "shape_authoritative": authoritative,
            "supported_shapes": (
                list(item.task_policy.supported_shapes)
                if item.task_policy is not None
                else ["single_intent"]
            ),
            "unresolved_slot_readiness": (
                "needs_slots"
                if item.required_slots and item.execution_mode != "knowledge-only"
                else None
            ),
            "execution_mode": item.execution_mode,
            "readiness": item.readiness or "planned",
            "required_slots": (
                [] if item.execution_mode == "knowledge-only" else item.required_slots
            ),
            "capability_bindings": (
                [] if item.execution_mode == "knowledge-only" else item.capability_bindings
            ),
            "score": item.score,
            "evidence": item.evidence,
            "source": "tool_local_graph",
            "constraint": "required" if required else "allowed",
            "rule_version": item.rule_version,
            "risk_level": item.risk_level,
            "slot_contract": (
                item.slot_contract.to_prompt_dict()
                if item.slot_contract is not None else None
            ),
            **(
                {"disambiguationHints": item.disambiguation_hints}
                if item.disambiguation_hints else {}
            ),
        }
    )


# --- Intent recall authority scores ---
_SCORE_SERVER_GUIDED_ENTRY = 300
_SCORE_SERVER_RECOMMENDATION_FOCUS = 280
_SCORE_PENDING_CONTEXT = 130
_SCORE_FAQ_PATTERN = 240
# --- Authority shortcut thresholds ---
_SCORE_AUTHORITY_THRESHOLD = 180
_SCORE_CANDIDATE_SEPARATION = 100
_SCORE_WEAK_RECALL_THRESHOLD = 100
# --- P1-1：非业务输入拒识阈值 ---
# Codex P1-1：旧实现对"你好"/"在吗"等纯社交闲聊输入也会返回业务 FAQ 候选，
# 评测者会误以为"业务请求被误判成闲聊"或"闲聊被错命中业务意图"。
# 这里设一个非任务/闲聊形态下的拒识阈值：召回分数低于该阈值时直接判 idle_chat，
# 不返回业务候选/FAQ，避免把"无业务意图的弱召回"误演成业务意图。
_SCORE_IDLE_CHAT_REJECT_THRESHOLD = _STRONG_MATCH_THRESHOLD  # 80：低于该分视为词法噪音，非业务命中


class _EvalProbeCapability:
    """评测阶梯 shim：只暴露 widen 阶梯读取的 ``id`` 字段。"""

    def __init__(self, id: str) -> None:
        self.id = id


class _EvalProbeRequest:
    """评测阶梯 shim：recall_intent_constraints 只读取
    message/page_context/entry_goal/conversation_focus/conversation_context，
    widen 阶梯只读取 message/plannable_capabilities[].id。"""

    def __init__(
        self,
        message: str,
        page_context: dict[str, Any],
        plannable_capabilities: list[_EvalProbeCapability],
    ) -> None:
        self.message = message
        self.page_context = PageContext.model_validate(page_context or {})
        self.entry_goal = None
        self.conversation_focus = None
        self.conversation_context = ConversationContext()
        self.plannable_capabilities = plannable_capabilities


def _project_intent_constraint(item: IntentConstraint) -> dict[str, Any]:
    projected: dict[str, Any] = {
        "intentId": item.intent_id,
        "title": item.title,
        "action": item.action,
        "businessObject": item.business_object,
        "requiredShape": item.required_shape,
        "expectedTaskCount": item.expected_task_count,
        "shapeAuthoritative": item.shape_authoritative,
        "supportedShapes": list(item.supported_shapes),
        "executionMode": item.execution_mode,
        "requiredSlots": list(item.required_slots),
        "capabilityBindings": list(item.capability_bindings),
        "score": item.score,
        "evidence": list(item.evidence),
        "constraint": item.constraint,
        "risk_level": item.risk_level,
    }
    if item.unresolved_slot_readiness is not None:
        projected["unresolvedSlotReadiness"] = item.unresolved_slot_readiness
    if item.disambiguation_hints:
        projected["disambiguationHints"] = item.disambiguation_hints
    return projected


def recall_intent_constraints(
    request: RuntimeAgentRunRequest,
    repository: IntentGraphRepository,
    faq_repository: MockFaqRepository | None = None,
    shape_assessment: IntentShapeAssessment | None = None,
) -> list[IntentConstraint]:
    page_context = request.page_context.model_dump(by_alias=True, exclude_none=True)
    analysis = faq_repository.analyze(request.message) if faq_repository else None
    candidates: list[IntentCandidate]
    required_shape = "single_intent"
    expected_task_count = 1
    authoritative = False
    if request.entry_goal is not None:
        authoritative = True
        candidates = [repository.candidate_for_intent(
            request.entry_goal.target_intent_id,
            score=_SCORE_SERVER_GUIDED_ENTRY,
            evidence=["server_guided_entry"],
        )]
    elif request.conversation_focus is not None:
        authoritative = True
        candidates = [repository.candidate_for_intent(
            request.conversation_focus.target_intent_id,
            score=_SCORE_SERVER_RECOMMENDATION_FOCUS,
            evidence=["server_recommendation_focus"],
        )]
    elif analysis and analysis.matches:
        authoritative = True
        faq_intent_ids = [item.entry.intent_id for item in analysis.matches]
        candidates = []
        for intent_id in dict.fromkeys(faq_intent_ids):
            evidence = ["faq_pattern"]
            if len(analysis.matches) > 1:
                evidence.append("faq_multiple")
            candidates.append(
                repository.candidate_for_intent(
                    intent_id, score=_SCORE_FAQ_PATTERN, evidence=evidence
                )
            )
        residual_candidates = _recall_residual_candidates(
            analysis.residual_semantics, repository, page_context
        )
        for candidate in residual_candidates:
            if candidate.intent_id not in {item.intent_id for item in candidates}:
                candidates.append(candidate)
        semantic_intent_ids = [*faq_intent_ids]
        if len(residual_candidates) == 1:
            semantic_intent_ids.append(residual_candidates[0].intent_id)
        if analysis.fully_covered and len(analysis.matches) == 1:
            required_shape = "single_intent"
            expected_task_count = 1
        elif analysis.fully_covered or len(residual_candidates) == 1:
            required_shape = (
                "same_intent_multi_task"
                if len(set(semantic_intent_ids)) == 1
                else "multi_intent"
            )
            expected_task_count = max(2, len(semantic_intent_ids))
        else:
            required_shape = "ambiguous_intent"
            expected_task_count = 0
    else:
        candidates = repository.recall(
            request.message, page_context=page_context
        )
    pending_intent_ids = list(dict.fromkeys([
        *(
            [request.conversation_context.pending_intent_id]
            if request.conversation_context.pending_intent_id else []
        ),
        *(item.intent_id for item in request.conversation_context.goals),
    ]))
    pending_context_injected = False
    if pending_intent_ids and not authoritative:
        by_id = {item.intent_id: item for item in candidates}
        for intent_id in pending_intent_ids:
            if intent_id not in by_id:
                by_id[intent_id] = repository.candidate_for_intent(
                    intent_id,
                    score=_SCORE_PENDING_CONTEXT,
                    evidence=["pending_context"],
                )
                pending_context_injected = True
        candidates = sorted(
            by_id.values(), key=lambda item: (-item.score, item.intent_id)
        )[:8]
        # Only inherit shape/task_count from pending context during
        # continuation — when the top candidate is NOT a new graph-recalled
        # intent. If the graph recalls a new intent that out-ranks the
        # pending-context floor, the user has likely switched topics; let
        # the graph drive the shape instead of inheriting stale values.
        if request.conversation_context.goals and (
            not candidates
            or "pending_context" in (candidates[0].evidence or [])
            or candidates[0].intent_id in pending_intent_ids
        ):
            required_shape = (
                request.conversation_context.intent_shape or "single_intent"
            )
            expected_task_count = len(request.conversation_context.goals)
    # Whether the top candidate is a graph-recalled NEW intent — not a
    # pending-context injection and not one of the pending intents. When
    # this is true, the user has likely switched topics; the authoritative
    # shortcut should follow the graph, not the stale pending context.
    # When false (top is a pending-context injection or a graph-recalled
    # pending intent), it is continuation — keep the candidate set open.
    top_is_new_graph_intent = (
        bool(candidates)
        and "pending_context" not in (candidates[0].evidence or [])
        and candidates[0].intent_id not in pending_intent_ids
    )
    if (
        not authoritative
        and candidates
        and (
            not pending_context_injected
            or top_is_new_graph_intent
        )
        and (
            shape_assessment is None
            or shape_assessment.shape == "single_intent"
        )
        and (
            candidates[0].score >= _SCORE_AUTHORITY_THRESHOLD
            or candidates[0].semantic_priority > 0
        )
    ):
        # For the separation check, ignore pending-context injections — they
        # are an artificial score floor, not a competing user intent. Measure
        # separation against other graph-recalled candidates only.
        separation_pool = (
            [
                item for item in candidates
                if "pending_context" not in (item.evidence or [])
            ]
            if pending_context_injected
            else candidates
        )
        if (
            len(separation_pool) == 1
            or (
                len(separation_pool) > 1
                and (
                    separation_pool[0].score - separation_pool[1].score
                    >= _SCORE_CANDIDATE_SEPARATION
                    or separation_pool[0].semantic_priority
                    > separation_pool[1].semantic_priority
                )
            )
        ):
            # A high-score, well-separated graph match is authoritative for
            # a single-intent utterance. This keeps overlapping generic
            # catalogue, FAQ, and purchase aliases available for genuinely
            # ambiguous text, while preventing the model from discarding an
            # exact graph-backed service request as non-task.
            candidates = [candidates[0]]
            authoritative = True
    # 当形态层通过相邻人名解析出多人实体（adjacent_person_entities）时，
    # 只要 top 候选明确支持 same_intent_multi_task 且含 party_reference 槽位，
    # 即可授权采纳多任务形态。不要求召回全局只有 1 个候选——图谱 confusable
    # 扩展总会拉回低分邻居，该条件在真实数据上恒不成立，导致「给张三李四转100」
    # 这类多对象诉求被永久误判为 single_intent。
    graph_authorizes_entity_cardinality = bool(
        shape_assessment is not None
        and "adjacent_person_entities" in shape_assessment.evidence
        and shape_assessment.shape == "same_intent_multi_task"
        and candidates
        and candidates[0].task_policy is not None
        and "same_intent_multi_task" in candidates[0].task_policy.supported_shapes
        and candidates[0].slot_contract is not None
        and any(
            slot.type == "party_reference"
            for slot in candidates[0].slot_contract.slots
        )
    )
    if (
        not authoritative
        and shape_assessment is not None
        and (shape_assessment.authoritative or graph_authorizes_entity_cardinality)
        and shape_assessment.shape in {"multi_intent", "same_intent_multi_task"}
    ):
        required_shape = shape_assessment.shape
        expected_task_count = shape_assessment.expected_task_count
        authoritative = True
    available_capability_ids = {item.id for item in request.plannable_capabilities}
    if not authoritative:
        supported_candidates = [
            item for item in candidates
            if item.execution_mode == "knowledge-only"
            or set(item.capability_bindings).issubset(available_capability_ids)
        ]
        if supported_candidates:
            candidates = supported_candidates
    repository.validate_capability_bindings(
        [item for item in candidates if item.execution_mode != "knowledge-only"],
        available_capability_ids,
    )
    required = authoritative or len(candidates) == 1
    return [
        _intent_constraint_from_candidate(
            item,
            required_shape=required_shape,
            expected_task_count=expected_task_count,
            authoritative=authoritative,
            required=required,
        )
        for item in candidates
    ]


def _reanchor_retained_goals(
    envelope: IntentTaskEnvelope,
    slot_evaluations: dict[str, SlotEvaluation],
    request: RuntimeAgentRunRequest,
    repository: IntentGraphRepository,
    evaluator: SlotContractEvaluator,
) -> tuple[IntentTaskEnvelope, dict[str, SlotEvaluation]]:
    """Restore retained goals the User Agent dropped mid-clarification.

    图谱 contextPolicy.retainOnClarification 声明的意图，其未完成目标在
    多轮澄清期间不因模型重建信封而静默消失（如 跨域"查余额+转账"澄清
    期间查询任务被丢弃）。仅当重建信封仍在续跑同一 pending envelope
    （至少含一个原目标）时补挂；用户切换新话题时不受影响。
    """
    context = request.conversation_context
    if (
        context is None
        or not context.goals
        or context.pending_envelope_id is None
        or not envelope.tasks
    ):
        return envelope, slot_evaluations
    present = {task.task_id for task in envelope.tasks}
    retained_ids = {goal.task_id for goal in context.goals}
    envelope_intents = {task.intent_id for task in envelope.tasks}
    goal_intents = {goal.intent_id for goal in context.goals}
    if not present & retained_ids and not envelope_intents & goal_intents:
        return envelope, slot_evaluations
    # 信封任务收敛到稳定目标 id：模型重建可能改写临时 task_id，意图相同
    # 仍视为同一目标的续跑，目标已确认槽位合并进任务（模型显式值优先）。
    id_map: dict[str, str] = {}
    claimed = present & retained_ids
    healed: list[IntentTask] = []
    for task in envelope.tasks:
        if task.task_id in retained_ids:
            healed.append(task)
            continue
        goal = next(
            (
                item for item in context.goals
                if item.intent_id == task.intent_id and item.task_id not in claimed
            ),
            None,
        )
        if goal is None:
            healed.append(task)
            continue
        claimed.add(goal.task_id)
        id_map[task.task_id] = goal.task_id
        healed.append(task.model_copy(update={
            "task_id": goal.task_id,
            "slots": {
                **{
                    item.name: SlotValue(
                        value=item.value, source="context", confidence=1
                    )
                    for item in goal.slots
                },
                **task.slots,
            },
        }))
    if id_map and envelope.ambiguities:
        envelope = envelope.model_copy(update={
            "ambiguities": [
                item.model_copy(update={
                    "task_ids": [
                        id_map.get(task_id, task_id) for task_id in item.task_ids
                    ],
                })
                for item in envelope.ambiguities
            ],
        })
    reanchored: list[IntentTask] = []
    for goal in context.goals:
        if goal.task_id in present or goal.task_id in claimed:
            continue
        try:
            candidate = repository.candidate_for_intent(
                goal.intent_id, score=0, evidence=["retained_goal"]
            )
        except IntentGraphError:
            continue
        policy = candidate.context_policy
        if policy is None or not policy.retain_on_clarification:
            continue
        reanchored.append(
            IntentTask.model_validate({
                "task_id": goal.task_id,
                "intent_id": goal.intent_id,
                "action": candidate.action,
                "business_object": candidate.business_object,
                "slots": {
                    item.name: {
                        "value": item.value,
                        "source": item.source,
                        "confidence": 1,
                    }
                    for item in goal.slots
                },
                "dependencies": [],
                "execution_readiness": {
                    "ready": "ready",
                    "needs_slots": "needs_slots",
                    "provisional": "needs_clarification",
                }[goal.status],
                "risk_level": candidate.risk_level,
                "task_status": {
                    "ready": "ready",
                    "needs_slots": "needs_clarification",
                    "provisional": "pending",
                }[goal.status],
            })
        )
    if not reanchored and not id_map:
        return envelope, slot_evaluations
    # 按会话目标的既定顺序拼接：委托顺序在多轮澄清间保持稳定，业务工作流
    # 恢复（delegatedTaskIds 顺序比对）才不会把补挂的目标误判为目标变更。
    healed_by_id = {task.task_id: task for task in healed}
    reanchored_by_id = {task.task_id: task for task in reanchored}
    ordered: list[IntentTask] = []
    seen: set[str] = set()
    for goal in context.goals:
        task = healed_by_id.get(goal.task_id) or reanchored_by_id.get(goal.task_id)
        if task is not None:
            ordered.append(task)
            seen.add(goal.task_id)
    for task in healed:
        if task.task_id not in seen:
            ordered.append(task)
    tasks = ordered
    shape = envelope.intent_shape
    intent_ids = {task.intent_id for task in tasks}
    if len(tasks) > 1:
        if len(intent_ids) > 1:
            shape = "multi_intent"
        elif shape == "single_intent":
            shape = "same_intent_multi_task"
    reanchored_envelope = envelope.model_copy(
        update={"tasks": tasks, "intent_shape": shape}
    )
    return _apply_slot_contracts(
        reanchored_envelope, request, repository, evaluator
    )


def _build_replan_envelope(
    request: RuntimeAgentRunRequest,
    envelope: IntentTaskEnvelope,
    active_task: IntentTask,
    proposals: list[Any],
    repository: IntentGraphRepository,
    evaluator: SlotContractEvaluator,
) -> tuple[IntentTaskEnvelope, dict[str, SlotEvaluation], str]:
    """同意图重构提案 → 新信封（确定性构建 + 全量槽位重评）。

    跨意图提案直接拒绝（编排器只采納当前意图的结构重构）；结构标识
    （action/business_object/risk_level/task_id）从活动任务确定性继承，
    模型只提供语义意图与槽位原始值，槽位 = 活动任务槽位 + 提案覆盖。
    返回 (新信封, 重评结果, 首个新任务 id)。
    """
    intent_id = active_task.intent_id
    if any(item.intent_id != intent_id for item in proposals):
        raise AgentContractError("replan proposals must stay on the active intent")
    candidate = repository.candidate_for_intent(
        intent_id, score=0, evidence=["replan_adoption"]
    )
    existing_ids = {task.task_id for task in envelope.tasks}
    kept = [
        task for task in envelope.tasks
        if task.task_id != active_task.task_id
    ]
    new_tasks: list[IntentTask] = []
    for index, proposal in enumerate(proposals, start=1):
        task_id = f"{active_task.task_id}_r{index}"
        while task_id in existing_ids:
            index += 1
            task_id = f"{active_task.task_id}_r{index}"
        existing_ids.add(task_id)
        slots = dict(active_task.slots)
        for name, raw in proposal.slots.items():
            slots[name] = SlotValue(value=raw, source="user_explicit", confidence=1)
        new_tasks.append(IntentTask(
            task_id=task_id,
            intent_id=intent_id,
            action=active_task.action,
            business_object=active_task.business_object,
            slots=slots,
            dependencies=[],
            execution_readiness="needs_slots",
            risk_level=active_task.risk_level,
        ))
    # 同意图拆分必须可区分：两个任务的槽位绑定完全相同意味着同一业务
    # 动作会被重复执行（对转账即双笔打款）。此类提案是模型幻觉结构，
    # 拒绝采納，由调用方降级为普通澄清在原结构上继续。
    signatures = [
        tuple(sorted((name, repr(item.value)) for name, item in task.slots.items()))
        for task in new_tasks
    ]
    if len(set(signatures)) != len(signatures):
        raise AgentContractError(
            "replan proposals contain indistinct duplicate task bindings"
        )
    merged_tasks = kept + new_tasks
    distinct_intents = {task.intent_id for task in merged_tasks}
    merged = IntentTaskEnvelope.model_validate({
        **envelope.model_dump(),
        "revision": envelope.revision + 1,
        "intent_shape": (
            "single_intent"
            if len(merged_tasks) == 1
            else "same_intent_multi_task"
            if len(distinct_intents) == 1
            else "multi_intent"
        ),
        # 旧歧义引用被替换的任务 id，采納后一律作废，由域适配器在
        # 新任务集上重新派生。
        "ambiguities": [],
        "tasks": [task.model_dump() for task in merged_tasks],
    })
    merged, evaluations = _apply_slot_contracts(
        merged, request, repository, evaluator
    )
    return merged, evaluations, new_tasks[0].task_id


def _apply_slot_contracts(
    envelope: IntentTaskEnvelope,
    request: RuntimeAgentRunRequest,
    repository: IntentGraphRepository,
    evaluator: SlotContractEvaluator,
) -> tuple[IntentTaskEnvelope, dict[str, SlotEvaluation]]:
    remembered: dict[str, SlotValue] = {}
    if request.conversation_context.pending_intent_id:
        remembered = {
            item.name: SlotValue(
                value=item.value,
                source="context",
                confidence=1,
            )
            for item in request.conversation_context.slots
            if item.source_intent_id == request.conversation_context.pending_intent_id
        }
    remembered_by_task: dict[str, dict[str, SlotValue]] = {
        goal.task_id: {
            item.name: SlotValue(value=item.value, source="context", confidence=1)
            for item in goal.slots
        }
        for goal in request.conversation_context.goals
    }
    shared_remembered = {
        item.name: SlotValue(value=item.value, source="context", confidence=1)
        for item in request.conversation_context.shared_slots
    }
    # 同族任务 fromAccount 传播：付款账户是用户对整批转账的一次性选择，
    # 不是逐笔追问。from conversation_context.goals 和 shared_slots 中
    # 保留原始 source（user_explicit/user_selection），因为
    # remembered_by_task 和 shared_remembered 会将 source 降级为 context，
    # 而 _assess 对 account_reference 拒绝 context。
    _sibling_from_accounts: dict[str, SlotValue] = {}
    for goal in request.conversation_context.goals:
        for slot in goal.slots:
            if (
                slot.name == "fromAccount"
                and slot.source in ("user_explicit", "user_selection")
            ):
                _sibling_from_accounts.setdefault(
                    goal.intent_id,
                    SlotValue(
                        value=slot.value,
                        source=slot.source,
                        confidence=1,
                    ),
                )
    # shared_slots 中的 fromAccount：applyBusinessReplySelection 将
    # fromAccount 存入 shared_slots（source 为 user_selection），而
    # shared_remembered 将 source 降级为 context。需要用原始 source
    # 覆盖，使同族任务传播生效。
    for slot in request.conversation_context.shared_slots:
        if (
            slot.name == "fromAccount"
            and slot.source in ("user_explicit", "user_selection")
        ):
            _sibling_from_accounts.setdefault(
                slot.source_intent_id,
                SlotValue(
                    value=slot.value,
                    source=slot.source,
                    confidence=1,
                ),
            )
    tasks = []
    evaluations: dict[str, SlotEvaluation] = {}
    for task in envelope.tasks:
        candidate = repository.candidate_for_intent(
            task.intent_id, score=0, evidence=["slot_evaluation"]
        )
        contract = candidate.slot_contract
        if contract is None:
            tasks.append(task)
            continue
        conversation_slots = (
            remembered
            if request.conversation_context.pending_intent_id == task.intent_id
            else {}
        )
        current_slots = dict(task.slots)
        # Normalizer-backed extraction is platform-owned and therefore
        # overrides a model guess. Extraction is per-contract — a task only
        # takes values for slots its own contract declares with normalizers —
        # and one message value landing on multiple sibling tasks is exactly
        # the declared ambiguity layer's arbitration domain (amount_scope),
        # not a reason to skip extraction. Ambiguous multi-value text yields
        # no extraction and remains a clarification.
        current_slots.update(
            extract_normalized_message_slots(contract, request.message)
        )
        for name, value in remembered_by_task.get(task.task_id, {}).items():
            current_slots.setdefault(name, value)
        # 同族任务 fromAccount 传播：付款账户是用户对整批转账的一次性
        # 选择，不是逐笔追问。直接赋值而非 setdefault：remembered_by_task
        # 将 source 降级为 context，_assess 对 account_reference 拒绝
        # context，需要用原始 user_explicit/user_selection source 覆盖。
        sibling_fa = _sibling_from_accounts.get(task.intent_id)
        current_fa = current_slots.get("fromAccount")
        if sibling_fa is not None:
            if current_fa is None or getattr(
                current_fa, "source", None
            ) not in ("user_explicit", "user_selection"):
                current_slots["fromAccount"] = sibling_fa
        for name, value in shared_remembered.items():
            if should_withhold_shared_slot(
                repository,
                task,
                name=name,
                context_ambiguities=request.conversation_context.ambiguities,
                message=request.message,
            ):
                continue
            current_slots.setdefault(name, value)
        # 澄清回答提取：轻量模型偶发未将账户选择填入 fromAccount 槽位，
        # 平台兜底从用户消息中提取账户引用（如"尾号6217"）。
        # current_slot_names 只含 user_explicit/user_selection source 的槽位
        # 名，intent_default 填的 fromAccount 不在其中——否则
        # extract_clarification_answer_slots 会跳过 fromAccount，用户的澄清
        # 回答永远填不进去，导致死循环（fromAccount 永远 ambiguous）。
        _user_filled_slot_names = frozenset(
            name
            for name, value in current_slots.items()
            if getattr(value, "source", None)
            in ("user_explicit", "user_selection")
        )
        current_slots.update(
            extract_clarification_answer_slots(
                contract,
                request.message,
                _user_filled_slot_names,
            )
        )
        # 合计口径声明使歧义继承的每笔金额槽位失效（含本轮消息提取值）：
        # 口径未定时的申报金额是总额而非每笔份额，域语义要求逐笔追问，
        # 不自动均分。放在全部合并之后，保证继承槽位与提取槽位一并失效。
        scope_withheld_slots = {
            name
            for name in current_slots
            if should_withhold_scope_inherited_slot(
                repository,
                task,
                name=name,
                context_ambiguities=request.conversation_context.ambiguities,
                message=request.message,
            )
        }
        for name in scope_withheld_slots:
            current_slots.pop(name, None)
        evaluation = evaluator.evaluate(
            contract=contract,
            current_slots=current_slots,
            conversation_slots=conversation_slots,
        )
        evaluations[task.task_id] = evaluation
        normalized_slots = dict(current_slots)
        for definition in contract.slots:
            assessment = evaluation.assessment(definition.name)
            if assessment.semantic_value is None:
                continue
            source_value = next(
                (
                    current_slots[key]
                    for key in (definition.name, *definition.extraction_keys)
                    if key in current_slots
                ),
                None,
            )
            normalized_slots[definition.name] = SlotValue(
                value=assessment.semantic_value,
                source=source_value.source if source_value else "context",
                confidence=source_value.confidence if source_value else 1,
            )
            if assessment.resolved_value is not None:
                normalized_slots[definition.resolved_field] = SlotValue(
                    value=assessment.resolved_value,
                    source=source_value.source if source_value else (
                        "intent_default"
                        if definition.default_value is not None else "context"
                    ),
                    confidence=source_value.confidence if source_value else 1,
                )
        tasks.append(task.model_copy(update={
            "slots": normalized_slots,
            "execution_readiness": evaluation.readiness,
        }))
    ambiguities = list(envelope.ambiguities)
    ambiguities = [
        item for item in ambiguities
        if not domain_ambiguity_is_resolved(
            repository,
            envelope,
            item,
            context_ambiguities=request.conversation_context.ambiguities,
            message=request.message,
            tasks=tasks,
        )
    ]
    # 结构歧义派生是域适配器的职责（按任务内容而非信封构成判定），
    # 在槽位评估后执行：跨域信封下转账任务的金额歧义不会因为其他域
    # 任务的存在而丢失，也不依赖形状提示的 task_indexes 位置映射。
    ambiguities = [
        *ambiguities,
        *derive_domain_ambiguities(
            repository, tasks, request.message, ambiguities,
            derive_kwargs={"shared_slots": envelope.shared_slots},
        ),
    ]
    return envelope.model_copy(update={
        "tasks": tasks,
        "ambiguities": ambiguities,
    }), evaluations


def _slot_assessment_payload(
    evaluations: dict[str, SlotEvaluation],
) -> dict[str, Any] | list[Any]:
    payloads = []
    for task_id, evaluation in evaluations.items():
        payloads.append({
            "taskId": task_id,
            "missing": list(evaluation.missing),
            "ambiguous": list(evaluation.ambiguous),
            "invalid": list(evaluation.invalid),
            "recognized": list(evaluation.recognized),
            "optionalAbsent": list(evaluation.optional_absent),
            "readiness": evaluation.readiness,
        })
    if len(payloads) == 1:
        result = dict(payloads[0])
        result.pop("taskId")
        return result
    return payloads


def _slot_summary(
    envelope: IntentTaskEnvelope,
    evaluations: dict[str, SlotEvaluation],
    constraints: list[IntentConstraint],
) -> str | None:
    if len(envelope.tasks) != 1:
        return None
    task = envelope.tasks[0]
    evaluation = evaluations.get(task.task_id)
    constraint = next(
        (item for item in constraints if item.intent_id == task.intent_id), None
    )
    contract = constraint.slot_contract if constraint is not None else None
    definitions = contract.get("slots") if isinstance(contract, dict) else None
    if evaluation is None or not isinstance(definitions, list):
        return None
    user_required = [
        item for item in definitions
        if isinstance(item, dict)
        and item.get("required") is True
        and "defaultValue" not in item
        and isinstance(item.get("name"), str)
        and isinstance(item.get("label"), str)
    ]
    if not user_required:
        return None
    complete_labels: list[str] = []
    pending_labels: list[str] = []
    for definition in user_required:
        try:
            assessment = evaluation.assessment(definition["name"])
        except KeyError:
            pending_labels.append(definition["label"])
            continue
        target = (
            complete_labels
            if assessment.state in {"extracted", "resolved"}
            else pending_labels
        )
        target.append(definition["label"])
    parts = [f"槽位完整度 {len(complete_labels)}/{len(user_required)}"]
    if complete_labels:
        parts.append("已识别 " + "、".join(complete_labels))
    if pending_labels:
        parts.append("待补充 " + "、".join(pending_labels))
    summary = " · ".join(parts)
    return summary if len(summary) <= 160 else None


def _build_conversation_state(
    response_status: str,
    envelope: IntentTaskEnvelope,
    evaluations: dict[str, SlotEvaluation],
    covered_task_ids: frozenset[str] | None = None,
) -> ConversationState:
    if response_status in {
        "clarification_required", "awaiting_confirmation"
    } and envelope.tasks:
        # 澄清/确认轮同样排除已交付结果的任务：前缀结果交付后残留的
        # 待办 goal 会被后续意图轮合并进新 envelope，造成已答复问题
        # 被反复续跑。covered 为 None 时保持原行为（不过滤）。
        goal_tasks = [
            task for task in envelope.tasks
            if is_intent_task_in_play(task.task_status)
            and (covered_task_ids is None
            or task.task_id not in covered_task_ids)
        ]
    elif (
        response_status == "succeeded"
        and covered_task_ids is not None
        and envelope.tasks
    ):
        # 部分完成的多目标运行：只保留未覆盖的待办目标，已交付结果的任务
        # 不再作为目标下发，避免下一轮重复执行。
        goal_tasks = [
            task for task in envelope.tasks
            if task.task_status
            in {"pending", "needs_slots", "awaiting_confirmation", "needs_clarification"}
            and task.task_id not in covered_task_ids
        ]
    else:
        goal_tasks = []
    if goal_tasks:
        goals = []
        ambiguous_task_ids = {
            task_id
            for ambiguity in envelope.ambiguities
            for task_id in ambiguity.task_ids
        }
        for task in goal_tasks:
            evaluation = evaluations.get(task.task_id)
            slots = []
            _slot_names_included: set[str] = set()
            if evaluation is not None:
                for name in evaluation.recognized:
                    value = evaluation.semantic_values.get(name)
                    source_slot = task.slots.get(name)
                    if value is None or source_slot is None:
                        continue
                    slots.append({
                        "name": name,
                        "value": value,
                        "source": source_slot.source,
                    })
                    _slot_names_included.add(name)
            # Defense-in-depth: 确保 fromAccount（用户已确认的付款账户）
            # 出现在 conversation_state.goals 中，即使 evaluation.recognized
            # 未包含它。这是批量转账跨轮次传播的关键链路。
            _fa_slot = task.slots.get("fromAccount")
            if (
                _fa_slot is not None
                and "fromAccount" not in _slot_names_included
                and getattr(_fa_slot, "source", None)
                in ("user_explicit", "user_selection")
            ):
                slots.append({
                    "name": "fromAccount",
                    "value": str(_fa_slot.value),
                    "source": _fa_slot.source,
                })
            goal_status = (
                "provisional"
                if task.task_id in ambiguous_task_ids
                else "ready" if task.execution_readiness == "ready" else "needs_slots"
            )
            goals.append({
                "taskId": task.task_id,
                "intentId": task.intent_id,
                "status": goal_status,
                "slots": slots,
            })
        graph_slot_names = {
            assessment.name
            for evaluation in evaluations.values()
            for assessment in evaluation.assessments
        }
        shared_slots = [
            {
                "name": name,
                "value": slot.value,
                "source": slot.source,
            }
            for name, slot in envelope.shared_slots.items()
            if name in graph_slot_names and slot.value is not None
        ]
        retained_task_ids = {task.task_id for task in goal_tasks}
        # 批量转账跨轮次 fromAccount 传播：已交付（completed/covered）任务
        # 被 goal_tasks 过滤后，其 fromAccount 不会出现在 goals 中。但付款
        # 账户是用户对整批操作的一次性选择，后续任务需要继承。将已完成任务
        # 的 fromAccount 注入 shared_slots，使 _sibling_from_accounts 在下
        # 一轮能找到它。
        _shared_slot_names = {item["name"] for item in shared_slots}
        for _task in envelope.tasks:
            if _task.task_id in retained_task_ids:
                continue
            _fa = _task.slots.get("fromAccount")
            if (
                _fa is not None
                and "fromAccount" not in _shared_slot_names
                and getattr(_fa, "source", None) in ("user_explicit", "user_selection")
                and _fa.value
            ):
                shared_slots.append({
                    "name": "fromAccount",
                    "value": str(_fa.value),
                    "source": _fa.source,
                })
                _shared_slot_names.add("fromAccount")
                break
        return ConversationState.model_validate({
            "schemaVersion": "2.0",
            "pendingEnvelopeId": envelope.delegation_id,
            "envelopeRevision": envelope.revision,
            "intentShape": envelope.intent_shape,
            "goals": goals,
            "sharedSlots": shared_slots,
            "ambiguities": [
                {
                    "type": item.type,
                    "taskIds": item.task_ids,
                    "options": item.options,
                }
                for item in envelope.ambiguities
                # 规划器偶发申报退化歧义（task_ids 少于 2 个：单任务
                # amount_scope 等）——ConversationState 要求歧义至少覆盖
                # 两个目标，保留会让终态投影整轮崩溃；与规划门对退化歧义
                # 的机械跳过同一原则，落入正常槽位澄清。
                # 已完成任务的 id 不进 goals（作为前缀结果单独投递）；仍
                # 引用它们的歧义对其余主题已失去待澄清意义，保留会让
                # ConversationState 的"歧义必须引用已知目标"校验整轮失败。
                if len(item.task_ids) >= 2 and set(item.task_ids).issubset(retained_task_ids)
            ],
            "status": "retain",
        })
    return ConversationState.model_validate({
        "schemaVersion": "1.0",
        "pendingIntentId": None,
        "status": "clear",
        "slots": [],
    })


def _recall_residual_candidates(
    residual: str,
    repository: IntentGraphRepository,
    page_context: dict[str, Any],
) -> list[IntentCandidate]:
    """Return graph candidates; semantic intent classification belongs to UserAgent."""

    if not residual:
        return []
    return repository.recall(residual, page_context=page_context)

def _intent_summary(
    envelope: IntentTaskEnvelope,
    constraints: list[IntentConstraint],
) -> str:
    if envelope.intent_shape == "ambiguous_intent":
        return "需要澄清业务意图"
    if envelope.intent_shape == "non_task":
        return "非任务型请求"
    title_by_id = {item.intent_id: item.title for item in constraints}
    titles = list(
        dict.fromkeys(
            title_by_id[task.intent_id]
            for task in envelope.tasks
            if task.intent_id in title_by_id
        )
    )
    summary = "、".join(titles) or "已识别业务意图"
    return summary if len(summary) <= 160 else summary[:159] + "…"


def _validation_summary(error: ValidationError) -> str:
    return ";".join(
        ".".join(str(part) for part in issue.get("loc", ()))
        + ":"
        + str(issue.get("type", "invalid"))
        + ":"
        + str(issue.get("msg", "invalid"))[:160]
        for issue in error.errors(include_url=False, include_context=False, include_input=False)[:8]
    )
