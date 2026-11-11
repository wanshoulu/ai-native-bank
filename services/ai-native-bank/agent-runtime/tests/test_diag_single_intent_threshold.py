"""诊断 89 个 single_intent case 的 top1/top2 score 分布
决定 single_intent 触发 LLM judge 的阈值"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import RecallSettings
from app.v4_graph_adapter import V4GraphAdapter

GRAPH_PATH = ROOT.parent.parent.parent / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DATASET_PATH = Path(__file__).resolve().parent / "intent_eval_v4_pilot.json"

def main():
    rs = RecallSettings.from_env()
    payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    adapter = V4GraphAdapter(payload, GRAPH_PATH, recall_settings=rs)
    adapter.apply_recall_settings(rs)

    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

    # 形态判定:复用 service.py 的 analyze_intent_shape
    from app.intent_shape import analyze_intent_shape

    single_cases = [c for c in dataset["cases"]
                    if analyze_intent_shape(c["query"]).shape == "single_intent"]
    print(f"single_intent case 数: {len(single_cases)}")

    # 统计 top1/top2 分布
    bins_top1 = {"<60":0, "60-79":0, "80-99":0, "100-119":0, ">=120":0}
    bins_top2 = {"<60":0, "60-79":0, "80-99":0, "100-119":0, ">=120":0, "NA":0}
    # 失败 case(top1 不在 expected)
    fails = []
    # 走 LLM 的候选数(按不同阈值)
    thresholds = [
        ("top1<80", lambda t1,t2: t1 < 80),
        ("top1<80 OR top2>=80", lambda t1,t2: t1 < 80 or (t2 is not None and t2 >= 80)),
        ("top1<80 OR top2>=60", lambda t1,t2: t1 < 80 or (t2 is not None and t2 >= 60)),
        ("top1<80 OR top2>=55", lambda t1,t2: t1 < 80 or (t2 is not None and t2 >= 55)),
        ("top1<100 OR top2>=60", lambda t1,t2: t1 < 100 or (t2 is not None and t2 >= 60)),
        ("always", lambda t1,t2: True),
    ]
    trigger_counts = {name: 0 for name, _ in thresholds}

    for case in single_cases:
        query = case["query"]
        expected = [e["intent_id"] for e in case.get("expected_intents", [])]
        cands = adapter.recall(query, page_context={}, limit=20, graph_expand=True)
        cands_sorted = sorted(cands, key=lambda c: -c.score)
        top1 = cands_sorted[0] if cands_sorted else None
        top2 = cands_sorted[1] if len(cands_sorted) > 1 else None
        t1_score = top1.score if top1 else 0
        t2_score = top2.score if top2 else None

        # 分桶
        def bucket(s):
            if s is None: return "NA"
            if s < 60: return "<60"
            if s < 80: return "60-79"
            if s < 100: return "80-99"
            if s < 120: return "100-119"
            return ">=120"
        bins_top1[bucket(t1_score)] += 1
        bins_top2[bucket(t2_score)] += 1

        # 失败 case
        if top1 and top1.intent_id not in expected:
            fails.append({
                "case_id": case["case_id"],
                "query": query,
                "top1": top1.intent_id,
                "top1_score": t1_score,
                "top2_score": t2_score,
                "expected": expected,
            })

        # 触发计数
        for name, fn in thresholds:
            if fn(t1_score, t2_score):
                trigger_counts[name] += 1

    print("\ntop1 score 分布:")
    for k, v in bins_top1.items():
        print(f"  {k}: {v}")
    print("\ntop2 score 分布:")
    for k, v in bins_top2.items():
        print(f"  {k}: {v}")

    print(f"\n失败 case 数(top1 不在 expected): {len(fails)}")
    for f in fails:
        print(f"  [{f['case_id']}] top1={f['top1']}({f['top1_score']}) top2_score={f['top2_score']} expected={f['expected']}")
        print(f"    query: {f['query']}")

    print("\n不同阈值下走 LLM 的 case 数:")
    for name, _ in thresholds:
        print(f"  {name}: {trigger_counts[name]}/{len(single_cases)}")

if __name__ == "__main__":
    main()
