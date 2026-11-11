'use strict';

const crypto = require('node:crypto');
const { BankApiError, requireString, requirePositiveMinor } = require('./api-contract');
const { moneyValue, validatePaging } = require('./bank-service');

const MIN_REPAYMENT_RATIO_BP = 1000; // 最低还款额 = 账单 10%

function createCreditCardService(options) {
  const repository = options.repository;
  const now = options.now || (() => new Date().toISOString());

  async function requireCustomer(customerId) {
    const idValue = requireString(customerId, 'customerId', { max: 64 });
    const customer = await repository.getCustomer(idValue);
    if (!customer || customer.status !== 'ACTIVE') throw notFound();
    return customer;
  }

  async function requireOwnedCard(customerId, cardId) {
    await requireCustomer(customerId);
    const card = await repository.getCreditCard(customerId, requireString(cardId, 'cardId', { max: 64 }));
    if (!card) throw notFound();
    return card;
  }

  async function listCards(customerId) {
    await requireCustomer(customerId);
    return (await repository.listCreditCards(customerId)).map(publicCard);
  }

  async function getCard(customerId, cardId) {
    return publicCard(await requireOwnedCard(customerId, cardId));
  }

  async function listTransactions(customerId, cardId, paging) {
    const card = await requireOwnedCard(customerId, cardId);
    const options = validatePaging(paging || {});
    const page = await repository.listCreditCardTransactions(card.id, options);
    return {
      data: page.data.map(item => ({
        id: item.id,
        direction: item.direction,
        amount: moneyValue(item.amountMinor, item.currency),
        description: item.description,
        status: item.status,
        occurredAt: item.occurredAt,
      })),
      meta: { nextCursor: page.nextCursor, hasNext: Boolean(page.nextCursor), limit: options.limit },
    };
  }

  async function getSettings(customerId, cardId) {
    const card = await requireOwnedCard(customerId, cardId);
    const settings = await repository.getCreditCardSettings(card.id);
    if (!settings) throw notFound();
    return publicSettings(settings);
  }

  async function activate(actor, cardId, idempotencyKey) {
    requireUserActor(actor);
    await requireOwnedCard(actor.customerId, cardId);
    return command(actor, 'credit-card.activation.create', idempotencyKey, { cardId }, async (tx, timestamp) => {
      const card = requireOwnedTxCard(tx, actor.customerId, cardId);
      if (card.status !== 'INACTIVE') throw stateConflict();
      tx.updateCreditCardStatus(card.id, 'INACTIVE', 'ACTIVE');
      return { id: id('card_activation'), cardId: card.id, status: 'ACTIVE', activatedAt: timestamp };
    });
  }

  async function reportLoss(actor, cardId, input, idempotencyKey) {
    requireUserActor(actor);
    await requireOwnedCard(actor.customerId, cardId);
    const body = exactObject(input, ['reason']);
    const reason = requireString(body.reason, 'reason', { max: 32 });
    if (!['CARD_MISSING', 'STOLEN', 'DAMAGED'].includes(reason)) validation('reason', 'unsupported');
    return command(actor, 'credit-card.loss-report.create', idempotencyKey, { cardId, reason }, async (tx, timestamp) => {
      const card = requireOwnedTxCard(tx, actor.customerId, cardId);
      if (card.status !== 'ACTIVE') throw stateConflict();
      tx.updateCreditCardStatus(card.id, 'ACTIVE', 'LOST');
      return { id: id('card_loss'), cardId: card.id, status: 'LOST', reason, reportedAt: timestamp };
    });
  }

  async function requestLimitChange(actor, cardId, input, idempotencyKey) {
    requireUserActor(actor);
    await requireOwnedCard(actor.customerId, cardId);
    const body = exactObject(input, ['requestedLimitMinor']);
    const requestedLimitMinor = boundedInteger(body.requestedLimitMinor, 'requestedLimitMinor', 100000, 50000000);
    return command(actor, 'credit-card.limit-change.create', idempotencyKey, { cardId, requestedLimitMinor }, async (tx, timestamp) => {
      const card = requireOwnedTxCard(tx, actor.customerId, cardId);
      if (card.status !== 'ACTIVE') throw stateConflict();
      const item = tx.insertCreditCardLimitChange({ id: id('card_limit'), cardId: card.id, customerId: actor.customerId, requestedLimitMinor, status: 'PENDING_REVIEW', createdAt: timestamp });
      return { id: item.id, cardId: item.cardId, requestedLimit: moneyValue(item.requestedLimitMinor, 'CNY'), status: item.status, submittedAt: item.createdAt };
    });
  }

  async function updateSecuritySettings(actor, cardId, input, idempotencyKey) {
    requireUserActor(actor);
    await requireOwnedCard(actor.customerId, cardId);
    const keys = ['onlineTransactionsEnabled', 'contactlessEnabled', 'overseasEnabled', 'singleTransactionLimitMinor', 'dailyLimitMinor'];
    const body = exactObject(input, keys, true);
    keys.slice(0, 3).forEach(key => { if (typeof body[key] !== 'boolean') validation(key, 'boolean_required'); });
    const singleTransactionLimitMinor = boundedInteger(body.singleTransactionLimitMinor, 'singleTransactionLimitMinor', 10000, 50000000);
    const dailyLimitMinor = boundedInteger(body.dailyLimitMinor, 'dailyLimitMinor', 10000, 50000000);
    if (singleTransactionLimitMinor > dailyLimitMinor) validation('singleTransactionLimitMinor', 'exceeds_daily_limit');
    const normalized = {
      onlineTransactionsEnabled: body.onlineTransactionsEnabled,
      contactlessEnabled: body.contactlessEnabled,
      overseasEnabled: body.overseasEnabled,
      singleTransactionLimitMinor,
      dailyLimitMinor,
    };
    return command(actor, 'credit-card.security-settings.update', idempotencyKey, Object.assign({ cardId }, normalized), async (tx, timestamp) => {
      const card = requireOwnedTxCard(tx, actor.customerId, cardId);
      if (card.status !== 'ACTIVE') throw stateConflict();
      const settings = tx.updateCreditCardSettings(card.id, normalized);
      return { id: id('card_settings'), cardId: card.id, status: 'UPDATED', settings: publicSettings(settings), updatedAt: timestamp };
    });
  }

  async function getBill(customerId, cardId) {
    const card = await requireOwnedCard(customerId, cardId);
    const outstandingMinor = Math.max(0, card.totalLimitMinor - card.availableLimitMinor);
    const minimumMinor = Math.ceil(outstandingMinor * MIN_REPAYMENT_RATIO_BP / 10000);
    return {
      cardId: card.id,
      productName: card.productName,
      maskedNo: card.maskedNo,
      status: card.status,
      totalLimit: moneyValue(card.totalLimitMinor, 'CNY'),
      availableLimit: moneyValue(card.availableLimitMinor, 'CNY'),
      outstanding: moneyValue(outstandingMinor, 'CNY'),
      minimumPayment: moneyValue(minimumMinor, 'CNY'),
      statementDay: card.statementDay,
      dueDay: card.dueDay,
      billStatus: outstandingMinor > 0 ? 'PAYMENT_DUE' : 'SETTLED',
      note: outstandingMinor > 0
        ? '本期未还部分将在到期日（每月 ' + card.dueDay + ' 日）前计收约定利息，建议全额还款。'
        : '当前账单已结清。',
      generatedAt: now(),
    };
  }

  async function repay(actor, cardId, input, idempotencyKey) {
    requireUserActor(actor);
    await requireOwnedCard(actor.customerId, cardId);
    const body = exactObject(input, ['fromAccountId', 'amountMinor']);
    const fromAccountId = requireString(body.fromAccountId, 'fromAccountId', { max: 64 });
    const amountMinor = requirePositiveMinor(body.amountMinor, 'amountMinor');
    return command(actor, 'credit-card.repayment.create', idempotencyKey, { cardId, fromAccountId, amountMinor }, async (tx, timestamp) => {
      const card = requireOwnedTxCard(tx, actor.customerId, cardId);
      if (card.status !== 'ACTIVE') throw stateConflict();
      const outstandingMinor = card.totalLimitMinor - card.availableLimitMinor;
      if (outstandingMinor <= 0) throw new BankApiError(422, 'no_outstanding', '当前账单已结清，无需还款');
      if (amountMinor > outstandingMinor) validation('amountMinor', 'exceeds_outstanding');
      const account = tx.queryOne("SELECT * FROM bank_accounts WHERE id = ? AND customer_id = ? AND visibility = 'CUSTOMER'", [fromAccountId, actor.customerId]);
      if (!account) throw notFound();
      if (account.status !== 'ACTIVE') throw new BankApiError(422, 'account_unavailable', '付款账户当前不可用');
      const balance = tx.getBalance(account.id);
      if (!balance) throw notFound();
      if (balance.availableMinor < amountMinor) throw new BankApiError(422, 'insufficient_funds', '付款账户可用余额不足');
      const repaymentId = id('card_repay');
      const newAvailable = balance.availableMinor - amountMinor;
      tx.updateBalance(account.id, newAvailable, balance.ledgerMinor - amountMinor, balance.version);
      tx.insertLedgerTransaction({ id: 'lg_' + repaymentId, businessType: 'CREDIT_CARD_REPAYMENT', businessId: repaymentId, currency: account.currency, postedAt: timestamp });
      tx.insertLedgerEntry({ id: 'le_' + repaymentId + '_a', ledgerTransactionId: 'lg_' + repaymentId, accountId: account.id, direction: 'DEBIT', amountMinor, description: '信用卡还款-' + card.productName });
      tx.insertLedgerEntry({ id: 'le_' + repaymentId + '_b', ledgerTransactionId: 'lg_' + repaymentId, accountId: 'acct_external_clearing', direction: 'CREDIT', amountMinor, description: card.productName + '还款清算' });
      tx.run('INSERT INTO bank_account_transactions (id,account_id,customer_id,direction,amount_minor,currency,business_type,category,counterparty,description,channel,balance_after_minor,occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', [
        'atxn_' + repaymentId, account.id, actor.customerId, 'DEBIT', amountMinor, account.currency,
        'CREDIT_CARD_REPAYMENT', 'CREDIT_CARD', '中国建设银行信用卡', '信用卡还款-' + card.productName, 'MOBILE_APP', newAvailable, timestamp,
      ]);
      tx.run('UPDATE bank_credit_cards SET available_limit_minor = MIN(total_limit_minor, available_limit_minor + ?), updated_at = ? WHERE id = ?', [amountMinor, timestamp, card.id]);
      tx.run('INSERT INTO bank_credit_card_transactions (id,card_id,direction,amount_minor,currency,description,status,occurred_at) VALUES (?,?,?,?,?,?,?,?)', [
        'ctxn_' + repaymentId, card.id, 'CREDIT', amountMinor, 'CNY', '主动还款（借记卡即时还款）', 'POSTED', timestamp,
      ]);
      return {
        id: repaymentId,
        cardId: card.id,
        status: 'SUCCEEDED',
        paid: moneyValue(amountMinor, account.currency),
        remainingOutstanding: moneyValue(Math.max(0, outstandingMinor - amountMinor), 'CNY'),
        fromAccount: { id: account.id, name: account.name, maskedNo: account.maskedNo },
        paidAt: timestamp,
      };
    });
  }

  async function command(actor, scope, idempotencyKey, request, work) {
    const requestHash = hash(request);
    return repository.withTransaction(async tx => {
      const replay = idempotentReplay(tx, scope, idempotencyKey, requestHash);
      if (replay) return replay;
      const timestamp = now();
      const response = await work(tx, timestamp);
      tx.saveIdempotency({ scope, key: idempotencyKey, requestHash, response, status: 'SUCCEEDED', createdAt: timestamp });
      tx.insertAudit({ id: id('audit'), traceId: actor.traceId || id('trace'), actorType: actor.type, actorId: actor.id, action: 'bank.' + scope, resourceType: 'credit-card', resourceId: request.cardId, outcome: 'SUCCEEDED', details: { resultStatus: response.status }, createdAt: timestamp });
      return response;
    });
  }

  return { listCards, getCard, listTransactions, getSettings, getBill, repay, activate, reportLoss, requestLimitChange, updateSecuritySettings };
}

