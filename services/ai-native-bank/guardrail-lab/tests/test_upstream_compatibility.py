import json
import tempfile
import unittest
from pathlib import Path

from evaluation.run_upstream_compatibility import classify_outcomes, load_corpus


class UpstreamCompatibilityTests(unittest.TestCase):
    def test_loads_white_and_black_without_mutating_source_labels(self):
        source = {
            "white_samples": [{"label": "white-original", "text": "safe"}],
            "black_samples": [{"label": "black-original", "text": "unsafe"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.json"
            path.write_text(json.dumps(source), encoding="utf-8")

            loaded = load_corpus(path)

        self.assertEqual(loaded, source)

    def test_acceptance_requires_no_white_blocks_and_no_black_allows(self):
        corpus = {
            "white_samples": [{"label": "w", "text": "safe"}],
            "black_samples": [{"label": "b", "text": "unsafe"}],
        }

        passed = classify_outcomes(
            corpus,
            lambda text, _request_id: "allow" if text == "safe" else "review",
        )
        failed = classify_outcomes(
            corpus,
            lambda text, _request_id: "block" if text == "safe" else "allow",
        )

        self.assertTrue(passed["passed"])
        self.assertEqual(passed["white"]["allow"], 1)
        self.assertEqual(passed["black"]["review"], 1)
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["whiteBlockIndexes"], [1])
        self.assertEqual(failed["blackAllowIndexes"], [1])

    def test_invalid_decisions_are_counted_as_unavailable(self):
        corpus = {
            "white_samples": [{"label": "w", "text": "safe"}],
            "black_samples": [{"label": "b", "text": "unsafe"}],
        }

        result = classify_outcomes(corpus, lambda _text, _request_id: "invalid")

        self.assertEqual(result["white"]["unavailable"], 1)
        self.assertEqual(result["black"]["unavailable"], 1)
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
