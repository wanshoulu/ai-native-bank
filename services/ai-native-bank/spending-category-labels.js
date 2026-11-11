'use strict';

// 交易类别展示名（与 agent-runtime user_result.py SPENDING_CATEGORY_LABELS 对齐；
// 未知类别回退原码）。cashflow-service 证据文本与推荐 artifact 共用，避免多份映射漂移。
const SPENDING_CATEGORY_LABELS = {
  FOOD_DINING: '餐饮',
  GROCERIES: '生鲜日用',
  TRANSPORT: '交通出行',
  UTILITIES: '水电燃气',
  TELECOM: '通讯',
  MEDICAL: '医疗健康',
  ENTERTAINMENT: '文化娱乐',
  CREDIT_CARD: '信用卡还款',
  CONSUMER_LOAN: '消费贷款还款',
  HOUSING_LOAN: '住房贷款还款',
  FUND_INVEST: '理财申购',
  INCOME_SALARY: '工资入账',
  INCOME_INTEREST: '利息入账',
  TRANSFER_INTERNAL: '内部转账',
  OTHER: '其他支出',
};

module.exports = { SPENDING_CATEGORY_LABELS };
