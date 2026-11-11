"""256 case 词法+语义召回对照

对照维度:
1. fixed(优化版)vs buggy(消歧元数据清零)— 验证 A1 数据治理价值
2. graph_expand on/off — 验证 B1 词法层图扩展开关效果
3. subgraph on/off — 验证 B2 子图层开关效果
4. semantic on/off — 验证语义召回价值
"""
from __future__ import annotations

import json
import os
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
            os.environ["QWEN_KEY"] = line[len("QWEN_KEY="):]
            break

from app.config import RecallSettings
from app.v4_graph_adapter import V4GraphAdapter
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from app.semantic_recall_v4 import SemanticRecallV4
from app.intent_shape import analyze_intent_shape

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"


def make_buggy_adapter(graph_path: Path) -> V4GraphAdapter:
    """模拟修复前:清零 confusableIntents 和 disambiguationHints"""
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
    return all(eid in ids for eid in expected) and len([eid for eid in expected if eid in ids]) >= 2


def check_non_task(case, candidates) -> bool:
    return len(candidates) == 0


def run_cases(adapter, subgraph, semantic, cases, *, use_subgraph=False, use_semantic=False):
    pass_count = 0
    total = 0
    for case in cases:
        shape = case.get("intent_shape")
        query = case["query"]
        candidates = adapter.recall(query, page_context={}, limit=10)

        # 子图扩召回
        if use_subgraph and subgraph and subgraph.enabled and candidates:
            top1_score = max(c.score for c in candidates)
            if top1_score < 80:  # _STRONG_MATCH_THRESHOLD
                expanded = subgraph.expand_recall(candidates)
                seen = {c.intent_id for c in candidates}
                for c in expanded:
                    if c.intent_id not in seen:
                        candidates.append(c)
                        seen.add(c.intent_id)

        # 语义召回
        if use_semantic and semantic and semantic.enabled:
            existing = {c.intent_id for c in candidates}
            sem_results = semantic.recall(query, exclude_ids=existing)
            candidates.extend(sem_results)

        # 检查
        if shape == "single_intent":
            ok = check_single(case, candidates)
        elif shape == "multi_intent":
            ok = check_multi(case, candidates)
        elif shape == "ambiguous_intent":
            ok = check_ambiguous(case, candidates)
        elif shape == "non_task":
            ok = check_non_task(case, candidates)
        else:
            continue
        if ok:
            pass_count += 1
        total += 1
    return pass_count, total


