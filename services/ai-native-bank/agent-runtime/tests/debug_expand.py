#!/usr/bin/env python
"""调试 graph_expand: 检查为什么某些 confusable 兄弟没有被扩召"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter

GRAPH_PATH = Path(r"d:\project\vscode\data\ai-native-bank\intent-graph\graph.ccb.intents.v2.1.0.json")
adapter = V4GraphAdapter.load(GRAPH_PATH)

# 测试查询
queries = ["查开通", "查下现金转出", "查申请进度", "查还完", "查未还金额"]

for query in queries:
    print(f"\n{'='*60}")
    print(f"Query: {query}")

    # 1. 词法召回（不执行 graph_expand）
    from app.intent_graph import IntentCandidate
    normalized = adapter._normalize(query) if hasattr(adapter, '_normalize') else query
    candidates_no_expand = adapter.recall(query, page_context={}, limit=8, graph_expand=False)

    print(f"\n词法召回 ({len(candidates_no_expand)} 个):")
    for c in candidates_no_expand:
        print(f"  {c.intent_id} score={c.score} evidence={c.evidence}")

    # 2. 检查每个 seed 的 confusable 兄弟
    print(f"\nConfusable 兄弟关系:")
    for c in candidates_no_expand:
        confusable = adapter._confusable_siblings.get(c.intent_id, set())
        all_sibs = adapter._siblings.get(c.intent_id, set())
        print(f"  {c.intent_id}:")
        print(f"    confusable_siblings: {sorted(confusable)}")
        print(f"    all_siblings: {sorted(all_sibs)}")

        # 检查每个 confusable 兄弟是否在 _intents_by_id 中
        for sib_id in sorted(confusable):
            intent = adapter._intents_by_id.get(sib_id)
            if intent is None:
                print(f"    ⚠️ {sib_id} 不在 _intents_by_id 中!")
            else:
                lifecycle = intent.get("lifecycle", "active")
                print(f"    ✅ {sib_id} lifecycle={lifecycle}")

    # 3. 完整召回（含 graph_expand）
    candidates_full = adapter.recall(query, page_context={}, limit=8, graph_expand=True)
    print(f"\n完整召回 ({len(candidates_full)} 个):")
    for c in candidates_full:
        tag = " (expanded)" if "graph_expand" in str(c.evidence) else ""
        print(f"  {c.intent_id} score={c.score} evidence={c.evidence}{tag}")
