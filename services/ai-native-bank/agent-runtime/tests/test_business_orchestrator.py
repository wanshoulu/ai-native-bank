import unittest

from app.business_loop import TodoSnapshotItem
from app.business_orchestrator import (
    BusinessLoopCoordinator,
    BusinessLoopInvariantError,
    BusinessLoopStalled,
    plan_obligation_snapshot,
    plan_guided_goal,
)
from app.contracts import GuidedEntryGoal, IntentTaskEnvelope
from app.todo_ledger import TodoLedgerSnapshot
from tests.test_contracts import envelope_payload


def obligation_entry_goal(*, obligation_id=None):
    payload = {
        "goalId": "goal_nba_obligation_001", "scenarioId": "obligation-repayment",
        "scenarioVersion": "1.0.0", "recommendationId": "nba_obligation_001",
        "targetIntentId": "ccb.obligation.repay-all", "mode": "guided",
        "contextRefs": ["card_bill_202609", "loan_due_202609", "utility_due_202609"],
        "workflowTemplateId": "obligation-repayment-v1",
    }
    if obligation_id is not None:
        payload["trustedInputs"] = {"obligationId": obligation_id}
    return GuidedEntryGoal.model_validate(payload)


def selected_obligation_entry_goal(obligation_id="utility_due_202609", obligation_type="utility"):
    return GuidedEntryGoal.model_validate({
        "goalId": "goal_nba_selected_obligation_001",
        "scenarioId": "obligation-selected-payment",
        "scenarioVersion": "1.0.0",
        "recommendationId": "nba_obligation_001",
        "targetIntentId": "ccb.obligation.pay-selected",
        "mode": "guided",
        "contextRefs": [obligation_id],
        "workflowTemplateId": "obligation-selected-payment-v1",
        "trustedInputs": {
            "obligationId": obligation_id,
            "obligationType": obligation_type,
        },
    })


def credit_card_repayment_entry_goal():
    return GuidedEntryGoal.model_validate({
        "goalId": "goal_nba_card_repayment_001",
        "scenarioId": "credit-card-repayment",
        "scenarioVersion": "1.0.0",
        "recommendationId": "nba_obligation_001",
        "targetIntentId": "ccb.home.repayment.repay",
        "mode": "guided",
        "contextRefs": ["credit_global_8891", "acct_salary_6217"],
        "workflowTemplateId": "credit-card-repayment-v1",
        "trustedInputs": {
            "cardId": "credit_global_8891",
            "fromAccountId": "acct_salary_6217",
        },
    })


