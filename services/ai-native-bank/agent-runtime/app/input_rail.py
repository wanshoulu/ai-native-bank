"""Bounded HTTP adapter for the framework-level input safety Rail."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from app.safety_gate import RailDecision


logger = logging.getLogger("ai_native_bank.agent_runtime.input_rail")

MAX_RAIL_RESPONSE_BYTES = 64 * 1024
TRANSIENT_STATUS_CODES = frozenset({429, 502, 503, 504})


def _unavailable(started: float, stage: str | None = None) -> RailDecision:
    return RailDecision(
        railId="finance-text-safety",
        stage=stage,
        decision="unavailable",
        reasonCode="guardrail_runtime_error",
        policyVersion="unavailable",
        responseKey="security.unavailable.default",
        durationMs=max(0, min(600_000, int((time.monotonic() - started) * 1000))),
    )


class DisabledInputRail:
    """Explicit development/off mode; never used as an implicit failure fallback."""

    async def evaluate(
        self,
        *,
        message: str,
        request_id: str,
        conversation_id: str,
    ) -> RailDecision:
        del message, request_id, conversation_id
        return RailDecision(
            railId="finance-text-safety",
            stage="input",
            decision="allow",
            reasonCode=None,
            policyVersion="disabled",
            responseKey=None,
            durationMs=0,
        )

    async def evaluate_input(
        self,
        *,
        message: str,
        request_id: str,
        conversation_id: str,
        history: list[str] | tuple[str, ...] | None = None,
    ) -> RailDecision:
        del history
        return await self.evaluate(
            message=message,
            request_id=request_id,
            conversation_id=conversation_id,
        )

    async def evaluate_output(
        self,
        *,
        text: str,
        request_id: str,
        conversation_id: str,
    ) -> RailDecision:
        del text, request_id, conversation_id
        return RailDecision(
            railId="finance-text-safety", stage="output", decision="allow",
            reasonCode=None, policyVersion="disabled", responseKey=None, durationMs=0,
        )

    async def evaluate_action(
        self,
        *,
        capability_id: str,
        risk_level: str,
        summary: str,
        requires_confirmation: bool,
        confirmed: bool,
        input_decision: str,
    ) -> RailDecision:
        del capability_id, risk_level, summary, requires_confirmation, confirmed, input_decision
        return RailDecision(
            railId="finance-text-safety", stage="action", decision="allow",
            reasonCode=None, policyVersion="disabled", responseKey=None, durationMs=0,
        )

    async def aclose(self) -> None:
        return None


class HttpInputRail:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_ms: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("input Rail URL is invalid")
        if not 16 <= len(service_token) <= 512 or any(
            character.isspace() for character in service_token
        ):
            raise ValueError("input Rail service identity is invalid")
        if not 500 <= timeout_ms <= 30_000:
            raise ValueError("input Rail timeout is invalid")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_ms / 1000),
            transport=transport,
            headers={
                "Authorization": "Bearer " + service_token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def evaluate(
        self,
        *,
        message: str,
        request_id: str,
        conversation_id: str,
    ) -> RailDecision:
        return await self.evaluate_input(
            message=message,
            request_id=request_id,
            conversation_id=conversation_id,
        )

    async def _evaluate_stage(
        self,
        *,
        stage: str,
        payload: dict[str, Any],
    ) -> RailDecision:
        started = time.monotonic()
        try:
            response: httpx.Response | None = None
            for attempt in range(2):
                response = await self._client.post(f"/v1/rails/{stage}", json=payload)
                if response.status_code not in TRANSIENT_STATUS_CODES or attempt == 1:
                    break
                await asyncio.sleep(0)
            if response is None or response.status_code != 200:
                return _unavailable(started, stage)
            if len(response.content) > MAX_RAIL_RESPONSE_BYTES:
                return _unavailable(started, stage)
            raw: Any = response.json()
            if not isinstance(raw, dict):
                return _unavailable(started, stage)
            decision = RailDecision.model_validate(raw)
            if decision.stage is not None and decision.stage != stage:
                return _unavailable(started, stage)
            return decision
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, UnicodeError, ValueError, ValidationError) as exc:
            logger.warning("%s Rail dependency failed: %s", stage.capitalize(), exc, exc_info=True)
            return _unavailable(started, stage)

    async def evaluate_input(
        self,
        *,
        message: str,
        request_id: str,
        conversation_id: str,
        history: list[str] | tuple[str, ...] | None = None,
    ) -> RailDecision:
        payload = {
            "message": message,
            "requestId": request_id,
            "conversationId": conversation_id,
        }
        if history:
            payload["history"] = list(history)[-6:]
        return await self._evaluate_stage(stage="input", payload=payload)

    async def evaluate_output(
        self,
        *,
        text: str,
        request_id: str,
        conversation_id: str,
    ) -> RailDecision:
        return await self._evaluate_stage(
            stage="output",
            payload={"text": text, "requestId": request_id, "conversationId": conversation_id},
        )

    async def evaluate_action(
        self,
        *,
        capability_id: str,
        risk_level: str,
        summary: str,
        requires_confirmation: bool,
        confirmed: bool,
        input_decision: str,
    ) -> RailDecision:
        return await self._evaluate_stage(
            stage="action",
            payload={
                "capabilityId": capability_id,
                "riskLevel": risk_level,
                "summary": summary,
                "requiresConfirmation": requires_confirmation,
                "confirmed": confirmed,
                "inputDecision": input_decision,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()
