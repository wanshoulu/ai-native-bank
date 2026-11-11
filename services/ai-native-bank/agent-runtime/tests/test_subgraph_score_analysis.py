#!/usr/bin/env python
"""分析救回 vs 干扰 case 的 top1/top2 分数,找收窄 LLM 触发条件的依据"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from tests.test_eval_v4_semantic import semantic_recall

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

RESCUE = {"V4-205", "V4-206", "V4-211", "V4-222", "V4-243"}
INTERFERE = {"V4-121", "V4-129", "V4-130", "V4-138", "V4-224", "V4-215"}

def main():
    import json
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = [c for c in dataset["cases"] if c["case_id"] in RESCUE | INTERFERE]

    adapter = V4GraphAdapter.load(GRAPH_PATH)
    subgraph = SubgraphReasoningV4(adapter)
    semantic = SemanticRecallV4(adapter)

    print(f"{'case':8s} {'类型':6s} {'top1_score':>10s} {'top2_score':>10s} {'触发?':>6s} {'top1_id':40s} {'top2_id':40s}")
    print("-" * 130)

    for case in sorted(cases, key=lambda c: c["case_id"]):
        cid = case["case_id"]
        query = case["query"]
        expected = [e["intent_id"] for e in case.get("expected_intents", [])]

        cands = semantic_recall(adapter, subgraph, semantic, query)
        if not cands:
            continue
        sorted_cands = sorted(cands, key=lambda c: -c.score)
        top1 = sorted_cands[0]
        top2 = sorted_cands[1] if len(sorted_cands) > 1 else None
        top1_score = top1.score
        top2_score = top2.score if top2 else 0

        # 当前触发条件: top1<80 or top2>=60
        trigger = top1_score < 80 or top2_score >= 60
        # 收窄后: 只 top1<80
        trigger_narrow = top1_score < 80

        cat = "救回" if cid in RESCUE else "干扰"
        top1_mark = "✓" if top1.intent_id in expected else "✗"
        top2_mark = "✓" if top2 and top2.intent_id in expected else "✗"

        print(f"{cid:8s} {cat:6s} {top1_score:>10.1f} {top2_score:>10.1f} {'Y' if trigger else 'N':>6s} {top1.intent_id[:38]:40s}{'('+top1_mark+')':2s} {(top2.intent_id if top2 else '')[:38]:40s}{'('+top2_mark+')':2s}")

    print()
    print("分析:")
    print("  救回 case 的 top1 分数都 < 80 (弱匹配), 收窄到 top1<80 不影响救回")
    print("  干扰 case 的 top1 分数如果都 >= 80, 收窄到 top1<80 可避免干扰")

    semantic.close()

if __name__ == "__main__":
    main()
