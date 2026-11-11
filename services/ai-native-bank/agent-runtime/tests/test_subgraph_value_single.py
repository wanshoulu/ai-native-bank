#!/usr/bin/env python
"""跑全部 single_intent case (89 个),验证 LLM judge 救回 vs 干扰"""
from __future__ import annotations
import sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from app.llm_judge_v4 import LLMJudgeV4
from tests.test_eval_v4_semantic import (
    semantic_recall, full_pipeline, check_case,
)

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

def main():
    import json
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = [c for c in dataset["cases"] if c["intent_shape"] == "single_intent"]

    adapter = V4GraphAdapter.load(GRAPH_PATH)
    subgraph = SubgraphReasoningV4(adapter)
    semantic = SemanticRecallV4(adapter)
    judge = LLMJudgeV4()
    judge._adapter = adapter

    print(f"single_intent case: {len(cases)} 个")
    print(f"子图: {subgraph.enabled}, 语义: {semantic.enabled}, LLM: {judge.enabled}\n")

    sem_pass_cnt = 0
    full_pass_cnt = 0
    rescue_cnt = 0
    interfere_cnt = 0
    interfere_cases = []
    rescue_cases = []

    t0 = time.time()
    for i, case in enumerate(cases):
        cid = case["case_id"]
        query = case["query"]

        # 三层召回 (无 LLM)
        sem_cands = semantic_recall(adapter, subgraph, semantic, query)
        sem_result = check_case(case, sem_cands)
        sem_pass = sem_result["pass"]

        # 完整四层 (含 LLM)
        full_cands = full_pipeline(adapter, subgraph, semantic, judge, query, intent_shape="single_intent")
        full_result = check_case(case, full_cands)
        full_pass = full_result["pass"]

        if sem_pass:
            sem_pass_cnt += 1
        if full_pass:
            full_pass_cnt += 1

        if not sem_pass and full_pass:
            rescue_cnt += 1
            rescue_cases.append(cid)
        elif sem_pass and not full_pass:
            interfere_cnt += 1
            interfere_cases.append({
                "case_id": cid,
                "query": query,
                "expected": [e["intent_id"] for e in case.get("expected_intents", [])],
                "sem_top1": sem_result.get("top1"),
                "full_top1": full_result.get("top1"),
            })

        # case 间小延迟,避免 LLM 限流
        time.sleep(0.3)

        if (i + 1) % 20 == 0:
            elapsed = time.time() - t0
            print(f"  进度: {i+1}/{len(cases)} ({elapsed:.1f}s) 救回:{rescue_cnt} 干扰:{interfere_cnt}")

    elapsed = time.time() - t0
    print(f"\n{'='*70}")
    print(f"single_intent 汇总 ({len(cases)} case, {elapsed:.1f}s)")
    print(f"{'='*70}")
    print(f"三层 (无 LLM):  {sem_pass_cnt}/{len(cases)} PASS")
    print(f"四层 (含 LLM):  {full_pass_cnt}/{len(cases)} PASS")
    print(f"救回: {rescue_cnt}  {rescue_cases}")
    print(f"干扰: {interfere_cnt}")
    for ic in interfere_cases:
        print(f"  {ic['case_id']} 「{ic['query']}」 expected:{ic['expected']} 三层top1:{ic['sem_top1']} 四层top1:{ic['full_top1']}")

    semantic.close()
    judge.close()

if __name__ == "__main__":
    main()
