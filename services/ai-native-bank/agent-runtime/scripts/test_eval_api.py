#!/usr/bin/env python
"""Test the /v1/internal/intent-evaluate endpoint for multi-intent with LLM judge."""
import sys, os, json, asyncio
sys.path.insert(0, "/app")
os.environ.setdefault("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
os.environ.setdefault("AI_BANK_TUGRAPH_USER", "admin")
os.environ.setdefault("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")

from app.config import RuntimeSettings

async def test():
    import httpx
    settings = RuntimeSettings.from_env()
    token = settings.runtime_token
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "message": "查余额，给张帅转100",
        "pageContext": {},
        "plannableCapabilityIds": [
            "bank.account.list", "bank.account.balance.get", "bank.account.detail.get",
            "bank.account.transactions.list", "bank.credit-card.list", "bank.credit-card.bill.get",
            "bank.payee.list", "bank.payee.validate", "bank.transfer.order.create", "bank.transfer.status.get",
        ],
    }
    async with httpx.AsyncClient(timeout=200) as client:
        resp = await client.post(
            "http://127.0.0.1:18120/v1/internal/intent-evaluate",
            json=body, headers=headers,
        )
        print(f"=== HTTP STATUS: {resp.status_code} ===")
        data = resp.json()
        shape = data.get("shape", {})
        print(f"shape={shape.get('shape')}, taskCount={shape.get('taskCount')}")
        jr = data.get("judgeResults")
        print(f"judgeResults={jr}")
        timings = data.get("timings", {})
        print(f"timings={timings}")
        constraints = data.get("constraints", [])
        print(f"constraints_count={len(constraints)}")
        for c in constraints[:4]:
            iid = c.get("intentId")
            sc = c.get("score")
            con = c.get("constraint")
            sa = c.get("shapeAuthoritative")
            print(f"  {iid}: score={sc}, constraint={con}, shapeAuth={sa}")
        print("\n--- FULL RESPONSE (first 3000 chars) ---")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])

asyncio.run(test())
print("\n---DONE---")
