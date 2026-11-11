"""Application-side log hardening for model and tool execution boundaries."""

from __future__ import annotations

import logging


SENSITIVE_FRAMEWORK_LOGGERS = (
    "common",
    "llm",
    "prompt",
    "tool",
    "agent",
    "runner",
    "session",
    "multi_agent",
)


def harden_framework_logging() -> None:
    """Suppress framework INFO records that may include model/tool arguments."""

    for name in SENSITIVE_FRAMEWORK_LOGGERS:
        framework_logger = logging.getLogger(name)
        framework_logger.setLevel(logging.WARNING)
        for handler in framework_logger.handlers:
            handler.setLevel(logging.WARNING)
