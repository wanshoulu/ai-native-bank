import asyncio
import types
import unittest

from app.contracts import BusinessWorkflowResumeRequest, RuntimeAgentRunRequest
from app.deepagent_factory import SessionRevisionConflict
from app.safety_gate import RailDecision
from app.service import AgentContractError, DualDeepAgentService
from tests.test_contracts import envelope_payload, request_payload
from tests.test_service import (
    FakeRunner,
    clarification_plan_payload,
    non_task_envelope_payload,
)


class FakeRail:
    def __init__(self, decision: str, *, delay: float = 0):
        self.decision = decision
        self.delay = delay
        self.calls = []

    async def evaluate(self, **payload):
        self.calls.append(payload)
        await asyncio.sleep(self.delay)
        terminal = self.decision in {"block", "unavailable"}
        return RailDecision.model_validate({
            "railId": "finance-text-safety",
            "decision": self.decision,
            "reasonCode": None if self.decision == "allow" else "risk_context_incomplete",
            "policyVersion": "2026.09",
            "responseKey": "security.block.default" if terminal else None,
            "durationMs": int(self.delay * 1000),
        })


class StageRail:
    def __init__(
        self,
        *,
        input_decision: str = "allow",
        output_decision: str = "allow",
        action_decision: str = "allow",
    ):
        self.input_decision = input_decision
        self.output_decision = output_decision
        self.action_decision = action_decision
        self.input_calls = []
        self.output_calls = []
        self.action_calls = []

    @staticmethod
    def _result(stage: str, decision: str) -> RailDecision:
        terminal = decision in {"block", "unavailable"}
        reason = {
            "input": "unsafe_content",
            "output": "unsafe_output",
            "action": "financial_control_evasion",
        }[stage]
        return RailDecision.model_validate({
            "railId": "finance-text-safety",
            "stage": stage,
            "decision": decision,
            "reasonCode": reason if decision != "allow" else None,
            "policyVersion": "finance-safety-v2",
            "responseKey": "security.block.default" if terminal else None,
            "durationMs": 1,
        })

    async def evaluate_input(self, **payload):
        self.input_calls.append(payload)
        return self._result("input", self.input_decision)

    async def evaluate_output(self, **payload):
        self.output_calls.append(payload)
        return self._result("output", self.output_decision)

    async def evaluate_action(self, **payload):
        self.action_calls.append(payload)
        return self._result("action", self.action_decision)


