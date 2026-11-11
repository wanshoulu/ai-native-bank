#!/usr/bin/env python
"""Verify graph version + 4-route recall pipeline + LLM judge trigger.
Uses HTTP API (real runtime) + TuGraph direct query (graph meta)."""
import json, urllib.request, urllib.parse, os
from neo4j import GraphDatabase

URI = os.environ.get("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
USER = os.environ.get("AI_BANK_TUGRAPH_USER", "admin")
PASSWORD = os.environ.get("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
DB = os.environ.get("AI_BANK_TUGRAPH_DATABASE", "default")

def probe(query):
    encoded = urllib.parse.quote(query)
    url = f"http://host.docker.internal:3000/api/ai-bank/v1/intent-recall-probe?message={encoded}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

# 1. Graph version from TuGraph GraphMeta
print("=== 1. GRAPH VERSION (TuGraph GraphMeta) ===")
driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
with driver.session(database=DB) as s:
    # Check GraphMeta node
    r = s.run("MATCH (n:GraphMeta) RETURN n LIMIT 5").values()
    if r:
        meta = r[0][0]
        print(f"GraphMeta node keys: {list(meta.keys())}")
        print(f"  version: {meta.get('version')}")
        print(f"  schemaVersion: {meta.get('schemaVersion')}")
        print(f"  graphId: {meta.get('graphId')}")
    else:
        print("GraphMeta node NOT FOUND")
    # Count IntentNode
    cnt = s.run("MATCH (n:IntentNode) RETURN count(n) AS c").single()["c"]
    print(f"IntentNode count: {cnt}")
driver.close()

# 2. graphVersion from runtime API
print("\n=== 2. GRAPH VERSION (runtime API /intent-recall-probe) ===")
r = probe("还本月信用卡")
data = r.get("data", {})
print(f"API graphVersion: {data.get('graphVersion')}")
print(f"API graphDigest: {data.get('graphDigest', '')[:40]}...")
print(f"API schemaVersion: {data.get('schemaVersion')}")
cands = data.get("candidates", [])
print(f"candidates count: {len(cands)}")
for c in cands[:3]:
    has_warn = "⚠️" in c.get("disambiguationHints", "")
    print(f"  {c.get('intentId')} score={c.get('score')} evidence={c.get('evidence')} warn={has_warn}")

# 3. Test LLM judge trigger with ambiguous query
print("\n=== 3. LLM JUDGE TRIGGER (ambiguous query) ===")
# Ambiguous: "查还款" could mean credit card repayment OR loan repayment
r2 = probe("查还款")
data2 = r2.get("data", {})
cands2 = data2.get("candidates", [])
print(f"query='查还款' candidates count: {len(cands2)}")
for c in cands2[:5]:
    has_warn = "⚠️" in c.get("disambiguationHints", "")
    print(f"  {c.get('intentId')} score={c.get('score')} evidence={c.get('evidence')} warn={has_warn}")

# 4. Test LLM judge trigger with multi-intent query
print("\n=== 4. MULTI-INTENT QUERY (查信用卡还款和贷款还款计划) ===")
r3 = probe("查信用卡还款和贷款还款计划")
data3 = r3.get("data", {})
cands3 = data3.get("candidates", [])
print(f"candidates count: {len(cands3)}")
for c in cands3[:5]:
    has_warn = "⚠️" in c.get("disambiguationHints", "")
    print(f"  {c.get('intentId')} score={c.get('score')} evidence={c.get('evidence')} warn={has_warn}")

print("\n---DONE---")
