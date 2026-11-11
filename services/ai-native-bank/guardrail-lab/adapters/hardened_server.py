"""Bootstrap the isolated upstream server with the local hardening overlay."""

from __future__ import annotations

import json
import os
import secrets
import sys
from typing import Annotated, Literal

import uvicorn
from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response


sys.path.insert(0, "/app")
sys.path.insert(0, "/opt/guardrail-adapters")

from hardened_runtime import (
    disable_third_party_telemetry,
    install_registration_hook,
    install_request_log_redaction,
    evaluate_action_rail,
    evaluate_input_rail,
    evaluate_output_rail,
    project_http_payload,
)
from finance_safety_policy import MAX_HISTORY_TURNS, policy_manifest


disable_third_party_telemetry()
install_registration_hook()
install_request_log_redaction()

from server import app
from src.api import state as api_state


class InputRailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(
        alias="requestId", pattern=r"^[A-Za-z0-9_.:-]+$", min_length=1, max_length=128
    )
    conversation_id: str = Field(
        alias="conversationId", pattern=r"^[A-Za-z0-9_.:-]+$", min_length=1, max_length=96
    )
    history: list[Annotated[str, Field(min_length=1, max_length=600)]] = Field(
        default_factory=list,
        max_length=MAX_HISTORY_TURNS,
    )


class OutputRailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=8000)
    request_id: str = Field(
        alias="requestId", pattern=r"^[A-Za-z0-9_.:-]+$", min_length=1, max_length=128
    )
    conversation_id: str = Field(
        alias="conversationId", pattern=r"^[A-Za-z0-9_.:-]+$", min_length=1, max_length=96
    )


class ActionRailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(
        alias="capabilityId", pattern=r"^bank\.[a-z0-9.-]+$", min_length=6, max_length=128
    )
    risk_level: Literal["L0", "L1", "L2", "L3"] = Field(alias="riskLevel")
    summary: str = Field(min_length=1, max_length=1000)
    requires_confirmation: bool = Field(alias="requiresConfirmation")
    confirmed: bool = False
    input_decision: Literal["allow", "review", "block", "unavailable"] = Field(
        alias="inputDecision"
    )


def require_internal_token(authorization: str | None = Header(default=None)) -> None:
    expected_token = os.environ.get("OPENAI_API_KEY", "")
    supplied = authorization or ""
    expected = "Bearer " + expected_token
    if not expected_token or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "服务身份无效"},
        )


@app.post("/v1/rails/input", dependencies=[Depends(require_internal_token)])
async def evaluate_input(payload: InputRailRequest) -> dict[str, object]:
    agent = getattr(api_state, "agent", None)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "guardrail_not_ready", "message": "安全服务尚未就绪"},
        )
    return await evaluate_input_rail(
        agent,
        message=payload.message,
        request_id=payload.request_id,
        conversation_id=payload.conversation_id,
        history=payload.history,
    )


@app.post("/v1/rails/output", dependencies=[Depends(require_internal_token)])
async def evaluate_output(payload: OutputRailRequest) -> dict[str, object]:
    agent = getattr(api_state, "agent", None)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "guardrail_not_ready", "message": "安全服务尚未就绪"},
        )
    return await evaluate_output_rail(
        agent,
        text=payload.text,
        request_id=payload.request_id,
        conversation_id=payload.conversation_id,
    )


@app.post("/v1/rails/action", dependencies=[Depends(require_internal_token)])
async def evaluate_action(payload: ActionRailRequest) -> dict[str, object]:
    return await evaluate_action_rail(
        capability_id=payload.capability_id,
        risk_level=payload.risk_level,
        summary=payload.summary,
        requires_confirmation=payload.requires_confirmation,
        confirmed=payload.confirmed,
        input_decision=payload.input_decision,
    )


@app.get("/v1/rails/status", dependencies=[Depends(require_internal_token)])
async def rail_status() -> dict[str, object]:
    return {
        "ready": getattr(api_state, "agent", None) is not None,
        "policy": policy_manifest(),
    }


@app.middleware("http")
async def add_four_state_chat_contract(request, call_next):
    response = await call_next(request)
    if request.url.path != "/chat" or response.status_code != 200:
        return response

    body = b"".join([chunk async for chunk in response.body_iterator])
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
        )
    if not isinstance(payload, dict):
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
        )

    projected = project_http_payload(payload)
    headers = dict(response.headers)
    headers.pop("content-length", None)
    headers.pop("content-type", None)
    return Response(
        content=json.dumps(projected, ensure_ascii=False),
        status_code=response.status_code,
        headers=headers,
        media_type="application/json",
        background=response.background,
    )


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        log_level="warning",
    )
