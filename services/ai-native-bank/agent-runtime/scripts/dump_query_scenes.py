#!/usr/bin/env python
from neo4j import GraphDatabase
import json
import os

URI = os.environ.get("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
USER = os.environ.get("AI_BANK_TUGRAPH_USER", "admin")
PASSWORD = os.environ.get("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
DB = os.environ.get("AI_BANK_TUGRAPH_DATABASE", "default")

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
with driver.session(database=DB) as s:
    for nid in ["ccb.channel.credit.cash.overview", "ccb.channel.credit.progress.overview"]:
        r = s.run(
            "MATCH (n:IntentNode {id: $id}) RETURN n.scenesJson AS scenes, n.title AS title",
            id=nid,
        ).single()
        print(f"\n=== {nid} (title={r['title']}) ===")
        scenes = json.loads(r["scenes"]) if r["scenes"] and r["scenes"] != "null" else []
        if scenes:
            print(f"scene[0] keys: {list(scenes[0].keys())}")
print("---DONE---")
