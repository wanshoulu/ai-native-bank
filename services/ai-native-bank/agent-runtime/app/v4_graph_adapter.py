#!/usr/bin/env python
"""
V4GraphAdapter: V4 图谱兼容接口

直接操作 V4 树结构，实现与 V2 引擎 (IntentGraphRepository) 相同的 recall() 接口。
返回相同的 IntentCandidate 对象，可无缝接入上层服务层。

核心设计:
- V4 leaf 节点的 scenes 展平为 intent 节点，直接用 V2 原始 ID 作为 intent_id
- 同一 parent 下的 leaf 互为兄弟（对应 V2 的 offers/HANDLED_BY 边）
- 打分常量、归一化函数、扩召逻辑与 V2 引擎完全一致
"""

import json
import re
import hashlib
from pathlib import Path
from typing import Any, Mapping

# ── V2 引擎打分常量 ──
# 简化分值体系：关键词不再垄断排序，与语义/子图召回统一在 60-80 区间
# 最终排序交给 LLM 裁决，召回阶段只负责把可能的候选捞进来
_SCORE_ALIAS_EXACT = 80
_SCORE_ALIAS_CONTAINS = 55
_SCORE_RECALL_TERMS = 65
_SCORE_RECALL_TERM_SETS = 75
_SCORE_NEGATIVE_EXAMPLE = -120
_SCORE_DISCARD_THRESHOLD = 40
_SCORE_PAGE_MENU = 35
_SCORE_PAGE_TAB = 25
_GRAPH_EXPAND_MAX_PER_SEED = 3
_GRAPH_EXPAND_MAX_TOTAL = 6
_GRAPH_EXPAND_MAX_SCORE = 65
# confusableIntents 兄弟使用更高权重，确保跨域混淆候选优先进入
_GRAPH_EXPAND_CONFUSABLE_WEIGHT = 0.85
_GRAPH_EXPAND_CONFUSABLE_CAP = 70
_GRAPH_EXPAND_EDGE_WEIGHTS = {
    "offers": 0.65,
    "TARGETS": 0.60,
    "HANDLED_BY": 0.55,
}


def _normalize(value: Any) -> str:
    """V2 引擎的归一化函数"""
    return re.sub(r"[\s，,。；;、！？!?]+", "", str(value or "").lower())


