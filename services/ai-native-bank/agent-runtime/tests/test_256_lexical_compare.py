"""256 case 词法召回对照: graph_expand=True(原版) vs graph_expand=False(优化版)"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import RecallSettings
from app.v4_graph_adapter import V4GraphAdapter

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

rs = RecallSettings.from_env()
payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
adapter = V4GraphAdapter(payload, GRAPH_PATH, recall_settings=rs)
adapter.apply_recall_settings(rs)

dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
cases = dataset["cases"]

def check_single(case, candidates) -> bool:
    expected = [e["intent_id"] for e in case.get("expected_intents", [])]
    if not expected or not candidates:
        return False
    return candidates[0].intent_id in expected

def check_multi(case, candidates) -> bool:
    expected = [e["intent_id"] for e in case.get("expected_intents", [])]
    if not expected:
        return False
    ids = {c.intent_id for c in candidates}
    return all(eid in ids for eid in expected)

def check_ambiguous(case, candidates) -> bool:
    expected = [e["intent_id"] for e in case.get("expected_intents", [])]
    if not expected:
        return False
    ids = {c.intent_id for c in candidates}
    found = [eid for eid in expected if eid in ids]
    return len(found) == len(expected) and len(found) >= 2

def check_non_task(case, candidates) -> bool:
    return len(candidates) == 0

def check(case, candidates) -> bool:
    shape = case.get("intent_shape")
    if shape == "single_intent":
        return check_single(case, candidates)
    elif shape == "multi_intent":
        return check_multi(case, candidates)
    elif shape == "ambiguous_intent":
        return check_ambiguous(case, candidates)
    elif shape == "non_task":
        return check_non_task(case, candidates)
    return False

# 对照: graph_expand=True vs graph_expand=False
pass_expand = 0
pass_no_expand = 0
flips_expand_to_no = 0  # expand 失败 -> no_expand 通过
flips_no_to_expand = 0  # no_expand 失败 -> expand 通过
both_pass = 0
both_fail = 0

for case in cases:
    query = case["query"]
    cands_expand = adapter.recall(query, page_context={}, limit=10, graph_expand=True)
    cands_no_expand = adapter.recall(query, page_context={}, limit=10, graph_expand=False)

    ok_expand = check(case, cands_expand)
    ok_no_expand = check(case, cands_no_expand)

    if ok_expand:
        pass_expand += 1
    if ok_no_expand:
        pass_no_expand += 1
    if ok_expand and ok_no_expand:
        both_pass += 1
    elif not ok_expand and not ok_no_expand:
        both_fail += 1
    elif ok_expand and not ok_no_expand:
        flips_no_to_expand += 1
    elif not ok_expand and ok_no_expand:
        flips_expand_to_no += 1

total = len(cases)
print("=" * 80)
print("256 case 词法召回对照: graph_expand=True(原版) vs graph_expand=False(优化版)")
print("=" * 80)
print(f"总数: {total}")
print(f"graph_expand=True  (原版):   {pass_expand}/{total} ({pass_expand/total*100:.1f}%)")
print(f"graph_expand=False (优化版): {pass_no_expand}/{total} ({pass_no_expand/total*100:.1f}%)")
print(f"差异: {pass_no_expand - pass_expand:+d}")
print()
print(f"both_pass: {both_pass}")
print(f"both_fail: {both_fail}")
print(f"expand 失败 -> no_expand 通过 (no_expand 更好): {flips_expand_to_no}")
print(f"no_expand 失败 -> expand 通过 (expand 更好): {flips_no_to_expand}")
