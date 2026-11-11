"""Run-scoped gateway for bounded read-only bank capabilities."""

from __future__ import annotations

import re
import copy
import logging
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Iterator

import httpx
try:
    from openjiuwen.core.foundation.tool import tool
except ImportError:
    def tool(*args, **kwargs):  # type: ignore[no-redef]
        """openjiuwen 不可用时的占位装饰器。"""
        if args and callable(args[0]) and not kwargs:
            return args[0]
        def decorator(func):
            return func
        return decorator

from app.business_loop import validate_authoritative_todo_snapshot
from app.safety_gate import (
    ExecutionPermit,
    PermitValidationError,
)
from app.todo_ledger import TodoLedger

CAPABILITY_ID = re.compile(r"^bank\.[a-z0-9.-]{1,123}$")
INPUT_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
# 读调用默认预算：1 次首发 + 2 次纠错重试。
DEFAULT_READ_CALL_BUDGET = 3
logger = logging.getLogger("ai_native_bank.agent_capability_gateway")


def _safe_default_scalar(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -(2**53) < value < 2**53
    if isinstance(value, str):
        return 1 <= len(value) <= 128 and not any(
            unicodedata.category(char).startswith("C") for char in value
        )
    return False


def _todo_belongs_to_delegated_task(
    todo_id: str, delegated_task_ids: list[str]
) -> bool:
    """Resolve Todo ownership without rewriting OpenJiuwen-native identifiers.

    A single delegated goal is the unambiguous owner of every Todo in its
    workspace.  Multiple goals still require a namespaced Todo identifier so
    persisted state cannot be attached to the wrong goal.
    """
    if len(delegated_task_ids) == 1:
        return True
    return any(
        todo_id == task_id
        or todo_id.startswith(task_id + ":")
        or todo_id.startswith(task_id + ".")
        for task_id in delegated_task_ids
    )


class AgentCapabilityInvocationError(RuntimeError):
    """A stable, value-free capability gateway failure."""

    def __init__(self, code: str, detail: str | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass
class AgentCapabilityContext:
    agent_run_id: str
    execution_allowlist: frozenset[str]
    plannable_allowlist: frozenset[str]
    successful_invocations: list[dict[str, Any]]
    call_budgets: dict[str, int]
    call_counts: dict[str, int]
    input_defaults: dict[str, dict[str, Any]]


@dataclass
class AuthorizedCapabilityContext:
    agent_run_id: str
    business_workflow_id: str
    grants: dict[str, dict[str, str]]
    consumed_grant_ids: set[str]
    receipts: list[dict[str, Any]]


class AgentCapabilityInvoker:
    def __init__(
        self,
        *,
        endpoint: str,
        service_token: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 20.0,
        checkpoint_endpoint: str | None = None,
        workflow_endpoint: str | None = None,
        authorized_endpoint: str | None = None,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("node_gateway_url_invalid")
        if not 16 <= len(service_token) <= 512 or any(char.isspace() for char in service_token):
            raise ValueError("node_gateway_token_invalid")
        self._endpoint = endpoint
        self._checkpoint_endpoint = checkpoint_endpoint or endpoint.replace(
            "/agent-capability-invocations", "/agent-action-checkpoints"
        )
        self._workflow_endpoint = workflow_endpoint or endpoint.replace(
            "/agent-capability-invocations", "/agent-workflows"
        )
        self._authorized_endpoint = authorized_endpoint or endpoint.replace(
            "/agent-capability-invocations",
            "/agent-authorized-capability-invocations",
        )
        self._service_token = service_token
        self._client = client or httpx.AsyncClient()
        self._timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self._context: ContextVar[AgentCapabilityContext | None] = ContextVar(
            "ai_bank_agent_capability_context", default=None
        )
        self._authorized_context: ContextVar[AuthorizedCapabilityContext | None] = (
            ContextVar("ai_bank_authorized_capability_context", default=None)
        )
        self._consumed_delegation_permits: dict[str, datetime] = {}

    def peek_agent_run_id(self) -> str | None:
        context = self._context.get() or self._authorized_context.get()
        return context.agent_run_id if context is not None else None

    def consume_delegation_permit(
        self,
        permit: ExecutionPermit,
        *,
        request_id: str,
        conversation_id: str,
        session_revision: int,
    ) -> None:
        """Validate and consume the one-shot permit at the A2A boundary.

        The permit is never forwarded to Node or exposed to a model.  Keeping
        consumption inside the gateway prevents a caller from replaying a
        previously allowed Rail decision to start another Business Agent run.
        """
        if not isinstance(permit, ExecutionPermit):
            raise AgentCapabilityInvocationError("execution_permit_invalid")
        try:
            permit.validate_binding(
                request_id=request_id,
                conversation_id=conversation_id,
                session_revision=session_revision,
                required_scope="a2a.delegate",
            )
        except PermitValidationError as exc:
            raise AgentCapabilityInvocationError(
                "execution_permit_invalid"
            ) from exc

        now = datetime.now(timezone.utc)
        self._consumed_delegation_permits = {
            permit_id: expiry
            for permit_id, expiry in self._consumed_delegation_permits.items()
            if expiry > now
        }
        if permit.permit_id in self._consumed_delegation_permits:
            raise AgentCapabilityInvocationError("execution_permit_replayed")
        expiry = permit.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        self._consumed_delegation_permits[permit.permit_id] = expiry

    @contextmanager
    def activate(
        self,
        agent_run_id: str,
        execution_allowlist: set[str] | frozenset[str],
        plannable_allowlist: set[str] | frozenset[str] | None = None,
        call_budgets: Mapping[str, int] | None = None,
        input_defaults: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Iterator[AgentCapabilityContext]:
        if not isinstance(agent_run_id, str) or not 1 <= len(agent_run_id) <= 128:
            raise AgentCapabilityInvocationError("agent_run_id_invalid")
        allowed = frozenset(execution_allowlist)
        if len(allowed) > 64 or any(not CAPABILITY_ID.fullmatch(item) for item in allowed):
            raise AgentCapabilityInvocationError("capability_allowlist_invalid")
        plannable = frozenset(plannable_allowlist or allowed)
        if len(plannable) > 64 or any(not CAPABILITY_ID.fullmatch(item) for item in plannable):
            raise AgentCapabilityInvocationError("capability_allowlist_invalid")
        # 读能力无副作用（写路径由一次性 grant 独立把关），预算只用于防
        # 死循环。允许少量重试：一次输入不合法的失败读不应把该能力锁死
        # 到运行结束，否则轻量模型会反复撞预算直至整体超时。
        budgets = {item: DEFAULT_READ_CALL_BUDGET for item in allowed}
        if call_budgets is not None:
            if (
                any(item not in allowed for item in call_budgets)
                or any(
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value not in set(range(DEFAULT_READ_CALL_BUDGET + 1))
                    for value in call_budgets.values()
                )
            ):
                raise AgentCapabilityInvocationError("capability_call_budget_invalid")
            budgets.update(call_budgets)
        defaults: dict[str, dict[str, Any]] = {}
        if input_defaults is not None:
            if not isinstance(input_defaults, Mapping) or any(
                capability_id not in allowed
                or not isinstance(values, Mapping)
                or len(values) > 16
                or any(
                    not INPUT_FIELD.fullmatch(str(field))
                    or not _safe_default_scalar(value)
                    for field, value in values.items()
                )
                for capability_id, values in input_defaults.items()
            ):
                raise AgentCapabilityInvocationError("capability_input_defaults_invalid")
            defaults = {
                capability_id: copy.deepcopy(dict(values))
                for capability_id, values in input_defaults.items()
            }
        context = AgentCapabilityContext(
            agent_run_id, allowed, plannable, [], budgets, {}, defaults
        )
        token = self._context.set(context)
        try:
            yield context
        finally:
            self._context.reset(token)

    def current_context(self) -> AgentCapabilityContext | None:
        return self._context.get()

    def is_authorized_context_active(self) -> bool:
        return self._authorized_context.get() is not None

    @contextmanager
    def activate_authorized(
        self,
        agent_run_id: str,
        business_workflow_id: str,
        grants: list[dict[str, str]],
    ) -> Iterator[AuthorizedCapabilityContext]:
        safe_id = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
        if not safe_id.fullmatch(agent_run_id or ""):
            raise AgentCapabilityInvocationError("agent_run_id_invalid")
        if not safe_id.fullmatch(business_workflow_id or ""):
            raise AgentCapabilityInvocationError("business_workflow_id_invalid")
        if not 1 <= len(grants) <= 16:
            raise AgentCapabilityInvocationError("capability_grants_invalid")
        indexed: dict[str, dict[str, str]] = {}
        for grant in grants:
            if not isinstance(grant, dict) or set(grant) != {
                "grantId", "taskId", "capabilityId", "expiresAt"
            }:
                raise AgentCapabilityInvocationError("capability_grants_invalid")
            if (
                not re.fullmatch(r"acg_[A-Za-z0-9_-]{1,96}", grant["grantId"])
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", grant["taskId"])
                or not CAPABILITY_ID.fullmatch(grant["capabilityId"])
                or grant["grantId"] in indexed
            ):
                raise AgentCapabilityInvocationError("capability_grants_invalid")
            indexed[grant["grantId"]] = dict(grant)
        context = AuthorizedCapabilityContext(
            agent_run_id, business_workflow_id, indexed, set(), []
        )
        token = self._authorized_context.set(context)
        try:
            yield context
        finally:
            self._authorized_context.reset(token)

    async def invoke_authorized_capability(
        self, grant_id: str, task_id: str
    ) -> dict[str, Any]:
        context = self._authorized_context.get()
        if context is None:
            raise AgentCapabilityInvocationError("authorized_context_not_active")
        grant = context.grants.get(grant_id)
        if grant is None or grant["taskId"] != task_id:
            raise AgentCapabilityInvocationError("capability_grant_not_allowed")
        if grant_id in context.consumed_grant_ids:
            raise AgentCapabilityInvocationError("capability_grant_already_used")
        context.consumed_grant_ids.add(grant_id)
        try:
            response = await self._client.post(
                self._authorized_endpoint,
                headers={"authorization": "Bearer " + self._service_token},
                json={
                    "agentRunId": context.agent_run_id,
                    "businessWorkflowId": context.business_workflow_id,
                    "taskId": task_id,
                    "grantId": grant_id,
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise AgentCapabilityInvocationError("node_gateway_unavailable") from exc
        if not 200 <= response.status_code < 300:
            logger.warning(
                "Authorized capability gateway rejected status=%s",
                response.status_code,
            )
            raise AgentCapabilityInvocationError("node_gateway_rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentCapabilityInvocationError("node_gateway_response_invalid") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise AgentCapabilityInvocationError("node_gateway_response_invalid")
        context.receipts.append(copy.deepcopy(payload["data"]))
        return payload["data"]

    async def invoke_read_capability(
        self, capability_id: str, input: dict[str, Any]
    ) -> dict[str, Any]:
        context = self.current_context()
        if context is None:
            raise AgentCapabilityInvocationError("context_not_active")
        if not CAPABILITY_ID.fullmatch(capability_id or ""):
            raise AgentCapabilityInvocationError("capability_id_invalid")
        if capability_id not in context.execution_allowlist:
            logger.warning(
                "Read capability rejected: capability_id=%s not in allowlist=%s agent_run_id=%s",
                capability_id,
                sorted(context.execution_allowlist),
                context.agent_run_id,
            )
            raise AgentCapabilityInvocationError("capability_not_allowed")
        if not isinstance(input, dict):
            raise AgentCapabilityInvocationError("capability_input_invalid")
        effective_input = {
            **copy.deepcopy(context.input_defaults.get(capability_id, {})),
            **copy.deepcopy(input),
        }
        used = context.call_counts.get(capability_id, 0)
        if used >= context.call_budgets.get(capability_id, 0):
            raise AgentCapabilityInvocationError("capability_call_budget_exceeded")
        # Consume before I/O. A failed or rejected call cannot be replayed within
        # the same agent context and accidentally duplicate a bank operation.
        context.call_counts[capability_id] = used + 1
        try:
            response = await self._client.post(
                self._endpoint,
                headers={"authorization": "Bearer " + self._service_token},
                json={
                    "agentRunId": context.agent_run_id,
                    "capabilityId": capability_id,
                    "input": effective_input,
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise AgentCapabilityInvocationError("node_gateway_unavailable") from exc
        if not 200 <= response.status_code < 300:
            error_code = "unknown"
            error_detail: str | None = None
            try:
                error_body = response.json()
                candidate = error_body.get("error", {}).get("code")
                if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9_]{1,64}", candidate):
                    error_code = candidate
                message = error_body.get("error", {}).get("message")
                if isinstance(message, str) and message.strip():
                    error_detail = message.strip()[:400]
            except (ValueError, AttributeError):
                pass
            logger.warning(
                "Read capability gateway rejected capability=%s status=%s code=%s input_keys=%s input_types=%s",
                capability_id, response.status_code, error_code, sorted(effective_input),
                {key: type(value).__name__ for key, value in sorted(effective_input.items())},
            )
            if error_code == "capability_input_invalid":
                # 契约校验失败是模型可自纠的错误：Node 侧在 message 里携带
                # 缺失/多余字段与可用字段全集，原样透传给工具层变成 guidance，
                # 模型一次失败即可按契约重试（而不是在同一槽位反复空转）。
                raise AgentCapabilityInvocationError(
                    "capability_input_invalid", error_detail
                )
            raise AgentCapabilityInvocationError("node_gateway_rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentCapabilityInvocationError("node_gateway_response_invalid") from exc
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("data"), dict)
            or not isinstance(payload.get("meta"), dict)
        ):
            raise AgentCapabilityInvocationError("node_gateway_response_invalid")
        result = payload["data"]
        context.successful_invocations.append({
            "capability_id": capability_id,
            "input": copy.deepcopy(effective_input),
            "output": copy.deepcopy(result),
        })
        return result

    async def create_action_checkpoint(
        self,
        capability_id: str,
        input: dict[str, Any],
        summary: dict[str, Any],
        *,
        business_workflow_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        context = self.current_context()
        if context is None:
            raise AgentCapabilityInvocationError("context_not_active")
        if capability_id not in context.plannable_allowlist:
            logger.warning(
                "Action checkpoint rejected: capability_id=%s not in plannable_allowlist=%s agent_run_id=%s",
                capability_id,
                sorted(context.plannable_allowlist),
                context.agent_run_id,
            )
            raise AgentCapabilityInvocationError("capability_not_allowed")
        if not isinstance(input, dict) or not isinstance(summary, dict):
            raise AgentCapabilityInvocationError("checkpoint_input_invalid")
        if (business_workflow_id is None) != (task_id is None):
            raise AgentCapabilityInvocationError("checkpoint_workflow_binding_invalid")
        if business_workflow_id is not None and (
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", business_workflow_id)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", task_id or "")
        ):
            raise AgentCapabilityInvocationError("checkpoint_workflow_binding_invalid")
        payload = {
            "agentRunId": context.agent_run_id,
            "capabilityId": capability_id,
            "input": input,
            "summary": summary,
        }
        if business_workflow_id is not None:
            payload.update({
                "businessWorkflowId": business_workflow_id,
                "taskId": task_id,
            })
        try:
            response = await self._client.post(
                self._checkpoint_endpoint,
                headers={"authorization": "Bearer " + self._service_token},
                json=payload,
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise AgentCapabilityInvocationError("node_gateway_unavailable") from exc
        if not 200 <= response.status_code < 300:
            error_code = "unknown"
            try:
                error_body = response.json()
                candidate = error_body.get("error", {}).get("code")
                if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9_]{1,64}", candidate):
                    error_code = candidate
            except (ValueError, AttributeError):
                pass
            logger.warning(
                "Checkpoint gateway rejected request status=%s code=%s capability=%s workflow_bound=%s input_keys=%s summary_keys=%s title_length=%s description_length=%s task_id_length=%s",
                response.status_code, error_code, capability_id,
                business_workflow_id is not None,
                sorted(input), sorted(summary),
                len(summary.get("title", "")) if isinstance(summary.get("title"), str) else -1,
                len(summary.get("description", "")) if isinstance(summary.get("description"), str) else -1,
                len(task_id or ""),
            )
            raise AgentCapabilityInvocationError("node_gateway_rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentCapabilityInvocationError("node_gateway_response_invalid") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise AgentCapabilityInvocationError("node_gateway_response_invalid")
        return payload["data"]

    async def persist_agent_workflow(
        self,
        *,
        agent_run_id: str,
        business_workflow_id: str,
        status: str,
        delegated_task_ids: list[str],
        expected_revision: int,
        envelope_revision: int,
        workflow_revision: int,
        todo_revision: int,
        todo_snapshot: list[dict[str, Any]],
        todo_digest: str,
        active_goal_task_id: str | None,
        active_todo_id: str | None,
        suspension_reason: str | None,
        last_outcome: dict[str, Any],
        pending_checkpoint_ids: list[str],
    ) -> dict[str, Any]:
        safe_id = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
        allowed_statuses = {
            "running", "awaiting_clarification", "awaiting_confirmation",
            "waiting_external", "completed", "partially_completed", "failed",
        }
        if not safe_id.fullmatch(agent_run_id or ""):
            raise AgentCapabilityInvocationError("agent_run_id_invalid")
        if not safe_id.fullmatch(business_workflow_id or ""):
            raise AgentCapabilityInvocationError("business_workflow_id_invalid")
        if status not in allowed_statuses:
            raise AgentCapabilityInvocationError("agent_workflow_status_invalid")
        if (
            not isinstance(expected_revision, int)
            or not 0 <= expected_revision <= 1_000_000
            or not isinstance(envelope_revision, int)
            or not 1 <= envelope_revision <= 1_000_000
            or not isinstance(workflow_revision, int)
            or workflow_revision != expected_revision + 1
            or not isinstance(todo_revision, int)
            or not 1 <= todo_revision <= 1_000_000
            or not 1 <= len(delegated_task_ids) <= 16
            or len(set(delegated_task_ids)) != len(delegated_task_ids)
            or any(not safe_id.fullmatch(item or "") for item in delegated_task_ids)
            or not 1 <= len(todo_snapshot) <= 128
            or len(pending_checkpoint_ids) > 16
            or len(set(pending_checkpoint_ids)) != len(pending_checkpoint_ids)
            or any(not safe_id.fullmatch(item or "") for item in pending_checkpoint_ids)
        ):
            raise AgentCapabilityInvocationError("agent_workflow_invalid")
        if (
            not re.fullmatch(r"[a-f0-9]{64}", todo_digest or "")
            or (active_goal_task_id is not None and (
                not safe_id.fullmatch(active_goal_task_id)
                or active_goal_task_id not in delegated_task_ids
            ))
            or (active_todo_id is not None and (
                active_goal_task_id is None
                or not safe_id.fullmatch(active_todo_id)
                or not _todo_belongs_to_delegated_task(
                    active_todo_id, delegated_task_ids
                )
            ))
            or suspension_reason not in {
                None, "awaiting_clarification", "awaiting_confirmation", "waiting_external"
            }
            or not isinstance(last_outcome, dict)
            or len(str(last_outcome)) > 8192
        ):
            raise AgentCapabilityInvocationError("agent_workflow_invalid")
        if status in {
            "running", "awaiting_clarification", "awaiting_confirmation", "waiting_external"
        } and (active_goal_task_id is None or active_todo_id is None):
            raise AgentCapabilityInvocationError("agent_workflow_invalid")
        if status == "completed" and (
            active_goal_task_id is not None or active_todo_id is not None
        ):
            raise AgentCapabilityInvocationError("agent_workflow_invalid")
        try:
            validated_todos = validate_authoritative_todo_snapshot(todo_snapshot)
        except ValueError:
            raise AgentCapabilityInvocationError("agent_workflow_invalid") from None
        if TodoLedger.digest(validated_todos) != todo_digest:
            raise AgentCapabilityInvocationError("agent_workflow_invalid")
        safe_todos: list[dict[str, Any]] = []
        for item in validated_todos:
            if not _todo_belongs_to_delegated_task(item.id, delegated_task_ids):
                raise AgentCapabilityInvocationError("agent_workflow_invalid")
            safe_todos.append({
                "id": item.id,
                "status": item.status,
                "dependsOn": list(item.depends_on),
            })
        body: dict[str, Any] = {
            "agentRunId": agent_run_id,
            "businessWorkflowId": business_workflow_id,
            "status": status,
            "delegatedTaskIds": list(delegated_task_ids),
            "expectedRevision": expected_revision,
            "envelopeRevision": envelope_revision,
            "workflowRevision": workflow_revision,
            "todoRevision": todo_revision,
            "todoSnapshot": safe_todos,
            "todoDigest": todo_digest,
            "activeGoalTaskId": active_goal_task_id,
            "activeTodoId": active_todo_id,
            "executionPolicy": "sequential",
            "suspensionReason": suspension_reason,
            "lastOutcome": copy.deepcopy(last_outcome),
            "pendingCheckpointIds": list(pending_checkpoint_ids),
        }

        async def _post(expected: int, revision: int):
            body["expectedRevision"] = expected
            body["workflowRevision"] = revision
            try:
                return await self._client.post(
                    self._workflow_endpoint,
                    headers={"authorization": "Bearer " + self._service_token},
                    json=body,
                    timeout=self._timeout_seconds,
                )
            except httpx.HTTPError as exc:
                raise AgentCapabilityInvocationError("node_gateway_unavailable") from exc

        response = await _post(expected_revision, workflow_revision)
        if response.status_code == 409:
            # 并发写者（如上一轮 run 的收尾持久化晚于本轮 restore）会使
            # 本轮加载到的 revision 在写入前过期——这是真实 CAS 竞争，
            # 不是调用方状态陈旧。按 CAS 写的标准做法：以 Node 当前
            # revision 为新基准有界重试一次，其余字段仍以调用方提供的
            # 权威投影为准（本轮的投影就是要落库的最新状态）。
            fresh: Any = None
            try:
                fresh = await self.load_agent_workflow(
                    agent_run_id=agent_run_id,
                    business_workflow_id=business_workflow_id,
                )
            except AgentCapabilityInvocationError:
                fresh = None
            if isinstance(fresh, dict) and isinstance(fresh.get("workflowRevision"), int):
                expected_revision = fresh["workflowRevision"]
                workflow_revision = expected_revision + 1
                response = await _post(expected_revision, workflow_revision)
        if response.status_code == 409:
            raise AgentCapabilityInvocationError("agent_workflow_revision_conflict")
        if not 200 <= response.status_code < 300:
            # 根因日志：上层只保留 node_gateway_rejected，排障需要真实拒绝原因。
            logger.warning(
                "agent workflow persistence rejected status=%s body=%s",
                response.status_code,
                response.text[:400],
            )
            raise AgentCapabilityInvocationError("node_gateway_rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentCapabilityInvocationError("node_gateway_response_invalid") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise AgentCapabilityInvocationError("node_gateway_response_invalid")
        data = payload["data"]
        if (
            set(data) != {
                "businessWorkflowId", "status", "workflowRevision", "todoRevision",
                "activeGoalTaskId",
            }
            or data["businessWorkflowId"] != business_workflow_id
            or data["status"] != status
            or data["workflowRevision"] != workflow_revision
            or data["todoRevision"] != todo_revision
            or data["activeGoalTaskId"] != active_goal_task_id
        ):
            raise AgentCapabilityInvocationError("node_gateway_response_invalid")
        return data

    async def load_agent_workflow(
        self,
        *,
        agent_run_id: str,
        business_workflow_id: str,
    ) -> dict[str, Any]:
        safe_id = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
        if not safe_id.fullmatch(agent_run_id or ""):
            raise AgentCapabilityInvocationError("agent_run_id_invalid")
        if not safe_id.fullmatch(business_workflow_id or ""):
            raise AgentCapabilityInvocationError("business_workflow_id_invalid")
        endpoint = (
            self._workflow_endpoint.rstrip("/")
            + "/"
            + quote(business_workflow_id, safe="")
        )
        try:
            response = await self._client.get(
                endpoint,
                headers={"authorization": "Bearer " + self._service_token},
                params={"agentRunId": agent_run_id},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise AgentCapabilityInvocationError("node_gateway_unavailable") from exc
        if response.status_code == 404:
            raise AgentCapabilityInvocationError("agent_workflow_not_found")
        if not 200 <= response.status_code < 300:
            raise AgentCapabilityInvocationError("node_gateway_rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentCapabilityInvocationError("node_gateway_response_invalid") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        expected_keys = {
            "businessWorkflowId", "status", "delegatedTaskIds", "envelopeRevision",
            "workflowRevision", "todoRevision", "todoSnapshot", "todoDigest",
            "activeGoalTaskId", "activeTodoId", "executionPolicy", "suspensionReason",
            "lastOutcome", "pendingCheckpointIds",
        }
        if not isinstance(data, dict) or set(data) != expected_keys:
            raise AgentCapabilityInvocationError("node_gateway_response_invalid")
        if (
            data["businessWorkflowId"] != business_workflow_id
            or data["executionPolicy"] != "sequential"
            or not all(
                isinstance(data[name], int) and 1 <= data[name] <= 1_000_000
                for name in ("envelopeRevision", "workflowRevision", "todoRevision")
            )
            or not isinstance(data.get("todoDigest"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", data["todoDigest"])
            or not isinstance(data["delegatedTaskIds"], list)
            or not isinstance(data["todoSnapshot"], list)
            or not isinstance(data["pendingCheckpointIds"], list)
            or not isinstance(data["lastOutcome"], dict)
        ):
            raise AgentCapabilityInvocationError("node_gateway_response_invalid")
        delegated_task_ids = data["delegatedTaskIds"]
        if (
            not 1 <= len(delegated_task_ids) <= 16
            or any(not isinstance(item, str) for item in delegated_task_ids)
            or len(delegated_task_ids) != len(set(delegated_task_ids))
            or any(not safe_id.fullmatch(item or "") for item in delegated_task_ids)
        ):
            raise AgentCapabilityInvocationError("node_gateway_response_invalid")
        try:
            validated_todos = validate_authoritative_todo_snapshot(
                data["todoSnapshot"]
            )
        except ValueError:
            raise AgentCapabilityInvocationError(
                "node_gateway_response_invalid"
            ) from None
        if (
            TodoLedger.digest(validated_todos) != data["todoDigest"]
            or any(
                not _todo_belongs_to_delegated_task(item.id, delegated_task_ids)
                for item in validated_todos
            )
        ):
            raise AgentCapabilityInvocationError("node_gateway_response_invalid")
        return copy.deepcopy(data)

    async def close(self) -> None:
        await self._client.aclose()


def create_read_capability_tool(invoker: AgentCapabilityInvoker):
    @tool(
        name="invoke_read_capability",
        description="调用当前 Agent Run 图谱绑定且服务端允许的只读银行能力。",
        input_params={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "capability_id": {
                    "type": "string",
                    "pattern": r"^bank\.[a-z0-9.-]+$",
                    "maxLength": 128,
                },
                "input": {"type": "object"},
            },
            "required": ["capability_id", "input"],
        },
        auto_extract=False,
    )
    async def invoke_read_capability(
        capability_id: str, input: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return await invoker.invoke_read_capability(capability_id, input)
        except AgentCapabilityInvocationError as exc:
            if exc.code == "capability_not_allowed":
                return {
                    "status": "error",
                    "errorCode": "capability_not_allowed",
                    "guidance": (
                        f"能力 {capability_id} 不在当前只读允许列表中。"
                        "该能力可能是需要确认的 plannable 能力，"
                        "不能通过 invoke_read_capability 直接调用。"
                        "请立即通过 submit_business_loop_outcome 提交 "
                        "status='awaiting_confirmation' 并结束本轮，"
                        "由平台创建确认检查点。"
                    ),
                }
            if exc.code == "capability_input_invalid":
                return {
                    "status": "error",
                    "errorCode": "capability_input_invalid",
                    "guidance": (
                        f"能力 {capability_id} 的输入未通过契约校验："
                        f"{exc.detail or '输入与契约不符'}。"
                        "请严格按上述契约字段重试；若缺少业务主键（如 payeeId、"
                        "accountId），先调用对应的列表/查询能力获取主键再重试，"
                        "不要自行编造字段名。"
                    ),
                }
            raise

    invoke_read_capability.card.parallel_safe = False
    invoke_read_capability.card.idempotent = True
    return invoke_read_capability


def create_authorized_capability_tool(invoker: AgentCapabilityInvoker):
    @tool(
        name="invoke_authorized_capability",
        description="消费本人确认后由服务端绑定的单次能力授权；不能传入或修改业务参数。",
        input_params={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "grant_id": {
                    "type": "string", "pattern": r"^acg_[A-Za-z0-9_-]+$",
                    "maxLength": 100,
                },
                "task_id": {
                    "type": "string", "pattern": r"^[A-Za-z0-9_.:-]+$",
                    "maxLength": 96,
                },
            },
            "required": ["grant_id", "task_id"],
        },
        auto_extract=False,
    )
    async def invoke_authorized_capability(
        grant_id: str, task_id: str
    ) -> dict[str, Any]:
        try:
            return await invoker.invoke_authorized_capability(grant_id, task_id)
        except AgentCapabilityInvocationError as exc:
            if exc.code == "authorized_context_not_active":
                return {
                    "status": "error",
                    "errorCode": "authorized_context_not_active",
                    "guidance": (
                        "当前阶段未激活授权上下文，invoke_authorized_capability "
                        "仅在 confirmation_resume 阶段可用。请立即通过 "
                        "submit_business_loop_outcome 提交 "
                        "status='awaiting_confirmation' 并结束本轮，"
                        "由平台创建确认检查点。"
                    ),
                }
            raise

    invoke_authorized_capability.card.parallel_safe = False
    invoke_authorized_capability.card.idempotent = True
    return invoke_authorized_capability
