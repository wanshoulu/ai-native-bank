#!/usr/bin/env python
"""Write disambiguationHints + confusableIntents into TuGraph IntentNode.scenesJson
for the 7 intents that were fixed in v4-fixed.json but never propagated to TuGraph.

Strategy: read current scenesJson from TuGraph, merge semantic_definition fields
(disambiguationHints, confusableIntents), write back. Preserves all other data.
"""
from neo4j import GraphDatabase
import json
import os

URI = os.environ.get("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
USER = os.environ.get("AI_BANK_TUGRAPH_USER", "admin")
PASSWORD = os.environ.get("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
DB = os.environ.get("AI_BANK_TUGRAPH_DATABASE", "default")

# === 7 intent fixes (extracted from graph.ccb.intents.v2.1.0.json) ===
# Each entry: (intent_id, confusableIntents, disambiguationHints)
FIXES = [
    (
        "ccb.channel.credit.repay.overview",
        [
            "ccb.channel.credit.card-manage.query",
            "ccb.channel.home.repayment.overview",
            "ccb.channel.credit.bill.overview",
            "ccb.channel.home.loan.repayment",
            "ccb.channel.credit.repay.faq",
            "ccb.channel.credit.repay.repay",
        ],
        "用户关注信用卡应还金额、未还金额或最低还款额时选本意图；关注信用卡卡片状态或额度选卡片管理查询；关注账单出账内容选账单查询。⚠️注意:query 仅含「还本月信用卡/怎么还款/如何还款」等泛指词而无「应还/未还/最低还款/欠多少」等金额词时不应选本意图,应选还款FAQ。",
    ),
    (
        "ccb.channel.home.loan.repayment",
        [
            "ccb.channel.home.loan.overview",
            "ccb.channel.home.repayment.overview",
            "ccb.channel.credit.repay.overview",
        ],
        "用户查看贷款还款计划、还款进度时选本意图；查看贷款概览选贷款概览；查看信用卡还款选还款概览。⚠️注意:query 含「还款计划」时必选本意图(贷款还款计划查询),即使用户同时提到还信用卡等其他操作,也应同时返回本意图。",
    ),
    (
        "ccb.channel.home.repayment.faq",
        [
            "ccb.channel.home.repayment.overview",
            "ccb.channel.credit.repay.faq",
            "ccb.channel.credit.bill.faq",
        ],
        "用户询问信用卡还款规则、到账时效、手续费等知识时选本意图；查看应还金额选还款概览；询问一键还款知识选还款FAQ。⚠️注意:query 含「查信用卡还款/看看信用卡还款」等泛指信用卡还款但无明显查账单或操作意图时必选本意图(知识咨询优先于账单查询)。",
    ),
    (
        "ccb.channel.credit.cash.faq",
        [
            "ccb.channel.credit.cash.overview",
            "ccb.channel.credit.cash.apply",
        ],
        "用户询问现金转出规则、手续费、到账时效、限额政策等知识时选本意图;查当前可借/可转额度或办理记录选现金转出查询;明确办理选现金转出办理。⚠️注意:query 含「能借多少」「能转多少」「能取多少」等查额度词时必选现金转出查询(overview),不应选本意图。",
    ),
    (
        "ccb.channel.credit.cash.overview",
        [
            "ccb.channel.credit.cash.apply",
            "ccb.channel.credit.cash.faq",
        ],
        "用户查看现金转出进度、记录、状态、当前可借额度时选本意图(含「能借多少」「能转多少」「能取多少」等查额度查询);办理现金转出选现金转出办理;询问现金转出规则/手续费/到账时效等知识选现金转出FAQ。⚠️注意:query 含「能借多少」「能转多少」「能取多少」时必选本意图。",
    ),
    (
        "ccb.channel.credit.progress.overview",
        [
            "ccb.channel.credit.apply.overview",
        ],
        "用户查询非信用卡申请类办卡进度(如房贷卡/附属卡/储蓄卡等)时选本意图;查询信用卡申请进度/办卡进度(含「办卡进度怎么样了」「申卡到哪一步了」等)选申请信用卡查询。⚠️注意:query 含「办卡进度怎么样了」「办卡进度」「申卡进度」「我的办卡进度」时不应选本意图,应选申请信用卡查询(apply.overview)——本意图仅用于非信用卡申请类办卡进度。",
    ),
    (
        "ccb.channel.credit.repay.faq",
        [
            "ccb.channel.credit.repay.overview",
            "ccb.channel.home.repayment.faq",
            "ccb.channel.credit.repay.repay",
        ],
        "用户询问还款规则、到账时效、手续费等知识时选本意图；用户泛指「还本月信用卡/怎么还款/如何还款」等无明显金额或规则意图时选本意图；查看具体应还金额选还款试算。⚠️注意:query 含「还本月信用卡」时必选本意图(产品已将该 query 标注为本意图典型 alias),不应解读为查应还金额。⚠️注意:query 含「还信用卡要手续费吗/还信用卡要手续费不/信用卡还款收手续费吗」等信用卡还款手续费知识咨询时必选本意图,不应选 home.repayment.faq(本意图与首页信用卡还款FAQ功能重叠,但产品标注归属本意图)。",
    ),
]


def merge_and_write(driver):
    results = []
    with driver.session(database=DB) as s:
        for intent_id, confusable, hints in FIXES:
            # 1. Read current scenesJson
            r = s.run(
                "MATCH (n:IntentNode {id: $id}) RETURN n.scenesJson AS scenes, n.title AS title",
                id=intent_id,
            ).single()
            if r is None:
                results.append((intent_id, "NOT_FOUND", False))
                continue

            title = r["title"]
            scenes_raw = r["scenes"]
            scenes = json.loads(scenes_raw) if scenes_raw and scenes_raw != "null" else []

            if not scenes:
                results.append((intent_id, f"empty_scenes (title={title})", False))
                continue

            # 2. Merge semantic_definition fields
            sd = scenes[0].setdefault("semantic_definition", {})
            old_hints = sd.get("disambiguationHints", "")
            old_confusable = sd.get("confusableIntents", [])
            sd["disambiguationHints"] = hints
            sd["confusableIntents"] = confusable

            # 3. Write back
            new_scenes_json = json.dumps(scenes, ensure_ascii=False)
            s.run(
                "MATCH (n:IntentNode {id: $id}) SET n.scenesJson = $scenes_json",
                id=intent_id,
                scenes_json=new_scenes_json,
            )

            changed = (old_hints != hints) or (old_confusable != confusable)
            results.append((
                intent_id,
                f"title={title}, scenes_count={len(scenes)}, "
                f"old_hints={'<empty>' if not old_hints else '<'+str(len(old_hints))+'chars>'}, "
                f"new_hints={len(hints)}chars, changed={changed}",
                True,
            ))

    return results


def verify(driver):
    """Verify all 7 intents now have disambiguationHints with ⚠️."""
    print("\n=== VERIFY ===")
    all_ok = True
    with driver.session(database=DB) as s:
        for intent_id, _, hints in FIXES:
            r = s.run(
                "MATCH (n:IntentNode {id: $id}) RETURN n.scenesJson AS scenes",
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
            actual_hints = sd.get("disambiguationHints", "")
            actual_confusable = sd.get("confusableIntents", [])
            has_warn = "⚠️" in actual_hints
            matches = actual_hints == hints
            print(f"  {intent_id}: hints={len(actual_hints)}chars, "
                  f"confusable={len(actual_confusable)}, "
                  f"has_⚠️={has_warn}, matches={matches} "
                  f"{'✅' if matches and has_warn else '❌'}")
            if not (matches and has_warn):
                all_ok = False
    return all_ok


def main():
    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    print("=== WRITE FIXES TO TUGRAPH ===")
    results = merge_and_write(driver)
    for intent_id, info, written in results:
        status = "WROTE" if written else "SKIP"
        print(f"  [{status}] {intent_id}: {info}")

    all_ok = verify(driver)
    driver.close()
    print(f"\n=== RESULT: {'ALL_OK ✅' if all_ok else 'HAS_FAILURES ❌'} ===")
    print("---DONE---")


if __name__ == "__main__":
    main()
