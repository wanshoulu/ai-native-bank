"""
通用 API 端点

包含根路径、健康检查、测试样本和基础统计接口。
"""
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from config.config import SERVER_CONFIG
from database import get_db, RequestLogCRUD
from src.api.models import SampleResponse
from src.api.utils import load_samples
from src.api import state

router = APIRouter()
logger = logging.getLogger("agent_service")


@router.get("/")
async def root():
    """返回服务信息"""
    return {
        "service": "智能体安全防护后端服务",
        "version": "1.0.0",
        "status": "在线",
        "endpoints": {
            "chat": "/chat (POST) - 聊天接口",
            "samples": "/samples (GET) - 获取测试用例",
            "health": "/health (GET) - 健康检查"
        }
    }


@router.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "agent_status": "initialized" if state.agent else "not_initialized"
    }


@router.get("/samples", response_model=SampleResponse)
async def get_samples():
    """
    获取测试样本数据

    返回白名单（正常）和黑名单（攻击）数据
    """
    try:
        samples = load_samples()
        return SampleResponse(
            white_samples=samples.get("white_samples", []),
            black_samples=samples.get("black_samples", [])
        )
    except Exception as e:
        logger.error(f"加载测试样本失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="加载测试样本失败") from e


@router.get("/statistics")
async def get_statistics(db: AsyncSession = Depends(get_db)):
    """
    获取安全统计数据

    返回总请求数、已拦截数、攻击数、安全率等信息
    """
    try:
        stats = await RequestLogCRUD.get_statistics(db)
        return {
            **stats.to_dict(),
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"获取统计数据失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取统计数据失败") from e
