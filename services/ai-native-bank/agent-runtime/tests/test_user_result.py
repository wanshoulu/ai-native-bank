import json
import os
import unittest
from pathlib import Path

from app.user_result import project_user_result


class UserResultProjectionTests(unittest.TestCase):
    def test_wealth_purchase_projects_authoritative_receipt_card(self):
        result = project_user_result([{
            "capability_id": "bank.wealth.purchase.create",
            "output": {
                "id": "wpay_private_001",
                "orderId": "wpay_private_001",
                "holdingId": "wh_private_001",
                "productId": "prod_w_anxin",
                "productName": "乾元-安鑫（按日）开放式固定收益类理财产品",
                "amount": {"amountMinor": 1000000, "currency": "CNY", "formatted": "¥10,000.00"},
                "accountId": "acct_salary_6217",
                "status": "CONFIRMED",
                "occurredAt": "2026-09-07T10:30:00.000+08:00",
            },
        }])

        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "wealth_purchase_receipt")
        self.assertEqual(result.title, "理财申购已完成")
        self.assertIn("乾元-安鑫", result.summary)
        self.assertIn("¥10,000.00", result.model_dump_json(by_alias=True))
        visible = result.model_dump_json(by_alias=True)
        self.assertNotIn("prod_w_anxin", visible)
        self.assertNotIn("acct_salary_6217", visible)
        self.assertNotIn("wpay_private_001", visible)

    def test_current_graph_has_governed_read_execute_intents(self):
        graph = json.loads(
            Path(
                os.getenv(
                    "AI_BANK_INTENT_GRAPH_PATH",
                    "/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v1.0.0.json",
                )
            ).read_text(encoding="utf-8")
        )
        actual = {
            node["id"]: tuple(node["capabilityBindings"])
            for node in graph["nodes"]
            if node.get("executionMode") == "read-execute"
        }

        self.assertEqual(actual, {
            "ccb.account.balance.query": (
                "bank.account.list",
                "bank.account.balance.get",
                "bank.account.detail.get",
            ),
            "ccb.account.detail.query": (
                "bank.account.list",
                "bank.account.detail.get",
            ),
            "ccb.account.transactions.query": (
                "bank.account.list",
                "bank.account.transactions.list",
                "bank.account.detail.get",
            ),
            "ccb.cashflow.salary-plan": (
                "bank.cashflow.salary-plan.get",
            ),
            "ccb.cashflow.spending-explain": (
                "bank.cashflow.spending-analysis.get",
            ),
            "ccb.credit.bill.overview": (
                "bank.credit-card.list",
                "bank.credit-card.bill.get",
            ),
            "ccb.credit.card-manage.security-query": (
                "bank.credit-card.list",
                "bank.credit-card.settings.get",
            ),
            "ccb.credit.card.query": ("bank.credit-card.list",),
            "ccb.credit.card.transactions.query": (
                "bank.credit-card.list",
                "bank.credit-card.detail.get",
                "bank.credit-card.transactions.list",
            ),
            "ccb.credit.repay.overview": (
                "bank.credit-card.list",
                "bank.credit-card.bill.get",
            ),
            "ccb.customer.profile.query": (
                "bank.customer.profile.get",
            ),
            "ccb.home.repayment.overview": (
                "bank.credit-card.list",
                "bank.credit-card.bill.get",
            ),
            "ccb.transfer.payee.query": ("bank.payee.list",),
            "ccb.wealth.advisor.recommend": (
                "bank.wealth.advisory.context.get",
            ),
            "ccb.wealth.deposit.overview": ("bank.wealth.product.list",),
            "ccb.wealth.dragon-money.overview": ("bank.wealth.holding.list",),
            "ccb.wealth.fund.overview": ("bank.wealth.product.list",),
            "ccb.wealth.overview.overview": ("bank.wealth.holding.list",),
            "ccb.wealth.portfolio.overview": ("bank.wealth.holding.list",),
            "ccb.wealth.wealth-product.overview": (
                "bank.wealth.product.list",
            ),
        })

    def test_wealth_advisory_projects_preference_gate_and_traceable_candidates(self):
        missing = project_user_result([{
            "capability_id": "bank.wealth.advisory.context.get",
            "output": {
                "recommendationStatus": "preferences_required",
                "missingPreferenceFields": ["investmentGoal", "horizonMonths"],
                "requiredActions": [{
                    "code": "PROVIDE_INVESTMENT_PREFERENCES",
                    "title": "补充投资目标和预计持有期限",
                    "message": "我希望稳健增值，预计持有12个月",
                }],
                "recommendations": [],
                "riskAssessment": {"status": "VALID", "level": "C2"},
                "dataSources": [],
            },
        }])
        self.assertIsNotNone(missing)
        self.assertEqual(missing.kind, "wealth_advisory_preferences_required")
        # 缺失字段由补录表单呈现；正文与条目不再枚举同一批字段名（避免卡片内容重复）
        self.assertNotIn("投资目标", missing.summary)
        self.assertNotIn("预计持有期限", missing.summary)
        self.assertEqual(missing.items, ())
        self.assertEqual(missing.actions[0].message, "我希望稳健增值，预计持有12个月")
        # 预设话术只能是可编辑草稿：投资偏好属开放输入，必须由用户确认后发送。
        self.assertEqual(missing.actions[0].interaction, "compose")
        # 槽位化补录表单：缺失字段驱动，投资目标为受控选项；
        # 金额随首轮表单一起给用户（选填），支持一轮收齐。
        form = missing.actions[0].form
        self.assertIsNotNone(form)
        self.assertEqual(
            [field.name for field in form.fields],
            ["investmentGoal", "horizonMonths", "plannedAmount"],
        )
        self.assertEqual(form.fields[0].type, "select")
        self.assertIn("稳健增值", form.fields[0].options)
        self.assertEqual(form.fields[1].type, "number")
        self.assertTrue(form.fields[1].required)
        self.assertEqual(form.fields[2].name, "plannedAmount")
        self.assertFalse(form.fields[2].required)

        ready = project_user_result([{
            "capability_id": "bank.wealth.advisory.context.get",
            "output": {
                "recommendationStatus": "ready",
                "missingPreferenceFields": [],
                "requiredActions": [],
                "riskAssessment": {"status": "VALID", "level": "C2"},
                "dataSources": [
                    {"kind": "core_data", "source": "risk_assessment", "status": "fresh", "recordCount": 1},
                    {"kind": "derived_profile", "source": "cashflow_profile", "status": "derived", "recordCount": 12},
                ],
                "recommendations": [{
                    "productId": "private_product_id",
                    "productName": "乾元-安鑫",
                    "category": "FIXED_INCOME_WEALTH",
                    "riskLevel": "R2",
                    "annualRateBp": 235,
                    "rateNote": "业绩比较基准2.10%-2.60%",
                    "termDays": 0,
                    "liquidity": "按日开放",
                    "suitability": "适配当前C2风险测评和12个月持有计划",
                    "suggestedAmountRange": {
                        "minimum": {"formatted": "¥10,000.00"},
                        "maximum": {"formatted": "¥20,000.00"},
                    },
                    "reasons": ["风险等级未超过当前适配上限", "可分散现有持仓"],
                }],
            },
        }])
        self.assertIsNotNone(ready)
        self.assertEqual(ready.kind, "wealth_advisory")
        self.assertEqual(len(ready.item_cards), 1)
        self.assertIn("风险测评", ready.summary)
        self.assertNotIn("private_product_id", ready.model_dump_json())

    def test_wealth_advisory_accepts_digit_dense_financial_text_and_breaks_amount_chains(self):
        # 收益率/费率文本数字密集但呈短串分布，不得触发账号形态防护（回归：prod_w_lqb2 卡片曾被误杀）
        ready = project_user_result([{
            "capability_id": "bank.wealth.advisory.context.get",
            "output": {
                "recommendationStatus": "ready",
                "missingPreferenceFields": [],
                "requiredActions": [],
                "riskAssessment": {"status": "VALID", "level": "C2"},
                "dataSources": [],
                "recommendations": [{
                    "productId": "prod_w_lqb2",
                    "productName": "龙钱宝2号",
                    "category": "CASH_MANAGEMENT",
                    "riskLevel": "R1",
                    "annualRateBp": 120,
                    "rateNote": "精选 50 只货币基金，七日年化 0.863%-1.74%，综合日内快赎额度 50 万元",
                    "termDays": 0,
                    "liquidity": "按日开放",
                    "suitability": "适配当前 C2 风险测评",
                    "suggestedAmountRange": {
                        "minimum": {"formatted": "¥100,000.00"},
                        "maximum": {"formatted": "¥200,000.00"},
                    },
                    "reasons": [
                        "组合角色：流动性备用（同类产品共享 20% 仓位上限）",
                        "产品风险等级 R1 未超过当前测评 C2 的适配上限",
                    ],
                }],
            },
        }])
        self.assertIsNotNone(ready)
        self.assertEqual(ready.kind, "wealth_advisory")
        self.assertEqual(len(ready.item_cards), 1)
        amount_meta = ready.item_cards[0].metas[1]
        # 区间上下限不同时用「起 · 至多」断开数字链，避免两个金额数字组被链接成疑似账号
        self.assertEqual(amount_meta.value, "¥100,000.00 起 · 至多 ¥200,000.00")
        self.assertIn("0.863%", ready.item_cards[0].action.detail)

    def test_wealth_advisory_amount_range_collapses_equal_bounds(self):
        ready = project_user_result([{
            "capability_id": "bank.wealth.advisory.context.get",
            "output": {
                "recommendationStatus": "ready",
                "missingPreferenceFields": [],
                "requiredActions": [],
                "riskAssessment": {"status": "VALID", "level": "C2"},
                "dataSources": [],
                "recommendations": [{
                    "productId": "prod_w_longbao",
                    "productName": "乾元-建行龙宝",
                    "category": "FIXED_INCOME_WEALTH",
                    "riskLevel": "R2",
                    "annualRateBp": 140,
                    "rateNote": "七日年化收益率随市场浮动",
                    "termDays": 0,
                    "liquidity": "按日开放",
                    "suitability": "适配当前 C2 风险测评",
                    "suggestedAmountRange": {
                        "minimum": {"formatted": "¥10,000.00"},
                        "maximum": {"formatted": "¥10,000.00"},
                    },
                    "reasons": ["组合角色：主推核心（同类产品共享 50% 仓位上限）"],
                }],
            },
        }])
        self.assertIsNotNone(ready)
        amount_meta = ready.item_cards[0].metas[1]
        self.assertEqual(amount_meta.value, "¥10,000.00")

    def test_wealth_advisory_projects_amount_conserving_plans_not_flat_candidates(self):
        ready = project_user_result([{
            "capability_id": "bank.wealth.advisory.context.get",
            "output": {
                "recommendationStatus": "ready",
                "missingPreferenceFields": [],
                "requiredActions": [],
                "riskAssessment": {"status": "VALID", "level": "C2"},
                "preferences": {
                    "investmentGoal": "steady_growth",
                    "horizonMonths": 3,
                    "plannedAmount": {"amountMinor": 1000000, "currency": "CNY", "formatted": "¥10,000.00"},
                    "productScope": "wealth_and_conservative_alternatives",
                },
                "dataSources": [
                    {"kind": "core_data", "source": "risk_assessment", "status": "fresh", "recordCount": 1},
                ],
                "recommendations": [],
                "primaryPlan": {
                    "planId": "plan_single_anxin",
                    "title": "稳健优选方案",
                    "summary": "整笔配置，方案简洁",
                    "kind": "single_product",
                    "totalAmount": {"amountMinor": 1000000, "currency": "CNY", "formatted": "¥10,000.00"},
                    "allocations": [{
                        "productId": "prod_w_anxin",
                        "productName": "乾元-安鑫",
                        "productCategory": "FIXED_INCOME_WEALTH",
                        "productCategoryLabel": "银行理财",
                        "portfolioRole": "core",
                        "portfolioRoleLabel": "核心配置",
                        "amount": {"amountMinor": 1000000, "currency": "CNY", "formatted": "¥10,000.00"},
                        "ratioBasisPoints": 10000,
                        "riskLevel": "R2",
                        "termDays": 0,
                        "liquidity": "按日开放",
                        "suitability": "适配当前风险等级和3个月持有计划",
                        "annualRateBp": 235,
                        "rateNote": "业绩比较基准随市场变化",
                        "reasons": ["风险等级适配", "金额满足起购要求"],
                    }],
                    "rationale": ["方案金额与计划投入一致"],
                },
                "alternativePlans": [],
                "planMetrics": {"candidateCount": 4, "feasiblePlanCount": 3, "amountConserved": True},
            },
        }])

        self.assertIsNotNone(ready)
        self.assertEqual(ready.kind, "wealth_advisory")
        self.assertEqual(len(ready.plans), 1)
        self.assertTrue(ready.plans[0].is_primary)
        self.assertEqual(ready.plans[0].total_amount_minor, 1000000)
        self.assertEqual(
            sum(item.amount_minor for item in ready.plans[0].allocations),
            1000000,
        )
        self.assertNotIn("了解详情", ready.model_dump_json(by_alias=True))
        self.assertIn("查看产品", ready.model_dump_json(by_alias=True))
        self.assertIn("乾元-安鑫", ready.plans[0].select_message)
        self.assertIn("10000元", ready.plans[0].select_message)
        self.assertNotIn("prod_w_anxin", ready.plans[0].select_message)
        self.assertTrue(ready.plans[0].select_message.startswith("申购理财产品："))

    def test_all_read_execute_capabilities_have_deterministic_results(self):
        cases = (
            (
                "bank.account.balance.get",
                {
                    "available": {"formatted": "¥88.00"},
                    "ledger": {"formatted": "¥100.00"},
                },
                "account_balance",
            ),
            (
                "bank.account.transactions.list",
                {"data": [{"description": "早餐", "amount": {"formatted": "-¥12.00"}}]},
                "transaction_list",
            ),
            (
                "bank.credit-card.list",
                {"cards": [{"productName": "龙卡", "maskedNo": "****8899", "status": "ACTIVE"}]},
                "credit_card_list",
            ),
            (
                "bank.credit-card.transactions.list",
                {"data": [{"description": "购物", "amount": {"formatted": "-¥20.00"}}]},
                "transaction_list",
            ),
            (
                "bank.payee.list",
                {"payees": [{"name": "张三", "maskedNo": "****1024", "status": "ACTIVE"}]},
                "payee_list",
            ),
            (
                "bank.wealth.holding.list",
                {
                    "holdingCount": 1,
                    "totalValue": {"formatted": "¥20,026.50"},
                    "totalEarnings": {"formatted": "¥26.50"},
                    "holdings": [{
                        "productName": "建信理财天天利（龙钱宝）现金管理类产品10号",
                        "currentValue": {"formatted": "¥20,026.50"},
                    }],
                },
                "wealth_holding_list",
            ),
            (
                "bank.cashflow.salary-plan.get",
                {
                    "salary": {"amountMinor": 1860000, "currency": "CNY"},
                    "availableBalance": {"amountMinor": 1860000, "currency": "CNY"},
                    "totalObligations": {"amountMinor": 686000, "currency": "CNY"},
                    "mandatoryObligations": {"amountMinor": 686000, "currency": "CNY"},
                    "plannedRepayment": {"amountMinor": 686000, "currency": "CNY"},
                    "uncoveredObligations": {"amountMinor": 0, "currency": "CNY"},
                    "livingBudget": {"amountMinor": 500000, "currency": "CNY"},
                    "emergencyReserve": {"amountMinor": 300000, "currency": "CNY"},
                    "allocatable": {"amountMinor": 374000, "currency": "CNY"},
                    "obligationItems": [{
                        "id": "card_1", "type": "credit_card",
                        "displayName": "龙卡账单", "amountMinor": 686000,
                        "dueAt": "2026-09-18", "sourceRefs": ["card_1"],
                    }],
                    "items": [
                        {"id": "planned_repayment", "amountMinor": 686000, "sourceRefs": ["bill_1"]},
                        {"id": "living_budget", "amountMinor": 500000, "sourceRefs": ["policy:living"]},
                    ],
                    "generatedAt": "2026-09-03T08:00:00.000Z",
                },
                "salary_monthly_plan",
            ),
            (
                "bank.cashflow.spending-analysis.get",
                {
                    "period": "2026-08",
                    "comparisonPeriod": "2026-07",
                    "current": {"netExpenseMinor": 190000},
                    "previous": {"netExpenseMinor": 90000},
                    "excluded": {"internalTransferMinor": 200000, "refundMinor": 30000},
                    "findings": [
                        {
                            "kind": "fact", "code": "NET_EXPENSE_TOTAL",
                            "text": "trusted provider wording is not shown directly",
                            "sourceRefs": ["txn_food"],
                        }
                    ],
                    "generatedAt": "2026-09-03T08:00:00.000Z",
                },
                "spending_explanation",
            ),
        )
        for capability_id, output, expected_kind in cases:
            with self.subTest(capability_id=capability_id):
                result = project_user_result([{
                    "capability_id": capability_id,
                    "input": {},
                    "output": output,
                }])
                self.assertIsNotNone(result)
                self.assertEqual(result.kind, expected_kind)

    def test_salary_plan_explains_total_due_available_funds_and_bill_breakdown(self):
        result = project_user_result([{
            "capability_id": "bank.cashflow.salary-plan.get",
            "input": {},
            "output": {
                "salary": {"amountMinor": 1860000, "currency": "CNY"},
                "availableBalance": {"amountMinor": 2864036, "currency": "CNY"},
                "totalObligations": {"amountMinor": 2311186, "currency": "CNY"},
                "plannedRepayment": {"amountMinor": 2064036, "currency": "CNY"},
                "uncoveredObligations": {"amountMinor": 247150, "currency": "CNY"},
                "mandatoryObligations": {"amountMinor": 2311186, "currency": "CNY"},
                "livingBudget": {"amountMinor": 500000, "currency": "CNY"},
                "emergencyReserve": {"amountMinor": 300000, "currency": "CNY"},
                "allocatable": {"amountMinor": 0, "currency": "CNY"},
                "obligationItems": [
                    {
                        "id": "card_1", "type": "credit_card",
                        "displayName": "龙卡全球支付卡 · 尾号 8891",
                        "amountMinor": 1422000, "dueAt": "2026-09-18",
                        "sourceRefs": ["card_1"],
                    },
                    {
                        "id": "loan_1", "type": "loan",
                        "displayName": "信用快贷 · 第 7 期",
                        "amountMinor": 254409, "dueAt": None,
                        "sourceRefs": ["loan_1"],
                    },
                    {
                        "id": "loan_2", "type": "loan",
                        "displayName": "个人住房贷款 · 第 39 期",
                        "amountMinor": 584777, "dueAt": None,
                        "sourceRefs": ["loan_2"],
                    },
                    {
                        "id": "utility_1", "type": "utility",
                        "displayName": "电费 · 2026-09",
                        "amountMinor": 50000, "dueAt": "2026-09-09",
                        "sourceRefs": ["utility_1"],
                    },
                ],
                "items": [
                    {"id": "mandatory_obligations", "amountMinor": 2064036, "sourceRefs": ["card_1"]},
                    {"id": "living_budget", "amountMinor": 500000, "sourceRefs": ["policy:living"]},
                    {"id": "emergency_reserve", "amountMinor": 300000, "sourceRefs": ["policy:reserve"]},
                ],
                "generatedAt": "2026-09-03T08:00:00.000Z",
            },
        }])

        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "salary_monthly_plan")
        self.assertIn("4", result.summary)
        self.assertTrue(any("龙卡全球支付卡" in item for item in result.items))
        self.assertTrue(any("资金缺口" in item for item in result.items))

    def test_empty_unknown_and_unsafe_outputs_do_not_become_success_results(self):
        cases = (
            ("bank.account.transactions.list", {"data": []}),
            ("bank.credit-card.list", {"cards": []}),
            ("bank.payee.list", {"payees": []}),
            ("bank.unknown.read", {"data": [{"description": "猜测"}]}),
            (
                "bank.account.balance.get",
                {"available": {"formatted": "accountId=acct_private"}, "ledger": {"formatted": "¥1"}},
            ),
            (
                "bank.cashflow.salary-plan.get",
                {
                    "salary": {"amountMinor": -1, "currency": "CNY"},
                    "mandatoryObligations": {"amountMinor": 0, "currency": "CNY"},
                    "livingBudget": {"amountMinor": 0, "currency": "CNY"},
                    "emergencyReserve": {"amountMinor": 0, "currency": "CNY"},
                    "allocatable": {"amountMinor": 0, "currency": "CNY"},
                    "items": [],
                },
            ),
            (
                "bank.cashflow.spending-analysis.get",
                {
                    "period": "not-a-month",
                    "comparisonPeriod": "2026-07",
                    "current": {"netExpenseMinor": 1},
                    "previous": {"netExpenseMinor": 1},
                    "excluded": {"internalTransferMinor": 0, "refundMinor": 0},
                    "findings": [],
                },
            ),
        )
        for capability_id, output in cases:
            with self.subTest(capability_id=capability_id):
                self.assertIsNone(project_user_result([{
                    "capability_id": capability_id,
                    "input": {},
                    "output": output,
                }]))

    def test_empty_period_zero_aggregate_accepts_unattributable_findings(self):
        # 本期净支出为 0（该月没有任何支出交易）：空 sourceRefs 是合法的零值聚合，
        # 应当产出有效的空月分析结果而不是 None（None 会把运行打成 blocked）。
        result = project_user_result([{
            "capability_id": "bank.cashflow.spending-analysis.get",
            "input": {},
            "output": {
                "period": "2026-09",
                "comparisonPeriod": "2026-08",
                "current": {"netExpenseMinor": 0},
                "previous": {"netExpenseMinor": 2528680},
                "excluded": {"internalTransferMinor": 0, "refundMinor": 0},
                "findings": [
                    {
                        "kind": "fact", "code": "NET_EXPENSE_TOTAL",
                        "text": "Net expense for 2026-09 is 0 minor units",
                        "sourceRefs": [],
                    },
                    {
                        "kind": "rule_inference", "code": "SPENDING_DECREASED",
                        "text": "Equal-period net expense changed by -2528680 minor units",
                        "sourceRefs": ["txn_prev_1"],
                    },
                ],
                "generatedAt": "2026-09-13T08:00:00.000Z",
            },
        }])
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "spending_explanation")
        self.assertTrue(any("¥0.00" in item for item in result.items))

    def test_nonzero_period_still_requires_source_refs(self):
        # 本期有支出（净支出 > 0）却给不出任何溯源引用：仍然拒绝投影。
        self.assertIsNone(project_user_result([{
            "capability_id": "bank.cashflow.spending-analysis.get",
            "input": {},
            "output": {
                "period": "2026-08",
                "comparisonPeriod": "2026-07",
                "current": {"netExpenseMinor": 190000},
                "previous": {"netExpenseMinor": 90000},
                "excluded": {"internalTransferMinor": 0, "refundMinor": 0},
                "findings": [{
                    "kind": "fact", "code": "NET_EXPENSE_TOTAL",
                    "text": "no traceable refs",
                    "sourceRefs": [],
                }],
                "generatedAt": "2026-09-13T08:00:00.000Z",
            },
        }]))

    def test_spending_categories_render_as_attribution_cards(self):
        # 类别归因：categories 契约字段逐卡展示明细；非法或负净额条目静默跳过。
        result = project_user_result([{
            "capability_id": "bank.cashflow.spending-analysis.get",
            "input": {},
            "output": {
                "period": "2026-08",
                "comparisonPeriod": "2026-07",
                "current": {"netExpenseMinor": 190000},
                "previous": {"netExpenseMinor": 90000},
                "excluded": {"internalTransferMinor": 0, "refundMinor": 0},
                "categories": [
                    {"category": "FOOD_DINING", "currentMinor": 120000, "previousMinor": 90000, "deltaMinor": 30000},
                    {"category": "TRANSPORT", "currentMinor": 5408, "previousMinor": 10816, "deltaMinor": -5408},
                    {"category": "BAD CATEGORY!", "currentMinor": 1, "previousMinor": 1, "deltaMinor": 0},
                    {"category": "NEGATIVE", "currentMinor": -5, "previousMinor": 0, "deltaMinor": -5},
                ],
                "findings": [{
                    "kind": "fact", "code": "NET_EXPENSE_TOTAL",
                    "text": "Net expense for 2026-08 is 190000 minor units",
                    "sourceRefs": ["txn_1"],
                }],
                "generatedAt": "2026-09-13T08:00:00.000Z",
            },
        }])
        self.assertIsNotNone(result)
        cards = {card.title: card for card in result.item_cards}
        self.assertIn("餐饮", cards)
        self.assertIn("交通出行", cards)
        self.assertNotIn("BAD CATEGORY!", cards)
        self.assertNotIn("NEGATIVE", cards)
        self.assertEqual(cards["餐饮"].subtitle, "较上月增加 ¥300.00")
        self.assertEqual(cards["交通出行"].subtitle, "较上月减少 ¥54.08")
        self.assertTrue(any("餐饮" in item and "较上月增加" in item for item in result.items))


if __name__ == "__main__":
    unittest.main()
