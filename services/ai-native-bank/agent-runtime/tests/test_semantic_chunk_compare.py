#!/usr/bin/env python
"""
语义索引方案对比：多向量 vs chunk vs 增强多向量

对比三种 embedding 索引设计：
  A. 当前方案：title + aliases + description 各自独立成向量，per intent 取 max
  B. chunk方案：name + desc + aliases + recallTerms + recallTermSets 合并为一条 chunk
  C. 增强多向量：所有内容（含 recallTerms）各自独立成向量，per intent 取 max

核心指标：
  - 语义召回命中率（词法失败时语义能否复活）
  - 期望意图在 top-k 中的排名
  - 相似度分数分布
"""

from __future__ import annotations

import json
import os

import numpy as np
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent  # agent-runtime/
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter  # noqa: E402

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

# ── Embedding 配置 ──
_EMBEDDING_TIMEOUT = 30.0
_EMBEDDING_BATCH_SIZE = 10
_EMBEDDING_DIMENSIONS = 1024
_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DEFAULT_MODEL = "text-embedding-v4"
_THRESHOLD = 0.55  # 降低门槛观察分布，不截断


def _load_qwen_key() -> str:
    key = os.getenv("QWEN_KEY", "")
    if key:
        return key
    candidates = [
        Path.cwd() / ".env",
        Path.cwd().parent / ".env",
        Path.cwd().parent.parent / ".env",
        ROOT.parent.parent.parent / ".env",
    ]
    for p in candidates:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("QWEN_KEY=") and not line.startswith("#"):
                    return line[len("QWEN_KEY="):]
    return ""


def _normalize_matrix(mat: np.ndarray) -> np.ndarray:
    """L2 归一化矩阵的每一行"""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _build_sim_matrix(query_vecs: np.ndarray, index_vecs: np.ndarray) -> np.ndarray:
    """计算 query × index 的余弦相似度矩阵（已归一化）"""
    return query_vecs @ index_vecs.T


def _embed_batch(client: httpx.Client, api_key: str, base_url: str, texts: list[str]) -> list[list[float]]:
    """分批调用 embedding API"""
    all_vectors: list[list[float]] = []
    for i in range(0, len(texts), _EMBEDDING_BATCH_SIZE):
        batch = texts[i:i + _EMBEDDING_BATCH_SIZE]
        resp = client.post(
            base_url + "/embeddings",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _DEFAULT_MODEL,
                "input": batch,
                "dimensions": _EMBEDDING_DIMENSIONS,
            },
        )
        if resp.status_code != 200:
            raise ValueError(f"embedding API {resp.status_code}: {resp.text[:200]}")
        data = resp.json().get("data", [])
        data.sort(key=lambda item: item.get("index", 0))
        for item in data:
            vec = item.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise ValueError("invalid embedding")
            all_vectors.append(vec)
    return all_vectors


# ── 方案A：当前多向量（title + aliases + description） ──

def build_index_a(intents: list[dict]) -> list[tuple[str, str]]:
    """返回 (intent_id, text) 列表，每个 text 是一条独立说法"""
    entries: list[tuple[str, str]] = []
    for intent in intents:
        iid = intent["id"]
        title = str(intent.get("title", "") or "").strip()
        if title:
            entries.append((iid, title[:512]))
        for alias in intent.get("aliases", []):
            t = str(alias or "").strip()
            if t:
                entries.append((iid, t[:512]))
        desc = str(intent.get("description", "") or "").strip()
        if desc:
            entries.append((iid, desc[:512]))
    return entries


# ── 方案B：chunk（name + desc + aliases + recallTerms + recallTermSets 合并） ──

def build_index_b(intents: list[dict]) -> list[tuple[str, str]]:
    """返回 (intent_id, chunk_text) 列表，每条意图一条 chunk"""
    entries: list[tuple[str, str]] = []
    for intent in intents:
        iid = intent["id"]
        parts: list[str] = []
        title = str(intent.get("title", "") or "").strip()
        if title:
            parts.append(f"意图名称: {title}")
        desc = str(intent.get("description", "") or "").strip()
        if desc:
            parts.append(f"描述: {desc}")
        aliases = intent.get("aliases", [])
        if aliases:
            parts.append("别称: " + "、".join(str(a) for a in aliases if a))
        recall_terms = intent.get("recallTerms", [])
        if recall_terms:
            parts.append("召回关键词: " + " ".join(str(t) for t in recall_terms if t))
        recall_sets = intent.get("recallTermSets", [])
        if recall_sets:
            parts.append("召回词组: " + " ".join(str(s) for s in recall_sets if s))
        disamb = intent.get("disambiguationHints", "")
        if disamb:
            parts.append(f"消歧提示: {disamb}")
        chunk = "\n".join(parts)
        if chunk.strip():
            entries.append((iid, chunk[:1024]))
    return entries


