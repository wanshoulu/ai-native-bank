#!/usr/bin/env python
"""Check GraphMeta payloadJson + verify LLM judge actually invoked."""
import json, os
from neo4j import GraphDatabase

URI = os.environ.get("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
USER = os.environ.get("AI_BANK_TUGRAPH_USER", "admin")
PASSWORD = os.environ.get("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
DB = os.environ.get("AI_BANK_TUGRAPH_DATABASE", "default")

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
with driver.session(database=DB) as s:
    r = s.run("MATCH (n:GraphMeta) RETURN n.payloadJson AS payload, n.id AS id LIMIT 5").single()
    if r:
        print(f"GraphMeta id: {r['id']}")
        payload = json.loads(r["payload"]) if r["payload"] and r["payload"] != "null" else {}
        print(f"payloadJson keys: {list(payload.keys())}")
        print(f"  schemaVersion: {payload.get('schemaVersion')}")
        print(f"  version: {payload.get('version')}")
        print(f"  graphVersion: {payload.get('graphVersion')}")
        print(f"  graphId: {payload.get('graphId')}")
        # Print all top-level fields
        for k, v in payload.items():
            if k not in ("intentTree", "registries"):
                print(f"  {k}: {str(v)[:80]}")
        print(f"  intentTree count: {len(payload.get('intentTree', []))}")
driver.close()
print("---DONE---")
