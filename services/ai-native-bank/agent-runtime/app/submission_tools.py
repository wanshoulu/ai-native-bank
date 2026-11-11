"""Trusted structured-output tools used by the two DeepAgents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

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


class SubmissionCollector:
    def __init__(self, label: str):
        self._label = label
        self._value: dict[str, Any] | None = None
        self._submitted = False

    def reset(self) -> None:
        self._value = None
        self._submitted = False

    def submit(self, value: dict[str, Any]) -> dict[str, Any]:
        if self._submitted:
            # 幂等响应：重复提交不覆盖首个结果。轻量模型可能把
            # "exactly once" 硬错误当成可重试的失败而反复重提直到
            # 超时；返回稳定成功并明确指示立即结束本轮。
            return {
                "accepted": True,
                "duplicate": True,
                "instruction": (
                    "该提交已完成并生效；不要再调用任何工具，立即结束本轮。"
                ),
            }
        self._submitted = True
        self._value = {key: item for key, item in value.items() if item is not None}
        return {
            "accepted": True,
            "instruction": "提交成功；不要再调用任何工具，立即结束本轮。",
        }

    def take(self) -> dict[str, Any] | None:
        return self._value


def create_submission_tool(
    *,
    name: str,
    description: str,
    schema: dict[str, Any],
    collector: SubmissionCollector,
    normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
):
    @tool(
        name=name,
        description=description,
        input_params=schema,
        auto_extract=False,
    )
    async def submit(**payload: Any) -> dict[str, Any]:
        if normalizer is not None:
            payload = normalizer(payload)
        return collector.submit(payload)

    submit.card.parallel_safe = False
    submit.card.idempotent = False
    return submit
