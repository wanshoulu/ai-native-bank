"""
内容过滤 API

包含内容过滤配置、分类检测、类别管理和锚点管理接口。
"""
import logging

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select as sa_select

from database import get_db
from database.models import ContentFilterAnchor as CFAnchorModel
from database import (
    ContentFilterConfigCRUD, ContentFilterCategoryCRUD, ContentFilterAnchorCRUD,
)
from src.classifier.service import get_classifier_service
from src.classifier.categories import DEFAULT_THRESHOLDS, CATEGORY_ANCHORS
from src.agent import get_agent
from src.api.models import (
    ContentFilterConfigResponse, ContentFilterConfigUpdateRequest,
    ContentFilterClassifyRequest, ContentFilterClassifyResponse,
    CreateCategoryRequest, UpdateCategoryRequest, AddAnchorRequest,
)

router = APIRouter()
logger = logging.getLogger("agent_service")


# ================ 内容过滤配置 ================

@router.get("/config/content_filter", response_model=ContentFilterConfigResponse)
async def get_content_filter_config_endpoint(db: AsyncSession = Depends(get_db)):
    """
    获取内容过滤配置

    从数据库返回当前内容过滤配置。
    """
    try:
        config = await ContentFilterConfigCRUD.get_or_create_config(db)
        return ContentFilterConfigResponse(**config.to_dict())
    except Exception as e:
        logger.error(f"获取内容过滤配置失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取内容过滤配置失败") from e


