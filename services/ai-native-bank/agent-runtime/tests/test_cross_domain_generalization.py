import unittest

from app.intent_shape import analyze_intent_shape


class CrossDomainGeneralizationTests(unittest.TestCase):
    def test_distinct_account_queries_are_non_authoritative_multi_intent_evidence(self):
        assessment = analyze_intent_shape("查工资卡余额并查流水")

        self.assertEqual(assessment.shape_hint, "multi_intent")
        self.assertFalse(assessment.authoritative)
        self.assertIn("explicit_clause_boundary", assessment.evidence)

    def test_credit_card_multi_instance_is_preserved_as_non_authoritative_evidence(self):
        assessment = analyze_intent_shape("把尾号8891和2268两张信用卡都挂失")

        self.assertEqual(assessment.shape_hint, "same_intent_multi_task")
        self.assertFalse(assessment.authoritative)
        self.assertEqual(assessment.entity_count_hint, 2)

    def test_shape_evidence_does_not_assign_a_domain_intent(self):
        payload = analyze_intent_shape("给张三李四分别充100元话费").to_prompt_dict()

        self.assertNotIn("intentId", payload)
        self.assertFalse(payload["authoritative"])


if __name__ == "__main__":
    unittest.main()

