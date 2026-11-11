import copy
import unittest

from pydantic import ValidationError

from app.contracts import CapabilityPlan, CapabilityProjection, IntentTaskEnvelope
from app.rails import (
    IntentConstraint,
    readonly_discovery_applies,
    validate_intent_constraints,
    validate_readonly_discovery,
)


def balance_constraint_payload(**changes):
    payload = {
        "intent_id": "ccb.account.balance.query",
        "title": "账户查询·查询与管理",
        "action": "QUERY",
        "business_object": "account_balance",
        "required_shape": "single_intent",
        "expected_task_count": 1,
        "unresolved_slot_readiness": "needs_slots",
        "execution_mode": "read-execute",
        "required_slots": ["accountId"],
        "capability_bindings": ["bank.account.list", "bank.account.balance.get"],
        "score": 200,
        "evidence": ["intent_keyword"],
        "source": "tool_local_graph",
        "constraint": "required",
        "rule_version": "balance.v1",
    }
    payload.update(changes)
    return payload


def balance_envelope_payload(**task_changes):
    task = {
        "task_id": "task_balance",
        "intent_id": "ccb.account.balance.query",
        "action": "QUERY",
        "business_object": "account_balance",
        "slots": {},
        "dependencies": [],
        "execution_readiness": "needs_slots",
        "risk_level": "L0",
    }
    task.update(task_changes)
    return {
        "schema_version": "1.0",
        "delegation_id": "dlg_balance",
        "trace_id": "trace_balance",
        "conversation_id": "conv_balance",
        "source_agent_run_id": "abr_balance",
        "intent_shape": "single_intent",
        "tasks": [task],
        "predicted_next_intents": [],
    }


def capability(capability_id, *, side_effect="none"):
    return CapabilityProjection.model_validate(
        {
            "id": capability_id,
            "title": capability_id,
            "description": "测试能力",
            "exposure": "model-callable",
            "sideEffect": side_effect,
            "riskLevel": "L1",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "possibleErrors": [],
            "readiness": {"status": "ready", "environment": "test", "blockers": []},
        }
    )


