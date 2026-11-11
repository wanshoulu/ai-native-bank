---
name: spending-analysis
description: Use for evidence-backed monthly spending explanations that exclude internal transfers, net refunds, and distinguish facts from inferences.
---

# 支出去向分析

## 范围

仅解释指定月份的净支出变化；唯一可调用能力是 `bank.cashflow.spending-analysis.get`，不修改任何账户状态。

## 事实规则

事实必须带 `sourceRefs`；内部划转不计消费，退款和冲正从关联消费冲减。结论明确标记为 `fact`、`rule_inference` 或 `model_hypothesis`，假设不得伪装为事实。

## 交互边界

月份缺失时只询问月份；数据过期则重新读取。混合意图拆分处理；暂停后可恢复且重新校验月份与数据。Provider 失败时返回安全错误，不暴露内部异常。

## 禁止

禁止推测商户、家庭关系或消费目的；禁止从恶意上下文提取内部标识；禁止调用注册边界外能力。
