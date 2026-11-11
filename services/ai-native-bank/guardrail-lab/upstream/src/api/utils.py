"""
API 辅助工具函数

集中管理辅助函数、安全分析逻辑和通用工具。
"""
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import HTTPException, Request

from config.config import GUARDRAILS_CUSTOM_PATH, LOG_STR_MAX, SAMPLES_FILE_PATH
from src.api.models import SecurityAnalysis

logger = logging.getLogger("agent_service")
security_logger = logging.getLogger("security")


def load_samples() -> Dict[str, List[Dict[str, str]]]:
    """从JSON文件加载测试样本数据"""
    samples_file = SAMPLES_FILE_PATH
    try:
        with open(samples_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"测试样本文件未找到: {samples_file}")
        return {"white_samples": [], "black_samples": []}
    except json.JSONDecodeError as e:
        logger.error(f"测试样本文件JSON格式错误: {str(e)}")
        return {"white_samples": [], "black_samples": []}
    except Exception as e:
        logger.error(f"加载测试样本失败: {str(e)}", exc_info=True)
        return {"white_samples": [], "black_samples": []}


def log_interaction(
    conversation_id: str,
    message: str,
    response: str,
    is_blocked: bool,
    detect_reason: str = ""
) -> None:
    """记录交互详情"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "conversation_id": conversation_id,
        "message": message[:LOG_STR_MAX] if len(message) > LOG_STR_MAX else message,
        "response": response[:LOG_STR_MAX] if response and len(response) > LOG_STR_MAX else response,
        "is_blocked": is_blocked,
        "detect_reason": detect_reason
    }

    if is_blocked:
        security_logger.warning(f"已拦截的交互: {log_entry}")
    else:
        logger.info(f"交互记录: {log_entry}")


def validate_message(message: str) -> None:
    """验证消息是否有效"""
    if not message or not message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")


def extract_client_info(http_request: Request) -> tuple[str | None, str | None]:
    """从HTTP请求中提取客户端信息"""
    source_ip = http_request.client.host if http_request.client else None
    user_agent = http_request.headers.get("user-agent", None)
    return source_ip, user_agent


def detect_attack_type(detect_reason: Optional[str]) -> str:
    """从检测/拦截原因中检测攻击类型"""
    if not detect_reason:
        return "unknown"

    detect_reason_lower = detect_reason.lower()

    # 处理 PII 检测：提取具体的实体类型
    if "pii detected" in detect_reason_lower:
        match = re.search(r"pii detected(?:\s+in\s+output)?:\s*([A-Z_]+(?:,\s*[A-Z_]+)*)", detect_reason, re.IGNORECASE)
        if match:
            first_type = match.group(1).split(",")[0].strip()
            return first_type
        return "pii"

    # 处理内容过滤触发
    if "内容过滤触发" in detect_reason or "content_filter" in detect_reason_lower:
        match = re.search(r"内容过滤触发:\s*([a-z_]+(?:,\s*[a-z_]+)*)", detect_reason, re.IGNORECASE)
        if match:
            first_type = match.group(1).split(",")[0].strip()
            return first_type
        return "content_filter"

    # 优先处理明确的攻击类型标识
    if "prompt_injection" in detect_reason_lower or ("提示" in detect_reason and "注入" in detect_reason):
        return "prompt_injection"
    elif "financial_fraud" in detect_reason_lower or "金融欺诈" in detect_reason:
        return "financial_fraud"
    elif "data_privacy" in detect_reason_lower or "数据隐私" in detect_reason:
        return "data_privacy"
    elif "malicious_instructions" in detect_reason_lower or "恶意指令" in detect_reason:
        return "malicious_instructions"
    elif "social_engineering" in detect_reason_lower or "社会工程" in detect_reason:
        return "social_engineering"

    # 基于关键词的推断
    if "注入" in detect_reason or "inject" in detect_reason_lower:
        return "prompt_injection"
    elif "欺诈" in detect_reason or "fraud" in detect_reason_lower:
        return "financial_fraud"
    elif "隐私" in detect_reason or "privacy" in detect_reason_lower:
        return "data_privacy"
    elif "恶意" in detect_reason or "malicious" in detect_reason_lower:
        return "malicious_instructions"
    elif "社会工程" in detect_reason or "social" in detect_reason_lower:
        return "social_engineering"

    return "unknown"


def infer_detected_by(detect_reason: Optional[str], detected_by: str = None) -> str:
    """推断拦截来源"""
    if detected_by:
        return detected_by

    if not detect_reason:
        return "guardrails"

    detect_reason_lower = detect_reason.lower()

    if any(keyword in detect_reason_lower for keyword in ["guardrails", "模式匹配", "关键字", "pattern"]):
        return "guardrails"
    elif any(keyword in detect_reason_lower for keyword in ["llm", "大模型", "模型", "安全检测", "security check"]):
        return "llm"
    elif any(keyword in detect_reason_lower for keyword in ["content_filter", "内容过滤", "有害内容"]):
        return "content_filter"

    # 默认设为guardrails（因为是第一层检测）
    return "guardrails"


def build_security_risks(
    detect_reason: str | None,
    is_attack: bool,
    detected_by: str | None,
    attack_type: str | None = None,
    extra_info: dict | None = None
) -> dict:
    """构建安全风险信息字典"""
    security_risks = {
        "detect_reason": detect_reason,
        "is_attack": is_attack,
        "detected_by": detected_by,
        "timestamp": datetime.now().isoformat(),
    }

    if attack_type and attack_type != "unknown":
        security_risks["attack_type_detailed"] = attack_type

    if extra_info:
        security_risks.update(extra_info)

    return security_risks


def analyze_security_result(
    is_blocked: bool,
    detect_reason: str | None = None,
    detected_by: str | None = None,
    violated_rules: list | None = None,
    detection_summary: dict | None = None,
) -> SecurityAnalysis:
    """
    分析安全检测结果，返回统一的安全分析信息。
    """
    # 拦截模式：使用拦截信息
    if is_blocked:
        if violated_rules and len(violated_rules) > 0:
            attack_type = violated_rules[0]
        else:
            attack_type = detect_attack_type(detect_reason)

        final_detected_by = infer_detected_by(detect_reason, detected_by)
        security_risks = build_security_risks(
            detect_reason=detect_reason,
            is_attack=True,
            detected_by=final_detected_by,
            attack_type=attack_type,
            extra_info={"action_taken": "block"},
        )
        return SecurityAnalysis(
            is_attack=True,
            attack_type=attack_type,
            security_risks=security_risks,
            detected_by=final_detected_by,
            detect_reason=detect_reason,
        )

    # 检测模式：使用 detection_summary 或 detect_reason
    if detection_summary:
        final_detect_reason = detection_summary.get("detect_reason") or detect_reason
        final_detected_by = detection_summary.get("detected_by") or detected_by
        final_violated_rules = detection_summary.get("violated_rules", [])
        if final_violated_rules and len(final_violated_rules) > 0:
            attack_type = final_violated_rules[0]
        else:
            attack_type = detect_attack_type(final_detect_reason)
        final_detected_by = infer_detected_by(final_detect_reason, final_detected_by)
        security_risks = build_security_risks(
            detect_reason=final_detect_reason,
            is_attack=True,
            detected_by=final_detected_by,
            attack_type=attack_type,
            extra_info={"action_taken": "detect", **detection_summary.get("extra", {})},
        )
        return SecurityAnalysis(
            is_attack=True,
            attack_type=attack_type,
            security_risks=security_risks,
            detected_by=final_detected_by,
            detect_reason=final_detect_reason,
        )

    # 无任何检测
    return SecurityAnalysis(
        is_attack=False,
        attack_type=None,
        security_risks=None,
        detected_by=None,
        detect_reason=None,
    )


def get_period_start_time(period: str) -> datetime | None:
    """Convert period string to start datetime."""
    now = datetime.now()
    period_map = {
        "24h": now - timedelta(hours=24),
        "7d": now - timedelta(days=7),
        "30d": now - timedelta(days=30),
    }
    return period_map.get(period)


def load_custom_rules() -> List[Dict]:
    """加载自定义规则"""
    custom_rules_path = GUARDRAILS_CUSTOM_PATH
    if not custom_rules_path.exists():
        return []

    try:
        with open(custom_rules_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("custom_rules", [])
    except Exception as e:
        logger.error(f"加载自定义规则失败: {e}")
        return []


def save_custom_rules(custom_rules: List[Dict]) -> bool:
    """保存自定义规则"""
    try:
        custom_rules_path = GUARDRAILS_CUSTOM_PATH
        data = {
            "version": "1.0",
            "last_updated": datetime.now().isoformat(),
            "custom_rules": custom_rules
        }

        with open(custom_rules_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return True
    except Exception as e:
        logger.error(f"保存自定义规则失败: {e}")
        return False