class GuidedOrchestrationTests(unittest.TestCase):
    def test_conversational_obligation_discovery_expands_to_confirmed_actions(self):
        result = plan_obligation_snapshot({
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
        })

        self.assertEqual(result.target_intent_id, "ccb.obligation.repay-all")
        self.assertEqual(
            [item.capability_id for item in result.todos],
            [
                "bank.credit-card.repayment.create",
                "bank.utility.payment.create",
            ],
        )
        self.assertTrue(all(item.requires_confirmation for item in result.todos))

    def test_credit_card_repayment_starts_with_authoritative_bill_and_account_reads(self):
        result = plan_guided_goal(credit_card_repayment_entry_goal())
        self.assertEqual(
            [(item.item_id, item.capability_id, item.input_bindings)
             for item in result.todos],
            [
                ("load-card-bill", "bank.credit-card.bill.get", {"cardId": "credit_global_8891"}),
                ("load-payment-account", "bank.account.list", {}),
                ("load-payment-balance", "bank.account.balance.get", {"accountId": "acct_salary_6217"}),
            ],
        )
        self.assertTrue(all(not item.requires_confirmation for item in result.todos))

    def test_obligation_goal_starts_with_registered_discovery_capability(self):
        result = plan_guided_goal(obligation_entry_goal())
        self.assertEqual(
            [(item.item_id, item.capability_id, item.requires_confirmation)
             for item in result.todos],
            [("load-obligations", "bank.obligation.list", False)],
        )

    def test_obligation_goal_creates_one_confirmed_todo_per_available_item(self):
        result = plan_guided_goal(obligation_entry_goal(), {
            "accountId": "acct_salary_6217",
            "items": [
                {"id": "card_bill_202609", "type": "credit_card", "amount": {"amountMinor": 386000}, "executionStatus": "available"},
                {"id": "loan_due_202609", "type": "loan", "amount": {"amountMinor": 250000}, "executionStatus": "available"},
                {"id": "utility_due_202609", "type": "utility", "amount": {"amountMinor": 50000}, "executionStatus": "available"},
            ],
        })
        self.assertEqual([item.capability_id for item in result.todos], [
            "bank.credit-card.repayment.create", "bank.loan.repayment.create",
            "bank.utility.payment.create",
        ])
        self.assertTrue(all(item.requires_confirmation for item in result.todos))
        self.assertEqual(result.todos[0].input_bindings, {
            "cardId": "card_bill_202609",
            "fromAccountId": "acct_salary_6217",
            "amountMinor": 386000,
        })
        self.assertEqual(result.todos[1].input_bindings, {
            "repaymentId": "loan_due_202609",
            "fromAccountId": "acct_salary_6217",
            "amountMinor": 250000,
        })
        self.assertEqual(result.todos[2].input_bindings, {
            "billId": "utility_due_202609",
            "accountId": "acct_salary_6217",
        })

    def test_obligation_goal_rejects_snapshot_without_trusted_account(self):
        with self.assertRaises(BusinessLoopInvariantError):
            plan_guided_goal(obligation_entry_goal(), {"items": []})

    def test_obligation_goal_limits_plan_to_selected_trusted_item(self):
        result = plan_guided_goal(selected_obligation_entry_goal(), {
            "accountId": "acct_salary_6217",
            "items": [
                {"id": "card_bill_202609", "type": "credit_card", "amount": {"amountMinor": 386000}, "executionStatus": "available"},
                {"id": "utility_due_202609", "type": "utility", "amount": {"amountMinor": 50000}, "executionStatus": "available"},
            ],
        })

        self.assertEqual(len(result.todos), 1)
        self.assertEqual(result.todos[0].item_id, "utility_due_202609")
        self.assertEqual(
            result.todos[0].capability_id, "bank.utility.payment.create"
        )
        self.assertEqual(result.todos[0].input_bindings, {
            "billId": "utility_due_202609",
            "accountId": "acct_salary_6217",
        })
        self.assertEqual(result.target_intent_id, "ccb.obligation.pay-selected")

    def test_selected_obligation_goal_rejects_type_mismatch(self):
        with self.assertRaisesRegex(
            BusinessLoopInvariantError,
            "selected guided obligation is unavailable",
        ):
            plan_guided_goal(selected_obligation_entry_goal(
                obligation_type="loan"
            ), {
                "accountId": "acct_salary_6217",
                "items": [
                    {"id": "utility_due_202609", "type": "utility", "amount": {"amountMinor": 50000}, "executionStatus": "available"},
                ],
            })

    def test_obligation_goal_rejects_unknown_selected_trusted_item(self):
        with self.assertRaisesRegex(
            BusinessLoopInvariantError,
            "selected guided obligation is unavailable",
        ):
            plan_guided_goal(selected_obligation_entry_goal(
                obligation_id="unknown_obligation"
            ), {
                "accountId": "acct_salary_6217",
                "items": [
                    {"id": "utility_due_202609", "type": "utility", "amount": {"amountMinor": 50000}, "executionStatus": "available"},
                ],
            })


def loop_outcome(*, active_goal="task_001", status="awaiting_clarification"):
    return {
        "schema_version": "2.0",
        "business_workflow_id": "bwf_1234567890abcdef12345678",
        "workflow_revision": 1,
        "status": status,
        "covered_task_ids": ["task_001"],
        "active_goal_task_id": active_goal,
        "active_todo_id": f"{active_goal}:collect" if active_goal else None,
        "todo_revision": 1,
        "next_action": "collect_account",
    }


