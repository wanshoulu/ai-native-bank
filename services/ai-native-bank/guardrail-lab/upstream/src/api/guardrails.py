"""
GuardRails 规则管理 API

包含规则的查询、重置、增删改接口。
"""
import logging

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from config.config import GUARDRAILS_DEFAULT_RULES
from database import get_db, GuardrailRuleCRUD
from src.agent import get_agent
from src.api.models import GuardrailRule, GuardrailRuleUpdate, EmptyRequest, GuardrailRulesResponse

router = APIRouter()
logger = logging.getLogger("agent_service")


@router.get("/guardrails/rules", response_model=GuardrailRulesResponse)
async def get_guardrails_rules(db: AsyncSession = Depends(get_db)):
    """获取所有GuardRails规则（系统+自定义）"""
    try:
        # 从数据库获取所有规则
        all_rules = await GuardrailRuleCRUD.get_all_rules(db)

        # 分离系统规则和自定义规则
        system_rules = [r for r in all_rules if r.rule_type == 'system']
        custom_rules = [r for r in all_rules if r.rule_type == 'custom']

        # 转换为响应格式
        system_rule_objects = [
            GuardrailRule(
                name=rule.name,
                description=rule.description,
                patterns=rule.patterns,
                response_message=rule.response_message
            )
            for rule in system_rules
        ]

        custom_rule_objects = [
            GuardrailRule(
                name=rule.name,
                description=rule.description,
                patterns=rule.patterns,
                response_message=rule.response_message
            )
            for rule in custom_rules
        ]

        return GuardrailRulesResponse(
            default_rules=system_rule_objects,
            custom_rules=custom_rule_objects,
            custom_file_path="database"  # 使用数据库存储
        )
    except Exception as e:
        logger.error(f"获取GuardRails规则失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取规则失败") from e


@router.post("/guardrails/rules/reset")
async def reset_guardrails_rules(request: EmptyRequest = None, db: AsyncSession = Depends(get_db)):
    """重置所有系统默认规则为config.py中的初始默认值"""
    try:
        # 删除所有系统默认规则并重新下发
        inserted_count = await GuardrailRuleCRUD.reset_system_rules(db, GUARDRAILS_DEFAULT_RULES)

        # 重新加载Agent的GuardRails
        agent = get_agent()
        rules_for_agent = await GuardrailRuleCRUD.get_rules_for_agent(db)

        if agent.reload_guardrails(rules_for_agent):
            return {"status": "success", "message": f"已重置 {inserted_count} 条默认规则为初始配置"}
        else:
            return {"status": "success", "message": f"已重置 {inserted_count} 条默认规则，GuardRails重新加载失败但已启用备用安全机制"}

    except Exception as e:
        logger.error(f"重置GuardRails默认规则失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="重置默认规则失败") from e


@router.post("/guardrails/rules/{rule_name}")
async def add_or_update_guardrail_rule(
    rule_name: str,
    rule_update: GuardrailRuleUpdate,
    db: AsyncSession = Depends(get_db)
):
    """添加或更新GuardRails规则"""
    try:
        # 检查规则是否已存在
        existing_rule = await GuardrailRuleCRUD.get_rule_by_name(db, rule_name)

        if rule_update.is_new:
            # 添加新规则模式
            if existing_rule:
                # 规则已存在，不允许添加
                raise HTTPException(
                    status_code=400,
                    detail=f"规则 '{rule_name}' 已存在，请使用不同的名称或编辑现有规则"
                )
            else:
                # 创建新规则
                rule_type = 'custom'
        else:
            # 更新现有规则模式
            if not existing_rule:
                # 规则不存在，无法更新
                raise HTTPException(
                    status_code=404,
                    detail=f"规则 '{rule_name}' 不存在"
                )

            # 检查是否为系统规则
            if existing_rule.rule_type == 'system':
                rule_type = 'system'
            else:
                rule_type = 'custom'

        # 创建或更新规则
        rule = await GuardrailRuleCRUD.create_or_update_rule(
            db,
            name=rule_name,
            patterns=rule_update.patterns,
            description=rule_update.description,
            response_message=rule_update.response_message,
            rule_type=rule_type
        )

        await db.commit()  # 提交事务

        # 重新加载Agent的GuardRails
        agent = get_agent()
        rules_for_agent = await GuardrailRuleCRUD.get_rules_for_agent(db)

        reload_success = agent.reload_guardrails(rules_for_agent)
        if reload_success:
            return {"status": "success", "message": f"规则 '{rule_name}' 更新成功"}
        else:
            # 即使重新加载失败，数据库已保存成功，返回success但附带警告信息
            logger.warning(f"规则 '{rule_name}' 已保存到数据库，但Agent重新加载GuardRails失败")
            return {"status": "success", "message": f"规则 '{rule_name}' 已保存，GuardRails重新加载失败但已启用备用安全机制"}

    except HTTPException:
        # 重新抛出HTTP异常（如400错误）
        raise
    except Exception as e:
        await db.rollback()  # 回滚事务
        logger.error(f"更新GuardRails规则失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="更新规则失败") from e


@router.delete("/guardrails/rules/{rule_name}")
async def delete_guardrail_rule(rule_name: str, db: AsyncSession = Depends(get_db)):
    """删除自定义GuardRails规则"""
    try:
        # 删除规则
        deleted = await GuardrailRuleCRUD.delete_rule(db, rule_name)

        if not deleted:
            # 检查是否是系统规则
            rule = await GuardrailRuleCRUD.get_rule_by_name(db, rule_name)
            if rule and rule.rule_type == 'system':
                raise HTTPException(status_code=400, detail="不能删除系统规则")
            else:
                raise HTTPException(status_code=404, detail="规则不存在")

        await db.commit()  # 提交事务

        # 重新加载Agent的GuardRails
        agent = get_agent()
        rules_for_agent = await GuardrailRuleCRUD.get_rules_for_agent(db)

        if agent.reload_guardrails(rules_for_agent):
            return {"status": "success", "message": f"规则 '{rule_name}' 删除成功"}
        else:
            return {"status": "warning", "message": "规则删除成功但Agent重新加载失败"}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()  # 回滚事务
        logger.error(f"删除GuardRails规则失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="删除规则失败") from e
