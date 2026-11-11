'use strict';

const crypto = require('crypto');
const { BankApiError, requirePositiveMinor, requireString } = require('./api-contract');
const { moneyValue } = require('./bank-service');

function createTransferService(options) {
  const repository = options.repository;
  const bankService = options.bankService;
  const now = options.now || (() => new Date().toISOString());
  const confirmationSecret = options.confirmationSecret || process.env.BANK_CONFIRMATION_SECRET || crypto.randomBytes(32).toString('hex');

  async function createOrder(actor, input, idempotencyKey) {
    requireCustomerActor(actor);
    const body = validateCreateInput(input);
    const account = await bankService.requireOwnedAccount(actor.customerId, body.fromAccountId);
    const payee = await bankService.requireOwnedPayee(actor.customerId, body.payeeId);
    if (account.status !== 'ACTIVE') throw new BankApiError(422, 'account_unavailable', '付款账户当前不可用');
    if (payee.status !== 'ACTIVE') throw new BankApiError(422, 'payee_unavailable', '收款方当前不可用');
    const balance = await repository.getBalance(account.id);
    const request = Object.assign({}, body, { customerId: actor.customerId });
    const requestHash = hash(request);

    return repository.withTransaction(async tx => {
      const replay = idempotentReplay(tx, 'transfer.order.create', idempotencyKey, requestHash);
      if (replay) return replay;
      const timestamp = now();
      const order = tx.insertTransferOrder({
        id: id('trf'), customerId: actor.customerId, fromAccountId: account.id, payeeId: payee.id,
        amountMinor: body.amountMinor, currency: body.currency, purpose: body.purpose,
        status: 'DRAFT', balanceVersion: balance.version, requestHash, createdAt: timestamp, updatedAt: timestamp,
      });
      const response = publicOrder(order);
      tx.saveIdempotency(record('transfer.order.create', idempotencyKey, requestHash, response, timestamp));
      audit(tx, actor, 'bank.transfer.order.create', 'transfer-order', order.id, 'SUCCEEDED', { amountMinor: order.amountMinor, currency: order.currency }, timestamp);
      return response;
    });
  }

  async function validateOrder(actor, transferOrderId, idempotencyKey) {
    requireCustomerActor(actor);
    const requestHash = hash({ transferOrderId });
    return repository.withTransaction(async tx => {
      const replay = idempotentReplay(tx, 'transfer.order.validate', idempotencyKey, requestHash);
      if (replay) return replay;
      const order = requireOwnedOrder(tx, actor.customerId, transferOrderId);
      if (order.status !== 'DRAFT') throw stateConflict();
      const account = tx.getAccountById(order.fromAccountId);
      const payee = tx.getPayeeById(order.payeeId);
      const balance = tx.getBalance(order.fromAccountId);
      if (!account || account.status !== 'ACTIVE') throw new BankApiError(422, 'account_unavailable', '付款账户当前不可用');
      if (!payee || payee.status !== 'ACTIVE') throw new BankApiError(422, 'payee_unavailable', '收款方当前不可用');
      if (!balance || balance.availableMinor < order.amountMinor) throw new BankApiError(422, 'insufficient_funds', '账户可用余额不足');
      if (order.amountMinor > 5000000) throw new BankApiError(422, 'transfer_limit_exceeded', '转账金额超过单笔限额');
      const timestamp = now();
      const validation = tx.insertValidation({
        id: id('val'), transferOrderId: order.id, result: 'PASS', feeMinor: 0,
        checks: { accountActive: true, payeeActive: true, sufficientFunds: true, withinLimit: true, balanceVersion: balance.version },
        expiresAt: addMinutes(timestamp, 5), createdAt: timestamp,
      });
      tx.updateTransferStatus(order.id, 'DRAFT', 'VALIDATED');
      const response = publicValidation(validation, balance.availableMinor - order.amountMinor);
      tx.saveIdempotency(record('transfer.order.validate', idempotencyKey, requestHash, response, timestamp));
      audit(tx, actor, 'bank.transfer.order.validate', 'transfer-order', order.id, 'SUCCEEDED', { validationId: validation.id }, timestamp);
      return response;
    });
  }

  async function createConfirmationChallenge(actor, transferOrderId, idempotencyKey) {
    requireCustomerActor(actor);
    const requestHash = hash({ transferOrderId });
    return repository.withTransaction(async tx => {
      const replayRecord = tx.getIdempotency('transfer.confirmation.request', idempotencyKey);
      if (replayRecord) {
        verifyReplay(replayRecord, requestHash);
        const replayChallenge = tx.getChallenge(replayRecord.response.challengeId);
        const replayOrder = requireOwnedOrder(tx, actor.customerId, transferOrderId);
        return publicChallenge(tx, replayOrder, replayChallenge, confirmationToken(replayChallenge.id, replayChallenge.summaryHash, confirmationSecret));
      }
      const order = requireOwnedOrder(tx, actor.customerId, transferOrderId);
      if (order.status !== 'VALIDATED') throw stateConflict();
      const validation = tx.getLatestValidation(order.id);
      const timestamp = now();
      if (!validation || validation.result !== 'PASS' || isExpired(validation.expiresAt, timestamp)) {
        throw new BankApiError(409, 'validation_expired', '转账校验已过期，请重新校验');
      }
      const summary = buildSummary(tx, order, validation.feeMinor);
      const summaryHash = hash(summary);
      const challengeId = id('challenge');
      const token = confirmationToken(challengeId, summaryHash, confirmationSecret);
      const challenge = tx.insertChallenge({
        id: challengeId, transferOrderId: order.id, tokenHash: hash(token), summaryHash,
        expiresAt: addMinutes(timestamp, 5), createdAt: timestamp,
      });
      tx.updateTransferStatus(order.id, 'VALIDATED', 'PENDING_CONFIRMATION');
      tx.saveIdempotency(record('transfer.confirmation.request', idempotencyKey, requestHash, { challengeId }, timestamp));
      audit(tx, actor, 'bank.transfer.confirmation.request', 'transfer-order', order.id, 'SUCCEEDED', { challengeId }, timestamp);
      return publicChallenge(tx, order, challenge, token);
    });
  }

  async function authorize(actor, transferOrderId, challengeId, token, idempotencyKey) {
    requireUserActor(actor);
    const requestHash = hash({ transferOrderId, challengeId, tokenHash: hash(String(token || '')) });
    return repository.withTransaction(async tx => {
      const replay = idempotentReplay(tx, 'transfer.authorize', idempotencyKey, requestHash);
      if (replay) return replay;
      const order = requireOwnedOrder(tx, actor.customerId, transferOrderId);
      if (order.status !== 'PENDING_CONFIRMATION') throw stateConflict();
      const challenge = tx.getChallenge(challengeId);
      if (!challenge || challenge.transferOrderId !== order.id) throw new BankApiError(404, 'confirmation_not_found', '确认挑战不存在');
      const timestamp = now();
      if (challenge.status === 'INVALIDATED') throw new BankApiError(409, 'confirmation_invalidated', '确认挑战已失效');
      if (challenge.status === 'USED' || challenge.usedAt) throw new BankApiError(409, 'confirmation_already_used', '确认挑战已使用');
      if (isExpired(challenge.expiresAt, timestamp)) throw new BankApiError(409, 'confirmation_expired', '确认挑战已过期');
      if (!safeEqual(hash(String(token || '')), challenge.tokenHash)) throw new BankApiError(403, 'invalid_confirmation_token', '确认令牌无效');
      const validation = tx.getLatestValidation(order.id);
      const currentSummaryHash = hash(buildSummary(tx, order, validation ? validation.feeMinor : 0));
      if (!safeEqual(currentSummaryHash, challenge.summaryHash)) throw new BankApiError(409, 'confirmation_summary_changed', '转账摘要已发生变化');
      const authorization = tx.insertAuthorization({
        id: id('auth'), transferOrderId: order.id, challengeId: challenge.id, actorId: actor.id,
        summaryHash: challenge.summaryHash, authorizedAt: timestamp,
      });
      tx.markChallengeUsed(challenge.id, timestamp);
      tx.updateTransferStatus(order.id, 'PENDING_CONFIRMATION', 'AUTHORIZED');
      const response = Object.assign({}, authorization, { status: 'AUTHORIZED' });
      tx.saveIdempotency(record('transfer.authorize', idempotencyKey, requestHash, response, timestamp));
      audit(tx, actor, 'bank.transfer.authorize', 'transfer-order', order.id, 'SUCCEEDED', { authorizationId: authorization.id }, timestamp);
      return response;
    });
  }

  async function cancelOrder(actor, transferOrderId, idempotencyKey) {
    requireCustomerActor(actor);
    const requestHash = hash({ transferOrderId });
    return repository.withTransaction(async tx => {
      const replay = idempotentReplay(tx, 'transfer.order.cancel', idempotencyKey, requestHash);
      if (replay) return replay;
      const order = requireOwnedOrder(tx, actor.customerId, transferOrderId);
      if (!['DRAFT', 'VALIDATED', 'PENDING_CONFIRMATION'].includes(order.status)) throw stateConflict();
      const timestamp = now();
      tx.invalidateActiveChallenges(order.id, timestamp, 'order_cancelled');
      tx.updateTransferStatus(order.id, order.status, 'CANCELLED');
      const response = { id: id('cancellation'), transferOrderId: order.id, status: 'CANCELLED', cancelledAt: timestamp };
      tx.saveIdempotency(record('transfer.order.cancel', idempotencyKey, requestHash, response, timestamp));
      audit(tx, actor, 'bank.transfer.order.cancel', 'transfer-order', order.id, 'SUCCEEDED', { cancellationId: response.id }, timestamp);
      return response;
    });
  }

  async function invalidateConfirmation(actor, challengeId, reason, idempotencyKey) {
    requireSystemActor(actor);
    const safeReason = requireString(reason, 'reason', { min: 1, max: 64 });
    const requestHash = hash({ challengeId, reason: safeReason });
    return repository.withTransaction(async tx => {
      const replay = idempotentReplay(tx, 'transfer.confirmation.invalidate', idempotencyKey, requestHash);
      if (replay) return replay;
      const challenge = tx.getChallenge(challengeId);
      if (!challenge) throw new BankApiError(404, 'confirmation_not_found', '确认挑战不存在');
      if (challenge.status !== 'ACTIVE') throw stateConflict();
      const timestamp = now();
      tx.invalidateChallenge(challenge.id, timestamp, safeReason);
      const response = { id: id('invalidation'), challengeId: challenge.id, status: 'INVALIDATED', invalidatedAt: timestamp, reason: safeReason };
      tx.saveIdempotency(record('transfer.confirmation.invalidate', idempotencyKey, requestHash, response, timestamp));
      audit(tx, actor, 'bank.transfer.confirmation.invalidate', 'confirmation-challenge', challenge.id, 'SUCCEEDED', { reason: safeReason }, timestamp);
      return response;
    });
  }

  async function execute(actor, transferOrderId, idempotencyKey) {
    requireSystemActor(actor);
    const requestHash = hash({ transferOrderId });
    return repository.withTransaction(async tx => {
      const replay = idempotentReplay(tx, 'transfer.execute', idempotencyKey, requestHash);
      if (replay) return replay;
      const order = tx.getTransferOrder(transferOrderId);
      if (!order || order.status !== 'AUTHORIZED') throw stateConflict();
      const authorization = tx.getAuthorization(order.id);
      if (!authorization) throw new BankApiError(409, 'authorization_missing', '转账指令尚未获得用户授权');
      // L2 防御性校验：转账意图在意图图谱中声明 riskLevel=L2、executionMode=confirm-required，
      // 此处显式验证授权必须关联到一个已被消费的确认挑战，避免任何路径绕过显式确认直接执行。
      if (!authorization.challengeId) throw new BankApiError(409, 'confirmation_missing', '转账指令缺少显式确认挑战');
      const challenge = tx.getChallenge(authorization.challengeId);
      if (!challenge || challenge.status !== 'USED' || !challenge.usedAt) {
        throw new BankApiError(409, 'confirmation_not_consumed', '确认挑战未被正确消费，无法执行转账');
      }
      const account = tx.getAccountById(order.fromAccountId);
      const payee = tx.getPayeeById(order.payeeId);
      const targetAccount = payee && tx.getAccountById(payee.targetAccountId);
      const sourceBalance = tx.getBalance(order.fromAccountId);
      const targetBalance = targetAccount && tx.getBalance(targetAccount.id);
      if (!account || account.status !== 'ACTIVE' || !payee || payee.status !== 'ACTIVE' || !targetAccount || !targetBalance) {
        throw new BankApiError(422, 'execution_party_unavailable', '付款方或收款方当前不可用');
      }
      if (sourceBalance.version !== order.balanceVersion) throw new BankApiError(409, 'account_version_conflict', '账户余额已发生变化，请重新确认');
      if (sourceBalance.availableMinor < order.amountMinor) throw new BankApiError(422, 'insufficient_funds', '账户可用余额不足');
      const timestamp = now();
      const ledgerTransactionId = id('ledger');
      tx.insertLedgerTransaction({ id: ledgerTransactionId, businessType: 'TRANSFER', businessId: order.id, currency: order.currency, postedAt: timestamp });
      tx.insertLedgerEntry({ id: id('entry'), ledgerTransactionId, accountId: account.id, direction: 'DEBIT', amountMinor: order.amountMinor, description: '转账给' + payee.name });
      tx.insertLedgerEntry({ id: id('entry'), ledgerTransactionId, accountId: targetAccount.id, direction: 'CREDIT', amountMinor: order.amountMinor, description: '转账清算' });
      tx.updateBalance(account.id, sourceBalance.availableMinor - order.amountMinor, sourceBalance.ledgerMinor - order.amountMinor, sourceBalance.version);
      tx.updateBalance(targetAccount.id, targetBalance.availableMinor + order.amountMinor, targetBalance.ledgerMinor + order.amountMinor, targetBalance.version);
      const execution = tx.insertExecution({
        id: id('execution'), transferOrderId: order.id, status: 'SUCCEEDED', ledgerTransactionId,
        receiptNo: receiptNo(timestamp), executedAt: timestamp, createdAt: timestamp,
      });
      tx.updateTransferStatus(order.id, 'AUTHORIZED', 'SUCCEEDED');
      tx.saveIdempotency(record('transfer.execute', idempotencyKey, requestHash, execution, timestamp));
      audit(tx, actor, 'bank.transfer.execute', 'transfer-order', order.id, 'SUCCEEDED', { executionId: execution.id, amountMinor: order.amountMinor }, timestamp);
      return execution;
    });
  }

  async function getOrder(customerId, transferOrderId) {
    const order = await repository.getTransferOrder(transferOrderId);
    if (!order || order.customerId !== customerId) throw new BankApiError(404, 'not_found', '资源不存在');
    const response = publicOrder(order);
    const execution = await repository.getExecutionByOrder(order.id);
    if (execution) response.executionId = execution.id;
    return response;
  }

  async function getExecution(customerId, executionId) {
    const execution = await repository.getTransferExecution(executionId);
    if (!execution) throw new BankApiError(404, 'not_found', '资源不存在');
    const order = await repository.getTransferOrder(execution.transferOrderId);
    if (!order || order.customerId !== customerId) throw new BankApiError(404, 'not_found', '资源不存在');
    // 附带收款人/付款账户/金额脱敏详情，供回单投影直接渲染卡号/银行名，
    // 避免下游再次反查目录。摘要构建失败时降级为纯 execution 返回。
    const summary = await repository.withTransaction(async tx => {
      try {
        return buildSummary(tx, order, 0);
      } catch (e) {
        return null;
      }
    });
    return Object.assign({}, execution, summary ? {
      fromAccount: summary.fromAccount,
      payee: summary.payee,
      amount: summary.amount,
    } : {});
  }

  return { createOrder, validateOrder, createConfirmationChallenge, authorize, cancelOrder, invalidateConfirmation, execute, getOrder, getExecution };
}

