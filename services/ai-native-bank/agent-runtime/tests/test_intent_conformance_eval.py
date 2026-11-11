from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path

from app.intent_conformance_eval import (
    IntentConformanceEvaluationError,
    evaluate_intent_conformance,
)


TEST_PATH = Path(__file__).resolve()
DEFAULT_ROOT = TEST_PATH.parents[4] if len(TEST_PATH.parents) > 4 else Path("/app")
ROOT = Path(os.environ.get("AI_BANK_PROJECT_ROOT", DEFAULT_ROOT))
GRAPH_PATH = ROOT / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v1.0.0.json"
DATASET_PATH = ROOT / "data" / "eval" / "ccb-intent-conformance.v1.json"


class IntentConformanceEvaluationTest(unittest.TestCase):
    def test_runtime_graph_recall_executes_every_published_contract_case(self) -> None:
        report = evaluate_intent_conformance(
            dataset_path=DATASET_PATH,
            graph_path=GRAPH_PATH,
            evaluated_at="2026-09-05T00:00:00Z",
        )

        self.assertEqual(report["datasetId"], "ccb-intent-conformance-v1")
        self.assertEqual(report["engine"]["component"], "IntentGraphRepository.recall")
        self.assertEqual(report["summary"]["cases"], 304)
        self.assertEqual(report["summary"]["activeCases"], 238)
        self.assertEqual(report["summary"]["plannedCases"], 66)
        self.assertEqual(report["summary"]["failedUtterances"], 0)
        self.assertEqual(report["summary"]["contractPassRate"], 1.0)
        self.assertEqual(report["summary"]["activeCandidateRecallRate"], 1.0)
        self.assertEqual(report["summary"]["plannedSuppressionRate"], 1.0)
        self.assertLess(report["summary"]["top1ExactRate"], 1.0)
        self.assertEqual(report["failures"], [])

    def test_runtime_evaluation_rejects_a_stale_dataset_graph_digest(self) -> None:
        dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        stale = copy.deepcopy(dataset)
        stale["graph"]["digest"] = "sha256:stale"

        with self.assertRaisesRegex(IntentConformanceEvaluationError, "digest"):
            evaluate_intent_conformance(
                dataset=stale,
                graph_path=GRAPH_PATH,
                evaluated_at="2026-09-05T00:00:00Z",
            )

    def test_runtime_evaluation_rejects_unbounded_or_unknown_case_input(self) -> None:
        dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        hostile = copy.deepcopy(dataset)
        hostile["cases"][0]["pageContext"]["customerId"] = "forbidden"

        with self.assertRaisesRegex(IntentConformanceEvaluationError, "page context"):
            evaluate_intent_conformance(
                dataset=hostile,
                graph_path=GRAPH_PATH,
                evaluated_at="2026-09-05T00:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
