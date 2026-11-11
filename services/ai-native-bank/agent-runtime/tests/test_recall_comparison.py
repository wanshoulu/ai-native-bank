#!/usr/bin/env python
"""意图召回对比测试：验证 confusableIntents 修复 + 直通关闭 + 多路证据汇入的效果。

测试维度：
  - 单意图 (easy / medium / hard)
  - 多意图 (clause-boundary 拆分)
  - 模糊意图 (跨域易混淆)
  - 非任务 (寒暄)

对比方式：
  A) fixed   — 当前修复后的 V4 适配器（confusableIntents 从 semantic_definition 读取）
  B) buggy   — 模拟修复前（confusableIntents 恒为空，disambiguationHints 恒 None）

两者共享同一份图谱数据、同一份归一化/拆分逻辑，唯一差异就是消歧元数据是否生效。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

# ── 路径准备 ──
ROOT = Path(__file__).resolve().parent.parent  # agent-runtime/
sys.path.insert(0, str(ROOT))

from app.intent_shape import analyze_intent_shape  # noqa: E402
from app.v4_graph_adapter import V4GraphAdapter  # noqa: E402

# tests/ -> agent-runtime/ -> ai-native-bank/ -> services/ -> vscode/
GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"


# ── 测试集 ──
@dataclass(frozen=True)
class TestCase:
    message: str
    expected_intent: str          # 期望的 top-1 intent_id（None 表示不期望召回）
    expected_in_candidates: str   # 至少应出现在候选集中（None 表示不适用）
    category: str                 # single_easy / single_medium / single_hard / multi / ambiguous / non_task
    difficulty: str               # easy / medium / hard
    note: str                     # 设计说明


TEST_CASES: list[TestCase] = [
    # ── 单意图·简单：精确别名命中 ──
    TestCase("查余额", "ccb.account.balance.query", "ccb.account.balance.query",
             "single_easy", "easy", "精确别名命中，无歧义"),
    TestCase("我的信用卡", "ccb.credit.card.query", "ccb.credit.card.query",
             "single_easy", "easy", "精确别名命中信用卡查询"),

    # ── 单意图·中等：口语化改写，非精确别名 ──
    TestCase("看下我卡里还剩多少钱", "ccb.account.balance.query", "ccb.account.balance.query",
             "single_medium", "medium", "口语改写，别名'卡里还剩多少钱'精确命中（数据补充后）"),
    TestCase("信用卡这个月要还多少", "ccb.credit.repay.overview", "ccb.credit.repay.overview",
             "single_medium", "medium", "还款试算，别名'信用卡这个月要还多少'精确命中（数据补充后）"),

    # ── 单意图·困难：跨域易混淆，词法可能误导 ──
    TestCase("银行卡还能用多少钱", "ccb.account.balance.query", "ccb.account.balance.query",
             "single_hard", "hard", "'卡'字跨域(储蓄卡/信用卡)，别名'银行卡还能用多少钱'精确命中（数据补充后）"),
    TestCase("我的信用卡这个月还多少", "ccb.credit.repay.overview", "ccb.credit.repay.overview",
             "single_hard", "hard", "'我的信用卡'精确命中 card.query 但真正意图是还款，repay.overview 也在候选中"),
    TestCase("卡里还有多少钱", "ccb.account.balance.query", "ccb.account.balance.query",
             "single_hard", "hard", "'卡'模糊，但 account.query 别名'卡里还有多少钱'精确命中"),

    # ── 多意图：clause-boundary 拆分 ──
    TestCase("查下余额并且看看信用卡还欠多少", None, "ccb.account.balance.query",
             "multi", "medium", "两子句拆分：account.query + repay.overview"),
    TestCase("查下卡里多少钱再看看信用卡欠多少", None, "ccb.credit.repay.overview",
             "multi", "hard", "口语化多意图，'再'拆分"),

    # ── 模糊意图 ──
    TestCase("我的卡", None, "ccb.credit.card.query",
             "ambiguous", "medium", "'我的卡'泛化别名已修正为'我的信用卡'，现在不再命中 card.query"),
    TestCase("还多少", None, "ccb.credit.repay.overview",
             "ambiguous", "hard", "极短输入，'还欠' recallTerm 命中 repay.overview（数据补充后）"),

    # ── 非任务 ──
    TestCase("你好", None, None,
             "non_task", "easy", "寒暄，期望零召回"),
    TestCase("今天天气怎么样", None, None,
             "non_task", "easy", "寒暄，期望零召回"),
]


# ── 构建 buggy 适配器（模拟修复前）──
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


def run_recall(adapter: V4GraphAdapter, message: str, page_context: dict | None = None):
    """执行召回，返回候选列表。"""
    return adapter.recall(message, page_context=page_context or {}, limit=8)


def format_candidates(candidates, max_show: int = 5) -> str:
    if not candidates:
        return "  (无候选)"
    lines = []
    for i, c in enumerate(candidates[:max_show]):
        confusable = f" confusable={list(c.confusable_intents)}" if c.confusable_intents else ""
        hints = f" hints={c.disambiguation_hints[:40]}..." if c.disambiguation_hints else ""
        lines.append(
            f"  [{i}] {c.intent_id}  score={c.score}  evidence={c.evidence}{confusable}{hints}"
        )
    return "\n".join(lines)


def check_result(tc: TestCase, candidates) -> dict:
    """检查召回结果是否符合预期。"""
    ids = [c.intent_id for c in candidates]
    top1 = ids[0] if ids else None

    checks = {
        "top1_match": top1 == tc.expected_intent if tc.expected_intent else None,
        "in_candidates": tc.expected_in_candidates in ids if tc.expected_in_candidates else None,
        "recall_count": len(ids),
    }
    return checks


def main():
    print("=" * 80)
    print("意图召回对比测试：confusableIntents 修复效果验证")
    print(f"图谱: {GRAPH_PATH}")
    print("=" * 80)

    # 加载两个适配器
    fixed_adapter = V4GraphAdapter.load(GRAPH_PATH)
    buggy_adapter = make_buggy_adapter(GRAPH_PATH)

    # 统计 confusableIntents 覆盖情况
    fixed_confusable_count = sum(
        1 for intent in fixed_adapter._intents if intent.get("confusableIntents")
    )
    buggy_confusable_count = sum(
        1 for intent in buggy_adapter._intents if intent.get("confusableIntents")
    )
    print(f"\n[元数据覆盖] confusableIntents 非空的意图数:")
    print(f"  fixed (修复后): {fixed_confusable_count}")
    print(f"  buggy (修复前): {buggy_confusable_count}")

    # 兄弟关系统计
    fixed_sibling_edges = sum(len(v) for v in fixed_adapter._siblings.values())
    buggy_sibling_edges = sum(len(v) for v in buggy_adapter._siblings.values())
    print(f"\n[兄弟关系] 跨域兄弟边数:")
    print(f"  fixed (修复后): {fixed_sibling_edges}")
    print(f"  buggy (修复前): {buggy_sibling_edges}")

    # 逐用例测试
    results = []
    for tc in TEST_CASES:
        print(f"\n{'─' * 80}")
        print(f"[{tc.category}/{tc.difficulty}] 「{tc.message}」")
        print(f"  期望: top1={tc.expected_intent}, in_candidates={tc.expected_in_candidates}")
        print(f"  说明: {tc.note}")

        # intent_shape 分析
        shape = analyze_intent_shape(tc.message)
        print(f"  shape: {shape.shape_hint}  segments={list(shape.segments)}  evidence={list(shape.evidence)}")

        # fixed 召回
        fixed_candidates = run_recall(fixed_adapter, tc.message)
        fixed_checks = check_result(tc, fixed_candidates)
        print(f"\n  ── fixed (修复后) ──")
        print(format_candidates(fixed_candidates))

        # buggy 召回
        buggy_candidates = run_recall(buggy_adapter, tc.message)
        buggy_checks = check_result(tc, buggy_candidates)
        print(f"\n  ── buggy (修复前) ──")
        print(format_candidates(buggy_candidates))

        # 对比
        improved = []
        regressed = []
        if fixed_checks["top1_match"] != buggy_checks["top1_match"]:
            if fixed_checks["top1_match"] is True:
                improved.append("top1 从错误→正确")
            elif buggy_checks["top1_match"] is True:
                regressed.append("top1 从正确→错误")

        if fixed_checks["in_candidates"] != buggy_checks["in_candidates"]:
            if fixed_checks["in_candidates"] is True:
                improved.append("期望意图从缺失→进入候选集")
            elif buggy_checks["in_candidates"] is True:
                regressed.append("期望意图从候选集→缺失")

        if improved:
            print(f"\n  ✅ 改善: {'; '.join(improved)}")
        if regressed:
            print(f"\n  ⚠️  回退: {'; '.join(regressed)}")
        if not improved and not regressed:
            status = "✅" if fixed_checks["top1_match"] else ("⚠️" if tc.expected_intent else "➖")
            print(f"\n  {status} 无变化")

        results.append({
            "tc": tc,
            "fixed_top1": fixed_candidates[0].intent_id if fixed_candidates else None,
            "buggy_top1": buggy_candidates[0].intent_id if buggy_candidates else None,
            "fixed_checks": fixed_checks,
            "buggy_checks": buggy_checks,
            "improved": improved,
            "regressed": regressed,
        })

    # 汇总
    print(f"\n{'=' * 80}")
    print("汇总")
    print("=" * 80)

    total = len(results)
    fixed_top1_pass = sum(1 for r in results if r["fixed_checks"]["top1_match"] is True)
    buggy_top1_pass = sum(1 for r in results if r["buggy_checks"]["top1_match"] is True)
    fixed_in_candidates = sum(1 for r in results if r["fixed_checks"]["in_candidates"] is True)
    buggy_in_candidates = sum(1 for r in results if r["buggy_checks"]["in_candidates"] is True)
    improved_count = sum(1 for r in results if r["improved"])
    regressed_count = sum(1 for r in results if r["regressed"])

    # 只统计有期望 top1 的用例
    top1_total = sum(1 for r in results if r["tc"].expected_intent is not None)
    in_cand_total = sum(1 for r in results if r["tc"].expected_in_candidates is not None)

    print(f"\n  top1 正确率 (有期望的 {top1_total} 条):")
    print(f"    fixed: {fixed_top1_pass}/{top1_total}")
    print(f"    buggy: {buggy_top1_pass}/{top1_total}")

    print(f"\n  期望意图在候选集中 ({in_cand_total} 条):")
    print(f"    fixed: {fixed_in_candidates}/{in_cand_total}")
    print(f"    buggy: {buggy_in_candidates}/{in_cand_total}")

    print(f"\n  改善用例: {improved_count}")
    print(f"  回退用例: {regressed_count}")

    # 明细表
    print(f"\n{'─' * 80}")
    print(f"{'用例':<28} {'类别':<16} {'buggy top1':<40} {'fixed top1':<40} {'变化'}")
    print(f"{'─' * 80}")
    for r in results:
        msg = r["tc"].message[:26]
        cat = r["tc"].category
        b_top = (r["buggy_top1"] or "(无)")[:38]
        f_top = (r["fixed_top1"] or "(无)")[:38]
        if r["improved"]:
            change = "✅ 改善"
        elif r["regressed"]:
            change = "⚠️ 回退"
        elif r["fixed_checks"]["top1_match"] is True:
            change = "✅ 正确"
        elif r["tc"].expected_intent is None and r["fixed_checks"]["recall_count"] == 0:
            change = "➖ 零召回(正确)"
        else:
            change = "❌ 仍错"
        print(f"{msg:<28} {cat:<16} {b_top:<40} {f_top:<40} {change}")

    print(f"\n{'=' * 80}")


if __name__ == "__main__":
    main()
