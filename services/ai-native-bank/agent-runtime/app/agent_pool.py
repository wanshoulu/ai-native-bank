"""Least-privilege OpenJiuwen Business DeepAgent pool keyed by execution-view digest."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.business_loop import AUTHORITATIVE_NODE_WORKFLOW_KEY
from app.resource_registry import (
    BusinessResourceRegistry,
    ExecutionView,
    ProviderRegistration,
    ResourceRegistryError,
    SkillRegistration,
)
from app.todo_ledger import TodoLedger, TodoLedgerSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedAgentView:
    intent_ids: tuple[str, ...]
    skill_ids: tuple[str, ...]
    skill_paths: tuple[str, ...]
    skills: tuple[SkillRegistration, ...]
    capability_ids: tuple[str, ...]
    providers: tuple[ProviderRegistration, ...]
    digest: str


class BusinessAgentPool:
    """Routes each delegated envelope to an agent with only its resolved resources."""

    def __init__(
        self,
        *,
        registry: BusinessResourceRegistry,
        skill_root: str | Path,
        workspace_root: str | Path,
        runner_factory: Callable[[ResolvedAgentView], Any],
    ) -> None:
        self._registry = registry
        self._skill_root = Path(skill_root).resolve()
        self._workspace_root = Path(workspace_root).resolve()
        self._workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._runner_factory = runner_factory
        self._runners: dict[str, Any] = {}
        self._session_views: dict[str, ResolvedAgentView] = {}
        self._todo_ledger = TodoLedger(self._workspace_root)

    def execution_view_for_payload(self, payload: dict[str, Any]) -> ResolvedAgentView:
        envelope = payload.get("intent_task_envelope")
        tasks = envelope.get("tasks") if isinstance(envelope, dict) else None
        if not isinstance(tasks, list) or not tasks:
            raise ResourceRegistryError("intent_binding_missing", "Delegated tasks are required")
        intent_ids: list[str] = []
        for item in tasks:
            intent_id = item.get("intent_id") if isinstance(item, dict) else None
            if not isinstance(intent_id, str) or not intent_id:
                raise ResourceRegistryError("intent_binding_missing", "Delegated intent is invalid")
            if self._registry.handles_intent(intent_id) and intent_id not in intent_ids:
                intent_ids.append(intent_id)
        if not intent_ids:
            logging.getLogger(__name__).warning(
                "Delegated tasks have no active skill: delegated=%s envelopeIntents=%s",
                [item.get("intent_id") for item in tasks if isinstance(item, dict)],
                sorted(self._registry.skills and {
                    intent_id
                    for skill in self._registry.skills
                    for intent_id in skill.handled_intent_ids
                } or set())[:40],
            )
            raise ResourceRegistryError(
                "skill_binding_missing", "No executable delegated intent has an active skill"
            )
        supplied_scopes = payload.get("actor_scopes")
        actor_scopes = (
            frozenset(supplied_scopes)
            if isinstance(supplied_scopes, list) and all(isinstance(item, str) for item in supplied_scopes)
            else frozenset(
                scope
                for provider in self._registry.providers
                for scope in provider.required_scopes
            )
        )
        base = self._registry.resolve_execution_view(
            intent_ids=tuple(intent_ids), actor_scopes=actor_scopes
        )
        skill_paths = tuple(self._safe_skill_path(item) for item in base.skills)
        return ResolvedAgentView(
            intent_ids=base.intent_ids,
            skill_ids=base.skill_ids,
            skill_paths=skill_paths,
            skills=base.skills,
            capability_ids=base.capability_ids,
            providers=base.providers,
            digest=base.digest,
        )

    def get_or_create(self, view: ResolvedAgentView) -> Any:
        runner = self._runners.get(view.digest)
        if runner is None:
            runner = self._runner_factory(view)
            self._runners[view.digest] = runner
        return runner

    async def run(self, payload: dict[str, Any], *, session_id: str) -> dict[str, Any] | None:
        previous_view = self._session_views.get(session_id)
        if previous_view is not None and (
            "intent_task_envelope" not in payload
            or AUTHORITATIVE_NODE_WORKFLOW_KEY in payload
        ):
            # 权威台账续跑沿用会话原有受限视图：接续 envelope 只覆盖剩余
            # 目标时（已交付任务被过滤），视图按持久化委托集合解析，收
            # 缩不应触发视图冲突，也不会扩大任何权限。
            view = previous_view
        else:
            view = self.execution_view_for_payload(payload)
        # confirmation_resume 阶段需要 invoke_authorized_capability 工具，
        # 该工具仅在授权上下文激活时注册。清除缓存的 runner 以触发重建，
        # 确保新 runner 能检测到已激活的授权上下文。
        if "confirmation_resume" in payload and view.digest in self._runners:
            del self._runners[view.digest]
        runner = self.get_or_create(view)
        if previous_view is not None and previous_view.digest != view.digest:
            raise ResourceRegistryError(
                "session_view_conflict",
                "Business session cannot change its restricted execution view",
            )
        if (
            previous_view is None
            and AUTHORITATIVE_NODE_WORKFLOW_KEY not in payload
            and hasattr(runner, "restore_todo_ledger")
        ):
            workflow_control = payload.get("workflow_control")
            expected_revision = (
                workflow_control.get("expected_todo_revision", 0)
                if isinstance(workflow_control, dict)
                else 0
            )
            persisted = self._todo_ledger.load(
                session_id, revision=max(1, int(expected_revision or 0))
            )
            if persisted.items:
                runner.restore_todo_ledger(
                    session_id,
                    persisted.revision,
                    [
                        {
                            "id": item.id,
                            "status": item.status,
                            "dependsOn": list(item.depends_on),
                        }
                        for item in persisted.items
                    ],
                )
        self._session_views[session_id] = view
        enriched = dict(payload)
        enriched["execution_view"] = {
            "digest": view.digest,
            "skillIds": list(view.skill_ids),
            "capabilityIds": list(view.capability_ids),
            "providerIds": [item.provider_id for item in view.providers],
            "mcpProviders": [
                {
                    "providerId": item.provider_id,
                    "capabilityId": item.capability_id,
                    "version": item.version,
                    "schemaDigest": item.schema_digest,
                    "serverId": item.endpoint_binding["serverId"],
                    "toolName": item.endpoint_binding["toolName"],
                }
                for item in view.providers
                if item.provider_type == "mcp"
                and item.exposure == "model-callable"
            ],
            "userActionProviders": [
                {
                    "providerId": item.provider_id,
                    "capabilityId": item.capability_id,
                    "version": item.version,
                }
                for item in view.providers
                if item.provider_type == "user-action"
                and item.exposure == "user-action"
            ],
        }
        result = await runner.run(enriched, session_id=session_id)
        if hasattr(runner, "load_todo_ledger"):
            try:
                result_revision = (
                    result.get("todo_revision", 1)
                    if isinstance(result, dict)
                    else 1
                )
                revision = max(1, int(result_revision or 1))
                native = runner.load_todo_ledger(session_id, revision=revision)
                if native.items:
                    self._todo_ledger.restore(
                        session_id,
                        revision,
                        [
                            {
                                "id": item.id,
                                "status": item.status,
                                "dependsOn": list(item.depends_on),
                            }
                            for item in native.items
                        ],
                    )
            except Exception:
                logger.warning(
                    "Post-run todo ledger restore skipped session_id=%s",
                    session_id,
                )
        return result

    def todo_snapshot(self, session_id: str):
        return self.load_todo_ledger(session_id).items

    def load_todo_ledger(self, session_id: str, revision: int = 1) -> TodoLedgerSnapshot:
        view = self._session_views.get(session_id)
        runner = self._runners.get(view.digest) if view is not None else None
        if runner is not None and hasattr(runner, "load_todo_ledger"):
            return runner.load_todo_ledger(session_id, revision=revision)
        return self._todo_ledger.load(session_id, revision)

    def restore_todo_ledger(
        self, session_id: str, revision: int, items: list[dict[str, Any]]
    ) -> TodoLedgerSnapshot:
        persisted = self._todo_ledger.restore(session_id, revision, items)
        view = self._session_views.get(session_id)
        runner = self._runners.get(view.digest) if view is not None else None
        if runner is not None and hasattr(runner, "restore_todo_ledger"):
            return runner.restore_todo_ledger(session_id, revision, items)
        return persisted

    def seed_todo_ledger(
        self, session_id: str, items: list[dict[str, Any]]
    ) -> TodoLedgerSnapshot:
        seeded = self._todo_ledger.seed(session_id, items)
        view = self._session_views.get(session_id)
        runner = self._runners.get(view.digest) if view is not None else None
        if runner is not None and hasattr(runner, "seed_todo_ledger"):
            return runner.seed_todo_ledger(session_id, items)
        return seeded

    def _safe_skill_path(self, skill: SkillRegistration) -> str:
        directory_name = Path(skill.entrypoint).parent.name
        candidate = (self._skill_root / directory_name).resolve()
        try:
            candidate.relative_to(self._skill_root)
        except ValueError as exc:
            raise ResourceRegistryError(
                "skill_entrypoint_invalid", f"Skill {skill.id} escapes the configured root"
            ) from exc
        if not (candidate / "SKILL.md").is_file():
            raise ResourceRegistryError(
                "skill_entrypoint_invalid", f"Skill {skill.id} entrypoint is unavailable"
            )
        return str(candidate)
