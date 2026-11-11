#!/usr/bin/env python
"""Test all 7 fix cases via /intent-recall-probe endpoint.
For each case, verify the expected intent appears in candidates with ⚠️ hints.
"""
import json
import urllib.request
import urllib.parse
import sys

# (case_name, query, expected_intent_in_candidates, should_have_warn)
CASES = [
    ("V4-021/022: 还本月信用卡", "还本月信用卡", "ccb.credit.repay.faq", True),
    ("V4-021/022: 查信用卡还款", "查信用卡还款", "ccb.home.repayment.faq", True),
    ("V4-158: 还款计划", "还款计划", "ccb.home.loan.repayment", True),
    ("V4: 查未还金额", "查查信用卡还欠多少", "ccb.credit.repay.overview", True),
    ("V4: 现金转出规则", "现金转出有什么要求", "ccb.credit.cash.faq", True),
    ("V4: 能借多少", "现金转出还剩多少", "ccb.credit.cash.overview", True),
    ("V4: 办卡进度", "办卡进度怎么样了", "ccb.credit.apply.overview", False),
]

def probe(query):
    encoded = urllib.parse.quote(query)
    url = f"http://host.docker.internal:3000/api/ai-bank/v1/intent-recall-probe?message={encoded}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

def main():
    all_ok = True
    print("=== 7 CASES TEST VIA /intent-recall-probe ===")
    print(f"{'case':<40} {'graphVer':>10} {'found':>6} {'warn':>5} {'top1':>35}")
    print("-" * 100)

    for case_name, query, expected_intent, should_have_warn in CASES:
        try:
            r = probe(query)
        except Exception as e:
            print(f"❌ {case_name:<38} ERROR: {e}")
            all_ok = False
            continue

        data = r.get("data", {})
        graph_ver = data.get("graphVersion", "?")
        cands = data.get("candidates", [])

        # Find expected intent in candidates
        found_intent = None
        found_hints = ""
        for c in cands:
            if c.get("intentId") == expected_intent:
                found_intent = c
                found_hints = c.get("disambiguationHints", "")
                break

        found = found_intent is not None
        has_warn = "⚠️" in found_hints if found_hints else False

        # Top 1 candidate
        top1 = cands[0] if cands else None
        top1_id = top1.get("intentId", "?") if top1 else "?"
        top1_score = top1.get("score", 0) if top1 else 0
        top1_str = f"{top1_id}({top1_score})"

        # Status: expected intent found AND has ⚠️ (or not, per case)
        status_ok = found and (has_warn == should_have_warn)
        status = "✅" if status_ok else "❌"

        print(f"{status} {case_name:<38} {graph_ver:>10} "
              f"{'Y' if found else 'N':>6} "
              f"{'Y' if has_warn else 'N':>5} "
              f"{top1_str:>35}")

        if not status_ok:
            all_ok = False
            print(f"    query: {query}")
            print(f"    expected_intent: {expected_intent}, found: {found}, "
                  f"has_warn: {has_warn} (expected {should_have_warn})")
            if found_intent:
                print(f"    hints: {found_hints[:150]}")
            print(f"    top3: {[(c.get('intentId'), c.get('score')) for c in cands[:3]]}")

    print(f"\n=== RESULT: {'ALL_OK ✅' if all_ok else 'HAS_FAILURES ❌'} ===")
    print("---DONE---")
    sys.exit(0 if all_ok else 1)

if __name__ == "__main__":
    main()
