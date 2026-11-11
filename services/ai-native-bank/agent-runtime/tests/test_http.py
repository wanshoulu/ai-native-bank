import json
import os
import unittest

from fastapi.testclient import TestClient

from app.config import RuntimeSettings
from app.contracts import (
    BusinessWorkflowResumeResponse,
    CapabilityPlan,
    ConfirmationView,
    ConversationState,
    IntentTaskEnvelope,
    RuntimeAgentRunResponse,
    UserResponse,
)
from app.main import create_app
from app.trace import TraceError, build_trace_event, project_safe_run
from tests.test_contracts import envelope_payload, request_payload
from tests.test_service import plan_payload


class FakeService:
    def __init__(self):
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        raw_envelope = envelope_payload()
        raw_envelope["tasks"][0]["slots"] = {
            "accountId": {
                "value": "acct_salary_private",
                "source": "context",
                "confidence": 0.9,
            }
        }
        envelope = IntentTaskEnvelope.model_validate(raw_envelope).model_copy(
            update={
                "trace_id": request.trace_id,
                "conversation_id": request.conversation_id,
                "source_agent_run_id": request.agent_run_id,
            }
        )
        raw_plan = plan_payload()
        raw_plan["steps"][0]["input_bindings"] = {
            "accountId": "acct_salary_private"
        }
        plan = CapabilityPlan.model_validate(raw_plan).model_copy(
            update={"delegation_id": envelope.delegation_id}
        )
        return RuntimeAgentRunResponse(
            agentRunId=request.agent_run_id,
            conversationId=request.conversation_id,
            status="succeeded",
            intentTaskEnvelope=envelope,
            capabilityPlan=plan,
            userResponse=UserResponse.model_validate({
                "schemaVersion": "1.0",
                "type": "result",
                "title": "查询完成",
                "body": "已安全返回查询结果。",
                "items": [],
                "actions": [],
                "source": None,
            }),
        )

    async def stream(self, request):
        result = await self.run(request)
        yield build_trace_event(
            sequence=1,
            stage="request.accepted",
            status="accepted",
            agent_run_id=request.agent_run_id,
            trace_id=request.trace_id,
            title="请求已接入",
            input_fields=["message"],
            output_fields=["traceId"],
            metrics={},
        )
        yield build_trace_event(
            sequence=2,
            stage="run.completed",
            status="completed",
            agent_run_id=request.agent_run_id,
            trace_id=request.trace_id,
            title="智能服务运行完成",
            input_fields=[],
            output_fields=["status"],
            metrics={"status": result.status},
            duration_ms=12,
            result=project_safe_run(result),
        )

    async def stream_with_result(self, request):
        result = await self.run(request)
        events = []
        events.append(build_trace_event(
            sequence=1, stage="request.accepted", status="accepted",
            agent_run_id=request.agent_run_id, trace_id=request.trace_id,
            title="请求已接入", input_fields=["message"], output_fields=["traceId"], metrics={},
        ))
        events.append(build_trace_event(
            sequence=2, stage="run.completed", status="completed",
            agent_run_id=request.agent_run_id, trace_id=request.trace_id,
            title="智能服务运行完成", input_fields=[], output_fields=["status"],
            metrics={"status": result.status}, duration_ms=12, result=project_safe_run(result),
        ))
        yield events[0], None
        yield events[1], result

    async def resume_business_workflow(self, business_workflow_id, request):
        self.requests.append(request)
        return BusinessWorkflowResumeResponse.model_validate({
            "schemaVersion": "1.0",
            "agentRunId": request.agent_run_id,
            "businessWorkflowId": business_workflow_id,
            "status": "succeeded",
            "receipts": [{
                "grantId": request.grants[0].grant_id,
                "taskId": request.grants[0].task_id,
                "capabilityId": request.grants[0].capability_id,
                "status": "SUCCEEDED",
                "outputKeys": ["status"],
            }],
        })


class FailingDeliveryService:
    async def run(self, request):
        envelope = IntentTaskEnvelope.model_validate(envelope_payload("non_task", [])).model_copy(
            update={
                "trace_id": request.trace_id,
                "conversation_id": request.conversation_id,
                "source_agent_run_id": request.agent_run_id,
            }
        )
        plan = CapabilityPlan.model_validate({
            "schema_version": "1.0",
            "business_agent_run_id": "bar_failed_safe",
            "delegation_id": envelope.delegation_id,
            "status": "blocked",
            "steps": [],
        })
        return RuntimeAgentRunResponse(
            agentRunId=request.agent_run_id,
            conversationId=request.conversation_id,
            status="blocked",
            intentTaskEnvelope=envelope,
            capabilityPlan=plan,
            userResponse=UserResponse.model_validate({
                "schemaVersion": "1.0",
                "type": "notice",
                "title": "请求未完成",
                "body": "智能服务暂时无法完成本次请求，请稍后重试。",
                "items": [],
                "actions": [],
                "source": None,
            }),
        )

    async def stream_with_result(self, request):
        result = await self.run(request)
        yield build_trace_event(
            sequence=1,
            stage="run.failed",
            status="failed",
            agent_run_id=request.agent_run_id,
            trace_id=request.trace_id,
            title="智能服务运行失败",
            input_fields=[],
            output_fields=["errorCode"],
            metrics={"failedStage": "user_agent_contract_violation"},
            error=TraceError(
                code="user_agent_contract_violation",
                message="用户意图结果未通过契约校验",
            ),
        ), result


