"""诊断 79 个依赖 graph_expand 命中的 case:
1. 扩召候选 score 分布
2. 原始召回 top1 score 分布
3. 方案 A(扩召候选 score cap = original_top1 - 1)的影响:多少 case 扩召候选会跌出 top5
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

# 方案 A 模拟:扩召候选 score cap = original_top1 - 1
def simulate_plan_a(case):
    """返回 (original_top1_score, expand_pass, plan_a_pass, expand_cands_scores, plan_a_cands_scores)"""
    query = case["query"]
    # 原始召回(graph_expand=False)
    cands_no_expand = adapter.recall(query, page_context={}, limit=10, graph_expand=False)
    original_top1_score = cands_no_expand[0].score if cands_no_expand else 0
    original_ids = {c.intent_id for c in cands_no_expand}

    # 原版 graph_expand=True
    cands_expand = adapter.recall(query, page_context={}, limit=10, graph_expand=True)
    expand_pass = check(case, cands_expand)

    # 方案 A: 对 cands_expand 中的扩召候选(不在 original_ids 中)score cap = original_top1 - 1
    cap = max(original_top1_score - 1, 0)
    plan_a_cands = []
    for c in cands_expand:
        if c.intent_id in original_ids:
            plan_a_cands.append(c)  # 原始召回,不改
        else:
            # 扩召候选,score cap
            if c.score > cap:
                # 创建新的候选对象,score = cap
                from app.intent_graph import IntentCandidate
                new_c = IntentCandidate(
                    intent_id=c.intent_id,
                    score=cap,
                    evidence=c.evidence + ["plan_a_cap"],
                    scope_in=c.scope_in,
                    scope_out=c.scope_out,
                )
                plan_a_cands.append(new_c)
            else:
                plan_a_cands.append(c)
    # 重新排序取 top5
    plan_a_cands = sorted(plan_a_cands, key=lambda x: (-x.semantic_priority, -x.score, x.intent_id))[:5]
    plan_a_pass = check(case, plan_a_cands)

    return {
        "original_top1_score": original_top1_score,
        "expand_pass": expand_pass,
        "plan_a_pass": plan_a_pass,
        "expand_top5_scores": [(c.intent_id, c.score, c.intent_id not in original_ids) for c in cands_expand[:5]],
        "plan_a_top5_scores": [(c.intent_id, c.score) for c in plan_a_cands[:5]],
        "expected": get_expected_ids(case),
    }

# 统计
expand_only_pass = []  # expand 通过, no_expand 失败 (79 个)
no_expand_only_pass = []  # expand 失败, no_expand 通过 (1 个)
both_pass = 0
both_fail = 0

for case in cases:
    query = case["query"]
    cands_expand = adapter.recall(query, page_context={}, limit=10, graph_expand=True)
    cands_no_expand = adapter.recall(query, page_context={}, limit=10, graph_expand=False)
    ok_expand = check(case, cands_expand)
    ok_no_expand = check(case, cands_no_expand)

    if ok_expand and not ok_no_expand:
        expand_only_pass.append(case)
    elif not ok_expand and ok_no_expand:
        no_expand_only_pass.append(case)
    elif ok_expand and ok_no_expand:
        both_pass += 1
    else:
        both_fail += 1

print("=" * 80)
print(f"分类统计:")
print(f"  both_pass: {both_pass}")
print(f"  both_fail: {both_fail}")
print(f"  expand_only_pass (expand 更好): {len(expand_only_pass)}")
print(f"  no_expand_only_pass (no_expand 更好): {len(no_expand_only_pass)}")
print()

# 分析 79 个 expand_only_pass case
print("=" * 80)
print(f"分析 {len(expand_only_pass)} 个 expand_only_pass case (graph_expand 贡献的):")
print("=" * 80)

# 这些 case 中,扩召候选(正确答案)的 score 分布
expand_correct_scores = []
original_top1_scores_when_expand_wins = []
plan_a_lose_count = 0  # 方案 A 会丢失的 case 数
plan_a_keep_count = 0  # 方案 A 保住的 case 数

for case in expand_only_pass:
    result = simulate_plan_a(case)
    expected = set(result["expected"])
    # 找到扩召候选中正确答案的 score
    for iid, score, is_expand in result["expand_top5_scores"]:
        if iid in expected and is_expand:
            expand_correct_scores.append(score)
            original_top1_scores_when_expand_wins.append(result["original_top1_score"])
            break
    # 方案 A 是否保住
    if result["plan_a_pass"]:
        plan_a_keep_count += 1
    else:
        plan_a_lose_count += 1

print(f"扩召正确答案 score 分布: {Counter(expand_correct_scores).most_common()}")
print(f"原始召回 top1 score 分布: {Counter(original_top1_scores_when_expand_wins).most_common()}")
print()
print(f"方案 A (扩召 cap = original_top1 - 1) 影响:")
print(f"  保住 (方案 A 通过): {plan_a_keep_count}")
print(f"  丢失 (方案 A 失败): {plan_a_lose_count}")
print()

# 分析 1 个 no_expand_only_pass case (V4-243 类)
print("=" * 80)
print(f"分析 {len(no_expand_only_pass)} 个 no_expand_only_pass case (graph_expand 挤掉正确答案的):")
print("=" * 80)
for case in no_expand_only_pass:
    result = simulate_plan_a(case)
    print(f"  query: {case['query'][:50]}")
    print(f"  expected: {result['expected']}")
    print(f"  original_top1_score: {result['original_top1_score']}")
    print(f"  expand_top5 (iid, score, is_expand): {result['expand_top5_scores']}")
    print(f"  plan_a_top5 (iid, score): {result['plan_a_top5_scores']}")
    print(f"  expand_pass: {result['expand_pass']}, plan_a_pass: {result['plan_a_pass']}")
    print()

# 总结
print("=" * 80)
print("方案 A 总结:")
total_expand = both_pass + len(expand_only_pass)
total_plan_a = both_pass + plan_a_keep_count + (1 if no_expand_only_pass and simulate_plan_a(no_expand_only_pass[0])["plan_a_pass"] else 0)
print(f"  原版 graph_expand=True: {total_expand}/256 ({total_expand/256*100:.1f}%)")
print(f"  方案 A: {total_plan_a}/256 ({total_plan_a/256*100:.1f}%)")
print(f"  差异: {total_plan_a - total_expand:+d}")
