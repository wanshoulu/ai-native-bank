# Finance Guardrail 文本问答护栏可行性报告

评估日期：2026-09-02  
固定上游：`cba20eb4151df267dc91cde2ba20a49f6ff9db99`  
结论：`feasible_for_isolated_text_qa_prototype`

## 一句话结论

第三方项目的分层检测、LangGraph 条件短路、中文金融 PII 与输出复检思路可以复用；在不修改上游源码的隔离适配层中，文本问答护栏已经形成可重复的四态功能闭环。但它仍是第一阶段参考实现，尚不具备直接接入双智能体或生产发布的条件。

## 可复用能力

1. 输入 PII、确定性规则、Embedding 分类、受约束 LLM 裁决的分层检查。
2. 输入阶段短路、回答生成、输出内容与 PII 复检的完整 workflow。
3. LangGraph checkpoint 和非流式/安全伪流式 HTTP 行为。
4. 合成案例、双轮稳定性、误放/误封、故障注入和延迟评测方法。
5. AI Harbor Chat/Embedding 内部网关与服务令牌边界。

## 必须替换或重构的部分

- 上游二态 `allow/block` 契约与多处 fail-open 异常策略；
- 将安全裁决和回答生成混在同一个 Agent 的结构；
- 上游宽泛 PII、无序实体重叠处理和可绕过白名单；
- 匿名聊天、历史、Dashboard 与配置管理 API；
- 原文日志、Guardrails Hub 外部 telemetry 和第三方 provider 配置；
- 面向正式平台的鉴权、RBAC、审计、策略版本和限流。

## 隔离增强原型现状

- 四态结果：`allow / block / review / unavailable`；
- 本地测试 61/61；核心安全模块覆盖约 89%；
- 加固故障/契约验证 15/15；真实 HTTP/会话/流式验证 11/11；
- 回归 72/72；审定后盲测 36/36；
- 误放、误封、运行错误和跨轮不稳定均为 0；
- Guardrails Hub metrics、remote inferencing 和外部 OTLP 导出已禁用；
- 上游源码保持固定且未修改。

这些指标只对当前小规模合成集和已锁定模型配置有效，不代表生产准确率。

## 推荐的第二阶段封装边界

当前 workflow 可以先封装为用户智能体的文本安全 Hook，而不是第三个会话 Agent：

```text
User request
  -> TextInputGuardrailHook
     -> allow: 进入 User DeepAgent
     -> review: 返回安全澄清，不进入业务执行
     -> block: 返回固定安全说明
     -> unavailable: 降级或重试，不伪装成功

User-facing answer
  -> TextOutputGuardrailHook
     -> 内容复检 + 高敏 PII 复检 + 安全投影
```

护栏不拥有 Session、意图图谱、业务 Todo 或交易状态；未来“智能体行动护栏”应放在 Business DeepAgent 的 Skill/MCP 调用、确认和执行边界，单独设计。

## 进入第二阶段前仍需确认

1. 第三方许可和可迁移代码边界；
2. openJiuwen Hook 生命周期、同步/异步接口与 trace 契约；
3. 平台级策略注册、版本、灰度、RBAC 和审计模型；
4. 更大规模中文、多语言、对抗改写与多轮上下文盲测；
5. Docker/WSL 长跑稳定性与正式部署资源预算；
6. 与现有双智能体系统的封装要求由用户确认后再开始。

