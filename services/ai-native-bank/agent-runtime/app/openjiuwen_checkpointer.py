"""Fail-closed installation of OpenJiuwen's persistent checkpointer."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Iterable

logger = logging.getLogger("ai_native_bank.agent_runtime.session")


class CheckpointerInitializationError(RuntimeError):
    """Raised without exposing persistence configuration details."""


class AgentSessionReleaseError(RuntimeError):
    """Raised when persisted agent-session state cannot be released."""


class AgentSessionReleasedError(RuntimeError):
    """Raised when a run tries to reuse a successfully released identity."""


class OpenJiuwenCheckpointerManager:
    """Own the native persistent checkpointer used by all AgentSession objects."""

    def __init__(self, database_path: str | Path) -> None:
        raw_path = str(database_path).strip()
        if raw_path:
            provider_path = os.path.abspath(raw_path)
            if not provider_path.endswith(".db"):
                provider_path += ".db"
            self._database_path = Path(provider_path)
        else:
            self._database_path = None
        self._checkpointer = None
        self._install_lock = asyncio.Lock()
        self._session_lock_index = asyncio.Lock()
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._released_session_ids: set[str] = set()

    async def install(self, *, force_new_instance: bool = False) -> None:
        """Install a SQLite-backed native checkpointer as the process default."""

        async with self._install_lock:
            if self._checkpointer is not None and not force_new_instance:
                return

            try:
                from openjiuwen.core.session.checkpointer import CheckpointerFactory
                from openjiuwen.core.session.checkpointer.checkpointer import (
                    CheckpointerConfig,
                )

                if self._database_path is None:
                    raise ValueError("checkpointer path is empty")
                self._reject_link_components()
                self._database_path.parent.mkdir(parents=True, exist_ok=True)
                self._reject_link_components()
                if os.name != "nt":
                    self._database_path.parent.chmod(0o700)
                config = CheckpointerConfig(
                    type="persistence",
                    conf={
                        "db_type": "sqlite",
                        "db_path": str(self._database_path),
                        "db_timeout": 30,
                        "db_enable_wal": True,
                    },
                )
                checkpointer = await CheckpointerFactory.create(config)
                # The native SQLite provider connects lazily.  A bounded release
                # probe creates its schema and proves the configured store is
                # usable before the runtime advertises readiness.
                await checkpointer.release("__ai_native_bank_startup_probe__")
                self._harden_storage_permissions()
                CheckpointerFactory.set_default_checkpointer(checkpointer)
            except Exception as exc:
                raise CheckpointerInitializationError(
                    "checkpointer_initialization_failed"
                ) from exc

            self._checkpointer = checkpointer

    @asynccontextmanager
    async def session_run_guard(self, session_id: str) -> AsyncIterator[None]:
        """Serialize one complete AgentSession lifecycle against release."""

        lock = await self._session_lock(session_id)
        await lock.acquire()
        try:
            if session_id in self._released_session_ids:
                raise AgentSessionReleasedError("agent_session_released")
            yield
        finally:
            self._harden_storage_permissions_best_effort()
            lock.release()

    async def release(self, session_ids: Iterable[str]) -> None:
        """Delete persisted state for the supplied public-safe session IDs."""

        checkpointer = self._checkpointer
        if checkpointer is None:
            raise AgentSessionReleaseError("agent_session_release_failed")

        ordered_session_ids = sorted(set(session_ids))
        locks: list[asyncio.Lock] = []
        try:
            for session_id in ordered_session_ids:
                lock = await self._session_lock(session_id)
                await lock.acquire()
                locks.append(lock)
            for session_id in ordered_session_ids:
                await checkpointer.release(session_id)
                self._released_session_ids.add(session_id)
            self._harden_storage_permissions()
        except Exception as exc:
            raise AgentSessionReleaseError("agent_session_release_failed") from exc
        finally:
            for lock in reversed(locks):
                lock.release()

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._session_lock_index:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _reject_link_components(self) -> None:
        database_path = self._database_path
        if database_path is None:
            raise ValueError("checkpointer path is empty")
        current = Path(database_path.anchor)
        parts = database_path.parts[1:] if database_path.anchor else database_path.parts
        for part in parts:
            current /= part
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                continue
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if stat.S_ISLNK(metadata.st_mode) or (
                reparse_flag and attributes & reparse_flag
            ):
                raise ValueError("checkpointer path contains a link")

    def _harden_storage_permissions(self) -> None:
        if os.name == "nt" or self._database_path is None:
            return
        if not self._database_path.exists():
            return
        self._database_path.parent.chmod(0o700)
        prefix = self._database_path.name
        for path in self._database_path.parent.iterdir():
            if path.name != prefix and not path.name.startswith(prefix + "-"):
                continue
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("checkpointer storage contains a link")
            if stat.S_ISREG(metadata.st_mode):
                path.chmod(0o600)

    def _harden_storage_permissions_best_effort(self) -> None:
        try:
            self._harden_storage_permissions()
        except Exception:
            logger.warning("OpenJiuwen checkpointer permission hardening failed")
