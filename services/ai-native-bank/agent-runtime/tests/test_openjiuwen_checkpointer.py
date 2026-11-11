"""Persistent OpenJiuwen Checkpointer lifecycle tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from os import environ
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from openjiuwen.core.single_agent import AgentCard, create_agent_session

from app.config import RuntimeNotReadyError, RuntimeSettings
from app.deepagent_factory import OpenJiuwenRunner
from app.main import create_app
from app.openjiuwen_checkpointer import (
    CheckpointerInitializationError,
    OpenJiuwenCheckpointerManager,
)


CARD = AgentCard(
    id="session-persistence-test-agent",
    name="session-persistence-test-agent",
    description="persistent session test",
)


class OpenJiuwenCheckpointerPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_checkpointer_restores_after_reinstall_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "private" / "sessions.db"
            manager = OpenJiuwenCheckpointerManager(database)
            await manager.install()
            self.assertTrue(database.is_file())

            first = create_agent_session(session_id="user:conv_test", card=CARD)
            await first.pre_run(
                inputs={"runtimeInvocation": {"schemaVersion": "1.0"}}
            )
            first.update_state(
                {"aiNativeBankRuntime": {"envelopeRevision": 7}}
            )
            await first.post_run()

            restarted_manager = OpenJiuwenCheckpointerManager(database)
            await restarted_manager.install()
            restored = create_agent_session(session_id="user:conv_test", card=CARD)
            await restored.pre_run(
                inputs={"runtimeInvocation": {"schemaVersion": "1.0"}}
            )
            self.assertEqual(
                restored.get_state("aiNativeBankRuntime.envelopeRevision"), 7
            )
            await restored.post_run()

            await restarted_manager.release(["user:conv_test"])
            empty = create_agent_session(session_id="user:conv_test", card=CARD)
            await empty.pre_run(
                inputs={"runtimeInvocation": {"schemaVersion": "1.0"}}
            )
            self.assertIsNone(
                empty.get_state("aiNativeBankRuntime.envelopeRevision")
            )
            await empty.post_run()

    async def test_initialization_error_does_not_expose_database_path(self):
        manager = OpenJiuwenCheckpointerManager("/private/secrets/checkpoints.db")
        with patch(
            "app.openjiuwen_checkpointer.CheckpointerFactory.create",
            new=AsyncMock(side_effect=RuntimeError("/private/secrets/checkpoints.db")),
        ):
            with self.assertRaisesRegex(
                CheckpointerInitializationError,
                "^checkpointer_initialization_failed$",
            ) as raised:
                await manager.install()
        self.assertNotIn("/private", str(raised.exception))

    async def test_lazy_sqlite_open_failure_is_detected_during_install(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            database.mkdir()
            manager = OpenJiuwenCheckpointerManager(database)

            with self.assertRaisesRegex(
                CheckpointerInitializationError,
                "^checkpointer_initialization_failed$",
            ):
                await manager.install()

    async def test_symlinked_parent_is_rejected_without_writing_outside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            linked_parent = root / "private"
            try:
                linked_parent.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            manager = OpenJiuwenCheckpointerManager(
                linked_parent / "sessions.db"
            )
            with self.assertRaisesRegex(
                CheckpointerInitializationError,
                "^checkpointer_initialization_failed$",
            ):
                await manager.install()

            self.assertFalse((outside / "sessions.db").exists())

    async def test_provider_appended_db_symlink_is_rejected_before_open(self):
        if __import__("os").name == "nt":
            self.skipTest("Linux symlink boundary test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            outside = root / "outside"
            private.mkdir()
            outside.mkdir()
            escaped_database = outside / "escaped.db"
            configured_path = private / "state"
            provider_path = private / "state.db"
            provider_path.symlink_to(escaped_database)

            manager = OpenJiuwenCheckpointerManager(configured_path)
            with self.assertRaisesRegex(
                CheckpointerInitializationError,
                "^checkpointer_initialization_failed$",
            ):
                await manager.install()

            self.assertFalse(escaped_database.exists())

    async def test_unqualified_sqlite_path_is_normalized_to_db_file(self):
        with tempfile.TemporaryDirectory() as directory:
            configured_path = Path(directory) / "private" / "state"
            manager = OpenJiuwenCheckpointerManager(configured_path)

            await manager.install()

            self.assertFalse(configured_path.exists())
            self.assertTrue(configured_path.with_name("state.db").is_file())

    async def test_private_directory_database_and_sidecars_have_owner_only_modes(self):
        if __import__("os").name == "nt":
            self.skipTest("POSIX permission bits are not available on Windows")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "private" / "sessions.db"
            manager = OpenJiuwenCheckpointerManager(database)

            await manager.install()

            self.assertEqual(database.parent.stat().st_mode & 0o777, 0o700)
            persisted_files = list(database.parent.glob("sessions.db*"))
            self.assertTrue(persisted_files)
            for path in persisted_files:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class RuntimeCheckpointerConfigurationTests(unittest.TestCase):
    def test_checkpointer_path_has_private_volume_default_and_environment_override(self):
        with patch.dict(environ, {}, clear=True):
            defaults = RuntimeSettings.from_env()
        with patch.dict(
            environ,
            {"AI_BANK_AGENT_CHECKPOINTER_PATH": "/private/runtime/openjiuwen.db"},
            clear=True,
        ):
            configured = RuntimeSettings.from_env()

        self.assertEqual(
            defaults.checkpointer_path,
            "/app/agent-workspaces/checkpoints/openjiuwen.db",
        )
        self.assertEqual(
            configured.checkpointer_path,
            "/private/runtime/openjiuwen.db",
        )


class OpenJiuwenRunnerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_run_is_called_when_agent_invoke_fails(self):
        session = Mock()
        session.pre_run = AsyncMock()
        session.update_state = Mock()
        session.post_run = AsyncMock()
        agent = Mock(card=CARD)
        agent.invoke = AsyncMock(side_effect=ValueError("model failure"))
        collector = Mock()
        collector.reset = Mock()
        runner = OpenJiuwenRunner(
            agent,
            Mock(root_path="/tmp/runner"),
            collector,
            OpenJiuwenCheckpointerManager("/tmp/runner-unit.db"),
        )

        with patch("app.deepagent_factory.create_agent_session", return_value=session):
            with self.assertRaisesRegex(ValueError, "model failure"):
                await runner.run(
                    {"schemaVersion": "1.0"},
                    session_id="user:conv_runner",
                )

        session.pre_run.assert_awaited_once()
        session.update_state.assert_called_once()
        session.post_run.assert_awaited_once_with()

    async def test_post_run_failure_is_mapped_to_safe_runtime_error(self):
        session = Mock()
        session.pre_run = AsyncMock()
        session.update_state = Mock()
        session.post_run = AsyncMock(
            side_effect=RuntimeError("/private/runtime/openjiuwen.db")
        )
        agent = Mock(card=CARD)
        agent.invoke = AsyncMock()
        collector = Mock()
        collector.reset = Mock()
        collector.take.return_value = {"ok": True}
        runner = OpenJiuwenRunner(
            agent,
            Mock(root_path="/tmp/runner"),
            collector,
            OpenJiuwenCheckpointerManager("/tmp/runner-unit.db"),
        )

        with patch("app.deepagent_factory.create_agent_session", return_value=session):
            with self.assertRaisesRegex(
                RuntimeNotReadyError,
                "^agent_session_persistence_failed$",
            ) as raised:
                await runner.run(
                    {"schemaVersion": "1.0"},
                    session_id="user:conv_runner",
                )
        self.assertNotIn("/private", str(raised.exception))

    async def test_invoke_error_remains_primary_when_post_run_also_fails(self):
        primary = ValueError("primary invoke failure")
        session = Mock(
            pre_run=AsyncMock(),
            update_state=Mock(),
            post_run=AsyncMock(
                side_effect=RuntimeError("/private/checkpoints.db")
            ),
        )
        agent = Mock(card=CARD, invoke=AsyncMock(side_effect=primary))
        collector = Mock(reset=Mock())
        runner = OpenJiuwenRunner(
            agent,
            Mock(root_path="/tmp/runner"),
            collector,
            OpenJiuwenCheckpointerManager("/tmp/runner-unit.db"),
        )

        with patch("app.deepagent_factory.create_agent_session", return_value=session):
            with self.assertLogs(
                "ai_native_bank.agent_runtime.session",
                level="WARNING",
            ) as logged:
                with self.assertRaises(ValueError) as raised:
                    await runner.run({}, session_id="user:conv_primary")

        self.assertIs(raised.exception, primary)
        self.assertNotIn("/private", " ".join(logged.output))

    async def test_cancellation_remains_primary_when_post_run_also_fails(self):
        primary = asyncio.CancelledError()
        session = Mock(
            pre_run=AsyncMock(),
            update_state=Mock(),
            post_run=AsyncMock(
                side_effect=RuntimeError("/private/checkpoints.db")
            ),
        )
        agent = Mock(card=CARD, invoke=AsyncMock(side_effect=primary))
        collector = Mock(reset=Mock())
        runner = OpenJiuwenRunner(
            agent,
            Mock(root_path="/tmp/runner"),
            collector,
            OpenJiuwenCheckpointerManager("/tmp/runner-unit.db"),
        )

        with patch("app.deepagent_factory.create_agent_session", return_value=session):
            with self.assertLogs(
                "ai_native_bank.agent_runtime.session",
                level="WARNING",
            ):
                with self.assertRaises(asyncio.CancelledError) as raised:
                    await runner.run({}, session_id="user:conv_cancelled")

        self.assertIs(raised.exception, primary)

    async def test_collector_error_remains_primary_when_post_run_also_fails(self):
        primary = LookupError("primary collector failure")
        session = Mock(
            pre_run=AsyncMock(),
            update_state=Mock(),
            post_run=AsyncMock(
                side_effect=RuntimeError("/private/checkpoints.db")
            ),
        )
        agent = Mock(card=CARD, invoke=AsyncMock())
        collector = Mock(reset=Mock(), take=Mock(side_effect=primary))
        runner = OpenJiuwenRunner(
            agent,
            Mock(root_path="/tmp/runner"),
            collector,
            OpenJiuwenCheckpointerManager("/tmp/runner-unit.db"),
        )

        with patch("app.deepagent_factory.create_agent_session", return_value=session):
            with self.assertLogs(
                "ai_native_bank.agent_runtime.session",
                level="WARNING",
            ):
                with self.assertRaises(LookupError) as raised:
                    await runner.run({}, session_id="user:conv_collector")

        self.assertIs(raised.exception, primary)


class OpenJiuwenSessionLifecycleCoordinationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def _persist_revision(self, session_id: str, revision: int) -> None:
        session = create_agent_session(session_id=session_id, card=CARD)
        await session.pre_run(inputs={"runtimeInvocation": {}})
        session.update_state(
            {"aiNativeBankRuntime": {"envelopeRevision": revision}}
        )
        await session.post_run()

    async def test_release_waits_for_real_run_commit_then_deletes_state(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = OpenJiuwenCheckpointerManager(
                Path(directory) / "sessions.db"
            )
            await manager.install()
            invoke_entered = asyncio.Event()
            allow_invoke_to_finish = asyncio.Event()

            async def invoke(_payload, *, session):
                invoke_entered.set()
                await allow_invoke_to_finish.wait()

            agent = Mock(card=CARD, invoke=AsyncMock(side_effect=invoke))
            collector = Mock(reset=Mock(), take=Mock(return_value={"ok": True}))
            runner = OpenJiuwenRunner(
                agent,
                Mock(root_path=directory),
                collector,
                manager,
            )
            session_id = "user:conv_release_race"

            run_task = asyncio.create_task(
                runner.run(
                    {"schemaVersion": "1.0", "envelopeRevision": 9},
                    session_id=session_id,
                )
            )
            await invoke_entered.wait()
            release_task = asyncio.create_task(manager.release([session_id]))
            await asyncio.sleep(0)
            self.assertFalse(release_task.done())

            allow_invoke_to_finish.set()
            await run_task
            await release_task

            reopened = create_agent_session(session_id=session_id, card=CARD)
            await reopened.pre_run(inputs={"runtimeInvocation": {}})
            self.assertIsNone(
                reopened.get_state("aiNativeBankRuntime.envelopeRevision")
            )
            await reopened.post_run()

    async def test_successful_release_tombstones_session_across_force_reinstall(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = OpenJiuwenCheckpointerManager(
                Path(directory) / "sessions.db"
            )
            await manager.install()
            session_id = "user:conv_released_first"
            await manager.release([session_id])
            await manager.install(force_new_instance=True)
            agent = Mock(card=CARD, invoke=AsyncMock())
            runner = OpenJiuwenRunner(
                agent,
                Mock(root_path=directory),
                Mock(reset=Mock(), take=Mock()),
                manager,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "^agent_session_released$",
            ):
                await runner.run({}, session_id=session_id)
            agent.invoke.assert_not_awaited()

    async def test_failed_release_does_not_tombstone_session(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = OpenJiuwenCheckpointerManager(
                Path(directory) / "sessions.db"
            )
            await manager.install()
            original_release = manager._checkpointer.release

            async def fail_release(session_id):
                if session_id == "user:conv_retryable":
                    raise RuntimeError("storage unavailable")
                await original_release(session_id)

            manager._checkpointer.release = fail_release
            with self.assertRaisesRegex(
                RuntimeError,
                "^agent_session_release_failed$",
            ):
                await manager.release(["user:conv_retryable"])

            async with manager.session_run_guard("user:conv_retryable"):
                pass

    async def test_partial_batch_release_tombstones_each_deleted_session_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = OpenJiuwenCheckpointerManager(
                Path(directory) / "sessions.db"
            )
            await manager.install()
            released_id = "user:conv_batch_a"
            failed_id = "user:conv_batch_b"
            await self._persist_revision(released_id, 11)
            await self._persist_revision(failed_id, 12)
            original_release = manager._checkpointer.release
            failed_release_entered = asyncio.Event()
            allow_failed_release = asyncio.Event()
            fail_once = True

            async def partial_release(session_id):
                nonlocal fail_once
                if session_id == failed_id and fail_once:
                    failed_release_entered.set()
                    await allow_failed_release.wait()
                    fail_once = False
                    raise RuntimeError("storage unavailable")
                await original_release(session_id)

            manager._checkpointer.release = partial_release
            release_task = asyncio.create_task(
                manager.release([failed_id, released_id])
            )
            await failed_release_entered.wait()

            async def wait_for_released_guard():
                async with manager.session_run_guard(released_id):
                    return "admitted"

            waiting_run = asyncio.create_task(
                wait_for_released_guard()
            )
            await asyncio.sleep(0)
            self.assertFalse(waiting_run.done())

            allow_failed_release.set()
            with self.assertRaisesRegex(
                RuntimeError,
                "^agent_session_release_failed$",
            ):
                await release_task
            with self.assertRaisesRegex(
                RuntimeError,
                "^agent_session_released$",
            ):
                await waiting_run

            reopened_a = create_agent_session(session_id=released_id, card=CARD)
            await reopened_a.pre_run(inputs={"runtimeInvocation": {}})
            self.assertIsNone(
                reopened_a.get_state("aiNativeBankRuntime.envelopeRevision")
            )

            async with manager.session_run_guard(failed_id):
                reopened_b = create_agent_session(session_id=failed_id, card=CARD)
                await reopened_b.pre_run(inputs={"runtimeInvocation": {}})
                self.assertEqual(
                    reopened_b.get_state(
                        "aiNativeBankRuntime.envelopeRevision"
                    ),
                    12,
                )

            await manager.release([failed_id, released_id])
            for session_id in (released_id, failed_id):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "^agent_session_released$",
                ):
                    async with manager.session_run_guard(session_id):
                        self.fail("released session guard unexpectedly admitted")

    async def test_hardening_failure_keeps_successfully_deleted_session_tombstoned(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = OpenJiuwenCheckpointerManager(
                Path(directory) / "sessions.db"
            )
            await manager.install()
            session_id = "user:conv_harden_failure"
            await self._persist_revision(session_id, 13)

            with patch.object(
                manager,
                "_harden_storage_permissions",
                side_effect=RuntimeError("permission hardening failed"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "^agent_session_release_failed$",
                ):
                    await manager.release([session_id])

            with self.assertRaisesRegex(
                RuntimeError,
                "^agent_session_released$",
            ):
                async with manager.session_run_guard(session_id):
                    self.fail("deleted session guard unexpectedly admitted")

            reopened = create_agent_session(session_id=session_id, card=CARD)
            await reopened.pre_run(inputs={"runtimeInvocation": {}})
            self.assertIsNone(
                reopened.get_state("aiNativeBankRuntime.envelopeRevision")
            )


class RecordingCheckpointerManager:
    def __init__(self, *, install_error=None, release_error=None):
        self.install_error = install_error
        self.release_error = release_error
        self.installed = False
        self.released: list[list[str]] = []

    async def install(self):
        if self.install_error:
            raise self.install_error
        self.installed = True

    async def release(self, session_ids):
        if self.release_error:
            raise self.release_error
        self.released.append(list(session_ids))


def runtime_settings(**overrides):
    values = {
        "runtime_token": "runtime-test-token",
        "model_provider": "openai",
        "model_name": "mock-model",
        "model_base_url": "http://127.0.0.1:9/v1",
        "model_api_key": "model-test-key",
        "checkpointer_path": "/private/runtime/openjiuwen.db",
    }
    values.update(overrides)
    return RuntimeSettings(**values)


class AgentSessionReleaseHttpTests(unittest.TestCase):
    def test_release_requires_bearer_exact_body_and_safe_session_ids(self):
        manager = RecordingCheckpointerManager()
        client = TestClient(
            create_app(
                runtime_settings(),
                service=Mock(),
                checkpointer_manager=manager,
            )
        )
        path = "/v1/agent-sessions/release"

        denied = client.post(path, json={"sessionIds": ["user:conv_001"]})
        accepted = client.post(
            path,
            json={
                "sessionIds": [
                    "user:conv_001",
                    "business:cust_demo_001:bwf_001",
                ]
            },
            headers={"authorization": "Bearer runtime-test-token"},
        )
        unknown = client.post(
            path,
            json={"sessionIds": ["user:conv_001"], "databasePath": "/private"},
            headers={"authorization": "Bearer runtime-test-token"},
        )
        unsafe = client.post(
            path,
            json={"sessionIds": ["user:../../private"]},
            headers={"authorization": "Bearer runtime-test-token"},
        )
        too_many = client.post(
            path,
            json={"sessionIds": [f"user:conv_{index}" for index in range(33)]},
            headers={"authorization": "Bearer runtime-test-token"},
        )
        empty = client.post(
            path,
            json={"sessionIds": []},
            headers={"authorization": "Bearer runtime-test-token"},
        )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json(), {"released": 2})
        self.assertEqual(manager.released, [[
            "user:conv_001",
            "business:cust_demo_001:bwf_001",
        ]])
        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(unsafe.status_code, 422)
        self.assertEqual(too_many.status_code, 422)
        self.assertEqual(empty.status_code, 422)

    def test_release_failure_is_safe(self):
        manager = RecordingCheckpointerManager(
            release_error=RuntimeError("/private/runtime/openjiuwen.db")
        )
        client = TestClient(
            create_app(
                runtime_settings(),
                service=Mock(),
                checkpointer_manager=manager,
            )
        )

        response = client.post(
            "/v1/agent-sessions/release",
            json={"sessionIds": ["user:conv_001"]},
            headers={"authorization": "Bearer runtime-test-token"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "agent_session_release_failed")
        self.assertNotIn("/private", response.text)

    def test_lifespan_initialization_failure_is_not_ready_and_redacts_path(self):
        manager = RecordingCheckpointerManager(
            install_error=RuntimeError("/private/runtime/openjiuwen.db")
        )
        app = create_app(
            runtime_settings(),
            service=Mock(),
            checkpointer_manager=manager,
        )

        with TestClient(app) as client:
            response = client.get("/health")

        self.assertFalse(response.json()["ready"])
        self.assertEqual(
            response.json()["runtimeState"],
            "checkpointer_initialization_failed",
        )
        self.assertNotIn("/private", response.text)

    def test_ready_health_reports_persistence_without_path(self):
        manager = RecordingCheckpointerManager()
        app = create_app(
            runtime_settings(),
            service=Mock(),
            checkpointer_manager=manager,
        )

        with TestClient(app) as client:
            response = client.get("/health")

        self.assertTrue(manager.installed)
        self.assertTrue(response.json()["ready"])
        self.assertEqual(response.json()["checkpointerProvider"], "persistence")
        self.assertNotIn("checkpointerPath", response.json())
        self.assertNotIn("/private", response.text)

    def test_unknown_runtime_initialization_error_is_mapped_to_safe_code(self):
        manager = RecordingCheckpointerManager()
        app = create_app(
            runtime_settings(),
            checkpointer_manager=manager,
        )
        with patch(
            "app.main._initialize_runtime_service",
            side_effect=RuntimeNotReadyError(
                "/private/runtime.db?token=secret-value"
            ),
        ):
            with TestClient(app) as client:
                response = client.get("/health")

        self.assertFalse(response.json()["ready"])
        self.assertEqual(
            response.json()["runtimeState"],
            "framework_initialization_failed",
        )
        self.assertNotIn("/private", response.text)
        self.assertNotIn("secret-value", response.text)


if __name__ == "__main__":
    unittest.main()
