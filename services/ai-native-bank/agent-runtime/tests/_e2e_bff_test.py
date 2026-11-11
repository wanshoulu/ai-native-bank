"""End-to-end test via BFF (port 3000) → local backend (port 18120)."""
import httpx
import json
import sys

BASE = "http://localhost:3000"
GRAPH = "bank:ccb-mobile-bank-intents"

queries = [
    "查余额",
    "我要转账给张三500元",
    "信用卡还款",
    "查询近期交易明细",
    "我想查一下我的工资流水",
]

for q in queries:
    r = httpx.post(
        f"{BASE}/api/intent-evaluate",
        json={"query": q, "options": {"graph": GRAPH}},
        timeout=60,
    )
    if r.status_code != 200:
        print(f"[FAIL] {r.status_code} for query: {q}")
        print(r.text[:500])
        continue
    d = r.json()
    if "error" in d:
        print(f"[ERROR] query={q}: {d['error']}")
        continue
    final = d.get("final_result")
    result_type = d.get("result_type")
    bank_info = d.get("methods", {}).get("bank", {})
    candidates = bank_info.get("candidates", [])
    steps = d.get("methods", {}).get("llm_judge", {}).get("decision_trace", {}).get("steps", [])
    print(f"[OK] query={q}")
    print(f"  result_type={result_type}")
    if final:
        print(f"  final_result: {final.get('intent_name')} (action={final.get('action_code')}, score={final.get('score')})")
    print(f"  candidates={len(candidates)}, widened={bank_info.get('widened')}")
    for s in steps:
        print(f"    {s.get('title')} -> {s.get('conclusion')}")
    print()

print("E2E BFF test complete.")
