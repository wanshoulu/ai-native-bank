#!/usr/bin/env python
"""E2E-5: Verify V4GraphAdapter can consume TuGraph-returned payload correctly.

Compare V4GraphAdapter instantiated with:
  A) TuGraph-returned payload (via tugraph_source.py)
  B) Original V4.1 JSON file

Both should produce identical intent counts, sibling maps, and confusable maps.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tugraph_source import load_v4_payload_from_tugraph
from app.v4_graph_adapter import V4GraphAdapter

# --- Load TuGraph payload ---
class MockSettings:
    tugraph_uri = "bolt://127.0.0.1:7687"
    tugraph_user = "admin"
    tugraph_password = "73@TuGraph"
    tugraph_database = "default"

print("Loading from TuGraph ...")
tg_payload = load_v4_payload_from_tugraph(MockSettings())

# --- Load original JSON ---
json_path = Path("data/ai-native-bank/intent-graph/archive/ccb-mobile-bank-intents.v4.1.json")
print(f"Loading from JSON: {json_path} ...")
with open(json_path, encoding="utf-8") as f:
    json_payload = json.load(f)

# --- Instantiate both adapters ---
skill_bindings_path = Path("data/ai-native-bank/intent-graph/skill-bindings.v1.json")

print("\nInstantiating V4GraphAdapter from TuGraph payload ...")
adapter_tg = V4GraphAdapter(tg_payload, path=skill_bindings_path)

print("Instantiating V4GraphAdapter from JSON payload ...")
adapter_json = V4GraphAdapter(json_payload, path=skill_bindings_path)

# --- Compare ---
all_ok = True

# 1. Intent count
tg_count = len(adapter_tg._intents)
json_count = len(adapter_json._intents)
ok = tg_count == json_count
print(f"\n1. Intent count: TuGraph={tg_count}, JSON={json_count} {'✅' if ok else '❌'}")
all_ok = all_ok and ok

# 2. Intent IDs match
tg_ids = {i["id"] for i in adapter_tg._intents}
json_ids = {i["id"] for i in adapter_json._intents}
missing = json_ids - tg_ids
extra = tg_ids - json_ids
ok = len(missing) == 0 and len(extra) == 0
print(f"2. Intent IDs: missing={len(missing)}, extra={len(extra)} {'✅' if ok else '❌'}")
if missing:
    print(f"   Missing: {list(missing)[:5]}")
if extra:
    print(f"   Extra: {list(extra)[:5]}")
all_ok = all_ok and ok

# 3. Siblings map
tg_sib_keys = set(adapter_tg._siblings.keys())
json_sib_keys = set(adapter_json._siblings.keys())
ok = tg_sib_keys == json_sib_keys
print(f"3. Siblings keys: TuGraph={len(tg_sib_keys)}, JSON={len(json_sib_keys)} {'✅' if ok else '❌'}")
# Check a few sibling sets
mismatches = 0
for k in json_sib_keys:
    tg_set = adapter_tg._siblings.get(k, set())
    json_set = adapter_json._siblings.get(k, set())
    if tg_set != json_set:
        mismatches += 1
        if mismatches <= 3:
            print(f"   MISMATCH for {k}: TG={tg_set}, JSON={json_set}")
ok = mismatches == 0
print(f"   Sibling set mismatches: {mismatches} {'✅' if ok else '❌'}")
all_ok = all_ok and ok

# 4. Confusable siblings map
tg_conf_keys = set(adapter_tg._confusable_siblings.keys())
json_conf_keys = set(adapter_json._confusable_siblings.keys())
ok = tg_conf_keys == json_conf_keys
print(f"4. Confusable siblings keys: TuGraph={len(tg_conf_keys)}, JSON={len(json_conf_keys)} {'✅' if ok else '❌'}")
mismatches = 0
for k in json_conf_keys:
    tg_set = adapter_tg._confusable_siblings.get(k, set())
    json_set = adapter_json._confusable_siblings.get(k, set())
    if tg_set != json_set:
        mismatches += 1
        if mismatches <= 3:
            print(f"   MISMATCH for {k}: TG={tg_set}, JSON={json_set}")
ok = mismatches == 0
print(f"   Confusable set mismatches: {mismatches} {'✅' if ok else '❌'}")
all_ok = all_ok and ok

# 5. Forward confusable map
tg_fwd_keys = set(adapter_tg._forward_confusable.keys())
json_fwd_keys = set(adapter_json._forward_confusable.keys())
ok = tg_fwd_keys == json_fwd_keys
print(f"5. Forward confusable keys: TuGraph={len(tg_fwd_keys)}, JSON={len(json_fwd_keys)} {'✅' if ok else '❌'}")
mismatches = 0
for k in json_fwd_keys:
    tg_set = adapter_tg._forward_confusable.get(k, set())
    json_set = adapter_json._forward_confusable.get(k, set())
    if tg_set != json_set:
        mismatches += 1
ok = mismatches == 0
print(f"   Forward confusable mismatches: {mismatches} {'✅' if ok else '❌'}")
all_ok = all_ok and ok

# 6. V4-to-V2 mapping
ok = adapter_tg._v4_to_v2 == adapter_json._v4_to_v2
print(f"6. V4-to-V2 mapping: {'✅' if ok else '❌'}")
all_ok = all_ok and ok

# 7. Action keys
ok = adapter_tg._action_keys == adapter_json._action_keys
print(f"7. Action keys: TuGraph={len(adapter_tg._action_keys)}, JSON={len(adapter_json._action_keys)} {'✅' if ok else '❌'}")
all_ok = all_ok and ok

# 8. Skill bindings loaded
ok = len(adapter_tg._skill_bindings) == len(adapter_json._skill_bindings)
print(f"8. Skill bindings: TuGraph={len(adapter_tg._skill_bindings)}, JSON={len(adapter_json._skill_bindings)} {'✅' if ok else '❌'}")
all_ok = all_ok and ok

# 9. Digest
ok = adapter_tg.digest == adapter_json.digest
print(f"9. Digest: {'✅' if ok else '❌'}")
print(f"   TuGraph: {adapter_tg.digest}")
print(f"   JSON:    {adapter_json.digest}")
all_ok = all_ok and ok

# 10. Spot check: get an intent and verify its confusableIntents
print("\n--- Spot check: intent 'ccb.credit.apply.overview' ---")
test_id = "ccb.credit.apply.overview"
tg_intent = adapter_tg._intents_by_id.get(test_id)
json_intent = adapter_json._intents_by_id.get(test_id)
if tg_intent and json_intent:
    print(f"  TuGraph confusableIntents: {tg_intent.get('confusableIntents', [])}")
    print(f"  JSON confusableIntents:    {json_intent.get('confusableIntents', [])}")
    ok = tg_intent.get("confusableIntents", []) == json_intent.get("confusableIntents", [])
    print(f"  Match: {'✅' if ok else '❌'}")
    all_ok = all_ok and ok

    # Check siblings
    tg_sibs = adapter_tg._siblings.get(test_id, set())
    json_sibs = adapter_json._siblings.get(test_id, set())
    print(f"  TuGraph siblings: {len(tg_sibs)}")
    print(f"  JSON siblings:    {len(json_sibs)}")
    ok = tg_sibs == json_sibs
    print(f"  Siblings match: {'✅' if ok else '❌'}")
    all_ok = all_ok and ok
else:
    print(f"  Intent not found: TG={tg_intent is not None}, JSON={json_intent is not None}")
    all_ok = False

print(f"\n{'='*50}")
print(f"E2E-5 Result: {'ALL OK ✅ — V4GraphAdapter works identically with TuGraph data' if all_ok else 'HAS FAILURES ❌'}")
print(f"{'='*50}")
