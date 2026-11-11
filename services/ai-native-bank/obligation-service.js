'use strict';

const crypto = require('node:crypto');
const { BankApiError } = require('./api-contract');

function createObligationService(options = {}) {
  const providers = [
    ['credit_card', options.cardProvider], ['loan', options.loanProvider], ['utility', options.utilityProvider],
  ];
  const accountProvider = options.accountProvider;
  const executors = options.executors || {};
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  if (providers.some(item => typeof item[1] !== 'function') || typeof accountProvider !== 'function') {
    throw new TypeError('obligation service dependencies are invalid');
  }

  async function list(customerId) {
    requireId(customerId, 'customerId');
    const [settled, account] = await Promise.all([Promise.all(providers.map(async ([type, provider]) => {
      try { return { type, values: await provider(customerId) }; }
      catch (_) { return { type, values: [] }; }
    })), accountProvider(customerId)]);
    if (!account || typeof account !== 'object') invalid('account');
    requireId(account.id, 'account.id');
    if (!Number.isSafeInteger(account.availableMinor) || account.availableMinor < 0) invalid('account.availableMinor');
    const items = [];
    settled.forEach(group => {
      if (!Array.isArray(group.values) || group.values.length > 128) invalid('provider');
      group.values.forEach(raw => {
        requireId(raw.id, 'item.id');
        if (!safeDisplayName(raw.displayName)
          || !Number.isSafeInteger(raw.amountMinor) || raw.amountMinor <= 0
          || !['available', 'unavailable'].includes(raw.executionStatus)) invalid('item');
        items.push({
          id: raw.id, type: group.type,
          displayName: raw.displayName,
          amount: { amountMinor: raw.amountMinor, currency: 'CNY' },
          dueAt: raw.dueAt || null,
          executionStatus: raw.executionStatus,
          sourceRefs: [raw.id],
        });
      });
    });
    return deepFreeze({
      accountId: account.id,
      availableBalance: { amountMinor: account.availableMinor, currency: 'CNY' },
      items,
      total: { amountMinor: items.reduce((sum, item) => sum + item.amount.amountMinor, 0), currency: 'CNY' },
      generatedAt: requireTime(now()),
    });
  }

  async function previewPlan(customerId, selection) {
    requireId(customerId, 'customerId');
    if (!selection || Object.keys(selection).length !== 2
      || !Array.isArray(selection.itemIds) || !selection.itemIds.length || selection.itemIds.length > 32) invalid('selection');
    requireId(selection.accountId, 'accountId');
    selection.itemIds.forEach(id => requireId(id, 'itemId'));
    if (new Set(selection.itemIds).size !== selection.itemIds.length) invalid('itemIds');
    const [available, account] = await Promise.all([list(customerId), accountProvider(customerId)]);
    if (!account || account.id !== selection.accountId || !Number.isSafeInteger(account.availableMinor)) invalid('account');
    const byId = new Map(available.items.map(item => [item.id, item]));
    const items = selection.itemIds.map(id => byId.get(id));
    if (items.some(item => !item || item.executionStatus !== 'available')) {
      throw new BankApiError(409, 'obligation_unavailable', 'An obligation is no longer available');
    }
    const totalMinor = items.reduce((sum, item) => sum + item.amount.amountMinor, 0);
    if (totalMinor > account.availableMinor) throw new BankApiError(409, 'insufficient_funds', 'Account balance is insufficient');
    const canonical = { customerId, accountId: selection.accountId, itemIds: selection.itemIds, totalMinor };
    return deepFreeze({
      accountId: selection.accountId,
      itemIds: selection.itemIds.slice(),
      items,
      total: { amountMinor: totalMinor, currency: 'CNY' },
      planDigest: digest(canonical),
      generatedAt: requireTime(now()),
    });
  }

  async function executePlan(customerId, plan, grant) {
    requireId(customerId, 'customerId');
    validateGrant(customerId, plan, grant);
    const results = [];
    for (const item of plan.items) {
      const execute = executors[item.type];
      if (typeof execute !== 'function') {
        results.push({ itemId: item.id, type: item.type, status: 'FAILED', receiptId: null, errorCode: 'executor_unavailable' });
        continue;
      }
      try {
        const receipt = await execute(item, { customerId, accountId: plan.accountId, grant });
        if (!receipt || typeof receipt.receiptId !== 'string') throw Object.assign(new Error('invalid receipt'), { retryable: false });
        results.push({ itemId: item.id, type: item.type, status: 'SUCCEEDED', receiptId: receipt.receiptId, errorCode: null });
      } catch (error) {
        results.push({
          itemId: item.id, type: item.type,
          status: error && error.retryable ? 'FAILED_RETRYABLE' : 'FAILED',
          receiptId: null, errorCode: error && error.retryable ? 'provider_temporarily_unavailable' : 'execution_failed',
        });
      }
    }
    const succeeded = results.filter(item => item.status === 'SUCCEEDED').length;
    return deepFreeze({
      status: succeeded === results.length ? 'COMPLETED' : succeeded ? 'PARTIALLY_COMPLETED' : 'FAILED',
      items: results,
      completedAt: requireTime(now()),
    });
  }

  return Object.freeze({ list, previewPlan, executePlan });
}

function validateGrant(customerId, plan, grant) {
  if (!plan || !grant || grant.customerId !== customerId || grant.accountId !== plan.accountId
    || grant.totalMinor !== plan.total.amountMinor || grant.planDigest !== plan.planDigest
    || JSON.stringify(grant.itemIds) !== JSON.stringify(plan.itemIds)) {
    throw new BankApiError(403, 'obligation_grant_invalid', 'Confirmation grant does not match the plan');
  }
  const canonical = { customerId, accountId: plan.accountId, itemIds: plan.itemIds, totalMinor: plan.total.amountMinor };
  if (digest(canonical) !== plan.planDigest) throw new BankApiError(409, 'obligation_plan_changed', 'Obligation plan has changed');
}
function digest(value) { return crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex'); }
function requireId(value, field) { if (typeof value !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(value)) invalid(field); }
function safeDisplayName(value) { return typeof value === 'string' && value.length > 0 && value.length <= 120 && !/[<>]/.test(value); }
function requireTime(value) { if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) invalid('time'); return new Date(value).toISOString(); }
function invalid(field) { throw new BankApiError(422, 'obligation_input_invalid', `${field} is invalid`); }
function deepFreeze(value) { if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value; Object.values(value).forEach(deepFreeze); return Object.freeze(value); }

module.exports = { createObligationService };
