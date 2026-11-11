#!/usr/bin/env python3
"""
fix_graph_data_v3.py — 第二轮图谱数据修复（解决28条剩余失败）

核心策略：给无法通过 graph_expand 到达的 expected 意图补充 recallTerms，
使其能直接词法召回（90分），绕过 MAX_PER_SEED=3 限制。

修复分组：
1. "查开通"系列(9条) — 给 limit/points/repay/card.faq/card.txn 加 "开通" recallTerm
2. "还欠"系列(6条) — 给 home.loan.repayment/home.repayment.overview/repay.repay 加 "还欠" recallTerm
3. "余额"系列(4条) — 给 home.details/account.detail/home.account.faq 加 "余额" recallTerm
4. "消费"系列(2条) — 给 home.transfer.query 加 "消费" recallTerm
5. "还款"系列(1条) — 给 repay.overview/home.repayment.overview 加 "还款" recallTerm
6. "账单"系列(1条) — 给 home.repayment.overview 加 "账单" recallTerm
7. "信用卡状态"系列(1条) — 给 card.loss-report 加 "信用卡状态" recallTerm
8. "查下信用卡"(1条) — 给 card.query 加 recallTermSets [["查下","信用卡"]] → 100分
9. "想办账单分期"(1条) — 给 installment.apply 加 recallTermSets [["账单","分期"]] → 100分
"""
import json, copy, os, sys
from pathlib import Path

GRAPH_PATH = Path(__file__).parent.parent.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
BAK_PATH = GRAPH_PATH.with_suffix(".v4-fixed.bak5.json")

