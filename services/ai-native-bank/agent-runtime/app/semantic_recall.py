"""Embedding-backed semantic recall widening for the intent graph.

对齐 AI Harbor 意图引擎的三层召回架构（词法/图谱召回 + 语义召回 + LLM 候选内
裁决）：确定性图谱召回仍是词法权威通道，本模块只在口语说法无法被子串召回时
扩充候选集。这里产出的候选只是可解释的召回证据，永远不作为最终意图裁决。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .intent_graph import IntentCandidate, IntentGraphRepository, IntentProjection

logger = logging.getLogger("ai_native_bank.agent_runtime.semantic_recall")

_EMBEDDING_TIMEOUT_SECONDS = 8.0
_EMBEDDING_BATCH_SIZE = 10
_EMBEDDING_CONCURRENCY = 6
_MAX_INDEX_TEXT_CHARS = 512
# 语义候选的分值区间必须低于词法精确别名（110）与权威捷径阈值（180）：
# 语义召回只扩候选，不改判意图。
_SEMANTIC_SCORES = (108, 104, 100, 96)
_MAX_SEMANTIC_CANDIDATES = len(_SEMANTIC_SCORES)
_DEFAULT_SIMILARITY_THRESHOLD = 0.55
# 构建期索引文件（scripts/build-intent-semantic-index.js 产出，随图谱一起 baked）。
# 必须携带 graphDigest/model/dimensions，运行时三重校验通过才加载。
_SEMANTIC_INDEX_FILENAME = "intent-semantic-index.v1.json"
# 治理面知识库检索模式（AI_BANK_SEMANTIC_RECALL_SOURCE=kb）：候选来自 OntoMind
# vector-kb search。KB 的 chunk 分值 = 1 - cosine 距离，与本地余弦同口径；
# chunk 榜会被同意图近义说法刷屏，必须按意图身份聚合后取 top（_rank_kb）。
# KB 文件名约定 intentId__title.txt（build-intent-semantic-index.js），意图身份在前缀。
_KB_SEARCH_TIMEOUT_SECONDS = 8.0
_DEFAULT_KB_TOP_K = 24
_KB_INTENT_FILENAME_SEP = "__"
_DEFAULT_KB_BASE_URL = "http://host.docker.internal:17386/ontomind/api"
_DEFAULT_KB_NAME = "意图语义索引-bank-ccb-mobile-bank-intents"
_DEFAULT_KB_EMBEDDING_MODEL = "text-embedding-v3"
_DEFAULT_KB_DIMENSIONS = 1024


@dataclass(frozen=True)
class SemanticRecallSettings:
    base_url: str
    api_key: str
    model: str
    similarity_threshold: float
    source: str = "baked"
    kb_base_url: str = _DEFAULT_KB_BASE_URL
    kb_name: str = _DEFAULT_KB_NAME
    kb_embedding_model: str = _DEFAULT_KB_EMBEDDING_MODEL
    kb_dimensions: int = _DEFAULT_KB_DIMENSIONS
    kb_top_k: int = _DEFAULT_KB_TOP_K

    @classmethod
    def from_env(cls) -> "SemanticRecallSettings":
        base_url = os.getenv("AI_BANK_MODEL_BASE_URL", "").strip().rstrip("/")
        api_key = os.getenv("AI_BANK_MODEL_API_KEY", "").strip()
        if not api_key:
            key_file = os.getenv("AI_BANK_MODEL_API_KEY_FILE", "").strip()
            if key_file:
                try:
                    with open(key_file, encoding="utf-8") as handle:
                        api_key = handle.read().strip()
                except OSError:
                    api_key = ""
        model = os.getenv("AI_BANK_EMBEDDING_MODEL", "text-embedding-v4").strip()
        try:
            threshold = float(
                os.getenv("AI_BANK_SEMANTIC_RECALL_THRESHOLD", str(_DEFAULT_SIMILARITY_THRESHOLD))
            )
        except ValueError:
            threshold = _DEFAULT_SIMILARITY_THRESHOLD
        if not 0 < threshold < 1:
            threshold = _DEFAULT_SIMILARITY_THRESHOLD
        source = os.getenv("AI_BANK_SEMANTIC_RECALL_SOURCE", "baked").strip().lower()
        if source not in {"baked", "kb"}:
            source = "baked"
        kb_base_url = os.getenv("AI_BANK_ONTO_BASE_URL", _DEFAULT_KB_BASE_URL).strip().rstrip("/")
        kb_name = os.getenv("AI_BANK_SEMANTIC_KB_NAME", _DEFAULT_KB_NAME).strip()
        kb_embedding_model = os.getenv(
            "AI_BANK_KB_EMBEDDING_MODEL", _DEFAULT_KB_EMBEDDING_MODEL
        ).strip()
        try:
            kb_dimensions = int(
                os.getenv("AI_BANK_KB_EMBEDDING_DIMENSIONS", str(_DEFAULT_KB_DIMENSIONS))
            )
        except ValueError:
            kb_dimensions = _DEFAULT_KB_DIMENSIONS
        try:
            kb_top_k = int(os.getenv("AI_BANK_KB_SEARCH_TOP_K", str(_DEFAULT_KB_TOP_K)))
        except ValueError:
            kb_top_k = _DEFAULT_KB_TOP_K
        kb_top_k = max(1, min(50, kb_top_k))
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            similarity_threshold=threshold,
            source=source,
            kb_base_url=kb_base_url,
            kb_name=kb_name,
            kb_embedding_model=kb_embedding_model,
            kb_dimensions=kb_dimensions,
            kb_top_k=kb_top_k,
        )

    @property
    def enabled(self) -> bool:
        baked_ready = bool(self.base_url and self.api_key and self.model)
        kb_ready = bool(self.kb_base_url and self.kb_name)
        return baked_ready or kb_ready


class SemanticRecallIndex:
    """Per-graph embedding index; built lazily so tests stay network-free."""

    def __init__(
        self,
        repository: IntentGraphRepository,
        settings: SemanticRecallSettings | None = None,
    ):
        self._repository = repository
        self._settings = settings or SemanticRecallSettings.from_env()
        self._lock = asyncio.Lock()
        self._built_digest: str | None = None
        self._vectors: list[tuple[str, list[float]]] = []
        self._degraded_logged: float = 0.0
        self._client: httpx.AsyncClient | None = None
        self._kb_id: str | None = None
        self._kb_digest_checked: str | None = None

    async def recall(self, message: str) -> list[IntentCandidate]:
        if not self._settings.enabled or not message.strip():
            return []
        if self._settings.source == "kb":
            try:
                return await self._recall_via_kb(message)
            except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError) as exc:
                # KB 不可用/陈旧时自动落回烤制索引，语义通道永不阻塞主链路。
                self._log_degraded(exc)
        return await self._recall_via_index(message)

    async def prewarm(self) -> None:
        """Best-effort index build at startup so the first weak-recall request
        does not pay the whole embedding cost; failures degrade silently."""
        if not self._settings.enabled:
            return
        try:
            if self._settings.source == "kb":
                await self._ensure_kb_ready()
            else:
                await self._ensure_index()
        except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError) as exc:
            self._log_degraded(exc)

    async def _recall_via_index(self, message: str) -> list[IntentCandidate]:
        try:
            vectors = await self._ensure_index()
            query_vector = await self._embed([message.strip()[:_MAX_INDEX_TEXT_CHARS]])
        except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError) as exc:
            self._log_degraded(exc)
            return []
        return self._rank(vectors, query_vector[0])

    async def _recall_via_kb(self, message: str) -> list[IntentCandidate]:
        kb_id = await self._ensure_kb_id()
        digest = self._repository.digest
        if self._kb_digest_checked != digest:
            await self._verify_kb_freshness(kb_id, digest)
        rows = await self._kb_search(kb_id, message.strip()[:_MAX_INDEX_TEXT_CHARS])
        return self._rank_kb(rows)

    async def _ensure_kb_ready(self) -> None:
        kb_id = await self._ensure_kb_id()
        if self._kb_digest_checked != self._repository.digest:
            await self._verify_kb_freshness(kb_id, self._repository.digest)

    async def _ensure_kb_id(self) -> str:
        if self._kb_id:
            return self._kb_id
        response = await self._kb_request("GET", "/v1/knowledge-bases?size=200")
        listed = (response.json() or {}).get("data") or []
        for item in listed:
            if isinstance(item, dict) and item.get("name") == self._settings.kb_name and item.get("id"):
                self._kb_id = str(item["id"])
                logger.info("Semantic recall KB resolved id=%s name=%s", self._kb_id, self._settings.kb_name)
                return self._kb_id
        raise ValueError(f"semantic KB not found: {self._settings.kb_name}")

    async def _verify_kb_freshness(self, kb_id: str, digest: str) -> None:
        """KB 与当前图谱 digest 必须同源（构建脚本写入 description），陈旧即回退。"""
        response = await self._kb_request("GET", f"/v1/knowledge-bases/{kb_id}")
        payload = response.json() or {}
        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        description = str((body or {}).get("description") or "")
        if f"digest={digest[:11]}" not in description:
            raise ValueError("semantic KB is stale for current graph digest")
        self._kb_digest_checked = digest

    async def _kb_search(self, kb_id: str, query: str) -> list:
        response = await self._kb_request(
            "POST",
            f"/v1/vector-kb/{kb_id}/search",
            headers={"x-usage-caller": "bank-semantic-recall"},
            json={
                "query": query,
                "top_k": self._settings.kb_top_k,
                "mode": "dense",
                "score_threshold": 0.0,
                "filters": {},
                "embedding": {
                    "model": self._settings.kb_embedding_model,
                    "dimensions": self._settings.kb_dimensions,
                },
                "include_images": False,
            },
        )
        results = (response.json() or {}).get("results")
        if not isinstance(results, list):
            raise ValueError("semantic KB search response shape is invalid")
        return results

    async def _kb_request(self, method: str, pathname: str, **kwargs):
        client = self._get_client()
        return await client.request(
            method,
            self._settings.kb_base_url + pathname,
            timeout=_KB_SEARCH_TIMEOUT_SECONDS,
            **kwargs,
        )

    def _rank_kb(self, rows: list) -> list[IntentCandidate]:
        # 消费口径：chunk 级命中按意图身份（文件名前缀）取 max——同意图近义说法
        # 会刷屏 chunk 榜，必须在意图维度聚合后再参与排序。
        known = {
            projection.intent_id for projection in self._repository.iter_active_intents()
        }
        best: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            metadata = row.get("metadata") or {}
            source = str(metadata.get("source") or "") if isinstance(metadata, dict) else ""
            intent_id = source.split(_KB_INTENT_FILENAME_SEP, 1)[0]
            if intent_id not in known:
                continue
            try:
                score = float(row.get("score"))
            except (TypeError, ValueError):
                continue
            if score < self._settings.similarity_threshold:
                continue
            current = best.get(intent_id)
            if current is None or score > current:
                best[intent_id] = score
        ranked = sorted(best.items(), key=lambda item: (-item[1], item[0]))
        candidates: list[IntentCandidate] = []
        for rank, (intent_id, _) in enumerate(ranked[:_MAX_SEMANTIC_CANDIDATES]):
            candidates.append(self._repository.candidate_for_intent(
                intent_id,
                score=_SEMANTIC_SCORES[min(rank, len(_SEMANTIC_SCORES) - 1)],
                evidence=["semantic_recall_kb"],
            ))
        return candidates

    def _rank(
        self, vectors: list[tuple[str, list[float]]], query: list[float]
    ) -> list[IntentCandidate]:
        # 多向量模型：一条意图有多条说法（title/aliases/description），查询与每条
        # 说法独立比对后按意图取 max——口语查询命中的是某一条具体说法，不是质心。
        best: dict[str, float] = {}
        for intent_id, vector in vectors:
            similarity = _cosine(query, vector)
            if similarity < self._settings.similarity_threshold:
                continue
            current = best.get(intent_id)
            if current is None or similarity > current:
                best[intent_id] = similarity
        hits = sorted(best.items(), key=lambda item: (-item[1], item[0]))
        candidates: list[IntentCandidate] = []
        for rank, (intent_id, _) in enumerate(hits[:_MAX_SEMANTIC_CANDIDATES]):
            candidates.append(self._repository.candidate_for_intent(
                intent_id,
                score=_SEMANTIC_SCORES[min(rank, len(_SEMANTIC_SCORES) - 1)],
                evidence=["semantic_recall"],
            ))
        return candidates

    async def _ensure_index(self) -> list[tuple[str, list[float]]]:
        digest = self._repository.digest
        if self._built_digest == digest and self._vectors:
            return self._vectors
        async with self._lock:
            if self._built_digest == digest and self._vectors:
                return self._vectors
            persisted = self._load_persisted_index(digest)
            if persisted is not None:
                self._vectors = persisted
                self._built_digest = digest
                logger.info(
                    "Semantic recall index loaded intents=%s entries=%s model=%s",
                    len({intent_id for intent_id, _ in persisted}),
                    len(persisted),
                    self._settings.model,
                )
                return self._vectors
            intents: list[str] = []
            texts: list[str] = []
            for intent in self._repository.iter_active_intents():
                for text in _intent_utterances(intent):
                    intents.append(intent.intent_id)
                    texts.append(text)
            vectors: list[list[float]] = []
            semaphore = asyncio.Semaphore(_EMBEDDING_CONCURRENCY)

            async def embed_batch(batch: list[str]) -> list[list[float]]:
                async with semaphore:
                    return await self._embed(batch)

            results = await asyncio.gather(*(
                embed_batch(texts[start:start + _EMBEDDING_BATCH_SIZE])
                for start in range(0, len(texts), _EMBEDDING_BATCH_SIZE)
            ))
            for batch_vectors in results:
                vectors.extend(batch_vectors)
            if len(vectors) != len(intents):
                raise ValueError("semantic recall index is incomplete")
            self._vectors = list(zip(intents, vectors, strict=True))
            self._built_digest = digest
            logger.info(
                "Semantic recall index built intents=%s entries=%s model=%s",
                len(set(intents)), len(intents), self._settings.model
            )
            return self._vectors

    def _index_path(self) -> Path:
        return self._repository.path.parent / _SEMANTIC_INDEX_FILENAME

    def _load_persisted_index(self, digest: str) -> list[tuple[str, list[float]]] | None:
        """加载构建期语义索引；缺失或与当前图谱/模型不一致时返回 None（在线重建兜底）。"""
        try:
            payload = json.loads(self._index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schemaVersion") != 1:
            return None
        if payload.get("graphDigest") != digest:
            return None
        if str(payload.get("model") or "") != self._settings.model:
            return None
        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            return None
        dimensions = payload.get("dimensions")
        known_ids = {
            projection.intent_id
            for projection in self._repository.iter_active_intents()
        }
        vectors: list[tuple[str, list[float]]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            intent_id = str(entry.get("intentId") or "")
            vector = entry.get("vector")
            if intent_id not in known_ids:
                return None
            if not isinstance(vector, list) or not vector:
                return None
            if dimensions is not None and len(vector) != dimensions:
                return None
            if not all(isinstance(value, (int, float)) for value in vector):
                return None
            vectors.append((intent_id, [float(value) for value in vector]))
        return vectors or None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_EMBEDDING_TIMEOUT_SECONDS)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        client = self._get_client()
        response = await client.post(
            self._settings.base_url + "/embeddings",
            headers={"Authorization": "Bearer " + self._settings.api_key},
            json={"model": self._settings.model, "input": texts},
        )
        if response.status_code != 200:
            raise ValueError(f"embedding upstream status {response.status_code}")
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("embedding response shape is invalid")
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in ordered]
        if any(not isinstance(vector, list) or not vector for vector in vectors):
            raise ValueError("embedding vector is invalid")
        return vectors

    def _log_degraded(self, error: Exception) -> None:
        now = time.monotonic()
        if self._degraded_logged and now - self._degraded_logged < 300:
            return
        self._degraded_logged = now
        logger.warning(
            "Semantic recall degraded; falling back to lexical recall only (%s)", error
        )


def _intent_utterances(intent: IntentProjection) -> list[str]:
    """一条意图的全部可召回说法：title + 全部 aliases + description。

    各条独立成向量（多向量召回）；recallTerms 属词法通道、negativeExamples 属
    排除语义，都不进正向索引。去重避免同文本重复占用向量。
    """
    texts: list[str] = []
    for candidate in [intent.title, *intent.aliases, intent.description]:
        text = str(candidate or "").strip()[:_MAX_INDEX_TEXT_CHARS]
        if text and text not in texts:
            texts.append(text)
    return texts


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0 or norm_right <= 0:
        return 0.0
    return dot / (norm_left ** 0.5 * norm_right ** 0.5) * 1.0
