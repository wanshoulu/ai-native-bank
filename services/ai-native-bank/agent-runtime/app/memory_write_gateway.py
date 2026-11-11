"""Private client for durable memory-command submission through the Node gateway."""

from __future__ import annotations

from typing import Any

import httpx


class MemoryWriteGatewayError(RuntimeError):
    pass


_ITEM_KEYS = {
    "candidateType", "predicate", "status", "decision", "governanceStatus",
    "reason", "policyVersion", "attempts",
}
_STATUSES = {"QUEUED", "PROCESSING", "PENDING_RETRY", "PERSISTED", "CONTESTED", "REJECTED", "DEAD_LETTER"}
_DECISIONS = {None, "ADD", "MERGE", "SUPERSEDE", "CONTESTED", "REJECT"}


class MemoryWriteGateway:
    def __init__(
        self, *, endpoint: str, service_token: str,
        client: httpx.AsyncClient | None = None, timeout_seconds: float = 2.5,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("memory command endpoint is invalid")
        if not 16 <= len(service_token) <= 512 or any(ch.isspace() for ch in service_token):
            raise ValueError("runtime service token is invalid")
        self._endpoint = endpoint
        self._service_token = service_token
        self._client = client or httpx.AsyncClient()
        self._timeout_seconds = max(0.2, min(float(timeout_seconds), 5.0))

    async def submit(self, request: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        try:
            response = await self._client.post(
                self._endpoint,
                headers={"Authorization": "Bearer " + self._service_token},
                json=request,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise MemoryWriteGatewayError("memory command gateway unavailable") from exc
        try:
            data = body["data"]
            if set(data) != {"schemaVersion", "items"} or data["schemaVersion"] != "1.0":
                raise ValueError
            items = data["items"]
            if not isinstance(items, list) or len(items) > 20:
                raise ValueError
            for item in items:
                if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
                    raise ValueError
                if item["status"] not in _STATUSES or item["decision"] not in _DECISIONS:
                    raise ValueError
                if not isinstance(item["attempts"], int) or item["attempts"] < 0:
                    raise ValueError
                if not all(isinstance(item[key], str) and item[key] for key in ("candidateType", "predicate")):
                    raise ValueError
                for key in ("governanceStatus", "reason", "policyVersion"):
                    if item[key] is not None and not isinstance(item[key], str):
                        raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryWriteGatewayError("memory command response is invalid") from exc
        return tuple(dict(item) for item in items)
