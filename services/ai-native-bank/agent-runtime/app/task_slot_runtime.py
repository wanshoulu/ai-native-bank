"""Graph-driven decisions for the active business Todo's semantic slots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .contracts import CapabilityPlan, CapabilityProjection, IntentTask
from .intent_graph import IntentCandidate
from .slot_contract import SlotEvaluation


class TaskSlotRuntimeError(RuntimeError):
    """Raised when an active task cannot be handled from its graph contract."""


@dataclass(frozen=True, slots=True)
class TaskSlotDecision:
    task_id: str
    slot_name: str | None
    slot_type: str | None
    slot_label: str | None
    resolver_capability_id: str | None
    resolved_field: str | None
    execution_capability_id: str
    response_policy: str
    readiness: Literal["resolve", "execute", "clarify"]


def decide_active_task(
    *,
    task: IntentTask,
    candidate: IntentCandidate | None,
    evaluation: SlotEvaluation,
    intent_shape: str,
) -> TaskSlotDecision:
    """Select the next operation using only the delegated task and graph metadata."""

    if candidate is None or candidate.intent_id != task.intent_id:
        raise TaskSlotRuntimeError("task and graph candidate do not match")
    # 纯查询（read-execute）直接绑定单一 capability 走 MCP，无槽位契约与 task_policy。
    if candidate.execution_mode == "read-execute" and candidate.slot_contract is None:
        if task.action != "QUERY" or len(candidate.capability_bindings) != 1:
            raise TaskSlotRuntimeError("read-execute intent must bind exactly one query capability")
        return TaskSlotDecision(
            task_id=task.task_id,
            slot_name=None,
            slot_type=None,
            slot_label=None,
            resolver_capability_id=None,
            resolved_field=None,
            execution_capability_id=candidate.capability_bindings[0],
            response_policy="query_result",
            readiness="execute",
        )
    # ``multi_intent`` describes the envelope orchestration of several distinct
    # intents.  Each delegated item is still one task of its own intent, so its
    # graph contract is the ``single_intent`` contract.  In contrast,
    # ``same_intent_multi_task`` really does ask one intent contract to support
    # multiple instances and therefore remains an explicit opt-in.
    task_contract_shape = (
        "single_intent" if intent_shape == "multi_intent" else intent_shape
    )
    if candidate.task_policy is None:
        if task_contract_shape != "single_intent":
            raise TaskSlotRuntimeError("task shape is not graph-approved")
    elif task_contract_shape not in candidate.task_policy.supported_shapes:
        raise TaskSlotRuntimeError("task shape is not graph-approved")
    contract = candidate.slot_contract
    if contract is None or not candidate.response_policy:
        raise TaskSlotRuntimeError("task graph contract is incomplete")
    execution_capability_id = contract.execution_binding.capability_id
    if execution_capability_id not in candidate.capability_bindings:
        raise TaskSlotRuntimeError("execution capability is outside graph bindings")

    unresolved = set(evaluation.missing + evaluation.ambiguous + evaluation.invalid)
    unresolved.update(
        definition.name
        for definition in contract.slots
        if definition.resolver_capability_id is not None
        and definition.name in evaluation.semantic_values
        and definition.resolved_field not in evaluation.resolved_values
    )
    for slot_name in contract.clarification_order:
        if slot_name not in unresolved:
            continue
        try:
            definition = contract.slot(slot_name)
        except KeyError as exc:
            raise TaskSlotRuntimeError("clarification slot is not declared") from exc
        resolver = definition.resolver_capability_id
        if resolver is not None and resolver not in candidate.capability_bindings:
            raise TaskSlotRuntimeError("resolver capability is outside graph bindings")
        return TaskSlotDecision(
            task_id=task.task_id,
            slot_name=definition.name,
            slot_type=definition.type,
            slot_label=definition.label,
            resolver_capability_id=resolver,
            resolved_field=definition.resolved_field,
            execution_capability_id=execution_capability_id,
            response_policy=candidate.response_policy,
            readiness="resolve" if resolver else "clarify",
        )

    if evaluation.readiness == "ready":
        return TaskSlotDecision(
            task_id=task.task_id,
            slot_name=None,
            slot_type=None,
            slot_label=None,
            resolver_capability_id=None,
            resolved_field=None,
            execution_capability_id=execution_capability_id,
            response_policy=candidate.response_policy,
            readiness="execute",
        )

    raise TaskSlotRuntimeError("unresolved slot is outside clarification order")


def active_task_capability_ids(
    task: IntentTask,
    candidate: IntentCandidate,
    decision: TaskSlotDecision,
) -> frozenset[str]:
    """Expose only the graph capability needed by the active task phase."""

    if decision.task_id != task.task_id or candidate.intent_id != task.intent_id:
        raise TaskSlotRuntimeError("capability boundary does not belong to the task")
    if task.action != "QUERY":
        return frozenset(candidate.capability_bindings)
    if decision.readiness == "resolve":
        return frozenset(
            {decision.resolver_capability_id}
            if decision.resolver_capability_id is not None
            else set()
        )
    if decision.readiness == "execute":
        return frozenset({decision.execution_capability_id})
    return frozenset()


def runtime_capability_phase(
    decision: TaskSlotDecision | None,
) -> dict[str, object] | None:
    """Project the graph-derived phase boundary into model-visible context."""

    if decision is None:
        return None
    completion_policy = {
        "resolve": "return_after_allowed_call",
        "execute": "continue_within_allowlist",
        "clarify": "return_without_capability_call",
    }[decision.readiness]
    return {
        "task_id": decision.task_id,
        "readiness": decision.readiness,
        "completion_policy": completion_policy,
    }


def build_graph_read_plan(
    plan: CapabilityPlan,
    task: IntentTask,
    decision: TaskSlotDecision,
    resolved_value: object,
) -> CapabilityPlan:
    """Build one read step using only the active task's graph-derived decision."""

    if not isinstance(plan, CapabilityPlan) or not isinstance(task, IntentTask):
        raise TaskSlotRuntimeError("plan and task must use runtime contracts")
    if (
        decision.task_id != task.task_id
        or decision.readiness not in {"resolve", "execute"}
    ):
        raise TaskSlotRuntimeError("slot decision does not belong to the active task")
    if not decision.resolved_field or resolved_value is None:
        raise TaskSlotRuntimeError("resolved slot binding is incomplete")
    return CapabilityPlan.model_validate({
        "schema_version": "1.0",
        "business_agent_run_id": plan.business_agent_run_id,
        "delegation_id": plan.delegation_id,
        "status": "planned",
        "steps": [{
            # This synthesized plan always contains exactly one active read
            # step.  Keep its public trace identifier stable and opaque;
            # embedding the delegated task UUID creates a credential-like
            # identifier that the Node trace boundary correctly rejects.
            "step_id": "step_execute",
            "task_id": task.task_id,
            "capability_id": decision.execution_capability_id,
            "input_bindings": {decision.resolved_field: resolved_value},
            "requires_user_confirmation": False,
            "depends_on": [],
        }],
    })


