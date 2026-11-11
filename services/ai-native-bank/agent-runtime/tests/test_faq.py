from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import RuntimeSettings
from app.faq import FaqError, MockFaqRepository


FAQ_PATH = Path(
    os.getenv(
        "AI_BANK_FAQ_PATH",
        "/app/data/ai-native-bank/faq/ccb-mobile-bank-faq.v1.json",
    )
)
GRAPH_PATH = Path("/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v1.0.0.json")

VALID_ACTION_KEYS = {
    "home.account.overview",
    "home.details.overview",
    "home.transfer.apply",
    "home.transfer.overview",
    "credit.card-manage.activation",
    "credit.card-manage.loss-report",
    "credit.card-manage.limit",
    "credit.card-manage.transactions",
}


class MockFaqRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MockFaqRepository.load(
            FAQ_PATH,
            valid_action_keys=VALID_ACTION_KEYS,
        )

    def _write_payload(self, payload: object, directory: str) -> Path:
        target = Path(directory) / "faq.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return target

    def test_loads_exactly_twelve_versioned_entries(self):
        self.assertEqual(len(self.repository.entries), 12)
        self.assertEqual(self.repository.version, "1.0.0")
        self.assertTrue(all(entry.version == "1.0.0" for entry in self.repository.entries))

    def test_repository_is_immutable_and_canonical_lookup_is_read_only(self):
        canonical = self.repository.canonical_entry(
            "faq.transfer.arrival-time", version="1.0.0"
        )
        self.assertIs(canonical, self.repository.match("转账多久到账"))

        with self.assertRaises((AttributeError, TypeError)):
            self.repository.entries = ()
        with self.assertRaises(AttributeError):
            self.repository.entries.append(canonical)
        with self.assertRaises(TypeError):
            self.repository.entries_by_id[canonical.id] = canonical
        with self.assertRaises(FaqError):
            self.repository.canonical_entry(canonical.id, version="9.9.9")
        with self.assertRaises(FaqError):
            self.repository.canonical_entry("faq.missing", version="1.0.0")

    def test_uses_exact_governed_schema_and_unique_ids(self):
        payload = json.loads(FAQ_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"schemaVersion", "version", "scope", "entries"})
        self.assertEqual(len({entry["id"] for entry in payload["entries"]}), 12)
        for entry in payload["entries"]:
            self.assertEqual(
                set(entry),
                {
                    "id",
                    "intentId",
                    "questionPatterns",
                    "answer",
                    "recommendedActionKeys",
                },
            )

    def test_matches_specific_question_without_model_authorship(self):
        entry = self.repository.match("转账多久到账")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.id, "faq.transfer.arrival-time")
        self.assertEqual(entry.intent_id, "ccb.home.transfer.faq")
        self.assertIn("home.transfer.overview", entry.recommended_action_keys)
        self.assertEqual(self.repository.match("请问，转账多久到账？").id, entry.id)
        self.assertIsNone(self.repository.match("今天天气怎么样"))
        self.assertIsNone(self.repository.match("  "))

    def test_does_not_match_generic_short_phrases(self):
        for message in ("转账", "信用卡", "卡", "怎么办"):
            with self.subTest(message=message):
                self.assertIsNone(self.repository.match(message))

    def test_match_analysis_distinguishes_full_coverage_and_residual_semantics(self):
        covered = self.repository.analyze("请问，转账多久到账呀？")
        mixed = self.repository.analyze("转账多久到账，再查一下工资卡余额")

        self.assertEqual([item.entry.id for item in covered.matches], [
            "faq.transfer.arrival-time"
        ])
        self.assertTrue(covered.fully_covered)
        self.assertEqual(covered.residual_semantics, "")
        self.assertFalse(mixed.fully_covered)
        self.assertEqual(mixed.residual_semantics, "查工资卡余额")

    def test_match_analysis_returns_all_distinct_faqs(self):
        for message in (
            "转账多久到账，转账收手续费吗",
            "转账多久到账和转账收手续费吗",
        ):
            with self.subTest(message=message):
                analysis = self.repository.analyze(message)
                self.assertEqual(
                    [item.entry.id for item in analysis.matches],
                    ["faq.transfer.arrival-time", "faq.transfer.fee"],
                )
                self.assertTrue(analysis.fully_covered)

    def test_governed_rephrases_are_explicit_patterns(self):
        cases = {
            "转账多久到帐": "faq.transfer.arrival-time",
            "请问转账多长时间能到": "faq.transfer.arrival-time",
            "转账费怎么收": "faq.transfer.fee",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(self.repository.match(question).id, expected)

    def test_rejects_unknown_recommended_action(self):
        payload = json.loads(FAQ_PATH.read_text(encoding="utf-8"))
        payload["entries"][0]["recommendedActionKeys"] = ["home.transfer.execute"]
        with tempfile.TemporaryDirectory() as directory:
            target = self._write_payload(payload, directory)
            with self.assertRaisesRegex(FaqError, "recommended action"):
                MockFaqRepository.load(target, valid_action_keys=VALID_ACTION_KEYS)

    def test_rejects_duplicate_ids_and_non_semantic_version(self):
        payload = json.loads(FAQ_PATH.read_text(encoding="utf-8"))
        payload["entries"][1]["id"] = payload["entries"][0]["id"]
        with tempfile.TemporaryDirectory() as directory:
            target = self._write_payload(payload, directory)
            with self.assertRaisesRegex(FaqError, "duplicated"):
                MockFaqRepository.load(target, valid_action_keys=VALID_ACTION_KEYS)

        payload = json.loads(FAQ_PATH.read_text(encoding="utf-8"))
        payload["version"] = "v1"
        with tempfile.TemporaryDirectory() as directory:
            target = self._write_payload(payload, directory)
            with self.assertRaisesRegex(FaqError, "version"):
                MockFaqRepository.load(target, valid_action_keys=VALID_ACTION_KEYS)

    def test_rejects_empty_or_oversized_answers(self):
        for answer in ("", "答" * 601):
            with self.subTest(length=len(answer)):
                payload = json.loads(FAQ_PATH.read_text(encoding="utf-8"))
                payload["entries"][0]["answer"] = answer
                with tempfile.TemporaryDirectory() as directory:
                    target = self._write_payload(payload, directory)
                    with self.assertRaisesRegex(FaqError, "answer"):
                        MockFaqRepository.load(target, valid_action_keys=VALID_ACTION_KEYS)

    def test_rejects_missing_or_blank_question_patterns(self):
        for patterns in ([], [" ，！？ "]):
            with self.subTest(patterns=patterns):
                payload = json.loads(FAQ_PATH.read_text(encoding="utf-8"))
                payload["entries"][0]["questionPatterns"] = patterns
                with tempfile.TemporaryDirectory() as directory:
                    target = self._write_payload(payload, directory)
                    with self.assertRaisesRegex(FaqError, "question pattern"):
                        MockFaqRepository.load(target, valid_action_keys=VALID_ACTION_KEYS)

    def test_rejects_unknown_top_level_and_entry_fields(self):
        payload = json.loads(FAQ_PATH.read_text(encoding="utf-8"))
        payload["debug"] = True
        with tempfile.TemporaryDirectory() as directory:
            target = self._write_payload(payload, directory)
            with self.assertRaisesRegex(FaqError, "schema"):
                MockFaqRepository.load(target, valid_action_keys=VALID_ACTION_KEYS)

        payload = json.loads(FAQ_PATH.read_text(encoding="utf-8"))
        payload["entries"][0]["url"] = "https://example.test"
        with tempfile.TemporaryDirectory() as directory:
            target = self._write_payload(payload, directory)
            with self.assertRaisesRegex(FaqError, "entry schema"):
                MockFaqRepository.load(target, valid_action_keys=VALID_ACTION_KEYS)

    def test_rejects_duplicate_json_object_keys(self):
        text = FAQ_PATH.read_text(encoding="utf-8").replace(
            '"answer":',
            '"answer": "重复字段不应覆盖受治理答案",\n      "answer":',
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "faq.json"
            target.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(FaqError, "duplicate JSON key"):
                MockFaqRepository.load(target, valid_action_keys=VALID_ACTION_KEYS)

    def test_rejects_any_entry_count_other_than_twelve(self):
        payload = json.loads(FAQ_PATH.read_text(encoding="utf-8"))
        payload["entries"].pop()
        with tempfile.TemporaryDirectory() as directory:
            target = self._write_payload(payload, directory)
            with self.assertRaisesRegex(FaqError, "exactly 12"):
                MockFaqRepository.load(target, valid_action_keys=VALID_ACTION_KEYS)


class FaqSettingsTests(unittest.TestCase):
    def test_faq_path_has_a_safe_default_and_environment_override(self):
        with patch.dict(os.environ, {}, clear=True):
            defaults = RuntimeSettings.from_env()
        self.assertEqual(
            defaults.faq_path,
            "/app/data/ai-native-bank/faq/ccb-mobile-bank-faq.v1.json",
        )

        with patch.dict(os.environ, {"AI_BANK_FAQ_PATH": "/tmp/governed-faq.json"}, clear=True):
            overridden = RuntimeSettings.from_env()
        self.assertEqual(overridden.faq_path, "/tmp/governed-faq.json")


class FaqContainerPermissionTests(unittest.TestCase):
    def test_runtime_cannot_modify_root_owned_inputs_and_can_write_logs(self):
        self.assertNotEqual(os.geteuid(), 0)
        self.assertEqual(Path.cwd(), Path("/app"))
        for target in (Path("/app/app"), GRAPH_PATH, FAQ_PATH):
            with self.subTest(target=target):
                self.assertEqual(target.stat().st_uid, 0)
                self.assertFalse(os.access(target, os.W_OK))
        log_directory = Path("/app/logs")
        self.assertEqual(log_directory.stat().st_uid, os.geteuid())
        self.assertTrue(os.access(log_directory, os.W_OK))


if __name__ == "__main__":
    unittest.main()
