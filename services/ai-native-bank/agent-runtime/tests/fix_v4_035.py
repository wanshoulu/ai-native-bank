#!/usr/bin/env python
"""修复 V4-035 "查下还款" 数据缺口：

问题：用户说"查下还款"时，期望 credit.repay.overview 在候选中，
但它既不是直接匹配也不是 confusable 兄弟。

修复策略：
1. credit.repay.overview 的 recallTerms 缺少"还款"，追加。
2. home.repayment.overview 与 credit.repay.overview 之间补双向 confusableIntents。
3. home.loan.repayment 与 credit.repay.overview 之间补双向 confusableIntents。
"""
import json
import shutil
from pathlib import Path

GRAPH_PATH = Path(__file__).resolve().parent.parent.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"

# 需要修复的 V2 intent_id
V2_CREDIT_REPAY_OVERVIEW = "ccb.credit.repay.overview"
V2_HOME_REPAYMENT_OVERVIEW = "ccb.home.repayment.overview"
V2_HOME_LOAN_REPAYMENT = "ccb.home.loan.repayment"


def build_v2_to_v4_mapping(tree):
    """遍历 intentTree 中的 leaf 节点，通过 observability.sourceRecords
    构建 V2 intent_id → V4 节点 ID 的映射。"""
    mapping = {}
    for node in tree:
        if node.get("node_role") != "leaf":
            continue
        obs = node.get("observability", {})
        for record in obs.get("sourceRecords", []):
            ref = record.get("ref", "")
            if ref.startswith("v2-source:"):
                v2_id = ref[len("v2-source:"):]
                mapping[v2_id] = node["id"]
    return mapping


def append_unique(target_list, items):
    """向 target_list 追加 items 中不已存在的值（追加不覆盖，去重）。
    返回实际追加的值列表。"""
    appended = []
    for item in items:
        if item not in target_list:
            target_list.append(item)
            appended.append(item)
    return appended


def fix_graph():
    """执行修复主逻辑"""
    # 读取 JSON
    data = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    tree = data.get("intentTree", [])

    # 构建 V2 → V4 映射
    v2_to_v4 = build_v2_to_v4_mapping(tree)

    # 确认三个目标 V2 intent_id 都能找到对应的 V4 节点
    required_v2_ids = [V2_CREDIT_REPAY_OVERVIEW, V2_HOME_REPAYMENT_OVERVIEW, V2_HOME_LOAN_REPAYMENT]
    for v2_id in required_v2_ids:
        if v2_id not in v2_to_v4:
            print(f"错误: 找不到 V2 intent_id '{v2_id}' 对应的 V4 节点")
            return

    credit_repay_v4 = v2_to_v4[V2_CREDIT_REPAY_OVERVIEW]
    home_repayment_v4 = v2_to_v4[V2_HOME_REPAYMENT_OVERVIEW]
    home_loan_v4 = v2_to_v4[V2_HOME_LOAN_REPAYMENT]

    print(f"V4 节点映射:")
    print(f"  {V2_CREDIT_REPAY_OVERVIEW}  → {credit_repay_v4}")
    print(f"  {V2_HOME_REPAYMENT_OVERVIEW} → {home_repayment_v4}")
    print(f"  {V2_HOME_LOAN_REPAYMENT}     → {home_loan_v4}")
    print()

    # 记录所有修改
    changes = []

    # 遍历 leaf 节点执行修复
    for node in tree:
        if node.get("node_role") != "leaf":
            continue
        v4_id = node["id"]

        for scene in node.get("scenes", []):
            sem_def = scene.get("semantic_definition", {})
            rp = scene.get("retrieval_profile", {})

            # ── 修复 1: credit.repay.overview 的 recallTerms 追加"还款" ──
            if v4_id == credit_repay_v4:
                recall_terms = rp.get("recallTerms", [])
                if recall_terms:  # 仅当节点已有 recallTerms 时才追加
                    appended = append_unique(recall_terms, ["还款"])
                    if appended:
                        rp["recallTerms"] = recall_terms
                        changes.append((v4_id, "recallTerms", appended))

            # ── 修复 2: home.repayment.overview 和 home.loan.repayment
            #    追加 credit.repay.overview 的 V4 节点 ID 到 confusableIntents ──
            if v4_id in (home_repayment_v4, home_loan_v4):
                confusables = sem_def.get("confusableIntents", [])
                if confusables is not None:
                    appended = append_unique(confusables, [credit_repay_v4])
                    if appended:
                        sem_def["confusableIntents"] = confusables
                        changes.append((v4_id, "confusableIntents", appended))

            # ── 修复 3: credit.repay.overview 反向追加
            #    home.repayment.overview 和 home.loan.repayment 的 V4 节点 ID ──
            if v4_id == credit_repay_v4:
                confusables = sem_def.get("confusableIntents", [])
                if confusables is not None:
                    appended = append_unique(confusables, [home_repayment_v4, home_loan_v4])
                    if appended:
                        sem_def["confusableIntents"] = confusables
                        changes.append((v4_id, "confusableIntents", appended))

    # 先备份为 .bak3.json
    backup_path = GRAPH_PATH.with_suffix(".bak3.json")
    shutil.copy2(GRAPH_PATH, backup_path)
    print(f"已备份原文件到: {backup_path}")
    print()

    # 保存修改后的文件
    GRAPH_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 输出修改摘要
    print(f"共修改 {len(changes)} 处:")
    print("-" * 60)
    for v4_id, field, appended in changes:
        print(f"  节点: {v4_id}")
        print(f"  字段: {field}")
        print(f"  追加: {appended}")
        print("-" * 60)


if __name__ == "__main__":
    fix_graph()
