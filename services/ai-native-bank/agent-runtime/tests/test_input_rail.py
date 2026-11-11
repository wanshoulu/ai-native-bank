import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.input_rail import DisabledInputRail, HttpInputRail
from app.config import RuntimeSettings


class InputRailClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_only_minimal_input_with_existing_service_identity(self):
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "railId": "finance-text-safety",
                    "decision": "allow",
                    "reasonCode": None,
                    "policyVersion": "finance-guardrail-hardened-v1",
                    "responseKey": None,
                    "durationMs": 15,
                },
            )

        rail = HttpInputRail(
            base_url="http://guardrail-runtime:8000",
            service_token="test-runtime-service-token",
            timeout_ms=3000,
            transport=httpx.MockTransport(handler),
        )
        result = await rail.evaluate(
            message="查询余额",
            request_id="req_1",
            conversation_id="conv_1",
        )
        await rail.aclose()

        self.assertEqual(result.decision, "allow")
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(request.url.path, "/v1/rails/input")
        self.assertEqual(
            request.headers["authorization"],
            "Bearer test-runtime-service-token",
        )
        self.assertEqual(json.loads(request.content), {
            "message": "查询余额",
            "requestId": "req_1",
            "conversationId": "conv_1",
        })

    async def test_posts_bounded_history_and_stage_specific_rails(self):
        calls = []

        async def handler(request):
            calls.append((request.url.path, json.loads(request.content)))
            stage = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={
                "railId": "finance-text-safety",
                "stage": stage,
                "decision": "allow",
                "reasonCode": None,
                "policyVersion": "finance-safety-v2",
                "responseKey": None,
                "durationMs": 3,
            })

        rail = HttpInputRail(
            base_url="http://guardrail-runtime:8000",
            service_token="test-runtime-service-token",
            timeout_ms=3000,
            transport=httpx.MockTransport(handler),
        )
        await rail.evaluate_input(
            message="继续",
            request_id="req_stage",
            conversation_id="conv_stage",
            history=["第一轮", "第二轮"],
        )
        await rail.evaluate_output(
            text="安全回复",
            request_id="req_stage",
            conversation_id="conv_stage",
        )
        await rail.evaluate_action(
            capability_id="bank.transfer.order.create",
            risk_level="L3",
            summary="创建待确认转账",
            requires_confirmation=True,
            confirmed=False,
            input_decision="allow",
        )
        await rail.aclose()

        self.assertEqual([item[0] for item in calls], [
            "/v1/rails/input", "/v1/rails/output", "/v1/rails/action",
        ])
        self.assertEqual(calls[0][1]["history"], ["第一轮", "第二轮"])
        self.assertNotIn("text", calls[2][1])

    async def test_stage_mismatch_fails_closed(self):
        async def handler(_request):
            return httpx.Response(200, json={
                "railId": "finance-text-safety",
                "stage": "input",
                "decision": "allow",
                "reasonCode": None,
                "policyVersion": "finance-safety-v2",
                "responseKey": None,
                "durationMs": 1,
            })

        rail = HttpInputRail(
            base_url="http://guardrail-runtime:8000",
            service_token="test-runtime-service-token",
            timeout_ms=3000,
            transport=httpx.MockTransport(handler),
        )
        result = await rail.evaluate_output(
            text="安全回复", request_id="req_mismatch", conversation_id="conv_mismatch"
        )
        await rail.aclose()
        self.assertEqual(result.decision, "unavailable")

    async def test_dependency_failures_are_unavailable_and_never_allow(self):
        scenarios = [
            httpx.ConnectTimeout("slow rail"),
            httpx.Response(401, json={"detail": "secret"}),
            httpx.Response(200, content=b"not-json"),
            httpx.Response(200, json={"decision": "allow"}),
            httpx.Response(200, content=b"x" * 70_000),
        ]
        for scenario in scenarios:
            async def handler(_request, value=scenario):
                if isinstance(value, Exception):
                    raise value
                return value

            rail = HttpInputRail(
                base_url="http://guardrail-runtime:8000",
                service_token="test-runtime-service-token",
                timeout_ms=3000,
                transport=httpx.MockTransport(handler),
            )
            with self.subTest(scenario=type(scenario).__name__):
                result = await rail.evaluate(
                    message="合成测试",
                    request_id="req_failure",
                    conversation_id="conv_failure",
                )
                self.assertEqual(result.decision, "unavailable")
                self.assertEqual(result.reason_code, "guardrail_runtime_error")
                self.assertEqual(result.response_key, "security.unavailable.default")
            await rail.aclose()

    async def test_retry_is_bounded_to_one_transient_retry(self):
        calls = 0

        async def handler(_request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, json={"error": "temporary"})
            return httpx.Response(200, json={
                "railId": "finance-text-safety",
                "decision": "review",
                "reasonCode": "insufficient_safety_context",
                "policyVersion": "finance-guardrail-hardened-v1",
                "responseKey": None,
                "durationMs": 20,
            })

        rail = HttpInputRail(
            base_url="http://guardrail-runtime:8000",
            service_token="test-runtime-service-token",
            timeout_ms=3000,
            transport=httpx.MockTransport(handler),
        )
        result = await rail.evaluate(
            message="边界风险请求",
            request_id="req_review",
            conversation_id="conv_review",
        )
        await rail.aclose()

        self.assertEqual(result.decision, "review")
        self.assertEqual(calls, 2)

    async def test_cancellation_propagates_instead_of_becoming_unavailable(self):
        async def handler(_request):
            raise asyncio.CancelledError()

        rail = HttpInputRail(
            base_url="http://guardrail-runtime:8000",
            service_token="test-runtime-service-token",
            timeout_ms=3000,
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(asyncio.CancelledError):
            await rail.evaluate(
                message="取消测试",
                request_id="req_cancel",
                conversation_id="conv_cancel",
            )
        await rail.aclose()

    async def test_disabled_rail_is_explicit_and_does_not_call_network(self):
        result = await DisabledInputRail().evaluate(
            message="查询余额",
            request_id="req_off",
            conversation_id="conv_off",
        )

        self.assertEqual(result.decision, "allow")
        self.assertEqual(result.policy_version, "disabled")


class InputRailSettingsTests(unittest.TestCase):
    def test_settings_are_bounded_and_default_to_explicit_off(self):
        with patch.dict(os.environ, {}, clear=True):
            defaults = RuntimeSettings.from_env()
        self.assertEqual(defaults.input_rail_mode, "off")
        self.assertEqual(defaults.input_rail_timeout_ms, 30000)
        self.assertEqual(defaults.permit_ttl_seconds, 30)

        with patch.dict(os.environ, {
            "AI_BANK_INPUT_RAIL_URL": "http://guardrail-runtime:8000",
            "AI_BANK_INPUT_RAIL_MODE": "enforce",
            "AI_BANK_INPUT_RAIL_TIMEOUT_MS": "2500",
            "AI_BANK_PERMIT_TTL_SECONDS": "45",
        }, clear=True):
            configured = RuntimeSettings.from_env()
        self.assertEqual(configured.input_rail_url, "http://guardrail-runtime:8000")
        self.assertEqual(configured.input_rail_mode, "enforce")
        self.assertEqual(configured.input_rail_timeout_ms, 2500)
        self.assertEqual(configured.permit_ttl_seconds, 45)

        with patch.dict(os.environ, {
            "AI_BANK_INPUT_RAIL_MODE": "unknown",
            "AI_BANK_INPUT_RAIL_TIMEOUT_MS": "20",
            "AI_BANK_PERMIT_TTL_SECONDS": "999",
        }, clear=True):
            bounded = RuntimeSettings.from_env()
        self.assertEqual(bounded.input_rail_mode, "off")
        self.assertEqual(bounded.input_rail_timeout_ms, 30000)
        self.assertEqual(bounded.permit_ttl_seconds, 30)


if __name__ == "__main__":
    unittest.main()