@router.post("/config/content_filter")
async def update_content_filter_config_endpoint(
    request: ContentFilterConfigUpdateRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    更新内容过滤配置并动态生效

    更新数据库中的内容过滤配置，并立即重新加载Agent的内容过滤服务。
    """
    try:
        # 验证 action_mode
        if request.action_mode is not None and request.action_mode not in ("block", "detect"):
            raise HTTPException(status_code=400, detail="action_mode 必须是 'block' 或 'detect'")

        # 更新数据库配置
        config = await ContentFilterConfigCRUD.update_config(
            db,
            input_enabled=request.input_enabled,
            output_enabled=request.output_enabled,
            action_mode=request.action_mode,
        )
        await db.commit()

        # 重新加载Agent的内容过滤配置
        current_agent = get_agent()
        reload_result = current_agent.reload_content_filter_config(config.to_dict())

        logger.info(
            f"内容过滤配置已更新并重新加载: input_enabled={config.input_enabled}, "
            f"output_enabled={config.output_enabled}, action_mode={config.action_mode}"
        )

        return {
            "status": "success",
            "message": "内容过滤配置已更新并动态生效",
            "config": config.to_dict(),
            "agent_reload": reload_result,
        }

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"更新内容过滤配置失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="更新内容过滤配置失败") from e


# ================ 内容过滤分类与类别管理 ================

@router.post("/content_filter/classify", response_model=ContentFilterClassifyResponse)
async def classify_content_endpoint(request: ContentFilterClassifyRequest):
    """
    内容过滤分类检测端点

    对输入文本进行内容分类检测，返回各类别分数和是否触发拦截。
    此端点仅用于测试/调试，不修改数据库。
    """
    try:
        if not request.text:
            raise HTTPException(status_code=400, detail="文本不能为空")

        cf_service = get_classifier_service()
        result = await cf_service.classify(request.text)

        return ContentFilterClassifyResponse(
            category_scores=result.category_scores,
            triggered_categories=result.triggered_categories,
            max_score=result.max_score,
            max_category=result.max_category,
            is_blocked=result.is_blocked,
            processing_time_ms=result.processing_time_ms,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"内容过滤分类失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="内容过滤分类失败") from e


@router.get("/content_filter/categories")
async def get_content_filter_categories(db: AsyncSession = Depends(get_db)):
    """
    获取内容过滤类别列表（从数据库读取）

    返回所有类别、锚点文本和阈值，包含向量化状态。
    """
    try:
        categories = await ContentFilterCategoryCRUD.get_all_categories(db)
        result = []
        for category in categories:
            anchors = await ContentFilterAnchorCRUD.get_anchors_by_category(db, category.id)
            result.append({
                "id": category.id,
                "name": category.name,
                "description": category.description,
                "category_type": category.category_type,
                "threshold": float(category.threshold),
                "is_active": category.is_active,
                "anchors": [
                    {
                        "id": a.id,
                        "text": a.text,
                        "has_embedding": a.embedding is not None,
                        "is_active": a.is_active,
                    }
                    for a in anchors
                ],
            })

        return {"categories": result}

    except Exception as e:
        logger.error(f"获取内容过滤类别失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取内容过滤类别失败") from e


@router.post("/content_filter/categories")
async def create_category_endpoint(
    request: CreateCategoryRequest,
    db: AsyncSession = Depends(get_db)
):
    """创建自定义内容过滤类别"""
    try:
        # 检查名称是否已存在
        existing = await ContentFilterCategoryCRUD.get_category_by_name(db, request.name)
        if existing:
            raise HTTPException(status_code=400, detail=f"类别 '{request.name}' 已存在")

        # 创建类别
        category = await ContentFilterCategoryCRUD.create_category(
            db,
            name=request.name,
            threshold=request.threshold,
            description=request.description,
            category_type='custom',
            is_active=True,
        )

        # 创建锚点（无 embedding，后续自动向量化）
        for text in request.anchors:
            await ContentFilterAnchorCRUD.create_anchor(
                db, category_id=category.id, text=text, embedding=None, is_active=True
            )

        await db.commit()

        # 向量化新锚点
        cf_service = get_classifier_service()
        vectorized = await cf_service.vectorize_missing_anchors(db)
        await db.commit()

        # 重新加载到内存
        await cf_service.load_from_db(db)

        logger.info(f"创建自定义类别: {request.name}, 锚点数: {len(request.anchors)}, 向量化: {vectorized}")
        return {"status": "success", "message": f"类别 '{request.name}' 创建成功", "vectorized": vectorized}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"创建类别失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"创建类别失败: {str(e)}") from e


@router.put("/content_filter/categories/{name}")
async def update_category_endpoint(
    name: str,
    request: UpdateCategoryRequest,
    db: AsyncSession = Depends(get_db)
):
    """修改内容过滤类别（支持修改锚点）"""
    try:
        category = await ContentFilterCategoryCRUD.get_category_by_name(db, name)
        if not category:
            raise HTTPException(status_code=404, detail=f"类别 '{name}' 不存在")

        # 更新类别属性
        await ContentFilterCategoryCRUD.update_category(
            db,
            name=name,
            threshold=request.threshold,
            description=request.description,
            is_active=request.is_active,
        )

        # 如果提供了新的锚点列表，智能判断是否有变化
        anchors_changed = False
        if request.anchors is not None:
            # 获取现有锚点文本集合
            existing_anchors = await ContentFilterAnchorCRUD.get_anchors_by_category(db, category.id)
            existing_texts = set(a.text.strip() for a in existing_anchors)
            new_texts = set(text.strip() for text in request.anchors if text.strip())

            # 只有当锚点文本有变化时才删除重建
            if existing_texts != new_texts:
                # 删除旧锚点
                await ContentFilterAnchorCRUD.delete_anchors_by_category(db, category.id)
                # 创建新锚点
                for text in request.anchors:
                    if text.strip():
                        await ContentFilterAnchorCRUD.create_anchor(
                            db, category_id=category.id, text=text.strip(), embedding=None, is_active=True
                        )
                anchors_changed = True

        await db.commit()

        # 只有当锚点有变化时才重新向量化
        if anchors_changed:
            cf_service = get_classifier_service()
            vectorized = await cf_service.vectorize_missing_anchors(db)
            await db.commit()
        else:
            vectorized = 0

        # 重新加载到内存
        cf_service = get_classifier_service()
        await cf_service.load_from_db(db)

        if anchors_changed:
            logger.info(f"更新类别: {name}, 锚点已变化, 向量化: {vectorized}")
        else:
            logger.info(f"更新类别: {name}, 锚点无变化, 跳过向量化")
        return {"status": "success", "message": f"类别 '{name}' 更新成功", "vectorized": vectorized, "anchors_changed": anchors_changed}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"更新类别失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"更新类别失败: {str(e)}") from e


@router.delete("/content_filter/categories/{name}")
async def delete_category_endpoint(name: str, db: AsyncSession = Depends(get_db)):
    """删除自定义内容过滤类别（系统类别不可删除）"""
    try:
        category = await ContentFilterCategoryCRUD.get_category_by_name(db, name)
        if not category:
            raise HTTPException(status_code=404, detail=f"类别 '{name}' 不存在")
        if category.category_type == 'system':
            raise HTTPException(status_code=403, detail="系统默认类别不可删除")

        deleted = await ContentFilterCategoryCRUD.delete_category(db, name)
        if not deleted:
            raise HTTPException(status_code=500, detail=f"删除类别 '{name}' 失败")

        await db.commit()

        # 重新加载到内存
        cf_service = get_classifier_service()
        await cf_service.load_from_db(db)

        logger.info(f"删除自定义类别: {name}")
        return {"status": "success", "message": f"类别 '{name}' 已删除"}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"删除类别失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"删除类别失败: {str(e)}") from e


@router.post("/content_filter/categories/{name}/anchors")
async def add_anchor_endpoint(
    name: str,
    request: AddAnchorRequest,
    db: AsyncSession = Depends(get_db)
):
    """为类别添加锚点"""
    try:
        category = await ContentFilterCategoryCRUD.get_category_by_name(db, name)
        if not category:
            raise HTTPException(status_code=404, detail=f"类别 '{name}' 不存在")

        anchor = await ContentFilterAnchorCRUD.create_anchor(
            db, category_id=category.id, text=request.text, embedding=None, is_active=True
        )
        await db.commit()

        # 向量化新锚点
        cf_service = get_classifier_service()
        vectorized = await cf_service.vectorize_missing_anchors(db)
        await db.commit()

        # 重新加载到内存
        await cf_service.load_from_db(db)

        logger.info(f"为类别 '{name}' 添加锚点: {request.text}")
        return {"status": "success", "message": "锚点添加成功", "anchor_id": anchor.id, "vectorized": vectorized}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"添加锚点失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"添加锚点失败: {str(e)}") from e


@router.delete("/content_filter/categories/{name}/anchors/{anchor_id}")
async def delete_anchor_endpoint(
    name: str,
    anchor_id: int,
    db: AsyncSession = Depends(get_db)
):
    """删除锚点"""
    try:
        # 验证类别存在
        category = await ContentFilterCategoryCRUD.get_category_by_name(db, name)
        if not category:
            raise HTTPException(status_code=404, detail=f"类别 '{name}' 不存在")

        result = await db.execute(
            sa_select(CFAnchorModel).where(CFAnchorModel.id == anchor_id, CFAnchorModel.category_id == category.id)
        )
        anchor = result.scalar_one_or_none()
        if not anchor:
            raise HTTPException(status_code=404, detail="锚点不存在")

        await db.delete(anchor)
        await db.flush()
        await db.commit()

        # 重新加载到内存
        cf_service = get_classifier_service()
        await cf_service.load_from_db(db)

        logger.info(f"删除锚点: {anchor_id} from 类别 '{name}'")
        return {"status": "success", "message": "锚点已删除"}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"删除锚点失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"删除锚点失败: {str(e)}") from e


@router.post("/content_filter/categories/reset")
async def reset_categories_endpoint(db: AsyncSession = Depends(get_db)):
    """重置默认类别为 categories.py 的原始配置（自定义类别保留）"""
    try:
        default_categories = {
            name: {"threshold": DEFAULT_THRESHOLDS.get(name, 0.75), "description": f"系统默认类别: {name}"}
            for name in DEFAULT_THRESHOLDS.keys()
        }

        # 重置系统类别
        reset_count = await ContentFilterCategoryCRUD.reset_system_categories(db, default_categories)
        await db.flush()

        # 重新初始化系统锚点
        anchor_count = await ContentFilterAnchorCRUD.initialize_system_anchors(db, CATEGORY_ANCHORS)
        await db.flush()

        # 向量化新锚点
        cf_service = get_classifier_service()
        vectorized = await cf_service.vectorize_missing_anchors(db)
        await db.commit()

        # 重新加载到内存
        await cf_service.load_from_db(db)

        logger.info(f"重置默认类别完成: 类别 {reset_count}, 锚点 {anchor_count}, 向量化 {vectorized}")
        return {
            "status": "success",
            "message": "默认类别已重置为原始配置",
            "categories_reset": reset_count,
            "anchors_created": anchor_count,
            "vectorized": vectorized,
        }

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"重置类别失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"重置类别失败: {str(e)}") from e


@router.post("/content_filter/revectorize")
async def revectorize_endpoint(db: AsyncSession = Depends(get_db)):
    """重新向量化所有锚点（重置 embedding 后重新计算）"""
    try:
        # 重置所有锚点的 embedding 为 NULL
        reset_count = await ContentFilterAnchorCRUD.reset_all_embeddings(db)
        await db.flush()
        logger.info(f"已重置 {reset_count} 个锚点的 embedding")

        # 向量化缺失的锚点
        cf_service = get_classifier_service()
        vectorized = await cf_service.vectorize_missing_anchors(db)
        await db.commit()

        # 重新加载到内存
        loaded = await cf_service.load_from_db(db)

        logger.info(f"重新向量化完成: 重置 {reset_count} 个, 向量化 {vectorized} 个")
        return {
            "status": "success",
            "message": f"重新向量化完成",
            "reset_count": reset_count,
            "vectorized": vectorized,
            "loaded": loaded,
        }

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"重新向量化失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"重新向量化失败: {str(e)}") from e
