"""Deterministic mobile-facing projections from trusted Node capability outputs."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from app.contracts import (
    UserFacingItemAction,
    UserFacingItemCard,
    UserFacingItemMeta,
    UserFacingPlan,
    UserFacingPlanAllocation,
    UserFacingResult,
    UserResponseActionForm,
)

# 交易类别展示名（与 bank_account_transactions.category 枚举对齐；未知类别回退原码）。
SPENDING_CATEGORY_LABELS = {
    "FOOD_DINING": "餐饮",
    "GROCERIES": "生鲜日用",
    "TRANSPORT": "交通出行",
    "UTILITIES": "水电燃气",
    "TELECOM": "通讯",
    "MEDICAL": "医疗健康",
    "ENTERTAINMENT": "文化娱乐",
    "CREDIT_CARD": "信用卡还款",
    "CONSUMER_LOAN": "消费贷款还款",
    "HOUSING_LOAN": "住房贷款还款",
    "FUND_INVEST": "理财申购",
    "INCOME_SALARY": "工资入账",
    "INCOME_INTEREST": "利息入账",
    "TRANSFER_INTERNAL": "内部转账",
    "OTHER": "其他支出",
}


def project_user_result(
    invocations: list[dict[str, Any]] | tuple[dict[str, Any], ...]
) -> UserFacingResult | None:
    try:
        return _project_user_result(invocations)
    except (ValidationError, ValueError, TypeError, AttributeError):
        return None


def _project_user_result(
    invocations: list[dict[str, Any]] | tuple[dict[str, Any], ...]
) -> UserFacingResult | None:
    if not invocations:
        return None
    latest = invocations[-1]
    capability_id = latest.get("capability_id")
    output = latest.get("output")
    if not isinstance(output, dict):
        return None
    if capability_id == "bank.credit-card.list":
        cards_data = output.get("cards")
        if not isinstance(cards_data, list) or not cards_data:
            return None
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        for card in cards_data[:10]:
            if not isinstance(card, dict):
                continue
            name = _text(card.get("productName"), "信用卡")
            masked = _text(card.get("maskedNo"), "尾号未知")
            status = _card_status(card.get("status"))
            limit = card.get("availableLimit")
            available = _text(limit.get("formatted"), "") if isinstance(limit, dict) else ""
            items.append(" · ".join(item for item in (name, masked, status, available) if item))

            metas = [UserFacingItemMeta(label="卡号", value=masked)]
            if available:
                metas.append(UserFacingItemMeta(label="可用额度", value=available))
            cards.append(UserFacingItemCard(
                title=name,
                subtitle=status,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="查看信用卡详情",
                    message=f"查看{masked}信用卡的详细信息",
                ),
            ))
        if not cards:
            return None
        return UserFacingResult(
            kind="credit_card_list",
            title="我的信用卡",
            summary=f"已查询到 {len(cards)} 张本人信用卡",
            items=items,
            item_cards=tuple(cards),
        )
    if capability_id == "bank.account.balance.get":
        available = output.get("available")
        ledger = output.get("ledger")
        if (
            not isinstance(available, dict)
            or not isinstance(ledger, dict)
            or not _valid_display_money(available.get("formatted"))
            or not _valid_display_money(ledger.get("formatted"))
        ):
            return None
        return UserFacingResult(
            kind="account_balance",
            title="账户余额",
            summary="余额数据已从核心账户服务查询",
            items=[
                "可用余额 " + _text(available.get("formatted"), "--"),
                "账面余额 " + _text(ledger.get("formatted"), "--"),
            ],
            item_cards=(UserFacingItemCard(
                title="账户余额",
                metas=(
                    UserFacingItemMeta(label="可用余额", value=_text(available.get("formatted"), "--")),
                    UserFacingItemMeta(label="账面余额", value=_text(ledger.get("formatted"), "--")),
                ),
            ),),
            actions=(
                UserFacingItemAction(label="查看账户明细", message="查看账户明细"),
            ),
        )
    if capability_id == "bank.account.list":
        accounts = output.get("accounts")
        if not isinstance(accounts, list):
            return None
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        for item in accounts[:10]:
            if not isinstance(item, dict):
                continue
            name = _text(item.get("name"), "银行账户")
            masked = _text(item.get("maskedNo"), "尾号未知")
            items.append(" · ".join((name, masked)))
            cards.append(UserFacingItemCard(
                title=name,
                subtitle=masked,
                action=UserFacingItemAction(
                    label="查看余额",
                    message=f"查询{masked}账户的余额",
                ),
            ))
        if not cards:
            return None
        return UserFacingResult(
            kind="account_list", title="我的账户",
            summary=f"已查询到 {len(cards)} 个可用账户",
            items=items, item_cards=tuple(cards),
        )
    if capability_id == "bank.credit-card.detail.get":
        product_name = _text(output.get("productName"), "信用卡详情")
        masked = _text(output.get("maskedNo"), "尾号未知")
        status_text = _card_status(output.get("status"))
        metas: list[UserFacingItemMeta] = [
            UserFacingItemMeta(label="卡号", value=masked),
            UserFacingItemMeta(label="状态", value=status_text),
        ]
        total_limit = output.get("totalLimit")
        if isinstance(total_limit, dict) and _valid_display_money(total_limit.get("formatted")):
            metas.append(UserFacingItemMeta(label="总额度", value=total_limit["formatted"]))
        available_limit = output.get("availableLimit")
        if isinstance(available_limit, dict) and _valid_display_money(available_limit.get("formatted")):
            metas.append(UserFacingItemMeta(label="可用额度", value=available_limit["formatted"]))
        return UserFacingResult(
            kind="credit_card_detail", title=product_name,
            summary="卡片状态与额度已更新",
            items=[masked, status_text],
            item_cards=(UserFacingItemCard(
                title=product_name,
                subtitle=masked,
                metas=tuple(metas),
            ),),
            actions=(
                UserFacingItemAction(label="返回卡片管理", message="查看我的信用卡"),
            ),
        )
    if capability_id in {"bank.account.transactions.list", "bank.credit-card.transactions.list"}:
        rows = output.get("data")
        if not isinstance(rows, list) or not rows:
            return None
        is_card_txn = capability_id == "bank.credit-card.transactions.list"
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        for row in rows[:10]:
            if not isinstance(row, dict):
                continue
            desc = _text(row.get("description"), "交易")
            amount = row.get("amount")
            formatted = _text(amount.get("formatted"), "") if isinstance(amount, dict) else ""
            direction = row.get("direction")
            is_credit = direction == "CREDIT"
            direction_text = "收入" if is_credit else ("支出" if direction == "DEBIT" else "交易")
            amount_text = (("+" if is_credit else "-") + formatted) if formatted else ""
            items.append(" · ".join(item for item in (desc, amount_text or formatted, direction_text) if item))
            metas: list[UserFacingItemMeta] = [
                UserFacingItemMeta(label="方向", value=direction_text),
            ]
            if formatted:
                metas.append(UserFacingItemMeta(label="金额", value=amount_text or formatted))
            time_field = "occurredAt" if is_card_txn else "postedAt"
            time_val = row.get(time_field)
            if isinstance(time_val, str) and time_val:
                metas.append(UserFacingItemMeta(label="时间", value=time_val[:10]))
            if is_card_txn:
                status_text = {"SETTLED": "已入账", "PENDING": "处理中", "FAILED": "失败"}.get(row.get("status"))
                if status_text:
                    metas.append(UserFacingItemMeta(label="状态", value=status_text))
            business_type = row.get("businessType")
            if isinstance(business_type, str) and business_type:
                metas.append(UserFacingItemMeta(label="类型", value=business_type))
            cards.append(UserFacingItemCard(
                title=desc,
                subtitle=amount_text or None,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="查看交易详情",
                    message="查看交易详情",
                ),
            ))
        if not cards:
            return None
        meta_info = output.get("meta")
        has_next = isinstance(meta_info, dict) and meta_info.get("hasNext")
        summary_parts = [f"已查询到 {len(cards)} 条交易"]
        if has_next:
            summary_parts.append("还有更多记录")
        return UserFacingResult(
            kind="transaction_list", title="交易明细",
            summary="；".join(summary_parts),
            items=items, item_cards=tuple(cards),
            actions=(
                UserFacingItemAction(label="查看更多明细", message="查看更多交易记录"),
            ),
        )
    if capability_id == "bank.payee.list":
        payees = output.get("payees")
        if not isinstance(payees, list) or not payees:
            return None
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        for payee in payees[:10]:
            if not isinstance(payee, dict):
                continue
            name = _text(payee.get("name"), "常用收款人")
            masked = _text(payee.get("maskedNo"), "账号已保护")
            status = "可用" if payee.get("status") == "ACTIVE" else "状态待确认"
            items.append(" · ".join((name, masked, status)))
            cards.append(UserFacingItemCard(
                title=name,
                subtitle=masked,
                metas=(UserFacingItemMeta(label="状态", value=status),) if status != "可用" else (),
                action=UserFacingItemAction(
                    label="发起转账",
                    message=f"给{name}转账",
                ),
            ))
        if not cards:
            return None
        return UserFacingResult(
            kind="payee_list",
            title="常用收款人",
            summary=f"已查询到 {len(cards)} 位常用收款人",
            items=items,
            item_cards=tuple(cards),
        )
    if capability_id == "bank.wealth.holding.list":
        holdings = output.get("holdings")
        total_value = output.get("totalValue")
        total_earnings = output.get("totalEarnings")
        if (
            not isinstance(holdings, list)
            or not isinstance(total_value, dict)
            or not isinstance(total_earnings, dict)
            or not _valid_display_money(total_value.get("formatted"))
            or not _valid_display_money(total_earnings.get("formatted"))
        ):
            return None
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        for holding in holdings[:10]:
            if not isinstance(holding, dict):
                continue
            product_name = _text(holding.get("productName"), "理财持仓")
            value = holding.get("currentValue")
            formatted = _text(value.get("formatted"), "") if isinstance(value, dict) else ""
            items.append(" · ".join(item for item in (product_name, formatted) if item))
            metas: list[UserFacingItemMeta] = []
            principal = holding.get("principal")
            if isinstance(principal, dict) and isinstance(principal.get("formatted"), str):
                metas.append(UserFacingItemMeta(label="本金", value=principal["formatted"]))
            earnings = holding.get("accumulatedEarnings")
            if isinstance(earnings, dict) and isinstance(earnings.get("formatted"), str):
                metas.append(UserFacingItemMeta(label="累计收益", value=earnings["formatted"]))
            if formatted:
                metas.append(UserFacingItemMeta(label="当前市值", value=formatted))
            cards.append(UserFacingItemCard(
                title=product_name,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="查看持仓详情",
                    message=f"查看{product_name}的持仓详情",
                ),
            ))
        return UserFacingResult(
            kind="wealth_holding_list",
            title="龙钱宝持仓",
            summary="持仓数据已从核心财富服务查询",
            items=[
                "持仓总额 " + _text(total_value.get("formatted"), "--"),
                "累计收益 " + _text(total_earnings.get("formatted"), "--"),
                *items,
            ],
            item_cards=tuple(cards) if cards else (),
            actions=(
                UserFacingItemAction(label="继续申购龙钱宝", message="我要申购龙钱宝"),
            ) if cards else (),
        )
    if capability_id == "bank.wealth.product.list":
        products = output.get("products")
        if not isinstance(products, list) or not products:
            return None
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        for product in products[:10]:
            if not isinstance(product, dict):
                continue
            name = _text(product.get("name"), "理财产品")
            risk = _risk_level(product.get("riskLevel"))
            rate_bp = product.get("annualRateBp")
            rate_pct = f"{rate_bp / 100:.2f}%" if isinstance(rate_bp, int) else "—"
            rate_line = f"业绩基准 {rate_pct}" if isinstance(rate_bp, int) else ""
            items.append(" · ".join(item for item in (name, risk, rate_line) if item))

            metas = [
                UserFacingItemMeta(label="风险等级", value=risk),
                UserFacingItemMeta(label="业绩基准", value=rate_pct),
            ]
            min_purchase = product.get("minPurchase")
            if isinstance(min_purchase, dict):
                mp_amount = min_purchase.get("amount")
                if isinstance(mp_amount, str) and mp_amount:
                    metas.append(UserFacingItemMeta(label="起购金额", value=mp_amount))
            term_type = product.get("termType")
            term_days = product.get("termDays")
            if term_type == "fixed" and isinstance(term_days, int) and term_days > 0:
                metas.append(UserFacingItemMeta(label="期限", value=_term_text(term_days)))
            elif term_type == "demand":
                metas.append(UserFacingItemMeta(label="期限", value="随时可取"))

            detail_parts: list[str] = []
            issuer = product.get("issuer")
            if isinstance(issuer, str) and issuer:
                detail_parts.append(f"发行机构：{issuer}")
            product_code = product.get("productCode")
            if isinstance(product_code, str) and product_code:
                detail_parts.append(f"产品编码：{product_code}")
            rate_note = product.get("rateNote")
            if isinstance(rate_note, str) and rate_note:
                detail_parts.append(rate_note)
            purchase_channel = product.get("purchaseChannel")
            if isinstance(purchase_channel, str) and purchase_channel:
                detail_parts.append(f"购买渠道：{purchase_channel}")
            features = product.get("features")
            if isinstance(features, dict):
                for feature_key, feature_value in features.items():
                    if isinstance(feature_value, str) and feature_value:
                        detail_parts.append(f"{feature_key}：{feature_value}")

            product_id = product.get("id")
            cards.append(UserFacingItemCard(
                id=str(product_id) if product_id else None,
                title=name,
                subtitle=risk,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="展开产品信息",
                    detail="\n".join(detail_parts) if detail_parts else None,
                ),
            ))
        if not cards:
            return None
        return UserFacingResult(
            kind="wealth_product_list",
            title="理财产品",
            summary=f"已查询到 {len(cards)} 款在售产品",
            items=items,
            item_cards=tuple(cards),
        )
    if capability_id == "bank.cashflow.salary-plan.get":
        fields = (
            ("salary", "本月工资"),
            ("availableBalance", "付款账户可用"),
            ("totalObligations", "待还合计"),
            ("plannedRepayment", "本次可安排还款"),
            ("uncoveredObligations", "仍有资金缺口"),
            ("livingBudget", "生活预算"),
            ("emergencyReserve", "应急储备"),
            ("allocatable", "可配置资金"),
        )
        amounts: dict[str, int] = {}
        currency: str | None = None
        for field, _ in fields:
            value = output.get(field)
            if not isinstance(value, dict):
                return None
            amount_minor = value.get("amountMinor")
            item_currency = value.get("currency")
            if (
                not isinstance(amount_minor, int)
                or isinstance(amount_minor, bool)
                or amount_minor < 0
                or not isinstance(item_currency, str)
                or item_currency not in {"CNY"}
            ):
                return None
            amounts[field] = amount_minor
            currency = currency or item_currency
            if item_currency != currency:
                return None
        mandatory = output.get("mandatoryObligations")
        if (
            amounts["salary"] <= 0
            or not isinstance(mandatory, dict)
            or mandatory.get("amountMinor") != amounts["totalObligations"]
            or mandatory.get("currency") != currency
            or amounts["plannedRepayment"] + amounts["uncoveredObligations"]
            != amounts["totalObligations"]
            or amounts["plannedRepayment"] + amounts["livingBudget"]
            + amounts["emergencyReserve"] + amounts["allocatable"]
            != amounts["availableBalance"]
        ):
            return None
        evidence = output.get("items")
        if not isinstance(evidence, list) or not evidence or any(
            not isinstance(item, dict)
            or not isinstance(item.get("sourceRefs"), list)
            or not item["sourceRefs"]
            for item in evidence
        ):
            return None
        obligation_items = output.get("obligationItems")
        if not isinstance(obligation_items, list) or not obligation_items or len(obligation_items) > 32:
            return None
        bill_lines: list[str] = []
        plan_cards: list[UserFacingItemCard] = []
        obligation_total = 0
        for item in obligation_items:
            if not isinstance(item, dict):
                return None
            amount_minor = item.get("amountMinor")
            display_name = item.get("displayName")
            source_refs = item.get("sourceRefs")
            if (
                item.get("type") not in {"credit_card", "loan", "utility"}
                or not isinstance(display_name, str)
                or not display_name
                or len(display_name) > 120
                or not isinstance(amount_minor, int)
                or isinstance(amount_minor, bool)
                or amount_minor <= 0
                or not isinstance(source_refs, list)
                or not source_refs
            ):
                return None
            obligation_total += amount_minor
            bill_lines.append(
                f"{display_name} {_format_minor(amount_minor, currency)}"
            )
            obl_type = item.get("type")
            type_text = {"credit_card": "信用卡", "loan": "贷款", "utility": "生活缴费"}.get(obl_type, "待还")
            amount_text = _format_minor(amount_minor, currency)
            plan_cards.append(UserFacingItemCard(
                title=display_name,
                subtitle=f"{type_text} · {amount_text}",
                metas=(
                    UserFacingItemMeta(label="类型", value=type_text),
                    UserFacingItemMeta(label="金额", value=amount_text),
                ),
                action=(
                    UserFacingItemAction(
                        label="立即还款",
                        message=f"还{display_name}",
                    )
                    if obl_type == "credit_card"
                    else None
                ),
            ))
        if obligation_total != amounts["totalObligations"]:
            return None
        return UserFacingResult(
            kind="salary_monthly_plan",
            title="本月待还与资金安排",
            summary=(
                f"待还共 {len(obligation_items)} 笔；已先保留生活预算和应急资金，"
                "再安排本次还款。"
            ),
            items=[
                f"{label} {_format_minor(amounts[field], currency)}"
                for field, label in fields
            ] + bill_lines,
            item_cards=tuple(plan_cards),
            actions=(
                UserFacingItemAction(label="调整资金安排", message="调整本月资金安排"),
            ),
        )
    if capability_id == "bank.cashflow.spending-analysis.get":
        period = output.get("period")
        comparison_period = output.get("comparisonPeriod")
        current = output.get("current")
        previous = output.get("previous")
        excluded = output.get("excluded")
        findings = output.get("findings")
        if (
            not _valid_period(period)
            or not _valid_period(comparison_period)
            or not isinstance(current, dict)
            or not isinstance(previous, dict)
            or not isinstance(excluded, dict)
            or not isinstance(findings, list)
            or not findings
        ):
            return None
        values = {
            "current": current.get("netExpenseMinor"),
            "previous": previous.get("netExpenseMinor"),
            "internal": excluded.get("internalTransferMinor"),
            "refund": excluded.get("refundMinor"),
        }
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in values.values()
        ):
            return None
        # 本期净支出为 0 说明该期间没有任何支出交易可引用：空 sourceRefs 是合法的
        # 「零值聚合」而非缺失溯源，不应把有效的空月分析打成 blocked。
        empty_period = values["current"] == 0
        if any(
            not isinstance(item, dict)
            or item.get("kind") not in {"fact", "rule_inference", "model_hypothesis"}
            or not isinstance(item.get("sourceRefs"), list)
            or (not item["sourceRefs"] and not empty_period)
            for item in findings
        ):
            return None
        delta = values["current"] - values["previous"]
        comparison = (
            f"较上月增加 {_format_minor(delta, 'CNY')}"
            if delta > 0 else
            f"较上月减少 {_format_minor(-delta, 'CNY')}"
            if delta < 0 else "与上月持平"
        )
        finding_kind_labels = {"fact": "事实", "rule_inference": "规则推断", "model_hypothesis": "模型假设"}
        finding_cards: list[UserFacingItemCard] = [UserFacingItemCard(
            title=f"{period} 支出概览",
            subtitle=comparison,
            metas=(
                UserFacingItemMeta(label="本期净支出", value=_format_minor(values['current'], 'CNY')),
                UserFacingItemMeta(label="上期净支出", value=_format_minor(values['previous'], 'CNY')),
                UserFacingItemMeta(label="已排除内部转账", value=_format_minor(values['internal'], 'CNY')),
                UserFacingItemMeta(label="已冲抵退款", value=_format_minor(values['refund'], 'CNY')),
            ),
        )]
        finding_items: list[str] = [
            f"{period} 净支出 {_format_minor(values['current'], 'CNY')}",
            f"{comparison_period} 净支出 {_format_minor(values['previous'], 'CNY')}",
            f"已排除内部转账 {_format_minor(values['internal'], 'CNY')}",
            f"已冲抵退款 {_format_minor(values['refund'], 'CNY')}",
        ]
        for finding in findings[:20]:
            f_kind = finding_kind_labels.get(finding.get("kind"), "分析")
            f_text = _text(finding.get("text"), "")
            if not f_text:
                continue
            finding_items.append(f"[{f_kind}] {f_text}")
            finding_cards.append(UserFacingItemCard(
                title=f_text,
                subtitle=f_kind,
                metas=(UserFacingItemMeta(label="类型", value=f_kind),),
            ))
        # 类别归因（与 cashflow-service categories 契约对齐）：变化最大的类别逐卡展示，
        # 让「完整归因」按钮承诺的明细依据真实可见。
        raw_categories = output.get("categories")
        if isinstance(raw_categories, list):
            for entry in raw_categories[:6]:
                if not isinstance(entry, dict):
                    continue
                cat_name = entry.get("category")
                cat_current = entry.get("currentMinor")
                cat_previous = entry.get("previousMinor")
                cat_delta = entry.get("deltaMinor")
                if (
                    not isinstance(cat_name, str)
                    or not re.fullmatch(r"[A-Za-z0-9_]{1,40}", cat_name)
                    or any(
                        not isinstance(value, int) or isinstance(value, bool) or value < 0
                        for value in (cat_current, cat_previous)
                    )
                    or not isinstance(cat_delta, int)
                    or isinstance(cat_delta, bool)
                ):
                    continue
                cat_label = SPENDING_CATEGORY_LABELS.get(cat_name, cat_name)
                if cat_delta < 0:
                    cat_change = f"较上月减少 {_format_minor(-cat_delta, 'CNY')}"
                elif cat_delta > 0:
                    cat_change = f"较上月增加 {_format_minor(cat_delta, 'CNY')}"
                else:
                    cat_change = "与上月持平"
                finding_items.append(
                    f"{cat_label} 本期 {_format_minor(cat_current, 'CNY')}（{cat_change}）"
                )
                finding_cards.append(UserFacingItemCard(
                    title=cat_label,
                    subtitle=cat_change,
                    metas=(
                        UserFacingItemMeta(label="本期支出", value=_format_minor(cat_current, 'CNY')),
                        UserFacingItemMeta(label="上期支出", value=_format_minor(cat_previous, 'CNY')),
                        UserFacingItemMeta(
                            label="环比变化",
                            value=(
                                f"-{_format_minor(-cat_delta, 'CNY')}"
                                if cat_delta < 0 else
                                f"+{_format_minor(cat_delta, 'CNY')}"
                                if cat_delta > 0 else
                                "¥0.00"
                            ),
                        ),
                    ),
                ))
        return UserFacingResult(
            kind="spending_explanation",
            title="本月支出分析",
            summary=f"本月净支出{comparison}，内部转账不计入支出，退款已冲抵",
            items=finding_items,
            item_cards=tuple(finding_cards),
            actions=(
                UserFacingItemAction(label="查看支出明细", message="查看支出明细"),
            ),
        )
    if capability_id == "bank.credit-card.bill.get":
        product_name = _text(output.get("productName"), "信用卡")
        masked = _text(output.get("maskedNo"), "尾号未知")
        outstanding = output.get("outstanding")
        minimum = output.get("minimumPayment")
        total_limit = output.get("totalLimit")
        available = output.get("availableLimit")
        if not isinstance(outstanding, dict) or not _valid_display_money(outstanding.get("formatted")):
            return None
        bill_status = output.get("billStatus")
        is_due = bill_status == "PAYMENT_DUE"
        metas: list[UserFacingItemMeta] = [
            UserFacingItemMeta(label="卡号", value=masked),
            UserFacingItemMeta(label="未还金额", value=_text(outstanding.get("formatted"), "--")),
        ]
        if isinstance(minimum, dict) and _valid_display_money(minimum.get("formatted")):
            metas.append(UserFacingItemMeta(label="最低还款", value=minimum["formatted"]))
        if isinstance(total_limit, dict) and _valid_display_money(total_limit.get("formatted")):
            metas.append(UserFacingItemMeta(label="总额度", value=total_limit["formatted"]))
        if isinstance(available, dict) and _valid_display_money(available.get("formatted")):
            metas.append(UserFacingItemMeta(label="可用额度", value=available["formatted"]))
        statement_day = output.get("statementDay")
        due_day = output.get("dueDay")
        if isinstance(statement_day, int):
            metas.append(UserFacingItemMeta(label="账单日", value=f"每月{statement_day}日"))
        if isinstance(due_day, int):
            metas.append(UserFacingItemMeta(label="到期还款日", value=f"每月{due_day}日"))
        detail_parts: list[str] = []
        note = output.get("note")
        if isinstance(note, str) and note:
            detail_parts.append(note)
        return UserFacingResult(
            kind="credit_card_bill",
            title=f"{product_name}本期账单",
            summary=f"未还 {_text(outstanding.get('formatted'), '--')}" + ("，请尽快还款" if is_due else "，账单已结清"),
            items=[
                f"未还金额 {_text(outstanding.get('formatted'), '--')}",
                f"最低还款 {_text(minimum.get('formatted'), '--')}" if isinstance(minimum, dict) else "",
                f"可用额度 {_text(available.get('formatted'), '--')}" if isinstance(available, dict) else "",
            ],
            item_cards=(UserFacingItemCard(
                title=product_name,
                subtitle=masked,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="立即还款" if is_due else "展开账单说明",
                    message=f"还{_text(output.get('maskedNo'), '')}信用卡" if is_due else None,
                    detail="\n".join(detail_parts) if detail_parts else None,
                ),
            ),),
        )
    if capability_id == "bank.loan.list":
        loans = output.get("data")
        if not isinstance(loans, list) or not loans:
            return None
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        for loan in loans[:10]:
            if not isinstance(loan, dict):
                continue
            name = _text(loan.get("productName"), "贷款")
            outstanding = loan.get("outstandingPrincipal")
            outstanding_text = _text(outstanding.get("formatted"), "--") if isinstance(outstanding, dict) else "--"
            status_text = {"ACTIVE": "还款中", "SETTLED": "已结清"}.get(loan.get("status"), "状态未知")
            items.append(" · ".join(item for item in (name, outstanding_text, status_text) if item))

            metas = [UserFacingItemMeta(label="状态", value=status_text)]
            if isinstance(outstanding, dict) and _valid_display_money(outstanding.get("formatted")):
                metas.append(UserFacingItemMeta(label="剩余本金", value=outstanding["formatted"]))
            monthly = loan.get("monthlyPayment")
            if isinstance(monthly, dict) and _valid_display_money(monthly.get("formatted")):
                metas.append(UserFacingItemMeta(label="月供", value=monthly["formatted"]))
            rate_bp = loan.get("annualRateBp")
            if isinstance(rate_bp, int):
                metas.append(UserFacingItemMeta(label="年利率", value=f"{rate_bp / 100:.2f}%"))
            remaining = loan.get("remainingMonths")
            if isinstance(remaining, int) and remaining > 0:
                metas.append(UserFacingItemMeta(label="剩余期限", value=f"{remaining}个月"))

            detail_parts = []
            contract_no = loan.get("contractNo")
            if isinstance(contract_no, str) and contract_no:
                detail_parts.append(f"合同编号：{contract_no}")
            repayment_method = loan.get("repaymentMethod")
            method_text = {"EQUAL_INSTALLMENT": "等额本息", "EQUAL_PRINCIPAL": "等额本金", "LUMP_SUM": "到期还本付息"}.get(repayment_method)
            if method_text:
                detail_parts.append(f"还款方式：{method_text}")
            repayment_account = loan.get("repaymentAccount")
            if isinstance(repayment_account, dict):
                acct_name = _text(repayment_account.get("name"), "")
                acct_masked = _text(repayment_account.get("maskedNo"), "")
                if acct_name or acct_masked:
                    detail_parts.append(f"还款账户：{acct_name} {acct_masked}".strip())
            cards.append(UserFacingItemCard(
                title=name,
                subtitle=status_text,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="展开贷款信息",
                    detail="\n".join(detail_parts) if detail_parts else None,
                ),
            ))
        if not cards:
            return None
        total_out = output.get("totalOutstanding")
        monthly_obl = output.get("monthlyObligation")
        summary_parts = [f"已查询到 {len(cards)} 笔贷款"]
        if isinstance(total_out, dict) and _valid_display_money(total_out.get("formatted")):
            summary_parts.append(f"剩余本金合计 {total_out['formatted']}")
        if isinstance(monthly_obl, dict) and _valid_display_money(monthly_obl.get("formatted")):
            summary_parts.append(f"月供合计 {monthly_obl['formatted']}")
        return UserFacingResult(
            kind="loan_list",
            title="我的贷款",
            summary="；".join(summary_parts),
            items=items,
            item_cards=tuple(cards),
        )
    if capability_id == "bank.obligation.list":
        obligation_items = output.get("items")
        if not isinstance(obligation_items, list) or not obligation_items:
            return None
        total = output.get("total")
        if not isinstance(total, dict) or not isinstance(total.get("amountMinor"), int):
            return None
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        for obl in obligation_items[:10]:
            if not isinstance(obl, dict):
                continue
            name = _text(obl.get("displayName"), "待还项")
            amount = obl.get("amount")
            if not isinstance(amount, dict) or not isinstance(amount.get("amountMinor"), int):
                continue
            currency = _text(amount.get("currency"), "CNY")
            amount_text = _format_minor(amount["amountMinor"], currency)
            obl_type = obl.get("type")
            type_text = {"credit_card": "信用卡", "loan": "贷款", "utility": "生活缴费"}.get(obl_type, "待还")
            status = obl.get("executionStatus")
            status_text = "可还款" if status == "available" else "暂不可还"
            items.append(f"{name} {amount_text} · {type_text} · {status_text}")

            metas = [
                UserFacingItemMeta(label="类型", value=type_text),
                UserFacingItemMeta(label="金额", value=amount_text),
                UserFacingItemMeta(label="状态", value=status_text),
            ]
            due_at = obl.get("dueAt")
            if isinstance(due_at, str) and due_at:
                metas.append(UserFacingItemMeta(label="到期日", value=due_at[:10]))

            cards.append(UserFacingItemCard(
                title=name,
                subtitle=f"{type_text} · {amount_text}",
                metas=tuple(metas),
                action=(
                    UserFacingItemAction(
                        label="立即还款",
                        message=f"还{name}",
                    )
                    if status == "available"
                    else None
                ),
            ))
        if not cards:
            return None
        total_currency = _text(total.get("currency"), "CNY")
        return UserFacingResult(
            kind="obligation_list",
            title="待还汇总",
            summary=f"待还共 {len(cards)} 笔，合计 {_format_minor(total['amountMinor'], total_currency)}",
            items=items,
            item_cards=tuple(cards),
            actions=(
                UserFacingItemAction(label="一键还款", message="帮我把该还的钱都处理掉"),
            ),
        )
    if capability_id == "bank.wealth.salary-insights.get":
        summary_text = _text(output.get("summary"), "")
        avg_salary = output.get("monthlyAvgSalary")
        avg_expense = output.get("monthlyAvgExpense")
        avg_net = output.get("monthlyAvgNet")
        payday = output.get("paydayHint")
        if not isinstance(avg_salary, dict) or not _valid_display_money(avg_salary.get("formatted")):
            return None
        items: list[str] = [
            f"月均工资 {_text(avg_salary.get('formatted'), '--')}",
        ]
        if isinstance(avg_expense, dict) and _valid_display_money(avg_expense.get("formatted")):
            items.append(f"月均支出 {_text(avg_expense['formatted'], '--')}")
        if isinstance(avg_net, dict) and _valid_display_money(avg_net.get("formatted")):
            items.append(f"月均结余 {_text(avg_net['formatted'], '--')}")
        if isinstance(payday, str) and payday:
            items.append(payday)
        metas: list[UserFacingItemMeta] = [
            UserFacingItemMeta(label="月均工资", value=_text(avg_salary.get("formatted"), "--")),
        ]
        if isinstance(avg_expense, dict) and _valid_display_money(avg_expense.get("formatted")):
            metas.append(UserFacingItemMeta(label="月均支出", value=avg_expense["formatted"]))
        if isinstance(avg_net, dict) and _valid_display_money(avg_net.get("formatted")):
            metas.append(UserFacingItemMeta(label="月均结余", value=avg_net["formatted"]))
        return UserFacingResult(
            kind="salary_insights",
            title="发薪与收支",
            summary=summary_text or "近一年工资流水与收支画像已生成",
            items=items,
            item_cards=(UserFacingItemCard(
                title="月度收支概览",
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="发薪理财建议",
                    message="查看我的理财推荐",
                ),
            ),) if metas else (),
        )
    if capability_id == "bank.wealth.advisory.context.get":
        status = output.get("recommendationStatus")
        risk = output.get("riskAssessment")
        risk_level = _text(risk.get("level"), "未取得") if isinstance(risk, dict) else "未取得"
        required_actions = output.get("requiredActions")
        action_items = required_actions if isinstance(required_actions, list) else []
        # 槽位化补录表单：字段集合完全由 provider 声明的缺失偏好驱动，
        # 投资目标选项来自受控词表，其余为数值输入。单位不出现在标签里，
        # 由前端拼装话术时按字段名补齐（个月/元），避免"（月）：12个月"式冗余。
        _PREFERENCE_FORM_FIELDS = {
            "investmentGoal": {
                "name": "investmentGoal",
                "label": "投资目标",
                "type": "select",
                "options": ["本金保全", "稳健增值", "长期增值"],
                "placeholder": None,
            },
            "horizonMonths": {
                "name": "horizonMonths",
                "label": "预计持有期限",
                "type": "number",
                "options": [],
                "placeholder": "如 12（月）",
            },
            "plannedAmountMinor": {
                "name": "plannedAmount",
                "label": "计划金额",
                "type": "number",
                "options": [],
                "placeholder": "如 50000（元）",
            },
        }
        missing_preference = [
            name
            for name in (
                output.get("missingPreferenceFields")
                if isinstance(output.get("missingPreferenceFields"), list)
                else []
            )
            if isinstance(name, str)
        ]
        missing_set = set(missing_preference)
        missing_form_fields = [
            dict(_PREFERENCE_FORM_FIELDS[name])
            for name in missing_preference
            if name in _PREFERENCE_FORM_FIELDS
        ]
        # 一次收齐：目标/期限缺失阶段的表单同时带上金额（选填），
        # 用户愿意即可一轮给全，服务端再按实际缺省按需追问。
        if "plannedAmountMinor" not in missing_set and missing_set - {"plannedAmountMinor"}:
            optional_amount = dict(_PREFERENCE_FORM_FIELDS["plannedAmountMinor"])
            optional_amount["required"] = False
            missing_form_fields.append(optional_amount)
        preference_form = (
            UserResponseActionForm.model_validate({"fields": missing_form_fields})
            if missing_form_fields
            else None
        )
        actions = tuple(
            UserFacingItemAction(
                label=_text(item.get("title"), "继续完善信息"),
                message=_text(item.get("message"), "继续完善理财建议所需信息"),
                interaction="compose",
                form=preference_form,
            )
            for item in action_items[:2]
            if isinstance(item, dict)
        )
        if status == "risk_assessment_required":
            raw_status = risk.get("status") if isinstance(risk, dict) else "MISSING"
            title = "风险测评已过期" if raw_status == "EXPIRED" else "需要完成风险测评"
            return UserFacingResult(
                kind="wealth_advisory_risk_assessment_required",
                title=title,
                summary="个性化理财建议需要有效的风险承受能力测评，本次未直接推荐产品。",
                items=(f"当前测评状态：{raw_status}",),
                actions=actions,
            )
        if status == "preferences_required":
            missing = output.get("missingPreferenceFields")
            labels = {
                "investmentGoal": "投资目标",
                "horizonMonths": "预计持有期限",
                "plannedAmountMinor": "计划金额",
            }
            missing_names = (
                [item for item in missing if isinstance(item, str)]
                if isinstance(missing, list)
                else []
            )
            # 补录表单已把缺失字段渲染成可编辑输入框；正文与条目只保留表单未覆盖的字段，
            # 避免同一批字段名在卡片上重复出现（正文一遍 + 条目一遍 + 表单标签一遍）。
            form_covered = {field["name"] for field in missing_form_fields}
            uncovered_labels = [
                labels.get(name, name) for name in missing_names if name not in form_covered
            ]
            if uncovered_labels:
                summary = "银行已有的风险测评、资金和持仓信息已查询；还需要了解" + "、".join(uncovered_labels) + "。"
            elif missing_form_fields:
                summary = "银行已有的风险测评、资金和持仓信息已查询；请在下方确认并补充本次投资安排。"
            else:
                summary = "银行已有的风险测评、资金和持仓信息已查询；还需要了解本次投资安排。"
            # 记忆默认偏好必须透明可见：已使用的目标/期限向用户说明来源，用户当前说法优先覆盖
            goal_labels = {
                "capital_preservation": "本金保全",
                "steady_growth": "稳健增值",
                "long_term_growth": "长期增值",
            }
            preferences = output.get("preferences")
            preference_items = list(uncovered_labels)
            if isinstance(preferences, dict):
                applied = []
                goal = preferences.get("investmentGoal")
                horizon = preferences.get("horizonMonths")
                if goal in goal_labels:
                    applied.append(f"投资目标：{goal_labels[goal]}")
                if isinstance(horizon, int) and horizon > 0:
                    applied.append(f"预计持有：约 {horizon} 个月")
                if applied:
                    preference_items.append(
                        "已按您的投资偏好：" + " · ".join(applied) + "（如需调整请直接说明）"
                    )
            return UserFacingResult(
                kind="wealth_advisory_preferences_required",
                title="完善本次投资安排",
                summary=summary,
                items=tuple(preference_items),
                actions=actions,
            )
        if status in {"no_suitable_products", "no_suitable_plan"}:
            return UserFacingResult(
                kind="wealth_advisory_no_match",
                title="当前条件下暂未形成合适方案",
                summary="当前在售产品无法同时满足风险等级、计划金额、起购要求和持有期限，请调整条件后重新分析。",
                actions=actions,
            )
        if status != "ready":
            return None
        primary_plan = output.get("primaryPlan")
        alternative_plans = output.get("alternativePlans")
        if isinstance(primary_plan, dict):
            raw_plans = [primary_plan]
            if isinstance(alternative_plans, list):
                raw_plans.extend(item for item in alternative_plans[:2] if isinstance(item, dict))
            plans = []
            for index, plan in enumerate(raw_plans):
                total = plan.get("totalAmount")
                allocations = plan.get("allocations")
                if not isinstance(total, dict) or not isinstance(allocations, list):
                    continue
                projected_allocations = []
                for allocation in allocations[:5]:
                    if not isinstance(allocation, dict):
                        continue
                    amount = allocation.get("amount")
                    if not isinstance(amount, dict):
                        continue
                    reasons = allocation.get("reasons")
                    detail_lines = [
                        _text(item, "") for item in reasons[:5]
                    ] if isinstance(reasons, list) else []
                    rate_note = _text(allocation.get("rateNote"), "")
                    if rate_note:
                        detail_lines.append("收益说明：" + rate_note)
                    projected_allocations.append(UserFacingPlanAllocation(
                        productId=_text(allocation.get("productId"), ""),
                        productName=_text(allocation.get("productName"), "适配产品"),
                        productCategory=_text(allocation.get("productCategory"), "WEALTH"),
                        productCategoryLabel=_text(allocation.get("productCategoryLabel"), "财富产品"),
                        portfolioRole=_text(allocation.get("portfolioRole"), "supplement"),
                        portfolioRoleLabel=_text(allocation.get("portfolioRoleLabel"), "补充配置"),
                        amountMinor=amount.get("amountMinor"),
                        amount=_text(amount.get("formatted"), "--"),
                        ratioBasisPoints=allocation.get("ratioBasisPoints"),
                        riskLevel=_risk_level(allocation.get("riskLevel")),
                        liquidity=_text(allocation.get("liquidity"), "以产品规则为准"),
                        suitability=_text(allocation.get("suitability"), "已通过本次适当性筛选"),
                        rateNote=rate_note or None,
                        detail="\n".join(filter(None, detail_lines)) or None,
                        productActionLabel="查看产品",
                    ))
                rationale = plan.get("rationale")
                plan_title = _text(plan.get("title"), "财富配置方案")
                allocation_summary = "、".join(
                    f"{item.product_name}，申购金额"
                    + (
                        f"{item.amount_minor // 100}元"
                        if item.amount_minor % 100 == 0
                        else f"{item.amount_minor // 100}元{item.amount_minor % 100:02d}分"
                    )
                    for item in projected_allocations
                )
                plans.append(UserFacingPlan(
                    id=_text(plan.get("planId"), ""),
                    title=plan_title,
                    summary=_text(plan.get("summary"), "已按当前条件形成完整方案"),
                    totalAmountMinor=total.get("amountMinor"),
                    totalAmount=_text(total.get("formatted"), "--"),
                    isPrimary=index == 0,
                    allocations=tuple(projected_allocations),
                    rationale=tuple(
                        _text(item, "") for item in rationale[:8]
                    ) if isinstance(rationale, list) else (),
                    selectLabel="选择该方案",
                    selectMessage=(
                        f"申购理财产品：{allocation_summary}，"
                        "请逐项核验并进入申购前确认"
                    ),
                ))
            if not plans:
                return None
            preferences = output.get("preferences")
            preference_items = []
            if isinstance(preferences, dict):
                planned = preferences.get("plannedAmount")
                if isinstance(planned, dict) and _valid_display_money(planned.get("formatted")):
                    preference_items.append("计划金额：" + planned["formatted"])
                horizon = preferences.get("horizonMonths")
                if isinstance(horizon, int) and horizon > 0:
                    preference_items.append(f"预计持有：{horizon} 个月")
            source_data = output.get("dataSources")
            source_count = len(source_data) if isinstance(source_data, list) else 0
            return UserFacingResult(
                kind="wealth_advisory",
                title="适合你的财富建议方案",
                summary=f"已结合当前{risk_level}风险测评及资金、持仓、投资历史等 {source_count} 类数据来源形成方案。",
                items=tuple(preference_items),
                plans=tuple(plans),
                actions=(UserFacingItemAction(
                    label="调整投资条件",
                    message="调整我的投资目标、期限、计划金额或产品范围",
                ),),
            )
        rec_items = output.get("recommendations")
        if not isinstance(rec_items, list) or not rec_items:
            return None
        role_labels = {
            "FIXED_INCOME_WEALTH": "主推核心",
            "CASH_MANAGEMENT": "流动性备用",
            "DEPOSIT": "稳健底仓",
        }
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        for rec in rec_items[:5]:
            if not isinstance(rec, dict):
                continue
            name = _text(rec.get("productName"), "适配产品")
            risk_text = _risk_level(rec.get("riskLevel"))
            role = role_labels.get(_text(rec.get("category"), ""), "补充配置")
            amount_range = rec.get("suggestedAmountRange")
            minimum = amount_range.get("minimum") if isinstance(amount_range, dict) else None
            maximum = amount_range.get("maximum") if isinstance(amount_range, dict) else None
            minimum_text = _text(minimum.get("formatted"), "--") if isinstance(minimum, dict) else "--"
            maximum_text = _text(maximum.get("formatted"), "--") if isinstance(maximum, dict) else "--"
            range_text = _amount_range_text(minimum_text, maximum_text)
            liquidity = _text(rec.get("liquidity"), "以产品规则为准")
            suitability = _text(rec.get("suitability"), "已通过本次适当性筛选")
            reasons = rec.get("reasons")
            reason_lines = [_text(item, "") for item in reasons[:5]] if isinstance(reasons, list) else []
            rate_note = _text(rec.get("rateNote"), "")
            rate_meta = rate_note if len(rate_note) <= 110 else rate_note[:110] + "…"
            product_id = rec.get("productId")
            card_id = None
            if isinstance(product_id, str) and re.fullmatch(
                r"^prod_[A-Za-z0-9_-]{1,63}$", product_id
            ):
                # 仅暴露公共目录形态的产品 id（详情页跳转锚点），内部标识一律不透出
                card_id = product_id
            items.append(f"{name} · {risk_text} · 建议金额 {range_text}")
            cards.append(UserFacingItemCard(
                id=card_id,
                title=name,
                subtitle=f"{role} · {risk_text}",
                metas=(
                    UserFacingItemMeta(label="适配结论", value=suitability),
                    UserFacingItemMeta(label="建议金额", value=range_text),
                    UserFacingItemMeta(label="期限与流动性", value=liquidity),
                    *((
                        UserFacingItemMeta(label="收益参考", value=rate_meta),
                    ) if rate_meta else ()),
                ),
                action=UserFacingItemAction(
                    label="展开适配理由",
                    detail="\n".join(filter(None, [*reason_lines, rate_note])) or None,
                ),
            ))
        if not cards:
            return None
        source_data = output.get("dataSources")
        source_count = len(source_data) if isinstance(source_data, list) else 0
        items.insert(0, "组合思路：主推核心 ≤50% · 流动性备用 ≤20% · 稳健底仓 ≤20%（同类产品共享仓位）")
        return UserFacingResult(
            kind="wealth_advisory",
            title="适合你的理财建议",
            summary=f"已结合当前{risk_level}风险测评及资金、持仓、投资历史等 {source_count} 类数据来源完成筛选。",
            items=tuple(items),
            item_cards=tuple(cards),
            actions=(UserFacingItemAction(label="调整投资条件", message="调整我的投资目标、期限或计划金额"),),
        )
    if capability_id == "bank.wealth.recommendations.get":
        rec_items = output.get("items")
        if not isinstance(rec_items, list) or not rec_items:
            return None
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        tag_text = {"emergency": "应急储备", "steady": "稳健配置", "growth": "成长增收益", "lock_in": "锁定收益"}
        for rec in rec_items[:10]:
            if not isinstance(rec, dict):
                continue
            name = _text(rec.get("productName"), "推荐产品")
            risk = _risk_level(rec.get("riskLevel"))
            rate_bp = rec.get("annualRateBp")
            rate_pct = f"{rate_bp / 100:.2f}%" if isinstance(rate_bp, int) else "—"
            tag = rec.get("tag")
            tag_label = tag_text.get(tag, tag or "推荐")
            items.append(f"{name} · {tag_label} · {risk} · {rate_pct}")

            metas = [
                UserFacingItemMeta(label="分类", value=tag_label),
                UserFacingItemMeta(label="风险等级", value=risk),
                UserFacingItemMeta(label="业绩基准", value=rate_pct),
            ]
            suggested = rec.get("suggestedAmount")
            if isinstance(suggested, dict) and _valid_display_money(suggested.get("formatted")):
                metas.append(UserFacingItemMeta(label="建议金额", value=suggested["formatted"]))
            rate_note = _text(rec.get("rateNote"), "")
            reason = _text(rec.get("reason"), "")

            cards.append(UserFacingItemCard(
                title=name,
                subtitle=tag_label,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="展开推荐理由",
                    detail="\n".join(filter(None, [rate_note, reason])) or None,
                ),
            ))
        if not cards:
            return None
        return UserFacingResult(
            kind="wealth_recommendations",
            title="理财推荐",
            summary=f"已根据收支画像匹配 {len(cards)} 款产品",
            items=items,
            item_cards=tuple(cards),
        )
    if capability_id == "bank.payee.validate":
        valid = output.get("valid")
        bank_name = _text(output.get("bankName"), "")
        masked = _text(output.get("maskedNo"), "")
        raw_status = output.get("status")
        status_text = {"ACTIVE": "正常", "INACTIVE": "已禁用", "DELETED": "已删除"}.get(raw_status, "状态未知")
        valid_text = "校验通过" if valid is True else "校验未通过"
        metas = [
            UserFacingItemMeta(label="收款银行", value=bank_name or "--"),
            UserFacingItemMeta(label="账号", value=masked or "--"),
            UserFacingItemMeta(label="状态", value=status_text),
            UserFacingItemMeta(label="校验结果", value=valid_text),
        ]
        return UserFacingResult(
            kind="payee_validation",
            title="收款方校验",
            summary=valid_text,
            items=[f"{bank_name} {masked}", valid_text],
            item_cards=(UserFacingItemCard(
                title=bank_name or "收款方校验",
                subtitle=masked,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="发起转账" if valid is True else "重新校验",
                    message="发起转账" if valid is True else None,
                ),
            ),),
            actions=(
                UserFacingItemAction(label="发起转账", message="发起转账"),
            ) if valid is True else (),
        )
    if capability_id == "bank.transfer.receipt.get":
        status = output.get("status")
        status_text = {"SUCCEEDED": "转账成功", "FAILED": "转账失败", "PROCESSING": "处理中"}.get(status, "状态未知")
        receipt_no = _text(output.get("receiptNo"), "")
        executed_at = _text(output.get("executedAt"), "")
        metas: list[UserFacingItemMeta] = [UserFacingItemMeta(label="状态", value=status_text)]
        # 附带的脱敏收款人/付款账户/金额详情：回单必须让用户核对对手方
        # 卡号与银行，否则成功回单只显示金额无法定位款项去向。
        from_account = output.get("fromAccount") if isinstance(output.get("fromAccount"), dict) else None
        payee = output.get("payee") if isinstance(output.get("payee"), dict) else None
        amount_obj = output.get("amount") if isinstance(output.get("amount"), dict) else None
        from_account_label: str | None = None
        payee_label: str | None = None
        amount_label: str | None = None
        if from_account is not None:
            from_name = _text(from_account.get("name"), "")
            from_masked = _text(from_account.get("maskedNo"), "")
            from_tail = re.sub(r"\s", "", from_masked)[-4:] if from_masked else ""
            if from_name and from_tail:
                from_account_label = f"{from_name} · 尾号{from_tail}"
            elif from_name:
                from_account_label = from_name
        if payee is not None:
            payee_name = _text(payee.get("name"), "")
            payee_bank = _text(payee.get("bankName"), "")
            payee_masked = _text(payee.get("maskedNo"), "")
            payee_tail = re.sub(r"\s", "", payee_masked)[-4:] if payee_masked else ""
            parts = [p for p in (payee_name, payee_bank, f"尾号{payee_tail}" if payee_tail else "") if p]
            if parts:
                payee_label = " · ".join(parts)
        if amount_obj is not None:
            amount_label = _text(amount_obj.get("formatted"), "")
        if from_account_label:
            metas.append(UserFacingItemMeta(label="付款账户", value=from_account_label))
        if payee_label:
            metas.append(UserFacingItemMeta(label="收款人", value=payee_label))
        if amount_label:
            metas.append(UserFacingItemMeta(label="转账金额", value=amount_label))
        if receipt_no:
            metas.append(UserFacingItemMeta(label="回单号", value=receipt_no))
        if executed_at:
            metas.append(UserFacingItemMeta(label="执行时间", value=_format_instant(executed_at)))
        failure_code = output.get("failureCode")
        if isinstance(failure_code, str) and failure_code:
            metas.append(UserFacingItemMeta(label="失败原因", value=failure_code))
        item_lines: list[str] = [status_text]
        if from_account_label:
            item_lines.append(f"付款账户 {from_account_label}")
        if payee_label:
            item_lines.append(f"收款人 {payee_label}")
        if amount_label:
            item_lines.append(f"转账金额 {amount_label}")
        if receipt_no:
            item_lines.append(f"回单号 {receipt_no}")
        return UserFacingResult(
            kind="transfer_receipt",
            title="转账回单",
            summary=status_text,
            items=item_lines,
            item_cards=(UserFacingItemCard(
                title="转账回单",
                subtitle=status_text,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="再次转账" if status == "SUCCEEDED" else "查看转账记录",
                    message="发起转账" if status == "SUCCEEDED" else "查看转账记录",
                ),
            ),),
        )
    if capability_id == "bank.credit-card.repayment.create":
        # 信用卡还款回单：output 含 fromAccount(name/maskedNo)、paid、
        # remainingOutstanding。用户需要看到付款账户尾号、还款金额、
        # 剩余未还，否则无法核对资金流向。状态非 SUCCEEDED 时不投影。
        status = output.get("status")
        if status != "SUCCEEDED":
            return None
        paid = output.get("paid")
        if not isinstance(paid, dict) or not _valid_display_money(paid.get("formatted")):
            return None
        paid_text = paid["formatted"]
        from_account = output.get("fromAccount") if isinstance(output.get("fromAccount"), dict) else None
        from_label: str | None = None
        if from_account is not None:
            from_name = _text(from_account.get("name"), "")
            from_masked = _text(from_account.get("maskedNo"), "")
            from_tail = re.sub(r"\s", "", from_masked)[-4:] if from_masked else ""
            if from_name and from_tail:
                from_label = f"{from_name} · 尾号{from_tail}"
            elif from_name:
                from_label = from_name
        remaining = output.get("remainingOutstanding")
        remaining_text = ""
        if isinstance(remaining, dict) and _valid_display_money(remaining.get("formatted")):
            remaining_text = remaining["formatted"]
        metas: list[UserFacingItemMeta] = [
            UserFacingItemMeta(label="还款金额", value=paid_text),
            UserFacingItemMeta(label="办理结果", value="还款成功"),
        ]
        if from_label:
            metas.append(UserFacingItemMeta(label="付款账户", value=from_label))
        if remaining_text:
            metas.append(UserFacingItemMeta(label="剩余未还", value=remaining_text))
        item_lines = [
            f"还款金额：{paid_text}",
            "办理结果：还款成功",
        ]
        if from_label:
            item_lines.append(f"付款账户：{from_label}")
        if remaining_text:
            item_lines.append(f"剩余未还：{remaining_text}")
        return UserFacingResult(
            kind="credit_card_repayment_receipt",
            title="信用卡还款已完成",
            summary=f"信用卡还款 {paid_text} 已成功",
            items=item_lines,
            item_cards=(UserFacingItemCard(
                title="信用卡还款",
                subtitle=f"还款 {paid_text}",
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="查看信用卡",
                    message="查看我的信用卡",
                ),
            ),),
        )
    if capability_id == "bank.credit-card.settings.get":
        online = output.get("onlineTransactionsEnabled")
        contactless = output.get("contactlessEnabled")
        overseas = output.get("overseasEnabled")
        single_limit = output.get("singleTransactionLimit")
        daily_limit = output.get("dailyLimit")
        metas = [
            UserFacingItemMeta(label="在线交易", value="已开启" if online else "已关闭"),
            UserFacingItemMeta(label="非接触支付", value="已开启" if contactless else "已关闭"),
            UserFacingItemMeta(label="境外交易", value="已开启" if overseas else "已关闭"),
        ]
        if isinstance(single_limit, dict) and _valid_display_money(single_limit.get("formatted")):
            metas.append(UserFacingItemMeta(label="单笔限额", value=single_limit["formatted"]))
        if isinstance(daily_limit, dict) and _valid_display_money(daily_limit.get("formatted")):
            metas.append(UserFacingItemMeta(label="日累计限额", value=daily_limit["formatted"]))
        return UserFacingResult(
            kind="credit_card_settings",
            title="交易安全设置",
            summary=f"在线交易{'已开启' if online else '已关闭'}，境外交易{'已开启' if overseas else '已关闭'}",
            items=[f"在线交易 {'已开启' if online else '已关闭'}", f"境外交易 {'已开启' if overseas else '已关闭'}"],
            item_cards=(UserFacingItemCard(
                title="交易安全设置",
                subtitle="信用卡安全配置",
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="返回卡片管理",
                    message="查看我的信用卡",
                ),
            ),),
        )
    if capability_id == "bank.loan.detail.get":
        name = _text(output.get("productName"), "贷款")
        status = output.get("status")
        status_text = {"ACTIVE": "还款中", "SETTLED": "已结清"}.get(status, "状态未知")
        outstanding = output.get("outstandingPrincipal")
        outstanding_text = _text(outstanding.get("formatted"), "--") if isinstance(outstanding, dict) else "--"
        metas = [UserFacingItemMeta(label="状态", value=status_text)]
        if isinstance(outstanding, dict) and _valid_display_money(outstanding.get("formatted")):
            metas.append(UserFacingItemMeta(label="剩余本金", value=outstanding["formatted"]))
        principal = output.get("principal")
        if isinstance(principal, dict) and _valid_display_money(principal.get("formatted")):
            metas.append(UserFacingItemMeta(label="贷款本金", value=principal["formatted"]))
        monthly = output.get("monthlyPayment")
        if isinstance(monthly, dict) and _valid_display_money(monthly.get("formatted")):
            metas.append(UserFacingItemMeta(label="月供", value=monthly["formatted"]))
        rate_bp = output.get("annualRateBp")
        if isinstance(rate_bp, int):
            metas.append(UserFacingItemMeta(label="年利率", value=f"{rate_bp / 100:.2f}%"))
        remaining = output.get("remainingMonths")
        if isinstance(remaining, int) and remaining > 0:
            metas.append(UserFacingItemMeta(label="剩余期限", value=f"{remaining}个月"))
        detail_parts: list[str] = []
        contract_no = output.get("contractNo")
        if isinstance(contract_no, str) and contract_no:
            detail_parts.append(f"合同编号：{contract_no}")
        repayment_method = output.get("repaymentMethod")
        method_text = {"EQUAL_INSTALLMENT": "等额本息", "EQUAL_PRINCIPAL": "等额本金", "LUMP_SUM": "到期还本付息"}.get(repayment_method)
        if method_text:
            detail_parts.append(f"还款方式：{method_text}")
        repayment_account = output.get("repaymentAccount")
        if isinstance(repayment_account, dict):
            acct_name = _text(repayment_account.get("name"), "")
            acct_masked = _text(repayment_account.get("maskedNo"), "")
            if acct_name or acct_masked:
                detail_parts.append(f"还款账户：{acct_name} {acct_masked}".strip())
        return UserFacingResult(
            kind="loan_detail",
            title=name,
            summary=f"{status_text} · 剩余本金 {outstanding_text}",
            items=[name, status_text, outstanding_text],
            item_cards=(UserFacingItemCard(
                title=name,
                subtitle=status_text,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="查看还款计划",
                    detail="\n".join(detail_parts) if detail_parts else None,
                    message=f"查看{name}的还款计划",
                ),
            ),),
        )
    if capability_id == "bank.loan.repayment.list":
        repayment_data = output.get("data")
        if not isinstance(repayment_data, list) or not repayment_data:
            return None
        product_name = _text(output.get("productName"), "贷款")
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        paid_count = 0
        for item in repayment_data[:12]:
            if not isinstance(item, dict):
                continue
            period_no = item.get("periodNo")
            due_date = _text(item.get("dueDate"), "")
            total = item.get("total")
            total_text = _text(total.get("formatted"), "--") if isinstance(total, dict) else "--"
            raw_status = item.get("status")
            status_text = {"PAID": "已还", "SCHEDULED": "待还", "OVERDUE": "逾期"}.get(raw_status, "状态未知")
            if raw_status == "PAID":
                paid_count += 1
            period_label = f"第{period_no}期" if isinstance(period_no, int) else "还款"
            items.append(" · ".join(part for part in (period_label, due_date, total_text, status_text) if part))
            metas = [UserFacingItemMeta(label="到期日", value=due_date or "--"), UserFacingItemMeta(label="状态", value=status_text)]
            principal = item.get("principal")
            if isinstance(principal, dict) and _valid_display_money(principal.get("formatted")):
                metas.append(UserFacingItemMeta(label="本金", value=principal["formatted"]))
            interest = item.get("interest")
            if isinstance(interest, dict) and _valid_display_money(interest.get("formatted")):
                metas.append(UserFacingItemMeta(label="利息", value=interest["formatted"]))
            if isinstance(total, dict) and _valid_display_money(total.get("formatted")):
                metas.append(UserFacingItemMeta(label="合计", value=total["formatted"]))
            paid_at = item.get("paidAt")
            if isinstance(paid_at, str) and paid_at:
                metas.append(UserFacingItemMeta(label="实还日期", value=paid_at))
            card_action = None
            if status_text in ("待还", "逾期"):
                card_action = UserFacingItemAction(
                    label="还款",
                    message=f"还{product_name}第{period_no}期" if isinstance(period_no, int) else f"还{product_name}",
                )
            cards.append(UserFacingItemCard(
                title=period_label,
                subtitle=f"{total_text} · {status_text}",
                metas=tuple(metas),
                action=card_action,
            ))
        if not cards:
            return None
        return UserFacingResult(
            kind="loan_repayment_list",
            title=f"{product_name}还款计划",
            summary=f"共 {len(cards)} 期，已还 {paid_count} 期",
            items=items,
            item_cards=tuple(cards),
        )
    if capability_id == "bank.loan.precalculate.create":
        purpose = output.get("purpose")
        purpose_text = {"CONSUMPTION": "消费", "RENOVATION": "装修", "HOUSING": "购房"}.get(purpose, "综合")
        estimated = output.get("estimatedLimits")
        based_on = output.get("basedOn")
        if not isinstance(estimated, dict) or not isinstance(based_on, dict):
            return None
        metas: list[UserFacingItemMeta] = [UserFacingItemMeta(label="用途", value=purpose_text)]
        limit_labels = {"kuaidai": "信用快贷", "jianyidai": "建易贷", "renovation": "装修分期"}
        items: list[str] = []
        for key in ("kuaidai", "jianyidai", "renovation"):
            limit = estimated.get(key)
            label = limit_labels.get(key, key)
            limit_text = _text(limit.get("formatted"), "--") if isinstance(limit, dict) else "--"
            metas.append(UserFacingItemMeta(label=label, value=limit_text))
            items.append(f"{label} {limit_text}")
        income = based_on.get("monthlyAvgIncome")
        if isinstance(income, dict) and _valid_display_money(income.get("formatted")):
            metas.append(UserFacingItemMeta(label="月均收入", value=income["formatted"]))
        obligation = based_on.get("monthlyObligation")
        if isinstance(obligation, dict) and _valid_display_money(obligation.get("formatted")):
            metas.append(UserFacingItemMeta(label="存量月供", value=obligation["formatted"]))
        disposable = based_on.get("monthlyDisposable")
        if isinstance(disposable, dict) and _valid_display_money(disposable.get("formatted")):
            metas.append(UserFacingItemMeta(label="可支配收入", value=disposable["formatted"]))
        return UserFacingResult(
            kind="loan_precalculate",
            title="贷款额度测算",
            summary=f"用途：{purpose_text}",
            items=items,
            item_cards=(UserFacingItemCard(
                title="测算结果",
                subtitle=purpose_text,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="查看贷款产品",
                    message="有哪些贷款产品",
                ),
            ),),
        )
    if capability_id == "bank.utility.bill.list":
        bills = output.get("data")
        if not isinstance(bills, list) or not bills:
            return None
        type_text = {"ELECTRICITY": "电费", "WATER": "水费", "GAS": "燃气费", "PHONE": "话费"}
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        unpaid_count = 0
        for bill in bills[:10]:
            if not isinstance(bill, dict):
                continue
            name = _text(bill.get("name"), "生活缴费")
            bill_type = bill.get("type")
            type_label = type_text.get(bill_type, _text(bill_type, "缴费"))
            period = _text(bill.get("period"), "")
            amount = bill.get("amount")
            amount_text = _text(amount.get("formatted"), "--") if isinstance(amount, dict) else "--"
            due_date = _text(bill.get("dueDate"), "")
            raw_status = bill.get("status")
            status_text = {"UNPAID": "待缴费", "PAID": "已缴费"}.get(raw_status, "状态未知")
            if raw_status == "UNPAID":
                unpaid_count += 1
            items.append(" · ".join(part for part in (name, period, amount_text, status_text) if part))
            metas = [
                UserFacingItemMeta(label="账期", value=period or "--"),
                UserFacingItemMeta(label="金额", value=amount_text),
                UserFacingItemMeta(label="到期日", value=due_date or "--"),
                UserFacingItemMeta(label="状态", value=status_text),
            ]
            cards.append(UserFacingItemCard(
                title=name,
                subtitle=f"{type_label} · {amount_text}",
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="去缴费",
                    message=f"我要缴纳{name}",
                ) if raw_status == "UNPAID" else None,
            ))
        if not cards:
            return None
        return UserFacingResult(
            kind="utility_bill_list",
            title="生活缴费",
            summary=f"共 {len(cards)} 笔账单，待缴 {unpaid_count} 笔",
            items=items,
            item_cards=tuple(cards),
        )
    if capability_id == "bank.wealth.product.detail.get":
        name = _text(output.get("name"), "理财产品")
        risk = _risk_level(output.get("riskLevel"))
        rate_bp = output.get("annualRateBp")
        rate_pct = f"{rate_bp / 100:.2f}%" if isinstance(rate_bp, int) and rate_bp > 0 else "—"
        metas: list[UserFacingItemMeta] = [
            UserFacingItemMeta(label="风险等级", value=risk),
            UserFacingItemMeta(label="业绩基准", value=rate_pct),
        ]
        min_purchase = output.get("minPurchase")
        if isinstance(min_purchase, dict) and _valid_display_money(min_purchase.get("formatted")):
            metas.append(UserFacingItemMeta(label="起购金额", value=min_purchase["formatted"]))
        term_type = output.get("termType")
        term_days = output.get("termDays")
        if term_type == "fixed" and isinstance(term_days, int) and term_days > 0:
            metas.append(UserFacingItemMeta(label="期限", value=_term_text(term_days)))
        elif term_type == "demand":
            metas.append(UserFacingItemMeta(label="期限", value="随时可取"))
        issuer = _text(output.get("issuer"), "")
        if issuer:
            metas.append(UserFacingItemMeta(label="发行机构", value=issuer))
        raw_status = output.get("status")
        status_text = {"ON_SALE": "在售", "SUSPENDED": "已暂停", "CLOSED": "已结束"}.get(raw_status, "状态未知")
        metas.append(UserFacingItemMeta(label="状态", value=status_text))
        detail_parts: list[str] = []
        product_code = output.get("productCode")
        if isinstance(product_code, str) and product_code:
            detail_parts.append(f"产品编码：{product_code}")
        rate_note = output.get("rateNote")
        if isinstance(rate_note, str) and rate_note:
            detail_parts.append(rate_note)
        purchase_channel = output.get("purchaseChannel")
        if isinstance(purchase_channel, str) and purchase_channel:
            detail_parts.append(f"购买渠道：{purchase_channel}")
        features = output.get("features")
        if isinstance(features, list):
            for feature in features:
                if isinstance(feature, str) and feature:
                    detail_parts.append(feature)
        return UserFacingResult(
            kind="wealth_product_detail",
            title=name,
            summary=f"{risk} · 业绩基准 {rate_pct}",
            items=[name, risk, rate_pct],
            item_cards=(UserFacingItemCard(
                title=name,
                subtitle=risk,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="立即申购",
                    detail="\n".join(detail_parts) if detail_parts else None,
                    message=f"我要申购{name}",
                ),
            ),),
        )
    if capability_id == "bank.wealth.transaction.list":
        txn_data = output.get("data")
        if not isinstance(txn_data, list) or not txn_data:
            return None
        direction_text = {"PURCHASE": "申购", "REDEMPTION": "赎回"}
        items: list[str] = []
        cards: list[UserFacingItemCard] = []
        for txn in txn_data[:10]:
            if not isinstance(txn, dict):
                continue
            name = _text(txn.get("productName"), "理财产品")
            direction = txn.get("direction")
            dir_label = direction_text.get(direction, _text(direction, "交易"))
            amount = txn.get("amount")
            amount_text = _text(amount.get("formatted"), "--") if isinstance(amount, dict) else "--"
            status = _text(txn.get("status"), "")
            occurred = _text(txn.get("occurredAt"), "")
            items.append(" · ".join(part for part in (name, dir_label, amount_text, occurred) if part))
            metas = [
                UserFacingItemMeta(label="类型", value=dir_label),
                UserFacingItemMeta(label="金额", value=amount_text),
            ]
            earnings = txn.get("earnings")
            if isinstance(earnings, dict) and _valid_display_money(earnings.get("formatted")):
                metas.append(UserFacingItemMeta(label="收益", value=earnings["formatted"]))
            if status:
                metas.append(UserFacingItemMeta(label="状态", value=status))
            if occurred:
                metas.append(UserFacingItemMeta(label="日期", value=occurred))
            cards.append(UserFacingItemCard(
                title=name,
                subtitle=f"{dir_label} · {amount_text}",
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="查看持仓",
                    message="查看我的理财持仓",
                ),
            ))
        if not cards:
            return None
        return UserFacingResult(
            kind="wealth_transaction_list",
            title="申赎记录",
            summary=f"已查询到 {len(cards)} 笔记录",
            items=items,
            item_cards=tuple(cards),
        )
    if capability_id == "bank.wealth.purchase.create":
        product_name = _text(output.get("productName"), "理财产品")
        amount = output.get("amount")
        if (
            output.get("status") != "CONFIRMED"
            or not isinstance(amount, dict)
            or not _valid_display_money(amount.get("formatted"))
        ):
            return None
        amount_text = amount["formatted"]
        metas = [
            UserFacingItemMeta(label="申购金额", value=amount_text),
            UserFacingItemMeta(label="办理结果", value="申购成功"),
        ]
        return UserFacingResult(
            kind="wealth_purchase_receipt",
            title="理财申购已完成",
            summary=f"{product_name}已成功申购 {amount_text}",
            items=[
                f"产品：{product_name}",
                f"申购金额：{amount_text}",
                "办理结果：申购成功",
            ],
            item_cards=(UserFacingItemCard(
                title=product_name,
                subtitle="申购成功",
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="查看持仓",
                    message="查看我的理财持仓",
                ),
            ),),
        )
    if capability_id == "bank.account.detail.get":
        name = _text(output.get("name"), "银行账户")
        account_type = output.get("type")
        type_text = {"DEBIT": "借记卡", "CREDIT": "信用卡"}.get(
            account_type, account_type if isinstance(account_type, str) and account_type else "账户"
        )
        status_text = {"ACTIVE": "正常使用中", "FROZEN": "已冻结", "CLOSED": "已销户"}.get(
            output.get("status"), "状态未知"
        )
        masked_no = _text(output.get("maskedNo"), "")
        currency = _text(output.get("currency"), "CNY")
        metas = [
            UserFacingItemMeta(label="类型", value=type_text),
            UserFacingItemMeta(label="状态", value=status_text),
            UserFacingItemMeta(label="币种", value=currency),
        ]
        if masked_no:
            metas.insert(1, UserFacingItemMeta(label="卡号", value=masked_no))
        return UserFacingResult(
            kind="account_detail",
            title=f"{name}详情",
            summary=f"{name}（{type_text}）{status_text}",
            items=(
                [f"账户：{name}" + (f"（{masked_no}）" if masked_no else "")]
                + [f"类型：{type_text}", f"状态：{status_text}", f"币种：{currency}"]
            ),
            item_cards=(UserFacingItemCard(
                title=name,
                subtitle=type_text,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="查询余额",
                    message="查询账户余额",
                ),
            ),),
        )
    if capability_id == "bank.transfer.status.get":
        status = output.get("status")
        status_text = {
            "DRAFT": "草稿", "VALIDATED": "已校验", "PENDING_CONFIRMATION": "待本人确认",
            "AUTHORIZED": "已授权，等待执行", "SUCCEEDED": "转账成功", "FAILED": "转账失败",
            "CANCELLED": "已取消",
        }.get(status, "状态未知")
        amount = output.get("amount")
        amount_text = (
            amount["formatted"]
            if isinstance(amount, dict) and _valid_display_money(amount.get("formatted"))
            else "--"
        )
        purpose = _text(output.get("purpose"), "")
        metas = [
            UserFacingItemMeta(label="状态", value=status_text),
            UserFacingItemMeta(label="金额", value=amount_text),
        ]
        if purpose:
            metas.append(UserFacingItemMeta(label="用途", value=purpose))
        return UserFacingResult(
            kind="transfer_status",
            title="转账状态",
            summary=f"这笔转账{status_text}，金额 {amount_text}",
            items=[f"状态：{status_text}", f"金额：{amount_text}"] + ([f"用途：{purpose}"] if purpose else []),
            item_cards=(UserFacingItemCard(
                title="转账指令",
                subtitle=status_text,
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="再次转账" if status == "SUCCEEDED" else "发起转账",
                    message="发起转账",
                ),
            ),),
        )
    if capability_id == "bank.customer.profile.get":
        display_name = _text(output.get("displayName"), "客户")
        mobile_masked = _text(output.get("mobileMasked"), "")
        customer_level = output.get("customerLevel")
        level_text = {"STANDARD": "标准客户", "GOLD": "黄金客户", "PLATINUM": "白金客户", "DIAMOND": "钻石客户"}.get(
            customer_level, customer_level if isinstance(customer_level, str) and customer_level else "标准客户"
        )
        metas = [UserFacingItemMeta(label="客户等级", value=level_text)]
        if mobile_masked:
            metas.insert(0, UserFacingItemMeta(label="预留手机", value=mobile_masked))
        return UserFacingResult(
            kind="customer_profile",
            title="客户资料",
            summary=f"{display_name}，{level_text}",
            items=(
                [f"姓名：{display_name}"]
                + ([f"预留手机：{mobile_masked}"] if mobile_masked else [])
                + [f"客户等级：{level_text}"]
            ),
            item_cards=(UserFacingItemCard(
                title=display_name,
                subtitle=level_text,
                metas=tuple(metas),
            ),),
        )
    if capability_id == "bank.wealth.redeem.create":
        product_name = _text(output.get("productName"), "理财产品")
        status = output.get("status")
        status_text = {"SUBMITTED": "已受理", "CONFIRMED": "赎回成功", "SETTLED": "资金已到账", "FAILED": "赎回失败"}.get(
            status, "处理中"
        )
        amount = output.get("amount")
        if not isinstance(amount, dict) or not _valid_display_money(amount.get("formatted")):
            return None
        amount_text = amount["formatted"]
        earnings = output.get("earnings")
        metas = [
            UserFacingItemMeta(label="赎回金额", value=amount_text),
            UserFacingItemMeta(label="办理结果", value=status_text),
        ]
        if isinstance(earnings, dict) and _valid_display_money(earnings.get("formatted")):
            metas.append(UserFacingItemMeta(label="持有收益", value=earnings["formatted"]))
        return UserFacingResult(
            kind="wealth_redemption_receipt",
            title="理财赎回已完成" if status in ("CONFIRMED", "SETTLED") else "理财赎回申请已提交",
            summary=f"{product_name}赎回 {amount_text}，{status_text}",
            items=[
                f"产品：{product_name}",
                f"赎回金额：{amount_text}",
                f"办理结果：{status_text}",
            ],
            item_cards=(UserFacingItemCard(
                title=product_name,
                subtitle=f"赎回 {amount_text}",
                metas=tuple(metas),
                action=UserFacingItemAction(
                    label="查看持仓",
                    message="查看我的理财持仓",
                ),
            ),),
        )
    return None


def _text(value: Any, fallback: str) -> str:
    return value[:160] if isinstance(value, str) and value else fallback


def _amount_range_text(minimum_text: str, maximum_text: str) -> str:
    if minimum_text == maximum_text:
        return minimum_text
    # 「起 · 至多」中间含 3 个文字，防止两个金额数字组被账号形态防护链接成疑似完整账号
    return f"{minimum_text} 起 · 至多 {maximum_text}"


def _card_status(value: Any) -> str:
    return {"ACTIVE": "状态正常", "INACTIVE": "待激活", "LOST": "已挂失"}.get(value, "状态未知")


def _risk_level(value: Any) -> str:
    return {
        "R1": "低风险", "R2": "中低风险", "R3": "中风险",
        "R4": "中高风险", "R5": "高风险",
    }.get(value, "风险等级待确认")


def _term_text(term_days: int) -> str:
    if term_days >= 365:
        years = term_days // 365
        rem = term_days % 365
        return f"{years}年" + (f"{rem // 30}个月" if rem >= 30 else "")
    if term_days >= 30:
        return f"{term_days // 30}个月"
    return f"{term_days}天"


def _valid_display_money(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 80


def _valid_period(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) is not None


def _format_instant(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return ""
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _format_minor(amount_minor: int, currency: str) -> str:
    symbol = "¥" if currency == "CNY" else currency + " "
    return f"{symbol}{amount_minor / 100:,.2f}"
