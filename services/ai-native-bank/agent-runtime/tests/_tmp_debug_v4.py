#!/usr/bin/env python
"""Debug V4-014 and V4-037 candidate scoring"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import V4GraphAdapter

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"

adapter = V4GraphAdapter.load(GRAPH_PATH)

for query in ["查下我的房贷还款计划", "怎么还"]:
    print(f"\n{'='*80}")
    print(f"Query: {query}")
    print(f"{'='*80}")

    cands = adapter.recall(query, page_context={}, limit=10)
    print(f"\nCandidates ({len(cands)}):")
    for c in cands:
        print(f"  {c.intent_id:45s} score={c.score:4d}  evidence={c.evidence}")

    # Show direct vs expand
    direct = [c for c in cands if not any("expand" in e for e in c.evidence)]
    expanded = [c for c in cands if any("expand" in e for e in c.evidence)]
    print(f"\nDirect: {len(direct)}, Expanded: {len(expanded)}")

    # For each direct candidate, show siblings
    for c in direct:
        conf = adapter._confusable_siblings.get(c.intent_id, set())
        sib = adapter._siblings.get(c.intent_id, set())
        reg = sib - conf
        print(f"\n  Seed {c.intent_id} (score={c.score}):")
        print(f"    confusable_siblings: {sorted(conf)}")
        print(f"    regular_siblings:    {sorted(reg)}")
