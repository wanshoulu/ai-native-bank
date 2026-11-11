#!/usr/bin/env python
"""
V4 LLM 裁决模块：用 Qwen LLM 在候选集上做最终意图判断。

设计要点：
- 只在候选集上做候选内裁决，不允许生成候选外意图
- 输入：用户查询 + 候选意图列表（含子图上下文）
- 输出：排序后的意图 ID 列表 + 置信度
- 支持 single_intent / multi_intent / ambiguous_intent 三种模式
- LLM 裁决不改写候选集，只对已有候选重新排序
- 核心升级：接收子图上下文（subgraph_contexts），让 LLM 基于图谱边关系
  （confusableIntents / disambiguationHints / siblings）做推理消歧
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("ai_native_bank.agent_runtime.llm_judge_v4")

_LLM_TIMEOUT = 30.0
_LLM_TEMPERATURE = 0.1
_DEFAULT_LLM_MODEL = "qwen-plus"
_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _load_qwen_key() -> str:
    """加载 LLM API 密钥。

    优先使用模型网关凭证（AI_BANK_MODEL_API_KEY / AI_BANK_MODEL_API_KEY_FILE），
    因为生产/Docker 环境通过 ai-harbor 模型网关调用 LLM，网关期望 runtime token；
    本地直连 Qwen API 时回退到 QWEN_KEY。
    """
    # 1) 模型网关凭证（生产 / Docker 环境）
    key = os.getenv("AI_BANK_MODEL_API_KEY", "").strip()
    if key:
        return key
    key_file = os.getenv("AI_BANK_MODEL_API_KEY_FILE", "").strip()
    if key_file:
        try:
            with open(key_file, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            pass
    # 2) 本地直连 Qwen API：QWEN_KEY 环境变量
    key = os.getenv("QWEN_KEY", "")
    if key:
        return key
    # 3) 本地开发：从 .env 文件加载 QWEN_KEY
    from pathlib import Path
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[4] / ".env",
    ]
    for p in candidates:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("QWEN_KEY=") and not line.startswith("#"):
                    return line[len("QWEN_KEY="):]
    return ""


_SYSTEM_PROMPT = """你是一个银行意图识别专家。你的任务是根据用户查询，从给定的候选意图列表中选择最匹配的意图。

你会收到每个候选意图的子图上下文，包括：
- title: 意图标题
- entry_path: 入口路径(如「信用卡>一键还款」或「首页>信用卡还款」) — 这是区分同质化意图的关键维度
- description: 意图描述
- disambiguation_hints: 消歧线索(告诉你何时该选这个意图、何时不该选)
- confusable_with: 容易混淆的意图列表(含入口路径 + differentiator 自动标注入口差异)
- siblings: 同类业务下的兄弟意图

推理规则：
1. 只能从候选列表中选择，不能生成候选外的意图
2. **优先对比 entry_path(入口路径)做消歧** — 两个意图 title/description 相似但 entry_path 不同时,需结合用户当前页面上下文判断
3. 重点参考 disambiguation_hints 做消歧判断——它明确描述了该意图与其他意图的边界
4. **⚠️强制规则(最高优先级):如果 disambiguation_hints 或 description 含「⚠️注意」「注意:query 含 XXX 时必选/不应选本意图」等强制语句,必须严格遵守,不得基于自己的语义理解/常识推断覆盖该规则。例如「query 含『办卡进度怎么样了』时必选 apply.overview」时,即使用户查询恰好是「办卡进度怎么样了」,也必须选 apply.overview,不得选其他意图。**
5. **不得基于训练数据记忆/常识推断 description 或 disambiguation_hints 的内容——只能严格引用候选列表中实际给出的文本。如果候选的 description 没有「可转额度」字样,不得在 reason 中声称该意图「覆盖可转额度」。**
6. 如果两个候选互为 confusable 关系,differentiator 字段会标注入口差异,优先按该差异消歧
7. 如果用户查询明确指向单一意图，只选一个
8. 如果用户查询涉及多个意图，按相关度排序选择多个
9. 如果用户查询模糊/有歧义，选择所有可能相关的意图
10. 返回 JSON 格式：{"selected": [{"intent_id": "xxx", "confidence": 0.95, "reason": "选择理由(需说明如何用入口路径/hints消歧)"}]}
11. confidence 取值 0-1，按降序排列
12. 最多返回 5 个意图
"""


class LLMJudgeV4:
    """V4 LLM 裁决器（支持子图推理上下文）"""

    def __init__(self, *, recall_settings=None):
        self._api_key = _load_qwen_key()
        self._base_url = os.getenv(
            "AI_BANK_LLM_BASE_URL",
            os.getenv("AI_BANK_MODEL_BASE_URL", _DEFAULT_BASE_URL),
        ).rstrip("/")
        self._model = os.getenv(
            "AI_BANK_LLM_MODEL",
            os.getenv("AI_BANK_MODEL_NAME", _DEFAULT_LLM_MODEL),
        )
        # 从配置覆盖模块级常量
        if recall_settings is not None:
            global _LLM_TIMEOUT, _LLM_TEMPERATURE
            _LLM_TIMEOUT = recall_settings.llm_timeout_seconds
            _LLM_TEMPERATURE = recall_settings.llm_temperature
        self._client: httpx.Client | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    @property
    def model_name(self) -> str:
        """当前裁决调用的模型名（评测观测面用，如实反映 AI_BANK_LLM_MODEL / AI_BANK_MODEL_NAME 配置）。"""
        return self._model

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(timeout=_LLM_TIMEOUT)
        return self._client

    def judge(
        self,
        query: str,
        candidates: list,
        intent_shape: str = "single_intent",
        subgraph_contexts: list[dict] | None = None,
    ) -> list[dict]:
        """
        LLM 候选内裁决：对候选集重新排序。

        参数：
        - subgraph_contexts: 子图推理上下文列表（来自 SubgraphReasoningV4.build_contexts），
          让 LLM 能看到每个候选的边关系和消歧线索

        返回: [{"intent_id": "xxx", "confidence": 0.95}, ...]
        """
        if not self.enabled or not candidates:
            return []
        if len(candidates) == 1:
            return [{"intent_id": candidates[0].intent_id, "confidence": 1.0}]

        # 构建候选描述：如果有子图上下文，用子图上下文；否则回退到基本描述
        candidate_descs = []
        contexts_by_id = {}
        if subgraph_contexts:
            for ctx in subgraph_contexts:
                contexts_by_id[ctx.get("intent_id", "")] = ctx

        for i, c in enumerate(candidates):
            ctx = contexts_by_id.get(c.intent_id)
            if ctx:
                candidate_descs.append(self._format_candidate_with_subgraph(i + 1, ctx))
            else:
                # 回退：从 adapter 获取基本信息
                adapter = getattr(self, "_adapter", None)
                intent = adapter._intents_by_id.get(c.intent_id) if adapter else None
                if intent:
                    candidate_descs.append(
                        f"[{i+1}] id={c.intent_id}\n"
                        f"    title={intent.get('title', '')}\n"
                        f"    description={intent.get('description', '')}"
                    )
                else:
                    candidate_descs.append(
                        f"[{i+1}] id={c.intent_id}\n    title={c.title}"
                    )

        candidate_list = "\n".join(candidate_descs)

        # 根据 intent_shape 调整提示
        if intent_shape == "multi_intent":
            task_hint = "用户查询可能涉及多个意图，请选择所有相关的。"
        elif intent_shape == "ambiguous_intent":
            task_hint = "用户查询有歧义，请仔细参考 disambiguation_hints 做消歧，选择所有可能相关的意图（至少2个）。"
        else:
            task_hint = "用户查询指向单一意图，请选择最匹配的一个。重点参考 disambiguation_hints 做判断。"

        user_prompt = f"""用户查询：「{query}」

