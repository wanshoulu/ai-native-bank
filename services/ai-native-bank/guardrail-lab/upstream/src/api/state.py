"""
API 全局状态管理

用于在 lifespan 初始化和路由模块之间共享 agent 实例，
避免循环导入问题。
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent import LangChainAgent

agent: "LangChainAgent | None" = None


def set_agent(a: "LangChainAgent") -> None:
    """设置全局 agent 实例"""
    global agent
    agent = a
