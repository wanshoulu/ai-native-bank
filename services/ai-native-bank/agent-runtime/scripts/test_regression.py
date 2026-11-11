#!/usr/bin/env python
"""Regression test: verify common intents unrelated to the 7 fixes still work."""
import json
import urllib.request
import urllib.parse
import sys

# (case_name, query, expected_intent_in_top5)
CASES = [
    ("账单查询", "查信用卡账单", "ccb.credit.bill.overview"),
    ("信用卡申请", "申请信用卡", "ccb.credit.apply.apply"),
    ("贷款概览", "查看我的贷款", "ccb.home.loan.overview"),
    ("信用卡额度", "查信用卡额度", "ccb.credit.card.query"),
    ("信用卡挂失", "信用卡丢了怎么办", "ccb.credit.card.loss"),
    ("分期", "信用卡分期", "ccb.credit.installment.overview"),
]

def probe(query):
    encoded = urllib.parse.quote(query)
    url = f"http://host.docker.internal:3000/api/ai-bank/v1/intent-recall-probe?message={encoded}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

def main():
    all_ok = True
    print("=== REGRESSION TEST (unrelated intents) ===")
    for case_name, query, expected in CASES:
        try:
            r = probe(query)
        except Exception as e:
            print(f"❌ {case_name}: ERROR: {e}")
            all_ok = False
            continue
        data = r.get("data", {})
        cands = data.get("candidates", [])
        top5_ids = [c.get("intentId") for c in cands[:5]]
        found = expected in top5_ids
        status = "✅" if found else "❌"
        print(f"{status} {case_name:<15} query='{query}' expected={expected} found={found} top3={top5_ids[:3]}")
        if not found:
            all_ok = False
    print(f"\n=== RESULT: {'ALL_OK ✅' if all_ok else 'HAS_FAILURES ❌'} ===")
    print("---DONE---")
    sys.exit(0 if all_ok else 1)

if __name__ == "__main__":
    main()
