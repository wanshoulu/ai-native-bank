"""Mock 300-case intent recall evaluation (deterministic, no LLM).

生成 300 个 mock 用例覆盖意图图谱全部意图并评测词法召回正确率：
  - 238 个 active 意图各 1 条主用例（模板化改写，模拟真实说法）
  - 40 个易混/困难意图追加第 2 条改写用例
  - 10 条高频意图手工口语用例
  - 12 条 planned 意图抑制用例（期望不召回）
评测口径与生产 MOA 链路一致：page_context={}，仅词法通道（弱召回时生产环境由
LLM 语义扩召兜底，本评测不含 LLM）。

用法：
  .venv/Scripts/python.exe eval/run_mock300_eval.py
输出：
  eval/mock300-cases.json   300 条用例（可复现）
  eval/mock300-report.json  评测报告（含汇总、分域、失败明细）
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
AGENT_RUNTIME = EVAL_DIR.parent
sys.path.insert(0, str(AGENT_RUNTIME))

from app.intent_graph import IntentGraphRepository  # noqa: E402

DEFAULT_ROOT = AGENT_RUNTIME.parents[2]
GRAPH_PATH = Path(
    os.environ.get("AI_BANK_INTENT_GRAPH_PATH", "")
    or (DEFAULT_ROOT / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v1.0.0.json")
)
CASES_PATH = EVAL_DIR / "mock300-cases.json"
REPORT_PATH = EVAL_DIR / "mock300-report.json"

HARD_CASE_COUNT = 40
TERMSET_CASE_COUNT = 10
PLANNED_CASE_COUNT = 12
COLOQUIAL_CASES = {
    "ccb.transfer.create": "给王芳转五百块钱",
    "ccb.account.balance.query": "帮我看下卡里的余额还有多少",
    "ccb.account.transactions.query": "帮我看看最近账户流水",
    "ccb.account.detail.query": "我想看下账户信息",
    "ccb.credit.card.loss-report": "我的信用卡丢了，赶紧挂失",
    "ccb.credit.bill.repay": "我要还清账单，别逾期",
    "ccb.credit.bill.overview": "查一下我的信用卡账单",
    "ccb.wealth.deposit.buy": "我想存个定期",
    "ccb.wealth.dragon-money.buy": "帮我申购龙钱宝",
    "ccb.cashflow.spending-explain": "这个月钱花哪了",
}

TEMPLATES = {
    "read-execute": [
        "帮我查一下{t}", "我想查{t}", "{t}怎么查", "请帮我查{t}",
        "看一下我的{t}", "查查{t}",
    ],
    "navigation-only": [
        "打开{t}", "{t}在哪里", "怎么进入{t}", "带我去{t}",
        "我要用{t}", "进入{t}",
    ],
    "knowledge-only": [
        "{t}是什么", "我想了解{t}", "{t}怎么办", "介绍一下{t}",
        "{t}有哪些", "怎么办理{t}",
    ],
    "confirm-required": [
        "我要{t}", "帮我{t}", "我想办理{t}", "麻烦帮我{t}",
        "我想{t}", "办一下{t}",
    ],
}

_HAS_DIGIT = re.compile(r"\d")


def _normalize(value: object) -> str:
    return re.sub(r"[\s，,。；;、！？!?]+", "", str(value or "").lower())


def _intent_terms(node: dict) -> list[str]:
    pool: list[str] = []
    for alias in node.get("aliases", []):
        text = _normalize(alias)
        if len(text) >= 2 and not _HAS_DIGIT.search(text):
            pool.append(text)
    for term in node.get("recallTerms", []):
        text = _normalize(term)
        if len(text) >= 2:
            pool.append(text)
    return list(dict.fromkeys(pool))


def _pick_term(
    terms: list[str], collisions: dict[str, int], skip: str = ""
) -> tuple[str, bool]:
    """返回 (term, strict)。按碰撞意图数升序、词长升序选最具区分度的词。"""
    ranked = sorted(terms, key=lambda term: (collisions.get(term, 0), len(term)))
    for term in ranked:
        if term != skip and collisions.get(term, 0) == 0:
            return term, True
    for term in ranked:
        if term != skip:
            return term, False
    raise RuntimeError("intent has no usable term")


def _collision_map(intents: list[dict]) -> tuple[dict[str, dict[str, int]], dict[str, list[str]]]:
    """term -> 该词会同时给多少个其他意图加分的碰撞计数；及每个意图的词表。"""
    term_owners: dict[str, set[str]] = defaultdict(set)
    terms_of: dict[str, list[str]] = {}
    for node in intents:
        terms = _intent_terms(node)
        terms_of[node["id"]] = terms
        for term in terms:
            term_owners[term].add(node["id"])
    collisions: dict[str, dict[str, int]] = {}
    for node in intents:
        own = set(terms_of[node["id"]])
        counter: dict[str, int] = {}
        for term in own:
            others: set[str] = set()
            for other_term, owners in term_owners.items():
                if other_term == term:
                    others |= owners - {node["id"]}
                elif other_term in term or term in other_term:
                    others |= owners
            counter[term] = len(others)
        collisions[node["id"]] = counter
    return collisions, terms_of


def _termset_utterance(node: dict) -> tuple[str, bool]:
    """无唯一词时用 recallTermSets 组合词激活 graph_term_set + semanticPriority 消歧。"""
    for term_set in node.get("recallTermSets", []):
        normalized = [_normalize(term) for term in term_set]
        if len(normalized) >= 2 and all(len(term) >= 2 for term in normalized):
            return "和".join(normalized), True
    return "", False


def build_cases(intents: list[dict], planned: list[dict]) -> list[dict]:
    collisions, terms_of = _collision_map(intents)
    cases: list[dict] = []
    mode_counter: Counter = Counter()
    for node in sorted(intents, key=lambda item: item["id"]):
        terms = terms_of[node["id"]]
        term, strict = _pick_term(terms, collisions[node["id"]])
        mode = node["executionMode"]
        index = mode_counter[mode]
        mode_counter[mode] += 1
        templates = TEMPLATES[mode]
        utterance = templates[index % len(templates)].format(t=term)
        cases.append(
            {
                "caseId": f"mock300-{len(cases) + 1:03d}",
                "kind": "primary",
                "intentId": node["id"],
                "lifecycle": "active",
                "strict": strict,
                "term": term,
                "utterance": utterance,
            }
        )

    # 无唯一词的意图：用 recallTermSets 组合词补 term_set 消歧用例（先统计供 hard 预算用）
    termset_count = 0
    termset_nodes: list[dict] = []
    for node in sorted(intents, key=lambda item: item["id"]):
        if termset_count >= TERMSET_CASE_COUNT:
            break
        if _pick_term(terms_of[node["id"]], collisions[node["id"]])[1]:
            continue
        combined, ok = _termset_utterance(node)
        if not ok:
            continue
        termset_count += 1
        termset_nodes.append(node)

    confusable_first = sorted(
        intents,
        key=lambda item: (not item.get("confusableIntents"), item["id"]),
    )
    hard_taken = 0
    hard_budget = HARD_CASE_COUNT - termset_count
    for node in confusable_first:
        if hard_taken >= hard_budget:
            break
        terms = terms_of[node["id"]]
        if len(terms) < 2:
            continue
        first_term, _ = _pick_term(terms, collisions[node["id"]])
        second_term, strict = _pick_term(terms, collisions[node["id"]], skip=first_term)
        mode = node["executionMode"]
        index = mode_counter[mode] + 1
        mode_counter[mode] += 1
        templates = TEMPLATES[mode]
        utterance = templates[index % len(templates)].format(t=second_term)
        hard_taken += 1
        cases.append(
            {
                "caseId": f"mock300-{len(cases) + 1:03d}",
                "kind": "hard",
                "intentId": node["id"],
                "lifecycle": "active",
                "strict": strict,
                "term": second_term,
                "utterance": utterance,
            }
        )

    for node in termset_nodes:
        combined, _ = _termset_utterance(node)
        cases.append(
            {
                "caseId": f"mock300-{len(cases) + 1:03d}",
                "kind": "termset",
                "intentId": node["id"],
                "lifecycle": "active",
                "strict": True,
                "term": combined,
                "utterance": combined,
            }
        )

    for intent_id, utterance in COLOQUIAL_CASES.items():
        cases.append(
            {
                "caseId": f"mock300-{len(cases) + 1:03d}",
                "kind": "colloquial",
                "intentId": intent_id,
                "lifecycle": "active",
                "strict": True,
                "term": "",
                "utterance": utterance,
            }
        )

    step = max(len(planned) // PLANNED_CASE_COUNT, 1)
    for node in sorted(planned, key=lambda item: item["id"])[::step][:PLANNED_CASE_COUNT]:
        term, _ = _pick_term(_intent_terms(node), {})
        cases.append(
            {
                "caseId": f"mock300-{len(cases) + 1:03d}",
                "kind": "planned-suppression",
                "intentId": node["id"],
                "lifecycle": node.get("lifecycle", "planned"),
                "strict": True,
                "term": term,
                "utterance": f"我要{term}",
            }
        )
    return cases


def evaluate(cases: list[dict], repository: IntentGraphRepository) -> dict:
    results: list[dict] = []
    failures: list[dict] = []
    active = [case for case in cases if case["lifecycle"] == "active"]
    planned = [case for case in cases if case["lifecycle"] != "active"]

    active_top1 = active_top3 = active_recall = active_zero = 0
    strict_total = strict_top1 = 0
    planned_suppressed = 0

    domain_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"cases": 0, "top1": 0, "recall": 0, "zero": 0}
    )
    mode_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"cases": 0, "top1": 0, "recall": 0}
    )

    for case in cases:
        candidates = repository.recall(case["utterance"], page_context={})
        candidate_ids = [candidate.intent_id for candidate in candidates]
        intent_id = case["intentId"]
        hit_top1 = bool(candidate_ids) and candidate_ids[0] == intent_id
        hit_top3 = intent_id in candidate_ids[:3]
        hit_recall = intent_id in candidate_ids
        zero = not candidate_ids
        record = {
            **case,
            "top1": hit_top1,
            "top3": hit_top3,
            "recalled": hit_recall,
            "zeroCandidates": zero,
            "candidateIntentIds": candidate_ids,
        }
        results.append(record)

        if case["lifecycle"] == "active":
            active_top1 += hit_top1
            active_top3 += hit_top3
            active_recall += hit_recall
            active_zero += zero
            if case["strict"]:
                strict_total += 1
                strict_top1 += hit_top1
            node_domain = intent_id.split(".")[1]
            domain_stats[node_domain]["cases"] += 1
            domain_stats[node_domain]["top1"] += hit_top1
            domain_stats[node_domain]["recall"] += hit_recall
            domain_stats[node_domain]["zero"] += zero
            mode = next(
                node["executionMode"]
                for node in ACTIVE_NODES
                if node["id"] == intent_id
            )
            mode_stats[mode]["cases"] += 1
            mode_stats[mode]["top1"] += hit_top1
            mode_stats[mode]["recall"] += hit_recall
            if not hit_top1 and len(failures) < 120:
                failures.append(record)
        else:
            planned_suppressed += intent_id not in candidate_ids

    def rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    return {
        "schemaVersion": "1.0",
        "reportType": "mock300-intent-recall-accuracy",
        "graph": {
            "graphId": repository.graph_id,
            "version": repository.version,
            "digest": repository.digest,
        },
        "engine": {
            "component": "IntentGraphRepository.recall",
            "pageContext": {},
            "llmUsed": False,
            "note": "与生产 MOA 链路一致；零候选/低分用例在生产由 LLM 语义扩召兜底",
        },
        "summary": {
            "cases": len(cases),
            "activeCases": len(active),
            "plannedCases": len(planned),
            "top1Accuracy": rate(active_top1, len(active)),
            "top3HitRate": rate(active_top3, len(active)),
            "candidateRecallRate": rate(active_recall, len(active)),
            "zeroCandidateRate": rate(active_zero, len(active)),
            "strictTop1Accuracy": rate(strict_top1, strict_total),
            "strictCaseCount": strict_total,
            "plannedSuppressionRate": rate(planned_suppressed, len(planned)),
            "activeTop1Hits": active_top1,
            "activeRecallHits": active_recall,
            "activeZeroCandidates": active_zero,
        },
        "byDomain": {
            domain: {
                **stats,
                "top1Accuracy": rate(stats["top1"], stats["cases"]),
                "candidateRecallRate": rate(stats["recall"], stats["cases"]),
            }
            for domain, stats in sorted(domain_stats.items())
        },
        "byExecutionMode": {
            mode: {
                **stats,
                "top1Accuracy": rate(stats["top1"], stats["cases"]),
                "candidateRecallRate": rate(stats["recall"], stats["cases"]),
            }
            for mode, stats in sorted(mode_stats.items())
        },
        "failures": failures,
        "results": results,
    }


def main() -> None:
    payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    nodes = payload["nodes"]
    intents = [
        node for node in nodes if node["type"] == "intent" and node.get("lifecycle", "active") == "active"
    ]
    planned = [
        node for node in nodes if node["type"] == "intent" and node.get("lifecycle") != "active"
    ]
    global ACTIVE_NODES
    ACTIVE_NODES = intents
    repository = IntentGraphRepository.load(GRAPH_PATH)

    cases = build_cases(intents, planned)
    assert len(cases) == 300, f"expected 300 cases, got {len(cases)}"
    covered = {case["intentId"] for case in cases if case["lifecycle"] == "active"}
    missing = {node["id"] for node in intents} - covered
    assert not missing, f"active intents not covered: {sorted(missing)}"

    CASES_PATH.write_text(
        json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    report = evaluate(cases, repository)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    summary = report["summary"]
    print("=== Mock300 意图召回评测 ===")
    print(f"graph: {repository.graph_id} v{repository.version}")
    print(f"cases: {summary['cases']} (active {summary['activeCases']} / planned {summary['plannedCases']})")
    print(f"top1 正确率        : {summary['top1Accuracy']:.2%} ({summary['activeTop1Hits']}/{summary['activeCases']})")
    print(f"top3 命中率        : {summary['top3HitRate']:.2%}")
    print(f"候选召回率(top-8)  : {summary['candidateRecallRate']:.2%} ({summary['activeRecallHits']}/{summary['activeCases']})")
    print(f"零候选率(需LLM兜底): {summary['zeroCandidateRate']:.2%} ({summary['activeZeroCandidates']})")
    print(f"唯一词用例 top1    : {summary['strictTop1Accuracy']:.2%} (n={summary['strictCaseCount']})")
    print(f"planned 抑制率     : {summary['plannedSuppressionRate']:.2%}")
    print("--- 按域 ---")
    for domain, stats in report["byDomain"].items():
        print(
            f"{domain:<10} cases={stats['cases']:<4} top1={stats['top1Accuracy']:.2%} "
            f"recall={stats['candidateRecallRate']:.2%} zero={stats['zero']}"
        )
    print("--- 按执行模式 ---")
    for mode, stats in report["byExecutionMode"].items():
        print(
            f"{mode:<18} cases={stats['cases']:<4} top1={stats['top1Accuracy']:.2%} "
            f"recall={stats['candidateRecallRate']:.2%}"
        )
    print(f"--- 失败用例（top1 未命中）共 {len(report['failures'])} 条（明细见报告） ---")
    for failure in report["failures"][:30]:
        print(
            f"[{failure['kind']}] {failure['intentId']} <- '{failure['utterance']}' "
            f"=> {failure['candidateIntentIds'][:3]}"
        )


ACTIVE_NODES: list[dict] = []

if __name__ == "__main__":
    main()