候选意图列表（含子图上下文）：
{candidate_list}

任务要求：{task_hint}

请基于子图上下文中的 disambiguation_hints 和 confusable_with 关系做推理消歧，从候选列表中选择最匹配的意图，返回 JSON：
{{"selected": [{{"intent_id": "候选id", "confidence": 0.95, "reason": "选择理由"}}]}}
"""

        # 简单重试:LLM 超时/5xx 时重试最多 2 次,避免基础设施抖动掩盖图谱效果
        import time as _time
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                client = self._get_client()
                resp = client.post(
                    self._base_url + "/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": _LLM_TEMPERATURE,
                        "response_format": {"type": "json_object"},
                    },
                )
                if resp.status_code != 200:
                    logger.warning("LLM judge API status %d: %s", resp.status_code, resp.text[:200])
                    if attempt < max_retries and resp.status_code >= 500:
                        _time.sleep(1.0 * (attempt + 1))
                        continue
                    return []

                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                parsed = json.loads(content)
                selected = parsed.get("selected", [])
                if not isinstance(selected, list):
                    return []

                # 校验 intent_id 在候选集中
                candidate_ids = {c.intent_id for c in candidates}
                result = []
                for item in selected:
                    if not isinstance(item, dict):
                        continue
                    iid = item.get("intent_id", "")
                    conf = item.get("confidence", 0.5)
                    if iid in candidate_ids:
                        result.append({"intent_id": iid, "confidence": float(conf)})

                return result

            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                logger.warning("LLM judge timeout/error (attempt %d/%d): %s", attempt + 1, max_retries + 1, exc)
                if attempt < max_retries:
                    _time.sleep(1.0 * (attempt + 1))
                    continue
                return []
            except Exception as exc:
                logger.warning("LLM judge failed: %s", exc)
                return []
        return []

    def _format_candidate_with_subgraph(self, idx: int, ctx: dict) -> str:
        """把子图上下文格式化为 LLM 可读的描述(含入口路径 + confusable 对比)"""
        parts = [
            f"[{idx}] id={ctx.get('intent_id', '')}",
            f"    title={ctx.get('title', '')}",
            f"    entry_path={ctx.get('entry_path', '') or '(未知)'}",
            f"    description={ctx.get('description', '')}",
        ]

        # disambiguation_hints — 最关键的消歧线索
        disambig = ctx.get("disambiguation_hints")
        if disambig:
            parts.append(f"    disambiguation_hints={disambig}")

        # confusable_with — 混淆关系(带入口对比,精简版避免 prompt 过长)
        confusable = ctx.get("confusable_with", [])
        if confusable:
            conf_lines = []
            for c in confusable:
                diff = c.get("differentiator", "")
                if diff:
                    conf_lines.append(f"{c['title']}({c['intent_id']},入口:{c.get('entry_path', '') or '?'},[{diff}])")
                else:
                    conf_lines.append(f"{c['title']}({c['intent_id']},入口:{c.get('entry_path', '') or '?'})")
            parts.append(f"    confusable_with(易混淆,对比入口): {'; '.join(conf_lines)}")

        # siblings — 同类兄弟(仅列 title,避免过长)
        siblings = ctx.get("siblings", [])
        if siblings:
            sib_titles = [s["title"] for s in siblings]
            parts.append(f"    siblings(同类业务): {', '.join(sib_titles)}")

        # business_object
        biz = ctx.get("business_object", "")
        if biz:
            parts.append(f"    business_object={biz}")

        return "\n".join(parts)

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()
            self._client = None
