"""验证 V4-021/022/158 三个修复 case — 复用 ab_compare 脚本的函数"""
import json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from app.semantic_recall_v4 import SemanticRecallV4
from app.llm_judge_v4 import LLMJudgeV4

GRAPH_PATH = ROOT / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = ROOT / "tests" / "intent_eval_v4_pilot.json"

# 复用 ab_compare 脚本
sys.path.insert(0, str(ROOT / "tests"))
from test_subgraph_ab_compare import (
    judge_with_subgraph, judge_without_subgraph, check_case,
    _STRONG_MATCH_THRESHOLD, _SUPER_STRONG_MATCH_THRESHOLD
)

def main():
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    target_ids = {"V4-021", "V4-022", "V4-158"}
    cases = [c for c in dataset["cases"] if c["case_id"] in target_ids]

    adapter = V4GraphAdapter.load(GRAPH_PATH)
    subgraph = SubgraphReasoningV4(adapter)
    semantic = SemanticRecallV4(adapter)
    judge = LLMJudgeV4()
    judge._adapter = adapter

    print(f"验证 {len(cases)} 个 case(复用 ab_compare 函数)")
    print(f"子图: {'✅' if subgraph.enabled else '❌'}  LLM: {'✅' if judge.enabled else '❌'}")
    print()

    for case in cases:
        case_id = case["case_id"]
        msg = case["query"]
        shape = case["intent_shape"]
        expected = [e["intent_id"] for e in case.get("expected_intents", [])]

        print(f"--- {case_id} query='{msg}' shape={shape} expected={expected}")

        # 词法召回(展示用)
        lex_cands = adapter.recall(msg, page_context={}, limit=12)
        print(f"  词法 top5: {[(c.intent_id.replace('ccb.channel.',''), c.score) for c in lex_cands[:5]]}")

        # Config A(无子图)
        cands_a = judge_without_subgraph(adapter, semantic, judge, msg, shape)
        passed_a = check_case(case, cands_a)
        print(f"  Config A(无子图) top5: {[(c.intent_id.replace('ccb.channel.',''), c.score) for c in cands_a[:5]]}")
        print(f"  Config A passed={passed_a}")

        # Config B(有子图)
        cands_b = judge_with_subgraph(adapter, subgraph, semantic, judge, msg, shape)
        passed_b = check_case(case, cands_b)
        print(f"  Config B(有子图) top5: {[(c.intent_id.replace('ccb.channel.',''), c.score) for c in cands_b[:5]]}")
        print(f"  Config B passed={passed_b}")
        print(f"  → {'✅ PASS' if passed_b else '❌ FAIL'}")
        print()

if __name__ == "__main__":
    main()