class RecordingCapabilityInvoker:
    def __init__(self):
        self.calls = 0

    def activate_authorized(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("blocked action must not activate capabilities")


class AttemptRunner(FakeRunner):
    def __init__(self, result, *, delay: float = 0):
        super().__init__(result)
        self.delay = delay
        self.commits = []
        self.rollbacks = []
        self.releases = []
        self.safety_clarifications = []

    @staticmethod
    def attempt_session_id(canonical_session_id, request_id):
        return canonical_session_id + ".attempt.fixed"

    async def run_attempt(self, payload, *, canonical_session_id, request_id):
        await asyncio.sleep(self.delay)
        submission = await self.run(
            payload,
            session_id=self.attempt_session_id(canonical_session_id, request_id),
        )
        return types.SimpleNamespace(
            attempt_session_id=self.attempt_session_id(canonical_session_id, request_id),
            submission=submission,
        )

    async def rollback_attempt(self, attempt_session_id):
        self.rollbacks.append(attempt_session_id)

    async def run_safety_clarification(self, payload, *, session_id):
        self.safety_clarifications.append({
            "payload": payload,
            "sessionId": session_id,
        })
        return {
            "schemaVersion": "1.0",
            "title": "请说明本次请求用途",
            "body": "为了安全地继续处理转账请求，请说明本次请求的实际用途和必要背景。",
        }

    async def release_attempt(self, attempt_session_id):
        self.releases.append(attempt_session_id)

    async def commit_safe_state(
        self, payload, *, canonical_session_id, expected_revision, request_id
    ):
        self.commits.append({
            "payload": payload,
            "canonicalSessionId": canonical_session_id,
            "expectedRevision": expected_revision,
            "requestId": request_id,
        })
        return {"envelopeRevision": expected_revision + 1}


class RevisionAwareAttemptRunner(AttemptRunner):
    def __init__(self, result, *, delay: float = 0):
        super().__init__(result, delay=delay)
        self.revisions = {}
        self.active_attempts = 0
        self.max_active_attempts = 0

    async def current_safe_revision(self, canonical_session_id):
        return self.revisions.get(canonical_session_id, 0)

    async def run_attempt(self, payload, *, canonical_session_id, request_id):
        self.active_attempts += 1
        self.max_active_attempts = max(self.max_active_attempts, self.active_attempts)
        try:
            return await super().run_attempt(
                payload,
                canonical_session_id=canonical_session_id,
                request_id=request_id,
            )
        finally:
            self.active_attempts -= 1

    async def commit_safe_state(
        self, payload, *, canonical_session_id, expected_revision, request_id
    ):
        current = self.revisions.get(canonical_session_id, 0)
        if current != expected_revision:
            raise SessionRevisionConflict("agent_session_revision_conflict")
        await super().commit_safe_state(
            payload,
            canonical_session_id=canonical_session_id,
            expected_revision=expected_revision,
            request_id=request_id,
        )
        self.revisions[canonical_session_id] = current + 1
        return {"envelopeRevision": current + 1}


class SafetyGatedServiceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request(*, run_id, request_id, conversation_id="conv_revision"):
        payload = request_payload()
        payload.update({
            "agentRunId": run_id,
            "requestId": request_id,
            "traceId": "trace_" + request_id,
            "conversationId": conversation_id,
        })
        return RuntimeAgentRunRequest.model_validate(payload)

    async def test_sequential_requests_read_the_canonical_session_revision(self):
        user = RevisionAwareAttemptRunner(envelope_payload())
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(clarification_plan_payload()),
            input_rail=FakeRail("allow"),
            input_rail_mode="enforce",
        )

        first = await service.run(self.request(run_id="abr_rev_1", request_id="req_rev_1"))
        second = await service.run(self.request(run_id="abr_rev_2", request_id="req_rev_2"))

        self.assertNotEqual(first.status, "failed")
        self.assertNotEqual(second.status, "failed")
        self.assertEqual(
            [item["expectedRevision"] for item in user.commits],
            [0, 1],
        )

    async def test_concurrent_requests_are_serialized_per_conversation(self):
        user = RevisionAwareAttemptRunner(envelope_payload(), delay=0.02)
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(clarification_plan_payload()),
            input_rail=FakeRail("allow", delay=0.01),
            input_rail_mode="enforce",
        )

        results = await asyncio.gather(
            service.run(self.request(run_id="abr_parallel_1", request_id="req_parallel_1")),
            service.run(self.request(run_id="abr_parallel_2", request_id="req_parallel_2")),
        )

        self.assertTrue(all(item.status != "failed" for item in results))
        self.assertEqual(
            [item["expectedRevision"] for item in user.commits],
            [0, 1],
        )
        self.assertEqual(user.max_active_attempts, 1)

    async def test_different_conversations_keep_user_agent_parallelism(self):
        user = RevisionAwareAttemptRunner(envelope_payload(), delay=0.02)
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(clarification_plan_payload()),
            input_rail=FakeRail("allow", delay=0.01),
            input_rail_mode="enforce",
        )

        await asyncio.gather(
            service.run(self.request(
                run_id="abr_conversation_a",
                request_id="req_conversation_a",
                conversation_id="conv_revision_a",
            )),
            service.run(self.request(
                run_id="abr_conversation_b",
                request_id="req_conversation_b",
                conversation_id="conv_revision_b",
            )),
        )

        self.assertEqual(user.max_active_attempts, 2)

    async def test_block_rolls_back_speculation_and_never_delegates(self):
        user = AttemptRunner(envelope_payload(), delay=0.01)
        business = FakeRunner(AssertionError("business agent must not start"))
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=business,
            input_rail=FakeRail("block", delay=0.03),
            input_rail_mode="enforce",
        )

        pairs = [item async for item in service.stream_with_result(
            RuntimeAgentRunRequest.model_validate(request_payload())
        )]

        result = pairs[-1][1]
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.intent_task_envelope)
        self.assertIsNone(result.capability_plan)
        self.assertEqual(len(user.rollbacks), 1)
        self.assertEqual(user.commits, [])
        self.assertEqual(user.safety_clarifications, [])
        self.assertEqual(business.inputs, [])
        self.assertNotIn("delegation.created", [event.stage for event, _ in pairs])

    async def test_allow_commits_validated_projection_before_delegation(self):
        user = AttemptRunner(envelope_payload(), delay=0.03)
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=business,
            input_rail=FakeRail("allow", delay=0.03),
            input_rail_mode="enforce",
        )

        pairs = [item async for item in service.stream_with_result(
            RuntimeAgentRunRequest.model_validate(request_payload())
        )]

        self.assertEqual(len(user.commits), 1)
        self.assertEqual(user.rollbacks, [])
        self.assertEqual(len(user.releases), 1)
        stages = [event.stage for event, _ in pairs]
        self.assertIn("delegation.created", stages)
        self.assertLess(stages.index("user_agent.committed"), stages.index("delegation.created"))

    async def test_output_block_replaces_an_early_non_task_response(self):
        payload = request_payload()
        payload["message"] = "你好"
        rail = StageRail(output_decision="block")
        service = DualDeepAgentService(
            user_runner=AttemptRunner(non_task_envelope_payload()),
            business_runner=FakeRunner(AssertionError("business agent must not start")),
            input_rail=rail,
            input_rail_mode="enforce",
        )

        pairs = [item async for item in service.stream_with_result(
            RuntimeAgentRunRequest.model_validate(payload)
        )]

        result = pairs[-1][1]
        stages = [event.stage for event, _ in pairs]
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.intent_task_envelope)
        self.assertEqual(len(rail.output_calls), 1)
        self.assertIn("output_rail.started", stages)
        self.assertIn("output_rail.completed", stages)
        self.assertIn("security_gate.decided", stages)
        self.assertEqual(stages[-1], "run.completed")

    async def test_confirmed_action_block_cannot_activate_a_capability(self):
        rail = StageRail(action_decision="block")
        invoker = RecordingCapabilityInvoker()
        service = DualDeepAgentService(
            user_runner=AttemptRunner(envelope_payload()),
            business_runner=FakeRunner(clarification_plan_payload()),
            capability_invoker=invoker,
            input_rail=rail,
            input_rail_mode="enforce",
        )
        request = BusinessWorkflowResumeRequest.model_validate({
            "resumeKind": "confirmation",
            "agentRunId": "abr_action_block",
            "conversationId": "conv_action_block",
            "customerId": "cust_demo_001",
            "checkpointId": "acp_action_block",
            "grants": [{
                "grantId": "acg_action_block",
                "taskId": "task_action_block",
                "capabilityId": "bank.transfer.order.create",
                "expiresAt": "2026-09-03T10:15:00.000Z",
            }],
        })

        with self.assertRaisesRegex(AgentContractError, "failed safety policy"):
            await service.resume_business_workflow("bwf_action_block", request)

        self.assertEqual(len(rail.action_calls), 1)
        self.assertEqual(invoker.calls, 0)

    async def test_unavailable_fails_closed_without_agent_artifacts(self):
        user = AttemptRunner(envelope_payload(), delay=0.05)
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(AssertionError("must not start")),
            input_rail=FakeRail("unavailable"),
            input_rail_mode="enforce",
        )

        result = await service.run(
            RuntimeAgentRunRequest.model_validate(request_payload())
        )

        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.intent_task_envelope)
        self.assertIsNone(result.capability_plan)
        self.assertEqual(len(user.rollbacks), 1)

    async def test_candidate_free_non_executable_input_survives_guardrail_unavailability(self):
        payload = request_payload()
        payload["message"] = "域外普通话题"
        user = AttemptRunner(AssertionError("candidate-free input must not call the model"))
        business = FakeRunner(AssertionError("non-task input must not start business agent"))
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=business,
            input_rail=FakeRail("unavailable"),
            input_rail_mode="enforce",
        )

        pairs = [item async for item in service.stream_with_result(
            RuntimeAgentRunRequest.model_validate(payload)
        )]

        result = pairs[-1][1]
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.intent_task_envelope.intent_shape, "non_task")
        self.assertEqual(user.inputs, [])
        self.assertEqual(business.inputs, [])
        stages = [event.stage for event, _ in pairs]
        self.assertNotIn("delegation.created", stages)
        gate = next(event for event, _ in pairs if event.stage == "security_gate.decided")
        self.assertIsNone(gate.summary.metrics["permitType"])

    async def test_candidate_free_non_executable_input_does_not_request_safety_clarification(self):
        payload = request_payload()
        payload["message"] = "无业务目标的普通短语"
        user = AttemptRunner(AssertionError("candidate-free input must not call the model"))
        business = FakeRunner(AssertionError("non-task input must not start business agent"))
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=business,
            input_rail=FakeRail("review"),
            input_rail_mode="enforce",
        )

        pairs = [item async for item in service.stream_with_result(
            RuntimeAgentRunRequest.model_validate(payload)
        )]

        result = pairs[-1][1]
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.intent_task_envelope.intent_shape, "non_task")
        self.assertEqual(user.inputs, [])
        self.assertEqual(user.safety_clarifications, [])
        self.assertEqual(business.inputs, [])
        self.assertNotIn("delegation.created", [event.stage for event, _ in pairs])

    async def test_allowed_candidate_free_input_never_receives_execution_authority(self):
        payload = request_payload()
        payload["message"] = "聊聊天气"
        user = AttemptRunner(AssertionError("candidate-free input must not call the model"))
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(AssertionError("non-task input must not start business agent")),
            input_rail=FakeRail("allow"),
            input_rail_mode="enforce",
        )

        pairs = [item async for item in service.stream_with_result(
            RuntimeAgentRunRequest.model_validate(payload)
        )]

        self.assertEqual(pairs[-1][1].status, "unsupported")
        stages = [event.stage for event, _ in pairs]
        self.assertNotIn("user_agent.committed", stages)
        self.assertNotIn("delegation.created", stages)
        gate = next(event for event, _ in pairs if event.stage == "security_gate.decided")
        self.assertIsNone(gate.summary.metrics["permitType"])

    async def test_review_returns_clarification_without_a2a_delegation(self):
        user = AttemptRunner(envelope_payload(), delay=0.01)
        business = FakeRunner(AssertionError("business agent must not start"))
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=business,
            input_rail=FakeRail("review", delay=0.01),
            input_rail_mode="enforce",
        )

        pairs = [item async for item in service.stream_with_result(
            RuntimeAgentRunRequest.model_validate(request_payload())
        )]

        result = pairs[-1][1]
        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.type, "clarification")
        self.assertIn("转账", result.user_response.body)
        self.assertIn("实际用途", result.user_response.body)
        self.assertIsNone(result.intent_task_envelope)
        self.assertEqual(len(user.rollbacks), 1)
        self.assertEqual(user.commits, [])
        self.assertEqual(len(user.safety_clarifications), 1)
        self.assertEqual(
            user.safety_clarifications[0]["sessionId"],
            "user:conv_001.attempt.fixed",
        )
        review = user.safety_clarifications[0]["payload"]["safety_review"]
        self.assertEqual(review["reason_code"], "risk_context_incomplete")
        self.assertIn("转账", review["intent_summary"])
        self.assertEqual(business.inputs, [])
        stages = [event.stage for event, _ in pairs]
        self.assertIn("user_agent.completed", stages)
        self.assertIn("user_agent.rolled_back", stages)
        self.assertLess(
            stages.index("user_agent.completed"),
            stages.index("user_agent.rolled_back"),
        )
        completed_event = next(
            event for event, _ in pairs if event.stage == "user_agent.completed"
        )
        self.assertIn("转账", completed_event.summary.metrics["intentSummary"])
        self.assertNotIn("delegation.created", stages)

    async def test_review_uses_stable_fallback_when_agent_does_not_submit_clarification(self):
        user = AttemptRunner(envelope_payload())

        async def missing_clarification(_payload, *, session_id):
            del session_id
            return None

        user.run_safety_clarification = missing_clarification
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(AssertionError("business agent must not start")),
            input_rail=FakeRail("review"),
            input_rail_mode="enforce",
        )

        pairs = [item async for item in service.stream_with_result(
            RuntimeAgentRunRequest.model_validate(request_payload())
        )]

        self.assertEqual(pairs[-1][0].stage, "run.completed")
        self.assertEqual(pairs[-1][1].status, "clarification_required")
        self.assertEqual(pairs[-1][1].user_response.type, "clarification")
        self.assertEqual(pairs[-1][1].user_response.title, "请补充必要信息")
        self.assertIn("业务背景", pairs[-1][1].user_response.body)
        self.assertEqual(len(user.rollbacks), 1)
        self.assertNotIn("delegation.created", [event.stage for event, _ in pairs])

    async def test_review_uses_stable_fallback_when_clarification_model_raises(self):
        user = AttemptRunner(envelope_payload())

        async def failed_clarification(_payload, *, session_id):
            del session_id
            raise RuntimeError("synthetic clarification failure")

        user.run_safety_clarification = failed_clarification
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(AssertionError("business agent must not start")),
            input_rail=FakeRail("review"),
            input_rail_mode="enforce",
        )

        pairs = [item async for item in service.stream_with_result(
            RuntimeAgentRunRequest.model_validate(request_payload())
        )]

        result = pairs[-1][1]
        self.assertEqual(pairs[-1][0].stage, "run.completed")
        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.title, "请补充必要信息")
        self.assertEqual(len(user.rollbacks), 1)
        self.assertNotIn("delegation.created", [event.stage for event, _ in pairs])


if __name__ == "__main__":
    unittest.main()
