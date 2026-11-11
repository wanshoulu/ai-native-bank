"""Private FastAPI surface for the AI-native bank Agent Runtime."""

from __future__ import annotations

import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import RuntimeNotReadyError, RuntimeSettings
from app.contracts import (
    AgentSessionReleaseRequest,
    BusinessWorkflowResumeRequest,
    BusinessWorkflowResumeResponse,
    InternalIntentEvaluateRequest,
    RuntimeAgentRunRequest,
    RuntimeAgentRunResponse,
)
from app.deepagent_factory import DeepAgentFactory
from app.logging_security import harden_framework_logging
from app.service import AgentContractError, DualDeepAgentService
from app.intent_graph import IntentGraphError, IntentGraphRepository
from app.faq import FaqError, MockFaqRepository
from app.input_rail import DisabledInputRail, HttpInputRail
from app.lexicon_rail import ChainedInputRail, LexiconInputRail
from app.openjiuwen_checkpointer import (
    CheckpointerInitializationError,
    OpenJiuwenCheckpointerManager,
)
from app.memory_runtime import (
    HttpMemoryGateway,
    IntentMemoryPolicyRegistry,
    KnowledgeGraphGateway,
    UserMemoryPolicyRegistry,
)
from app.memory_write_gateway import MemoryWriteGateway


logger = logging.getLogger("ai_native_bank.agent_runtime")


MAX_BODY_BYTES = 256 * 1024


