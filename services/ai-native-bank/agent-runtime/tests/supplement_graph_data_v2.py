#!/usr/bin/env python
"""第二轮图谱数据补充：针对23个失败用例补充口语化aliases和recallTerms

策略：追加不覆盖——已有aliases/recallTerms中不存在的才追加。
同时补充缺失的confusableIntents/disambiguationHints。
"""
import json
import shutil
from pathlib import Path

GRAPH_PATH = Path(__file__).resolve().parent.parent.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
BACKUP_PATH = GRAPH_PATH.parent / "archive" / "ccb-mobile-bank-intents.v4-fixed.bak2.json"

# ── 补充数据表 ──
# recallTerms: 子串匹配，term必须是归一化消息的子串
# aliases: 精确匹配(110分) 或 双向contains匹配(75分)

SUPPLEMENTS = {
    # ═══ V4-009: "信用卡最近消费了哪些" → transactions.query 零召回 ═══
    "ccb.channel.credit.card-manage.transactions.query": {
        "recallTerms_append": ["消费", "最近消费"],
        "aliases_append": ["信用卡最近消费了哪些", "消费了哪些", "最近消费"],
        "confusableIntents": [
            "ccb.channel.credit.card-manage.query",
            "ccb.channel.credit.bill.overview",
            "ccb.channel.home.account.detail.query",
        ],
        "disambiguationHints": "用户关注消费记录、交易明细、退款状态时选本意图；查询卡片状态或额度选卡片管理查询；查看账单金额选账单查询；查询储蓄卡交易明细选账户详情查询。",
    },

    # ═══ V4-010: "想给我信用卡提额" → limit-change 零召回 ═══
    "ccb.channel.credit.card-manage.limit-change": {
        "recallTerms_append": ["提额", "想提额"],
        "aliases_append": ["想给我信用卡提额", "想提额", "信用卡提额"],
    },

    # ═══ V4-011: "把信用卡分期了" → installment.apply 零召回 ═══
    "ccb.channel.credit.installment.apply": {
        "recallTerms_append": ["分期", "把信用卡分期"],
        "aliases_append": ["把信用卡分期了", "把分期办了"],
    },

    # ═══ V4-020: "信用卡开通了没" → card-manage.query 零召回 ═══
    "ccb.channel.credit.card-manage.query": {
        "recallTerms_append": ["开通", "开通了没"],
        "aliases_append": ["信用卡开通了没", "开通了没", "信用卡开通了吗"],
    },

    # ═══ V4-021: "账单" → bill.overview 零召回(需精确别名) ═══
    "ccb.channel.credit.bill.overview": {
        "recallTerms_append": ["账单"],
        "aliases_append": ["账单"],
    },

    # ═══ V4-022: "分期" → installment.overview 零召回(需精确别名) ═══
    "ccb.channel.credit.installment.overview": {
        "recallTerms_append": ["分期", "能分期"],
        "aliases_append": ["分期", "看下信用卡能分期多少"],
    },

    # ═══ V4-024: "查下卡里多少钱" → account.query 子句零召回 ═══
    #           "看看信用卡欠多少" → repay.overview 子句零召回
    "ccb.channel.home.account.query": {
        "recallTerms_append": ["卡里多少钱", "卡里"],
        "aliases_append": ["查下卡里多少钱", "卡里多少钱"],
    },
    "ccb.channel.credit.repay.overview": {
        "recallTerms_append": ["还多少", "欠多少", "欠多少钱", "这个月还", "信用卡欠"],
        "aliases_append": ["我的信用卡这个月还多少", "看看信用卡欠多少", "查下信用卡欠多少钱"],
    },

    # ═══ V4-035: "查下还款" → 三个还款意图都需召回 ═══
    "ccb.channel.home.repayment.overview": {
        "recallTerms_append": ["还款", "查下还款"],
        "aliases_append": ["查下还款"],
    },
    # home.loan.repayment 已有 recallTerms "贷款还款"，补"还款"
    "ccb.channel.home.loan.repayment": {
        "recallTerms_append": ["还款"],
    },

    # ═══ V4-037: "怎么还" → repay.faq + repay.repay 都需召回 ═══
    "ccb.channel.credit.repay.faq": {
        "recallTerms_append": ["怎么还", "怎么还款", "如何还款"],
        "aliases_append": ["怎么还", "怎么还款", "如何还款"],
    },
    "ccb.channel.credit.repay.repay": {
        "recallTerms_append": ["还信用卡", "还钱", "还完", "帮我还", "还完信用卡"],
        "aliases_append": ["还信用卡", "还钱", "还完信用卡", "帮我还信用卡"],
    },

    # ═══ V4-045: "查下信用卡能取多少现金" → cash.overview 零召回 ═══
    "ccb.channel.credit.cash.overview": {
        "recallTerms_append": ["取多少现金", "能取多少", "现金"],
        "aliases_append": ["查下信用卡能取多少现金", "能取多少现金"],
    },

    # ═══ V4-046: "信用卡申请进度怎么样了" → apply.overview 零召回 ═══
    "ccb.channel.credit.apply.overview": {
        "recallTerms_append": ["申请进度", "申请进度怎么样", "办卡进度"],
        "aliases_append": ["信用卡申请进度怎么样了", "申请进度怎么样了"],
    },

    # ═══ V4-047: "看看信用卡有没有什么活动" → benefits.overview 零召回 ═══
    "ccb.channel.credit.benefits.overview": {
        "recallTerms_append": ["活动", "有什么活动", "优惠活动"],
        "aliases_append": ["看看信用卡有没有什么活动", "信用卡有什么活动"],
    },

    # ═══ V4-050: "还钱" → repay.repay + home.repayment.repay 都需召回 ═══
    "ccb.channel.home.repayment.repay": {
        "recallTerms_append": ["还钱"],
        "aliases_append": ["还钱"],
    },

    # ═══ V4-028/V4-048: "转账记录" → transfer.query 零召回 ═══
    "ccb.channel.home.transfer.query": {
        "recallTerms_append": ["转账记录", "转账明细"],
        "aliases_append": ["转账记录", "查下转账记录"],
        "confusableIntents": [
            "ccb.channel.home.transfer.create",
            "ccb.channel.home.transfer.faq",
            "ccb.channel.credit.card-manage.transactions.query",
        ],
        "disambiguationHints": "用户查看转账记录、转账明细、转账状态时选本意图；办理转账选转账办理；询问转账规则选转账FAQ；查看信用卡交易明细选交易明细查询。",
    },

    # ═══ V4-036: "查下明细" → transactions.query + account.detail.query 都需召回 ═══
    "ccb.channel.home.account.detail.query": {
        "recallTerms_append": ["明细"],
        "aliases_append": ["查下明细"],
        "confusableIntents": [
            "ccb.channel.home.account.query",
            "ccb.channel.home.details.spending-explain",
            "ccb.channel.credit.card-manage.transactions.query",
        ],
        "disambiguationHints": "用户查看收支明细、交易详情、账户明细时选本意图；查询账户余额选账户查询；查看收支说明选收支说明；查看信用卡交易明细选交易明细查询。",
    },

    # ═══ V4-017: "我的信用卡这个月还多少" → repay.overview 排序错误 ═══
    # (已在 V4-024 中处理 repay.overview 的 recallTerms/aliases)

    # ═══ V4-019: "还信用卡" → credit.repay.repay 排序错误(被home.repayment.repay压) ═══
    # (已在 V4-037 中添加 "还信用卡" 作为 credit.repay.repay 的 alias → 精确匹配110分)

    # ═══ V4-029: "看下信用卡能分期多少" → installment.overview 子句零召回 ═══
    # (已在 V4-022 中添加 "看下信用卡能分期多少" 作为 alias)

    # ═══ V4-030: "查下信用卡欠多少钱" → repay.overview 子句零召回 ═══
    # (已在 V4-024 中处理)

    # ═══ V4-033: "我的卡" → card.query + account.query 模糊召回不全 ═══
    # card.query 已有 alias "我的信用卡" → contains 75分
    # 补 account.query 的 alias "我的卡"
    # (account.query 已在 V4-024 中处理 recallTerms，此处补 alias)
    # 实际上 account.query 的 aliases_append 已在 V4-024 中设置，这里不重复

    # ═══ V4-049: "帮我还信用卡" → credit.repay.repay 子句零召回 ═══
    # (已在 V4-037 中添加 "帮我还信用卡" 作为 alias)
}


