#!/usr/bin/env python
"""Directly test LLMJudgeV4.judge() to confirm Qwen LLM is actually called."""
import sys, os, asyncio
sys.path.insert(0, "/app")
os.environ.setdefault("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
os.environ.setdefault("AI_BANK_TUGRAPH_USER", "admin")
os.environ.setdefault("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")

from app.config import RuntimeSettings
from app.tugraph_source import load_v4_payload_from_tugraph
from app.v4_graph_adapter import V4GraphAdapter
from app.llm_judge_v4 import LLMJudgeV4
from pathlib import Path

settings = RuntimeSettings.from_env()
payload = load_v4_payload_from_tugraph(settings)
adapter = V4GraphAdapter(payload, Path("."))

print("=== LLMJudgeV4 Direct Test ===")
judge = LLMJudgeV4(recall_settings=settings.recall_settings)
print(f"judge.enabled: {judge.enabled}")
print(f"judge._model: {judge._model}")
print(f"judge._base_url: {judge._base_url}")
print(f"judge._api_key present: {'YES' if judge._api_key else 'NO'}")
print(f"judge._api_key prefix: {judge._api_key[:8] if judge._api_key else 'N/A'}...")

# Build mock candidates for "查还款" — ambiguous between credit card repay and loan repay
from app.intent_graph import IntentCandidate
cands = [
    IntentCandidate(
        intent_id="ccb.credit.repay.overview",
        title="一键还款·还款试算",
        action="QUERY",
        business_object="credit.repay",
        execution_mode="query",
        risk_level="L0",
        required_slots=[],
        capability_bindings=[],
        rule_version="v4",
        score=55,
        evidence=["alias"],
        disambiguation_hints="用户关注信用卡应还金额...⚠️注意:query 仅含「还本月信用卡/怎么还款/如何还款」等泛指词而无「应还/未还/最低还款/欠多少」等金额词时不应选本意图,应选还款FAQ。",
    ),
    IntentCandidate(
        intent_id="ccb.home.loan.repayment",
        title="贷款服务·还款计划",
        action="QUERY",
        business_object="home.loan",
        execution_mode="query",
        risk_level="L0",
        required_slots=[],
        capability_bindings=[],
        rule_version="v4",
        score=55,
        evidence=["alias"],
        disambiguation_hints="用户查看贷款还款计划、还款进度时选本意图...⚠️注意:query 含「还款计划」时必选本意图...",
    ),
    IntentCandidate(
        intent_id="ccb.credit.bill.overview",
        title="信用卡·账单查询",
        action="QUERY",
        business_object="credit.bill",
        execution_mode="query",
        risk_level="L0",
        required_slots=[],
        capability_bindings=[],
        rule_version="v4",
        score=46,
        evidence=["graph_expand:confusable"],
    ),
]

print(f"\nCalling judge.judge() with {len(cands)} candidates for query='查还款'...")
print("(This will call Qwen LLM API — wait up to 30s)")
result = judge.judge("查还款", cands, "ambiguous", subgraph_contexts={})
print(f"\nLLM judge returned: {len(result)} results")
for r in result:
    print(f"  intent_id={r.get('intent_id')}, confidence={r.get('confidence')}")
print("---DONE---")
