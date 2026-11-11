#!/usr/bin/env python
"""验证 ambiguous/multi 失败 case 在完整四层链路下的效果"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from app.llm_judge_v4 import LLMJudgeV4
from tests.test_eval_v4_semantic import (
    lexical_recall, subgraph_recall, semantic_recall, full_pipeline,
    check_case,
)

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

# +subgraph 失败的 ambiguous/multi case (不含 single_intent)
TARGET_IDS = {"V4-021", "V4-022", "V4-077", "V4-097", "V4-158", "V4-250"}

def main():
    import json
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = [c for c in dataset["cases"] if c["case_id"] in TARGET_IDS]

    adapter = V4GraphAdapter.load(GRAPH_PATH)
    subgraph = SubgraphReasoningV4(adapter)
    semantic = SemanticRecallV4(adapter)
    judge = LLMJudgeV4()
    judge._adapter = adapter

    print(f"子图: {subgraph.enabled}, 语义: {semantic.enabled}, LLM: {judge.enabled}")
    print(f"目标 case: {len(cases)} 个 (ambiguous/multi 失败 case)\n")

    rescue_cnt = 0
    interfere_cnt = 0
    for case in cases:
        cid = case["case_id"]
        query = case["query"]
        shape = case["intent_shape"]
        expected = [e["intent_id"] for e in case.get("expected_intents", [])]

        # 三层召回 (无 LLM)
        sem_cands = semantic_recall(adapter, subgraph, semantic, query)
        sem_result = check_case(case, sem_cands)
        sem_pass = sem_result["pass"]

        # 完整四层 (含 LLM)
        full_cands = full_pipeline(adapter, subgraph, semantic, judge, query, intent_shape=shape)
        full_result = check_case(case, full_cands)
        full_pass = full_result["pass"]

        status = "PASS" if full_pass else "FAIL"
        if not sem_pass and full_pass:
            rescue = "救回"
            rescue_cnt += 1
        elif sem_pass and not full_pass:
            rescue = "干扰"
            interfere_cnt += 1
        else:
            rescue = "无变化"

        print(f"{cid} {status} (三层:{'PASS' if sem_pass else 'FAIL'} → 四层:{'PASS' if full_pass else 'FAIL'}) {rescue}")
        print(f"  query: {query}  shape: {shape}")
        print(f"  expected: {expected}")
        print(f"  三层 cands: {sem_result.get('candidate_ids', [])}")
        print(f"  四层 cands: {full_result.get('candidate_ids', [])}")
        if full_result.get("missing"):
            print(f"  missing: {full_result['missing']}")
        print()

    print(f"汇总: 救回 {rescue_cnt}, 干扰 {interfere_cnt}, 无变化 {len(cases) - rescue_cnt - interfere_cnt}")

    semantic.close()
    judge.close()

if __name__ == "__main__":
    main()