def build_graph_action_plan(
    plan: CapabilityPlan,
    task: IntentTask,
    decision: TaskSlotDecision,
    evaluation: SlotEvaluation,
    capability: CapabilityProjection,
    resolved_references: dict[str, object] | None = None,
) -> CapabilityPlan | None:
    """Build a confirmation plan from graph slots and the capability contract.

    This is intentionally domain-neutral: product/account/card specifics stay
    in the intent graph, while required input fields stay in the capability
    schema.  A partial or non-user-action binding simply remains suspended.
    """

    if not isinstance(plan, CapabilityPlan) or not isinstance(task, IntentTask):
        raise TaskSlotRuntimeError("plan and task must use runtime contracts")
    if decision.task_id != task.task_id or task.action != "DO":
        return None
    if capability.id != decision.execution_capability_id:
        raise TaskSlotRuntimeError("action capability does not match graph binding")
    if capability.exposure != "user-action" or capability.side_effect == "none":
        raise TaskSlotRuntimeError("action capability lacks confirmation boundary")
    resolved_references = dict(resolved_references or {})
    # A reference slot is intentionally only semantically extracted by the
    # User Agent.  The Business Agent resolves it through the graph-declared
    # discovery capability.  Once that resolver returns the contract's
    # resolved field, the original semantic slot must no longer keep an
    # otherwise complete action suspended.  Other unresolved slots still
    # fail closed.
    externally_resolved_slot = (
        decision.slot_name
        if decision.resolved_field
        and decision.resolved_field in resolved_references
        else None
    )
    unresolved_missing = {
        name for name in evaluation.missing if name != externally_resolved_slot
    }
    unresolved_ambiguous = {
        name for name in evaluation.ambiguous if name != externally_resolved_slot
    }
    unresolved_invalid = {
        name for name in evaluation.invalid if name != externally_resolved_slot
    }
    if unresolved_missing or unresolved_ambiguous or unresolved_invalid:
        return None

    bindings = dict(evaluation.resolved_values)
    bindings.update(resolved_references)
    required = capability.input_schema.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(name, str) for name in required
    ):
        raise TaskSlotRuntimeError("action capability input contract is invalid")
    if not set(required).issubset(bindings):
        return None

    properties = capability.input_schema.get("properties", {})
    if not isinstance(properties, dict):
        raise TaskSlotRuntimeError("action capability input properties are invalid")
    projected = {
        name: value for name, value in bindings.items() if name in properties
    }
    return CapabilityPlan.model_validate({
        "schema_version": "1.0",
        "business_agent_run_id": plan.business_agent_run_id,
        "delegation_id": plan.delegation_id,
        "status": "planned",
        "steps": [{
            "step_id": "step_confirm_action",
            "task_id": task.task_id,
            "capability_id": capability.id,
            "input_bindings": projected,
            "requires_user_confirmation": True,
            "depends_on": [],
        }],
    })
