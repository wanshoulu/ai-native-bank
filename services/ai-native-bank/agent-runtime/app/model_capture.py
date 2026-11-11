"""Operator-facing model call capture.

LLM 调用的输入/输出属于运维观测数据（LangSmith 式），与面向用户的
safe trace 严格分层：本模块独立采集、独立上报，绝不进入 trace.py 的
公开事件流。采集失败只降级记日志，绝不影响业务运行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
try:
    from openjiuwen.core.foundation.llm import (
        Model,
        ModelClientConfig,
        ModelRequestConfig,
    )
except ImportError:
    class Model:  # type: ignore[no-redef]
        """openjiuwen 不可用时的占位基类。"""
    class ModelClientConfig:  # type: ignore[no-redef]
        pass
    class ModelRequestConfig:  # type: ignore[no-redef]
        pass
from pydantic import BaseModel

logger = logging.getLogger("ai_native_bank.model_capture")

MAX_TEXT_BYTES = 16000
_MAX_DEPTH = 8
_MAX_ITEMS = 32
_SAFE_PHASE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
# 模型侧历史消息上限（网关 messages 上限之内的安全余量）。
MODEL_HISTORY_LIMIT = 160


def _message_role(message: Any) -> str:
    role = getattr(message, "role", None)
    return role if isinstance(role, str) else ""


def bound_model_history(messages: Any) -> Any:
    """确定性历史上限：超出时从最近的用户消息边界截断。

    会话跨轮持久化（task loop 多轮 ReAct + checkpointer）会让消息数无
    上限增长，最终撞网关的 messages 上限。截断只发生在用户消息边界
    （用户消息之前的交换必然完整闭合，不会产生悬空的 tool_calls/tool
    结果配对），系统消息始终保留。非序列或未超限时原样返回。
    """
    if not isinstance(messages, (list, tuple)):
        return messages
    if len(messages) <= MODEL_HISTORY_LIMIT:
        return messages
    system_prefix: list[Any] = []
    index = 0
    while index < len(messages) and _message_role(messages[index]) == "system":
        system_prefix.append(messages[index])
        index += 1
    body = list(messages[index:])
    budget = max(1, MODEL_HISTORY_LIMIT - len(system_prefix))
    if len(body) <= budget:
        return [*system_prefix, *body]
    # 在预算允许的最早切点起，找最近的用户消息边界。
    for cut in range(len(body) - budget, len(body)):
        if _message_role(body[cut]) == "user":
            return [*system_prefix, *body[cut:]]
    # 回退：非 tool 消息边界（首条非 tool 不会悬挂 tool 配对）。
    for cut in range(len(body) - budget, len(body)):
        if _message_role(body[cut]) != "tool":
            return [*system_prefix, *body[cut:]]
    return [*system_prefix, *body[-budget:]]

_run_scope: ContextVar[str | None] = ContextVar(
    "ai_bank_model_call_run_scope", default=None
)


def set_run_scope(agent_run_id: str):
    if not isinstance(agent_run_id, str) or not agent_run_id:
        raise ValueError("model_capture_run_id_invalid")
    return _run_scope.set(agent_run_id)


def clear_run_scope(token) -> None:
    _run_scope.reset(token)


def current_run_id() -> str | None:
    return _run_scope.get()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return str(value)[:256]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return _bounded(value.model_dump(exclude_none=True), depth + 1)
    if isinstance(value, dict):
        return {
            str(key)[:64]: _bounded(item, depth + 1)
            for key, item in list(value.items())[:_MAX_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth + 1) for item in list(value)[:_MAX_ITEMS]]
    return str(value)[:512]


def _truncate_utf8(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= MAX_TEXT_BYTES:
        return text
    return raw[: MAX_TEXT_BYTES - 16].decode("utf-8", errors="ignore") + "…[truncated]"


def _compact_json(value: Any) -> str:
    return _truncate_utf8(json.dumps(value, ensure_ascii=False, default=str))


def _render_messages(messages: Any) -> str:
    return _compact_json(_bounded(messages))


def _render_assistant(message: Any) -> str:
    return _compact_json(_bounded(message))


class ModelCallRecorder:
    """Bounded async reporter for one wrapped-model family."""

    def __init__(
        self,
        *,
        endpoint: str,
        service_token: str,
        model_name: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 5.0,
    ):
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("model_capture_endpoint_invalid")
        self._endpoint = endpoint
        self._service_token = service_token
        self._model_name = model_name
        self._client = client or httpx.AsyncClient()
        self._timeout_seconds = timeout_seconds
        self._fallback: Callable[[], str | None] | None = None

    def bind_run_id_fallback(self, resolver: Callable[[], str | None]) -> None:
        """Resolve run id from an active capability scope when no explicit scope is set."""
        self._fallback = resolver

    def _resolve_run_id(self) -> str | None:
        explicit = current_run_id()
        if explicit:
            return explicit
        if self._fallback is not None:
            try:
                return self._fallback()
            except Exception:
                return None
        return None

    async def record(
        self,
        *,
        phase: str,
        messages_text: str,
        response_text: str,
        tools_count: int,
        duration_ms: int,
        status: str,
    ) -> None:
        run_id = self._resolve_run_id()
        if not run_id:
            return
        payload = {
            "agentRunId": run_id,
            "phase": phase,
            "model": self._model_name,
            "promptJson": _truncate_utf8(messages_text),
            "responseText": _truncate_utf8(response_text),
            "toolsCount": tools_count,
            "durationMs": duration_ms,
            "status": status,
            "occurredAt": _now_iso(),
        }
        try:
            response = await self._client.post(
                self._endpoint,
                headers={"authorization": "Bearer " + self._service_token},
                json=payload,
                timeout=self._timeout_seconds,
            )
        except (httpx.HTTPError, asyncio.CancelledError) as exc:
            logger.warning(
                "Model call capture unavailable code=%s phase=%s",
                type(exc).__name__, phase,
            )
            return
        if not 200 <= response.status_code < 300:
            logger.warning(
                "Model call capture rejected status=%s phase=%s prompt_chars=%s response_chars=%s",
                response.status_code, phase, len(messages_text), len(response_text),
            )


class RecordingModel(Model):
    """Model decorator that reports every invoke/stream call for observability."""

    def __init__(
        self,
        client_config: ModelClientConfig,
        request_config: ModelRequestConfig,
        *,
        recorder: ModelCallRecorder,
        phase: str,
    ):
        super().__init__(client_config, request_config)
        if not _SAFE_PHASE.fullmatch(phase or ""):
            raise ValueError("model_capture_phase_invalid")
        self._recorder = recorder
        self._phase = phase

    async def invoke(
        self,
        messages=None,
        *,
        tools=None,
        **kwargs,
    ):
        started = time.monotonic()
        status = "ok"
        response_text = ""
        try:
            message = await super().invoke(
                bound_model_history(messages), tools=tools, **kwargs
            )
            response_text = _render_assistant(message)
            return message
        except BaseException as exc:
            status = "error"
            response_text = _compact_json({"errorType": type(exc).__name__})
            raise
        finally:
            await self._report(
                messages=messages,
                tools=tools,
                response_text=response_text,
                duration_ms=int((time.monotonic() - started) * 1000),
                status=status,
            )

    def stream(self, messages=None, *, tools=None, **kwargs):
        recorder = self._recorder
        phase = self._phase
        base_stream = super().stream

        async def wrapped():
            started = time.monotonic()
            status = "ok"
            content_parts: list[str] = []
            chunk_trace: list[Any] = []
            tool_calls: dict[int, dict] = {}
            bounded_messages = bound_model_history(messages)
            try:
                async for chunk in base_stream(bounded_messages, tools=tools, **kwargs):
                    if len(chunk_trace) < 200:
                        text = getattr(chunk, "content", None)
                        if isinstance(text, str):
                            content_parts.append(text)
                        else:
                            chunk_trace.append(_bounded(chunk))
                    for fragment in getattr(chunk, "tool_calls", None) or []:
                        index = getattr(fragment, "index", 0) or 0
                        slot = tool_calls.setdefault(
                            index, {"index": index, "id": "", "name": "", "arguments": ""}
                        )
                        fragment_id = getattr(fragment, "id", "") or ""
                        fragment_name = getattr(fragment, "name", "") or ""
                        fragment_args = getattr(fragment, "arguments", "") or ""
                        if fragment_id and not slot["id"]:
                            slot["id"] = fragment_id
                        if fragment_name and not slot["name"]:
                            slot["name"] = fragment_name
                        slot["arguments"] += fragment_args
                    yield chunk
            except BaseException as exc:
                status = "error"
                content_parts.append("«" + type(exc).__name__ + "»")
                raise
            finally:
                response_text = "".join(content_parts)
                if tool_calls:
                    assembled = [tool_calls[key] for key in sorted(tool_calls)]
                    for call in assembled:
                        if len(call["arguments"]) > 2048:
                            call["arguments"] = call["arguments"][:2048] + "…[truncated]"
                    response_text += "\n" + _compact_json({"toolCalls": assembled})
                if chunk_trace:
                    response_text += "\n" + _compact_json(chunk_trace)
                await recorder.record(
                    phase=phase,
                    messages_text=_render_messages(messages),
                    response_text=_truncate_utf8(response_text),
                    tools_count=len(tools) if tools else 0,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    status=status,
                )

        return wrapped()

    async def _report(
        self,
        *,
        messages,
        tools,
        response_text: str,
        duration_ms: int,
        status: str,
    ) -> None:
        try:
            await self._recorder.record(
                phase=self._phase,
                messages_text=_render_messages(messages),
                response_text=response_text,
                tools_count=len(tools) if tools else 0,
                duration_ms=duration_ms,
                status=status,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Model call capture failed code=record_error phase=%s", self._phase)
