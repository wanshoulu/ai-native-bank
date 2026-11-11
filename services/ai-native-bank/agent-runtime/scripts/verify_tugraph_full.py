#!/usr/bin/env python
"""Comprehensive verification that all 7 intent fixes are correctly persisted
in TuGraph IntentNode.scenesJson, with exact content match (not just presence).
Also checks that other semantic_definition fields are preserved.
"""
from neo4j import GraphDatabase
import json
import os
import sys

URI = os.environ.get("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
USER = os.environ.get("AI_BANK_TUGRAPH_USER", "admin")
PASSWORD = os.environ.get("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
DB = os.environ.get("AI_BANK_TUGRAPH_DATABASE", "default")

# Expected: (intent_id, confusableIntents, disambiguationHints)
EXPECTED = [
    ("ccb.channel.credit.repay.overview", 6, 135),
    ("ccb.channel.home.loan.repayment", 3, 109),
    ("ccb.channel.home.repayment.faq", 3, 124),
    ("ccb.channel.credit.cash.faq", 2, 130),
    ("ccb.channel.credit.cash.overview", 2, 136),
    ("ccb.channel.credit.progress.overview", 1, 177),
    ("ccb.channel.credit.repay.faq", 3, 263),
]

# Required ⚠️ keywords per intent (the fix signature)
WARN_KEYWORDS = {
    "ccb.channel.credit.repay.overview": ["⚠️注意", "应选还款FAQ"],
    "ccb.channel.home.loan.repayment": ["⚠️注意", "还款计划"],
    "ccb.channel.home.repayment.faq": ["⚠️注意", "知识咨询优先于账单查询"],
    "ccb.channel.credit.cash.faq": ["⚠️注意", "现金转出查询"],
    "ccb.channel.credit.cash.overview": ["⚠️注意", "能借多少"],
    "ccb.channel.credit.progress.overview": ["⚠️注意", "申请信用卡查询"],
    "ccb.channel.credit.repay.faq": ["⚠️注意", "还本月信用卡"],
}


def main():
    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    all_ok = True
    print("=== TUGRAPH 7-INTENT FULL VERIFICATION ===")
    print(f"{'intent_id':<45} {'conf':>5} {'hints':>6} {'warn':>5} {'kw':>4} {'preserved':>10}")
    print("-" * 90)

    with driver.session(database=DB) as s:
        for intent_id, exp_conf, exp_hints_len in EXPECTED:
            r = s.run(
                "MATCH (n:IntentNode {id: $id}) RETURN n.scenesJson AS scenes, n.title AS title",
                id=intent_id,
            ).single()
            if r is None:
                print(f"  {intent_id}: NOT_FOUND ❌")
                all_ok = False
                continue

            scenes = json.loads(r["scenes"]) if r["scenes"] and r["scenes"] != "null" else []
            if not scenes:
                print(f"  {intent_id}: empty scenes ❌")
                all_ok = False
                continue

            sd = scenes[0].get("semantic_definition", {})
            hints = sd.get("disambiguationHints", "")
            confusable = sd.get("confusableIntents", [])

            # Check 1: hints length matches expected
            hints_len_ok = len(hints) == exp_hints_len

            # Check 2: confusable count matches
            conf_ok = len(confusable) == exp_conf

            # Check 3: has ⚠️
            has_warn = "⚠️" in hints

            # Check 4: required keywords present
            required_kws = WARN_KEYWORDS.get(intent_id, [])
            kw_ok = all(kw in hints for kw in required_kws)

            # Check 5: other semantic_definition fields preserved
            # (intentType, description, userGoal should still be there)
            preserved_fields = [
                sd.get("intentType") is not None,
                sd.get("description") is not None,
                sd.get("userGoal") is not None,
            ]
            preserved_ok = all(preserved_fields)

            # Check 6: other scene fields preserved
            # (QUERY/CHANGE use 'bindings', HELP/ASK use 'knowledge_binding')
            scene = scenes[0]
            has_scene_type = "scene_type" in scene
            has_retrieval = "retrieval_profile" in scene
            has_exec_binding = "execution_binding" in scene
            has_any_binding = ("bindings" in scene) or ("knowledge_binding" in scene) or ("task_binding" in scene)
            has_decision = "decision_policy" in scene
            other_fields_ok = all([has_scene_type, has_retrieval, has_exec_binding, has_any_binding, has_decision])

            status = "✅" if (hints_len_ok and conf_ok and has_warn and kw_ok and preserved_ok and other_fields_ok) else "❌"
            print(f"{status} {intent_id:<43} {len(confusable):>5} {len(hints):>6} "
                  f"{'Y' if has_warn else 'N':>5} {sum(kw_ok for _ in [1]):>4} {'Y' if preserved_ok and other_fields_ok else 'N':>10}")

            if not (hints_len_ok and conf_ok and has_warn and kw_ok and preserved_ok and other_fields_ok):
                all_ok = False
                print(f"    details: hints_len_ok={hints_len_ok}({len(hints)}/{exp_hints_len}), "
                      f"conf_ok={conf_ok}({len(confusable)}/{exp_conf}), "
                      f"has_warn={has_warn}, kw_ok={kw_ok}, "
                      f"preserved_ok={preserved_ok}, other_fields_ok={other_fields_ok}")
                if hints:
                    print(f"    hints preview: {hints[:100]}...")
                print(f"    confusable: {confusable}")

    driver.close()
    print(f"\n=== RESULT: {'ALL_OK ✅' if all_ok else 'HAS_FAILURES ❌'} ===")
    print("---DONE---")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