function validateCreateInput(input) {
  const body = input || {};
  if (body.currency !== 'CNY') throw new BankApiError(422, 'unsupported_currency', '第一期仅支持人民币转账', [{ field: 'currency', code: 'unsupported' }]);
  return {
    fromAccountId: requireString(body.fromAccountId, 'fromAccountId', { max: 64 }),
    payeeId: requireString(body.payeeId, 'payeeId', { max: 64 }),
    amountMinor: requirePositiveMinor(body.amountMinor, 'amountMinor'),
    currency: 'CNY',
    purpose: body.purpose == null
      ? ''
      : requireString(body.purpose, 'purpose', { min: 1, max: 40 }),
  };
}

function buildSummary(tx, order, feeMinor) {
  const account = tx.getAccountById(order.fromAccountId);
  const payee = tx.getPayeeById(order.payeeId);
  const balance = tx.getBalance(order.fromAccountId);
  if (!account || !payee || !balance) throw new BankApiError(404, 'not_found', '转账摘要依赖资源不存在');
  return {
    transferOrderId: order.id,
    fromAccount: { id: account.id, name: account.name, maskedNo: account.maskedNo },
    payee: { id: payee.id, name: payee.name, bankName: payee.bankName, maskedNo: payee.maskedNo },
    amount: moneyValue(order.amountMinor, order.currency),
    fee: moneyValue(feeMinor || 0, order.currency),
    balanceAfter: moneyValue(balance.availableMinor - order.amountMinor - (feeMinor || 0), order.currency),
    ...(order.purpose ? { purpose: order.purpose } : {}),
  };
}

