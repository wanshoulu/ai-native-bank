'use strict';

/**
 * 贷款域服务：我的贷款、还款计划查询，以及基于发薪流水的统一测额（确定性规则演示）。
 */

const crypto = require('node:crypto');
const { BankApiError, requireString } = require('./api-contract');
const { moneyValue } = require('./bank-service');

function createLoanService(options) {
  const opts = options || {};
  const repository = opts.repository;
  const bankService = opts.bankService;
  const wealthService = opts.wealthService;
  const now = typeof opts.now === 'function' ? opts.now : () => new Date().toISOString();
  if (!repository || !bankService || !wealthService) throw new Error('loan service requires repository, bankService and wealthService');

  async function listLoans(customerId) {
    await bankService.getCustomer(customerId);
    const loans = await repository.listLoans(customerId);
    const accounts = new Map((await bankService.listAccounts(customerId)).map(account => [account.id, account]));
    return {
      data: loans.map(loan => toPublicLoan(loan, accounts.get(loan.repaymentAccountId))),
      totalOutstanding: moneyValue(
        loans.filter(loan => loan.status === 'ACTIVE').reduce((sum, loan) => sum + loan.outstandingPrincipalMinor, 0),
        'CNY',
      ),
      monthlyObligation: moneyValue(
        loans.filter(loan => loan.status === 'ACTIVE').reduce((sum, loan) => sum + loan.monthlyPaymentMinor, 0),
        'CNY',
      ),
    };
  }

  async function getLoan(customerId, loanId) {
    await bankService.getCustomer(customerId);
    const loan = await repository.getLoan(customerId, requireString(loanId, 'loanId', { max: 64 }));
    if (!loan) throw new BankApiError(404, 'loan_not_found', '贷款不存在');
    const accounts = new Map((await bankService.listAccounts(customerId)).map(account => [account.id, account]));
    return toPublicLoan(loan, accounts.get(loan.repaymentAccountId));
  }

  async function listRepayments(customerId, loanId) {
    const loan = await getLoan(customerId, loanId);
    const rows = await repository.listLoanRepayments(customerId, loan.id);
    return {
      loanId: loan.id,
      productName: loan.productName,
      data: rows.map(row => ({
        periodNo: row.periodNo,
        dueDate: row.dueDate,
        principal: moneyValue(row.principalMinor, 'CNY'),
        interest: moneyValue(row.interestMinor, 'CNY'),
        total: moneyValue(row.totalMinor, 'CNY'),
        status: row.status,
        paidAt: row.paidAt,
      })),
    };
  }

  /**
   * 统一测额：月均收入 × 系数 - 存量月供占用，得到各产品可测额度（演示规则，非授信承诺）。
   * 规则与建行快贷上限（20 万）、消费贷年化上限 6%（2026-08 综合融资成本公示）保持一致。
   */
  async function precalculate(actor, input, idempotencyKey) {
    if (!actor || !actor.customerId || !['agent', 'user'].includes(actor.type)) {
      throw new BankApiError(403, 'forbidden', '当前调用方无权发起测额');
    }
    const body = validatePrecalculateInput(input);
    const requestHash = hash({ customerId: actor.customerId, purpose: body.purpose });
    // 计算所需数据在事务外读取（事务回调内不得调用被写队列屏障包装的公开读方法，否则死锁）
    const insights = await wealthService.getSalaryInsights(actor.customerId);
    const loans = await repository.listLoans(actor.customerId);
    const monthlyIncome = Math.max(0, insights.monthlyAvgSalary.amountMinor);
    const monthlyObligation = loans
      .filter(loan => loan.status === 'ACTIVE')
      .reduce((sum, loan) => sum + loan.monthlyPaymentMinor, 0);
    const disposable = Math.max(0, monthlyIncome - monthlyObligation);
    // 收入偿付比不超过 50%：可承载月供 = 月均可支配收入的一半
    const capacity = Math.floor(disposable / 2);
    const kuaDaiLimitMinor = Math.min(20000000, Math.max(0, capacity * 12 * 2));
    const jianYiLimitMinor = Math.min(30000000, Math.max(0, capacity * 36 * 2));
    const renovationLimitMinor = Math.min(100000000, Math.max(0, capacity * 60 * 2));
    const calculatedResponse = () => {
      const timestamp = now();
      return {
        purpose: body.purpose,
        estimatedLimits: {
          kuaidai: moneyValue(kuaDaiLimitMinor, 'CNY'),
          jianyidai: moneyValue(jianYiLimitMinor, 'CNY'),
          renovation: moneyValue(renovationLimitMinor, 'CNY'),
        },
        basedOn: {
          monthlyAvgIncome: insights.monthlyAvgSalary,
          monthlyObligation: moneyValue(monthlyObligation, 'CNY'),
          monthlyDisposable: moneyValue(disposable, 'CNY'),
          monthsAnalyzed: insights.months.length,
        },
        products: [
          { productId: 'prod_loan_kuaidai', name: '信用快贷', rateNote: '年化利率 3.00% 起，最高 20 万元，线上随借随还' },
          { productId: 'prod_loan_jianyidai', name: '建易贷', rateNote: '年化 3.20% 起，最长 3 年，面向代发工资客群' },
          { productId: 'prod_loan_zhuangxiu', name: '装修分期', rateNote: '月费率 0.18% 起，6-60 期，指定装修类商户' },
        ],
        note: '模拟测额结果，不构成授信承诺；实际额度以审批为准。年化利率上限执行监管公示要求。',
        calculatedAt: timestamp,
      };
    };
    return repository.withTransaction(async tx => {
      const replay = idempotentReplay(tx, 'loan.precalculate', idempotencyKey, requestHash);
      if (replay) return replay;
      const timestamp = now();
      const response = calculatedResponse();
      tx.saveIdempotency(record('loan.precalculate', idempotencyKey, requestHash, response, timestamp));
      audit(tx, actor, 'bank.loan.precalculate', 'loan-precalculation', actor.customerId, 'SUCCEEDED', { purpose: body.purpose }, timestamp);
      return response;
    });
  }

  async function repay(actor, input, idempotencyKey) {
    if (!actor || actor.type !== 'user' || !actor.customerId) throw new BankApiError(403, 'forbidden', '贷款还款必须由客户本人确认');
    const body = input && typeof input === 'object' && !Array.isArray(input) ? input : {};
    const repaymentId = requireString(body.repaymentId, 'repaymentId', { max: 64 });
    const fromAccountId = requireString(body.fromAccountId, 'fromAccountId', { max: 64 });
    if (!Number.isSafeInteger(body.amountMinor) || body.amountMinor <= 0) throw new BankApiError(422, 'validation_error', '还款金额无效');
    const requestHash = hash({ repaymentId, fromAccountId, amountMinor: body.amountMinor });
    return repository.withTransaction(async tx => {
      const replay = idempotentReplay(tx, 'loan.repayment.create', idempotencyKey, requestHash);
      if (replay) return replay;
      const row = tx.queryOne(`SELECT repayment.*, loan.product_name FROM bank_loan_repayments repayment
        INNER JOIN bank_loans loan ON loan.id = repayment.loan_id
        WHERE repayment.id = ? AND repayment.customer_id = ?`, [repaymentId, actor.customerId]);
      if (!row) throw new BankApiError(404, 'loan_repayment_not_found', '贷款还款计划不存在');
      if (!['DUE', 'PENDING', 'SCHEDULED'].includes(row.status)) throw new BankApiError(409, 'loan_repayment_unavailable', '该期贷款当前不可还款');
      if (Number(row.total_minor) !== body.amountMinor) throw new BankApiError(409, 'loan_repayment_changed', '贷款应还金额已变化，请重新确认');
      const account = tx.queryOne("SELECT * FROM bank_accounts WHERE id = ? AND customer_id = ? AND visibility = 'CUSTOMER' AND status = 'ACTIVE'", [fromAccountId, actor.customerId]);
      if (!account) throw new BankApiError(404, 'not_found', '付款账户不存在或不可用');
      const balance = tx.getBalance(fromAccountId);
      if (!balance || balance.availableMinor < body.amountMinor) throw new BankApiError(422, 'insufficient_funds', '付款账户可用余额不足');
      const timestamp = now();
      const paymentId = id('loan_repay');
      const ledgerId = 'lg_' + paymentId;
      const nextAvailable = balance.availableMinor - body.amountMinor;
      tx.updateBalance(fromAccountId, nextAvailable, balance.ledgerMinor - body.amountMinor, balance.version);
      tx.insertLedgerTransaction({ id: ledgerId, businessType: 'LOAN_REPAYMENT', businessId: paymentId, currency: account.currency, postedAt: timestamp });
      tx.insertLedgerEntry({ id: 'le_' + paymentId + '_a', ledgerTransactionId: ledgerId, accountId: fromAccountId, direction: 'DEBIT', amountMinor: body.amountMinor, description: row.product_name + '还款' });
      tx.insertLedgerEntry({ id: 'le_' + paymentId + '_b', ledgerTransactionId: ledgerId, accountId: 'acct_external_clearing', direction: 'CREDIT', amountMinor: body.amountMinor, description: row.product_name + '还款清算' });
      tx.run(`INSERT INTO bank_account_transactions
        (id,account_id,customer_id,direction,amount_minor,currency,business_type,category,counterparty,description,channel,balance_after_minor,occurred_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`, [
        'atxn_' + paymentId, fromAccountId, actor.customerId, 'DEBIT', body.amountMinor, account.currency,
        'LOAN_REPAYMENT', 'LOAN', row.product_name, row.product_name + '还款', 'AGENT', nextAvailable, timestamp,
      ]);
      tx.run("UPDATE bank_loan_repayments SET status = 'PAID', paid_at = ? WHERE id = ? AND customer_id = ?", [timestamp, repaymentId, actor.customerId]);
      tx.run(`UPDATE bank_loans SET outstanding_principal_minor = MAX(0, outstanding_principal_minor - ?),
        remaining_months = MAX(0, remaining_months - 1) WHERE id = ? AND customer_id = ?`, [Number(row.principal_minor), row.loan_id, actor.customerId]);
      const response = {
        id: paymentId, repaymentId, loanId: row.loan_id, accountId: fromAccountId,
        status: 'SUCCEEDED', paid: moneyValue(body.amountMinor, account.currency), paidAt: timestamp,
      };
      tx.saveIdempotency(record('loan.repayment.create', idempotencyKey, requestHash, response, timestamp));
      audit(tx, actor, 'bank.loan.repayment.create', 'loan-repayment', repaymentId, 'SUCCEEDED', { paymentId, amountMinor: body.amountMinor }, timestamp);
      return response;
    });
  }

  function validatePrecalculateInput(input) {
    const value = input && typeof input === 'object' && !Array.isArray(input) ? input : {};
    const allowed = new Set(['purpose']);
    const unknown = Object.keys(value).filter(key => !allowed.has(key));
    if (unknown.length) throw new BankApiError(422, 'validation_error', '测额请求包含不允许的字段', unknown.map(field => ({ field, code: 'unknown_field' })));
    const purposes = new Set(['CONSUMPTION', 'RENOVATION', 'HOUSING']);
    const purpose = value.purpose == null ? 'CONSUMPTION' : requireString(value.purpose, 'purpose', { max: 32 });
    if (!purposes.has(purpose)) throw new BankApiError(422, 'validation_error', '测额用途无效', [{ field: 'purpose', code: 'invalid' }]);
    return { purpose };
  }

  function toPublicLoan(loan, repaymentAccount) {
    return {
      id: loan.id,
      productName: loan.productName,
      loanType: loan.loanType,
      contractNo: loan.contractNo,
      principal: moneyValue(loan.principalMinor, loan.currency),
      outstandingPrincipal: moneyValue(loan.outstandingPrincipalMinor, loan.currency),
      annualRatePercent: (loan.annualRateBp / 100).toFixed(2) + '%',
      annualRateBp: loan.annualRateBp,
      rateType: loan.rateType,
      termMonths: loan.termMonths,
      remainingMonths: loan.remainingMonths,
      repaymentMethod: loan.repaymentMethod,
      monthlyPayment: moneyValue(loan.monthlyPaymentMinor, loan.currency),
      monthlyPaymentDay: loan.monthlyPaymentDay,
      status: loan.status,
      disbursedAt: loan.disbursedAt,
      settledAt: loan.settledAt,
      repaymentAccount: repaymentAccount
        ? { id: repaymentAccount.id, name: repaymentAccount.name, maskedNo: repaymentAccount.maskedNo }
        : null,
    };
  }

  function id(prefix) { return prefix + '_' + crypto.randomUUID(); }
  function hash(value) { return crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex'); }
  function record(scope, key, requestHash, response, createdAt) {
    if (!key) throw new BankApiError(400, 'missing_idempotency_key', '缺少幂等键');
    return { scope, key, requestHash, response, status: 'SUCCEEDED', createdAt };
  }
  function idempotentReplay(tx, scope, key, requestHash) {
    if (!key) throw new BankApiError(400, 'missing_idempotency_key', '缺少幂等键');
    const existing = tx.getIdempotency(scope, key);
    if (!existing) return null;
    if (existing.requestHash !== requestHash) throw new BankApiError(409, 'idempotency_conflict', '同一幂等键对应了不同请求');
    return existing.response;
  }
  function audit(tx, actor, action, resourceType, resourceId, outcome, details, createdAt) {
    tx.insertAudit({ id: id('audit'), traceId: actor.traceId || id('trace'), actorType: actor.type, actorId: actor.id || null, action, resourceType, resourceId, outcome, details, createdAt });
  }

  return { listLoans, getLoan, listRepayments, precalculate, repay };
}

module.exports = { createLoanService };
