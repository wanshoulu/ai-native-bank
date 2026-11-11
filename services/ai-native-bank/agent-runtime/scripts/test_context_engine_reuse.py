# coding: utf-8
"""验证 ContextEngine 跨轮复用是否生效 + decide() 多轮裁决链路。

场景模拟用户报告的故障：
  轮1: "我要转账100" -> transfer.create goals 播种
  轮2: "有哪些理财推荐" -> 应判定 CREATE_TASK 跳转，且 ContextEngine pool_size>0 证明跨轮复用

运行：
  docker exec ai-native-bank-agent-runtime python /app/scripts/test_context_engine_reuse.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from dataclasses import dataclass

workspace = Path("/app") if Path("/app/data/ai-native-bank").exists() else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace))

from app.config import RuntimeSettings
from app.task_continuity import FrameworkTaskContinuity


@dataclass
class _Goal:
    task_id: str
    intent_id: str
    status: str = "needs_slots"
    slots: list = None

    def __post_init__(self):
        if self.slots is None:
            self.slots = []


def _describe(goal):
    return f"{goal.intent_id} (task={goal.task_id} status={goal.status})"


async def main():
    settings = RuntimeSettings.from_env()
    print(
        f"[setup] provider={settings.model_provider} name={settings.model_name} "
        f"base_url={'set' if settings.model_base_url else 'empty'} "
        f"api_key={'set' if settings.model_api_key else 'EMPTY'}"
    )

    continuity = FrameworkTaskContinuity(settings)

    conversation_id = "conv-context-engine-reuse-test"

    # 轮1: 转账意图
    print("\n=== Round 1: 我要转账100 ===")
    goals_r1 = [
        _Goal(task_id="task_transfer_1", intent_id="ccb.transfer.create", status="needs_slots"),
    ]
    try:
        d1 = await continuity.decide(
            conversation_id=conversation_id,
            message="我要转账100",
            goals=goals_r1,
            describe_goal=_describe,
        )
        print(f"[R1] switched={d1.switched} operation={d1.operation} confidence={d1.confidence}")
    except Exception as exc:
        print(f"[R1] decide failed: {type(exc).__name__}: {exc}")

    # 轮2: 意图跳转
    print("\n=== Round 2: 有哪些理财推荐 ===")
    goals_r2 = [
        _Goal(task_id="task_transfer_1", intent_id="ccb.transfer.create", status="needs_slots"),
    ]
    try:
        d2 = await continuity.decide(
            conversation_id=conversation_id,
            message="有哪些理财推荐",
            goals=goals_r2,
            describe_goal=_describe,
        )
        print(f"[R2] switched={d2.switched} operation={d2.operation} confidence={d2.confidence}")
        print(f"[R2] target_description={d2.target_description}")
    except Exception as exc:
        print(f"[R2] decide failed: {type(exc).__name__}: {exc}")

    # 检查 ContextEngine 缓存
    cached = continuity._context_engines.get(conversation_id)
    pool_size = len(getattr(cached, "_context_pool", {}) or {}) if cached else 0
    print(f"\n[CHECK] conversation_id={conversation_id} cached_engine={'yes' if cached else 'no'} pool_size={pool_size}")
    if pool_size > 0:
        print("[CHECK] PASS - ContextEngine 跨轮复用生效（pool_size > 0）")
    else:
        print("[CHECK] FAIL - ContextEngine pool 空，跨轮上下文丢失")


if __name__ == "__main__":
    asyncio.run(main())
