#!/usr/bin/env python
"""V4 测试集召回验证脚本

读取 intent_eval_v4_pilot.json 测试集，对每条用例执行召回，
按 intent_shape / difficulty 分类统计通过率，并输出详细报告。

同时对比 fixed vs buggy（消歧元数据清零）的效果差异。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # agent-runtime/
sys.path.insert(0, str(ROOT))

from app.intent_shape import analyze_intent_shape  # noqa: E402
from app.v4_graph_adapter import V4GraphAdapter  # noqa: E402

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"


def make_buggy_adapter(graph_path: Path) -> V4GraphAdapter:
    """模拟修复前：将所有 confusableIntents 和 disambiguationHints 清零。"""
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    tree = payload.get("intentTree", [])
    for node in tree:
        if node.get("node_role") != "leaf":
            continue
        for scene in node.get("scenes", []):
            sd = scene.get("semantic_definition", {})
            if "confusableIntents" in sd:
                sd["confusableIntents"] = []
            if "disambiguationHints" in sd:
                sd["disambiguationHints"] = None
    return V4GraphAdapter(payload, graph_path)


def run_recall(adapter: V4GraphAdapter, message: str, limit: int = 10):
    return adapter.recall(message, page_context={}, limit=limit)


def check_single(case, candidates) -> dict:
    """单意图：检查 top1 和 expected_in_candidates"""
    expected_ids = [e["intent_id"] for e in case.get("expected_intents", [])]
    candidate_ids = [c.intent_id for c in candidates]
    top1 = candidate_ids[0] if candidate_ids else None

    return {
        "top1_match": top1 in expected_ids if expected_ids else None,
        "in_candidates": any(eid in candidate_ids for eid in expected_ids) if expected_ids else None,
        "top1": top1,
        "candidate_ids": candidate_ids[:5],
    }


def check_multi(case, candidates) -> dict:
    """多意图：检查所有 expected intents 是否在候选集中"""
    expected_ids = [e["intent_id"] for e in case.get("expected_intents", [])]
    candidate_ids = [c.intent_id for c in candidates]
    found = [eid for eid in expected_ids if eid in candidate_ids]
    return {
        "all_in_candidates": len(found) == len(expected_ids) if expected_ids else None,
        "found_count": len(found),
        "expected_count": len(expected_ids),
        "missing": [eid for eid in expected_ids if eid not in candidate_ids],
        "candidate_ids": candidate_ids[:5],
    }


def check_ambiguous(case, candidates) -> dict:
    """模糊意图：检查所有 expected intents 是否都在候选集中（不能只命中1个）"""
    expected_ids = [e["intent_id"] for e in case.get("expected_intents", [])]
    candidate_ids = [c.intent_id for c in candidates]
    found = [eid for eid in expected_ids if eid in candidate_ids]
    return {
        "candidates_in_set": len(found) == len(expected_ids) if expected_ids else None,
        "found_count": len(found),
        "expected_count": len(expected_ids),
        "missing": [eid for eid in expected_ids if eid not in candidate_ids],
        "candidate_ids": candidate_ids[:5],
    }


def check_non_task(case, candidates) -> dict:
    """非任务：期望零召回"""
    return {
        "zero_recall": len(candidates) == 0,
        "recall_count": len(candidates),
    }


def main():
    print("=" * 80)
    print("V4 测试集召回验证")
    print(f"测试集: {DATASET_PATH}")
    print(f"图谱:   {GRAPH_PATH}")
    print("=" * 80)

    # 加载测试集
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    print(f"用例总数: {len(cases)}")

    # 加载适配器
    fixed_adapter = V4GraphAdapter.load(GRAPH_PATH)
    buggy_adapter = make_buggy_adapter(GRAPH_PATH)

    # 统计
    stats = {
        "fixed": {"pass": 0, "fail": 0, "partial": 0, "total": 0},
        "buggy": {"pass": 0, "fail": 0, "partial": 0, "total": 0},
    }

    detail_lines = []

    # 候选集对比数据收集
    candidate_diffs = []

    # 构建 confusableIntents 相关 intent_id 集合（V4 节点 ID → V2 intent_id 映射后）
    # 包含所有作为 confusableIntents 源或目标的 intent_id
    confusable_related_ids: set[str] = set()
    for _intent in fixed_adapter._intents:
        _confs = _intent.get("confusableIntents", [])
        if _confs:
            confusable_related_ids.add(_intent["id"])  # 源 intent
            for _conf in _confs:
                _mapped = fixed_adapter._v4_to_v2.get(_conf, _conf)
                confusable_related_ids.add(_mapped)  # 目标 intent

    for case in cases:
        case_id = case["case_id"]
        query = case["query"]
        shape = case["intent_shape"]
        difficulty = case["difficulty"]
        domain = case.get("domain", "")

        # intent_shape 分析
        shape_info = analyze_intent_shape(query)

        # fixed 召回
        fixed_candidates = run_recall(fixed_adapter, query)
        # buggy 召回
        buggy_candidates = run_recall(buggy_adapter, query)

        # 检查
        if shape == "single_intent":
            fixed_result = check_single(case, fixed_candidates)
            buggy_result = check_single(case, buggy_candidates)
            fixed_pass = fixed_result["top1_match"]
            buggy_pass = buggy_result["top1_match"]
        elif shape == "multi_intent":
            fixed_result = check_multi(case, fixed_candidates)
            buggy_result = check_multi(case, buggy_candidates)
            fixed_pass = fixed_result["all_in_candidates"]
            buggy_pass = buggy_result["all_in_candidates"]
        elif shape == "ambiguous_intent":
            fixed_result = check_ambiguous(case, fixed_candidates)
            buggy_result = check_ambiguous(case, buggy_candidates)
            fixed_pass = fixed_result["candidates_in_set"]
            buggy_pass = buggy_result["candidates_in_set"]
        else:  # non_task
            fixed_result = check_non_task(case, fixed_candidates)
            buggy_result = check_non_task(case, buggy_candidates)
            fixed_pass = fixed_result["zero_recall"]
            buggy_pass = buggy_result["zero_recall"]

        # 统计
        for label, result, pass_val in [("fixed", fixed_result, fixed_pass), ("buggy", buggy_result, buggy_pass)]:
            stats[label]["total"] += 1
            if pass_val is True:
                stats[label]["pass"] += 1
            elif pass_val is False:
                stats[label]["fail"] += 1
            else:
                stats[label]["partial"] += 1

        # 状态符号
        def status_symbol(pass_val):
            if pass_val is True:
                return "✅"
            elif pass_val is False:
                return "❌"
            return "➖"

        fixed_sym = status_symbol(fixed_pass)
        buggy_sym = status_symbol(buggy_pass)

        # 变化检测
        if fixed_pass is True and buggy_pass is False:
            change = "📈 改善"
        elif fixed_pass is False and buggy_pass is True:
            change = "📉 回退"
        else:
            change = "➖ 无变化"

        # 输出详情
        fixed_top = fixed_result.get("top1") or fixed_result.get("candidate_ids", ["(无)"])
        if isinstance(fixed_top, list):
            fixed_top_str = fixed_top[0] if fixed_top else "(无)"
        else:
            fixed_top_str = fixed_top or "(无)"

        detail_lines.append(
            f"{case_id:8s} {shape:18s} {difficulty:6s} {fixed_sym}{buggy_sym} {change:10s} 「{query[:25]:25s}」 fixed_top1={fixed_top_str:35s}"
        )

        # 打印详细
        print(f"\n{'─' * 80}")
        print(f"[{case_id}] {shape}/{difficulty} 「{query}」")
        print(f"  shape_hint={shape_info.shape_hint}  segments={list(shape_info.segments)}")
        print(f"  expected: {[e['intent_id'] for e in case.get('expected_intents', [])]}")
        print(f"  fixed:  {fixed_sym}  top1={fixed_result.get('top1','—')}  candidates={fixed_result.get('candidate_ids', [])[:3]}")
        print(f"  buggy:  {buggy_sym}  top1={buggy_result.get('top1','—')}  candidates={buggy_result.get('candidate_ids', [])[:3]}")
        print(f"  {change}")

        # 收集完整候选 ID 用于对比分析
        fixed_full_ids = [c.intent_id for c in fixed_candidates]
        buggy_full_ids = [c.intent_id for c in buggy_candidates]
        fixed_set = set(fixed_full_ids)
        buggy_set = set(buggy_full_ids)
        fixed_only = fixed_set - buggy_set
        buggy_only = buggy_set - fixed_set
        if fixed_only or buggy_only:
            candidate_diffs.append({
                "case_id": case_id,
                "query": query,
                "shape": shape,
                "domain": domain,
                "fixed_only": sorted(fixed_only),
                "buggy_only": sorted(buggy_only),
                "common": sorted(fixed_set & buggy_set),
            })

    # 汇总
    print("\n" + "=" * 80)
    print("汇总")
    print("=" * 80)

    print(f"\n{'用例':10s} {'类型':20s} {'难度':8s} {'fixed':6s} {'buggy':6s} {'变化':10s} {'查询':25s} {'fixed top1'}")
    print("─" * 120)
    for line in detail_lines:
        print(line)

    print("\n" + "─" * 60)
    print(f"{'':12s} {'通过':>6s} {'失败':>6s} {'跳过':>6s} {'总数':>6s} {'通过率':>8s}")
    print("─" * 60)
    for label in ("fixed", "buggy"):
        s = stats[label]
        rate = s["pass"] / s["total"] * 100 if s["total"] else 0
        print(f"{label:12s} {s['pass']:>6d} {s['fail']:>6d} {s['partial']:>6d} {s['total']:>6d} {rate:>7.1f}%")

    # 按 shape 分组统计
    print(f"\n按 intent_shape 分组:")
    for shape_name in ("single_intent", "multi_intent", "ambiguous_intent", "non_task"):
        shape_cases = [c for c in cases if c["intent_shape"] == shape_name]
        if not shape_cases:
            continue
        # 重新统计这个 shape
        pass_count = 0
        for i, case in enumerate(shape_cases):
            # 使用前面已计算的 stats — 简单重新检查
            if shape_name == "single_intent":
                r = check_single(case, run_recall(fixed_adapter, case["query"]))
                if r["top1_match"]:
                    pass_count += 1
            elif shape_name == "multi_intent":
                r = check_multi(case, run_recall(fixed_adapter, case["query"]))
                if r["all_in_candidates"]:
                    pass_count += 1
            elif shape_name == "ambiguous_intent":
                r = check_ambiguous(case, run_recall(fixed_adapter, case["query"]))
                if r["candidates_in_set"]:
                    pass_count += 1
            else:
                r = check_non_task(case, run_recall(fixed_adapter, case["query"]))
                if r["zero_recall"]:
                    pass_count += 1
        print(f"  {shape_name:20s}: {pass_count}/{len(shape_cases)} 通过")

    # ── 候选集对比分析 ──
    print("\n" + "=" * 80)
    print("候选集对比分析 (fixed vs buggy)")
    print("=" * 80)

    diff_count = len(candidate_diffs)
    print(f"\n候选集有差异的用例数: {diff_count} / {len(cases)}")

    total_fixed_extra = 0  # fixed 比 buggy 多召回的候选总数
    confusable_count = 0  # 属于 confusableIntents 相关的候选数
    cross_domain_count = 0  # 属于跨域的候选数
    both_count = 0  # 同时满足 confusableIntents 相关且跨域的候选数

    for diff in candidate_diffs:
        print(f"\n  [{diff['case_id']}] 「{diff['query'][:30]}」 domain={diff['domain']}")
        print(f"    common candidates: {diff['common']}")

        if diff["fixed_only"]:
            print(f"    fixed 多出 (graph_expand 额外候选): {diff['fixed_only']}")
            total_fixed_extra += len(diff["fixed_only"])
            # 计算种子候选的域集合（common 候选的域）
            seed_domains = {
                fixed_adapter._intents_by_id.get(sid, {}).get("domain", "")
                for sid in diff["common"]
                if sid in fixed_adapter._intents_by_id
            }
            for fid in diff["fixed_only"]:
                # 检查是否 confusableIntents 相关
                is_confusable = fid in confusable_related_ids
                if is_confusable:
                    confusable_count += 1
                # 检查是否跨域（候选域不同于种子候选域）
                intent_meta = fixed_adapter._intents_by_id.get(fid, {})
                extra_domain = intent_meta.get("domain", "")
                is_cross_domain = bool(seed_domains) and extra_domain not in seed_domains
                if is_cross_domain:
                    cross_domain_count += 1
                if is_confusable and is_cross_domain:
                    both_count += 1
                print(
                    f"      → {fid}  confusable={'是' if is_confusable else '否'}  "
                    f"domain={extra_domain}  cross_domain={'是' if is_cross_domain else '否'}"
                )

        if diff["buggy_only"]:
            print(f"    buggy 多出: {diff['buggy_only']}")

    print("\n" + "─" * 60)
    print("候选集对比总结")
    print("─" * 60)
    print(f"  fixed 比 buggy 多召回的候选总数: {total_fixed_extra}")
    print(f"  其中 confusableIntents 相关: {confusable_count} / {total_fixed_extra}")
    print(f"  其中跨域候选: {cross_domain_count} / {total_fixed_extra}")
    print(f"  其中 confusableIntents 相关且跨域: {both_count} / {total_fixed_extra}")
    all_confusable = (confusable_count == total_fixed_extra) if total_fixed_extra > 0 else True
    all_cross_domain = (cross_domain_count == total_fixed_extra) if total_fixed_extra > 0 else True
    all_both = (both_count == total_fixed_extra) if total_fixed_extra > 0 else True
    print(f"  是否全部为 confusableIntents 相关候选: {'是' if all_confusable else '否'}")
    print(f"  是否全部为跨域候选: {'是' if all_cross_domain else '否'}")
    print(f"  是否全部为 confusableIntents 相关的跨域候选: {'是' if all_both else '否'}")


if __name__ == "__main__":
    main()
