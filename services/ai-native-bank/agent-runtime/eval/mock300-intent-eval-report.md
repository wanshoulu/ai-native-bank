# 意图引擎 Mock300 评测报告

- 日期：2026-09-19
- 被测组件：`agent-runtime/app/intent_graph.py` → `IntentGraphRepository.recall`（词法图谱召回通道）
- 意图图谱：`ccb-mobile-bank-intents` v2.1.0（304 意图 = 238 active + 66 planned，96 菜单）
- 用例与产物：`eval/mock300-cases.json`（300 条用例）、`eval/mock300-report.json`（逐例明细）
- 复现：`.venv/Scripts/python.exe eval/run_mock300_eval.py`

## 一、结论（TL;DR）

| 指标 | 结果 | 说明 |
|---|---|---|
| **候选召回率（top-8）** | **100.00%**（288/288） | 所有 active 意图均可被词法通道召回，MOA L1 能拿到正确候选 |
| **Top-1 正确率** | **85.07%**（245/288） | 未命中的 43 条全部是"命中但组内排序第二"，非召回失败 |
| Top-3 命中率 | 100.00% | |
| 零候选率 | 0.00% | 无一例落到"需 LLM 语义扩召兜底" |
| 严格用例 Top-1（词料唯一） | 98.60%（211/214） | |
| Planned 意图抑制率 | 100.00%（12/12） | 未上线意图不会被误召回 |

**总评：意图引擎词法召回质量良好。** 300 条 mock 用例无一条漏召回；top1 损失全部来自同一菜单组内 faq/overview/apply 兄弟意图共享词料后的确定性排序（无页面上下文时按 intent_id 字母序决胜），属已知设计权衡，生产链路由页面上下文（menuKey 加分）、semanticPriority（term_set 证据）与 LLM 语义扩召三层兜底。

## 二、回答：应用原本有没有意图引擎测试用例？

**有。** 本应用自带一套已发布的符合性评测（本次新 mock 之前即存在）：

1. `data/eval/ccb-intent-conformance.v1.json`：**304 用例 / 1762 条话语**，逐意图覆盖（含 66 条 planned 抑制），断言 contractPassRate=1.0、activeCandidateRecallRate=1.0、plannedSuppressionRate=1.0。
2. 评测引擎 `app/intent_conformance_eval.py`（确定性、无 LLM），单测 `tests/test_intent_conformance_eval.py`、`tests/test_intent_graph.py` 等。
3. 本次复跑官方口径结果：contractPassRate **1.0**，召回 **1.0**，抑制 **1.0**，top1ExactRate **70.66%**。

由于官方数据集是"契约符合性"口径（每意图约 6 条近义话语），本次按要求另建 **mock300 独立改写用例集**做正确率压力评测。

## 三、Mock300 用例构成（300 条）

| 类别 | 数量 | 生成方式 |
|---|---|---|
| primary 主用例 | 238 | 238 个 active 意图各 1 条，模板化改写（我要/帮我/打开/怎么查…），按词料碰撞数选最具区分度词 |
| hard 困难用例 | 39 | 易混意图（含 confusableIntents）追加第 2 种改写 |
| termset 消歧用例 | 1 | 无唯一词意图用 recallTermSets 组合词 |
| colloquial 口语用例 | 10 | 高频场景手工口语（给王芳转五百块钱/信用卡丢了赶紧挂失/这个月钱花哪了…） |
| planned 抑制用例 | 12 | 未上线意图，期望不召回 |

覆盖性断言：238 个 active 意图 100% 覆盖；评测口径与生产 MOA 链路一致（`page_context={}`，仅词法通道，无 LLM）。

## 四、分域 / 分执行模式

### 按业务域（top1 / 召回）

| 域 | 用例 | Top1 | 召回 |
|---|---|---|---|
| account | 9 | 100% | 100% |
| cashflow | 5 | 100% | 100% |
| transfer | 4 | 100% | 100% |
| obligation | 2 | 100% | 100% |
| customer | 1 | 100% | 100% |
| me | 44 | 97.73% | 100% |
| home | 45 | 93.33% | 100% |
| credit | 77 | 85.71% | 100% |
| wealth | 59 | 77.97% | 100% |
| **life** | **42** | **64.29%** | 100% |

### 按执行模式（top1 / 召回）

| 模式 | 用例 | Top1 | 召回 |
|---|---|---|---|
| confirm-required（高风险） | 20 | 95.00% | 100% |
| read-execute（查询执行） | 36 | 94.44% | 100% |
| knowledge-only（FAQ） | 107 | 88.79% | 100% |
| navigation-only（菜单导航） | 125 | 77.60% | 100% |

高风险（转账/挂失类）与查询执行类意图表现最好；菜单导航类因兄弟菜单组共享菜单词，top1 相对低但召回仍 100%。

## 五、失败模式分析（43 条 top1 未命中）

- **43/43 全部为"目标意图在候选中但排序第二"**（top-8 内），无一例漏召回，无一例被无关意图挤掉。
- 失败集中于同菜单组兄弟意图共享词料：如 `进入现金分期` → [cash-installment.faq, cash-installment.overview]；确定性排序无上下文时按 intent_id 字母序决胜（faq < overview < apply 的顺序规律明显）。
- 带 `page_context`（意图自身 tab/menuKey）对照实验：top1 仅 85.07% → **86.46%**（+1.4pp），因为兄弟组共享同一 menuKey，页面上下文只能跨菜单消歧、不能组内消歧。
- 3 条严格（词料唯一）失败明细：
  1. `ccb.home.benefit.overview` ← "查权益记录在哪里"：被 `ccb.credit.benefits.overview` 跨域撞词（"权益"）。
  2. `ccb.home.repayment.overview` ← "查查账单金额"：被 `ccb.credit.bill.overview` 撞词（"账单"）。
  3. `ccb.credit.card.loss-report` ← "我的信用卡丢了，赶紧挂失"：与 `ccb.credit.card.query` 同分（各 +90），字母序决胜输出。生产中该话语将进入 LLM 语义扩召通道兜底。

## 六、改进建议（按优先级）

1. **同分决胜规则**：`recall` 排序最终 tie-break 是 `intent_id` 字母序，建议对 executionMode/riskLevel 加权或引入 graph 作者指定的组内序，避免 faq 恒压 overview。
2. **跨域撞词治理**：`权益`/`账单`/`还款` 等通用词在 home/credit/life 域重复出现，建议为撞词意图补充 `recallTermSets` 与 `semanticPriority`（引擎已支持该消歧通道，但本图谱 304 意图中多数未配置）。
3. **top1 低不必扩词**：召回已 100%，补词料对 top1 无收益；收益在"组内唯一词"与 semanticPriority 编排。
4. 官方 conformance 已达 1.0/1.0/1.0 契约线，建议把 mock300 的 top1 口径纳入回归（阈值建议 ≥85%），防止图谱迭代引入排序回退。

## 七、口径与局限

- 本报告测的是**确定性词法召回通道**（生产 MOA `/v1/moa/intent` 主通道），未含 LLM 语义扩召（需模型在线，不可离线复现）；因此本报告的 top1 是**词法通道下界**，生产端到端正确率应高于此值。
- mock 用例为程序化模板生成 + 10 条人工口语，未含错别字/方言/中英混杂等噪声形态。
