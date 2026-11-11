"""Safe structural state stored through OpenJiuwen's native Session checkpointer."""

from __future__ import annotations

from typing import Any


def project_safe_session_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Project workflow structure only; never persist messages or slot values."""

    envelope = payload.get("intent_task_envelope")
    envelope = envelope if isinstance(envelope, dict) else {}
    raw_tasks = envelope.get("tasks")
    tasks = []
    if isinstance(raw_tasks, list):
        for item in raw_tasks[:16]:
            if not isinstance(item, dict):
                continue
            task_id = item.get("task_id")
            intent_id = item.get("intent_id")
            if not isinstance(task_id, str) or not isinstance(intent_id, str):
                continue
            tasks.append({
                "taskId": task_id[:160],
                "intentId": intent_id[:160],
                "status": str(item.get("task_status") or "pending")[:32],
            })

    execution_view = payload.get("execution_view")
    execution_view = execution_view if isinstance(execution_view, dict) else {}
    graph = payload.get("graph_context")
    graph = graph if isinstance(graph, dict) else {}
    state: dict[str, Any] = {
        "schemaVersion": "1.0",
        "delegationId": str(envelope.get("delegation_id") or "")[:96],
        "envelopeRevision": _positive_int(envelope.get("revision"), 1),
        "intentShape": str(envelope.get("intent_shape") or "non_task")[:32],
        "tasks": tasks,
        "executionView": {
            "digest": str(execution_view.get("digest") or "")[:80],
            "skillIds": _bounded_strings(execution_view.get("skillIds"), 8, 128),
            "capabilityIds": _bounded_strings(execution_view.get("capabilityIds"), 64, 128),
            "providerIds": _bounded_strings(execution_view.get("providerIds"), 64, 128),
        },
        "graph": {
            "version": str(graph.get("version") or "")[:32],
            "digest": str(graph.get("digest") or "")[:80],
        },
    }
    workflow_id = payload.get("business_workflow_id")
    if isinstance(workflow_id, str):
        state["businessWorkflowId"] = workflow_id[:160]
    resume_kind = payload.get("resume_kind")
    if isinstance(resume_kind, str):
        state["resumeKind"] = resume_kind[:32]
    return state


def _bounded_strings(value: Any, maximum: int, item_length: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:item_length] for item in value[:maximum] if isinstance(item, str)]


def _positive_int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default
