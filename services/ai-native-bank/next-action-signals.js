'use strict';

const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_MANIFEST_PATH = path.resolve(
  __dirname, '../../data/ai-native-bank/registries/signal-providers.v1.json',
);
const ALLOWED_SURFACES = new Set(['home', 'agent', 'channel']);
const SALARY_EDGE_WINDOW_MS = 72 * 60 * 60 * 1000;

class NextActionSignalInputError extends Error {
  constructor(message) {
    super('next_action_signal_input_invalid: ' + message);
    this.name = 'NextActionSignalInputError';
    this.code = 'next_action_signal_input_invalid';
  }
}

function readDefaultDefinitions() {
  const manifest = JSON.parse(fs.readFileSync(DEFAULT_MANIFEST_PATH, 'utf8'));
  return manifest.signals;
}

function validateInput(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new NextActionSignalInputError('input must be an object');
  const allowed = new Set(['customerId', 'surface', 'channel', 'personalizationEnabled']);
  const unknown = Object.keys(input).filter(key => !allowed.has(key));
  if (unknown.length) throw new NextActionSignalInputError('unknown fields: ' + unknown.join(','));
  if (typeof input.customerId !== 'string' || !/^[A-Za-z0-9_.:-]{1,96}$/.test(input.customerId)) {
    throw new NextActionSignalInputError('customerId is invalid');
  }
  if (!ALLOWED_SURFACES.has(input.surface)) throw new NextActionSignalInputError('surface is invalid');
  if (input.channel != null && (typeof input.channel !== 'string' || !/^[a-z][a-z0-9-]{0,31}$/.test(input.channel))) {
    throw new NextActionSignalInputError('channel is invalid');
  }
  if (input.surface === 'channel' && input.channel == null) throw new NextActionSignalInputError('channel is required');
  if (input.surface !== 'channel' && input.channel != null) throw new NextActionSignalInputError('channel is not allowed');
  if (typeof input.personalizationEnabled !== 'boolean') throw new NextActionSignalInputError('personalizationEnabled is invalid');
}

function validateDefinitions(definitions) {
  if (!Array.isArray(definitions) || !definitions.length || definitions.length > 128) {
    throw new TypeError('signalDefinitions must be a bounded non-empty array');
  }
  const byId = new Map();
  for (const item of definitions) {
    if (!item || typeof item !== 'object' || Array.isArray(item)
      || typeof item.id !== 'string' || typeof item.providerId !== 'string'
      || !['boolean', 'integer', 'number', 'string', 'string_array'].includes(item.valueType)
      || !['financial', 'context', 'preference', 'workflow'].includes(item.classification)
      || !Number.isInteger(item.freshnessSeconds) || item.freshnessSeconds < 1) {
      throw new TypeError('signalDefinitions contains an invalid definition');
    }
    if (byId.has(item.id)) throw new TypeError('signalDefinitions contains duplicate ids');
    byId.set(item.id, Object.freeze({ ...item }));
  }
  return byId;
}

function validValue(type, value) {
  if (type === 'boolean') return typeof value === 'boolean';
  if (type === 'integer') return Number.isSafeInteger(value);
  if (type === 'number') return typeof value === 'number' && Number.isFinite(value);
  if (type === 'string') return typeof value === 'string' && value.length <= 160 && !/[<>]/.test(value);
  return Array.isArray(value) && value.length <= 16
    && value.every(item => typeof item === 'string' && item.length <= 160 && !/[<>]/.test(item));
}

function projectProviderValues(id, values, definitions, input, observedAt) {
  if (!values || typeof values !== 'object' || Array.isArray(values)) throw new TypeError('invalid provider result');
  const entries = Object.entries(values);
  if (entries.length > 32) throw new TypeError('provider result is too large');
  const projected = {};
  for (const [signalId, value] of entries) {
    const definition = definitions.get(signalId);
    if (!definition || definition.providerId !== id || !validValue(definition.valueType, value)) {
      throw new TypeError('provider result violates signal contract');
    }
    if (!input.personalizationEnabled && definition.classification === 'preference') continue;
    projected[signalId] = {
      value: clone(value),
      providerId: id,
      classification: definition.classification,
      observedAt,
      expiresAt: new Date(Date.parse(observedAt) + definition.freshnessSeconds * 1000).toISOString(),
    };
  }
  return projected;
}

