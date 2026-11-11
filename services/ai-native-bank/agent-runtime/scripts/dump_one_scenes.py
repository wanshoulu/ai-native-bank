#!/usr/bin/env python
"""Dump current scenesJson for one IntentNode to understand structure."""
from neo4j import GraphDatabase
import json
import os

URI = os.environ.get("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
USER = os.environ.get("AI_BANK_TUGRAPH_USER", "admin")
PASSWORD = os.environ.get("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
DB = os.environ.get("AI_BANK_TUGRAPH_DATABASE", "default")

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
with driver.session(database=DB) as s:
    nid = "ccb.channel.credit.repay.overview"
    r = s.run(
        "MATCH (n:IntentNode {id: $id}) RETURN n.scenesJson AS scenes, n.title AS title",
        id=nid,
    ).single()
    if r is None:
        print(f"{nid}: NOT FOUND")
    else:
        print(f"title: {r['title']}")
        scenes = json.loads(r["scenes"]) if r["scenes"] and r["scenes"] != "null" else []
        print(f"scenes count: {len(scenes)}")
        print(json.dumps(scenes, ensure_ascii=False, indent=2))
print("---DONE---")
