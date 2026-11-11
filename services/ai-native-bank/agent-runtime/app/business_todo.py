"""Deterministic safety rails for Business DeepAgent goal selection.

The Business Agent owns planning and Todo creation.  These helpers only enforce
ordering invariants that a financial workflow is not allowed to violate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


GoalStatus = Literal[
    "pending",
    "ready",
    "in_progress",
    "awaiting_clarification",
    "awaiting_confirmation",
    "waiting_external",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
]

TERMINAL_GOAL_STATUSES = frozenset({
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
})

ACTIVE_GOAL_STATUSES = frozenset({
    "in_progress",
    "awaiting_clarification",
    "awaiting_confirmation",
    "waiting_external",
})


@dataclass(frozen=True)
class GoalState:
    task_id: str
    sequence: int
    status: GoalStatus

    def __post_init__(self) -> None:
        if not self.task_id or len(self.task_id) > 96:
            raise ValueError("goal task id is invalid")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise ValueError("goal sequence is invalid")
        if self.sequence < 1:
            raise ValueError("goal sequence is invalid")


def validate_sequential_goals(goals: tuple[GoalState, ...]) -> None:
    task_ids = [item.task_id for item in goals]
    sequences = [item.sequence for item in goals]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("sequential workflow has duplicate goal ids")
    if len(sequences) != len(set(sequences)):
        raise ValueError("sequential workflow has duplicate goal sequences")
    if sum(item.status in ACTIVE_GOAL_STATUSES for item in goals) > 1:
        raise ValueError("sequential workflow has multiple active goals")


def select_active_goal(
    goals: tuple[GoalState, ...],
    previous_active_task_id: str | None,
) -> GoalState | None:
    """Return the sticky current goal, never a future goal with missing slots."""

    validate_sequential_goals(goals)
    indexed = {item.task_id: item for item in goals}
    previous = indexed.get(previous_active_task_id or "")
    if previous is not None and previous.status not in TERMINAL_GOAL_STATUSES:
        return previous
    return next(
        (
            item
            for item in sorted(goals, key=lambda value: value.sequence)
            if item.status not in TERMINAL_GOAL_STATUSES
        ),
        None,
    )


def sequential_progress(
    goals: tuple[GoalState, ...],
    active_task_id: str,
) -> dict[str, int | str]:
    validate_sequential_goals(goals)
    ordered = sorted(goals, key=lambda item: item.sequence)
    try:
        active_index = next(
            index for index, item in enumerate(ordered)
            if item.task_id == active_task_id
        )
    except StopIteration as exc:
        raise ValueError("active goal is not part of the workflow") from exc
    return {
        "mode": "sequential",
        "current": active_index + 1,
        "total": len(ordered),
        "completed": sum(
            item.status == "succeeded" for item in ordered
        ),
        "failed": sum(item.status == "failed" for item in ordered),
        "skipped": sum(
            item.status in {"skipped", "cancelled"} for item in ordered
        ),
    }


# ── IntentTask.task_status 谓词（service.py 中 7+ 处分散过滤的统一封装）──

def is_intent_task_in_play(task_status: str) -> bool:
    """IntentTask 非 skipped：mock-only/unsupported 的意图不进入后续处理。

    统一封装 service.py 中分散的 ``task.task_status != "skipped"`` 过滤，
    让状态语义变更只需修改此函数而非多处调用点。
    """
    return task_status != "skipped"
