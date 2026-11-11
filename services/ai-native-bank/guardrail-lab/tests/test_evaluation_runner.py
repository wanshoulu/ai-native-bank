import json
import tempfile
import unittest
from pathlib import Path

from adapters.evaluate_text_guardrail import (
    load_cases,
    normalize_disposition,
    select_cases,
    summarize_results,
)


class EvaluationRunnerTest(unittest.TestCase):
    def test_load_cases_rejects_unknown_disposition_and_duplicate_ids(self):
        valid = {
            "id": "case-001",
            "category": "safe",
            "text": "synthetic",
            "expectedDisposition": "allow",
            "notes": "synthetic case",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            invalid = {**valid, "expectedDisposition": "maybe"}
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "disposition"):
                load_cases(path)

            path.write_text(
                "\n".join((json.dumps(valid), json.dumps(valid))),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                load_cases(path)

    def test_normalizes_declared_four_state_decision_before_legacy_boolean(self):
        self.assertEqual(
            normalize_disposition({"decision": "review", "is_blocked": True}),
            "review",
        )
        self.assertEqual(
            normalize_disposition({"decision": "unavailable", "is_blocked": True}),
            "unavailable",
        )
        self.assertEqual(normalize_disposition({"is_blocked": True}), "block")
        self.assertEqual(normalize_disposition({"is_blocked": False}), "allow")
        self.assertEqual(
            normalize_disposition({"decision": "unknown", "is_blocked": False}),
            "error",
        )
        self.assertEqual(normalize_disposition({}), "error")

    def test_selects_a_bounded_evaluation_batch(self):
        cases = [{"id": f"case-{index}"} for index in range(5)]

        self.assertEqual(
            [case["id"] for case in select_cases(cases, offset=1, limit=2)],
            ["case-1", "case-2"],
        )
        with self.assertRaisesRegex(ValueError, "offset"):
            select_cases(cases, offset=-1, limit=2)
        with self.assertRaisesRegex(ValueError, "limit"):
            select_cases(cases, offset=0, limit=0)
        with self.assertRaisesRegex(ValueError, "empty"):
            select_cases(cases, offset=5, limit=1)

    def test_summary_counts_review_and_unavailable_separately(self):
        rows = [
            {
                "id": "a",
                "category": "ambiguous",
                "expectedDisposition": "review",
                "actualDisposition": "review",
                "elapsedMs": 10,
            },
            {
                "id": "b",
                "category": "safe",
                "expectedDisposition": "allow",
                "actualDisposition": "unavailable",
                "elapsedMs": 20,
            },
        ]

        summary = summarize_results(rows)

        self.assertEqual(summary["reviewMatches"], 1)
        self.assertEqual(summary["unavailableRuns"], 1)
        self.assertEqual(summary["reviewUnsupported"], 0)
        self.assertEqual(summary["categoryDecisionCounts"]["ambiguous"]["review"], 1)

    def test_summarizes_accuracy_safety_stability_and_latency(self):
        results = [
            {"id": "a", "category": "safe", "round": 1, "expectedDisposition": "allow", "actualDisposition": "allow", "elapsedMs": 100},
            {"id": "a", "category": "safe", "round": 2, "expectedDisposition": "allow", "actualDisposition": "allow", "elapsedMs": 120},
            {"id": "b", "category": "unsafe", "round": 1, "expectedDisposition": "block", "actualDisposition": "allow", "elapsedMs": 200},
            {"id": "b", "category": "unsafe", "round": 2, "expectedDisposition": "block", "actualDisposition": "block", "elapsedMs": 220},
            {"id": "c", "category": "ambiguous", "round": 1, "expectedDisposition": "review", "actualDisposition": "block", "elapsedMs": 300},
            {"id": "c", "category": "ambiguous", "round": 2, "expectedDisposition": "review", "actualDisposition": "block", "elapsedMs": 320},
        ]
        summary = summarize_results(results)

        self.assertEqual(summary["runs"], 6)
        self.assertEqual(summary["exactMatches"], 3)
        self.assertEqual(summary["falseAllows"], 1)
        self.assertEqual(summary["falseBlocks"], 0)
        self.assertEqual(summary["reviewUnsupported"], 2)
        self.assertEqual(summary["unstableCases"], 1)
        self.assertEqual(summary["latencyMs"]["p50"], 220)
        self.assertEqual(summary["latencyMs"]["p95"], 320)


if __name__ == "__main__":
    unittest.main()