def main():
    print("=" * 80)
    print("256 case 词法+语义召回对照")
    print("=" * 80)

    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    print(f"测试集总数: {len(cases)}")

    # 加载 fixed + buggy
    rs_default = RecallSettings.from_env()
    fixed_payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    fixed_adapter = V4GraphAdapter(fixed_payload, GRAPH_PATH, recall_settings=rs_default)
    fixed_adapter.apply_recall_settings(rs_default)
    buggy_adapter = make_buggy_adapter(GRAPH_PATH)

    # 子图 + 语义(基于 fixed)
    subgraph = SubgraphReasoningV4(fixed_adapter, recall_settings=rs_default)
    print(f"subgraph enabled={subgraph.enabled}")

    sem_rs = RecallSettings.from_env()
    semantic = SemanticRecallV4(fixed_adapter, recall_settings=sem_rs)
    print(f"semantic enabled={semantic.enabled}")

    # 构建 embedding 索引(只需一次)
    if semantic.enabled:
        print("[semantic] 构建索引中...")
        t0 = time.time()
        semantic._build_index()
        print(f"[semantic] 索引构建完成, 用时 {time.time()-t0:.1f}s, entries={len(semantic._vectors)}")

    print()
    print("=" * 80)
    print("对照组")
    print("=" * 80)

    # 1) buggy(原版消歧清零)— 基线
    print("[1/5] buggy (原版消歧清零) 召回中...")
    t0 = time.time()
    b_pass, b_total = run_cases(buggy_adapter, None, None, cases)
    print(f"  buggy: {b_pass}/{b_total} ({b_pass/b_total*100:.1f}%) 用时 {time.time()-t0:.1f}s")

    # 2) fixed (优化版)— A1 数据治理 + graph_expand on
    print("[2/5] fixed (优化版) 召回中...")
    t0 = time.time()
    f_pass, f_total = run_cases(fixed_adapter, None, None, cases)
    print(f"  fixed: {f_pass}/{f_total} ({f_pass/f_total*100:.1f}%) 用时 {time.time()-t0:.1f}s")

    # 3) fixed + subgraph
    print("[3/5] fixed + subgraph 召回中...")
    t0 = time.time()
    fs_pass, fs_total = run_cases(fixed_adapter, subgraph, None, cases, use_subgraph=True)
    print(f"  fixed+sub: {fs_pass}/{fs_total} ({fs_pass/fs_total*100:.1f}%) 用时 {time.time()-t0:.1f}s")

    # 4) fixed + semantic
    print("[4/5] fixed + semantic 召回中...")
    t0 = time.time()
    fse_pass, fse_total = run_cases(fixed_adapter, None, semantic, cases, use_semantic=True)
    print(f"  fixed+sem: {fse_pass}/{fse_total} ({fse_pass/fse_total*100:.1f}%) 用时 {time.time()-t0:.1f}s")

    # 5) fixed + subgraph + semantic(全开)
    print("[5/5] fixed + subgraph + semantic 召回中...")
    t0 = time.time()
    fall_pass, fall_total = run_cases(fixed_adapter, subgraph, semantic, cases,
                                       use_subgraph=True, use_semantic=True)
    print(f"  fixed+sub+sem: {fall_pass}/{fall_total} ({fall_pass/fall_total*100:.1f}%) 用时 {time.time()-t0:.1f}s")

    # 6) graph_expand off 验证
    print("[6/6] fixed (graph_expand off) 召回中...")
    os.environ["AI_BANK_RECALL_GRAPH_EXPAND_ENABLED"] = "false"
    rs_no_expand = RecallSettings.from_env()
    no_expand_adapter = V4GraphAdapter(fixed_payload, GRAPH_PATH, recall_settings=rs_no_expand)
    no_expand_adapter.apply_recall_settings(rs_no_expand)
    t0 = time.time()
    ne_pass, ne_total = run_cases(no_expand_adapter, None, None, cases)
    print(f"  fixed(no_expand): {ne_pass}/{ne_total} ({ne_pass/ne_total*100:.1f}%) 用时 {time.time()-t0:.1f}s")

    print()
    print("=" * 80)
    print("汇总")
    print("=" * 80)
    print(f"{'对照组':<25} {'通过率':<15} {'差异'}")
    print("-" * 60)
    print(f"{'buggy (消歧清零)':<25} {b_pass}/{b_total} ({b_pass/b_total*100:.1f}%)")
    print(f"{'fixed (优化版)':<25} {f_pass}/{f_total} ({f_pass/f_total*100:.1f}%)")
    print(f"{'fixed + subgraph':<25} {fs_pass}/{fs_total} ({fs_pass/fs_total*100:.1f}%)")
    print(f"{'fixed + semantic':<25} {fse_pass}/{fse_total} ({fse_pass/fse_total*100:.1f}%)")
    print(f"{'fixed + sub + sem':<25} {fall_pass}/{fall_total} ({fall_pass/fall_total*100:.1f}%)")
    print(f"{'fixed (no_expand)':<25} {ne_pass}/{ne_total} ({ne_pass/ne_total*100:.1f}%)")

    # 保存结果
    out = {
        "buggy": {"pass": b_pass, "total": b_total, "rate": b_pass/b_total},
        "fixed": {"pass": f_pass, "total": f_total, "rate": f_pass/f_total},
        "fixed_subgraph": {"pass": fs_pass, "total": fs_total, "rate": fs_pass/fs_total},
        "fixed_semantic": {"pass": fse_pass, "total": fse_total, "rate": fse_pass/fse_total},
        "fixed_all": {"pass": fall_pass, "total": fall_total, "rate": fall_pass/fall_total},
        "fixed_no_expand": {"pass": ne_pass, "total": ne_total, "rate": ne_pass/ne_total},
    }
    out_path = Path(__file__).resolve().parent / "_tmp_recall_compare_result.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果: {out_path}")


if __name__ == "__main__":
    main()
