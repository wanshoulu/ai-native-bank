#!/usr/bin/env python
"""调试 LLM judge 完整输出（含 reason）"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from app.llm_judge_v4 import LLMJudgeV4, _SYSTEM_PROMPT, _LLM_TEMPERATURE, _DEFAULT_LLM_MODEL, _DEFAULT_BASE_URL
from tests.test_eval_v4_semantic import semantic_recall
import httpx

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
cases = {c["case_id"]: c for c in dataset["cases"]}

adapter = V4GraphAdapter.load(GRAPH_PATH)
subgraph = SubgraphReasoningV4(adapter)
semantic = SemanticRecallV4(adapter)
judge = LLMJudgeV4()
judge._adapter = adapter

target_ids = ["V4-121", "V4-215", "V4-224"]
for cid in target_ids:
    case = cases[cid]
    query = case["query"]
    expected = [e["intent_id"] for e in case.get("expected_intents", [])]
    shape = case["intent_shape"]

    cands = semantic_recall(adapter, subgraph, semantic, query)
    sorted_cands = list(cands)[:12]
    top1 = sorted_cands[0]
    print(f"\n{'='*80}")
    print(f"{cid} 「{query}」 expected={expected}")
    print(f"  词法 top1: {top1.intent_id} score={top1.score}")
    print(f"  候选 top5: {[(c.intent_id, c.score) for c in sorted_cands[:5]]}")

    # 构建子图上下文
    contexts = subgraph.build_contexts(sorted_cands[:12])

    # 构建 LLM prompt
    from tests.test_eval_v4_semantic import _check_confusable_pairs
    candidate_descs = []
    for i, c in enumerate(sorted_cands[:12]):
        ctx = contexts[i]
        candidate_descs.append(judge._format_candidate_with_subgraph(i + 1, ctx))
    candidate_list = "\n".join(candidate_descs)
    task_hint = "用户查询指向单一意图，请选择最匹配的一个。重点参考 disambiguation_hints 做判断。"
    user_prompt = f"""用户查询：「{query}」

候选意图列表（含子图上下文）：
{candidate_list}

任务要求：{task_hint}

请基于子图上下文中的 disambiguation_hints 和 confusable_with 关系做推理消歧，从候选列表中选择最匹配的意图，返回 JSON：
{{"selected": [{{"intent_id": "候选id", "confidence": 0.95, "reason": "选择理由"}}]}}
"""
    # 调用 LLM
    try:
        client = httpx.Client(timeout=12.0)
        resp = client.post(
            _DEFAULT_BASE_URL + "/chat/completions",
            headers={
                "Authorization": f"Bearer {judge._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _DEFAULT_LLM_MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": _LLM_TEMPERATURE,
                "response_format": {"type": "json_object"},
            },
        )
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = json.loads(content)
        print(f"\n  LLM 完整输出:")
        for item in parsed.get("selected", [])[:5]:
            print(f"    {item.get('intent_id', '?')} conf={item.get('confidence', 0)} reason={item.get('reason', '')[:200]}")
    except Exception as exc:
        print(f"  LLM 调用失败: {exc}")
