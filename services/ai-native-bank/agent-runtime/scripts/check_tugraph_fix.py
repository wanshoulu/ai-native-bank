#!/usr/bin/env python
"""Check whether TuGraph IntentNode scenesJson contains the V4 fix (⚠️ hints)."""
from neo4j import GraphDatabase
import json
import os

URI = os.environ.get("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
USER = os.environ.get("AI_BANK_TUGRAPH_USER", "admin")
PASSWORD = os.environ.get("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
DB = os.environ.get("AI_BANK_TUGRAPH_DATABASE", "default")

IDS = [
    "ccb.channel.credit.repay.overview",
    "ccb.channel.home.loan.repayment",
    "ccb.channel.home.repayment.faq",
    "ccb.channel.credit.cash.faq",
    "ccb.channel.credit.cash.overview",
    "ccb.channel.credit.progress.overview",
    "ccb.channel.credit.repay.faq",
    "ccb.channel.credit.apply.query",
]

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
with driver.session(database=DB) as s:
    for nid in IDS:
        r = s.run(
            "MATCH (n:IntentNode {id: $id}) RETURN n.scenesJson AS scenes",
            id=nid,
        ).single()
        if r is None:
            print(f"{nid}: NOT FOUND")
            continue
        scenes = json.loads(r["scenes"]) if r["scenes"] and r["scenes"] != "null" else []
        hints = ""
        if scenes:
            hints = scenes[0].get("semantic_definition", {}).get("disambiguationHints", "")
        has_warn = "⚠️" in hints
        print(f"{nid}: has_hints={bool(hints)} has_warn={has_warn} | {hints[:80] if hints else '(none)'}")
print("---DONE---")
