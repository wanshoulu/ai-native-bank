"""验证 V4-243 LLM 消歧:graph_expand=True + with_sub,用 top12(跟 service.py 一致)
跑 3 次确认稳定性"""
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

    # graph_expand=True(默认),limit=20 捞全候选
    candidates = adapter.recall(query, page_context={}, limit=20, graph_expand=True)
    print(f"\n召回候选 ({len(candidates)}):")
    for i, c in enumerate(candidates[:12], 1):
        marker = " <-- expected" if c.intent_id in expected else ""
        print(f"  [{i}] {c.intent_id}  score={c.score}  title={c.title}{marker}")

    # 用 top12(跟 service.py 一致)
    top12 = sorted(candidates, key=lambda c: -c.score)[:12]
    print(f"\ntop12 (按 score 降序):")
    for i, c in enumerate(top12, 1):
        marker = " <-- expected" if c.intent_id in expected else ""
        print(f"  [{i}] {c.intent_id}  score={c.score}{marker}")

    # 构建子图上下文
    contexts = subgraph.build_contexts(top12)
    print("\n子图上下文 entry_path 检查:")
    for ctx in contexts:
        if ctx["intent_id"] in expected:
            print(f"  --- {ctx['intent_id']} ---")
            print(f"  entry_path: {ctx.get('entry_path', '') or '(空)'}")
            print(f"  disambiguation_hints: {(ctx.get('disambiguation_hints') or '')[:100]}")
            conf = ctx.get("confusable_with", [])
            for c in conf:
                print(f"    confusable: {c['title']}({c['intent_id']})")
                print(f"      entry_path: {c.get('entry_path', '') or '(空)'}")
                print(f"      differentiator: {c.get('differentiator', '') or '(空)'}")

    # 跑 3 次 LLM 裁决
    print("\n" + "=" * 70)
    print("LLM 裁决 (graph_expand=True + with_sub, top12) x3")
    print("=" * 70)
    pass_count = 0
    for run in range(3):
        result = judge.judge(query, top12, "single_intent", contexts)
        selected_ids = [s.get("intent_id") for s in result]
        passed = expected[0] in selected_ids if expected else False
        status = "PASS" if passed else "FAIL"
        if passed:
            pass_count += 1
        print(f"\n[run {run+1}] {status}")
        print(f"  selected: {selected_ids}")
        if not passed:
            for s in result[:2]:
                print(f"    - {s.get('intent_id')}  confidence={s.get('confidence', 0)}")
                print(f"      reason: {s.get('reason', '')[:120]}")

    print(f"\n通过率: {pass_count}/3")
    print(f"结论: {'graph_expand=True + with_sub 可用' if pass_count >= 2 else 'graph_expand=True + with_sub 不可用'}")

if __name__ == "__main__":
    asyncio.run(main())
