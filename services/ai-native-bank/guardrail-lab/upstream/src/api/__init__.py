"""
API 路由注册模块

将所有外部交互的 API 端点按功能模块拆分注册。
"""
from fastapi import FastAPI

from src.api import chat, common, config, content_filter, dashboard, guardrails, pii, whitelist


def register_routers(app: FastAPI) -> None:
    """注册所有 API 路由到 FastAPI 应用"""
    app.include_router(common.router)
    app.include_router(chat.router)
    app.include_router(guardrails.router)
    app.include_router(whitelist.router)
    app.include_router(pii.router)
    app.include_router(config.router)
    app.include_router(dashboard.router)
    app.include_router(content_filter.router)
