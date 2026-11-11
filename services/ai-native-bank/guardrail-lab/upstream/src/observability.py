"""
Phoenix 链路观测模块

基于 OpenTelemetry + OpenInference 实现：
- LangChainInstrumentor 自动埋点：捕获 LLM 链 / LangGraph 工作流调用
- observe_guardrail_node 装饰器：记录护栏节点的判定结果与耗时
- request_context：请求根 span + session.id 绑定（conversation_id 聚合视图）
- traced：轻量 span 上下文（用于 Embedding 调用等手动埋点）

Phoenix 服务端为本地进程（uvx arize-phoenix serve），trace 数据不出本机。
初始化失败时静默降级为无观测模式，不影响主服务。
"""
import functools
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional

from config.config import PHOENIX_ENABLED, PHOENIX_ENDPOINT

logger = logging.getLogger("agent_service")

_enabled = False
_tracer = None
_provider = None

# 护栏节点返回值中需要记录到 span 的字段
_NODE_ATTR_KEYS = (
    "is_blocked",
    "blocked_by",
    "block_reason",
    "violated_rules",
    "is_white_listed",
    "pii_input_detected",
    "pii_input_entities",
    "pii_output_detected",
    "pii_output_entities",
    "content_filter_triggered",
    "content_filter_categories",
)


def init_observability() -> bool:
    """初始化 Phoenix 观测（OTLP/HTTP 上报 + LangChain 自动埋点）。

    失败时静默降级，返回 False；主服务不受影响。
    """
    global _enabled, _tracer, _provider

    if not PHOENIX_ENABLED:
        logger.info("Phoenix 链路观测未启用（PHOENIX_ENABLED=False）")
        return False

    if _enabled:
        # uvicorn reload 场景避免重复 instrument
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from openinference.instrumentation.langchain import LangChainInstrumentor

        provider = TracerProvider(
            resource=Resource.create({"service.name": "finance-guardrail"})
        )
        exporter = OTLPSpanExporter(endpoint=f"{PHOENIX_ENDPOINT}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        LangChainInstrumentor().instrument(tracer_provider=provider)

        _provider = provider
        _tracer = trace.get_tracer("finance-guardrail")
        _enabled = True
        logger.info(f"Phoenix 链路观测已启用: {PHOENIX_ENDPOINT}")
        return True
    except Exception as e:
        logger.warning(f"Phoenix 观测初始化失败，已降级为无观测模式: {str(e)}")
        _enabled = False
        _tracer = None
        return False


def shutdown_observability() -> None:
    """关闭观测，刷新未上报的 span。"""
    global _enabled, _tracer, _provider
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception as e:
            logger.warning(f"Phoenix 观测关闭异常: {str(e)}")
    _enabled = False
    _tracer = None
    _provider = None


def is_observability_enabled() -> bool:
    return _enabled


def get_tracer():
    return _tracer


@contextmanager
def request_context(
    conversation_id: str,
    endpoint: str,
    source_ip: Optional[str] = None,
    streaming_mode: Optional[str] = None,
):
    """请求根 span + session.id 绑定。

    conversation_id 映射为 Phoenix 的 session.id，
    同一对话的多次请求可在 UI 中聚合为会话视图。
    观测未启用时为零开销 no-op。
    """
    if not _enabled or _tracer is None:
        yield
        return

    from openinference.instrumentation import using_session

    with using_session(conversation_id):
        with _tracer.start_as_current_span(f"chat.{endpoint}") as span:
            span.set_attribute("session.id", conversation_id)
            span.set_attribute("chat.endpoint", endpoint)
            if source_ip:
                span.set_attribute("client.source_ip", source_ip)
            if streaming_mode:
                span.set_attribute("chat.streaming_mode", streaming_mode)
            yield span


@contextmanager
def traced(name: str, **attributes):
    """轻量 span 上下文管理器，用于手动埋点（如 Embedding 调用）。

    观测未启用时为零开销 no-op。
    """
    if not _enabled or _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        yield span


def observe_guardrail_node(node_name: str) -> Callable:
    """护栏节点观测装饰器。

    包装 LangGraph 节点函数，记录节点耗时与判定结果
    （is_blocked / blocked_by / violated_rules / 内容过滤与 PII 标记）。
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(state, config=None, **kwargs) -> Dict[str, Any]:
            if not _enabled or _tracer is None:
                return await fn(state, config)

            with _tracer.start_as_current_span(f"guardrail.{node_name}") as span:
                span.set_attribute("guardrail.node", node_name)
                start = time.perf_counter()
                try:
                    result = await fn(state, config)
                except Exception as e:
                    span.record_exception(e)
                    raise
                span.set_attribute(
                    "guardrail.duration_ms",
                    round((time.perf_counter() - start) * 1000, 2),
                )
                if isinstance(result, dict):
                    for key in _NODE_ATTR_KEYS:
                        value = result.get(key)
                        if value is None:
                            continue
                        if isinstance(value, (list, dict)):
                            span.set_attribute(
                                f"guardrail.{key}",
                                json.dumps(value, ensure_ascii=False),
                            )
                        else:
                            span.set_attribute(f"guardrail.{key}", value)
                return result

        return wrapper

    return decorator


def observe_content_filter(fn: Callable) -> Callable:
    """内容过滤分类观测装饰器。

    包装 ContentFilterService.classify，记录触发类别、分数与耗时。
    """

    @functools.wraps(fn)
    async def wrapper(self, text: str, is_output: bool = False):
        if not _enabled or _tracer is None:
            return await fn(self, text, is_output)

        with _tracer.start_as_current_span("content_filter.classify") as span:
            span.set_attribute("content_filter.is_output", is_output)
            try:
                result = await fn(self, text, is_output)
            except Exception as e:
                span.record_exception(e)
                raise
            span.set_attribute(
                "content_filter.triggered_categories",
                result.triggered_categories or [],
            )
            span.set_attribute("content_filter.is_blocked", result.is_blocked)
            span.set_attribute("content_filter.max_score", result.max_score or 0.0)
            span.set_attribute(
                "content_filter.processing_time_ms", result.processing_time_ms
            )
            if result.max_category:
                span.set_attribute("content_filter.max_category", result.max_category)
            if result.category_scores:
                span.set_attribute(
                    "content_filter.category_scores",
                    json.dumps(result.category_scores, ensure_ascii=False),
                )
            return result

    return wrapper
