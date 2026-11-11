# coding: utf-8
"""框架任务连续性层：openjiuwen TaskManager + IntentRecognizer 组件级接入。

职责边界：
- 框架（真跑机制）：IntentRecognizer（LLM 任务操作工具裁决）+ TaskManager
  （任务状态机：暂停/恢复/层级/优先级）。任务操作判定不在本模块写任何规则。
- 本模块：组装框架组件；把 Node 持久化的会话目标（ConversationContextGoal）
  播种为框架 Task；按识别出的 Intent 操作框架任务状态；输出裁决结论。
- 持久化权威仍是 Node（bank_agent_workflows / 会话状态）。框架 TaskManager
  每轮由会话上下文播种，会话内不另设存储。

裁决语义（框架 IntentType 原生语义，非本模块发明）：
- CREATE_TASK = "执行新任务；若存在执行中任务则先中断"——待办任务存在时
  即任务跳转（SWITCH）。本轮调用方据此剥离续接上下文，按全新意图规划。
- SUPPLEMENT_TASK = 为待补充信息的任务提供必要信息后继续——续答。
- UNKNOWN_TASK / 无工具调用 = 无法判定任务操作——调用方维持现有续接行为。
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from dataclasses import dataclass
from typing import Any, Callable

try:
    from openjiuwen.core.controller import IntentType, Task, TaskStatus, TextDataFrame
    from openjiuwen.core.controller.config import ControllerConfig
    from openjiuwen.core.controller.modules import TaskManager
    from openjiuwen.core.controller.modules.intent_recognizer import IntentRecognizer
    from openjiuwen.core.controller.modules.task_manager import TaskManagerState
    from openjiuwen.core.controller.schema.event import InputEvent
    from openjiuwen.core.context_engine import ContextEngine, ContextEngineConfig
    from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.session.agent import Session
except ImportError:
    class IntentType:  # type: ignore[no-redef]
        pass
    class Task:  # type: ignore[no-redef]
        pass
    class TaskStatus:  # type: ignore[no-redef]
        SUBMITTED = "submitted"
        INPUT_REQUIRED = "input_required"
    class TextDataFrame:  # type: ignore[no-redef]
        pass
    class ControllerConfig:  # type: ignore[no-redef]
        pass
    class TaskManager:  # type: ignore[no-redef]
        pass
    class TaskManagerState:  # type: ignore[no-redef]
        pass
    class IntentRecognizer:  # type: ignore[no-redef]
        pass
    class InputEvent:  # type: ignore[no-redef]
        pass
    class ContextEngine:  # type: ignore[no-redef]
        pass
    class ContextEngineConfig:  # type: ignore[no-redef]
        pass
    class Model:  # type: ignore[no-redef]
        pass
    class ModelClientConfig:  # type: ignore[no-redef]
        pass
    class ModelRequestConfig:  # type: ignore[no-redef]
        pass
    class Runner:  # type: ignore[no-redef]
        class resource_mgr:
            @staticmethod
            def add_model(*_args, **_kwargs):
                pass
    class Session:  # type: ignore[no-redef]
        pass

_app_pkg_logger = logging.getLogger("ai_native_bank")
if not _app_pkg_logger.handlers and not logging.getLogger().handlers:
    # 容器内 app 层 logger 默认无 handler（INFO 会被整体丢弃），补一路
    # stderr handler 保证框架裁决与续接调度可观测；若根 logger 已配置则遵循宿主配置。
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    _app_pkg_logger.addHandler(_handler)
    _app_pkg_logger.setLevel(logging.INFO)

logger = logging.getLogger("ai_native_bank.task_continuity")

_MODEL_ID = "bank-intent-recognition"

# IntentToolkits 暴露的任务操作工具子集（框架原生支持全集 8 种）。只装配
# 管线有下游语义的 4 种：create（跳转）、resume/supplement（续接）、unknown
# （fail-open）。pause/cancel/modify/dependent 的裁决结果在本管线中没有
# 消费方，装配它们只会放大工具 schema 与模型输出分支，拉长裁决时延。
_INTENT_TYPE_LIST = [
    "create_task",
    "resume_task",
    "supplement_task",
    "unknown_task",
]

_GOAL_STATUS_TO_TASK_STATUS = {
    "provisional": TaskStatus.SUBMITTED,
    "ready": TaskStatus.SUBMITTED,
    "needs_slots": TaskStatus.INPUT_REQUIRED,
}


@dataclass(frozen=True)
class ContinuityDecision:
    """一次任务操作裁决的结论。

    switched=True 表示框架判定本轮是新任务（CREATE 即中断现有任务），
    调用方应剥离续接上下文、按全新意图规划本轮。
    """

    switched: bool
    operation: str
    confidence: float
    target_description: str | None
    target_task_id: str | None
    clarification_prompt: str | None
    task_count: int


def _provider(model: Model) -> Callable[[], Model]:
    return lambda: model


class FrameworkTaskContinuity:
    """openjiuwen 框架任务连续性组件的运行时装配与调用入口。"""

    def __init__(self, settings: Any):
        self._settings = settings
        self._registered = False
        # 跨轮复用 ContextEngine：以 conversation_id 为 key 缓存实例，
        # 让 IntentRecognizer.recognize 内部 get_context 命中 _context_pool，
        # 恢复 openjiuwen 框架原生的多轮上下文复用 + 压缩管道。
        self._context_engines: dict[str, Any] = {}
        # 第 3 步：TaskManagerState JSON 持久化目录。框架 TaskManager 默认纯内存，
        # 每轮 new TaskManager(config=config) 是临时实例，PAUSED 状态随实例 GC
        # 丢弃。这里以 conversation_id 为 key 落盘 TaskManagerState，让下一轮
        # decide 能 load_state 恢复上一轮 PAUSED 任务，意图裁决时能看到上一轮
        # 上下文。会话结束由 release_context_engine 清理对应文件。
        self._task_state_dir = pathlib.Path("/app/agent-workspaces/task-states")
        try:
            self._task_state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "Task continuity task_state_dir mkdir failed path=%s err=%s",
                self._task_state_dir,
                exc,
            )

    def _context_engine_for(self, conversation_id: str) -> Any:
        """返回会话级 ContextEngine 实例（跨轮复用 + 滑动窗口压缩）。

        框架原生 IntentRecognizer.recognize 用 session.get_session_id() 去
        context_engine.get_context() 取上下文；命中 _context_pool 即复用历史，
        未命中则 create_context 新建。每轮 new ContextEngine() 会让 _context_pool
        永远空，跨轮上下文完全丢失。这里以 conversation_id 缓存实例，让同一会话
        跨轮复用同一 ContextEngine + 其内部的 SessionModelContext + 压缩管道。

        第 2 步：配置 ContextEngineConfig(max_context_message_num=100) 启用
        ContextMessageBuffer 滑动窗口——SessionModelContext 内部
        `_message_buffer = ContextMessageBuffer(history_messages, config.max_context_message_num)`，
        `_if_need_resize()` 在消息数超过 max_buffer_size*2 时截断旧消息，
        只保留最近 max_buffer_size 条。框架原生压缩管道因此被激活。
        """
        cached = self._context_engines.get(conversation_id)
        if cached is not None:
            pool_size = len(getattr(cached, "_context_pool", {}) or {})
            logger.info(
                "Task continuity reused ContextEngine conversation_id=%s pool_size=%s",
                conversation_id,
                pool_size,
            )
            return cached
        engine = ContextEngine(config=ContextEngineConfig(max_context_message_num=100))
        self._context_engines[conversation_id] = engine
        logger.info(
            "Task continuity created ContextEngine conversation_id=%s max_context_message_num=100",
            conversation_id,
        )
        return engine

    def release_context_engine(self, conversation_id: str) -> None:
        """会话结束时释放 ContextEngine 缓存与持久化 TaskManagerState（由 service.py 调用）。"""
        self._context_engines.pop(conversation_id, None)
        state_path = self._task_state_path(conversation_id)
        try:
            if state_path.exists():
                state_path.unlink()
                logger.info(
                    "Task continuity released TaskManagerState conversation_id=%s",
                    conversation_id,
                )
        except OSError as exc:
            logger.warning(
                "Task continuity release TaskManagerState failed conversation_id=%s err=%s",
                conversation_id,
                exc,
            )

    def _task_state_path(self, conversation_id: str) -> pathlib.Path:
        return self._task_state_dir / f"{conversation_id}.json"

    def _load_task_state(self, conversation_id: str) -> Any | None:
        """从磁盘加载上一轮 TaskManagerState（PAUSED 任务等）。

        第 3 步：框架 TaskManager 默认纯内存，每轮 new TaskManager(config=config) 是
        临时实例，PAUSED 状态随实例 GC 丢弃。这里在 decide 入口加载上一轮持久化的
        TaskManagerState，让 task_manager.load_state 恢复 PAUSED 任务，使得
        IntentRecognizer 在做意图裁决时能看到上一轮 PAUSED 的任务上下文。
        """
        path = self._task_state_path(conversation_id)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                return None
            if hasattr(TaskManagerState, "model_validate_json"):
                state = TaskManagerState.model_validate_json(text)
            else:  # Pydantic v1 fallback
                state = TaskManagerState.parse_raw(text)
            logger.info(
                "Task continuity loaded TaskManagerState conversation_id=%s tasks=%s",
                conversation_id,
                list(getattr(state, "tasks", {}).keys()),
            )
            return state
        except Exception as exc:
            logger.warning(
                "Task continuity load TaskManagerState failed conversation_id=%s err=%s",
                conversation_id,
                exc,
            )
            return None

    def _save_task_state(self, conversation_id: str, state: Any) -> None:
        """落盘当前轮 TaskManagerState（含本轮设置的 PAUSED 状态）。

        第 3 步：decide 出口调用，让下一轮 decide 能 load_state 恢复本轮 PAUSED 任务。
        """
        path = self._task_state_path(conversation_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if hasattr(state, "model_dump_json"):
                text = state.model_dump_json(indent=2)
            else:
                text = json.dumps(state, default=str, indent=2)
            path.write_text(text, encoding="utf-8")
            logger.info(
                "Task continuity saved TaskManagerState conversation_id=%s tasks=%s",
                conversation_id,
                list(getattr(state, "tasks", {}).keys()),
            )
        except Exception as exc:
            logger.warning(
                "Task continuity save TaskManagerState failed conversation_id=%s err=%s",
                conversation_id,
                exc,
            )

    def _ensure_model_registered(self) -> None:
        if self._registered:
            return
        client_config = ModelClientConfig(
            client_provider=self._settings.model_provider,
            api_key=self._settings.model_api_key,
            api_base=self._settings.model_base_url,
            max_retries=2,
        )
        request_config = ModelRequestConfig(
            model=self._settings.model_name,
            temperature=0.0,
            top_p=0.2,
            max_tokens=4096,
        )
        Runner.resource_mgr.add_model(_MODEL_ID, _provider(Model(client_config, request_config)))
        self._registered = True

    async def _seed_tasks(
        self,
        task_manager: TaskManager,
        conversation_id: str,
        goals: list[Any],
        describe_goal: Callable[[Any], str],
    ) -> list[Task]:
        tasks: list[Task] = []
        for goal in goals:
            status = _GOAL_STATUS_TO_TASK_STATUS.get(goal.status, TaskStatus.SUBMITTED)
            tasks.append(Task(
                session_id=conversation_id,
                task_id=goal.task_id,
                task_type=goal.intent_id,
                description=describe_goal(goal),
                status=status,
                input_required_fields=(
                    {"goal_status": "needs_slots"} if status == TaskStatus.INPUT_REQUIRED else None
                ),
            ))
        if tasks:
            await task_manager.add_task(tasks)
        return tasks

    async def decide(
        self,
        *,
        conversation_id: str,
        message: str,
        goals: list[Any],
        describe_goal: Callable[[Any], str],
    ) -> ContinuityDecision:
        """对一条续接期消息做任务操作裁决。任何异常由调用方 fail-open 处理。"""
        started = time.monotonic()
        self._ensure_model_registered()
        config = ControllerConfig(
            intent_llm_id=_MODEL_ID,
            intent_confidence_threshold=0.55,
            intent_type_list=_INTENT_TYPE_LIST,
        )

        async def _recognize():
            task_manager = TaskManager(config=config)
            # 第 3 步：恢复上一轮持久化的 TaskManagerState（PAUSED 任务等），
            # 让 IntentRecognizer 在意图裁决时能看到上一轮 PAUSED 的任务上下文。
            # 必须在 _seed_tasks 之前 load_state，否则播种的 goal 任务会与
            # 上一轮 PAUSED 任务并存（这是期望行为：裁决器同时看到新旧任务）。
            prior_state = self._load_task_state(conversation_id)
            if prior_state is not None:
                try:
                    await task_manager.load_state(prior_state)
                    logger.info(
                        "Task continuity restored prior TaskManagerState conversation_id=%s prior_tasks=%s",
                        conversation_id,
                        list(getattr(prior_state, "tasks", {}).keys()),
                    )
                except Exception as exc:
                    logger.warning(
                        "Task continuity load_state failed conversation_id=%s err=%s",
                        conversation_id,
                        exc,
                    )
            tasks = await self._seed_tasks(
                task_manager, conversation_id, goals, describe_goal
            )
            context_engine = self._context_engine_for(conversation_id)
            recognizer = IntentRecognizer(config, task_manager, None, context_engine)
            event = InputEvent.from_user_input(message)
            session = Session(session_id=f"intent-recognition:{conversation_id}")
            intents = await recognizer.recognize(event, session)
            return intents, task_manager, tasks

        try:
            intents, task_manager, tasks = await _recognize()
        except json.JSONDecodeError:
            # flash 档模型偶发产出截断的 tool-call 参数，框架对参数做
            # json.loads 直接失败；换全新任务上下文重试一次，仍失败则
            # 交由调用方 fail-open。
            logger.warning("Task continuity recognizer retry on malformed tool call")
            intents, task_manager, tasks = await _recognize()

        # DEBUG: 验证 ContextEngine 跨轮复用是否生效
        context_engine = self._context_engines.get(conversation_id)
        pool_size = len(getattr(context_engine, "_context_pool", {}) or {}) if context_engine else 0
        logger.info(
            "Task continuity pre-decision conversation_id=%s goals=%s seeded_tasks=%s context_pool_size=%s",
            conversation_id,
            [getattr(g, "task_id", "?") for g in goals],
            [t.task_id for t in tasks],
            pool_size,
        )

        switched = False
        target_description: str | None = None
        target_task_id: str | None = None
        confidence = 0.0
        operation = "none"
        clarification_prompt: str | None = None
        for intent in intents:
            confidence = max(confidence, intent.confidence)
            if intent.intent_type == IntentType.CREATE_TASK and tasks:
                # 框架语义：CREATE_TASK 中断执行中任务——即任务跳转。
                switched = True
                operation = IntentType.CREATE_TASK.value
                target_description = intent.target_task_description
                await task_manager.update_task_status(
                    [task.task_id for task in tasks], TaskStatus.PAUSED
                )
            elif intent.intent_type == IntentType.SUPPLEMENT_TASK and operation == "none":
                operation = IntentType.SUPPLEMENT_TASK.value
                target_task_id = intent.target_task_id
            elif intent.intent_type == IntentType.RESUME_TASK and operation == "none":
                operation = IntentType.RESUME_TASK.value
                target_task_id = intent.target_task_id
            elif intent.intent_type == IntentType.PAUSE_TASK and operation == "none":
                operation = IntentType.PAUSE_TASK.value
            elif intent.intent_type == IntentType.CANCEL_TASK and operation == "none":
                operation = IntentType.CANCEL_TASK.value
            elif intent.intent_type == IntentType.UNKNOWN_TASK:
                clarification_prompt = intent.clarification_prompt
                if operation == "none":
                    operation = IntentType.UNKNOWN_TASK.value
        if not intents:
            operation = IntentType.UNKNOWN_TASK.value
        decision = ContinuityDecision(
            switched=switched,
            operation=operation,
            confidence=round(confidence, 4),
            target_description=target_description,
            target_task_id=target_task_id,
            clarification_prompt=clarification_prompt,
            task_count=len(tasks),
        )
        logger.info(
            "Task continuity decided operation=%s switched=%s confidence=%s target=%s description=%s seeds=%s elapsed_ms=%s",
            decision.operation,
            decision.switched,
            decision.confidence,
            decision.target_task_id,
            (decision.target_description or "")[:40],
            [task.task_id for task in tasks],
            int((time.monotonic() - started) * 1000),
        )
        # 第 3 步：落盘当前轮 TaskManagerState（含本轮设置的 PAUSED 状态），
        # 让下一轮 decide 能 load_state 恢复本轮 PAUSED 任务。任何 IO 异常
        # 仅 warning，不影响裁决结论返回（调用方 fail-open）。
        try:
            current_state = await task_manager.get_state()
            self._save_task_state(conversation_id, current_state)
        except Exception as exc:
            logger.warning(
                "Task continuity persist TaskManagerState failed conversation_id=%s err=%s",
                conversation_id,
                exc,
            )
        return decision

    async def extract_structured(
        self,
        *,
        system: str,
        message: str,
        max_tokens: int = 512,
        timeout_seconds: float = 12.0,
    ) -> str | None:
        """卡死路径的一次性结构化抽取调用（复用已注册的裁决模型）。

        仅在确定性卡死签名上由编排器触发，常态路径零调用。返回模型
        原文（调用方自行做 JSON 容错解析），任何异常交调用方 fail-open。
        """
        self._ensure_model_registered()
        model = await Runner.resource_mgr.get_model(_MODEL_ID)
        assistant = await model.invoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": message},
            ],
            temperature=0.0,
            top_p=0.2,
            max_tokens=max_tokens,
            timeout=timeout_seconds,
        )
        content = getattr(assistant, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(assistant, dict):
            value = assistant.get("content")
            return value if isinstance(value, str) else None
        return None