function publicCard(card) {
  return {
    id: card.id,
    maskedNo: card.maskedNo,
    productName: card.productName,
    network: card.network,
    cardType: card.cardType,
    status: card.status,
    totalLimit: moneyValue(card.totalLimitMinor, 'CNY'),
    availableLimit: moneyValue(card.availableLimitMinor, 'CNY'),
    cashLimit: moneyValue(card.cashLimitMinor, 'CNY'),
    statementDay: card.statementDay,
    dueDay: card.dueDay,
    updatedAt: card.updatedAt,
  };
}

function publicSettings(settings) {
  return {
    cardId: settings.cardId,
    onlineTransactionsEnabled: settings.onlineTransactionsEnabled,
    contactlessEnabled: settings.contactlessEnabled,
    overseasEnabled: settings.overseasEnabled,
    singleTransactionLimit: moneyValue(settings.singleTransactionLimitMinor, 'CNY'),
    dailyLimit: moneyValue(settings.dailyLimitMinor, 'CNY'),
    updatedAt: settings.updatedAt,
  };
}

function requireOwnedTxCard(tx, customerId, cardId) {
  const card = tx.getCreditCard(cardId);
  if (!card || card.customerId !== customerId) throw notFound();
  return card;
}

function requireUserActor(actor) {
  if (!actor || actor.type !== 'user' || !actor.customerId || actor.id !== actor.customerId) {
    throw new BankApiError(403, 'forbidden', '卡片变更必须来自明确的用户操作');
  }
}

