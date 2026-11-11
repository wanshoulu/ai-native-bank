#!/usr/bin/env python
"""Dump full scenesJson for the 'failed' intents to confirm other_fields_ok=False
is pre-existing (not introduced by our write)."""
from neo4j import GraphDatabase
import json
import os

URI = os.environ.get("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
USER = os.environ.get("AI_BANK_TUGRAPH_USER", "admin")
PASSWORD = os.environ.get("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
DB = os.environ.get("AI_BANK_TUGRAPH_DATABASE", "default")

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
with driver.session(database=DB) as s:
    for nid in ["ccb.channel.home.repayment.faq", "ccb.channel.credit.cash.faq"]:
        r = s.run(
            "MATCH (n:IntentNode {id: $id}) RETURN n.scenesJson AS scenes, n.title AS title",
            id=nid,
        ).single()
        print(f"\n=== {nid} (title={r['title']}) ===")
        scenes = json.loads(r["scenes"]) if r["scenes"] and r["scenes"] != "null" else []
        print(f"scenes count: {len(scenes)}")
        if scenes:
            print(f"scene[0] keys: {list(scenes[0].keys())}")
            # Print non-semantic fields
            for k, v in scenes[0].items():
                if k != "semantic_definition":
                    print(f"  {k}: {type(v).__name__} = {str(v)[:80]}")
            sd = scenes[0].get("semantic_definition", {})
            print(f"semantic_definition keys: {list(sd.keys())}")
print("---DONE---")
