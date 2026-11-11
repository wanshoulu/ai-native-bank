#!/usr/bin/env python
"""
语义向量索引设计对比测试

方案A（当前多向量）: 每个意图的 title/aliases/description 各生成一个向量，按意图取 max sim
方案B（用户建议chunk）: 每个意图的 name+description+aliases 合并成一条 chunk，一个意图一个向量

对比指标：
1. 召回命中率：拉入的候选中多少是期望意图
2. 相似度分布：期望意图 vs 非期望意图的相似度是否有明显区分
3. Top-K 准确率：query 的期望意图在语义召回 Top-K 中的比例
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4, _cosine, _load_qwen_key, _MAX_TEXT_CHARS, _EMBEDDING_DIMENSIONS, _DEFAULT_BASE_URL, _DEFAULT_MODEL, _EMBEDDING_BATCH_SIZE, _EMBEDDING_TIMEOUT
import httpx

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"


def _intent_chunk(intent: dict) -> str:
    """方案B：把意图的 name+description+aliases 合并成一条 chunk"""
    parts = []
    title = str(intent.get("title", "") or "").strip()
    if title:
        parts.append(title)
    desc = str(intent.get("description", "") or "").strip()
    if desc:
        parts.append(desc)
    aliases = intent.get("aliases", [])
    if aliases:
        parts.append("相似问：" + "、".join(str(a) for a in aliases if a))
    text = "。".join(parts)
    return text[:_MAX_TEXT_CHARS]


def _intent_utterances(intent: dict) -> list[str]:
    """方案A：多条文本（当前设计）"""
    texts: list[str] = []
    title = str(intent.get("title", "") or "").strip()
    if title:
        texts.append(title[:_MAX_TEXT_CHARS])
    for alias in intent.get("aliases", []):
        t = str(alias or "").strip()
        if t and t not in texts:
            texts.append(t[:_MAX_TEXT_CHARS])
    desc = str(intent.get("description", "") or "").strip()
    if desc and desc not in texts:
        texts.append(desc[:_MAX_TEXT_CHARS])
    return texts


def embed_texts(texts: list[str], api_key: str, base_url: str, model: str) -> list[list[float]]:
    """批量调用 embedding API"""
    client = httpx.Client(timeout=_EMBEDDING_TIMEOUT)
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
                "model": model,
                "input": batch,
                "dimensions": _EMBEDDING_DIMENSIONS,
            },
        )
        if resp.status_code != 200:
            raise ValueError(f"embedding API status {resp.status_code}: {resp.text[:200]}")
        data = resp.json().get("data", [])
        data.sort(key=lambda item: item.get("index", 0))
        for item in data:
            vec = item.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise ValueError("embedding vector is invalid")
            all_vectors.append(vec)
    client.close()
    return all_vectors


def main():
    print("=" * 80)
    print("语义向量索引设计对比测试")
    print("=" * 80)

    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    adapter = V4GraphAdapter.load(GRAPH_PATH)

    api_key = _load_qwen_key()
    base_url = _DEFAULT_BASE_URL
    model = _DEFAULT_MODEL

    if not api_key:
        print("❌ QWEN_KEY 未找到")
        return

    # ── 构建两种索引 ──
    # 方案A：多向量（每个意图多条文本，每条一个向量）
    print("\n## 构建方案A索引（多向量）...")
    multi_entries: list[tuple[str, str]] = []  # (intent_id, text)
    for intent in adapter._intents:
        lifecycle = intent.get("lifecycle", "active")
        if lifecycle in ("deprecated", "retired"):
            continue
        for text in _intent_utterances(intent):
            multi_entries.append((intent["id"], text))
    print(f"  方案A: {len(multi_entries)} 条向量（{len({iid for iid,_ in multi_entries})} 个意图）")

    # 方案B：单chunk（每个意图一条向量）
    print("## 构建方案B索引（单chunk）...")
    chunk_entries: list[tuple[str, str]] = []  # (intent_id, text)
    for intent in adapter._intents:
        lifecycle = intent.get("lifecycle", "active")
        if lifecycle in ("deprecated", "retired"):
            continue
        chunk = _intent_chunk(intent)
        if chunk:
            chunk_entries.append((intent["id"], chunk))
    print(f"  方案B: {len(chunk_entries)} 条向量（{len({iid for iid,_ in chunk_entries})} 个意图）")

    # 生成 embedding
    print("\n## 生成 embedding...")
    print(f"  方案A: {len(multi_entries)} 条...", flush=True)
    multi_vectors = embed_texts([t for _, t in multi_entries], api_key, base_url, model)
    multi_index = list(zip([iid for iid, _ in multi_entries], multi_vectors, strict=True))
    print(f"  方案A: 完成")

    print(f"  方案B: {len(chunk_entries)} 条...", flush=True)
    chunk_vectors = embed_texts([t for _, t in chunk_entries], api_key, base_url, model)
    chunk_index = list(zip([iid for iid, _ in chunk_entries], chunk_vectors, strict=True))
    print(f"  方案B: 完成")

    # ── 逐用例对比 ──
    print("\n## 逐用例对比...")

    THRESHOLDS = [0.45, 0.55, 0.65, 0.75]

    for threshold in THRESHOLDS:
        print(f"\n{'=' * 80}")
        print(f"## 阈值 = {threshold}")
        print(f"{'=' * 80}")

        stats_a = {"total": 0, "expected_in_topk": 0, "correct_ratio": 0.0,
                   "hit_cases": 0, "miss_cases": 0, "noise": 0}
        stats_b = {"total": 0, "expected_in_topk": 0, "correct_ratio": 0.0,
                    "hit_cases": 0, "miss_cases": 0, "noise": 0}

        # 相似度分布
        sims_a_expected = []
        sims_a_nonexpected = []
        sims_b_expected = []
        sims_b_nonexpected = []

        detail_cases = []

        for case in cases:
            query = case["query"]
            expected_ids = [e["intent_id"] for e in case.get("expected_intents", [])]
            expected_set = set(expected_ids)

            # query embedding
            query_vec = embed_texts([query[:_MAX_TEXT_CHARS]], api_key, base_url, model)[0]

            # 方案A: 按意图取 max sim
            best_a: dict[str, float] = {}
            for intent_id, vec in multi_index:
                sim = _cosine(query_vec, vec)
                if sim < threshold:
                    continue
                current = best_a.get(intent_id)
                if current is None or sim > current:
                    best_a[intent_id] = sim
            ranked_a = sorted(best_a.items(), key=lambda x: (-x[1], x[0]))[:5]
            top5_a = set(iid for iid, _ in ranked_a)
            expected_in_top5_a = expected_set & top5_a
            has_expected_a = bool(expected_in_top5_a)

            # 方案B: 直接 sim
            best_b: dict[str, float] = {}
            for intent_id, vec in chunk_index:
                sim = _cosine(query_vec, vec)
                if sim < threshold:
                    continue
                best_b[intent_id] = sim
            ranked_b = sorted(best_b.items(), key=lambda x: (-x[1], x[0]))[:5]
            top5_b = set(iid for iid, _ in ranked_b)
            expected_in_top5_b = expected_set & top5_b
            has_expected_b = bool(expected_in_top5_b)

            # 统计
            stats_a["total"] += 1
            stats_b["total"] += 1
            if has_expected_a:
                stats_a["expected_in_topk"] += 1
            if has_expected_b:
                stats_b["expected_in_topk"] += 1

            # 相似度分布
            for iid, sim in ranked_a:
                if iid in expected_set:
                    sims_a_expected.append(sim)
                else:
                    sims_a_nonexpected.append(sim)
            for iid, sim in ranked_b:
                if iid in expected_set:
                    sims_b_expected.append(sim)
                else:
                    sims_b_nonexpected.append(sim)

            # 噪音统计
            stats_a["noise"] += len(top5_a - expected_set) if not case.get("intent_shape") == "non_task" else 0
            stats_b["noise"] += len(top5_b - expected_set) if not case.get("intent_shape") == "non_task" else 0

            # 记录差异用例
            if has_expected_a != has_expected_b:
                detail_cases.append({
                    "case_id": case["case_id"],
                    "query": query,
                    "shape": case["intent_shape"],
                    "expected": expected_ids,
                    "A_top5": [(iid, round(s, 3)) for iid, s in ranked_a[:5]],
                    "A_hit": has_expected_a,
                    "B_top5": [(iid, round(s, 3)) for iid, s in ranked_b[:5]],
                    "B_hit": has_expected_b,
                })

        # ── 输出结果 ──
        print(f"\n### Top5 命中率（期望意图在 Top5 中）")
        print(f"  方案A（多向量）: {stats_a['expected_in_topk']}/{stats_a['total']} ({stats_a['expected_in_topk']/stats_a['total']*100:.1f}%)")
        print(f"  方案B（单chunk）: {stats_b['expected_in_topk']}/{stats_b['total']} ({stats_b['expected_in_topk']/stats_b['total']*100:.1f}%)")

        print(f"\n### 相似度分布")
        if sims_a_expected:
            avg_exp_a = sum(sims_a_expected) / len(sims_a_expected)
            avg_non_a = sum(sims_a_nonexpected) / len(sims_a_nonexpected) if sims_a_nonexpected else 0
            print(f"  方案A: 期望意图平均相似度 {avg_exp_a:.4f}, 非期望意图平均相似度 {avg_non_a:.4f}, 差值 {avg_exp_a - avg_non_a:.4f}")
        if sims_b_expected:
            avg_exp_b = sum(sims_b_expected) / len(sims_b_expected)
            avg_non_b = sum(sims_b_nonexpected) / len(sims_b_nonexpected) if sims_b_nonexpected else 0
            print(f"  方案B: 期望意图平均相似度 {avg_exp_b:.4f}, 非期望意图平均相似度 {avg_non_b:.4f}, 差值 {avg_exp_b - avg_non_b:.4f}")

        print(f"\n### 噪音候选总数（非期望意图在Top5中）")
        print(f"  方案A: {stats_a['noise']}")
        print(f"  方案B: {stats_b['noise']}")

        if detail_cases:
            print(f"\n### 差异用例（A/B 命中结果不同）: {len(detail_cases)} 条")
            for d in detail_cases[:15]:
                a_mark = "✅" if d["A_hit"] else "❌"
                b_mark = "✅" if d["B_hit"] else "❌"
                print(f"  {d['case_id']:8s} [{d['shape']:18s}] 「{d['query'][:20]:20s}」 A={a_mark} B={b_mark}")
                print(f"           期望: {d['expected']}")
                print(f"           A_top5: {d['A_top5'][:3]}")
                print(f"           B_top5: {d['B_top5'][:3]}")


if __name__ == "__main__":
    main()
