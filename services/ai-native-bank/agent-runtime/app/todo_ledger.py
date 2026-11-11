"""Durable, privacy-safe projection of an OpenJiuwen Todo workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.business_loop import (
    TodoSnapshotItem,
    load_openjiuwen_todo_snapshot,
    validate_authoritative_todo_snapshot,
)
from app.contracts import MAX_AGENT_SESSION_ID_LENGTH


_SAFE_SESSION_ID = re.compile(
    rf"^[A-Za-z0-9_.:-]{{1,{MAX_AGENT_SESSION_ID_LENGTH}}}$"
)


class TodoLedgerConflict(ValueError):
    """Raised when persisted and workspace Todo projections have drifted."""


@dataclass(frozen=True)
class TodoLedgerSnapshot:
    revision: int
    digest: str
    items: tuple[TodoSnapshotItem, ...]


class TodoLedger:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _target(self, session_id: str) -> Path:
        if not _SAFE_SESSION_ID.fullmatch(session_id) or ".." in session_id:
            raise ValueError("business session id is invalid")
        target = (self.root / "todo" / session_id / "todo.json").resolve()
        if self.root not in target.parents:
            raise ValueError("todo path escapes workspace")
        return target

    @staticmethod
    def digest(items: tuple[TodoSnapshotItem, ...]) -> str:
        payload = [
            {
                "dependsOn": sorted(item.depends_on),
                "id": item.id,
                "status": item.status,
            }
            for item in items
        ]
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def load(self, session_id: str, revision: int = 1) -> TodoLedgerSnapshot:
        if revision < 1:
            raise ValueError("todo revision must be positive")
        items = load_openjiuwen_todo_snapshot(self.root, session_id)
        return TodoLedgerSnapshot(
            revision=revision,
            digest=self.digest(items),
            items=items,
        )

    def restore(
        self,
        session_id: str,
        persisted_revision: int,
        persisted_items: list[dict[str, Any]],
    ) -> TodoLedgerSnapshot:
        if persisted_revision < 1:
            raise ValueError("todo revision must be positive")
        item_tuple = validate_authoritative_todo_snapshot(persisted_items)
        self._write_native(
            session_id,
            [
                {
                    "id": item.id,
                    "content": item.id,
                    "activeForm": item.id,
                    "description": "",
                    "status": item.status,
                    "depends_on": list(item.depends_on),
                    "result_summary": None,
                    "meta_data": None,
                    "selected_model_id": None,
                }
                for item in item_tuple
            ],
        )
        return TodoLedgerSnapshot(
            revision=persisted_revision,
            digest=self.digest(item_tuple),
            items=item_tuple,
        )

    def seed(
        self,
        session_id: str,
        items: list[dict[str, Any]],
    ) -> TodoLedgerSnapshot:
        """以图谱语义初始化全新工作流台账（首跑前，尚无 Node 权威快照）。

        与 restore（投影 Node 快照，契约只含 id/status/dependsOn，content
        只能回退为 id）不同：seed 的 item 携带 title 语义字段写成原生格式。
        裸 id 台账会被模型当作不可读计划而整体重写为自造 id，重新触发
        coverage 冲突——content 必须是面向模型的中文描述。
        """
        item_tuple = validate_authoritative_todo_snapshot([
            {
                "id": item["id"],
                "status": item["status"],
                "dependsOn": item.get("dependsOn") or [],
            }
            for item in items
        ])
        title_by_id = {
            item["id"]: str(item.get("title") or item["id"]) for item in items
        }
        self._write_native(
            session_id,
            [
                {
                    "id": item.id,
                    "content": title_by_id.get(item.id, item.id),
                    "activeForm": "正在" + title_by_id.get(item.id, item.id),
                    "description": "",
                    "status": item.status,
                    "depends_on": list(item.depends_on),
                    "result_summary": None,
                    "meta_data": None,
                    "selected_model_id": None,
                }
                for item in item_tuple
            ],
        )
        return TodoLedgerSnapshot(
            revision=1,
            digest=self.digest(item_tuple),
            items=item_tuple,
        )

    def _write_native(self, session_id: str, native: list[dict[str, Any]]) -> None:
        target = self._target(session_id)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix="todo-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
                json.dump(native, temporary, ensure_ascii=False, separators=(",", ":"))
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def reconcile(
        self,
        session_id: str,
        persisted_revision: int,
        persisted_digest: str,
    ) -> TodoLedgerSnapshot:
        if not re.fullmatch(r"[a-f0-9]{64}", persisted_digest):
            raise TodoLedgerConflict("todo ledger digest conflict")
        snapshot = self.load(session_id, persisted_revision)
        if snapshot.digest != persisted_digest:
            raise TodoLedgerConflict("todo ledger digest conflict")
        return snapshot
