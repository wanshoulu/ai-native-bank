"""End-to-end test for intent-evaluate endpoint."""
import httpx
import json
import sys

TOKEN = "dev-runtime-token-secure-2026"
BASE = "http://localhost:18120"

queries = [
    "查余额",
    "我要转账给张三500元",
    "信用卡还款",
    "查询近期交易明细",
    "我想查一下我的工资流水",
]

for q in queries:
    r = httpx.post(
        f"{BASE}/v1/internal/intent-evaluate",
        json={
            "message": q,
            "pageContext": None,
            "plannableCapabilityIds": [],
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"[FAIL] {r.status_code} for query: {q}")
        print(r.text[:500])
        continue
    d = r.json()
    shape = d["shape"]
    constraints = d["constraints"]
    print(f"[OK] query={q}")
    print(f"  shape={shape['shape']} taskCount={shape['taskCount']}")
    print(f"  constraints={len(constraints)} widened={d['widened']}")
    print(f"  timings={d['timings']}")
    for c in constraints[:3]:
        print(f"    {c['intentId']} score={c['score']} evidence={c.get('evidence',[])} authoritative={c.get('authoritative',False)}")
    print()
