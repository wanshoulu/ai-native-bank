"""验证 V4-243 LLM 消歧正确(优化版:父链路 + 入口路径 + confusable differentiator)"""
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

async def main():
    rs = RecallSettings.from_env()
    payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    adapter = V4GraphAdapter(payload, GRAPH_PATH, recall_settings=rs)
    adapter.apply_recall_settings(rs)
    subgraph = SubgraphReasoningV4(adapter, recall_settings=rs)
    judge = LLMJudgeV4(recall_settings=rs)
    print(f"subgraph.enabled={subgraph.enabled}, judge.enabled={judge.enabled}")

    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    case = next(c for c in dataset["cases"] if c["case_id"] == "V4-243")
    query = case["query"]
    expected = [e["intent_id"] for e in case.get("expected_intents", [])]
    print(f"\ncase_id={case['case_id']}")
    print(f"query={query}")
    print(f"expected={expected}")

    candidates = adapter.recall(query, page_context={}, limit=10)
    print(f"\n召回候选 ({len(candidates)}):")
    for i, c in enumerate(candidates[:5], 1):
        print(f"  [{i}] {c.intent_id}  score={c.score}  title={c.title}")

    # 构建子图上下文
    contexts = subgraph.build_contexts(candidates[:5])
    print("\n子图上下文 (LLM 收到的):")
    print("=" * 70)
    for i, ctx in enumerate(contexts, 1):
        print(f"\n--- 候选 [{i}] {ctx['intent_id']} ---")
        print(f"  title: {ctx.get('title', '')}")
        print(f"  entry_path: {ctx.get('entry_path', '') or '(空)'}")
        print(f"  description: {ctx.get('description', '')}")
        print(f"  disambiguation_hints: {ctx.get('disambiguation_hints') or '(空)'}")
        conf = ctx.get("confusable_with", [])
        if conf:
            print(f"  confusable_with:")
            for c in conf:
                print(f"    - {c['title']}({c['intent_id']})")
                print(f"      entry_path: {c.get('entry_path', '') or '(空)'}")
                print(f"      differentiator: {c.get('differentiator', '') or '(空)'}")

    # 调用 LLM 裁决 with_sub
    print("\n" + "=" * 70)
    print("调用 LLM 裁决 (with_sub,优化版)...")
    print("=" * 70)
    result = judge.judge(query, candidates[:5], "single_intent", contexts)
    print(f"\nLLM 结果:")
    print(f"  selected:")
    for s in result:
        print(f"    - {s.get('intent_id')}  confidence={s.get('confidence', 0)}")
        print(f"      reason: {s.get('reason', '')}")

    selected_ids = [s.get("intent_id") for s in result]
    passed = expected[0] in selected_ids if expected else False
    print(f"\n期望: {expected[0]}")
    print(f"LLM 选择: {selected_ids}")
    print(f"通过: {passed}")

if __name__ == "__main__":
    asyncio.run(main())
