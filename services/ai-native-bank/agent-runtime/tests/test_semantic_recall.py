from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.intent_graph import IntentGraphRepository
from app.semantic_recall import SemanticRecallIndex, SemanticRecallSettings, _intent_utterances


GRAPH_PATH = Path(
    os.getenv(
        "AI_BANK_INTENT_GRAPH_PATH",
        "/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v1.0.0.json",
    )
)

SETTINGS = SemanticRecallSettings(
    base_url="http://embedding.test",
    api_key="test-key",
    model="text-embedding-v4",
    similarity_threshold=0.55,
)

KB_SETTINGS = SemanticRecallSettings(
    base_url="http://embedding.test",
    api_key="test-key",
    model="text-embedding-v4",
    similarity_threshold=0.55,
    source="kb",
    kb_base_url="http://onto.test",
    kb_name="kb-test",
)

DIMENSIONS = 8


def _unit_vector(position: int) -> list[float]:
    vector = [0.0] * DIMENSIONS
    vector[position % DIMENSIONS] = 1.0
    return vector


class SemanticRecallMultiVectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = IntentGraphRepository.load(GRAPH_PATH)
        self.index = SemanticRecallIndex(self.repository, SETTINGS)
        self.intent_ids = [
            projection.intent_id for projection in self.repository.iter_active_intents()
        ]

    def test_utterances_cover_title_all_aliases_and_description(self):
        projections = {
            projection.intent_id: projection
            for projection in self.repository.iter_active_intents()
        }
        target = projections["ccb.cashflow.spending-explain"]
        utterances = _intent_utterances(target)
        self.assertIn(target.title, utterances)
        for alias in target.aliases:
            self.assertIn(alias, utterances)
        self.assertIn(target.description, utterances)
        self.assertEqual(len(utterances), len(set(utterances)))

    def test_rank_aggregates_per_intent_by_max_similarity(self):
        # 第一条意图的两条说法都过阈值，只保留更高分的一条参与排序；
        # 第二条意图过阈值但分值更低，排在其后。
        first, second = self.intent_ids[0], self.intent_ids[1]
        vectors = [
            (first, _unit_vector(0)),
            (first, [0.9, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
            (second, [0.6, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
        ]
        ranked = self.index._rank(vectors, _unit_vector(0))
        self.assertEqual([candidate.intent_id for candidate in ranked], [first, second])
        self.assertEqual(ranked[0].score, 108)
        self.assertEqual(ranked[1].score, 104)

    def test_rank_drops_intents_below_threshold(self):
        vectors = [(self.intent_ids[0], _unit_vector(2))]
        self.assertEqual(self.index._rank(vectors, _unit_vector(0)), [])


class SemanticRecallPersistedIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = IntentGraphRepository.load(GRAPH_PATH)
        self.index = SemanticRecallIndex(self.repository, SETTINGS)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.indexPath = Path(self.tmp.name) / "intent-semantic-index.v1.json"
        self.index._index_path = lambda: self.indexPath

    def _write_index(self, payload) -> None:
        self.indexPath.write_text(json.dumps(payload), encoding="utf-8")

    def _valid_payload(self) -> dict:
        projections = self.repository.iter_active_intents()[:2]
        return {
            "schemaVersion": 1,
            "graphId": self.repository.graph_id,
            "graphDigest": self.repository.digest,
            "model": SETTINGS.model,
            "dimensions": DIMENSIONS,
            "createdAt": "2026-09-22T00:00:00Z",
            "entries": [
                {
                    "intentId": projection.intent_id,
                    "kind": "title",
                    "text": projection.title,
                    "vector": _unit_vector(position),
                }
                for position, projection in enumerate(projections)
            ],
        }

    def test_loads_persisted_index_when_digest_and_model_match(self):
        self._write_index(self._valid_payload())
        loaded = self.index._load_persisted_index(self.repository.digest)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded), 2)

    def test_ensure_index_prefers_persisted_over_online_build(self):
        self._write_index(self._valid_payload())

        async def fail_embed(texts):
            raise AssertionError("online embedding must not run when persisted index loads")

        self.index._embed = fail_embed
        import asyncio

        vectors = asyncio.run(self.index._ensure_index())
        self.assertEqual(len(vectors), 2)
        self.assertEqual(self.index._built_digest, self.repository.digest)

    def test_rejects_persisted_index_on_digest_mismatch(self):
        payload = self._valid_payload()
        payload["graphDigest"] = "sha256:" + "0" * 64
        self._write_index(payload)
        self.assertIsNone(self.index._load_persisted_index(self.repository.digest))

    def test_rejects_persisted_index_on_model_mismatch(self):
        payload = self._valid_payload()
        payload["model"] = "text-embedding-v3"
        self._write_index(payload)
        self.assertIsNone(self.index._load_persisted_index(self.repository.digest))

    def test_rejects_persisted_index_with_unknown_intent_id(self):
        payload = self._valid_payload()
        payload["entries"][0]["intentId"] = "intent.missing"
        self._write_index(payload)
        self.assertIsNone(self.index._load_persisted_index(self.repository.digest))

    def test_missing_index_file_returns_none(self):
        self.assertIsNone(self.index._load_persisted_index(self.repository.digest))


def _kb_row(intent_id: str, score: float, title: str = "t") -> dict:
    return {"score": score, "metadata": {"source": f"{intent_id}__{title}.txt"}}


class SemanticRecallKbRankTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = IntentGraphRepository.load(GRAPH_PATH)
        self.index = SemanticRecallIndex(self.repository, KB_SETTINGS)
        self.intent_ids = [
            projection.intent_id for projection in self.repository.iter_active_intents()
        ]

    def test_rank_kb_aggregates_chunks_per_intent_by_max(self):
        # 同意图多条 chunk 命中只保留最高分；chunk 榜顺序不等于意图榜顺序。
        first, second = self.intent_ids[0], self.intent_ids[1]
        rows = [
            _kb_row(first, 0.62),
            _kb_row(first, 0.81, title="alias"),
            _kb_row(second, 0.70),
        ]
        ranked = self.index._rank_kb(rows)
        self.assertEqual([candidate.intent_id for candidate in ranked], [first, second])
        self.assertEqual(ranked[0].score, 108)
        self.assertEqual(ranked[1].score, 104)
        self.assertEqual(ranked[0].evidence, ["semantic_recall_kb"])

    def test_rank_kb_drops_below_threshold_unknown_intents_and_malformed_rows(self):
        rows = [
            _kb_row(self.intent_ids[0], 0.30),
            _kb_row("intent.missing", 0.95),
            "garbage",
            {"score": "oops", "metadata": {"source": f"{self.intent_ids[0]}__t.txt"}},
        ]
        self.assertEqual(self.index._rank_kb(rows), [])

    def test_rank_kb_caps_candidates_at_semantic_scores(self):
        rows = [
            _kb_row(intent_id, 0.60 + position * 0.01)
            for position, intent_id in enumerate(self.intent_ids[:6])
        ]
        ranked = self.index._rank_kb(rows)
        self.assertEqual(len(ranked), 4)
        self.assertEqual([candidate.score for candidate in ranked], [108, 104, 100, 96])


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class SemanticRecallKbDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = IntentGraphRepository.load(GRAPH_PATH)
        self.index = SemanticRecallIndex(self.repository, KB_SETTINGS)

    def test_kb_failure_falls_back_to_baked_index(self):
        import asyncio

        async def fail_kb(message):
            raise ValueError("kb stale")

        async def baked(message):
            return ["sentinel"]

        self.index._recall_via_kb = fail_kb
        self.index._recall_via_index = baked
        self.assertEqual(asyncio.run(self.index.recall("查余额")), ["sentinel"])

    def test_verify_kb_freshness_accepts_matching_digest_marker(self):
        import asyncio

        digest = self.repository.digest
        description = f"意图图谱语义召回索引（治理面镜像） | digest={digest[:11]}… | 逐说法"

        async def fake_request(method, pathname, **kwargs):
            return _FakeResponse({"data": {"description": description}})

        self.index._kb_request = fake_request
        asyncio.run(self.index._verify_kb_freshness("kb-1", digest))
        self.assertEqual(self.index._kb_digest_checked, digest)

    def test_verify_kb_freshness_rejects_stale_digest(self):
        import asyncio

        async def fake_request(method, pathname, **kwargs):
            return _FakeResponse({"data": {"description": "digest=sha256:00000000000 stale"}})

        self.index._kb_request = fake_request
        with self.assertRaises(ValueError):
            asyncio.run(self.index._verify_kb_freshness("kb-1", self.repository.digest))
        self.assertIsNone(self.index._kb_digest_checked)


if __name__ == "__main__":
    unittest.main()
