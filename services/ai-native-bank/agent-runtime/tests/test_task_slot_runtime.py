import inspect
import unittest
from types import MappingProxyType

from app.contracts import CapabilityPlan, CapabilityProjection, IntentTask
from app.intent_graph import (
    ExecutionBinding,
    IntentCandidate,
    IntentSlotContract,
    SlotDefinition,
    TaskPolicy,
)
from app.slot_contract import SlotAssessment, SlotEvaluation
from app.task_slot_runtime import (
    TaskSlotDecision,
    TaskSlotRuntimeError,
    active_task_capability_ids,
    build_graph_action_plan,
    build_graph_read_plan,
    decide_active_task,
)


def _task(intent_id="demo.query.alpha"):
    return IntentTask.model_validate({
        "task_id": "task_alpha",
        "intent_id": intent_id,
        "action": "QUERY",
        "business_object": "demo_subject",
        "slots": {},
        "dependencies": [],
        "execution_readiness": "needs_slots",
        "risk_level": "L1",
    })


def _candidate(
    *,
    intent_id="demo.query.alpha",
    slot_type="account_reference",
    resolver="bank.account.list",
    execution="bank.demo.read",
    shapes=("single_intent",),
):
    definition = SlotDefinition(
        slot_id="slot.demo.subject",
        name="subject",
        label="Subject",
        type=slot_type,
        required=True,
        extraction_keys=("subject",),
        resolved_field="subjectId",
        resolver_capability_id=resolver,
    )
    return IntentCandidate(
        intent_id=intent_id,
        title="Demo query",
        action="QUERY",
        business_object="demo_subject",
        execution_mode="read-execute",
        risk_level="L1",
        required_slots=["subject"],
        capability_bindings=[item for item in (resolver, execution) if item],
        score=100,
        evidence=["test"],
        rule_version="test",
        slot_contract=IntentSlotContract(
            version="2.0",
            slots=(definition,),
            clarification_order=("subject",),
            execution_binding=ExecutionBinding(execution, False),
        ),
        task_policy=TaskPolicy(shapes, "sequential", 8),
        response_policy="query_result",
    )


def _evaluation(
    readiness="needs_slots",
    unresolved=("subject",),
    resolved_values=None,
):
    state = "missing" if unresolved else "resolved"
    return SlotEvaluation(
        assessments=(SlotAssessment("subject", "Subject", state),),
        semantic_values=MappingProxyType({}),
        resolved_values=MappingProxyType(resolved_values or {}),
        missing=tuple(unresolved),
        ambiguous=(),
        invalid=(),
        recognized=(),
        optional_absent=(),
        readiness=readiness,
    )


