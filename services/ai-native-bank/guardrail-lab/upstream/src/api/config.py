"""
配置管理 API（安全护栏、PII、提示词防御）

注意：内容过滤配置已移至 content_filter.py 模块，
保持同领域接口的内聚性。
"""
import logging

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from config.config import (
    get_guardrail_config, update_guardrail_config,
    PII_ENTITY_OPTIONS, SECURITY_CHECK_PROMPT,
)
from database import (
    get_db,
    PIIConfigCRUD, PromptDefenseConfigCRUD,
)
from src.agent import get_agent
from src.api.models import (
    GuardrailConfigRequest, GuardrailConfigResponse,
    PIIConfigResponse, PIIConfigUpdateRequest,
    PromptDefenseConfigResponse, PromptDefenseConfigUpdateRequest,
)

router = APIRouter()
logger = logging.getLogger("agent_service")


# ================ 安全护栏配置 ================

@router.get("/config/guardrail", response_model=GuardrailConfigResponse)
async def get_guardrail_config_endpoint():
    """
    获取当前安全护栏配置

    返回运行时配置、PII实体选项列表、提示词防御内容。
    """
    try:
        config = get_guardrail_config()
        return GuardrailConfigResponse(
            config=config,
            pii_entity_options=PII_ENTITY_OPTIONS,
            security_check_prompt=SECURITY_CHECK_PROMPT.strip(),
        )
    except Exception as e:
        logger.error(f"获取安全护栏配置失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取配置失败") from e


@router.post("/config/guardrail")
async def update_guardrail_config_endpoint(request: GuardrailConfigRequest):
    """
    更新安全护栏配置并动态生效

    更新后自动通知Agent重新加载配置，无需重启服务。
    """
    try:
        updates = {}
        if request.white_list is not None:
            updates["white_list"] = request.white_list
        if request.black_list is not None:
            updates["black_list"] = request.black_list
        if request.pii_detection is not None:
            updates["pii_detection"] = request.pii_detection
        if request.prompt_defense is not None:
            updates["prompt_defense"] = request.prompt_defense

        updated_config = update_guardrail_config(updates)

        # 通知Agent重新加载运行时配置
        current_agent = get_agent()
        reload_result = current_agent.reload_runtime_config()

        logger.info(f"安全护栏配置已更新: {list(updates.keys())}")

        return {
            "status": "success",
            "message": "配置已更新并动态生效",
            "config": updated_config,
            "agent_reload": reload_result,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新安全护栏配置失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="更新配置失败") from e


# ================ PII 配置 ================

@router.get("/config/pii", response_model=PIIConfigResponse)
async def get_pii_config_endpoint(db: AsyncSession = Depends(get_db)):
    """
    获取PII检测配置

    从数据库返回当前PII检测配置。
    """
    try:
        config = await PIIConfigCRUD.get_or_create_config(db)
        return PIIConfigResponse(**config.to_dict())
    except Exception as e:
        logger.error(f"获取PII配置失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取PII配置失败") from e


@router.post("/config/pii")
async def update_pii_config_endpoint(
    request: PIIConfigUpdateRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    更新PII检测配置并动态生效

    更新数据库中的PII配置，并立即重新加载Agent的PII检测服务。
    """
    try:
        # 更新数据库配置
        config = await PIIConfigCRUD.update_config(
            db,
            input_enabled=request.input_enabled,
            output_enabled=request.output_enabled,
            input_entities=request.input_entities,
            output_entities=request.output_entities,
            use_presidio=request.use_presidio,
            threshold=request.threshold,
            anonymize_input=request.anonymize_input,
            anonymize_output=request.anonymize_output,
            input_action_mode=request.input_action_mode,
            output_action_mode=request.output_action_mode,
        )
        await db.commit()

        # 重新加载Agent的PII配置
        current_agent = get_agent()
        reload_result = current_agent.reload_pii_config(config.to_dict())

        logger.info(f"PII配置已更新并重新加载: input_enabled={config.input_enabled}, output_enabled={config.output_enabled}")

        return {
            "status": "success",
            "message": "PII配置已更新并动态生效",
            "config": config.to_dict(),
            "agent_reload": reload_result,
        }

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"更新PII配置失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="更新PII配置失败") from e


# ================ 提示词防御配置 ================

@router.get("/config/prompt_defense", response_model=PromptDefenseConfigResponse)
async def get_prompt_defense_config_endpoint(db: AsyncSession = Depends(get_db)):
    """
    获取提示词防御配置

    从数据库返回当前提示词防御配置。
    """
    try:
        config = await PromptDefenseConfigCRUD.get_or_create_config(db)
        return PromptDefenseConfigResponse(**config.to_dict())
    except Exception as e:
        logger.error(f"获取提示词防御配置失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取提示词防御配置失败") from e


@router.post("/config/prompt_defense")
async def update_prompt_defense_config_endpoint(
    request: PromptDefenseConfigUpdateRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    更新提示词防御配置并动态生效

    更新数据库中的提示词防御配置，并立即重新加载Agent的配置。
    """
    try:
        # 更新数据库配置
        config = await PromptDefenseConfigCRUD.update_config(
            db,
            enabled=request.enabled,
            prompt_content=request.prompt_content,
        )
        await db.commit()

        # 重新加载Agent的提示词防御配置
        current_agent = get_agent()
        reload_result = current_agent.reload_prompt_defense_config(config.to_dict())

        logger.info(f"提示词防御配置已更新并重新加载: enabled={config.enabled}")

        return {
            "status": "success",
            "message": "提示词防御配置已更新并动态生效",
            "config": config.to_dict(),
            "agent_reload": reload_result,
        }

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"更新提示词防御配置失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="更新提示词防御配置失败") from e


@router.post("/config/prompt_defense/reset")
async def reset_prompt_defense_config_endpoint(db: AsyncSession = Depends(get_db)):
    """
    重置提示词防御配置为系统默认值

    将 SECURITY_CHECK_PROMPT 从 config.py 重新覆盖到数据库。
    """
    try:
        # 更新数据库配置为默认值
        config = await PromptDefenseConfigCRUD.update_config(
            db,
            prompt_content=SECURITY_CHECK_PROMPT,
        )
        await db.commit()

        # 重新加载Agent的提示词防御配置
        current_agent = get_agent()
        reload_result = current_agent.reload_prompt_defense_config(config.to_dict())

        logger.info("提示词防御配置已重置为系统默认值")

        return {
            "status": "success",
            "message": "提示词防御配置已重置为系统默认值",
            "config": config.to_dict(),
            "agent_reload": reload_result,
        }

    except Exception as e:
        await db.rollback()
        logger.error(f"重置提示词防御配置失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="重置提示词防御配置失败") from e
