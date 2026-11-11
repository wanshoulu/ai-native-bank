#!/usr/bin/env python
"""
V4 子图推理模块：通过图谱边关系召回 + 构建子图上下文供 LLM 推理消歧。

核心设计：
- 只沿 confusableIntents 边做扩召回——这是显式声明的混淆关系，有针对性
  siblings 边太宽泛（786条覆盖全部意图），拉入的全是噪音，已停用
- 为每个候选构建子图上下文（subgraph context）：包含其边关系路径、
  disambiguationHints、相邻意图的 description，让 LLM 能基于
  图谱结构做推理消歧
- 不取代词法/embedding召回，而是在其基础上补充子图召回 + 丰富候选上下文

边类型：
1. confusableIntents — 显式声明的混淆关系（最强推理信号，唯一用于扩召回的边）
2. siblings (同 parent) — 仅用于上下文展示，不参与扩召回
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ai_native_bank.agent_runtime.subgraph_reasoning_v4")

# 子图扩召回分值：与关键词/语义召回统一在 60-80 区间
_SUBGRAPH_SCORES = (72, 69, 66, 63)
_MAX_SUBGRAPH_CANDIDATES = 4
_MAX_HOP = 2

# 词法强匹配阈值：top1 ≥ alias exact(80) 时不触发子图扩召回
_STRONG_MATCH_THRESHOLD = 80


class SubgraphReasoningV4:
    """V4 子图推理：边关系召回 + 子图上下文构建"""

    def __init__(self, adapter: Any, *, recall_settings=None):
        self._adapter = adapter
        self._enabled_flag = True
        # 从配置覆盖模块级常量
        if recall_settings is not None:
            global _SUBGRAPH_SCORES, _MAX_SUBGRAPH_CANDIDATES, _STRONG_MATCH_THRESHOLD
            self._enabled_flag = recall_settings.subgraph_enabled
            _MAX_SUBGRAPH_CANDIDATES = recall_settings.subgraph_max_candidates
            _STRONG_MATCH_THRESHOLD = recall_settings.strong_match_threshold
            scores = tuple(
                recall_settings.subgraph_score_top - i * recall_settings.subgraph_score_step
                for i in range(recall_settings.subgraph_max_candidates)
            )
            _SUBGRAPH_SCORES = scores

    @property
    def enabled(self) -> bool:
        return self._enabled_flag

    # ── 边关系召回 ──

    def expand_recall(
        self,
        candidates: list,
        exclude_ids: set[str] | None = None,
    ) -> list:
        """
        子图扩召回：在已有候选基础上，沿 confusableIntents 边召回混淆意图。

        策略：
        1. 词法已强匹配(top1≥80)时不扩召回——避免引入干扰候选
        2. 词法无候选时不扩召回——non_task 场景不应凭空产生候选
        3. 对每个已有候选（seed），沿 confusableIntents 边召回 1-hop 邻居
        4. siblings/businessObject 边不再用于扩召回（噪音太大，命中率0.5%）
        5. 返回新召回的候选列表（不含已有候选）
        """
        if not candidates:
            return []

        # 词法强匹配时不扩召回
        top1_score = max(c.score for c in candidates)
        if top1_score >= _STRONG_MATCH_THRESHOLD:
            return []

        existing = {c.intent_id for c in candidates}
        exclude = exclude_ids or set()
        exclude |= existing

        new_candidates = []
        added_ids: set[str] = set()

        # 按 seed 分数降序，高分 seed 的邻居优先
        for seed in sorted(candidates, key=lambda c: -c.score):
            if len(new_candidates) >= _MAX_SUBGRAPH_CANDIDATES:
                break

            seed_id = seed.intent_id
            # 只用 confusable 边召回
            neighbor_ids = self._get_confusable_neighbors(seed_id)
            ranked = [(nid, "confusable") for nid in sorted(neighbor_ids)]

            for rank, (neighbor_id, edge_type) in enumerate(ranked):
                if len(new_candidates) >= _MAX_SUBGRAPH_CANDIDATES:
                    break
                if neighbor_id in exclude or neighbor_id in added_ids:
                    continue

                intent = self._adapter._intents_by_id.get(neighbor_id)
                if intent is None:
                    continue
                lifecycle = intent.get("lifecycle", "active")
                if lifecycle in ("deprecated", "retired"):
                    continue

                score = _SUBGRAPH_SCORES[min(rank, len(_SUBGRAPH_SCORES) - 1)]
                candidate = self._adapter._build_candidate(
                    intent,
                    score=score,
                    evidence=[f"subgraph:{edge_type}"],
                )
                new_candidates.append(candidate)
                added_ids.add(neighbor_id)

        return new_candidates

    # ── 子图上下文构建 ──

    @staticmethod
    def _format_entry_path(intent: dict) -> str:
        """从父链路提取入口路径(如「信用卡频道>一键还款」)。

        优先用 menu_path,其次用 title 拼。
        """
        chain = intent.get("_v4_parent_chain") or []
        if not chain:
            return ""
        parts: list[str] = []
        for p in chain:
            role = p.get("node_role", "")
            if role in ("root",):
                continue
            menu_path = p.get("menu_path", "")
            title = p.get("title", "")
            # category 的 menu_path 通常已含 domain,如 "信用卡>一键还款"
            if role == "category" and menu_path:
                return menu_path
            if title:
                parts.append(title)
        return ">".join(parts) if parts else ""

    def build_context(self, candidate) -> dict:
        """
        为单个候选构建子图上下文,供 LLM 推理消歧。

        返回(真子图结构,含父链路 + 入口路径 + confusable 对比):
        {
            "intent_id": "...",
            "title": "...",
            "description": "...",
            "entry_path": "信用卡>一键还款",          ← 入口路径(关键消歧维度)
            "parent_chain": [{"role","title","menu_path"}],  ← 父链路结构
            "disambiguation_hints": "...",
            "confusable_with": [
                {"intent_id","title","entry_path","differentiator","edge":"confusable"},
                ...
            ],
            "siblings": [...],
            "business_object": "...",
        }
        """
        adapter = self._adapter
        intent = adapter._intents_by_id.get(candidate.intent_id)
        if intent is None:
            return {
                "intent_id": candidate.intent_id,
                "title": candidate.title,
                "description": "",
                "entry_path": "",
                "parent_chain": [],
                "disambiguation_hints": None,
                "confusable_with": [],
                "siblings": [],
            }

        # disambiguationHints — 最关键的消歧线索
        disambig = intent.get("disambiguationHints")
        if isinstance(disambig, list):
            disambig_text = " ".join(str(h) for h in disambig)
        else:
            disambig_text = str(disambig) if disambig else None

        # 入口路径 + 父链路 (真子图核心)
        entry_path = self._format_entry_path(intent)
        parent_chain_raw = intent.get("_v4_parent_chain") or []
        parent_chain = [
            {
                "role": p.get("node_role", ""),
                "title": p.get("title", ""),
                "menu_path": p.get("menu_path", ""),
            }
            for p in parent_chain_raw
            if p.get("node_role") != "root"
        ]

        # confusableIntents — 混淆关系邻居(带入口对比)
        confusable_list = []
        forward_ids = adapter._forward_confusable.get(candidate.intent_id, set())
        for cid in sorted(forward_ids):
            c_intent = adapter._intents_by_id.get(cid)
            if c_intent is None:
                continue
            c_entry = self._format_entry_path(c_intent)
            # differentiator: 自动推导入口差异
            if entry_path and c_entry and entry_path != c_entry:
                differentiator = f"入口不同(本意图:{entry_path} vs {c_entry})"
            elif entry_path == c_entry and entry_path:
                differentiator = "同入口,需按动作/对象区分"
            else:
                differentiator = ""
            confusable_list.append({
                "intent_id": cid,
                "title": c_intent.get("title", ""),
                "entry_path": c_entry,
                "differentiator": differentiator,
                "edge": "confusable",
            })

        # siblings (同 parent, 排除 confusable) — 同类业务兄弟
        sibling_list = []
        all_confusable = adapter._confusable_siblings.get(candidate.intent_id, set())
        all_siblings = adapter._siblings.get(candidate.intent_id, set())
        regular_siblings = all_siblings - all_confusable
        for sid in sorted(regular_siblings)[:4]:  # 最多4个兄弟
            s_intent = adapter._intents_by_id.get(sid)
            if s_intent is None:
                continue
            sibling_list.append({
                "intent_id": sid,
                "title": s_intent.get("title", ""),
                "description": s_intent.get("description", ""),
                "edge": "sibling",
            })

        return {
            "intent_id": candidate.intent_id,
            "title": intent.get("title", ""),
            "description": intent.get("description", ""),
            "entry_path": entry_path,
            "parent_chain": parent_chain,
            "disambiguation_hints": disambig_text,
            "confusable_with": confusable_list,
            "siblings": sibling_list,
            "business_object": intent.get("businessObject", ""),
        }

    def build_contexts(self, candidates: list) -> list[dict]:
        """为候选列表构建子图上下文"""
        return [self.build_context(c) for c in candidates]

    # ── 内部方法 ──

    def _get_confusable_neighbors(self, intent_id: str) -> set[str]:
        """获取意图的 confusable 邻居 ID（正反向合并）"""
        neighbors: set[str] = set()
        neighbors |= self._adapter._forward_confusable.get(intent_id, set())
        neighbors |= self._adapter._confusable_siblings.get(intent_id, set())
        neighbors.discard(intent_id)
        return neighbors
