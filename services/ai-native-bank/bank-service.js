'use strict';

const { BankApiError, requireString } = require('./api-contract');

function createBankService(options) {
  const repository = options.repository;
  const now = options.now || (() => new Date().toISOString());

  async function getCustomer(customerId) {
    const customer = await repository.getCustomer(requireString(customerId, 'customerId', { max: 64 }));
    if (!customer || customer.status !== 'ACTIVE') throw notFound();
    return customer;
  }

  async function listAccounts(customerId) {
    await getCustomer(customerId);
    const accounts = await repository.listAccounts(customerId);
    return accounts.map(toPublicAccount);
  }

  async function requireOwnedAccount(customerId, accountId) {
    await getCustomer(customerId);
    const account = await repository.getAccount(customerId, requireString(accountId, 'accountId', { max: 64 }));
    if (!account) throw notFound();
    return account;
  }

  async function getAccount(customerId, accountId) {
    return toPublicAccount(await requireOwnedAccount(customerId, accountId));
  }

  async function getBalance(customerId, accountId) {
    const account = await requireOwnedAccount(customerId, accountId);
    const balance = await repository.getBalance(account.id);
    if (!balance) throw notFound();
    return {
      accountId: account.id,
      available: moneyValue(balance.availableMinor, account.currency),
      ledger: moneyValue(balance.ledgerMinor, account.currency),
      version: balance.version,
      updatedAt: balance.updatedAt,
    };
  }

  async function listTransactions(customerId, accountId, paging) {
    const account = await requireOwnedAccount(customerId, accountId);
    const opts = validatePaging(paging || {});
    const page = await repository.listTransactions(account.id, opts);
    return {
      data: page.data.map(item => ({
        id: item.id,
        direction: item.direction,
        amount: moneyValue(Math.abs(item.signedMinor), item.currency),
        signedAmountMinor: item.signedMinor,
        description: item.description,
        businessType: item.businessType,
        postedAt: item.postedAt,
      })),
      meta: { nextCursor: page.nextCursor, hasNext: Boolean(page.nextCursor), limit: opts.limit },
    };
  }

  async function listPayees(customerId, options) {
    await getCustomer(customerId);
    return (await repository.listPayees(customerId, options)).map(toPublicPayee);
  }

  async function requireOwnedPayee(customerId, payeeId) {
    await getCustomer(customerId);
    const payee = await repository.getPayee(customerId, requireString(payeeId, 'payeeId', { max: 64 }));
    if (!payee) throw notFound();
    return payee;
  }

  async function validatePayee(customerId, payeeId) {
    const payee = await requireOwnedPayee(customerId, payeeId);
    if (payee.status !== 'ACTIVE') {
      throw new BankApiError(422, 'payee_unavailable', '收款方当前不可用', [{ field: 'payeeId', code: 'inactive' }]);
    }
    return { valid: true, payee: toPublicPayee(payee), validatedAt: now() };
  }

  return {
    getCustomer,
    listAccounts,
    getAccount,
    getBalance,
    listTransactions,
    listPayees,
    validatePayee,
    requireOwnedAccount,
    requireOwnedPayee,
  };
}

function validatePaging(input) {
  const limit = input.limit == null ? 20 : Number(input.limit);
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw new BankApiError(422, 'validation_error', 'limit 必须是 1 到 100 的整数', [{ field: 'limit', code: 'out_of_range' }]);
  }
  if (input.cursor != null) {
    const cursor = String(input.cursor);
    let decoded = '';
    try { decoded = Buffer.from(cursor, 'base64url').toString('utf8'); } catch (error) { decoded = ''; }
    if (!/^\d+$/.test(decoded)) throw new BankApiError(422, 'invalid_cursor', '分页游标无效', [{ field: 'cursor', code: 'invalid' }]);
  }
  return { limit, cursor: input.cursor == null ? null : String(input.cursor) };
}

function moneyValue(amountMinor, currency) {
  const code = currency || 'CNY';
  const symbol = code === 'CNY' ? '¥' : code + ' ';
  return {
    amountMinor: Number(amountMinor),
    currency: code,
    display: symbol + (Number(amountMinor) / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  };
}

function toPublicAccount(account) {
  return { id: account.id, maskedNo: account.maskedNo, name: account.name, type: account.type, currency: account.currency, status: account.status };
}

function toPublicPayee(payee) {
  return { id: payee.id, name: payee.name, bankName: payee.bankName, maskedNo: payee.maskedNo, status: payee.status };
}

function notFound() {
  return new BankApiError(404, 'not_found', '资源不存在');
}

module.exports = { createBankService, moneyValue, validatePaging };
