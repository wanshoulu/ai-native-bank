#!/usr/bin/env python
"""分析所有失败用例，输出详细信息"""
import json, sys, os
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent  # agent-runtime/
sys.path.insert(0, str(ROOT))

from app.intent_shape import analyze_intent_shape  # noqa: E402
from app.v4_graph_adapter import V4GraphAdapter  # noqa: E402

GRAPH_PATH = Path(r"d:\project\vscode\data\ai-native-bank\intent-graph\graph.ccb.intents.v2.1.0.json")
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

adapter = V4GraphAdapter.load(GRAPH_PATH)

dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
cases = dataset["cases"]

failures = []
for case in cases:
    query = case["query"]
    shape = case["intent_shape"]
    difficulty = case.get("difficulty", "")
    expected_ids = [e["intent_id"] for e in case.get("expected_intents", [])]

    result = adapter.recall(query, page_context={}, limit=10)
    candidates = [r.intent_id for r in result]
    top1 = candidates[0] if candidates else None

    if shape == "single_intent":
        passed = top1 in expected_ids if expected_ids else False
    elif shape == "multi_intent":
        passed = all(e in candidates for e in expected_ids)
    elif shape == "ambiguous_intent":
        passed = all(e in candidates for e in expected_ids)
    elif shape == "non_task":
        passed = len(candidates) == 0
    else:
        passed = True

    if not passed:
        missing = [e for e in expected_ids if e not in candidates]
        failures.append({
            "case_id": case["case_id"],
            "query": query,
            "shape": shape,
            "difficulty": difficulty,
            "expected_ids": expected_ids,
            "top1": top1,
            "candidates": candidates,
            "missing": missing,
        })

print(f"总失败: {len(failures)}/{len(cases)}")
print()

by_shape = defaultdict(list)
for f in failures:
    by_shape[f["shape"]].append(f)

for shape, items in sorted(by_shape.items()):
    print(f"=== {shape} ({len(items)}条失败) ===")
    for f in items:
        exp_str = ", ".join(f["expected_ids"])
        cand_str = ", ".join(f["candidates"][:6])
        missing_str = ", ".join(f["missing"]) if f["missing"] else "—"
        print(f"  {f['case_id']} [{f['difficulty']:6s}] q='{f['query']}'")
        print(f"    expected: [{exp_str}]")
        print(f"    top1: {f['top1']}")
        print(f"    candidates: [{cand_str}]")
        if f["missing"]:
            print(f"    MISSING: {missing_str}")
    print()