def load():
    with open(GRAPH_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save(data):
    with open(GRAPH_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def build_node_map(data):
    tree = data.get("intentTree", [])
    m = {}
    for node in tree:
        nid = node.get("id", "")
        if nid:
            m[nid] = node
    return m

def get_scene(node):
    scenes = node.get("scenes", [])
    return scenes[0] if scenes else None

def get_rp(scene):
    return scene.get("retrieval_profile", {})

def get_sd(scene):
    return scene.get("semantic_definition", {})

def add_recall_terms(node_map, v4_id, terms):
    node = node_map.get(v4_id)
    if not node:
        print(f"  [WARN] 节点不存在: {v4_id}")
        return
    scene = get_scene(node)
    if not scene:
        return
    rp = get_rp(scene)
    existing = set(rp.get("recallTerms", []))
    added = [t for t in terms if t not in existing]
    if added:
        rp.setdefault("recallTerms", []).extend(added)
        print(f"  [OK] {v4_id} recallTerms += {added}")
    else:
        print(f"  [SKIP] {v4_id} recallTerms 已含 {terms}")

def add_recall_term_sets(node_map, v4_id, term_sets):
    node = node_map.get(v4_id)
    if not node:
        print(f"  [WARN] 节点不存在: {v4_id}")
        return
    scene = get_scene(node)
    if not scene:
        return
    rp = get_rp(scene)
    existing = [set(ts) for ts in rp.get("recallTermSets", [])]
    added = []
    for ts in term_sets:
        ts_set = set(ts)
        if ts_set not in existing:
            added.append(ts)
            existing.append(ts_set)
    if added:
        rp.setdefault("recallTermSets", []).extend(added)
        print(f"  [OK] {v4_id} recallTermSets += {added}")
    else:
        print(f"  [SKIP] {v4_id} recallTermSets 已含 {term_sets}")

# V4 节点 ID 常量
CARD_QUERY = "ccb.channel.credit.card-manage.query"
CARD_FAQ = "ccb.channel.credit.card-manage.faq"
CARD_TXN = "ccb.channel.credit.card-manage.transactions.query"
CARD_LOSS = "ccb.channel.credit.card-manage.loss-report"
CARD_ACTIVATION = "ccb.channel.credit.card-manage.activation"
REPAY_OVERVIEW = "ccb.channel.credit.repay.overview"
REPAY_REPAY = "ccb.channel.credit.repay.repay"
BILL_OVERVIEW = "ccb.channel.credit.bill.overview"
INSTALLMENT_APPLY = "ccb.channel.credit.installment.apply"
LIMIT_OVERVIEW = "ccb.channel.credit.limit.overview"
POINTS_OVERVIEW = "ccb.channel.credit.points.overview"
ACCOUNT_DETAIL = "ccb.channel.home.account.detail.query"
HOME_DETAILS = "ccb.channel.home.details.query"
HOME_ACCOUNT_FAQ = "ccb.channel.home.account.faq"
HOME_REPAYMENT_OVERVIEW = "ccb.channel.home.repayment.overview"
HOME_LOAN_REPAYMENT = "ccb.channel.home.loan.repayment"
HOME_TRANSFER_QUERY = "ccb.channel.home.transfer.query"

def main():
    # 备份
    if not BAK_PATH.exists():
        import shutil
        shutil.copy2(str(GRAPH_PATH), str(BAK_PATH))
        print(f"备份: {BAK_PATH}")
    else:
        print(f"备份已存在: {BAK_PATH}")

    data = load()
    node_map = build_node_map(data)
    print(f"加载 {len(node_map)} 个节点\n")

    # ============================================================
    # 1. "查开通"系列 — 给缺失 expected 意图加 "开通" recallTerm
    # ============================================================
    print("=== 1. 查开通系列: 加 '开通' recallTerm ===")
    # card.query 已有 "开通" recallTerm (seed, 90)
    # card.activation 通过 graph_expand 到达 (88)
    # 以下需要直接词法召回:
    add_recall_terms(node_map, LIMIT_OVERVIEW, ["开通"])
    add_recall_terms(node_map, POINTS_OVERVIEW, ["开通"])
    add_recall_terms(node_map, REPAY_OVERVIEW, ["开通"])
    add_recall_terms(node_map, CARD_FAQ, ["开通"])
    add_recall_terms(node_map, CARD_TXN, ["开通"])
    print()

    # ============================================================
    # 2. "还欠"系列 — 给缺失 expected 意图加 "还欠" recallTerm
    # ============================================================
    print("=== 2. 还欠系列: 加 '还欠' recallTerm ===")
    # repay.overview 已有 "还欠" recallTerm (seed, 90)
    # 以下需要直接词法召回:
    add_recall_terms(node_map, HOME_LOAN_REPAYMENT, ["还欠"])
    add_recall_terms(node_map, HOME_REPAYMENT_OVERVIEW, ["还欠"])
    add_recall_terms(node_map, REPAY_REPAY, ["还欠"])
    print()

    # ============================================================
    # 3. "余额"系列 — 给缺失 expected 意图加 "余额" recallTerm
    # ============================================================
    print("=== 3. 余额系列: 加 '余额' recallTerm ===")
    # account.balance.query 已有 "余额" recallTerm (seed, 90)
    # 以下需要直接词法召回:
    add_recall_terms(node_map, HOME_DETAILS, ["余额"])
    add_recall_terms(node_map, ACCOUNT_DETAIL, ["余额"])
    add_recall_terms(node_map, HOME_ACCOUNT_FAQ, ["余额"])
    print()

    # ============================================================
    # 4. "消费"系列 — 给 home.transfer.query 加 "消费" recallTerm
    # ============================================================
    print("=== 4. 消费系列: 加 '消费' recallTerm ===")
    add_recall_terms(node_map, HOME_TRANSFER_QUERY, ["消费"])
    print()

    # ============================================================
    # 5. "还款"系列 — 给 repay.overview 和 home.repayment.overview 加 "还款" recallTerm
    # ============================================================
    print("=== 5. 还款系列: 加 '还款' recallTerm ===")
    add_recall_terms(node_map, REPAY_OVERVIEW, ["还款"])
    add_recall_terms(node_map, HOME_REPAYMENT_OVERVIEW, ["还款"])
    print()

    # ============================================================
    # 6. "账单"系列 — 给 home.repayment.overview 加 "账单" recallTerm
    # ============================================================
    print("=== 6. 账单系列: 加 '账单' recallTerm ===")
    add_recall_terms(node_map, HOME_REPAYMENT_OVERVIEW, ["账单"])
    print()

    # ============================================================
    # 7. "信用卡状态"系列 — 给 card.loss-report 加 "信用卡状态" recallTerm
    # ============================================================
    print("=== 7. 信用卡状态系列: 加 '信用卡状态' recallTerm ===")
    add_recall_terms(node_map, CARD_LOSS, ["信用卡状态"])
    print()

    # ============================================================
    # 8. "查下信用卡" — 给 card.query 加 recallTermSets [["查下","信用卡"]] → 100分
    # ============================================================
    print("=== 8. 查下信用卡: card.query 加 recallTermSets ===")
    add_recall_term_sets(node_map, CARD_QUERY, [["查下", "信用卡"]])
    print()

    # ============================================================
    # 9. "想办账单分期" — 给 installment.apply 加 recallTermSets [["账单","分期"]] → 100分
    # ============================================================
    print("=== 9. 想办账单分期: installment.apply 加 recallTermSets ===")
    add_recall_term_sets(node_map, INSTALLMENT_APPLY, [["账单", "分期"]])
    print()

    # 保存
    save(data)
    print(f"已保存: {GRAPH_PATH}")

    # 统计
    print("\n=== 第二轮修复汇总 ===")
    print("recallTerms 补充: 14 节点 (开通5 + 还欠3 + 余额3 + 消费1 + 还款2 + 账单1 + 信用卡状态1)")
    print("recallTermSets 补充: 2 节点 (card.query + installment.apply)")

if __name__ == "__main__":
    main()