function createNextActionSignalService(options = {}) {
  const definitions = validateDefinitions(options.signalDefinitions || readDefaultDefinitions());
  const providers = Object.freeze({ ...(options.providers || {}) });
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  return Object.freeze({
    collect: async input => {
      validateInput(input);
      const observedAt = now();
      if (Number.isNaN(Date.parse(observedAt))) throw new TypeError('now must return an ISO date');
      const settled = await Promise.all(Object.entries(providers).map(async ([id, provider]) => {
        if (typeof provider !== 'function') return { id, unavailable: true };
        try {
          const values = await provider(Object.freeze({ ...input }));
          return { id, values: projectProviderValues(id, values, definitions, input, observedAt) };
        } catch (_) {
          return { id, unavailable: true };
        }
      }));
      const signals = {};
      const unavailableProviderIds = [];
      for (const item of settled) {
        if (item.unavailable) unavailableProviderIds.push(item.id);
        else Object.assign(signals, item.values);
      }
      unavailableProviderIds.sort();
      if (unavailableProviderIds.length) {
        // 可观测性：provider 失败会被静默降级，但必须有痕迹，否则声明场景会无声消失
        console.warn('[next-action-signals] providers unavailable, their signals are skipped: '
          + unavailableProviderIds.join(','));
      }
      return deepFreeze({
        schemaVersion: '1.0',
        customerId: input.customerId,
        surface: input.surface,
        channel: input.channel,
        personalizationEnabled: input.personalizationEnabled,
        collectedAt: observedAt,
        signals,
        unavailableProviderIds: unavailableProviderIds.slice(0, 64),
      });
    },
  });
}

