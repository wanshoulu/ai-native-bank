#!/usr/bin/env python
"""Full end-to-end test of multi-intent recognition via HTTP API."""
import sys, os, json, asyncio, uuid
sys.path.insert(0, "/app")
os.environ.setdefault("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
os.environ.setdefault("AI_BANK_TUGRAPH_USER", "admin")
os.environ.setdefault("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")

from app.config import RuntimeSettings
from app.tugraph_source import load_v4_payload_from_tugraph
from app.v4_graph_adapter import V4GraphAdapter
from app.intent_shape import analyze_intent_shape
from app.service import recall_intent_constraints, _EvalProbeRequest, _EvalProbeCapability

settings = RuntimeSettings.from_env()
payload = load_v4_payload_from_tugraph(settings)
adapter = V4GraphAdapter(payload, __import__("pathlib").Path("."))

# Step 1: Verify shape assessment
shape = analyze_intent_shape("查余额，给张三转100")
print("=== SHAPE ===")
print(f"shape={shape.shape}, authoritative={shape.authoritative}, segments={shape.segments}, expected_task_count={shape.expected_task_count}")

# Step 2: Verify constraints
caps = list(adapter.all_capability_ids)
request = _EvalProbeRequest(
    message="查余额，给张三转100",
    page_context={},
    plannable_capabilities=[_EvalProbeCapability(id=c) for c in caps],
)
constraints = recall_intent_constraints(request, adapter, None, shape)
print("\n=== CONSTRAINTS ===")
print(f"count={len(constraints)}")
for c in constraints[:4]:
    print(f"  {c.intent_id}: required_shape={c.required_shape}, expected_task_count={c.expected_task_count}, constraint={c.constraint}, shape_authoritative={c.shape_authoritative}, score={c.score}")

# Step 3: Full HTTP API test
import httpx

async def test_api():
    # Build a minimal RuntimeAgentRunRequest
    body = {
        "agentRunId": f"test-{uuid.uuid4().hex[:12]}",
        "conversationId": f"conv-{uuid.uuid4().hex[:12]}",
        "requestId": f"req-{uuid.uuid4().hex[:12]}",
        "traceId": f"trace-{uuid.uuid4().hex[:12]}",
        "customerId": "test-customer-001",
        "actorType": "agent",
        "message": "查余额，给张三转100",
        "pageContext": {},
        "conversationContext": {},
        "safetyContext": {},
        "executableCapabilities": [
            {"id": c, "title": c.split(".")[-1], "description": "capability", "exposure": "model-callable", "sideEffect": "idempotent", "riskLevel": "L0", "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"}, "possibleErrors": [], "readiness": {"status": "ready", "environment": "production", "blockers": []}}
            for c in [
                "bank.account.list", "bank.account.balance.get", "bank.account.detail.get",
                "bank.account.transactions.list", "bank.credit-card.list", "bank.credit-card.bill.get",
                "bank.payee.list", "bank.payee.validate", "bank.transfer.order.create", "bank.transfer.status.get",
            ]
        ],
        "plannableCapabilities": [
            {"id": c, "title": c.split(".")[-1], "description": "capability", "exposure": "model-callable", "sideEffect": "idempotent", "riskLevel": "L0", "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"}, "possibleErrors": [], "readiness": {"status": "ready", "environment": "production", "blockers": []}}
            for c in [
                "bank.account.list", "bank.account.balance.get", "bank.account.detail.get",
                "bank.account.transactions.list", "bank.credit-card.list", "bank.credit-card.bill.get",
                "bank.payee.list", "bank.payee.validate", "bank.transfer.order.create", "bank.transfer.status.get",
            ]
        ],
    }
    token = settings.runtime_token
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=200) as client:
        resp = await client.post(
            "http://127.0.0.1:18120/v1/agent-runs",
            json=body,
            headers=headers,
        )
        print(f"\n=== HTTP API RESPONSE ===")
        print(f"status={resp.status_code}")
        if resp.status_code == 201:
            data = resp.json()
            # Look for intent shape and task count
            envelope = data.get("envelope", data)
            tasks = envelope.get("tasks", [])
            print(f"tasks_count={len(tasks)}")
            for t in tasks:
                print(f"  task: intentId={t.get('intentId')}, description={t.get('description', '')[:60]}")
            shape_info = envelope.get("intentShape", envelope.get("intent_shape", {}))
            if shape_info:
                print(f"intentShape={shape_info}")
            # Print full response for debugging
            print(f"\n--- FULL RESPONSE (first 2000 chars) ---")
            print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
        else:
            print(f"error: {resp.text[:1000]}")

asyncio.run(test_api())
print("\n---DONE---")
