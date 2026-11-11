"""LLM 消歧对照测试:with_subgraph vs no_subgraph

从测试集筛 top1 不正确的 case,对照 LLM 裁决:
- with_sub: 带子图上下文(disambiguation_hints + confusable_with)
- no_sub: 不带子图上下文(只 title + description)

复用 V4GraphAdapter + SubgraphReasoningV4.build_contexts + LLMJudgeV4
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # agent-runtime/
sys.path.insert(0, str(ROOT))

# 强制从 .env 加载 QWEN_KEY
env_path = Path(r"d:\project\vscode\.env")
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("QWEN_KEY=") and not line.startswith("#"):
            import os
            os.environ["QWEN_KEY"] = line[len("QWEN_KEY="):]
            break

from app.config import RecallSettings
from app.intent_graph import IntentCandidate
from app.v4_graph_adapter import V4GraphAdapter
from app.subgraph_reasoning_v4 import SubgraphReasoningV4

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"


def main():
    print("=" * 80)
    print("LLM 消歧对照测试:with_sub vs no_sub")
    print("=" * 80)

    rs = RecallSettings.from_env()
    payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    adapter = V4GraphAdapter(payload, GRAPH_PATH, recall_settings=rs)
    adapter.apply_recall_settings(rs)
    subgraph = SubgraphReasoningV4(adapter, recall_settings=rs)

    # 延迟导入 LLMJudgeV4(在 QWEN_KEY 加载后)
    from app.llm_judge_v4 import LLMJudgeV4
    judge = LLMJudgeV4(recall_settings=rs)

    if not judge.enabled:
        print("[FATAL] LLM judge 未启用,QWEN_KEY 无效")
        return
    print(f"[OK] LLM judge enabled, model={judge._model}")
    print(f"[OK] subgraph enabled={subgraph.enabled}")

    # 加载测试集
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    print(f"测试集总数: {len(cases)}")

    # 筛 top1 不正确 + single_intent 的 case(消歧场景)
    sample_cases = []
    for case in cases:
        if case.get("intent_shape") != "single_intent":
            continue
        query = case["query"]
        expected = [e["intent_id"] for e in case.get("expected_intents", [])]
        candidates = adapter.recall(query, page_context={}, limit=10)
        if not candidates:
            continue
        top1 = candidates[0].intent_id
        if top1 in expected:
            continue  # top1 已正确,不需消歧
        sample_cases.append((case, candidates, expected))

    print(f"top1 不正确的 single_intent case: {len(sample_cases)}")
    if not sample_cases:
        print("[WARN] 无可测 case")
        return

    # 取前 15 个
    sample_cases = sample_cases[:15]
    print(f"采样: {len(sample_cases)} case\n")

    results = []
    for i, (case, candidates, expected) in enumerate(sample_cases, 1):
        case_id = case["case_id"]
        query = case["query"]
        top1 = candidates[0].intent_id
        print(f"[{i}/{len(sample_cases)}] case={case_id} query=「{query}」")
        print(f"  top1={top1} expected={expected}")

        # with_sub: 带子图上下文
        t0 = time.time()
        try:
            sub_ctx = subgraph.build_contexts(candidates) if subgraph.enabled else []
            with_result = judge.judge(query, candidates, subgraph_contexts=sub_ctx)
            with_status = "ok"
        except Exception as e:
            with_result = []
            with_status = f"err:{type(e).__name__}"
        with_time = time.time() - t0

        # no_sub: 不带子图上下文
        t1 = time.time()
        try:
            no_result = judge.judge(query, candidates, subgraph_contexts=[])
            no_status = "ok"
        except Exception as e:
            no_result = []
            no_status = f"err:{type(e).__name__}"
        no_time = time.time() - t1

        with_top1 = with_result[0]["intent_id"] if with_result else None
        no_top1 = no_result[0]["intent_id"] if no_result else None
        with_match = with_top1 in expected
        no_match = no_top1 in expected

        print(f"  with_sub: top1={with_top1} match={with_match} time={with_time:.1f}s [{with_status}]")
        print(f"  no_sub:   top1={no_top1} match={no_match} time={no_time:.1f}s [{no_status}]")
        print()

        results.append({
            "case_id": case_id,
            "query": query,
            "expected": expected,
            "top1_recall": top1,
            "with_sub": {"top1": with_top1, "match": with_match, "time": with_time, "status": with_status},
            "no_sub": {"top1": no_top1, "match": no_match, "time": no_time, "status": no_status},
        })

    # 汇总
    print("=" * 80)
    print("汇总")
    print("=" * 80)
    with_pass = sum(1 for r in results if r["with_sub"]["match"])
    no_pass = sum(1 for r in results if r["no_sub"]["match"])
    both_pass = sum(1 for r in results if r["with_sub"]["match"] and r["no_sub"]["match"])
    neither_pass = sum(1 for r in results if not r["with_sub"]["match"] and not r["no_sub"]["match"])
    with_only = sum(1 for r in results if r["with_sub"]["match"] and not r["no_sub"]["match"])
    no_only = sum(1 for r in results if not r["with_sub"]["match"] and r["no_sub"]["match"])
    with_timeout = sum(1 for r in results if "err" in r["with_sub"]["status"])
    no_timeout = sum(1 for r in results if "err" in r["no_sub"]["status"])

    total = len(results)
    print(f"total cases:        {total}")
    print(f"with_sub pass:      {with_pass}/{total} ({with_pass/total*100:.1f}%)")
    print(f"no_sub pass:        {no_pass}/{total} ({no_pass/total*100:.1f}%)")
    print(f"both pass:          {both_pass}")
    print(f"neither pass:       {neither_pass}")
    print(f"with_sub only pass: {with_only}")
    print(f"no_sub only pass:   {no_only}")
    print(f"with_sub err/timeout: {with_timeout}")
    print(f"no_sub err/timeout:   {no_timeout}")

    # 保存详细结果
    out_path = Path(__file__).resolve().parent / "_tmp_llm_compare_result.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果已保存: {out_path}")


if __name__ == "__main__":
    main()
