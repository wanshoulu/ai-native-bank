#!/usr/bin/env python
"""
V4 语义召回模块：用 Qwen text-embedding-v4 对意图的全部可检索文本
构建向量索引，查询时通过余弦相似度匹配，返回语义候选。

设计要点：
- 独立于 V4GraphAdapter，不修改词法召回逻辑
- 语义候选分值 (78,75,72,69)，低于 alias exact (80)，与 recallTerms (65) 区间重叠
- 语义候选 evidence 标记为 "semantic_recall"，与词法候选区分
- 增强多向量索引：title + aliases + description + recallTerms + recallTermSets
  + disambiguationHints 各自独立成向量，查询与每条独立比对后按意图取 max
- 索引按 graph digest + 索引版本 缓存，变化时自动重建
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("ai_native_bank.agent_runtime.semantic_recall_v4")

# ── 常量 ──
_EMBEDDING_TIMEOUT = 30.0  # 批量构建索引时需要更长超时
_EMBEDDING_BATCH_SIZE = 10  # Qwen embedding API 限制单次最多 10 条
_EMBEDDING_MAX_RETRIES = 3
_EMBEDDING_RETRY_DELAY = 2.0
_MAX_TEXT_CHARS = 512
_DEFAULT_SIMILARITY_THRESHOLD = 0.45  # 降低门槛：0.65太严格，部分意图相似度低于阈值被丢弃
_EMBEDDING_DIMENSIONS = 1024

# 索引版本：改变 _intent_utterances 内容时递增，使旧缓存失效
_INDEX_VERSION = 2

# 语义候选分值：与关键词召回统一在 60-80 区间，不再被压制
_SEMANTIC_SCORES = (78, 75, 72, 69)
_MAX_SEMANTIC_CANDIDATES = len(_SEMANTIC_SCORES)

# 默认 base_url（Qwen DashScope compatible mode）
_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DEFAULT_MODEL = "text-embedding-v4"


def _load_qwen_key() -> str:
    """加载 embedding API 密钥。

    优先使用模型网关凭证（AI_BANK_MODEL_API_KEY / AI_BANK_MODEL_API_KEY_FILE），
    因为生产环境通过 ai-harbor 模型网关调用 embedding，网关期望 runtime token；
    本地直连 Qwen API 时回退到 QWEN_KEY。
    """
    # 1) 模型网关凭证（生产 / Docker 环境）
    key = os.getenv("AI_BANK_MODEL_API_KEY", "").strip()
    if key:
        return key
    key_file = os.getenv("AI_BANK_MODEL_API_KEY_FILE", "").strip()
    if key_file:
        try:
            with open(key_file, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            pass
    # 2) 本地直连 Qwen API：QWEN_KEY 环境变量
    key = os.getenv("QWEN_KEY", "")
    if key:
        return key
    # 3) 本地开发：从 .env 文件加载 QWEN_KEY
    candidates = [
        Path.cwd() / ".env",
        Path.cwd().parent / ".env",
        Path.cwd().parent.parent / ".env",
    ]
    _local_env = Path(__file__).resolve().parents[4] / ".env" if len(Path(__file__).resolve().parents) > 4 else None
    if _local_env is not None:
        candidates.append(_local_env)
    for p in candidates:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("QWEN_KEY=") and not line.startswith("#"):
                    return line[len("QWEN_KEY="):]
    return ""


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def _intent_utterances(intent: dict) -> list[str]:
    """提取意图的全部可索引说法（增强多向量方案C）：
    title + aliases + description + recallTerms + recallTermSets + disambiguationHints
    
    每条独立成向量，查询与每条独立比对后按意图取 max。
    比 chunk 方案更精确：短查询不会被长文本稀释。
    """
    texts: list[str] = []
    # title (userGoal)
    title = str(intent.get("title", "") or "").strip()
    if title:
        texts.append(title[:_MAX_TEXT_CHARS])
    # aliases
    for alias in intent.get("aliases", []):
        t = str(alias or "").strip()
        if t and t not in texts:
            texts.append(t[:_MAX_TEXT_CHARS])
    # description
    desc = str(intent.get("description", "") or "").strip()
    if desc and desc not in texts:
        texts.append(desc[:_MAX_TEXT_CHARS])
    # recallTerms — 用户实际搜索时使用的关键词
    for term in intent.get("recallTerms", []):
        t = str(term or "").strip()
        if t and t not in texts:
            texts.append(t[:_MAX_TEXT_CHARS])
    # recallTermSets — 词组（集合型召回词）
    for term_set in intent.get("recallTermSets", []):
        if isinstance(term_set, list):
            t = " ".join(str(ts) for ts in term_set if ts).strip()
        else:
            t = str(term_set or "").strip()
        if t and t not in texts:
            texts.append(t[:_MAX_TEXT_CHARS])
    # disambiguationHints — 消歧线索，包含区分意图的关键信息
    disambig = intent.get("disambiguationHints")
    if isinstance(disambig, list):
        for hint in disambig:
            t = str(hint or "").strip()
            if t and t not in texts:
                texts.append(t[:_MAX_TEXT_CHARS])
    elif disambig:
        t = str(disambig).strip()
        if t and t not in texts:
            texts.append(t[:_MAX_TEXT_CHARS])
    return texts


class SemanticRecallV4:
    """V4 图谱的 embedding 语义召回"""

    def __init__(self, adapter: Any, tugraph_config: dict | None = None, *, recall_settings=None):
        self._adapter = adapter
        self._api_key = _load_qwen_key()
        self._base_url = os.getenv("AI_BANK_MODEL_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
        self._model = os.getenv("AI_BANK_EMBEDDING_MODEL", _DEFAULT_MODEL)
        # 从配置覆盖模块级常量
        if recall_settings is not None:
            global _DEFAULT_SIMILARITY_THRESHOLD, _SEMANTIC_SCORES, _MAX_SEMANTIC_CANDIDATES
            _DEFAULT_SIMILARITY_THRESHOLD = recall_settings.semantic_similarity_threshold
            scores = tuple(
                recall_settings.semantic_score_top - i * recall_settings.semantic_score_step
                for i in range(recall_settings.semantic_max_candidates)
            )
            _SEMANTIC_SCORES = scores
            _MAX_SEMANTIC_CANDIDATES = recall_settings.semantic_max_candidates
        self._threshold = _DEFAULT_SIMILARITY_THRESHOLD
        self._client: httpx.Client | None = None

        # 索引缓存
        self._built_digest: str | None = None
        self._vectors: list[tuple[str, list[float]]] = []  # (intent_id, vector)

        # 缓存索引文件路径
        self._index_path = adapter.path.parent / "v4-semantic-index.json"

        # TuGraph KNN 向量检索配置
        self._tugraph_config = tugraph_config
        self._tugraph_enabled = bool(
            tugraph_config and tugraph_config.get("base_url") and tugraph_config.get("password")
        )
        self._tugraph_token: str | None = None
        self._tugraph_token_time: float = 0.0
        self._tugraph_client: httpx.Client | None = None

        # V4 node ID → intent 映射（TuGraph KNN 返回的是 V4 node ID，不是 intent ID）
        self._intents_by_v4_node_id: dict[str, Any] = {}
        for _intent in adapter._intents:
            _v4_id = _intent.get("_v4_node_id", "")
            if _v4_id:
                self._intents_by_v4_node_id[_v4_id] = _intent

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(timeout=_EMBEDDING_TIMEOUT)
        return self._client

    # ── TuGraph KNN 向量检索 ──

    def _get_tugraph_client(self) -> httpx.Client:
        if self._tugraph_client is None or self._tugraph_client.is_closed:
            self._tugraph_client = httpx.Client(timeout=30.0)
        return self._tugraph_client

    def _tugraph_get_token(self) -> str:
        """获取 TuGraph JWT token，缓存复用（1小时有效）"""
        if self._tugraph_token and (time.time() - self._tugraph_token_time < 3600):
            return self._tugraph_token
        cfg = self._tugraph_config
        client = self._get_tugraph_client()
        resp = client.post(
            f"{cfg['base_url']}/login",
            json={"user": cfg.get("user", "admin"), "password": cfg["password"]},
        )
        if resp.status_code != 200:
            raise ValueError(f"TuGraph login failed: {resp.status_code}: {resp.text[:200]}")
        token = resp.json().get("jwt")
        if not token:
            raise ValueError("TuGraph login returned no JWT")
        self._tugraph_token = token
        self._tugraph_token_time = time.time()
        logger.info("TuGraph JWT token acquired for semantic recall")
        return token

    def _tugraph_knn_search(
        self, query_vec: list[float], limit: int = 10
    ) -> list[tuple[str, float]]:
        """TuGraph HNSW KNN 搜索，返回 [(intent_id, similarity), ...]

        TuGraph 索引使用 L2 距离，不能直接用 1-distance 作为 cosine similarity。
        在 Cypher 中同时返回 node.embedding，本地计算真实 cosine 相似度。
        取比 limit 更多的候选（candidate_limit），重排后截取 limit。
        """
        import math

        cfg = self._tugraph_config
        token = self._tugraph_get_token()
        client = self._get_tugraph_client()
        vec_literal = "[" + ",".join(str(v) for v in query_vec) + "]"
        candidate_limit = max(limit * 3, 20)  # 多取候选，重排后截取
        cypher = (
            f"CALL db.vertexVectorKnnSearch('IntentNode', 'embedding', {vec_literal}, "
            f"{{limit: {candidate_limit}}}) YIELD node, distance "
            f"RETURN node.id AS node_id, distance, node.embedding AS embedding"
        )
        resp = client.post(
            f"{cfg['base_url']}/cypher",
            json={
                "script": cypher,
                "graph": cfg.get("database", "default"),
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            raise ValueError(
                f"TuGraph KNN search failed: {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        rows = data.get("result", data.get("data", []))

        # 预计算 query_vec 的范数
        query_norm = math.sqrt(sum(v * v for v in query_vec))

        results: list[tuple[str, float]] = []
        for row in rows:
            if isinstance(row, list) and len(row) >= 3:
                node_id, distance, embedding = row[0], row[1], row[2]
            elif isinstance(row, dict):
                node_id = row.get("node_id")
                distance = row.get("distance")
                embedding = row.get("embedding")
            else:
                continue
            if not node_id or embedding is None:
                continue
            # 本地计算真实 cosine 相似度
            stored_norm = math.sqrt(sum(v * v for v in embedding))
            if stored_norm == 0 or query_norm == 0:
                continue
            dot = sum(a * b for a, b in zip(query_vec, embedding))
            cosine_sim = dot / (query_norm * stored_norm)
            results.append((str(node_id), cosine_sim))

        # 按 cosine 相似度降序排序，截取 limit
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """调用 Qwen embedding API（含重试和进度日志）"""
        all_vectors: list[list[float]] = []
        total_batches = (len(texts) + _EMBEDDING_BATCH_SIZE - 1) // _EMBEDDING_BATCH_SIZE
        for i in range(0, len(texts), _EMBEDDING_BATCH_SIZE):
            batch_idx = i // _EMBEDDING_BATCH_SIZE
            batch = texts[i:i + _EMBEDDING_BATCH_SIZE]
            vectors = self._embed_batch_with_retry(batch, batch_idx, total_batches)
            all_vectors.extend(vectors)
        return all_vectors

    def _embed_batch_with_retry(
        self, batch: list[str], batch_idx: int, total_batches: int
    ) -> list[list[float]]:
        """单批 embedding 调用，含重试（重建 client 避免连接池过期）"""
        last_exc: Exception | None = None
        for attempt in range(_EMBEDDING_MAX_RETRIES + 1):
            try:
                if attempt > 0:
                    # 重建 client 避免连接池中过期连接
                    if self._client is not None and not self._client.is_closed:
                        self._client.close()
                    self._client = None
                    time.sleep(_EMBEDDING_RETRY_DELAY * attempt)
                    logger.info(
                        "Embedding batch %d/%d retry %d/%d",
                        batch_idx + 1, total_batches, attempt, _EMBEDDING_MAX_RETRIES,
                    )
                client = self._get_client()
                resp = client.post(
                    self._base_url + "/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "input": batch,
                        "dimensions": _EMBEDDING_DIMENSIONS,
                    },
                )
                if resp.status_code != 200:
                    raise ValueError(f"embedding API status {resp.status_code}: {resp.text[:200]}")
                data = resp.json().get("data", [])
                if len(data) != len(batch):
                    raise ValueError(f"embedding count mismatch: {len(data)} != {len(batch)}")
                # 按 index 排序
                data.sort(key=lambda item: item.get("index", 0))
                vectors: list[list[float]] = []
                for item in data:
                    vec = item.get("embedding")
                    if not isinstance(vec, list) or not vec:
                        raise ValueError("embedding vector is invalid")
                    vectors.append(vec)
                if total_batches > 50 and (batch_idx + 1) % 50 == 0:
                    logger.info("Embedding progress: %d/%d batches", batch_idx + 1, total_batches)
                return vectors
            except Exception as exc:
                last_exc = exc
                if attempt < _EMBEDDING_MAX_RETRIES:
                    logger.warning(
                        "Embedding batch %d/%d failed (attempt %d): %s",
                        batch_idx + 1, total_batches, attempt + 1, exc,
                    )
                else:
                    logger.error(
                        "Embedding batch %d/%d failed after %d retries: %s",
                        batch_idx + 1, total_batches, _EMBEDDING_MAX_RETRIES, exc,
                    )
        assert last_exc is not None
        raise last_exc

    def _build_index(self) -> None:
        """构建全意图 embedding 索引"""
        digest = self._adapter.digest
        if self._built_digest == digest and self._vectors:
            return

        # 尝试加载缓存索引
        cached = self._load_cached_index(digest)
        if cached is not None:
            self._vectors = cached
            self._built_digest = digest
            logger.info("V4 semantic index loaded from cache: %d entries", len(cached))
            return

        # 在线构建：允许 active 和 planned 节点（与 graph_expand 一致）
        intent_texts: list[tuple[str, str]] = []  # (intent_id, text)
        for intent in self._adapter._intents:
            lifecycle = intent.get("lifecycle", "active")
            if lifecycle in ("deprecated", "retired"):
                continue
            for text in _intent_utterances(intent):
                intent_texts.append((intent["id"], text))

        if not intent_texts:
            self._vectors = []
            self._built_digest = digest
            return

        print(f"  [semantic] 构建 embedding 索引: {len(intent_texts)} 条说法...", flush=True)
        texts = [t for _, t in intent_texts]
        vectors = self._embed(texts)

        self._vectors = list(zip(
            [iid for iid, _ in intent_texts],
            vectors,
            strict=True,
        ))
        self._built_digest = digest
        logger.info(
            "V4 semantic index built: %d intents, %d entries, model=%s",
            len({iid for iid, _ in self._vectors}),
            len(self._vectors),
            self._model,
        )

        # 保存缓存
        self._save_cached_index(digest)

    def _load_cached_index(self, digest: str) -> list[tuple[str, list[float]]] | None:
        """加载缓存的 embedding 索引"""
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("graphDigest") != digest:
            return None
        if payload.get("model") != self._model:
            return None
        if payload.get("indexVersion") != _INDEX_VERSION:
            return None  # 索引版本不匹配，需重建
        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            return None
        vectors: list[tuple[str, list[float]]] = []
        for entry in entries:
            intent_id = entry.get("intentId", "")
            vec = entry.get("vector")
            if not intent_id or not isinstance(vec, list) or not vec:
                continue
            vectors.append((intent_id, [float(v) for v in vec]))
        return vectors if vectors else None

    def _save_cached_index(self, digest: str) -> None:
        """保存 embedding 索引到磁盘缓存"""
        try:
            payload = {
                "schemaVersion": 1,
                "graphDigest": digest,
                "model": self._model,
                "dimensions": _EMBEDDING_DIMENSIONS,
                "indexVersion": _INDEX_VERSION,
                "entries": [
                    {"intentId": iid, "vector": vec}
                    for iid, vec in self._vectors
                ],
            }
            self._index_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("V4 semantic index cached to %s (%d entries)", self._index_path, len(self._vectors))
        except OSError as exc:
            logger.warning("V4 semantic index cache save failed: %s (path=%s)", exc, self._index_path)

    def prewarm(self) -> None:
        """预热：始终构建内存索引（fallback 路径必需），TuGraph 模式额外缓存 token"""
        if self._tugraph_enabled:
            try:
                self._tugraph_get_token()
                logger.info("TuGraph semantic recall prewarmed (token cached)")
            except Exception as exc:
                logger.warning("TuGraph prewarm failed: %s", exc)
        # 无论是否启用 TuGraph，都构建内存索引作为 fallback
        try:
            self._build_index()
            logger.info("V4 semantic in-memory index prewarmed: %d entries", len(self._vectors))
        except Exception as exc:
            logger.warning("V4 semantic prewarm index build failed: %s", exc)

    @staticmethod
    def _candidate_trace(candidate, similarity: float) -> dict:
        return {
            "intentId": candidate.intent_id, "title": candidate.title,
            "action": candidate.action, "businessObject": candidate.business_object,
            "score": candidate.score, "similarity": round(similarity, 6),
            "evidence": list(candidate.evidence),
        }

    def recall(self, message: str, exclude_ids: set[str] | None = None, *, diagnostics: dict | None = None) -> list:
        """
        语义召回：返回 top-k 语义候选 IntentCandidate。
        exclude_ids 中的意图不会被返回（已被词法召回覆盖）。
        """
        trace = diagnostics if diagnostics is not None else {}
        trace.update(status="completed", candidates=[], contributedCount=0, model=self._model,
                     reason="向量检索已执行；展示去重前本路候选，新增数量按实际进入融合的候选计算。")
        if not self.enabled:
            trace.update(status="disabled", reason="向量服务未配置，本次未执行。")
            return []
        if not message.strip():
            trace.update(status="skipped", reason="输入为空，本次未执行。")
            return []

        # 确保内存索引已构建（prewarm 已构建则直接命中缓存）
        try:
            self._build_index()
        except Exception as exc:
            logger.warning("V4 semantic index build failed: %s", exc)
            trace.update(status="error", errorCode="index_build_failed", reason="向量索引准备失败。")
            return []

        if not self._vectors:
            trace.update(status="unavailable", reason="向量索引为空，无法执行检索。")
            return []

        # 查询 embedding（一次性计算，TuGraph KNN 和内存余弦共用）
        try:
            query_vec = self._embed([message.strip()[:_MAX_TEXT_CHARS]])[0]
        except Exception as exc:
            logger.warning("V4 semantic query embedding failed: %s", exc)
            trace.update(status="error", errorCode="query_embedding_failed", reason="查询向量生成失败，请检查向量服务。")
            return []

        # TuGraph KNN 向量检索路径（主路径）
        # TuGraph 存储的是意图平均向量，部分意图可能因平均稀释而召回不到
        # 此时回退到内存多向量匹配（每条 utterance 独立比对取 max，精度更高）
        if self._tugraph_enabled:
            results = self._tugraph_recall_with_vec(query_vec, exclude_ids, diagnostics=trace)
            if results:
                trace.update(contributedCount=len(results), backend="tugraph_knn")
                return results
            logger.debug("TuGraph KNN returned empty, falling back to in-memory cosine")

        # 内存余弦相似度路径（fallback / 非 TuGraph 模式）
        best: dict[str, float] = {}
        for intent_id, vec in self._vectors:
            sim = _cosine(query_vec, vec)
            if sim < self._threshold:
                continue
            current = best.get(intent_id)
            if current is None or sim > current:
                best[intent_id] = sim

        # 排序
        ranked = sorted(best.items(), key=lambda item: (-item[1], item[0]))
        exclude = exclude_ids or set()

        # 构建候选
        candidates = []
        raw_candidates = []
        for rank, (intent_id, sim) in enumerate(ranked):
            if rank >= _MAX_SEMANTIC_CANDIDATES:
                break
            intent = self._adapter._intents_by_id.get(intent_id)
            if intent is None:
                continue
            score = _SEMANTIC_SCORES[min(rank, len(_SEMANTIC_SCORES) - 1)]
            candidate = self._adapter._build_candidate(
                intent,
                score=score,
                evidence=["semantic_recall"],
            )
            raw_candidates.append(self._candidate_trace(candidate, sim))
            if intent_id in exclude:
                continue
            # 附加相似度到 evidence（调试用）
            candidates.append(candidate)

        trace.update(status="completed", candidates=raw_candidates, contributedCount=len(candidates), backend="in_memory_cosine")
        return candidates

    def _tugraph_recall_with_vec(self, query_vec: list[float], exclude_ids: set[str] | None = None, *, diagnostics: dict | None = None) -> list:
        """TuGraph KNN 向量检索召回：用 TuGraph HNSW 索引替代内存余弦相似度"""
        # TuGraph KNN 搜索（多取一些，用阈值和 exclude 过滤）
        try:
            knn_results = self._tugraph_knn_search(query_vec, limit=10)
        except Exception as exc:
            logger.warning("TuGraph KNN search failed: %s", exc)
            if diagnostics is not None:
                diagnostics["fallbackReason"] = "tugraph_knn_failed"
            return []

        if not knn_results:
            return []

        # 按相似度降序排序
        ranked = sorted(knn_results, key=lambda item: (-item[1], item[0]))
        exclude = exclude_ids or set()

        # 构建候选（与内存路径完全一致的打分逻辑）
        candidates = []
        raw_candidates = []
        for rank, (v4_node_id, sim) in enumerate(ranked):
            if sim < self._threshold:
                continue
            if rank >= _MAX_SEMANTIC_CANDIDATES:
                break
            intent = self._intents_by_v4_node_id.get(v4_node_id)
            if intent is None:
                # 回退：也许 KNN 返回的就是 intent ID
                intent = self._adapter._intents_by_id.get(v4_node_id)
            if intent is None:
                continue
            score = _SEMANTIC_SCORES[min(rank, len(_SEMANTIC_SCORES) - 1)]
            candidate = self._adapter._build_candidate(
                intent,
                score=score,
                evidence=["semantic_recall"],
            )
            raw_candidates.append(self._candidate_trace(candidate, sim))
            if v4_node_id in exclude:
                continue
            candidates.append(candidate)

        logger.debug(
            "TuGraph KNN recall: %d results, %d candidates after filter",
            len(knn_results),
            len(candidates),
        )
        if diagnostics is not None:
            diagnostics.update(candidates=raw_candidates, contributedCount=len(candidates))
        return candidates

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()
            self._client = None
        if self._tugraph_client is not None and not self._tugraph_client.is_closed:
            self._tugraph_client.close()
            self._tugraph_client = None
