"""
Dashboard 监控面板 API

包含安全统计、攻击类型分布、拦截来源、趋势和日志查询接口。
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, RequestLogCRUD
from src.api.utils import get_period_start_time

router = APIRouter()
logger = logging.getLogger("agent_service")


@router.get("/dashboard/statistics")
async def get_dashboard_statistics(
    period: str = "24h",
    db: AsyncSession = Depends(get_db)
):
    """
    Get security statistics for dashboard with time period filtering.

    Query params:
        period: "24h" | "7d" | "30d" | "all" (default: "24h")
    """
    try:
        if period not in ("24h", "7d", "30d", "all"):
            raise HTTPException(status_code=400, detail="period must be 24h, 7d, 30d, or all")

        stats = await RequestLogCRUD.get_statistics_for_period(db, period=period)
        return {
            "period": period,
            **stats.to_dict(),
            "timestamp": datetime.now().isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取Dashboard统计数据失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取统计数据失败") from e


@router.get("/dashboard/attack-types")
async def get_dashboard_attack_types(
    period: str = "24h",
    db: AsyncSession = Depends(get_db)
):
    """
    Get attack type distribution for dashboard.

    Query params:
        period: "24h" | "7d" | "30d" | "all" (default: "24h")
    """
    try:
        if period not in ("24h", "7d", "30d", "all"):
            raise HTTPException(status_code=400, detail="period must be 24h, 7d, 30d, or all")

        now = datetime.now()
        start_time = get_period_start_time(period)
        distribution = await RequestLogCRUD.get_attack_type_distribution(
            db,
            start_time=start_time,
            end_time=now,
        )
        return {
            "period": period,
            "distribution": distribution,
            "timestamp": datetime.now().isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取攻击类型分布失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取攻击类型分布失败") from e


@router.get("/dashboard/blocked-by")
async def get_dashboard_detected_by(
    period: str = "24h",
    db: AsyncSession = Depends(get_db)
):
    """
    Get detected_by source distribution for dashboard.

    Query params:
        period: "24h" | "7d" | "30d" | "all" (default: "24h")
    """
    try:
        if period not in ("24h", "7d", "30d", "all"):
            raise HTTPException(status_code=400, detail="period must be 24h, 7d, 30d, or all")

        now = datetime.now()
        start_time = get_period_start_time(period)
        distribution = await RequestLogCRUD.get_detected_by_distribution(
            db,
            start_time=start_time,
            end_time=now,
        )
        return {
            "period": period,
            "distribution": distribution,
            "timestamp": datetime.now().isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取拦截来源分布失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取拦截来源分布失败") from e


@router.get("/dashboard/trends")
async def get_dashboard_trends(
    period: str = "24h",
    db: AsyncSession = Depends(get_db)
):
    """
    Get time series trends for dashboard.

    Query params:
        period: "24h" | "7d" | "30d" | "all" (default: "24h")
    """
    try:
        if period not in ("24h", "7d", "30d", "all"):
            raise HTTPException(status_code=400, detail="period must be 24h, 7d, 30d, or all")

        now = datetime.now()
        if period == "all":
            start_time = await RequestLogCRUD.get_earliest_log_time(db)
            if start_time is None:
                return {
                    "period": period,
                    "interval_hours": 1,
                    "data": [],
                    "timestamp": now.isoformat(),
                }
            # Calculate interval to keep data points under 30
            total_hours = max(1, int((now - start_time).total_seconds() / 3600))
            if total_hours <= 24:
                interval_hours = 1
            elif total_hours <= 168:
                interval_hours = 6
            elif total_hours <= 720:
                interval_hours = 24
            else:
                interval_hours = max(1, int(total_hours / 30))
        else:
            interval_map = {
                "24h": (now - timedelta(hours=24), 1),
                "7d": (now - timedelta(days=7), 6),
                "30d": (now - timedelta(days=30), 24),
            }
            start_time, interval_hours = interval_map[period]

        data = await RequestLogCRUD.get_time_series_statistics(
            db,
            start_time=start_time,
            end_time=now,
            interval_hours=interval_hours,
        )

        # Simplify response: only keep essential fields
        simplified = []
        for item in data:
            simplified.append({
                "time": item["time"],
                "total": item["total_requests"],
                "blocked": item["blocked_requests"],
                "attacks": item["attack_requests"],
                "safe": item["safe_requests"],
            })

        return {
            "period": period,
            "interval_hours": interval_hours,
            "data": simplified,
            "timestamp": now.isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取趋势数据失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取趋势数据失败") from e


@router.get("/dashboard/logs")
async def get_dashboard_logs(
    limit: int = 20,
    offset: int = 0,
    is_attack: bool | None = None,
    is_blocked: bool | None = None,
    attack_type: str | None = None,
    period: str = "24h",
    db: AsyncSession = Depends(get_db)
):
    """
    Get paginated and filtered logs for dashboard.

    Query params:
        limit: max records per page (default: 20, max: 100)
        offset: skip offset (default: 0)
        is_attack: filter by attack status (optional)
        is_blocked: filter by blocked status (optional)
        attack_type: filter by attack type (optional)
        period: time period filter (24h, 7d, 30d, all)
    """
    try:
        if period not in ("24h", "7d", "30d", "all"):
            raise HTTPException(status_code=400, detail="period must be 24h, 7d, 30d, or all")

        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)
        now = datetime.now()
        start_time = get_period_start_time(period)

        logs, total = await RequestLogCRUD.get_filtered_logs(
            db,
            limit=limit,
            offset=offset,
            is_attack=is_attack,
            is_blocked=is_blocked,
            attack_type=attack_type,
            start_time=start_time,
            end_time=now,
        )

        return {
            "logs": [log.to_dict() for log in logs],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
            "period": period,
            "timestamp": datetime.now().isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取日志列表失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取日志列表失败") from e
