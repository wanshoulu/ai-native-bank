#!/usr/bin/env python
"""批量补充图谱数据：confusableIntents + disambiguationHints + recallTerms

聚焦 credit 和 home 域的高频混淆意图对。
直接修改 graph.ccb.intents.v2.1.0.json，可回退（先备份）。
"""
import json
import shutil
from pathlib import Path

GRAPH_PATH = Path(__file__).resolve().parent.parent.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
BACKUP_PATH = GRAPH_PATH.parent / "archive" / "ccb-mobile-bank-intents.v4-fixed.bak.json"

# ── 补充数据表 ──
# confusableIntents 用 V4 节点 ID 格式（ccb.channel.xxx.yyy）
# disambiguationHints 是给 LLM 看的消歧提示
# recallTerms 是子串匹配的召回词

SUPPLEMENTS = {
    # ═══ credit.bill 簇 ═══
    "ccb.channel.credit.bill.repay": {
        "confusableIntents": [
            "ccb.channel.credit.bill.overview",
            "ccb.channel.credit.repay.repay",
            "ccb.channel.home.repayment.repay",
        ],
        "disambiguationHints": "用户明确要求从账单页面执行还款动作时选本意图；仅查看账单内容选账单查询；关注应还金额试算选还款试算；从首页还款入口还款选首页立即还款。",
    },
    "ccb.channel.credit.bill.faq": {
        "confusableIntents": [
            "ccb.channel.credit.bill.overview",
            "ccb.channel.credit.repay.faq",
            "ccb.channel.home.repayment.faq",
        ],
        "disambiguationHints": "用户询问账单规则、出账周期、账单日等知识时选本意图；查看具体账单金额选账单查询；询问还款规则选还款FAQ或首页还款FAQ。",
    },

    # ═══ credit.card-manage 簇（card.query 已有，补充其他） ═══
    "ccb.channel.credit.card-manage.activation": {
        "confusableIntents": [
            "ccb.channel.credit.card-manage.query",
            "ccb.channel.credit.card-manage.faq",
        ],
        "disambiguationHints": "用户明确要求激活新卡或开卡时选本意图；查询卡片状态或额度选卡片管理查询；询问卡片功能知识选卡片FAQ。",
    },
    "ccb.channel.credit.card-manage.limit-change": {
        "confusableIntents": [
            "ccb.channel.credit.card-manage.query",
            "ccb.channel.credit.limit.overview",
        ],
        "disambiguationHints": "用户明确要求调整信用卡额度（提额/降额）时选本意图；仅查询当前额度选卡片管理查询；查看额度申请进度选额度管理查询。",
    },
    "ccb.channel.credit.card-manage.loss-report": {
        "confusableIntents": [
            "ccb.channel.credit.card-manage.query",
            "ccb.channel.credit.card-manage.faq",
        ],
        "disambiguationHints": "用户明确要求挂失信用卡时选本意图；查询卡片状态选卡片管理查询；询问卡片安全问题选卡片FAQ。",
    },
    "ccb.channel.credit.card-manage.transactions-query": {
        "confusableIntents": [
            "ccb.channel.credit.card-manage.query",
            "ccb.channel.home.account.detail.query",
        ],
        "disambiguationHints": "用户关注信用卡消费记录、交易明细、退款状态时选本意图；查询卡片状态或额度选卡片管理查询；查询储蓄卡交易明细选账户详情查询。",
        "recallTerms": ["信用卡明细", "信用卡交易", "消费记录", "退款状态"],
    },
    "ccb.channel.credit.card-manage.faq": {
        "confusableIntents": [
            "ccb.channel.credit.card-manage.query",
            "ccb.channel.credit.card-manage.activation",
        ],
        "disambiguationHints": "用户询问信用卡功能、用卡知识、安全问题时选本意图；查询卡片状态或额度选卡片管理查询；要求激活卡片选卡片激活。",
    },
    "ccb.channel.credit.card-manage.security-settings-update": {
        "confusableIntents": [
            "ccb.channel.credit.card-manage.query",
            "ccb.channel.credit.card-manage.faq",
        ],
        "disambiguationHints": "用户明确要求修改卡片安全设置（交易密码、支付限额等）时选本意图；查询卡片状态选卡片管理查询；询问安全知识选卡片FAQ。",
    },

    # ═══ credit.repay 与 auto-repay 簇 ═══
    "ccb.channel.credit.auto-repay.setup": {
        "confusableIntents": [
            "ccb.channel.credit.auto-repay.overview",
            "ccb.channel.credit.repay.repay",
        ],
        "disambiguationHints": "用户明确要求设置或开通自动还款（约定还款）时选本意图；查看已有自动还款规则选约定还款查询；手动执行一次性还款选一键还款。",
        "recallTerms": ["自动还款", "自动扣款", "约定还款"],
    },
    "ccb.channel.credit.auto-repay.overview": {
        "confusableIntents": [
            "ccb.channel.credit.auto-repay.setup",
            "ccb.channel.credit.repay.overview",
        ],
        "disambiguationHints": "用户查看自动还款规则、扣款账户、还款方式时选本意图；设置自动还款选约定还款设置；查看当前应还金额选还款试算。",
        "recallTerms": ["自动还款", "自动扣款", "约定还款"],
    },

    # ═══ credit.installment 簇 ═══
    "ccb.channel.credit.installment.apply": {
        "confusableIntents": [
            "ccb.channel.credit.installment.overview",
            "ccb.channel.credit.bill.repay",
        ],
        "disambiguationHints": "用户明确要求办理账单分期时选本意图；查看可分期额度和费率选分期试算；直接还款选账单还款。",
        "recallTerms": ["账单分期", "分期还款", "申请分期"],
    },
    "ccb.channel.credit.installment.overview": {
        "confusableIntents": [
            "ccb.channel.credit.installment.apply",
            "ccb.channel.credit.bill.overview",
        ],
        "disambiguationHints": "用户查看可分期金额、分期费率、期数选择时选本意图；办理分期选分期申请；查看账单内容选账单查询。",
        "recallTerms": ["可分期", "分期手续费", "分期期数"],
    },

    # ═══ credit.cash 簇 ═══
    "ccb.channel.credit.cash.overview": {
        "confusableIntents": [
            "ccb.channel.credit.cash.apply",
            "ccb.channel.credit.cash.faq",
        ],
        "disambiguationHints": "用户查看现金转出进度、记录、状态时选本意图；办理现金转出选现金转出办理；询问现金转出知识选现金转出FAQ。",
        "recallTerms": ["现金转出", "取现", "预借现金"],
    },
    "ccb.channel.credit.cash.apply": {
        "confusableIntents": [
            "ccb.channel.credit.cash.overview",
        ],
        "disambiguationHints": "用户明确要求办理现金转出时选本意图；查看现金转出进度选现金转出查询。",
    },

    # ═══ credit.points / benefits / limit ═══
    "ccb.channel.credit.points.overview": {
        "confusableIntents": [
            "ccb.channel.credit.benefits.overview",
            "ccb.channel.credit.card-manage.query",
        ],
        "disambiguationHints": "用户关注积分余额、积分获取、积分过期时选本意图；查看卡权益选卡权益查询；查询卡片状态选卡片管理查询。",
        "recallTerms": ["积分", "龙积分", "积分余额"],
    },
    "ccb.channel.credit.benefits.overview": {
        "confusableIntents": [
            "ccb.channel.credit.points.overview",
            "ccb.channel.credit.card-manage.query",
        ],
        "disambiguationHints": "用户关注卡权益、权益状态、权益记录时选本意图；查看积分选龙积分查询；查询卡片状态选卡片管理查询。",
        "recallTerms": ["卡权益", "权益", "权益记录"],
    },
    "ccb.channel.credit.limit.overview": {
        "confusableIntents": [
            "ccb.channel.credit.card-manage.query",
            "ccb.channel.credit.card-manage.limit-change",
        ],
        "disambiguationHints": "用户查看额度申请进度、提额记录时选本意图；查询当前额度选卡片管理查询；要求调整额度选额度管理办理。",
    },

    # ═══ credit.apply / progress ═══
    "ccb.channel.credit.apply.overview": {
        "confusableIntents": [
            "ccb.channel.credit.apply.apply",
            "ccb.channel.credit.progress.overview",
        ],
        "disambiguationHints": "用户查看信用卡申请进度、申请记录时选本意图；办理新卡申请选信用卡办理；查看其他业务申请进度选申请进度查询。",
    },

    # ═══ home.repayment 簇（overview 已有，补充 repay/faq） ═══
    "ccb.channel.home.repayment.repay": {
        "confusableIntents": [
            "ccb.channel.home.repayment.overview",
            "ccb.channel.credit.repay.repay",
            "ccb.channel.credit.bill.repay",
        ],
        "disambiguationHints": "用户从首页还款入口明确要求执行还款动作时选本意图；查看应还金额选还款概览；从信用卡还款入口还款选信用卡还款。",
    },
    "ccb.channel.home.repayment.faq": {
        "confusableIntents": [
            "ccb.channel.home.repayment.overview",
            "ccb.channel.credit.repay.faq",
        ],
        "disambiguationHints": "用户询问信用卡还款规则、到账时效、手续费等知识时选本意图；查看应还金额选还款概览；询问一键还款知识选还款FAQ。",
        "recallTerms": ["信用卡还款", "还款规则", "还款手续费"],
    },

    # ═══ home.transfer 簇 ═══
    "ccb.channel.home.transfer.faq": {
        "confusableIntents": [
            "ccb.channel.home.transfer.create",
            "ccb.channel.home.transfer.query",
        ],
        "disambiguationHints": "用户询问转账规则、到账时效、手续费等知识时选本意图；办理转账选转账办理；查看转账记录选转账查询。",
        "recallTerms": ["转账规则", "转账手续费", "转账到账"],
    },

    # ═══ home.account 簇 ═══
    "ccb.channel.home.account.apply": {
        "confusableIntents": [
            "ccb.channel.home.account.query",
            "ccb.channel.home.account.faq",
        ],
        "disambiguationHints": "用户明确要求开户或办理账户时选本意图；查询账户余额选账户查询；询问账户知识选账户FAQ。",
    },
    "ccb.channel.home.account.faq": {
        "confusableIntents": [
            "ccb.channel.home.account.query",
            "ccb.channel.home.account.apply",
        ],
        "disambiguationHints": "用户询问账户功能、类型、安全等知识时选本意图；查询余额选账户查询；开户选账户办理。",
        "recallTerms": ["账户知识", "账户类型", "账户安全"],
    },

    # ═══ home.loan 簇 ═══
    "ccb.channel.home.loan.overview": {
        "confusableIntents": [
            "ccb.channel.home.loan.measure",
            "ccb.channel.home.loan.repayment",
        ],
        "disambiguationHints": "用户查看贷款概览、贷款情况时选本意图；测算贷款额度选贷款测算；查询还款计划选贷款还款。",
        "recallTerms": ["贷款", "贷款概览", "贷款情况"],
    },
    "ccb.channel.home.loan.repayment": {
        "confusableIntents": [
            "ccb.channel.home.loan.overview",
            "ccb.channel.home.repayment.overview",
        ],
        "disambiguationHints": "用户查看贷款还款计划、还款进度时选本意图；查看贷款概览选贷款概览；查看信用卡还款选还款概览。",
        "recallTerms": ["贷款还款", "还款计划", "贷款还款计划"],
    },

    # ═══ home.details 簇 ═══
    "ccb.channel.home.details.query": {
        "confusableIntents": [
            "ccb.channel.home.account.query",
            "ccb.channel.home.details.spending-explain",
        ],
        "disambiguationHints": "用户查看收支明细、交易详情时选本意图；查询账户余额选账户查询；查看收支说明选收支说明。",
        "recallTerms": ["收支明细", "交易详情", "明细"],
    },
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

            # 补充 confusableIntents（不覆盖已有值）
            if "confusableIntents" in supplement:
                existing = sd.get("confusableIntents", [])
                if not existing:
                    sd["confusableIntents"] = supplement["confusableIntents"]
                    scene["semantic_definition"] = sd
                    found = True

            # 补充 disambiguationHints（不覆盖已有值）
            if "disambiguationHints" in supplement:
                existing = sd.get("disambiguationHints")
                if not existing:
                    sd["disambiguationHints"] = supplement["disambiguationHints"]
                    scene["semantic_definition"] = sd
                    found = True

            # 补充 recallTerms（不覆盖已有值，追加不重复的）
            if "recallTerms" in supplement:
                existing = rp.get("recallTerms", [])
                if not existing:
                    rp["recallTerms"] = supplement["recallTerms"]
                    scene["retrieval_profile"] = rp
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
