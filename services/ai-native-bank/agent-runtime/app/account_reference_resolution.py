"""Semantic account-reference adapter shared by graph-declared banking tasks.

The security-hardened implementation remains in ``balance_resolution`` during the
compatibility window. Runtime orchestration imports this neutral adapter instead
of selecting behavior by intent id.
"""

from __future__ import annotations

from typing import Any

from .balance_resolution import (
    AccountOption,
    BalanceResolution as AccountReferenceResolution,
    BalanceResolutionError as AccountReferenceResolutionError,
    resolve_balance_account,
)


def resolve_account_reference(
    source_message: str | None,
    accounts: Any,
) -> AccountReferenceResolution:
    """Resolve a semantic account reference from validated account discovery."""

    return resolve_balance_account(source_message, accounts)


__all__ = [
    "AccountOption",
    "AccountReferenceResolution",
    "AccountReferenceResolutionError",
    "resolve_account_reference",
]
