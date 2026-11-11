"""
白名单规则管理 API

包含白名单规则的查询、重置、增删改接口。
"""
import logging

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, WhitelistRuleCRUD
from src.agent import get_agent
from src.api.models import WhitelistRuleCreate

router = APIRouter()
logger = logging.getLogger("agent_service")


@router.get("/whitelist/rules")
async def get_whitelist_rules(db: AsyncSession = Depends(get_db)):
    """获取所有白名单规则"""
    try:
        rules = await WhitelistRuleCRUD.get_all_rules(db)
        return {
            "rules": [
                {
                    "name": rule.name,
                    "description": rule.description,
                    "pattern": rule.pattern,
                    "is_active": rule.is_active,
                    "created_at": rule.created_at.isoformat() if rule.created_at else None,
                    "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
                }
                for rule in rules
            ]
        }
    except Exception as e:
        logger.error(f"获取白名单规则失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取白名单规则失败") from e


@router.post("/whitelist/rules/reset")
async def reset_whitelist_rules(db: AsyncSession = Depends(get_db)):
    """重置所有白名单规则（清空）"""
    try:
        deleted_count = await WhitelistRuleCRUD.delete_all_rules(db)
        await db.commit()

        # 重新加载Agent的白名单规则（为空）
        current_agent = get_agent()
        current_agent.reload_white_list_rules([])

        return {
            "status": "success",
            "message": f"已清空 {deleted_count} 条白名单规则"
        }
    except Exception as e:
        await db.rollback()
        logger.error(f"重置白名单规则失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="重置白名单规则失败") from e


@router.post("/whitelist/rules/{rule_name}")
async def add_or_update_whitelist_rule(
    rule_name: str,
    request: WhitelistRuleCreate,
    db: AsyncSession = Depends(get_db)
):
    """添加或更新白名单规则"""
    try:
        rule = await WhitelistRuleCRUD.create_or_update_rule(
            db,
            name=rule_name,
            pattern=request.pattern,
            description=request.description
        )
        await db.commit()

        # 重新加载Agent的白名单规则
        current_agent = get_agent()
        active_patterns = await WhitelistRuleCRUD.get_patterns_for_agent(db)
        current_agent.reload_white_list_rules(active_patterns)

        return {
            "status": "success",
            "message": f"白名单规则 '{rule_name}' 保存成功",
            "rule": {
                "name": rule.name,
                "description": rule.description,
                "pattern": rule.pattern,
                "is_active": rule.is_active,
            }
        }
    except Exception as e:
        await db.rollback()
        logger.error(f"保存白名单规则失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="保存白名单规则失败") from e


@router.delete("/whitelist/rules/{rule_name}")
async def delete_whitelist_rule(rule_name: str, db: AsyncSession = Depends(get_db)):
    """删除白名单规则"""
    try:
        deleted = await WhitelistRuleCRUD.delete_rule(db, rule_name)
        if not deleted:
            raise HTTPException(status_code=404, detail="规则不存在")

        await db.commit()

        # 重新加载Agent的白名单规则
        current_agent = get_agent()
        active_patterns = await WhitelistRuleCRUD.get_patterns_for_agent(db)
        current_agent.reload_white_list_rules(active_patterns)

        return {
            "status": "success",
            "message": f"白名单规则 '{rule_name}' 删除成功"
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"删除白名单规则失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="删除白名单规则失败") from e
