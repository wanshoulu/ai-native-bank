"""完整四层链路测试:词法 → 子图 → 语义 → LLM 裁决
模拟 service.py _v4_probe_pipeline 的逻辑,对 256 case 跑完整链路
验证 single_intent 弱召回/有竞争时触发 LLM 的修改
"""
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
from app.subgraph_reasoning_v4 import SubgraphReasoningV4, _STRONG_MATCH_THRESHOLD
from app.semantic_recall_v4 import SemanticRecallV4
from app.llm_judge_v4 import LLMJudgeV4
from app.intent_shape import analyze_intent_shape

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"


async def probe_pipeline(adapter, subgraph, semantic, judge, message):
    """模拟 service.py _v4_probe_pipeline 的完整四层链路"""
    # 第一层:词法召回(graph_expand=True,跟生产一致)
    candidates = adapter.recall(message, page_context={}, limit=20, graph_expand=True)

    # 第二层:子图扩召回(top1 < _STRONG_MATCH_THRESHOLD 时)
    if candidates and subgraph and subgraph.enabled:
        top1_score = max(c.score for c in candidates)
        if top1_score < _STRONG_MATCH_THRESHOLD:
            expanded = subgraph.expand_recall(candidates)
            if expanded:
                candidates = candidates + list(expanded)

    # 第三层:语义召回
    if semantic and semantic.enabled:
        if not candidates:
            sem = await asyncio.to_thread(semantic.recall, message, set())
        else:
            known = {c.intent_id for c in candidates}
            sem = await asyncio.to_thread(semantic.recall, message, known)
        if sem:
            candidates = candidates + list(sem)

    if not candidates:
        return [], None

    # 第四层:LLM 裁决
    shape = analyze_intent_shape(message).shape
    should_judge = False
    sorted_cands = sorted(candidates, key=lambda c: -c.score)
    judged_shape = None

    if shape == "single_intent" and candidates:
        top1_score = sorted_cands[0].score if sorted_cands else 0
        top2_score = sorted_cands[1].score if len(sorted_cands) > 1 else 0
        should_judge = (
            top1_score < _STRONG_MATCH_THRESHOLD
            or top2_score >= 60
        )
    elif shape not in ("single_intent", "non_task"):
        should_judge = True

    if should_judge and sorted_cands and judge and judge.enabled:
        contexts = subgraph.build_contexts(sorted_cands[:12])
        judged = await asyncio.to_thread(
            judge.judge, message, sorted_cands[:12], shape, contexts
        )
        if judged:
            judged_order = {item["intent_id"]: item["confidence"] for item in judged}
            judged_cands = []
            remaining = []
            for c in sorted_cands:
                if c.intent_id in judged_order:
                    judged_cands.append((c, judged_order[c.intent_id]))
                else:
                    remaining.append(c)
            judged_cands.sort(key=lambda x: -x[1])
            candidates = [c for c, _ in judged_cands] + remaining
            judged_shape = shape

    return candidates[:12], judged_shape


def check_result(case, candidates):
    shape = case.get("intent_shape")
    expected = [e["intent_id"] for e in case.get("expected_intents", [])]
    if not expected:
        return shape == "non_task"
    if not candidates:
        return False
    if shape == "single_intent":
        return candidates[0].intent_id in expected
    if shape == "multi_intent":
        ids = {c.intent_id for c in candidates}
        return all(eid in ids for eid in expected)
    if shape == "ambiguous_intent":
        ids = {c.intent_id for c in candidates}
        return all(eid in ids for eid in expected) and len([eid for eid in expected if eid in ids]) >= 2
    if shape == "non_task":
        return len(candidates) == 0
    return False


async def main():
    rs = RecallSettings.from_env()
    payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    adapter = V4GraphAdapter(payload, GRAPH_PATH, recall_settings=rs)
    adapter.apply_recall_settings(rs)
    subgraph = SubgraphReasoningV4(adapter, recall_settings=rs)
    semantic = SemanticRecallV4(adapter, recall_settings=rs)
    judge = LLMJudgeV4(recall_settings=rs)
    print(f"subgraph.enabled={subgraph.enabled}, semantic.enabled={semantic.enabled}, judge.enabled={judge.enabled}")
    print(f"_STRONG_MATCH_THRESHOLD={_STRONG_MATCH_THRESHOLD}")

    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    print(f"\n测试 {len(cases)} 个 case(完整四层链路)")

    # 先测 5 个验证 case
    verify_ids = {"V4-205", "V4-206", "V4-211", "V4-222", "V4-243"}
    verify_cases = [c for c in cases if c["case_id"] in verify_ids]
    print(f"\n=== 验证 case ({len(verify_cases)} 个) ===")
    pass_count = 0
    for case in verify_cases:
        candidates, judged_shape = await probe_pipeline(adapter, subgraph, semantic, judge, case["query"])
        ok = check_result(case, candidates)
        top1 = candidates[0].intent_id if candidates else None
        expected = [e["intent_id"] for e in case.get("expected_intents", [])]
        status = "PASS" if ok else "FAIL"
        if ok:
            pass_count += 1
        print(f"  [{case['case_id']}] {status}  top1={top1}  expected={expected}  judged={judged_shape}")
    print(f"验证 case: {pass_count}/{len(verify_cases)}")

    # 询问是否继续全量测试
    print(f"\n=== 全量测试 ({len(cases)} 个 case) ===")
    print("(全量测试需要 ~10 分钟 LLM 调用,跑全部 256 case)")
    full_pass = 0
    shape_stats = {
        "single_intent": [0, 0],  # [pass, total]
        "multi_intent": [0, 0],
        "ambiguous_intent": [0, 0],
        "non_task": [0, 0],
    }
    failed_cases = []
    for i, case in enumerate(cases):
        candidates, _ = await probe_pipeline(adapter, subgraph, semantic, judge, case["query"])
        ok = check_result(case, candidates)
        shape = case.get("intent_shape")
        if shape in shape_stats:
            shape_stats[shape][1] += 1
            if ok:
                shape_stats[shape][0] += 1
        if ok:
            full_pass += 1
        else:
            failed_cases.append((case["case_id"], shape, case["query"],
                                  candidates[0].intent_id if candidates else None,
                                  [e["intent_id"] for e in case.get("expected_intents", [])]))
        if (i + 1) % 20 == 0:
            print(f"  进度: {i+1}/{len(cases)}, 通过 {full_pass}/{i+1}")

    print(f"\n=== 全量结果 ===")
    print(f"总通过: {full_pass}/{len(cases)} ({full_pass*100/len(cases):.1f}%)")
    for shape, (p, t) in shape_stats.items():
        if t > 0:
            print(f"  {shape}: {p}/{t} ({p*100/t:.1f}%)")

    print(f"\n失败 case ({len(failed_cases)} 个):")
    for cid, shape, query, top1, expected in failed_cases[:30]:
        print(f"  [{cid}] {shape}  query={query}  top1={top1}  expected={expected}")


if __name__ == "__main__":
    asyncio.run(main())
