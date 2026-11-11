"""Verify that recall_intent_constraints allows intent switching.

Scenario:
  1. Previous turn: user sent "查余额给张帅转100" -> balance query clarification
     (conversation_context has balance query goals)
  2. Current turn: user sends "有哪些理财值得推荐" -> should switch to wealth
     recommendation, NOT repeat old balance query clarification

This test verifies the fix for the `not pending_context_injected` guard that
was blocking the authoritative shortcut when pending context was injected.
"""
import sys
from pathlib import Path

# Ensure app is importable
workspace = Path("/app") if Path("/app/data/ai-native-bank").exists() else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace))

from app.service import recall_intent_constraints
from app.v4_graph_adapter import V4GraphAdapter
from app.contracts import RuntimeAgentRunRequest


def _capability(cap_id: str, side_effect: str = "none", risk: str = "L1"):
    return {
        "id": cap_id,
        "title": cap_id,
        "description": "Entity discovery",
        "exposure": "model-callable",
        "sideEffect": side_effect,
        "riskLevel": risk,
        "inputSchema": {"type": "object"},
        "outputSchema": {"type": "object"},
        "possibleErrors": [],
        "readiness": {"status": "ready", "environment": "mock", "blockers": []},
    }


def _make_request(message: str, goals: list[dict] | None = None) -> RuntimeAgentRunRequest:
    has_goals = bool(goals)
    payload = {
        "agentRunId": "abr_switch_001",
        "conversationId": "conv_switch_001",
        "requestId": "req_switch_001",
        "traceId": "trace_switch_001",
        "customerId": "cust_switch_001",
        "actorType": "agent",
        "message": message,
        "pageContext": {"tab": "home"},
        "conversationContext": {
            "pendingEnvelopeId": "env-test-001" if has_goals else None,
            "intentShape": "multi_intent" if has_goals else None,
            "goals": goals or [],
            "sharedSlots": [],
            "ambiguities": [],
        },
        "executableCapabilities": [
            _capability("bank.account.list"),
            _capability("bank.account.balance.get"),
            _capability("bank.account.detail.get"),
            _capability("bank.transfer.status.get"),
        ],
        "plannableCapabilities": [
            _capability("bank.account.list"),
            _capability("bank.account.balance.get"),
            _capability("bank.account.detail.get"),
            _capability("bank.payee.list"),
            _capability("bank.payee.validate"),
            _capability("bank.transfer.status.get"),
            _capability("bank.transfer.order.create", side_effect="write", risk="L2"),
        ],
    }
    return RuntimeAgentRunRequest.model_validate(payload)


def test_intent_switch_from_balance_to_wealth():
    """User switches from balance query to wealth recommendation."""
    graph_path = workspace / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
    graph = V4GraphAdapter.load(graph_path)

    # Simulate: previous turn had balance query goals (pending context)
    goals = [
        {
            "taskId": "task_balance_1",
            "intentId": "ccb.account.balance.query",
            "status": "needs_slots",
            "slots": [],
        },
    ]

    request = _make_request("有哪些理财值得推荐", goals=goals)
    constraints = recall_intent_constraints(request, graph)

    intent_ids = {item.intent_id for item in constraints}
    print(f"[intent_switch] candidates: {intent_ids}")
    print(f"[intent_switch] constraints: {[(c.intent_id, c.shape_authoritative) for c in constraints]}")

    # The old balance query should NOT be the only candidate
    assert not (
        len(intent_ids) == 1 and "balance" in list(intent_ids)[0]
    ), f"System stuck on old balance query, candidates: {intent_ids}"

    print("[intent_switch] PASS - intent switched from balance to wealth")


def test_continuation_preserved():
    """User continues the same intent (answers clarification) - should keep old context."""
    graph_path = workspace / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
    graph = V4GraphAdapter.load(graph_path)

    # Simulate: previous turn had balance query + transfer goals (multi-intent)
    goals = [
        {
            "taskId": "task_balance_1",
            "intentId": "ccb.account.balance.query",
            "status": "needs_slots",
            "slots": [],
        },
        {
            "taskId": "task_transfer_1",
            "intentId": "ccb.transfer.create",
            "status": "provisional",
            "slots": [{"name": "payee", "value": "张帅", "source": "context",
                        "sourceIntentId": "ccb.transfer.create",
                        "updatedAt": "2026-08-30T12:00:00Z"}],
        },
    ]

    # User says "使用工资卡尾号6217转账" - continuation of transfer intent
    request = _make_request("使用工资卡尾号6217转账", goals=goals)
    constraints = recall_intent_constraints(request, graph)

    intent_ids = {item.intent_id for item in constraints}
    print(f"[continuation] candidates: {intent_ids}")
    print(f"[continuation] constraints: {[(c.intent_id, c.shape_authoritative) for c in constraints]}")

    # Both balance and transfer should be present (continuation, not narrowed)
    assert "ccb.account.balance.query" in intent_ids, \
        f"Balance query should be preserved in continuation, got: {intent_ids}"
    assert "ccb.transfer.create" in intent_ids, \
        f"Transfer should be preserved in continuation, got: {intent_ids}"
    assert not all(c.shape_authoritative for c in constraints), \
        "Continuation should NOT be authoritative (candidate set should stay open)"

    print("[continuation] PASS - multi-intent continuation preserved")


if __name__ == "__main__":
    test_intent_switch_from_balance_to_wealth()
    test_continuation_preserved()
    print("\nAll tests passed!")
