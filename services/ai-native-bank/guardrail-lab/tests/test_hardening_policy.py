import unittest

from adapters.hardening_policy import (
    Decision,
    ReasonCode,
    normalize_upstream_result,
    review_result,
    unavailable_result,
)


class HardeningPolicyTest(unittest.TestCase):
    def test_unavailable_is_not_reported_as_attack_or_success(self):
        result = unavailable_result(
            ReasonCode.SECURITY_DETECTOR_UNAVAILABLE,
            conversation_id="synthetic-conversation",
        )

        self.assertEqual(result["decision"], Decision.UNAVAILABLE.value)
        self.assertTrue(result["is_blocked"])
        self.assertFalse(result["is_attack"])
        self.assertEqual(result["detected_by"], "system_guardrail")
        self.assertNotIn("Traceback", result["response"])

    def test_upstream_error_is_normalized_without_leaking_details(self):
        result = normalize_upstream_result(
            {
                "response": "internal failure",
                "is_blocked": False,
                "error": "provider secret and stack trace",
            },
            conversation_id="synthetic-conversation",
        )

        self.assertEqual(
            result["reason_code"],
            ReasonCode.GUARDRAIL_RUNTIME_ERROR.value,
        )
        self.assertNotIn("provider secret", str(result))
        self.assertNotIn("stack trace", str(result))

    def test_confirmed_prompt_injection_uses_stable_safe_template(self):
        result = normalize_upstream_result(
            {
                "response": "risk=high; rule=private_rule",
                "is_blocked": True,
                "block_reason": "prompt_injection",
                "blocked_by": "llm",
                "violated_rules": ["private_rule"],
            },
            conversation_id="synthetic-conversation",
        )

        self.assertEqual(result["decision"], Decision.BLOCK.value)
        self.assertEqual(result["reason_code"], ReasonCode.PROMPT_INJECTION.value)
        self.assertNotIn("private_rule", result["response"])
        self.assertNotIn("risk=high", result["response"])
        self.assertEqual(result["violated_rules"], [])

    def test_sensitive_output_is_distinct_from_an_attack(self):
        result = normalize_upstream_result(
            {
                "response": "unsafe original answer",
                "is_blocked": True,
                "block_reason": "PII detected in output: CN_BANK_CARD",
                "blocked_by": "pii",
            },
            conversation_id="synthetic-conversation",
        )

        self.assertEqual(result["reason_code"], ReasonCode.SENSITIVE_OUTPUT.value)
        self.assertFalse(result["is_attack"])
        self.assertNotIn("CN_BANK_CARD", result["response"])

    def test_clean_result_remains_allowed(self):
        result = normalize_upstream_result(
            {
                "response": "safe answer",
                "is_blocked": False,
                "conversation_id": "upstream-value",
            },
            conversation_id="synthetic-conversation",
        )

        self.assertEqual(result["decision"], Decision.ALLOW.value)
        self.assertFalse(result["is_blocked"])
        self.assertEqual(result["response"], "safe answer")
        self.assertEqual(result["conversation_id"], "synthetic-conversation")

    def test_review_is_a_non_attack_terminal_decision(self):
        result = review_result(
            ReasonCode.AMBIGUOUS_RISK_INTENT,
            conversation_id="synthetic-conversation",
        )

        self.assertEqual(result["decision"], Decision.REVIEW.value)
        self.assertTrue(result["is_blocked"])
        self.assertFalse(result["is_attack"])
        self.assertEqual(result["reason_code"], "ambiguous_risk_intent")
        self.assertNotIn("score", result["response"].lower())
        self.assertNotIn("rule", result["response"].lower())

    def test_review_marker_from_llm_is_normalized_to_review(self):
        result = normalize_upstream_result(
            {
                "response": "internal text",
                "is_blocked": True,
                "block_reason": "ambiguous_risk_intent",
                "blocked_by": "llm",
            },
            conversation_id="synthetic-conversation",
        )

        self.assertEqual(result["decision"], "review")
        self.assertEqual(result["reason_code"], "ambiguous_risk_intent")
        self.assertFalse(result["is_attack"])
        self.assertNotEqual(result["response"], "internal text")

    def test_empty_allowed_answer_is_unavailable(self):
        result = normalize_upstream_result(
            {"response": "   ", "is_blocked": False},
            conversation_id="synthetic-conversation",
        )

        self.assertEqual(result["decision"], "unavailable")
        self.assertEqual(result["reason_code"], "empty_answer")

    def test_llm_unsafe_category_is_not_misreported_as_prompt_injection(self):
        result = normalize_upstream_result(
            {
                "is_blocked": True,
                "block_reason": "fraud_or_laundering",
                "blocked_by": "llm",
            },
            conversation_id="synthetic-conversation",
        )

        self.assertEqual(result["decision"], "block")
        self.assertEqual(result["reason_code"], "unsafe_content")


if __name__ == "__main__":
    unittest.main()