def _load_intent_graph(graph_path: str, runtime_settings: "RuntimeSettings"):
    """Load intent graph, auto-detecting V2 vs V4 format.

    V2: JSON with "nodes"/"edges" keys → IntentGraphRepository
    V4: JSON with "intentTree" key → V4GraphAdapter (implements same interface)

    intent_graph_source=tugraph loads the V4 payload from TuGraph
    (Bolt) instead of the JSON file; falls back to the file on any failure.
    """
    from pathlib import Path

    path = Path(graph_path)
    if runtime_settings.intent_graph_source == "tugraph":
        try:
            from app.tugraph_source import load_v4_payload_from_tugraph

            payload = load_v4_payload_from_tugraph(runtime_settings)
            logger.info("intent graph source: TuGraph")
        except Exception:
            logger.exception(
                "TuGraph intent graph load failed; falling back to file %s", graph_path
            )
            payload = None
        if payload is not None:
            from app.v4_graph_adapter import V4GraphAdapter

            return V4GraphAdapter(payload, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "intentTree" in payload:
        from app.v4_graph_adapter import V4GraphAdapter

        return V4GraphAdapter(payload, path)
    return IntentGraphRepository.load(path)


def _initialize_runtime_service(
    runtime_settings: RuntimeSettings,
    session_lifecycle: OpenJiuwenCheckpointerManager,
) -> DualDeepAgentService:
    intent_graph = _load_intent_graph(runtime_settings.intent_graph_path, runtime_settings)
    faq_repository = MockFaqRepository.load(
        runtime_settings.faq_path,
        valid_action_keys=intent_graph.action_keys,
    )
    for faq_entry in faq_repository.entries:
        try:
            binding = intent_graph.candidate_for_intent(
                faq_entry.intent_id,
                score=240,
                evidence=["faq_pattern"],
            )
        except IntentGraphError as exc:
            raise FaqError("FAQ intent binding is missing") from exc
        if binding.execution_mode != "knowledge-only":
            raise FaqError("FAQ intent binding must be knowledge-only")
        if binding.required_slots or binding.capability_bindings:
            raise FaqError("FAQ intent binding must be isolated")
    # DeepAgent bundle：openjiuwen 不可用时降级（仅意图评测可用）
    user_runner = None
    business_runner = None
    capability_invoker = None
    try:
        bundle = DeepAgentFactory(
            runtime_settings,
            session_lifecycle=session_lifecycle,
        ).create()
        user_runner = bundle.user
        business_runner = bundle.business
        capability_invoker = bundle.capability_invoker
    except Exception:
        logger.warning(
            "DeepAgentFactory unavailable — agent runs disabled, "
            "intent evaluation still available"
        )
    input_rail = (
        DisabledInputRail()
        if runtime_settings.input_rail_mode == "off"
        else ChainedInputRail(
            [
                LexiconInputRail(runtime_settings.lexicon_dir),
                HttpInputRail(
                    base_url=runtime_settings.input_rail_url,
                    service_token=runtime_settings.runtime_token,
                    timeout_ms=runtime_settings.input_rail_timeout_ms,
                ),
            ]
        )
    )
    harden_framework_logging()
    memory_gateway = None
    memory_write_gateway = None
    memory_policies = None
    memory_user_policies = None
    if any(mode != "off" for mode in (
        runtime_settings.memory_mode,
        runtime_settings.user_memory_mode,
        runtime_settings.task_memory_mode,
    )):
        if not runtime_settings.memory_service_token:
            raise RuntimeNotReadyError("memory_service_token_not_configured")
        memory_policies = IntentMemoryPolicyRegistry.load(
            runtime_settings.intent_memory_policy_path
        )
        memory_user_policies = UserMemoryPolicyRegistry.load(
            runtime_settings.user_memory_policy_path
        )
        kg_gateway = KnowledgeGraphGateway(
            base_url=runtime_settings.kg_gateway_url,
            graph_id=runtime_settings.kg_graph_id,
            timeout_seconds=runtime_settings.kg_timeout_ms / 1000,
        )
        memory_gateway = HttpMemoryGateway(
            base_url=runtime_settings.memory_control_url,
            service_token=runtime_settings.memory_service_token,
            policies=memory_policies,
            user_policies=memory_user_policies,
            timeout_seconds=runtime_settings.memory_timeout_ms / 1000,
            mode=runtime_settings.task_memory_mode,
            user_mode=runtime_settings.user_memory_mode,
            kg_gateway=kg_gateway,
        )
        if "governed_write" in {
            runtime_settings.memory_mode,
            runtime_settings.user_memory_mode,
            runtime_settings.task_memory_mode,
        }:
            memory_write_gateway = MemoryWriteGateway(
                endpoint=runtime_settings.node_memory_command_url,
                service_token=runtime_settings.runtime_token,
                timeout_seconds=runtime_settings.memory_timeout_ms / 1000,
            )
    return DualDeepAgentService(
        user_runner,
        business_runner,
        intent_graph,
        capability_invoker=capability_invoker,
        faq_repository=faq_repository,
        input_rail=input_rail,
        input_rail_mode=runtime_settings.input_rail_mode,
        permit_ttl_seconds=runtime_settings.permit_ttl_seconds,
        memory_gateway=memory_gateway,
        memory_write_gateway=memory_write_gateway,
        memory_policies=memory_policies,
        memory_user_policies=memory_user_policies,
    )


class BodyLimitMiddleware:
    def __init__(self, app: Any, maximum_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.maximum_bytes = maximum_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = 0
        if content_length > self.maximum_bytes:
            response = JSONResponse(
                status_code=413,
                content={"detail": {"code": "request_body_too_large", "message": "请求体超过大小限制"}},
            )
            await response(scope, receive, send)
            return

        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.maximum_bytes:
                    raise HTTPException(status_code=413, detail="request_body_too_large")
            return message

        try:
            await self.app(scope, limited_receive, send)
        except HTTPException as exc:
            if exc.status_code != 413:
                raise
            response = JSONResponse(
                status_code=413,
                content={"detail": {"code": "request_body_too_large", "message": "请求体超过大小限制"}},
            )
            await response(scope, receive, send)


def create_app(
    settings: RuntimeSettings | None = None,
    *,
    service: DualDeepAgentService | None = None,
    checkpointer_manager: OpenJiuwenCheckpointerManager | None = None,
) -> FastAPI:
    runtime_settings = settings or RuntimeSettings.from_env()
    manager = checkpointer_manager or OpenJiuwenCheckpointerManager(
        runtime_settings.checkpointer_path
    )
    startup_error: str | None = None
    if not runtime_settings.model_configured:
        startup_error = "model_not_configured"
    elif not runtime_settings.runtime_token:
        startup_error = "runtime_token_not_configured"

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if application.state.startup_error is None:
            try:
                await manager.install()
                application.state.checkpointer_ready = True
            except CheckpointerInitializationError:
                application.state.checkpointer_ready = False
                logger.error("OpenJiuwen checkpointer initialization failed")
            except Exception:
                application.state.checkpointer_ready = False
                logger.error("OpenJiuwen checkpointer initialization failed")

            if application.state.runtime_service is None:
                try:
                    application.state.runtime_service = _initialize_runtime_service(
                        runtime_settings,
                        manager,
                    )
                    if hasattr(application.state.runtime_service, "prewarm_semantic_recall"):
                        application.state.runtime_service.prewarm_semantic_recall()
                except RuntimeNotReadyError as exc:
                    code = str(exc)
                    application.state.startup_error = (
                        code
                        if code in {
                            "model_not_configured",
                            "memory_service_token_not_configured",
                        }
                        else "framework_initialization_failed"
                    )
                except IntentGraphError:
                    application.state.startup_error = "intent_graph_invalid"
                except FaqError:
                    application.state.startup_error = "faq_repository_invalid"
                except Exception:
                    logger.error(
                        "Runtime service initialization failed",
                        exc_info=True,
                    )
                    application.state.startup_error = "framework_initialization_failed"
        yield

    app = FastAPI(
        title="AI Native Bank Agent Runtime",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(BodyLimitMiddleware)
    app.state.startup_error = startup_error
    app.state.runtime_service = service
    app.state.checkpointer_manager = manager
    app.state.checkpointer_ready = False

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, _exc: RequestValidationError):
        logger.warning(
            "Runtime request validation rejected errors=%s",
            [
                {
                    "loc": [str(part) for part in item.get("loc", ())],
                    "type": str(item.get("type", "validation_error")),
                }
                for item in _exc.errors()
            ],
        )
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "validation_error", "message": "请求格式不合法"}},
        )

    @app.exception_handler(AgentContractError)
    async def contract_error_handler(_request: Request, _exc: AgentContractError):
        logger.warning("Agent contract rejected: %s", _exc)
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "agent_contract_violation", "message": "智能体结构化结果不合法"}},
        )

    def require_service_token(authorization: str | None = Header(default=None)) -> None:
        if not runtime_settings.runtime_token:
            raise HTTPException(
                status_code=503,
                detail={"code": "runtime_token_not_configured", "message": "运行时尚未就绪"},
            )
        supplied = authorization or ""
        expected = "Bearer " + runtime_settings.runtime_token
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "unauthorized", "message": "服务身份无效"},
            )

    @app.get("/health")
    async def health() -> dict[str, object]:
        status_payload = runtime_settings.public_status(
            framework_available=(
                app.state.runtime_service is not None
                and app.state.startup_error is None
            ),
            checkpointer_available=app.state.checkpointer_ready,
        )
        status_payload["runtimeState"] = (
            "ready"
            if status_payload["ready"]
            else (app.state.startup_error or "not_ready")
        )
        return status_payload

    @app.post(
        "/v1/agent-sessions/release",
        status_code=200,
        dependencies=[Depends(require_service_token)],
    )
    async def release_agent_sessions(
        payload: AgentSessionReleaseRequest,
    ) -> dict[str, int]:
        try:
            await app.state.checkpointer_manager.release(payload.session_ids)
        except Exception:
            logger.warning("OpenJiuwen agent-session release failed")
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "agent_session_release_failed",
                    "message": "Agent session release failed",
                },
            ) from None
        return {"released": len(set(payload.session_ids))}

    @app.post(
        "/v1/agent-runs",
        response_model=RuntimeAgentRunResponse,
        response_model_by_alias=True,
        # Keep the established compact wire shape. Security terminal responses
        # set UserResponse.items/actions explicitly, so their required empty
        # arrays survive this filter without expanding unrelated payloads.
        response_model_exclude_unset=True,
        status_code=201,
        dependencies=[Depends(require_service_token)],
    )
    async def create_agent_run(
        payload: RuntimeAgentRunRequest, request: Request
    ) -> RuntimeAgentRunResponse | StreamingResponse:
        runtime_service = app.state.runtime_service
        if app.state.startup_error or runtime_service is None:
            code = app.state.startup_error or "runtime_unavailable"
            raise HTTPException(
                status_code=503,
                detail={"code": code, "message": "运行时尚未就绪"},
            )
        if "text/event-stream" in request.headers.get("accept", "").lower():
            async def event_stream():
                trace_count = 0
                try:
                    if hasattr(runtime_service, "stream_with_result"):
                        async for item, result in runtime_service.stream_with_result(payload):
                            trace_count += 1
                            body = item.model_dump_json(by_alias=True, exclude_none=True)
                            yield f"event: agent.trace\ndata: {body}\n\n".encode("utf-8")
                            if result is not None:
                                if result.conversation_state is not None:
                                    private_state = result.conversation_state.model_dump_json(
                                        by_alias=True,
                                        exclude_none=False,
                                        exclude=(
                                            {
                                                "pending_envelope_id", "envelope_revision",
                                                "intent_shape", "goals",
                                                "shared_slots", "ambiguities",
                                            }
                                            if result.conversation_state.schema_version == "1.0"
                                            else {"pending_intent_id", "slots"}
                                        ),
                                    )
                                    yield f"event: agent.state\ndata: {private_state}\n\n".encode("utf-8")
                                delivery = result.model_dump_json(
                                    by_alias=True,
                                    exclude_none=True,
                                    include={
                                        "agent_run_id",
                                        "status",
                                        "user_result",
                                        "user_response",
                                        "user_responses",
                                        "confirmation",
                                        "clarification_form",
                                    },
                                )
                                yield f"event: agent.result\ndata: {delivery}\n\n".encode("utf-8")
                    else:
                        async for item in runtime_service.stream(payload):
                            trace_count += 1
                            body = item.model_dump_json(by_alias=True, exclude_none=True)
                            yield f"event: agent.trace\ndata: {body}\n\n".encode("utf-8")
                except Exception:
                    logger.exception(
                        "event_stream unhandled exception agent_run_id=%s trace_id=%s",
                        payload.agent_run_id,
                        payload.trace_id,
                    )
                    error_payload = json.dumps({
                        "schemaVersion": "1.0",
                        "sequence": trace_count + 1,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "stage": "run.failed",
                        "status": "failed",
                        "durationMs": 0,
                        "agentRunId": payload.agent_run_id,
                        "traceId": payload.trace_id,
                        "summary": {
                            "title": "智能服务运行失败",
                            "input": [],
                            "output": ["errorCode"],
                            "metrics": {"failedStage": "stream_error"},
                        },
                        "error": {
                            "code": "agent_runtime_failed",
                            "message": "智能服务事件流异常",
                        },
                    })
                    yield f"event: agent.trace\ndata: {error_payload}\n\n".encode("utf-8")

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                },
            )
        result = await runtime_service.run(payload)
        # Serialize from the already validated runtime contract so nested
        # model_fields_set semantics are preserved: explicit clears stay null,
        # empty action payloads stay {}, and explicitly required empty
        # UserResponse arrays stay present.
        delivery = result.model_dump(by_alias=True, exclude_unset=True)
        response_items = []
        if isinstance(delivery.get("userResponse"), dict):
            response_items.append(delivery["userResponse"])
        if isinstance(delivery.get("userResponses"), list):
            response_items.extend(
                item for item in delivery["userResponses"] if isinstance(item, dict)
            )
        for user_response in response_items:
            # These arrays are required by the browser/Node wire contract even
            # when the validated model obtained them from default factories.
            user_response.setdefault("items", [])
            user_response.setdefault("actions", [])
        return JSONResponse(status_code=201, content=delivery)

    @app.get(
        "/v1/internal/intent-recall",
        dependencies=[Depends(require_service_token)],
    )
    async def intent_recall_probe(
        message: str, tab: str | None = None, menuKey: str | None = None
    ) -> dict[str, object]:
        """只读意图召回探针（评估观测面，不触发规划与业务执行）。"""
        runtime_service = app.state.runtime_service
        if app.state.startup_error or runtime_service is None:
            code = app.state.startup_error or "runtime_unavailable"
            raise HTTPException(
                status_code=503,
                detail={"code": code, "message": "运行时尚未就绪"},
            )
        if not message.strip() or len(message) > 200:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_message", "message": "探针消息非法"},
            )
        candidates = await runtime_service.probe_intent_recall(
            message, {"tab": tab, "menuKey": menuKey}
        )
        graph_version, graph_digest = runtime_service.intent_graph_identity
        return {
            "schemaVersion": "1.0",
            "graphVersion": graph_version,
            "graphDigest": graph_digest,
            "candidates": candidates,
        }

    @app.post(
        "/v1/internal/intent-evaluate",
        dependencies=[Depends(require_service_token)],
    )
    async def intent_evaluate_ladder(
        payload: InternalIntentEvaluateRequest,
    ) -> dict[str, object]:
        """只读评测阶梯（评估观测面）：复刻生产意图识别确定性链路（形态判定 →
        图谱召回 → 向量语义拓宽 → LLM 候选内裁决），无业务副作用。

        LLM 裁决按 evaluate_intent_ladder 的条件触发（non_task 跳过；
        single_intent 弱召回或强竞争触发；multi_intent / ambiguous_intent /
        same_intent_multi_task 始终触发）。无 LLM 裁决结果时引擎回退到
        shapeAuthoritative 收敛。"""
        runtime_service = app.state.runtime_service
        if app.state.startup_error or runtime_service is None:
            code = app.state.startup_error or "runtime_unavailable"
            raise HTTPException(
                status_code=503,
                detail={"code": code, "message": "运行时尚未就绪"},
            )
        if not payload.message.strip():
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_message", "message": "评测消息非法"},
            )
        return await runtime_service.evaluate_intent_ladder(
            payload.message,
            payload.page_context.model_dump(by_alias=True, exclude_none=True)
            if payload.page_context is not None
            else None,
            list(payload.plannable_capability_ids),
        )

    @app.post(
        "/v1/business-workflows/{business_workflow_id}/resume",
        response_model=BusinessWorkflowResumeResponse,
        response_model_by_alias=True,
        response_model_exclude_none=True,
        # Resume state is a wire contract, not a display projection.  Keep
        # default-valued empty arrays/revisions so the Node gateway can
        # validate and durably persist the complete state on every turn.
        response_model_exclude_defaults=False,
        status_code=200,
        dependencies=[Depends(require_service_token)],
    )
    async def resume_business_workflow(
        business_workflow_id: str,
        payload: BusinessWorkflowResumeRequest,
    ) -> BusinessWorkflowResumeResponse:
        runtime_service = app.state.runtime_service
        if app.state.startup_error or runtime_service is None:
            code = app.state.startup_error or "runtime_unavailable"
            raise HTTPException(
                status_code=503,
                detail={"code": code, "message": "运行时尚未就绪"},
            )
        if not business_workflow_id or len(business_workflow_id) > 160:
            raise HTTPException(
                status_code=422,
                detail={"code": "validation_error", "message": "Workflow 标识不合法"},
            )
        return await runtime_service.resume_business_workflow(
            business_workflow_id, payload
        )

    return app


app = create_app()
