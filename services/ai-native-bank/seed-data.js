'use strict';

const SEED = {
  customer: { id: 'cust_demo_001', name: '陈先生', status: 'ACTIVE', city: '上海' },
  accounts: [
    { id: 'acct_salary_6217', customerId: 'cust_demo_001', maskedNo: '**** **** **** 6217', name: '工资卡', type: 'DEBIT_CARD', currency: 'CNY', status: 'ACTIVE', visibility: 'CUSTOMER' },
    { id: 'acct_saving_8032', customerId: 'cust_demo_001', maskedNo: '**** **** **** 8032', name: '储蓄卡', type: 'SAVINGS', currency: 'CNY', status: 'ACTIVE', visibility: 'CUSTOMER' },
    { id: 'acct_external_clearing', customerId: null, maskedNo: 'EXTERNAL-CLEARING', name: '外部清算户', type: 'SYSTEM', currency: 'CNY', status: 'ACTIVE', visibility: 'SYSTEM' },
    { id: 'acct_opening_equity', customerId: null, maskedNo: 'OPENING-EQUITY', name: '期初平衡户', type: 'SYSTEM', currency: 'CNY', status: 'ACTIVE', visibility: 'SYSTEM' },
  ],
  balances: [
    { accountId: 'acct_salary_6217', availableMinor: 2864036, ledgerMinor: 2864036, version: 1 },
    { accountId: 'acct_saving_8032', availableMinor: 7582000, ledgerMinor: 7582000, version: 1 },
    { accountId: 'acct_external_clearing', availableMinor: 112800, ledgerMinor: 112800, version: 1 },
    { accountId: 'acct_opening_equity', availableMinor: -10558836, ledgerMinor: -10558836, version: 1 },
  ],
  payees: [
    { id: 'payee_zhang_4321', customerId: 'cust_demo_001', name: '张伟', bankName: '中国建设银行', maskedNo: '**** **** **** 4321', status: 'ACTIVE', targetAccountId: 'acct_external_clearing' },
    { id: 'payee_zhangsan_7319', customerId: 'cust_demo_001', name: '张三', bankName: '中国建设银行', maskedNo: '**** **** **** 7319', status: 'ACTIVE', targetAccountId: 'acct_external_clearing' },
    { id: 'payee_li_6088', customerId: 'cust_demo_001', name: '李敏', bankName: '中国工商银行', maskedNo: '**** **** **** 6088', status: 'ACTIVE', targetAccountId: 'acct_external_clearing' },
    { id: 'payee_zhangshuai_1102', customerId: 'cust_demo_001', name: '张帅', bankName: '中国建设银行', maskedNo: '**** **** **** 1102', status: 'ACTIVE', targetAccountId: 'acct_external_clearing' },
    { id: 'payee_lisi_3308', customerId: 'cust_demo_001', name: '李四', bankName: '中国工商银行', maskedNo: '**** **** **** 3308', status: 'ACTIVE', targetAccountId: 'acct_external_clearing' },
  ],
  creditCards: [
    { id: 'credit_global_8891', customerId: 'cust_demo_001', maskedNo: '**** **** **** 8891', productName: '龙卡全球支付卡', network: 'VISA', cardType: 'PHYSICAL', status: 'ACTIVE', totalLimitMinor: 6000000, availableLimitMinor: 4578000, cashLimitMinor: 1200000, statementDay: 5, dueDay: 18 },
    { id: 'credit_digital_2268', customerId: 'cust_demo_001', maskedNo: '**** **** **** 2268', productName: '龙卡数字信用卡', network: 'UNIONPAY', cardType: 'DIGITAL', status: 'INACTIVE', totalLimitMinor: 3000000, availableLimitMinor: 3000000, cashLimitMinor: 600000, statementDay: 12, dueDay: 25 },
  ],
  creditCardSettings: [
    { cardId: 'credit_global_8891', onlineTransactionsEnabled: true, contactlessEnabled: true, overseasEnabled: false, singleTransactionLimitMinor: 300000, dailyLimitMinor: 1000000 },
    { cardId: 'credit_digital_2268', onlineTransactionsEnabled: false, contactlessEnabled: false, overseasEnabled: false, singleTransactionLimitMinor: 100000, dailyLimitMinor: 300000 },
  ],
  creditCardTransactions: [
    { id: 'card_txn_0829_coffee', cardId: 'credit_global_8891', direction: 'DEBIT', amountMinor: 3800, currency: 'CNY', description: '咖啡店消费', status: 'POSTED', occurredAt: '2026-08-29T09:12:00.000+08:00' },
    { id: 'card_txn_0828_hotel', cardId: 'credit_global_8891', direction: 'DEBIT', amountMinor: 68800, currency: 'CNY', description: '酒店消费', status: 'POSTED', occurredAt: '2026-08-28T21:30:00.000+08:00' },
    { id: 'card_txn_0827_refund', cardId: 'credit_global_8891', direction: 'CREDIT', amountMinor: 12800, currency: 'CNY', description: '退款入账', status: 'POSTED', occurredAt: '2026-08-27T15:20:00.000+08:00' },
  ],
  customerPoints: [
    { customerId: 'cust_demo_001', balance: 12860, expiringAmount: 3200, expiryDate: '2026-09-30' },
  ],
  // 期初总账（ledger_opening）由 seed-wealth-data.js 按一年历史流水推演后动态生成，
  // 以保证复式记账平衡与余额快照一致。
  ledgerTransactions: [
    { id: 'ledger_salary_0828', businessType: 'SALARY', businessId: 'seed-salary-0828', currency: 'CNY', postedAt: '2026-08-28T08:30:00.000+08:00' },
    { id: 'ledger_payment_0828', businessType: 'CARD_PAYMENT', businessId: 'seed-payment-0828', currency: 'CNY', postedAt: '2026-08-28T12:18:00.000+08:00' },
    { id: 'ledger_transfer_0827', businessType: 'TRANSFER', businessId: 'seed-transfer-0827', currency: 'CNY', postedAt: '2026-08-27T20:31:00.000+08:00' },
  ],
  ledgerEntries: [
    { id: 'entry_salary_credit', ledgerTransactionId: 'ledger_salary_0828', accountId: 'acct_salary_6217', direction: 'CREDIT', amountMinor: 1860000, description: '代发工资' },
    { id: 'entry_salary_debit', ledgerTransactionId: 'ledger_salary_0828', accountId: 'acct_opening_equity', direction: 'DEBIT', amountMinor: 1860000, description: '代发清算' },
    { id: 'entry_payment_debit', ledgerTransactionId: 'ledger_payment_0828', accountId: 'acct_salary_6217', direction: 'DEBIT', amountMinor: 12800, description: '餐饮消费' },
    { id: 'entry_payment_credit', ledgerTransactionId: 'ledger_payment_0828', accountId: 'acct_external_clearing', direction: 'CREDIT', amountMinor: 12800, description: '快捷支付清算' },
    { id: 'entry_transfer_debit', ledgerTransactionId: 'ledger_transfer_0827', accountId: 'acct_salary_6217', direction: 'DEBIT', amountMinor: 100000, description: '转账给张伟' },
    { id: 'entry_transfer_credit', ledgerTransactionId: 'ledger_transfer_0827', accountId: 'acct_external_clearing', direction: 'CREDIT', amountMinor: 100000, description: '外部转账清算' },
  ],
};

module.exports = { SEED };
