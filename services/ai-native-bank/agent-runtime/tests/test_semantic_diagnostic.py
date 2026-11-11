"""诊断:semantic recall 为什么拖后腿?

逐 case 对比 fixed vs fixed+semantic,找出失败的 case 和失败模式。
"""
from __future__ import annotations

import json
import os
import sys
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
from app.semantic_recall_v4 import SemanticRecallV4

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"


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


def main():
    rs = RecallSettings.from_env()
    payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    adapter = V4GraphAdapter(payload, GRAPH_PATH, recall_settings=rs)
    adapter.apply_recall_settings(rs)
    semantic = SemanticRecallV4(adapter, recall_settings=rs)
    print(f"semantic enabled={semantic.enabled}")
    semantic._build_index()
    print(f"semantic index entries={len(semantic._vectors)}")

    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]

    # 逐 case 对比
    flips = []  # pass->fail
    improves = []  # fail->pass
    same_pass = 0
    same_fail = 0
    by_shape_flip = {"single_intent": 0, "multi_intent": 0, "ambiguous_intent": 0, "non_task": 0}

    for case in cases:
        shape = case.get("intent_shape")
        query = case["query"]

        # fixed: 词法
        lex = adapter.recall(query, page_context={}, limit=10)
        # fixed+semantic: 词法 + 语义
        existing = {c.intent_id for c in lex}
        sem = semantic.recall(query, exclude_ids=existing) if semantic.enabled else []
        full = list(lex) + list(sem)

        lex_ok = check(case, lex)
        full_ok = check(case, full)

        if lex_ok and not full_ok:
            flips.append((case, lex, sem, full))
            by_shape_flip[shape] = by_shape_flip.get(shape, 0) + 1
        elif not lex_ok and full_ok:
            improves.append((case, lex, sem, full))
        elif lex_ok and full_ok:
            same_pass += 1
        else:
            same_fail += 1

    print()
    print("=" * 80)
    print(f"same_pass: {same_pass}")
    print(f"same_fail: {same_fail}")
    print(f"flips (pass->fail): {len(flips)}  按shape: {by_shape_flip}")
    print(f"improves (fail->pass): {len(improves)}")
    print()

    # 详细分析 flips
    print("=" * 80)
    print("失败 case 详细分析 (pass->fail)")
    print("=" * 80)
    for i, (case, lex, sem, full) in enumerate(flips[:20], 1):
        shape = case.get("intent_shape")
        query = case["query"]
        expected = [e["intent_id"] for e in case.get("expected_intents", [])]
        lex_ids = [c.intent_id for c in lex[:3]]
        sem_ids = [c.intent_id for c in sem[:3]]
        full_ids = [c.intent_id for c in full[:3]]
        print(f"\n[flip {i}] case={case['case_id']} shape={shape}")
        print(f"  query: {query}")
        print(f"  expected: {expected}")
        print(f"  lex top3: {lex_ids}")
        print(f"  sem top3: {sem_ids}")
        print(f"  full top3: {full_ids}")
        print(f"  lex_count={len(lex)}, sem_count={len(sem)}, full_count={len(full)}")
        # 关键:检查 top1 是否变了
        if lex and full:
            lex_top1 = lex[0].intent_id
            full_top1 = full[0].intent_id
            print(f"  top1 变化: lex={lex_top1} -> full={full_top1} (变={lex_top1 != full_top1})")
            if lex_top1 == full_top1:
                print(f"  *** top1 没变,失败原因不是 top1 被替换 ***")
                # 检查是不是 non_task
                if shape == "non_task":
                    print(f"  *** non_task case,语义召回添加了候选导致非零召回 ***")
                elif shape == "ambiguous_intent":
                    found_in_full = [eid for eid in expected if eid in {c.intent_id for c in full}]
                    found_in_lex = [eid for eid in expected if eid in {c.intent_id for c in lex}]
                    print(f"  ambiguous: lex found={found_in_lex}, full found={found_in_full}")

    # 改善的也看下
    print()
    print("=" * 80)
    print(f"改善 case (fail->pass): {len(improves)}")
    print("=" * 80)
    for i, (case, lex, sem, full) in enumerate(improves[:5], 1):
        shape = case.get("intent_shape")
        query = case["query"]
        expected = [e["intent_id"] for e in case.get("expected_intents", [])]
        lex_ids = [c.intent_id for c in lex[:3]]
        sem_ids = [c.intent_id for c in sem[:3]]
        print(f"[improve {i}] case={case['case_id']} shape={shape} query={query}")
        print(f"  expected: {expected}")
        print(f"  lex top3: {lex_ids}")
        print(f"  sem top3: {sem_ids}")


if __name__ == "__main__":
    main()
