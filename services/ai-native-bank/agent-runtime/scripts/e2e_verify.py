#!/usr/bin/env python
"""E2E test: verify TuGraph data matches JSON and tugraph_source reads back correctly."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neo4j import GraphDatabase

# Load original V4.1 JSON
with open("data/ai-native-bank/intent-graph/archive/ccb-mobile-bank-intents.v4.1.json", encoding="utf-8") as f:
    json_graph = json.load(f)

driver = GraphDatabase.driver("bolt://127.0.0.1:7687", auth=("admin", "73@TuGraph"))
all_ok = True

with driver.session(database="default") as s:
    print("=== E2E-3: Verify TuGraph data matches JSON ===\n")

    # 1. IntentNode count
    r = s.run("MATCH (n:IntentNode) RETURN count(n) AS cnt").single()
    tg_nodes = r["cnt"] if r else 0
    json_nodes = len(json_graph["intentTree"])
    ok = tg_nodes == json_nodes
    print(f"IntentNode: TuGraph={tg_nodes}, JSON={json_nodes} {'✅' if ok else '❌'}")
    all_ok = all_ok and ok

    # 2. CONFUSABLE_WITH edge count
    r = s.run("MATCH ()-[r:CONFUSABLE_WITH]->() RETURN count(r) AS cnt").single()
    tg_edges = r["cnt"] if r else 0
    json_edges = sum(1 for rel in json_graph["relations"] if rel["type"] == "confusable_with")
    ok = tg_edges == json_edges
    print(f"CONFUSABLE_WITH: TuGraph={tg_edges}, JSON={json_edges} {'✅' if ok else '❌'}")
    all_ok = all_ok and ok

    # 3. GraphMeta
    r = s.run("MATCH (m:GraphMeta {id: 'meta'}) RETURN m.payloadJson AS p").single()
    ok = r is not None
    print(f"GraphMeta: {'present ✅' if ok else 'MISSING ❌'}")
    all_ok = all_ok and ok

    # 4. RegistryNode count
    r = s.run("MATCH (n:RegistryNode) RETURN count(n) AS cnt").single()
    tg_regs = r["cnt"] if r else 0
    json_regs = sum(1 for v in json_graph.get("resourceRegistries", {}).values() if v)
    ok = tg_regs == json_regs
    print(f"RegistryNode: TuGraph={tg_regs}, JSON={json_regs} {'✅' if ok else '❌'}")
    all_ok = all_ok and ok

    # 5. Spot check: verify a specific node's data
    print("\n--- Spot check: node 'ccb.channel.credit.apply.faq' ---")
    r = s.run(
        "MATCH (n:IntentNode {id: 'ccb.channel.credit.apply.faq'}) "
        "RETURN n.id AS id, n.title AS title, n.intentType AS intentType, "
        "n.scenesJson AS scenesJson"
    ).single()
    if r:
        print(f"  id={r['id']}, title={r['title']}, intentType={r['intentType']}")
        json_node = next((n for n in json_graph["intentTree"] if n["id"] == "ccb.channel.credit.apply.faq"), None)
        if json_node:
            json_it = json_node.get("scenes", [{}])[0].get("semantic_definition", {}).get("intentType", "")
            ok = r["intentType"] == json_it
            print(f"  intentType match: TuGraph={r['intentType']}, JSON={json_it} {'✅' if ok else '❌'}")
            all_ok = all_ok and ok
    else:
        print("  NOT FOUND ❌")
        all_ok = False

    # 6. Spot check: verify a specific edge
    print("\n--- Spot check: edge ccb.channel.credit.apply.faq -> ccb.channel.credit.apply.overview ---")
    r = s.run(
        "MATCH (a:IntentNode {id: 'ccb.channel.credit.apply.faq'})-[r:CONFUSABLE_WITH]->(b:IntentNode {id: 'ccb.channel.credit.apply.overview'}) "
        "RETURN a.id AS src, b.id AS dst, r.relType AS relType, r.evidence AS evidence"
    ).single()
    if r:
        print(f"  src={r['src']}, dst={r['dst']}, relType={r['relType']}, evidence={r['evidence']}")
        # Find matching JSON relation
        json_rel = next((rel for rel in json_graph["relations"]
                         if rel["type"] == "confusable_with"
                         and "apply.faq#HELP" in rel.get("from", "")
                         and "apply.overview#QUERY" in rel.get("to", "")), None)
        if json_rel:
            print(f"  JSON: from={json_rel['from']}, to={json_rel['to']}, evidence={json_rel.get('evidence','')}")
            ok = r["relType"] == json_rel["type"]
            print(f"  relType match: {'✅' if ok else '❌'}")
            all_ok = all_ok and ok
        else:
            print("  JSON relation not found ⚠️")
    else:
        print("  Edge NOT FOUND ❌")
        all_ok = False

driver.close()

print(f"\n=== E2E-3 Result: {'ALL OK ✅' if all_ok else 'HAS FAILURES ❌'} ===\n")

# === E2E-4: Verify tugraph_source.py can read back complete data ===
print("=== E2E-4: Verify tugraph_source.py reads back complete data ===\n")

from app.tugraph_source import load_v4_payload_from_tugraph

class MockSettings:
    tugraph_uri = "bolt://127.0.0.1:7687"
    tugraph_user = "admin"
    tugraph_password = "73@TuGraph"
    tugraph_database = "default"

payload = load_v4_payload_from_tugraph(MockSettings())

e2e4_ok = True

# Check basic structure
print(f"schemaVersion: {payload.get('schemaVersion', '?')}")
print(f"graphId: {payload.get('graphId', '?')}")
print(f"intentTree nodes: {len(payload.get('intentTree', []))}")
print(f"relations: {len(payload.get('relations', []))}")
print(f"resourceRegistries keys: {list(payload.get('resourceRegistries', {}).keys())}")

# Verify node count
ok = len(payload["intentTree"]) == len(json_graph["intentTree"])
print(f"\nNode count match: {len(payload['intentTree'])} vs {len(json_graph['intentTree'])} {'✅' if ok else '❌'}")
e2e4_ok = e2e4_ok and ok

# Verify relation count
ok = len(payload["relations"]) == sum(1 for r in json_graph["relations"] if r["type"] == "confusable_with")
print(f"Relation count match: {len(payload['relations'])} vs {sum(1 for r in json_graph['relations'] if r['type'] == 'confusable_with')} {'✅' if ok else '❌'}")
e2e4_ok = e2e4_ok and ok

# Verify relation from/to have #suffix (reconstructed)
print("\n--- Verify relation from/to have #suffix (reconstructed) ---")
suffix_ok = 0
suffix_fail = 0
for rel in payload["relations"]:
    for field in ["from", "to"]:
        val = rel.get(field, "")
        if "#" in val:
            suffix_ok += 1
        else:
            suffix_fail += 1
            if suffix_fail <= 3:
                print(f"  MISSING #suffix: {field}={val}")

print(f"  With #suffix: {suffix_ok}, Without: {suffix_fail}")
ok = suffix_fail == 0
print(f"  All relations have #suffix: {'✅' if ok else '❌'}")
e2e4_ok = e2e4_ok and ok

# Verify relation from/to match original JSON exactly
print("\n--- Verify relation from/to match original JSON ---")
json_rels_set = set()
for rel in json_graph["relations"]:
    if rel["type"] == "confusable_with":
        json_rels_set.add((rel["from"], rel["to"]))

tg_rels_set = set()
for rel in payload["relations"]:
    tg_rels_set.add((rel["from"], rel["to"]))

missing = json_rels_set - tg_rels_set
extra = tg_rels_set - json_rels_set
print(f"  JSON relations: {len(json_rels_set)}, TuGraph relations: {len(tg_rels_set)}")
print(f"  Missing (in JSON but not TuGraph): {len(missing)}")
if missing:
    for m in list(missing)[:3]:
        print(f"    {m}")
print(f"  Extra (in TuGraph but not JSON): {len(extra)}")
ok = len(missing) == 0 and len(extra) == 0
print(f"  Exact match: {'✅' if ok else '❌'}")
e2e4_ok = e2e4_ok and ok

# Verify registries
print("\n--- Verify resourceRegistries ---")
json_regs = json_graph.get("resourceRegistries", {})
tg_regs = payload.get("resourceRegistries", {})
for key in json_regs:
    if json_regs[key]:
        ok = key in tg_regs and bool(tg_regs[key])
        print(f"  {key}: JSON={len(json_regs[key])}, TuGraph={'present' if key in tg_regs and tg_regs[key] else 'MISSING'} {'✅' if ok else '❌'}")
        e2e4_ok = e2e4_ok and ok

# Verify digest
json_digest = json_graph.get("digest", "")
tg_digest = payload.get("digest", "")
ok = json_digest == tg_digest
print(f"\nDigest match: {'✅' if ok else '❌'}")
print(f"  JSON:   {json_digest}")
print(f"  TuGraph: {tg_digest}")
e2e4_ok = e2e4_ok and ok

print(f"\n=== E2E-4 Result: {'ALL OK ✅' if e2e4_ok else 'HAS FAILURES ❌'} ===")

# Overall
print(f"\n{'='*50}")
print(f"OVERALL: {'ALL E2E TESTS PASSED ✅' if all_ok and e2e4_ok else 'HAS FAILURES ❌'}")
print(f"{'='*50}")
