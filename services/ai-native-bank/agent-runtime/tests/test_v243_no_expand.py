"""验证 V4-243:graph_expand=False + LLM with_sub 能否选对"""
import json, os, sys
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

rs = RecallSettings.from_env()
payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
adapter = V4GraphAdapter(payload, GRAPH_PATH, recall_settings=rs)
adapter.apply_recall_settings(rs)
subgraph = SubgraphReasoningV4(adapter, recall_settings=rs)
judge = LLMJudgeV4(recall_settings=rs)

dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
case = next(c for c in dataset["cases"] if c["case_id"] == "V4-243")
query = case["query"]
expected = [e["intent_id"] for e in case.get("expected_intents", [])]
print(f"case_id={case['case_id']}  query={query}  expected={expected}")

# graph_expand=False
candidates = adapter.recall(query, page_context={}, limit=10, graph_expand=False)
print(f"\ngraph_expand=False 召回候选 ({len(candidates)}):")
for i, c in enumerate(candidates[:5], 1):
    exp = ' <-- EXPECTED' if c.intent_id == expected[0] else ''
    print(f"  [{i}] {c.intent_id}  score={c.score}  title={c.title}{exp}")

# 构建子图上下文
contexts = subgraph.build_contexts(candidates[:5])

# LLM 裁决
print("\n调用 LLM 裁决 (graph_expand=False + with_sub)...")
result = judge.judge(query, candidates[:5], "single_intent", contexts)
print(f"\nLLM 结果:")
for s in result:
    print(f"  - {s.get('intent_id')}  confidence={s.get('confidence', 0)}")
    print(f"    reason: {s.get('reason', '')}")

selected_ids = [s.get("intent_id") for s in result]
passed = expected[0] in selected_ids
print(f"\n期望: {expected[0]}")
print(f"LLM 选择: {selected_ids}")
print(f"通过: {passed}")
