"""诊断 V4-243:LLM 到底收到什么 prompt,为什么选错"""
from __future__ import annotations
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

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

rs = RecallSettings.from_env()
payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
adapter = V4GraphAdapter(payload, GRAPH_PATH, recall_settings=rs)
adapter.apply_recall_settings(rs)
subgraph = SubgraphReasoningV4(adapter, recall_settings=rs)

dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
cases = dataset["cases"]

# 找 V4-243
target = None
for case in cases:
    if case["case_id"] == "V4-243":
        target = case
        break

if not target:
    print("V4-243 not found")
    sys.exit(1)

query = target["query"]
expected = [e["intent_id"] for e in target.get("expected_intents", [])]
print(f"case_id: {target['case_id']}")
print(f"query: {query}")
print(f"expected: {expected}")
print(f"intent_shape: {target.get('intent_shape')}")
print()

candidates = adapter.recall(query, page_context={}, limit=10)
print(f"召回候选 ({len(candidates)}):")
for i, c in enumerate(candidates[:8], 1):
    print(f"  [{i}] {c.intent_id}  score={c.score}  title={c.title}")

# 构建子图上下文
print()
print("=" * 80)
print("子图上下文 (LLM 实际收到的):")
print("=" * 80)
contexts = subgraph.build_contexts(candidates[:5])
for i, ctx in enumerate(contexts, 1):
    print(f"\n--- 候选 [{i}] {ctx['intent_id']} ---")
    print(f"title: {ctx.get('title', '')}")
    print(f"description: {ctx.get('description', '')}")
    print(f"disambiguation_hints: {ctx.get('disambiguation_hints') or '(空)'}")
    confusable = ctx.get("confusable_with", [])
    if confusable:
        print(f"confusable_with:")
        for c in confusable:
            print(f"  - {c['title']} ({c['intent_id']})")
            print(f"    desc: {c.get('description', '')[:100]}")
    else:
        print(f"confusable_with: (空)")
    siblings = ctx.get("siblings", [])
    if siblings:
        print(f"siblings: {[s['title'] for s in siblings]}")

# 看 expected intent 的 disambiguation_hints
print()
print("=" * 80)
print("期望意图的 disambiguation_hints:")
print("=" * 80)
for eid in expected:
    intent = adapter._intents_by_id.get(eid)
    if intent:
        print(f"\n{eid}:")
        print(f"  title: {intent.get('title', '')}")
        print(f"  description: {intent.get('description', '')}")
        print(f"  disambiguation_hints: {intent.get('disambiguationHints') or '(空)'}")
        conf = intent.get("confusableIntents", [])
        print(f"  confusableIntents: {conf}")
    else:
        print(f"\n{eid}: NOT IN GRAPH")
