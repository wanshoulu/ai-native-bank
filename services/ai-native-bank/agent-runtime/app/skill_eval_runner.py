"""Digest-bound, machine-verifiable contracts for AI Native Bank Skill evals."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9._-]{1,127}$")
_ALLOWED_OBSERVATION_FIELDS = frozenset({
    "status", "intentIds", "capabilityIds", "taskCount",
    "requiresConfirmation", "userResponseType",
})
_ALLOWED_STATUSES = frozenset({
    "succeeded", "clarification_required", "awaiting_confirmation",
    "navigation_only", "unsupported", "blocked", "unavailable",
})
_SAFE_RUN_FIELDS = frozenset({
    "kind", "id", "conversationId", "delegationId", "status", "intentShape",
    "tasks", "steps", "predictedNextIntents", "requiresConfirmation",
    "errorCode", "failedStage",
})
_SAFE_TASK_FIELDS = frozenset({
    "id", "intentId", "action", "businessObject", "readiness", "risk",
    "dependencies", "slotNames",
})
_SAFE_STEP_FIELDS = frozenset({
    "id", "taskId", "capabilityId", "inputKeys", "requiresConfirmation",
    "dependencies",
})


class SkillEvalContractError(ValueError):
    """Raised when eval contracts or safe Runtime observations are invalid."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillEvalContractError(f"invalid JSON asset: {path.name}") from error
    if not isinstance(value, dict):
        raise SkillEvalContractError(f"JSON asset must be an object: {path.name}")
    return value


def _require_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SkillEvalContractError(f"{label} is invalid")
    return value


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise SkillEvalContractError(f"{label} is invalid")
    return value


def _declared_skill_evals(root: Path) -> tuple[dict[tuple[str, str], dict[str, Any]], int]:
    values: dict[tuple[str, str], dict[str, Any]] = {}
    total = 0
    for path in sorted((root / "data" / "ai-native-bank" / "skills").glob("*/evals/evals.json")):
        payload = _read_json(path)
        evals = payload.get("evals")
        if not isinstance(evals, list) or not evals:
            raise SkillEvalContractError(f"Skill evals are missing: {path.parent.parent.name}")
        skill_id = payload.get("skillId")
        if not isinstance(skill_id, str):
            skill_id = {
                "cashflow-planning": "bank.cashflow-planning",
                "spending-analysis": "bank.spending-analysis",
                "obligation-management": "bank.obligation-management",
                "wealth-management": "bank.wealth-management",
            }.get(path.parent.parent.name)
        _require_identifier(skill_id, "skillId")
        for item in evals:
            if not isinstance(item, dict):
                raise SkillEvalContractError("Skill eval case must be an object")
            eval_id = _require_identifier(item.get("id"), "evalId")
            key = (skill_id, eval_id)
            if key in values:
                raise SkillEvalContractError("duplicate Skill eval identity")
            if not isinstance(item.get("prompt"), str) or not item["prompt"].strip():
                raise SkillEvalContractError("Skill eval prompt is missing")
            values[key] = item
            total += 1
    return values, total


