"""Build bounded memory candidate commands from authoritative terminal runs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.contracts import IntentTaskEnvelope
from app.memory_runtime import IntentMemoryPolicyRegistry, UserMemoryPolicyRegistry


@dataclass(frozen=True)
class MemoryWriteSummary:
    candidate_count: int = 0
    canonical_episode_count: int = 0
    user_preference_count: int = 0
    add_count: int = 0
    merge_count: int = 0
    supersede_count: int = 0
    contested_count: int = 0
    reject_count: int = 0
    persisted_count: int = 0
    pending_retry_count: int = 0
    dead_letter_count: int = 0

    @classmethod
    def from_results(cls, candidates: list[dict[str, Any]], results: tuple[dict[str, Any], ...] = ()) -> "MemoryWriteSummary":
        decisions = [item.get("decision") for item in results]
        statuses = [item.get("status") for item in results]
        return cls(
            candidate_count=len(candidates),
            canonical_episode_count=sum(item["command"]["candidateType"] == "canonical_service_episode" for item in candidates),
            user_preference_count=sum(item["command"]["candidateType"] == "user_preference" for item in candidates),
            add_count=decisions.count("ADD"), merge_count=decisions.count("MERGE"),
            supersede_count=decisions.count("SUPERSEDE"), contested_count=decisions.count("CONTESTED"),
            reject_count=decisions.count("REJECT"), persisted_count=statuses.count("PERSISTED"),
            pending_retry_count=sum(status in {"QUEUED", "PROCESSING", "PENDING_RETRY"} for status in statuses),
            dead_letter_count=statuses.count("DEAD_LETTER"),
        )


def build_user_preference_candidates(
    *, hints: list[dict[str, Any]], policies: UserMemoryPolicyRegistry,
    tenant_id: str, customer_id: str, agent_domain: str, agent_run_id: str,
    occurred_at: datetime,
) -> list[dict[str, Any]]:
    timestamp = occurred_at.isoformat()
    candidates: list[dict[str, Any]] = []
    seen_predicates: set[str] = set()
    for hint in hints[:5]:
        predicate = hint.get("predicate")
        policy = policies.write_hint(str(predicate))
        if policy is None or hint.get("evidence_kind") != "user_explicit" or hint.get("consent_granted") is not True:
            continue
        if predicate in seen_predicates:
            continue
        value = hint.get("value")
        if isinstance(value, (dict, list, tuple, set)) or value is None:
            continue
        seen_predicates.add(predicate)
        candidate_type = policy["candidateType"]
        source_revision = "1"
        # Idempotency = agentRunId + taskId + candidateType + predicate + sourceRevision.
        # The value is deliberately excluded; conflicting values for the same
        # 5-tuple are dropped here so the gateway never sees a 409 on the batch.
        digest = hashlib.sha256(
            f"{agent_run_id}::{candidate_type}:{predicate}:{source_revision}".encode("utf-8")
        ).hexdigest()
        candidates.append({
            "taskId": None,
            "intentId": None,
            "command": {
                "schemaVersion": "1.0", "idempotencyKey": f"memory.{digest[:40]}",
                "candidateType": policy["candidateType"],
                "scope": {
                    "tenantId": tenant_id, "customerId": customer_id,
                    "agentDomain": agent_domain, "memorySpace": "personal-preference",
                    "purpose": policy["purpose"],
                },
                "subjectRef": "service-preference", "predicate": predicate, "value": value,
                "validFrom": timestamp,
                "source": {
                    "type": "user_explicit", "label": "explicit user preference",
                    "sourceRef": agent_run_id, "sourceRevision": source_revision,
                },
                "evidence": {
                    "eventType": "user_preference_declared", "occurredAt": timestamp,
                    "safeSummary": f"User explicitly set the {predicate} preference",
                    "contentHashShort": digest[:12],
                },
                "consentGranted": True, "safetyStatus": "allow",
            },
        })
    return candidates


def build_canonical_episode_candidates(
    *, envelope: IntentTaskEnvelope, policies: IntentMemoryPolicyRegistry,
    tenant_id: str, customer_id: str, agent_domain: str, agent_run_id: str,
    status: str, occurred_at: datetime,
    user_message: str = "",
) -> list[dict[str, Any]]:
    if status != "succeeded":
        return []
    timestamp = occurred_at.isoformat()
    candidates: list[dict[str, Any]] = []
    seen_tasks: set[str] = set()
    for task in envelope.tasks:
        policy = policies.write_policy(task.intent_id, status)
        if policy is None or task.task_id in seen_tasks:
            continue
        seen_tasks.add(task.task_id)
        # 构造语义丰富的 value，包含业务对象和用户原始消息，
        # 使得 embedding 后能与用户后续查询的语义空间匹配
        msg_excerpt = user_message[:80] if user_message else ""
        value = f"用户办理{task.business_object}，结果{status}"
        if msg_excerpt:
            value += f"。用户说：{msg_excerpt}"
        # Idempotency 5-tuple: agentRunId + taskId + candidateType + predicate
        # + sourceRevision (envelope revision); value excluded on purpose.
        source_revision = str(envelope.revision)
        digest = hashlib.sha256(
            f"{agent_run_id}:{task.task_id}:{policy['candidateType']}:"
            f"{policy['predicate']}:{source_revision}".encode("utf-8")
        ).hexdigest()
        command = {
            "schemaVersion": "1.0",
            "idempotencyKey": f"memory.{digest[:40]}",
            "candidateType": policy["candidateType"],
            "scope": {
                "tenantId": tenant_id,
                "customerId": customer_id,
                "agentDomain": agent_domain,
                "memorySpace": "customer-service",
                "purpose": policy["purpose"],
            },
            "subjectRef": "service-history",
            "predicate": policy["predicate"],
            "value": value,
            "validFrom": timestamp,
            "source": {
                "type": "canonical_run",
                "label": "governed service run",
                "sourceRef": agent_run_id,
                "sourceRevision": source_revision,
            },
            "evidence": {
                "eventType": "canonical_service_run",
                "occurredAt": timestamp,
                "safeSummary": f"用户执行{task.action}操作，业务对象{task.business_object}，意图{task.intent_id}，执行结果{status}",
                "contentHashShort": digest[:12],
            },
            "consentGranted": False,
            "safetyStatus": "allow",
        }
        candidates.append({
            "taskId": task.task_id,
            "intentId": task.intent_id,
            "command": command,
        })
    return candidates