class V4GraphAdapter:
    """
    V4 图谱适配器：直接操作 V4 树结构，实现 V2 引擎的 recall() 接口。

    接口兼容:
    - recall(message, *, page_context, limit, graph_expand) -> list[IntentCandidate]
    - version: str
    - digest: str
    - graph_id: str
    """

    def __init__(self, v4_data: dict, path: str | Path | None = None, *, recall_settings=None):
        self._payload = v4_data
        self._path = Path(path) if path else Path(".")
        self.graph_id = v4_data.get("graphId", "ccb-mobile-bank-intents")
        self.version = v4_data.get("version", "4.0.0")
        self.digest = v4_data.get("digest", "")

        # graph_expand 开关默认值（apply_recall_settings 会按 RecallSettings 覆盖）
        self._graph_expand_enabled_flag = True

        # 从配置覆盖模块级打分常量（环境变量优先于代码默认值）
        if recall_settings is not None:
            global _SCORE_ALIAS_EXACT, _SCORE_ALIAS_CONTAINS, _SCORE_RECALL_TERMS
            global _SCORE_RECALL_TERM_SETS, _SCORE_NEGATIVE_EXAMPLE, _SCORE_DISCARD_THRESHOLD
            global _GRAPH_EXPAND_MAX_PER_SEED, _GRAPH_EXPAND_MAX_TOTAL, _GRAPH_EXPAND_MAX_SCORE
            global _GRAPH_EXPAND_CONFUSABLE_WEIGHT, _GRAPH_EXPAND_CONFUSABLE_CAP
            self._graph_expand_enabled_flag = recall_settings.graph_expand_enabled
            _SCORE_ALIAS_EXACT = recall_settings.score_alias_exact
            _SCORE_ALIAS_CONTAINS = recall_settings.score_alias_contains
            _SCORE_RECALL_TERMS = recall_settings.score_recall_terms
            _SCORE_RECALL_TERM_SETS = recall_settings.score_recall_term_sets
            _SCORE_NEGATIVE_EXAMPLE = recall_settings.score_negative_example
            _SCORE_DISCARD_THRESHOLD = recall_settings.score_discard_threshold
            _GRAPH_EXPAND_MAX_PER_SEED = recall_settings.graph_expand_max_per_seed
            _GRAPH_EXPAND_MAX_TOTAL = recall_settings.graph_expand_max_total
            _GRAPH_EXPAND_MAX_SCORE = recall_settings.graph_expand_max_score
            _GRAPH_EXPAND_CONFUSABLE_WEIGHT = recall_settings.graph_expand_confusable_weight
            _GRAPH_EXPAND_CONFUSABLE_CAP = recall_settings.graph_expand_confusable_cap

        # 从数据文件加载 skill binding 映射
        self._skill_bindings, self._intent_to_skill = self._load_skill_bindings()

        # 展平 V4 树，构建 intent 节点
        self._intents: list[dict] = []
        self._intents_by_id: dict[str, dict] = {}
        self._siblings: dict[str, set[str]] = {}  # intent_id → sibling intent_ids
        self._confusable_siblings: dict[str, set[str]] = {}  # confusableIntents 来源的兄弟 (正向+反向)
        self._forward_confusable: dict[str, set[str]] = {}  # 仅自身声明的 confusableIntents (正向)

        # V4 节点 ID → V2 intent_id 映射（confusableIntents 用 V4 节点 ID 引用）
        self._v4_to_v2: dict[str, str] = {}

        tree = v4_data.get("intentTree", [])

        # 按 parent 分组 leaf 节点
        leaves_by_parent: dict[str, list[dict]] = {}
        for node in tree:
            if node.get("node_role") == "leaf":
                pid = node.get("parent_intent_id", "")
                leaves_by_parent.setdefault(pid, []).append(node)

        # 展平 scenes 为 intent 节点
        for node in tree:
            if node.get("node_role") != "leaf":
                continue

            scenes = node.get("scenes", [])
            for scene in scenes:
                scene_type = scene.get("scene_type", "DO")

                rp = scene.get("retrieval_profile", {})
                sd = scene.get("semantic_definition", {})
                eb = scene.get("execution_binding", {})
                tb = scene.get("task_binding", {})
                dp = scene.get("decision_policy", {})

                # 直接用 V2 原始 ID 作为 intent_id
                v2_id = self._extract_v2_id(node)
                intent_id = v2_id or f"{node['id']}#{scene_type}"

                # 构建 V2 兼容的 intent 字段
                intent_dict = {
                    "id": intent_id,
                    "title": sd.get("userGoal", node.get("title", "")),
                    "description": sd.get("description", ""),
                    "action": scene_type,
                    "businessObject": self._extract_business_object(node, scene),
                    "executionMode": node.get("execution_mode") or eb.get("executionMode", "read-execute"),
                    "riskLevel": eb.get("riskLevel", "L1"),
                    "readiness": eb.get("readiness", "planned"),
                    "aliases": rp.get("aliases", []),
                    "recallTerms": rp.get("recallTerms", []),
                    "recallTermSets": rp.get("recallTermSets", []),
                    "negativeExamples": rp.get("negativeExamples", []),
                    "semanticPriority": rp.get("semanticPriority", 0),
                    "requiredSlots": [
                        s.get("name", s.get("slotId", ""))
                        for s in tb.get("slotContract", {}).get("slots", [])
                        if s.get("required", False)
                    ],
                    "capabilityBindings": tb.get("slotContract", {}).get("capabilityBindings", []),
                    "slotContract": tb.get("slotContract"),
                    "skillBinding": self._lookup_skill_binding(intent_id),
                    "businessObjectId": None,
                    "taskPolicy": {
                        "supportedShapes": dp.get("supportedShapes", ["single_intent"]),
                        "mode": dp.get("orchestrationHint", "parallel"),
                        "maxTasks": 1,
                    },
                    "contextPolicy": dp.get("contextPolicy", {}),
                    "responsePolicy": dp.get("responsePolicy"),
                    "orchestrationMode": dp.get("orchestrationHint", "parallel"),
                    "confusableIntents": sd.get("confusableIntents", []),
                    "disambiguationHints": sd.get("disambiguationHints"),
                    "lifecycle": node.get("lifecycle", "active"),
                    "domain": self._extract_domain(node, tree),
                    "menuKey": self._extract_menu_key(node, tree),
                    # V4 元数据
                    "_v4_node_id": node["id"],
                    "_v4_scene_type": scene_type,
                    "_v4_scene": scene,
                    # V4 父链路 (供 SubgraphReasoningV4 构建真子图使用)
                    "parent_intent_id": node.get("parent_intent_id", ""),
                    "node_role": node.get("node_role", "leaf"),
                    "path": node.get("path", ""),
                    "_v4_parent_chain": self._build_parent_chain(node, tree),
                }

                self._intents.append(intent_dict)
                self._intents_by_id[intent_id] = intent_dict

                # 记录 V4 节点 ID → V2 intent_id 映射
                v4_node_id = node["id"]
                if v4_node_id != intent_id:
                    self._v4_to_v2[v4_node_id] = intent_id

        # 构建兄弟关系: 同一 parent 下的所有 leaf 的 scenes 互为兄弟
        for parent_id, leaves in leaves_by_parent.items():
            # 收集该 parent 下所有 intent IDs
            sibling_ids: set[str] = set()
            for leaf in leaves:
                leaf_v2_id = self._extract_v2_id(leaf)
                for scene in leaf.get("scenes", []):
                    vid = leaf_v2_id or f"{leaf['id']}#{scene.get('scene_type', 'DO')}"
                    sibling_ids.add(vid)

            # 设置双向兄弟关系
            for sid in sibling_ids:
                self._siblings.setdefault(sid, set()).update(sibling_ids - {sid})

        # 同时构建跨 parent 的兄弟关系 (对应 V2 的 HANDLED_BY / TARGETS 边)
        # V4 中 confusableIntents 指向的节点也是兄弟
        for intent in self._intents:
            vid = intent["id"]
            for conf in intent.get("confusableIntents", []):
                # confusableIntents 可能用 V4 节点 ID 或 V2 ID 引用，两者都尝试
                other_id = self._v4_to_v2.get(conf, conf)
                other = self._intents_by_id.get(other_id)
                if other:
                    self._siblings.setdefault(vid, set()).add(other["id"])
                    self._siblings.setdefault(other["id"], set()).add(vid)
                    # 记录正向关系 (vid 自身声明的 confusable)
                    self._forward_confusable.setdefault(vid, set()).add(other["id"])
                    # confusable_siblings 双向 (正向+反向)，扩召时更高权重
                    self._confusable_siblings.setdefault(vid, set()).add(other["id"])
                    self._confusable_siblings.setdefault(other["id"], set()).add(vid)

        # 构建 action_keys (与 V2 引擎 action_keys 兼容)
        # V2 格式: "{channel}.{menuId}.{actionId}"
        self._intent_action_keys: dict[str, str] = {}
        for intent in self._intents:
            vid = intent["id"]
            action_key = self._build_action_key(intent)
            if action_key:
                self._intent_action_keys[vid] = action_key
        self._action_keys: frozenset[str] = frozenset(self._intent_action_keys.values())

        # V4 图谱校验：与 intent-graph.schema.json 对齐，确保 enum 值合法。
        # V4 路径历史上完全跳过校验，导致 schema 与数据/引擎逻辑脱节。
        self._validate_v4_graph()

    # V4 合法 enum 值（与 intent-graph.schema.json 保持同步）
    _VALID_SCHEMA_VERSIONS = frozenset({"2.0", "4.0", "4.1"})
    _VALID_READINESS = frozenset({"ready", "mock-only", "planned"})
    _VALID_EXECUTION_MODES = frozenset({
        "read-execute", "confirm-required", "knowledge-only",
        "navigation-only", "unavailable", "multi_intent",
    })

    def _validate_v4_graph(self) -> None:
        """V4 图谱轻量校验：schemaVersion、readiness、executionMode enum 合法性。
        不做 digest 校验（V4 数据可能由工具链重新生成，digest 变更频繁）。
        校验失败时记录警告但不阻塞加载——与 V2 _validate_graph 的严格模式不同，
        V4 路径选择警告而非异常，避免工具链迭代期间阻断服务启动。"""
        sv = self._payload.get("schemaVersion", "")
        if sv not in self._VALID_SCHEMA_VERSIONS:
            import logging
            logging.getLogger(__name__).warning(
                "V4 graph schemaVersion not in %s: got %r (path=%s)",
                sorted(self._VALID_SCHEMA_VERSIONS), sv, self._path,
            )
        invalid_count = 0
        for intent in self._intents:
            readiness = intent.get("readiness", "planned")
            if readiness not in self._VALID_READINESS:
                invalid_count += 1
                import logging
                logging.getLogger(__name__).warning(
                    "V4 intent readiness not in %s: intent_id=%s readiness=%r",
                    sorted(self._VALID_READINESS), intent.get("id"), readiness,
                )
            exec_mode = intent.get("executionMode", "read-execute")
            if exec_mode not in self._VALID_EXECUTION_MODES:
                invalid_count += 1
                import logging
                logging.getLogger(__name__).warning(
                    "V4 intent executionMode not in %s: intent_id=%s mode=%r",
                    sorted(self._VALID_EXECUTION_MODES), intent.get("id"), exec_mode,
                )
        if invalid_count:
            import logging
            logging.getLogger(__name__).warning(
                "V4 graph validation: %d invalid enum values (path=%s)",
                invalid_count, self._path,
            )

    def apply_recall_settings(self, recall_settings) -> None:
        """运行时注入 RecallSettings，覆盖模块级打分常量。"""
        global _SCORE_ALIAS_EXACT, _SCORE_ALIAS_CONTAINS, _SCORE_RECALL_TERMS
        global _SCORE_RECALL_TERM_SETS, _SCORE_NEGATIVE_EXAMPLE, _SCORE_DISCARD_THRESHOLD
        global _GRAPH_EXPAND_MAX_PER_SEED, _GRAPH_EXPAND_MAX_TOTAL, _GRAPH_EXPAND_MAX_SCORE
        global _GRAPH_EXPAND_CONFUSABLE_WEIGHT, _GRAPH_EXPAND_CONFUSABLE_CAP
        _SCORE_ALIAS_EXACT = recall_settings.score_alias_exact
        _SCORE_ALIAS_CONTAINS = recall_settings.score_alias_contains
        _SCORE_RECALL_TERMS = recall_settings.score_recall_terms
        _SCORE_RECALL_TERM_SETS = recall_settings.score_recall_term_sets
        _SCORE_NEGATIVE_EXAMPLE = recall_settings.score_negative_example
        _SCORE_DISCARD_THRESHOLD = recall_settings.score_discard_threshold
        _GRAPH_EXPAND_MAX_PER_SEED = recall_settings.graph_expand_max_per_seed
        _GRAPH_EXPAND_MAX_TOTAL = recall_settings.graph_expand_max_total
        _GRAPH_EXPAND_MAX_SCORE = recall_settings.graph_expand_max_score
        _GRAPH_EXPAND_CONFUSABLE_WEIGHT = recall_settings.graph_expand_confusable_weight
        _GRAPH_EXPAND_CONFUSABLE_CAP = recall_settings.graph_expand_confusable_cap
        # graph_expand 开关（默认 True，可经 AI_BANK_RECALL_GRAPH_EXPAND_ENABLED=false 关闭）
        self._graph_expand_enabled_flag = recall_settings.graph_expand_enabled

    def _load_skill_bindings(self) -> tuple[dict[str, dict], dict[str, str]]:
        """从数据文件加载 skill binding 映射 (skill-bindings.v1.json)"""
        bindings_file = self._path.parent / "skill-bindings.v1.json"
        if not bindings_file.exists():
            return {}, {}
        try:
            data = json.loads(bindings_file.read_text(encoding="utf-8"))
            return data.get("skillBindings", {}), data.get("intentToSkill", {})
        except (OSError, json.JSONDecodeError):
            return {}, {}

    def _extract_v2_id(self, node: dict) -> str | None:
        """从 V4 节点的 observability.sourceRecords 提取 V2 原始 ID"""
        for rec in node.get("observability", {}).get("sourceRecords", []):
            ref = rec.get("ref", "")
            if ref.startswith("v2-source:"):
                return ref[len("v2-source:"):]
        return None

    def _build_action_key(self, intent: dict) -> str | None:
        """从 intent_id 构建 action_key (格式: channel.menuId.actionId)"""
        # intent_id 即 V2 ID，格式: ccb.{domain}.{menuKey}.{actionId}
        intent_id = intent.get("id", "")
        if intent_id:
            parts = intent_id.split(".")
            if len(parts) >= 4:
                return f"{parts[1]}.{parts[2]}.{parts[3]}" if len(parts) == 4 else f"{parts[1]}.{'.'.join(parts[2:-1])}.{parts[-1]}"
            elif len(parts) == 3:
                return f"{parts[1]}.{parts[1]}.{parts[2]}"
        return None

    def _lookup_skill_binding(self, intent_id: str) -> dict | None:
        """根据 V2 intent_id 查找对应的 skill binding"""
        skill_id = self._intent_to_skill.get(intent_id)
        if skill_id is None:
            return None
        meta = self._skill_bindings.get(skill_id)
        if meta is None:
            return None
        return {"skillId": skill_id, "version": meta["version"], "digest": meta["digest"]}

    def _extract_business_object(self, node: dict, scene: dict) -> str:
        """从 V4 节点提取业务对象"""
        bindings = node.get("bindings", {})
        biz_objs = bindings.get("businessObjects", [])
        if biz_objs:
            return biz_objs[0]
        tb = scene.get("task_binding", {})
        return tb.get("actionCode", "unknown")

    def _extract_domain(self, node: dict, tree: list) -> str:
        """从 V4 树提取 domain (对应 V2 的 channel)"""
        parent_id = node.get("parent_intent_id", "")
        # 向上找到 domain 节点
        while parent_id:
            parent = next((n for n in tree if n["id"] == parent_id), None)
            if parent is None:
                break
            if parent.get("node_role") == "domain":
                return parent.get("title", "")
            parent_id = parent.get("parent_intent_id", "")
        return ""

    def _build_parent_chain(self, node: dict, tree: list) -> list[dict]:
        """构建节点向上的父链路 (root → domain → category → leaf)。

        返回格式: [{"id","title","node_role","scope_in","menu_path"}]
        用于 SubgraphReasoningV4 构建真子图,提供入口路径等消歧维度。
        """
        chain: list[dict] = []
        parent_id = node.get("parent_intent_id", "")
        while parent_id:
            parent = next((n for n in tree if n["id"] == parent_id), None)
            if parent is None:
                break
            menu = parent.get("bindings", {}).get("menu", {}) or {}
            chain.append({
                "id": parent["id"],
                "title": parent.get("title", ""),
                "node_role": parent.get("node_role", ""),
                "scope_in": parent.get("scope_in", ""),
                "menu_path": menu.get("path", ""),
                "navigationTerms": parent.get("navigationTerms", []),
            })
            parent_id = parent.get("parent_intent_id", "")
        # 反转: root → ... → leaf 的父链路
        chain.reverse()
        return chain

    def _extract_menu_key(self, node: dict, tree: list) -> str:
        """从 V4 树提取 menuKey"""
        bindings = node.get("bindings", {})
        menu = bindings.get("menu", {})
        path = menu.get("path", "")
        if path:
            # "信用卡>申请信用卡" → "credit.apply"
            # 从 navigationTerms 或 scope_in 获取
            category = next(
                (n for n in tree if n["id"] == node.get("parent_intent_id")),
                None,
            )
            if category:
                nav_terms = category.get("navigationTerms", [])
                if nav_terms:
                    return nav_terms[0]
        return ""

    @classmethod
    def load(cls, path: str | Path) -> "V4GraphAdapter":
        graph_path = Path(path)
        payload = json.loads(graph_path.read_text(encoding="utf-8"))
        return cls(payload, graph_path)

    def recall(
        self,
        message: str,
        *,
        page_context: dict[str, Any] | None = None,
        limit: int = 8,
        graph_expand: bool = True,
    ) -> list:
        """
        词法召回 + 图扩展，与 V2 引擎 recall() 接口完全兼容。
        返回 list[IntentCandidate]。
        """
        from app.intent_graph import IntentCandidate

        normalized = _normalize(message)
        if not normalized:
            return []

        context = page_context or {}
        tab = str(context.get("tab") or "").strip().lower()
        menu_key = str(context.get("menuKey") or context.get("menu_key") or "").strip().lower()

        candidates: list[IntentCandidate] = []

        for intent in self._intents:
            if intent.get("lifecycle", "active") != "active":
                continue

            score = 0
            evidence: list[str] = []

            # alias 打分 — 遍历所有别名取最高分，exact(110) 优先于 contains(75)
            alias_score = 0
            for alias in intent.get("aliases", []):
                alias_normalized = _normalize(alias)
                if len(alias_normalized) >= 2 and (
                    alias_normalized in normalized or normalized in alias_normalized
                ):
                    if alias_normalized == normalized:
                        alias_score = _SCORE_ALIAS_EXACT
                        break
                    elif alias_score < _SCORE_ALIAS_CONTAINS:
                        alias_score = _SCORE_ALIAS_CONTAINS
            if alias_score > 0:
                score += alias_score
                evidence.append("alias")

            # recallTerms 打分
            for term in intent.get("recallTerms", []):
                term_normalized = _normalize(term)
                if len(term_normalized) >= 2 and term_normalized in normalized:
                    score += _SCORE_RECALL_TERMS
                    evidence.append("graph_term")
                    break

            # recallTermSets 打分
            for term_set in intent.get("recallTermSets", []):
                if all(_normalize(term) in normalized for term in term_set):
                    score += _SCORE_RECALL_TERM_SETS
                    evidence.append("graph_term_set")
                    break

            # 页面上下文加分
            if menu_key and intent.get("menuKey", "").lower() == menu_key:
                score += _SCORE_PAGE_MENU
                evidence.append("page_menu")
            elif tab and intent.get("domain", "").lower() == tab:
                score += _SCORE_PAGE_TAB
                evidence.append("page_tab")

            # 负例惩罚
            if any(_normalize(item) in normalized for item in intent.get("negativeExamples", [])):
                score += _SCORE_NEGATIVE_EXAMPLE
                evidence.append("negative_example")

            if score < _SCORE_DISCARD_THRESHOLD:
                continue

            candidates.append(self._build_candidate(intent, score=score, evidence=evidence))

        maximum = min(max(int(limit), 1), 8)

        # 图扩展（受 graph_expand 形参 + RecallSettings.graph_expand_enabled 双重控制）
        if graph_expand and self._graph_expand_enabled_flag and candidates:
            candidates = self._graph_expand(candidates)

        return sorted(
            candidates,
            key=lambda item: (-item.semantic_priority, -item.score, item.intent_id),
        )[:maximum]

    def _graph_expand(self, candidates: list) -> list:
        """2-hop 兄弟扩召，confusableIntents 兄弟优先且更高分"""
        from app.intent_graph import IntentCandidate

        if not candidates:
            return candidates

        existing_ids: set[str] = {c.intent_id for c in candidates}
        expanded: list[IntentCandidate] = []
        total_added = 0

        # 按分数降序遍历: 高分 seed 的 confusable 兄弟优先扩召获得高分，
        # 避免低分 seed 先扩召同一兄弟占位 existing_ids 导致高分 seed 无法扩召
        for seed in sorted(candidates, key=lambda c: -c.score):
            if total_added >= _GRAPH_EXPAND_MAX_TOTAL:
                break
            added_for_seed = 0

            # 优先级: 正向 confusable > 反向 confusable > 普通兄弟
            forward_ids = self._forward_confusable.get(seed.intent_id, set())
            all_confusable = self._confusable_siblings.get(seed.intent_id, set())
            reverse_confusable = all_confusable - forward_ids
            all_siblings = self._siblings.get(seed.intent_id, set())
            regular_ids = all_siblings - all_confusable

            # 按优先级合并: 正向 confusable → 反向 confusable → 普通兄弟
            ordered_siblings = sorted(forward_ids) + sorted(reverse_confusable) + sorted(regular_ids)

            for sibling_id in ordered_siblings:
                if added_for_seed >= _GRAPH_EXPAND_MAX_PER_SEED:
                    break
                if total_added >= _GRAPH_EXPAND_MAX_TOTAL:
                    break
                if sibling_id in existing_ids:
                    continue

                intent = self._intents_by_id.get(sibling_id)
                if intent is None:
                    continue
                # 允许 active 和 planned 节点，仅跳过 deprecated/retired
                lifecycle = intent.get("lifecycle", "active")
                if lifecycle in ("deprecated", "retired"):
                    continue

                is_confusable = sibling_id in all_confusable
                if is_confusable:
                    # confusable 兄弟: 更高权重，确保跨域混淆候选优先进入
                    expansion_score = min(
                        int(seed.score * _GRAPH_EXPAND_CONFUSABLE_WEIGHT),
                        _GRAPH_EXPAND_CONFUSABLE_CAP,
                    )
                    evidence_label = "graph_expand:confusable"
                else:
                    expansion_score = min(
                        int(seed.score * _GRAPH_EXPAND_EDGE_WEIGHTS["offers"]),
                        _GRAPH_EXPAND_MAX_SCORE,
                    )
                    evidence_label = "graph_expand:offers"

                if expansion_score < _SCORE_DISCARD_THRESHOLD:
                    continue

                expanded.append(self._build_candidate(
                    intent,
                    score=expansion_score,
                    evidence=[evidence_label],
                ))
                existing_ids.add(sibling_id)
                added_for_seed += 1
                total_added += 1

        return candidates + expanded

    def _safe_slot_contract(self, value):
        """安全处理 slotContract: V4 场景可能没有 executionBinding，返回 None 而非抛异常"""
        if value is None:
            return None
        if not isinstance(value, Mapping):
            return None
        # 检查是否有必需字段
        if "executionBinding" not in value:
            return None
        # 延迟导入规避循环依赖；必须放在 try 外，
        # 否则 NameError 会被下方 except 吞掉导致 slot_contract 永远为 None
        from app.intent_graph import _project_slot_contract
        try:
            return _project_slot_contract(value)
        except (KeyError, TypeError, ValueError):
            return None

    def _build_candidate(self, intent: dict, *, score: int, evidence: list):
        """构建 IntentCandidate，与 V2 引擎 _candidate() 一致"""
        from app.intent_graph import (
            IntentCandidate,
            _project_skill_binding,
            _project_task_policy,
            _project_context_policy,
            _project_orchestration_mode,
        )

        return IntentCandidate(
            intent_id=intent["id"],
            title=intent["title"],
            action=intent["action"],
            business_object=intent["businessObject"],
            execution_mode=intent["executionMode"],
            risk_level=intent["riskLevel"],
            required_slots=list(intent.get("requiredSlots", [])),
            capability_bindings=list(intent.get("capabilityBindings", [])),
            score=score,
            evidence=list(dict.fromkeys(evidence)),
            rule_version=f"v{self.version}.{self.digest[7:19]}",
            semantic_priority=(
                int(intent.get("semanticPriority", 0))
                if "graph_term_set" in evidence else 0
            ),
            slot_contract=self._safe_slot_contract(intent.get("slotContract")),
            skill_binding=_project_skill_binding(intent.get("skillBinding")),
            task_policy=_project_task_policy(intent.get("taskPolicy")),
            context_policy=_project_context_policy(intent.get("contextPolicy")),
            response_policy=intent.get("responsePolicy"),
            business_object_id=intent.get("businessObjectId"),
            orchestration_mode=_project_orchestration_mode(intent.get("orchestrationMode")),
            readiness=intent.get("readiness"),
            confusable_intents=tuple(intent.get("confusableIntents") or ()),
            disambiguation_hints=(
                str(intent["disambiguationHints"])
                if intent.get("disambiguationHints") else None
            ),
        )

    @property
    def path(self) -> Path:
        """与 V2 引擎 path 属性兼容 (semantic_recall 依赖此属性定位索引文件)"""
        return self._path

    @property
    def action_keys(self) -> frozenset[str]:
        """与 V2 引擎 action_keys 兼容"""
        return self._action_keys

    @property
    def all_capability_ids(self) -> frozenset[str]:
        """图谱中所有意图引用的能力 ID（评测端点在未提供能力列表时使用）。"""
        return frozenset(
            cap_id
            for intent in self._intents
            for cap_id in intent.get("capabilityBindings", [])
        )

    def action_key_for_intent(self, intent_id: str) -> str:
        """与 V2 引擎 action_key_for_intent 兼容"""
        action_key = self._intent_action_keys.get(intent_id)
        if action_key is None:
            from app.intent_graph import IntentGraphError
            raise IntentGraphError("navigation intent binding is missing")
        return action_key

    def candidate_for_intent(self, intent_id: str, *, score: int, evidence: list[str]):
        """与 V2 引擎 candidate_for_intent 兼容"""
        intent = self._intents_by_id.get(intent_id)
        if intent is None:
            from app.intent_graph import IntentGraphError
            raise IntentGraphError("FAQ intent binding is missing")
        return self._build_candidate(intent, score=score, evidence=evidence)

    @property
    def intent_graph_identity(self) -> tuple[str, str]:
        """与 service.py 的 intent_graph_identity 兼容"""
        return self.version, self.digest

    def validate_capability_bindings(
        self,
        candidates: list,
        available_capability_ids: set[str],
    ) -> None:
        """与 V2 引擎 validate_capability_bindings 兼容"""
        from app.intent_graph import IntentGraphError

        missing = sorted(
            {
                capability_id
                for candidate in candidates
                for capability_id in candidate.capability_bindings
                if capability_id not in available_capability_ids
            }
        )
        if missing:
            raise IntentGraphError("runtime capability binding missing: " + ",".join(missing))

    def iter_active_intents(self):
        """与 V2 引擎 iter_active_intents() 兼容"""
        from app.intent_graph import IntentProjection

        projections = [
            IntentProjection(
                intent_id=intent["id"],
                title=intent.get("title", ""),
                aliases=list(intent.get("aliases", [])),
                description=intent.get("description", ""),
            )
            for intent in self._intents
            if intent.get("lifecycle", "active") == "active"
        ]
        projections.sort(key=lambda item: item.intent_id)
        return projections
