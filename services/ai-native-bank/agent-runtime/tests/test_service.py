import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.contracts import (
    BusinessWorkflowResumeRequest,
    CapabilityProjection,
    IntentTask,
    IntentTaskEnvelope,
    RuntimeAgentRunRequest,
    SlotValue,
)
from app.business_loop import (
    AUTHORITATIVE_NODE_WORKFLOW_KEY,
    TodoSnapshotItem,
    business_workflow_id,
)
from app.business_orchestrator import BusinessLoopInvariantError, plan_guided_goal
from app.todo_ledger import TodoLedger, TodoLedgerSnapshot
from app.faq import FaqError, MockFaqRepository
from app.domain_adapters import transfer as transfer_adapter
from app.intent_graph import IntentGraphRepository
from app.intent_shape import analyze_intent_shape
from app.node_gateway import AgentCapabilityInvoker
from app.memory_runtime import MemoryRecallResult
from app.rails import validate_intent_constraints
from app.service import (
    AgentContractError,
    DualDeepAgentService,
    _apply_slot_contracts,
    _authoritative_node_todo_restore,
    _guided_entry_submission,
    _guided_credit_card_repayment_facts,
    _guided_obligation_confirmation_plan,
    _guided_obligation_summary,
    _parse_envelope_submission,
    _parse_memory_variables,
    _reanchor_retained_goals,
    _retain_graph_execution_context,
    _successful_result_requires_clarification,
    _should_restore_business_workflow,
    recall_intent_constraints,
)
from app.slot_contract import SlotContractEvaluator
from app.user_result import project_user_result
from tests.test_contracts import (
    envelope_payload,
    recommendation_focus_payload,
    request_payload,
)


class GuidedEntrySubmissionTests(unittest.TestCase):
    def test_guided_credit_card_entry_seeds_only_server_owned_identifiers(self):
        workspace = (
            Path("/app") if Path("/app/data/ai-native-bank").exists()
            else Path(__file__).resolve().parents[4]
        )
        graph = IntentGraphRepository.load(
            workspace / "data" / "ai-native-bank" / "intent-graph"
            / "graph.ccb.intents.v1.0.0.json"
        )
        payload = request_payload()
        payload["entryGoal"] = {
            "goalId": "goal_nba_card_repayment_001",
            "scenarioId": "credit-card-repayment",
            "scenarioVersion": "1.0.0",
            "recommendationId": "nba_card_repayment_001",
            "targetIntentId": "ccb.home.repayment.repay",
            "mode": "guided",
            "contextRefs": ["credit_global_8891", "acct_salary_6217"],
            "workflowTemplateId": "credit-card-repayment-v1",
            "trustedInputs": {
                "cardId": "credit_global_8891",
                "fromAccountId": "acct_salary_6217",
            },
        }

        submission = _guided_entry_submission(
            RuntimeAgentRunRequest.model_validate(payload), graph
        )

        self.assertEqual(submission["tasks"][0]["slots"], {
            "cardId": {
                "value": "credit_global_8891", "source": "context", "confidence": 1,
            },
            "fromAccountId": {
                "value": "acct_salary_6217", "source": "context", "confidence": 1,
            },
        })
        self.assertNotIn("amountMinor", submission["tasks"][0]["slots"])

    def test_guided_credit_card_facts_require_matching_bill_account_and_balance(self):
        facts = _guided_credit_card_repayment_facts([
            {"capability_id": "bank.credit-card.bill.get", "output": {
                "cardId": "credit_global_8891", "productName": "龙卡全球支付卡",
                "maskedNo": "****8891", "billStatus": "PAYMENT_DUE",
                "outstanding": {"amountMinor": 1422000},
                "minimumPayment": {"amountMinor": 142200}, "dueDay": 18,
            }},
            {"capability_id": "bank.account.list", "output": {"accounts": [{
                "id": "acct_salary_6217", "name": "工资卡", "maskedNo": "****6217",
                "status": "ACTIVE",
            }]}},
            {"capability_id": "bank.account.balance.get", "output": {
                "accountId": "acct_salary_6217", "available": {"amountMinor": 2854036},
            }},
        ], {"cardId": "credit_global_8891", "fromAccountId": "acct_salary_6217"})

        self.assertEqual(facts["outstanding_minor"], 1422000)
        self.assertEqual(facts["minimum_minor"], 142200)
        self.assertEqual(facts["account_label"], "工资卡 · 尾号6217")

    def test_guided_credit_card_facts_treat_a_zero_settled_bill_as_a_business_result(self):
        facts = _guided_credit_card_repayment_facts([
            {"capability_id": "bank.credit-card.bill.get", "output": {
                "cardId": "credit_global_8891", "productName": "龙卡全球支付卡",
                "maskedNo": "****8891", "billStatus": "SETTLED",
                "outstanding": {"amountMinor": 0},
                "minimumPayment": {"amountMinor": 0}, "dueDay": 18,
            }},
            {"capability_id": "bank.account.list", "output": {"accounts": [{
                "id": "acct_salary_6217", "name": "工资卡", "maskedNo": "****6217",
                "status": "ACTIVE",
            }]}},
            {"capability_id": "bank.account.balance.get", "output": {
                "accountId": "acct_salary_6217", "available": {"amountMinor": 1442036},
            }},
        ], {"cardId": "credit_global_8891", "fromAccountId": "acct_salary_6217"})

        self.assertEqual(facts["bill_state"], "settled")
        self.assertEqual(facts["outstanding_minor"], 0)
        self.assertEqual(facts["minimum_minor"], 0)
    def test_server_guided_entry_builds_a_deterministic_skill_bound_envelope(self):
        workspace = (
            Path("/app") if Path("/app/data/ai-native-bank").exists()
            else Path(__file__).resolve().parents[4]
        )
        graph = IntentGraphRepository.load(
            workspace / "data" / "ai-native-bank" / "intent-graph"
            / "graph.ccb.intents.v1.0.0.json"
        )
        payload = request_payload()
        payload["message"] = "任意不可信文本都不能替换服务端目标"
        payload["entryGoal"] = {
            "goalId": "goal_nba_salary_001",
            "scenarioId": "salary-monthly-plan",
            "scenarioVersion": "1.0.0",
            "recommendationId": "nba_salary_001",
            "targetIntentId": "ccb.cashflow.salary-plan",
            "mode": "guided",
            "contextRefs": ["salary_event_20260903"],
            "workflowTemplateId": "salary-allocation-v1",
        }
        request = RuntimeAgentRunRequest.model_validate(payload)

        first = _guided_entry_submission(request, graph)
        second = _guided_entry_submission(request, graph)

        self.assertEqual(first, second)
        self.assertEqual(first["intent_shape"], "single_intent")
        self.assertEqual(len(first["tasks"]), 1)
        self.assertEqual(
            first["tasks"][0]["intent_id"], "ccb.cashflow.salary-plan"
        )
        self.assertEqual(first["tasks"][0]["execution_readiness"], "ready")

    def test_guided_obligation_projection_is_confirmation_only(self):
        payload = request_payload()
        payload["entryGoal"] = {
            "goalId": "goal_nba_obligation_001",
            "scenarioId": "obligation-repayment",
            "scenarioVersion": "1.0.0",
            "recommendationId": "nba_obligation_001",
            "targetIntentId": "ccb.obligation.repay-all",
            "mode": "guided",
            "contextRefs": ["card_001", "utility_001"],
            "workflowTemplateId": "obligation-repayment-v1",
        }
        request = RuntimeAgentRunRequest.model_validate(payload)
        workspace = (
            Path("/app") if Path("/app/data/ai-native-bank").exists()
            else Path(__file__).resolve().parents[4]
        )
        graph = IntentGraphRepository.load(
            workspace / "data" / "ai-native-bank" / "intent-graph"
            / "graph.ccb.intents.v1.0.0.json"
        )
        envelope = IntentTaskEnvelope.model_validate(
            _guided_entry_submission(request, graph)
        )
        snapshot = {
            "accountId": "acct_salary_6217",
            "items": [
                {"id": "card_001", "type": "credit_card", "amount": {"amountMinor": 386000}, "executionStatus": "available"},
                {"id": "utility_001", "type": "utility", "amount": {"amountMinor": 50000}, "executionStatus": "available"},
            ],
        }
        guided = plan_guided_goal(request.entry_goal, snapshot)

        plan = _guided_obligation_confirmation_plan(envelope, guided)
        summary = _guided_obligation_summary(guided, snapshot)

        self.assertEqual(plan.status, "planned")
        self.assertEqual(len(plan.steps), 2)
        self.assertTrue(all(item.requires_user_confirmation for item in plan.steps))
        self.assertEqual(
            [item.step_id for item in plan.steps],
            ["step_obligation_1", "step_obligation_2"],
        )
        self.assertTrue(
            all(envelope.tasks[0].task_id not in item.step_id for item in plan.steps)
        )
        self.assertEqual(summary["taskCount"], 2)
        self.assertEqual(summary["totalAmountMinor"], 436000)
        self.assertIn("¥3860.00", summary["items"][0])
        self.assertIn("确认后才会执行实际扣款", summary["description"])

    def test_guided_spending_plan_requires_and_binds_the_trusted_period(self):
        payload = request_payload()
        payload["entryGoal"] = {
            "goalId": "goal_nba_spending_001",
            "scenarioId": "spending-explanation",
            "scenarioVersion": "1.0.0",
            "recommendationId": "nba_spending_001",
            "targetIntentId": "ccb.cashflow.spending-explain",
            "mode": "guided",
            "contextRefs": ["signal:input.period"],
            "workflowTemplateId": "spending-explanation-v1",
            "trustedInputs": {"period": "2026-08"},
        }
        goal = RuntimeAgentRunRequest.model_validate(payload).entry_goal

        plan = plan_guided_goal(goal)

        self.assertEqual(
            plan.todos[0].input_bindings, {"period": "2026-08"}
        )
        payload["entryGoal"]["trustedInputs"]["period"] = "../secrets"
        with self.assertRaisesRegex(
            BusinessLoopInvariantError, "period is unavailable"
        ):
            plan_guided_goal(
                RuntimeAgentRunRequest.model_validate(payload).entry_goal
            )


class ParseMemoryVariablesTests(unittest.TestCase):
    def test_extracts_valid_kv_pairs(self):
        raw = {"memory_variables": {
            "selected_account": "acct_salary_6217",
            "last_query_date": "2026-09-06",
        }}
        result = _parse_memory_variables(raw)
        self.assertEqual(result, {
            "selected_account": "acct_salary_6217",
            "last_query_date": "2026-09-06",
        })

    def test_returns_empty_for_none_input(self):
        self.assertEqual(_parse_memory_variables(None), {})

    def test_returns_empty_when_field_missing(self):
        self.assertEqual(_parse_memory_variables({"other_field": 1}), {})

    def test_strips_variable_names(self):
        raw = {"memory_variables": {"  selected_account  ": "acct_001"}}
        result = _parse_memory_variables(raw)
        self.assertEqual(result, {"selected_account": "acct_001"})

    def test_rejects_non_string_values(self):
        raw = {"memory_variables": {"count": 42, "valid": "yes"}}
        result = _parse_memory_variables(raw)
        self.assertEqual(result, {"valid": "yes"})

    def test_rejects_oversized_name(self):
        raw = {"memory_variables": {"x" * 129: "value"}}
        self.assertEqual(_parse_memory_variables(raw), {})

    def test_rejects_oversized_value(self):
        raw = {"memory_variables": {"key": "v" * 501}}
        self.assertEqual(_parse_memory_variables(raw), {})

    def test_rejects_more_than_twenty_variables(self):
        raw = {"memory_variables": {f"key_{i}": str(i) for i in range(21)}}
        self.assertEqual(_parse_memory_variables(raw), {})


class RecommendationFocusIntentTests(unittest.TestCase):
    def test_focus_authoritatively_scopes_an_elliptical_follow_up(self):
        workspace = (
            Path("/app") if Path("/app/data/ai-native-bank").exists()
            else Path(__file__).resolve().parents[4]
        )
        graph = IntentGraphRepository.load(
            workspace / "data" / "ai-native-bank" / "intent-graph"
            / "graph.ccb.intents.v1.0.0.json"
        )
        payload = request_payload()
        payload["message"] = "为什么待还这么多"
        payload["conversationFocus"] = recommendation_focus_payload()
        payload["plannableCapabilities"].append({
            "id": "bank.cashflow.salary-plan.get",
            "title": "工资资金安排",
            "description": "查询工资资金安排",
            "exposure": "model-callable",
            "sideEffect": "none",
            "riskLevel": "L1",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "possibleErrors": [],
            "readiness": {"status": "ready", "environment": "mock", "blockers": []},
        })
        request = RuntimeAgentRunRequest.model_validate(payload)

        constraints = recall_intent_constraints(request, graph)

        self.assertEqual(len(constraints), 1)
        self.assertEqual(constraints[0].intent_id, "ccb.cashflow.salary-plan")
        self.assertTrue(constraints[0].shape_authoritative)
        self.assertIn("server_recommendation_focus", constraints[0].evidence)


