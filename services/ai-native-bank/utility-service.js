'use strict';

const crypto = require('node:crypto');
const { BankApiError } = require('./api-contract');
const { moneyValue } = require('./bank-service');

function createUtilityService(options = {}) {
  const repository = options.repository;
  const bankService = options.bankService;
  if (!repository || !bankService) throw new TypeError('utility service dependencies are required');

  async function listBills(customerId) {
    await bankService.getCustomer(customerId);
    const rows = await repository.listUtilityBills(customerId);
    return {
      data: rows.map(row => ({
        id: row.id, type: row.type, name: row.name, period: row.period,
        amount: moneyValue(row.amountMinor, row.currency), dueDate: row.dueDate,
        status: row.status,
      })),
    };
  }

  async function pay(actor, input, idempotencyKey) {
    if (!actor || actor.type !== 'user' || !actor.customerId) throw new BankApiError(403, 'forbidden', '生活缴费必须由客户本人确认');
    if (!input || typeof input !== 'object' || Array.isArray(input)
      || Object.keys(input).some(key => !['billId', 'accountId'].includes(key))) {
      throw new BankApiError(422, 'validation_error', '生活缴费请求无效');
    }
    if (typeof idempotencyKey !== 'string' || !idempotencyKey) throw new BankApiError(400, 'missing_idempotency_key', '缺少幂等键');
    const requestHash = crypto.createHash('sha256').update(JSON.stringify({
      billId: input.billId, accountId: input.accountId,
    })).digest('hex');
    const result = await repository.payUtilityBill({
      customerId: actor.customerId, billId: input.billId, accountId: input.accountId,
      idempotencyKey, requestHash, traceId: actor.traceId,
      actorType: actor.type, actorId: actor.id,
    });
    return Object.assign({}, result, { paid: moneyValue(result.paid.amountMinor, result.paid.currency) });
  }

  return Object.freeze({ listBills, pay });
}

module.exports = { createUtilityService };