def validate_skill_eval_contracts(
    payload: Mapping[str, Any], *, graph: Mapping[str, Any],
    skill_registry: Mapping[str, Any],
    declared_evals: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    if payload.get("schemaVersion") != "1.0" or payload.get("scope") != "high-frequency-first":
        raise SkillEvalContractError("Skill eval dataset identity is invalid")
    graph_ref = payload.get("graph")
    if not isinstance(graph_ref, dict):
        raise SkillEvalContractError("graph reference is missing")
    if graph_ref.get("id") != graph.get("graphId") or graph_ref.get("version") != graph.get("version"):
        raise SkillEvalContractError("graph identity mismatch")
    if graph_ref.get("digest") != graph.get("digest"):
        raise SkillEvalContractError("graph digest mismatch")
    _require_digest(graph_ref.get("digest"), "graph digest")

    authoritative = {item["id"]: item for item in skill_registry.get("skills", [])}
    declared_contracts = payload.get("skills")
    if not isinstance(declared_contracts, list) or set(authoritative) != {
        item.get("id") for item in declared_contracts if isinstance(item, dict)
    }:
        raise SkillEvalContractError("Skill digest coverage mismatch")
    for item in declared_contracts:
        skill_id = _require_identifier(item.get("id"), "skillId")
        actual = authoritative[skill_id]
        if item.get("version") != actual.get("version"):
            raise SkillEvalContractError(f"Skill version mismatch: {skill_id}")
        if item.get("digest") != actual.get("digest"):
            raise SkillEvalContractError(f"Skill digest mismatch: {skill_id}")
        _require_digest(item.get("digest"), "Skill digest")
        if payload.get("runtimeVersion") not in actual.get("compatibleRuntimeVersions", []):
            raise SkillEvalContractError(f"Runtime version is incompatible: {skill_id}")

    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != len(authoritative):
        raise SkillEvalContractError("high-frequency suite must contain one case per Skill")
    seen: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            raise SkillEvalContractError("runtime eval case must be an object")
        skill_id = _require_identifier(case.get("skillId"), "skillId")
        eval_id = _require_identifier(case.get("evalId"), "evalId")
        identity = (skill_id, eval_id)
        if identity in seen or identity not in declared_evals:
            raise SkillEvalContractError("runtime eval identity is duplicate or unknown")
        seen.add(identity)
        declared = declared_evals[identity]
        if declared.get("category") != "single_task":
            raise SkillEvalContractError("high-frequency runtime case must be single_task")
        execution = case.get("execution")
        if not isinstance(execution, dict) or execution.get("fixture") != "fresh_session" \
                or execution.get("transport") != "sse" \
                or not isinstance(execution.get("repetitions"), int) \
                or not 1 <= execution["repetitions"] <= 5:
            raise SkillEvalContractError("runtime execution contract is invalid")
        expect = case.get("expect")
        if not isinstance(expect, dict):
            raise SkillEvalContractError("runtime expectation is missing")
        intent_ids = expect.get("intentIds")
        capability_ids = expect.get("requiredCapabilityIds")
        statuses = expect.get("allowedStatuses")
        if not isinstance(intent_ids, list) or not intent_ids or not all(
            isinstance(item, str) and _IDENTIFIER.fullmatch(item) for item in intent_ids
        ):
            raise SkillEvalContractError("expected intent IDs are invalid")
        if not isinstance(capability_ids, list) or not all(
            isinstance(item, str) and _IDENTIFIER.fullmatch(item) for item in capability_ids
        ):
            raise SkillEvalContractError("expected capability IDs are invalid")
        if not isinstance(statuses, list) or not statuses or not set(statuses) <= _ALLOWED_STATUSES:
            raise SkillEvalContractError("allowed statuses are invalid")
        actual_skill = authoritative[skill_id]
        if not set(intent_ids) <= set(actual_skill.get("handledIntentIds", [])):
            raise SkillEvalContractError(f"eval intent is outside Skill ownership: {skill_id}")
        if not set(capability_ids) <= set(actual_skill.get("requiredCapabilityIds", [])):
            raise SkillEvalContractError(f"eval capability is outside Skill boundary: {skill_id}")
        if expect.get("requiresConfirmation") not in (True, False, None):
            raise SkillEvalContractError("confirmation expectation is invalid")
        if not isinstance(expect.get("minimumTaskCount"), int) or not 1 <= expect["minimumTaskCount"] <= 32:
            raise SkillEvalContractError("minimum task count is invalid")
        normalized.append({
            **case,
            "category": declared["category"],
            "prompt": declared["prompt"],
            "assertions": list(declared.get("assertions", [])),
        })
    if {item["skillId"] for item in normalized} != set(authoritative):
        raise SkillEvalContractError("high-frequency suite Skill coverage mismatch")
    return {
        "datasetId": payload["datasetId"],
        "graph": dict(graph_ref),
        "runtimeVersion": payload["runtimeVersion"],
        "cases": normalized,
    }


def load_skill_eval_suite(root: Path) -> dict[str, Any]:
    graph = _read_json(root / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v1.0.0.json")
    skills = _read_json(root / "data" / "ai-native-bank" / "registries" / "skills.v1.json")
    source = _read_json(root / "data" / "eval" / "ccb-skill-runtime-contracts.v1.json")
    declared_evals, declared_count = _declared_skill_evals(root)
    validated = validate_skill_eval_contracts(
        source, graph=graph, skill_registry=skills, declared_evals=declared_evals,
    )
    return {
        **validated,
        "summary": {
            "skills": len(skills["skills"]),
            "declaredCases": declared_count,
            "runtimeCases": len(validated["cases"]),
            "specOnlyCases": declared_count - len(validated["cases"]),
        },
        "source": source,
        "authoritativeGraph": graph,
        "authoritativeSkills": skills,
        "declaredEvals": declared_evals,
    }


def evaluate_runtime_observation(
    case: Mapping[str, Any], observation: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(observation, Mapping) or set(observation) != _ALLOWED_OBSERVATION_FIELDS:
        raise SkillEvalContractError("Runtime observation fields are invalid")
    status = observation.get("status")
    intent_ids = observation.get("intentIds")
    capability_ids = observation.get("capabilityIds")
    task_count = observation.get("taskCount")
    confirmation = observation.get("requiresConfirmation")
    response_type = observation.get("userResponseType")
    if status not in _ALLOWED_STATUSES or not isinstance(intent_ids, list) \
            or not isinstance(capability_ids, list) or not isinstance(task_count, int) \
            or task_count < 0 or not isinstance(confirmation, bool) \
            or not isinstance(response_type, str) or len(response_type) > 32:
        raise SkillEvalContractError("Runtime observation values are invalid")
    if not all(isinstance(item, str) and _IDENTIFIER.fullmatch(item) for item in intent_ids + capability_ids):
        raise SkillEvalContractError("Runtime observation identifiers are invalid")

    expected = case["expect"]
    failures: list[dict[str, str]] = []
    if intent_ids != expected["intentIds"]:
        failures.append({"code": "intent_mismatch", "field": "intentIds"})
    missing_capabilities = set(expected["requiredCapabilityIds"]) - set(capability_ids)
    if missing_capabilities:
        failures.append({"code": "required_capability_missing", "field": "capabilityIds"})
    if status not in expected["allowedStatuses"]:
        failures.append({"code": "terminal_status_mismatch", "field": "status"})
    if task_count < expected["minimumTaskCount"]:
        failures.append({"code": "task_count_below_minimum", "field": "taskCount"})
    if expected["requiresConfirmation"] is not None \
            and confirmation is not expected["requiresConfirmation"]:
        failures.append({"code": "confirmation_state_mismatch", "field": "requiresConfirmation"})
    return {
        "skillId": case["skillId"],
        "evalId": case["evalId"],
        "passed": not failures,
        "failures": failures,
        "observation": {
            "status": status,
            "intentIds": list(intent_ids),
            "capabilityIds": list(capability_ids),
            "taskCount": task_count,
            "requiresConfirmation": confirmation,
            "userResponseType": response_type,
        },
    }


def observation_from_sse_events(events: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(events, list) or not events or len(events) > 256:
        raise SkillEvalContractError("SSE event collection is invalid")
    completed: Mapping[str, Any] | None = None
    failed_terminal = False
    delivery: Mapping[str, Any] | None = None
    observed_capability_ids: list[str] = []
    for event in events:
        if not isinstance(event, Mapping) or set(event) != {"event", "data"}:
            raise SkillEvalContractError("SSE event fields are invalid")
        data = event.get("data")
        if not isinstance(data, Mapping):
            raise SkillEvalContractError("SSE event data is invalid")
        stage = data.get("stage")
        if isinstance(stage, str) and stage.startswith("run."):
            result = data.get("summary", {}).get("result") if isinstance(data.get("summary"), Mapping) else None
            if isinstance(result, Mapping):
                completed = result
        if stage == "run.failed" and data.get("status") == "failed":
            failed_terminal = True
        if (
            stage == "capability.invocation_completed"
            and data.get("status") == "completed"
        ):
            summary = data.get("summary")
            metrics = summary.get("metrics") if isinstance(summary, Mapping) else None
            capability_id = (
                metrics.get("capabilityId")
                if isinstance(metrics, Mapping)
                and metrics.get("outcome") == "succeeded"
                else None
            )
            if isinstance(capability_id, str):
                observed_capability_ids.append(capability_id)
        if event.get("event") == "agent.result":
            delivery = data
    if completed is None and failed_terminal:
        completed = {"kind": "agent-run", "status": "blocked"}
    if completed is None or delivery is None:
        raise SkillEvalContractError("SSE terminal evidence is missing")
    if not set(completed) <= _SAFE_RUN_FIELDS:
        raise SkillEvalContractError("Runtime result projection fields are invalid")
    tasks = completed.get("tasks", [])
    steps = completed.get("steps", [])
    if not isinstance(tasks, list) or not isinstance(steps, list) or len(tasks) > 32 or len(steps) > 64:
        raise SkillEvalContractError("Runtime task or step projection is invalid")
    if any(not isinstance(item, Mapping) or not set(item) <= _SAFE_TASK_FIELDS for item in tasks):
        raise SkillEvalContractError("Runtime task projection fields are invalid")
    if any(not isinstance(item, Mapping) or not set(item) <= _SAFE_STEP_FIELDS for item in steps):
        raise SkillEvalContractError("Runtime step projection fields are invalid")
    intent_ids = [item.get("intentId") for item in tasks]
    planned_capability_ids = [item.get("capabilityId") for item in steps]
    capability_ids = list(dict.fromkeys(
        observed_capability_ids + planned_capability_ids
    ))
    if not all(isinstance(item, str) and _IDENTIFIER.fullmatch(item) for item in intent_ids + capability_ids):
        raise SkillEvalContractError("Runtime terminal identifiers are invalid")
    user_response = delivery.get("userResponse")
    if not isinstance(user_response, Mapping) or not isinstance(user_response.get("type"), str):
        raise SkillEvalContractError("Runtime user response evidence is invalid")
    status = delivery.get("status")
    if status not in _ALLOWED_STATUSES:
        raise SkillEvalContractError("Runtime terminal status is invalid")
    requires_confirmation = bool(
        completed.get("requiresConfirmation") is True or delivery.get("confirmation") is not None
    )
    return {
        "status": status,
        "intentIds": intent_ids,
        "capabilityIds": capability_ids,
        "taskCount": len(tasks),
        "requiresConfirmation": requires_confirmation,
        "userResponseType": user_response["type"],
    }


__all__ = [
    "SkillEvalContractError", "evaluate_runtime_observation",
    "load_skill_eval_suite", "observation_from_sse_events",
    "validate_skill_eval_contracts",
]
