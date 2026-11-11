#!/usr/bin/env python
"""
V4 四层召回验证脚本：词法召回 → 子图推理召回 → embedding 语义召回 → LLM 裁决

对比四种配置的召回效果：
1. lexical:           纯词法 + 图谱扩召（基线）
2. +subgraph:         词法 + 子图推理召回（边关系召回相关意图）
3. +semantic:          词法 + 子图 + embedding 语义召回
4. full_pipeline:     完整四层（词法 + 子图 + embedding + LLM 裁决，LLM 看子图上下文）
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# SKIP_LLM=1 时跳过 full_pipeline（LLM 裁决），只跑前三层
_SKIP_LLM = bool(os.getenv("SKIP_LLM"))

ROOT = Path(__file__).resolve().parent.parent  # agent-runtime/
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter  # noqa: E402
from app.semantic_recall_v4 import SemanticRecallV4  # noqa: E402
from app.subgraph_reasoning_v4 import SubgraphReasoningV4  # noqa: E402
from app.llm_judge_v4 import LLMJudgeV4  # noqa: E402

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"


def lexical_recall(adapter: V4GraphAdapter, message: str, limit: int = 10):
    """第一层：纯词法 + 图谱扩召"""
    return adapter.recall(message, page_context={}, limit=limit)


def subgraph_recall(
    adapter: V4GraphAdapter,
    subgraph: SubgraphReasoningV4,
    message: str,
    limit: int = 10,
):
    """第一层 + 第二层：词法召回 + 子图推理召回（沿边关系扩展候选池）
    
    子图扩召回只在词法弱匹配(top1<80)时触发。
    扩召回候选追加到候选池尾部，不参与 top5 排序竞争——保证 LLM 能看到
    全部相关意图，同时不挤掉词法精确命中。
    """
    lexical_candidates = lexical_recall(adapter, message, limit)
    if not subgraph.enabled:
        return lexical_candidates

    # 子图扩召回：沿 confusableIntents 边拉入相关意图
    # expand_recall 内部会检查 top1 分值，强匹配(≥80)时不扩召回
    expanded = subgraph.expand_recall(lexical_candidates)

    if not expanded:
        return lexical_candidates

    # 扩召回候选追加到尾部，不参与排序竞争
    # 保持词法候选的 top1 位置——子图候选仅扩充候选池供 LLM 裁决
    merged = list(lexical_candidates) + list(expanded)

    maximum = min(max(limit, 1), 12)
    return merged[:maximum]


def semantic_recall(
    adapter: V4GraphAdapter,
    subgraph: SubgraphReasoningV4,
    semantic: SemanticRecallV4,
    message: str,
    limit: int = 10,
):
    """第一层 + 第二层 + 第三层：词法 + 子图 + embedding 语义召回
    
    语义召回只在词法弱匹配(top1<80)时触发，避免给强匹配引入噪音。
    词法返回0候选时不触发语义召回——大概率是 non_task 查询，
    语义召回只会凭空创造噪音候选。
    语义候选与词法/子图候选合并后追加到尾部，不参与排序竞争——
    保持词法候选的 top1 位置，语义候选仅扩充候选池供 LLM 裁决。
    """
    candidates = subgraph_recall(adapter, subgraph, message, limit)
    if not semantic.enabled:
        return candidates

    # 词法返回0候选 → 大概率 non_task，不触发语义召回
    if not candidates:
        return []

    # 语义召回门槛：词法已强匹配(alias exact 80)时不触发
    top1_score = max(c.score for c in candidates)
    if top1_score >= 80:
        return candidates

    known_ids = {c.intent_id for c in candidates}
    semantic_candidates = semantic.recall(message, exclude_ids=known_ids)

    if not semantic_candidates:
        return candidates

    # 语义候选追加到尾部，不参与排序竞争
    merged = list(candidates) + list(semantic_candidates)

    maximum = min(max(limit, 1), 12)
    return merged[:maximum]


def full_pipeline(
    adapter: V4GraphAdapter,
    subgraph: SubgraphReasoningV4,
    semantic: SemanticRecallV4,
    judge: LLMJudgeV4,
    message: str,
    intent_shape: str = "single_intent",
    limit: int = 10,
):
    """完整四层链路：词法 → 子图召回 → embedding → LLM 裁决（含子图上下文）
    
    LLM 裁决触发策略（对齐 service.py _v4_probe_pipeline）：
    - non_task: 不触发 LLM
    - single_intent: 弱召回(top1<80)或存在强竞争候选(top2>=60)时触发 LLM 救回
      ——子图召回拉入的正确候选需经 LLM 重排才能进入 top1
    - ambiguous_intent / multi_intent: 触发 LLM 重排序，
      但不截断候选列表——LLM 只负责把更相关的候选排前面，不丢弃任何候选
    """
    # 前三层
    candidates = semantic_recall(adapter, subgraph, semantic, message, limit)
    if not candidates:
        return []

    if not judge.enabled:
        return candidates

    # ── LLM 触发决策 ── (对齐 service.py _v4_probe_pipeline)
    # non_task: 不触发 LLM
    # single_intent: 弱召回(top1<80)或存在强竞争候选(top2>=60)时触发 LLM 救回
    #   ——子图召回拉入的正确候选需经 LLM 重排才能进入 top1
    # ambiguous / multi_intent: 触发 LLM 重排序（候选内裁决，不截断）
    if intent_shape == "non_task":
        return candidates[:limit]

    # 保留词法优先顺序——扩召回候选追加到尾部,不参与 top1 竞争
    # (sorted 会把语义/子图高分候选挤掉词法 top1,违背设计意图)
    sorted_cands = list(candidates)

    should_judge = False
    if intent_shape == "single_intent" and sorted_cands:
        top1_score = sorted_cands[0].score
        top2_score = sorted_cands[1].score if len(sorted_cands) > 1 else 0
        should_judge = (top1_score < 80 or top2_score >= 60)
    elif intent_shape in ("ambiguous_intent", "multi_intent"):
        should_judge = True

    if not should_judge:
        return candidates[:limit]

    subgraph_contexts = subgraph.build_contexts(sorted_cands[:12])

    judged = judge.judge(
        message, sorted_cands[:12],
        intent_shape=intent_shape,
        subgraph_contexts=subgraph_contexts,
    )
    if not judged:
        return sorted_cands[:limit]

    # 按 LLM 裁决结果重排序——保留全部候选，不截断
    judged_order = {item["intent_id"]: item["confidence"] for item in judged}
    # 超强匹配保护:原 top1 分数 >= 145 时,LLM 重排后仍保留原 top1
    # ——避免 LLM 把多关键词强匹配的 top1 错误排走
    original_top1 = sorted_cands[0]
    super_strong = original_top1.score >= 145
    judged_candidates = []
    remaining = []
    for c in sorted_cands:
        if c.intent_id in judged_order:
            judged_candidates.append((c, judged_order[c.intent_id]))
        else:
            remaining.append(c)

    judged_candidates.sort(key=lambda x: -x[1])
    result = [c for c, _ in judged_candidates] + remaining

    if super_strong:
        # 把原 top1 强制提到第一位
        result = (
            [original_top1]
            + [c for c in result if c.intent_id != original_top1.intent_id]
        )

    # ambiguous/multi_intent 保留全部候选（不截断到 8）
    return result[:limit]


def _check_confusable_pairs(subgraph: SubgraphReasoningV4, candidates: list) -> bool:
    """检查候选间是否存在 confusable 关系——只有容易混淆的候选才需要 LLM 消歧"""
    candidate_ids = {c.intent_id for c in candidates}
    for c in candidates:
        neighbors = subgraph._get_confusable_neighbors(c.intent_id)
        if neighbors & candidate_ids:
            return True
    return False


# ── 检查函数 ──

def check_single(case, candidates) -> dict:
    expected_ids = [e["intent_id"] for e in case.get("expected_intents", [])]
    candidate_ids = [c.intent_id for c in candidates]
    top1 = candidate_ids[0] if candidate_ids else None
    return {
        "pass": top1 in expected_ids if expected_ids else None,
        "top1": top1,
        "candidate_ids": candidate_ids[:5],
    }


def check_multi(case, candidates) -> dict:
    expected_ids = [e["intent_id"] for e in case.get("expected_intents", [])]
    candidate_ids = [c.intent_id for c in candidates]
    found = [eid for eid in expected_ids if eid in candidate_ids]
    return {
        "pass": len(found) == len(expected_ids) if expected_ids else None,
        "found_count": len(found),
        "expected_count": len(expected_ids),
        "missing": [eid for eid in expected_ids if eid not in candidate_ids],
        "candidate_ids": candidate_ids[:5],
    }


def check_ambiguous(case, candidates) -> dict:
    expected_ids = [e["intent_id"] for e in case.get("expected_intents", [])]
    candidate_ids = [c.intent_id for c in candidates]
    found = [eid for eid in expected_ids if eid in candidate_ids]
    return {
        "pass": len(found) == len(expected_ids) if expected_ids else None,
        "found_count": len(found),
        "expected_count": len(expected_ids),
        "missing": [eid for eid in expected_ids if eid not in candidate_ids],
        "candidate_ids": candidate_ids[:5],
    }


def check_non_task(case, candidates) -> dict:
    return {
        "pass": len(candidates) == 0,
        "recall_count": len(candidates),
    }


def check_case(case, candidates):
    shape = case["intent_shape"]
    if shape == "single_intent":
        return check_single(case, candidates)
    elif shape == "multi_intent":
        return check_multi(case, candidates)
    elif shape == "ambiguous_intent":
        return check_ambiguous(case, candidates)
    else:
        return check_non_task(case, candidates)


def main():
    print("=" * 80)
    print("V4 四层召回验证: 词法 → 子图推理 → embedding 语义 → LLM 裁决")
    print(f"测试集: {DATASET_PATH}")
    print(f"图谱:   {GRAPH_PATH}")
    print("=" * 80)

    # 加载测试集
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    print(f"用例总数: {len(cases)}")

    # 加载适配器和模块
    adapter = V4GraphAdapter.load(GRAPH_PATH)
    subgraph = SubgraphReasoningV4(adapter)
    semantic = SemanticRecallV4(adapter)
    judge = LLMJudgeV4()
    judge._adapter = adapter  # 让 judge 能访问 intent 详情

    print(f"子图推理: {'✅ 启用' if subgraph.enabled else '❌ 禁用'}")
    print(f"语义召回: {'✅ 启用' if semantic.enabled else '❌ 禁用 (无 QWEN_KEY)'}")
    if _SKIP_LLM:
        print("LLM 裁决: ⏭️ 跳过 (SKIP_LLM)")
    else:
        print(f"LLM 裁决: {'✅ 启用' if judge.enabled else '❌ 禁用 (无 QWEN_KEY)'}")

    # 统计
    configs = ["lexical", "+subgraph", "+semantic"]
    if not _SKIP_LLM:
        configs.append("full_pipeline")
    stats = {cfg: {"pass": 0, "fail": 0, "total": 0} for cfg in configs}
    shape_stats = {cfg: {} for cfg in configs}
    failures = {cfg: [] for cfg in configs}

    # 各层"复活"的用例数
    subgraph_helped = 0
    semantic_helped = 0
    llm_helped = 0

    t0 = time.time()

    for i, case in enumerate(cases):
        case_id = case["case_id"]
        query = case["query"]
        shape = case["intent_shape"]

        # 1. 纯词法召回
        lex_candidates = lexical_recall(adapter, query)
        lex_result = check_case(case, lex_candidates)

        # 2. 词法 + 子图推理召回
        sub_candidates = subgraph_recall(adapter, subgraph, query)
        sub_result = check_case(case, sub_candidates)

        # 3. 词法 + 子图 + embedding 语义召回
        sem_candidates = semantic_recall(adapter, subgraph, semantic, query)
        sem_result = check_case(case, sem_candidates)

        # 4. 完整四层链路
        if _SKIP_LLM:
            full_result = {"pass": None}
        else:
            full_candidates = full_pipeline(adapter, subgraph, semantic, judge, query, intent_shape=shape)
            full_result = check_case(case, full_candidates)

        # 统计
        results = [("lexical", lex_result), ("+subgraph", sub_result), ("+semantic", sem_result)]
        if not _SKIP_LLM:
            results.append(("full_pipeline", full_result))
        for cfg, result in results:
            if cfg not in stats:
                continue
            stats[cfg]["total"] += 1
            if result["pass"] is True:
                stats[cfg]["pass"] += 1
            else:
                stats[cfg]["fail"] += 1
                failures[cfg].append({
                    "case_id": case_id,
                    "query": query,
                    "shape": shape,
                    "missing": result.get("missing", []),
                    "top1": result.get("top1"),
                    "candidates": result.get("candidate_ids", []),
                })

            if shape not in shape_stats[cfg]:
                shape_stats[cfg][shape] = {"pass": 0, "total": 0}
            shape_stats[cfg][shape]["total"] += 1
            if result["pass"] is True:
                shape_stats[cfg][shape]["pass"] += 1

        # 检测各层帮助
        if lex_result["pass"] is not True and sub_result["pass"] is True:
            subgraph_helped += 1
        if sub_result["pass"] is not True and sem_result["pass"] is True:
            semantic_helped += 1
        if not _SKIP_LLM and sem_result["pass"] is not True and full_result["pass"] is True:
            llm_helped += 1

        # 进度输出
        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            print(f"  进度: {i+1}/{len(cases)} ({elapsed:.1f}s)")

    elapsed = time.time() - t0

    # ── 汇总 ──
    print("\n" + "=" * 80)
    print("汇总")
    print("=" * 80)

    print(f"\n{'配置':25s} {'通过':>6s} {'失败':>6s} {'总数':>6s} {'通过率':>8s}")
    print("─" * 60)
    for cfg in configs:
        s = stats[cfg]
        rate = s["pass"] / s["total"] * 100 if s["total"] else 0
        print(f"{cfg:25s} {s['pass']:>6d} {s['fail']:>6d} {s['total']:>6d} {rate:>7.1f}%")

    print(f"\n按 intent_shape 分组:")
    shapes = ["single_intent", "multi_intent", "ambiguous_intent", "non_task"]
    print(f"  {'配置':25s} {'single':>10s} {'multi':>10s} {'ambiguous':>10s} {'non_task':>10s}")
    print("  " + "─" * 70)
    for cfg in configs:
        parts = [cfg]
        for shape in shapes:
            ss = shape_stats[cfg].get(shape, {"pass": 0, "total": 0})
            if ss["total"] > 0:
                parts.append(f"{ss['pass']}/{ss['total']}")
            else:
                parts.append("-")
        print(f"  {parts[0]:25s} {parts[1]:>10s} {parts[2]:>10s} {parts[3]:>10s} {parts[4]:>10s}")

    print(f"\n子图推理召回复活用例: {subgraph_helped}")
    print(f"embedding 语义召回复活用例: {semantic_helped}")
    print(f"LLM 裁决复活用例: {llm_helped}")
    print(f"总耗时: {elapsed:.1f}s")

    # ── 失败详情 ──
    for cfg in configs:
        if not failures[cfg]:
            continue
        print(f"\n{'=' * 80}")
        print(f"{cfg} 失败用例 ({len(failures[cfg])} 条):")
        print("=" * 80)
        for f in failures[cfg]:
            missing_str = ", ".join(f.get("missing", [])) if f.get("missing") else "(无缺失)"
            top1_str = f.get("top1", "—") or "—"
            print(f"  {f['case_id']:8s} [{f['shape']:18s}] 「{f['query'][:25]:25s}」")
            print(f"    missing: {missing_str}")
            print(f"    top1:    {top1_str}")
            print(f"    cands:   {f.get('candidates', [])}")

    # 清理
    semantic.close()
    if not _SKIP_LLM:
        judge.close()


if __name__ == "__main__":
    main()
