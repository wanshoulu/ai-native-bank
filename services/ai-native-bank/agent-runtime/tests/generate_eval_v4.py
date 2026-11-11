#!/usr/bin/env python
"""生成 200+ V4 意图评测测试用例，覆盖全部 37 个 confusableIntents 节点。

核心策略:
- 对于每个跨父级 confusable 对 (A, B): A 的 recallTerm 匹配 (score=90)，
  B 不直接匹配，fixed 通过 confusable 扩召 (0.85*90=76≥60) 召回 B，
  buggy 因 confusableIntents 清零无法扩召 B → 产生差异
- 对于同父级 confusable 对: A score=90，fixed 扩召 B (76≥60)，
  buggy 以普通兄弟扩召 (0.65*90=58<60) 被丢弃 → 产生差异
- 关键: query 必须匹配 recallTerm 但不匹配 alias (否则 score=165，普通扩召=80≥60 不被丢弃)
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # agent-runtime/
sys.path.insert(0, str(ROOT))

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"


def normalize(v):
    return re.sub(r"[\s，,。；;、！？!?]+", "", str(v or "").lower())


# ── 加载图谱 ──
payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
tree = payload["intentTree"]

# ── 提取所有 leaf 节点的 scene 信息 ──
nodes = []
for node in tree:
    if node.get("node_role") != "leaf":
        continue
    v2_id = None
    for rec in node.get("observability", {}).get("sourceRecords", []):
        if rec.get("ref", "").startswith("v2-source:"):
            v2_id = rec["ref"][len("v2-source:"):]
            break
    for scene in node.get("scenes", []):
        rp = scene.get("retrieval_profile", {})
        sd = scene.get("semantic_definition", {})
        tb = scene.get("task_binding", {})
        n = {
            "v4_id": node["id"],
            "v2_id": v2_id or f"{node['id']}#{scene.get('scene_type', 'DO')}",
            "parent_id": node.get("parent_intent_id", ""),
            "display_name": sd.get("userGoal", node.get("title", "")),
            "scene_type": scene.get("scene_type", "DO"),
            "action_code": tb.get("actionCode", scene.get("scene_type", "query").lower()),
            "aliases": rp.get("aliases", []),
            "recallTerms": rp.get("recallTerms", []),
            "recallTermSets": rp.get("recallTermSets", []),
            "negativeExamples": rp.get("negativeExamples", []),
            "confusableIntents": sd.get("confusableIntents", []),
            "semanticPriority": rp.get("semanticPriority", 0),
            "domain": "",
        }
        # 提取 domain
        pid = node.get("parent_intent_id", "")
        while pid:
            p = next((x for x in tree if x["id"] == pid), None)
            if not p:
                break
            if p.get("node_role") == "domain":
                n["domain"] = p.get("title", "")
                break
            pid = p.get("parent_intent_id", "")
        nodes.append(n)

v4_to_v2 = {n["v4_id"]: n["v2_id"] for n in nodes if n["v4_id"] != n["v2_id"]}
v2_to_node = {n["v2_id"]: n for n in nodes}


# ── 匹配函数 (与 v4_graph_adapter.py 完全一致) ──
def alias_match(q, node):
    nq = normalize(q)
    for a in node["aliases"]:
        na = normalize(a)
        if len(na) >= 2 and (na in nq or nq in na):
            return True
    return False


def recall_match(q, node):
    nq = normalize(q)
    for t in node["recallTerms"]:
        nt = normalize(t)
        if len(nt) >= 2 and nt in nq:
            return True
    return False


def neg_match(q, node):
    nq = normalize(q)
    for item in node["negativeExamples"]:
        ni = normalize(item)
        if len(ni) >= 2 and ni in nq:
            return True
    return False


def calc_score(q, node):
    nq = normalize(q)
    score = 0
    alias_score = 0
    for a in node["aliases"]:
        na = normalize(a)
        if len(na) >= 2 and (na in nq or nq in na):
            if na == nq:
                alias_score = 110
                break
            elif alias_score < 75:
                alias_score = 75
    score += alias_score
    for t in node["recallTerms"]:
        nt = normalize(t)
        if len(nt) >= 2 and nt in nq:
            score += 90
            break
    for item in node["negativeExamples"]:
        ni = normalize(item)
        if len(ni) >= 2 and ni in nq:
            score -= 160
            break
    return score


# ── 找到所有 confusable 对 ──
pairs = set()
for n in nodes:
    for conf in n["confusableIntents"]:
        conf_v2 = v4_to_v2.get(conf, conf)
        if conf_v2 in v2_to_node and n["v2_id"] != conf_v2:
            pairs.add(tuple(sorted([n["v2_id"], conf_v2])))

cross_pairs = []
same_pairs = []
for a_id, b_id in sorted(pairs):
    a, b = v2_to_node[a_id], v2_to_node[b_id]
    if a["parent_id"] != b["parent_id"]:
        cross_pairs.append((a, b))
    else:
        same_pairs.append((a, b))

print(f"跨父级 confusable 对: {len(cross_pairs)}")
print(f"同父级 confusable 对: {len(same_pairs)}")


# ── 口语化 query 模板 ──
TEMPLATES = [
    "{t}", "查{t}", "看看{t}", "查下{t}", "帮我查{t}",
    "{t}是多少", "{t}怎么样了", "我的{t}", "查查{t}", "想看{t}",
    "帮我看看{t}", "我想查{t}", "看下{t}", "查一下{t}",
    "我的{t}情况", "帮我查下{t}", "看看我的{t}", "想知道{t}",
    "查查我的{t}", "帮我看下{t}", "问下{t}", "查查{t}多少",
    "看看{t}多少", "查下{t}怎么样", "我的{t}是多少",
]


def gen_queries(node):
    """生成匹配 recallTerms 但不匹配 aliases 的口语化 query (score=90)"""
    results = []
    for term in node["recallTerms"]:
        nt = normalize(term)
        if len(nt) < 2:
            continue
        for tmpl in TEMPLATES:
            q = tmpl.format(t=term)
            nq = normalize(q)
            if nt not in nq:
                continue
            if alias_match(q, node):
                continue
            if neg_match(q, node):
                continue
            if calc_score(q, node) == 90:
                results.append(q)
    return list(dict.fromkeys(results))  # 去重保序


# ── 生成测试用例 ──
cases = []
cid = [0]


def add(query, diff, shape, domain, exp_intents, rationale, src_nodes,
        amb_type="", clarify=False, decision="execute", slots=None):
    cid[0] += 1
    cases.append({
        "case_id": f"V4-{cid[0]:03d}",
        "query": query,
        "user_input": query,
        "difficulty": diff,
        "intent_shape": shape,
        "domain": domain,
        "expression_style": "concise" if len(query) <= 4 else "natural",
        "expected_intents": exp_intents,
        "expected_intent_names": [e["display_name"] for e in exp_intents],
        "expected_actions": [e["action_code"] for e in exp_intents],
        "clarify_needed": clarify,
        "expected_decision_type": decision,
        "slots": slots or {},
        "ambiguity_type": amb_type,
        "rationale": rationale,
        "source_graph_nodes": src_nodes,
    })


def make_intent(node, role="primary"):
    return {
        "intent_id": node["v2_id"],
        "intent_name": node["display_name"],
        "display_name": node["display_name"],
        "action_code": node["action_code"],
        "domain": node["domain"],
        "role": role,
    }


# ── 1. 跨父级 confusable 对 → ambiguous_intent 用例 (核心差异化) ──
ambiguous_count = 0
for a, b in cross_pairs:
    queries = gen_queries(a)
    if not queries:
        queries = gen_queries(b)
        if queries:
            a, b = b, a
    if not queries:
        continue

    for q in queries[:2]:  # 每对最多取2个query
        b_score = calc_score(q, b)
        if b_score >= 60:
            continue  # B 直接匹配，无差异

        diff = "ultra" if len(q) <= 3 else ("hard" if len(q) <= 6 else "medium")
        is_cross_domain = a["domain"] != b["domain"]
        domain = "跨域" if is_cross_domain else a["domain"]
        amb_type = "domain_ambiguity" if is_cross_domain else "action_target_ambiguity"

        add(q, diff, "ambiguous_intent", domain,
            [make_intent(a, "candidate"), make_intent(b, "candidate")],
            f"'{q}'匹配{a['display_name']}的recallTerm(score=90)，"
            f"{b['display_name']}不直接匹配，fixed通过confusable扩召(0.85*90=76)进入候选，"
            f"buggy因confusableIntents清零无法扩召",
            [a["v4_id"], b["v4_id"]],
            amb_type, clarify=True, decision="clarify")
        ambiguous_count += 1

print(f"跨父级 ambiguous_intent 用例: {ambiguous_count}")

# ── 2. 同父级 confusable 对 → ambiguous_intent 用例 ──
same_ambiguous_count = 0
for a, b in same_pairs:
    queries = gen_queries(a)
    if not queries:
        queries = gen_queries(b)
        if queries:
            a, b = b, a
    if not queries:
        continue

    q = queries[0]
    b_score = calc_score(q, b)
    if b_score >= 60:
        continue

    diff = "ultra" if len(q) <= 3 else ("hard" if len(q) <= 6 else "medium")

    add(q, diff, "ambiguous_intent", a["domain"],
        [make_intent(a, "candidate"), make_intent(b, "candidate")],
        f"'{q}'匹配{a['display_name']}的recallTerm(score=90)，"
        f"{b['display_name']}不直接匹配，fixed通过confusable扩召(76)进入候选，"
        f"buggy普通兄弟扩召(58<60)被丢弃",
        [a["v4_id"], b["v4_id"]],
        "action_target_ambiguity", clarify=True, decision="clarify")
    same_ambiguous_count += 1

print(f"同父级 ambiguous_intent 用例: {same_ambiguous_count}")

# ── 3. 三元组 ambiguous_intent 用例 (一个query匹配A, 期望A+B+C) ──
triple_count = 0
for n in nodes:
    confs = n["confusableIntents"]
    if len(confs) < 2:
        continue
    queries = gen_queries(n)
    if not queries:
        continue

    # 找到两个不直接匹配的 confusable 兄弟
    exp = [make_intent(n, "candidate")]
    src = [n["v4_id"]]
    for conf in confs:
        conf_v2 = v4_to_v2.get(conf, conf)
        conf_node = v2_to_node.get(conf_v2)
        if not conf_node:
            continue
        q = queries[0]
        if calc_score(q, conf_node) < 60:
            exp.append(make_intent(conf_node, "candidate"))
            src.append(conf_node["v4_id"])

    if len(exp) >= 3:
        q = queries[0]
        diff = "ultra" if len(q) <= 3 else "hard"
        add(q, diff, "ambiguous_intent", "跨域",
            exp,
            f"'{q}'匹配{n['display_name']}的recallTerm，"
            f"多个confusable兄弟通过扩召进入候选",
            src,
            "domain_ambiguity", clarify=True, decision="clarify")
        triple_count += 1

print(f"三元组 ambiguous_intent 用例: {triple_count}")

# ── 4. single_intent 用例 (覆盖全部37个confusableIntents节点) ──
single_count = 0
seen = set()
for n in nodes:
    if not n["confusableIntents"]:
        continue
    if n["v2_id"] in seen:
        continue
    seen.add(n["v2_id"])

    # 优先用 alias 精确匹配 (easy)
    best_q = None
    best_score = 0
    for alias in n["aliases"][:8]:
        s = calc_score(alias, n)
        if s > best_score:
            best_q = alias
            best_score = s

    if best_q and best_score >= 110:
        add(best_q, "easy", "single_intent", n["domain"],
            [make_intent(n, "primary")],
            f"别名精确命中，无歧义",
            [n["v4_id"]])
        single_count += 1
    elif best_q and best_score >= 75:
        add(best_q, "medium", "single_intent", n["domain"],
            [make_intent(n, "primary")],
            f"别名包含匹配",
            [n["v4_id"]])
        single_count += 1
    else:
        # 用 recallTerm 匹配
        queries = gen_queries(n)
        if queries:
            q = queries[0]
            add(q, "medium", "single_intent", n["domain"],
                [make_intent(n, "primary")],
                f"recallTerm匹配",
                [n["v4_id"]])
            single_count += 1

# 补充 hard single_intent (用口语化/短query)
for n in nodes:
    if not n["confusableIntents"]:
        continue
    if len(cases) >= 120:
        break
    queries = gen_queries(n)
    if len(queries) >= 2:
        q = queries[1]
        diff = "hard" if len(q) <= 6 else "medium"
        add(q, diff, "single_intent", n["domain"],
            [make_intent(n, "primary")],
            f"口语化recallTerm匹配，需区分confusable兄弟",
            [n["v4_id"]])
        single_count += 1

print(f"single_intent 用例: {single_count}")

# ── 5. multi_intent 用例 ──
# 组合两个不同域的 recallTerm query
multi_combos = [
    ("查余额", "ccb.account.balance.query", "信用卡还多少", "ccb.credit.repay.overview"),
    ("看看流水", "ccb.account.transactions.query", "信用卡欠多少", "ccb.credit.repay.overview"),
    ("查余额", "ccb.account.balance.query", "查下信用卡账单", "ccb.credit.bill.overview"),
    ("帮我转账", "ccb.transfer.create", "查下信用卡还欠多少", "ccb.credit.repay.overview"),
    ("还信用卡", "ccb.credit.repay.repay", "查下余额", "ccb.account.balance.query"),
    ("查下积分", "ccb.credit.points.overview", "看看信用卡额度", "ccb.credit.card.query"),
    ("查下流水", "ccb.account.transactions.query", "还信用卡", "ccb.credit.repay.repay"),
    ("看看信用卡账单", "ccb.credit.bill.overview", "转账给李四500", "ccb.transfer.create"),
    ("查下信用卡欠多少", "ccb.credit.repay.overview", "看看流水", "ccb.account.transactions.query"),
    ("查余额", "ccb.account.balance.query", "信用卡挂失", "ccb.credit.card.loss-report"),
    ("查下信用卡能分期多少", "ccb.credit.installment.overview", "看看积分", "ccb.credit.points.overview"),
    ("还完信用卡", "ccb.credit.repay.repay", "查下房贷还款计划", "ccb.home.loan.repayment"),
    ("查下转账记录", "ccb.transfer.payee.query", "信用卡还欠多少", "ccb.credit.repay.overview"),
    ("查下信用卡账单", "ccb.credit.bill.overview", "看看信用卡积分", "ccb.credit.points.overview"),
    ("设置自动还款", "ccb.credit.auto-repay.setup", "查下余额", "ccb.account.balance.query"),
    ("信用卡提额", "ccb.credit.card.limit-change", "查下流水", "ccb.account.transactions.query"),
    ("查下信用卡账单", "ccb.credit.bill.overview", "还信用卡", "ccb.credit.repay.repay"),
    ("看看信用卡权益", "ccb.credit.benefits.overview", "查下积分", "ccb.credit.points.overview"),
    ("查下流水", "ccb.account.transactions.query", "查下信用卡能取多少现金", "ccb.credit.cash.overview"),
    ("查下还款计划", "ccb.home.loan.repayment", "还信用卡", "ccb.credit.repay.repay"),
]

multi_count = 0
for q1, id1, q2, id2 in multi_combos:
    n1 = v2_to_node.get(id1)
    n2 = v2_to_node.get(id2)
    if not n1 or not n2:
        continue
    query = f"{q1}然后{q2}"
    diff = "hard" if len(query) <= 12 else "ultra"
    domain = "跨域" if n1["domain"] != n2["domain"] else n1["domain"]
    add(query, diff, "multi_intent", domain,
        [make_intent(n1, "primary"), make_intent(n2, "secondary")],
        f"'然后'拆分：{n1['display_name']} + {n2['display_name']}",
        [n1["v4_id"], n2["v4_id"]],
        decision="route")
    multi_count += 1

print(f"multi_intent 用例: {multi_count}")

# ── 6. non_task 用例 ──
non_tasks = [
    ("你好", "easy", "寒暄，非业务任务"),
    ("今天天气怎么样", "easy", "与银行业务无关"),
    ("谢谢", "easy", "寒暄，非业务任务"),
    ("你是机器人吗", "easy", "身份询问，非业务任务"),
    ("帮我定个闹钟", "medium", "手机功能，非银行业务"),
    ("今天几号", "easy", "日期查询，非银行业务"),
    ("讲个笑话", "easy", "娱乐，非银行业务"),
    ("附近的餐厅", "medium", "生活服务，非银行业务"),
    ("帮我查快递", "medium", "快递查询，非银行业务"),
    ("播放音乐", "easy", "多媒体，非银行业务"),
    ("你好啊", "easy", "寒暄，非业务任务"),
    ("再见", "easy", "寒暄，非业务任务"),
    ("设置闹钟明天7点", "medium", "手机功能，非银行业务"),
    ("打开蓝牙", "easy", "手机功能，非银行业务"),
    ("导航回家", "medium", "导航，非银行业务"),
    ("翻译一下hello", "medium", "翻译，非银行业务"),
    ("计算器", "easy", "工具，非银行业务"),
    ("记一下明天开会", "medium", "备忘录，非银行业务"),
    ("你叫什么名字", "easy", "身份询问，非业务任务"),
    ("银行卡怎么办", "medium", "问题过于宽泛，无法定位具体意图"),
]

for q, diff, reason in non_tasks:
    add(q, diff, "non_task", "", [], reason, [])

print(f"non_task 用例: {len(non_tasks)}")

# ── 7. 补充 ultra 难度短query用例 ──
short_queries = [
    ("还款", "ccb.credit.repay.overview", "ccb.home.repayment.overview", "跨域"),
    ("账单", "ccb.credit.bill.overview", "ccb.home.repayment.overview", "跨域"),
    ("还钱", "ccb.credit.repay.repay", "ccb.home.repayment.repay", "跨域"),
    ("流水", "ccb.account.transactions.query", "ccb.credit.card.transactions.query", "跨域"),
    ("积分", "ccb.credit.points.overview", "ccb.credit.benefits.overview", "信用卡"),
    ("额度", "ccb.credit.card.query", "ccb.credit.card.limit-change", "信用卡"),
    ("挂失", "ccb.credit.card.loss-report", "ccb.credit.card.query", "信用卡"),
    ("激活", "ccb.credit.card.activation", "ccb.credit.card.query", "信用卡"),
    ("分期", "ccb.credit.installment.overview", "ccb.credit.installment.apply", "信用卡"),
    ("取现", "ccb.credit.cash.overview", "ccb.credit.cash.apply", "信用卡"),
    ("贷款", "ccb.home.loan.overview", "ccb.home.loan.repayment", "首页"),
    ("转账", "ccb.transfer.create", "ccb.transfer.payee.query", "首页"),
    ("收款人", "ccb.transfer.payee.query", "ccb.transfer.create", "首页"),
    ("权益", "ccb.credit.benefits.overview", "ccb.credit.points.overview", "信用卡"),
    ("明细", "ccb.account.detail.query", "ccb.credit.card.transactions.query", "跨域"),
]

ultra_count = 0
for q, id1, id2, domain in short_queries:
    n1 = v2_to_node.get(id1)
    n2 = v2_to_node.get(id2)
    if not n1 or not n2:
        continue
    add(q, "ultra", "ambiguous_intent", domain,
        [make_intent(n1, "candidate"), make_intent(n2, "candidate")],
        f"极短query '{q}'，多个意图可能匹配，需澄清",
        [n1["v4_id"], n2["v4_id"]],
        "domain_ambiguity" if domain == "跨域" else "action_target_ambiguity",
        clarify=True, decision="clarify")
    ultra_count += 1

print(f"ultra 短query 用例: {ultra_count}")

# ── 8. 补充 hard single_intent 口语化用例 ──
hard_singles = [
    ("信用卡最低要还多少", "ccb.credit.repay.overview", "信用卡", "最低还款是还款试算的关注点"),
    ("查查信用卡欠了多少钱", "ccb.credit.repay.overview", "信用卡", "口语化查询欠款金额"),
    ("看看信用卡本月要还多少", "ccb.credit.repay.overview", "信用卡", "口语化查询应还金额"),
    ("信用卡还差多少没还", "ccb.credit.repay.overview", "信用卡", "口语化查未还金额"),
    ("查查信用卡账单出来了没", "ccb.credit.bill.overview", "信用卡", "口语化查账单状态"),
    ("信用卡账单到期了吗", "ccb.credit.bill.overview", "信用卡", "查账单到期日"),
    ("看看信用卡本月账单金额", "ccb.credit.bill.overview", "信用卡", "查本期账单金额"),
    ("帮我查下信用卡流水", "ccb.credit.card.transactions.query", "信用卡", "口语化查交易明细"),
    ("信用卡最近花了哪些钱", "ccb.credit.card.transactions.query", "信用卡", "口语化查消费记录"),
    ("看看信用卡退款到账没", "ccb.credit.card.transactions.query", "信用卡", "查退款状态"),
    ("想给信用卡提额", "ccb.credit.card.limit-change", "信用卡", "口语化提额请求"),
    ("信用卡额度能调高吗", "ccb.credit.card.limit-change", "信用卡", "问额度调整"),
    ("想降低信用卡额度", "ccb.credit.card.limit-change", "信用卡", "降额请求"),
    ("信用卡怎么激活", "ccb.credit.card.activation", "信用卡", "口语化问激活方法"),
    ("新卡怎么开卡", "ccb.credit.card.activation", "信用卡", "口语化问开卡"),
    ("信用卡丢了咋办", "ccb.credit.card.loss-report", "信用卡", "口语化挂失"),
    ("卡不见了怎么处理", "ccb.credit.card.loss-report", "信用卡", "口语化挂失"),
    ("想办账单分期", "ccb.credit.installment.apply", "信用卡", "口语化申请分期"),
    ("信用卡能分几期", "ccb.credit.installment.overview", "信用卡", "查可分期数"),
    ("分期手续费多少", "ccb.credit.installment.overview", "信用卡", "查分期费率"),
    ("查下信用卡能取多少现金", "ccb.credit.cash.overview", "信用卡", "查取现额度"),
    ("信用卡预借现金能借多少", "ccb.credit.cash.overview", "信用卡", "查预借现金额度"),
    ("积分什么时候过期", "ccb.credit.points.overview", "信用卡", "查积分有效期"),
    ("积分怎么来的", "ccb.credit.points.overview", "信用卡", "查积分获取方式"),
    ("信用卡有什么优惠活动", "ccb.credit.benefits.overview", "信用卡", "口语化查权益"),
    ("信用卡权益怎么查", "ccb.credit.benefits.overview", "信用卡", "查权益信息"),
    ("自动还款怎么查", "ccb.credit.auto-repay.overview", "信用卡", "查自动还款设置"),
    ("自动还款绑的哪张卡", "ccb.credit.auto-repay.overview", "信用卡", "查扣款账户"),
    ("想开通自动还款", "ccb.credit.auto-repay.setup", "信用卡", "设置自动还款"),
    ("改下自动还款的卡", "ccb.credit.auto-repay.setup", "信用卡", "修改扣款账户"),
    ("信用卡申请到哪一步了", "ccb.credit.apply.overview", "信用卡", "查申请进度"),
    ("办卡进度怎么样了", "ccb.credit.apply.overview", "信用卡", "口语化查进度"),
    ("查查银行卡余额", "ccb.account.balance.query", "首页", "口语化查余额"),
    ("卡里还剩多少钱", "ccb.account.balance.query", "首页", "口语化查余额"),
    ("账户里还有多少", "ccb.account.balance.query", "首页", "口语化查余额"),
    ("查下账户明细", "ccb.account.transactions.query", "首页", "查流水"),
    ("看看最近的交易记录", "ccb.account.transactions.query", "首页", "查交易记录"),
    ("查下账户详情", "ccb.account.detail.query", "首页", "查账户信息"),
    ("钱花哪去了", "ccb.cashflow.spending-explain", "首页", "口语化支出分析"),
    ("这个月钱花得太多了", "ccb.cashflow.spending-explain", "首页", "口语化支出分析"),
    ("查下我的贷款", "ccb.home.loan.overview", "首页", "查贷款概览"),
    ("房贷还款计划怎么查", "ccb.home.loan.repayment", "首页", "查贷款还款计划"),
    ("每期还多少", "ccb.home.loan.repayment", "首页", "查每期还款额"),
    ("怎么转账给别人", "ccb.transfer.create", "首页", "口语化转账"),
    ("给张三转500", "ccb.transfer.create", "首页", "口语化转账+slot"),
    ("查下转账记录", "ccb.transfer.payee.query", "首页", "查转账状态"),
    ("常用收款人有哪些", "ccb.transfer.payee.query", "首页", "查收款人列表"),
    ("转账多久到账", "ccb.home.transfer.faq", "首页", "转账FAQ"),
    ("转账收手续费吗", "ccb.home.transfer.faq", "首页", "转账FAQ"),
    ("信用卡怎么还款", "ccb.credit.repay.faq", "信用卡", "还款FAQ"),
    ("还款多久到账", "ccb.credit.repay.faq", "信用卡", "还款时效"),
    ("还信用卡要手续费吗", "ccb.credit.repay.faq", "信用卡", "还款费用"),
    ("信用卡还款规则是什么", "ccb.home.repayment.faq", "首页", "还款规则FAQ"),
    ("跨行还信用卡行吗", "ccb.home.repayment.faq", "首页", "跨行还款FAQ"),
]

for q, vid, domain, reason in hard_singles:
    n = v2_to_node.get(vid)
    if not n:
        continue
    # 检查是否有已有的相同query
    if any(c["query"] == q for c in cases):
        continue
    diff = "hard" if len(q) <= 8 else "medium"
    add(q, diff, "single_intent", domain,
        [make_intent(n, "primary")],
        reason,
        [n["v4_id"]])

print(f"补充 hard single_intent 用例: {len(hard_singles)}")

# ── 9. 补充 ambiguous_intent 口语化用例 ──
extra_ambiguous = [
    ("查下还款", [("ccb.credit.repay.overview", "还款试算"), ("ccb.home.repayment.overview", "还款概览"), ("ccb.home.loan.repayment", "贷款还款")], "跨域", "domain_ambiguity"),
    ("怎么还款", [("ccb.credit.repay.faq", "还款FAQ"), ("ccb.credit.repay.repay", "一键还款"), ("ccb.home.repayment.faq", "还款FAQ")], "跨域", "action_ambiguity"),
    ("还多少", [("ccb.credit.repay.overview", "还款试算"), ("ccb.credit.bill.overview", "账单查询"), ("ccb.home.repayment.overview", "还款概览")], "跨域", "action_target_ambiguity"),
    ("查下欠款", [("ccb.credit.repay.overview", "还款试算"), ("ccb.credit.bill.overview", "账单查询"), ("ccb.home.repayment.overview", "还款概览")], "跨域", "domain_ambiguity"),
    ("查下信用卡", [("ccb.credit.card.query", "卡片管理"), ("ccb.credit.bill.overview", "账单查询"), ("ccb.credit.repay.overview", "还款试算")], "信用卡", "action_target_ambiguity"),
    ("信用卡状态", [("ccb.credit.card.query", "卡片管理"), ("ccb.credit.card.loss-report", "卡片挂失"), ("ccb.credit.card.activation", "卡片激活")], "信用卡", "action_target_ambiguity"),
    ("信用卡额度", [("ccb.credit.card.query", "卡片管理"), ("ccb.credit.card.limit-change", "额度调整"), ("ccb.credit.limit.overview", "额度管理")], "信用卡", "action_target_ambiguity"),
    ("查下消费", [("ccb.credit.card.transactions.query", "交易明细"), ("ccb.account.transactions.query", "账户明细"), ("ccb.cashflow.spending-explain", "支出分析")], "跨域", "domain_ambiguity"),
    ("查下账", [("ccb.credit.bill.overview", "账单查询"), ("ccb.account.transactions.query", "账户明细"), ("ccb.account.detail.query", "账户详情")], "跨域", "domain_ambiguity"),
    ("怎么转账", [("ccb.transfer.create", "发起转账"), ("ccb.home.transfer.faq", "转账FAQ"), ("ccb.transfer.payee.query", "收款人与进度")], "首页", "action_ambiguity"),
    ("信用卡办理", [("ccb.credit.apply.overview", "申请进度"), ("ccb.credit.card.query", "卡片管理")], "信用卡", "action_target_ambiguity"),
    ("查下扣款", [("ccb.credit.auto-repay.overview", "约定还款查询"), ("ccb.credit.repay.overview", "还款试算")], "信用卡", "action_target_ambiguity"),
]

for q, exp_list, domain, amb_type in extra_ambiguous:
    if any(c["query"] == q for c in cases):
        continue
    exp_intents = []
    src_nodes = []
    for vid, name in exp_list:
        n = v2_to_node.get(vid)
        if n:
            exp_intents.append(make_intent(n, "candidate"))
            src_nodes.append(n["v4_id"])
    if len(exp_intents) >= 2:
        add(q, "hard", "ambiguous_intent", domain, exp_intents,
            f"'{q}'歧义，可能匹配多种意图",
            src_nodes, amb_type, clarify=True, decision="clarify")

# ── 统计与输出 ──
diff_dist = Counter(c["difficulty"] for c in cases)
shape_dist = Counter(c["intent_shape"] for c in cases)

# 覆盖率统计
covered_v2_ids = set()
for c in cases:
    for e in c["expected_intents"]:
        covered_v2_ids.add(e["intent_id"])

confusable_v2_ids = set()
for n in nodes:
    if n["confusableIntents"]:
        confusable_v2_ids.add(n["v2_id"])

covered_confusable = covered_v2_ids & confusable_v2_ids

dataset = {
    "meta": {
        "dataset_name": "mobile_banking_intent_eval_v4_pilot",
        "version": "v2.0",
        "generated_at": "2026-09-30T00:00:00.000Z",
        "generator": "script-assisted-curation",
        "graph_path": str(GRAPH_PATH).replace("\\", "/"),
        "graph_version": "v4-fixed",
        "target_total": len(cases),
        "difficulty_distribution": dict(diff_dist),
        "intent_shape_distribution": dict(shape_dist),
        "confusable_coverage": f"{len(covered_confusable)}/{len(confusable_v2_ids)}",
        "notes": (
            f"V4图谱扩展测试集v2.0，覆盖{len(covered_confusable)}/{len(confusable_v2_ids)}个confusableIntents节点。"
            f"核心策略：用recallTerm匹配(score=90)但避开alias匹配，"
            f"使fixed通过confusable扩召(0.85*90=76≥60)召回兄弟意图，"
            f"buggy因confusableIntents清零(跨父级)或普通兄弟扩召过低(同父级58<60)无法召回。"
        ),
    },
    "cases": cases,
}

DATASET_PATH.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"\n{'=' * 60}")
print(f"生成完毕: {len(cases)} 条测试用例")
print(f"难度分布: {dict(diff_dist)}")
print(f"形状分布: {dict(shape_dist)}")
print(f"confusable覆盖: {len(covered_confusable)}/{len(confusable_v2_ids)}")
print(f"输出文件: {DATASET_PATH}")
print(f"{'=' * 60}")