class FakeBusinessRunner:
    def __init__(self, submissions, snapshots):
        self.submissions = list(submissions)
        self.snapshots = list(snapshots)
        self.session_ids = []

    async def run(self, payload, *, session_id):
        self.session_ids.append(session_id)
        return self.submissions.pop(0)

    def load_todo_ledger(self, session_id, revision=1):
        snapshot = self.snapshots.pop(0)
        return TodoLedgerSnapshot(revision=revision, digest=snapshot[0], items=snapshot[1])

    def restore_todo_ledger(self, session_id, revision, items):
        snapshot = self.snapshots.pop(0)
        restored = tuple(
            TodoSnapshotItem(
                id=item["id"],
                status=item["status"],
                depends_on=tuple(item.get("dependsOn", [])),
            )
            if isinstance(item, dict)
            else item
            for item in items
        )
        return TodoLedgerSnapshot(revision=revision, digest=snapshot[0], items=restored)

    def seed_todo_ledger(self, session_id, items):
        self.seeded_items = [
            (item["id"], item["status"]) for item in items
        ]
        seeded = tuple(
            TodoSnapshotItem(
                id=item["id"],
                status=item["status"],
                depends_on=tuple(item.get("dependsOn") or []),
            )
            for item in items
        )
        return TodoLedgerSnapshot(revision=1, digest="c" * 64, items=seeded)


class BusinessLoopCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _query_envelope():
        # 单任务 QUERY 意图（默认模板是 DO 转账；DO 任务在单次 loop 内
        # 不可能合法完成，会被无凭据完成守卫回滚，不适合验证提交规整）。
        payload = envelope_payload()
        payload["tasks"] = [{**payload["tasks"][0], "action": "QUERY"}]
        return IntentTaskEnvelope.model_validate(payload)

    @staticmethod
    def _read_invocation():
        return {
            "capability_id": "bank.account.list",
            "input": {},
            "output": {"accounts": []},
        }

    async def test_completed_submission_with_stale_active_pointers_is_normalized_at_boundary(self):
        envelope = self._query_envelope()
        outcome = loop_outcome(status="completed")
        runner = FakeBusinessRunner([], [(
            "a" * 64,
            (TodoSnapshotItem(id="task_001", status="completed", depends_on=[]),),
        )])
        invocation = self._read_invocation()

        async def run_iteration(payload, *, session_id):
            return outcome, [invocation]

        result = await BusinessLoopCoordinator(runner, max_iterations=2).advance(
            envelope=envelope,
            workflow_id="bwf_1234567890abcdef12345678",
            session_id="business:conv:bwf_1234567890abcdef12345678",
            initial_payload={},
            run_iteration=run_iteration,
            capabilities_by_task={"task_001": frozenset({"bank.account.list"})},
        )

        self.assertEqual(result.outcome.status, "completed")
        self.assertIsNone(result.outcome.active_goal_task_id)
        self.assertIsNone(result.outcome.active_todo_id)
        self.assertIsNone(result.outcome.next_action)

    async def test_completed_ledger_clears_stale_model_active_goal_projection(self):
        envelope = self._query_envelope()
        outcome = loop_outcome(status="awaiting_clarification")
        runner = FakeBusinessRunner([], [(
            "a" * 64,
            (TodoSnapshotItem(id="load-obligations", status="completed", depends_on=[]),),
        )])
        invocation = self._read_invocation()

        async def run_iteration(payload, *, session_id):
            return outcome, [invocation]

        result = await BusinessLoopCoordinator(runner, max_iterations=2).advance(
            envelope=envelope,
            workflow_id="bwf_1234567890abcdef12345678",
            session_id="business:conv:bwf_1234567890abcdef12345678",
            initial_payload={},
            run_iteration=run_iteration,
            capabilities_by_task={"task_001": frozenset({"bank.account.list"})},
        )

        self.assertIsNone(result.outcome.active_goal_task_id)
        self.assertIsNone(result.outcome.active_todo_id)
        self.assertEqual(result.outcome.status, "completed")

    def _two_goal_envelope(self):
        payload = envelope_payload()
        payload["tasks"] = [
            {**payload["tasks"][0], "task_id": "task_balance_query"},
            {**payload["tasks"][0], "task_id": "task_transfer_create"},
        ]
        payload["intent_shape"] = "multi_intent"
        return IntentTaskEnvelope.model_validate(payload)

    async def test_rejects_capability_invocation_owned_by_a_future_goal(self):
        envelope = self._two_goal_envelope()
        outcome = loop_outcome(active_goal="task_balance_query")
        outcome["covered_task_ids"] = ["task_balance_query", "task_transfer_create"]
        outcome["active_todo_id"] = "task_balance_query:collect"
        runner = FakeBusinessRunner([], [(
            "a" * 64,
            (
                TodoSnapshotItem(id="task_balance_query", status="in_progress", depends_on=[]),
                TodoSnapshotItem(id="task_balance_query:collect", status="in_progress", depends_on=[]),
                TodoSnapshotItem(id="task_transfer_create", status="pending", depends_on=[]),
            ),
        )])

        async def run_iteration(payload, *, session_id):
            return outcome, [{
                "capability_id": "bank.payee.list",
                "input": {},
                "output": {"items": []},
            }]

        with self.assertRaisesRegex(
            BusinessLoopInvariantError,
            "active goal capability conflict",
        ):
            await BusinessLoopCoordinator(runner, max_iterations=2).advance(
                envelope=envelope,
                workflow_id="bwf_1234567890abcdef12345678",
                session_id="business:conv:bwf_1234567890abcdef12345678",
                initial_payload={},
                run_iteration=run_iteration,
                capabilities_by_task={
                    "task_balance_query": frozenset({"bank.account.list", "bank.account.balance.get"}),
                    "task_transfer_create": frozenset({"bank.account.list", "bank.payee.list"}),
                },
            )

    async def test_records_successful_invocation_against_active_goal(self):
        envelope = self._two_goal_envelope()
        outcome = loop_outcome(active_goal="task_balance_query")
        outcome["covered_task_ids"] = ["task_balance_query", "task_transfer_create"]
        outcome["active_todo_id"] = "task_balance_query:collect"
        runner = FakeBusinessRunner([], [(
            "a" * 64,
            (
                TodoSnapshotItem(id="task_balance_query", status="in_progress", depends_on=[]),
                TodoSnapshotItem(id="task_balance_query:collect", status="in_progress", depends_on=[]),
                TodoSnapshotItem(id="task_transfer_create", status="pending", depends_on=[]),
            ),
        )])

        async def run_iteration(payload, *, session_id):
            return outcome, [{
                "capability_id": "bank.account.list",
                "input": {},
                "output": {"items": []},
            }]

        result = await BusinessLoopCoordinator(runner, max_iterations=2).advance(
            envelope=envelope,
            workflow_id="bwf_1234567890abcdef12345678",
            session_id="business:conv:bwf_1234567890abcdef12345678",
            initial_payload={},
            run_iteration=run_iteration,
            capabilities_by_task={
                "task_balance_query": frozenset({"bank.account.list", "bank.account.balance.get"}),
                "task_transfer_create": frozenset({"bank.account.list", "bank.payee.list"}),
            },
        )

        self.assertEqual(result.task_invocations[0].task_id, "task_balance_query")
        self.assertEqual(result.task_invocations[0].capability_id, "bank.account.list")

    async def test_repeated_identical_read_is_recorded_once_for_native_projection(self):
        envelope = self._query_envelope()
        outcome = loop_outcome(active_goal=None, status="completed")
        outcome["active_todo_id"] = None
        runner = FakeBusinessRunner([], [(
            "a" * 64,
            (TodoSnapshotItem(id="task_001", status="completed", depends_on=[]),),
        )])
        invocation = {
            "capability_id": "bank.account.list",
            "input": {},
            "output": {"accounts": []},
        }

        async def run_iteration(payload, *, session_id):
            return outcome, [invocation, dict(invocation)]

        result = await BusinessLoopCoordinator(runner, max_iterations=2).advance(
            envelope=envelope,
            workflow_id="bwf_1234567890abcdef12345678",
            session_id="business:conv:bwf_1234567890abcdef12345678",
            initial_payload={},
            run_iteration=run_iteration,
            capabilities_by_task={"task_001": frozenset({"bank.account.list"})},
        )

        self.assertEqual(len(result.successful_invocations), 1)
        self.assertEqual(len(result.task_invocations), 1)

    async def test_multi_goal_todo_coverage_is_repaired_in_same_session(self):
        payload = envelope_payload()
        payload["tasks"] = [
            {**payload["tasks"][0], "task_id": "task_first"},
            {**payload["tasks"][0], "task_id": "task_second"},
        ]
        payload["intent_shape"] = "same_intent_multi_task"
        envelope = IntentTaskEnvelope.model_validate(payload)
        invalid = loop_outcome(active_goal="task_first")
        invalid["covered_task_ids"] = ["task_first", "task_second"]
        invalid["active_todo_id"] = "merged_transfer"
        repaired = loop_outcome(active_goal="task_first")
        repaired["covered_task_ids"] = ["task_first", "task_second"]
        repaired["active_todo_id"] = "task_first:collect"
        repaired["todo_revision"] = 2
        runner = FakeBusinessRunner(
            [invalid, repaired],
            [
                (
                    "a" * 64,
                    (
                        TodoSnapshotItem(
                            id="merged_transfer", status="in_progress", depends_on=[]
                        ),
                    ),
                ),
                (
                    "b" * 64,
                    (
                        TodoSnapshotItem(
                            id="task_first", status="in_progress", depends_on=[]
                        ),
                        TodoSnapshotItem(
                            id="task_first:collect", status="in_progress", depends_on=[]
                        ),
                        TodoSnapshotItem(
                            id="task_second", status="pending", depends_on=[]
                        ),
                    ),
                ),
            ],
        )
        session_id = "business:conv:bwf_1234567890abcdef12345678"

        result = await BusinessLoopCoordinator(runner, max_iterations=3).advance(
            envelope=envelope,
            workflow_id="bwf_1234567890abcdef12345678",
            session_id=session_id,
            initial_payload={"intent_task_envelope": payload},
            run_iteration=runner.run,
        )

        self.assertEqual(result.outcome.active_goal_task_id, "task_first")
        self.assertEqual(runner.session_ids, [session_id, session_id])

    async def test_coverage_conflict_falls_back_to_authoritative_ledger(self):
        # 模型修复轮后仍提交自造 id 的台账视图：按 envelope 确定性重播
        # 权威台账继续推进（澄清等下游语义正常产出），而不是让整轮 blocked。
        payload = envelope_payload()
        payload["tasks"] = [
            {**payload["tasks"][0], "task_id": "task_first"},
            {**payload["tasks"][0], "task_id": "task_second"},
        ]
        payload["intent_shape"] = "same_intent_multi_task"
        envelope = IntentTaskEnvelope.model_validate(payload)
        junk_first = loop_outcome(active_goal="task_first")
        junk_first["covered_task_ids"] = ["task_first", "task_second"]
        junk_first["active_todo_id"] = "merged_transfer"
        junk_first["todo_revision"] = 2
        junk_again = loop_outcome(active_goal="task_first")
        junk_again["covered_task_ids"] = ["task_first", "task_second"]
        junk_again["active_todo_id"] = "merged_transfer"
        junk_again["todo_revision"] = 3
        junk_view = (
            "a" * 64,
            (
                TodoSnapshotItem(
                    id="merged_transfer", status="in_progress", depends_on=[]
                ),
            ),
        )
        runner = FakeBusinessRunner(
            [junk_first, junk_again], [junk_view, junk_view]
        )
        session_id = "business:conv:bwf_1234567890abcdef12345678"

        result = await BusinessLoopCoordinator(runner, max_iterations=2).advance(
            envelope=envelope,
            workflow_id="bwf_1234567890abcdef12345678",
            session_id=session_id,
            initial_payload={"intent_task_envelope": payload},
            run_iteration=runner.run,
        )

        self.assertEqual(result.outcome.status, "awaiting_clarification")
        # todo_revision 以权威台账为准机械覆盖
        self.assertEqual(result.outcome.todo_revision, 1)
        self.assertEqual(result.snapshot.revision, 1)
        self.assertEqual(
            set(runner.seeded_items),
            {("task_first", "in_progress"), ("task_second", "pending")},
        )
        self.assertEqual(runner.session_ids, [session_id, session_id])

    async def test_stalled_continue_gets_one_bounded_repair_before_failure(self):
        envelope = IntentTaskEnvelope.model_validate(envelope_payload())
        stalled = loop_outcome(status="continue")
        stalled["active_todo_id"] = "task_001"
        repeated = loop_outcome(status="continue")
        repeated["active_todo_id"] = "task_001"
        recovered = loop_outcome(status="awaiting_clarification")
        recovered["active_todo_id"] = "task_001"
        recovered["todo_revision"] = 2
        ledger = (
            "a" * 64,
            (TodoSnapshotItem(id="task_001", status="in_progress", depends_on=[]),),
        )
        advanced = (
            "b" * 64,
            (TodoSnapshotItem(id="task_001", status="in_progress", depends_on=[]),),
        )
        runner = FakeBusinessRunner(
            [stalled, repeated, recovered], [ledger, ledger, advanced]
        )
        payloads = []

        async def run_iteration(payload, *, session_id):
            payloads.append(dict(payload))
            return await runner.run(payload, session_id=session_id)

        session_id = "business:conv:bwf_1234567890abcdef12345678"
        result = await BusinessLoopCoordinator(runner, max_iterations=4).advance(
            envelope=envelope,
            workflow_id="bwf_1234567890abcdef12345678",
            session_id=session_id,
            initial_payload={"intent_task_envelope": {}},
            run_iteration=run_iteration,
        )

        self.assertEqual(result.outcome.status, "awaiting_clarification")
        self.assertEqual(len(payloads), 3)
        repair = payloads[2].get("repair_feedback")
        self.assertIsNotNone(repair)
        self.assertEqual(repair["codes"], ["loop_turn_without_progress"])
        self.assertEqual(repair["max_attempts"], 1)
        self.assertEqual(
            payloads[2]["workflow_control"]["expected_todo_revision"], 1
        )
        self.assertNotIn("repair_feedback", payloads[1])

    async def test_stalled_continue_fails_after_repair_budget_is_spent(self):
        envelope = IntentTaskEnvelope.model_validate(envelope_payload())
        stalled = loop_outcome(status="continue")
        stalled["active_todo_id"] = "task_001"
        ledger = (
            "a" * 64,
            (TodoSnapshotItem(id="task_001", status="in_progress", depends_on=[]),),
        )
        runner = FakeBusinessRunner(
            [stalled, stalled, stalled], [ledger, ledger, ledger]
        )

        with self.assertRaises(BusinessLoopStalled):
            await BusinessLoopCoordinator(runner, max_iterations=3).advance(
                envelope=envelope,
                workflow_id="bwf_1234567890abcdef12345678",
                session_id="business:conv:bwf_1234567890abcdef12345678",
                initial_payload={},
                run_iteration=runner.run,
            )
        self.assertEqual(len(runner.session_ids), 3)

    async def test_single_goal_accepts_native_step_ids_without_goal_prefix(self):
        envelope = IntentTaskEnvelope.model_validate(envelope_payload())
        outcome = loop_outcome()
        outcome["active_todo_id"] = "resolve_payee"
        runner = FakeBusinessRunner([outcome], [(
            "a" * 64,
            (
                TodoSnapshotItem(
                    id="resolve_from_account", status="completed", depends_on=[]
                ),
                TodoSnapshotItem(
                    id="resolve_payee", status="in_progress", depends_on=[]
                ),
                TodoSnapshotItem(
                    id="create_order", status="pending", depends_on=[]
                ),
            ),
        )])

        result = await BusinessLoopCoordinator(runner, max_iterations=2).advance(
            envelope=envelope,
            workflow_id="bwf_1234567890abcdef12345678",
            session_id="business:conv:bwf_1234567890abcdef12345678",
            initial_payload={},
            run_iteration=runner.run,
        )

        self.assertEqual(result.outcome.status, "awaiting_clarification")
        self.assertEqual(result.outcome.active_goal_task_id, "task_001")
        self.assertEqual(result.outcome.active_todo_id, "resolve_payee")

    async def test_native_todo_ledger_normalizes_model_declared_goal_coverage(self):
        payload = envelope_payload()
        payload["tasks"] = [
            {**payload["tasks"][0], "task_id": "task_first"},
            {**payload["tasks"][0], "task_id": "task_future"},
        ]
        payload["intent_shape"] = "same_intent_multi_task"
        envelope = IntentTaskEnvelope.model_validate(payload)
        outcome = loop_outcome(active_goal="task_first")
        outcome["covered_task_ids"] = ["task_first"]
        outcome["active_todo_id"] = "task_first"
        runner = FakeBusinessRunner([outcome], [(
            "a" * 64,
            (
                TodoSnapshotItem(id="task_first", status="in_progress", depends_on=[]),
                TodoSnapshotItem(id="task_future", status="pending", depends_on=[]),
            ),
        )])

        result = await BusinessLoopCoordinator(runner, max_iterations=2).advance(
            envelope=envelope,
            workflow_id="bwf_1234567890abcdef12345678",
            session_id="business:conv:bwf_1234567890abcdef12345678",
            initial_payload={},
            run_iteration=runner.run,
        )

        self.assertEqual(result.outcome.covered_task_ids, ["task_first", "task_future"])

    async def test_sticky_first_goal_corrects_future_goal_projection(self):
        payload = envelope_payload()
        payload["tasks"] = [
            {**payload["tasks"][0], "task_id": "task_first"},
            {**payload["tasks"][0], "task_id": "task_future"},
        ]
        payload["intent_shape"] = "same_intent_multi_task"
        envelope = IntentTaskEnvelope.model_validate(payload)
        outcome = loop_outcome(active_goal="task_future")
        outcome["covered_task_ids"] = ["task_first", "task_future"]
        runner = FakeBusinessRunner([outcome], [(
            "a" * 64,
            (
                TodoSnapshotItem(id="task_first", status="pending", depends_on=[]),
                TodoSnapshotItem(id="task_future", status="in_progress", depends_on=[]),
                TodoSnapshotItem(id="task_future:collect", status="in_progress", depends_on=[]),
            ),
        )])

        result = await BusinessLoopCoordinator(runner, max_iterations=2).advance(
            envelope=envelope,
            workflow_id="bwf_1234567890abcdef12345678",
            session_id="business:conv:bwf_1234567890abcdef12345678",
            initial_payload={},
            run_iteration=runner.run,
        )

        # 台账顺序策略下活动 goal 权威推导为 task_first：模型把活动指针
        # 投到未来 goal 属投影漂移，由平台直接纠正而非判 workflow 失败。
        self.assertEqual(result.outcome.active_goal_task_id, "task_first")
        self.assertEqual(result.outcome.active_todo_id, "task_first")

    async def test_continue_replans_in_same_business_session_until_suspension(self):
        envelope = IntentTaskEnvelope.model_validate(envelope_payload())
        continued = loop_outcome(status="continue")
        continued["next_action"] = "resolve_payee"
        suspended = loop_outcome(status="awaiting_clarification")
        suspended["todo_revision"] = 2
        runner = FakeBusinessRunner(
            [continued, suspended],
            [
                ("a" * 64, (
                    TodoSnapshotItem(id="task_001", status="in_progress", depends_on=[]),
                    TodoSnapshotItem(id="task_001:collect", status="in_progress", depends_on=[]),
                )),
                ("b" * 64, (
                    TodoSnapshotItem(id="task_001", status="in_progress", depends_on=[]),
                    TodoSnapshotItem(id="task_001:collect", status="in_progress", depends_on=[]),
                )),
            ],
        )
        session_id = "business:conv:bwf_1234567890abcdef12345678"

        result = await BusinessLoopCoordinator(runner, max_iterations=3).advance(
            envelope=envelope,
            workflow_id="bwf_1234567890abcdef12345678",
            session_id=session_id,
            initial_payload={},
            run_iteration=runner.run,
        )

        self.assertEqual(result.outcome.status, "awaiting_clarification")
        self.assertEqual(runner.session_ids, [session_id, session_id])


if __name__ == "__main__":
    unittest.main()