class OrderedDeliveryService(FakeService):
    async def run(self, request):
        result = await super().run(request)
        clarification = UserResponse.model_validate({
            "schemaVersion": "1.0",
            "type": "clarification",
            "title": "请选择信用卡",
            "body": "余额已经查询完成，请选择需要还款的信用卡。",
            "items": [],
            "actions": [{
                "id": "select-card",
                "label": "选择信用卡",
                "kind": "reply",
                "payload": {"message": "选择信用卡"},
            }],
            "source": None,
        })
        return result.model_copy(update={
            "status": "clarification_required",
            "user_response": None,
            "user_responses": (result.user_response, clarification),
        })


class SecurityTerminalDeliveryService:
    async def run(self, request):
        return RuntimeAgentRunResponse(
            agentRunId=request.agent_run_id,
            conversationId=request.conversation_id,
            status="blocked",
            userResponse=UserResponse(
                schemaVersion="1.0",
                type="notice",
                title="请求暂未处理",
                body="检测到本次内容可能存在金融安全风险，暂不继续办理。",
            ),
        )


class ClarificationDeliveryService(FakeService):
    async def run(self, request):
        result = await super().run(request)
        return result.model_copy(update={
            "status": "clarification_required",
            "conversation_state": ConversationState.model_validate({
                "schemaVersion": "1.0",
                "pendingIntentId": "ccb.transfer.create",
                "status": "retain",
                "slots": [
                    {"name": "payee", "value": "张三", "source": "user_explicit"},
                    {"name": "amount", "value": "100", "source": "user_explicit"},
                ],
            }),
            "user_response": UserResponse.model_validate({
                "schemaVersion": "1.0",
                "type": "clarification",
                "title": "选择查询账户",
                "body": "您想查询哪张卡的余额？",
                "items": [],
                "actions": [{
                    "id": "account-option-1",
                    "label": "工资卡",
                    "description": "尾号 6217",
                    "kind": "reply",
                    "payload": {"message": "查询尾号6217的余额"},
                }],
                "source": None,
            }),
        })


class ClearStateDeliveryService(FakeService):
    async def run(self, request):
        result = await super().run(request)
        return result.model_copy(update={
            "conversation_state": ConversationState.model_validate({
                "schemaVersion": "1.0",
                "pendingIntentId": None,
                "status": "clear",
                "slots": [],
            }),
        })


class ConfirmationDeliveryService(FakeService):
    async def run(self, request):
        result = await super().run(request)
        return result.model_copy(update={
            "status": "awaiting_confirmation",
            "user_response": UserResponse.model_validate({
                "schemaVersion": "1.0",
                "type": "confirmation",
                "title": "Confirm transfer draft",
                "body": "Create a transfer draft without moving funds.",
                "items": [],
                "actions": [
                    {"id": "confirm-action", "label": "Confirm", "kind": "confirm", "payload": {}},
                    {"id": "dismiss-action", "label": "Cancel", "kind": "dismiss", "payload": {}},
                ],
                "source": None,
            }),
            "confirmation": ConfirmationView.model_validate({
                "checkpointId": "acp_http_test",
                "capabilityId": "bank.transfer.order.create",
                "confirmationToken": "t" * 43,
                "expiresAt": "2026-08-30T10:05:00.000Z",
                "summary": {
                    "title": "Confirm transfer draft",
                    "description": "Create a transfer draft without moving funds.",
                    "riskLevel": "L1",
                },
            }),
            "conversation_state": ConversationState.model_validate({
                "schemaVersion": "1.0",
                "pendingIntentId": None,
                "status": "clear",
                "slots": [],
            }),
        })


def settings(**overrides):
    values = {
        "runtime_token": "runtime-test-token",
        "model_provider": "openai",
        "model_name": "mock-model",
        "model_base_url": "http://127.0.0.1:9/v1",
        "model_api_key": "model-test-key",
        "workspace_root": "/tmp/ai-native-bank-test-agents",
    }
    values.update(overrides)
    return RuntimeSettings(**values)


