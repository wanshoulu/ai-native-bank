#!/usr/bin/env python
"""快速验证:5 个 single_intent 失败 case 在完整四层链路下的救回效果"""
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
)

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

# 5 个 single_intent 失败 case
TARGET_IDS = {"V4-205", "V4-206", "V4-211", "V4-222", "V4-243"}

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
    print(f"目标 case: {len(cases)} 个\n")

    for case in cases:
        cid = case["case_id"]
        query = case["query"]
        expected = [e["intent_id"] for e in case.get("expected_intents", [])]

        # 三层召回 (无 LLM)
        sem_cands = semantic_recall(adapter, subgraph, semantic, query)
        sem_top1 = sem_cands[0].intent_id if sem_cands else None
        sem_pass = sem_top1 in expected

        # 完整四层 (含 LLM)
        full_cands = full_pipeline(adapter, subgraph, semantic, judge, query, intent_shape="single_intent")
        full_top1 = full_cands[0].intent_id if full_cands else None
        full_pass = full_top1 in expected

        # 子图拉入了哪些候选
        lex_cands = lexical_recall(adapter, query)
        lex_ids = [c.intent_id for c in lex_cands]
        sub_extra = [c.intent_id for c in sem_cands if c.intent_id not in lex_ids]

        status = "PASS" if full_pass else "FAIL"
        rescue = "救回" if (not sem_pass and full_pass) else ("无变化" if sem_pass == full_pass else "干扰")
        print(f"{cid} {status} (三层:{'PASS' if sem_pass else 'FAIL'} → 四层:{'PASS' if full_pass else 'FAIL'}) {rescue}")
        print(f"  query: {query}")
        print(f"  expected: {expected}")
        print(f"  三层 top1: {sem_top1}")
        print(f"  四层 top1: {full_top1}")
        print(f"  子图新增: {sub_extra}")
        print(f"  候选: {[c.intent_id for c in sem_cands[:8]]}")
        print()

    semantic.close()
    judge.close()

if __name__ == "__main__":
    main()