function createBuiltInSignalProviders(options = {}) {
  const repository = options.repository || {};
  const wealthService = options.wealthService || {};
  const bankService = options.bankService || {};
  const creditCardService = options.creditCardService || {};
  const loanService = options.loanService || {};
  const experienceEventProvider = typeof options.experienceEventProvider === 'function'
    ? options.experienceEventProvider : async () => [];
  // 事件语义模型：salary_credited 是 edge（有限有效窗），不是"当月布尔"。
  const edgeEventConsumer = typeof options.edgeEventConsumer === 'function'
    ? options.edgeEventConsumer : null;
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();

  const providers = {
    workflow: async input => {
      const workflows = typeof repository.listResumableAgentWorkflowRecordsInternal === 'function'
        ? await repository.listResumableAgentWorkflowRecordsInternal(input.customerId)
        : typeof repository.listResumableAgentWorkflows === 'function'
          ? await repository.listResumableAgentWorkflows(input.customerId, options.intentGraph)
          : [];
      return { 'workflow.resumable': workflows.length > 0, 'workflow.resumable_count': workflows.length };
    },
    salary: async input => {
      const events = await experienceEventProvider(input.customerId);
      const currentTime = now();
      const currentMs = Date.parse(currentTime);
      const salaryEvent = Array.isArray(events) ? events
        .filter(item => item && item.type === 'salary_credited'
          && typeof item.occurredAt === 'string'
          && Number.isFinite(Date.parse(item.occurredAt))
          && Date.parse(item.occurredAt) <= currentMs
          && currentMs - Date.parse(item.occurredAt) <= SALARY_EDGE_WINDOW_MS
          && item.payload && Number.isSafeInteger(item.payload.amountMinor))
        .sort((left, right) => right.occurredAt.localeCompare(left.occurredAt))[0] : null;
      if (salaryEvent) {
        if (edgeEventConsumer) {
          // 消费留痕失败不影响信号本身（stable-ID 已保证卡片幂等）
          await edgeEventConsumer(input.customerId, salaryEvent).catch(() => null);
        }
        return {
          'salary.recently_credited': true,
          'salary.amount_minor': salaryEvent.payload.amountMinor,
        };
      }
      const insights = await requiredCall(wealthService, 'getSalaryInsights', input.customerId);
      const month = Array.isArray(insights.months)
        ? insights.months.find(item => item.month === currentTime.slice(0, 7)) || insights.months[insights.months.length - 1]
        : null;
      const amount = month && month.salary && Number.isSafeInteger(month.salary.amountMinor)
        ? month.salary.amountMinor : 0;
      return { 'salary.recently_credited': amount > 0 && month.month === currentTime.slice(0, 7), 'salary.amount_minor': amount };
    },
    salary_calendar: async input => {
      const events = await experienceEventProvider(input.customerId);
      const currentTime = now();
      const currentMs = Date.parse(currentTime);
      const creditDays = (Array.isArray(events) ? events : [])
        .filter(item => item && item.type === 'salary_credited'
          && typeof item.occurredAt === 'string' && Number.isFinite(Date.parse(item.occurredAt))
          && Date.parse(item.occurredAt) <= currentMs)
        .sort((left, right) => right.occurredAt.localeCompare(left.occurredAt))
        .slice(0, 6)
        .map(item => new Date(item.occurredAt).getUTCDate());
      if (!creditDays.length) {
        return {
          'salary.expected_day': 0,
          'salary.days_until_credit': 2678400,
          'salary.pre_credit_day': false,
        };
      }
      const counts = new Map();
      creditDays.forEach(day => counts.set(day, (counts.get(day) || 0) + 1));
      let expectedDay = 0;
      let bestCount = 0;
      counts.forEach((count, day) => {
        if (count > bestCount || (count === bestCount && day > expectedDay)) {
          expectedDay = day;
          bestCount = count;
        }
      });
      const nowDate = new Date(currentTime);
      let nextCredit = new Date(Date.UTC(
        nowDate.getUTCFullYear(), nowDate.getUTCMonth(), Math.min(expectedDay, daysInMonth(nowDate.getUTCFullYear(), nowDate.getUTCMonth())),
      ));
      if (nextCredit.getTime() <= currentMs) {
        nextCredit = new Date(Date.UTC(
          nowDate.getUTCFullYear(), nowDate.getUTCMonth() + 1, Math.min(expectedDay, daysInMonth(nowDate.getUTCFullYear(), nowDate.getUTCMonth() + 1)),
        ));
      }
      const daysUntil = daysBetween(currentTime, nextCredit.toISOString());
      return {
        'salary.expected_day': expectedDay,
        'salary.days_until_credit': Number.isInteger(daysUntil) && daysUntil >= 0 ? daysUntil : 2678400,
        'salary.pre_credit_day': daysUntil === 1,
      };
    },
    card: async input => {
      const cards = await requiredCall(creditCardService, 'listCards', input.customerId);
      const bills = await Promise.all((cards || []).filter(card => card.status === 'ACTIVE').map(card => (
        requiredCall(creditCardService, 'getBill', input.customerId, card.id).catch(() => null)
      )));
      const active = bills.filter(bill => bill && Number(bill.outstanding && bill.outstanding.amountMinor) > 0);
      const days = active.map(bill => {
        if (!Number.isInteger(bill.dueDay)) return null;
        const nowDate = new Date(now());
        let due = new Date(nowDate.getFullYear(), nowDate.getMonth(), bill.dueDay);
        if (due < nowDate) due = new Date(nowDate.getFullYear(), nowDate.getMonth() + 1, bill.dueDay);
        return daysBetween(now(), due.toISOString());
      }).filter(Number.isInteger);
      return {
        'card.bill_due_count': active.length,
        'card.days_to_due_min': days.length ? Math.min(...days) : 2678400,
      };
    },
    utility: async input => {
      const bills = await requiredCall(repository, 'listUtilityBills', input.customerId);
      return { 'utility.unpaid_count': (bills || []).filter(item => String(item.status).toUpperCase() === 'UNPAID').length };
    },
    loan: async input => {
      const result = await requiredCall(loanService, 'listLoans', input.customerId);
      const nowIso = now();
      const nowDate = new Date(nowIso);
      const days = ((result && result.data) || [])
        .filter(item => item.status === 'ACTIVE' && Number.isInteger(item.monthlyPaymentDay))
        .map(item => {
          let due = new Date(nowDate.getFullYear(), nowDate.getMonth(), item.monthlyPaymentDay);
          if (due < nowDate) due = new Date(nowDate.getFullYear(), nowDate.getMonth() + 1, item.monthlyPaymentDay);
          return daysBetween(nowIso, due.toISOString());
        }).filter(Number.isInteger);
      return { 'loan.due_within_days': days.length ? Math.min(...days) : 2678400 };
    },
    points: async input => {
      const points = await requiredCall(repository, 'getCustomerPoints', input.customerId);
      return { 'points.expiring_amount': Number.isSafeInteger(points && points.expiringAmount) ? points.expiringAmount : 0 };
    },
    life: async input => {
      const forecast = await requiredCall(repository, 'getWeatherForecast');
      const today = (forecast && forecast[0]) || null;
      // 哨兵值：无数据时 condition/temp 取不触发任何天气场景的中性值（fail-safe）
      return {
        'life.weather_condition': today && typeof today.condition === 'string' ? today.condition : 'unknown',
        'life.weather_temp_high': today && Number.isFinite(today.tempHigh) ? today.tempHigh : 999,
      };
    },
    promo: async input => {
      const offers = await requiredCall(repository, 'listActiveOffers');
      const items = Array.isArray(offers) ? offers : [];
      return {
        'promo.expiring_soon_count': items.filter(item => Number.isFinite(item.daysToExpiry) && item.daysToExpiry <= 3).length,
        'promo.dining_offer_count': items.filter(item => item.category === 'dining').length,
      };
    },
    spending: async input => {
      const events = await experienceEventProvider(input.customerId);
      const currentMonth = now().slice(0, 7);
      const anomaly = Array.isArray(events) ? events.find(item => item
        && item.type === 'spending_anomaly'
        && typeof item.occurredAt === 'string' && item.occurredAt.slice(0, 7) === currentMonth
        && item.payload && typeof item.payload.ratio === 'number'
        && Number.isFinite(item.payload.ratio)) : null;
      const insights = await requiredCall(wealthService, 'getSalaryInsights', input.customerId);
      const income = Number(insights.monthlyAvgSalary && insights.monthlyAvgSalary.amountMinor) || 0;
      const expense = Number(insights.monthlyAvgExpense && insights.monthlyAvgExpense.amountMinor) || 0;
      const ratio = anomaly ? anomaly.payload.ratio : income > 0 ? expense / income : 0;
      const period = anomaly && typeof anomaly.payload.period === 'string'
        && /^\d{4}-(0[1-9]|1[0-2])$/.test(anomaly.payload.period)
        ? anomaly.payload.period : null;
      return {
        'spending.anomaly_detected': ratio > 0.8,
        'spending.expense_income_ratio': ratio,
        ...(period ? { 'input.period': period } : {}),
      };
    },
    wealth: async input => {
      const accounts = await requiredCall(bankService, 'listAccounts', input.customerId);
      const balances = typeof repository.getBalance === 'function'
        ? await Promise.all((accounts || []).map(item => repository.getBalance(item.id).catch(() => null))) : [];
      const liquid = balances.reduce((sum, item) => sum + Number(item && item.availableMinor || 0), 0);
      // listHoldings 返回分页对象 {holdingCount,...,holdings:[]}，市值取 totalValue 聚合值
      const book = await requiredCall(wealthService, 'listHoldings', input.customerId);
      const invested = Number(book && book.totalValue && book.totalValue.amountMinor) || 0;
      const currentMs = Date.parse(now());
      const maturingDays = ((book && book.holdings) || [])
        .filter(item => item.status === 'HOLDING' && typeof item.maturesAt === 'string'
          && Number.isFinite(Date.parse(item.maturesAt)))
        .map(item => Math.ceil((Date.parse(item.maturesAt) - currentMs) / 86400000))
        .filter(days => days >= 0);
      return {
        'wealth.idle_cash_ratio': liquid + invested > 0 ? liquid / (liquid + invested) : 0,
        'wealth.holding_maturing_count': maturingDays.filter(days => days <= 30).length,
        'wealth.days_to_maturity_min': maturingDays.length ? Math.min(...maturingDays) : 2678400,
      };
    },
    time: async () => {
      const current = new Date(now());
      const hour = current.getHours();
      const band = hour >= 5 && hour < 11 ? 'morning'
        : hour < 14 ? 'noon' : hour < 18 ? 'afternoon' : hour < 23 ? 'evening' : 'night';
      return { 'time.band': band, 'time.weekend': current.getDay() === 0 || current.getDay() === 6 };
    },
    obligation: async input => {
      const [cards, utilities, loans] = await Promise.all([
        providers.card(input), providers.utility(input), providers.loan(input),
      ]);
      return { 'obligation.due_count': cards['card.bill_due_count']
        + utilities['utility.unpaid_count'] + (loans['loan.due_within_days'] <= 31 ? 1 : 0) };
    },
    page: async input => ({ 'page.channel': input.channel || input.surface }),
    preference: async input => {
      const ids = typeof repository.listFrequentIntentIds === 'function'
        ? await repository.listFrequentIntentIds(input.customerId, 8) : [];
      return { 'preference.frequent_intent_ids': (ids || []).slice(0, 8) };
    },
  };
  return Object.freeze(providers);
}

async function requiredCall(target, method, ...args) {
  if (!target || typeof target[method] !== 'function') throw new TypeError(method + ' is unavailable');
  return target[method](...args);
}

function daysBetween(from, to) {
  const left = Date.parse(from);
  const right = Date.parse(to);
  if (!Number.isFinite(left) || !Number.isFinite(right)) return null;
  return Math.ceil((right - left) / 86400000);
}

function daysInMonth(year, month) {
  return new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
}

function clone(value) { return Array.isArray(value) ? value.slice() : value; }
function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

module.exports = {
  NextActionSignalInputError,
  createBuiltInSignalProviders,
  createNextActionSignalService,
};