function exactObject(input, allowedKeys, requireAll) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) validation('body', 'object_required');
  const unknown = Object.keys(input).filter(key => !allowedKeys.includes(key));
  if (unknown.length) validation(unknown[0], 'unknown_field');
  if (requireAll) {
    const missing = allowedKeys.find(key => !Object.hasOwn(input, key));
    if (missing) validation(missing, 'required');
  }
  return input;
}

function boundedInteger(value, field, minimum, maximum) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) validation(field, 'out_of_range');
  return value;
}

function validation(field, code) {
  throw new BankApiError(422, 'validation_error', '信用卡请求参数不合法', [{ field, code }]);
}

function idempotentReplay(tx, scope, key, requestHash) {
  if (!key) throw new BankApiError(400, 'missing_idempotency_key', '缺少幂等键');
  const existing = tx.getIdempotency(scope, key);
  if (!existing) return null;
  if (existing.requestHash !== requestHash) throw new BankApiError(409, 'idempotency_conflict', '同一幂等键对应了不同请求');
  return existing.response;
}

function id(prefix) { return prefix + '_' + crypto.randomUUID(); }
function hash(value) { return crypto.createHash('sha256').update(canonical(value)).digest('hex'); }
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
}
function stateConflict() { return new BankApiError(409, 'state_conflict', '信用卡状态不允许当前操作'); }
function notFound() { return new BankApiError(404, 'not_found', '资源不存在'); }

module.exports = { createCreditCardService };
