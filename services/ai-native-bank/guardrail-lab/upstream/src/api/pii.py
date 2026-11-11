"""
PII 检测 API

包含 PII 检测和匿名化接口。
"""
import logging
from typing import Dict

from fastapi import APIRouter, HTTPException

from config.config import LOG_STR_MAX
from src.pii_detection import get_pii_service
from src.api.models import (
    PIIDetectRequest, PIIAnonymizeRequest, PIIDetectResponse, PIIAnonymizeResponse
)

router = APIRouter()
logger = logging.getLogger("agent_service")


@router.post("/pii/detect", response_model=PIIDetectResponse)
async def detect_pii_endpoint(request: PIIDetectRequest):
    """
    PII检测端点

    检测文本中是否包含个人身份信息（PII），
    支持中国金融场景PII和英文PII检测。
    """
    try:
        if not request.text:
            raise HTTPException(status_code=400, detail="文本不能为空")

        pii_service = get_pii_service()
        entity_types = request.entity_types or ["all"]
        results = pii_service.detect_text(request.text, entity_type=entity_types[0])

        entity_counts: Dict[str, int] = {}
        for r in results:
            entity_counts[r.entity_type] = entity_counts.get(r.entity_type, 0) + 1

        return PIIDetectResponse(
            has_pii=len(results) > 0,
            total_count=len(results),
            entity_counts=entity_counts,
            entities=[
                {
                    "type": r.entity_type,
                    "text": r.text[:LOG_STR_MAX] + "..." if len(r.text) > LOG_STR_MAX else r.text,
                    "position": (r.start, r.end),
                    "score": r.score,
                }
                for r in results
            ],
            original_text=request.text,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"PII检测失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="PII检测失败") from e


@router.post("/pii/anonymize", response_model=PIIAnonymizeResponse)
async def anonymize_pii_endpoint(request: PIIAnonymizeRequest):
    """
    PII匿名化端点

    对文本中的个人身份信息进行匿名化/脱敏处理，
    返回脱敏后的文本。
    """
    try:
        if not request.text:
            raise HTTPException(status_code=400, detail="文本不能为空")

        pii_service = get_pii_service()
        entity_types = request.entity_types or ["all"]
        anonymized = pii_service.anonymize_text(request.text, entity_type=entity_types[0])

        # 检测脱敏后的结果
        results = pii_service.detect_text(request.text, entity_type=entity_types[0])
        entity_counts: Dict[str, int] = {}
        for r in results:
            entity_counts[r.entity_type] = entity_counts.get(r.entity_type, 0) + 1

        return PIIAnonymizeResponse(
            original_text=request.text,
            anonymized_text=anonymized,
            has_pii=len(results) > 0,
            entity_counts=entity_counts,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"PII匿名化失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="PII匿名化失败") from e
