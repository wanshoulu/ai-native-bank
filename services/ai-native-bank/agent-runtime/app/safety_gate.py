"""Framework-level safety gate contracts and transactional run attempts."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal

from pydantic import Field, model_validator

from app.contracts import FrozenStrictModel, StrictModel


RailDisposition = Literal["allow", "review", "block", "unavailable"]
PermitScope = Literal[
    "user_agent.commit",
    "user_agent.clarify",
    "a2a.delegate",
]


class PermitValidationError(ValueError):
    """A permit cannot authorize the requested framework transition."""


class RunAttemptStateError(RuntimeError):
    """A RunAttempt received an invalid or conflicting state transition."""


class RailDecision(FrozenStrictModel):
    """Value-free decision returned by one registered input Rail."""

    rail_id: str = Field(
        alias="railId", pattern=r"^[a-z0-9][a-z0-9.-]{0,95}$"
    )
    stage: Literal["input", "output", "action"] | None = None
    decision: RailDisposition
    reason_code: str | None = Field(
        default=None,
        alias="reasonCode",
        pattern=r"^[a-z][a-z0-9_]{1,95}$",
    )
    policy_version: str = Field(
        alias="policyVersion",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$",
    )
    response_key: str | None = Field(
        default=None,
        alias="responseKey",
        pattern=r"^[a-z][a-z0-9_.-]{1,95}$",
    )
    duration_ms: int = Field(alias="durationMs", ge=0, le=600_000)

    @model_validator(mode="after")
    def validate_disposition_shape(self) -> "RailDecision":
        if self.decision == "allow":
            if self.reason_code is not None or self.response_key is not None:
                raise ValueError("allow decision cannot carry reason or response")
        elif self.decision == "review":
            if self.reason_code is None or self.response_key is not None:
                raise ValueError("review requires a reason and no Rail response")
        elif self.reason_code is None or self.response_key is None:
            raise ValueError("terminal Rail decision requires reason and response")
        return self


class _PermitBase(FrozenStrictModel):
    permit_id: str = Field(
        alias="permitId", pattern=r"^permit_[A-Za-z0-9_-]{8,96}$"
    )
    request_id: str = Field(alias="requestId", min_length=1, max_length=128)
    conversation_id: str = Field(
        alias="conversationId", min_length=1, max_length=96
    )
    session_revision: int = Field(alias="sessionRevision", ge=0)
    policy_version: str = Field(
        alias="policyVersion",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$",
    )
    expires_at: datetime = Field(alias="expiresAt")
    scopes: frozenset[PermitScope]

    def allows(self, scope: str) -> bool:
        return scope in self.scopes

    def validate_binding(
        self,
        *,
        request_id: str,
        conversation_id: str,
        session_revision: int,
        required_scope: str,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(timezone.utc)
        expiry = self.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if current >= expiry:
            raise PermitValidationError("execution permit expired")
        if request_id != self.request_id:
            raise PermitValidationError("execution permit request mismatch")
        if conversation_id != self.conversation_id:
            raise PermitValidationError("execution permit conversation mismatch")
        if session_revision != self.session_revision:
            raise PermitValidationError("execution permit revision mismatch")
        if required_scope not in self.scopes:
            raise PermitValidationError("execution permit scope denied")


class ExecutionPermit(_PermitBase):
    permit_type: Literal["execution"] = Field(
        default="execution", alias="permitType"
    )
    scopes: frozenset[PermitScope] = Field(
        default=frozenset({"user_agent.commit", "a2a.delegate"})
    )

    @model_validator(mode="after")
    def validate_scopes(self) -> "ExecutionPermit":
        if self.scopes != frozenset({"user_agent.commit", "a2a.delegate"}):
            raise ValueError("execution permit scopes are fixed")
        return self

    @classmethod
    def issue(
        cls,
        *,
        request_id: str,
        conversation_id: str,
        session_revision: int,
        rail_decision: RailDecision,
        ttl_seconds: int,
    ) -> "ExecutionPermit":
        if rail_decision.decision != "allow":
            raise PermitValidationError("execution permit requires allow")
        if not 5 <= ttl_seconds <= 120:
            raise PermitValidationError("execution permit ttl is invalid")
        return cls(
            permitId="permit_" + uuid.uuid4().hex,
            requestId=request_id,
            conversationId=conversation_id,
            sessionRevision=session_revision,
            policyVersion=rail_decision.policy_version,
            expiresAt=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        )


class ClarificationPermit(_PermitBase):
    permit_type: Literal["clarification"] = Field(
        default="clarification", alias="permitType"
    )
    scopes: frozenset[PermitScope] = Field(
        default=frozenset({"user_agent.clarify"})
    )

    @model_validator(mode="after")
    def validate_scopes(self) -> "ClarificationPermit":
        if self.scopes != frozenset({"user_agent.clarify"}):
            raise ValueError("clarification permit scopes are fixed")
        return self

    @classmethod
    def issue(
        cls,
        *,
        request_id: str,
        conversation_id: str,
        session_revision: int,
        rail_decision: RailDecision,
        ttl_seconds: int,
    ) -> "ClarificationPermit":
        if rail_decision.decision != "review":
            raise PermitValidationError("clarification permit requires review")
        if not 5 <= ttl_seconds <= 120:
            raise PermitValidationError("clarification permit ttl is invalid")
        return cls(
            permitId="permit_" + uuid.uuid4().hex,
            requestId=request_id,
            conversationId=conversation_id,
            sessionRevision=session_revision,
            policyVersion=rail_decision.policy_version,
            expiresAt=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        )


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class RunAttempt:
    """Own one buffered Agent result and exactly one terminal transition."""

    def __init__(
        self,
        *,
        attempt_id: str,
        commit_callback: Callable[[Any, ExecutionPermit], Any | Awaitable[Any]],
        rollback_callback: Callable[[], Any | Awaitable[Any]],
        clarification_callback: Callable[
            [Any, RailDecision, ClarificationPermit], Any | Awaitable[Any]
        ] | None = None,
    ) -> None:
        if not attempt_id or len(attempt_id) > 160:
            raise ValueError("attempt id is invalid")
        self.attempt_id = attempt_id
        self._commit_callback = commit_callback
        self._rollback_callback = rollback_callback
        self._clarification_callback = clarification_callback
        self._state: Literal[
            "running", "buffered", "committed", "clarified", "rolled_back"
        ] = (
            "running"
        )
        self._buffer: Any = None
        self._terminal_result: Any = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    def buffer(self, value: Any) -> None:
        if self._state not in {"running", "buffered"}:
            raise RunAttemptStateError("terminal attempt cannot accept a result")
        if self._state == "buffered":
            raise RunAttemptStateError("attempt already has a result")
        self._buffer = value
        self._state = "buffered"

    async def commit(
        self,
        rail_decision: RailDecision,
        permit: ExecutionPermit,
    ) -> Any:
        if rail_decision.decision != "allow":
            raise RunAttemptStateError("attempt commit requires allow")
        if (
            permit.policy_version != rail_decision.policy_version
            or not permit.allows("user_agent.commit")
        ):
            raise RunAttemptStateError("attempt commit permit is invalid")
        async with self._lock:
            if self._state == "committed":
                return self._terminal_result
            if self._state != "buffered":
                raise RunAttemptStateError("attempt has no committable result")
            result = await _await_if_needed(
                self._commit_callback(self._buffer, permit)
            )
            self._terminal_result = result
            self._state = "committed"
            return result

    async def clarify(
        self,
        rail_decision: RailDecision,
        permit: ClarificationPermit,
    ) -> Any:
        if rail_decision.decision != "review":
            raise RunAttemptStateError("attempt clarification requires review")
        if (
            permit.policy_version != rail_decision.policy_version
            or not permit.allows("user_agent.clarify")
            or self._clarification_callback is None
        ):
            raise RunAttemptStateError("attempt clarification permit is invalid")
        async with self._lock:
            if self._state == "clarified":
                return self._terminal_result
            if self._state != "buffered":
                raise RunAttemptStateError("attempt has no clarifiable result")
            result = await _await_if_needed(
                self._clarification_callback(self._buffer, rail_decision, permit)
            )
            self._terminal_result = result
            self._state = "clarified"
            return result

    async def rollback(self) -> None:
        async with self._lock:
            if self._state == "rolled_back":
                return
            if self._state in {"committed", "clarified"}:
                raise RunAttemptStateError("completed attempt cannot roll back")
            await _await_if_needed(self._rollback_callback())
            self._buffer = None
            self._state = "rolled_back"


def aggregate_rail_decisions(decisions: list[RailDecision]) -> RailDecision:
    if not decisions:
        raise ValueError("at least one Rail decision is required")
    priority = {"allow": 0, "review": 1, "unavailable": 2, "block": 3}
    return max(decisions, key=lambda item: priority[item.decision])


@dataclass(frozen=True)
class GateOutcome:
    decision: RailDecision
    result: Any = None
    execution_permit: ExecutionPermit | None = None
    clarification_permit: ClarificationPermit | None = None


class SafetyGatedRunCoordinator:
    """Join one input Rail and one speculative User Agent attempt safely."""

    def __init__(self, *, ttl_seconds: int) -> None:
        if not 5 <= ttl_seconds <= 120:
            raise ValueError("permit ttl is invalid")
        self._ttl_seconds = ttl_seconds

    async def run(
        self,
        *,
        rail: Any,
        agent_call: Callable[[], Awaitable[Any]],
        attempt: RunAttempt,
        message: str,
        request_id: str,
        conversation_id: str,
        session_revision: int,
    ) -> GateOutcome:
        rail_task = asyncio.create_task(
            rail.evaluate(
                message=message,
                request_id=request_id,
                conversation_id=conversation_id,
            )
        )
        agent_task = asyncio.create_task(agent_call())
        try:
            try:
                rail_decision = await rail_task
                if not isinstance(rail_decision, RailDecision):
                    raise TypeError("Rail returned an invalid decision")
            except asyncio.CancelledError:
                raise
            except Exception:
                rail_decision = RailDecision(
                    railId="finance-text-safety",
                    decision="unavailable",
                    reasonCode="guardrail_runtime_error",
                    policyVersion="unavailable",
                    responseKey="security.unavailable.default",
                    durationMs=0,
                )

            if rail_decision.decision in {"block", "unavailable"}:
                if not agent_task.done():
                    agent_task.cancel()
                await asyncio.gather(agent_task, return_exceptions=True)
                await attempt.rollback()
                return GateOutcome(decision=rail_decision)

            try:
                agent_result = await agent_task
            except asyncio.CancelledError:
                raise
            except Exception:
                await attempt.rollback()
                raise
            attempt.buffer(agent_result)

            if rail_decision.decision == "review":
                clarification_permit = ClarificationPermit.issue(
                    request_id=request_id,
                    conversation_id=conversation_id,
                    session_revision=session_revision,
                    rail_decision=rail_decision,
                    ttl_seconds=self._ttl_seconds,
                )
                result = await attempt.clarify(
                    rail_decision,
                    clarification_permit,
                )
                return GateOutcome(
                    decision=rail_decision,
                    result=result,
                    clarification_permit=clarification_permit,
                )

            execution_permit = ExecutionPermit.issue(
                request_id=request_id,
                conversation_id=conversation_id,
                session_revision=session_revision,
                rail_decision=rail_decision,
                ttl_seconds=self._ttl_seconds,
            )
            result = await attempt.commit(rail_decision, execution_permit)
            return GateOutcome(
                decision=rail_decision,
                result=result,
                execution_permit=execution_permit,
            )
        except asyncio.CancelledError:
            for task in (rail_task, agent_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(rail_task, agent_task, return_exceptions=True)
            if attempt.state not in {"committed", "clarified", "rolled_back"}:
                await attempt.rollback()
            raise
        finally:
            for task in (rail_task, agent_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(rail_task, agent_task, return_exceptions=True)
