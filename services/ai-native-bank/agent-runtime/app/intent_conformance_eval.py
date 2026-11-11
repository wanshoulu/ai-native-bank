from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .intent_graph import IntentGraphRepository


class IntentConformanceEvaluationError(ValueError):
    """Raised when an intent evaluation artifact is stale or outside its contract."""


_PAGE_CONTEXT_KEYS = frozenset({"tab", "menuKey"})
_MAX_CASES = 1_000
_MAX_UTTERANCES_PER_CASE = 10
_MAX_UTTERANCE_LENGTH = 512
_MAX_FAILURE_DETAILS = 100


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntentConformanceEvaluationError(f"{label} cannot be loaded") from exc
    if not isinstance(payload, dict):
        raise IntentConformanceEvaluationError(f"{label} must be an object")
    return payload


def _bounded_text(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise IntentConformanceEvaluationError(f"{label} is invalid")
    return value.strip()


def _graph_intents(graph_payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    nodes = graph_payload.get("nodes")
    if not isinstance(nodes, list):
        raise IntentConformanceEvaluationError("graph nodes are invalid")
    return {
        node["id"]: node
        for node in nodes
        if isinstance(node, Mapping)
        and node.get("type") == "intent"
        and isinstance(node.get("id"), str)
    }


def _validate_dataset(
    dataset: Mapping[str, Any],
    *,
    repository: IntentGraphRepository,
    graph_intents: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    graph_binding = dataset.get("graph")
    if not isinstance(graph_binding, Mapping):
        raise IntentConformanceEvaluationError("dataset graph binding is invalid")
    if graph_binding.get("graphId") != repository.graph_id:
        raise IntentConformanceEvaluationError("dataset graph id is stale")
    if graph_binding.get("version") != repository.version:
        raise IntentConformanceEvaluationError("dataset graph version is stale")
    if graph_binding.get("digest") != repository.digest:
        raise IntentConformanceEvaluationError("dataset graph digest is stale")

    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > _MAX_CASES:
        raise IntentConformanceEvaluationError("dataset cases are invalid")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, Mapping):
            raise IntentConformanceEvaluationError("dataset case is invalid")
        intent_id = _bounded_text(case.get("intentId"), label="intent id", maximum=128)
        if intent_id in seen or intent_id not in graph_intents:
            raise IntentConformanceEvaluationError("dataset intent coverage is invalid")
        seen.add(intent_id)
        context = case.get("pageContext")
        if not isinstance(context, Mapping) or set(context) != _PAGE_CONTEXT_KEYS:
            raise IntentConformanceEvaluationError("dataset page context is invalid")
        tab = _bounded_text(context.get("tab"), label="page context tab", maximum=64)
        menu_key = _bounded_text(
            context.get("menuKey"), label="page context menu key", maximum=128
        )
        intent = graph_intents[intent_id]
        if tab != intent.get("domain") or menu_key != intent.get("menuKey"):
            raise IntentConformanceEvaluationError("dataset page context does not match graph")
        utterances = case.get("positiveUtterances")
        if (
            not isinstance(utterances, list)
            or not utterances
            or len(utterances) > _MAX_UTTERANCES_PER_CASE
        ):
            raise IntentConformanceEvaluationError("dataset utterances are invalid")
        for utterance in utterances:
            _bounded_text(
                utterance,
                label="dataset utterance",
                maximum=_MAX_UTTERANCE_LENGTH,
            )
        expected = case.get("expected")
        if not isinstance(expected, Mapping):
            raise IntentConformanceEvaluationError("dataset expectation is invalid")
        for field in (
            "semanticVersion",
            "lifecycle",
            "readiness",
            "executionMode",
            "responsePolicy",
        ):
            if expected.get(field) != intent.get(field):
                raise IntentConformanceEvaluationError(
                    f"dataset expectation does not match graph: {field}"
                )
    if seen != set(graph_intents):
        raise IntentConformanceEvaluationError("dataset intent coverage is incomplete")
    return cases


def _report_digest(report: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def evaluate_intent_conformance(
    *,
    graph_path: str | Path,
    evaluated_at: str,
    dataset_path: str | Path | None = None,
    dataset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the published candidate/suppression contract without using an LLM."""

    if (dataset_path is None) == (dataset is None):
        raise IntentConformanceEvaluationError(
            "provide exactly one of dataset_path or dataset"
        )
    timestamp = _bounded_text(evaluated_at, label="evaluated at", maximum=64)
    graph_file = Path(graph_path)
    graph_payload = _read_json(graph_file, label="intent graph")
    repository = IntentGraphRepository.load(graph_file)
    loaded_dataset = (
        _read_json(Path(dataset_path), label="intent evaluation dataset")
        if dataset_path is not None
        else dict(dataset or {})
    )
    intents = _graph_intents(graph_payload)
    cases = _validate_dataset(
        loaded_dataset,
        repository=repository,
        graph_intents=intents,
    )

    case_results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    active_cases = 0
    planned_cases = 0
    active_utterances = 0
    planned_utterances = 0
    active_candidate_hits = 0
    planned_suppressions = 0
    top1_exact = 0

    for case in cases:
        intent_id = str(case["intentId"])
        lifecycle = str(case["expected"]["lifecycle"])
        is_active = lifecycle == "active"
        utterances = list(case["positiveUtterances"])
        passed = 0
        case_top1 = 0
        if is_active:
            active_cases += 1
            active_utterances += len(utterances)
        else:
            planned_cases += 1
            planned_utterances += len(utterances)
        for utterance in utterances:
            candidates = repository.recall(
                utterance,
                page_context=dict(case["pageContext"]),
            )
            candidate_ids = [candidate.intent_id for candidate in candidates]
            if candidate_ids and candidate_ids[0] == intent_id:
                top1_exact += 1
                case_top1 += 1
            contract_passed = intent_id in candidate_ids if is_active else intent_id not in candidate_ids
            if is_active and intent_id in candidate_ids:
                active_candidate_hits += 1
            if not is_active and intent_id not in candidate_ids:
                planned_suppressions += 1
            if contract_passed:
                passed += 1
            elif len(failures) < _MAX_FAILURE_DETAILS:
                failures.append(
                    {
                        "caseId": case["caseId"],
                        "intentId": intent_id,
                        "utterance": utterance,
                        "candidateIntentIds": candidate_ids,
                        "expected": "candidate" if is_active else "suppressed",
                    }
                )
        case_results.append(
            {
                "caseId": case["caseId"],
                "intentId": intent_id,
                "lifecycle": lifecycle,
                "utterances": len(utterances),
                "passedUtterances": passed,
                "top1ExactUtterances": case_top1,
                "status": "passed" if passed == len(utterances) else "failed",
            }
        )

    utterance_count = active_utterances + planned_utterances
    passed_count = active_candidate_hits + planned_suppressions
    unsigned_report: dict[str, Any] = {
        "schemaVersion": "1.0",
        "reportType": "intent-graph-contract-execution",
        "datasetId": loaded_dataset.get("datasetId"),
        "datasetDigest": loaded_dataset.get("digest"),
        "evaluatedAt": timestamp,
        "graph": {
            "graphId": repository.graph_id,
            "version": repository.version,
            "digest": repository.digest,
        },
        "engine": {
            "component": "IntentGraphRepository.recall",
            "mode": "deterministic-candidate-constraint",
            "llmUsed": False,
        },
        "summary": {
            "cases": len(cases),
            "activeCases": active_cases,
            "plannedCases": planned_cases,
            "utterances": utterance_count,
            "passedUtterances": passed_count,
            "failedUtterances": utterance_count - passed_count,
            "contractPassRate": passed_count / utterance_count,
            "activeCandidateRecallRate": active_candidate_hits / active_utterances,
            "plannedSuppressionRate": planned_suppressions / planned_utterances,
            "top1ExactRate": top1_exact / active_utterances,
        },
        "caseResults": case_results,
        "failures": failures,
        "failureDetailsTruncated": utterance_count - passed_count > len(failures),
    }
    return {**unsigned_report, "digest": _report_digest(unsigned_report)}
