"""Deterministic lifecycle helpers for revisioned User DeepAgent envelopes."""

from __future__ import annotations

from typing import Any


def next_envelope_revision(previous: int | None) -> int:
    if previous is None:
        return 1
    if not isinstance(previous, int) or isinstance(previous, bool) or previous < 1:
        raise ValueError("envelope revision is invalid")
    return previous + 1


def merge_envelope_revision(
    *,
    previous_tasks: dict[str, dict[str, Any]],
    completed_task_ids: set[str],
    revised_tasks: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    for task_id in completed_task_ids:
        if (
            task_id in revised_tasks
            and revised_tasks[task_id] != previous_tasks.get(task_id)
        ):
            raise ValueError("completed intent task is immutable")
    merged = dict(previous_tasks)
    merged.update(revised_tasks)
    return merged
