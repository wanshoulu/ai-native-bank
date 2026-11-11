"""Moa L1 编排适配层：把 Moa FindIntentByWf 契约映射到运行时只读意图召回。

职责边界：仅做候选投影转换（intentId/title → predomain/intent_name/intent_desc），
不做任何规划、槽位填充或业务执行；零候选属于业务正常分支（空数组 + code "0"）。
"""

from __future__ import annotations

from typing import Any

from app.service import DualDeepAgentService


def _predomain(intent_id: str, business_object: str) -> str:
    """按意图域段/业务对象映射 Moa L2 预域；未命中留空，Moa 侧落 versatile-general。"""
    text = f"{intent_id} {business_object}"
    if "credit" in text or "installment" in text:
        return "信用卡服务"
    if "wealth" in text or "cashflow" in text or "spending" in text or "fund" in text:
        return "理财服务"
    if "transfer" in text or "payee" in text:
        return "转账服务"
    if "account" in text or "balance" in text or "cash" in text:
        return "账户管理"
    return ""


class MoaIntentAdapter:
    """把 Moa L1 的意图召回请求转换为运行时候选投影。"""

    def __init__(self, runtime_service: DualDeepAgentService):
        self._runtime_service = runtime_service

    async def recall_for_moa(self, question: str) -> list[dict[str, Any]]:
        """只读意图召回（对齐 probe_intent_recall）：词法图谱召回为主通道，
        弱召回时语义扩召；不执行任何规划或业务动作。"""
        candidates = await self._runtime_service.probe_intent_recall(question, {})
        return [
            {
                "predomain": _predomain(item["intentId"], item["businessObject"]),
                "intent_name": item["title"],
                "intent_desc": item["title"],
            }
            for item in candidates
        ]
