#!/usr/bin/env python
"""
语义召回 & 子图推理价值分析脚本

不跑通过率，而是分析：
1. 语义召回触发率：多少用例真正触发了 embedding（top1<80 时才触发）
2. 语义召回候选质量：拉入的候选中多少是期望意图
3. 子图召回触发率：多少用例触发了子图扩召回
4. 子图召回候选质量：拉入的候选中多少是期望意图
5. 子图上下文质量：disambiguationHints 覆盖率、confusable 关系覆盖率
6. 各场景下的召回来源分布：候选来自词法/子图/语义哪个路径
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4
from app.subgraph_reasoning_v4 import SubgraphReasoningV4

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"


def main():
    print("=" * 80)
    print("语义召回 & 子图推理价值分析")
    print("=" * 80)

    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]

    adapter = V4GraphAdapter.load(GRAPH_PATH)
    subgraph = SubgraphReasoningV4(adapter)
    semantic = SemanticRecallV4(adapter)

    # ── 1. 图谱层面分析 ──
    print("\n## 1. 图谱边关系统计")
    total_confusable_edges = sum(len(v) for v in adapter._forward_confusable.values())
    total_sibling_edges = sum(len(v) for v in adapter._siblings.values())
    intents_with_disambig = 0
    intents_with_description = 0
    intents_with_usergoal = 0
    for intent in adapter._intents:
        if intent.get("disambiguationHints"):
            intents_with_disambig += 1
        if intent.get("description"):
            intents_with_description += 1
        if intent.get("userGoal"):
            intents_with_usergoal += 1
    total_intents = len(adapter._intents)
    print(f"  总意图数: {total_intents}")
    print(f"  confusable 边: {total_confusable_edges} (覆盖 {len(adapter._forward_confusable)} 个意图)")
    print(f"  sibling 边: {total_sibling_edges} (覆盖 {len(adapter._siblings)} 个意图)")
    print(f"  有 disambiguationHints: {intents_with_disambig}/{total_intents} ({intents_with_disambig/total_intents*100:.1f}%)")
    print(f"  有 description: {intents_with_description}/{total_intents} ({intents_with_description/total_intents*100:.1f}%)")
    print(f"  有 userGoal: {intents_with_usergoal}/{total_intents} ({intents_with_usergoal/total_intents*100:.1f}%)")

    # ── 2. 逐用例分析 ──
    print("\n## 2. 逐用例召回来源分析")

    stats = {
        "lexical_triggered": 0,        # 词法召回有结果
        "subgraph_triggered": 0,       # 子图扩召回触发
        "subgraph_candidates_added": 0, # 子图拉入的候选总数
        "subgraph_correct": 0,         # 子图拉入的候选中是期望意图的
        "semantic_triggered": 0,       # 语义召回触发
        "semantic_candidates_added": 0, # 语义拉入的候选总数
        "semantic_correct": 0,         # 语义拉入的候选中是期望意图的
        "disambig_available": 0,       # 候选中至少有一个有disambiguationHints
        "confusable_available": 0,     # 候选间存在confusable关系
    }

    # 按 intent_shape 分组统计
    shape_stats = defaultdict(lambda: {
        "subgraph_triggered": 0,
        "semantic_triggered": 0,
        "subgraph_correct": 0,
        "semantic_correct": 0,
        "total": 0,
    })

    # 详细用例记录（有子图/语义召回的）
    detail_cases = []

    for case in cases:
        query = case["query"]
        shape = case["intent_shape"]
        expected_ids = [e["intent_id"] for e in case.get("expected_intents", [])]
        expected_set = set(expected_ids)

        shape_stats[shape]["total"] += 1

        # 词法召回
        lex_candidates = adapter.recall(query, page_context={}, limit=10)
        if lex_candidates:
            stats["lexical_triggered"] += 1

        # 子图扩召回
        sub_candidates = subgraph.expand_recall(lex_candidates)
        if sub_candidates:
            stats["subgraph_triggered"] += 1
            shape_stats[shape]["subgraph_triggered"] += 1
            stats["subgraph_candidates_added"] += len(sub_candidates)
            for c in sub_candidates:
                if c.intent_id in expected_set:
                    stats["subgraph_correct"] += 1
                    shape_stats[shape]["subgraph_correct"] += 1

        # 语义召回
        top1_score = max(c.score for c in lex_candidates) if lex_candidates else 0
        if top1_score < 80:  # 门槛
            known_ids = {c.intent_id for c in lex_candidates}
            sem_candidates = semantic.recall(query, exclude_ids=known_ids)
            if sem_candidates:
                stats["semantic_triggered"] += 1
                shape_stats[shape]["semantic_triggered"] += 1
                stats["semantic_candidates_added"] += len(sem_candidates)
                for c in sem_candidates:
                    if c.intent_id in expected_set:
                        stats["semantic_correct"] += 1
                        shape_stats[shape]["semantic_correct"] += 1

        # 子图上下文质量
        all_candidates = list(lex_candidates) + list(sub_candidates)
        if all_candidates:
            contexts = subgraph.build_contexts(all_candidates)
            has_disambig = any(ctx.get("disambiguation_hints") for ctx in contexts)
            has_confusable = any(ctx.get("confusable_with") for ctx in contexts)
            if has_disambig:
                stats["disambig_available"] += 1
            if has_confusable:
                stats["confusable_available"] += 1

        # 记录有子图或语义召回的用例
        if sub_candidates or (top1_score < 80 and sem_candidates):
            detail_cases.append({
                "case_id": case["case_id"],
                "query": query,
                "shape": shape,
                "expected": expected_ids,
                "lex_top1": lex_candidates[0].intent_id if lex_candidates else None,
                "lex_top1_score": top1_score,
                "sub_added": [c.intent_id for c in sub_candidates],
                "sub_correct": [c.intent_id for c in sub_candidates if c.intent_id in expected_set],
                "sem_added": [c.intent_id for c in sem_candidates] if top1_score < 80 else [],
                "sem_correct": [c.intent_id for c in sem_candidates if c.intent_id in expected_set] if top1_score < 80 else [],
            })

    # ── 3. 输出统计 ──
    total = len(cases)
    print(f"\n## 3. 召回触发率")
    print(f"  词法召回有结果:   {stats['lexical_triggered']}/{total} ({stats['lexical_triggered']/total*100:.1f}%)")
    print(f"  子图扩召回触发:   {stats['subgraph_triggered']}/{total} ({stats['subgraph_triggered']/total*100:.1f}%)")
    print(f"  语义召回触发:     {stats['semantic_triggered']}/{total} ({stats['semantic_triggered']/total*100:.1f}%)")

    print(f"\n## 4. 召回候选质量")
    if stats["subgraph_candidates_added"] > 0:
        print(f"  子图召回: 拉入 {stats['subgraph_candidates_added']} 候选, 其中 {stats['subgraph_correct']} 个是期望意图 ({stats['subgraph_correct']/stats['subgraph_candidates_added']*100:.1f}%)")
    else:
        print(f"  子图召回: 未拉入任何候选")
    if stats["semantic_candidates_added"] > 0:
        print(f"  语义召回: 拉入 {stats['semantic_candidates_added']} 候选, 其中 {stats['semantic_correct']} 个是期望意图 ({stats['semantic_correct']/stats['semantic_candidates_added']*100:.1f}%)")
    else:
        print(f"  语义召回: 未拉入任何候选 (阈值0.65太严？)")

    print(f"\n## 5. 子图上下文质量")
    print(f"  候选含 disambiguationHints: {stats['disambig_available']}/{total} ({stats['disambig_available']/total*100:.1f}%)")
    print(f"  候选间有 confusable 关系:   {stats['confusable_available']}/{total} ({stats['confusable_available']/total*100:.1f}%)")

    print(f"\n## 6. 按 intent_shape 分组")
    print(f"  {'shape':20s} {'total':>5s} {'子图触发':>8s} {'子图命中':>8s} {'语义触发':>8s} {'语义命中':>8s}")
    print("  " + "─" * 65)
    for shape in ["single_intent", "multi_intent", "ambiguous_intent", "non_task"]:
        s = shape_stats[shape]
        print(f"  {shape:20s} {s['total']:>5d} {s['subgraph_triggered']:>8d} {s['subgraph_correct']:>8d} {s['semantic_triggered']:>8d} {s['semantic_correct']:>8d}")

    print(f"\n## 7. 详细用例（有子图/语义召回的）")
    print(f"  共 {len(detail_cases)} 条")
    for d in detail_cases[:30]:
        print(f"  {d['case_id']:8s} [{d['shape']:18s}] 「{d['query'][:25]:25s}」 lex_top1={d['lex_top1']}({d['lex_top1_score']})")
        if d['sub_added']:
            correct_mark = "✅" if d['sub_correct'] else "❌"
            print(f"           子图: {correct_mark} added={d['sub_added']}")
            if d['sub_correct']:
                print(f"                命中期望意图: {d['sub_correct']}")
        if d['sem_added']:
            correct_mark = "✅" if d['sem_correct'] else "❌"
            print(f"           语义: {correct_mark} added={d['sem_added']}")
            if d['sem_correct']:
                print(f"                命中期望意图: {d['sem_correct']}")

    # ── 8. 语义召回阈值分析 ──
    print(f"\n## 8. 语义召回阈值分析")
    print(f"  当前阈值: 0.65")
    # 统计不同阈值下能触发多少用例
    if semantic.enabled:
        threshold_counts = {0.45: 0, 0.50: 0, 0.55: 0, 0.60: 0, 0.65: 0, 0.70: 0}
        for case in cases[:50]:  # 采样50条
            query = case["query"]
            lex_candidates = adapter.recall(query, page_context={}, limit=10)
            top1_score = max(c.score for c in lex_candidates) if lex_candidates else 0
            if top1_score >= 80:
                continue
            known_ids = {c.intent_id for c in lex_candidates}
            sem_candidates = semantic.recall(query, exclude_ids=known_ids)
            # 看这些候选的相似度
            for threshold in threshold_counts:
                # recall 内部用了 0.65 阈值，我们只能看返回的候选
                # 如果有候选返回，说明 0.65 阈值下能触发
                pass
            if sem_candidates:
                threshold_counts[0.65] += 1
        print(f"  在50条采样中，语义召回触发: {threshold_counts[0.65]}/50")
        print(f"  （要测不同阈值需修改源码阈值后重新运行）")

    semantic.close()


if __name__ == "__main__":
    main()
