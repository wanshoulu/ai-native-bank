"""Graph-backed domain profile lookups used by the generic orchestrator."""

from __future__ import annotations

from app.contracts import IntentAmbiguity, IntentTask, IntentTaskEnvelope
from app.intent_graph import IntentGraphRepository
from app.domain_adapters import transfer as transfer_adapter


_TRANSFER_SKILL_ID = "bank.transfer-remittance"

# 域适配器注册表：按图谱 Skill 绑定选择，不按意图 ID。新增域时在此登记
# 提供 derive_ambiguities 的适配器即可参与结构歧义派生。
_DOMAIN_ADAPTERS = {
    _TRANSFER_SKILL_ID: transfer_adapter,
}


def skill_id_for_task(repository: IntentGraphRepository, task: IntentTask) -> str | None:
    candidate = repository.candidate_for_intent(
        task.intent_id, score=0, evidence=["business_profile"]
    )
    return candidate.skill_binding.skill_id if candidate.skill_binding is not None else None


def uses_transfer_compatibility_adapter(
    repository: IntentGraphRepository, task: IntentTask
) -> bool:
    """Select the isolated legacy adapter by graph Skill binding, not intent ID."""

    return skill_id_for_task(repository, task) == _TRANSFER_SKILL_ID


def all_use_transfer_compatibility_adapter(
    repository: IntentGraphRepository, envelope: IntentTaskEnvelope
) -> bool:
    return bool(envelope.tasks) and all(
        uses_transfer_compatibility_adapter(repository, task)
        for task in envelope.tasks
    )


def ambiguity_tasks(
    envelope: IntentTaskEnvelope, ambiguity
) -> list[IntentTask]:
    """Tasks referenced by one ambiguity, not the whole envelope.

    跨域多意图（如 查余额+转账）下，歧义只挂在其中某域的任务上；
    域路由必须按歧义引用的 task_ids 判定，不能要求整个信封同域。
    """

    referenced = set(ambiguity.task_ids)
    return [task for task in envelope.tasks if task.task_id in referenced]


def ambiguity_uses_transfer_compatibility_adapter(
    repository: IntentGraphRepository, envelope: IntentTaskEnvelope, ambiguity
) -> bool:
    scoped = ambiguity_tasks(envelope, ambiguity)
    return bool(scoped) and all(
        uses_transfer_compatibility_adapter(repository, task)
        for task in scoped
    )


def project_domain_ambiguity(repository, envelope, ambiguity, projector, *, message=None):
    if ambiguity_uses_transfer_compatibility_adapter(repository, envelope, ambiguity):
        return transfer_adapter.project_ambiguity(envelope, ambiguity, projector, message=message)
    raise ValueError("no graph-bound ambiguity projector is registered")


def domain_ambiguity_is_projectable(repository, envelope, ambiguity) -> bool:
    """无适配器的域不可投影。

    规划模型对任意跨任务槽位都可能声明歧义（如 from_account 同时挂在
    查余额与转账两个任务上），未注册适配器的域投不出任何用户可见卡片。
    之前此处返回 True、投影时 ValueError 上抛，整轮 run 直接 blocked。
    返回 False 后投影选择会跳过该声明：要么落到域适配器按图谱派生的
    正确范围歧义（derive_domain_ambiguities 产出的 transfer 域子集），
    要么机械跳过落入正常槽位澄清——与 dangling task_ids / 退化
    amount_scope 的处理同一原则。
    """

    if ambiguity_uses_transfer_compatibility_adapter(repository, envelope, ambiguity):
        return transfer_adapter.ambiguity_is_projectable(ambiguity)
    return False


def should_withhold_shared_slot(repository, task, *, name, context_ambiguities, message):
    if uses_transfer_compatibility_adapter(repository, task):
        return transfer_adapter.withhold_shared_slot(
            name=name,
            context_ambiguities=context_ambiguities,
            message=message,
        )
    return False


def should_withhold_scope_inherited_slot(repository, task, *, name, context_ambiguities, message):
    if uses_transfer_compatibility_adapter(repository, task):
        return transfer_adapter.withhold_scope_inherited_amount_slot(
            name=name,
            context_ambiguities=context_ambiguities,
            message=message,
        )
    return False


def domain_ambiguity_is_resolved(
    repository, envelope, ambiguity, *, context_ambiguities, message, tasks
):
    if ambiguity_uses_transfer_compatibility_adapter(repository, envelope, ambiguity):
        return transfer_adapter.ambiguity_is_resolved(
            ambiguity,
            context_ambiguities=context_ambiguities,
            message=message,
            tasks=tasks,
        )
    return False


def derive_domain_ambiguities(
    repository,
    tasks: list[IntentTask],
    message: str,
    existing_ambiguities=(),
    *,
    derive_kwargs: dict | None = None,
) -> list[IntentAmbiguity]:
    """Let each domain adapter derive ambiguities from its own tasks.

    派生按域分组进行（域 = 图谱 skill 绑定），只把该域的任务交给该域的
    适配器；跨域信封不会因为其他域的存在而丢失结构歧义。与既有歧义
    （planner 声明或历史保留）按 (type, task_ids) 去重。derive_kwargs
    供特定调用方（如 replan 采纳）向适配器传递显式场景标记。
    """

    derived: list[IntentAmbiguity] = []
    seen = {
        (item.type, frozenset(item.task_ids))
        for item in existing_ambiguities
    }
    for skill_id, adapter in _DOMAIN_ADAPTERS.items():
        derive = getattr(adapter, "derive_ambiguities", None)
        if derive is None:
            continue
        scoped = [
            task for task in tasks
            if skill_id_for_task(repository, task) == skill_id
        ]
        if not scoped:
            continue
        for ambiguity in derive(
            tasks=scoped, message=message, **(derive_kwargs or {})
        ):
            key = (ambiguity.type, frozenset(ambiguity.task_ids))
            if key in seen:
                continue
            seen.add(key)
            derived.append(ambiguity)
    return derived
