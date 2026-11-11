"""5 case LLM 消歧对照:原版(graph_expand=True, no_sub) vs 优化版(graph_expand=False, with_sub)"""
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
print(f"subgraph.enabled={subgraph.enabled}, judge.enabled={judge.enabled}")

dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

# 找 top1 不正确的 case (LLM 消歧有意义的 case)
target_cases = []
for case in dataset["cases"]:
    if case.get("intent_shape") != "single_intent":
        continue
    expected = [e["intent_id"] for e in case.get("expected_intents", [])]
    if not expected:
        continue
    candidates = adapter.recall(case["query"], page_context={}, limit=10, graph_expand=True)
    if not candidates:
        continue
    if candidates[0].intent_id != expected[0]:
        target_cases.append(case)
    if len(target_cases) >= 5:
        break

print(f"\n找到 {len(target_cases)} 个 top1 不正确的 case:")
for c in target_cases:
    print(f"  - {c['case_id']}: {c['query']}")

print("\n" + "=" * 80)
print("对照测试:原版(graph_expand=True, no_sub) vs 优化版(graph_expand=False, with_sub)")
print("=" * 80)

results = {"original": [], "optimized": []}

for case in target_cases:
    query = case["query"]
    expected = [e["intent_id"] for e in case.get("expected_intents", [])]
    print(f"\n[case {case['case_id']}] query={query}  expected={expected[0]}")

    # 原版: graph_expand=True, no_sub (LLM 不带子图上下文)
    cands_orig = adapter.recall(query, page_context={}, limit=10, graph_expand=True)[:5]
    try:
        result_orig = judge.judge(query, cands_orig, "single_intent", None)
        orig_sel = [s.get("intent_id") for s in result_orig]
        orig_pass = expected[0] in orig_sel
    except Exception as e:
        orig_sel = []
        orig_pass = False
        print(f"  原版 LLM 异常: {e}")

    # 优化版: graph_expand=False, with_sub (LLM 带优化版子图上下文)
    cands_opt = adapter.recall(query, page_context={}, limit=10, graph_expand=False)[:5]
    contexts = subgraph.build_contexts(cands_opt)
    try:
        result_opt = judge.judge(query, cands_opt, "single_intent", contexts)
        opt_sel = [s.get("intent_id") for s in result_opt]
        opt_pass = expected[0] in opt_sel
    except Exception as e:
        opt_sel = []
        opt_pass = False
        print(f"  优化版 LLM 异常: {e}")

    print(f"  原版(graph_expand=True, no_sub):   top5={[c.intent_id for c in cands_orig]} → LLM={orig_sel}  pass={orig_pass}")
    print(f"  优化版(graph_expand=False, with_sub): top5={[c.intent_id for c in cands_opt]} → LLM={opt_sel}  pass={opt_pass}")

    results["original"].append(orig_pass)
    results["optimized"].append(opt_pass)

print("\n" + "=" * 80)
print("汇总")
print("=" * 80)
orig_pass_count = sum(results["original"])
opt_pass_count = sum(results["optimized"])
print(f"原版 (graph_expand=True, no_sub):    {orig_pass_count}/{len(target_cases)} ({orig_pass_count/len(target_cases)*100:.1f}%)")
print(f"优化版 (graph_expand=False, with_sub): {opt_pass_count}/{len(target_cases)} ({opt_pass_count/len(target_cases)*100:.1f}%)")
print(f"差异: {opt_pass_count - orig_pass_count:+d}")