function publicChallenge(tx, order, challenge, token) {
  if (!challenge) throw new BankApiError(404, 'confirmation_not_found', '确认挑战不存在');
  if (challenge.status !== 'ACTIVE') throw stateConflict();
  const validation = tx.getLatestValidation(order.id);
  return {
    id: challenge.id,
    transferOrderId: order.id,
    status: 'PENDING_CONFIRMATION',
    summary: buildSummary(tx, order, validation ? validation.feeMinor : 0),
    confirmationToken: token,
    expiresAt: challenge.expiresAt,
  };
}

function publicOrder(order) {
  return { id: order.id, fromAccountId: order.fromAccountId, payeeId: order.payeeId, amount: moneyValue(order.amountMinor, order.currency), purpose: order.purpose, status: order.status, balanceVersion: order.balanceVersion, createdAt: order.createdAt, updatedAt: order.updatedAt };
}

function publicValidation(validation, balanceAfterMinor) {
  return { id: validation.id, transferOrderId: validation.transferOrderId, result: validation.result, checks: validation.checks, fee: moneyValue(validation.feeMinor, 'CNY'), balanceAfter: moneyValue(balanceAfterMinor, 'CNY'), expiresAt: validation.expiresAt };
}

function id(prefix) { return prefix + '_' + crypto.randomUUID(); }
function hash(value) { return crypto.createHash('sha256').update(typeof value === 'string' ? value : canonical(value)).digest('hex'); }
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
}
function confirmationToken(challengeId, summaryHash, secret) { return crypto.createHmac('sha256', secret).update(challengeId + ':' + summaryHash).digest('base64url'); }
function safeEqual(left, right) {
  const a = Buffer.from(String(left));
  const b = Buffer.from(String(right));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
function addMinutes(timestamp, minutes) { return new Date(new Date(timestamp).getTime() + minutes * 60000).toISOString(); }
function isExpired(expiresAt, timestamp) { return new Date(expiresAt).getTime() <= new Date(timestamp).getTime(); }
function receiptNo(timestamp) { return 'RCPT-' + String(timestamp).replace(/\D/g, '').slice(0, 14) + '-' + crypto.randomBytes(3).toString('hex').toUpperCase(); }
function stateConflict() { return new BankApiError(409, 'state_conflict', '转账指令状态不允许当前操作'); }

function requireOwnedOrder(tx, customerId, orderId) {
  const order = tx.getTransferOrder(orderId);
  if (!order || order.customerId !== customerId) throw new BankApiError(404, 'not_found', '资源不存在');
  return order;
}

function requireCustomerActor(actor) {
  if (!actor || !actor.customerId || !['agent', 'user'].includes(actor.type)) throw new BankApiError(403, 'forbidden', '当前调用方无权访问客户能力');
}
function requireUserActor(actor) {
  if (!actor || actor.type !== 'user' || !actor.customerId || actor.id !== actor.customerId) throw new BankApiError(403, 'forbidden', '转账授权必须由客户本人完成');
}
function requireSystemActor(actor) {
  if (!actor || actor.type !== 'system') throw new BankApiError(403, 'forbidden', '最终执行能力仅允许受控执行引擎调用');
}

function record(scope, key, requestHash, response, createdAt) {
  if (!key) throw new BankApiError(400, 'missing_idempotency_key', '缺少幂等键');
  return { scope, key, requestHash, response, status: 'SUCCEEDED', createdAt };
}
function idempotentReplay(tx, scope, key, requestHash) {
  if (!key) throw new BankApiError(400, 'missing_idempotency_key', '缺少幂等键');
  const existing = tx.getIdempotency(scope, key);
  if (!existing) return null;
  verifyReplay(existing, requestHash);
  return existing.response;
}
function verifyReplay(existing, requestHash) {
  if (existing.requestHash !== requestHash) throw new BankApiError(409, 'idempotency_conflict', '同一幂等键对应了不同请求');
}
function audit(tx, actor, action, resourceType, resourceId, outcome, details, createdAt) {
  tx.insertAudit({ id: id('audit'), traceId: actor.traceId || id('trace'), actorType: actor.type, actorId: actor.id || null, action, resourceType, resourceId, outcome, details, createdAt });
}

module.exports = { createTransferService };
