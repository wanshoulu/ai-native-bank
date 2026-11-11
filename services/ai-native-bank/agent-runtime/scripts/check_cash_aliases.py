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
    r = s.run(
        "MATCH (n:IntentNode {id: $id}) RETURN n.scenesJson AS scenes, n.title AS title",
        id="ccb.channel.credit.cash.overview",
    ).single()
    scenes = json.loads(r["scenes"])
    aliases = scenes[0].get("retrieval_profile", {}).get("aliases", [])
    print(f"title: {r['title']}")
    print(f"aliases count: {len(aliases)}")
    print("aliases containing '借' or '转' or '取':")
    for a in aliases:
        if any(k in a for k in ["借", "转", "取"]):
            print(f"  - {a}")
print("---DONE---")
