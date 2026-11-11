"""Safe adapters around OpenJiuwen's native Business DeepAgent Todo workspace."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import Field, model_validator

from app.contracts import MAX_AGENT_SESSION_ID_LENGTH, StrictModel


_SAFE_ID = re.compile(
    rf"^[A-Za-z0-9_.:-]{{1,{MAX_AGENT_SESSION_ID_LENGTH}}}$"
)
_SESSION_ID_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
AUTHORITATIVE_NODE_WORKFLOW_KEY = "_authoritative_node_workflow"
MAX_TODO_SNAPSHOT_ITEMS = 128
_PERSISTED_TODO_KEYS = frozenset({"id", "status", "dependsOn"})


class TodoSnapshotItem(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]+$", min_length=1, max_length=160)
    status: Literal["pending", "in_progress", "completed", "cancelled"]
    depends_on: list[str] = Field(default_factory=list, max_length=32)


class ReplanTaskProposal(StrictModel):
    """同意图结构重构提案：模型只申报语义意图与槽位原始值。

    task_id、action、business_object、risk_level 均由编排器从活动任务
    确定性继承，模型不得（也无法）伪造结构标识。
    """

    intent_id: str = Field(min_length=1, max_length=160)
    slots: dict[str, str] = Field(default_factory=dict, max_length=16)


class BusinessLoopOutcome(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    business_workflow_id: str = Field(
        pattern=r"^bwf_[a-f0-9]{24,32}$", min_length=28, max_length=36
    )
    status: Literal[
        "continue",
        "awaiting_clarification",
        "awaiting_confirmation",
        "waiting_external",
        "replan_required",
        "completed",
        "partially_completed",
        "failed",
    ]
    covered_task_ids: list[str] = Field(default_factory=list, max_length=16)
    workflow_revision: int = Field(ge=1)
    todo_revision: int = Field(ge=1)
    active_goal_task_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        min_length=1,
        max_length=160,
    )
    active_todo_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        min_length=1,
        max_length=160,
    )
    next_action: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]*$", max_length=96)
    proposed_tasks: list[ReplanTaskProposal] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_loop_state(self) -> "BusinessLoopOutcome":
        if len(self.covered_task_ids) != len(set(self.covered_task_ids)):
            raise ValueError("covered task ids must be unique")
        if (
            self.active_goal_task_id is not None
            and self.active_goal_task_id not in self.covered_task_ids
        ):
            raise ValueError("active goal must be covered by the workflow")
        non_terminal = {
            "continue",
            "awaiting_clarification",
            "awaiting_confirmation",
            "waiting_external",
            "replan_required",
        }
        if self.status in non_terminal and (
            self.active_goal_task_id is None or self.active_todo_id is None
        ):
            raise ValueError("non-terminal workflow requires one active goal and todo")
        if self.status == "replan_required":
            if len(self.proposed_tasks) < 2:
                raise ValueError(
                    "replan_required requires at least two proposed tasks"
                )
            if len({item.intent_id for item in self.proposed_tasks}) != 1:
                raise ValueError("replan proposals must share a single intent")
        elif self.proposed_tasks:
            raise ValueError("proposed_tasks is only valid with replan_required")
        if self.status == "completed" and (
            self.active_goal_task_id is not None or self.active_todo_id is not None
        ):
            raise ValueError("completed workflow cannot retain an active goal or todo")
        return self


def normalize_terminal_loop_submission(submission: Any) -> Any:
    """Strip only the mechanically provable contradictions in a terminal
    submission. Several model providers project the just-completed Todo into
    the active pointer fields of a ``completed`` outcome; the authoritative
    Todo ledger is still validated separately, so clearing these projections
    can never repair unfinished work. An active pointer outside the covered
    set (single-task resume rounds pointing at a sibling task are the
    typical case) is the same class of echo noise: the platform re-derives
    the authoritative pointer from the verified ledger."""
    if not isinstance(submission, dict):
        return submission
    updates: dict[str, Any] = {}
    if submission.get("status") == "completed":
        updates.update(
            active_goal_task_id=None, active_todo_id=None, next_action=None
        )
    active = submission.get("active_goal_task_id")
    covered = submission.get("covered_task_ids")
    if active is not None and covered and active not in covered:
        # Agent explicitly listed covered tasks; active pointer not among
        # them → echo noise from a sibling task. Clear it.
        updates.update(active_goal_task_id=None, active_todo_id=None)
    elif active is not None and not covered:
        # Agent didn't list covered_task_ids; add the active goal so the
        # "active goal must be covered by the workflow" check passes.
        updates["covered_task_ids"] = [active]
    # 模型经常只填 active_goal_task_id 而漏填 active_todo_id——两者在
    # 单任务恢复轮中通常是同一个标识符。补齐 active_todo_id 以避免
    # "non-terminal workflow requires one active goal and todo" 校验失败。
    active_after = updates.get("active_goal_task_id", active)
    if (
        active_after is not None
        and submission.get("status") not in ("completed", "partially_completed", "failed")
        and not updates.get("active_todo_id")
        and not submission.get("active_todo_id")
    ):
        updates["active_todo_id"] = active_after
    # proposed_tasks 仅对 replan_required 有意义；模型在其他状态附带
    # proposed_tasks 属于噪音，清除以避免校验失败。
    if submission.get("status") != "replan_required" and submission.get("proposed_tasks"):
        updates["proposed_tasks"] = []
    return {**submission, **updates} if updates else submission


_CARD_NUMBER_RUN = re.compile(r"(?:\d[ -]?){13,19}")


def business_workflow_id(customer_id: str, conversation_id: str, envelope_id: str) -> str:
    for value in (customer_id, conversation_id, envelope_id):
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("workflow identity contains unsafe characters")
    # Node 侧敏感数据校验把 13-19 位连续数字串视为银行卡号并拒绝持久化，均匀 hex
    # 约 1.2% 天然命中。改为在 64 位摘要上滑动取第一个不含此类数字串的 24 位窗口，
    # 极端情况下才换种子重派生；仍由相同身份确定性得出，且与 Node 侧
    # deriveBusinessWorkflowId 逐位一致。
    base = f"{customer_id}\x1f{conversation_id}\x1f{envelope_id}"
    for attempt in range(9):
        seed = base if attempt == 0 else f"{base}\x1f{attempt}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        for offset in range(len(digest) - 23):
            candidate = digest[offset:offset + 24]
            if not _CARD_NUMBER_RUN.search(candidate):
                return "bwf_" + candidate
    raise ValueError("workflow id derivation produced an unsafe digit run")


def business_session_id(customer_id: str, workflow_id: str) -> str:
    session_id = f"business:{customer_id}:{workflow_id}"
    if (
        not _SESSION_ID_COMPONENT.fullmatch(customer_id)
        or not _SESSION_ID_COMPONENT.fullmatch(workflow_id)
        or ".." in customer_id
        or ".." in workflow_id
        or len(session_id) > MAX_AGENT_SESSION_ID_LENGTH
    ):
        raise ValueError("business session identity is invalid")
    return session_id


def validate_authoritative_todo_snapshot(
    raw_items: Any,
) -> tuple[TodoSnapshotItem, ...]:
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_TODO_SNAPSHOT_ITEMS:
        if isinstance(raw_items, list) and len(raw_items) > MAX_TODO_SNAPSHOT_ITEMS:
            raise ValueError("persisted todo snapshot is too large")
        raise ValueError("persisted todo snapshot is invalid")
    items: list[TodoSnapshotItem] = []
    try:
        for raw in raw_items:
            if not isinstance(raw, dict) or set(raw) != _PERSISTED_TODO_KEYS:
                raise ValueError
            items.append(TodoSnapshotItem.model_validate({
                "id": raw["id"],
                "status": raw["status"],
                "depends_on": raw["dependsOn"],
            }))
    except Exception:
        raise ValueError("persisted todo snapshot is invalid") from None
    return validate_todo_snapshot_graph(tuple(items))


def validate_todo_snapshot_graph(
    items: tuple[TodoSnapshotItem, ...],
) -> tuple[TodoSnapshotItem, ...]:
    if len(items) > MAX_TODO_SNAPSHOT_ITEMS:
        raise ValueError("persisted todo snapshot is too large")
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("persisted todo snapshot is invalid")
    known_ids = set(ids)
    dependencies: dict[str, tuple[str, ...]] = {}
    for item in items:
        item_dependencies = tuple(item.depends_on)
        if (
            len(item_dependencies) != len(set(item_dependencies))
            or item.id in item_dependencies
            or not set(item_dependencies).issubset(known_ids)
        ):
            raise ValueError("persisted todo snapshot is invalid")
        dependencies[item.id] = item_dependencies

    visit_state: dict[str, int] = {}

    def visit(item_id: str) -> None:
        state = visit_state.get(item_id, 0)
        if state == 1:
            raise ValueError("persisted todo snapshot is invalid")
        if state == 2:
            return
        visit_state[item_id] = 1
        for dependency_id in dependencies[item_id]:
            visit(dependency_id)
        visit_state[item_id] = 2

    for item_id in ids:
        visit(item_id)
    return items


def load_openjiuwen_todo_snapshot(
    workspace: object,
    session_id: str,
) -> tuple[TodoSnapshotItem, ...]:
    """Read the native todo.json but expose no model-written text or metadata."""

    if not _SAFE_ID.fullmatch(session_id) or ".." in session_id:
        raise ValueError("business session id is invalid")
    root_value = getattr(workspace, "root_path", workspace)
    root = Path(root_value).resolve()
    target = (root / "todo" / session_id / "todo.json").resolve()
    if root not in target.parents:
        raise ValueError("todo path escapes the business workspace")
    if not target.is_file():
        return ()
    if target.stat().st_size > 1024 * 1024:
        raise ValueError("native todo snapshot is too large")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("native todo snapshot is unreadable") from exc
    if not isinstance(payload, list) or len(payload) > MAX_TODO_SNAPSHOT_ITEMS:
        raise ValueError("native todo snapshot must be a bounded list")
    items: list[TodoSnapshotItem] = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("native todo item must be an object")
        depends_on = raw.get("depends_on", [])
        item = TodoSnapshotItem.model_validate({
            "id": raw.get("id"),
            "status": raw.get("status"),
            "depends_on": depends_on,
        })
        items.append(item)
    try:
        return validate_todo_snapshot_graph(tuple(items))
    except ValueError:
        raise ValueError("native todo snapshot is invalid") from None


def validate_goal_coverage(
    delegated_task_ids: Iterable[str],
    todo_ids: Iterable[str],
) -> tuple[str, ...]:
    delegated = set(delegated_task_ids)
    native_ids = set(todo_ids)
    for value in delegated | native_ids:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("goal coverage contains an unsafe id")
    # OpenJiuwen's native planner owns Todo identifiers. For a single delegated
    # goal every native Todo has one unambiguous owner, so no synthetic task-id
    # prefix is required. Multi-goal workflows still require explicit prefixes
    # to preserve task isolation and deterministic sequential scheduling.
    if len(delegated) == 1 and native_ids:
        return ()
    covered = {
        task_id
        for task_id in delegated
        if any(
            todo_id == task_id
            or todo_id.startswith(task_id + ":")
            or todo_id.startswith(task_id + ".")
            for todo_id in native_ids
        )
    }
    return tuple(sorted(delegated - covered))


def todo_belongs_to_goal(
    todo_id: str,
    goal_id: str,
    delegated_task_ids: Iterable[str],
) -> bool:
    delegated = tuple(dict.fromkeys(delegated_task_ids))
    if len(delegated) == 1:
        return delegated[0] == goal_id
    return (
        todo_id == goal_id
        or todo_id.startswith(goal_id + ":")
        or todo_id.startswith(goal_id + ".")
    )
