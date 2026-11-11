"""模拟方案 I: LLM 看到的 top5 = 原始召回 top3 + 扩召 top2(去重)
对比:graph_expand=True(原版) vs 方案 I
"""
import json, sys
from pathlib import Path
from collections import Counter

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

def get_expected_ids(case):
    return [e["intent_id"] for e in case.get("expected_intents", [])]

def check(case, candidates) -> bool:
    shape = case.get("intent_shape")
    expected = get_expected_ids(case)
    if not expected:
        return len(candidates) == 0 if shape == "non_task" else False
    if shape == "single_intent":
        return candidates and candidates[0].intent_id in expected
    elif shape == "multi_intent":
        ids = {c.intent_id for c in candidates}
        return all(eid in ids for eid in expected)
    elif shape == "ambiguous_intent":
        ids = {c.intent_id for c in candidates}
        found = [eid for eid in expected if eid in ids]
        return len(found) == len(expected) and len(found) >= 2
    elif shape == "non_task":
        return len(candidates) == 0
    return False

def plan_i_top5(query, original_top_n=3, expand_top_n=2):
    """方案 I: 原始召回 top3 + 扩召 top2(去重)"""
    cands_no_expand = adapter.recall(query, page_context={}, limit=10, graph_expand=False)
    cands_expand = adapter.recall(query, page_context={}, limit=10, graph_expand=True)

    original_ids = {c.intent_id for c in cands_no_expand}
    original_top = cands_no_expand[:original_top_n]
    expand_only = [c for c in cands_expand if c.intent_id not in original_ids][:expand_top_n]

    # 合并去重,按 score 排序
    merged = list(original_top) + list(expand_only)
    seen = set()
    deduped = []
    for c in merged:
        if c.intent_id not in seen:
            deduped.append(c)
            seen.add(c.intent_id)
    return deduped[:5]

# 对照
pass_expand = 0
pass_plan_i = 0
both_pass = 0
both_fail = 0
expand_only_pass = 0
plan_i_only_pass = 0

# 详细分析
plan_i_keeps_expand = 0  # expand 通过, plan_i 也通过
plan_i_loses_expand = 0  # expand 通过, plan_i 失败
plan_i_saves_no_expand = 0  # expand 失败, plan_i 通过

for case in cases:
    query = case["query"]
    cands_expand = adapter.recall(query, page_context={}, limit=10, graph_expand=True)
    cands_plan_i = plan_i_top5(query)

    ok_expand = check(case, cands_expand)
    ok_plan_i = check(case, cands_plan_i)

    if ok_expand:
        pass_expand += 1
    if ok_plan_i:
        pass_plan_i += 1
    if ok_expand and ok_plan_i:
        both_pass += 1
    elif not ok_expand and not ok_plan_i:
        both_fail += 1
    elif ok_expand and not ok_plan_i:
        expand_only_pass += 1
        plan_i_loses_expand += 1
    elif not ok_expand and ok_plan_i:
        plan_i_only_pass += 1
        plan_i_saves_no_expand += 1

total = len(cases)
print("=" * 80)
print("方案 I 模拟: 原始召回 top3 + 扩召 top2(去重)")
print("=" * 80)
print(f"总数: {total}")
print(f"graph_expand=True (原版): {pass_expand}/{total} ({pass_expand/total*100:.1f}%)")
print(f"方案 I (top3+top2):       {pass_plan_i}/{total} ({pass_plan_i/total*100:.1f}%)")
print(f"差异: {pass_plan_i - pass_expand:+d}")
print()
print(f"both_pass: {both_pass}")
print(f"both_fail: {both_fail}")
print(f"expand 通过, plan_i 失败 (plan_i 丢失): {plan_i_loses_expand}")
print(f"expand 失败, plan_i 通过 (plan_i 救回): {plan_i_saves_no_expand}")
print()

# 分析 plan_i 丢失的 case
if plan_i_loses_expand > 0:
    print("=" * 80)
    print(f"分析 {plan_i_loses_expand} 个 plan_i 丢失的 case:")
    print("=" * 80)
    for case in cases:
        query = case["query"]
        cands_expand = adapter.recall(query, page_context={}, limit=10, graph_expand=True)
        cands_plan_i = plan_i_top5(query)
        ok_expand = check(case, cands_expand)
        ok_plan_i = check(case, cands_plan_i)
        if ok_expand and not ok_plan_i:
            expected = get_expected_ids(case)
            print(f"  query: {query[:40]}")
            print(f"  expected: {expected}")
            print(f"  expand_top5: {[(c.intent_id, c.score) for c in cands_expand[:5]]}")
            print(f"  plan_i_top5: {[(c.intent_id, c.score) for c in cands_plan_i[:5]]}")
            # 找到正确答案在 expand 中的位置
            for i, c in enumerate(cands_expand[:10]):
                if c.intent_id in expected:
                    print(f"  正确答案在 expand 第 {i+1} 位 (score={c.score})")
                    break
            print()

# 分析 plan_i 救回的 case
if plan_i_saves_no_expand > 0:
    print("=" * 80)
    print(f"分析 {plan_i_saves_no_expand} 个 plan_i 救回的 case:")
    print("=" * 80)
    for case in cases:
        query = case["query"]
        cands_expand = adapter.recall(query, page_context={}, limit=10, graph_expand=True)
        cands_plan_i = plan_i_top5(query)
        ok_expand = check(case, cands_expand)
        ok_plan_i = check(case, cands_plan_i)
        if not ok_expand and ok_plan_i:
            expected = get_expected_ids(case)
            print(f"  query: {query[:40]}")
            print(f"  expected: {expected}")
            print(f"  expand_top5: {[(c.intent_id, c.score) for c in cands_expand[:5]]}")
            print(f"  plan_i_top5: {[(c.intent_id, c.score) for c in cands_plan_i[:5]]}")
            print()
