#!/usr/bin/env python
"""Verify graph version + multi-route recall + LLM judge end-to-end."""
import sys, os, json, asyncio
sys.path.insert(0, "/app")
os.environ.setdefault("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
os.environ.setdefault("AI_BANK_TUGRAPH_USER", "admin")
os.environ.setdefault("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")

from app.config import RuntimeSettings
from app.tugraph_source import load_v4_payload_from_tugraph
from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from app.llm_judge_v4 import LLMJudgeV4
from app.service import RuntimeService
from pathlib import Path

settings = RuntimeSettings.from_env()
payload = load_v4_payload_from_tugraph(settings)

print("=== 1. GRAPH VERSION ===")
print(f"payload.schemaVersion: {payload.get('schemaVersion')}")
print(f"payload.version: {payload.get('version')}")
print(f"payload.graphVersion: {payload.get('graphVersion')}")
print(f"intentTree count: {len(payload.get('intentTree', []))}")

print("\n=== 2. V4GraphAdapter version/digest ===")
adapter = V4GraphAdapter(payload, Path("."))
print(f"adapter.version: {adapter.version}")
print(f"adapter.digest: {adapter.digest}")

print("\n=== 3. MULTI-ROUTE RECALL COMPONENTS ===")
# Check SemanticRecallV4 (embedding semantic recall, uses TuGraph KNN)
tugraph_config = {
    "uri": settings.tugraph_uri,
    "user": settings.tugraph_user,
    "password": settings.tugraph_password,
    "database": settings.tugraph_database,
    "base_url": getattr(settings, "tugraph_rest_url", "http://host.docker.internal:7070"),
}
semantic = SemanticRecallV4(adapter, tugraph_config=tugraph_config)
semantic.prewarm()
print(f"SemanticRecallV4.enabled: {semantic.enabled}")
print(f"SemanticRecallV4._tugraph_enabled: {semantic._tugraph_enabled}")

# Check SubgraphReasoningV4
subgraph = SubgraphReasoningV4(adapter, recall_settings=settings.recall_settings)
print(f"SubgraphReasoningV4.enabled: {subgraph.enabled}")

# Check LLMJudgeV4
judge = LLMJudgeV4(recall_settings=settings.recall_settings)
print(f"LLMJudgeV4.enabled: {judge.enabled}")
print(f"LLMJudgeV4._model: {judge._model}")
print(f"LLMJudgeV4._base_url: {judge._base_url}")

print("\n=== 4. MULTI-ROUTE RECALL TEST (query='还本月信用卡') ===")
async def test():
    # Use the actual RuntimeService to test full 4-layer pipeline
    svc = RuntimeService(settings)
    await svc.initialize()
    print(f"RuntimeService.intent_graph_identity: {svc.intent_graph_identity}")
    print(f"  graph_version={svc.intent_graph_identity[0]}")
    print(f"  graph_digest={svc.intent_graph_identity[1][:40]}...")
    print(f"  _v4_semantic={'YES' if svc._v4_semantic else 'NO'}")
    print(f"  _v4_subgraph={'YES' if svc._v4_subgraph else 'NO'}")
    print(f"  _v4_judge={'YES' if svc._v4_judge else 'NO'}")

    cands = await svc.probe_intent_recall("还本月信用卡")
    print(f"\nCandidates count: {len(cands)}")
    for c in cands[:5]:
        has_warn = "⚠️" in c.get("disambiguationHints", "")
        print(f"  {c.get('intentId')} score={c.get('score')} evidence={c.get('evidence')} warn={has_warn}")

asyncio.run(test())
print("---DONE---")
