import unittest

from app.intent_shape import analyze_intent_shape


class IntentShapeAnalyzerTests(unittest.TestCase):
    def test_punctuation_only_input_is_authoritative_non_task(self):
        assessment = analyze_intent_shape("？ ……！")

        self.assertEqual(assessment.shape, "non_task")
        self.assertEqual(assessment.expected_task_count, 0)
        self.assertEqual(assessment.segments, ())
        self.assertTrue(assessment.authoritative)
        self.assertIn("no_semantic_signal", assessment.evidence)

    def test_single_transfer_is_single_intent(self):
        assessment = analyze_intent_shape("给张帅转100")

        self.assertEqual(assessment.shape, "single_intent")
        self.assertEqual(assessment.entities, ())
        self.assertEqual(assessment.expected_task_count, 1)
        self.assertEqual(assessment.ambiguities, ())
        self.assertFalse(assessment.authoritative)

    def test_explicit_per_payee_transfer_preserves_two_business_goals(self):
        assessment = analyze_intent_shape("给张帅和李四各转100元")

        self.assertEqual(assessment.shape, "same_intent_multi_task")
        self.assertEqual(assessment.entities, ())
        self.assertEqual(assessment.entity_count_hint, 2)
        self.assertEqual(assessment.expected_task_count, 2)
        self.assertEqual(assessment.ambiguities, ())
        self.assertIn("amount_scope:per_task", assessment.evidence)

    def test_unspecified_amount_scope_is_a_task_level_ambiguity(self):
        assessment = analyze_intent_shape("给张帅和李四转100元")

        self.assertEqual(assessment.shape, "same_intent_multi_task")
        self.assertEqual(assessment.entities, ())
        self.assertEqual(assessment.entity_count_hint, 2)
        self.assertEqual(assessment.expected_task_count, 2)
        self.assertEqual(len(assessment.ambiguities), 1)
        ambiguity = assessment.ambiguities[0]
        self.assertEqual(ambiguity.kind, "amount_scope")
        self.assertEqual(ambiguity.task_indexes, (0, 1))
        self.assertEqual(ambiguity.options, ("per_task", "total"))

    def test_adjacent_two_character_payee_names_preserve_two_business_goals(self):
        assessment = analyze_intent_shape("给张三李四转200")

        self.assertEqual(assessment.shape, "same_intent_multi_task")
        self.assertEqual(assessment.entities, ("张三", "李四"))
        self.assertEqual(assessment.expected_task_count, 2)
        self.assertFalse(assessment.authoritative)
        self.assertIn("adjacent_person_entities", assessment.evidence)

    def test_adjacent_three_and_two_character_names_are_split_without_examples(self):
        assessment = analyze_intent_shape("向王小宝赵六转100")

        self.assertEqual(assessment.shape, "same_intent_multi_task")
        self.assertEqual(assessment.entities, ("王小宝", "赵六"))
        self.assertEqual(assessment.expected_task_count, 2)
        self.assertFalse(assessment.authoritative)

    def test_adjacent_names_with_per_payee_marker_have_resolved_amount_scope(self):
        assessment = analyze_intent_shape("给张三李四各转200元")

        self.assertEqual(assessment.shape, "same_intent_multi_task")
        self.assertEqual(assessment.entities, ("张三", "李四"))
        self.assertEqual(assessment.entity_count_hint, 2)
        self.assertEqual(assessment.expected_task_count, 2)
        self.assertEqual(assessment.ambiguities, ())
        self.assertIn("amount_scope:per_task", assessment.evidence)

    def test_compound_surname_four_character_name_remains_one_business_goal(self):
        assessment = analyze_intent_shape("给欧阳娜娜转200")

        self.assertEqual(assessment.shape, "single_intent")
        self.assertEqual(assessment.entities, ())
        self.assertEqual(assessment.expected_task_count, 1)
        self.assertEqual(assessment.ambiguities, ())

    def test_distinct_business_actions_are_multi_intent(self):
        assessment = analyze_intent_shape("查余额并给张三转100")

        self.assertEqual(assessment.shape, "multi_intent")
        self.assertEqual(assessment.expected_task_count, 2)
        self.assertEqual(assessment.segments, ("查余额", "给张三转100"))

    def test_total_amount_marker_is_not_treated_as_unresolved_scope(self):
        assessment = analyze_intent_shape("给张帅和李四总共转100元")

        self.assertEqual(assessment.shape, "same_intent_multi_task")
        self.assertEqual(assessment.ambiguities, ())
        self.assertIn("amount_scope:total", assessment.evidence)

    def test_prompt_projection_contains_only_shape_evidence(self):
        payload = analyze_intent_shape("给张帅和李四转100元").to_prompt_dict()

        self.assertEqual(payload["shape"], "same_intent_multi_task")
        self.assertFalse(payload["authoritative"])
        self.assertEqual(payload["expectedTaskCount"], 2)
        self.assertEqual(payload["entities"], [])
        self.assertEqual(payload["entityCountHint"], 2)
        self.assertEqual(payload["ambiguities"][0]["kind"], "amount_scope")
        self.assertNotIn("intentId", str(payload))


if __name__ == "__main__":
    unittest.main()
