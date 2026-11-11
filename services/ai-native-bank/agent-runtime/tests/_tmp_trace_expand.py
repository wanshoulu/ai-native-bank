#!/usr/bin/env python
"""Trace graph_expand step by step for V4-037"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.v4_graph_adapter import (
    V4GraphAdapter,
    _GRAPH_EXPAND_MAX_PER_SEED,
    _GRAPH_EXPAND_MAX_TOTAL,
    _GRAPH_EXPAND_CONFUSABLE_WEIGHT,
    _GRAPH_EXPAND_CONFUSABLE_CAP,
    _GRAPH_EXPAND_EDGE_WEIGHTS,
    _GRAPH_EXPAND_MAX_SCORE,
    _SCORE_DISCARD_THRESHOLD,
)

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
adapter = V4GraphAdapter.load(GRAPH_PATH)

query = "怎么还"
from app.v4_graph_adapter import _normalize
normalized = _normalize(query)

# Reproduce recall up to graph_expand
candidates = []
for intent in adapter._intents:
    if intent.get("lifecycle", "active") != "active":
        continue
    score = 0
    evidence = []
    alias_score = 0
    for alias in intent.get("aliases", []):
        an = _normalize(alias)
        if len(an) >= 2 and (an in normalized or normalized in an):
            if an == normalized:
                alias_score = 110
                break
            elif alias_score < 75:
                alias_score = 75
    if alias_score > 0:
        score += alias_score
        evidence.append("alias")
    for term in intent.get("recallTerms", []):
        tn = _normalize(term)
        if len(tn) >= 2 and tn in normalized:
            score += 90
            evidence.append("graph_term")
            break
    if score < 60:
        continue
    candidates.append(adapter._build_candidate(intent, score=score, evidence=evidence))

print(f"Direct candidates ({len(candidates)}):")
for c in candidates:
    print(f"  {c.intent_id:45s} score={c.score}")

# Now trace graph_expand
existing_ids = {c.intent_id for c in candidates}
expanded = []
total_added = 0

print(f"\nConstants: MAX_PER_SEED={_GRAPH_EXPAND_MAX_PER_SEED}, MAX_TOTAL={_GRAPH_EXPAND_MAX_TOTAL}")
print(f"  CONFUSABLE_WEIGHT={_GRAPH_EXPAND_CONFUSABLE_WEIGHT}, CONFUSABLE_CAP={_GRAPH_EXPAND_CONFUSABLE_CAP}")
print(f"  OFFERS_WEIGHT={_GRAPH_EXPAND_EDGE_WEIGHTS['offers']}, MAX_SCORE={_GRAPH_EXPAND_MAX_SCORE}")
print(f"  DISCARD_THRESHOLD={_SCORE_DISCARD_THRESHOLD}")

for seed in candidates:
    if total_added >= _GRAPH_EXPAND_MAX_TOTAL:
        print(f"\n  [BREAK] total_added={total_added} >= MAX_TOTAL")
        break
    added_for_seed = 0
    confusable_ids = adapter._confusable_siblings.get(seed.intent_id, set())
    all_siblings = adapter._siblings.get(seed.intent_id, set())
    regular_ids = all_siblings - confusable_ids

    print(f"\n  Seed: {seed.intent_id} (score={seed.score})")
    print(f"    confusable_ids: {sorted(confusable_ids)}")
    print(f"    regular_ids: {sorted(regular_ids)}")

    for sibling_id in list(confusable_ids) + list(regular_ids):
        if added_for_seed >= _GRAPH_EXPAND_MAX_PER_SEED:
            print(f"    [BREAK seed] added_for_seed={added_for_seed} >= MAX_PER_SEED")
            break
        if total_added >= _GRAPH_EXPAND_MAX_TOTAL:
            print(f"    [BREAK total] total_added={total_added} >= MAX_TOTAL")
            break
        if sibling_id in existing_ids:
            print(f"    SKIP {sibling_id} (already in existing_ids)")
            continue

        intent = adapter._intents_by_id.get(sibling_id)
        if intent is None or intent.get("lifecycle", "active") != "active":
            print(f"    SKIP {sibling_id} (not found or inactive)")
            continue

        is_confusable = sibling_id in confusable_ids
        if is_confusable:
            expansion_score = min(int(seed.score * _GRAPH_EXPAND_CONFUSABLE_WEIGHT), _GRAPH_EXPAND_CONFUSABLE_CAP)
            label = "confusable"
        else:
            expansion_score = min(int(seed.score * _GRAPH_EXPAND_EDGE_WEIGHTS["offers"]), _GRAPH_EXPAND_MAX_SCORE)
            label = "offers"

        discarded = expansion_score < _SCORE_DISCARD_THRESHOLD
        print(f"    {'ADD' if not discarded else 'DISCARD'} {sibling_id} score={expansion_score} ({label})")
        if discarded:
            continue

        expanded.append((sibling_id, expansion_score, label))
        existing_ids.add(sibling_id)
        added_for_seed += 1
        total_added += 1

print(f"\nExpanded ({len(expanded)}):")
for sid, sc, label in expanded:
    print(f"  {sid:45s} score={sc} ({label})")

print(f"\nTotal candidates: {len(candidates) + len(expanded)}")
print(f"ccb.credit.repay.repay in expanded: {'ccb.credit.repay.repay' in [e[0] for e in expanded]}")