# ── 方案C：增强多向量（title + aliases + description + recallTerms + recallTermSets） ──

def build_index_c(intents: list[dict]) -> list[tuple[str, str]]:
    """返回 (intent_id, text) 列表，所有内容各自独立成向量"""
    entries: list[tuple[str, str]] = []
    for intent in intents:
        iid = intent["id"]
        title = str(intent.get("title", "") or "").strip()
        if title:
            entries.append((iid, title[:512]))
        for alias in intent.get("aliases", []):
            t = str(alias or "").strip()
            if t:
                entries.append((iid, t[:512]))
        desc = str(intent.get("description", "") or "").strip()
        if desc:
            entries.append((iid, desc[:512]))
        for term in intent.get("recallTerms", []):
            t = str(term or "").strip()
            if t:
                entries.append((iid, t[:512]))
        for term_set in intent.get("recallTermSets", []):
            t = str(term_set or "").strip()
            if t:
                entries.append((iid, t[:512]))
        disamb = str(intent.get("disambiguationHints", "") or "").strip()
        if disamb:
            entries.append((iid, disamb[:512]))
    return entries


def run_comparison():
    api_key = _load_qwen_key()
    if not api_key:
        print("ERROR: QWEN_KEY 未配置")
        return

    # 加载图谱
    adapter = V4GraphAdapter.load(GRAPH_PATH)
    intents = [
        i for i in adapter._intents
        if i.get("lifecycle", "active") not in ("deprecated", "retired")
    ]
    print(f"活跃意图数: {len(intents)}")

    # 加载测试集
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    test_cases = dataset.get("cases", dataset) if isinstance(dataset, dict) else dataset
    print(f"测试用例数: {len(test_cases)}")

    # 构建三种索引
    index_a = build_index_a(intents)
    index_b = build_index_b(intents)
    index_c = build_index_c(intents)

    print(f"\n索引条目数:")
    print(f"  方案A（当前多向量）: {len(index_a)} 条")
    print(f"  方案B（chunk）:      {len(index_b)} 条")
    print(f"  方案C（增强多向量）: {len(index_c)} 条")

    # 检查 recallTerms 覆盖率
    has_recall_terms = sum(1 for i in intents if i.get("recallTerms"))
    has_recall_sets = sum(1 for i in intents if i.get("recallTermSets"))
    has_disamb = sum(1 for i in intents if i.get("disambiguationHints"))
    print(f"\n意图字段覆盖率:")
    print(f"  recallTerms:      {has_recall_terms}/{len(intents)} ({100*has_recall_terms/len(intents):.1f}%)")
    print(f"  recallTermSets:   {has_recall_sets}/{len(intents)} ({100*has_recall_sets/len(intents):.1f}%)")
    print(f"  disambiguationHints: {has_disamb}/{len(intents)} ({100*has_disamb/len(intents):.1f}%)")

    # Embedding 构建索引（带磁盘缓存）
    client = httpx.Client(timeout=_EMBEDDING_TIMEOUT)
    base_url = os.getenv("AI_BANK_MODEL_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
    cache_dir = Path(__file__).resolve().parent / ".cache"

    def _build_or_load(name: str, entries: list[tuple[str, str]]) -> list[tuple[str, list[float]]]:
        """构建或从缓存加载 embedding 索引"""
        cache_file = cache_dir / f"semantic_compare_{name}.json"
        # 检查缓存
        if cache_file.exists():
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                if payload.get("model") == _DEFAULT_MODEL and len(payload.get("entries", [])) == len(entries):
                    texts_in_cache = [e["text"] for e in payload["entries"]]
                    texts_needed = [t for _, t in entries]
                    if texts_in_cache == texts_needed:
                        print(f"  从缓存加载: {name} ({len(entries)}条)")
                        return [(e["intentId"], e["vector"]) for e in payload["entries"]]
            except (json.JSONDecodeError, KeyError):
                pass

        # 在线构建
        texts = [t for _, t in entries]
        vecs = _embed_batch(client, api_key, base_url, texts)
        vectors = list(zip([iid for iid, _ in entries], vecs, strict=True))

        # 保存缓存
        try:
            cache_dir.mkdir(exist_ok=True)
            payload = {
                "model": _DEFAULT_MODEL,
                "entries": [{"intentId": iid, "text": t, "vector": vec} for (iid, t), vec in zip(entries, vecs)],
            }
            cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

        return vectors

    print(f"\n构建方案A索引（{len(index_a)}条）...", flush=True)
    vectors_a = _build_or_load("A", index_a)
    print(f"  完成: {len(vectors_a)} 条向量")

    print(f"\n构建方案B索引（{len(index_b)}条）...", flush=True)
    vectors_b = _build_or_load("B", index_b)
    print(f"  完成: {len(vectors_b)} 条向量")

    print(f"\n构建方案C索引（{len(index_c)}条）...", flush=True)
    vectors_c = _build_or_load("C", index_c)
    print(f"  完成: {len(vectors_c)} 条向量")

    # 收集所有测试 query
    # queries: (message, [expected_intents], intent_shape)
    queries: list[tuple[str, list[str], str]] = []
    for case in test_cases:
        msg = case.get("query", case.get("user_input", ""))
        expected_intents = case.get("expected_intents", [])
        expected_ids = [e["intent_id"] for e in expected_intents if e.get("intent_id")]
        shape = case.get("intent_shape", "single_intent")
        if msg and expected_ids:
            queries.append((msg, expected_ids, shape))

    print(f"\n开始语义召回对比（{len(queries)} 条query）...", flush=True)

    # 批量 embed 所有 query（带缓存）
    all_query_texts = [q[0][:512] for q in queries]
    print(f"  Embedding {len(all_query_texts)} 条query...", flush=True)

    query_cache_file = cache_dir / "semantic_compare_queries.json"
    query_vecs = None
    if query_cache_file.exists():
        try:
            payload = json.loads(query_cache_file.read_text(encoding="utf-8"))
            if payload.get("model") == _DEFAULT_MODEL and len(payload.get("entries", [])) == len(all_query_texts):
                cached_texts = [e["text"] for e in payload["entries"]]
                if cached_texts == all_query_texts:
                    print(f"  从缓存加载 query embeddings ({len(all_query_texts)}条)")
                    query_vecs = [e["vector"] for e in payload["entries"]]
        except (json.JSONDecodeError, KeyError):
            pass

    if query_vecs is None:
        query_vecs = _embed_batch(client, api_key, base_url, all_query_texts)
        try:
            cache_dir.mkdir(exist_ok=True)
            payload = {
                "model": _DEFAULT_MODEL,
                "entries": [{"text": t, "vector": v} for t, v in zip(all_query_texts, query_vecs)],
            }
            query_cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # 先跑词法召回，找出词法失败的用例
    print(f"\n  运行词法召回...", flush=True)
    # (idx, message, [expected_ids], shape)
    lexical_fail_cases: list[tuple[int, str, list[str], str]] = []
    for idx, (msg, expected_ids, shape) in enumerate(queries):
        lex_cands = adapter.recall(msg, page_context={}, limit=10)
        top_ids = {c.intent_id for c in lex_cands[:5]}
        # 词法失败 = 没有任何期望意图出现在 top5
        if not top_ids.intersection(expected_ids):
            lexical_fail_cases.append((idx, msg, expected_ids, shape))
    print(f"  词法失败用例: {len(lexical_fail_cases)}/{len(queries)}")

    # 对三种方案分别计算语义召回结果（numpy 向量化加速）
    results: dict[str, list[dict]] = {"A": [], "B": [], "C": []}

    # query 向量矩阵 (Q × D)
    q_mat = np.array(query_vecs, dtype=np.float32)
    q_mat = _normalize_matrix(q_mat)

    for method_name, vectors in [("A", vectors_a), ("B", vectors_b), ("C", vectors_c)]:
        print(f"\n  方案{method_name} 语义召回...", flush=True)

        # 构建索引矩阵 (N × D) 和 intent_id 映射
        index_ids = [iid for iid, _ in vectors]
        index_mat = np.array([vec for _, vec in vectors], dtype=np.float32)
        index_mat = _normalize_matrix(index_mat)

        # 一次性计算 Q × N 相似度矩阵
        sim_matrix = q_mat @ index_mat.T  # (Q × N)

        # intent_id → row indices 映射
        id_to_rows: dict[str, list[int]] = {}
        for row, iid in enumerate(index_ids):
            id_to_rows.setdefault(iid, []).append(row)

        for idx, (msg, expected_ids, shape) in enumerate(queries):
            sims = sim_matrix[idx]  # (N,)

            # per intent 取 max
            best: dict[str, float] = {}
            for iid, rows in id_to_rows.items():
                best[iid] = float(max(sims[r] for r in rows))

            # 排序
            ranked = sorted(best.items(), key=lambda x: (-x[1], x[0]))

            # top-10 结果
            top10 = ranked[:10]
            top_ids = [iid for iid, _ in top10]
            top_sims = [sim for _, sim in top10]

            # 所有期望意图的排名和分数，取最佳
            best_rank = None
            best_sim = None
            for rank, (iid, sim) in enumerate(ranked):
                if iid in expected_ids:
                    if best_rank is None or rank + 1 < best_rank:
                        best_rank = rank + 1  # 1-based
                        best_sim = sim

            top5_set = set(top_ids[:5])
            top3_set = set(top_ids[:3])
            in_top5 = bool(top5_set.intersection(expected_ids))
            in_top3 = bool(top3_set.intersection(expected_ids))
            in_top1 = top_ids[0] in expected_ids if top_ids else False

            results[method_name].append({
                "idx": idx,
                "message": msg,
                "expected_ids": expected_ids,
                "shape": shape,
                "best_rank": best_rank,
                "best_sim": best_sim,
                "in_top1": in_top1,
                "in_top3": in_top3,
                "in_top5": in_top5,
                "top5_ids": top_ids[:5],
                "top5_sims": [round(s, 4) for s in top_sims[:5]],
            })

    # ── 汇总报告 ──
    print("\n" + "=" * 80)
    print("语义索引方案对比报告")
    print("=" * 80)

    # 全局准确率
    print("\n【全局 top-k 命中率】")
    print(f"{'方案':<20} {'top1':>8} {'top3':>8} {'top5':>8} {'avg_sim':>10}")
    for method_name in ["A", "B", "C"]:
        r = results[method_name]
        top1 = sum(1 for x in r if x["in_top1"])
        top3 = sum(1 for x in r if x["in_top3"])
        top5 = sum(1 for x in r if x["in_top5"])
        valid_sims = [x["best_sim"] for x in r if x["best_sim"] is not None]
        avg_sim = sum(valid_sims) / len(valid_sims) if valid_sims else 0
        label = {"A": "A（当前多向量）", "B": "B（chunk）", "C": "C（增强多向量）"}[method_name]
        total = len(r) if r else 1
        print(f"  {label:<20} {top1}/{total} ({100*top1/total:.1f}%)  {top3}/{total} ({100*top3/total:.1f}%)  {top5}/{total} ({100*top5/total:.1f}%)  {avg_sim:.4f}")

    # 词法失败用例的语义复活率
    if lexical_fail_cases:
        print(f"\n【词法失败用例的语义复活率（{len(lexical_fail_cases)}条）】")
        print(f"{'方案':<20} {'复活top1':>10} {'复活top3':>10} {'复活top5':>10} {'avg_sim':>10}")
        fail_indices = {idx for idx, _, _, _ in lexical_fail_cases}
        for method_name in ["A", "B", "C"]:
            r = [x for x in results[method_name] if x["idx"] in fail_indices]
            top1 = sum(1 for x in r if x["in_top1"])
            top3 = sum(1 for x in r if x["in_top3"])
            top5 = sum(1 for x in r if x["in_top5"])
            valid_sims = [x["best_sim"] for x in r if x["best_sim"] is not None]
            avg_sim = sum(valid_sims) / len(valid_sims) if valid_sims else 0
            label = {"A": "A（当前多向量）", "B": "B（chunk）", "C": "C（增强多向量）"}[method_name]
            total = len(r) if r else 1
            print(f"  {label:<20} {top1}/{total} ({100*top1/total:.1f}%)   {top3}/{total} ({100*top3/total:.1f}%)   {top5}/{total} ({100*top5/total:.1f}%)   {avg_sim:.4f}")

    # 详细：词法失败用例的逐条对比
    if lexical_fail_cases:
        print(f"\n【词法失败用例逐条对比】")
        print(f"{'#':>3} {'message':<30} {'expected':<40} {'A_rank':>7} {'A_sim':>8} {'B_rank':>7} {'B_sim':>8} {'C_rank':>7} {'C_sim':>8}")
        for i, (idx, msg, expected_ids, shape) in enumerate(lexical_fail_cases):
            ra = results["A"][idx]
            rb = results["B"][idx]
            rc = results["C"][idx]
            msg_short = msg[:28] + ".." if len(msg) > 30 else msg
            exp_short = expected_ids[0][:38] + ".." if len(expected_ids[0]) > 40 else expected_ids[0]
            a_rank = str(ra["best_rank"]) if ra["best_rank"] else "N/A"
            b_rank = str(rb["best_rank"]) if rb["best_rank"] else "N/A"
            c_rank = str(rc["best_rank"]) if rc["best_rank"] else "N/A"
            a_sim = f"{ra['best_sim']:.4f}" if ra["best_sim"] else "N/A"
            b_sim = f"{rb['best_sim']:.4f}" if rb["best_sim"] else "N/A"
            c_sim = f"{rc['best_sim']:.4f}" if rc["best_sim"] else "N/A"
            print(f"  {i+1:>2} {msg_short:<30} {exp_short:<40} {a_rank:>7} {a_sim:>8} {b_rank:>7} {b_sim:>8} {c_rank:>7} {c_sim:>8}")

    # 相似度分数分布
    print(f"\n【期望意图相似度分数分布】")
    print(f"{'方案':<20} {'≥0.75':>8} {'≥0.70':>8} {'≥0.65':>8} {'≥0.60':>8} {'≥0.55':>8} {'<0.55':>8}")
    for method_name in ["A", "B", "C"]:
        r = results[method_name]
        sims = [x["best_sim"] for x in r if x["best_sim"] is not None]
        total = len(sims) if sims else 1
        if not sims:
            continue
        c75 = sum(1 for s in sims if s >= 0.75)
        c70 = sum(1 for s in sims if s >= 0.70)
        c65 = sum(1 for s in sims if s >= 0.65)
        c60 = sum(1 for s in sims if s >= 0.60)
        c55 = sum(1 for s in sims if s >= 0.55)
        below = sum(1 for s in sims if s < 0.55)
        label = {"A": "A（当前多向量）", "B": "B（chunk）", "C": "C（增强多向量）"}[method_name]
        print(f"  {label:<20} {c75:>8} {c70:>8} {c65:>8} {c60:>8} {c55:>8} {below:>8}")

    # 方案间差异
    print(f"\n【方案B（chunk）vs 方案A（当前）：期望意图排名变化】")
    improved = 0
    worsened = 0
    same = 0
    for idx in range(len(queries)):
        ra = results["A"][idx]
        rb = results["B"][idx]
        a_rank = ra["best_rank"] or 999
        b_rank = rb["best_rank"] or 999
        if b_rank < a_rank:
            improved += 1
        elif b_rank > a_rank:
            worsened += 1
        else:
            same += 1
    print(f"  B比A好: {improved}, B比A差: {worsened}, 相同: {same}")

    print(f"\n【方案C（增强多向量）vs 方案A（当前）：期望意图排名变化】")
    improved = 0
    worsened = 0
    same = 0
    for idx in range(len(queries)):
        ra = results["A"][idx]
        rc = results["C"][idx]
        a_rank = ra["best_rank"] or 999
        c_rank = rc["best_rank"] or 999
        if c_rank < a_rank:
            improved += 1
        elif c_rank > a_rank:
            worsened += 1
        else:
            same += 1
    print(f"  C比A好: {improved}, C比A差: {worsened}, 相同: {same}")

    client.close()
    print("\n完成。")


if __name__ == "__main__":
    run_comparison()
