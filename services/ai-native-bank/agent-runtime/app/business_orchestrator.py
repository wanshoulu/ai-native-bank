"""Bounded outer loop supervising the Business DeepAgent's native Todo loop."""

from __future__ import annotations

import logging
import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from app.business_loop import (
    BusinessLoopOutcome,
    TodoSnapshotItem,
    normalize_terminal_loop_submission,
    todo_belongs_to_goal,
    validate_goal_coverage,
)
from app.business_todo import GoalState, select_active_goal
from app.contracts import GuidedEntryGoal, IntentTaskEnvelope
from app.todo_ledger import TodoLedgerSnapshot


logger = logging.getLogger("ai_native_bank.agent_runtime.business_orchestrator")


class BusinessLoopInvariantError(ValueError):
    """The model-visible loop diverged from the authoritative workflow state."""


class BusinessLoopStalled(BusinessLoopInvariantError):
    """The bounded supervisor budget was exhausted without a suspension or terminal."""


class TodoLedgerRunner(Protocol):
    def load_todo_ledger(
        self, session_id: str, revision: int = 1
    ) -> TodoLedgerSnapshot: ...

    def restore_todo_ledger(
        self, session_id: str, revision: int, items: list[dict[str, Any]]
    ) -> TodoLedgerSnapshot: ...


IterationRunner = Callable[
    [dict[str, Any]],
    Awaitable[dict[str, Any] | tuple[dict[str, Any] | None, list[dict[str, Any]]] | None],
]


@dataclass(frozen=True)
class TaskInvocation:
    task_id: str
    capability_id: str
    input: dict[str, Any]
    output: dict[str, Any]


@dataclass(frozen=True)
class BusinessLoopAdvance:
    outcome: BusinessLoopOutcome
    snapshot: TodoLedgerSnapshot
    successful_invocations: tuple[dict[str, Any], ...]
    task_invocations: tuple[TaskInvocation, ...]
    completed_task_ids: tuple[str, ...]


@dataclass(frozen=True)
class GuidedTodo:
    item_id: str
    capability_id: str
    requires_confirmation: bool
    input_bindings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GuidedWorkflowPlan:
    target_intent_id: str
    todos: tuple[GuidedTodo, ...]


