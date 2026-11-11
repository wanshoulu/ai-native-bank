import unittest

import httpx

from app.memory_write_gateway import MemoryWriteGateway, MemoryWriteGatewayError


class MemoryWriteGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_submits_batch_to_node_and_accepts_only_safe_projection(self):
        async def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer runtime-token-123456")
            self.assertEqual(request.url.path, "/api/ai-bank/v1/internal/agent-memory-commands")
            return httpx.Response(201, json={"data": {"schemaVersion": "1.0", "items": [{
                "candidateType": "canonical_service_episode", "predicate": "service_episode_summary",
                "status": "PERSISTED", "decision": "ADD", "governanceStatus": "active",
                "reason": "canonical_episode_appended", "policyVersion": "bank-memory-policy-v1", "attempts": 1,
            }]}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = MemoryWriteGateway(
            endpoint="http://node/api/ai-bank/v1/internal/agent-memory-commands",
            service_token="runtime-token-123456", client=client,
        )
        result = await gateway.submit({"agentRunId": "abr_1", "customerId": "cust_1", "candidates": [{}]})
        self.assertEqual(result[0]["status"], "PERSISTED")

    async def test_rejects_unknown_response_fields(self):
        async def handler(_request):
            return httpx.Response(201, json={"data": {"schemaVersion": "1.0", "items": [{
                "candidateType": "canonical_service_episode", "predicate": "service_episode_summary",
                "status": "PERSISTED", "decision": "ADD", "governanceStatus": "active",
                "reason": "ok", "policyVersion": "v1", "attempts": 1, "value": "secret",
            }]}})
        gateway = MemoryWriteGateway(endpoint="http://node/memory", service_token="runtime-token-123456", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with self.assertRaises(MemoryWriteGatewayError):
            await gateway.submit({"agentRunId": "abr_1", "customerId": "cust_1", "candidates": [{}]})
