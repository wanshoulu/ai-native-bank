'use strict';

const { BankApiError } = require('./api-contract');
const { SPENDING_CATEGORY_LABELS } = require('./spending-category-labels');

function createCashflowService(options = {}) {
  const salaryProvider = options.salaryProvider;
  const obligationProvider = options.obligationProvider;
  const transactionProvider = options.transactionProvider;
  const supplementaryAccountProvider = typeof options.supplementaryAccountProvider === 'function'
    ? options.supplementaryAccountProvider
    : async () => [];
  const policyProvider = typeof options.policyProvider === 'function'
    ? options.policyProvider
    : async () => null;
  const policy = options.policy || {};
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  if (typeof salaryProvider !== 'function' || typeof obligationProvider !== 'function'
    || typeof transactionProvider !== 'function'
    || !Number.isSafeInteger(policy.livingBudgetMinor) || policy.livingBudgetMinor < 0
    || !Number.isSafeInteger(policy.emergencyReserveMinor) || policy.emergencyReserveMinor < 0) {
    throw new TypeError('cashflow service dependencies are invalid');
  }

  // 记忆召回优先（用户明示偏好是唯一权威来源），静态 persona 策略只作兜底。
  async function resolvePolicy(customerId) {
    const recalled = await policyProvider(customerId);
    const livingValue = recalled ? recalled.livingBudgetMinor : undefined;
    const reserveValue = recalled ? recalled.emergencyReserveMinor : undefined;
    const living = Number.isSafeInteger(livingValue) && livingValue >= 0 ? livingValue : policy.livingBudgetMinor;
    const reserve = Number.isSafeInteger(reserveValue) && reserveValue >= 0 ? reserveValue : policy.emergencyReserveMinor;
    return {
      livingBudgetMinor: living,
      emergencyReserveMinor: reserve,
      livingBudgetMemory: livingValue != null && living === livingValue,
      emergencyReserveMemory: reserveValue != null && reserve === reserveValue,
    };
  }

  async function getSalaryPlan(customerId) {
    requireId(customerId, 'customerId');
    const [salaryRecord, obligations, supplementaryRecords, effectivePolicy] = await Promise.all([
      salaryProvider(customerId), obligationProvider(customerId), supplementaryAccountProvider(customerId),
      resolvePolicy(customerId),
    ]);
    const salaryMinor = requireMinor(salaryRecord.amountMinor, 'salary');
    const obligationMinor = requireNonNegative(obligations.amountMinor, 'obligations');
    const availableMinor = obligations.availableBalanceMinor === undefined
      ? salaryMinor
      : requireNonNegative(obligations.availableBalanceMinor, 'availableBalance');
    let remaining = availableMinor;
    const living = Math.min(remaining, effectivePolicy.livingBudgetMinor); remaining -= living;
    const reserve = Math.min(remaining, effectivePolicy.emergencyReserveMinor); remaining -= reserve;
    const plannedRepayment = Math.min(remaining, obligationMinor); remaining -= plannedRepayment;
    const uncoveredObligations = obligationMinor - plannedRepayment;
    const currency = salaryRecord.currency || 'CNY';
    const items = [
      { id: 'planned_repayment', amountMinor: plannedRepayment, sourceRefs: obligations.sourceRefs || [] },
      {
        id: 'living_budget', amountMinor: living,
        sourceRefs: [effectivePolicy.livingBudgetMemory ? 'memory:monthly_living_budget' : 'policy:living_budget'],
      },
      {
        id: 'emergency_reserve', amountMinor: reserve,
        sourceRefs: [effectivePolicy.emergencyReserveMemory ? 'memory:emergency_reserve_preference' : 'policy:emergency_reserve'],
      },
      { id: 'allocatable', amountMinor: remaining, sourceRefs: [salaryRecord.sourceRef] },
    ];
    if (items.some(item => !Array.isArray(item.sourceRefs) || !item.sourceRefs.length)) {
      throw new BankApiError(502, 'cashflow_source_missing', 'Cashflow source reference is missing');
    }
    return deepFreeze({
      salary: money(salaryMinor, currency),
      availableBalance: money(availableMinor, currency),
      totalObligations: money(obligationMinor, currency),
      mandatoryObligations: money(obligationMinor, currency),
      plannedRepayment: money(plannedRepayment, currency),
      uncoveredObligations: money(uncoveredObligations, currency),
      livingBudget: money(living, currency),
      emergencyReserve: money(reserve, currency),
      allocatable: money(remaining, currency),
      obligationItems: normalizeObligationItems(obligations.items || [], obligationMinor),
      supplementaryAccounts: normalizeSupplementaryAccounts(supplementaryRecords, currency),
      items,
      generatedAt: requireTime(now()),
    });
  }

  async function getSpendingAnalysis(customerId, query) {
    requireId(customerId, 'customerId');
    if (!query || Object.keys(query).length !== 1 || !/^\d{4}-(0[1-9]|1[0-2])$/.test(query.period || '')) {
      throw new BankApiError(422, 'cashflow_query_invalid', 'Spending period is invalid');
    }
    const transactions = await transactionProvider(customerId);
    if (!Array.isArray(transactions) || transactions.length > 10000) {
      throw new BankApiError(502, 'cashflow_provider_invalid', 'Transaction provider response is invalid');
    }
    const previousPeriod = previousMonth(query.period);
    const current = summarizePeriod(transactions, query.period);
    const previous = summarizePeriod(transactions, previousPeriod);
    const delta = current.netExpenseMinor - previous.netExpenseMinor;
    const sourceRefs = current.expenseIds.slice(0, 64);
    // findings 唯一消费方是用户可见的归因卡片（user_result.py 投影），文本必须
    // 在源头即为展示语言；金额用 display 形式，类别名走共享展示名映射。
    const findings = [{
      kind: 'fact', code: 'NET_EXPENSE_TOTAL',
      text: `${query.period} 净支出 ${formatMoney(current.netExpenseMinor)}`,
      sourceRefs,
    }];
    if (delta !== 0) findings.push({
      kind: 'rule_inference', code: delta > 0 ? 'SPENDING_INCREASED' : 'SPENDING_DECREASED',
      text: `净支出较 ${previousPeriod} ${delta > 0 ? '增加' : '减少'} ${formatMoney(Math.abs(delta))}`,
      sourceRefs: [...sourceRefs, ...previous.expenseIds.slice(0, 64)].slice(0, 64),
    });
    // 类别归因（通用方法）：两期分类净支出对齐后取变化最大的前 8 类，
    // 并为主要驱动类别补充带交易溯源的规则推断（无本期凭证的类别不发 finding）。
    const categories = buildCategoryAttribution(current.categories, previous.categories);
    categories.filter(item => item.deltaMinor !== 0).slice(0, 2).forEach(item => {
      const source = current.categories.find(row => row.category === item.category);
      const evidence = source && source.expenseIds.length ? source.expenseIds.slice(0, 64) : null;
      if (!evidence) return;
      const categoryLabel = SPENDING_CATEGORY_LABELS[item.category] || item.category;
      findings.push({
        kind: 'rule_inference',
        code: item.deltaMinor < 0 ? 'CATEGORY_SPENDING_DECREASE' : 'CATEGORY_SPENDING_INCREASE',
        text: `${categoryLabel}：净支出较 ${previousPeriod} ${item.deltaMinor > 0 ? '增加' : '减少'} ${formatMoney(Math.abs(item.deltaMinor))}`,
        sourceRefs: evidence,
      });
    });
    return deepFreeze({
      period: query.period,
      comparisonPeriod: previousPeriod,
      current: { netExpenseMinor: current.netExpenseMinor },
      previous: { netExpenseMinor: previous.netExpenseMinor },
      excluded: {
        internalTransferMinor: current.internalTransferMinor,
        refundMinor: current.refundMinor,
      },
      categories,
      findings,
      generatedAt: requireTime(now()),
    });
  }

  return Object.freeze({ getSalaryPlan, getSpendingAnalysis });
}

