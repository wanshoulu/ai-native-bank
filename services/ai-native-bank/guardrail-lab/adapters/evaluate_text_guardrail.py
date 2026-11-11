"""Run synthetic text-guardrail cases without printing prompts or model answers."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


def normalize_disposition(response: dict[str, Any]) -> str:
    if "decision" in response:
        decision = response.get("decision")
        if decision in {"allow", "block", "review", "unavailable"}:
            return str(decision)
        return "error"
    if response.get("is_blocked") is True:
        return "block"
    if response.get("is_blocked") is False:
        return "allow"
    return "error"


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(fraction * len(ordered)))
    return round(ordered[index], 1)


def summarize_results(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(results)
    dispositions: dict[str, set[str]] = defaultdict(set)
    decision_counts: Counter[str] = Counter()
    category_decisions: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        actual = str(row["actualDisposition"])
        dispositions[str(row["id"])].add(actual)
        decision_counts[actual] += 1
        category_decisions[str(row.get("category", "uncategorized"))][actual] += 1
    latencies = [float(row["elapsedMs"]) for row in rows]
    return {
        "runs": len(rows),
        "cases": len(dispositions),
        "exactMatches": sum(
            row["expectedDisposition"] == row["actualDisposition"] for row in rows
        ),
        "falseAllows": sum(
            row["expectedDisposition"] in {"block", "review"}
            and row["actualDisposition"] == "allow"
            for row in rows
        ),
        "falseBlocks": sum(
            row["expectedDisposition"] == "allow"
            and row["actualDisposition"] in {"block", "review"}
            for row in rows
        ),
        "reviewMatches": sum(
            row["expectedDisposition"] == "review"
            and row["actualDisposition"] == "review"
            for row in rows
        ),
        "reviewUnsupported": sum(
            row["expectedDisposition"] == "review" and row["actualDisposition"] != "review"
            for row in rows
        ),
        "unavailableRuns": decision_counts["unavailable"],
        "errors": sum(row["actualDisposition"] == "error" for row in rows),
        "unstableCases": sum(len(values) > 1 for values in dispositions.values()),
        "decisionCounts": dict(sorted(decision_counts.items())),
        "categoryDecisionCounts": {
            category: dict(sorted(counts.items()))
            for category, counts in sorted(category_decisions.items())
        },
        "latencyMs": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
    }


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    cases = []
    case_ids: set[str] = set()
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        required = {"id", "category", "text", "expectedDisposition", "notes"}
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError(f"Invalid case schema at line {line_number}")
        if any(not isinstance(value[key], str) or not value[key].strip() for key in required):
            raise ValueError(f"Invalid empty case field at line {line_number}")
        if value["expectedDisposition"] not in {"allow", "block", "review"}:
            raise ValueError(f"Invalid expected disposition at line {line_number}")
        if len(value["text"]) > 4096:
            raise ValueError(f"Case text exceeds limit at line {line_number}")
        if value["id"] in case_ids:
            raise ValueError(f"Duplicate case id at line {line_number}")
        case_ids.add(value["id"])
        cases.append(value)
    return cases


def select_cases(
    cases: list[dict[str, Any]], *, offset: int = 0, limit: int | None = None
) -> list[dict[str, Any]]:
    if offset < 0:
        raise ValueError("Case offset must be non-negative")
    if limit is not None and limit <= 0:
        raise ValueError("Case limit must be positive")
    selected = cases[offset:] if limit is None else cases[offset : offset + limit]
    if not selected:
        raise ValueError("Selected case batch is empty")
    return selected


def run_case(url: str, case: dict[str, Any], round_number: int, timeout: float) -> dict[str, Any]:
    request_body = json.dumps(
        {
            "message": case["text"],
            "conversation_id": f"feasibility-{case['id']}-round-{round_number}",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    started = time.perf_counter()
    response: dict[str, Any] = {}
    status = 0
    error_code = None
    try:
        request = urllib.request.Request(
            url,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as upstream:
            status = upstream.status
            response = json.load(upstream)
    except urllib.error.HTTPError as error:
        status = error.code
        error_code = "http_error"
    except (OSError, ValueError, json.JSONDecodeError):
        error_code = "request_failed"
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    actual = normalize_disposition(response)
    return {
        "id": case["id"],
        "category": case["category"],
        "round": round_number,
        "expectedDisposition": case["expectedDisposition"],
        "actualDisposition": actual,
        "matchedExpected": actual == case["expectedDisposition"],
        "httpStatus": status,
        "detectedBy": response.get("detected_by"),
        "violatedRuleCount": len(response.get("violated_rules") or []),
        "responseChars": len(response.get("response") or ""),
        "elapsedMs": elapsed_ms,
        "errorCode": error_code,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--rounds", type=int, default=2, choices=range(1, 4))
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    arguments = parser.parse_args()

    cases = select_cases(
        load_cases(arguments.cases),
        offset=arguments.offset,
        limit=arguments.limit,
    )
    results = []
    for round_number in range(1, arguments.rounds + 1):
        for case in cases:
            result = run_case(arguments.url, case, round_number, arguments.timeout)
            results.append(result)
            print(json.dumps({"type": "result", **result}, ensure_ascii=False), flush=True)
    print(json.dumps({"type": "summary", **summarize_results(results)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
