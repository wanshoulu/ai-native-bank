"""Environment configuration with redacted public status."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field


OPENJIUWEN_VERSION = "0.1.17.post1"
USER_AGENT_ID = "ai-native-bank-user-agent"
BUSINESS_AGENT_ID = "ai-native-bank-business-agent"


class RuntimeNotReadyError(RuntimeError):
    """Runtime cannot execute a real DeepAgent request."""


@dataclass(frozen=True)
class RecallSettings:
    """V4 四层召回管线的可配置阈值与分值。

    所有字段支持环境变量覆盖，默认值与原硬编码常量一致。
    调整时只需设置环境变量，无需修改代码或重建镜像。
    """

    # ── 词法层（v4_graph_adapter） ──
    score_alias_exact: int = 80
    score_alias_contains: int = 55
    score_recall_terms: int = 65
    score_recall_term_sets: int = 75
    score_negative_example: int = -120
    score_discard_threshold: int = 40
    graph_expand_enabled: bool = True
    graph_expand_max_per_seed: int = 3
    graph_expand_max_total: int = 6
    graph_expand_max_score: int = 65
    graph_expand_confusable_weight: float = 0.85
    graph_expand_confusable_cap: int = 70

    # ── 子图层（subgraph_reasoning_v4） ──
    subgraph_enabled: bool = True
    subgraph_score_top: int = 72
    subgraph_score_step: int = 3
    subgraph_max_candidates: int = 4
    strong_match_threshold: int = 80

    # ── 语义层（semantic_recall_v4） ──
    semantic_similarity_threshold: float = 0.45
    semantic_score_top: int = 78
    semantic_score_step: int = 3
    semantic_max_candidates: int = 4

    # ── LLM 裁决层（llm_judge_v4） ──
    llm_timeout_seconds: float = 30.0
    llm_temperature: float = 0.1

    @classmethod
    def from_env(cls) -> "RecallSettings":
        return cls(
            # ── 词法层 ──
            score_alias_exact=_bounded_int(
                os.getenv("AI_BANK_RECALL_SCORE_ALIAS_EXACT"), 80, 50, 100
            ),
            score_alias_contains=_bounded_int(
                os.getenv("AI_BANK_RECALL_SCORE_ALIAS_CONTAINS"), 55, 20, 80
            ),
            score_recall_terms=_bounded_int(
                os.getenv("AI_BANK_RECALL_SCORE_RECALL_TERMS"), 65, 30, 90
            ),
            score_recall_term_sets=_bounded_int(
                os.getenv("AI_BANK_RECALL_SCORE_RECALL_TERM_SETS"), 75, 40, 95
            ),
            score_negative_example=_bounded_int(
                os.getenv("AI_BANK_RECALL_SCORE_NEGATIVE_EXAMPLE"), -120, -200, -10
            ),
            score_discard_threshold=_bounded_int(
                os.getenv("AI_BANK_RECALL_SCORE_DISCARD_THRESHOLD"), 40, 0, 60
            ),
            graph_expand_enabled=_env_bool(
                os.getenv("AI_BANK_RECALL_GRAPH_EXPAND_ENABLED"), True
            ),
            graph_expand_max_per_seed=_bounded_int(
                os.getenv("AI_BANK_RECALL_GRAPH_EXPAND_MAX_PER_SEED"), 3, 1, 10
            ),
            graph_expand_max_total=_bounded_int(
                os.getenv("AI_BANK_RECALL_GRAPH_EXPAND_MAX_TOTAL"), 6, 1, 20
            ),
            graph_expand_max_score=_bounded_int(
                os.getenv("AI_BANK_RECALL_GRAPH_EXPAND_MAX_SCORE"), 65, 30, 90
            ),
            graph_expand_confusable_weight=_bounded_float(
                os.getenv("AI_BANK_RECALL_GRAPH_EXPAND_CONFUSABLE_WEIGHT"),
                0.85,
                0.0,
                1.0,
            ),
            graph_expand_confusable_cap=_bounded_int(
                os.getenv("AI_BANK_RECALL_GRAPH_EXPAND_CONFUSABLE_CAP"), 70, 40, 95
            ),
            # ── 子图层 ──
            subgraph_enabled=_env_bool(
                os.getenv("AI_BANK_RECALL_SUBGRAPH_ENABLED"), True
            ),
            subgraph_score_top=_bounded_int(
                os.getenv("AI_BANK_RECALL_SUBGRAPH_SCORE_TOP"), 72, 50, 90
            ),
            subgraph_score_step=_bounded_int(
                os.getenv("AI_BANK_RECALL_SUBGRAPH_SCORE_STEP"), 3, 1, 10
            ),
            subgraph_max_candidates=_bounded_int(
                os.getenv("AI_BANK_RECALL_SUBGRAPH_MAX_CANDIDATES"), 4, 1, 12
            ),
            strong_match_threshold=_bounded_int(
                os.getenv("AI_BANK_RECALL_STRONG_MATCH_THRESHOLD"), 80, 50, 100
            ),
            # ── 语义层 ──
            semantic_similarity_threshold=_bounded_float(
                os.getenv("AI_BANK_RECALL_SEMANTIC_SIMILARITY_THRESHOLD"),
                0.45,
                0.1,
                0.9,
            ),
            semantic_score_top=_bounded_int(
                os.getenv("AI_BANK_RECALL_SEMANTIC_SCORE_TOP"), 78, 50, 95
            ),
            semantic_score_step=_bounded_int(
                os.getenv("AI_BANK_RECALL_SEMANTIC_SCORE_STEP"), 3, 1, 10
            ),
            semantic_max_candidates=_bounded_int(
                os.getenv("AI_BANK_RECALL_SEMANTIC_MAX_CANDIDATES"), 4, 1, 12
            ),
            # ── LLM 裁决层 ──
            llm_timeout_seconds=_bounded_float(
                os.getenv("AI_BANK_RECALL_LLM_TIMEOUT_SECONDS"), 30.0, 3.0, 60.0
            ),
            llm_temperature=_bounded_float(
                os.getenv("AI_BANK_RECALL_LLM_TEMPERATURE"), 0.1, 0.0, 1.0
            ),
        )


@dataclass(frozen=True)
class RuntimeSettings:
    runtime_token: str = field(default="", repr=False)
    model_provider: str = ""
    model_name: str = ""
    model_base_url: str = field(default="", repr=False)
    model_api_key: str = field(default="", repr=False)
    workspace_root: str = "/app/agent-workspaces"
    checkpointer_path: str = field(
        default="/app/agent-workspaces/checkpoints/openjiuwen.db",
        repr=False,
    )
    host: str = "0.0.0.0"
    port: int = 18120
    max_iterations: int = 8
    run_timeout_seconds: int = 180
    precompleted_resume_timeout_seconds: int = 30
    intent_graph_path: str = "/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v2.1.0.json"
    faq_path: str = "/app/data/ai-native-bank/faq/ccb-mobile-bank-faq.v1.json"
    resource_registry_root: str = "/app/data/ai-native-bank/registries"
    business_skill_root: str = "/app/data/ai-native-bank/skills"
    node_gateway_url: str = "http://ai-harbor:3000/api/ai-bank/v1/internal/agent-capability-invocations"
    lexicon_dir: str = "/app/data/ai-native-bank/guardrail/lexicon"
    input_rail_url: str = "http://ai-native-bank-guardrail-runtime:8000"
    input_rail_mode: str = "off"
    input_rail_timeout_ms: int = 30_000
    permit_ttl_seconds: int = 30
    memory_mode: str = "off"
    memory_control_url: str = "http://agent-memory-control:18130"
    memory_service_token: str = field(default="", repr=False)
    intent_memory_policy_path: str = "/app/data/ai-native-bank/registries/intent-memory-policies.v1.json"
    user_memory_policy_path: str = "/app/data/ai-native-bank/registries/user-agent-memory-policies.v1.json"
    node_memory_command_url: str = "http://ai-harbor:3000/api/ai-bank/v1/internal/agent-memory-commands"
    user_memory_mode: str = "off"
    task_memory_mode: str = "off"
    memory_timeout_ms: int = 5000
    kg_gateway_url: str = "http://ai-harbor:3000"
    kg_graph_id: str = "bank-semantic-graph/bank-semantic-graph-v1"
    kg_timeout_ms: int = 3000
    intent_graph_source: str = "file"
    tugraph_uri: str = "bolt://host.docker.internal:7687"
    tugraph_rest_url: str = "http://localhost:7070"
    tugraph_user: str = field(default="admin", repr=False)
    tugraph_password: str = field(default="", repr=False)
    tugraph_database: str = "default"
    recall_settings: RecallSettings = field(default_factory=RecallSettings)

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        allowed_memory_modes = {"off", "shadow", "read_only", "governed_write"}
        memory_mode = _enum_value(
            os.getenv("AI_BANK_MEMORY_MODE"),
            fallback="off",
            allowed=allowed_memory_modes,
        )
        return cls(
            runtime_token=_read_secret("AI_BANK_AGENT_RUNTIME_TOKEN", "AI_BANK_AGENT_RUNTIME_TOKEN_FILE"),
            model_provider=os.getenv("AI_BANK_MODEL_PROVIDER", "").strip(),
            model_name=os.getenv("AI_BANK_MODEL_NAME", "").strip(),
            model_base_url=os.getenv("AI_BANK_MODEL_BASE_URL", "").strip(),
            model_api_key=_read_secret("AI_BANK_MODEL_API_KEY", "AI_BANK_MODEL_API_KEY_FILE"),
            workspace_root=os.getenv("AI_BANK_AGENT_WORKSPACE_ROOT", "/app/agent-workspaces").strip(),
            checkpointer_path=os.getenv(
                "AI_BANK_AGENT_CHECKPOINTER_PATH",
                "/app/agent-workspaces/checkpoints/openjiuwen.db",
            ).strip(),
            host=os.getenv("AI_BANK_AGENT_RUNTIME_HOST", "0.0.0.0").strip(),
            port=_bounded_int(os.getenv("AI_BANK_AGENT_RUNTIME_PORT"), 18120, 1, 65535),
            max_iterations=_bounded_int(os.getenv("AI_BANK_AGENT_MAX_ITERATIONS"), 8, 2, 20),
            run_timeout_seconds=_bounded_int(
                os.getenv("AI_BANK_RUN_TIMEOUT_SECONDS"), 180, 60, 900
            ),
            precompleted_resume_timeout_seconds=_bounded_int(
                os.getenv("AI_BANK_PRECOMPLETED_RESUME_TIMEOUT_SECONDS"), 30, 10, 120
            ),
            intent_graph_path=os.getenv(
                "AI_BANK_INTENT_GRAPH_PATH",
                "/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v2.1.0.json",
            ).strip(),
            faq_path=os.getenv(
                "AI_BANK_FAQ_PATH",
                "/app/data/ai-native-bank/faq/ccb-mobile-bank-faq.v1.json",
            ).strip(),
            resource_registry_root=os.getenv(
                "AI_BANK_RESOURCE_REGISTRY_ROOT",
                "/app/data/ai-native-bank/registries",
            ).strip(),
            business_skill_root=os.getenv(
                "AI_BANK_BUSINESS_SKILL_ROOT",
                "/app/data/ai-native-bank/skills",
            ).strip(),
            node_gateway_url=os.getenv(
                "AI_BANK_NODE_GATEWAY_URL",
                "http://ai-harbor:3000/api/ai-bank/v1/internal/agent-capability-invocations",
            ).strip(),
            lexicon_dir=os.getenv(
                "AI_BANK_LEXICON_DIR",
                "/app/data/ai-native-bank/guardrail/lexicon",
            ).strip(),
            input_rail_url=os.getenv(
                "AI_BANK_INPUT_RAIL_URL",
                "http://ai-native-bank-guardrail-runtime:8000",
            ).strip(),
            input_rail_mode=_enum_value(
                os.getenv("AI_BANK_INPUT_RAIL_MODE"),
                fallback="off",
                allowed={"off", "shadow", "enforce"},
            ),
            input_rail_timeout_ms=_bounded_int(
                os.getenv("AI_BANK_INPUT_RAIL_TIMEOUT_MS"), 30_000, 500, 30_000
            ),
            permit_ttl_seconds=_bounded_int(
                os.getenv("AI_BANK_PERMIT_TTL_SECONDS"), 30, 5, 120
            ),
            memory_mode=memory_mode,
            memory_control_url=os.getenv(
                "AI_BANK_MEMORY_CONTROL_URL", "http://agent-memory-control:18130"
            ).strip(),
            memory_service_token=_read_secret(
                "AI_BANK_MEMORY_SERVICE_TOKEN", "AI_BANK_MEMORY_SERVICE_TOKEN_FILE"
            ),
            intent_memory_policy_path=os.getenv(
                "AI_BANK_INTENT_MEMORY_POLICY_PATH",
                "/app/data/ai-native-bank/registries/intent-memory-policies.v1.json",
            ).strip(),
            user_memory_policy_path=os.getenv(
                "AI_BANK_USER_MEMORY_POLICY_PATH",
                "/app/data/ai-native-bank/registries/user-agent-memory-policies.v1.json",
            ).strip(),
            node_memory_command_url=os.getenv(
                "AI_BANK_NODE_MEMORY_COMMAND_URL",
                "http://ai-harbor:3000/api/ai-bank/v1/internal/agent-memory-commands",
            ).strip(),
            user_memory_mode=_enum_value(
                os.getenv("AI_BANK_USER_MEMORY_MODE"),
                fallback=memory_mode,
                allowed=allowed_memory_modes,
            ),
            task_memory_mode=_enum_value(
                os.getenv("AI_BANK_TASK_MEMORY_MODE"),
                fallback=memory_mode,
                allowed=allowed_memory_modes,
            ),
            memory_timeout_ms=_bounded_int(
                os.getenv("AI_BANK_MEMORY_TIMEOUT_MS"), 5000, 200, 10000
            ),
            kg_gateway_url=os.getenv(
                "AI_BANK_KG_GATEWAY_URL",
                "http://ai-harbor:3000",
            ).strip(),
            kg_graph_id=os.getenv(
                "AI_BANK_KG_GRAPH_ID",
                "bank-semantic-graph/bank-semantic-graph-v1",
            ).strip(),
            kg_timeout_ms=_bounded_int(
                os.getenv("AI_BANK_KG_TIMEOUT_MS"), 3000, 500, 10_000
            ),
            intent_graph_source=_enum_value(
                os.getenv("AI_BANK_INTENT_GRAPH_SOURCE"),
                fallback="file",
                allowed={"file", "tugraph"},
            ),
            tugraph_uri=os.getenv("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687").strip(),
            tugraph_rest_url=os.getenv("AI_BANK_TUGRAPH_REST_URL", "http://localhost:7070").strip(),
            tugraph_user=os.getenv("AI_BANK_TUGRAPH_USER", "admin").strip(),
            tugraph_password=os.getenv("AI_BANK_TUGRAPH_PASSWORD", "").strip(),
            tugraph_database=os.getenv("AI_BANK_TUGRAPH_DATABASE", "default").strip(),
            recall_settings=RecallSettings.from_env(),
        )

    @property
    def model_configured(self) -> bool:
        return all((self.model_provider, self.model_name, self.model_base_url, self.model_api_key))

    def public_status(
        self,
        *,
        framework_available: bool,
        checkpointer_available: bool | None = None,
    ) -> dict[str, object]:
        persistence_ready = (
            framework_available
            if checkpointer_available is None
            else checkpointer_available
        )
        return {
            "provider": "openjiuwen",
            "version": OPENJIUWEN_VERSION,
            "ready": bool(
                framework_available
                and persistence_ready
                and self.model_configured
                and self.runtime_token
            ),
            "checkpointerProvider": "persistence" if persistence_ready else None,
            "modelConfigured": self.model_configured,
            "modelProvider": self.model_provider or None,
            "modelName": self.model_name or None,
            "agents": [USER_AGENT_ID, BUSINESS_AGENT_ID],
            "memoryMode": self.memory_mode,
            "userMemoryMode": self.user_memory_mode,
            "taskMemoryMode": self.task_memory_mode,
            "userMemoryPolicyConfigured": bool(self.user_memory_policy_path),
            "memoryWriteConfigured": bool(
                self.memory_service_token
                and self.node_memory_command_url
                and "governed_write" in {self.user_memory_mode, self.task_memory_mode}
            ),
            "memoryConfigured": bool(
                self.memory_mode == "off" or self.memory_service_token
            ),
        }


def _bounded_int(value: str | None, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else fallback
    except ValueError:
        return fallback
    return parsed if minimum <= parsed <= maximum else fallback


def _env_bool(value: str | None, fallback: bool) -> bool:
    """解析环境变量为 bool，支持 1/true/yes/on（不区分大小写）。空值或非法值返回 fallback。"""
    if value is None or value == "":
        return fallback
    return value.strip().lower() in ("1", "true", "yes", "on")


def _bounded_float(
    value: str | None, fallback: float, minimum: float, maximum: float
) -> float:
    """解析环境变量为 float，带边界校验。"""
    try:
        parsed = float(value) if value is not None else fallback
    except (ValueError, TypeError):
        return fallback
    return parsed if minimum <= parsed <= maximum else fallback


def _enum_value(
    value: str | None,
    *,
    fallback: str,
    allowed: set[str],
) -> str:
    normalized = (value or fallback).strip().lower()
    return normalized if normalized in allowed else fallback


def _read_secret(value_name: str, file_name: str) -> str:
    direct = os.getenv(value_name, "").strip()
    if direct:
        return _validated_secret(direct)
    path = os.getenv(file_name, "").strip()
    if not path:
        return ""
    try:
        metadata = os.stat(path)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            return ""
        with open(path, "r", encoding="utf-8") as secret_file:
            return _validated_secret(secret_file.read().strip())
    except (OSError, ValueError):
        return ""


def _validated_secret(value: str) -> str:
    if not 16 <= len(value) <= 512 or any(character.isspace() for character in value):
        raise ValueError("invalid secret configuration")
    return value