function normalizeSupplementaryAccounts(values, currency) {
  if (!Array.isArray(values) || values.length > 8) invalid('supplementaryAccounts');
  return values.map(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) invalid('supplementaryAccount');
    requireId(item.accountId, 'supplementaryAccount.accountId');
    if (typeof item.accountName !== 'string' || !item.accountName || item.accountName.length > 40
      || /[<>]/.test(item.accountName)) invalid('supplementaryAccount');
    const availableMinor = requireNonNegative(item.availableMinor, 'supplementaryAccount.availableMinor');
    if ((item.currency || currency) !== 'CNY') invalid('supplementaryAccount.currency');
    return {
      accountId: item.accountId,
      accountName: item.accountName,
      availableMinor,
      currency: item.currency || currency,
    };
  });
}

function normalizeObligationItems(values, expectedTotal) {
  if (!Array.isArray(values) || values.length > 32) invalid('obligationItems');
  const items = values.map(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) invalid('obligationItem');
    requireId(item.id, 'obligationItem.id');
    if (!['credit_card', 'loan', 'utility'].includes(item.type)
      || typeof item.displayName !== 'string' || !item.displayName || item.displayName.length > 120
      || /[<>]/.test(item.displayName)) invalid('obligationItem');
    const amountMinor = requireMinor(item.amountMinor, 'obligationItem.amountMinor');
    const sourceRefs = Array.isArray(item.sourceRefs) && item.sourceRefs.length
      ? item.sourceRefs.slice(0, 8)
      : [item.id];
    sourceRefs.forEach(value => requireId(value, 'obligationItem.sourceRef'));
    const dueAt = item.dueAt == null ? null : String(item.dueAt);
    if (dueAt !== null && !Number.isFinite(Date.parse(dueAt))) invalid('obligationItem.dueAt');
    if (!['available', 'unavailable'].includes(item.executionStatus)) {
      invalid('obligationItem.executionStatus');
    }
    return {
      id: item.id, type: item.type, displayName: item.displayName,
      amountMinor, dueAt, executionStatus: item.executionStatus, sourceRefs,
    };
  });
  if (items.length && items.reduce((sum, item) => sum + item.amountMinor, 0) !== expectedTotal) {
    invalid('obligationItems.total');
  }
  return items;
}

