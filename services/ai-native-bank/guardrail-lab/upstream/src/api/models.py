"""
API 数据模型定义

集中管理所有 Pydantic 请求/响应模型和辅助数据类。
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, field_validator


class ChatRequest(BaseModel):
    """聊天端点的请求模型"""
    message: str
    conversation_id: str = "default"


class ChatResponse(BaseModel):
    """聊天端点的响应模型"""
    response: str
    is_blocked: bool = False
    detect_reason: str = ""
    detected_by: str | None = None
    violated_rules: List[str] = []
    response_message: str | None = None
    conversation_id: str
    timestamp: str


class SampleResponse(BaseModel):
    """测试样本端点的响应模型"""
    white_samples: List[Dict[str, str]]
    black_samples: List[Dict[str, str]]


@dataclass
class SecurityAnalysis:
    """安全分析结果数据类"""
    is_attack: bool
    attack_type: str | None
    security_risks: dict | None
    detected_by: str | None
    detect_reason: str | None = None


class GuardrailPattern(BaseModel):
    """单个正则表达式模式"""
    pattern: str


class GuardrailRule(BaseModel):
    """GuardRails规则"""
    name: str
    description: str
    patterns: List[str]
    response_message: str | None = None


class GuardrailRuleUpdate(BaseModel):
    """规则更新请求"""
    patterns: List[str]
    is_new: bool = True  # 默认为添加新规则
    description: str | None = None
    response_message: str | None = None

    @field_validator('patterns')
    def validate_patterns(cls, v):
        if not v or len(v) == 0:
            raise ValueError('正则表达式模式不能为空')
        # 过滤空字符串
        v = [p.strip() for p in v if p and p.strip()]
        if len(v) == 0:
            raise ValueError('正则表达式模式不能为空')
        return v


class EmptyRequest(BaseModel):
    """空请求体模型"""
    pass  # 完全空的模型，不要求任何字段


class GuardrailRulesResponse(BaseModel):
    """规则列表响应"""
    default_rules: List[GuardrailRule]
    custom_rules: List[GuardrailRule]
    custom_file_path: str


class WhitelistRuleCreate(BaseModel):
    """白名单规则创建/更新请求"""
    pattern: str
    description: str | None = None

    @field_validator('pattern')
    def validate_pattern(cls, v):
        if not v or not v.strip():
            raise ValueError('正则表达式模式不能为空')
        return v.strip()


class WhitelistRuleResponse(BaseModel):
    """白名单规则响应"""
    name: str
    description: str | None = None
    pattern: str
    is_active: bool
    created_at: str | None = None
    updated_at: str | None = None


class PIIDetectRequest(BaseModel):
    """PII检测请求"""
    text: str
    entity_types: Optional[List[str]] = None  # 默认为所有类型


class PIIAnonymizeRequest(BaseModel):
    """PII匿名化请求"""
    text: str
    entity_types: Optional[List[str]] = None


class PIIDetectResponse(BaseModel):
    """PII检测响应"""
    has_pii: bool
    total_count: int
    entity_counts: Dict[str, int]
    entities: List[Dict[str, Any]]
    original_text: str


class PIIAnonymizeResponse(BaseModel):
    """PII匿名化响应"""
    original_text: str
    anonymized_text: str
    has_pii: bool
    entity_counts: Dict[str, int]


class GuardrailConfigSection(BaseModel):
    """单个配置节"""
    enabled: bool
    patterns: Optional[List[str]] = None
    entities: Optional[List[str]] = None


class GuardrailConfigRequest(BaseModel):
    """安全护栏配置更新请求"""
    white_list: Optional[Dict[str, Any]] = None
    black_list: Optional[Dict[str, Any]] = None
    pii_detection: Optional[Dict[str, Any]] = None
    prompt_defense: Optional[Dict[str, Any]] = None


class GuardrailConfigResponse(BaseModel):
    """安全护栏配置响应"""
    config: Dict[str, Any]
    pii_entity_options: List[Dict[str, str]]
    security_check_prompt: str


class PIIConfigResponse(BaseModel):
    """PII配置响应"""
    id: int
    input_enabled: bool
    output_enabled: bool
    input_entities: List[str]
    output_entities: List[str]
    use_presidio: bool
    threshold: float
    anonymize_input: bool
    anonymize_output: bool
    input_action_mode: str
    output_action_mode: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PIIConfigUpdateRequest(BaseModel):
    """PII配置更新请求"""
    input_enabled: Optional[bool] = None
    output_enabled: Optional[bool] = None
    input_entities: Optional[List[str]] = None
    output_entities: Optional[List[str]] = None
    use_presidio: Optional[bool] = None
    threshold: Optional[float] = None
    anonymize_input: Optional[bool] = None
    anonymize_output: Optional[bool] = None
    input_action_mode: Optional[str] = None
    output_action_mode: Optional[str] = None


class PromptDefenseConfigResponse(BaseModel):
    """提示词防御配置响应"""
    id: int
    enabled: bool
    prompt_content: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PromptDefenseConfigUpdateRequest(BaseModel):
    """提示词防御配置更新请求"""
    enabled: Optional[bool] = None
    prompt_content: Optional[str] = None


class ContentFilterConfigResponse(BaseModel):
    """内容过滤配置响应"""
    id: int
    input_enabled: bool
    output_enabled: bool
    action_mode: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ContentFilterConfigUpdateRequest(BaseModel):
    """内容过滤配置更新请求"""
    input_enabled: Optional[bool] = None
    output_enabled: Optional[bool] = None
    action_mode: Optional[str] = None


class ContentFilterClassifyRequest(BaseModel):
    """内容过滤分类请求"""
    text: str


class ContentFilterClassifyResponse(BaseModel):
    """内容过滤分类响应"""
    category_scores: Dict[str, float]
    triggered_categories: List[str]
    max_score: float
    max_category: Optional[str]
    is_blocked: bool
    processing_time_ms: float


class CreateCategoryRequest(BaseModel):
    """创建自定义类别请求"""
    name: str
    threshold: float = 0.75
    description: str = ""
    anchors: List[str]


class UpdateCategoryRequest(BaseModel):
    """更新类别请求"""
    threshold: Optional[float] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    anchors: Optional[List[str]] = None


class AddAnchorRequest(BaseModel):
    """添加锚点请求"""
    text: str