class TaskSlotRuntimeTests(unittest.TestCase):
    def test_decides_resolver_from_graph_contract_for_arbitrary_intent(self):
        decision = decide_active_task(
            task=_task(),
            candidate=_candidate(),
            evaluation=_evaluation(),
            intent_shape="multi_intent",
        )

        self.assertEqual(decision.task_id, "task_alpha")
        self.assertEqual(decision.slot_name, "subject")
        self.assertEqual(decision.slot_type, "account_reference")
        self.assertEqual(decision.resolver_capability_id, "bank.account.list")
        self.assertEqual(decision.resolved_field, "subjectId")
        self.assertEqual(decision.execution_capability_id, "bank.demo.read")
        self.assertEqual(decision.response_policy, "query_result")
        self.assertEqual(decision.readiness, "resolve")

    def test_ready_task_uses_graph_execution_binding(self):
        decision = decide_active_task(
            task=_task(),
            candidate=_candidate(),
            evaluation=_evaluation(
                "ready", (), {"subjectId": "subject_internal_1"}
            ),
            intent_shape="multi_intent",
        )

        self.assertEqual(decision.readiness, "execute")
        self.assertEqual(decision.execution_capability_id, "bank.demo.read")
        self.assertIsNone(decision.slot_name)

    def test_distinct_intent_task_uses_its_single_task_contract_inside_multi_intent(self):
        """Envelope composition must not rewrite each delegated task's own shape."""

        decision = decide_active_task(
            task=_task(),
            candidate=_candidate(shapes=("single_intent",)),
            evaluation=_evaluation(),
            intent_shape="multi_intent",
        )

        self.assertEqual(decision.task_id, "task_alpha")
        self.assertEqual(decision.readiness, "resolve")

    def test_query_capability_boundary_exposes_only_the_current_phase(self):
        resolving = decide_active_task(
            task=_task(), candidate=_candidate(), evaluation=_evaluation(),
            intent_shape="multi_intent",
        )
        executing = decide_active_task(
            task=_task(), candidate=_candidate(),
            evaluation=_evaluation("ready", (), {"subjectId": "subject_internal_1"}),
            intent_shape="multi_intent",
        )

        self.assertEqual(
            active_task_capability_ids(_task(), _candidate(), resolving),
            frozenset({"bank.account.list"}),
        )
        self.assertEqual(
            active_task_capability_ids(_task(), _candidate(), executing),
            frozenset({"bank.demo.read"}),
        )

    def test_extracted_resolver_backed_reference_still_requires_resolution(self):
        evaluation = SlotEvaluation(
            assessments=(SlotAssessment(
                "subject", "Subject", "extracted", semantic_value="尾号6217"
            ),),
            semantic_values=MappingProxyType({"subject": "尾号6217"}),
            resolved_values=MappingProxyType({}),
            missing=(), ambiguous=(), invalid=(), recognized=("subject",),
            optional_absent=(), readiness="ready",
        )

        decision = decide_active_task(
            task=_task(), candidate=_candidate(), evaluation=evaluation,
            intent_shape="multi_intent",
        )

        self.assertEqual(decision.readiness, "resolve")
        self.assertEqual(decision.slot_name, "subject")

    def test_builds_read_plan_from_decision_without_business_constants(self):
        decision = decide_active_task(
            task=_task(),
            candidate=_candidate(),
            evaluation=_evaluation(),
            intent_shape="multi_intent",
        )
        source = CapabilityPlan.model_validate({
            "schema_version": "1.0",
            "business_agent_run_id": "bar_demo",
            "delegation_id": "dlg_demo",
            "status": "clarification_required",
            "steps": [],
        })

        result = build_graph_read_plan(source, _task(), decision, "subject_internal_1")

        self.assertEqual(result.status, "planned")
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].task_id, "task_alpha")
        self.assertEqual(result.steps[0].capability_id, "bank.demo.read")
        self.assertEqual(result.steps[0].input_bindings, {"subjectId": "subject_internal_1"})
        self.assertFalse(result.steps[0].requires_user_confirmation)

    def test_builds_confirmed_action_from_graph_slots_and_capability_schema(self):
        task = _task().model_copy(update={"action": "DO"})
        decision = TaskSlotDecision(
            task_id="task_alpha", slot_name="subject",
            slot_type="account_reference", slot_label="Subject",
            resolver_capability_id="bank.account.list",
            resolved_field="accountId",
            execution_capability_id="bank.demo.create",
            response_policy="confirmation_then_receipt", readiness="resolve",
        )
        evaluation = SlotEvaluation(
            assessments=(SlotAssessment(
                "subject", "Subject", "extracted", semantic_value="primary account"
            ),),
            semantic_values=MappingProxyType({"subject": "primary account"}),
            resolved_values=MappingProxyType({"amountMinor": 10000}),
            missing=(), ambiguous=(), invalid=(), recognized=("subject",),
            optional_absent=(), readiness="ready",
        )
        capability = CapabilityProjection.model_validate({
            "id": "bank.demo.create", "title": "Create demo",
            "description": "Create a governed demo action",
            "exposure": "user-action", "sideEffect": "state-change",
            "riskLevel": "L2",
            "inputSchema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "accountId": {"type": "string"},
                    "amountMinor": {"type": "integer"},
                },
                "required": ["accountId", "amountMinor"],
            },
            "outputSchema": {"type": "object"}, "possibleErrors": [],
            "readiness": {"status": "ready", "environment": "mock", "blockers": []},
        })
        source = CapabilityPlan.model_validate({
            "schema_version": "1.0", "business_agent_run_id": "bar_demo",
            "delegation_id": "dlg_demo", "status": "clarification_required", "steps": [],
        })

        result = build_graph_action_plan(
            source, task, decision, evaluation, capability,
            {"accountId": "account_internal_1"},
        )

        self.assertEqual(result.status, "planned")
        self.assertEqual(result.steps[0].capability_id, "bank.demo.create")
        self.assertEqual(result.steps[0].input_bindings, {
            "accountId": "account_internal_1", "amountMinor": 10000,
        })
        self.assertTrue(result.steps[0].requires_user_confirmation)

    def test_action_plan_stays_suspended_until_required_inputs_are_resolved(self):
        task = _task().model_copy(update={"action": "DO"})
        decision = TaskSlotDecision(
            task_id="task_alpha", slot_name="subject", slot_type="account_reference",
            slot_label="Subject", resolver_capability_id="bank.account.list",
            resolved_field="accountId", execution_capability_id="bank.demo.create",
            response_policy="confirmation_then_receipt", readiness="resolve",
        )
        capability = CapabilityProjection.model_validate({
            "id": "bank.demo.create", "title": "Create demo",
            "description": "Create a governed demo action", "exposure": "user-action",
            "sideEffect": "state-change", "riskLevel": "L2",
            "inputSchema": {
                "type": "object", "properties": {"accountId": {"type": "string"}},
                "required": ["accountId"],
            },
            "outputSchema": {"type": "object"}, "possibleErrors": [],
            "readiness": {"status": "ready", "environment": "mock", "blockers": []},
        })
        source = CapabilityPlan.model_validate({
            "schema_version": "1.0", "business_agent_run_id": "bar_demo",
            "delegation_id": "dlg_demo", "status": "clarification_required", "steps": [],
        })

        self.assertIsNone(build_graph_action_plan(
            source, task, decision, _evaluation(), capability, {}
        ))

    def test_resolver_output_satisfies_its_missing_semantic_reference_slot(self):
        task = _task().model_copy(update={"action": "DO"})
        decision = TaskSlotDecision(
            task_id="task_alpha", slot_name="subject", slot_type="account_reference",
            slot_label="Subject", resolver_capability_id="bank.account.list",
            resolved_field="accountId", execution_capability_id="bank.demo.create",
            response_policy="confirmation_then_receipt", readiness="resolve",
        )
        evaluation = SlotEvaluation(
            assessments=(
                SlotAssessment("subject", "Subject", "missing"),
                SlotAssessment(
                    "amount", "Amount", "resolved",
                    semantic_value="100", resolved_value=10000,
                ),
            ),
            semantic_values=MappingProxyType({"amount": "100"}),
            resolved_values=MappingProxyType({"amountMinor": 10000}),
            missing=("subject",), ambiguous=(), invalid=(), recognized=("amount",),
            optional_absent=(), readiness="needs_slots",
        )
        capability = CapabilityProjection.model_validate({
            "id": "bank.demo.create", "title": "Create demo",
            "description": "Create a governed demo action", "exposure": "user-action",
            "sideEffect": "state-change", "riskLevel": "L2",
            "inputSchema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "accountId": {"type": "string"},
                    "amountMinor": {"type": "integer"},
                },
                "required": ["accountId", "amountMinor"],
            },
            "outputSchema": {"type": "object"}, "possibleErrors": [],
            "readiness": {"status": "ready", "environment": "mock", "blockers": []},
        })
        source = CapabilityPlan.model_validate({
            "schema_version": "1.0", "business_agent_run_id": "bar_demo",
            "delegation_id": "dlg_demo", "status": "clarification_required", "steps": [],
        })

        result = build_graph_action_plan(
            source, task, decision, evaluation, capability,
            {"accountId": "account_internal_1"},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "planned")
        self.assertEqual(result.steps[0].input_bindings, {
            "accountId": "account_internal_1", "amountMinor": 10000,
        })

    def test_incomplete_or_unapproved_graph_contract_fails_closed(self):
        cases = (
            (None, _task(), _evaluation(), "multi_intent"),
            (_candidate(intent_id="demo.query.beta"), _task(), _evaluation(), "multi_intent"),
            (_candidate(shapes=("single_intent",)), _task(), _evaluation(), "same_intent_multi_task"),
            (_candidate(), _task(), _evaluation(unresolved=("outside_contract",)), "multi_intent"),
        )
        for candidate, task, evaluation, shape in cases:
            with self.subTest(candidate=candidate, shape=shape):
                with self.assertRaises(TaskSlotRuntimeError):
                    decide_active_task(
                        task=task,
                        candidate=candidate,
                        evaluation=evaluation,
                        intent_shape=shape,
                    )

    def test_unresolved_slot_without_resolver_requires_direct_clarification(self):
        decision = decide_active_task(
            task=_task(),
            candidate=_candidate(resolver=None),
            evaluation=_evaluation(),
            intent_shape="multi_intent",
        )

        self.assertEqual(decision.readiness, "clarify")
        self.assertIsNone(decision.resolver_capability_id)

    def test_generic_runtime_source_has_no_business_specific_constants(self):
        import app.task_slot_runtime as module

        source = inspect.getsource(module)
        for forbidden in (
            "ccb.account.balance.query",
            "ccb.transfer.create",
            "account_balance",
            "transfer_order",
            "credit_card",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