class HttpTests(unittest.TestCase):
    def test_security_terminal_json_keeps_required_empty_response_lists(self):
        client = TestClient(create_app(settings(), service=SecurityTerminalDeliveryService()))

        response = client.post(
            "/v1/agent-runs",
            json=request_payload(),
            headers={"authorization": "Bearer runtime-test-token"},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["userResponse"]["items"], [])
        self.assertEqual(payload["userResponse"]["actions"], [])

    def test_json_confirmation_keeps_empty_action_payloads_and_explicit_clear_state(self):
        client = TestClient(create_app(settings(), service=ConfirmationDeliveryService()))

        response = client.post(
            "/v1/agent-runs",
            json=request_payload(),
            headers={"authorization": "Bearer runtime-test-token"},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(
            [item["payload"] for item in payload["userResponse"]["actions"]],
            [{}, {}],
        )
        self.assertIn("pendingIntentId", payload["conversationState"])
        self.assertIsNone(payload["conversationState"]["pendingIntentId"])

    def test_clear_private_state_sse_keeps_explicit_null_pending_intent(self):
        client = TestClient(create_app(settings(), service=ClearStateDeliveryService()))

        response = client.post(
            "/v1/agent-runs",
            json=request_payload(),
            headers={
                "authorization": "Bearer runtime-test-token",
                "accept": "text/event-stream",
            },
        )

        state_frame = response.text.split("event: agent.state\ndata: ", 1)[1]
        state = json.loads(state_frame.split("\n\n", 1)[0])
        self.assertEqual(
            state,
            {
                "schemaVersion": "1.0",
                "pendingIntentId": None,
                "status": "clear",
                "slots": [],
            },
        )

    def test_agent_result_sse_omits_null_fields_from_discriminated_action_payload(self):
        client = TestClient(create_app(settings(), service=ClarificationDeliveryService()))

        response = client.post(
            "/v1/agent-runs",
            json=request_payload(),
            headers={
                "authorization": "Bearer runtime-test-token",
                "accept": "text/event-stream",
            },
        )

        result_frame = response.text.split("event: agent.result\ndata: ", 1)[1]
        delivery = json.loads(result_frame.split("\n\n", 1)[0])
        self.assertEqual(
            delivery["userResponse"]["actions"][0]["payload"],
            {"message": "查询尾号6217的余额"},
        )
        self.assertIn("event: agent.state", response.text)
        self.assertNotIn("conversationState", result_frame.split("\n\n", 1)[0])
        self.assertNotIn("张三", result_frame.split("\n\n", 1)[0])

    def test_agent_result_sse_preserves_ordered_multi_intent_responses(self):
        client = TestClient(create_app(settings(), service=OrderedDeliveryService()))

        response = client.post(
            "/v1/agent-runs",
            json=request_payload(),
            headers={
                "authorization": "Bearer runtime-test-token",
                "accept": "text/event-stream",
            },
        )

        result_frame = response.text.split("event: agent.result\ndata: ", 1)[1]
        delivery = json.loads(result_frame.split("\n\n", 1)[0])
        self.assertNotIn("userResponse", delivery)
        self.assertEqual(
            [item["type"] for item in delivery["userResponses"]],
            ["result", "clarification"],
        )

    def test_agent_run_stream_uses_sse_and_contains_only_safe_projection(self):
        service = FakeService()
        client = TestClient(create_app(settings(), service=service))

        response = client.post(
            "/v1/agent-runs",
            json=request_payload(),
            headers={
                "authorization": "Bearer runtime-test-token",
                "accept": "text/event-stream",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertIn("event: agent.trace", response.text)
        self.assertIn("event: agent.result", response.text)
        self.assertIn('"agentRunId":"abr_001"', response.text)
        self.assertIn('"stage":"run.completed"', response.text)
        self.assertIn('"userResponse":{"schemaVersion":"1.0"', response.text)
        self.assertNotIn("acct_salary_private", response.text)
        self.assertNotIn("input_bindings", response.text)
        self.assertNotIn('"value"', response.text)
        self.assertEqual(len(service.requests), 1)

    def test_failed_trace_is_followed_by_safe_agent_result(self):
        client = TestClient(create_app(settings(), service=FailingDeliveryService()))

        response = client.post(
            "/v1/agent-runs",
            json=request_payload(),
            headers={
                "authorization": "Bearer runtime-test-token",
                "accept": "text/event-stream",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertLess(
            response.text.index("event: agent.trace"),
            response.text.index("event: agent.result"),
        )
        self.assertIn('"stage":"run.failed"', response.text)
        self.assertIn('"status":"blocked"', response.text)
        result_payload = response.text.split("event: agent.result", 1)[1]
        self.assertNotIn("user_agent_contract_violation", result_payload)

    def test_agent_run_stream_requires_auth_and_runtime_readiness_before_headers(self):
        ready_client = TestClient(create_app(settings(), service=FakeService()))
        denied = ready_client.post(
            "/v1/agent-runs",
            json=request_payload(),
            headers={"accept": "text/event-stream"},
        )
        unavailable = TestClient(create_app(settings(model_name="", model_api_key=""))).post(
            "/v1/agent-runs",
            json=request_payload(),
            headers={
                "authorization": "Bearer runtime-test-token",
                "accept": "text/event-stream",
            },
        )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json()["detail"]["code"], "model_not_configured")

    def test_health_is_redacted_and_does_not_require_browser_credentials(self):
        configured = settings(
            memory_mode="governed_write",
            user_memory_mode="read_only",
            task_memory_mode="governed_write",
            memory_service_token="memory-service-token",
        )
        client = TestClient(create_app(configured, service=FakeService()))
        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        text = response.text
        self.assertNotIn("runtime-test-token", text)
        self.assertNotIn("model-test-key", text)
        self.assertNotIn("127.0.0.1", text)
        self.assertEqual(response.json()["provider"], "openjiuwen")
        self.assertNotIn("modelBaseUrl", response.json())
        self.assertNotIn("tokenFile", response.json())
        self.assertEqual(response.json()["userMemoryMode"], "read_only")
        self.assertEqual(response.json()["taskMemoryMode"], "governed_write")
        self.assertEqual(response.json()["userMemoryPolicyConfigured"], True)
        self.assertEqual(response.json()["memoryWriteConfigured"], True)
        self.assertNotIn("memoryControlUrl", response.json())
        self.assertNotIn("memoryPolicyPath", response.json())

    def test_agent_run_requires_internal_bearer_and_returns_created_resource(self):
        service = FakeService()
        client = TestClient(create_app(settings(), service=service))
        denied = client.post("/v1/agent-runs", json=request_payload())
        accepted = client.post(
            "/v1/agent-runs",
            json=request_payload(),
            headers={"authorization": "Bearer runtime-test-token"},
        )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(accepted.json()["agentRunId"], "abr_001")
        self.assertEqual(len(service.requests), 1)

    def test_business_workflow_resume_requires_bearer_and_strict_grants(self):
        service = FakeService()
        client = TestClient(create_app(settings(), service=service))
        payload = {
            "agentRunId": "abr_001",
            "conversationId": "conv_001",
            "customerId": "cust_demo_001",
            "checkpointId": "acp_one",
            "grants": [{
                "grantId": "acg_one",
                "taskId": "task_loss",
                "capabilityId": "bank.credit-card.loss-report.create",
                "expiresAt": "2026-08-31T10:05:00.000Z",
            }],
        }

        denied = client.post("/v1/business-workflows/bwf_one/resume", json=payload)
        accepted = client.post(
            "/v1/business-workflows/bwf_one/resume",
            json=payload,
            headers={"authorization": "Bearer runtime-test-token"},
        )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["businessWorkflowId"], "bwf_one")
        self.assertEqual(accepted.json()["receipts"][0]["status"], "SUCCEEDED")

    def test_validation_errors_and_body_limit_do_not_echo_sensitive_input(self):
        client = TestClient(create_app(settings(), service=FakeService()))
        payload = request_payload()
        payload["confirmationToken"] = "confirmation-top-secret"
        invalid = client.post(
            "/v1/agent-runs", json=payload,
            headers={"authorization": "Bearer runtime-test-token"},
        )
        oversized = client.post(
            "/v1/agent-runs",
            content=b"x" * (257 * 1024),
            headers={
                "authorization": "Bearer runtime-test-token",
                "content-type": "application/json",
            },
        )

        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn("confirmation-top-secret", invalid.text)
        self.assertEqual(oversized.status_code, 413)

    def test_missing_model_or_runtime_token_fails_closed(self):
        no_model = settings(model_name="", model_api_key="")
        no_token = settings(runtime_token="")
        model_response = TestClient(create_app(no_model)).post(
            "/v1/agent-runs", json=request_payload(),
            headers={"authorization": "Bearer runtime-test-token"},
        )
        token_response = TestClient(create_app(no_token)).post(
            "/v1/agent-runs", json=request_payload(),
        )

        self.assertEqual(model_response.status_code, 503)
        self.assertEqual(model_response.json()["detail"]["code"], "model_not_configured")
        self.assertEqual(token_response.status_code, 503)
        self.assertEqual(token_response.json()["detail"]["code"], "runtime_token_not_configured")


if __name__ == "__main__":
    unittest.main()
