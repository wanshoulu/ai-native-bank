import unittest
import asyncio
import time
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from app.safety_gate import (
    ClarificationPermit,
    ExecutionPermit,
    PermitValidationError,
    RailDecision,
    RunAttempt,
    RunAttemptStateError,
    SafetyGatedRunCoordinator,
)


def decision(disposition="allow", **changes):
    payload = {
        "railId": "finance-text-safety",
        "decision": disposition,
        "reasonCode": None,
        "policyVersion": "2026.09",
        "responseKey": None,
        "durationMs": 8,
    }
    if disposition == "review":
        payload.update(
            reasonCode="insufficient_safety_context",
            responseKey=None,
        )
    elif disposition == "block":
        payload.update(
            reasonCode="unsafe_content",
            responseKey="security.block.default",
        )
    elif disposition == "unavailable":
        payload.update(
            reasonCode="security_detector_unavailable",
            responseKey="security.unavailable.default",
        )
    payload.update(changes)
    return RailDecision.model_validate(payload)


class RailDecisionContractTests(unittest.TestCase):
    def test_four_states_have_strict_stable_shapes(self):
        self.assertEqual(decision().decision, "allow")
        self.assertEqual(decision("review").reason_code, "insufficient_safety_context")
        self.assertEqual(decision("block").response_key, "security.block.default")
        self.assertEqual(decision("unavailable").decision, "unavailable")

        invalid = [
            {"decision": "error"},
            {"decision": "allow", "reasonCode": "unsafe_content"},
            {"decision": "review", "reasonCode": None},
            {"decision": "block", "responseKey": None},
            {"unexpected": True},
        ]
        base = decision().model_dump(by_alias=True)
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                RailDecision.model_validate({**base, **changes})

    def test_decision_never_accepts_detector_evidence_or_raw_text(self):
        base = decision("block").model_dump(by_alias=True)
        for field, value in (
            ("message", "完整用户输入"),
            ("confidence", 0.99),
            ("evidence", ["detector prompt"]),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                RailDecision.model_validate({**base, field: value})


class PermitContractTests(unittest.TestCase):
    def test_allow_issues_execution_permit_bound_to_request_and_revision(self):
        permit = ExecutionPermit.issue(
            request_id="req_1",
            conversation_id="conv_1",
            session_revision=3,
            rail_decision=decision("allow"),
            ttl_seconds=30,
        )

        self.assertTrue(permit.allows("user_agent.commit"))
        self.assertTrue(permit.allows("a2a.delegate"))
        permit.validate_binding(
            request_id="req_1",
            conversation_id="conv_1",
            session_revision=3,
            required_scope="a2a.delegate",
        )

        mismatches = [
            {"request_id": "req_2"},
            {"conversation_id": "conv_2"},
            {"session_revision": 4},
            {"required_scope": "user_agent.clarify"},
        ]
        defaults = {
            "request_id": "req_1",
            "conversation_id": "conv_1",
            "session_revision": 3,
            "required_scope": "a2a.delegate",
        }
        for changes in mismatches:
            with self.subTest(changes=changes), self.assertRaises(PermitValidationError):
                permit.validate_binding(**{**defaults, **changes})

    def test_review_only_issues_clarification_permit(self):
        permit = ClarificationPermit.issue(
            request_id="req_1",
            conversation_id="conv_1",
            session_revision=3,
            rail_decision=decision("review"),
            ttl_seconds=30,
        )

        self.assertTrue(permit.allows("user_agent.clarify"))
        self.assertFalse(permit.allows("a2a.delegate"))
        with self.assertRaises(PermitValidationError):
            permit.validate_binding(
                request_id="req_1",
                conversation_id="conv_1",
                session_revision=3,
                required_scope="a2a.delegate",
            )

    def test_wrong_decision_and_expired_permit_are_rejected(self):
        with self.assertRaises(PermitValidationError):
            ExecutionPermit.issue(
                request_id="req_1",
                conversation_id="conv_1",
                session_revision=3,
                rail_decision=decision("review"),
                ttl_seconds=30,
            )

        expired = ExecutionPermit(
            permitId="permit_expired01",
            permitType="execution",
            requestId="req_1",
            conversationId="conv_1",
            sessionRevision=3,
            policyVersion="2026.09",
            expiresAt=datetime.now(timezone.utc) - timedelta(seconds=1),
            scopes=frozenset({"user_agent.commit", "a2a.delegate"}),
        )
        with self.assertRaises(PermitValidationError):
            expired.validate_binding(
                request_id="req_1",
                conversation_id="conv_1",
                session_revision=3,
                required_scope="a2a.delegate",
            )


class RunAttemptStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_attempt_commits_or_rolls_back_exactly_once(self):
        calls = []

        async def commit(value, permit):
            calls.append(("commit", value))
            self.assertTrue(permit.allows("user_agent.commit"))
            return {"committed": value}

        async def rollback():
            calls.append(("rollback", None))

        attempt = RunAttempt(
            attempt_id="attempt_1",
            commit_callback=commit,
            rollback_callback=rollback,
        )
        attempt.buffer({"intent": "balance"})
        permit = ExecutionPermit.issue(
            request_id="req_attempt",
            conversation_id="conv_attempt",
            session_revision=1,
            rail_decision=decision("allow"),
            ttl_seconds=30,
        )
        first = await attempt.commit(decision("allow"), permit)
        second = await attempt.commit(decision("allow"), permit)

        self.assertEqual(first, {"committed": {"intent": "balance"}})
        self.assertIs(first, second)
        self.assertEqual(calls, [("commit", {"intent": "balance"})])
        with self.assertRaises(RunAttemptStateError):
            await attempt.rollback()

    async def test_rollback_is_idempotent_and_discards_late_result(self):
        calls = []

        async def rollback():
            calls.append("rollback")

        attempt = RunAttempt(
            attempt_id="attempt_2",
            commit_callback=lambda _value, _permit: None,
            rollback_callback=rollback,
        )
        await attempt.rollback()
        await attempt.rollback()

        self.assertEqual(calls, ["rollback"])
        with self.assertRaises(RunAttemptStateError):
            attempt.buffer({"late": True})
        with self.assertRaises(RunAttemptStateError):
            permit = ExecutionPermit.issue(
                request_id="req_late",
                conversation_id="conv_late",
                session_revision=1,
                rail_decision=decision("allow"),
                ttl_seconds=30,
            )
            await attempt.commit(decision("allow"), permit)


class FakeRail:
    def __init__(self, result, *, delay=0.0):
        self.result = result
        self.delay = delay
        self.started = asyncio.Event()

    async def evaluate(self, **_kwargs):
        self.started.set()
        await asyncio.sleep(self.delay)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class SafetyGatedCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def _attempt(self, calls):
        async def commit(value, permit):
            calls.append(("commit", value, permit.permit_type))
            return {"committed": value}

        async def clarify(value, rail_decision, permit):
            calls.append(("clarify", value, rail_decision.reason_code, permit.permit_type))
            return {"question": "请补充说明办理目的"}

        async def rollback():
            calls.append(("rollback",))

        return RunAttempt(
            attempt_id="attempt_gate",
            commit_callback=commit,
            clarification_callback=clarify,
            rollback_callback=rollback,
        )

    async def test_allow_runs_rail_and_agent_in_parallel_then_commits(self):
        calls = []
        rail = FakeRail(decision("allow"), delay=0.1)

        async def agent():
            calls.append(("agent_started",))
            await asyncio.sleep(0.1)
            return {"intent": "balance"}

        started = time.monotonic()
        outcome = await SafetyGatedRunCoordinator(ttl_seconds=30).run(
            rail=rail,
            agent_call=agent,
            attempt=self._attempt(calls),
            message="查余额",
            request_id="req_parallel",
            conversation_id="conv_parallel",
            session_revision=2,
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.18)
        self.assertEqual(outcome.decision.decision, "allow")
        self.assertEqual(outcome.result, {"committed": {"intent": "balance"}})
        self.assertTrue(outcome.execution_permit.allows("a2a.delegate"))
        self.assertIn(("agent_started",), calls)
        self.assertIn(("commit", {"intent": "balance"}, "execution"), calls)

    async def test_block_rolls_back_and_discards_agent_that_ignores_cancel(self):
        calls = []
        rail = FakeRail(decision("block"), delay=0.01)

        async def agent():
            calls.append(("agent_started",))
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                calls.append(("agent_cancelled",))
                return {"late": "must be discarded"}

        outcome = await SafetyGatedRunCoordinator(ttl_seconds=30).run(
            rail=rail,
            agent_call=agent,
            attempt=self._attempt(calls),
            message="风险请求",
            request_id="req_block",
            conversation_id="conv_block",
            session_revision=2,
        )

        self.assertEqual(outcome.decision.decision, "block")
        self.assertIsNone(outcome.result)
        self.assertIsNone(outcome.execution_permit)
        self.assertIn(("rollback",), calls)
        self.assertIn(("agent_cancelled",), calls)
        self.assertFalse(any(item[0] == "commit" for item in calls))

    async def test_review_uses_clarification_permit_and_never_execution_permit(self):
        calls = []
        outcome = await SafetyGatedRunCoordinator(ttl_seconds=30).run(
            rail=FakeRail(decision("review")),
            agent_call=lambda: asyncio.sleep(0, result={"intent": "transfer"}),
            attempt=self._attempt(calls),
            message="边界风险",
            request_id="req_review",
            conversation_id="conv_review",
            session_revision=5,
        )

        self.assertEqual(outcome.result, {"question": "请补充说明办理目的"})
        self.assertIsNone(outcome.execution_permit)
        self.assertTrue(outcome.clarification_permit.allows("user_agent.clarify"))
        self.assertFalse(outcome.clarification_permit.allows("a2a.delegate"))
        self.assertEqual(calls[0][0], "clarify")

    async def test_unavailable_and_rail_exception_fail_closed(self):
        for rail_result in (
            decision("unavailable"),
            RuntimeError("private dependency detail"),
        ):
            calls = []
            outcome = await SafetyGatedRunCoordinator(ttl_seconds=30).run(
                rail=FakeRail(rail_result),
                agent_call=lambda: asyncio.sleep(0.01, result={"intent": "balance"}),
                attempt=self._attempt(calls),
                message="查询",
                request_id="req_unavailable",
                conversation_id="conv_unavailable",
                session_revision=1,
            )
            self.assertEqual(outcome.decision.decision, "unavailable")
            self.assertEqual(calls, [("rollback",)])

    async def test_agent_error_rolls_back_when_rail_allows(self):
        calls = []

        async def failing_agent():
            raise RuntimeError("model failure")

        with self.assertRaisesRegex(RuntimeError, "model failure"):
            await SafetyGatedRunCoordinator(ttl_seconds=30).run(
                rail=FakeRail(decision("allow")),
                agent_call=failing_agent,
                attempt=self._attempt(calls),
                message="查询",
                request_id="req_agent_error",
                conversation_id="conv_agent_error",
                session_revision=1,
            )
        self.assertEqual(calls, [("rollback",)])


if __name__ == "__main__":
    unittest.main()
