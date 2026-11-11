"""Deterministic, value-free Rails for tool-local intent and planning constraints."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.contracts import (
    CapabilityPlan,
    CapabilityProjection,
    IntentShape,
    IntentTaskEnvelope,
    StrictModel,
)


class IntentConstraint(StrictModel):
    """A deterministic tool-local constraint, never a model confidence score."""

    intent_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=80)
    action: Literal["ASK", "QUERY", "NAVIGATE", "DO", "CHANGE", "CANCEL", "HELP"]
    business_object: str = Field(min_length=1, max_length=128)
    required_shape: IntentShape
    expected_task_count: int = Field(default=1, ge=0, le=16)
    shape_authoritative: bool = False
    supported_shapes: list[IntentShape] = Field(default_factory=list, max_length=5)
    unresolved_slot_readiness: Literal["needs_slots", "needs_clarification"] | None = None
    execution_mode: Literal["read-execute", "confirm-required", "navigation-only", "knowledge-only", "unavailable"] = "read-execute"
    readiness: str = Field(default="planned", max_length=32)
    required_slots: list[str] = Field(default_factory=list, max_length=32)
    capability_bindings: list[str] = Field(default_factory=list, max_length=32)
    score: int = Field(default=0, ge=0, le=1000)
    evidence: list[str] = Field(default_factory=list, max_length=16)
    source: Literal["tool_local_graph"]
    constraint: Literal["required", "allowed"]
    rule_version: str = Field(
        pattern=r"^[A-Za-z0-9_.-]+$", min_length=1, max_length=32
    )
    risk_level: str = Field(default="L1", max_length=8)
    slot_contract: dict[str, Any] | None = None
    disambiguation_hints: str | None = Field(
        default=None, alias="disambiguationHints", max_length=400
    )


def validate_intent_constraints(
    envelope: IntentTaskEnvelope,
    constraints: list[IntentConstraint],
) -> tuple[str, ...]:
    """Keep model decisions inside the authoritative graph candidate set."""

    if not constraints:
        if envelope.intent_shape == "non_task":
            return ()
        if envelope.intent_shape == "ambiguous_intent":
            return ("candidate_free_ambiguity_invalid",)
        return ("candidate_required",)
    violations: list[str] = []
    by_id = {item.intent_id: item for item in constraints}
    shape_authoritative = all(item.shape_authoritative for item in constraints)
    if len(constraints) == 1 and shape_authoritative:
        expected = constraints[0]
        if envelope.intent_shape != expected.required_shape:
            violations.append("intent_shape_mismatch")
        if len(envelope.tasks) != expected.expected_task_count:
            violations.append("task_cardinality_mismatch")
        elif expected.required_shape in {"multi_intent", "same_intent_multi_task"} and len(envelope.tasks) < 2:
            violations.append("task_cardinality_mismatch")
        if expected.required_shape == "same_intent_multi_task" and any(
            task.intent_id != expected.intent_id for task in envelope.tasks
        ):
            violations.append("intent_id_mismatch")
    elif shape_authoritative and len({item.required_shape for item in constraints}) == 1 and constraints[0].required_shape != "single_intent":
        expected_shape = constraints[0].required_shape
        if envelope.intent_shape != expected_shape:
            violations.append("intent_shape_mismatch")
        if expected_shape in {"multi_intent", "same_intent_multi_task"} and len(envelope.tasks) < 2:
            violations.append("task_cardinality_mismatch")
        if expected_shape == "multi_intent" and not {
            task.intent_id for task in envelope.tasks
        }.issubset({item.intent_id for item in constraints}):
            violations.append("intent_id_mismatch")
    elif envelope.intent_shape == "single_intent" and len(envelope.tasks) != 1:
        violations.append("task_cardinality_mismatch")
    elif envelope.intent_shape in {"multi_intent", "same_intent_multi_task"} and len(envelope.tasks) < 2:
        violations.append("task_cardinality_mismatch")
    if not shape_authoritative:
        if envelope.intent_shape == "multi_intent":
            if len({task.intent_id for task in envelope.tasks}) < 2:
                violations.append("intent_id_mismatch")
        elif envelope.intent_shape == "same_intent_multi_task":
            if len({task.intent_id for task in envelope.tasks}) != 1:
                violations.append("intent_id_mismatch")
            matched = by_id.get(envelope.tasks[0].intent_id) if envelope.tasks else None
            if matched is not None and "same_intent_multi_task" not in matched.supported_shapes:
                violations.append("intent_shape_unsupported")
        elif envelope.intent_shape == "single_intent" and envelope.tasks:
            matched = by_id.get(envelope.tasks[0].intent_id)
            if matched is not None and matched.supported_shapes and "single_intent" not in matched.supported_shapes:
                violations.append("intent_shape_unsupported")
    for task in envelope.tasks:
        expected = by_id.get(task.intent_id)
        if expected is None:
            violations.append("intent_id_mismatch")
            continue
        if task.action != expected.action:
            violations.append("action_mismatch")
        if task.business_object != expected.business_object:
            violations.append("business_object_mismatch")
        if expected.unresolved_slot_readiness is not None:
            has_all_required_slots = all(
                slot_name in task.slots for slot_name in expected.required_slots
            )
            expected_readiness = "ready" if has_all_required_slots else expected.unresolved_slot_readiness
            unresolved_state = task.execution_readiness in {
                "needs_slots", "needs_clarification"
            }
            if task.execution_readiness != expected_readiness and not unresolved_state:
                violations.append("execution_readiness_mismatch")
    return tuple(dict.fromkeys(violations))


def graph_bound_discovery_applies(
    envelope: IntentTaskEnvelope,
    capabilities: list[CapabilityProjection],
    constraints: list[IntentConstraint] | None = None,
) -> bool:
    if len(envelope.tasks) != 1:
        return False
    task = envelope.tasks[0]
    if task.execution_readiness not in {"needs_slots", "needs_clarification"}:
        return False
    projected = {item.id: item for item in capabilities}
    matched = next(
        (item for item in (constraints or []) if item.intent_id == task.intent_id), None
    )
    if constraints is not None and matched is None:
        return False
    # Discovery applies only when an unresolved semantic reference has a
    # graph-declared resolver. A missing scalar such as month/date cannot be
    # discovered safely; the valid terminal state is clarification.
    if matched is not None and isinstance(matched.slot_contract, dict):
        slot_definitions = matched.slot_contract.get("slots")
        if isinstance(slot_definitions, list):
            for definition in slot_definitions:
                if not isinstance(definition, dict) or not definition.get("required"):
                    continue
                raw_extraction_keys = definition.get("extractionKeys")
                extraction_keys = (
                    raw_extraction_keys
                    if isinstance(raw_extraction_keys, list)
                    else []
                )
                keys = tuple(
                    key for key in (
                        definition.get("name"),
                        *extraction_keys,
                        definition.get("resolvedField"),
                    )
                    if isinstance(key, str) and key
                )
                if not any(key in task.slots for key in keys) and not isinstance(
                    definition.get("resolverCapabilityId"), str
                ):
                    return False
    required = tuple(matched.capability_bindings) if matched else tuple(projected)
    return all(
        capability_id in projected
        and projected[capability_id].side_effect == "none"
        for capability_id in required
    ) and bool(required)


def validate_graph_bound_discovery(
    plan: CapabilityPlan,
    envelope: IntentTaskEnvelope,
    capabilities: list[CapabilityProjection],
    constraints: list[IntentConstraint] | None = None,
    completed_capability_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Validate graph-bound read discovery without inspecting business values."""

    if not graph_bound_discovery_applies(envelope, capabilities, constraints):
        return ()
    capability_map = {item.id: item for item in capabilities}
    matched = next(
        (item for item in (constraints or []) if item.intent_id == envelope.tasks[0].intent_id), None
    )
    expected = list(matched.capability_bindings) if matched else list(capability_map)
    completed = list(dict.fromkeys(completed_capability_ids or ()))
    if plan.status != "planned":
        if (
            plan.status == "clarification_required"
            and completed
            and completed == expected[:len(completed)]
        ):
            return ()
        return ("readonly_discovery_required",)

    violations: list[str] = []
    task_id = envelope.tasks[0].task_id
    if len(plan.steps) != len(expected):
        violations.append("readonly_discovery_required")
    if [step.capability_id for step in plan.steps] != expected:
        violations.append("discovery_step_order_invalid")
    for index, step in enumerate(plan.steps):
        expected_dependencies = [] if index == 0 else [plan.steps[index - 1].step_id]
        if step.depends_on != expected_dependencies:
            violations.append("discovery_dependency_missing")

    if any(step.task_id != task_id for step in plan.steps):
        violations.append("discovery_task_mismatch")
    if any(step.requires_user_confirmation for step in plan.steps):
        violations.append("unexpected_confirmation")
    if any(
        step.capability_id not in capability_map
        or capability_map[step.capability_id].side_effect != "none"
        for step in plan.steps
    ):
        violations.append("unexpected_side_effect_capability")

    allowed = set(expected)
    if any(step.capability_id not in allowed for step in plan.steps):
        if "readonly_discovery_required" not in violations:
            violations.append("readonly_discovery_required")

    return tuple(dict.fromkeys(violations))


# Compatibility aliases for existing callers; behavior is graph-bound and contains no balance seed.
readonly_discovery_applies = graph_bound_discovery_applies
validate_readonly_discovery = validate_graph_bound_discovery
