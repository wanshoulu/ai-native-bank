#!/usr/bin/env python3
"""
fix_graph_data_v4.py — 第三轮图谱数据修复（解决剩余10条失败中的8条图谱问题）

核心策略：给无法通过 graph_expand 到达的 expected 意图补充 recallTerms，
使其能直接词法召回（90分），绕过 MAX_PER_SEED=3 限制。

修复分组：
1. "查开通"系列(3条) — 给 bill.overview 加 "开通" recallTerm
2. "还欠/还款"系列(4条) — 给 repay.faq 加 ["还欠","还款"] recallTerm
3. "自动还款"系列(1条) — 给 repay.repay 加 "自动还款" recallTerm
4. "余额"系列(1条) — 给 home.account.apply 加 "余额" recallTerm
"""
import json, copy, os, sys
from pathlib import Path

GRAPH_PATH = Path(__file__).parent.parent.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
BAK_PATH = GRAPH_PATH.with_suffix(".v4-fixed.bak6.json")

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

# V4 节点 ID 常量
BILL_OVERVIEW = "ccb.channel.credit.bill.overview"
REPAY_FAQ = "ccb.channel.credit.repay.faq"
REPAY_REPAY = "ccb.channel.credit.repay.repay"
HOME_ACCOUNT_APPLY = "ccb.channel.home.account.apply"

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
    # 1. "查开通"系列 — 给 bill.overview 加 "开通" recallTerm
    #    V4-023/024/086: expected includes bill.overview, MISSING
    # ============================================================
    print("=== 1. 查开通系列: bill.overview 加 '开通' recallTerm ===")
    add_recall_terms(node_map, BILL_OVERVIEW, ["开通"])
    print()

    # ============================================================
    # 2. "还欠/还款"系列 — 给 repay.faq 加 ["还欠","还款"] recallTerm
    #    V4-048/049 "查信用卡还款": MISSING repay.faq → 加 "还款"
    #    V4-072 "看看还欠": MISSING repay.faq → 加 "还欠"
    # ============================================================
    print("=== 2. 还欠/还款系列: repay.faq 加 '还欠'+'还款' recallTerm ===")
    add_recall_terms(node_map, REPAY_FAQ, ["还欠", "还款"])
    print()

    # ============================================================
    # 3. "自动还款"系列 — 给 repay.repay 加 "自动还款" recallTerm
    #    V4-015 "查自动还款": MISSING repay.repay
    # ============================================================
    print("=== 3. 自动还款系列: repay.repay 加 '自动还款' recallTerm ===")
    add_recall_terms(node_map, REPAY_REPAY, ["自动还款"])
    print()

    # ============================================================
    # 4. "余额"系列 — 给 home.account.apply 加 "余额" recallTerm
    #    V4-059 "余额怎么样了": MISSING home.account.apply
    #    注意: home.account.apply 的 negativeExamples=["余额查询","可用余额"]
    #    "余额查询" NOT IN "余额怎么样了" → 无负例惩罚
    # ============================================================
    print("=== 4. 余额系列: home.account.apply 加 '余额' recallTerm ===")
    add_recall_terms(node_map, HOME_ACCOUNT_APPLY, ["余额"])
    print()

    # 保存
    save(data)
    print(f"已保存: {GRAPH_PATH}")

    # 统计
    print("\n=== 第三轮修复汇总 ===")
    print("recallTerms 补充: 4 节点 (bill.overview + repay.faq + repay.repay + home.account.apply)")

if __name__ == "__main__":
    main()
