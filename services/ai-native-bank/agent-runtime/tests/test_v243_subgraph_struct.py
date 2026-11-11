"""诊断 V4-243 真实子图结构:父/子/兄弟/confusable 路径"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import RecallSettings
from app.v4_graph_adapter import V4GraphAdapter

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"

rs = RecallSettings.from_env()
payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
adapter = V4GraphAdapter(payload, GRAPH_PATH, recall_settings=rs)
adapter.apply_recall_settings(rs)

# V4-243 涉及的关键意图
target_ids = [
    "ccb.credit.repay.faq",       # expected
    "ccb.home.repayment.faq",     # LLM 选错的
    "ccb.credit.repay.repay",     # top1
    "ccb.credit.repay.overview",
]

print("=" * 80)
print("V4-243 涉及意图的完整子图结构")
print("=" * 80)

for tid in target_ids:
    intent = adapter._intents_by_id.get(tid)
    if not intent:
        print(f"\n{tid}: NOT FOUND")
        continue

    print(f"\n{'='*70}")
    print(f"意图: {tid}")
    print(f"  title: {intent.get('title', '')}")
    print(f"  description: {intent.get('description', '')}")
    print(f"  path: {intent.get('path', '')}")
    print(f"  parent_intent_id: {intent.get('parent_intent_id', '')}")
    print(f"  node_role: {intent.get('node_role', '')}")
    print(f"  disambiguation_hints: {intent.get('disambiguationHints') or '(空)'}")
    print(f"  confusableIntents(声明): {intent.get('confusableIntents', [])}")

    # 父节点链路 (root → domain → category → leaf)
    print(f"\n  父节点链路 (向上遍历):")
    tree = payload["intentTree"]
    pid = intent.get("parent_intent_id", "")
    chain = []
    while pid:
        parent = next((n for n in tree if n["id"] == pid), None)
        if not parent:
            break
        chain.append(parent)
        pid = parent.get("parent_intent_id", "")
    for p in chain:
        print(f"    - [{p.get('node_role')}] {p['id']}: {p.get('title', '')}")
        if p.get("scope_in"):
            print(f"      scope_in: {p['scope_in']}")
        if p.get("navigationTerms"):
            print(f"      navigationTerms: {p['navigationTerms']}")
        if p.get("bindings", {}).get("menu"):
            print(f"      menu.path: {p['bindings']['menu'].get('path', '')}")

    # 子节点 (如果是 category/domain)
    children = [n for n in tree if n.get("parent_intent_id") == tid]
    if children:
        print(f"\n  子节点 ({len(children)}):")
        for ch in children:
            print(f"    - [{ch.get('node_role')}] {ch['id']}: {ch.get('title', '')}")

    # 兄弟 (同 parent 的 leaf)
    parent_id = intent.get("parent_intent_id", "")
    siblings_in_tree = [n for n in tree if n.get("parent_intent_id") == parent_id and n["id"] != tid]
    if siblings_in_tree:
        print(f"\n  同 parent 兄弟 (树结构, {len(siblings_in_tree)}):")
        for s in siblings_in_tree:
            print(f"    - {s['id']}: {s.get('title', '')}")

    # adapter 索引的兄弟
    adapter_siblings = adapter._siblings.get(tid, set())
    if adapter_siblings:
        print(f"\n  adapter._siblings ({len(adapter_siblings)}):")
        for sid in sorted(adapter_siblings):
            s_int = adapter._intents_by_id.get(sid)
            if s_int:
                print(f"    - {sid}: {s_int.get('title', '')}")

    # confusable 邻居 (正向 + 反向)
    fwd = adapter._forward_confusable.get(tid, set())
    conf_sib = adapter._confusable_siblings.get(tid, set())
    print(f"\n  confusable 关系:")
    print(f"    forward (自身声明): {sorted(fwd)}")
    print(f"    双向 (含反向): {sorted(conf_sib)}")
    for cid in sorted(conf_sib):
        c_int = adapter._intents_by_id.get(cid)
        if c_int:
            print(f"      - {cid}: {c_int.get('title', '')}")
            print(f"        desc: {c_int.get('description', '')}")
            print(f"        hints: {c_int.get('disambiguationHints') or '(空)'}")