class FakeRunner:
    def __init__(self, result):
        self.results = result if isinstance(result, list) else [result]
        self.inputs = []
        self.session_ids = []

    async def run(self, payload, *, session_id):
        self.inputs.append(payload)
        self.session_ids.append(session_id)
        result = self.results[min(len(self.inputs) - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(payload)
        return result


class FakeMemoryGateway:
    def __init__(self, user_result, task_results=None, task_policy_version="bank-memory-intent-v1"):
        self.user_result = user_result
        self.task_results = task_results or {}
        self.task_policy_version = task_policy_version
        self.user_calls = []
        self.task_calls = []

    async def recall_user_context(self, **kwargs):
        self.user_calls.append(kwargs)
        return self.user_result

    async def recall(self, **kwargs):
        self.task_calls.append(kwargs)
        return self.task_results.get(kwargs["intent_id"])

class ContextCheckingRunner(FakeRunner):
    def __init__(self, result, invoker):
        super().__init__(result)
        self.invoker = invoker
        self.contexts = []

    async def run(self, payload, *, session_id):
        self.contexts.append(self.invoker.current_context())
        return await super().run(payload, session_id=session_id)


class ToolCallingRunner(FakeRunner):
    def __init__(self, result, invoker, calls):
        super().__init__(result)
        self.invoker = invoker
        self.calls = calls

    async def run(self, payload, *, session_id):
        for capability_id, input_value in self.calls:
            await self.invoker.invoke_read_capability(capability_id, input_value)
        return await super().run(payload, session_id=session_id)


class FirstRunToolCallingRunner(FakeRunner):
    def __init__(self, result, invoker, calls):
        super().__init__(result)
        self.invoker = invoker
        self.calls = calls

    async def run(self, payload, *, session_id):
        if not self.inputs:
            for capability_id, input_value in self.calls:
                await self.invoker.invoke_read_capability(capability_id, input_value)
        return await super().run(payload, session_id=session_id)


class TodoSnapshotRunner(FakeRunner):
    def __init__(self, result, todo_ids):
        super().__init__(result)
        self.todo_ids = todo_ids
        self.snapshot_session_ids = []

    def todo_snapshot(self, session_id):
        self.snapshot_session_ids.append(session_id)
        return tuple(
            TodoSnapshotItem(id=item, status="pending", depends_on=[])
            for item in self.todo_ids
        )


class AuthorizedToolCallingRunner(TodoSnapshotRunner):
    def __init__(self, result, invoker, todo_ids):
        super().__init__(result, todo_ids)
        self.invoker = invoker

    async def run(self, payload, *, session_id):
        grant = payload["confirmation_resume"]["grants"][0]
        await self.invoker.invoke_authorized_capability(
            grant["grantId"], grant["taskId"]
        )
        return await super().run(payload, session_id=session_id)


class NativeLoopToolCallingRunner(FirstRunToolCallingRunner):
    def __init__(self, result, invoker, calls, todo_items):
        super().__init__(result, invoker, calls)
        self.todo_items = tuple(todo_items)

    def load_todo_ledger(self, session_id, revision=1):
        return TodoLedgerSnapshot(
            revision=revision,
            digest=("a" * 63) + str(min(revision, 9)),
            items=self.todo_items,
        )

    def todo_snapshot(self, session_id):
        return self.todo_items


class NativeClarificationResumeRunner(FakeRunner):
    def __init__(
        self, invoker, workflow_id, initial_items, resumed_items,
        covered_task_ids=None, stale_revisions=False,
    ):
        super().__init__(None)
        self.invoker = invoker
        self.workflow_id = workflow_id
        self.initial_items = tuple(initial_items)
        self.resumed_items = tuple(resumed_items)
        self.covered_task_ids = covered_task_ids or ["task_zhang", "task_li"]
        self.stale_revisions = stale_revisions

    def restore_todo_ledger(self, session_id, revision, items):
        restored = tuple(
            TodoSnapshotItem(
                id=item["id"], status=item["status"], depends_on=item["dependsOn"]
            )
            for item in items
        )
        return TodoLedgerSnapshot(
            revision=revision, digest=TodoLedger.digest(restored), items=restored
        )

    def load_todo_ledger(self, session_id, revision=1):
        return TodoLedgerSnapshot(
            revision=revision,
            digest=TodoLedger.digest(self.resumed_items),
            items=self.resumed_items,
        )

    async def run(self, payload, *, session_id):
        self.inputs.append(payload)
        self.session_ids.append(session_id)
        await self.invoker.invoke_read_capability("bank.account.list", {})
        await self.invoker.invoke_read_capability("bank.payee.list", {})
        await self.invoker.invoke_read_capability(
            "bank.payee.validate", {"payeeId": "payee_li_5678"}
        )
        return {
            "schema_version": "2.0",
            "business_workflow_id": payload["business_workflow_id"],
            "workflow_revision": (
                1 if self.stale_revisions
                else payload["workflow_control"]["next_workflow_revision"]
            ),
            "status": "awaiting_confirmation",
            "covered_task_ids": self.covered_task_ids,
            "active_goal_task_id": "task_li",
            "active_todo_id": "task_li",
            "todo_revision": (
                1 if self.stale_revisions
                else payload["workflow_control"]["expected_todo_revision"] + 1
            ),
            "next_action": "confirm_transfer",
        }


class NativeGraphActionResumeRunner(FakeRunner):
    """Return a native suspension while platform slot resolution finishes."""

    def __init__(self, workflow_id, items, task_id):
        super().__init__(None)
        self.workflow_id = workflow_id
        self.items = tuple(items)
        self.task_id = task_id

    def restore_todo_ledger(self, session_id, revision, items):
        restored = tuple(
            TodoSnapshotItem(
                id=item["id"], status=item["status"], depends_on=item["dependsOn"]
            )
            for item in items
        )
        return TodoLedgerSnapshot(
            revision=revision, digest=TodoLedger.digest(restored), items=restored
        )

    def load_todo_ledger(self, session_id, revision=1):
        return TodoLedgerSnapshot(
            revision=revision,
            digest=TodoLedger.digest(self.items),
            items=self.items,
        )

    async def run(self, payload, *, session_id):
        self.inputs.append(payload)
        self.session_ids.append(session_id)
        return {
            "schema_version": "2.0",
            "business_workflow_id": payload["business_workflow_id"],
            "workflow_revision": payload["workflow_control"]["next_workflow_revision"],
            "status": "awaiting_clarification",
            "covered_task_ids": [self.task_id],
            "active_goal_task_id": self.task_id,
            "active_todo_id": self.task_id,
            "todo_revision": payload["workflow_control"]["expected_todo_revision"] + 1,
            "next_action": "collect_required_slots",
        }


class NativeAuthorizedTerminalRunner(FakeRunner):
    """Simulate a final native Todo completing before the model response fails."""

    def __init__(
        self, invoker, initial_items, resumed_items, result, *, invoke_grant=True
    ):
        super().__init__(result)
        self.invoker = invoker
        self.initial_items = tuple(initial_items)
        self.resumed_items = tuple(resumed_items)
        self.invoke_grant = invoke_grant

    def restore_todo_ledger(self, session_id, revision, items):
        restored = tuple(
            TodoSnapshotItem(
                id=item["id"], status=item["status"], depends_on=item["dependsOn"]
            )
            for item in items
        )
        return TodoLedgerSnapshot(
            revision=revision, digest=TodoLedger.digest(restored), items=restored
        )

    def load_todo_ledger(self, session_id, revision=1):
        return TodoLedgerSnapshot(
            revision=revision,
            digest=TodoLedger.digest(self.resumed_items),
            items=self.resumed_items,
        )

    async def run(self, payload, *, session_id):
        self.inputs.append(payload)
        self.session_ids.append(session_id)
        if self.invoke_grant:
            grant = payload["confirmation_resume"]["grants"][0]
            await self.invoker.invoke_authorized_capability(
                grant["grantId"], grant["taskId"]
            )
        result = self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class RestoredNativeRunRunner(FakeRunner):
    """Exercise an ordinary chat turn that continues a persisted workflow."""

    def __init__(self, workflow_id, resumed_items):
        super().__init__(None)
        self.workflow_id = workflow_id
        self.resumed_items = tuple(resumed_items)
        self.restored = None
        self.restore_events = []

    def restore_todo_ledger(self, session_id, revision, items):
        self.restore_events.append("node_todo_restored")
        restored = tuple(
            TodoSnapshotItem(
                id=item["id"], status=item["status"], depends_on=item["dependsOn"]
            )
            for item in items
        )
        self.restored = TodoLedgerSnapshot(
            revision=revision, digest=TodoLedger.digest(restored), items=restored
        )
        return self.restored

    def load_todo_ledger(self, session_id, revision=1):
        return TodoLedgerSnapshot(
            revision=revision,
            digest=TodoLedger.digest(self.resumed_items),
            items=self.resumed_items,
        )

    async def run(self, payload, *, session_id):
        self.restore_events.append("openjiuwen_session_restored")
        restore = payload.pop(AUTHORITATIVE_NODE_WORKFLOW_KEY, None)
        if restore is not None:
            self.restore_todo_ledger(
                session_id,
                restore["todoRevision"],
                restore["todoSnapshot"],
            )
        self.inputs.append(payload)
        self.session_ids.append(session_id)
        control = payload["workflow_control"]
        return {
            "schema_version": "2.0",
            "business_workflow_id": self.workflow_id,
            "workflow_revision": control["next_workflow_revision"],
            "status": "awaiting_confirmation",
            "covered_task_ids": ["task_transfer"],
            "active_goal_task_id": "task_transfer",
            "active_todo_id": "create_transfer_order",
            "todo_revision": control["expected_todo_revision"] + 1,
            "next_action": "confirm_transfer",
        }


class CrossConversationNativeResumeRunner(FakeRunner):
    """Model a native workflow whose Goal survives conversation rebinding."""

    def __init__(self, workflow_id, items):
        super().__init__(None)
        self.workflow_id = workflow_id
        self.items = tuple(items)
        self.restored_revisions = []

    def restore_todo_ledger(self, session_id, revision, items):
        restored = tuple(
            TodoSnapshotItem(
                id=item["id"],
                status=item["status"],
                depends_on=item["dependsOn"],
            )
            for item in items
        )
        self.restored_revisions.append((revision, restored))
        return TodoLedgerSnapshot(
            revision=revision,
            digest=TodoLedger.digest(restored),
            items=restored,
        )

    def load_todo_ledger(self, session_id, revision=1):
        return TodoLedgerSnapshot(
            revision=revision,
            digest=TodoLedger.digest(self.items),
            items=self.items,
        )

    async def run(self, payload, *, session_id):
        runtime_payload = dict(payload)
        restore = runtime_payload.pop(AUTHORITATIVE_NODE_WORKFLOW_KEY)
        self.restore_todo_ledger(
            session_id,
            restore["todoRevision"],
            restore["todoSnapshot"],
        )
        self.inputs.append(runtime_payload)
        self.session_ids.append(session_id)
        control = runtime_payload["workflow_control"]
        return {
            "schema_version": "2.0",
            "business_workflow_id": self.workflow_id,
            "workflow_revision": control["next_workflow_revision"],
            "status": "awaiting_clarification",
            "covered_task_ids": ["task_transfer"],
            "active_goal_task_id": "task_transfer",
            "active_todo_id": "task_transfer",
            "todo_revision": control["expected_todo_revision"] + 1,
            "next_action": "collect_transfer_slots",
        }


def plan_payload(capability_id="bank.transfer.order.create"):
    return {
        "schema_version": "1.0",
        "business_agent_run_id": "bar_001",
        "delegation_id": "dlg_model_supplied",
        "status": "planned",
        "steps": [
            {
                "step_id": "step_001",
                "task_id": "task_001",
                "capability_id": capability_id,
                "input_bindings": {},
                "requires_user_confirmation": False,
                "depends_on": [],
            }
        ],
    }


def balance_capability_payload(capability_id, *, side_effect="none"):
    return {
        "id": capability_id,
        "title": capability_id,
        "description": "只读账户能力",
        "exposure": "model-callable",
        "sideEffect": side_effect,
        "riskLevel": "L1",
        "inputSchema": {"type": "object"},
        "outputSchema": {"type": "object"},
        "possibleErrors": [],
        "readiness": {"status": "ready", "environment": "test", "blockers": []},
    }


def balance_request_payload(*, include_list=True):
    payload = request_payload()
    payload["message"] = "查一下工资卡余额"
    payload["executableCapabilities"] = [
        balance_capability_payload("bank.account.detail.get"),
        balance_capability_payload("bank.account.balance.get"),
    ]
    payload["plannableCapabilities"] = [
        balance_capability_payload("bank.account.detail.get"),
        balance_capability_payload("bank.account.balance.get"),
    ]
    if include_list:
        payload["executableCapabilities"].insert(
            0, balance_capability_payload("bank.account.list")
        )
        payload["plannableCapabilities"].insert(
            0, balance_capability_payload("bank.account.list")
        )
    return payload


def balance_envelope_payload():
    payload = envelope_payload()
    payload["tasks"][0].update(
        {
            "task_id": "task_balance",
            "intent_id": "ccb.account.balance.query",
            "action": "QUERY",
            "business_object": "account_balance",
            "execution_readiness": "needs_slots",
            "risk_level": "L1",
        }
    )
    return payload


def wealth_purchase_envelope_payload():
    return envelope_payload(tasks=[{
        "task_id": "task_wealth_purchase",
        "intent_id": "ccb.wealth.dragon-money.buy",
        "action": "DO",
        "business_object": "wealth_order",
        "slots": {
            "productName": {
                "value": "建信理财天天利（龙钱宝）现金管理类产品10号",
                "source": "user_explicit",
                "confidence": 1,
            },
        },
        "dependencies": [],
        "execution_readiness": "needs_slots",
        "risk_level": "L2",
    }])


TWO_ACTIVE_ACCOUNTS = [
    {
        "id": "acct_salary_6217",
        "name": "工资卡",
        "maskedNo": "****6217",
        "status": "ACTIVE",
    },
    {
        "id": "acct_savings_8032",
        "name": "储蓄卡",
        "maskedNo": "****8032",
        "status": "ACTIVE",
    },
]


def faq_task_payload(task_id="task_faq", *, intent_id="ccb.home.transfer.faq"):
    return {
        "task_id": task_id,
        "intent_id": intent_id,
        "action": "HELP",
        "business_object": "transfer",
        "slots": {},
        "dependencies": [],
        "execution_readiness": "ready",
        "risk_level": "L0",
    }


def navigation_plan_payload():
    return {
        "schema_version": "1.0",
        "business_agent_run_id": "bar_nav",
        "delegation_id": "dlg_model",
        "status": "navigation_only",
        "steps": [],
    }


def non_task_envelope_payload():
    return envelope_payload("non_task", [])


def clarification_plan_payload():
    return {
        "schema_version": "1.0",
        "business_agent_run_id": "bar_001",
        "delegation_id": "dlg_model_supplied",
        "status": "clarification_required",
        "steps": [],
    }


def balance_discovery_plan_payload():
    return {
        "schema_version": "1.0",
        "business_agent_run_id": "bar_001",
        "delegation_id": "dlg_model_supplied",
        "status": "planned",
        "steps": [
            {
                "step_id": "step_accounts",
                "task_id": "task_balance",
                "capability_id": "bank.account.list",
                "input_bindings": {},
                "requires_user_confirmation": False,
                "depends_on": [],
            },
            {
                "step_id": "step_balance",
                "task_id": "task_balance",
                "capability_id": "bank.account.balance.get",
                "input_bindings": {"accountId": {"fromStep": "step_accounts"}},
                "requires_user_confirmation": False,
                "depends_on": ["step_accounts"],
            },
        ],
    }


class TransferAmbiguityDerivationTests(unittest.TestCase):
    """域不变量：一个金额对应多个转账任务且无分摊标记 → 每人/合计未定。"""

    @staticmethod
    def _task(task_id, amount=None):
        slots = {}
        if amount is not None:
            slots["amount"] = SlotValue.model_validate(
                {"value": amount, "source": "user_explicit", "confidence": 1},
            )
        return IntentTask.model_validate({
            "task_id": task_id,
            "intent_id": "ccb.transfer.create",
            "action": "DO",
            "business_object": "transfer_order",
            "slots": slots,
            "dependencies": [],
            "execution_readiness": "needs_slots",
            "risk_level": "L2",
        })

    def test_equal_amounts_without_markers_derive_amount_scope(self):
        derived = transfer_adapter.derive_ambiguities(
            tasks=[self._task("a", "100"), self._task("b", "100")],
            message="查余额，给张三李四转100",
        )
        self.assertEqual([item.type for item in derived], ["amount_scope"])
        self.assertEqual(derived[0].task_ids, ["a", "b"])
        self.assertEqual(derived[0].options, ["per_task", "total"])

    def test_distribution_marker_suppresses_derivation(self):
        self.assertEqual(transfer_adapter.derive_ambiguities(
            tasks=[self._task("a", "100"), self._task("b", "100")],
            message="给他们每人转100",
        ), [])

    def test_total_marker_suppresses_derivation(self):
        self.assertEqual(transfer_adapter.derive_ambiguities(
            tasks=[self._task("a", "100"), self._task("b", "100")],
            message="总共转100",
        ), [])

    def test_unequal_amounts_are_explicit_per_task(self):
        self.assertEqual(transfer_adapter.derive_ambiguities(
            tasks=[self._task("a", "100"), self._task("b", "200")],
            message="给张三转100给李四转200",
        ), [])

    def test_unresolved_amounts_fall_back_to_normal_slot_clarification(self):
        self.assertEqual(transfer_adapter.derive_ambiguities(
            tasks=[self._task("a"), self._task("b")],
            message="给张三李四转账",
        ), [])

    def test_context_inherited_amounts_never_rederive(self):
        inherited = self._task("a")
        inherited = inherited.model_copy(update={
            "slots": {"amount": SlotValue.model_validate(
                {"value": "100", "source": "context", "confidence": 1},
            )},
        })
        second = inherited.model_copy(update={"task_id": "b"})
        self.assertEqual(transfer_adapter.derive_ambiguities(
            tasks=[inherited, second],
            message="使用工资卡尾号6217转账",
        ), [])

    def test_shared_amount_derives_when_task_amounts_missing(self):
        derived = transfer_adapter.derive_ambiguities(
            tasks=[self._task("a"), self._task("b")],
            message="给张三李四转100",
            shared_slots={"amount": SlotValue.model_validate(
                {"value": "100", "source": "user_explicit", "confidence": 1},
            )},
        )
        self.assertEqual(derived[0].task_ids, ["a", "b"])

    def test_single_task_never_derives(self):
        self.assertEqual(transfer_adapter.derive_ambiguities(
            tasks=[self._task("a", "100")],
            message="给张三转100",
        ), [])

    @staticmethod
    def _scope_ambiguity(task_ids=("a", "b")):
        from app.contracts import IntentAmbiguity

        return IntentAmbiguity.model_validate({
            "type": "amount_scope",
            "task_ids": list(task_ids),
            "options": ["per_task", "total"],
        })

    def test_total_scope_message_classification_priority(self):
        self.assertEqual(
            transfer_adapter.message_amount_scope("两人合计100元"), "total"
        )
        self.assertEqual(transfer_adapter.message_amount_scope("每人100元"), "per_task")
        # 合计标记优先：同现时「总共200，每人100」声明的是总额校验口径。
        self.assertEqual(
            transfer_adapter.message_amount_scope("总共200，每人100"), "total"
        )
        self.assertIsNone(transfer_adapter.message_amount_scope("给张三转100"))

    def test_total_scope_resolves_ambiguity_and_withholds_amounts(self):
        ambiguity = self._scope_ambiguity()
        # 合计口径一经声明即消解：金额槽位由扣留机制失效，交常规缺槽
        # 澄清逐笔追问（不自动均分），不再以继承金额误判消解完成。
        self.assertTrue(transfer_adapter.ambiguity_is_resolved(
            ambiguity,
            context_ambiguities=(ambiguity,),
            message="两人合计100元",
            tasks=[self._task("a", "100"), self._task("b", "100")],
        ))
        # 分摊口径仍要求歧义引用的任务金额已落到 amountMinor。
        self.assertFalse(transfer_adapter.ambiguity_is_resolved(
            ambiguity,
            context_ambiguities=(ambiguity,),
            message="每人100元",
            tasks=[self._task("a", "100"), self._task("b", "100")],
        ))
        for name in ("amount", "amountMinor"):
            self.assertTrue(transfer_adapter.withhold_scope_inherited_amount_slot(
                name=name,
                context_ambiguities=(ambiguity,),
                message="两人合计100元",
            ))
        self.assertFalse(transfer_adapter.withhold_scope_inherited_amount_slot(
            name="amount", context_ambiguities=(ambiguity,), message="每人100元",
        ))
        self.assertFalse(transfer_adapter.withhold_scope_inherited_amount_slot(
            name="amount", context_ambiguities=(), message="两人合计100元",
        ))
        self.assertFalse(transfer_adapter.withhold_scope_inherited_amount_slot(
            name="fromAccount", context_ambiguities=(ambiguity,), message="两人合计100元",
        ))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_graph_query_retains_accepted_semantic_inputs_for_continuation(self):
        workspace = (
            Path("/app") if Path("/app/data/ai-native-bank").exists()
            else Path(__file__).resolve().parents[4]
        )
        graph = IntentGraphRepository.load(
            workspace / "data" / "ai-native-bank" / "intent-graph"
            / "graph.ccb.intents.v1.0.0.json"
        )
        envelope = IntentTaskEnvelope.model_validate(envelope_payload(tasks=[{
            "task_id": "task_wealth_advisory",
            "intent_id": "ccb.wealth.advisor.recommend",
            "action": "QUERY",
            "business_object": "wealth_advice",
            "slots": {},
            "dependencies": [],
            "execution_readiness": "ready",
            "risk_level": "L1",
        }]))
        contract = graph.candidate_for_intent(
            "ccb.wealth.advisor.recommend", score=0, evidence=["test"]
        ).slot_contract
        self.assertIsNotNone(contract)
        evaluator = SlotContractEvaluator()
        initial = evaluator.evaluate(
            contract=contract,
            current_slots={},
            conversation_slots={},
        )

        retained_envelope, retained_evaluations = _retain_graph_execution_context(
            envelope,
            {"task_wealth_advisory": initial},
            graph,
            evaluator,
            [{
                "capability_id": "bank.wealth.advisory.context.get",
                "input": {
                    "investmentGoal": "steady_growth",
                    "horizonMonths": 12,
                },
                "output": {
                    "recommendationStatus": "preferences_required",
                    "missingPreferenceFields": ["plannedAmountMinor"],
                },
            }],
        )

        task = retained_envelope.tasks[0]
        self.assertEqual(task.slots["investmentGoal"].value, "steady_growth")
        self.assertEqual(task.slots["horizonMonths"].value, 12)
        self.assertEqual(task.slots["investmentGoal"].source, "context")
        self.assertNotIn("plannedAmount", task.slots)
        self.assertEqual(
            retained_evaluations[task.task_id].semantic_values["investmentGoal"],
            "steady_growth",
        )

    def test_structured_capability_result_marks_only_missing_input_outcome_as_continuable(self):
        self.assertTrue(_successful_result_requires_clarification([{
            "capability_id": "bank.wealth.advisory.context.get",
            "input": {},
            "output": {
                "recommendationStatus": "preferences_required",
                "missingPreferenceFields": ["plannedAmountMinor"],
                "requiredActions": [{"code": "PROVIDE_PLANNED_AMOUNT"}],
            },
        }]))
        self.assertFalse(_successful_result_requires_clarification([{
            "capability_id": "bank.wealth.advisory.context.get",
            "input": {"plannedAmountMinor": 1000000},
            "output": {
                "recommendationStatus": "ready",
                "missingPreferenceFields": [],
                "requiredActions": [],
            },
        }]))

    def test_structured_clarification_is_not_a_completed_result_prefix(self):
        invocations = [{
            "capability_id": "bank.wealth.advisory.context.get",
            "input": {},
            "output": {
                "recommendationStatus": "preferences_required",
                "missingPreferenceFields": ["plannedAmountMinor"],
                "requiredActions": [{
                    "code": "PROVIDE_PLANNED_AMOUNT",
                    "message": "我这次计划投入10000元，请继续推荐",
                }],
            },
        }]

        self.assertTrue(_successful_result_requires_clarification(invocations))
        projected = project_user_result(invocations)
        self.assertIsNotNone(projected)
        self.assertEqual(projected.kind, "wealth_advisory_preferences_required")

    async def test_task_memory_is_bound_to_task_and_intent_with_value_free_summary(self):
        def pack(predicate, value):
            display = (
                ("月度生活预算", "¥6,200 / 月")
                if predicate == "monthly_living_budget"
                else ("风险偏好", "平衡型")
            )
            return {
                "facts": [{
                    "memId": "mem_test_001", "content": "test memory content",
                    "predicate": predicate, "value": value,
                    "memoryType": "semantic_memory", "sensitivity": "L2",
                    "safeLabel": display[0], "safeValueSummary": display[1],
                    "validFrom": "2026-09-01T00:00:00Z",
                    "sourceAuthority": "user_explicit",
                    "requiresCoreRefresh": True, "requiresConfirmation": True,
                }],
                "conflicts": [],
                "omissions": {"expired": 0, "policy": 0},
            }

        memory = FakeMemoryGateway(
            MemoryRecallResult(outcome="not_applicable"),
            task_results={
                "ccb.cashflow.salary-plan": pack("monthly_living_budget", 6200),
                "ccb.wealth.fund.overview": pack("risk_preference", "balanced"),
            },
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(None), business_runner=FakeRunner(None),
            memory_gateway=memory,
        )
        tasks = []
        for task_id, intent_id in (
            ("task_salary", "ccb.cashflow.salary-plan"),
            ("task_wealth", "ccb.wealth.fund.overview"),
        ):
            item = balance_envelope_payload()["tasks"][0]
            item.update({"task_id": task_id, "intent_id": intent_id})
            tasks.append(item)
        envelope = IntentTaskEnvelope.model_validate(
            envelope_payload("multi_intent", tasks)
        )
        request = RuntimeAgentRunRequest.model_validate(balance_request_payload())

        result = await service._recall_task_memories(request, envelope)

        self.assertEqual(result.outcome, "applied")
        self.assertEqual({(item["taskId"], item["intentId"]) for item in result.context_packs}, {
            ("task_salary", "ccb.cashflow.salary-plan"),
            ("task_wealth", "ccb.wealth.fund.overview"),
        })
        self.assertTrue(all("summary" in item for item in result.context_packs))
        self.assertTrue(all("intentLabel" in item for item in result.context_packs))
        detail = result.trace_detail(scope="task")
        self.assertEqual(detail["detailStatus"], "available")
        self.assertEqual(sum(len(group["items"]) for group in detail["groups"]), 2)
        self.assertNotIn("6200", json.dumps(result.trace_metrics()))

    async def test_user_agent_receives_memory_before_intent_submission(self):
        memory = FakeMemoryGateway(MemoryRecallResult(
            outcome="applied",
            context_packs=(
                {"purpose": "service_personalization", "contextPack": {"facts": []}},
                {"purpose": "service_continuity", "contextPack": {"facts": []}},
            ),
            fact_count=2,
            policy_version="bank-memory-user-v1",
        ))
        user = FakeRunner(balance_envelope_payload())
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(balance_discovery_plan_payload()),
            memory_gateway=memory,
        )
        request = RuntimeAgentRunRequest.model_validate(balance_request_payload())

        events = [event async for event in service.stream(request)]

        self.assertEqual(len(user.inputs[0]["user_memory_context_packs"]), 2)
        recalled = next(
            event for event in events
            if event.stage == "memory.user_context_recalled"
        )
        self.assertEqual(recalled.summary.metrics["outcome"], "applied")
        self.assertEqual(recalled.summary.memory_recall.scope, "user")
        self.assertEqual(recalled.summary.memory_recall.detail_status, "redacted")

    async def test_degraded_user_memory_does_not_block_intent_reasoning(self):
        memory = FakeMemoryGateway(MemoryRecallResult(outcome="degraded"))
        user = FakeRunner(balance_envelope_payload())
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(balance_discovery_plan_payload()),
            memory_gateway=memory,
        )
        request = RuntimeAgentRunRequest.model_validate(balance_request_payload())

        events = [event async for event in service.stream(request)]

        self.assertEqual(len(user.inputs), 1)
        self.assertNotIn("user_memory_context_packs", user.inputs[0])
        recalled = next(
            event for event in events
            if event.stage == "memory.user_context_recalled"
        )
        self.assertEqual(recalled.summary.metrics["outcome"], "degraded")
        self.assertEqual(recalled.summary.memory_recall.detail_status, "degraded")

    async def test_trusted_single_query_result_overrides_failed_model_lifecycle(self):
        def handler(http_request):
            body = json.loads(http_request.content) if http_request.content else {}
            if http_request.url.path.endswith("agent-workflows"):
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": body["businessWorkflowId"],
                    "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            self.assertEqual(
                body["capabilityId"],
                "bank.cashflow.spending-analysis.get",
            )
            return httpx.Response(200, json={"data": {
                "period": "2026-08",
                "comparisonPeriod": "2026-07",
                "current": {"netExpenseMinor": 190000},
                "previous": {"netExpenseMinor": 90000},
                "excluded": {
                    "internalTransferMinor": 0,
                    "refundMinor": 0,
                },
                "findings": [{
                    "kind": "fact",
                    "code": "NET_EXPENSE_TOTAL",
                    "text": "本月净支出高于上月",
                    "sourceRefs": ["txn_001"],
                }],
                "generatedAt": "2026-09-05T08:00:00.000Z",
            }, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        capability = balance_capability_payload(
            "bank.cashflow.spending-analysis.get"
        )
        request = request_payload()
        request.update({
            "message": "为什么八月钱花得快",
            "executableCapabilities": [capability],
            "plannableCapabilities": [capability],
        })
        envelope = envelope_payload(tasks=[{
            "task_id": "task_spending",
            "intent_id": "ccb.cashflow.spending-explain",
            "action": "QUERY",
            "business_object": "spending_analysis",
            "slots": {
                "period": {
                    "value": "2026-08",
                    "source": "user_explicit",
                    "confidence": 1,
                },
            },
            "dependencies": [],
            "execution_readiness": "ready",
            "risk_level": "L1",
        }])

        def outcome(payload):
            return {
                "schema_version": "2.0",
                "business_workflow_id": payload["business_workflow_id"],
                "workflow_revision": 1,
                "status": "failed",
                "covered_task_ids": ["task_spending"],
                "active_goal_task_id": "task_spending",
                "active_todo_id": "task_spending",
                "todo_revision": 1,
                "next_action": "model_lifecycle_failed",
            }

        business = NativeLoopToolCallingRunner(
            outcome,
            invoker,
            [("bank.cashflow.spending-analysis.get", {"period": "2026-08"})],
            [TodoSnapshotItem(
                id="task_spending", status="in_progress", depends_on=[]
            )],
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=business,
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "succeeded", result.model_dump())
        self.assertEqual(result.user_result.kind, "spending_explanation")

    async def test_conversational_obligation_loop_discovers_before_batch_confirmation(self):
        captured = []

        def handler(http_request):
            body = json.loads(http_request.content) if http_request.content else {}
            captured.append((http_request.url.path, body))
            if http_request.url.path.endswith("agent-workflows"):
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": body["businessWorkflowId"],
                    "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            if http_request.url.path.endswith("agent-action-checkpoints"):
                return httpx.Response(201, json={"data": {
                    "checkpointId": "acp_" + body["capabilityId"].replace(".", "_"),
                    "capabilityId": body["capabilityId"],
                    "confirmationToken": "o" * 43,
                    "expiresAt": "2026-09-05T16:00:00.000Z",
                    "summary": {
                        "title": "确认待还事项",
                        "description": "实际扣款前逐项确认",
                        "riskLevel": "L2",
                    },
                }, "meta": {}})
            self.assertEqual(body["capabilityId"], "bank.obligation.list")
            return httpx.Response(200, json={"data": {
                "accountId": "acct_salary_6217",
                "items": [
                    {
                        "id": "card_bill_202609",
                        "type": "credit_card",
                        "amount": {"amountMinor": 386000},
                        "executionStatus": "available",
                    },
                    {
                        "id": "utility_due_202609",
                        "type": "utility",
                        "amount": {"amountMinor": 50000},
                        "executionStatus": "available",
                    },
                ],
            }, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        request = request_payload()
        read_capabilities = [
            balance_capability_payload(capability_id)
            for capability_id in (
                "bank.obligation.list",
                "bank.loan.list",
                "bank.loan.detail.get",
                "bank.loan.repayment.list",
                "bank.loan.precalculate.create",
                "bank.utility.bill.list",
            )
        ]
        action_capabilities = []
        for capability_id in (
            "bank.credit-card.repayment.create",
            "bank.loan.repayment.create",
            "bank.utility.payment.create",
        ):
            capability = balance_capability_payload(
                capability_id, side_effect="state-change"
            )
            capability["exposure"] = "user-action"
            capability["riskLevel"] = "L2"
            action_capabilities.append(capability)
        request.update({
            "message": "把该还的钱都处理掉",
            "executableCapabilities": read_capabilities,
            "plannableCapabilities": [*read_capabilities, *action_capabilities],
        })
        envelope = envelope_payload(tasks=[{
            "task_id": "task_obligation",
            "intent_id": "ccb.obligation.repay-all",
            "action": "DO",
            "business_object": "obligation_plan",
            "slots": {},
            "dependencies": [],
            "execution_readiness": "ready",
            "risk_level": "L2",
        }])

        def outcome(payload):
            return {
                "schema_version": "2.0",
                "business_workflow_id": payload["business_workflow_id"],
                "workflow_revision": 1,
                "status": "awaiting_clarification",
                "covered_task_ids": ["task_obligation"],
                "active_goal_task_id": "task_obligation",
                "active_todo_id": "task_obligation",
                "todo_revision": 1,
                "next_action": "inspect_obligations",
            }

        business = NativeLoopToolCallingRunner(
            outcome,
            invoker,
            [],
            [TodoSnapshotItem(
                id="task_obligation", status="in_progress", depends_on=[]
            )],
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=business,
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "awaiting_confirmation")
        self.assertEqual(len(result.confirmation.confirmations), 2)
        self.assertEqual(result.confirmation.summary.task_count, 2)
        self.assertEqual(
            [body["capabilityId"] for path, body in captured
             if path.endswith("agent-capability-invocations")],
            ["bank.obligation.list"],
        )

    def test_pre_business_clarification_context_does_not_restore_a_missing_workflow(self):
        payload = request_payload()
        payload["conversationContext"] = {
            "pendingEnvelopeId": "dlg_pre_business_clarification",
            "envelopeRevision": 1,
            "intentShape": "single_intent",
            "goals": [
                {
                    "taskId": "task_001",
                    "intentId": "ccb.transfer.create",
                    "status": "provisional",
                    "slots": [],
                },
            ],
            "sharedSlots": [],
            "ambiguities": [],
        }
        request = RuntimeAgentRunRequest.model_validate(payload)
        context = request.conversation_context
        envelope = _parse_envelope_submission(envelope_payload(), request)

        self.assertFalse(_should_restore_business_workflow(context, envelope))

        resumed = context.model_copy(
            update={
                "goals": [
                    context.goals[0].model_copy(update={"status": "ready"}),
                ]
            }
        )
        self.assertTrue(_should_restore_business_workflow(resumed, envelope))

    async def test_business_iteration_has_a_bounded_runtime(self):
        class QuickRunner:
            async def run(self, payload, *, session_id):
                return clarification_plan_payload()

        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=QuickRunner(),
        )

        with patch.dict(
            os.environ,
            {"AI_BANK_BUSINESS_ITERATION_TIMEOUT_SECONDS": "1"},
        ):
            with patch(
                "app.service.asyncio.wait_for", wraps=asyncio.wait_for
            ) as wait_for:
                await service._run_business(
                    {},
                    session_id="business:bounded-runtime",
                    agent_run_id="abr_bounded_runtime",
                    execution_allowlist=set(),
                )
        wait_for.assert_awaited_once()
        self.assertEqual(wait_for.await_args.kwargs["timeout"], 1.0)

    async def test_business_runner_receives_authoritative_runtime_capability_phase(self):
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=business,
        )

        await service._run_business(
            {
                "business_workflow_id": "bwf_phase_contract",
                "runtime_capability_phase": {
                    "task_id": "task_balance_query",
                    "readiness": "resolve",
                    "completion_policy": "return_after_allowed_call",
                },
            },
            session_id="business:phase-contract",
            agent_run_id="abr_phase_contract",
            execution_allowlist={"bank.account.list"},
        )

        self.assertEqual(
            business.inputs[0]["runtime_capability_allowlist"],
            ["bank.account.list"],
        )
        self.assertEqual(
            business.inputs[0]["runtime_capability_phase"],
            {
                "task_id": "task_balance_query",
                "readiness": "resolve",
                "completion_policy": "return_after_allowed_call",
            },
        )

    def test_new_intent_does_not_reuse_pending_workflow_envelope(self):
        payload = balance_request_payload()
        payload["conversationContext"] = {
            "pendingEnvelopeId": "dlg_pending_transfer",
            "envelopeRevision": 3,
            "intentShape": "same_intent_multi_task",
            "goals": [
                {
                    "taskId": "transfer_zhangsan",
                    "intentId": "ccb.transfer.create",
                    "status": "needs_slots",
                    "slots": [],
                },
                {
                    "taskId": "transfer_lisi",
                    "intentId": "ccb.transfer.create",
                    "status": "needs_slots",
                    "slots": [],
                },
            ],
            "sharedSlots": [],
            "ambiguities": [],
        }
        request = RuntimeAgentRunRequest.model_validate(payload)

        envelope = _parse_envelope_submission(
            balance_envelope_payload(), request
        )

        self.assertNotEqual(envelope.delegation_id, "dlg_pending_transfer")
        self.assertRegex(envelope.delegation_id, r"^dlg_[0-9a-f]{32}$")

    def test_same_pending_goal_scope_reuses_workflow_envelope(self):
        payload = request_payload()
        payload["conversationContext"] = {
            "pendingEnvelopeId": "dlg_pending_transfer",
            "envelopeRevision": 3,
            "intentShape": "single_intent",
            "goals": [{
                "taskId": "task_001",
                "intentId": "ccb.transfer.create",
                "status": "needs_slots",
                "slots": [],
            }],
            "sharedSlots": [],
            "ambiguities": [],
        }
        request = RuntimeAgentRunRequest.model_validate(payload)

        envelope = _parse_envelope_submission(envelope_payload(), request)

        self.assertEqual(envelope.delegation_id, "dlg_pending_transfer")

    def test_started_workflow_restores_only_when_current_envelope_matches_goal_scope(self):
        payload = request_payload()
        payload["conversationContext"] = {
            "pendingEnvelopeId": "dlg_pending_transfer",
            "envelopeRevision": 3,
            "intentShape": "single_intent",
            "goals": [{
                "taskId": "task_001",
                "intentId": "ccb.transfer.create",
                "status": "ready",
                "slots": [],
            }],
            "sharedSlots": [],
            "ambiguities": [],
        }
        request = RuntimeAgentRunRequest.model_validate(payload)
        same_scope = _parse_envelope_submission(envelope_payload(), request)
        different_scope = _parse_envelope_submission(
            balance_envelope_payload(), request
        )

        self.assertTrue(
            _should_restore_business_workflow(
                request.conversation_context, same_scope
            )
        )
        self.assertFalse(
            _should_restore_business_workflow(
                request.conversation_context, different_scope
            )
        )

    async def test_navigation_only_intent_bypasses_business_agent_and_returns_navigation(self):
        graph, faq_repository = self._faq_dependencies()
        business = FakeRunner(AssertionError("business agent must not start"))
        envelope = wealth_purchase_envelope_payload()
        envelope["tasks"][0].update({
            "intent_id": "ccb.wealth.dragon-money.auto-invest",
            "action": "CHANGE",
            "business_object": "auto_save",
            "slots": {},
            "execution_readiness": "navigation_only",
        })
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=business,
            intent_graph=graph,
            faq_repository=faq_repository,
        )
        payload = request_payload()
        payload.update({
            "message": "设置龙钱宝自动转入",
            "pageContext": {
                "tab": "wealth",
                "menuKey": "wealth.dragon-money",
            },
        })
        wealth_read_ids = (
            "bank.wealth.product.list",
            "bank.wealth.holding.list",
            "bank.wealth.transaction.list",
        )
        wealth_reads = [
            balance_capability_payload(capability_id)
            for capability_id in wealth_read_ids
        ]
        purchase = balance_capability_payload(
            "bank.wealth.purchase.create", side_effect="write"
        )
        purchase.update({"exposure": "user-action", "riskLevel": "L2"})
        redeem = balance_capability_payload(
            "bank.wealth.redeem.create", side_effect="write"
        )
        redeem.update({"exposure": "user-action", "riskLevel": "L2"})
        payload["executableCapabilities"].extend(wealth_reads)
        payload["plannableCapabilities"].extend(
            [*wealth_reads, purchase, redeem]
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(payload))

        self.assertEqual(result.status, "navigation_only")
        self.assertEqual(result.user_response.type, "navigation")
        self.assertEqual(result.user_response.actions[0].kind, "navigate")
        self.assertEqual(business.inputs, [])

    def test_corrupt_authoritative_node_todo_digest_fails_before_runtime_restore(self):
        workflow = {
            "todoRevision": 4,
            "todoSnapshot": [{
                "id": "task_transfer",
                "status": "in_progress",
                "dependsOn": [],
            }],
            "todoDigest": "0" * 64,
        }

        with self.assertRaisesRegex(
            AgentContractError,
            "^persisted workflow todo digest is invalid$",
        ):
            _authoritative_node_todo_restore(workflow)

    def test_authoritative_node_todo_restore_accepts_128_and_rejects_129(self):
        def workflow(count):
            items = tuple(
                TodoSnapshotItem(
                    id=f"task_{index:03d}",
                    status="pending",
                    depends_on=[],
                )
                for index in range(count)
            )
            return {
                "todoRevision": 7,
                "todoSnapshot": [{
                    "id": item.id,
                    "status": item.status,
                    "dependsOn": [],
                } for item in items],
                "todoDigest": TodoLedger.digest(items),
            }

        _instruction, snapshot = _authoritative_node_todo_restore(workflow(128))
        self.assertEqual(len(snapshot.items), 128)
        with self.assertRaisesRegex(
            AgentContractError,
            "^persisted workflow todo snapshot is invalid$",
        ):
            _authoritative_node_todo_restore(workflow(129))

    def test_natural_reply_migrates_legacy_platform_clarification_marker(self):
        items = (
            TodoSnapshotItem(
                id="wealth_recommend", status="completed", depends_on=[]
            ),
            TodoSnapshotItem(
                id="task_wealth:awaiting_clarification",
                status="in_progress",
                depends_on=[],
            ),
        )
        workflow = {
            "status": "awaiting_clarification",
            "delegatedTaskIds": ["task_wealth"],
            "activeGoalTaskId": "task_wealth",
            "activeTodoId": "task_wealth:awaiting_clarification",
            "todoRevision": 2,
            "todoSnapshot": [
                {
                    "id": item.id,
                    "status": item.status,
                    "dependsOn": list(item.depends_on),
                }
                for item in items
            ],
            "todoDigest": TodoLedger.digest(items),
        }

        instruction, snapshot = _authoritative_node_todo_restore(
            workflow,
            resume_user_reply=True,
        )

        self.assertEqual(
            [(item.id, item.status) for item in snapshot.items],
            [("wealth_recommend", "in_progress")],
        )
        self.assertEqual(instruction["todoSnapshot"], [{
            "id": "wealth_recommend",
            "status": "in_progress",
            "dependsOn": [],
        }])
        self.assertEqual(instruction["todoDigest"], snapshot.digest)

    def test_structured_native_clarification_todo_is_not_rewritten(self):
        items = (
            TodoSnapshotItem(
                id="collect_investment_amount",
                status="in_progress",
                depends_on=[],
            ),
        )
        workflow = {
            "status": "awaiting_clarification",
            "delegatedTaskIds": ["task_wealth"],
            "activeGoalTaskId": "task_wealth",
            "activeTodoId": "collect_investment_amount",
            "todoRevision": 2,
            "todoSnapshot": [{
                "id": items[0].id,
                "status": items[0].status,
                "dependsOn": [],
            }],
            "todoDigest": TodoLedger.digest(items),
        }

        _instruction, snapshot = _authoritative_node_todo_restore(
            workflow,
            resume_user_reply=True,
        )

        self.assertEqual(snapshot.items, items)

    async def test_invalid_node_todo_dependency_is_safe_and_never_reaches_runtime(self):
        secret = "sk-secret_marker_that_must_not_be_logged"
        workflow_id = "bwf_" + "c" * 24
        requests = []

        def handler(http_request):
            requests.append((http_request.method, str(http_request.url)))
            return httpx.Response(200, json={"data": {
                "businessWorkflowId": workflow_id,
                "status": "awaiting_clarification",
                "delegatedTaskIds": ["task_transfer"],
                "envelopeRevision": 1,
                "workflowRevision": 1,
                "todoRevision": 1,
                "todoSnapshot": [{
                    "id": "task_transfer",
                    "status": "in_progress",
                    "dependsOn": [{"token": secret}],
                }],
                "todoDigest": "0" * 64,
                "activeGoalTaskId": "task_transfer",
                "activeTodoId": "task_transfer",
                "executionPolicy": "sequential",
                "suspensionReason": "awaiting_clarification",
                "lastOutcome": {"status": "awaiting_clarification"},
                "pendingCheckpointIds": [],
            }, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        runner = CrossConversationNativeResumeRunner(
            workflow_id,
            (TodoSnapshotItem(
                id="task_transfer", status="in_progress", depends_on=[]
            ),),
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=runner,
            capability_invoker=invoker,
        )
        request = BusinessWorkflowResumeRequest.model_validate({
            "resumeKind": "clarification",
            "agentRunId": "abr_invalid_restore",
            "conversationId": "conv_invalid_restore",
            "customerId": "cust_demo_001",
            "taskId": "task_transfer",
            "grants": [],
            "clarificationMessage": "继续办理",
            "expectedWorkflowRevision": 1,
            "conversationContext": {
                "pendingIntentId": None,
                "slots": [],
                "pendingEnvelopeId": "dlg_invalid_restore",
                "envelopeRevision": 1,
                "intentShape": "single_intent",
                "goals": [{
                    "taskId": "task_transfer",
                    "intentId": "ccb.transfer.create",
                    "slots": [],
                    "status": "needs_slots",
                }],
                "sharedSlots": [],
                "ambiguities": [],
            },
            "executableCapabilityIds": [],
        })

        with self.assertLogs(
            "ai_native_bank.agent_runtime.service",
            level="WARNING",
        ) as logs:
            with self.assertRaisesRegex(
                AgentContractError,
                "^native business workflow restore failed$",
            ):
                await service.resume_business_workflow(workflow_id, request)

        self.assertNotIn(secret, "\n".join(logs.output))
        self.assertEqual(runner.session_ids, [])
        self.assertEqual(runner.restored_revisions, [])
        self.assertTrue(all(method == "GET" for method, _url in requests))

    async def test_resume_business_workflow_uses_customer_workflow_session_across_conversations(self):
        captured = []
        workflow_id = "bwf_" + "a" * 24

        def handler(http_request):
            body = json.loads(http_request.content)
            captured.append((http_request.url.path, body))
            if http_request.url.path.endswith("agent-workflows"):
                body = json.loads(http_request.content)
                return httpx.Response(201, json={
                    "data": {
                        "businessWorkflowId": workflow_id,
                        "status": body["status"],
                        "workflowRevision": body["workflowRevision"],
                        "todoRevision": body["todoRevision"],
                        "activeGoalTaskId": body["activeGoalTaskId"],
                    },
                    "meta": {},
                })
            return httpx.Response(200, json={
                "data": {
                    "grantId": "acg_one", "taskId": "task_loss",
                    "capabilityId": "bank.credit-card.loss-report.create",
                    "status": "SUCCEEDED", "result": {"status": "LOST"},
                },
                "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        runner = AuthorizedToolCallingRunner(
            clarification_plan_payload(), invoker, ["task_loss"],
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=runner,
            capability_invoker=invoker,
        )
        request = BusinessWorkflowResumeRequest.model_validate({
            "agentRunId": "abr_one", "conversationId": "conv_one",
            "customerId": "cust_demo_001", "checkpointId": "acp_one",
            "grants": [{
                "grantId": "acg_one", "taskId": "task_loss",
                "capabilityId": "bank.credit-card.loss-report.create",
                "expiresAt": "2026-08-31T10:05:00.000Z",
            }],
        })

        result = await service.resume_business_workflow(workflow_id, request)
        resumed_request = request.model_copy(update={
            "agent_run_id": "abr_two",
            "conversation_id": "conv_two",
        })
        resumed_result = await service.resume_business_workflow(
            workflow_id, resumed_request
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(resumed_result.status, "succeeded")
        self.assertEqual(runner.session_ids, [
            f"business:cust_demo_001:{workflow_id}",
            f"business:cust_demo_001:{workflow_id}",
        ])
        authorized = next(item for item in captured if item[0].endswith(
            "agent-authorized-capability-invocations"
        ))
        self.assertEqual(set(authorized[1]), {
            "agentRunId", "businessWorkflowId", "taskId", "grantId",
        })
        self.assertNotIn("input", authorized[1])

    async def test_native_workflow_rebind_keeps_session_goal_and_todo_revision(self):
        workflow_id = "bwf_" + "b" * 24
        todo_items = (
            TodoSnapshotItem(
                id="task_transfer",
                status="in_progress",
                depends_on=[],
            ),
        )
        workflow_state = {"workflowRevision": 1, "todoRevision": 1}
        workflow_writes = []

        def handler(http_request):
            body = json.loads(http_request.content) if http_request.content else None
            if http_request.method == "GET":
                return httpx.Response(200, json={"data": {
                    "businessWorkflowId": workflow_id,
                    "status": "awaiting_clarification",
                    "delegatedTaskIds": ["task_transfer"],
                    "envelopeRevision": 1,
                    "workflowRevision": workflow_state["workflowRevision"],
                    "todoRevision": workflow_state["todoRevision"],
                    "todoSnapshot": [{
                        "id": item.id,
                        "status": item.status,
                        "dependsOn": list(item.depends_on),
                    } for item in todo_items],
                    "todoDigest": TodoLedger.digest(todo_items),
                    "activeGoalTaskId": "task_transfer",
                    "activeTodoId": "task_transfer",
                    "executionPolicy": "sequential",
                    "suspensionReason": "awaiting_clarification",
                    "lastOutcome": {"status": "awaiting_clarification"},
                    "pendingCheckpointIds": [],
                }, "meta": {}})
            if http_request.url.path.endswith("agent-workflows"):
                workflow_writes.append(body)
                self.assertEqual(
                    body["expectedRevision"],
                    workflow_state["workflowRevision"],
                )
                workflow_state.update({
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                })
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": workflow_id,
                    "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            raise AssertionError(f"unexpected request: {http_request.method} {http_request.url}")

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        runner = CrossConversationNativeResumeRunner(workflow_id, todo_items)
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=runner,
            capability_invoker=invoker,
        )
        base_request = {
            "resumeKind": "clarification",
            "agentRunId": "abr_old",
            "conversationId": "conv_old",
            "customerId": "cust_demo_001",
            "taskId": "task_transfer",
            "grants": [],
            "clarificationMessage": "继续办理",
            "expectedWorkflowRevision": 1,
            "conversationContext": {
                "pendingIntentId": None,
                "slots": [],
                "pendingEnvelopeId": "dlg_old",
                "envelopeRevision": 1,
                "intentShape": "single_intent",
                "goals": [{
                    "taskId": "task_transfer",
                    "intentId": "ccb.transfer.create",
                    "slots": [],
                    "status": "needs_slots",
                }],
                "sharedSlots": [],
                "ambiguities": [],
            },
            "executableCapabilityIds": [],
        }

        first = await service.resume_business_workflow(
            workflow_id,
            BusinessWorkflowResumeRequest.model_validate(base_request),
        )
        rebound_request = dict(base_request)
        rebound_request.update({
            "agentRunId": "abr_new",
            "conversationId": "conv_new",
            "expectedWorkflowRevision": 2,
        })
        second = await service.resume_business_workflow(
            workflow_id,
            BusinessWorkflowResumeRequest.model_validate(rebound_request),
        )

        expected_session_id = f"business:cust_demo_001:{workflow_id}"
        self.assertEqual(runner.session_ids, [expected_session_id, expected_session_id])
        self.assertEqual([item[0] for item in runner.restored_revisions], [1, 2])
        self.assertEqual([item.workflow_revision for item in (first, second)], [2, 3])
        self.assertEqual(
            [item["todoRevision"] for item in workflow_writes],
            [2, 3],
        )
        self.assertEqual(
            [item["activeGoalTaskId"] for item in workflow_writes],
            ["task_transfer", "task_transfer"],
        )

    async def test_resume_uses_authorized_receipt_when_agent_omits_final_submission(self):
        workflow_id = "bwf_" + "c" * 24

        def handler(http_request):
            if http_request.url.path.endswith("agent-workflows"):
                body = json.loads(http_request.content)
                return httpx.Response(201, json={
                    "data": {
                        "businessWorkflowId": workflow_id,
                        "status": body["status"],
                        "workflowRevision": body["workflowRevision"],
                        "todoRevision": body["todoRevision"],
                        "activeGoalTaskId": body["activeGoalTaskId"],
                    },
                    "meta": {},
                })
            return httpx.Response(200, json={
                "data": {
                    "grantId": "acg_receipt", "taskId": "task_transfer",
                    "capabilityId": "bank.transfer.order.create",
                    "status": "SUCCEEDED", "result": {"receipt": {"status": "SUCCEEDED"}},
                },
                "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        runner = AuthorizedToolCallingRunner(None, invoker, ["task_transfer"])
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=runner,
            capability_invoker=invoker,
        )
        request = BusinessWorkflowResumeRequest.model_validate({
            "agentRunId": "abr_receipt", "conversationId": "conv_receipt",
            "customerId": "cust_demo_001", "checkpointId": "acp_receipt",
            "grants": [{
                "grantId": "acg_receipt", "taskId": "task_transfer",
                "capabilityId": "bank.transfer.order.create",
                "expiresAt": "2026-08-31T10:05:00.000Z",
            }],
        })

        result = await service.resume_business_workflow(workflow_id, request)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.receipts[0].status, "SUCCEEDED")

    async def test_native_final_confirmation_reconciles_committed_transfer(self):
        workflow_id = "bwf_" + "e" * 24
        initial_items = (
            TodoSnapshotItem(id="task_zhang", status="completed", depends_on=[]),
            TodoSnapshotItem(id="task_li", status="in_progress", depends_on=[]),
        )
        resumed_items = (
            TodoSnapshotItem(id="task_zhang", status="completed", depends_on=[]),
            TodoSnapshotItem(id="task_li", status="completed", depends_on=[]),
        )

        valid_terminal_outcome = {
            "schema_version": "2.0",
            "business_workflow_id": workflow_id,
            "workflow_revision": 5,
            "status": "completed",
            "covered_task_ids": ["task_zhang", "task_li"],
            "active_goal_task_id": None,
            "active_todo_id": None,
            "todo_revision": 5,
            "next_action": None,
        }
        for case, runner_result, case_initial_items, should_reconcile, invoke_grant in (
            ("missing_outcome", None, initial_items, True, True),
            (
                "post_commit_runner_error",
                RuntimeError("model response failed"),
                initial_items,
                True,
                True,
            ),
            (
                "post_commit_runner_error_missing_invocation",
                RuntimeError("model response failed"),
                initial_items,
                True,
                False,
            ),
            (
                "valid_outcome_missing_invocation",
                valid_terminal_outcome,
                initial_items,
                True,
                False,
            ),
            (
                "ungranted_goal_was_not_previously_terminal",
                None,
                (
                    TodoSnapshotItem(
                        id="task_zhang", status="in_progress", depends_on=[]
                    ),
                    TodoSnapshotItem(
                        id="task_li", status="in_progress", depends_on=[]
                    ),
                ),
                False,
                True,
            ),
        ):
            with self.subTest(case=case):
                captured = []

                def handler(http_request):
                    body = (
                        json.loads(http_request.content)
                        if http_request.content else None
                    )
                    captured.append((http_request.method, http_request.url.path, body))
                    if http_request.method == "GET":
                        return httpx.Response(200, json={"data": {
                            "businessWorkflowId": workflow_id,
                            "status": "awaiting_confirmation",
                            "delegatedTaskIds": ["task_zhang", "task_li"],
                            "envelopeRevision": 1,
                            "workflowRevision": 4,
                            "todoRevision": 4,
                            "todoSnapshot": [
                                {
                                    "id": item.id,
                                    "status": item.status,
                                    "dependsOn": list(item.depends_on),
                                }
                                for item in case_initial_items
                            ],
                            "todoDigest": TodoLedger.digest(case_initial_items),
                            "activeGoalTaskId": "task_li",
                            "activeTodoId": "task_li",
                            "executionPolicy": "sequential",
                            "suspensionReason": "awaiting_confirmation",
                            "lastOutcome": {"status": "awaiting_confirmation"},
                            "pendingCheckpointIds": ["acp_li"],
                        }, "meta": {}})
                    if http_request.url.path.endswith(
                        "agent-authorized-capability-invocations"
                    ):
                        return httpx.Response(200, json={"data": {
                            "grantId": "acg_li",
                            "taskId": "task_li",
                            "capabilityId": "bank.transfer.order.create",
                            "status": "SUCCEEDED",
                            "result": {
                                "receipt": {
                                    "status": "SUCCEEDED",
                                    "transactionId": "txn_mock_li",
                                }
                            },
                        }, "meta": {}})
                    if http_request.url.path.endswith("agent-workflows"):
                        return httpx.Response(201, json={"data": {
                            "businessWorkflowId": workflow_id,
                            "status": body["status"],
                            "workflowRevision": body["workflowRevision"],
                            "todoRevision": body["todoRevision"],
                            "activeGoalTaskId": body["activeGoalTaskId"],
                        }, "meta": {}})
                    return httpx.Response(404)

                invoker = AgentCapabilityInvoker(
                    endpoint="http://node.test/internal/agent-capability-invocations",
                    service_token="runtime-service-secret",
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                )
                self.addAsyncCleanup(invoker.close)
                runner = NativeAuthorizedTerminalRunner(
                    invoker, case_initial_items, resumed_items, runner_result,
                    invoke_grant=invoke_grant,
                )
                service = DualDeepAgentService(
                    user_runner=FakeRunner(envelope_payload()),
                    business_runner=runner,
                    capability_invoker=invoker,
                )
                goals = []
                for task_id, payee in (
                    ("task_zhang", "张三"),
                    ("task_li", "李四"),
                ):
                    goals.append({
                        "taskId": task_id,
                        "intentId": "ccb.transfer.create",
                        "status": "ready",
                        "slots": [
                            {
                                "name": "payee", "value": payee,
                                "source": "context",
                                "sourceIntentId": "ccb.transfer.create",
                                "updatedAt": "2026-08-31T10:00:00.000Z",
                            },
                            {
                                "name": "fromAccount", "value": "工资卡尾号6217",
                                "source": "context",
                                "sourceIntentId": "ccb.transfer.create",
                                "updatedAt": "2026-08-31T10:00:00.000Z",
                            },
                            {
                                "name": "amount", "value": 200,
                                "source": "context",
                                "sourceIntentId": "ccb.transfer.create",
                                "updatedAt": "2026-08-31T10:00:00.000Z",
                            },
                            {
                                "name": "currency", "value": "CNY",
                                "source": "context",
                                "sourceIntentId": "ccb.transfer.create",
                                "updatedAt": "2026-08-31T10:00:00.000Z",
                            },
                        ],
                    })
                request = BusinessWorkflowResumeRequest.model_validate({
                    "resumeKind": "confirmation",
                    "agentRunId": "abr_native_terminal",
                    "conversationId": "conv_native_terminal",
                    "customerId": "cust_demo_001",
                    "checkpointId": "acp_li",
                    "grants": [{
                        "grantId": "acg_li",
                        "taskId": "task_li",
                        "capabilityId": "bank.transfer.order.create",
                        "expiresAt": "2026-08-31T10:15:00.000Z",
                    }],
                    "expectedWorkflowRevision": 4,
                    "conversationContext": {
                        "pendingIntentId": None,
                        "slots": [],
                        "pendingEnvelopeId": "dlg_native_terminal",
                        "envelopeRevision": 1,
                        "intentShape": "same_intent_multi_task",
                        "goals": goals,
                        "sharedSlots": [],
                        "ambiguities": [],
                    },
                    "executableCapabilityIds": [],
                })

                if not should_reconcile:
                    with self.assertRaisesRegex(
                        Exception, "native business workflow resume failed"
                    ):
                        await service.resume_business_workflow(
                            workflow_id, request
                        )
                    workflow_writes = [
                        body for method, path, body in captured
                        if method == "POST" and path.endswith("agent-workflows")
                    ]
                    self.assertEqual(workflow_writes, [])
                    continue

                result = await service.resume_business_workflow(workflow_id, request)

                self.assertEqual(result.status, "succeeded")
                self.assertEqual(result.receipts[0].status, "SUCCEEDED")
                self.assertEqual(result.workflow_revision, 5)
                workflow_writes = [
                    body for method, path, body in captured
                    if method == "POST" and path.endswith("agent-workflows")
                ]
                self.assertEqual(workflow_writes[-1]["status"], "completed")
                self.assertEqual(workflow_writes[-1]["workflowRevision"], 5)
                self.assertEqual(workflow_writes[-1]["todoRevision"], 5)
                self.assertIsNone(workflow_writes[-1]["activeGoalTaskId"])
                self.assertEqual(workflow_writes[-1]["pendingCheckpointIds"], [])

    async def test_clarification_resume_reuses_business_session_and_creates_current_confirmation(self):
        workflow_id = "bwf_" + "d" * 24
        initial_items = (
            TodoSnapshotItem(id="task_zhang", status="completed", depends_on=[]),
            TodoSnapshotItem(id="task_li", status="in_progress", depends_on=[]),
        )
        captured = []

        def handler(http_request):
            body = json.loads(http_request.content) if http_request.content else None
            captured.append((http_request.method, http_request.url.path, body))
            if http_request.method == "GET":
                return httpx.Response(200, json={"data": {
                    "businessWorkflowId": workflow_id,
                    "status": "awaiting_clarification",
                    "delegatedTaskIds": ["task_zhang", "task_li"],
                    "envelopeRevision": 1, "workflowRevision": 1,
                    "todoRevision": 1,
                    "todoSnapshot": [
                        {"id": item.id, "status": item.status, "dependsOn": []}
                        for item in initial_items
                    ],
                    "todoDigest": TodoLedger.digest(initial_items),
                    "activeGoalTaskId": "task_li", "activeTodoId": "task_li",
                    "executionPolicy": "sequential",
                    "suspensionReason": "awaiting_clarification",
                    "lastOutcome": {"status": "awaiting_clarification"},
                    "pendingCheckpointIds": [],
                }, "meta": {}})
            if http_request.url.path.endswith("agent-action-checkpoints"):
                return httpx.Response(201, json={"data": {
                    "checkpointId": "acp_li_resume",
                    "capabilityId": "bank.transfer.order.create",
                    "confirmationToken": "r" * 43,
                    "expiresAt": "2026-08-31T10:05:00.000Z",
                    "summary": {
                        "title": "确认转账", "description": "向李四转账200.00元",
                        "riskLevel": "L2",
                    },
                }, "meta": {}})
            if http_request.url.path.endswith("agent-workflows"):
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": workflow_id, "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            outputs = {
                "bank.account.list": {"accounts": [{
                    "id": "acct_salary_6217", "name": "工资卡",
                    "maskedNo": "****6217", "status": "ACTIVE",
                }]},
                "bank.payee.list": {"payees": [{
                    "id": "payee_li_5678", "name": "李四",
                    "maskedNo": "****5678", "status": "ACTIVE",
                }]},
                "bank.payee.validate": {
                    "payeeId": "payee_li_5678", "valid": True,
                    "status": "ACTIVE", "checkedAt": "2026-08-31T10:00:00.000Z",
                },
            }
            return httpx.Response(200, json={
                "data": outputs[body["capabilityId"]], "meta": {}
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        runner = NativeClarificationResumeRunner(
            invoker, workflow_id, initial_items, initial_items,
            covered_task_ids=["task_li"],
            stale_revisions=True,
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=runner, capability_invoker=invoker,
        )
        request = BusinessWorkflowResumeRequest.model_validate({
            "resumeKind": "clarification",
            "agentRunId": "abr_multi", "conversationId": "conv_multi",
            "customerId": "cust_demo_001", "taskId": "task_li", "grants": [],
            "clarificationMessage": "使用工资卡尾号6217转账",
            "expectedWorkflowRevision": 1,
            "conversationContext": {
                "pendingIntentId": None, "slots": [],
                "pendingEnvelopeId": "dlg_multi", "envelopeRevision": 1,
                "intentShape": "same_intent_multi_task",
                "goals": [
                    {
                        "taskId": task_id, "intentId": "ccb.transfer.create",
                        "slots": [
                            {"name": "payee", "value": payee, "source": "context",
                             "sourceIntentId": "ccb.transfer.create", "updatedAt": "2026-08-31T10:00:00.000Z"},
                            {"name": "fromAccount", "value": "工资卡", "source": "context",
                             "sourceIntentId": "ccb.transfer.create", "updatedAt": "2026-08-31T10:00:00.000Z"},
                            {"name": "amount", "value": 200, "source": "context",
                             "sourceIntentId": "ccb.transfer.create", "updatedAt": "2026-08-31T10:00:00.000Z"},
                            {"name": "currency", "value": "CNY", "source": "context",
                             "sourceIntentId": "ccb.transfer.create", "updatedAt": "2026-08-31T10:00:00.000Z"},
                        ],
                        "status": "ready",
                    }
                    for task_id, payee in (("task_zhang", "张三"), ("task_li", "李四"))
                ],
                "sharedSlots": [], "ambiguities": [],
            },
            "executableCapabilityIds": [
                "bank.account.list", "bank.payee.list", "bank.payee.validate",
            ],
        })

        result = await service.resume_business_workflow(workflow_id, request)

        self.assertEqual(result.status, "awaiting_confirmation")
        self.assertEqual(result.active_goal_task_id, "task_li")
        self.assertEqual(result.task_progress.current, 2)
        self.assertEqual(result.task_progress.completed, 1)
        self.assertEqual(result.receipts, [])
        self.assertEqual(
            runner.session_ids,
            [f"business:cust_demo_001:{workflow_id}"],
        )
        self.assertIn("clarification_resume", runner.inputs[0])
        self.assertEqual(
            runner.inputs[0]["clarification_resume"]["message"],
            "使用工资卡尾号6217转账",
        )
        self.assertEqual(
            runner.inputs[0]["slot_resolution_context"]["source_message"],
            "使用工资卡尾号6217转账",
        )
        self.assertNotIn("confirmation_resume", runner.inputs[0])
        workflow_writes = [
            body for method, path, body in captured
            if method == "POST" and path.endswith("agent-workflows")
        ]
        self.assertEqual(
            workflow_writes[-1]["lastOutcome"]["covered_task_ids"],
            ["task_zhang", "task_li"],
        )
        self.assertEqual(workflow_writes[-1]["workflowRevision"], 2)
        self.assertEqual(workflow_writes[-1]["todoRevision"], 2)
        self.assertTrue(any(path.endswith("agent-action-checkpoints") for _, path, _ in captured))

    async def test_graph_action_resume_creates_wealth_purchase_confirmation_after_account_selection(self):
        workflow_id = "bwf_" + "e" * 24
        task_id = "task_wealth_purchase"
        initial_items = (
            TodoSnapshotItem(id=task_id, status="in_progress", depends_on=[]),
        )
        captured = []

        def handler(http_request):
            body = json.loads(http_request.content) if http_request.content else None
            captured.append((http_request.method, http_request.url.path, body))
            if http_request.method == "GET":
                return httpx.Response(200, json={"data": {
                    "businessWorkflowId": workflow_id,
                    "status": "awaiting_clarification",
                    "delegatedTaskIds": [task_id],
                    "envelopeRevision": 1,
                    "workflowRevision": 1,
                    "todoRevision": 1,
                    "todoSnapshot": [{
                        "id": task_id, "status": "in_progress", "dependsOn": [],
                    }],
                    "todoDigest": TodoLedger.digest(initial_items),
                    "activeGoalTaskId": task_id,
                    "activeTodoId": task_id,
                    "executionPolicy": "sequential",
                    "suspensionReason": "awaiting_clarification",
                    "lastOutcome": {"status": "awaiting_clarification"},
                    "pendingCheckpointIds": [],
                }, "meta": {}})
            if http_request.url.path.endswith("agent-action-checkpoints"):
                return httpx.Response(201, json={"data": {
                    "checkpointId": "acp_wealth_resume",
                    "capabilityId": "bank.wealth.purchase.create",
                    "confirmationToken": "w" * 43,
                    "expiresAt": "2026-09-07T11:05:00.000Z",
                    "summary": body["summary"],
                }, "meta": {}})
            if http_request.url.path.endswith("agent-workflows"):
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": workflow_id,
                    "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            outputs = {
                "bank.wealth.product.list": {"products": [{
                    "id": "prod_w_anxin",
                    "name": "乾元-安鑫（按日）开放式固定收益类理财产品",
                    "productCode": "GY-AX-OD-2024",
                    "status": "ON_SALE",
                    "riskLevel": "R2",
                    "category": "FIXED_INCOME_WEALTH",
                }]},
                "bank.account.list": {"accounts": [{
                    "id": "acct_salary_6217",
                    "name": "工资卡",
                    "maskedNo": "****6217",
                    "status": "ACTIVE",
                }]},
            }
            return httpx.Response(200, json={
                "data": outputs[body["capabilityId"]], "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        runner = NativeGraphActionResumeRunner(workflow_id, initial_items, task_id)
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=runner,
            capability_invoker=invoker,
        )
        request = BusinessWorkflowResumeRequest.model_validate({
            "resumeKind": "clarification",
            "agentRunId": "abr_wealth",
            "conversationId": "conv_wealth",
            "customerId": "cust_demo_001",
            "taskId": task_id,
            "grants": [],
            "clarificationMessage": "选择尾号6217的账户",
            "expectedWorkflowRevision": 1,
            "conversationContext": {
                "pendingIntentId": None,
                "slots": [],
                "pendingEnvelopeId": "dlg_wealth",
                "envelopeRevision": 1,
                "intentShape": "single_intent",
                "goals": [{
                    "taskId": task_id,
                    "intentId": "ccb.wealth.wealth-product.buy",
                    "status": "needs_slots",
                    "slots": [
                        {
                            "name": "productReference",
                            "value": "乾元-安鑫（按日）开放式固定收益类理财产品",
                            "source": "context",
                            "sourceIntentId": "ccb.wealth.wealth-product.buy",
                            "updatedAt": "2026-09-07T10:00:00.000Z",
                        },
                        {
                            "name": "amount",
                            "value": "10000",
                            "source": "context",
                            "sourceIntentId": "ccb.wealth.wealth-product.buy",
                            "updatedAt": "2026-09-07T10:00:00.000Z",
                        },
                    ],
                }],
                "sharedSlots": [],
                "ambiguities": [],
            },
            "executableCapabilityIds": [
                "bank.wealth.product.list",
                "bank.account.list",
                "bank.wealth.purchase.create",
            ],
        })

        result = await service.resume_business_workflow(workflow_id, request)

        self.assertEqual(result.status, "awaiting_confirmation")
        self.assertEqual(result.confirmation.capability_id, "bank.wealth.purchase.create")
        self.assertEqual(result.user_response.type, "confirmation")
        self.assertIn("乾元-安鑫", result.user_response.model_dump_json(by_alias=True))
        checkpoint = next(
            body for method, path, body in captured
            if method == "POST" and path.endswith("agent-action-checkpoints")
        )
        self.assertEqual(checkpoint["input"], {
            "productId": "prod_w_anxin",
            "accountId": "acct_salary_6217",
            "amountMinor": 1000000,
        })
        public_response = result.user_response.model_dump_json(by_alias=True)
        self.assertNotIn("prod_w_anxin", public_response)
        self.assertNotIn("acct_salary_6217", public_response)

    async def test_query_clarification_resume_returns_result_then_next_goal_without_slot_reuse(self):
        workflow_id = "bwf_" + "c" * 24
        initial_items = (
            TodoSnapshotItem(id="task_balance", status="in_progress", depends_on=[]),
            TodoSnapshotItem(id="task_transfer", status="pending", depends_on=[]),
        )
        resumed_items = (
            TodoSnapshotItem(id="task_balance", status="completed", depends_on=[]),
            TodoSnapshotItem(id="task_transfer", status="in_progress", depends_on=[]),
        )
        calls = []

        def handler(http_request):
            body = json.loads(http_request.content) if http_request.content else None
            if http_request.method == "GET":
                return httpx.Response(200, json={"data": {
                    "businessWorkflowId": workflow_id,
                    "status": "awaiting_clarification",
                    "delegatedTaskIds": ["task_balance", "task_transfer"],
                    "envelopeRevision": 1, "workflowRevision": 1,
                    "todoRevision": 1,
                    "todoSnapshot": [
                        {"id": item.id, "status": item.status, "dependsOn": []}
                        for item in initial_items
                    ],
                    "todoDigest": TodoLedger.digest(initial_items),
                    "activeGoalTaskId": "task_balance",
                    "activeTodoId": "task_balance",
                    "executionPolicy": "sequential",
                    "suspensionReason": "awaiting_clarification",
                    "lastOutcome": {"status": "awaiting_clarification"},
                    "pendingCheckpointIds": [],
                }, "meta": {}})
            if http_request.url.path.endswith("agent-workflows"):
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": workflow_id, "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            calls.append(body["capabilityId"])
            outputs = {
                "bank.account.list": {"accounts": TWO_ACTIVE_ACCOUNTS},
                "bank.account.balance.get": {
                    "accountId": "acct_salary_6217",
                    "available": {"formatted": "CNY 8,888.00"},
                    "ledger": {"formatted": "CNY 9,000.00"},
                },
                "bank.payee.list": {"payees": [{
                    "id": "payee_zhang_4321", "name": "\u5f20\u4e09",
                    "maskedNo": "****4321", "status": "ACTIVE",
                }]},
            }
            return httpx.Response(200, json={
                "data": outputs[body["capabilityId"]], "meta": {}
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)

        class QueryResumeRunner(FakeRunner):
            def __init__(self):
                super().__init__(None)
                self.run_count = 0

            def restore_todo_ledger(self, session_id, revision, items):
                return TodoLedgerSnapshot(
                    revision=revision,
                    digest=TodoLedger.digest(initial_items),
                    items=initial_items,
                )

            def load_todo_ledger(self, session_id, revision=1):
                items = resumed_items if self.run_count >= 1 else initial_items
                return TodoLedgerSnapshot(
                    revision=revision,
                    digest=TodoLedger.digest(items),
                    items=items,
                )

            async def run(self, payload, *, session_id):
                self.inputs.append(payload)
                self.session_ids.append(session_id)
                self.run_count += 1
                completion = payload["authoritative_task_completion"]
                if completion != {
                    "task_id": "task_balance",
                    "capability_id": "bank.account.balance.get",
                    "result_state": "succeeded",
                    "output_keys": ["accountId", "available", "ledger"],
                }:
                    raise AssertionError("authoritative completion is unsafe")
                raise TimeoutError("model stalled after authoritative Todo update")

        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=QueryResumeRunner(),
            capability_invoker=invoker,
        )
        request = BusinessWorkflowResumeRequest.model_validate({
            "resumeKind": "clarification",
            "agentRunId": "abr_query_resume",
            "conversationId": "conv_query_resume",
            "customerId": "cust_demo_001",
            "taskId": "task_balance",
            "clarificationMessage": "\u9009\u62e9\u5c3e\u53f76217\u7684\u8d26\u6237",
            "grants": [],
            "expectedWorkflowRevision": 1,
            "conversationContext": {
                "pendingIntentId": None, "slots": [],
                "pendingEnvelopeId": "dlg_query_resume", "envelopeRevision": 1,
                "intentShape": "multi_intent",
                "goals": [
                    {
                        "taskId": "task_balance",
                        "intentId": "ccb.account.balance.query",
                        "slots": [], "status": "needs_slots",
                    },
                    {
                        "taskId": "task_transfer",
                        "intentId": "ccb.transfer.create",
                        "slots": [
                            {"name": "payee", "value": "\u5f20\u4e09", "source": "context",
                             "sourceIntentId": "ccb.transfer.create", "updatedAt": "2026-09-01T10:00:00.000Z"},
                            {"name": "amount", "value": 100, "source": "context",
                             "sourceIntentId": "ccb.transfer.create", "updatedAt": "2026-09-01T10:00:00.000Z"},
                        ],
                        "status": "needs_slots",
                    },
                ],
                "sharedSlots": [], "ambiguities": [],
            },
            "executableCapabilityIds": [
                "bank.account.list", "bank.account.balance.get",
                "bank.payee.list", "bank.payee.validate",
            ],
        })

        result = await service.resume_business_workflow(workflow_id, request)

        self.assertEqual(result.status, "clarification_required")
        self.assertIsNone(result.user_response)
        self.assertEqual(
            [item.type for item in result.user_responses],
            ["result", "clarification"],
        )
        self.assertEqual(result.user_responses[0].title, "\u8d26\u6237\u4f59\u989d")
        self.assertIn("\u8f6c\u51fa\u8d26\u6237", result.user_responses[1].title + result.user_responses[1].body)
        self.assertNotIn("6217", "".join(action.label for action in result.user_responses[1].actions))
        self.assertEqual(calls[:2], ["bank.account.list", "bank.account.balance.get"])

    async def test_invalid_business_resume_reconciles_read_only_graph_progress(self):
        workflow_id = "bwf_" + "e" * 24
        initial_items = (
            TodoSnapshotItem(id="task_balance", status="completed", depends_on=[]),
            TodoSnapshotItem(id="task_repayment", status="in_progress", depends_on=[]),
        )
        calls = []
        workflow_writes = []

        def handler(http_request):
            body = json.loads(http_request.content) if http_request.content else None
            if http_request.method == "GET":
                return httpx.Response(200, json={"data": {
                    "businessWorkflowId": workflow_id,
                    "status": "awaiting_clarification",
                    "delegatedTaskIds": ["task_balance", "task_repayment"],
                    "envelopeRevision": 2, "workflowRevision": 2,
                    "todoRevision": 2,
                    "todoSnapshot": [
                        {
                            "id": item.id, "status": item.status,
                            "dependsOn": list(item.depends_on),
                        }
                        for item in initial_items
                    ],
                    "todoDigest": TodoLedger.digest(initial_items),
                    "activeGoalTaskId": "task_repayment",
                    "activeTodoId": "task_repayment",
                    "executionPolicy": "sequential",
                    "suspensionReason": "awaiting_clarification",
                    "lastOutcome": {"status": "awaiting_clarification"},
                    "pendingCheckpointIds": [],
                }, "meta": {}})
            if http_request.url.path.endswith("agent-workflows"):
                workflow_writes.append(body)
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": workflow_id, "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            calls.append((body["capabilityId"], body["input"]))
            outputs = {
                "bank.credit-card.list": {"cards": [{
                    "id": "credit_global_8891", "name": "龙卡全球支付卡",
                    "maskedNo": "****8891", "status": "ACTIVE",
                }]},
                "bank.account.list": {"accounts": [{
                    "id": "acct_salary_6217", "name": "工资卡",
                    "maskedNo": "****6217", "status": "ACTIVE",
                }]},
                "bank.credit-card.bill.get": {
                    "cardId": "credit_global_8891", "productName": "龙卡全球支付卡",
                    "maskedNo": "****8891", "billStatus": "PAYMENT_DUE",
                    "outstanding": {"amountMinor": 1422000},
                    "minimumPayment": {"amountMinor": 142200}, "dueDay": 18,
                },
                "bank.account.balance.get": {
                    "accountId": "acct_salary_6217",
                    "available": {"amountMinor": 2854036},
                },
            }
            return httpx.Response(200, json={
                "data": outputs[body["capabilityId"]], "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)

        class InvalidReadOnlyResumeRunner(FakeRunner):
            def restore_todo_ledger(self, session_id, revision, items):
                return TodoLedgerSnapshot(
                    revision=revision,
                    digest=TodoLedger.digest(initial_items),
                    items=initial_items,
                )

            def load_todo_ledger(self, session_id, revision=1):
                return TodoLedgerSnapshot(
                    revision=revision,
                    digest=TodoLedger.digest(initial_items),
                    items=initial_items,
                )

            async def run(self, payload, *, session_id):
                self.inputs.append(payload)
                self.session_ids.append(session_id)
                await invoker.invoke_read_capability("bank.credit-card.list", {})
                await invoker.invoke_read_capability("bank.account.list", {})
                await invoker.invoke_read_capability(
                    "bank.credit-card.bill.get", {"cardId": "credit_global_8891"}
                )
                return {"invalid": "model omitted the business outcome contract"}

        runner = InvalidReadOnlyResumeRunner(None)
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=runner,
            capability_invoker=invoker,
        )
        request = BusinessWorkflowResumeRequest.model_validate({
            "resumeKind": "clarification",
            "agentRunId": "abr_repayment_resume",
            "conversationId": "conv_repayment_resume",
            "customerId": "cust_demo_001",
            "taskId": "task_repayment",
            "clarificationMessage": "选择尾号6217的账户",
            "grants": [],
            "expectedWorkflowRevision": 2,
            "conversationContext": {
                "pendingIntentId": None, "slots": [],
                "pendingEnvelopeId": "dlg_repayment_resume", "envelopeRevision": 2,
                "intentShape": "multi_intent",
                "goals": [
                    {
                        "taskId": "task_balance",
                        "intentId": "ccb.account.balance.query",
                        "slots": [], "status": "ready",
                    },
                    {
                        "taskId": "task_repayment",
                        "intentId": "ccb.home.repayment.repay",
                        "slots": [
                            {
                                "name": "card", "value": "尾号8891",
                                "source": "context",
                                "sourceIntentId": "ccb.home.repayment.repay",
                                "updatedAt": "2026-09-05T10:00:00.000Z",
                            },
                            {
                                "name": "fromAccount", "value": "尾号6217",
                                "source": "context",
                                "sourceIntentId": "ccb.home.repayment.repay",
                                "updatedAt": "2026-09-05T10:00:01.000Z",
                            },
                        ],
                        "status": "needs_slots",
                    },
                ],
                "sharedSlots": [], "ambiguities": [],
            },
            "executableCapabilityIds": [
                "bank.credit-card.list", "bank.credit-card.bill.get",
                "bank.account.list", "bank.account.balance.get",
            ],
        })

        result = await service.resume_business_workflow(workflow_id, request)

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.active_goal_task_id, "task_repayment")
        self.assertEqual(result.user_response.title, "请选择本次还款金额")
        self.assertEqual(
            [item.label for item in result.user_response.actions],
            ["全额还款 14,220.00元", "最低还款 1,422.00元", "输入其他金额"],
        )
        self.assertEqual(workflow_writes[-1]["status"], "awaiting_clarification")
        self.assertEqual(workflow_writes[-1]["workflowRevision"], 3)
        self.assertTrue(all(capability_id != "bank.credit-card.repayment.create"
                            for capability_id, _ in calls))

    async def test_settled_credit_card_resume_completes_without_terminal_task_progress(self):
        workflow_id = "bwf_" + "f" * 24
        initial_items = (
            TodoSnapshotItem(id="task_balance", status="completed", depends_on=[]),
            TodoSnapshotItem(id="task_repayment", status="in_progress", depends_on=[]),
        )
        workflow_writes = []

        def handler(http_request):
            body = json.loads(http_request.content) if http_request.content else None
            if http_request.method == "GET":
                return httpx.Response(200, json={"data": {
                    "businessWorkflowId": workflow_id,
                    "status": "awaiting_clarification",
                    "delegatedTaskIds": ["task_balance", "task_repayment"],
                    "envelopeRevision": 2, "workflowRevision": 2,
                    "todoRevision": 2,
                    "todoSnapshot": [
                        {"id": item.id, "status": item.status,
                         "dependsOn": list(item.depends_on)}
                        for item in initial_items
                    ],
                    "todoDigest": TodoLedger.digest(initial_items),
                    "activeGoalTaskId": "task_repayment",
                    "activeTodoId": "task_repayment",
                    "executionPolicy": "sequential",
                    "suspensionReason": "awaiting_clarification",
                    "lastOutcome": {"status": "awaiting_clarification"},
                    "pendingCheckpointIds": [],
                }, "meta": {}})
            if http_request.url.path.endswith("agent-workflows"):
                workflow_writes.append(body)
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": workflow_id, "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            outputs = {
                "bank.credit-card.list": {"cards": [{
                    "id": "credit_global_8891", "name": "龙卡全球支付卡",
                    "maskedNo": "****8891", "status": "ACTIVE",
                }]},
                "bank.account.list": {"accounts": [{
                    "id": "acct_salary_6217", "name": "工资卡",
                    "maskedNo": "****6217", "status": "ACTIVE",
                }]},
                "bank.credit-card.bill.get": {
                    "cardId": "credit_global_8891", "productName": "龙卡全球支付卡",
                    "maskedNo": "****8891", "billStatus": "SETTLED",
                    "outstanding": {"amountMinor": 0},
                    "minimumPayment": {"amountMinor": 0}, "dueDay": 18,
                },
                "bank.account.balance.get": {
                    "accountId": "acct_salary_6217",
                    "available": {"amountMinor": 2854036},
                },
            }
            return httpx.Response(200, json={
                "data": outputs[body["capabilityId"]], "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)

        class SettledResumeRunner(FakeRunner):
            def restore_todo_ledger(self, session_id, revision, items):
                return TodoLedgerSnapshot(
                    revision=revision,
                    digest=TodoLedger.digest(initial_items), items=initial_items,
                )

            def load_todo_ledger(self, session_id, revision=1):
                return TodoLedgerSnapshot(
                    revision=revision,
                    digest=TodoLedger.digest(initial_items), items=initial_items,
                )

            async def run(self, payload, *, session_id):
                self.inputs.append(payload)
                self.session_ids.append(session_id)
                await invoker.invoke_read_capability("bank.credit-card.list", {})
                await invoker.invoke_read_capability("bank.account.list", {})
                await invoker.invoke_read_capability(
                    "bank.credit-card.bill.get", {"cardId": "credit_global_8891"}
                )
                return {"invalid": "authoritative settled state wins"}

        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=SettledResumeRunner(None),
            capability_invoker=invoker,
        )
        request = BusinessWorkflowResumeRequest.model_validate({
            "resumeKind": "clarification",
            "agentRunId": "abr_settled_repayment_resume",
            "conversationId": "conv_settled_repayment_resume",
            "customerId": "cust_demo_001",
            "taskId": "task_repayment",
            "clarificationMessage": "选择尾号6217的账户",
            "grants": [],
            "expectedWorkflowRevision": 2,
            "conversationContext": {
                "pendingIntentId": None, "slots": [],
                "pendingEnvelopeId": "dlg_settled_resume", "envelopeRevision": 2,
                "intentShape": "multi_intent",
                "goals": [
                    {"taskId": "task_balance", "intentId": "ccb.account.balance.query",
                     "slots": [], "status": "ready"},
                    {"taskId": "task_repayment", "intentId": "ccb.home.repayment.repay",
                     "slots": [
                         {"name": "card", "value": "尾号8891", "source": "context",
                          "sourceIntentId": "ccb.home.repayment.repay",
                          "updatedAt": "2026-09-05T10:00:00.000Z"},
                         {"name": "fromAccount", "value": "尾号6217", "source": "context",
                          "sourceIntentId": "ccb.home.repayment.repay",
                          "updatedAt": "2026-09-05T10:00:01.000Z"},
                     ], "status": "needs_slots"},
                ],
                "sharedSlots": [], "ambiguities": [],
            },
            "executableCapabilityIds": [
                "bank.credit-card.list", "bank.credit-card.bill.get",
                "bank.account.list", "bank.account.balance.get",
            ],
        })

        result = await service.resume_business_workflow(workflow_id, request)

        self.assertEqual(result.status, "succeeded")
        self.assertIsNone(result.active_goal_task_id)
        self.assertIsNone(result.task_progress)
        self.assertEqual(result.user_response.title, "本期信用卡账单已结清")
        self.assertEqual(workflow_writes[-1]["status"], "completed")
        self.assertIsNone(workflow_writes[-1]["activeGoalTaskId"])

    async def test_multi_goal_business_uses_stable_workflow_session_and_native_todo_coverage(self):
        request = request_payload()
        request.update({
            "message": "给张帅和李四各转100",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
        })
        tasks = []
        for task_id, payee in (("task_zhang", "张帅"), ("task_li", "李四")):
            task = envelope_payload()["tasks"][0]
            task["task_id"] = task_id
            task["slots"] = {
                "payeeName": {"value": payee, "source": "user_explicit", "confidence": 1},
                "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
            }
            tasks.append(task)
        envelope = envelope_payload("same_intent_multi_task", tasks)
        business = TodoSnapshotRunner(
            clarification_plan_payload(), ["task_zhang", "task_li:select-account"]
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope), business_runner=business,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(len(business.session_ids), 1)
        self.assertRegex(
            business.session_ids[0],
            r"^business:cust_demo_001:bwf_[a-f0-9]{24}$",
        )
        self.assertEqual(business.snapshot_session_ids, business.session_ids)
        self.assertEqual(
            business.inputs[0]["business_workflow_id"],
            business.session_ids[0].split(":")[-1],
        )

    async def test_multi_goal_business_persists_safe_native_todo_snapshot(self):
        request = request_payload()
        request.update({
            "message": "给张帅和李四各转100",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
        })
        tasks = []
        for task_id, payee in (("task_zhang", "张帅"), ("task_li", "李四")):
            task = envelope_payload()["tasks"][0]
            task["task_id"] = task_id
            task["slots"] = {
                "payeeName": {
                    "value": payee, "source": "user_explicit", "confidence": 1,
                },
                "amount": {
                    "value": "100", "source": "user_explicit", "confidence": 1,
                },
            }
            tasks.append(task)
        captured = []

        def handler(http_request):
            captured.append(http_request)
            body = json.loads(http_request.content)
            if http_request.url.path.endswith("agent-capability-invocations"):
                output = (
                    {"accounts": []}
                    if body["capabilityId"] == "bank.account.list"
                    else {"payees": []}
                )
                return httpx.Response(200, json={"data": output, "meta": {}})
            return httpx.Response(201, json={
                "data": {
                    "businessWorkflowId": body["businessWorkflowId"],
                    "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                },
                "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload("same_intent_multi_task", tasks)),
            business_runner=TodoSnapshotRunner(
                clarification_plan_payload(), ["task_zhang", "task_li"],
            ),
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "clarification_required")
        workflow_request = next(
            item for item in captured if str(item.url).endswith("/agent-workflows")
        )
        body = json.loads(workflow_request.content)
        self.assertEqual(body["status"], "awaiting_clarification")
        self.assertEqual(body["delegatedTaskIds"], ["task_zhang", "task_li"])
        self.assertEqual(set(body["todoSnapshot"][0]), {"id", "status", "dependsOn"})

    async def test_multi_goal_business_rejects_native_todo_missing_a_delegated_goal(self):
        request = request_payload()
        request.update({
            "message": "给张帅和李四各转100",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
        })
        tasks = []
        for task_id, payee in (("task_zhang", "张帅"), ("task_li", "李四")):
            task = envelope_payload()["tasks"][0]
            task["task_id"] = task_id
            task["slots"] = {
                "payeeName": {
                    "value": payee, "source": "user_explicit", "confidence": 1,
                },
                "amount": {
                    "value": "100", "source": "user_explicit", "confidence": 1,
                },
            }
            tasks.append(task)
        envelope = envelope_payload("same_intent_multi_task", tasks)
        business = TodoSnapshotRunner(
            clarification_plan_payload(), ["task_zhang:select-account"],
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope), business_runner=business,
        )

        events = [
            item async for item in service.stream(
                RuntimeAgentRunRequest.model_validate(request)
            )
        ]

        self.assertEqual(events[-1].stage, "run.failed")
        self.assertEqual(events[-1].error.code, "business_agent_todo_goal_conflict")
        self.assertNotIn("business_agent.completed", [item.stage for item in events])

    async def test_amount_scope_reply_keeps_both_pending_transfer_goals(self):
        request = request_payload()
        request.update({
            "message": "每人100元",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
            "conversationContext": {
                "pendingEnvelopeId": "dlg_multi_001",
                "intentShape": "same_intent_multi_task",
                "goals": [
                    {
                        "taskId": "task_zhang", "intentId": "ccb.transfer.create",
                        "status": "provisional",
                        "slots": [{
                            "name": "payee", "value": "张帅", "source": "context",
                            "sourceIntentId": "ccb.transfer.create",
                            "updatedAt": "2026-08-30T12:00:00Z",
                        }],
                    },
                    {
                        "taskId": "task_li", "intentId": "ccb.transfer.create",
                        "status": "provisional",
                        "slots": [{
                            "name": "payee", "value": "李四", "source": "context",
                            "sourceIntentId": "ccb.transfer.create",
                            "updatedAt": "2026-08-30T12:00:00Z",
                        }],
                    },
                ],
                "sharedSlots": [{
                    "name": "amount", "value": "100", "source": "context",
                    "sourceIntentId": "ccb.transfer.create",
                    "updatedAt": "2026-08-30T12:00:00Z",
                }],
                "ambiguities": [{
                    "type": "amount_scope",
                    "taskIds": ["task_zhang", "task_li"],
                    "options": ["per_task", "total"],
                }],
            },
        })
        tasks = []
        for task_id in ("task_zhang", "task_li"):
            task = envelope_payload()["tasks"][0]
            task["task_id"] = task_id
            tasks.append(task)
        envelope = envelope_payload("same_intent_multi_task", tasks)
        envelope["delegation_id"] = "dlg_multi_001"
        envelope["ambiguities"] = [{
            "type": "amount_scope",
            "task_ids": ["task_zhang", "task_li"],
            "options": ["per_task", "total"],
        }]
        user = FakeRunner(envelope)
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(user_runner=user, business_runner=business)

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.intent_task_envelope.intent_shape, "same_intent_multi_task")
        self.assertEqual(len(result.intent_task_envelope.tasks), 2)
        self.assertEqual(
            [task.slots["payee"].value for task in result.intent_task_envelope.tasks],
            ["张帅", "李四"],
        )
        self.assertTrue(all(
            task.slots["amountMinor"].value == 10000
            for task in result.intent_task_envelope.tasks
        ))
        self.assertEqual(result.intent_task_envelope.ambiguities, [])
        self.assertEqual(len(business.inputs), 1)
        self.assertEqual(len(business.inputs[0]["intent_task_envelope"]["tasks"]), 2)
        self.assertIn("张帅、李四", result.user_response.body)
        self.assertIn("各转账 100.00 元", result.user_response.body)

    def test_dropped_cross_domain_goal_is_reanchored_after_clarification(self):
        # 跨域信封（查余额 + 双收款人转账）澄清期间，模型重建信封丢掉了
        # 尚缺槽位的余额查询任务；图谱 contextPolicy.retainOnClarification
        # 声明该目标不得静默消失，必须按保留目标确定性补挂。
        # 请求脚手架在默认 transfer 能力之上追加 balance 读能力
        # （与 harbor 能力投影一致）：两类候选都要通过能力可用性过滤，
        # 重锚后位于首位的查询任务也才能通过活动查询能力门禁。
        request = request_payload()
        for capability_id in (
            "bank.account.detail.get", "bank.account.balance.get",
        ):
            balance_capability = balance_capability_payload(capability_id)
            request["executableCapabilities"].append(balance_capability)
            request["plannableCapabilities"].append(balance_capability)
        request.update({
            "message": "每人100元",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
            "conversationContext": {
                "pendingEnvelopeId": "dlg_cross_reanchor",
                "intentShape": "multi_intent",
                "goals": [
                    {
                        "taskId": "task_balance", "intentId": "ccb.account.balance.query",
                        "status": "needs_slots",
                        "slots": [],
                    },
                    {
                        "taskId": "task_zhang", "intentId": "ccb.transfer.create",
                        "status": "provisional",
                        "slots": [{
                            "name": "payee", "value": "张三", "source": "context",
                            "sourceIntentId": "ccb.transfer.create",
                            "updatedAt": "2026-08-30T12:00:00Z",
                        }],
                    },
                    {
                        "taskId": "task_li", "intentId": "ccb.transfer.create",
                        "status": "provisional",
                        "slots": [{
                            "name": "payee", "value": "李四", "source": "context",
                            "sourceIntentId": "ccb.transfer.create",
                            "updatedAt": "2026-08-30T12:00:00Z",
                        }],
                    },
                ],
                "sharedSlots": [{
                    "name": "amount", "value": "100", "source": "context",
                    "sourceIntentId": "ccb.transfer.create",
                    "updatedAt": "2026-08-30T12:00:00Z",
                }],
                "ambiguities": [],
            },
        })
        tasks = []
        for task_id in ("task_zhang", "task_li"):
            task = envelope_payload()["tasks"][0]
            task["task_id"] = task_id
            tasks.append(task)
        envelope = envelope_payload("same_intent_multi_task", tasks)
        envelope["delegation_id"] = "dlg_cross_reanchor"
        envelope["ambiguities"] = []

        runtime_request = RuntimeAgentRunRequest.model_validate(request)
        workspace = (
            Path("/app") if Path("/app/data/ai-native-bank").exists()
            else Path(__file__).resolve().parents[4]
        )
        graph = IntentGraphRepository.load(
            workspace / "data" / "ai-native-bank" / "intent-graph"
            / "graph.ccb.intents.v1.0.0.json"
        )
        evaluator = SlotContractEvaluator()
        parsed = _parse_envelope_submission(envelope, runtime_request)
        evaluated, slot_evaluations = _apply_slot_contracts(
            parsed, runtime_request, graph, evaluator
        )

        # 模型信封丢目标时，图谱约束校验必然拒绝（期望任务数=3）：
        reanchored, _ = _reanchor_retained_goals(
            evaluated, slot_evaluations, runtime_request, graph, evaluator,
        )

        self.assertEqual(reanchored.intent_shape, "multi_intent")
        self.assertEqual(len(reanchored.tasks), 3)
        # 补挂目标按会话目标的既定顺序插位（而非追加在末尾），
        # 与业务工作流 delegatedTaskIds 的委托顺序比对保持一致。
        self.assertEqual(
            [task.task_id for task in reanchored.tasks],
            ["task_balance", "task_zhang", "task_li"],
        )
        balance = reanchored.tasks[0]
        self.assertEqual(balance.intent_id, "ccb.account.balance.query")
        self.assertEqual(balance.execution_readiness, "needs_slots")

        # 重锚后的信封必须能通过强约束校验（先补挂后校验的接线依据）：
        constraints = recall_intent_constraints(runtime_request, graph)
        self.assertEqual(
            validate_intent_constraints(reanchored, constraints),
            (),
        )

    def test_pending_context_injection_never_narrows_recall_candidates(self):
        # 上下文续跑轮（待澄清期间补充账户信息）：消息召回 transfer 候选
        # （低分），goals 注入 balance 候选（130 分）。注入分不参与
        # "高分悬殊→权威收窄"判定，候选集合保持并集且非权威，
        # 否则收敛成单个 balance 约束会把续跑信封逼入约束冲突。
        workspace = (
            Path("/app") if Path("/app/data/ai-native-bank").exists()
            else Path(__file__).resolve().parents[4]
        )
        graph = IntentGraphRepository.load(
            workspace / "data" / "ai-native-bank" / "intent-graph"
            / "graph.ccb.intents.v1.0.0.json"
        )
        payload = request_payload()
        payload.update({
            "message": "使用工资卡尾号6217转账",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
            "conversationContext": {
                "pendingEnvelopeId": "dlg_account_reply",
                "intentShape": "multi_intent",
                "goals": [
                    {
                        "taskId": "task_balance", "intentId": "ccb.account.balance.query",
                        "status": "needs_slots",
                        "slots": [],
                    },
                    {
                        "taskId": "task_zhang", "intentId": "ccb.transfer.create",
                        "status": "provisional",
                        "slots": [{
                            "name": "payee", "value": "张三", "source": "context",
                            "sourceIntentId": "ccb.transfer.create",
                            "updatedAt": "2026-08-30T12:00:00Z",
                        }],
                    },
                    {
                        "taskId": "task_li", "intentId": "ccb.transfer.create",
                        "status": "provisional",
                        "slots": [{
                            "name": "payee", "value": "李四", "source": "context",
                            "sourceIntentId": "ccb.transfer.create",
                            "updatedAt": "2026-08-30T12:00:00Z",
                        }],
                    },
                ],
                "sharedSlots": [{
                    "name": "amount", "value": "100", "source": "context",
                    "sourceIntentId": "ccb.transfer.create",
                    "updatedAt": "2026-08-30T12:00:00Z",
                }],
                "ambiguities": [],
            },
        })
        request = RuntimeAgentRunRequest.model_validate(payload)
        # harbor 能力投影始终携带全量能力；测试脚手架需补齐 balance 读能力，
        # 否则 balance 候选会被能力可用性过滤掉（与待验证问题无关）。
        for capability_id in (
            "bank.account.list", "bank.account.balance.get",
            "bank.account.detail.get",
        ):
            balance_capability = CapabilityProjection.model_validate(
                balance_capability_payload(capability_id)
            )
            request.executable_capabilities.append(balance_capability)
            request.plannable_capabilities.append(balance_capability)

        constraints = recall_intent_constraints(request, graph)

        intent_ids = {item.intent_id for item in constraints}
        self.assertIn("ccb.account.balance.query", intent_ids)
        self.assertIn("ccb.transfer.create", intent_ids)
        self.assertFalse(all(item.shape_authoritative for item in constraints))

    def test_rewritten_model_task_id_still_continues_retained_goals(self):
        # 模型重建信封时把转账任务改写成全新 task_id 且未携带 payee：
        # 续跑判定按意图重叠识别，任务收敛回目标稳定 id，
        # 目标已确认槽位（收款人）必须合并回来，不得向用户重复追问。
        workspace = (
            Path("/app") if Path("/app/data/ai-native-bank").exists()
            else Path(__file__).resolve().parents[4]
        )
        graph = IntentGraphRepository.load(
            workspace / "data" / "ai-native-bank" / "intent-graph"
            / "graph.ccb.intents.v1.0.0.json"
        )
        evaluator = SlotContractEvaluator()
        payload = request_payload()
        for capability_id in (
            "bank.account.list", "bank.account.balance.get",
            "bank.account.detail.get",
        ):
            balance_capability = CapabilityProjection.model_validate(
                balance_capability_payload(capability_id)
            )
            payload["executableCapabilities"].append(balance_capability)
            payload["plannableCapabilities"].append(balance_capability)
        payload.update({
            "message": "使用工资卡尾号6217转账",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
            "conversationContext": {
                "pendingEnvelopeId": "dlg_rewritten_id",
                "intentShape": "multi_intent",
                "goals": [
                    {
                        "taskId": "task_balance", "intentId": "ccb.account.balance.query",
                        "status": "needs_slots",
                        "slots": [],
                    },
                    {
                        "taskId": "task_zhang", "intentId": "ccb.transfer.create",
                        "status": "provisional",
                        "slots": [{
                            "name": "payee", "value": "张三", "source": "context",
                            "sourceIntentId": "ccb.transfer.create",
                            "updatedAt": "2026-08-30T12:00:00Z",
                        }],
                    },
                    {
                        "taskId": "task_li", "intentId": "ccb.transfer.create",
                        "status": "provisional",
                        "slots": [{
                            "name": "payee", "value": "李四", "source": "context",
                            "sourceIntentId": "ccb.transfer.create",
                            "updatedAt": "2026-08-30T12:00:00Z",
                        }],
                    },
                ],
                "sharedSlots": [{
                    "name": "amount", "value": "100", "source": "context",
                    "sourceIntentId": "ccb.transfer.create",
                    "updatedAt": "2026-08-30T12:00:00Z",
                }],
                "ambiguities": [],
            },
        })
        request = RuntimeAgentRunRequest.model_validate(payload)
        task = envelope_payload()["tasks"][0]
        task["task_id"] = "task_rewritten"
        task["slots"] = {}
        envelope = envelope_payload("single_intent", [task])
        envelope["delegation_id"] = "dlg_rewritten_id"
        envelope["ambiguities"] = []
        parsed = _parse_envelope_submission(envelope, request)
        evaluated, slot_evaluations = _apply_slot_contracts(
            parsed, request, graph, evaluator
        )

        reanchored, _ = _reanchor_retained_goals(
            evaluated, slot_evaluations, request, graph, evaluator,
        )

        self.assertEqual(
            [item.task_id for item in reanchored.tasks],
            ["task_balance", "task_zhang", "task_li"],
        )
        zhang = reanchored.tasks[1]
        self.assertIn("payee", zhang.slots)
        self.assertEqual(zhang.slots["payee"].value, "张三")
        li = reanchored.tasks[2]
        self.assertIn("payee", li.slots)
        self.assertEqual(li.slots["payee"].value, "李四")

    async def test_total_amount_scope_never_silently_splits_between_goals(self):
        request = request_payload()
        request.update({
            "message": "两人合计100元",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
            "conversationContext": {
                "pendingEnvelopeId": "dlg_multi_total",
                "intentShape": "same_intent_multi_task",
                "goals": [
                    {
                        "taskId": task_id, "intentId": "ccb.transfer.create",
                        "status": "provisional",
                        "slots": [{
                            "name": "payee", "value": payee, "source": "context",
                            "sourceIntentId": "ccb.transfer.create",
                            "updatedAt": "2026-08-30T12:00:00Z",
                        }],
                    }
                    for task_id, payee in (("task_zhang", "张帅"), ("task_li", "李四"))
                ],
                "sharedSlots": [{
                    "name": "amount", "value": "100", "source": "context",
                    "sourceIntentId": "ccb.transfer.create",
                    "updatedAt": "2026-08-30T12:00:00Z",
                }],
                "ambiguities": [{
                    "type": "amount_scope", "taskIds": ["task_zhang", "task_li"],
                    "options": ["per_task", "total"],
                }],
            },
        })
        tasks = []
        for task_id in ("task_zhang", "task_li"):
            task = envelope_payload()["tasks"][0]
            task["task_id"] = task_id
            tasks.append(task)
        envelope = envelope_payload("same_intent_multi_task", tasks)
        envelope["delegation_id"] = "dlg_multi_total"

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            outputs = {
                "bank.account.list": {"accounts": [
                    {"id": "acct_salary_6217", "name": "工资卡", "maskedNo": "****6217", "status": "ACTIVE"},
                ]},
                "bank.payee.list": {"payees": [
                    {"id": "payee_zhangshuai_1101", "name": "张帅", "maskedNo": "****1101", "status": "ACTIVE"},
                    {"id": "payee_lisi_3308", "name": "李四", "maskedNo": "****3308", "status": "ACTIVE"},
                ]},
            }
            return httpx.Response(200, json={"data": outputs[body["capabilityId"]], "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=FakeRunner(clarification_plan_payload()),
            capability_invoker=invoker,
            intent_graph=self._v4_intent_graph(),
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "clarification_required")
        self.assertTrue(all(
            "amountMinor" not in task.slots
            for task in result.intent_task_envelope.tasks
        ))
        self.assertEqual(result.user_response.title, "请分别输入转账金额")
        self.assertIn("张帅、李四", result.user_response.body)

    @staticmethod
    def _v4_intent_graph():
        from app.v4_graph_adapter import V4GraphAdapter

        path = Path(
            "/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v2.1.0.json"
        )
        if not path.exists():
            path = (
                Path(__file__).resolve().parents[4]
                / "data" / "ai-native-bank" / "intent-graph"
                / "graph.ccb.intents.v2.1.0.json"
            )
        return V4GraphAdapter(
            json.loads(path.read_text(encoding="utf-8")), path
        )

    async def test_total_scope_reply_drops_planner_inherited_amounts(self):
        """真实链路回归：planner 重建的转账任务自带每笔金额（口径未定时的
        申报值），合计口径声明后必须失效并逐笔追问，而不是按每人金额执行。"""
        request = request_payload()
        request.update({
            "message": "两人合计100元",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
            "conversationContext": {
                "pendingEnvelopeId": "dlg_multi_total",
                "intentShape": "same_intent_multi_task",
                "goals": [
                    {
                        "taskId": task_id, "intentId": "ccb.transfer.create",
                        "status": "provisional",
                        "slots": [{
                            "name": "payee", "value": payee, "source": "context",
                            "sourceIntentId": "ccb.transfer.create",
                            "updatedAt": "2026-08-30T12:00:00Z",
                        }],
                    }
                    for task_id, payee in (("task_zhang", "张帅"), ("task_li", "李四"))
                ],
                "sharedSlots": [{
                    "name": "amount", "value": "100", "source": "context",
                    "sourceIntentId": "ccb.transfer.create",
                    "updatedAt": "2026-08-30T12:00:00Z",
                }],
                "ambiguities": [{
                    "type": "amount_scope", "taskIds": ["task_zhang", "task_li"],
                    "options": ["per_task", "total"],
                }],
            },
        })
        tasks = []
        for task_id in ("task_zhang", "task_li"):
            task = envelope_payload()["tasks"][0]
            task.update({
                "task_id": task_id,
                "slots": {
                    "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
                    "amountMinor": {"value": "10000", "source": "user_explicit", "confidence": 1},
                },
            })
            tasks.append(task)
        envelope = envelope_payload("same_intent_multi_task", tasks)
        envelope["delegation_id"] = "dlg_multi_total"

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            outputs = {
                "bank.account.list": {"accounts": [
                    {"id": "acct_salary_6217", "name": "工资卡", "maskedNo": "****6217", "status": "ACTIVE"},
                ]},
                "bank.payee.list": {"payees": [
                    {"id": "payee_zhangshuai_1101", "name": "张帅", "maskedNo": "****1101", "status": "ACTIVE"},
                    {"id": "payee_lisi_3308", "name": "李四", "maskedNo": "****3308", "status": "ACTIVE"},
                ]},
            }
            return httpx.Response(200, json={"data": outputs[body["capabilityId"]], "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=FakeRunner(clarification_plan_payload()),
            capability_invoker=invoker,
            intent_graph=self._v4_intent_graph(),
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        # 合计口径下继承金额必须失效：既不落槽执行，也不投出确认卡；
        # 账户链解析完转出账户后落入逐笔金额澄清（不自动均分）。
        self.assertEqual(result.status, "clarification_required")
        self.assertTrue(all(
            "amount" not in task.slots and "amountMinor" not in task.slots
            for task in result.intent_task_envelope.tasks
        ))
        self.assertEqual(result.intent_task_envelope.ambiguities, [])
        self.assertIsNone(result.confirmation)
        self.assertEqual(result.user_response.title, "请分别输入转账金额")

    async def test_shared_account_reply_is_applied_to_every_pending_transfer_goal(self):
        request = request_payload()
        request.update({
            "message": "使用工资卡尾号6217转账",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
            "conversationContext": {
                "pendingEnvelopeId": "dlg_multi_account",
                "intentShape": "same_intent_multi_task",
                "goals": [
                    {
                        "taskId": task_id, "intentId": "ccb.transfer.create",
                        "status": "needs_slots",
                        "slots": [
                            {
                                "name": "payee", "value": payee, "source": "context",
                                "sourceIntentId": "ccb.transfer.create",
                                "updatedAt": "2026-08-30T12:00:00Z",
                            },
                            {
                                "name": "amount", "value": "100", "source": "context",
                                "sourceIntentId": "ccb.transfer.create",
                                "updatedAt": "2026-08-30T12:00:00Z",
                            },
                        ],
                    }
                    for task_id, payee in (("task_zhang", "张帅"), ("task_li", "李四"))
                ],
                "sharedSlots": [{
                    "name": "fromAccount", "value": "工资卡尾号6217", "source": "context",
                    "sourceIntentId": "ccb.transfer.create",
                    "updatedAt": "2026-08-30T12:00:00Z",
                }],
                "ambiguities": [],
            },
        })
        tasks = []
        for task_id in ("task_zhang", "task_li"):
            task = envelope_payload()["tasks"][0]
            task["task_id"] = task_id
            tasks.append(task)
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload("same_intent_multi_task", tasks)),
            business_runner=business,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(len(result.intent_task_envelope.tasks), 2)
        self.assertTrue(all(
            task.slots["fromAccount"].value == "工资卡尾号6217"
            for task in result.intent_task_envelope.tasks
        ))
        self.assertEqual(
            len(business.inputs[0]["intent_task_envelope"]["tasks"]), 2,
        )

    async def test_multi_payee_transfer_preserves_two_goals_and_clarifies_amount_scope(self):
        request = request_payload()
        request.update({
            "message": "给张帅和李四转100",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
        })
        first = envelope_payload()["tasks"][0]
        first.update({
            "task_id": "task_zhang",
            "slots": {
                "payeeName": {"value": "张帅", "source": "user_explicit", "confidence": 1},
            },
        })
        second = json.loads(json.dumps(first, ensure_ascii=False))
        second["task_id"] = "task_li"
        second["slots"]["payeeName"]["value"] = "李四"
        envelope = envelope_payload("same_intent_multi_task", [first, second])
        envelope["ambiguities"] = [{
            "type": "amount_scope",
            "task_ids": ["task_zhang", "task_li"],
            "options": ["per_task", "total"],
        }]
        envelope["shared_slots"] = {
            "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
        }
        user = FakeRunner(envelope)
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(user_runner=user, business_runner=business)

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.intent_task_envelope.intent_shape, "same_intent_multi_task")
        self.assertEqual(len(result.intent_task_envelope.tasks), 2)
        self.assertEqual(
            [task.slots["payee"].value for task in result.intent_task_envelope.tasks],
            ["张帅", "李四"],
        )
        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.title, "请确认转账金额")
        self.assertEqual(
            [action.label for action in result.user_response.actions],
            ["每人100元", "两人合计100元"],
        )
        self.assertEqual(len(business.inputs), 0)
        assessment = user.inputs[0]["intent_shape_assessment"]
        self.assertEqual(assessment["shape"], "single_intent")
        self.assertEqual(assessment["expectedTaskCount"], 1)
        self.assertFalse(assessment["authoritative"])
        self.assertEqual(user.inputs[0]["intent_candidates"][0]["expected_task_count"], 1)
        self.assertEqual(result.conversation_state.schema_version, "2.0")
        self.assertEqual(
            [goal.task_id for goal in result.conversation_state.goals],
            ["task_zhang", "task_li"],
        )
        self.assertEqual(result.conversation_state.shared_slots[0].name, "amount")
        self.assertEqual(result.conversation_state.shared_slots[0].value, "100")
        self.assertEqual(result.conversation_state.ambiguities[0].type, "amount_scope")

    async def test_adjacent_payee_names_preserve_two_goals_and_clarify_amount_scope(self):
        request = request_payload()
        request.update({
            "message": "给张三李四转200",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
        })
        first = envelope_payload()["tasks"][0]
        first.update({
            "task_id": "task_zhang",
            "slots": {
                "payeeName": {"value": "张三", "source": "user_explicit", "confidence": 1},
            },
        })
        second = json.loads(json.dumps(first, ensure_ascii=False))
        second["task_id"] = "task_li"
        second["slots"]["payeeName"]["value"] = "李四"
        envelope = envelope_payload("same_intent_multi_task", [first, second])
        envelope["ambiguities"] = [{
            "type": "amount_scope",
            "task_ids": ["task_zhang", "task_li"],
            "options": ["per_task", "total"],
        }]
        envelope["shared_slots"] = {
            "amount": {"value": "200", "source": "user_explicit", "confidence": 1},
        }
        user = FakeRunner(envelope)
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(user_runner=user, business_runner=business)

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.intent_task_envelope.intent_shape, "same_intent_multi_task")
        self.assertEqual(
            [task.slots["payee"].value for task in result.intent_task_envelope.tasks],
            ["张三", "李四"],
        )
        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.title, "请确认转账金额")
        self.assertEqual(len(business.inputs), 0)
        assessment = user.inputs[0]["intent_shape_assessment"]
        self.assertEqual(assessment["shape"], "same_intent_multi_task")
        self.assertEqual(assessment["entities"], ["张三", "李四"])
        self.assertEqual(assessment["expectedTaskCount"], 2)
        self.assertFalse(assessment["authoritative"])

    async def test_adjacent_payee_names_repair_a_merged_single_task(self):
        request = request_payload()
        request.update({
            "message": "给张三李四转100",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
        })
        merged = envelope_payload()
        merged["tasks"][0]["slots"] = {
            "payeeName": {
                "value": "张三李四", "source": "user_explicit", "confidence": 1,
            },
            "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
        }
        first = json.loads(json.dumps(merged["tasks"][0], ensure_ascii=False))
        first.update({
            "task_id": "task_zhang",
            "slots": {
                "payeeName": {
                    "value": "张三", "source": "user_explicit", "confidence": 1,
                },
            },
        })
        second = json.loads(json.dumps(first, ensure_ascii=False))
        second["task_id"] = "task_li"
        second["slots"]["payeeName"]["value"] = "李四"
        repaired = envelope_payload("same_intent_multi_task", [first, second])
        repaired["shared_slots"] = {
            "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
        }
        repaired["ambiguities"] = [{
            "type": "amount_scope",
            "task_ids": ["task_zhang", "task_li"],
            "options": ["per_task", "total"],
        }]
        user = FakeRunner([merged, repaired])
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(user_runner=user, business_runner=business)

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(len(user.inputs), 2)
        self.assertIn(
            "task_cardinality_mismatch",
            user.inputs[1]["repair_feedback"]["codes"],
        )
        self.assertEqual(
            [task.slots["payee"].value for task in result.intent_task_envelope.tasks],
            ["张三", "李四"],
        )
        self.assertEqual(result.user_response.title, "请确认转账金额")

    async def test_user_agent_repairs_a_schema_invalid_ambiguity_once(self):
        request = request_payload()
        request.update({
            "message": "给张三李四转100",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
        })
        tasks = []
        for task_id, payee in (("task_zhang", "张三"), ("task_li", "李四")):
            task = envelope_payload()["tasks"][0]
            task.update({
                "task_id": task_id,
                "slots": {
                    "payeeName": {
                        "value": payee,
                        "source": "user_explicit",
                        "confidence": 1,
                    },
                },
            })
            tasks.append(task)
        repaired = envelope_payload("same_intent_multi_task", tasks)
        repaired["shared_slots"] = {
            "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
        }
        repaired["ambiguities"] = [{
            "type": "amount_scope",
            "task_ids": ["task_zhang", "task_li"],
            "options": ["per_task", "total"],
        }]
        invalid = json.loads(json.dumps(repaired, ensure_ascii=False))
        invalid["ambiguities"] = [{
            "type": "amount_scope",
            "task_ids": [],
            "options": ["每人100元", "两人合计100元"],
        }]
        user = FakeRunner([invalid, repaired])
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(clarification_plan_payload()),
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(len(user.inputs), 2)
        self.assertEqual(
            user.inputs[1]["repair_feedback"]["codes"],
            ["submission_contract_invalid"],
        )
        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(len(result.intent_task_envelope.tasks), 2)
        self.assertEqual(
            result.intent_task_envelope.ambiguities[0].task_ids,
            ["task_zhang", "task_li"],
        )

    async def test_omitted_amount_scope_ambiguity_is_derived_from_equal_task_amounts(self):
        request = request_payload()
        request.update({
            "message": "给张三李四转100",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
        })
        tasks = []
        for task_id, payee in (("task_zhang", "张三"), ("task_li", "李四")):
            task = envelope_payload()["tasks"][0]
            task.update({
                "task_id": task_id,
                "slots": {
                    "payee": {"value": payee, "source": "user_explicit", "confidence": 1},
                    "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
                },
            })
            tasks.append(task)
        envelope = envelope_payload("same_intent_multi_task", tasks)
        envelope["ambiguities"] = []
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=FakeRunner(clarification_plan_payload()),
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.title, "请确认转账金额")
        self.assertEqual(result.intent_task_envelope.ambiguities[0].type, "amount_scope")
        self.assertEqual(
            result.intent_task_envelope.ambiguities[0].task_ids,
            ["task_zhang", "task_li"],
        )

    async def test_cross_domain_envelope_still_clarifies_amount_scope(self):
        # 跨域多意图（查余额 + 双收款人转账）：金额歧义按域派生，
        # 不因信封混入其他域任务而丢失，也不依赖形状提示的任务序号。
        request = request_payload()
        request.update({
            "message": "查余额，给张三李四转100",
            # 中性页面上下文：menuKey 会把图谱召回收窄到单域，混合信封过不了约束 rail。
            "pageContext": {"tab": "home"},
            # 查询域候选要求 balance/detail 能力绑定齐全，否则召回候选被能力过滤裁掉。
            "plannableCapabilities": [
                *request["plannableCapabilities"],
                *[
                    {
                        "id": capability_id,
                        "title": capability_id,
                        "description": "Account entity resolution",
                        "exposure": "model-callable",
                        "sideEffect": "none",
                        "riskLevel": "L1",
                        "inputSchema": {"type": "object"},
                        "outputSchema": {"type": "object"},
                        "possibleErrors": [],
                        "readiness": {"status": "ready", "environment": "mock", "blockers": []},
                    }
                    for capability_id in (
                        "bank.account.balance.get",
                        "bank.account.detail.get",
                    )
                ],
            ],
        })
        transfer = envelope_payload()["tasks"][0]
        tasks = [{
            "task_id": "task_balance",
            "intent_id": "ccb.account.balance.query",
            "action": "QUERY",
            "business_object": "account_balance",
            "slots": {},
            "dependencies": [],
            "execution_readiness": "needs_slots",
            "risk_level": "L1",
        }]
        for task_id, payee in (("task_zhang", "张三"), ("task_li", "李四")):
            task = json.loads(json.dumps(transfer, ensure_ascii=False))
            task.update({
                "task_id": task_id,
                "slots": {
                    "payeeName": {"value": payee, "source": "user_explicit", "confidence": 1},
                    "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
                },
            })
            tasks.append(task)
        envelope = envelope_payload("multi_intent", tasks)
        envelope["ambiguities"] = []
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=business,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.title, "请确认转账金额")
        self.assertEqual(
            [action.label for action in result.user_response.actions],
            ["每人100元", "两人合计100元"],
        )
        self.assertEqual(
            result.intent_task_envelope.ambiguities[0].task_ids,
            ["task_zhang", "task_li"],
        )
        self.assertEqual(len(business.inputs), 0)

    async def test_single_task_spurious_envelope_ambiguity_is_canonicalized(self):
        request = request_payload()
        envelope = envelope_payload()
        envelope["ambiguities"] = [{
            "type": "amount_scope",
            "task_ids": ["task_001"],
            "options": ["per_task", "total"],
        }]
        user = FakeRunner(envelope)
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(user_runner=user, business_runner=business)

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.intent_task_envelope.ambiguities, [])
        self.assertEqual(len(business.inputs), 1)

    async def test_amount_scope_projection_accepts_same_validated_amount_repeated_per_task(self):
        request = request_payload()
        request.update({
            "message": "给张帅和李四转100",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
        })
        tasks = []
        for task_id, payee in (("task_zhang", "张帅"), ("task_li", "李四")):
            task = envelope_payload()["tasks"][0]
            task.update({
                "task_id": task_id,
                "slots": {
                    "payeeName": {"value": payee, "source": "user_explicit", "confidence": 1},
                    "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
                },
            })
            tasks.append(task)
        envelope = envelope_payload("same_intent_multi_task", tasks)
        envelope["ambiguities"] = [{
            "type": "amount_scope", "task_ids": ["task_zhang", "task_li"],
            "options": ["per_task", "total"],
        }]
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=FakeRunner(clarification_plan_payload()),
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.title, "请确认转账金额")
        self.assertEqual(
            [action.label for action in result.user_response.actions],
            ["每人100元", "两人合计100元"],
        )

    def _mixed_domain_envelope_payload(self):
        query_task = {
            "task_id": "task_balance",
            "intent_id": "ccb.account.balance.query",
            "action": "QUERY",
            "business_object": "account_balance",
            "slots": {},
            "dependencies": [],
            "execution_readiness": "ready",
            "risk_level": "L0",
        }
        transfer_tasks = []
        for task_id, payee in (("task_zhang", "张三"), ("task_li", "李四")):
            task = envelope_payload()["tasks"][0]
            task.update({
                "task_id": task_id,
                "slots": {
                    "payeeName": {"value": payee, "source": "user_explicit", "confidence": 1},
                    "amount": {"value": "200", "source": "user_explicit", "confidence": 1},
                },
            })
            transfer_tasks.append(task)
        envelope = envelope_payload("multi_intent", [query_task, *transfer_tasks])
        envelope["ambiguities"] = [{
            "type": "amount_scope",
            "task_ids": ["task_zhang", "task_li"],
            "options": ["per_task", "total"],
        }]
        return envelope

    async def test_cross_domain_envelope_projects_ambiguity_by_referenced_tasks(self):
        request = request_payload()
        request.update({
            "message": "查余额，给张三李四转200",
            # 中性页面上下文：menuKey 会把图谱召回收窄到单域，混合信封过不了约束 rail。
            "pageContext": {"tab": "home"},
            # 查询域候选要求 balance/detail 能力绑定齐全，否则召回候选被能力过滤裁掉。
            "plannableCapabilities": [
                *request["plannableCapabilities"],
                *[
                    {
                        "id": capability_id,
                        "title": capability_id,
                        "description": "Account entity resolution",
                        "exposure": "model-callable",
                        "sideEffect": "none",
                        "riskLevel": "L1",
                        "inputSchema": {"type": "object"},
                        "outputSchema": {"type": "object"},
                        "possibleErrors": [],
                        "readiness": {"status": "ready", "environment": "mock", "blockers": []},
                    }
                    for capability_id in (
                        "bank.account.balance.get",
                        "bank.account.detail.get",
                    )
                ],
            ],
        })
        service = DualDeepAgentService(
            user_runner=FakeRunner(self._mixed_domain_envelope_payload()),
            business_runner=FakeRunner(clarification_plan_payload()),
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.title, "请确认转账金额")
        self.assertEqual(
            [action.label for action in result.user_response.actions],
            ["每人200元", "两人合计200元"],
        )
        self.assertEqual(
            result.intent_task_envelope.ambiguities[0].task_ids,
            ["task_zhang", "task_li"],
        )

    async def test_cross_domain_ambiguity_resolves_by_referenced_tasks_answer(self):
        workspace = (
            Path("/app") if Path("/app/data/ai-native-bank").exists()
            else Path(__file__).resolve().parents[4]
        )
        graph = IntentGraphRepository.load(
            workspace / "data" / "ai-native-bank" / "intent-graph"
            / "graph.ccb.intents.v1.0.0.json"
        )
        from app.business_profile import domain_ambiguity_is_resolved
        from app.contracts import IntentAmbiguity, IntentTaskEnvelope, SlotValue

        envelope = IntentTaskEnvelope.model_validate(self._mixed_domain_envelope_payload())
        ambiguity = envelope.ambiguities[0]
        resolved_tasks = [
            task.model_copy(update={
                "slots": {
                    **task.slots,
                    "amountMinor": SlotValue(value="20000", source="context", confidence=1),
                },
            })
            for task in envelope.tasks
        ]

        # 未出现每人/各等作用域措辞时，混合信封下的转账歧义不应被误判已消解。
        self.assertFalse(domain_ambiguity_is_resolved(
            graph, envelope, ambiguity,
            context_ambiguities=(ambiguity,),
            message="200",
            tasks=resolved_tasks,
        ))
        # 出现「每人」且歧义引用的转账任务金额已落到 amountMinor 时应消解；
        # 同一信封内的查询域任务不参与判定。
        self.assertTrue(domain_ambiguity_is_resolved(
            graph, envelope, ambiguity,
            context_ambiguities=(ambiguity,),
            message="每人200",
            tasks=resolved_tasks,
        ))

    async def test_partial_transfer_returns_only_missing_account_as_safe_choices(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            outputs = {
                "bank.account.list": {"accounts": [
                    {"id": "acct_salary_6217", "name": "工资卡", "maskedNo": "****6217", "status": "ACTIVE"},
                    {"id": "acct_saving_8032", "name": "储蓄卡", "maskedNo": "****8032", "status": "ACTIVE"},
                ]},
                "bank.payee.list": {"payees": [
                    {"id": "payee_zhangsan_7319", "name": "张三", "maskedNo": "****7319", "status": "ACTIVE"},
                ]},
            }
            return httpx.Response(200, json={"data": outputs[body["capabilityId"]], "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        ids = [
            "bank.account.list", "bank.account.balance.get", "bank.account.detail.get", "bank.transfer.status.get", "bank.payee.list", "bank.payee.validate",
            "bank.transfer.order.create",
        ]
        capabilities = [balance_capability_payload(item) for item in ids]
        capabilities[-1]["sideEffect"] = "state-change"
        request = request_payload()
        request.update({
            "message": "我要给张三转账100",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
            "executableCapabilities": capabilities[:-1],
            "plannableCapabilities": capabilities,
        })
        envelope = envelope_payload(tasks=[{
            "task_id": "task_transfer", "intent_id": "ccb.transfer.create",
            "action": "DO", "business_object": "transfer_order",
            "slots": {
                "payeeName": {"value": "张三", "source": "user_explicit", "confidence": 1},
                "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
            },
            "dependencies": [], "execution_readiness": "needs_slots", "risk_level": "L2",
        }])
        business = ToolCallingRunner(
            clarification_plan_payload(), invoker,
            [("bank.account.list", {}), ("bank.payee.list", {})],
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope), business_runner=business,
            capability_invoker=invoker,
        )

        streamed = [
            item async for item in service.stream_with_result(
                RuntimeAgentRunRequest.model_validate(request)
            )
        ]
        result = next(candidate for _event, candidate in streamed if candidate is not None)
        intent_event = next(
            event for event, _candidate in streamed
            if event.stage == "user_agent.completed"
        )

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.title, "请选择转出账户")
        self.assertEqual(result.user_response.body, "已识别向张三转账 100.00 元，请选择转出账户。")
        self.assertEqual([item.label for item in result.user_response.actions], ["工资卡", "储蓄卡"])
        self.assertTrue(all("acct_" not in item.payload.message for item in result.user_response.actions))
        self.assertEqual(result.conversation_state.status, "retain")
        self.assertEqual(result.conversation_state.schema_version, "2.0")
        self.assertIsNone(result.conversation_state.pending_intent_id)
        self.assertEqual(result.conversation_state.goals[0].intent_id, "ccb.transfer.create")
        self.assertEqual(
            {item.name: item.value for item in result.conversation_state.goals[0].slots},
            {"payee": "张三", "amount": "100"},
        )
        self.assertEqual(
            intent_event.summary.metrics["slotSummary"],
            "槽位完整度 2/3 · 已识别 收款人、金额 · 待补充 转出账户",
        )
        await invoker.close()

    async def test_multi_transfer_partial_plan_is_held_until_missing_payee_is_collected(self):
        checkpoint_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.url.path.endswith("agent-action-checkpoints"):
                checkpoint_calls.append(body)
                return httpx.Response(201, json={"data": {
                    "checkpointId": "acp_partial_should_not_exist",
                    "capabilityId": "bank.transfer.order.create",
                    "confirmationToken": "p" * 43,
                    "expiresAt": "2026-08-30T10:05:00.000Z",
                    "summary": {
                        "title": "confirm transfer", "description": "transfer order",
                        "riskLevel": "L2",
                    },
                }, "meta": {}})
            outputs = {
                "bank.account.list": {"accounts": [{
                    "id": "acct_salary_6217", "name": "工资卡",
                    "maskedNo": "****6217", "status": "ACTIVE",
                }]},
                "bank.payee.list": {"payees": [{
                    "id": "payee_zhang_4321", "name": "张三",
                    "maskedNo": "****4321", "status": "ACTIVE",
                }]},
            }
            return httpx.Response(200, json={"data": outputs[body["capabilityId"]], "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        ids = [
            "bank.account.list", "bank.account.balance.get", "bank.account.detail.get", "bank.transfer.status.get", "bank.payee.list", "bank.payee.validate",
            "bank.transfer.order.create",
        ]
        capabilities = [balance_capability_payload(item) for item in ids]
        capabilities[-1]["sideEffect"] = "state-change"
        request = request_payload()
        request.update({
            "message": "给张三李四各转200，使用工资卡尾号6217转账",
            "executableCapabilities": capabilities[:-1],
            "plannableCapabilities": capabilities,
        })
        tasks = []
        for task_id, payee in (("task_zhang", "张三"), ("task_li", "李四")):
            tasks.append({
                "task_id": task_id, "intent_id": "ccb.transfer.create",
                "action": "DO", "business_object": "transfer_order",
                "slots": {
                    "payeeName": {"value": payee, "source": "user_explicit", "confidence": 1},
                    "amount": {"value": "200", "source": "user_explicit", "confidence": 1},
                    "fromAccountName": {"value": "工资卡尾号6217", "source": "user_explicit", "confidence": 1},
                },
                "dependencies": [], "execution_readiness": "ready", "risk_level": "L2",
            })
        partial_plan = {
            "schema_version": "1.0", "business_agent_run_id": "bar_partial",
            "delegation_id": "dlg_model_supplied", "status": "planned",
            "steps": [{
                "step_id": "step_zhang", "task_id": "task_zhang",
                "capability_id": "bank.transfer.order.create",
                "input_bindings": {
                    "fromAccountId": "acct_salary_6217",
                    "payeeId": "payee_zhang_4321",
                    "amountMinor": 20000, "currency": "CNY",
                },
                "requires_user_confirmation": True, "depends_on": [],
            }],
        }
        business = ToolCallingRunner(
            partial_plan, invoker,
            [("bank.account.list", {}), ("bank.payee.list", {})],
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload("same_intent_multi_task", tasks)),
            business_runner=business,
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.schema_version, "1.1")
        self.assertEqual(result.user_response.title, "补充第2笔收款信息")
        self.assertEqual(result.clarification_form.kind, "transfer_payee")
        fields = {item.name: item.value for item in result.clarification_form.fields}
        self.assertEqual(fields["payeeName"], "李四")
        self.assertEqual(fields["accountNo"], "")
        self.assertEqual(checkpoint_calls, [])
        self.assertIn("工资卡", result.user_response.items[1])
        self.assertIn("200.00", result.user_response.items[2])

    async def test_transfer_slot_contract_overrides_model_readiness_and_preserves_present_slots(self):
        request = request_payload()
        request.update({
            "message": "我要给张三转账100",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
        })
        envelope = envelope_payload(tasks=[{
            "task_id": "task_transfer",
            "intent_id": "ccb.transfer.create",
            "action": "DO",
            "business_object": "transfer_order",
            "slots": {
                "payeeName": {"value": "张三", "source": "user_explicit", "confidence": 1},
                "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
            },
            "dependencies": [],
            "execution_readiness": "ready",
            "risk_level": "L2",
        }])
        user = FakeRunner(envelope)
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(user_runner=user, business_runner=business)

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        task = result.intent_task_envelope.tasks[0]
        self.assertEqual(task.execution_readiness, "needs_slots")
        self.assertEqual(task.slots["amountMinor"].value, 10000)
        self.assertEqual(task.slots["currency"].value, "CNY")
        assessment = business.inputs[0]["slot_assessment"]
        self.assertEqual(assessment["missing"], ["fromAccount"])
        self.assertEqual(assessment["recognized"], ["payee", "amount"])
        self.assertNotIn("purpose", assessment["missing"])

    async def test_pending_transfer_context_constrains_short_continuation_and_completes_slots(self):
        request = request_payload()
        request.update({
            "message": "工资卡",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
            "conversationContext": {
                "pendingIntentId": "ccb.transfer.create",
                "slots": [
                    {"name": "payee", "value": "张三", "source": "context", "sourceIntentId": "ccb.transfer.create", "updatedAt": "2026-08-30T12:00:00Z"},
                    {"name": "amount", "value": "100", "source": "context", "sourceIntentId": "ccb.transfer.create", "updatedAt": "2026-08-30T12:00:00Z"},
                ],
            },
        })
        envelope = envelope_payload(tasks=[{
            "task_id": "task_transfer",
            "intent_id": "ccb.transfer.create",
            "action": "DO",
            "business_object": "transfer_order",
            "slots": {
                "fromAccountName": {"value": "工资卡", "source": "user_selection", "confidence": 1},
            },
            "dependencies": [],
            "execution_readiness": "needs_slots",
            "risk_level": "L2",
        }])
        user = FakeRunner(envelope)
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(user_runner=user, business_runner=business)

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(user.inputs[0]["intent_candidates"][0]["intent_id"], "ccb.transfer.create")
        self.assertEqual(result.intent_task_envelope.tasks[0].execution_readiness, "ready")
        self.assertEqual(business.inputs[0]["slot_assessment"]["missing"], [])

    def _faq_dependencies(self):
        intent_graph = IntentGraphRepository.load(
            os.getenv(
                "AI_BANK_INTENT_GRAPH_PATH",
                "/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v1.0.0.json",
            )
        )
        faq_repository = MockFaqRepository.load(
            os.getenv(
                "AI_BANK_FAQ_PATH",
                "/app/data/ai-native-bank/faq/ccb-mobile-bank-faq.v1.json",
            ),
            valid_action_keys=intent_graph.action_keys,
        )
        return intent_graph, faq_repository

    async def _run_balance_response(self, *, message, accounts):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            if body["capabilityId"] == "bank.account.list":
                output = {"accounts": accounts}
            elif body["capabilityId"] == "bank.account.balance.get":
                output = {
                    "accountId": body["input"]["accountId"],
                    "available": {"formatted": "¥8,888.00"},
                    "ledger": {"formatted": "¥9,000.00"},
                }
            else:
                raise AssertionError("unexpected capability invocation")
            return httpx.Response(200, json={"data": output, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        business = ToolCallingRunner(
            clarification_plan_payload(),
            invoker,
            [("bank.account.list", {})],
        )
        graph, faq_repository = self._faq_dependencies()
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=business,
            intent_graph=graph,
            capability_invoker=invoker,
            faq_repository=faq_repository,
        )
        payload = balance_request_payload()
        payload["message"] = message
        payload["pageContext"] = {"tab": "home", "menuKey": "home.account"}
        try:
            result = await service.run(RuntimeAgentRunRequest.model_validate(payload))
            return result, calls
        finally:
            await invoker.close()

    async def _run_faq_response(self, message, envelope):
        graph, faq_repository = self._faq_dependencies()
        business = FakeRunner(navigation_plan_payload())
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=business,
            intent_graph=graph,
            faq_repository=faq_repository,
        )
        payload = request_payload()
        payload["message"] = message
        result = await service.run(RuntimeAgentRunRequest.model_validate(payload))
        return result, business

    def test_faq_match_becomes_the_only_authoritative_candidate(self):
        intent_graph, faq_repository = self._faq_dependencies()
        payload = request_payload()
        payload["message"] = "转账多久到账"

        candidates = recall_intent_constraints(
            RuntimeAgentRunRequest.model_validate(payload),
            intent_graph,
            faq_repository,
        )

        self.assertEqual(
            [item.intent_id for item in candidates], ["ccb.home.transfer.faq"]
        )
        self.assertEqual(candidates[0].execution_mode, "knowledge-only")
        self.assertEqual(candidates[0].score, 240)
        self.assertEqual(candidates[0].evidence, ["faq_pattern"])
        self.assertEqual(candidates[0].constraint, "required")

    def test_generic_transfer_does_not_match_faq_and_keeps_graph_recall(self):
        intent_graph, faq_repository = self._faq_dependencies()
        payload = request_payload()
        payload["message"] = "转账"
        request = RuntimeAgentRunRequest.model_validate(payload)

        candidates = recall_intent_constraints(
            request, intent_graph, faq_repository
        )

        self.assertIsNone(faq_repository.match("转账"))
        self.assertEqual(
            [item.intent_id for item in candidates],
            [
                item.intent_id
                for item in intent_graph.recall(
                    request.message,
                    page_context=request.page_context.model_dump(
                        by_alias=True, exclude_none=True
                    ),
                )
            ],
        )
        self.assertTrue(all("faq_pattern" not in item.evidence for item in candidates))

    def test_dominant_graph_match_is_required_without_hiding_multi_intent_text(self):
        intent_graph, _faq_repository = self._faq_dependencies()
        for message in ("给我推荐理财产品", "有哪些理财产品推荐"):
            with self.subTest(message=message):
                payload = request_payload()
                payload["message"] = message
                advisory = balance_capability_payload(
                    "bank.wealth.advisory.context.get"
                )
                payload["plannableCapabilities"].append(advisory)
                payload["executableCapabilities"].append(advisory)
                request = RuntimeAgentRunRequest.model_validate(payload)

                candidates = recall_intent_constraints(
                    request,
                    intent_graph,
                    shape_assessment=analyze_intent_shape(request.message),
                )

                self.assertEqual(
                    [item.intent_id for item in candidates],
                    ["ccb.wealth.advisor.recommend"],
                )
                self.assertEqual(candidates[0].constraint, "required")
                self.assertTrue(candidates[0].shape_authoritative)

    def test_faq_and_residual_business_semantics_are_recalled_together(self):
        intent_graph, faq_repository = self._faq_dependencies()
        cases = {
            "转账多久到账，再查一下工资卡余额": {
                "ccb.home.transfer.faq", "ccb.account.balance.query",
            },
            "信用卡丢了怎么办，然后查工资卡余额": {
                "ccb.credit.card.faq", "ccb.account.balance.query",
            },
            "转账多久到账，给张三转100元": {
                "ccb.home.transfer.faq", "ccb.transfer.create",
            },
        }
        for message, expected_ids in cases.items():
            with self.subTest(message=message):
                payload = request_payload()
                payload["message"] = message
                if "余额" in message:
                    for capability_id in (
                        "bank.account.balance.get", "bank.account.detail.get",
                    ):
                        capability = balance_capability_payload(capability_id)
                        payload["plannableCapabilities"].append(capability)
                        payload["executableCapabilities"].append(capability)
                candidates = recall_intent_constraints(
                    RuntimeAgentRunRequest.model_validate(payload),
                    intent_graph,
                    faq_repository,
                )

                self.assertEqual(
                    {item.intent_id for item in candidates}, expected_ids
                )
                self.assertTrue(all(
                    item.required_shape == "multi_intent" for item in candidates
                ))

    def test_two_faqs_do_not_collapse_to_one_single_intent_task(self):
        intent_graph, faq_repository = self._faq_dependencies()
        payload = request_payload()
        payload["message"] = "转账多久到账，转账收手续费吗"

        candidates = recall_intent_constraints(
            RuntimeAgentRunRequest.model_validate(payload),
            intent_graph,
            faq_repository,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].intent_id, "ccb.home.transfer.faq")
        self.assertEqual(candidates[0].required_shape, "same_intent_multi_task")
        self.assertIn("faq_multiple", candidates[0].evidence)

    def test_explicit_faq_rephrases_and_uncovered_knowledge_gate(self):
        intent_graph, faq_repository = self._faq_dependencies()
        cases = (
            "转账多久到帐",
            "请问转账多长时间能到",
            "转账费怎么收",
        )
        for message in cases:
            with self.subTest(message=message):
                payload = request_payload()
                payload["message"] = message
                candidates = recall_intent_constraints(
                    RuntimeAgentRunRequest.model_validate(payload),
                    intent_graph,
                    faq_repository,
                )
                self.assertEqual(
                    [item.intent_id for item in candidates],
                    ["ccb.home.transfer.faq"],
                )
                self.assertEqual(candidates[0].evidence, ["faq_pattern"])

        payload = request_payload()
        payload["message"] = "转账后可以撤回吗"
        uncovered = recall_intent_constraints(
            RuntimeAgentRunRequest.model_validate(payload),
            intent_graph,
            faq_repository,
        )
        self.assertEqual(
            {item.intent_id for item in uncovered},
            {"ccb.home.transfer.faq", "ccb.transfer.create"},
        )
        faq_candidate = next(
            item for item in uncovered if item.intent_id == "ccb.home.transfer.faq"
        )
        self.assertIn("graph_term", faq_candidate.evidence)
        self.assertEqual(faq_candidate.capability_bindings, [])
        self.assertTrue(all(not item.shape_authoritative for item in uncovered))

    def test_transfer_evidence_classification_separates_execution_from_knowledge(self):
        intent_graph, faq_repository = self._faq_dependencies()
        execution_requests = (
            "请帮我转账给张三100元可以吗",
            "我想转账给张三100元，可以吗",
            "转账给张三一百元可以吗",
        )
        knowledge_requests = (
            "给张三转100元要手续费吗",
            "给张三转100元多久到账",
            "给张三转100元有限额吗",
            "给张三转100元为什么失败",
            "给张三转一百元多久到账",
        )

        for message in execution_requests:
            with self.subTest(kind="execution", message=message):
                payload = request_payload()
                payload["message"] = message
                candidates = recall_intent_constraints(
                    RuntimeAgentRunRequest.model_validate(payload),
                    intent_graph,
                    faq_repository,
                )

                self.assertEqual(
                    [item.intent_id for item in candidates], ["ccb.transfer.create"]
                )
                self.assertNotIn("faq_uncovered", candidates[0].evidence)

        for message in knowledge_requests:
            with self.subTest(kind="knowledge", message=message):
                payload = request_payload()
                payload["message"] = message
                candidates = recall_intent_constraints(
                    RuntimeAgentRunRequest.model_validate(payload),
                    intent_graph,
                    faq_repository,
                )

                self.assertEqual(
                    {item.intent_id for item in candidates},
                    {"ccb.home.transfer.faq", "ccb.transfer.create"},
                )
                self.assertTrue(all(not item.shape_authoritative for item in candidates))

    def test_explicit_execution_and_knowledge_evidence_preserves_both_intents(self):
        intent_graph, faq_repository = self._faq_dependencies()
        payload = request_payload()
        payload["message"] = "请帮我给张三转100元，并告诉我多久到账"

        candidates = recall_intent_constraints(
            RuntimeAgentRunRequest.model_validate(payload),
            intent_graph,
            faq_repository,
        )

        self.assertEqual(
            {item.intent_id for item in candidates},
            {"ccb.home.transfer.faq", "ccb.transfer.create"},
        )
        self.assertTrue(all(not item.shape_authoritative for item in candidates))
        self.assertTrue(all(item.required_shape == "single_intent" for item in candidates))

    async def test_service_injects_faq_constraint_into_both_validation_passes(self):
        intent_graph, faq_repository = self._faq_dependencies()
        faq_envelope = envelope_payload(tasks=[{
            "task_id": "task_faq",
            "intent_id": "ccb.home.transfer.faq",
            "action": "HELP",
            "business_object": "transfer",
            "slots": {},
            "dependencies": [],
            "execution_readiness": "ready",
            "risk_level": "L0",
        }])
        business = FakeRunner({
            "schema_version": "1.0",
            "business_agent_run_id": "bar_faq",
            "delegation_id": "dlg_model",
            "status": "navigation_only",
            "steps": [],
        })
        service = DualDeepAgentService(
            user_runner=FakeRunner(faq_envelope),
            business_runner=business,
            intent_graph=intent_graph,
            faq_repository=faq_repository,
        )
        payload = request_payload()
        payload["message"] = "转账多久到账"

        result = await service.run(RuntimeAgentRunRequest.model_validate(payload))

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.user_response.type, "answer")
        self.assertEqual(business.inputs[0]["capabilities"], [])

    async def test_knowledge_only_intent_rejects_non_navigation_plan(self):
        intent_graph, faq_repository = self._faq_dependencies()
        faq_envelope = envelope_payload(tasks=[{
            "task_id": "task_faq",
            "intent_id": "ccb.home.transfer.faq",
            "action": "HELP",
            "business_object": "transfer",
            "slots": {},
            "dependencies": [],
            "execution_readiness": "ready",
            "risk_level": "L0",
        }])
        service = DualDeepAgentService(
            user_runner=FakeRunner(faq_envelope),
            business_runner=FakeRunner(clarification_plan_payload()),
            intent_graph=intent_graph,
            faq_repository=faq_repository,
        )
        payload = request_payload()
        payload["message"] = "转账多久到账"

        result = await service.run(RuntimeAgentRunRequest.model_validate(payload))
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.user_response.type, "notice")

    async def test_knowledge_only_request_cannot_reach_node_with_malicious_graph_binding(self):
        payload = json.loads(Path(
            os.getenv(
                "AI_BANK_INTENT_GRAPH_PATH",
                "/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v1.0.0.json",
            )
        ).read_text(encoding="utf-8"))
        faq_node = next(
            item for item in payload["nodes"]
            if item["id"] == "ccb.home.transfer.faq"
        )
        faq_node["capabilityBindings"] = ["bank.account.list"]
        malicious_graph = IntentGraphRepository(payload, Path("malicious.json"))
        faq_repository = MockFaqRepository.load(
            os.getenv(
                "AI_BANK_FAQ_PATH",
                "/app/data/ai-native-bank/faq/ccb-mobile-bank-faq.v1.json",
            ),
            valid_action_keys=malicious_graph.action_keys,
        )
        node_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            node_calls.append(json.loads(request.content))
            return httpx.Response(200, json={
                "data": {"accounts": []}, "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        faq_envelope = envelope_payload(tasks=[{
            "task_id": "task_faq",
            "intent_id": "ccb.home.transfer.faq",
            "action": "HELP",
            "business_object": "transfer",
            "slots": {},
            "dependencies": [],
            "execution_readiness": "ready",
            "risk_level": "L0",
        }])
        navigation_plan = {
            "schema_version": "1.0",
            "business_agent_run_id": "bar_faq",
            "delegation_id": "dlg_model",
            "status": "navigation_only",
            "steps": [],
        }
        business = ToolCallingRunner(
            navigation_plan, invoker, [("bank.account.list", {})]
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(faq_envelope),
            business_runner=business,
            intent_graph=malicious_graph,
            capability_invoker=invoker,
            faq_repository=faq_repository,
        )
        request = request_payload()
        request["message"] = "转账多久到账"

        events = [
            item async for item in service.stream(
                RuntimeAgentRunRequest.model_validate(request)
            )
        ]

        self.assertEqual(node_calls, [])
        self.assertEqual(events[-1].stage, "run.failed")
        self.assertEqual(events[-1].error.code, "agent_runtime_failed")
        self.assertEqual(len(business.inputs), 0)
        await invoker.close()

    async def test_knowledge_only_successful_invocation_is_rejected_without_repair(self):
        intent_graph, faq_repository = self._faq_dependencies()
        faq_envelope = envelope_payload(tasks=[{
            "task_id": "task_faq",
            "intent_id": "ccb.home.transfer.faq",
            "action": "HELP",
            "business_object": "transfer",
            "slots": {},
            "dependencies": [],
            "execution_readiness": "ready",
            "risk_level": "L0",
        }])
        navigation_plan = {
            "schema_version": "1.0",
            "business_agent_run_id": "bar_faq",
            "delegation_id": "dlg_model",
            "status": "navigation_only",
            "steps": [],
        }

        class InjectedInvocationService(DualDeepAgentService):
            business_runs = 0

            async def _run_business(self, *args, **kwargs):
                self.business_runs += 1
                return navigation_plan, [{
                    "capability_id": "bank.account.list",
                    "input": {},
                    "output": {"accounts": []},
                }]

        service = InjectedInvocationService(
            user_runner=FakeRunner(faq_envelope),
            business_runner=FakeRunner(navigation_plan),
            intent_graph=intent_graph,
            faq_repository=faq_repository,
        )
        payload = request_payload()
        payload["message"] = "转账多久到账"

        events = [
            item async for item in service.stream(
                RuntimeAgentRunRequest.model_validate(payload)
            )
        ]

        self.assertEqual(service.business_runs, 1)
        self.assertEqual(events[-1].error.code, "knowledge_only_capability_violation")
        self.assertNotIn("business_agent.completed", [item.stage for item in events])

    async def test_governed_single_faq_returns_answer_with_source_and_registered_actions(self):
        result, business = await self._run_faq_response(
            "转账多久到账",
            envelope_payload(tasks=[faq_task_payload()]),
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.user_response.type, "answer")
        self.assertEqual(
            result.user_response.source.id, "faq.transfer.arrival-time"
        )
        self.assertTrue(result.user_response.actions)
        self.assertTrue(
            all(action.kind == "navigate" for action in result.user_response.actions)
        )
        self.assertEqual(business.inputs[0]["capabilities"], [])

    async def test_faq_user_content_is_separate_from_safe_trace_projection(self):
        graph, faq_repository = self._faq_dependencies()
        entry = faq_repository.match("转账多久到账")
        service = DualDeepAgentService(
            user_runner=FakeRunner(
                envelope_payload(tasks=[faq_task_payload()])
            ),
            business_runner=FakeRunner(navigation_plan_payload()),
            intent_graph=graph,
            faq_repository=faq_repository,
        )
        payload = request_payload()
        payload["message"] = "转账多久到账"

        pairs = [
            item async for item in service.stream_with_result(
                RuntimeAgentRunRequest.model_validate(payload)
            )
        ]
        terminal_trace = pairs[-1][0].model_dump_json(by_alias=True)

        self.assertEqual(pairs[-1][1].user_response.body, entry.answer)
        self.assertNotIn(entry.answer, terminal_trace)
        self.assertNotIn("userResponse", terminal_trace)
        self.assertNotIn("recommendedActionKeys", terminal_trace)

    async def test_projection_exception_becomes_failed_trace_and_safe_notice(self):
        graph, faq_repository = self._faq_dependencies()
        service = DualDeepAgentService(
            user_runner=FakeRunner(
                envelope_payload(tasks=[faq_task_payload()])
            ),
            business_runner=FakeRunner(navigation_plan_payload()),
            intent_graph=graph,
            faq_repository=faq_repository,
        )
        payload = request_payload()
        payload["message"] = "转账多久到账"

        with patch.object(
            service._user_response_projector,
            "project_faq",
            side_effect=RuntimeError("private-projection-detail"),
        ):
            pairs = [
                item async for item in service.stream_with_result(
                    RuntimeAgentRunRequest.model_validate(payload)
                )
            ]

        self.assertEqual(pairs[-1][0].stage, "run.failed")
        self.assertEqual(pairs[-1][1].status, "blocked")
        serialized = pairs[-1][1].model_dump_json(by_alias=True)
        self.assertNotIn("private-projection-detail", serialized)

    async def test_uncovered_faq_returns_explicit_notice_without_guessing(self):
        result, _business = await self._run_faq_response(
            "转账后可以撤回吗",
            envelope_payload(tasks=[faq_task_payload()]),
        )

        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.user_response.type, "notice")
        self.assertIn("不会猜测", result.user_response.body)
        self.assertTrue(result.user_response.actions)
        self.assertIsNone(result.user_response.source)

    async def test_two_faq_matches_require_user_to_choose_a_canonical_question(self):
        result, _business = await self._run_faq_response(
            "转账多久到账，转账收手续费吗",
            envelope_payload(
                "same_intent_multi_task",
                [faq_task_payload("task_arrival"), faq_task_payload("task_fee")],
            ),
        )

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.type, "clarification")
        self.assertEqual(
            [action.payload.message for action in result.user_response.actions],
            ["转账多久到账", "转账收手续费吗"],
        )
        self.assertEqual(result.capability_plan.status, "clarification_required")
        self.assertEqual(result.capability_plan.steps, [])

    async def test_faq_and_balance_composite_is_split_before_balance_execution(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            return httpx.Response(
                200,
                json={"data": {"accounts": [TWO_ACTIVE_ACCOUNTS[0]]}, "meta": {}},
            )

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        mixed_envelope = envelope_payload(
            "multi_intent",
            [
                faq_task_payload(),
                balance_envelope_payload()["tasks"][0] | {"task_id": "task_balance"},
            ],
        )
        graph, faq_repository = self._faq_dependencies()
        service = DualDeepAgentService(
            user_runner=FakeRunner(mixed_envelope),
            business_runner=ToolCallingRunner(
                balance_discovery_plan_payload(),
                invoker,
                [("bank.account.list", {})],
            ),
            intent_graph=graph,
            capability_invoker=invoker,
            faq_repository=faq_repository,
        )
        payload = balance_request_payload()
        payload["message"] = "转账多久到账，再查一下工资卡余额"

        try:
            result = await service.run(RuntimeAgentRunRequest.model_validate(payload))
        finally:
            await invoker.close()

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.type, "clarification")
        self.assertEqual(calls, [])
        self.assertEqual(service._business_runner.inputs, [])
        replies = [action.payload.message for action in result.user_response.actions]
        self.assertIn("转账多久到账", replies)
        self.assertIn("查询账户余额", replies)
        self.assertEqual(result.capability_plan.steps, [])

    async def test_faq_and_transfer_composite_never_starts_business_or_calls_node(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"accounts": []}, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        transfer_task = envelope_payload()["tasks"][0]
        mixed_envelope = envelope_payload(
            "multi_intent",
            [faq_task_payload(), transfer_task],
        )
        graph, faq_repository = self._faq_dependencies()
        business = ToolCallingRunner(
            clarification_plan_payload(), invoker, [("bank.account.list", {})]
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(mixed_envelope),
            business_runner=business,
            intent_graph=graph,
            capability_invoker=invoker,
            faq_repository=faq_repository,
        )
        payload = request_payload()
        payload["message"] = "转账多久到账，再给张三转 500 元"
        try:
            result = await service.run(RuntimeAgentRunRequest.model_validate(payload))
        finally:
            await invoker.close()

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.capability_plan.steps, [])
        self.assertEqual(business.inputs, [])
        self.assertEqual(calls, [])

    async def test_faq_load_failure_has_dedicated_fail_closed_runtime_state(self):
        from app.config import RuntimeSettings
        from app.main import create_app

        settings = RuntimeSettings(
            runtime_token="runtime-test-token",
            model_provider="openai",
            model_name="mock-model",
            model_base_url="http://127.0.0.1:9/v1",
            model_api_key="model-test-key",
        )
        fake_bundle = type("Bundle", (), {
            "user": FakeRunner(envelope_payload()),
            "business": FakeRunner(plan_payload()),
            "capability_invoker": None,
        })()
        graph = IntentGraphRepository.load(
            os.getenv(
                "AI_BANK_INTENT_GRAPH_PATH",
                "/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v1.0.0.json",
            )
        )
        with (
            patch("app.main.OpenJiuwenCheckpointerManager.install", return_value=None),
            patch("app.main.DeepAgentFactory.create", return_value=fake_bundle),
            patch("app.main.IntentGraphRepository.load", return_value=graph),
            patch("app.main.MockFaqRepository.load", side_effect=FaqError("invalid")),
        ):
            app = create_app(settings)
            async with app.router.lifespan_context(app):
                self.assertEqual(
                    app.state.startup_error,
                    "faq_repository_invalid",
                )

    async def test_faq_binding_with_capability_fails_before_agent_factory_startup(self):
        from app.config import RuntimeSettings
        from app.main import create_app

        graph_payload = json.loads(Path(
            os.getenv(
                "AI_BANK_INTENT_GRAPH_PATH",
                "/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v1.0.0.json",
            )
        ).read_text(encoding="utf-8"))
        next(
            item for item in graph_payload["nodes"]
            if item["id"] == "ccb.home.transfer.faq"
        )["capabilityBindings"] = ["bank.account.list"]
        malicious_graph = IntentGraphRepository(
            graph_payload, Path("malicious.json")
        )
        faq_repository = MockFaqRepository.load(
            os.getenv(
                "AI_BANK_FAQ_PATH",
                "/app/data/ai-native-bank/faq/ccb-mobile-bank-faq.v1.json",
            ),
            valid_action_keys=malicious_graph.action_keys,
        )
        settings = RuntimeSettings(
            runtime_token="runtime-test-token",
            model_provider="openai",
            model_name="mock-model",
            model_base_url="http://127.0.0.1:9/v1",
            model_api_key="model-test-key",
        )
        with (
            patch("app.main.OpenJiuwenCheckpointerManager.install", return_value=None),
            patch("app.main.IntentGraphRepository.load", return_value=malicious_graph),
            patch("app.main.MockFaqRepository.load", return_value=faq_repository),
            patch("app.main.DeepAgentFactory.create") as create_factory,
        ):
            app = create_app(settings)
            async with app.router.lifespan_context(app):
                self.assertEqual(
                    app.state.startup_error,
                    "faq_repository_invalid",
                )
                create_factory.assert_not_called()

    def test_user_result_is_deterministically_projected_from_trusted_tool_output(self):
        result = project_user_result([{
            "capability_id": "bank.credit-card.list",
            "input": {},
            "output": {"cards": [{
                "id": "credit_global_8891", "maskedNo": "****8891",
                "productName": "龙卡全球支付卡", "status": "ACTIVE",
                "availableLimit": {"formatted": "¥46,328.00"},
            }]},
        }])
        self.assertEqual(result.kind, "credit_card_list")
        self.assertIn("****8891", result.items[0])
        self.assertNotIn("credit_global_8891", result.model_dump_json())

    async def test_successful_read_run_uses_tool_output_and_returns_succeeded(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.assertEqual(body["capabilityId"], "bank.credit-card.list")
            return httpx.Response(200, json={"data": {"cards": [{
                "id": "credit_global_8891", "maskedNo": "****8891",
                "productName": "龙卡全球支付卡", "status": "ACTIVE",
                "availableLimit": {"formatted": "¥46,328.00"},
            }]}, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal", service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        request = request_payload()
        request.update({
            "message": "查询本人信用卡、状态和额度",
            "pageContext": {"tab": "credit", "menuKey": "credit.card-manage"},
            "executableCapabilities": [balance_capability_payload("bank.credit-card.list")],
            "plannableCapabilities": [balance_capability_payload("bank.credit-card.list")],
        })
        envelope = envelope_payload(tasks=[{
            "task_id": "task_card", "intent_id": "ccb.credit.card.query", "action": "QUERY",
            "business_object": "credit_card", "slots": {}, "dependencies": [],
            "execution_readiness": "ready", "risk_level": "L1",
        }])
        plan = {
            "schema_version": "1.0", "business_agent_run_id": "bar_card",
            "delegation_id": "dlg_model", "status": "planned", "steps": [{
                "step_id": "step_cards", "task_id": "task_card",
                "capability_id": "bank.credit-card.list", "input_bindings": {},
                "requires_user_confirmation": False, "depends_on": [],
            }],
        }
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=ToolCallingRunner(plan, invoker, [("bank.credit-card.list", {})]),
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.user_result.kind, "credit_card_list")
        self.assertEqual(result.user_response.type, "result")
        self.assertIsNone(result.confirmation)
        await invoker.close()

    async def test_validated_ready_read_plan_is_executed_when_model_only_submits_plan(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            return httpx.Response(200, json={"data": {"cards": [{
                "id": "credit_global_8891", "maskedNo": "****8891",
                "productName": "龙卡全球支付卡", "status": "ACTIVE",
                "availableLimit": {"formatted": "¥46,328.00"},
            }]}, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal", service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        request = request_payload()
        request.update({
            "message": "查一下我的信用卡",
            "pageContext": {"tab": "credit", "menuKey": "credit.card-package"},
            "executableCapabilities": [balance_capability_payload("bank.credit-card.list")],
            "plannableCapabilities": [balance_capability_payload("bank.credit-card.list")],
        })
        envelope = envelope_payload(tasks=[{
            "task_id": "task_card", "intent_id": "ccb.credit.card.query", "action": "QUERY",
            "business_object": "credit_card", "slots": {}, "dependencies": [],
            "execution_readiness": "ready", "risk_level": "L1",
        }])
        plan = {
            "schema_version": "1.0", "business_agent_run_id": "bar_card",
            "delegation_id": "dlg_model", "status": "planned", "steps": [{
                "step_id": "step_cards", "task_id": "task_card",
                "capability_id": "bank.credit-card.list", "input_bindings": {},
                "requires_user_confirmation": False, "depends_on": [],
            }],
        }
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope), business_runner=FakeRunner(plan),
            capability_invoker=invoker,
        )

        events = [item async for item in service.stream(RuntimeAgentRunRequest.model_validate(request))]
        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.user_result.kind, "credit_card_list")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(item["capabilityId"] == "bank.credit-card.list" for item in calls))
        self.assertIn("capability.invocation_completed", [item.stage for item in events])
        await invoker.close()

    async def test_confirm_required_plan_creates_checkpoint_without_exposing_saved_input(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            if request.url.path.endswith("agent-capability-invocations"):
                return httpx.Response(200, json={"data": {"cards": [{
                    "id": "credit_global_8891", "maskedNo": "****8891",
                    "productName": "Global Payment Card", "status": "ACTIVE",
                }]}, "meta": {}})
            return httpx.Response(201, json={"data": {
                "checkpointId": "acp_runtime_001",
                "capabilityId": "bank.credit-card.loss-report.create",
                "confirmationToken": "c" * 43,
                "expiresAt": "2026-08-30T10:05:00.000Z",
                "summary": {
                    "title": "请确认信用卡挂失", "description": "挂失本人信用卡",
                    "riskLevel": "L2",
                },
            }, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        capability = balance_capability_payload(
            "bank.credit-card.loss-report.create", side_effect="state-change"
        )
        capability["exposure"] = "user-action"
        capability["title"] = "信用卡挂失"
        capability["description"] = "挂失本人信用卡"
        capability["riskLevel"] = "L2"
        request = request_payload()
        request.update({
            "message": "挂失信用卡", "pageContext": {"tab": "credit", "menuKey": "credit.card-manage"},
            "executableCapabilities": [balance_capability_payload("bank.credit-card.list")],
            "plannableCapabilities": [
                balance_capability_payload("bank.credit-card.list"), capability,
            ],
        })
        envelope = envelope_payload(tasks=[{
            "task_id": "task_loss", "intent_id": "ccb.credit.card.loss-report", "action": "CHANGE",
            "business_object": "credit_card", "slots": {
                "cardId": {"value": "credit_global_8891", "source": "user_selection", "confidence": 1},
                "reason": {"value": "CARD_MISSING", "source": "user_explicit", "confidence": 1},
            }, "dependencies": [],
            "execution_readiness": "ready", "risk_level": "L2",
        }])
        plan = {
            "schema_version": "1.0", "business_agent_run_id": "bar_loss",
            "delegation_id": "dlg_model", "status": "planned", "steps": [{
                "step_id": "step_loss", "task_id": "task_loss",
                "capability_id": "bank.credit-card.loss-report.create",
                "input_bindings": {"cardId": "credit_global_8891", "reason": "CARD_MISSING"},
                "requires_user_confirmation": True, "depends_on": [],
            }],
        }
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=ToolCallingRunner(
                plan, invoker, [("bank.credit-card.list", {})]
            ),
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "awaiting_confirmation")
        self.assertEqual(result.confirmation.capability_id, "bank.credit-card.loss-report.create")
        self.assertEqual(result.user_response.type, "confirmation")
        self.assertNotIn(
            result.confirmation.confirmation_token,
            result.user_response.model_dump_json(by_alias=True),
        )
        self.assertNotIn("normalizedInput", result.model_dump_json(by_alias=True))
        self.assertEqual(captured[-1]["input"]["cardId"], "credit_global_8891")
        await invoker.close()

    async def test_transfer_discovery_repairs_clarification_with_unique_owned_entities(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append((request.url.path, body))
            if request.url.path.endswith("agent-action-checkpoints"):
                return httpx.Response(201, json={"data": {
                    "checkpointId": "acp_transfer_repair",
                    "capabilityId": "bank.transfer.order.create",
                    "confirmationToken": "t" * 43,
                    "expiresAt": "2026-08-30T10:05:00.000Z",
                    "summary": {
                        "title": "请确认创建转账指令",
                        "description": "创建不产生资金移动的转账草稿",
                        "riskLevel": "L1",
                    },
                }, "meta": {}})
            outputs = {
                "bank.account.list": {"accounts": [{
                    "id": "acct_salary_6217", "name": "工资卡",
                    "maskedNo": "****6217", "status": "ACTIVE",
                }]},
                "bank.payee.list": {"payees": [{
                    "id": "payee_zhang_4321", "name": "张伟",
                    "maskedNo": "****4321", "status": "ACTIVE",
                }]},
                "bank.payee.validate": {
                    "payeeId": "payee_zhang_4321", "valid": True,
                    "status": "ACTIVE", "checkedAt": "2026-08-30T10:00:00.000Z",
                },
            }
            return httpx.Response(200, json={"data": outputs[body["capabilityId"]], "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        ids = [
            "bank.account.list", "bank.account.balance.get", "bank.account.detail.get", "bank.transfer.status.get",
            "bank.payee.list", "bank.payee.validate",
            "bank.transfer.order.create",
        ]
        capabilities = [balance_capability_payload(item) for item in ids]
        capabilities[-1]["sideEffect"] = "state-change"
        request = request_payload()
        request.update({
            "message": "从工资卡给张伟转100元",
            "pageContext": {"tab": "home", "menuKey": "home.transfer"},
            "executableCapabilities": capabilities[:-1],
            "plannableCapabilities": capabilities,
        })
        envelope = envelope_payload(tasks=[{
            "task_id": "task_transfer", "intent_id": "ccb.transfer.create",
            "action": "DO", "business_object": "transfer_order",
            "slots": {
                "fromAccountName": {"value": "工资卡", "source": "user_explicit", "confidence": 1},
                "payeeName": {"value": "张伟", "source": "user_explicit", "confidence": 1},
                "amountMinor": {"value": 10000, "source": "user_explicit", "confidence": 1},
                "currency": {"value": "CNY", "source": "intent_default", "confidence": 1},
            },
            "dependencies": [], "execution_readiness": "needs_slots", "risk_level": "L2",
        }])
        repaired_plan = {
            "schema_version": "1.0", "business_agent_run_id": "bar_transfer",
            "delegation_id": "dlg_model", "status": "planned", "steps": [{
                "step_id": "step_transfer", "task_id": "task_transfer",
                "capability_id": "bank.transfer.order.create",
                "input_bindings": {
                    "fromAccountId": "acct_salary_6217", "payeeId": "payee_zhang_4321",
                    "amountMinor": 10000, "currency": "CNY",
                },
                "requires_user_confirmation": True, "depends_on": [],
            }],
        }
        business = FirstRunToolCallingRunner(
            [clarification_plan_payload(), repaired_plan], invoker,
            [("bank.account.list", {}), ("bank.payee.list", {})],
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope), business_runner=business,
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "awaiting_confirmation")
        self.assertEqual(result.confirmation.capability_id, "bank.transfer.order.create")
        self.assertEqual(result.conversation_state.status, "retain")
        self.assertEqual(
            business.inputs[1]["confirmation_resolution"]["resolved_input"],
            repaired_plan["steps"][0]["input_bindings"],
        )
        capability_calls = [body["capabilityId"] for path, body in captured if path.endswith("invocations")]
        self.assertEqual(capability_calls, [
            "bank.account.list", "bank.payee.list", "bank.payee.validate",
        ])
        await invoker.close()

    async def test_multi_transfer_plan_creates_only_the_first_task_checkpoint(self):
        captured = []
        checkpoint_number = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal checkpoint_number
            body = json.loads(request.content)
            captured.append((request.url.path, body))
            if request.url.path.endswith("agent-action-checkpoints"):
                checkpoint_number += 1
                marker = "zhang" if checkpoint_number == 1 else "li"
                return httpx.Response(201, json={"data": {
                    "checkpointId": f"acp_batch_{marker}",
                    "capabilityId": "bank.transfer.order.create",
                    "confirmationToken": marker[0] * 43,
                    "expiresAt": "2026-08-30T10:05:00.000Z",
                    "summary": {
                        "title": "请确认创建转账指令",
                        "description": "创建不产生资金移动的转账草稿",
                        "riskLevel": "L2",
                    },
                }, "meta": {}})
            if request.url.path.endswith("agent-workflows"):
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": body["businessWorkflowId"],
                    "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            raise AssertionError(request.url.path)

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        request = request_payload()
        request["message"] = "从工资卡给张帅和李四各转100元"
        balance_capability = balance_capability_payload(
            "bank.account.balance.get"
        )
        request["executableCapabilities"].append(balance_capability)
        request["plannableCapabilities"].append(balance_capability)
        tasks = []
        steps = []
        for index, (task_id, payee, payee_id) in enumerate((
            ("task_zhang", "张帅", "payee_zhang_4321"),
            ("task_li", "李四", "payee_zhangsan_7319"),
        ), 1):
            tasks.append({
                "task_id": task_id, "intent_id": "ccb.transfer.create",
                "action": "DO", "business_object": "transfer_order",
                "slots": {
                    "payeeName": {"value": payee, "source": "user_explicit", "confidence": 1},
                    "amountMinor": {"value": 10000, "source": "user_explicit", "confidence": 1},
                    "fromAccountName": {"value": "工资卡", "source": "user_explicit", "confidence": 1},
                },
                "dependencies": [], "execution_readiness": "ready", "risk_level": "L2",
            })
            steps.append({
                "step_id": f"step_{index}", "task_id": task_id,
                "capability_id": "bank.transfer.order.create",
                "input_bindings": {
                    "fromAccountId": "acct_salary_6217", "payeeId": payee_id,
                    "amountMinor": 10000, "currency": "CNY",
                },
                "requires_user_confirmation": True, "depends_on": [],
            })
        envelope = envelope_payload("same_intent_multi_task", tasks)
        plan = {
            "schema_version": "1.0", "business_agent_run_id": "bar_batch",
            "delegation_id": "dlg_model", "status": "planned", "steps": steps,
        }
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope),
            business_runner=TodoSnapshotRunner(plan, ["task_zhang", "task_li"]),
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "awaiting_confirmation")
        self.assertEqual(result.confirmation.summary.task_count, 1)
        self.assertEqual(result.confirmation.summary.total_amount_minor, 10000)
        self.assertEqual(result.confirmation.summary.items, [
            "1. 张帅  100.00元",
        ])
        self.assertEqual(result.confirmation.confirmations, [])
        self.assertEqual(result.user_response.task_progress.current, 1)
        self.assertEqual(result.user_response.task_progress.total, 2)
        self.assertEqual(result.user_response.task_progress.completed, 0)
        self.assertEqual(result.user_response.items, (
            "1. 张帅  100.00元",
        ))
        self.assertNotIn(
            "confirmationToken", result.user_response.model_dump_json(by_alias=True)
        )
        checkpoint_calls = [
            body for path, body in captured
            if path.endswith("agent-action-checkpoints")
        ]
        self.assertEqual([item["taskId"] for item in checkpoint_calls], [
            "task_zhang",
        ])

    async def test_native_business_loop_projects_one_transfer_confirmation(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append((request.url.path, body))
            if request.url.path.endswith("agent-workflows"):
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": body["businessWorkflowId"],
                    "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            if request.url.path.endswith("agent-action-checkpoints"):
                return httpx.Response(201, json={"data": {
                    "checkpointId": "acp_native_transfer",
                    "capabilityId": "bank.transfer.order.create",
                    "confirmationToken": "n" * 43,
                    "expiresAt": "2026-08-30T10:05:00.000Z",
                    "summary": {
                        "title": "请确认创建转账指令",
                        "description": "创建不产生资金移动的转账草稿",
                        "riskLevel": "L2",
                    },
                }, "meta": {}})
            outputs = {
                "bank.account.list": {"accounts": [{
                    "id": "acct_salary_6217", "name": "工资卡",
                    "maskedNo": "****6217", "status": "ACTIVE",
                }]},
                "bank.payee.list": {"payees": [{
                    "id": "payee_zhang_4321", "name": "张伟",
                    "maskedNo": "****4321", "status": "ACTIVE",
                }]},
                "bank.payee.validate": {
                    "payeeId": "payee_zhang_4321", "valid": True,
                    "status": "ACTIVE", "checkedAt": "2026-08-30T10:00:00.000Z",
                },
            }
            return httpx.Response(200, json={"data": outputs[body["capabilityId"]], "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        ids = [
            "bank.account.list", "bank.account.balance.get", "bank.account.detail.get", "bank.transfer.status.get", "bank.payee.list",
            "bank.payee.validate", "bank.transfer.order.create",
        ]
        capabilities = [balance_capability_payload(item) for item in ids]
        capabilities[-1]["sideEffect"] = "state-change"
        request = request_payload()
        request.update({
            "message": "从工资卡给张伟转100元",
            "executableCapabilities": capabilities[:-1],
            "plannableCapabilities": capabilities,
        })
        envelope = envelope_payload(tasks=[{
            "task_id": "task_transfer", "intent_id": "ccb.transfer.create",
            "action": "DO", "business_object": "transfer_order",
            "slots": {
                "fromAccountName": {"value": "工资卡", "source": "user_explicit", "confidence": 1},
                "payeeName": {"value": "张伟", "source": "user_explicit", "confidence": 1},
                "amountMinor": {"value": 10000, "source": "user_explicit", "confidence": 1},
                "currency": {"value": "CNY", "source": "intent_default", "confidence": 1},
            },
            "dependencies": [], "execution_readiness": "needs_slots", "risk_level": "L2",
        }])
        workflow_ids = []

        def outcome(payload):
            workflow_ids.append(payload["business_workflow_id"])
            return {
                "schema_version": "2.0",
                "business_workflow_id": payload["business_workflow_id"],
                "workflow_revision": 1, "status": "awaiting_confirmation",
                "covered_task_ids": ["task_transfer"],
                "active_goal_task_id": "task_transfer",
                "active_todo_id": "task_transfer:collect",
                "todo_revision": 1, "next_action": "confirm_transfer",
            }
        business = NativeLoopToolCallingRunner(
            outcome, invoker,
            [("bank.account.list", {}), ("bank.payee.list", {})],
            [
                TodoSnapshotItem(id="task_transfer", status="in_progress", depends_on=[]),
                TodoSnapshotItem(id="task_transfer:collect", status="in_progress", depends_on=[]),
            ],
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope), business_runner=business,
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "awaiting_confirmation")
        self.assertEqual(result.confirmation.capability_id, "bank.transfer.order.create")
        self.assertEqual(business.session_ids, [
            f"business:{request['customerId']}:{workflow_ids[0]}",
        ])
        workflow_calls = [body for path, body in captured if path.endswith("agent-workflows")]
        self.assertEqual([item["workflowRevision"] for item in workflow_calls], [1, 2])
        self.assertEqual(workflow_calls[-1]["activeGoalTaskId"], "task_transfer")

    async def test_completed_guided_salary_loop_projects_the_trusted_read_result(self):
        captured = []

        def handler(http_request):
            body = json.loads(http_request.content)
            captured.append((http_request.url.path, body))
            if http_request.url.path.endswith("agent-workflows"):
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": body["businessWorkflowId"],
                    "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            return httpx.Response(200, json={"data": {
                "salary": {"amountMinor": 1860000, "currency": "CNY"},
                "availableBalance": {"amountMinor": 1860000, "currency": "CNY"},
                "totalObligations": {"amountMinor": 686000, "currency": "CNY"},
                "mandatoryObligations": {"amountMinor": 686000, "currency": "CNY"},
                "plannedRepayment": {"amountMinor": 686000, "currency": "CNY"},
                "uncoveredObligations": {"amountMinor": 0, "currency": "CNY"},
                "livingBudget": {"amountMinor": 500000, "currency": "CNY"},
                "emergencyReserve": {"amountMinor": 300000, "currency": "CNY"},
                "allocatable": {"amountMinor": 374000, "currency": "CNY"},
                "obligationItems": [{
                    "id": "card_1", "type": "credit_card",
                    "displayName": "龙卡账单", "amountMinor": 686000,
                    "dueAt": "2026-09-18", "sourceRefs": ["card_1"],
                }],
                "items": [
                    {"id": "planned_repayment", "amountMinor": 686000, "sourceRefs": ["bill_1"]},
                    {"id": "living_budget", "amountMinor": 500000, "sourceRefs": ["policy:living"]},
                    {"id": "emergency_reserve", "amountMinor": 300000, "sourceRefs": ["policy:reserve"]},
                    {"id": "allocatable", "amountMinor": 374000, "sourceRefs": ["salary_1"]},
                ],
                "generatedAt": "2026-09-03T08:00:00.000Z",
            }, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        request = request_payload()
        request.update({
            "message": "安排本月工资",
            "entryGoal": {
                "goalId": "goal_nba_salary_001",
                "scenarioId": "salary-monthly-plan",
                "scenarioVersion": "1.0.0",
                "recommendationId": "nba_salary_001",
                "targetIntentId": "ccb.cashflow.salary-plan",
                "mode": "guided",
                "contextRefs": ["salary_event_20260903"],
                "workflowTemplateId": "salary-allocation-v1",
            },
            "executableCapabilities": [
                balance_capability_payload("bank.cashflow.salary-plan.get")
            ],
            "plannableCapabilities": [
                balance_capability_payload("bank.cashflow.salary-plan.get")
            ],
        })

        def outcome(payload):
            return {
                "schema_version": "2.0",
                "business_workflow_id": payload["business_workflow_id"],
                "workflow_revision": 1,
                "status": "completed",
                "covered_task_ids": [
                    payload["intent_task_envelope"]["tasks"][0]["task_id"]
                ],
                "active_goal_task_id": None,
                "active_todo_id": None,
                "todo_revision": 1,
                "next_action": None,
            }

        business = NativeLoopToolCallingRunner(
            outcome,
            invoker,
            [("bank.cashflow.salary-plan.get", {})],
            [TodoSnapshotItem(id="salary-plan", status="completed", depends_on=[])],
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(None), business_runner=business,
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.user_result.kind, "salary_monthly_plan")
        self.assertEqual(result.user_response.title, "本月待还与资金安排")
        workflow_calls = [
            body for path, body in captured if path.endswith("agent-workflows")
        ]
        self.assertEqual(workflow_calls[-1]["status"], "completed")
        self.assertIsNone(workflow_calls[-1]["activeGoalTaskId"])

    async def test_chat_followup_restores_native_workflow_revision_before_advancing(self):
        request = request_payload()
        request.update({
            "message": "使用工资卡尾号6217转账",
            "conversationId": "conv_native_followup",
            "agentRunId": "abr_native_followup",
            "conversationContext": {
                "pendingEnvelopeId": "dlg_native_followup",
                "envelopeRevision": 1,
                "intentShape": "single_intent",
                "goals": [{
                    "taskId": "task_transfer",
                    "intentId": "ccb.transfer.create",
                    "status": "ready",
                    "slots": [],
                }],
                "sharedSlots": [],
                "ambiguities": [],
            },
        })
        envelope = envelope_payload(tasks=[{
            "task_id": "task_transfer", "intent_id": "ccb.transfer.create",
            "action": "DO", "business_object": "transfer_order",
            "slots": {
                "fromAccountName": {"value": "工资卡", "source": "user_explicit", "confidence": 1},
                "payeeName": {"value": "张三", "source": "context", "confidence": 1},
                "amountMinor": {"value": 10000, "source": "context", "confidence": 1},
                "currency": {"value": "CNY", "source": "intent_default", "confidence": 1},
            },
            "dependencies": [], "execution_readiness": "ready", "risk_level": "L2",
        }])
        workflow_id = business_workflow_id(
            request["customerId"], request["conversationId"], "dlg_native_followup"
        )
        initial_items = (
            TodoSnapshotItem(id="resolve_from_account", status="in_progress", depends_on=[]),
            TodoSnapshotItem(id="resolve_payee", status="completed", depends_on=[]),
            TodoSnapshotItem(id="create_transfer_order", status="pending", depends_on=[]),
        )
        resumed_items = (
            TodoSnapshotItem(id="resolve_from_account", status="completed", depends_on=[]),
            TodoSnapshotItem(id="resolve_payee", status="completed", depends_on=[]),
            TodoSnapshotItem(id="create_transfer_order", status="in_progress", depends_on=[]),
        )
        workflow_writes = []

        def handler(http_request):
            body = json.loads(http_request.content) if http_request.content else None
            if http_request.method == "GET":
                return httpx.Response(200, json={"data": {
                    "businessWorkflowId": workflow_id,
                    "status": "awaiting_clarification",
                    "delegatedTaskIds": ["task_transfer"],
                    "envelopeRevision": 1, "workflowRevision": 1,
                    "todoRevision": 1,
                    "todoSnapshot": [{
                        "id": item.id, "status": item.status,
                        "dependsOn": list(item.depends_on),
                    } for item in initial_items],
                    "todoDigest": TodoLedger.digest(initial_items),
                    "activeGoalTaskId": "task_transfer",
                    "activeTodoId": "resolve_from_account",
                    "executionPolicy": "sequential",
                    "suspensionReason": "awaiting_clarification",
                    "lastOutcome": {"status": "awaiting_clarification"},
                    "pendingCheckpointIds": [],
                }, "meta": {}})
            if http_request.url.path.endswith("agent-workflows"):
                workflow_writes.append(body)
                if body["expectedRevision"] != len(workflow_writes):
                    return httpx.Response(409, json={
                        "error": {"code": "agent_workflow_revision_conflict"}
                    })
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": workflow_id, "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            if http_request.url.path.endswith("agent-action-checkpoints"):
                return httpx.Response(201, json={"data": {
                    "checkpointId": "acp_native_followup",
                    "capabilityId": "bank.transfer.order.create",
                    "confirmationToken": "f" * 43,
                    "expiresAt": "2026-08-31T10:05:00.000Z",
                    "summary": {"title": "请确认转账", "description": "向张三转账100.00元", "riskLevel": "L2"},
                }, "meta": {}})
            outputs = {
                "bank.account.list": {"accounts": [{
                    "id": "acct_salary_6217", "name": "工资卡",
                    "maskedNo": "****6217", "status": "ACTIVE",
                }]},
                "bank.payee.list": {"payees": [{
                    "id": "payee_zhang_4321", "name": "张三",
                    "maskedNo": "****4321", "status": "ACTIVE",
                }]},
                "bank.payee.validate": {
                    "payeeId": "payee_zhang_4321", "valid": True,
                    "status": "ACTIVE", "checkedAt": "2026-08-31T10:00:00.000Z",
                },
            }
            return httpx.Response(200, json={"data": outputs[body["capabilityId"]], "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        runner = RestoredNativeRunRunner(workflow_id, resumed_items)
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope), business_runner=runner,
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "awaiting_confirmation")
        self.assertEqual(result.conversation_state.status, "retain")
        self.assertEqual(
            result.conversation_state.pending_envelope_id, "dlg_native_followup"
        )
        self.assertIsNotNone(runner.restored)
        self.assertEqual(runner.restore_events[:2], [
            "openjiuwen_session_restored",
            "node_todo_restored",
        ])
        self.assertEqual(runner.inputs[0]["workflow_control"], {
            "expected_workflow_revision": 1,
            "next_workflow_revision": 2,
            "expected_todo_revision": 1,
            "previous_active_goal_task_id": "task_transfer",
            "execution_policy": "sequential",
        })
        self.assertEqual([item["workflowRevision"] for item in workflow_writes], [2, 3])

    async def test_user_agent_receives_graph_bound_balance_intent_candidates(self):
        user = FakeRunner(balance_envelope_payload())
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=business,
        )
        payload = balance_request_payload(include_list=True)

        _events = [
            item
            async for item in service.stream(RuntimeAgentRunRequest.model_validate(payload))
        ]

        candidates = user.inputs[0]["intent_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["intent_id"], "ccb.account.balance.query")
        self.assertEqual(candidates[0]["title"], "账户查询·查询与管理")
        self.assertEqual(candidates[0]["source"], "tool_local_graph")
        self.assertEqual(candidates[0]["constraint"], "required")
        self.assertEqual(candidates[0]["execution_mode"], "read-execute")
        self.assertEqual(candidates[0]["capability_bindings"], [
            "bank.account.list", "bank.account.balance.get", "bank.account.detail.get"
        ])
        self.assertEqual(
            business.inputs[0]["slot_resolution_context"],
            {
                "source_message": "查一下工资卡余额",
                "page_context": {"tab": "home", "menu_key": "home.transfer"},
            },
        )
        intent_event = next(item for item in _events if item.stage == "user_agent.completed")
        self.assertEqual(
            intent_event.summary.metrics["intentSummary"],
            "账户查询·查询与管理",
        )

    async def test_non_authoritative_candidate_does_not_force_user_agent_shape(self):
        user = FakeRunner([non_task_envelope_payload(), balance_envelope_payload()])
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(balance_discovery_plan_payload()),
        )

        events = [
            item
            async for item in service.stream(
                RuntimeAgentRunRequest.model_validate(balance_request_payload())
            )
        ]

        self.assertEqual(len(user.inputs), 1)
        self.assertEqual(user.session_ids, ["user:conv_001"])
        self.assertNotIn("repair_feedback", user.inputs[0])
        self.assertEqual(events[-1].stage, "run.completed")
        self.assertEqual(events[-1].summary.metrics["status"], "unsupported")

    async def test_non_authoritative_candidate_never_triggers_shape_repair_loop(self):
        user = FakeRunner([non_task_envelope_payload(), non_task_envelope_payload()])
        business = FakeRunner(balance_discovery_plan_payload())
        service = DualDeepAgentService(user_runner=user, business_runner=business)

        events = [
            item
            async for item in service.stream(
                RuntimeAgentRunRequest.model_validate(balance_request_payload())
            )
        ]

        self.assertEqual(len(user.inputs), 1)
        self.assertEqual(business.inputs, [])
        self.assertEqual(events[-1].stage, "run.completed")
        self.assertEqual(events[-1].summary.metrics["status"], "unsupported")
        self.assertIn("user_agent.completed", [item.stage for item in events])

    async def test_punctuation_only_request_is_guided_without_running_business_agent(self):
        user = FakeRunner(AssertionError("punctuation-only input must not call the model"))
        business = FakeRunner(AssertionError("non-task input must not start business agent"))
        service = DualDeepAgentService(user_runner=user, business_runner=business)
        payload = balance_request_payload()
        payload["message"] = "？ ……！"

        result = await service.run(RuntimeAgentRunRequest.model_validate(payload))

        self.assertEqual(result.intent_task_envelope.intent_shape, "non_task")
        self.assertEqual(result.intent_task_envelope.tasks, [])
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.user_response.title, "请告诉我您想办理什么")
        self.assertNotIn(
            "请补充办理信息",
            result.user_response.title + result.user_response.body,
        )
        self.assertEqual(user.inputs, [])
        self.assertEqual(business.inputs, [])

    async def test_business_readonly_policy_repairs_once(self):
        business = FakeRunner(
            [clarification_plan_payload(), balance_discovery_plan_payload()]
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=business,
        )

        events = [
            item
            async for item in service.stream(
                RuntimeAgentRunRequest.model_validate(balance_request_payload())
            )
        ]

        self.assertEqual(len(business.inputs), 1)
        self.assertEqual(events[-1].stage, "run.failed")
        self.assertEqual(events[-1].error.code, "slot_resolver_invocation_failed")

    async def test_business_readonly_policy_fails_closed_after_one_repair(self):
        business = FakeRunner(
            [clarification_plan_payload(), clarification_plan_payload()]
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=business,
        )

        events = [
            item
            async for item in service.stream(
                RuntimeAgentRunRequest.model_validate(balance_request_payload())
            )
        ]

        self.assertEqual(len(business.inputs), 1)
        self.assertEqual(events[-1].stage, "run.failed")
        self.assertEqual(
            events[-1].error.code, "slot_resolver_invocation_failed"
        )
        self.assertNotIn("business_agent.completed", [item.stage for item in events])

    async def test_business_accepts_clarification_after_successful_discovery_prefix(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body["capabilityId"])
            return httpx.Response(200, json={"data": {"accounts": [
                {"id": "acct_salary_6217", "name": "工资卡", "maskedNo": "****6217", "status": "ACTIVE"},
                {"id": "acct_daily_1108", "name": "日常账户", "maskedNo": "****1108", "status": "ACTIVE"},
            ]}, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=business,
            capability_invoker=invoker,
        )

        result = await service.run(
            RuntimeAgentRunRequest.model_validate(
                balance_request_payload() | {"message": "查余额"}
            )
        )

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(len(business.inputs), 1)
        self.assertEqual(captured, ["bank.account.list"])
        await invoker.close()

    async def test_balance_rejects_precalled_balance_capability_before_http(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {}, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=ToolCallingRunner(
                balance_discovery_plan_payload(),
                invoker,
                [("bank.account.balance.get", {"accountId": "acct_attacker"})],
            ),
            capability_invoker=invoker,
        )
        try:
            pairs = [
                item async for item in service.stream_with_result(
                    RuntimeAgentRunRequest.model_validate(balance_request_payload())
                )
            ]
        finally:
            await invoker.close()

        self.assertEqual(calls, [])
        self.assertEqual(pairs[-1][0].stage, "run.failed")
        self.assertEqual(pairs[-1][1].status, "blocked")
        self.assertEqual(pairs[-1][1].user_response.type, "notice")
        self.assertNotIn("acct_attacker", pairs[-1][1].model_dump_json(by_alias=True))

    async def test_balance_requires_both_capabilities_to_be_executable_before_business(self):
        for missing_capability in (
            "bank.account.list",
            "bank.account.balance.get",
        ):
            with self.subTest(missing_capability=missing_capability):
                calls = []

                def handler(request: httpx.Request) -> httpx.Response:
                    calls.append(json.loads(request.content))
                    return httpx.Response(200, json={"data": {}, "meta": {}})

                invoker = AgentCapabilityInvoker(
                    endpoint="http://node.test/internal",
                    service_token="runtime-service-secret",
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                )
                business = FakeRunner(clarification_plan_payload())
                service = DualDeepAgentService(
                    user_runner=FakeRunner(balance_envelope_payload()),
                    business_runner=business,
                    capability_invoker=invoker,
                )
                payload = balance_request_payload()
                payload["executableCapabilities"] = [
                    item for item in payload["executableCapabilities"]
                    if item["id"] != missing_capability
                ]
                try:
                    pairs = [
                        item async for item in service.stream_with_result(
                            RuntimeAgentRunRequest.model_validate(payload)
                        )
                    ]
                finally:
                    await invoker.close()

                self.assertEqual(business.inputs, [])
                self.assertEqual(calls, [])
                self.assertEqual(pairs[-1][0].stage, "run.failed")
                self.assertEqual(pairs[-1][1].status, "blocked")
                self.assertEqual(pairs[-1][1].user_response.type, "notice")

    async def test_account_list_gateway_failure_is_a_controlled_safe_delivery(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content))
            return httpx.Response(503, json={"error": "private-upstream-detail"})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=FakeRunner(clarification_plan_payload()),
            capability_invoker=invoker,
        )
        try:
            pairs = [
                item async for item in service.stream_with_result(
                    RuntimeAgentRunRequest.model_validate(balance_request_payload())
                )
            ]
            result = await service.run(
                RuntimeAgentRunRequest.model_validate(balance_request_payload())
            )
        finally:
            await invoker.close()

        self.assertEqual([item["capabilityId"] for item in calls], [
            "bank.account.list", "bank.account.list",
        ])
        self.assertEqual(pairs[-1][0].stage, "run.failed")
        self.assertEqual(pairs[-1][1].status, "blocked")
        self.assertEqual(result.status, "blocked")
        self.assertNotIn(
            "private-upstream-detail", pairs[-1][1].model_dump_json(by_alias=True)
        )

    async def test_balance_gateway_failures_are_controlled_safe_deliveries(self):
        def response_for(failure_kind, request, body):
            if body["capabilityId"] == "bank.account.list":
                return httpx.Response(
                    200,
                    json={"data": {"accounts": [TWO_ACTIVE_ACCOUNTS[0]]}, "meta": {}},
                )
            if failure_kind == "503":
                return httpx.Response(503, json={"error": "private-balance-detail"})
            if failure_kind == "timeout":
                raise httpx.ReadTimeout("private-balance-timeout", request=request)
            return httpx.Response(200, content=b"not-json")

        for failure_kind in ("503", "timeout", "invalid_json"):
            with self.subTest(failure_kind=failure_kind):
                calls = []

                def handler(request: httpx.Request) -> httpx.Response:
                    body = json.loads(request.content)
                    calls.append(body)
                    return response_for(failure_kind, request, body)

                invoker = AgentCapabilityInvoker(
                    endpoint="http://node.test/internal",
                    service_token="runtime-service-secret",
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                )
                service = DualDeepAgentService(
                    user_runner=FakeRunner(balance_envelope_payload()),
                    business_runner=FakeRunner(clarification_plan_payload()),
                    capability_invoker=invoker,
                )
                try:
                    pairs = [
                        item async for item in service.stream_with_result(
                            RuntimeAgentRunRequest.model_validate(
                                balance_request_payload() | {"message": "查余额"}
                            )
                        )
                    ]
                finally:
                    await invoker.close()

                self.assertEqual(
                    [item["capabilityId"] for item in calls],
                    ["bank.account.list", "bank.account.balance.get"],
                )
                self.assertEqual(pairs[-1][0].stage, "run.failed")
                self.assertEqual(pairs[-1][1].status, "blocked")
                serialized = pairs[-1][1].model_dump_json(by_alias=True)
                self.assertNotIn("private-balance", serialized)
                self.assertNotIn("not-json", serialized)

    async def test_balance_rejects_nonempty_account_list_input_after_one_http_call(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"accounts": []}, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=ToolCallingRunner(
                clarification_plan_payload(),
                invoker,
                [("bank.account.list", {"customerId": "unsafe"})],
            ),
            capability_invoker=invoker,
        )
        try:
            result = await service.run(
                RuntimeAgentRunRequest.model_validate(balance_request_payload())
            )
        finally:
            await invoker.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.user_response.type, "notice")

    async def test_generic_balance_with_two_accounts_returns_specific_safe_choices(self):
        result, calls = await self._run_balance_response(
            message="查余额", accounts=TWO_ACTIVE_ACCOUNTS
        )

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.type, "clarification")
        self.assertEqual(result.user_response.body, "请选择当前任务使用的查询账户。")
        self.assertEqual(len(result.user_response.actions), 2)
        serialized = result.user_response.model_dump_json(by_alias=True)
        self.assertIn("尾号 6217", serialized)
        self.assertIn("尾号 8032", serialized)
        self.assertNotIn("acct_salary_6217", serialized)
        self.assertNotIn("acct_savings_8032", serialized)
        self.assertEqual(
            [item["capabilityId"] for item in calls], ["bank.account.list"]
        )

    async def test_multi_intent_starts_with_active_balance_account_choice(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            if body["capabilityId"] != "bank.account.list":
                raise AssertionError(body["capabilityId"])
            return httpx.Response(200, json={
                "data": {"accounts": TWO_ACTIVE_ACCOUNTS}, "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        transfer = envelope_payload()["tasks"][0]
        transfer.update({
            "task_id": "task_transfer",
            "slots": {
                "payeeName": {"value": "张三", "source": "user_explicit", "confidence": 1},
                "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
            },
        })
        mixed = envelope_payload("multi_intent", [
            balance_envelope_payload()["tasks"][0],
            transfer,
        ])
        ids = [
            "bank.account.list", "bank.account.balance.get", "bank.account.detail.get", "bank.transfer.status.get", "bank.payee.list",
            "bank.payee.validate", "bank.transfer.order.create",
        ]
        capabilities = [balance_capability_payload(item) for item in ids]
        capabilities[-1]["sideEffect"] = "state-change"
        request = request_payload()
        request.update({
            "message": "先查余额，再给张三转100元",
            "executableCapabilities": capabilities[:-1],
            "plannableCapabilities": capabilities,
        })
        service = DualDeepAgentService(
            user_runner=FakeRunner(mixed),
            business_runner=ToolCallingRunner(
                clarification_plan_payload(), invoker,
                [("bank.account.list", {})],
            ),
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.type, "clarification")
        self.assertEqual(result.user_response.title, "请选择查询账户")
        self.assertEqual(len(result.user_response.actions), 2)
        self.assertEqual(
            [item["capabilityId"] for item in calls], ["bank.account.list"]
        )

    async def test_native_multi_intent_emits_balance_result_before_transfer_card(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.url.path.endswith("agent-workflows"):
                return httpx.Response(201, json={"data": {
                    "businessWorkflowId": body["businessWorkflowId"],
                    "status": body["status"],
                    "workflowRevision": body["workflowRevision"],
                    "todoRevision": body["todoRevision"],
                    "activeGoalTaskId": body["activeGoalTaskId"],
                }, "meta": {}})
            calls.append(body)
            outputs = {
                "bank.account.list": {"accounts": TWO_ACTIVE_ACCOUNTS},
                "bank.account.balance.get": {
                    "accountId": "acct_salary_6217",
                    "available": {"formatted": "¥8,888.00"},
                    "ledger": {"formatted": "¥9,000.00"},
                },
                "bank.payee.list": {"payees": [{
                    "id": "payee_zhang_4321", "name": "张三",
                    "maskedNo": "****4321", "status": "ACTIVE",
                }]},
                "bank.payee.validate": {
                    "payeeId": "payee_zhang_4321", "valid": True,
                    "status": "ACTIVE", "checkedAt": "2026-09-01T10:00:00Z",
                },
            }
            return httpx.Response(200, json={
                "data": outputs[body["capabilityId"]], "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)

        class AdvancingMultiIntentRunner(FakeRunner):
            def __init__(self):
                super().__init__(None)
                self.iteration = 0

            async def run(self, payload, *, session_id):
                self.iteration += 1
                self.inputs.append(payload)
                self.session_ids.append(session_id)
                if self.iteration == 1:
                    await invoker.invoke_read_capability("bank.account.list", {})
                    status = "continue"
                    revision = 1
                else:
                    await invoker.invoke_read_capability("bank.payee.list", {})
                    status = "awaiting_clarification"
                    revision = 2
                return {
                    "schema_version": "2.0",
                    "business_workflow_id": payload["business_workflow_id"],
                    "workflow_revision": 1,
                    "status": status,
                    "covered_task_ids": ["task_balance", "task_transfer"],
                    "active_goal_task_id": "task_transfer",
                    "active_todo_id": "task_transfer:collect",
                    "todo_revision": revision,
                    "next_action": "collect_transfer_account",
                }

            def load_todo_ledger(self, session_id, revision=1):
                items = (
                    TodoSnapshotItem(id="task_balance", status="completed", depends_on=[]),
                    TodoSnapshotItem(id="task_transfer", status="in_progress", depends_on=[]),
                    TodoSnapshotItem(id="task_transfer:collect", status="in_progress", depends_on=[]),
                )
                return TodoLedgerSnapshot(
                    revision=revision,
                    digest=TodoLedger.digest(items)[:-1] + str(revision),
                    items=items,
                )

        balance = balance_envelope_payload()["tasks"][0]
        balance["slots"] = {
            "accountReference": {
                "value": "尾号6217", "source": "user_explicit", "confidence": 1,
            },
        }
        transfer = envelope_payload()["tasks"][0]
        transfer.update({
            "task_id": "task_transfer",
            "slots": {
                "payeeName": {"value": "张三", "source": "user_explicit", "confidence": 1},
                "amount": {"value": "100", "source": "user_explicit", "confidence": 1},
            },
        })
        mixed = envelope_payload("multi_intent", [balance, transfer])
        ids = [
            "bank.account.list", "bank.account.balance.get", "bank.account.detail.get", "bank.transfer.status.get", "bank.payee.list",
            "bank.payee.validate", "bank.transfer.order.create",
        ]
        capabilities = [balance_capability_payload(item) for item in ids]
        capabilities[-1]["sideEffect"] = "state-change"
        request = request_payload()
        request.update({
            "message": "先查询尾号6217余额，再给张三转100元",
            "executableCapabilities": capabilities[:-1],
            "plannableCapabilities": capabilities,
        })
        service = DualDeepAgentService(
            user_runner=FakeRunner(mixed),
            business_runner=AdvancingMultiIntentRunner(),
            capability_invoker=invoker,
        )

        result = await service.run(RuntimeAgentRunRequest.model_validate(request))

        self.assertEqual(result.status, "clarification_required")
        self.assertIsNone(result.user_response)
        self.assertEqual(
            [item.type for item in result.user_responses],
            ["result", "clarification"],
        )
        self.assertEqual(result.user_responses[0].title, "账户余额")
        self.assertIn(
            "转出账户",
            result.user_responses[1].title + result.user_responses[1].body,
        )
        self.assertNotIn(
            "acct_salary_6217",
            result.model_dump_json(by_alias=True),
        )

    async def test_explicit_or_single_balance_reuses_discovery_and_executes_only_balance(self):
        cases = (
            ("查询工资卡余额", TWO_ACTIVE_ACCOUNTS, "acct_salary_6217"),
            ("查询尾号8032的余额", TWO_ACTIVE_ACCOUNTS, "acct_savings_8032"),
            ("查余额", [TWO_ACTIVE_ACCOUNTS[0]], "acct_salary_6217"),
        )
        for message, accounts, expected_account_id in cases:
            with self.subTest(message=message):
                result, calls = await self._run_balance_response(
                    message=message, accounts=accounts
                )

                self.assertEqual(result.status, "succeeded")
                self.assertEqual(result.user_response.type, "result")
                self.assertEqual(
                    [item["capabilityId"] for item in calls],
                    ["bank.account.list", "bank.account.balance.get"],
                )
                self.assertEqual(
                    calls[1]["input"], {"accountId": expected_account_id}
                )

    async def test_unknown_explicit_balance_reference_never_selects_an_account(self):
        result, calls = await self._run_balance_response(
            message="查询尾号9999的余额", accounts=TWO_ACTIVE_ACCOUNTS
        )

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.user_response.type, "clarification")
        self.assertEqual(
            [item["capabilityId"] for item in calls], ["bank.account.list"]
        )

    async def test_missing_balance_amounts_return_blocked_notice_not_planned_null(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            output = (
                {"accounts": [TWO_ACTIVE_ACCOUNTS[0]]}
                if body["capabilityId"] == "bank.account.list"
                else {"accountId": body["input"]["accountId"], "available": {}}
            )
            return httpx.Response(200, json={"data": output, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=FakeRunner(clarification_plan_payload()),
            capability_invoker=invoker,
        )
        try:
            result = await service.run(
                RuntimeAgentRunRequest.model_validate(
                    balance_request_payload() | {"message": "查余额"}
                )
            )
        finally:
            await invoker.close()

        self.assertEqual(
            [item["capabilityId"] for item in calls],
            ["bank.account.list", "bank.account.balance.get"],
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.user_response.type, "notice")
        self.assertIsNone(result.user_result)

    async def test_empty_trusted_account_list_returns_blocked_notice(self):
        result, calls = await self._run_balance_response(
            message="查余额", accounts=[]
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.user_response.type, "notice")
        self.assertIn("暂无可用账户", result.user_response.title)
        self.assertEqual(
            [item["capabilityId"] for item in calls], ["bank.account.list"]
        )

    async def test_duplicate_balance_tails_degrade_to_registered_navigation(self):
        duplicate_tail_accounts = [
            TWO_ACTIVE_ACCOUNTS[0],
            {
                "id": "acct_savings_6217",
                "name": "储蓄卡",
                "maskedNo": "****6217",
                "status": "ACTIVE",
            },
        ]
        result, calls = await self._run_balance_response(
            message="查余额", accounts=duplicate_tail_accounts
        )

        self.assertEqual(result.status, "navigation_only")
        self.assertEqual(result.user_response.type, "navigation")
        self.assertEqual(result.user_response.actions[0].kind, "navigate")
        self.assertEqual(
            [item["capabilityId"] for item in calls], ["bank.account.list"]
        )

    async def test_dirty_account_output_fails_closed_instead_of_becoming_empty(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "accounts": [{
                            "id": "acct_salary_6217",
                            "name": "工资卡",
                            "maskedNo": "****6217",
                        }]
                    },
                    "meta": {},
                },
            )

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        graph, faq_repository = self._faq_dependencies()
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=ToolCallingRunner(
                clarification_plan_payload(), invoker, [("bank.account.list", {})]
            ),
            intent_graph=graph,
            capability_invoker=invoker,
            faq_repository=faq_repository,
        )
        payload = balance_request_payload()
        payload["message"] = "查余额"
        try:
            events = [
                item async for item in service.stream(
                    RuntimeAgentRunRequest.model_validate(payload)
                )
            ]
        finally:
            await invoker.close()

        self.assertEqual(events[-1].stage, "run.failed")
        self.assertEqual(events[-1].error.code, "slot_resolver_data_invalid")
        self.assertEqual(
            [item["capabilityId"] for item in calls], ["bank.account.list"]
        )

    async def test_missing_graph_bound_capability_fails_closed_before_business_agent(self):
        business = FakeRunner(clarification_plan_payload())
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=business,
        )

        result = await service.run(
            RuntimeAgentRunRequest.model_validate(
                balance_request_payload(include_list=False)
            )
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.user_response.type, "notice")
        self.assertEqual(len(business.inputs), 0)

    async def test_user_repair_rejects_sensitive_slots_without_trace_leakage(self):
        unsafe = balance_envelope_payload()
        unsafe["tasks"][0]["slots"]["confirmation_token"] = {
            "value": "secret-confirmation-value",
            "source": "context",
            "confidence": 1,
        }
        user = FakeRunner(unsafe)
        service = DualDeepAgentService(
            user_runner=user,
            business_runner=FakeRunner(balance_discovery_plan_payload()),
        )

        events = [
            item
            async for item in service.stream(
                RuntimeAgentRunRequest.model_validate(balance_request_payload())
            )
        ]
        serialized = json.dumps(
            [item.model_dump(by_alias=True, mode="json") for item in events],
            ensure_ascii=False,
        )

        self.assertEqual(events[-1].error.code, "user_agent_contract_violation")
        self.assertNotIn("secret-confirmation-value", serialized)
        self.assertNotIn("confirmation_token", serialized)
        self.assertNotIn("user-repair:", serialized)
        self.assertNotIn("repair_feedback", serialized)

    async def test_business_repair_rejects_side_effect_capability(self):
        unsafe = balance_discovery_plan_payload()
        unsafe["steps"].append(
            {
                "step_id": "step_execute",
                "task_id": "task_balance",
                "capability_id": "bank.transfer.execute",
                "input_bindings": {},
                "requires_user_confirmation": False,
                "depends_on": ["step_balance"],
            }
        )
        payload = balance_request_payload()
        payload["plannableCapabilities"].append(
            balance_capability_payload(
                "bank.transfer.execute", side_effect="funds-movement"
            )
        )
        business = FakeRunner([clarification_plan_payload(), unsafe])
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=business,
        )

        events = [
            item
            async for item in service.stream(RuntimeAgentRunRequest.model_validate(payload))
        ]
        serialized = json.dumps(
            [item.model_dump(by_alias=True, mode="json") for item in events],
            ensure_ascii=False,
        )

        self.assertEqual(events[-1].error.code, "slot_resolver_invocation_failed")
        self.assertNotIn("bank.transfer.execute", serialized)
        self.assertNotIn("input_bindings", serialized)
        self.assertNotIn("business-repair:", serialized)
        self.assertNotIn("repair_feedback", serialized)

    async def test_stream_emits_authoritative_safe_stage_sequence(self):
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=FakeRunner(plan_payload()),
        )
        request = RuntimeAgentRunRequest.model_validate(request_payload())

        events = [item async for item in service.stream(request)]

        self.assertEqual(
            [item.stage for item in events],
            [
                "request.accepted",
                "input_rail.started",
                "user_agent.started",
                "intent_graph.retrieved",
                "input_rail.completed",
                "security_gate.decided",
                "user_agent.committed",
                "user_agent.completed",
                "delegation.created",
                "business_agent.started",
                "business_agent.completed",
                "plan.validated",
                "run.completed",
            ],
        )
        self.assertEqual([item.sequence for item in events], list(range(1, 14)))
        self.assertTrue(all(item.agent_run_id == "abr_001" for item in events))
        self.assertTrue(all(item.trace_id == "trace_001" for item in events))
        self.assertTrue(all(item.duration_ms is None or item.duration_ms >= 0 for item in events))
        serialized = json.dumps(
            events[-1].model_dump(by_alias=True, mode="json"), ensure_ascii=False
        )
        self.assertNotIn('"value"', serialized)
        self.assertNotIn("input_bindings", serialized)

    async def test_stream_stops_at_user_contract_failure(self):
        service = DualDeepAgentService(
            user_runner=FakeRunner(None),
            business_runner=FakeRunner(plan_payload()),
        )
        events = [
            item
            async for item in service.stream(
                RuntimeAgentRunRequest.model_validate(request_payload())
            )
        ]

        self.assertEqual(
            [item.stage for item in events],
            [
                "request.accepted",
                "input_rail.started",
                "user_agent.started",
                "intent_graph.retrieved",
                "input_rail.completed",
                "run.failed",
            ],
        )
        self.assertEqual(events[-1].error.code, "user_agent_contract_violation")
        self.assertNotIn("intent submission", events[-1].error.message)

    async def test_controlled_failure_delivers_safe_notice_after_failed_trace(self):
        service = DualDeepAgentService(
            user_runner=FakeRunner(None),
            business_runner=FakeRunner(plan_payload()),
        )
        request = RuntimeAgentRunRequest.model_validate(request_payload())

        pairs = [item async for item in service.stream_with_result(request)]
        result = await service.run(request)

        self.assertEqual(pairs[-1][0].stage, "run.failed")
        self.assertEqual(pairs[-1][1].status, "blocked")
        self.assertEqual(pairs[-1][1].user_response.type, "notice")
        self.assertEqual(result.status, "blocked")
        serialized = pairs[-1][1].model_dump_json(by_alias=True)
        self.assertNotIn("user_agent_contract_violation", serialized)
        self.assertNotIn("intent submission", serialized)

    async def test_stream_logs_only_safe_contract_diagnostics(self):
        invalid = envelope_payload()
        invalid["tasks"] = []
        service = DualDeepAgentService(
            user_runner=FakeRunner(invalid),
            business_runner=FakeRunner(plan_payload()),
        )

        with self.assertLogs("ai_native_bank.agent_runtime.service", level="WARNING") as captured:
            events = [
                item
                async for item in service.stream(
                    RuntimeAgentRunRequest.model_validate(request_payload())
                )
            ]

        self.assertEqual(events[-1].error.code, "user_agent_contract_violation")
        diagnostic = " ".join(captured.output)
        self.assertIn("single_intent requires exactly one task", diagnostic)
        self.assertNotIn("工资卡", diagnostic)
        self.assertNotIn("acct_salary", diagnostic)

    async def test_stream_distinguishes_business_and_plan_failures(self):
        request = RuntimeAgentRunRequest.model_validate(request_payload())
        business_failure = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=FakeRunner(None),
        )
        plan_failure = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=FakeRunner(plan_payload("bank.transfer.execute")),
        )

        business_events = [item async for item in business_failure.stream(request)]
        plan_events = [item async for item in plan_failure.stream(request)]

        self.assertEqual(business_events[-1].error.code, "business_agent_contract_violation")
        self.assertEqual(plan_events[-1].error.code, "capability_plan_invalid")
        self.assertEqual(plan_events[-2].stage, "business_agent.completed")
        self.assertNotIn("plan.validated", [item.stage for item in plan_events])

    async def test_run_enforces_authoritative_ids_and_minimal_a2a_context(self):
        user = FakeRunner(envelope_payload())
        business = FakeRunner(plan_payload())
        service = DualDeepAgentService(user_runner=user, business_runner=business)
        request = RuntimeAgentRunRequest.model_validate(request_payload())

        result = await service.run(request)

        self.assertEqual(result.agent_run_id, "abr_001")
        self.assertEqual(result.intent_task_envelope.trace_id, "trace_001")
        self.assertEqual(result.intent_task_envelope.conversation_id, "conv_001")
        self.assertEqual(result.intent_task_envelope.source_agent_run_id, "abr_001")
        self.assertEqual(result.capability_plan.delegation_id, result.intent_task_envelope.delegation_id)
        self.assertNotIn("message", business.inputs[0])
        self.assertNotIn("customerId", business.inputs[0])
        self.assertEqual(set(business.inputs[0]), {
            "business_workflow_id", "intent_task_envelope", "capabilities", "slot_resolution_context",
            "slot_assessment", "runtime_capability_allowlist", "runtime_capability_phase",
        })
        self.assertEqual(
            business.inputs[0]["slot_assessment"]["missing"],
            ["payee", "fromAccount", "amount"],
        )
        self.assertEqual(
            set(business.inputs[0]["slot_resolution_context"]),
            {"source_message", "page_context"},
        )
        self.assertEqual(user.session_ids[0], "user:conv_001")
        self.assertRegex(
            business.session_ids[0],
            r"^business:cust_demo_001:bwf_[a-f0-9]{24}$",
        )

    async def test_run_rejects_capability_outside_server_projection(self):
        service = DualDeepAgentService(
            user_runner=FakeRunner(envelope_payload()),
            business_runner=FakeRunner(plan_payload("bank.transfer.execute")),
        )
        result = await service.run(RuntimeAgentRunRequest.model_validate(request_payload()))
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.user_response.type, "notice")

    async def test_business_run_activates_graph_bound_execution_context_and_always_clears_it(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            outputs = {
                "bank.account.list": {"accounts": [{
                    "id": "acct_salary_6217", "name": "Salary account",
                    "maskedNo": "****6217", "status": "ACTIVE",
                }]},
                "bank.account.balance.get": {
                    "accountId": "acct_salary_6217",
                    "availableBalance": {"formatted": "CNY 1,000.00"},
                },
            }
            return httpx.Response(200, json={
                "data": outputs[body["capabilityId"]], "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        business = ContextCheckingRunner(balance_discovery_plan_payload(), invoker)
        service = DualDeepAgentService(
            user_runner=FakeRunner(balance_envelope_payload()),
            business_runner=business,
            capability_invoker=invoker,
        )

        await service.run(RuntimeAgentRunRequest.model_validate(balance_request_payload()))

        self.assertEqual(business.contexts[0].agent_run_id, "abr_001")
        self.assertEqual(
            business.contexts[0].execution_allowlist,
            frozenset({"bank.account.list"}),
        )
        self.assertIsNone(invoker.current_context())
        await invoker.close()

    async def test_run_rejects_missing_structured_submission(self):
        service = DualDeepAgentService(user_runner=FakeRunner(None), business_runner=FakeRunner(plan_payload()))
        result = await service.run(RuntimeAgentRunRequest.model_validate(request_payload()))
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.user_response.type, "notice")


if __name__ == "__main__":
    unittest.main()