def plan_guided_goal(
    entry_goal: GuidedEntryGoal, snapshot: Mapping[str, Any] | None = None
) -> GuidedWorkflowPlan:
    """Expand trusted guided goals without letting model text choose capabilities."""

    if entry_goal.target_intent_id == "ccb.cashflow.salary-plan":
        return GuidedWorkflowPlan(entry_goal.target_intent_id, (
            GuidedTodo("salary-plan", "bank.cashflow.salary-plan.get", False),
        ))
    if entry_goal.target_intent_id == "ccb.cashflow.spending-explain":
        period = entry_goal.trusted_inputs.get("period")
        if not isinstance(period, str) or re.fullmatch(
            r"\d{4}-(0[1-9]|1[0-2])", period
        ) is None:
            raise BusinessLoopInvariantError(
                "guided spending period is unavailable"
            )
        return GuidedWorkflowPlan(entry_goal.target_intent_id, (
            GuidedTodo(
                "spending-analysis",
                "bank.cashflow.spending-analysis.get",
                False,
                {"period": period},
            ),
        ))
    if entry_goal.target_intent_id == "ccb.home.repayment.repay":
        card_id = entry_goal.trusted_inputs.get("cardId")
        account_id = entry_goal.trusted_inputs.get("fromAccountId")
        if any(
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) is None
            for value in (card_id, account_id)
        ):
            raise BusinessLoopInvariantError(
                "guided credit-card repayment context is unavailable"
            )
        return GuidedWorkflowPlan(entry_goal.target_intent_id, (
            GuidedTodo(
                "load-card-bill", "bank.credit-card.bill.get", False,
                {"cardId": card_id},
            ),
            GuidedTodo(
                "load-payment-account", "bank.account.list", False, {},
            ),
            GuidedTodo(
                "load-payment-balance", "bank.account.balance.get", False,
                {"accountId": account_id},
            ),
        ))
    if entry_goal.target_intent_id not in {
        "ccb.obligation.repay-all", "ccb.obligation.pay-selected"
    }:
        raise BusinessLoopInvariantError("guided goal target is not registered")
    if entry_goal.target_intent_id == "ccb.obligation.pay-selected":
        selected_obligation_id = entry_goal.trusted_inputs.get("obligationId")
        selected_obligation_type = entry_goal.trusted_inputs.get("obligationType")
        if (
            not isinstance(selected_obligation_id, str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", selected_obligation_id)
            is None
            or selected_obligation_type not in {"credit_card", "loan", "utility"}
        ):
            raise BusinessLoopInvariantError(
                "selected guided obligation is unavailable"
            )
    if snapshot is None:
        return GuidedWorkflowPlan(entry_goal.target_intent_id, (
            GuidedTodo("load-obligations", "bank.obligation.list", False),
        ))
    return plan_obligation_snapshot(
        snapshot,
        selected_obligation_id=entry_goal.trusted_inputs.get("obligationId"),
        selected_obligation_type=entry_goal.trusted_inputs.get("obligationType"),
        target_intent_id=entry_goal.target_intent_id,
    )


def plan_obligation_snapshot(
    snapshot: Mapping[str, Any],
    *,
    selected_obligation_id: str | None = None,
    selected_obligation_type: str | None = None,
    target_intent_id: str = "ccb.obligation.repay-all",
) -> GuidedWorkflowPlan:
    """Expand trusted obligation facts into confirmation-bound action todos."""

    items = snapshot.get("items") if isinstance(snapshot, Mapping) else None
    account_id = snapshot.get("accountId") if isinstance(snapshot, Mapping) else None
    if not isinstance(items, list) or len(items) > 8:
        raise BusinessLoopInvariantError("guided obligation snapshot is invalid")
    if (
        not isinstance(account_id, str)
        or not account_id
        or len(account_id) > 160
    ):
        raise BusinessLoopInvariantError("guided obligation account is invalid")
    if selected_obligation_id is not None:
        if (
            not isinstance(selected_obligation_id, str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", selected_obligation_id)
            is None
        ):
            raise BusinessLoopInvariantError(
                "selected guided obligation is unavailable"
            )
        selected_items = [
            item for item in items
            if isinstance(item, Mapping)
            and item.get("id") == selected_obligation_id
            and (
                selected_obligation_type is None
                or item.get("type") == selected_obligation_type
            )
            and item.get("executionStatus") == "available"
        ]
        if len(selected_items) != 1:
            raise BusinessLoopInvariantError(
                "selected guided obligation is unavailable"
            )
        items = selected_items
    capability_by_type = {
        "credit_card": "bank.credit-card.repayment.create",
        "loan": "bank.loan.repayment.create",
        "utility": "bank.utility.payment.create",
    }
    todos: list[GuidedTodo] = []
    for item in items:
        item_id = item.get("id") if isinstance(item, Mapping) else None
        capability_id = capability_by_type.get(item.get("type")) if isinstance(item, Mapping) else None
        amount = item.get("amount") if isinstance(item, Mapping) else None
        amount_minor = amount.get("amountMinor") if isinstance(amount, Mapping) else None
        if (
            not isinstance(item_id, str) or not item_id or capability_id is None
            or not isinstance(amount_minor, int) or isinstance(amount_minor, bool)
            or amount_minor <= 0
        ):
            raise BusinessLoopInvariantError("guided obligation item is invalid")
        if item.get("executionStatus") == "available":
            if item.get("type") == "credit_card":
                input_bindings = {
                    "cardId": item_id,
                    "fromAccountId": account_id,
                    "amountMinor": amount_minor,
                }
            elif item.get("type") == "loan":
                input_bindings = {
                    "repaymentId": item_id,
                    "fromAccountId": account_id,
                    "amountMinor": amount_minor,
                }
            else:
                input_bindings = {"billId": item_id, "accountId": account_id}
            todos.append(GuidedTodo(
                item_id, capability_id, True, input_bindings
            ))
    return GuidedWorkflowPlan(target_intent_id, tuple(todos))


class BusinessLoopCoordinator:
    def __init__(self, business_runner: TodoLedgerRunner, *, max_iterations: int = 4):
        if not 1 <= max_iterations <= 16:
            raise ValueError("business loop iteration budget is invalid")
        self._business = business_runner
        self._max_iterations = max_iterations

    async def advance(
        self,
        *,
        envelope: IntentTaskEnvelope,
        workflow_id: str,
        session_id: str,
        initial_payload: dict[str, Any],
        run_iteration: Callable[..., Awaitable[Any]],
        expected_workflow_revision: int = 0,
        previous_active_task_id: str | None = None,
        expected_todo_revision: int = 0,
        capabilities_by_task: Mapping[str, frozenset[str]] | None = None,
    ) -> BusinessLoopAdvance:
        delegated_ids = tuple(
            item.task_id for item in envelope.tasks
            if item.task_status != "skipped"
        )
        if not delegated_ids:
            raise BusinessLoopInvariantError("business loop requires delegated goals")
        payload = dict(initial_payload)
        payload["workflow_control"] = {
            "expected_workflow_revision": expected_workflow_revision,
            "next_workflow_revision": expected_workflow_revision + 1,
            "expected_todo_revision": expected_todo_revision,
            "previous_active_goal_task_id": previous_active_task_id,
            "execution_policy": "sequential",
        }
        all_invocations: list[dict[str, Any]] = []
        task_invocations: list[TaskInvocation] = []
        previous_digest: str | None = None
        previous_todo_revision = expected_todo_revision
        repair_attempts = 0
        iteration_active_task_id = previous_active_task_id or delegated_ids[0]
        covered_ids: list[str] = []

        for iteration in range(1, self._max_iterations + 1):
            raw_result = await run_iteration(payload, session_id=session_id)
            iteration_invocations: list[dict[str, Any]] = []
            if isinstance(raw_result, tuple):
                submission, invocations = raw_result
                iteration_invocations = list(invocations)
            else:
                submission = raw_result
            # A terminal submission cannot have an active pointer. Some model
            # providers retain the just-completed Todo in those projection
            # fields. Normalize only this mechanically provable contradiction;
            # the authoritative Todo ledger is still validated below and an
            # unfinished ledger can never be promoted to a completed workflow.
            if isinstance(submission, dict) and submission.get("status") == "completed":
                submission = {
                    **submission,
                    "active_goal_task_id": None,
                    "active_todo_id": None,
                    "next_action": None,
                }
            # 与上面终态修复同类：部分模型会把 next_action 名（如
            # clarify_card_reference）投进 active_goal_task_id。该值不在
            # delegated 任务集中即可证明是投影垃圾；用本次迭代的权威活动
            # 任务替换（其被 LLM 声明覆盖时），否则清空指针。Todo 台账的
            # 权威校验在后续步骤仍然生效。
            if isinstance(submission, dict):
                # 轻量模型偶发漏投 schema_version，platform 固定 2.0 契约，
                # 机械回填即可，不依赖模型自纠轮。
                submission.setdefault("schema_version", "2.0")
                # workflow_id/workflow_revision 是编排器本轮从持久化状态
                # 算出的簿记值，模型回显错值不携带任何信息（与 active
                # 指针修复同一原则）：以权威值覆盖，避免 revision invariant
                # 让整轮崩成 blocked。台账 todo_revision 仍独立校验模型
                # 是否在过期台账视图上作答。
                submission["business_workflow_id"] = workflow_id
                submission["workflow_revision"] = expected_workflow_revision + 1
                projected_goal = submission.get("active_goal_task_id")
                # 非终态提交必须有活动 goal/todo 指针：轻量模型偶发只投
                # status 与结果而漏投活动指针，schema 校验会直接拒绝并
                # 让整个 workflow 崩溃。活动任务由平台按迭代权威跟踪，
                # 此处机械回填即可，不依赖模型自纠轮。
                if submission.get("status") in (
                    "continue",
                    "awaiting_clarification",
                    "awaiting_confirmation",
                    "waiting_external",
                ):
                    if not (isinstance(projected_goal, str) and projected_goal):
                        submission = {
                            **submission,
                            "active_goal_task_id": iteration_active_task_id,
                            "active_todo_id": iteration_active_task_id,
                        }
                        projected_goal = iteration_active_task_id
                    if not (
                        isinstance(submission.get("active_todo_id"), str)
                        and submission.get("active_todo_id")
                    ):
                        submission = {
                            **submission,
                            "active_todo_id": submission["active_goal_task_id"],
                        }
                covered_ids = submission.get("covered_task_ids")
                if not isinstance(covered_ids, list):
                    covered_ids = []
                needs_repair = (
                    isinstance(projected_goal, str)
                    and projected_goal
                    and projected_goal not in covered_ids
                )
                if needs_repair and projected_goal not in delegated_ids:
                    submission = {**submission, "covered_task_ids": covered_ids}
                    if iteration_active_task_id in covered_ids:
                        submission["active_goal_task_id"] = iteration_active_task_id
                    elif submission.get("status") in ("continue", "awaiting_clarification", "awaiting_confirmation", "waiting_external"):
                        submission["active_goal_task_id"] = iteration_active_task_id
                        if covered_ids:
                            covered_ids = list(covered_ids)
                            if iteration_active_task_id not in covered_ids:
                                covered_ids.append(iteration_active_task_id)
                            submission["covered_task_ids"] = covered_ids
                        else:
                            submission["covered_task_ids"] = [iteration_active_task_id]
                    else:
                        submission["active_goal_task_id"] = None
                elif needs_repair and projected_goal in delegated_ids:
                    covered_ids = list(covered_ids)
                    if projected_goal not in covered_ids:
                        covered_ids.append(projected_goal)
                    submission = {**submission, "covered_task_ids": covered_ids}
            elif submission is None and iteration < self._max_iterations:
                # 循环结束但模型没有通过 submit_business_loop_outcome 提交
                # （常见于轻量模型以纯文本收尾或被工具错误带偏）。不直接判
                # 失败：注入一次有界修复轮，要求重新结构化提交。修复机会与
                # 台账覆盖修复共用预算，避免无限往返。
                if repair_attempts == 0:
                    repair_attempts += 1
                    payload = dict(initial_payload)
                    payload["repair_feedback"] = {
                        "attempt": repair_attempts,
                        "max_attempts": 1,
                        "codes": ["loop_outcome_submission_missing"],
                        "required_goal_task_ids": list(delegated_ids),
                    }
                    payload["workflow_control"] = {
                        "expected_workflow_revision": expected_workflow_revision,
                        "next_workflow_revision": expected_workflow_revision + 1,
                        "expected_todo_revision": previous_todo_revision,
                        "previous_active_goal_task_id": previous_active_task_id,
                        "execution_policy": "sequential",
                    }
                    logger.warning(
                        "Business loop requested bounded outcome submission repair"
                    )
                    continue
            # submission 仍为 None（模型用完修复轮仍未通过
            # submit_business_loop_outcome 提交）。构造确定性降级
            # outcome 而非让 Pydantic 崩溃，让上游能以 failed 状态
            # 正常回复用户而非返回 blocked。
            if submission is None:
                logger.warning(
                    "Business loop ended without structured submission; "
                    "synthesizing failed outcome (iteration=%s)", iteration
                )
                submission = {
                    "schema_version": "2.0",
                    "business_workflow_id": workflow_id,
                    "status": "failed",
                    "covered_task_ids": list(covered_ids) if covered_ids else [],
                    "workflow_revision": expected_workflow_revision + 1,
                    "todo_revision": max(1, previous_todo_revision),
                    "active_goal_task_id": None,
                    "active_todo_id": None,
                    "next_action": None,
                }
            try:
                outcome = BusinessLoopOutcome.model_validate(
                    normalize_terminal_loop_submission(submission)
                )
            except Exception as exc:
                raise BusinessLoopInvariantError("business loop outcome is invalid") from exc
            if (
                outcome.business_workflow_id != workflow_id
                or outcome.workflow_revision != expected_workflow_revision + 1
                or outcome.todo_revision < max(1, previous_todo_revision)
            ):
                logger.warning(
                    "Business loop contract mismatch workflow_match=%s workflow_revision=%s expected_workflow_revision=%s todo_revision=%s minimum_todo_revision=%s",
                    outcome.business_workflow_id == workflow_id,
                    outcome.workflow_revision,
                    expected_workflow_revision + 1,
                    outcome.todo_revision,
                    max(1, previous_todo_revision),
                )
                raise BusinessLoopInvariantError("business loop revision conflict")

            snapshot = self._business.load_todo_ledger(
                session_id, revision=outcome.todo_revision
            )
            uncovered = validate_goal_coverage(
                delegated_ids, (item.id for item in snapshot.items)
            )
            if uncovered:
                if repair_attempts == 0 and iteration < self._max_iterations:
                    repair_attempts += 1
                    previous_digest = snapshot.digest
                    previous_todo_revision = outcome.todo_revision
                    payload = dict(initial_payload)
                    payload["repair_feedback"] = {
                        "attempt": repair_attempts,
                        "max_attempts": 1,
                        "codes": ["todo_goal_coverage_missing"],
                        "required_goal_task_ids": list(delegated_ids),
                    }
                    payload["workflow_control"] = {
                        "expected_workflow_revision": expected_workflow_revision,
                        "next_workflow_revision": expected_workflow_revision + 1,
                        "expected_todo_revision": previous_todo_revision,
                        "previous_active_goal_task_id": previous_active_task_id,
                        "execution_policy": "sequential",
                    }
                    logger.warning(
                        "Business loop requested bounded Todo coverage repair missing_goal_count=%s",
                        len(uncovered),
                    )
                    continue
                # 模型在修复轮后仍提交不覆盖 delegated 集的台账视图（轻量
                # 模型会把 delegated 目标整体重写为自己的通用计划 id）。
                # delegated 集是权威边界：按 envelope 确定性重播权威台账
                # 继续推进，让澄清/确认等下游语义正常产出，而不是让整轮
                # blocked。模型自造的 todo id 与其 todo_revision 一样是
                # 回显簿记，不携带信息（与 workflow_revision 覆盖同一原则）。
                fallback_snapshot = self._business.seed_todo_ledger(
                    session_id,
                    [
                        {
                            "id": task.task_id,
                            "status": (
                                "in_progress"
                                if task.task_id == iteration_active_task_id
                                else "pending"
                            ),
                            "dependsOn": [],
                            "title": task.intent_id,
                        }
                        for task in envelope.tasks
                        if task.task_status != "skipped"
                    ],
                )
                logger.warning(
                    "Business loop rejected model todo rewrite; reseeded"
                    " authoritative ledger"
                )
                snapshot = fallback_snapshot
                outcome = outcome.model_copy(
                    update={"todo_revision": fallback_snapshot.revision}
                )
            # The native Todo ledger is the authoritative goal-coverage record.
            # covered_task_ids is a model-authored projection and may contain only
            # the current goal even when the ledger correctly covers every goal.
            outcome = outcome.model_copy(
                update={"covered_task_ids": list(delegated_ids)}
            )
            goals = goal_states_from_todos(envelope, snapshot.items)
            try:
                selected = select_active_goal(goals, previous_active_task_id)
            except ValueError as exc:
                raise BusinessLoopInvariantError("sequential goal state conflict") from exc
            selected_id = selected.task_id if selected is not None else None
            if selected_id is None and goals and all(
                item.status == "succeeded" for item in goals
            ):
                # The native Todo ledger is authoritative. Some models retain
                # the just-completed goal in their terminal projection; clear
                # that stale pointer instead of turning a completed, verified
                # ledger into a false workflow failure.
                outcome = outcome.model_copy(update={
                    "status": "completed",
                    "active_goal_task_id": None,
                    "active_todo_id": None,
                    "next_action": None,
                })
            if outcome.active_goal_task_id != selected_id:
                if selected_id is not None:
                    # 台账仍有未完成 goal，但模型投影的 active goal 缺失或
                    # 指错（常见于轻量模型完成部分 Todo 后提前提交终局）。
                    # active goal 由已验证台账权威推导，直接纠正投影即可，
                    # 与上方终局投影纠正遵循同一原则，不必消耗修复轮。若
                    # 模型同时宣告 completed/succeeded 终态，则与"仍有活动
                    # goal"矛盾，一并降级为 awaiting_clarification，交还平
                    # 台确定性槽位链路向用户收集缺失信息。
                    submitted_active = outcome.active_goal_task_id
                    update = {
                        "active_goal_task_id": selected_id,
                        "active_todo_id": selected_id,
                    }
                    if outcome.status in {"completed", "succeeded"}:
                        update["status"] = "awaiting_clarification"
                        update["next_action"] = "collect_required_slots"
                    outcome = outcome.model_copy(update=update)
                    logger.warning(
                        "Business loop corrected active goal projection "
                        "submitted=%s authoritative=%s status=%s",
                        submitted_active,
                        selected_id,
                        update.get("status", outcome.status),
                    )
                else:
                    raise BusinessLoopInvariantError(
                        "business loop active goal conflict"
                    )
            # active_todo_id 权威化：模型投影与上面的机械回填都可能给出
            # 不存在或跨 goal 的 todo 指针（goal task id 并不等于台账中
            # 模型自定义的 todo 语义 id）。活动 goal 已由台账权威推导，
            # 其活动 todo 同样可机械推导：取属于该 goal 且未完成的首个
            # todo。找不到时保留原值，交由后续存在性校验兜底。
            if selected_id is not None:
                authoritative_todo = next(
                    (
                        item.id
                        for item in snapshot.items
                        if item.status not in ("completed", "cancelled")
                        and todo_belongs_to_goal(
                            item.id, selected_id, delegated_ids
                        )
                    ),
                    None,
                )
                if authoritative_todo is not None and (
                    outcome.active_todo_id is None
                    or not any(
                        item.id == outcome.active_todo_id
                        for item in snapshot.items
                    )
                    or not todo_belongs_to_goal(
                        outcome.active_todo_id, selected_id, delegated_ids
                    )
                ):
                    outcome = outcome.model_copy(
                        update={"active_todo_id": authoritative_todo}
                    )
            if iteration_invocations:
                if capabilities_by_task is None:
                    raise BusinessLoopInvariantError(
                        "active goal capability boundary is missing"
                    )
                # 使用所有任务能力并集进行对账：多任务场景下 LLM 可能
                # 在当前活跃任务的迭代中调用其他任务的能力（如查余额
                # 迭代中预读收款方列表），per-task 边界会误判为冲突。
                allowed = set().union(
                    *capabilities_by_task.values()
                ) if capabilities_by_task else frozenset()
                for invocation in iteration_invocations:
                    capability_id = invocation.get("capability_id")
                    input_payload = invocation.get("input")
                    output_payload = invocation.get("output")
                    if (
                        capability_id not in allowed
                        or not isinstance(input_payload, dict)
                        or not isinstance(output_payload, dict)
                    ):
                        raise BusinessLoopInvariantError(
                            "active goal capability conflict"
                        )
                    copied = {
                        "capability_id": capability_id,
                        "input": copy.deepcopy(input_payload),
                        "output": copy.deepcopy(output_payload),
                    }
                    if any(
                        existing.task_id == iteration_active_task_id
                        and existing.capability_id == capability_id
                        and existing.input == input_payload
                        and existing.output == output_payload
                        for existing in task_invocations
                    ):
                        continue
                    all_invocations.append(copied)
                    task_invocations.append(TaskInvocation(
                        task_id=iteration_active_task_id,
                        capability_id=capability_id,
                        input=copy.deepcopy(input_payload),
                        output=copy.deepcopy(output_payload),
                    ))
            # 泛化对账：succeeded 的 QUERY goal 必须有调用背书。resolve 阶段
            # 调用引用解析器、execute 阶段调用执行能力，两者都会在
            # task_invocations 留下记录。没有任何调用却被标完成是模型投影
            # 幻觉，会让下游把未执行的任务当作已完成结果交付。回滚其 Todo
            # 后重新选 active goal，交给平台确定性槽位解析接管，不依赖模型
            # 修复轮（修复轮可能再次跳步或空提交）。此规则不针对任何特定
            # 意图或任务，只要求查询任务的完成必须可被能力调用证明。
            # 写类任务（DO/CHANGE/CANCEL）在单次业务 loop 内不可能合法完
            # 成：写操作只能经确认 checkpoint → 用户确认 → 授权 resume 才
            # 算执行。loop 内被标 succeeded（如只查了收款人列表就宣告转账
            # 完成）一律回滚，交还平台确定性链路生成确认卡。
            unbacked_task_ids = [
                task.task_id
                for task in envelope.tasks
                if any(
                    g.task_id == task.task_id and g.status == "succeeded"
                    for g in goals
                )
                and (
                    task.action in {"DO", "CHANGE", "CANCEL"}
                    or (
                        task.action == "QUERY"
                        and not any(
                            inv.task_id == task.task_id
                            for inv in task_invocations
                        )
                    )
                )
            ]
            if unbacked_task_ids:
                corrected_items = [
                    {
                        "id": item.id,
                        "status": (
                            "in_progress"
                            if item.status == "completed"
                            and any(
                                todo_belongs_to_goal(item.id, tid, delegated_ids)
                                for tid in unbacked_task_ids
                            )
                            else item.status
                        ),
                        "dependsOn": list(item.depends_on),
                    }
                    for item in snapshot.items
                ]
                snapshot = self._business.restore_todo_ledger(
                    session_id, outcome.todo_revision, corrected_items
                )
                goals = goal_states_from_todos(envelope, snapshot.items)
                selected = select_active_goal(goals, None)
                if selected is None:
                    raise BusinessLoopInvariantError(
                        "unbacked goal rollback left no active goal"
                    )
                rollback_todo = next(
                    (
                        item.id
                        for item in snapshot.items
                        if item.status not in ("completed", "cancelled")
                        and todo_belongs_to_goal(
                            item.id, selected.task_id, delegated_ids
                        )
                    ),
                    None,
                )
                # 回滚后的 active_todo_id 必须是 snapshot 中真实存在且属于
                # selected goal 的 todo。multi-goal 场景下 selected.task_id
                # 是 envelope 维度的 goal id，通常不是台账 todo id，直接
                # fallback 会产生指向不存在 todo 的 outcome。此处显式校验，
                # 不再静默 fallback，与下方统一一致性校验保持同一标准。
                if rollback_todo is None:
                    raise BusinessLoopInvariantError(
                        "unbacked rollback selected goal has no resumable todo"
                    )
                outcome = outcome.model_copy(update={
                    "status": "awaiting_clarification",
                    "active_goal_task_id": selected.task_id,
                    "active_todo_id": rollback_todo,
                    "next_action": "collect_required_slots",
                })
                # 回滚路径必须在返回前通过与正常路径一致的存在性/跨 goal
                # 校验，避免回滚后的 outcome 绕过一致性断言（时序错位）。
                if not any(
                    item.id == outcome.active_todo_id for item in snapshot.items
                ):
                    raise BusinessLoopInvariantError(
                        "business loop active todo is missing"
                    )
                if not todo_belongs_to_goal(
                    outcome.active_todo_id,
                    outcome.active_goal_task_id,
                    delegated_ids,
                ):
                    raise BusinessLoopInvariantError(
                        "business loop active todo belongs to another goal"
                    )
                logger.warning(
                    "Business loop reverted unbacked query completions "
                    "tasks=%s active=%s",
                    unbacked_task_ids,
                    selected.task_id,
                )
                return BusinessLoopAdvance(
                    outcome=outcome,
                    snapshot=snapshot,
                    successful_invocations=tuple(all_invocations),
                    task_invocations=tuple(task_invocations),
                    completed_task_ids=tuple(
                        goal.task_id for goal in goals if goal.status == "succeeded"
                    ),
                )
            if outcome.active_todo_id is not None and not any(
                item.id == outcome.active_todo_id for item in snapshot.items
            ):
                raise BusinessLoopInvariantError("business loop active todo is missing")
            if (
                outcome.active_todo_id is not None
                and outcome.active_goal_task_id is not None
                and not todo_belongs_to_goal(
                    outcome.active_todo_id,
                    outcome.active_goal_task_id,
                    delegated_ids,
                )
            ):
                raise BusinessLoopInvariantError(
                    "business loop active todo belongs to another goal"
                )
            if outcome.status == "completed" and any(
                item.status not in {"succeeded", "cancelled"} for item in goals
            ):
                raise BusinessLoopInvariantError("completed loop retains unfinished goals")
            if outcome.status != "continue":
                return BusinessLoopAdvance(
                    outcome=outcome,
                    snapshot=snapshot,
                    successful_invocations=tuple(all_invocations),
                    task_invocations=tuple(task_invocations),
                    completed_task_ids=tuple(
                        goal.task_id for goal in goals if goal.status == "succeeded"
                    ),
                )
            if previous_digest == snapshot.digest:
                if repair_attempts == 0 and iteration < self._max_iterations:
                    repair_attempts += 1
                    previous_todo_revision = outcome.todo_revision
                    payload = dict(initial_payload)
                    payload["repair_feedback"] = {
                        "attempt": repair_attempts,
                        "max_attempts": 1,
                        "codes": ["loop_turn_without_progress"],
                        "active_goal_task_id": outcome.active_goal_task_id,
                        "active_todo_id": outcome.active_todo_id,
                    }
                    payload["workflow_control"] = {
                        "expected_workflow_revision": expected_workflow_revision,
                        "next_workflow_revision": expected_workflow_revision + 1,
                        "expected_todo_revision": outcome.todo_revision,
                        "previous_active_goal_task_id": outcome.active_goal_task_id,
                        "execution_policy": "sequential",
                    }
                    logger.warning(
                        "Business loop requested bounded no-progress repair active_goal=%s",
                        outcome.active_goal_task_id,
                    )
                    continue
                raise BusinessLoopStalled("business loop continued without todo progress")
            if previous_todo_revision and outcome.todo_revision <= previous_todo_revision:
                raise BusinessLoopStalled("business loop continued without revision progress")
            previous_digest = snapshot.digest
            previous_todo_revision = outcome.todo_revision
            previous_active_task_id = outcome.active_goal_task_id
            iteration_active_task_id = outcome.active_goal_task_id
            payload = {
                "business_workflow_id": workflow_id,
                "loop_iteration": iteration + 1,
                "loop_feedback": {
                    "previous_status": outcome.status,
                    "previous_next_action": outcome.next_action,
                    "active_goal_task_id": outcome.active_goal_task_id,
                    "active_todo_id": outcome.active_todo_id,
                    "todo_revision": outcome.todo_revision,
                    "todo_digest": snapshot.digest,
                },
                "workflow_control": {
                    "expected_workflow_revision": expected_workflow_revision,
                    "next_workflow_revision": expected_workflow_revision + 1,
                    "expected_todo_revision": previous_todo_revision,
                    "previous_active_goal_task_id": previous_active_task_id,
                    "execution_policy": "sequential",
                },
            }
        raise BusinessLoopStalled(
            "business loop exhausted without suspension or terminal state"
        )


def goal_states_from_todos(
    envelope: IntentTaskEnvelope,
    items: tuple[TodoSnapshotItem, ...],
) -> tuple[GoalState, ...]:
    result: list[GoalState] = []
    active_tasks = [
        task for task in envelope.tasks
        if task.task_status != "skipped"
    ]
    delegated_ids = tuple(task.task_id for task in active_tasks)
    for sequence, task in enumerate(active_tasks, 1):
        matching = [
            item for item in items
            if todo_belongs_to_goal(item.id, task.task_id, delegated_ids)
        ]
        if any(item.status == "in_progress" for item in matching):
            status = "in_progress"
        elif matching and all(item.status in {"completed", "cancelled"} for item in matching):
            status = "cancelled" if all(item.status == "cancelled" for item in matching) else "succeeded"
        else:
            status = "ready"
        result.append(GoalState(task_id=task.task_id, sequence=sequence, status=status))
    return tuple(result)
