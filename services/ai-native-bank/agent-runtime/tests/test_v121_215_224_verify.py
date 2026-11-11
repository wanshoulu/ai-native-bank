#!/usr/bin/env python
"""验证 V4-121/215/224 三个干扰 case 在图谱修复后是否消除干扰"""
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
    semantic_recall, full_pipeline, check_case
)

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

import json
dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
cases = {c["case_id"]: c for c in dataset["cases"]}

adapter = V4GraphAdapter.load(GRAPH_PATH)
subgraph = SubgraphReasoningV4(adapter)
semantic = SemanticRecallV4(adapter)
judge = LLMJudgeV4()
judge._adapter = adapter

print(f"subgraph: {subgraph.enabled}, semantic: {semantic.enabled}, judge: {judge.enabled}")
print(f"graph: {GRAPH_PATH.name}")
print("=" * 80)

target_ids = ["V4-121", "V4-215", "V4-224"]
for cid in target_ids:
    case = cases.get(cid)
    if not case:
        print(f"{cid}: NOT FOUND")
        continue
    query = case["query"]
    expected = [e["intent_id"] for e in case.get("expected_intents", [])]
    shape = case["intent_shape"]

    # 三层
    sem_cands = semantic_recall(adapter, subgraph, semantic, query)
    sem_top1 = sem_cands[0].intent_id if sem_cands else None
    sem_pass = sem_top1 in expected if expected else None

    # 四层
    full_cands = full_pipeline(adapter, subgraph, semantic, judge, query, intent_shape=shape)
    full_top1 = full_cands[0].intent_id if full_cands else None
    full_pass = full_top1 in expected if expected else None

    status = "✅消除" if (sem_pass and not full_pass) else ("✅保持" if (sem_pass and full_pass) else "❌干扰" if (sem_pass and not full_pass) else "?")
    print(f"\n{cid} 「{query}」")
    print(f"  expected: {expected}")
    print(f"  三层 top1: {sem_top1} ({'✓' if sem_pass else '✗'}) score={sem_cands[0].score if sem_cands else 0}")
    print(f"  四层 top1: {full_top1} ({'✓' if full_pass else '✗'})")
    print(f"  状态: {status}")
    # 候选列表
    print(f"  四层候选 top5: {[c.intent_id for c in full_cands[:5]]}")
