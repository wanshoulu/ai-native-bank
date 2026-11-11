#!/usr/bin/env python
"""分析第五轮测评的失败用例，重点看LLM裁决帮倒忙的single_intent用例"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from app.llm_judge_v4 import LLMJudgeV4

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

# 导入测试脚本中的函数
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_eval_v4_semantic import (
    lexical_recall, subgraph_recall, semantic_recall,
    full_pipeline, _check_confusable_pairs,
)


def main():
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]

    adapter = V4GraphAdapter.load(GRAPH_PATH)
    subgraph = SubgraphReasoningV4(adapter)
    semantic = SemanticRecallV4(adapter)
    judge = LLMJudgeV4()
    judge._adapter = adapter

    print("=" * 90)
    print("第五轮失败用例深度分析")
    print("=" * 90)

    # 收集所有 full_pipeline 失败的用例
    failures = []
    for case in cases:
        query = case["query"]
        shape = case["intent_shape"]
        expected_ids = [e["intent_id"] for e in case.get("expected_intents", [])]

        # 获取三层召回结果
        lex_cands = lexical_recall(adapter, query)
        sub_cands = subgraph_recall(adapter, subgraph, query)
        sem_cands = semantic_recall(adapter, subgraph, semantic, query)
        full_cands = full_pipeline(adapter, subgraph, semantic, judge, query, intent_shape=shape)

        # 检查各层结果
        if shape == "single_intent":
            lex_top1 = lex_cands[0].intent_id if lex_cands else None
            sem_top1 = sem_cands[0].intent_id if sem_cands else None
            full_top1 = full_cands[0].intent_id if full_cands else None

            lex_pass = lex_top1 in expected_ids
            sem_pass = sem_top1 in expected_ids
            full_pass = full_top1 in expected_ids

            if not full_pass:
                # full_pipeline 失败
                # 看每层得分
                sem_sorted = sorted(sem_cands, key=lambda c: -c.score)
                full_sorted = sorted(full_cands, key=lambda c: -c.score) if full_cands else []

                # 检查 LLM 是否被触发
                has_confusable = _check_confusable_pairs(subgraph, sem_cands) if len(sem_cands) > 1 else False

                # 检查 sem 层 top1 和 top2 分数差
                top1_score = sem_sorted[0].score if sem_sorted else 0
                top2_score = sem_sorted[1].score if len(sem_sorted) > 1 else 0
                score_gap = top1_score - top2_score

                # 看 expected 在候选中的位置
                expected_in_sem = None
                for i, c in enumerate(sem_sorted):
                    if c.intent_id in expected_ids:
                        expected_in_sem = i + 1
                        break

                llm_triggered = has_confusable and len(sem_cands) > 1

                failures.append({
                    "case_id": case["case_id"],
                    "query": query,
                    "shape": shape,
                    "expected": expected_ids,
                    "lex_top1": lex_top1,
                    "lex_pass": lex_pass,
                    "sem_top1": sem_top1,
                    "sem_pass": sem_pass,
                    "full_top1": full_top1,
                    "full_pass": full_pass,
                    "top1_score": top1_score,
                    "top2_score": top2_score,
                    "score_gap": score_gap,
                    "has_confusable": has_confusable,
                    "llm_triggered": llm_triggered,
                    "expected_in_sem_rank": expected_in_sem,
                    "sem_candidates": [(c.intent_id, c.score) for c in sem_sorted[:5]],
                    "full_candidates": [(c.intent_id, c.score) for c in full_sorted[:5]],
                })
        elif shape in ("ambiguous_intent", "multi_intent"):
            sem_ids = [c.intent_id for c in sem_cands]
            full_ids = [c.intent_id for c in full_cands]
            sem_found = [eid for eid in expected_ids if eid in sem_ids]
            full_found = [eid for eid in expected_ids if eid in full_ids]

            if len(full_found) != len(expected_ids):
                failures.append({
                    "case_id": case["case_id"],
                    "query": query,
                    "shape": shape,
                    "expected": expected_ids,
                    "sem_found": sem_found,
                    "sem_missing": [eid for eid in expected_ids if eid not in sem_ids],
                    "full_found": full_found,
                    "full_missing": [eid for eid in expected_ids if eid not in full_ids],
                    "sem_candidates": [(c.intent_id, c.score) for c in sorted(sem_cands, key=lambda c: -c.score)[:5]],
                    "full_candidates": [(c.intent_id, c.score) for c in sorted(full_cands, key=lambda c: -c.score)[:5]],
                })
        elif shape == "non_task":
            if len(full_cands) > 0:
                failures.append({
                    "case_id": case["case_id"],
                    "query": query,
                    "shape": shape,
                    "full_count": len(full_cands),
                    "full_candidates": [(c.intent_id, c.score) for c in full_cands[:3]],
                })

    # ── 分析 single_intent 失败 ──
    single_failures = [f for f in failures if f["shape"] == "single_intent"]
    print(f"\n{'='*90}")
    print(f"single_intent 失败: {len(single_failures)} 条")
    print(f"{'='*90}")

    llm_hurt = 0
    llm_neutral = 0
    data_missing = 0

    for f in single_failures:
        print(f"\n  {f['case_id']} 「{f['query'][:30]}」")
        print(f"    expected: {f['expected']}")
        print(f"    lex_top1: {f['lex_top1']} ({'✅' if f['lex_pass'] else '❌'})")
        print(f"    sem_top1: {f['sem_top1']} ({'✅' if f['sem_pass'] else '❌'})")
        print(f"    full_top1: {f['full_top1']} (❌)")
        print(f"    top1_score={f['top1_score']}, top2_score={f['top2_score']}, gap={f['score_gap']}")
        print(f"    has_confusable={f['has_confusable']}, llm_triggered={f['llm_triggered']}")
        print(f"    expected_in_sem_rank: {f['expected_in_sem_rank']}")
        print(f"    sem_cands: {f['sem_candidates']}")
        print(f"    full_cands: {f['full_candidates']}")

        if f["sem_pass"] and not f["full_pass"]:
            # sem 层正确，full 层错误 → LLM 帮倒忙
            llm_hurt += 1
            print(f"    >>> LLM 帮倒忙！sem_top1 正确但被 LLM 换走")
        elif not f["sem_pass"] and f["expected_in_sem_rank"] is None:
            # expected 意图根本不在候选中 → 数据缺失
            data_missing += 1
            print(f"    >>> 数据缺失：expected 意图不在任何候选中")
        else:
            llm_neutral += 1
            print(f"    >>> 其他问题")

    print(f"\n  汇总:")
    print(f"    LLM 帮倒忙 (sem正确→full错误): {llm_hurt}")
    print(f"    数据缺失 (expected不在候选中): {data_missing}")
    print(f"    其他: {llm_neutral}")

    # ── 分析分数差分布 ──
    print(f"\n{'='*90}")
    print("LLM帮倒忙用例的分数差分布:")
    print(f"{'='*90}")
    for f in single_failures:
        if f["sem_pass"] and not f["full_pass"]:
            print(f"  {f['case_id']} gap={f['score_gap']:>5.1f}  top1={f['top1_score']}/{f['sem_top1']}  top2={f['top2_score']}  llm_triggered={f['llm_triggered']}")

    # ── 分析 ambiguous/multi 失败 ──
    amb_multi_failures = [f for f in failures if f["shape"] in ("ambiguous_intent", "multi_intent")]
    print(f"\n{'='*90}")
    print(f"ambiguous/multi 失败: {len(amb_multi_failures)} 条")
    print(f"{'='*90}")
    for f in amb_multi_failures:
        print(f"\n  {f['case_id']} [{f['shape']}] 「{f['query'][:30]}」")
        print(f"    expected: {f['expected']}")
        print(f"    sem_missing: {f.get('sem_missing', [])}")
        print(f"    full_missing: {f.get('full_missing', [])}")
        print(f"    sem_cands: {f.get('sem_candidates', [])}")
        print(f"    full_cands: {f.get('full_candidates', [])}")

    # ── 分析 non_task 失败 ──
    non_task_failures = [f for f in failures if f["shape"] == "non_task"]
    print(f"\n{'='*90}")
    print(f"non_task 失败: {len(non_task_failures)} 条")
    print(f"{'='*90}")
    for f in non_task_failures:
        print(f"  {f['case_id']} 「{f['query'][:30]}」 count={f['full_count']} cands={f['full_candidates']}")

    # ── 统计 LLM 触发情况 ──
    print(f"\n{'='*90}")
    print("LLM 触发统计 (全部用例):")
    print(f"{'='*90}")
    llm_trigger_count = 0
    llm_trigger_by_shape = {}
    for case in cases:
        query = case["query"]
        shape = case["intent_shape"]
        sem_cands = semantic_recall(adapter, subgraph, semantic, query)
        if len(sem_cands) <= 1:
            continue
        has_conf = _check_confusable_pairs(subgraph, sem_cands)
        if has_conf:
            llm_trigger_count += 1
            llm_trigger_by_shape[shape] = llm_trigger_by_shape.get(shape, 0) + 1

    print(f"  LLM 触发总次数: {llm_trigger_count}/{len(cases)}")
    for shape, count in sorted(llm_trigger_by_shape.items()):
        total = sum(1 for c in cases if c["intent_shape"] == shape)
        print(f"    {shape}: {count}/{total}")

    semantic.close()
    judge.close()


if __name__ == "__main__":
    main()