def balance_discovery_plan_payload():
    return {
        "schema_version": "1.0",
        "business_agent_run_id": "bar_balance",
        "delegation_id": "dlg_balance",
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


class RailTests(unittest.TestCase):
    def setUp(self):
        self.constraint = IntentConstraint.model_validate(balance_constraint_payload())
        self.envelope = IntentTaskEnvelope.model_validate(balance_envelope_payload())
        self.capabilities = [
            capability("bank.account.list"),
            capability("bank.account.balance.get"),
        ]

    def test_intent_constraint_is_strict_and_contains_no_business_values(self):
        with self.assertRaises(ValidationError):
            IntentConstraint.model_validate(
                balance_constraint_payload(account_id="acct_private")
            )
        with self.assertRaises(ValidationError):
            IntentConstraint.model_validate(balance_constraint_payload(source="model"))
        with self.assertRaises(ValidationError):
            IntentConstraint.model_validate(balance_constraint_payload(rule_version="bad value"))

    def test_unique_required_candidate_matches_balance_task_without_mutation(self):
        before = self.envelope.model_dump()

        violations = validate_intent_constraints(self.envelope, [self.constraint])

        self.assertEqual(violations, ())
        self.assertEqual(self.envelope.model_dump(), before)

    def test_zero_or_multiple_candidates_do_not_apply_a_hard_constraint(self):
        wrong = IntentTaskEnvelope.model_validate(
            balance_envelope_payload(intent_id="ccb.account.transactions.query")
        )
        other = IntentConstraint.model_validate(
            balance_constraint_payload(intent_id="ccb.account.transactions.query")
        )

        self.assertEqual(validate_intent_constraints(wrong, []), ("candidate_required",))
        self.assertEqual(validate_intent_constraints(wrong, [self.constraint, other]), ())

    def test_candidate_free_ambiguity_is_not_a_banking_intent(self):
        ambiguous = IntentTaskEnvelope.model_validate({
            "schema_version": "1.0",
            "delegation_id": "dlg_ambiguous",
            "trace_id": "trace_ambiguous",
            "conversation_id": "conv_ambiguous",
            "source_agent_run_id": "abr_ambiguous",
            "intent_shape": "ambiguous_intent",
            "tasks": [],
            "predicted_next_intents": [],
        })
        non_task = ambiguous.model_copy(update={"intent_shape": "non_task"})

        self.assertEqual(
            validate_intent_constraints(ambiguous, []),
            ("candidate_free_ambiguity_invalid",),
        )
        self.assertEqual(validate_intent_constraints(non_task, []), ())

    def test_unique_candidate_returns_stable_field_violation_codes(self):
        cases = {
            "intent_id_mismatch": {"intent_id": "ccb.account.transactions.query"},
            "action_mismatch": {"action": "ASK"},
            "business_object_mismatch": {"business_object": "account_transactions"},
            "execution_readiness_mismatch": {
                "execution_readiness": "ready"
            },
        }
        for expected, changes in cases.items():
            with self.subTest(expected=expected):
                envelope = IntentTaskEnvelope.model_validate(
                    balance_envelope_payload(**changes)
                )
                self.assertEqual(
                    validate_intent_constraints(envelope, [self.constraint]),
                    (expected,),
                )

    def test_confirm_required_missing_entity_accepts_both_unresolved_readiness_states(self):
        constraint = IntentConstraint.model_validate(balance_constraint_payload(
            intent_id="ccb.credit.card.loss-report",
            action="CHANGE",
            business_object="credit_card",
            execution_mode="confirm-required",
            required_slots=["cardId", "reason"],
            capability_bindings=[
                "bank.credit-card.list", "bank.credit-card.loss-report.create",
            ],
        ))
        envelope = IntentTaskEnvelope.model_validate(balance_envelope_payload(
            intent_id="ccb.credit.card.loss-report",
            action="CHANGE",
            business_object="credit_card",
            slots={
                "cardId": {"value": "全球支付卡", "source": "user_explicit", "confidence": 1},
                "reason": {"value": "CARD_MISSING", "source": "user_explicit", "confidence": 1},
            },
            execution_readiness="needs_clarification",
        ))

        self.assertEqual(validate_intent_constraints(envelope, [constraint]), ())

    def test_non_authoritative_unique_candidate_does_not_invent_shape_or_cardinality(self):
        envelope = IntentTaskEnvelope.model_validate(
            {
                **balance_envelope_payload(),
                "intent_shape": "non_task",
                "tasks": [],
            }
        )

        self.assertEqual(validate_intent_constraints(envelope, [self.constraint]), ())

    def test_same_intent_multi_task_constraint_requires_two_tasks(self):
        constraint = IntentConstraint.model_validate(balance_constraint_payload(
            intent_id="ccb.home.transfer.faq",
            action="HELP",
            business_object="transfer",
            required_shape="same_intent_multi_task",
            expected_task_count=2,
            shape_authoritative=True,
            supported_shapes=["single_intent", "same_intent_multi_task"],
            unresolved_slot_readiness=None,
            execution_mode="knowledge-only",
            required_slots=[],
            capability_bindings=[],
        ))
        single = IntentTaskEnvelope.model_validate(balance_envelope_payload(
            intent_id="ccb.home.transfer.faq",
            action="HELP",
            business_object="transfer",
            execution_readiness="ready",
        ))

        self.assertEqual(
            validate_intent_constraints(single, [constraint]),
            ("intent_shape_mismatch", "task_cardinality_mismatch"),
        )

    def test_same_intent_multi_task_constraint_enforces_exact_goal_count(self):
        constraint = IntentConstraint.model_validate(balance_constraint_payload(
            intent_id="ccb.transfer.create",
            action="DO",
            business_object="transfer_order",
            required_shape="same_intent_multi_task",
            expected_task_count=2,
            shape_authoritative=True,
            supported_shapes=["single_intent", "same_intent_multi_task"],
            unresolved_slot_readiness="needs_slots",
            execution_mode="confirm-required",
            required_slots=["payee", "amount", "account"],
            capability_bindings=["bank.transfer.order.create"],
        ))
        first = balance_envelope_payload()["tasks"][0]
        first.update({
            "task_id": "task_zhang", "intent_id": "ccb.transfer.create",
            "action": "DO", "business_object": "transfer_order",
        })
        second = copy.deepcopy(first)
        second["task_id"] = "task_li"
        valid = IntentTaskEnvelope.model_validate({
            **balance_envelope_payload(),
            "intent_shape": "same_intent_multi_task",
            "tasks": [first, second],
        })
        third = copy.deepcopy(second)
        third["task_id"] = "task_wang"
        too_many = IntentTaskEnvelope.model_validate({
            **balance_envelope_payload(),
            "intent_shape": "same_intent_multi_task",
            "tasks": [first, second, third],
        })

        self.assertEqual(validate_intent_constraints(valid, [constraint]), ())
        self.assertEqual(
            validate_intent_constraints(too_many, [constraint]),
            ("task_cardinality_mismatch",),
        )

    def test_required_multi_intent_constraints_require_each_distinct_intent(self):
        faq = IntentConstraint.model_validate(balance_constraint_payload(
            intent_id="ccb.home.transfer.faq",
            action="HELP",
            business_object="transfer",
            required_shape="multi_intent",
            unresolved_slot_readiness=None,
            execution_mode="knowledge-only",
            required_slots=[],
            capability_bindings=[],
        ))
        balance = self.constraint.model_copy(update={"required_shape": "multi_intent"})
        tasks = [
            {
                "task_id": "task_faq", "intent_id": "ccb.home.transfer.faq",
                "action": "HELP", "business_object": "transfer", "slots": {},
                "dependencies": [], "execution_readiness": "ready", "risk_level": "L0",
            },
            balance_envelope_payload()["tasks"][0],
        ]
        envelope = IntentTaskEnvelope.model_validate({
            **balance_envelope_payload(), "intent_shape": "multi_intent", "tasks": tasks,
        })

        self.assertEqual(validate_intent_constraints(envelope, [faq, balance]), ())

    def test_multi_intent_rejects_repeated_instances_of_only_one_intent(self):
        tasks = [
            balance_envelope_payload()["tasks"][0],
            {
                **balance_envelope_payload()["tasks"][0],
                "task_id": "task_balance_second",
            },
        ]
        envelope = IntentTaskEnvelope.model_validate({
            **balance_envelope_payload(),
            "intent_shape": "multi_intent",
            "tasks": tasks,
        })

        self.assertEqual(
            validate_intent_constraints(envelope, [self.constraint]),
            ("intent_id_mismatch",),
        )

    def test_balance_needs_slots_requires_ordered_readonly_discovery(self):
        plan = CapabilityPlan.model_validate(balance_discovery_plan_payload())
        before = plan.model_dump()

        self.assertTrue(readonly_discovery_applies(self.envelope, self.capabilities, [self.constraint]))
        self.assertEqual(
            validate_readonly_discovery(plan, self.envelope, self.capabilities, [self.constraint]), ()
        )
        self.assertEqual(plan.model_dump(), before)

    def test_readonly_discovery_also_applies_when_agent_marks_missing_slot_for_clarification(self):
        envelope = IntentTaskEnvelope.model_validate(balance_envelope_payload(
            execution_readiness="needs_clarification"
        ))

        self.assertEqual(validate_intent_constraints(envelope, [self.constraint]), ())
        self.assertTrue(readonly_discovery_applies(envelope, self.capabilities, [self.constraint]))

    def test_balance_policy_rejects_early_clarification_and_direct_balance(self):
        clarification = CapabilityPlan.model_validate(
            {
                **balance_discovery_plan_payload(),
                "status": "clarification_required",
                "steps": [],
            }
        )
        direct_payload = balance_discovery_plan_payload()
        direct_payload["steps"] = [direct_payload["steps"][1]]
        direct_payload["steps"][0]["depends_on"] = []
        direct = CapabilityPlan.model_validate(direct_payload)

        self.assertEqual(
            validate_readonly_discovery(clarification, self.envelope, self.capabilities, [self.constraint]),
            ("readonly_discovery_required",),
        )
        self.assertIn(
            "discovery_step_order_invalid",
            validate_readonly_discovery(direct, self.envelope, self.capabilities, [self.constraint]),
        )

    def test_balance_policy_accepts_clarification_only_after_ordered_discovery_prefix(self):
        clarification = CapabilityPlan.model_validate(
            {
                **balance_discovery_plan_payload(),
                "status": "clarification_required",
                "steps": [],
            }
        )

        self.assertEqual(
            validate_readonly_discovery(
                clarification,
                self.envelope,
                self.capabilities,
                [self.constraint],
                ["bank.account.list"],
            ),
            (),
        )
        self.assertEqual(
            validate_readonly_discovery(
                clarification,
                self.envelope,
                self.capabilities,
                [self.constraint],
                ["bank.account.balance.get"],
            ),
            ("readonly_discovery_required",),
        )

    def test_balance_policy_rejects_order_dependency_confirmation_and_side_effects(self):
        reversed_payload = balance_discovery_plan_payload()
        reversed_payload["steps"] = list(reversed(reversed_payload["steps"]))
        reversed_payload["steps"][0]["depends_on"] = []
        reversed_payload["steps"][1]["depends_on"] = []
        reversed_plan = CapabilityPlan.model_validate(reversed_payload)

        confirmation_payload = balance_discovery_plan_payload()
        confirmation_payload["steps"][1]["requires_user_confirmation"] = True
        confirmation_plan = CapabilityPlan.model_validate(confirmation_payload)

        side_effect_payload = balance_discovery_plan_payload()
        side_effect_payload["steps"].append(
            {
                "step_id": "step_transfer",
                "task_id": "task_balance",
                "capability_id": "bank.transfer.execute",
                "input_bindings": {},
                "requires_user_confirmation": False,
                "depends_on": ["step_balance"],
            }
        )
        side_effect_plan = CapabilityPlan.model_validate(side_effect_payload)
        side_effect_capabilities = self.capabilities + [
            capability("bank.transfer.execute", side_effect="state-change")
        ]

        reversed_violations = validate_readonly_discovery(
            reversed_plan, self.envelope, self.capabilities, [self.constraint]
        )
        self.assertIn("discovery_step_order_invalid", reversed_violations)
        self.assertIn("discovery_dependency_missing", reversed_violations)
        self.assertEqual(
            validate_readonly_discovery(
                confirmation_plan, self.envelope, self.capabilities, [self.constraint]
            ),
            ("unexpected_confirmation",),
        )
        self.assertIn(
            "unexpected_side_effect_capability",
            validate_readonly_discovery(
                side_effect_plan, self.envelope, side_effect_capabilities, [self.constraint]
            ),
        )

    def test_missing_discovery_capability_allows_clarification(self):
        clarification = CapabilityPlan.model_validate(
            {
                **balance_discovery_plan_payload(),
                "status": "clarification_required",
                "steps": [],
            }
        )
        capabilities = [capability("bank.account.balance.get")]

        self.assertFalse(readonly_discovery_applies(self.envelope, capabilities, [self.constraint]))
        self.assertEqual(
            validate_readonly_discovery(clarification, self.envelope, capabilities, [self.constraint]), ()
        )

    def test_scalar_slot_without_resolver_allows_clarification_before_execution(self):
        constraint = IntentConstraint.model_validate(balance_constraint_payload(
            intent_id="ccb.cashflow.spending-explain",
            business_object="spending_analysis",
            required_slots=["period"],
            capability_bindings=["bank.cashflow.spending-analysis.get"],
            slot_contract={
                "version": "2.0",
                "slots": [{
                    "slotId": "slot.cashflow.period",
                    "name": "period",
                    "label": "分析月份",
                    "type": "text",
                    "required": True,
                    "extractionKeys": ["period", "month"],
                    "normalizer": "year_month",
                    "resolvedField": "period",
                }],
                "clarificationOrder": ["period"],
                "executionBinding": {
                    "capabilityId": "bank.cashflow.spending-analysis.get",
                    "requiresConfirmation": False,
                },
            },
        ))
        envelope = IntentTaskEnvelope.model_validate(balance_envelope_payload(
            intent_id="ccb.cashflow.spending-explain",
            business_object="spending_analysis",
        ))
        capabilities = [capability("bank.cashflow.spending-analysis.get")]
        clarification = CapabilityPlan.model_validate({
            **balance_discovery_plan_payload(),
            "status": "clarification_required",
            "steps": [],
        })

        self.assertFalse(readonly_discovery_applies(envelope, capabilities, [constraint]))
        self.assertEqual(
            validate_readonly_discovery(
                clarification, envelope, capabilities, [constraint]
            ),
            (),
        )

    def test_non_balance_ready_and_multi_task_envelopes_do_not_trigger_policy(self):
        non_balance = IntentTaskEnvelope.model_validate(
            balance_envelope_payload(
                intent_id="ccb.account.transactions.query",
                business_object="account_transactions",
            )
        )
        ready_payload = balance_envelope_payload(execution_readiness="ready")
        ready_payload["tasks"][0]["slots"] = {
            "accountId": {
                "value": "opaque",
                "source": "user_selection",
                "confidence": 1,
            }
        }
        ready = IntentTaskEnvelope.model_validate(ready_payload)
        multi_payload = balance_envelope_payload()
        second_task = copy.deepcopy(multi_payload["tasks"][0])
        second_task["task_id"] = "task_balance_2"
        multi_payload["intent_shape"] = "same_intent_multi_task"
        multi_payload["tasks"].append(second_task)
        multi = IntentTaskEnvelope.model_validate(multi_payload)

        self.assertFalse(readonly_discovery_applies(non_balance, self.capabilities, [self.constraint]))
        self.assertFalse(readonly_discovery_applies(ready, self.capabilities, [self.constraint]))
        self.assertFalse(readonly_discovery_applies(multi, self.capabilities, [self.constraint]))


if __name__ == "__main__":
    unittest.main()
