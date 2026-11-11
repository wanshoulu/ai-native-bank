from __future__ import annotations

import os
import unittest
from datetime import date
from pathlib import Path

from app.contracts import SlotValue
from app.intent_graph import IntentGraphRepository
from app.slot_contract import (
    SlotContractEvaluator,
    evaluate_amount,
    extract_normalized_message_slots,
    resolve_reference,
    with_provider_resolved_reference,
)


GRAPH_PATH = Path(
    os.getenv(
        "AI_BANK_INTENT_GRAPH_PATH",
        "/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v1.0.0.json",
    )
)


def explicit(value):
    return SlotValue(value=value, source="user_explicit", confidence=1)


def remembered(value):
    return SlotValue(value=value, source="context", confidence=1)


class SlotContractEvaluatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repository = IntentGraphRepository.load(GRAPH_PATH)
        cls.repository = repository
        cls.contract = repository.candidate_for_intent(
            "ccb.transfer.create", score=240, evidence=["test"]
        ).slot_contract
        cls.evaluator = SlotContractEvaluator()

    def test_credit_limit_normalization_is_not_constrained_by_transfer_limit(self):
        contract = self.repository.candidate_for_intent(
            "ccb.credit.card.limit-change", score=240, evidence=["test"]
        ).slot_contract

        result = self.evaluator.evaluate(
            contract=contract,
            current_slots={
                "cardReference": explicit("尾号8891"),
                "requestedLimit": explicit("100000"),
            },
            conversation_slots={},
        )

        self.assertEqual(result.resolved_values["requestedLimitMinor"], 10_000_000)
        self.assertEqual(result.readiness, "ready")

    def test_year_month_normalizer_uses_platform_clock_for_month_only_text(self):
        contract = self.repository.candidate_for_intent(
            "ccb.cashflow.spending-explain", score=240, evidence=["test"]
        ).slot_contract
        evaluator = SlotContractEvaluator(reference_date=date(2026, 9, 5))

        result = evaluator.evaluate(
            contract=contract,
            current_slots={"period": explicit("八月")},
            conversation_slots={},
        )

        self.assertEqual(result.resolved_values["period"], "2026-08")
        self.assertEqual(result.readiness, "ready")

    def test_message_period_extraction_is_single_task_safe_and_rejects_ambiguity(self):
        contract = self.repository.candidate_for_intent(
            "ccb.cashflow.spending-explain", score=240, evidence=["test"]
        ).slot_contract

        extracted = extract_normalized_message_slots(
            contract,
            "为什么八月钱花得快",
            reference_date=date(2026, 9, 5),
        )
        ambiguous = extract_normalized_message_slots(
            contract,
            "分别解释七月和八月支出",
            reference_date=date(2026, 9, 5),
        )

        self.assertEqual(extracted["period"].value, "2026-08")
        self.assertEqual(extracted["period"].source, "user_explicit")
        self.assertEqual(ambiguous, {})

    def test_wealth_advice_message_extracts_planned_amount_and_horizon_from_one_reply(self):
        contract = self.repository.candidate_for_intent(
            "ccb.wealth.advisor.recommend", score=240, evidence=["test"]
        ).slot_contract

        extracted = extract_normalized_message_slots(
            contract,
            "投资1万，持有3个月",
            reference_date=date(2026, 9, 5),
        )
        result = self.evaluator.evaluate(
            contract=contract,
            current_slots=extracted,
            conversation_slots={"investmentGoal": remembered("steady_growth")},
        )

        self.assertEqual(result.resolved_values["plannedAmountMinor"], 1_000_000)
        self.assertEqual(result.resolved_values["horizonMonths"], 3)
        self.assertEqual(result.resolved_values["investmentGoal"], "steady_growth")

    def test_transfer_message_preserves_present_slots_and_only_marks_account_missing(self):
        result = self.evaluator.evaluate(
            contract=self.contract,
            current_slots={
                "payeeName": explicit("张三"),
                "amount": explicit("100"),
            },
            conversation_slots={},
        )

        self.assertEqual(result.resolved_values["amountMinor"], 10000)
        self.assertEqual(result.resolved_values["currency"], "CNY")
        self.assertEqual(result.missing, ("fromAccount",))
        self.assertEqual(result.recognized, ("payee", "amount"))
        self.assertEqual(result.optional_absent, ("purpose",))
        self.assertEqual(result.readiness, "needs_slots")

    def test_current_explicit_value_overrides_same_intent_conversation_value(self):
        result = self.evaluator.evaluate(
            contract=self.contract,
            current_slots={"amount": explicit("200")},
            conversation_slots={"amount": remembered("100")},
        )

        self.assertEqual(result.resolved_values["amountMinor"], 20000)

    def test_currency_slot_validates_format_without_embedding_product_currency_policy(self):
        result = self.evaluator.evaluate(
            contract=self.contract,
            current_slots={"currency": explicit("usd")},
            conversation_slots={},
        )

        self.assertEqual(result.resolved_values["currency"], "USD")

    def test_invalid_and_ambiguous_values_do_not_become_ready(self):
        self.assertEqual(evaluate_amount("0").state, "invalid")
        self.assertEqual(evaluate_amount("1.001").state, "invalid")
        self.assertEqual(resolve_reference(["first", "second"]).state, "ambiguous")
        self.assertEqual(resolve_reference([]).state, "missing")

    def test_optional_purpose_is_preserved_only_when_explicit(self):
        absent = self.evaluator.evaluate(
            contract=self.contract,
            current_slots={},
            conversation_slots={},
        )
        present = self.evaluator.evaluate(
            contract=self.contract,
            current_slots={"purpose": explicit("房租")},
            conversation_slots={},
        )

        self.assertNotIn("purpose", absent.resolved_values)
        self.assertEqual(present.resolved_values["purpose"], "房租")

    def test_amount_minor_input_is_not_multiplied_twice(self):
        result = self.evaluator.evaluate(
            contract=self.contract,
            current_slots={"amountMinor": explicit(10000)},
            conversation_slots={},
        )

        self.assertEqual(result.resolved_values["amountMinor"], 10000)

    def test_money_semantic_type_normalizes_common_currency_notation(self):
        for value, expected_minor in (
            ("100元", 10_000),
            ("¥1,000.50", 100_050),
            ("CNY 88.20", 8_820),
            ("88块", 8_800),
        ):
            with self.subTest(value=value):
                assessment = evaluate_amount(value)
                self.assertEqual(assessment.state, "resolved")
                self.assertEqual(assessment.resolved_value, expected_minor)

    def test_amount_and_reference_helpers_close_invalid_boundaries(self):
        self.assertEqual(resolve_reference(["only"]).value, "only")
        for value, already_minor in [
            (True, False),
            ("not-a-number", False),
            ("1.5", True),
            ("10000000000000.01", False),
        ]:
            self.assertEqual(
                evaluate_amount(value, already_minor=already_minor).state,
                "invalid",
            )

    def test_contract_marks_malformed_semantic_values_for_clarification(self):
        result = self.evaluator.evaluate(
            contract=self.contract,
            current_slots={
                "payeeName": explicit(123),
                "fromAccountName": explicit(" "),
                "amount": explicit(True),
                "currency": explicit("US"),
                "purpose": explicit("x" * 41),
            },
            conversation_slots={},
        )

        self.assertEqual(result.readiness, "needs_clarification")
        self.assertEqual(
            result.invalid,
            ("payee", "fromAccount", "amount", "currency", "purpose"),
        )
        with self.assertRaises(KeyError):
            result.assessment("unknown")

    def test_only_provider_resolution_can_promote_reference_to_resolved(self):
        contract = self.repository.candidate_for_intent(
            "ccb.home.repayment.repay", score=240, evidence=["test"]
        ).slot_contract
        card = contract.slot("card")
        raw = self.evaluator.evaluate(
            contract=contract,
            current_slots={
                "card": explicit("尾号8891的信用卡"),
                "cardId": explicit("credit-card-8891"),
            },
            conversation_slots={},
        )

        self.assertEqual(raw.assessment("card").state, "extracted")
        self.assertNotIn("cardId", raw.resolved_values)

        trusted = with_provider_resolved_reference(
            raw,
            card,
            semantic_value="龙卡全球支付卡 · 尾号8891",
            resolved_value="credit-card-8891",
        )

        self.assertEqual(trusted.assessment("card").state, "resolved")
        self.assertEqual(trusted.resolved_values["cardId"], "credit-card-8891")
        self.assertEqual(trusted.missing, ("fromAccount", "amount"))
        self.assertEqual(trusted.readiness, "needs_slots")

    def test_provider_resolution_rejects_untrusted_identifier_shape(self):
        contract = self.repository.candidate_for_intent(
            "ccb.home.repayment.repay", score=240, evidence=["test"]
        ).slot_contract
        evaluation = self.evaluator.evaluate(
            contract=contract,
            current_slots={"card": explicit("尾号8891的信用卡")},
            conversation_slots={},
        )

        with self.assertRaises(ValueError):
            with_provider_resolved_reference(
                evaluation,
                contract.slot("card"),
                semantic_value="龙卡全球支付卡 · 尾号8891",
                resolved_value="../../unsafe",
            )

    def test_sequential_reference_resolution_retains_all_provider_facts(self):
        contract = self.repository.candidate_for_intent(
            "ccb.home.repayment.repay", score=240, evidence=["test"]
        ).slot_contract
        slots = {
            "card": remembered("龙卡全球支付卡 · 尾号8891"),
            "cardId": remembered("credit-card-8891"),
            "fromAccount": remembered("工资卡 · 尾号6217"),
            "fromAccountId": remembered("account-6217"),
        }
        evaluation = self.evaluator.evaluate(
            contract=contract,
            current_slots=slots,
            conversation_slots={},
        )
        for name, semantic, identifier in (
            ("card", "龙卡全球支付卡 · 尾号8891", "credit-card-8891"),
            ("fromAccount", "工资卡 · 尾号6217", "account-6217"),
        ):
            evaluation = with_provider_resolved_reference(
                evaluation,
                contract.slot(name),
                semantic_value=semantic,
                resolved_value=identifier,
            )

        self.assertEqual(
            dict(evaluation.resolved_values),
            {
                "cardId": "credit-card-8891",
                "fromAccountId": "account-6217",
            },
        )
        self.assertEqual(evaluation.missing, ("amount",))
        self.assertEqual(evaluation.readiness, "needs_slots")


if __name__ == "__main__":
    unittest.main()
