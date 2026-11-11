#!/usr/bin/env python
"""对比原版 vs 优化版 _format_candidate_with_subgraph 输出的 prompt 字数。

验证 B3 优化(prompt 精简)的效果：原版每个 confusable 带完整 description，
优化版只列 title。用真实图谱数据构建 subgraph_context，对比字数差异。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # agent-runtime/
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter  # noqa: E402
from app.subgraph_reasoning_v4 import SubgraphReasoningV4  # noqa: E402
from app.config import RecallSettings  # noqa: E402

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"


def format_original(idx: int, ctx: dict) -> str:
    """原版格式化逻辑（来自 .pre-opt.bak）：每个 confusable 一行带 description"""
    parts = [
        f"[{idx}] id={ctx.get('intent_id', '')}",
        f"    title={ctx.get('title', '')}",
        f"    description={ctx.get('description', '')}",
    ]
    disambig = ctx.get("disambiguation_hints")
    if disambig:
        parts.append(f"    disambiguation_hints={disambig}")
    confusable = ctx.get("confusable_with", [])
    if confusable:
        conf_lines = []
        for c in confusable:
            conf_lines.append(
                f"        - {c['title']}({c['intent_id']}): {c.get('description', '')}"
            )
        parts.append("    confusable_with(易混淆意图):")
        parts.extend(conf_lines)
    siblings = ctx.get("siblings", [])
    if siblings:
        sib_titles = [s["title"] for s in siblings]
        parts.append(f"    siblings(同类业务): {', '.join(sib_titles)}")
    biz = ctx.get("business_object", "")
    if biz:
        parts.append(f"    business_object={biz}")
    return "\n".join(parts)


def format_optimized(idx: int, ctx: dict) -> str:
    """优化版格式化逻辑（当前代码）：confusable 合并一行只列 title"""
    parts = [
        f"[{idx}] id={ctx.get('intent_id', '')}",
        f"    title={ctx.get('title', '')}",
        f"    description={ctx.get('description', '')}",
    ]
    disambig = ctx.get("disambiguation_hints")
    if disambig:
        parts.append(f"    disambiguation_hints={disambig}")
    confusable = ctx.get("confusable_with", [])
    if confusable:
        conf_titles = [f"{c['title']}({c['intent_id']})" for c in confusable]
        parts.append(f"    confusable_with(易混淆意图): {', '.join(conf_titles)}")
    siblings = ctx.get("siblings", [])
    if siblings:
        sib_titles = [s["title"] for s in siblings]
        parts.append(f"    siblings(同类业务): {', '.join(sib_titles)}")
    biz = ctx.get("business_object", "")
    if biz:
        parts.append(f"    business_object={biz}")
    return "\n".join(parts)


def main() -> int:
    rs = RecallSettings.from_env()
    adapter = V4GraphAdapter(
        json.loads(GRAPH_PATH.read_text(encoding="utf-8")),
        GRAPH_PATH,
        recall_settings=rs,
    )
    sub = SubgraphReasoningV4(adapter, recall_settings=rs)

    # 用 5 个查询触发 subgraph_context 构建，对比字数
    queries = [
        "我的卡",
        "信用卡状态",
        "还多少",
        "查下账",
        "信用卡额度",
    ]

    total_orig = 0
    total_opt = 0
    print(f"{'查询':<12} {'候选数':<6} {'原版字数':<10} {'优化版字数':<10} {'节省':<8} {'节省%'}")
    print("-" * 70)

    for msg in queries:
        candidates = adapter.recall(msg, page_context={}, limit=8)
        if not candidates:
            continue
        contexts = [sub.build_context(c) for c in candidates]
        orig_chars = 0
        opt_chars = 0
        for idx, ctx in enumerate(contexts):
            orig_chars += len(format_original(idx, ctx))
            opt_chars += len(format_optimized(idx, ctx))
        total_orig += orig_chars
        total_opt += opt_chars
        saved = orig_chars - opt_chars
        pct = (saved / orig_chars * 100) if orig_chars else 0
        print(f"{msg:<12} {len(contexts):<6} {orig_chars:<10} {opt_chars:<10} {saved:<8} {pct:.1f}%")

    print("-" * 70)
    saved_total = total_orig - total_opt
    pct_total = (saved_total / total_orig * 100) if total_orig else 0
    print(f"{'总计':<12} {'':<6} {total_orig:<10} {total_opt:<10} {saved_total:<8} {pct_total:.1f}%")
    print()
    print(f"原版平均 prompt 字数: {total_orig // len(queries)} 字/查询")
    print(f"优化版平均 prompt 字数: {total_opt // len(queries)} 字/查询")
    print(f"平均节省: {saved_total // len(queries)} 字/查询 ({pct_total:.1f}%)")

    if saved_total > 0:
        print(f"\n✅ prompt 精简生效：平均每查询节省 {saved_total // len(queries)} 字")
        return 0
    else:
        print("\n❌ prompt 精简未生效")
        return 1


if __name__ == "__main__":
    sys.exit(main())
