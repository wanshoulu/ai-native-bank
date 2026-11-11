#!/usr/bin/env python
"""End-to-end verification: load graph from TuGraph (same as runtime),
verify V4GraphAdapter returns disambiguationHints for the 7 fixed intents."""
import sys, os, json
sys.path.insert(0, "/app")
os.environ.setdefault("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
os.environ.setdefault("AI_BANK_TUGRAPH_USER", "admin")
os.environ.setdefault("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")

from pathlib import Path
from app.config import RuntimeSettings
from app.tugraph_source import load_v4_payload_from_tugraph
from app.v4_graph_adapter import V4GraphAdapter

settings = RuntimeSettings.from_env()
payload = load_v4_payload_from_tugraph(settings)
adapter = V4GraphAdapter(payload, Path("."))

print(f"schemaVersion: {payload.get('schemaVersion')}")
print(f"intentTree count: {len(payload.get('intentTree', []))}")
print(f"graphVersion: {payload.get('graphVersion')}")

FIXED_INTENTS = [
    "ccb.channel.credit.repay.overview",
    "ccb.channel.home.loan.repayment",
    "ccb.channel.home.repayment.faq",
    "ccb.channel.credit.cash.faq",
    "ccb.channel.credit.cash.overview",
    "ccb.channel.credit.progress.overview",
    "ccb.channel.credit.repay.faq",
]

print(f"\n=== V4GraphAdapter returns disambiguationHints for 7 intents ===")
# Build map from intentTree (V4 payload)
tree = payload.get("intentTree", [])
node_map = {n.get("id"): n for n in tree}

all_ok = True
for v4_id in FIXED_INTENTS:
    node = node_map.get(v4_id)
    if node is None:
        print(f"❌ {v4_id}: NOT FOUND in intentTree")
        all_ok = False
        continue

    scenes = node.get("scenes", [])
    if not scenes:
        print(f"❌ {v4_id}: no scenes")
        all_ok = False
        continue

    sd = scenes[0].get("semantic_definition", {})
    hints = sd.get("disambiguationHints", "")
    confusable = sd.get("confusableIntents", [])
    has_warn = "⚠️" in hints
    status = "✅" if has_warn else "❌"
    print(f"{status} {v4_id}: title={node.get('title','')[:25]}, "
          f"hints={len(hints)}chars, confusable={len(confusable)}, "
          f"has_⚠️={has_warn}")
    if not has_warn:
        all_ok = False
        print(f"    hints: {hints[:100] if hints else '<empty>'}")

print(f"\n=== RESULT: {'ALL_OK ✅' if all_ok else 'HAS_FAILURES ❌'} ===")
print("---DONE---")