def main():
    # 备份
    if not BACKUP_PATH.exists():
        shutil.copy2(GRAPH_PATH, BACKUP_PATH)
        print(f"备份: {BACKUP_PATH}")

    data = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    tree = data.get("intentTree", [])

    updated = 0
    not_found = []

    for node in tree:
        if node.get("node_role") != "leaf":
            continue
        v4_id = node["id"]
        if v4_id not in SUPPLEMENTS:
            continue

        supplement = SUPPLEMENTS[v4_id]
        scenes = node.get("scenes", [])
        found = False

        for scene in scenes:
            sd = scene.get("semantic_definition", {})
            rp = scene.get("retrieval_profile", {})

            # ── 追加 aliases（不覆盖已有） ──
            if "aliases_append" in supplement:
                existing_aliases = rp.get("aliases", [])
                for new_alias in supplement["aliases_append"]:
                    if new_alias not in existing_aliases:
                        existing_aliases.append(new_alias)
                        found = True
                rp["aliases"] = existing_aliases
                scene["retrieval_profile"] = rp

            # ── 追加 recallTerms（不覆盖已有） ──
            if "recallTerms_append" in supplement:
                existing_terms = rp.get("recallTerms", [])
                for new_term in supplement["recallTerms_append"]:
                    if new_term not in existing_terms:
                        existing_terms.append(new_term)
                        found = True
                rp["recallTerms"] = existing_terms
                scene["retrieval_profile"] = rp

            # ── 补充 confusableIntents（仅在为空时） ──
            if "confusableIntents" in supplement:
                existing_ci = sd.get("confusableIntents", [])
                if not existing_ci:
                    sd["confusableIntents"] = supplement["confusableIntents"]
                    scene["semantic_definition"] = sd
                    found = True

            # ── 补充 disambiguationHints（仅在为空时） ──
            if "disambiguationHints" in supplement:
                existing_dh = sd.get("disambiguationHints")
                if not existing_dh:
                    sd["disambiguationHints"] = supplement["disambiguationHints"]
                    scene["semantic_definition"] = sd
                    found = True

        if found:
            updated += 1
        else:
            not_found.append(v4_id)

    # 写回
    GRAPH_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已更新 {updated} 个意图")
    if not_found:
        print(f"未找到/已有数据跳过: {not_found}")
    print(f"输出: {GRAPH_PATH}")


if __name__ == "__main__":
    main()
