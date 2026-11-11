#!/usr/bin/env python
"""验证 RecallSettings 开关可控性：默认行为不变 + 关闭后图扩展/子图召回真的关掉。

测试矩阵：
  1) 默认(graph_expand=True, subgraph=True)：与原版一致，含 graph_expand 证据
  2) graph_expand_enabled=False：召回结果不含 graph_expand 证据
  3) subgraph_enabled=False：SubgraphReasoningV4.enabled 返回 False
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # agent-runtime/
sys.path.insert(0, str(ROOT))

from dataclasses import replace as dc_replace

from app.v4_graph_adapter import V4GraphAdapter  # noqa: E402
from app.subgraph_reasoning_v4 import SubgraphReasoningV4  # noqa: E402
from app.config import RecallSettings  # noqa: E402

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"


def has_graph_expand_evidence(candidates) -> bool:
    return any(
        any("graph_expand" in (ev or "") for ev in (c.evidence or []))
        for c in candidates
    )


def main() -> int:
    failures: list[str] = []
    message = "我的卡"  # 会触发 graph_expand:confusable 的查询（见 test_recall_comparison）

    # ── 1) 默认开关：含 graph_expand 证据 ──
    rs_default = RecallSettings.from_env()
    adapter_default = V4GraphAdapter(
        json.loads(GRAPH_PATH.read_text(encoding="utf-8")),
        GRAPH_PATH,
        recall_settings=rs_default,
    )
    cands_default = adapter_default.recall(message, page_context={}, limit=10)
    has_expand_default = has_graph_expand_evidence(cands_default)
    print(f"[1] 默认开关 graph_expand_enabled={rs_default.graph_expand_enabled}")
    print(f"    subgraph_enabled={rs_default.subgraph_enabled}")
    print(f"    召回 {len(cands_default)} 条，含 graph_expand 证据: {has_expand_default}")
    if not has_expand_default:
        failures.append("默认开关下应含 graph_expand 证据(原版行为不变)")

    # ── 2) graph_expand_enabled=False：不含 graph_expand 证据 ──
    rs_no_expand = dc_replace(rs_default, graph_expand_enabled=False)
    adapter_no_expand = V4GraphAdapter(
        json.loads(GRAPH_PATH.read_text(encoding="utf-8")),
        GRAPH_PATH,
        recall_settings=rs_no_expand,
    )
    cands_no_expand = adapter_no_expand.recall(message, page_context={}, limit=10)
    has_expand_closed = has_graph_expand_evidence(cands_no_expand)
    print(f"[2] graph_expand_enabled=False")
    print(f"    召回 {len(cands_no_expand)} 条，含 graph_expand 证据: {has_expand_closed}")
    if has_expand_closed:
        failures.append("graph_expand_enabled=False 时不应含 graph_expand 证据")

    # ── 3) subgraph_enabled=False：SubgraphReasoningV4.enabled 返回 False ──
    rs_no_sub = dc_replace(rs_default, subgraph_enabled=False)
    sub_reasoning = SubgraphReasoningV4(adapter_default, recall_settings=rs_no_sub)
    print(f"[3] subgraph_enabled=False")
    print(f"    SubgraphReasoningV4.enabled = {sub_reasoning.enabled}")
    if sub_reasoning.enabled:
        failures.append("subgraph_enabled=False 时 SubgraphReasoningV4.enabled 应为 False")

    # ── 4) subgraph_enabled=True(默认)：SubgraphReasoningV4.enabled 返回 True ──
    sub_reasoning_default = SubgraphReasoningV4(adapter_default, recall_settings=rs_default)
    print(f"[4] 默认 subgraph_enabled=True")
    print(f"    SubgraphReasoningV4.enabled = {sub_reasoning_default.enabled}")
    if not sub_reasoning_default.enabled:
        failures.append("默认 subgraph_enabled=True 时 SubgraphReasoningV4.enabled 应为 True")

    print()
    if failures:
        print("❌ 开关可控性测试失败:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("✅ 开关可控性测试通过：默认行为不变 + 开关可关闭对应层")
    return 0


if __name__ == "__main__":
    sys.exit(main())
