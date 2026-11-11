#!/usr/bin/env python
"""子图召回价值对比:Config A(无子图) vs Config B(有子图)

优化版:跳过不触发 LLM 的强匹配 case,只对需要 LLM 裁决的 case 调两个配置。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from app.llm_judge_v4 import LLMJudgeV4

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

_STRONG_MATCH_THRESHOLD = 80
_SUPER_STRONG_MATCH_THRESHOLD = 145


def check_case(case, candidates):
    shape = case["intent_shape"]
    expected_ids = [e["intent_id"] for e in case.get("expected_intents", [])]
    candidate_ids = [c.intent_id for c in candidates]

    if shape == "single_intent":
        top1 = candidate_ids[0] if candidate_ids else None
        return top1 in expected_ids if expected_ids else None
    elif shape in ("ambiguous_intent", "multi_intent"):
        found = [eid for eid in expected_ids if eid in candidate_ids]
        return len(found) == len(expected_ids) if expected_ids else None
    else:  # non_task
        return len(candidates) == 0


def judge_with_subgraph(adapter, subgraph, semantic, judge, message, intent_shape):
    """Config B: 词法 + 子图 + embedding + LLM(有子图上下文)"""
    # 词法 + 子图 + embedding
    candidates = adapter.recall(message, page_context={}, limit=12)
    if subgraph.enabled and candidates:
        expanded = subgraph.expand_recall(candidates)
        if expanded:
            candidates = (list(candidates) + list(expanded))[:12]

    if semantic.enabled and candidates:
        top1_score = max(c.score for c in candidates)
        if top1_score < _STRONG_MATCH_THRESHOLD:
            known_ids = {c.intent_id for c in candidates}
            sem_cands = semantic.recall(message, exclude_ids=known_ids)
            if sem_cands:
                candidates = (list(candidates) + list(sem_cands))[:12]

    if not candidates:
        return []
    if intent_shape == "non_task":
        return candidates[:12]

    sorted_cands = list(candidates)
    should_judge = False
    if intent_shape == "single_intent" and sorted_cands:
        top1_score = sorted_cands[0].score
        top2_score = sorted_cands[1].score if len(sorted_cands) > 1 else 0
        should_judge = (top1_score < _STRONG_MATCH_THRESHOLD or top2_score >= 60)
    elif intent_shape in ("ambiguous_intent", "multi_intent"):
        should_judge = True

    if not should_judge:
        return candidates[:12]

    # Config B:有子图上下文
    subgraph_contexts = subgraph.build_contexts(sorted_cands[:12])
    judged = judge.judge(
        message, sorted_cands[:12],
        intent_shape=intent_shape,
        subgraph_contexts=subgraph_contexts,
    )
    if not judged:
        return sorted_cands[:12]

    judged_order = {item["intent_id"]: item["confidence"] for item in judged}
    original_top1 = sorted_cands[0]
    super_strong = original_top1.score >= _SUPER_STRONG_MATCH_THRESHOLD
    judged_candidates = []
    remaining = []
    for c in sorted_cands:
        if c.intent_id in judged_order:
            judged_candidates.append((c, judged_order[c.intent_id]))
        else:
            remaining.append(c)

    judged_candidates.sort(key=lambda x: -x[1])
    result = [c for c, _ in judged_candidates] + remaining
    if super_strong:
        result = [original_top1] + [c for c in result if c.intent_id != original_top1.intent_id]
    return result[:12]


def judge_without_subgraph(adapter, semantic, judge, message, intent_shape):
    """Config A: 词法 + embedding + LLM(无子图上下文)"""
    # 词法 + embedding(无子图)
    candidates = adapter.recall(message, page_context={}, limit=12)

    if semantic.enabled and candidates:
        top1_score = max(c.score for c in candidates)
        if top1_score < _STRONG_MATCH_THRESHOLD:
            known_ids = {c.intent_id for c in candidates}
            sem_cands = semantic.recall(message, exclude_ids=known_ids)
            if sem_cands:
                candidates = (list(candidates) + list(sem_cands))[:12]

    if not candidates:
        return []
    if intent_shape == "non_task":
        return candidates[:12]

    sorted_cands = list(candidates)
    should_judge = False
    if intent_shape == "single_intent" and sorted_cands:
        top1_score = sorted_cands[0].score
        top2_score = sorted_cands[1].score if len(sorted_cands) > 1 else 0
        should_judge = (top1_score < _STRONG_MATCH_THRESHOLD or top2_score >= 60)
    elif intent_shape in ("ambiguous_intent", "multi_intent"):
        should_judge = True

    if not should_judge:
        return candidates[:12]

    # Config A:无子图上下文
    judged = judge.judge(
        message, sorted_cands[:12],
        intent_shape=intent_shape,
        subgraph_contexts=None,
    )
    if not judged:
        return sorted_cands[:12]

    judged_order = {item["intent_id"]: item["confidence"] for item in judged}
    original_top1 = sorted_cands[0]
    super_strong = original_top1.score >= _SUPER_STRONG_MATCH_THRESHOLD
    judged_candidates = []
    remaining = []
    for c in sorted_cands:
        if c.intent_id in judged_order:
            judged_candidates.append((c, judged_order[c.intent_id]))
        else:
            remaining.append(c)

    judged_candidates.sort(key=lambda x: -x[1])
    result = [c for c, _ in judged_candidates] + remaining
    if super_strong:
        result = [original_top1] + [c for c in result if c.intent_id != original_top1.intent_id]
    return result[:12]


def process_case(case, adapter, subgraph, semantic, judge):
    """处理单个 case,返回 (case, passed_a, passed_b, cands_a, cands_b, llm_used, error)"""
    case_id = case["case_id"]
    msg = case["query"]
    shape = case["intent_shape"]
    expected = [e["intent_id"] for e in case.get("expected_intents", [])]

    # 先判断是否需要 LLM(用词法召回结果判断)
    lex_cands = adapter.recall(msg, page_context={}, limit=12)
    need_llm = False
    if shape == "non_task":
        need_llm = False
    elif shape == "single_intent" and lex_cands:
        top1 = lex_cands[0].score
        top2 = lex_cands[1].score if len(lex_cands) > 1 else 0
        need_llm = (top1 < _STRONG_MATCH_THRESHOLD or top2 >= 60)
    elif shape in ("ambiguous_intent", "multi_intent"):
        need_llm = True

    if not need_llm:
        passed = check_case(case, lex_cands[:12])
        return case, passed, passed, lex_cands[:12], lex_cands[:12], False, None

    # 需要 LLM,调两个配置
    try:
        cands_a = judge_without_subgraph(adapter, semantic, judge, msg, shape)
        passed_a = check_case(case, cands_a)
    except Exception as e:
        cands_a = lex_cands[:12]
        passed_a = check_case(case, cands_a)
        err_a = str(e)
    else:
        err_a = None

    try:
        cands_b = judge_with_subgraph(adapter, subgraph, semantic, judge, msg, shape)
        passed_b = check_case(case, cands_b)
    except Exception as e:
        cands_b = lex_cands[:12]
        passed_b = check_case(case, cands_b)
        err_b = str(e)
    else:
        err_b = None

    err = err_a or err_b
    return case, passed_a, passed_b, cands_a, cands_b, True, err


def main():
    print("=" * 100)
    print("子图召回价值对比: Config A(无子图) vs Config B(有子图) - 并发版")
    print(f"测试集: {DATASET_PATH}")
    print(f"图谱:   {GRAPH_PATH}")
    print("=" * 100)

    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    print(f"用例总数: {len(cases)}")

    adapter = V4GraphAdapter.load(GRAPH_PATH)
    subgraph = SubgraphReasoningV4(adapter)
    semantic = SemanticRecallV4(adapter)
    judge = LLMJudgeV4()
    judge._adapter = adapter

    print(f"子图推理: {'✅' if subgraph.enabled else '❌'}")
    print(f"语义召回: {'✅' if semantic.enabled else '❌'}")
    print(f"LLM 裁决: {'✅' if judge.enabled else '❌'}")
    print(f"LLM timeout: 30s (已优化)")
    print(f"并发: 4 workers")
    print()

    # 并发处理
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    _print_lock = threading.Lock()

    results = {}  # case_id -> result tuple
    t_start = time.time()
    completed = 0
    llm_calls = 0
    skipped = 0

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(process_case, case, adapter, subgraph, semantic, judge): case for case in cases}
        for future in as_completed(futures):
            case, passed_a, passed_b, cands_a, cands_b, llm_used, err = future.result()
            case_id = case["case_id"]
            msg = case["query"]
            shape = case["intent_shape"]
            expected = [e["intent_id"] for e in case.get("expected_intents", [])]

            results[case_id] = {
                "case": case, "passed_a": passed_a, "passed_b": passed_b,
                "cands_a": cands_a, "cands_b": cands_b, "llm_used": llm_used, "err": err,
            }

            if llm_used:
                llm_calls += 1
            else:
                skipped += 1
            completed += 1

            if err:
                with _print_lock:
                    print(f"  {case_id} ERROR: {err}")

            if completed % 10 == 0 or completed == len(cases):
                elapsed = time.time() - t_start
                a_pass = sum(1 for r in results.values() if r["passed_a"])
                b_pass = sum(1 for r in results.values() if r["passed_b"])
                with _print_lock:
                    print(f"  progress: {completed}/{len(cases)} ({elapsed:.0f}s) llm_calls={llm_calls} skipped={skipped} A={a_pass}/{completed} B={b_pass}/{completed}")

    elapsed = time.time() - t_start
    print()
    print("=" * 100)
    print(f"对比结果(全量 {len(cases)} case, LLM 调用 {llm_calls} 次, 跳过 {skipped}, 耗时 {elapsed:.0f}s):")
    print("=" * 100)

    # 按原始顺序汇总
    a_pass = a_fail = 0
    b_pass = b_fail = 0
    a_fail_cases = []
    b_fail_cases = []
    shape_stats = {"A": {}, "B": {}}

    for case in cases:
        case_id = case["case_id"]
        r = results[case_id]
        passed_a = r["passed_a"]
        passed_b = r["passed_b"]
        cands_a = r["cands_a"]
        cands_b = r["cands_b"]
        llm_used = r["llm_used"]
        msg = case["query"]
        shape = case["intent_shape"]
        expected = [e["intent_id"] for e in case.get("expected_intents", [])]

        if passed_a:
            a_pass += 1
        else:
            a_fail += 1
            top1_a = cands_a[0].intent_id if cands_a else "(empty)"
            a_fail_cases.append({"case_id": case_id, "query": msg[:25], "shape": shape, "expected": expected, "top1": top1_a, "llm": llm_used, "cands": [c.intent_id for c in cands_a[:5]]})

        if passed_b:
            b_pass += 1
        else:
            b_fail += 1
            top1_b = cands_b[0].intent_id if cands_b else "(empty)"
            b_fail_cases.append({"case_id": case_id, "query": msg[:25], "shape": shape, "expected": expected, "top1": top1_b, "llm": llm_used, "cands": [c.intent_id for c in cands_b[:5]]})

        for cfg, passed in [("A", passed_a), ("B", passed_b)]:
            if shape not in shape_stats[cfg]:
                shape_stats[cfg][shape] = {"pass": 0, "fail": 0}
            shape_stats[cfg][shape]["pass" if passed else "fail"] += 1

    total_a = a_pass + a_fail
    total_b = b_pass + b_fail
    rate_a = a_pass / total_a * 100 if total_a else 0
    rate_b = b_pass / total_b * 100 if total_b else 0

    print(f"\nConfig A(无子图): {a_pass}/{total_a} = {rate_a:.1f}%")
    print(f"Config B(有子图): {b_pass}/{total_b} = {rate_b:.1f}%")
    print(f"子图净增益: +{b_pass - a_pass} case ({rate_b - rate_a:+.1f}pp)")

    print(f"\n按 shape 分组:")
    for shape in ["single_intent", "ambiguous_intent", "multi_intent", "non_task"]:
        sa = shape_stats["A"].get(shape, {"pass": 0, "fail": 0})
        sb = shape_stats["B"].get(shape, {"pass": 0, "fail": 0})
        ta = sa["pass"] + sa["fail"]
        tb = sb["pass"] + sb["fail"]
        ra = sa["pass"] / ta * 100 if ta else 0
        rb = sb["pass"] / tb * 100 if tb else 0
        delta = sb["pass"] - sa["pass"]
        print(f"  {shape:20s}: A={sa['pass']}/{ta}({ra:.1f}%)  B={sb['pass']}/{tb}({rb:.1f}%)  Δ={delta:+d}")

    # 差异分析
    print("\n" + "=" * 100)
    print("差异分析:")
    a_fail_ids = {f["case_id"] for f in a_fail_cases}
    b_fail_ids = {f["case_id"] for f in b_fail_cases}
    b_only_rescued = [f for f in a_fail_cases if f["case_id"] not in b_fail_ids]
    a_only_rescued = [f for f in b_fail_cases if f["case_id"] not in a_fail_ids]

    print(f"\nConfig A 失败但 Config B 通过(B 比 A 多救回): {len(b_only_rescued)} case")
    for f in b_only_rescued:
        print(f"  {f['case_id']:8s} query={f['query']:25s} shape={f['shape']:15s} expected={f['expected']} A_top1={f['top1']}")

    print(f"\nConfig B 失败但 Config A 通过(A 比 B 多救回): {len(a_only_rescued)} case")
    for f in a_only_rescued:
        print(f"  {f['case_id']:8s} query={f['query']:25s} shape={f['shape']:15s} expected={f['expected']} B_top1={f['top1']}")

    # 保存详细结果
    result_path = Path(__file__).resolve().parent / "ab_compare_result.json"
    result_path.write_text(json.dumps({
        "summary": {
            "total": len(cases),
            "A_no_subgraph": {"pass": a_pass, "fail": a_fail, "rate": rate_a},
            "B_with_subgraph": {"pass": b_pass, "fail": b_fail, "rate": rate_b},
            "delta": b_pass - a_pass,
            "llm_calls": llm_calls,
            "elapsed_s": elapsed,
        },
        "a_fail_cases": a_fail_cases,
        "b_fail_cases": b_fail_cases,
        "b_only_rescued": b_only_rescued,
        "a_only_rescued": a_only_rescued,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果已保存到: {result_path}")


if __name__ == "__main__":
    main()