function summarizePeriod(transactions, period) {
  const rows = transactions.filter(item => typeof item.occurredAt === 'string' && item.occurredAt.slice(0, 7) === period);
  let expense = 0; let refund = 0; let internal = 0;
  const expenseIds = [];
  const categoryNet = new Map();
  const categoryExpenseIds = new Map();
  rows.forEach(item => {
    requireId(item.id, 'transaction.id');
    requireNonNegative(item.amountMinor, 'transaction.amountMinor');
    if (item.kind === 'internal_transfer') internal += item.amountMinor;
    else if (item.kind === 'refund' || item.kind === 'reversal') {
      refund += item.amountMinor;
      const category = safeCategory(item.category);
      categoryNet.set(category, (categoryNet.get(category) || 0) - item.amountMinor);
    } else if (item.kind === 'expense') {
      expense += item.amountMinor; expenseIds.push(item.id);
      const category = safeCategory(item.category);
      categoryNet.set(category, (categoryNet.get(category) || 0) + item.amountMinor);
      if (!categoryExpenseIds.has(category)) categoryExpenseIds.set(category, []);
      categoryExpenseIds.get(category).push(item.id);
    }
  });
  const categories = Array.from(categoryNet.entries())
    .map(([category, netExpenseMinor]) => ({ category, netExpenseMinor, expenseIds: categoryExpenseIds.get(category) || [] }))
    .sort((a, b) => b.netExpenseMinor - a.netExpenseMinor || a.category.localeCompare(b.category));
  return {
    netExpenseMinor: Math.max(0, expense - refund), refundMinor: refund, internalTransferMinor: internal,
    expenseIds, categories,
  };
}

function buildCategoryAttribution(currentCategories, previousCategories) {
  const entries = new Map();
  currentCategories.forEach(({ category, netExpenseMinor }) => {
    entries.set(category, { category, currentMinor: netExpenseMinor, previousMinor: 0 });
  });
  previousCategories.forEach(({ category, netExpenseMinor }) => {
    const entry = entries.get(category) || { category, currentMinor: 0, previousMinor: 0 };
    entry.previousMinor = netExpenseMinor;
    entries.set(category, entry);
  });
  return Array.from(entries.values())
    .map(entry => ({ ...entry, deltaMinor: entry.currentMinor - entry.previousMinor }))
    .sort((a, b) => Math.abs(b.deltaMinor) - Math.abs(a.deltaMinor) || a.category.localeCompare(b.category))
    .slice(0, 8);
}

function safeCategory(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_]{1,40}$/.test(value) ? value : 'OTHER';
}
function previousMonth(period) {
  const [year, month] = period.split('-').map(Number);
  const date = new Date(Date.UTC(year, month - 2, 1));
  return date.toISOString().slice(0, 7);
}
function money(amountMinor, currency) { return { amountMinor, currency }; }
function formatMoney(amountMinor) {
  if (!Number.isSafeInteger(amountMinor)) throw new TypeError('money amount is invalid');
  return `¥${(amountMinor / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function requireMinor(value, field) { if (!Number.isSafeInteger(value) || value <= 0) invalid(field); return value; }
function requireNonNegative(value, field) { if (!Number.isSafeInteger(value) || value < 0) invalid(field); return value; }
function requireId(value, field) { if (typeof value !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(value)) invalid(field); }
function requireTime(value) { if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) invalid('time'); return new Date(value).toISOString(); }
function invalid(field) { throw new BankApiError(422, 'cashflow_input_invalid', `${field} is invalid`); }
function deepFreeze(value) { if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value; Object.values(value).forEach(deepFreeze); return Object.freeze(value); }

module.exports = { createCashflowService };
