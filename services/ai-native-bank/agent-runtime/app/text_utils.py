"""Shared text normalization utilities for intent matching and FAQ analysis."""

from __future__ import annotations

import re
from typing import Any


def normalize(value: Any) -> str:
    """Strip whitespace and CJK punctuation, then lowercase."""
    return re.sub(r"[\s，,。；;、！？!?]+", "", str(value or "").lower())
