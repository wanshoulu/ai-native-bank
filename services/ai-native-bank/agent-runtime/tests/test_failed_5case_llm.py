"""5 个词法召回失败的 single_intent case,LLM 消歧救回测试
用 graph_expand=True + with_sub + top12(跟 service.py 一致)"""
from __future__ import annotations
import json, os, sys, asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env_path = Path(r"d:\project\vscode\.env")
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("QWEN_KEY=") and not line.startswith("#"):
            os.environ["QWEN_KEY"] = line[len("QWEN_KEY="):]
            break

from app.config import RecallSettings
from app.v4_graph_adapter import V4GraphAdapter
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from app.llm_judge_v4 import LLMJudgeV4

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

FAILED_CASE_IDS = ["V4-205", "V4-206", "V4-211", "V4-222", "V4-243"]

async def main():
    rs = RecallSettings.from_env()
    payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    adapter = V4GraphAdapter(payload, GRAPH_PATH, recall_settings=rs)
    adapter.apply_recall_settings(rs)
    subgraph = SubgraphReasoningV4(adapter, recall_settings=rs)
    judge = LLMJudgeV4(recall_settings=rs)
    print(f"subgraph.enabled={subgraph.enabled}, judge.enabled={judge.enabled}")

    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = [c for c in dataset["cases"] if c["case_id"] in FAILED_CASE_IDS]

    print(f"\n测试 {len(cases)} 个词法召回失败的 single_intent case")
    print("=" * 80)

    pass_count = 0
    for case in cases:
        case_id = case["case_id"]
        query = case["query"]
        expected = [e["intent_id"] for e in case.get("expected_intents", [])]

        # graph_expand=True, limit=20
        candidates = adapter.recall(query, page_context={}, limit=20, graph_expand=True)
        top12 = sorted(candidates, key=lambda c: -c.score)[:12]

        # 检查正确答案是否在 top12 内
        expected_in_top12 = any(c.intent_id in expected for c in top12)

        # 构建子图上下文
        contexts = subgraph.build_contexts(top12)

        # LLM 裁决
        result = judge.judge(query, top12, "single_intent", contexts)
        selected_ids = [s.get("intent_id") for s in result]
        passed = expected[0] in selected_ids if expected else False

        status = "PASS" if passed else "FAIL"
        if passed:
            pass_count += 1

        print(f"\n[{case_id}] {status}")
        print(f"  query: {query}")
        print(f"  expected: {expected}")
        print(f"  词法 top1: {top12[0].intent_id} (score={top12[0].score})")
        print(f"  正确答案在 top12: {'是' if expected_in_top12 else '否'}")
        if expected_in_top12:
            for i, c in enumerate(top12, 1):
                if c.intent_id in expected:
                    print(f"    正确答案排在第 {i} 位 (score={c.score})")
                    break
        print(f"  LLM 选择: {selected_ids}")
        if not passed:
            for s in result[:2]:
                print(f"    - {s.get('intent_id')}  confidence={s.get('confidence', 0)}")
                print(f"      reason: {s.get('reason', '')[:120]}")

    print("\n" + "=" * 80)
    print(f"LLM 消歧救回测试: {pass_count}/{len(cases)} 通过")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(main())
