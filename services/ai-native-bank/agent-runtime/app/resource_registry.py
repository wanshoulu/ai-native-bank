"""Immutable Skill/Capability/Provider registry shared with the Node control plane."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

_USAGE_TASKS: set = set()


async def _post_usage_event(event: dict) -> None:
    import httpx

    base = (os.getenv("AI_HARBOR_URL") or "http://ai-harbor:3000").rstrip("/")
    token = os.getenv("USAGE_EVENT_TOKEN") or "ontomind-internal-usage"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(f"{base}/api/knowledge/usage-event", json=event, headers={"x-usage-token": token})
    except Exception:
        pass


def _report_skill_usage(intent_ids: tuple[str, ...], skill_count: int) -> None:
    """Fire-and-forget：业务运行按意图解析到技能即视为一次技能知识消费。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_post_usage_event({
        "carrier": "skill",
        "caller": "ai-native-bank",
        "query": ",".join(intent_ids)[:200],
        "hits": skill_count,
    }))
    _USAGE_TASKS.add(task)
    task.add_done_callback(_USAGE_TASKS.discard)


_SKILL_ID = re.compile(r"^bank\.[a-z0-9.-]+$")
_PROVIDER_ID = re.compile(r"^provider\.[a-z0-9.-]+$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class ResourceRegistryError(RuntimeError):
    """A registry artifact or requested execution view is not safe to use."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SkillRegistration:
    id: str
    version: str
    digest: str
    status: str
    handled_intent_ids: tuple[str, ...]
    intent_capability_bindings: Mapping[str, tuple[str, ...]]
    business_object_ids: tuple[str, ...]
    required_capability_ids: tuple[str, ...]
    risk_boundary: str
    compatible_graph_versions: tuple[str, ...]
    compatible_runtime_versions: tuple[str, ...]
    entrypoint: str
    owner: str


@dataclass(frozen=True)
class ProviderRegistration:
    provider_id: str
    provider_type: str
    capability_id: str
    version: str
    schema_digest: str
    exposure: str
    required_scopes: tuple[str, ...]
    health: str
    endpoint_binding: Mapping[str, Any]


@dataclass(frozen=True)
class ExecutionView:
    intent_ids: tuple[str, ...]
    skill_ids: tuple[str, ...]
    skills: tuple[SkillRegistration, ...]
    capability_ids: tuple[str, ...]
    providers: tuple[ProviderRegistration, ...]
    digest: str


class BusinessResourceRegistry:
    __slots__ = (
        "skills", "providers", "digest", "_skills_by_intent",
        "_providers_by_capability", "_sealed",
    )

    def __init__(
        self,
        skills: tuple[SkillRegistration, ...],
        providers: tuple[ProviderRegistration, ...],
    ) -> None:
        skills_by_intent: dict[str, SkillRegistration] = {}
        for skill in skills:
            for intent_id in skill.handled_intent_ids:
                if intent_id in skills_by_intent:
                    raise ResourceRegistryError(
                        "registry_contract_invalid", f"Intent {intent_id} has multiple skills"
                    )
                skills_by_intent[intent_id] = skill
        providers_by_capability: dict[str, ProviderRegistration] = {}
        for provider in providers:
            if provider.capability_id in providers_by_capability:
                raise ResourceRegistryError(
                    "registry_contract_invalid",
                    f"Capability {provider.capability_id} has multiple providers",
                )
            providers_by_capability[provider.capability_id] = provider
        object.__setattr__(self, "skills", skills)
        object.__setattr__(self, "providers", providers)
        object.__setattr__(self, "_skills_by_intent", MappingProxyType(skills_by_intent))
        object.__setattr__(self, "_providers_by_capability", MappingProxyType(providers_by_capability))
        object.__setattr__(self, "digest", _digest({
            "skills": [_skill_dict(item) for item in skills],
            "providers": [_provider_dict(item) for item in providers],
        }))
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("business resource registry is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def load(cls, root: str | Path) -> "BusinessResourceRegistry":
        registry_root = Path(root)
        skills_payload = _read_json(registry_root / "skills.v1.json", "skill_registry_unavailable")
        providers_payload = _read_json(
            registry_root / "providers.v1.json", "provider_registry_unavailable"
        )
        _require_exact_keys(skills_payload, {"schemaVersion", "skills"}, "skill registry")
        _require_exact_keys(providers_payload, {"schemaVersion", "providers"}, "provider registry")
        if skills_payload.get("schemaVersion") != "1.0" or providers_payload.get("schemaVersion") != "1.0":
            raise ResourceRegistryError("registry_contract_invalid", "Unsupported registry version")
        skills = tuple(_parse_skill(item) for item in skills_payload.get("skills", ()))
        providers = tuple(_parse_provider(item) for item in providers_payload.get("providers", ()))
        if not skills or not providers:
            raise ResourceRegistryError("registry_contract_invalid", "Registry cannot be empty")
        return cls(skills, providers)

    def resolve_execution_view(
        self,
        *,
        intent_ids: tuple[str, ...],
        actor_scopes: frozenset[str],
    ) -> ExecutionView:
        requested_intents = tuple(dict.fromkeys(intent_ids))
        if not requested_intents:
            raise ResourceRegistryError("intent_binding_missing", "At least one intent is required")
        selected_skills: list[SkillRegistration] = []
        capability_ids: list[str] = []
        for intent_id in requested_intents:
            skill = self._skills_by_intent.get(intent_id)
            if skill is None or skill.status != "active":
                raise ResourceRegistryError(
                    "skill_binding_missing", f"No active skill handles {intent_id}"
                )
            if skill not in selected_skills:
                selected_skills.append(skill)
            for capability_id in skill.intent_capability_bindings[intent_id]:
                if capability_id not in capability_ids:
                    capability_ids.append(capability_id)
        selected_providers: list[ProviderRegistration] = []
        for capability_id in capability_ids:
            provider = self._providers_by_capability.get(capability_id)
            if provider is None or provider.health != "ready":
                raise ResourceRegistryError(
                    "provider_unavailable", f"No healthy provider serves {capability_id}"
                )
            if any(scope not in actor_scopes for scope in provider.required_scopes):
                raise ResourceRegistryError(
                    "provider_scope_denied", f"Actor is not authorized for {capability_id}"
                )
            selected_providers.append(provider)
        body = {
            "intentIds": list(requested_intents),
            "skillIds": [item.id for item in selected_skills],
            "skills": [_skill_dict(item) for item in selected_skills],
            "capabilityIds": capability_ids,
            "providers": [_provider_dict(item) for item in selected_providers],
        }
        return ExecutionView(
            intent_ids=requested_intents,
            skill_ids=tuple(body["skillIds"]),
            skills=tuple(selected_skills),
            capability_ids=tuple(capability_ids),
            providers=tuple(selected_providers),
            digest=_digest(body),
        )

    def get_skill(self, skill_id: str) -> SkillRegistration:
        for skill in self.skills:
            if skill.id == skill_id:
                return skill
        raise ResourceRegistryError("skill_unavailable", f"Skill {skill_id} is unavailable")

    def handles_intent(self, intent_id: str) -> bool:
        skill = self._skills_by_intent.get(intent_id)
        return skill is not None and skill.status == "active"


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResourceRegistryError(code, f"Cannot load {path.name}") from exc
    if not isinstance(value, dict):
        raise ResourceRegistryError("registry_contract_invalid", f"{path.name} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ResourceRegistryError("registry_contract_invalid", f"{label} has invalid fields")


def _parse_skill(value: Mapping[str, Any]) -> SkillRegistration:
    keys = {
        "id", "version", "digest", "status", "handledIntentIds",
        "intentCapabilityBindings", "businessObjectIds", "requiredCapabilityIds",
        "riskBoundary", "compatibleGraphVersions", "compatibleRuntimeVersions",
        "entrypoint", "owner",
    }
    _require_exact_keys(value, keys, "skill")
    if not _SKILL_ID.fullmatch(str(value["id"])) or not _VERSION.fullmatch(str(value["version"])):
        raise ResourceRegistryError("registry_contract_invalid", "Skill identity is invalid")
    if not _DIGEST.fullmatch(str(value["digest"])) or value["status"] not in {"active", "disabled"}:
        raise ResourceRegistryError("registry_contract_invalid", "Skill lifecycle is invalid")
    handled = tuple(str(item) for item in value["handledIntentIds"])
    required = tuple(str(item) for item in value["requiredCapabilityIds"])
    raw_bindings = value["intentCapabilityBindings"]
    if not handled or set(raw_bindings) != set(handled):
        raise ResourceRegistryError("registry_contract_invalid", "Skill intent bindings are incomplete")
    allowed = set(required)
    bindings = {
        intent_id: tuple(str(item) for item in raw_bindings[intent_id])
        for intent_id in handled
    }
    if any(capability not in allowed for items in bindings.values() for capability in items):
        raise ResourceRegistryError("registry_contract_invalid", "Intent exceeds skill capability boundary")
    return SkillRegistration(
        id=str(value["id"]),
        version=str(value["version"]),
        digest=str(value["digest"]),
        status=str(value["status"]),
        handled_intent_ids=handled,
        intent_capability_bindings=MappingProxyType(bindings),
        business_object_ids=tuple(str(item) for item in value["businessObjectIds"]),
        required_capability_ids=required,
        risk_boundary=str(value["riskBoundary"]),
        compatible_graph_versions=tuple(str(item) for item in value["compatibleGraphVersions"]),
        compatible_runtime_versions=tuple(str(item) for item in value["compatibleRuntimeVersions"]),
        entrypoint=str(value["entrypoint"]),
        owner=str(value["owner"]),
    )


def _parse_provider(value: Mapping[str, Any]) -> ProviderRegistration:
    keys = {
        "providerId", "providerType", "capabilityId", "version", "schemaDigest",
        "exposure", "requiredScopes", "health", "endpointBinding",
    }
    _require_exact_keys(value, keys, "provider")
    if not _PROVIDER_ID.fullmatch(str(value["providerId"])) or not _VERSION.fullmatch(str(value["version"])):
        raise ResourceRegistryError("registry_contract_invalid", "Provider identity is invalid")
    if not _DIGEST.fullmatch(str(value["schemaDigest"])):
        raise ResourceRegistryError("registry_contract_invalid", "Provider schema digest is invalid")
    provider_type = value["providerType"]
    exposure = value["exposure"]
    if (provider_type, exposure) not in {
        ("mcp", "model-callable"),
        ("user-action", "user-action"),
    }:
        raise ResourceRegistryError("provider_not_allowed", "Provider exposure is not agent-safe")
    if value["health"] not in {"ready", "degraded", "unavailable"}:
        raise ResourceRegistryError("registry_contract_invalid", "Provider health is invalid")
    endpoint_binding = value["endpointBinding"]
    if not isinstance(endpoint_binding, Mapping):
        raise ResourceRegistryError("registry_contract_invalid", "Provider endpoint binding is invalid")
    expected_binding_keys = (
        {"serverId", "toolName"}
        if provider_type == "mcp"
        else {"gatewayId", "action"}
    )
    _require_exact_keys(endpoint_binding, expected_binding_keys, "provider endpoint binding")
    if any(
        not isinstance(item, str)
        or not re.fullmatch(r"^[a-z][a-z0-9._-]{1,95}$", item)
        for item in endpoint_binding.values()
    ):
        raise ResourceRegistryError("registry_contract_invalid", "Provider endpoint binding is invalid")
    return ProviderRegistration(
        provider_id=str(value["providerId"]),
        provider_type=str(provider_type),
        capability_id=str(value["capabilityId"]),
        version=str(value["version"]),
        schema_digest=str(value["schemaDigest"]),
        exposure=str(exposure),
        required_scopes=tuple(str(item) for item in value["requiredScopes"]),
        health=str(value["health"]),
        endpoint_binding=MappingProxyType(dict(endpoint_binding)),
    )


def _skill_dict(value: SkillRegistration) -> dict[str, Any]:
    return {
        "id": value.id,
        "version": value.version,
        "digest": value.digest,
        "status": value.status,
        "handledIntentIds": list(value.handled_intent_ids),
        "intentCapabilityBindings": {
            key: list(items) for key, items in value.intent_capability_bindings.items()
        },
        "businessObjectIds": list(value.business_object_ids),
        "requiredCapabilityIds": list(value.required_capability_ids),
        "riskBoundary": value.risk_boundary,
        "compatibleGraphVersions": list(value.compatible_graph_versions),
        "compatibleRuntimeVersions": list(value.compatible_runtime_versions),
        "entrypoint": value.entrypoint,
        "owner": value.owner,
    }


def _provider_dict(value: ProviderRegistration) -> dict[str, Any]:
    return {
        "providerId": value.provider_id,
        "providerType": value.provider_type,
        "capabilityId": value.capability_id,
        "version": value.version,
        "schemaDigest": value.schema_digest,
        "exposure": value.exposure,
        "requiredScopes": list(value.required_scopes),
        "health": value.health,
        "endpointBinding": dict(value.endpoint_binding),
    }


def _digest(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
