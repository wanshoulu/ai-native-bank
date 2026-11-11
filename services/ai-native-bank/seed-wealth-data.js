'use strict';

/**
 * 发薪理财推荐应用扩展种子数据。
 *
 * 数据口径基于建行真实公开信息整理（截至 2026-08）：
 * - 存款/大额存单利率：建行 2025-05-20 挂牌利率（活期 0.05%、3M 0.65%、6M 0.85%、
 *   1Y 0.95%、2Y 1.05%、3Y 1.25%、5Y 1.30%；大额存单 20 万起 1Y 1.20%、3Y 1.55%、
 *   5Y 1.55%/1.60%，2026-07 重新上架）。
 * - 理财产品：建信理财官网产品页（天天利（龙钱宝）10 号 JXTTLLQB202506100、
 *   七日年化约 1.45%、业绩比较基准 1.38%；龙钱宝 2 号对接 50 只货基、
 *   七日年化 0.863%-1.74%；乾元-私享 GD072017001000Y02 近期收益率 1.30%-1.75%；
 *   乾元-日鑫月益按月开放、预期最高年化 0.72%-3.15%）。
 * - 贷款：建行《个人贷款正常履约年化综合融资成本上限公示》（2026-08-01 起施行，
 *   个人消费贷年化上限 6%、房贷上限 LPR+0.5%）；装修分期月费率 0.18% 起。
 * - 信用卡：龙卡全球支付卡/数字信用卡/bilibili 联名卡，账单分期每期费率约 0.60% 起。
 *
 * 客户故事：陈先生（cust_demo_001），上海某科技公司员工，2023 年购房（建行按揭）、
 * 2026 年调薪，发薪日（28 日）后自动攒 + 基金定投 + 定期/理财配置，用于发薪理财
 * 推荐应用的用户画像与推荐样本。
 */

const CUSTOMER_ID = 'cust_demo_001';
const SALARY_ACCOUNT = 'acct_salary_6217';
const SAVING_ACCOUNT = 'acct_saving_8032';
const EQUITY_ACCOUNT = 'acct_opening_equity';
const CLEARING_ACCOUNT = 'acct_external_clearing';
const EMPLOYER = '上海云启科技有限公司';

// 历史流水窗口：2025-09-01 ~ 2026-08-26（8 月末流水沿用原 seed 的 3 笔记录）
const HISTORY_OPENING_AT = '2025-09-01T00:00:00.000+08:00';
const LAST_HISTORY_DAY = '2026-08-26';

// 窗口结束时的目标余额（原 seed 8 月末流水入账前）：
// 工资卡 2,864,036 + 100,000(8/27 转出) - 1,860,000(8/28 工资) + 12,800(8/28 消费)
const TARGET_SALARY_END = 1116836;
const TARGET_SAVING_END = 7582000;

/* ------------------------------------------------------------------ */
/* 建行真实产品目录                                                    */
/* ------------------------------------------------------------------ */

const PRODUCTS = [
  // 存款（挂牌利率，2025-05-20 起）
  { id: 'prod_dep_demand', category: 'DEPOSIT', name: '活期存款', issuer: '中国建设银行', productCode: 'DEP-DEMAND', riskLevel: 'R1', minPurchaseMinor: 100, minIncrementMinor: 100, termType: 'DEMAND', termDays: null, annualRateBp: 5, rateType: 'LISTED_RATE', rateNote: '挂牌年利率 0.05%', purchaseChannel: 'BOTH', status: 'ON_SALE', features: { interestSettlement: '按季结息' }, sortOrder: 10 },
  { id: 'prod_dep_t3m', category: 'DEPOSIT', name: '整存整取3个月', issuer: '中国建设银行', productCode: 'DEP-T3M', riskLevel: 'R1', minPurchaseMinor: 5000, minIncrementMinor: 100, termType: 'FIXED', termDays: 90, annualRateBp: 65, rateType: 'LISTED_RATE', rateNote: '挂牌年利率 0.65%', purchaseChannel: 'BOTH', status: 'ON_SALE', features: { earlyWithdrawal: '提前支取按活期 0.05% 计息' }, sortOrder: 11 },
  { id: 'prod_dep_t6m', category: 'DEPOSIT', name: '整存整取6个月', issuer: '中国建设银行', productCode: 'DEP-T6M', riskLevel: 'R1', minPurchaseMinor: 5000, minIncrementMinor: 100, termType: 'FIXED', termDays: 180, annualRateBp: 85, rateType: 'LISTED_RATE', rateNote: '挂牌年利率 0.85%', purchaseChannel: 'BOTH', status: 'ON_SALE', features: { earlyWithdrawal: '提前支取按活期 0.05% 计息' }, sortOrder: 12 },
  { id: 'prod_dep_1y', category: 'DEPOSIT', name: '整存整取1年', issuer: '中国建设银行', productCode: 'DEP-1Y', riskLevel: 'R1', minPurchaseMinor: 5000, minIncrementMinor: 100, termType: 'FIXED', termDays: 360, annualRateBp: 95, rateType: 'LISTED_RATE', rateNote: '挂牌年利率 0.95%', purchaseChannel: 'BOTH', status: 'ON_SALE', features: { earlyWithdrawal: '提前支取按活期 0.05% 计息' }, sortOrder: 13 },
  { id: 'prod_dep_2y', category: 'DEPOSIT', name: '整存整取2年', issuer: '中国建设银行', productCode: 'DEP-2Y', riskLevel: 'R1', minPurchaseMinor: 5000, minIncrementMinor: 100, termType: 'FIXED', termDays: 720, annualRateBp: 105, rateType: 'LISTED_RATE', rateNote: '挂牌年利率 1.05%', purchaseChannel: 'BOTH', status: 'ON_SALE', features: {}, sortOrder: 14 },
  { id: 'prod_dep_3y', category: 'DEPOSIT', name: '整存整取3年', issuer: '中国建设银行', productCode: 'DEP-3Y', riskLevel: 'R1', minPurchaseMinor: 5000, minIncrementMinor: 100, termType: 'FIXED', termDays: 1080, annualRateBp: 125, rateType: 'LISTED_RATE', rateNote: '挂牌年利率 1.25%', purchaseChannel: 'BOTH', status: 'ON_SALE', features: {}, sortOrder: 15 },
  { id: 'prod_dep_5y', category: 'DEPOSIT', name: '整存整取5年', issuer: '中国建设银行', productCode: 'DEP-5Y', riskLevel: 'R1', minPurchaseMinor: 5000, minIncrementMinor: 100, termType: 'FIXED', termDays: 1800, annualRateBp: 130, rateType: 'LISTED_RATE', rateNote: '挂牌年利率 1.30%（与3年期利率倒挂）', purchaseChannel: 'BOTH', status: 'ON_SALE', features: {}, sortOrder: 16 },
  { id: 'prod_dep_notify1d', category: 'DEPOSIT', name: '通知存款1天', issuer: '中国建设银行', productCode: 'DEP-N1D', riskLevel: 'R1', minPurchaseMinor: 50000, minIncrementMinor: 10000, termType: 'DEMAND', termDays: 1, annualRateBp: 10, rateType: 'LISTED_RATE', rateNote: '挂牌年利率 0.10%', purchaseChannel: 'BOTH', status: 'ON_SALE', features: {}, sortOrder: 17 },
  { id: 'prod_dep_notify7d', category: 'DEPOSIT', name: '通知存款7天', issuer: '中国建设银行', productCode: 'DEP-N7D', riskLevel: 'R1', minPurchaseMinor: 50000, minIncrementMinor: 10000, termType: 'DEMAND', termDays: 7, annualRateBp: 30, rateType: 'LISTED_RATE', rateNote: '挂牌年利率 0.30%', purchaseChannel: 'BOTH', status: 'ON_SALE', features: {}, sortOrder: 18 },
  // 大额存单（20 万起，2026 年在售）
  { id: 'prod_cd_3m', category: 'CERTIFICATE_OF_DEPOSIT', name: '个人大额存单3个月（2026年第5期）', issuer: '中国建设银行', productCode: 'CD-2026-3M', riskLevel: 'R1', minPurchaseMinor: 20000000, minIncrementMinor: 1000000, termType: 'FIXED', termDays: 90, annualRateBp: 90, rateType: 'LISTED_RATE', rateNote: '年利率 0.90%，20万元起存', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { transferable: true }, sortOrder: 20 },
  { id: 'prod_cd_6m', category: 'CERTIFICATE_OF_DEPOSIT', name: '个人大额存单6个月（2026年第9期）', issuer: '中国建设银行', productCode: 'CD-2026-6M', riskLevel: 'R1', minPurchaseMinor: 20000000, minIncrementMinor: 1000000, termType: 'FIXED', termDays: 180, annualRateBp: 110, rateType: 'LISTED_RATE', rateNote: '年利率 1.10%，20万元起存', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { transferable: true }, sortOrder: 21 },
  { id: 'prod_cd_1y', category: 'CERTIFICATE_OF_DEPOSIT', name: '个人大额存单1年（2026年第12期）', issuer: '中国建设银行', productCode: 'CD-2026-1Y', riskLevel: 'R1', minPurchaseMinor: 20000000, minIncrementMinor: 1000000, termType: 'FIXED', termDays: 360, annualRateBp: 120, rateType: 'LISTED_RATE', rateNote: '年利率 1.20%，20万元起存', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { transferable: true }, sortOrder: 22 },
  { id: 'prod_cd_3y', category: 'CERTIFICATE_OF_DEPOSIT', name: '个人大额存单3年（2026年第7期）', issuer: '中国建设银行', productCode: 'CD-2026-3Y', riskLevel: 'R1', minPurchaseMinor: 20000000, minIncrementMinor: 1000000, termType: 'FIXED', termDays: 1080, annualRateBp: 155, rateType: 'LISTED_RATE', rateNote: '年利率 1.55%，20万元起存，按月付息', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { transferable: true, interestPayment: '按月付息' }, sortOrder: 23 },
  { id: 'prod_cd_5y', category: 'CERTIFICATE_OF_DEPOSIT', name: '个人大额存单5年（2026年重上架版）', issuer: '中国建设银行', productCode: 'CD-2026-5Y', riskLevel: 'R1', minPurchaseMinor: 20000000, minIncrementMinor: 1000000, termType: 'FIXED', termDays: 1800, annualRateBp: 155, rateType: 'LISTED_RATE', rateNote: '年利率 1.55%-1.60%，2026年7月重新上架，额度有限', purchaseChannel: 'MOBILE_APP', status: 'LIMITED', features: { transferable: true }, sortOrder: 24 },
  // 现金管理类理财（建信理财）
  { id: 'prod_w_lqb10', category: 'CASH_MANAGEMENT', name: '建信理财天天利（龙钱宝）现金管理类产品10号', issuer: '建信理财有限责任公司', productCode: 'JXTTLLQB202506100', riskLevel: 'R1', minPurchaseMinor: 100, minIncrementMinor: 100, termType: 'OPEN_DAILY', termDays: null, annualRateBp: 145, rateType: 'SEVEN_DAY_ANNUALIZED', rateNote: '七日年化收益率约 1.45%，业绩比较基准 1.38%，成立以来年化 1.45%（2025-06-27 成立）', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { fastRedemptionLimitDailyMinor: 10000, nav: '1.00 元/份' }, sortOrder: 30 },
  { id: 'prod_w_lqb2', category: 'CASH_MANAGEMENT', name: '龙钱宝2号（货币基金组合）', issuer: '建信理财有限责任公司', productCode: 'LQB2-FUND50', riskLevel: 'R1', minPurchaseMinor: 100, minIncrementMinor: 100, termType: 'OPEN_DAILY', termDays: null, annualRateBp: 130, rateType: 'SEVEN_DAY_ANNUALIZED', rateNote: '精选 50 只货币基金，七日年化 0.863%-1.74%，综合日内快赎额度 50 万元', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { fastRedemptionLimitDailyMinor: 500000, underlying: '货币基金组合' }, sortOrder: 31 },
  // 固收/净值型理财（乾元系列）
  { id: 'prod_w_anxin', category: 'FIXED_INCOME_WEALTH', name: '乾元-安鑫（按日）开放式固定收益类理财产品', issuer: '中国建设银行', productCode: 'GY-AX-OD-2024', riskLevel: 'R2', minPurchaseMinor: 1000000, minIncrementMinor: 1000, termType: 'OPEN_DAILY', termDays: null, annualRateBp: 235, rateType: 'BENCHMARK', rateNote: '业绩比较基准（年化）2.10%-2.60%，以产品说明书为准', purchaseChannel: 'BOTH', status: 'ON_SALE', features: { redemption: 'T+1 到账' }, sortOrder: 32 },
  { id: 'prod_w_rixyy', category: 'FIXED_INCOME_WEALTH', name: '乾元-日鑫月益（按月开放）资产组合型理财产品', issuer: '中国建设银行', productCode: 'GY-RXYY-MO', riskLevel: 'R2', minPurchaseMinor: 5000000, minIncrementMinor: 1000, termType: 'OPEN_MONTHLY', termDays: 30, annualRateBp: 252, rateType: 'BENCHMARK', rateNote: '按月开放，持有 30 天以上预期最高年化 2.52%，180 天以上 3.15%', purchaseChannel: 'BOTH', status: 'ON_SALE', features: { openCycle: '按月开放申赎' }, sortOrder: 33 },
  { id: 'prod_w_longbao', category: 'FIXED_INCOME_WEALTH', name: '乾元-建行龙宝（按日）开放式净值型理财产品', issuer: '中国建设银行', productCode: 'GY-LB-OD-2018', riskLevel: 'R2', minPurchaseMinor: 1000000, minIncrementMinor: 1000, termType: 'OPEN_DAILY', termDays: null, annualRateBp: 140, rateType: 'SEVEN_DAY_ANNUALIZED', rateNote: '净值型，七日年化收益率随市场浮动，内部风险评级 2 盏警示灯', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: {}, sortOrder: 34 },
  { id: 'prod_w_sixiang', category: 'FIXED_INCOME_WEALTH', name: '乾元-私享（按日）开放式私人银行人民币理财产品', issuer: '中国建设银行', productCode: 'GD072017001000Y02', riskLevel: 'R3', minPurchaseMinor: 60000000, minIncrementMinor: 10000, termType: 'OPEN_DAILY', termDays: null, annualRateBp: 145, rateType: 'SEVEN_DAY_ANNUALIZED', rateNote: '2025Q2 投资收益率区间 1.30%-1.75%，私人银行客户专属，2018-01-11 成立', purchaseChannel: 'COUNTER', status: 'ON_SALE', features: { audience: '私人银行客户' }, sortOrder: 35 },
  // 代销基金（建行渠道）
  { id: 'prod_fund_000051', category: 'FUND', name: '华夏沪深300ETF联接A', issuer: '华夏基金管理有限公司（建行代销）', productCode: '000051', riskLevel: 'R3', minPurchaseMinor: 100, minIncrementMinor: 100, termType: 'NO_FIXED', termDays: null, annualRateBp: null, rateType: 'FUND_NAV', rateNote: '指数型（沪深300），申购费率 0.12%（一折），净值型', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { fundType: '股票指数型', subscriptionFee: '0.12%' }, sortOrder: 40 },
  { id: 'prod_fund_005827', category: 'FUND', name: '易方达蓝筹精选混合', issuer: '易方达基金管理有限公司（建行代销）', productCode: '005827', riskLevel: 'R3', minPurchaseMinor: 100, minIncrementMinor: 100, termType: 'NO_FIXED', termDays: null, annualRateBp: null, rateType: 'FUND_NAV', rateNote: '混合型（偏股），申购费率 0.15%（一折），净值型', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { fundType: '混合偏股型', subscriptionFee: '0.15%' }, sortOrder: 41 },
  { id: 'prod_fund_070009', category: 'FUND', name: '嘉实超短债债券C', issuer: '嘉实基金管理有限公司（建行代销）', productCode: '070009', riskLevel: 'R2', minPurchaseMinor: 100, minIncrementMinor: 100, termType: 'NO_FIXED', termDays: null, annualRateBp: null, rateType: 'FUND_NAV', rateNote: '短债型，C 类免申购费（销售服务费 0.1%/年）', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { fundType: '短债型' }, sortOrder: 42 },
  // 贷款产品
  { id: 'prod_loan_mortgage', category: 'LOAN', name: '个人住房贷款', issuer: '中国建设银行', productCode: 'LOAN-MORTGAGE', riskLevel: 'NA', minPurchaseMinor: 10000000, minIncrementMinor: 10000, termType: 'FIXED', termDays: null, annualRateBp: 320, rateType: 'LPR_BASED', rateNote: '5年期以上执行 LPR+加点，年化利率上限 LPR+0.5%；存量房贷利率随 LPR 每年1月1日重定价', purchaseChannel: 'BOTH', status: 'ON_SALE', features: { maxTermMonths: 360, repaymentMethods: ['等额本息', '等额本金'] }, sortOrder: 50 },
  { id: 'prod_loan_kuaidai', category: 'LOAN', name: '信用快贷', issuer: '中国建设银行', productCode: 'LOAN-KUAIDAI', riskLevel: 'NA', minPurchaseMinor: 100000, minIncrementMinor: 1000, termType: 'FIXED', termDays: 360, annualRateBp: 324, rateType: 'APR', rateNote: '年化利率 3.00% 起（上限 6%），最高 20 万元，线上申请随借随还；2026 年底前享受财政贴息 1 个百分点', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { maxAmountMinor: 20000000, onlineOnly: true, subsidy: '2026年底前财政贴息1个百分点' }, sortOrder: 51 },
  { id: 'prod_loan_jianyidai', category: 'LOAN', name: '建易贷', issuer: '中国建设银行', productCode: 'LOAN-JYD', riskLevel: 'NA', minPurchaseMinor: 100000, minIncrementMinor: 1000, termType: 'FIXED', termDays: 1080, annualRateBp: 330, rateType: 'APR', rateNote: '面向公积金缴存/代发工资客群的信用消费贷，年化 3.20% 起，最长 3 年', purchaseChannel: 'BOTH', status: 'ON_SALE', features: { maxAmountMinor: 30000000 }, sortOrder: 52 },
  { id: 'prod_loan_zhuangxiu', category: 'LOAN', name: '装修分期', issuer: '中国建设银行', productCode: 'LOAN-ZX', riskLevel: 'NA', minPurchaseMinor: 200000, minIncrementMinor: 1000, termType: 'FIXED', termDays: 720, annualRateBp: 216, rateType: 'APR', rateNote: '月费率 0.18% 起，2万-100万，6-60 期，指定装修类商户消费', purchaseChannel: 'BOTH', status: 'ON_SALE', features: { monthlyFeeRate: '0.18%/月起', maxAmountMinor: 100000000, terms: '6-60期' }, sortOrder: 53 },
  { id: 'prod_loan_auto', category: 'LOAN', name: '龙卡信用卡汽车分期', issuer: '中国建设银行', productCode: 'LOAN-AUTO', riskLevel: 'NA', minPurchaseMinor: 2000000, minIncrementMinor: 1000, termType: 'FIXED', termDays: 1080, annualRateBp: 300, rateType: 'APR', rateNote: '信用卡专项分期额度，首年费率优惠，12/24/36 期可选', purchaseChannel: 'BOTH', status: 'ON_SALE', features: {}, sortOrder: 54 },
  // 信用卡产品
  { id: 'prod_cc_global', category: 'CREDIT_CARD', name: '龙卡全球支付信用卡（白金卡）', issuer: '中国建设银行', productCode: 'CC-GLOBAL-PLATINUM', riskLevel: 'NA', minPurchaseMinor: 0, minIncrementMinor: 0, termType: 'NA', termDays: null, annualRateBp: null, rateType: 'NA', rateNote: 'VISA/万事达双标，境外消费返现 1%，免货币转换费；年费 580 元，刷满 3 笔免次年', purchaseChannel: 'BOTH', status: 'ON_SALE', features: { annualFeeMinor: 58000, overseasCashback: '1%', currencyFee: '免货币转换费' }, sortOrder: 60 },
  { id: 'prod_cc_digital', category: 'CREDIT_CARD', name: '龙卡数字信用卡', issuer: '中国建设银行', productCode: 'CC-DIGITAL', riskLevel: 'NA', minPurchaseMinor: 0, minIncrementMinor: 0, termType: 'NA', termDays: null, annualRateBp: null, rateType: 'NA', rateNote: '银联单标数字卡，即申即用，绑定移动支付', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { annualFeeMinor: 0 }, sortOrder: 61 },
  { id: 'prod_cc_bilibili', category: 'CREDIT_CARD', name: '龙卡bilibili联名信用卡', issuer: '中国建设银行', productCode: 'CC-BILIBILI', riskLevel: 'NA', minPurchaseMinor: 0, minIncrementMinor: 0, termType: 'NA', termDays: null, annualRateBp: null, rateType: 'NA', rateNote: '银联单标，B站大会员/充值立减、消费享积分', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { annualFeeMinor: 0, perks: 'B站会员权益' }, sortOrder: 62 },
  // 信用卡服务业务
  { id: 'prod_ccs_bill', category: 'CREDIT_CARD_SERVICE', name: '账单分期', issuer: '中国建设银行', productCode: 'CCS-BILL-INST', riskLevel: 'NA', minPurchaseMinor: 30000, minIncrementMinor: 0, termType: 'NA', termDays: null, annualRateBp: null, rateType: 'APR', rateNote: '3/6/12/18/24 期，每期手续费率 0.60% 起；2026 年底前新办账单分期享财政贴息年化 1 个百分点', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { terms: [3, 6, 12, 18, 24], monthlyFeeRate: '0.60%起', subsidy: '2026年底前贴息1个百分点' }, sortOrder: 63 },
  { id: 'prod_ccs_cash', category: 'CREDIT_CARD_SERVICE', name: '现金分期', issuer: '中国建设银行', productCode: 'CCS-CASH-INST', riskLevel: 'NA', minPurchaseMinor: 20000, minIncrementMinor: 0, termType: 'NA', termDays: null, annualRateBp: null, rateType: 'APR', rateNote: '转入本行借记卡使用，3/6/12/18/24 期，每期手续费率 0.65% 起', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { terms: [3, 6, 12, 18, 24] }, sortOrder: 64 },
  { id: 'prod_ccs_autorepay', category: 'CREDIT_CARD_SERVICE', name: '自动还款（全额/最低）', issuer: '中国建设银行', productCode: 'CCS-AUTOREPAY', riskLevel: 'NA', minPurchaseMinor: 0, minIncrementMinor: 0, termType: 'NA', termDays: null, annualRateBp: null, rateType: 'NA', rateNote: '绑定本行借记卡，到期自动扣款，可选全额还款或最低还款额（账单金额 10%）', purchaseChannel: 'MOBILE_APP', status: 'ON_SALE', features: { minRepaymentRatio: '10%' }, sortOrder: 65 },
];

/* ------------------------------------------------------------------ */
/* 确定性随机工具                                                      */
/* ------------------------------------------------------------------ */

function mulberry32(seedValue) {
  let a = seedValue;
  return function next() {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function dayOfWeek(dateStr) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const days = Math.floor(Date.UTC(y, m - 1, d) / 86400000);
  return (days + 4) % 7; // 0=周日 ... 6=周六
}
function daysInMonth(yearMonth) {
  const [y, m] = yearMonth.split('-').map(Number);
  return new Date(Date.UTC(y, m, 0)).getUTCDate();
}
function pad(n) { return String(n).padStart(2, '0'); }
function iso(dayStr, time) { return dayStr + 'T' + time + '.000+08:00'; }
function addMonths(yearMonth, delta) {
  const [y, m] = yearMonth.split('-').map(Number);
  const total = y * 12 + (m - 1) + delta;
  return Math.floor(total / 12) + '-' + pad((total % 12) + 1);
}
function monthRange(fromYm, toYm) {
  const list = [];
  for (let ym = fromYm; ; ym = addMonths(ym, 1)) { list.push(ym); if (ym === toYm) break; }
  return list;
}
function withinWindow(dayStr) { return dayStr <= LAST_HISTORY_DAY; }
// 发薪日：28 日，周末顺延（2 月不足则提前到 27 日）
function paydayOf(yearMonth) {
  const last = daysInMonth(yearMonth);
  let day = 28;
  if (day > last) day = last;
  const dow = dayOfWeek(yearMonth + '-' + pad(day));
  if (dow === 6) day = yearMonth.endsWith('-02') ? 27 : 30;
  else if (dow === 0) day = 29;
  return yearMonth + '-' + pad(day);
}

/* ------------------------------------------------------------------ */
/* 等额本息工具                                                        */
/* ------------------------------------------------------------------ */

function equalPaymentMinor(principalMinor, annualBp, months) {
  const r = annualBp / 10000 / 12;
  const factor = Math.pow(1 + r, months);
  return Math.round(principalMinor * r * factor / (factor - 1));
}

/**
 * 按利率阶梯摊还，返回每期 { periodNo, dueDate, principalMinor, interestMinor, totalMinor }
 */
function amortize(principalMinor, firstPeriodYm, paymentDay, termMonths, rateSteps) {
  let balance = principalMinor;
  let payment = null;
  let rateBp = null;
  const rows = [];
  for (let i = 0; i < termMonths; i += 1) {
    const ym = addMonths(firstPeriodYm, i);
    const step = rateSteps.find(item => item.from === ym);
    if (step || rateBp === null) {
      if (step) rateBp = step.rateBp;
      payment = equalPaymentMinor(balance, rateBp, termMonths - i);
    }
    const interest = Math.round(balance * rateBp / 10000 / 12);
    let principal = payment - interest;
    if (i === termMonths - 1 || principal >= balance) principal = balance;
    balance -= principal;
    rows.push({
      periodNo: i + 1,
      dueDate: ym + '-' + pad(paymentDay),
      principalMinor: principal,
      interestMinor: interest,
      totalMinor: principal + interest,
    });
  }
  return { rows, remainingBalanceMinor: 0, finalPaymentMinor: payment, finalRateBp: rateBp };
}

/* ------------------------------------------------------------------ */
/* 信用卡账单                                                          */
/* ------------------------------------------------------------------ */

const CARD_GLOBAL = 'credit_global_8891';   // 账单日 5，到期日 18
const CARD_DIGITAL = 'credit_digital_2268'; // 未激活，无流水
const CARD_BILIBILI = 'credit_bili_6612';   // 账单日 22，到期日 10

// ------------------------------------------------------------------
// 事件构建（先全部生成，再按时间排序、两遍走账）
// ------------------------------------------------------------------

function buildWealthSeed(autoSaveMinor) {
  const rng = mulberry32(0x5eed2026);
  function pick(min, max) { return Math.round(min + (max - min) * rng()); }
  function pickItem(list) { return list[Math.floor(rng() * list.length)]; }
  const events = [];   // { at, dayStr, time, accountId, direction, amountMinor, businessType, category, counterparty, description, channel, ledgerGroup }
  let eventSeq = 0;
  function push(dayStr, time, accountId, direction, amountMinor, businessType, category, counterparty, description, channel, ledgerGroup) {
    eventSeq += 1;
    events.push({
      at: iso(dayStr, time), dayStr, accountId, direction, amountMinor,
      businessType, category, counterparty, description, channel,
      ledgerGroup: ledgerGroup || null, seq: eventSeq,
    });
  }
  /** 工资卡 -> 储蓄卡 内部互转（单一账务事务，两条流水） */
  function transferOwn(dayStr, amountMinor, description) {
    const group = 'own_' + dayStr + '_' + (eventSeq + 1);
    push(dayStr, '09:00:00', SALARY_ACCOUNT, 'DEBIT', amountMinor, 'TRANSFER_OWN', 'TRANSFER_INTERNAL', CUSTOMER_ID, description, 'MOBILE_APP', group);
    push(dayStr, '09:00:01', SAVING_ACCOUNT, 'CREDIT', amountMinor, 'TRANSFER_OWN', 'TRANSFER_INTERNAL', CUSTOMER_ID, description, 'MOBILE_APP', group);
  }

  const months = monthRange('2025-09', '2026-08');

  // ---- 收入：工资 / 年终奖 / 报销 ----
  months.forEach(ym => {
    const amount = ym <= '2026-01' ? 1785000 : 1860000; // 2026-02 起调薪 4.2%
    const payday = paydayOf(ym);
    if (!withinWindow(payday)) return;
    push(payday, '08:30:00', SALARY_ACCOUNT, 'CREDIT', amount, 'SALARY', 'INCOME_SALARY', EMPLOYER, '代发工资', 'AUTO', null);
  });
  push('2026-01-15', '10:02:00', SALARY_ACCOUNT, 'CREDIT', 5580000, 'BONUS', 'INCOME_BONUS', EMPLOYER, '年终奖金（3个月）', 'AUTO', null);
  [
    ['2025-10-11', 235000], ['2026-04-09', 176000], ['2026-06-12', 89000],
  ].forEach(([day, amount]) => push(day, '14:20:00', SALARY_ACCOUNT, 'CREDIT', amount, 'TRANSFER_IN', 'INCOME_REIMBURSE', EMPLOYER, '差旅费报销', 'MOBILE_APP', null));

  // ---- 发薪理财：次月 1 日自动攒（工资卡 -> 储蓄卡，额度自动校准） ----
  months.forEach(ym => {
    if (ym === '2025-09') return;
    transferOwn(addMonths(ym, 0) + '-01', autoSaveMinor, '发薪自动攒-转入储蓄卡');
  });
  // 年终奖理财：2026-01-20 转存 30,000
  transferOwn('2026-01-20', 3000000, '年终奖转存-储蓄卡');

  // ---- 基金投资（工资卡）：定投 + 初始买入 + 一笔蓝筹 ----
  push('2025-09-02', '14:00:00', SALARY_ACCOUNT, 'DEBIT', 1000000, 'FUND_PURCHASE', 'FUND_INVEST', '华夏基金-建行代销', '基金申购-华夏沪深300ETF联接A（初始建仓）', 'MOBILE_APP', null);
  months.forEach(ym => {
    if (ym === '2025-09') return;
    const day = ym + '-02';
    if (!withinWindow(day)) return;
    push(day, '14:00:00', SALARY_ACCOUNT, 'DEBIT', 100000, 'FUND_PURCHASE', 'FUND_INVEST', '华夏基金-建行代销', '基金定投扣款-华夏沪深300ETF联接A', 'AUTO', null);
  });
  push('2025-10-14', '15:30:00', SALARY_ACCOUNT, 'DEBIT', 800000, 'FUND_PURCHASE', 'FUND_INVEST', '易方达基金-建行代销', '基金申购-易方达蓝筹精选混合', 'MOBILE_APP', null);

  // ---- 房贷（工资卡，每月 15 日扣款，摊还明细见 loans） ----
  const mortgage = amortize(120000000, '2023-07', 15, 300, [
    { from: '2023-07', rateBp: 420 },
    { from: '2024-10', rateBp: 390 }, // 2024-10 存量房贷批量下调
    { from: '2025-01', rateBp: 330 }, // LPR 重定价
    { from: '2026-01', rateBp: 320 }, // 2025-05 LPR 下调后重定价
  ]);
  mortgage.rows.filter(row => row.dueDate >= '2025-09-01' && withinWindow(row.dueDate)).forEach(row => {
    push(row.dueDate, '06:00:00', SAVING_ACCOUNT, 'DEBIT', row.totalMinor, 'LOAN_REPAYMENT', 'HOUSING_LOAN', '中国建设银行-个人贷款', '个人住房贷款月供（第' + row.periodNo + '期）', 'AUTO', null);
  });

  // ---- 信用快贷：2026-02-10 放款 3 万，等额本息 12 期 ----
  const kuaidai = amortize(3000000, '2026-03', 10, 12, [{ from: '2026-03', rateBp: 324 }]);
  push('2026-02-10', '10:30:00', SALARY_ACCOUNT, 'CREDIT', 3000000, 'LOAN_DISBURSE', 'INCOME_LOAN', '中国建设银行-信用快贷', '信用快贷放款入账', 'MOBILE_APP', null);
  push('2026-02-12', '20:15:00', SALARY_ACCOUNT, 'DEBIT', 1870000, 'CONSUMPTION', 'SHOPPING', '京东商城-自营', '家电以旧换新（冰箱/洗衣机）', 'MOBILE_APP', null);
  push('2026-02-14', '21:05:00', SALARY_ACCOUNT, 'DEBIT', 1130000, 'CONSUMPTION', 'SHOPPING', 'Apple Store', 'Apple 产品购买（快贷资金用途）', 'MOBILE_APP', null);
  kuaidai.rows.filter(row => withinWindow(row.dueDate)).forEach(row => {
    push(row.dueDate, '06:00:00', SALARY_ACCOUNT, 'DEBIT', row.totalMinor, 'LOAN_REPAYMENT', 'CONSUMER_LOAN', '中国建设银行-信用快贷', '信用快贷月供（第' + row.periodNo + '期）', 'AUTO', null);
  });

  // ---- 理财（储蓄卡）：申赎与到期 ----
  push('2025-09-05', '09:30:00', SAVING_ACCOUNT, 'CREDIT', 3015000, 'WEALTH_MATURITY', 'INCOME_WEALTH', '中国建设银行', '整存整取6个月到期本息（2025-03-05 存入，利率1.00%）', 'AUTO', null);
  push('2025-09-08', '10:00:00', SAVING_ACCOUNT, 'DEBIT', 3000000, 'WEALTH_PURCHASE', 'WEALTH_INVEST', '建信理财', '申购-天天利（龙钱宝）现金管理类10号', 'MOBILE_APP', null);
  push('2025-11-18', '09:40:00', SAVING_ACCOUNT, 'CREDIT', 5026900, 'WEALTH_REDEMPTION', 'INCOME_WEALTH', '中国建设银行', '乾元-日鑫月益开放期赎回本息（2025-08-15 申购，年化2.15%）', 'MOBILE_APP', null);
  push('2025-11-20', '11:00:00', SAVING_ACCOUNT, 'DEBIT', 5000000, 'WEALTH_PURCHASE', 'WEALTH_INVEST', '中国建设银行', '存入-整存整取1年（利率0.95%，2026-11-20 到期）', 'MOBILE_APP', null);
  push('2026-02-27', '10:10:00', SAVING_ACCOUNT, 'CREDIT', 3018500, 'WEALTH_REDEMPTION', 'INCOME_WEALTH', '建信理财', '赎回-天天利（龙钱宝）现金管理类10号（含收益1,850）', 'MOBILE_APP', null);
  push('2026-03-02', '10:05:00', SAVING_ACCOUNT, 'CREDIT', 6089300, 'WEALTH_REDEMPTION', 'INCOME_WEALTH', '中国建设银行', '部分赎回-乾元-安鑫开放式固收（含收益8,930）', 'MOBILE_APP', null);
  push('2026-03-16', '10:20:00', SAVING_ACCOUNT, 'DEBIT', 20000000, 'WEALTH_PURCHASE', 'WEALTH_INVEST', '中国建设银行', '认购-个人大额存单3年（利率1.55%，按月付息）', 'MOBILE_APP', null);
  monthRange('2026-04', '2026-08').forEach(ym => {
    push(ym + '-20', '08:00:00', SAVING_ACCOUNT, 'CREDIT', 25833, 'INTEREST', 'INCOME_INTEREST', '中国建设银行', '大额存单3年-月度派息', 'AUTO', null);
  });

  // ---- 生活消费（工资卡，小额快捷支付为主） ----
  const dining = [
    ['美团-外卖订单', 2500, 7800], ['饿了么-外卖', 2000, 6500], ['罗森便利店', 800, 3500],
    ['星巴克', 3000, 4200], ['瑞幸咖啡', 990, 1990], ['大众点评-餐饮', 4500, 15800], ['肯德基宅急送', 2900, 5900],
  ];
  const shopping = [
    ['盒马鲜生', 3500, 16800], ['京东商城-自营', 4800, 39000], ['天猫超市', 2900, 12800],
    ['山姆会员商店', 12000, 45000], ['网易严选', 5800, 16800],
  ];
  months.forEach(ym => {
    const dim = daysInMonth(ym);
    // 话费
    if (withinWindow(ym + '-05')) push(ym + '-05', '09:15:00', SALARY_ACCOUNT, 'DEBIT', 12800, 'TELECOM', 'TELECOM', '中国移动上海公司', '话费-5G畅享套餐', 'AUTO', null);
    // 爱奇艺连续包月
    if (withinWindow(ym + '-03')) push(ym + '-03', '07:00:00', SALARY_ACCOUNT, 'DEBIT', 2500, 'CONSUMPTION', 'ENTERTAINMENT', '爱奇艺', '爱奇艺VIP连续包月', 'QUICK_PAY', null);
    // 双月抄表：电费/水费（单月）、燃气（双月）
    const m = Number(ym.split('-')[1]);
    if (m % 2 === 1) {
      if (withinWindow(ym + '-14')) push(ym + '-14', '10:30:00', SALARY_ACCOUNT, 'DEBIT', pick(8000, 16000), 'UTILITIES', 'UTILITIES', '国网上海电力', '电费（双月抄表）', 'MOBILE_APP', null);
      if (withinWindow(ym + '-16')) push(ym + '-16', '10:30:00', SALARY_ACCOUNT, 'DEBIT', pick(3000, 4600), 'UTILITIES', 'UTILITIES', '上海市自来水供水公司', '水费（双月抄表）', 'MOBILE_APP', null);
    } else if (withinWindow(ym + '-18')) {
      push(ym + '-18', '10:30:00', SALARY_ACCOUNT, 'DEBIT', pick(2000, 3600), 'UTILITIES', 'UTILITIES', '上海燃气有限公司', '燃气费（双月抄表）', 'MOBILE_APP', null);
    }
    // 地铁通勤
    for (let i = 0; i < 5; i += 1) {
      const day = ym + '-' + pad(2 + Math.floor(rng() * (dim - 2)));
      if (!withinWindow(day)) continue;
      push(day, pad(8 + Math.floor(rng() * 3)) + ':' + pad(Math.floor(rng() * 60)) + ':00', SALARY_ACCOUNT, 'DEBIT', pick(300, 900), 'CONSUMPTION', 'TRANSPORT', '上海地铁', '交通卡充值-地铁通勤', 'QUICK_PAY', null);
    }
    // 餐饮
    const diningCount = 8 + Math.floor(rng() * 4);
    for (let i = 0; i < diningCount; i += 1) {
      const [name, min, max] = pickItem(dining);
      const day = ym + '-' + pad(1 + Math.floor(rng() * dim));
      if (!withinWindow(day)) continue;
      push(day, pad(11 + Math.floor(rng() * 10)) + ':' + pad(Math.floor(rng() * 60)) + ':00', SALARY_ACCOUNT, 'DEBIT', pick(min, max), 'CONSUMPTION', 'FOOD_DINING', name.split('-')[0], name, 'QUICK_PAY', null);
    }
    // 购物
    const shoppingCount = 2 + Math.floor(rng() * 3);
    for (let i = 0; i < shoppingCount; i += 1) {
      const [name, min, max] = pickItem(shopping);
      const day = ym + '-' + pad(1 + Math.floor(rng() * dim));
      if (!withinWindow(day)) continue;
      push(day, pad(10 + Math.floor(rng() * 12)) + ':' + pad(Math.floor(rng() * 60)) + ':00', SALARY_ACCOUNT, 'DEBIT', pick(min, max), 'CONSUMPTION', 'GROCERIES', name, '日常购物-' + name, 'QUICK_PAY', null);
    }
  });
  // 特殊消费事件（工资卡）
  [
    ['2025-10-03', '三亚湾景区-游玩', 108000, 'TRAVEL'],
    ['2025-11-11', '京东商城-自营（双11）', 620000, 'SHOPPING'],
    ['2026-01-22', '年货节采购（盒马/山姆）', 250000, 'GROCERIES'],
    ['2026-02-15', '跨行转账-春节红包（母亲）', 300000, 'FAMILY_SUPPORT'],
    ['2026-05-02', '五一周边游-莫干山民宿', 260000, 'TRAVEL'],
    ['2026-06-18', '京东商城-自营（618）', 380000, 'SHOPPING'],
    ['2026-07-11', '益丰大药房-体检用药', 86000, 'MEDICAL'],
  ].forEach(([day, name, amount, category]) => {
    if (!withinWindow(day)) return;
    const businessType = category === 'FAMILY_SUPPORT' ? 'TRANSFER_OUT' : 'CONSUMPTION';
    push(day, '16:30:00', SALARY_ACCOUNT, 'DEBIT', amount, businessType, category, category === 'FAMILY_SUPPORT' ? '王秀兰（母）' : name.split('-')[0], name, businessType === 'TRANSFER_OUT' ? 'MOBILE_APP' : 'QUICK_PAY', null);
  });

  // ---- 信用卡消费与还款（全球支付卡 + bilibili 卡） ----
  const cardTxns = [];
  function cardSpend(cardId, dayStr, time, amountMinor, counterparty, description) {
    cardTxns.push({ cardId, dayStr, time, amountMinor, counterparty, description, direction: 'DEBIT' });
  }
  // 全球支付卡：月度消费 + 大额出行；8 月（8/6-8/26）固定凑至未还 14,220 元（与原 seed 卡额度一致）
  const globalAugFixed = [
    ['2026-08-07', '12:10', 8650, '美团-外卖订单'],
    ['2026-08-09', '19:40', 12900, '网易严选'],
    ['2026-08-12', '21:15', 39900, '京东商城-自营'],
    ['2026-08-14', '16:20', 798000, '中国东方航空-机票（国庆出行预订）'],
    ['2026-08-15', '11:05', 320000, '携程-酒店预订'],
    ['2026-08-16', '12:35', 4520, '饿了么-外卖'],
    ['2026-08-18', '09:50', 6800, '星巴克'],
    ['2026-08-20', '18:25', 15680, '大众点评-餐饮'],
    ['2026-08-21', '20:40', 128800, '京东家电-以旧换新'],
    ['2026-08-22', '15:10', 8900, '天猫超市'],
    ['2026-08-24', '12:55', 7250, '饿了么-外卖'],
    ['2026-08-25', '17:30', 10800, '盒马鲜生'],
  ];
  months.forEach(ym => {
    if (ym === '2026-08') { globalAugFixed.forEach(([day, time, amount, name]) => cardSpend(CARD_GLOBAL, day, time, amount, name.split('-')[0], name)); return; }
    const dim = daysInMonth(ym);
    // 国庆出行（2025-10）
    if (ym === '2025-10') {
      cardSpend(CARD_GLOBAL, '2025-10-01', '08:45', 420000, '中国东方航空', '机票-上海往返三亚');
      cardSpend(CARD_GLOBAL, '2025-10-02', '14:20', 360000, '亚朵酒店', '三亚湾酒店住宿3晚');
    }
    if (ym === '2026-02') cardSpend(CARD_GLOBAL, '2026-02-08', '19:10', 188000, '山姆会员商店', '年货采购（信用卡支付）');
    const count = 10 + Math.floor(rng() * 5);
    for (let i = 0; i < count; i += 1) {
      const [name, min, max] = pickItem(dining.concat(shopping));
      const day = ym + '-' + pad(1 + Math.floor(rng() * dim));
      cardSpend(CARD_GLOBAL, day, pad(10 + Math.floor(rng() * 12)) + ':' + pad(Math.floor(rng() * 60)), pick(min, Math.min(max, 20000)), name.split('-')[0], name);
    }
  });
  // bilibili 卡：小额 B 站消费 + 8 月当期未还 237 元
  months.forEach(ym => {
    if (ym === '2026-08') {
      cardSpend(CARD_BILIBILI, '2026-08-23', '21:00', 14800, '哔哩哔哩', 'B站大会院年卡');
      cardSpend(CARD_BILIBILI, '2026-08-25', '20:30', 8900, '哔哩哔哩', 'B站渠道充值');
      return;
    }
    const dim = daysInMonth(ym);
    cardSpend(CARD_BILIBILI, ym + '-' + pad(3 + Math.floor(rng() * (dim - 3))), '21:30', pick(2500, 6800), '哔哩哔哩', 'B站会员/充电');
  });

  // 账单与自动还款（消费与工资卡/卡账户两侧一致）
  const allCardSpends = cardTxns
    .slice()
    .sort((a, b) => (a.dayStr + a.time < b.dayStr + b.time ? -1 : 1));
  function billOf(cardId, statementDay /* 'YYYY-MM-DD' 账单日 */) {
    // 账期：上一账单日次日 ~ 本账单日
    const periodStart = addMonths(statementDay.slice(0, 7), -1) + '-' + pad(Number(statementDay.slice(8, 10)) + 1);
    return allCardSpends
      .filter(item => item.cardId === cardId && item.dayStr > periodStart && item.dayStr <= statementDay)
      .reduce((sum, item) => sum + item.amountMinor, 0);
  }
  monthRange('2025-10', '2026-08').forEach(ym => {
    // 全球支付卡：账单日 5 -> 还款日 17（到期 18 前），当月 17 日在窗口内则还款
    const repayDay = ym + '-17';
    if (withinWindow(repayDay)) {
      const bill = billOf(CARD_GLOBAL, ym + '-05');
      if (bill > 0) {
        push(repayDay, '06:30:00', SALARY_ACCOUNT, 'DEBIT', bill, 'CREDIT_CARD_REPAYMENT', 'CREDIT_CARD', '中国建设银行信用卡', '信用卡自动还款-龙卡全球支付卡（全额）', 'AUTO', null);
        cardTxns.push({ cardId: CARD_GLOBAL, dayStr: repayDay, time: '06:30', amountMinor: bill, counterparty: '中国建设银行信用卡', description: '自动还款-全额还款（账单日5日）', direction: 'CREDIT' });
      }
    }
    // bilibili 卡：账单日 22（上月）-> 还款日 9
    const repayDayBili = ym + '-09';
    if (withinWindow(repayDayBili)) {
      const bill = billOf(CARD_BILIBILI, addMonths(ym, -1) + '-22');
      if (bill > 0) {
        push(repayDayBili, '06:30:00', SALARY_ACCOUNT, 'DEBIT', bill, 'CREDIT_CARD_REPAYMENT', 'CREDIT_CARD', '中国建设银行信用卡', '信用卡自动还款-龙卡bilibili联名卡（全额）', 'AUTO', null);
        cardTxns.push({ cardId: CARD_BILIBILI, dayStr: repayDayBili, time: '06:30', amountMinor: bill, counterparty: '中国建设银行信用卡', description: '自动还款-全额还款（账单日22日）', direction: 'CREDIT' });
      }
    }
  });

  // ---- 排序 + 两遍走账 ----
  events.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : a.seq - b.seq));

  const net = { [SALARY_ACCOUNT]: 0, [SAVING_ACCOUNT]: 0 };
  events.forEach(e => { net[e.accountId] += e.direction === 'CREDIT' ? e.amountMinor : -e.amountMinor; });
  const openingSalary = TARGET_SALARY_END - net[SALARY_ACCOUNT];
  const openingSaving = TARGET_SAVING_END - net[SAVING_ACCOUNT];
  if (openingSalary < 1000000 || openingSaving < 500000) return null;

  const balances = { [SALARY_ACCOUNT]: openingSalary, [SAVING_ACCOUNT]: openingSaving };
  const accountTransactions = [];
  const ledgerTransactions = [];
  const ledgerEntries = [];
  let ledgerSeq = 0;
  let txnSeq = 0;
  const INCOME_TYPES = new Set(['SALARY', 'BONUS', 'TRANSFER_IN', 'INTEREST', 'WEALTH_MATURITY', 'WEALTH_REDEMPTION', 'LOAN_DISBURSE']);

  function ledgerPost(groupId, entriesDef, businessType, businessId, postedAt) {
    if (ledgerTransactions.some(item => item.id === groupId)) return;
    ledgerSeq += 1;
    ledgerTransactions.push({ id: groupId, businessType, businessId, currency: 'CNY', postedAt });
    entriesDef.forEach((def, index) => {
      ledgerEntries.push({
        id: groupId + '_e' + (index + 1),
        ledgerTransactionId: groupId, accountId: def.accountId,
        direction: def.direction, amountMinor: def.amountMinor, description: def.description,
      });
    });
  }

  // 期初（替代原 seed 的 ledger_opening）
  ledgerPost('ledger_opening', [
    { accountId: SALARY_ACCOUNT, direction: 'CREDIT', amountMinor: openingSalary, description: '期初余额' },
    { accountId: SAVING_ACCOUNT, direction: 'CREDIT', amountMinor: openingSaving, description: '期初余额' },
    { accountId: EQUITY_ACCOUNT, direction: 'DEBIT', amountMinor: openingSalary + openingSaving, description: '期初平衡' },
  ], 'OPENING', 'seed-opening-v2', HISTORY_OPENING_AT);

  const seenGroups = new Set();
  const walkBalances = { [SALARY_ACCOUNT]: openingSalary, [SAVING_ACCOUNT]: openingSaving };
  const undersupplied = events.some(e => {
    walkBalances[e.accountId] += e.direction === 'CREDIT' ? e.amountMinor : -e.amountMinor;
    return walkBalances[e.accountId] < 0;
  });
  if (undersupplied) return null;
  events.forEach(e => {
    balances[e.accountId] += e.direction === 'CREDIT' ? e.amountMinor : -e.amountMinor;
    txnSeq += 1;
    const txnId = 'atxn_' + e.dayStr.replace(/-/g, '') + '_' + pad(txnSeq);
    accountTransactions.push({
      id: txnId, accountId: e.accountId, customerId: CUSTOMER_ID,
      direction: e.direction, amountMinor: e.amountMinor, currency: 'CNY',
      businessType: e.businessType, category: e.category,
      counterparty: e.counterparty, description: e.description, channel: e.channel,
      balanceAfterMinor: balances[e.accountId], occurredAt: e.at,
    });
    if (e.ledgerGroup) {
      if (!seenGroups.has(e.ledgerGroup)) {
        seenGroups.add(e.ledgerGroup);
        const pair = events.filter(item => item.ledgerGroup === e.ledgerGroup);
        const out = pair.find(item => item.direction === 'DEBIT');
        const inflow = pair.find(item => item.direction === 'CREDIT');
        ledgerPost('lg_' + e.dayStr.replace(/-/g, '') + '_' + pad(ledgerSeq + 1), [
          { accountId: out.accountId, direction: 'DEBIT', amountMinor: out.amountMinor, description: out.description },
          { accountId: inflow.accountId, direction: 'CREDIT', amountMinor: inflow.amountMinor, description: inflow.description },
        ], 'TRANSFER_OWN', txnId, e.at);
      }
      return;
    }
    const counterpartyAccount = INCOME_TYPES.has(e.businessType) ? EQUITY_ACCOUNT : CLEARING_ACCOUNT;
    const counterpartyDirection = e.direction === 'CREDIT' ? 'DEBIT' : 'CREDIT';
    ledgerPost('lg_' + e.dayStr.replace(/-/g, '') + '_' + pad(ledgerSeq + 1), [
      { accountId: e.accountId, direction: e.direction, amountMinor: e.amountMinor, description: e.description },
      { accountId: counterpartyAccount, direction: counterpartyDirection, amountMinor: e.amountMinor, description: e.description + '-清算' },
    ], e.businessType, txnId, e.at);
  });

  // ---- 理财持仓与申赎记录 ----
  const VALUED_AT = '2026-08-26T18:00:00.000+08:00';
  const wealthHoldings = [
    { id: 'wh_lqb10', productId: 'prod_w_lqb10', productName: '建信理财天天利（龙钱宝）现金管理类产品10号', category: 'CASH_MANAGEMENT', accountId: SAVING_ACCOUNT, principalMinor: 2000000, currentValueMinor: 2002650, accumulatedEarningsMinor: 2650, status: 'HOLDING', openedAt: '2023-05-10T10:00:00.000+08:00', maturesAt: null },
    { id: 'wh_anxin', productId: 'prod_w_anxin', productName: '乾元-安鑫（按日）开放式固定收益类理财产品', category: 'FIXED_INCOME_WEALTH', accountId: SAVING_ACCOUNT, principalMinor: 800000, currentValueMinor: 812470, accumulatedEarningsMinor: 12470, status: 'HOLDING', openedAt: '2024-06-20T14:00:00.000+08:00', maturesAt: null },
    { id: 'wh_cd_3y', productId: 'prod_cd_3y', productName: '个人大额存单3年（2026年第7期）', category: 'CERTIFICATE_OF_DEPOSIT', accountId: SAVING_ACCOUNT, principalMinor: 20000000, currentValueMinor: 20000000, accumulatedEarningsMinor: 129165, status: 'HOLDING', openedAt: '2026-03-16T10:20:00.000+08:00', maturesAt: '2029-03-16T00:00:00.000+08:00' },
    { id: 'wh_dep_1y', productId: 'prod_dep_1y', productName: '整存整取1年', category: 'DEPOSIT', accountId: SAVING_ACCOUNT, principalMinor: 5000000, currentValueMinor: 5000000, accumulatedEarningsMinor: 0, status: 'HOLDING', openedAt: '2025-10-03T11:00:00.000+08:00', maturesAt: '2026-10-03T00:00:00.000+08:00' },
    { id: 'wh_fund_hs300', productId: 'prod_fund_000051', productName: '华夏沪深300ETF联接A', category: 'FUND', accountId: SALARY_ACCOUNT, principalMinor: 2200000, currentValueMinor: 2376440, accumulatedEarningsMinor: 176440, status: 'HOLDING', openedAt: '2025-09-02T14:00:00.000+08:00', maturesAt: null },
    { id: 'wh_fund_lanchou', productId: 'prod_fund_005827', productName: '易方达蓝筹精选混合', category: 'FUND', accountId: SALARY_ACCOUNT, principalMinor: 800000, currentValueMinor: 767200, accumulatedEarningsMinor: -32800, status: 'HOLDING', openedAt: '2025-10-14T15:30:00.000+08:00', maturesAt: null },
    // 已了结
    { id: 'wh_dep_6m_settled', productId: 'prod_dep_t6m', productName: '整存整取6个月', category: 'DEPOSIT', accountId: SAVING_ACCOUNT, principalMinor: 3000000, currentValueMinor: 0, accumulatedEarningsMinor: 15000, status: 'SETTLED', openedAt: '2025-03-05T09:00:00.000+08:00', maturesAt: '2025-09-05T00:00:00.000+08:00' },
    { id: 'wh_rixyy_redeemed', productId: 'prod_w_rixyy', productName: '乾元-日鑫月益（按月开放）资产组合型理财产品', category: 'FIXED_INCOME_WEALTH', accountId: SAVING_ACCOUNT, principalMinor: 5000000, currentValueMinor: 0, accumulatedEarningsMinor: 26900, status: 'REDEEMED', openedAt: '2025-08-15T10:00:00.000+08:00', maturesAt: null },
  ].map(item => Object.assign(item, { customerId: CUSTOMER_ID, currency: 'CNY', lastValuedAt: VALUED_AT }));

  const wealthTransactions = [
    { id: 'wtxn_dep6m_maturity', productId: 'prod_dep_t6m', productName: '整存整取6个月', accountId: SAVING_ACCOUNT, direction: 'MATURITY', amountMinor: 3000000, earningsMinor: 15000, occurredAt: '2025-09-05T09:30:00.000+08:00' },
    { id: 'wtxn_lqb10_buy', productId: 'prod_w_lqb10', productName: '建信理财天天利（龙钱宝）现金管理类产品10号', accountId: SAVING_ACCOUNT, direction: 'PURCHASE', amountMinor: 3000000, earningsMinor: 0, occurredAt: '2025-09-08T10:00:00.000+08:00' },
    { id: 'wtxn_rixyy_redeem', productId: 'prod_w_rixyy', productName: '乾元-日鑫月益（按月开放）资产组合型理财产品', accountId: SAVING_ACCOUNT, direction: 'REDEMPTION', amountMinor: 5000000, earningsMinor: 26900, occurredAt: '2025-11-18T09:40:00.000+08:00' },
    { id: 'wtxn_dep1y_buy', productId: 'prod_dep_1y', productName: '整存整取1年', accountId: SAVING_ACCOUNT, direction: 'PURCHASE', amountMinor: 5000000, earningsMinor: 0, occurredAt: '2025-11-20T11:00:00.000+08:00' },
    { id: 'wtxn_lqb10_redeem', productId: 'prod_w_lqb10', productName: '建信理财天天利（龙钱宝）现金管理类产品10号', accountId: SAVING_ACCOUNT, direction: 'REDEMPTION', amountMinor: 3000000, earningsMinor: 1850, occurredAt: '2026-02-27T10:10:00.000+08:00' },
    { id: 'wtxn_anxin_redeem', productId: 'prod_w_anxin', productName: '乾元-安鑫（按日）开放式固定收益类理财产品', accountId: SAVING_ACCOUNT, direction: 'REDEMPTION', amountMinor: 6000000, earningsMinor: 8930, occurredAt: '2026-03-02T10:05:00.000+08:00' },
    { id: 'wtxn_cd3y_buy', productId: 'prod_cd_3y', productName: '个人大额存单3年（2026年第7期）', accountId: SAVING_ACCOUNT, direction: 'PURCHASE', amountMinor: 20000000, earningsMinor: 0, occurredAt: '2026-03-16T10:20:00.000+08:00' },
    { id: 'wtxn_hs300_init', productId: 'prod_fund_000051', productName: '华夏沪深300ETF联接A', accountId: SALARY_ACCOUNT, direction: 'PURCHASE', amountMinor: 1000000, earningsMinor: 0, occurredAt: '2025-09-02T14:00:00.000+08:00' },
    { id: 'wtxn_lanchou_buy', productId: 'prod_fund_005827', productName: '易方达蓝筹精选混合', accountId: SALARY_ACCOUNT, direction: 'PURCHASE', amountMinor: 800000, earningsMinor: 0, occurredAt: '2025-10-14T15:30:00.000+08:00' },
  ];
  months.forEach(ym => {
    if (ym === '2025-09') return;
    if (!withinWindow(ym + '-02')) return;
    wealthTransactions.push({
      id: 'wtxn_hs300_dca_' + ym.replace('-', ''), productId: 'prod_fund_000051', productName: '华夏沪深300ETF联接A',
      accountId: SALARY_ACCOUNT, direction: 'PURCHASE', amountMinor: 100000, earningsMinor: 0,
      occurredAt: iso(ym + '-02', '14:00:00'),
    });
  });
  monthRange('2026-04', '2026-08').forEach(ym => {
    wealthTransactions.push({
      id: 'wtxn_cd3y_interest_' + ym.replace('-', ''), productId: 'prod_cd_3y', productName: '个人大额存单3年（2026年第7期）',
      accountId: SAVING_ACCOUNT, direction: 'DIVIDEND', amountMinor: 25833, earningsMinor: 25833,
      occurredAt: iso(ym + '-20', '08:00:00'),
    });
  });
  wealthTransactions.forEach(item => {
    item.customerId = CUSTOMER_ID; item.currency = 'CNY'; item.status = 'CONFIRMED';
  });

  // ---- 贷款 ----
  const mortgagePaid = mortgage.rows.filter(row => row.dueDate <= LAST_HISTORY_DAY);
  const mortgageBalance = 120000000 - mortgagePaid.reduce((sum, row) => sum + row.principalMinor, 0);
  const mortgageCurrent = mortgage.rows[mortgagePaid.length - 1];
  const kuaidaiPaid = kuaidai.rows.filter(row => row.dueDate <= LAST_HISTORY_DAY);
  const kuaidaiBalance = 3000000 - kuaidaiPaid.reduce((sum, row) => sum + row.principalMinor, 0);
  const renovation = amortize(8000000, '2024-09', 15, 12, [{ from: '2024-09', rateBp: 345 }]);

  const loans = [
    {
      id: 'loan_mortgage_001', productId: 'prod_loan_mortgage', productName: '个人住房贷款',
      loanType: 'MORTGAGE', repaymentAccountId: SAVING_ACCOUNT, contractNo: 'HT-3100***8821',
      principalMinor: 120000000, outstandingPrincipalMinor: mortgageBalance,
      annualRateBp: mortgageCurrent ? 320 : 420, rateType: 'LPR_BASED',
      termMonths: 300, remainingMonths: 300 - mortgagePaid.length,
      repaymentMethod: 'EQUAL_PAYMENT', monthlyPaymentMinor: mortgageCurrent ? mortgageCurrent.totalMinor : 0,
      monthlyPaymentDay: 15, status: 'ACTIVE', disbursedAt: '2023-06-15T10:00:00.000+08:00', settledAt: null,
    },
    {
      id: 'loan_kuaidai_001', productId: 'prod_loan_kuaidai', productName: '信用快贷',
      loanType: 'CREDIT', repaymentAccountId: SALARY_ACCOUNT, contractNo: 'HT-3100***3042',
      principalMinor: 3000000, outstandingPrincipalMinor: kuaidaiBalance,
      annualRateBp: 324, rateType: 'FIXED',
      termMonths: 12, remainingMonths: 12 - kuaidaiPaid.length,
      repaymentMethod: 'EQUAL_PAYMENT', monthlyPaymentMinor: kuaidai.rows[0].totalMinor,
      monthlyPaymentDay: 10, status: 'ACTIVE', disbursedAt: '2026-02-10T10:30:00.000+08:00', settledAt: null,
    },
    {
      id: 'loan_renovation_001', productId: 'prod_loan_zhuangxiu', productName: '装修分期（信用贷）',
      loanType: 'CREDIT', repaymentAccountId: SALARY_ACCOUNT, contractNo: 'HT-3100***1177',
      principalMinor: 8000000, outstandingPrincipalMinor: 0,
      annualRateBp: 345, rateType: 'FIXED',
      termMonths: 12, remainingMonths: 0,
      repaymentMethod: 'EQUAL_PAYMENT', monthlyPaymentMinor: renovation.rows[0].totalMinor,
      monthlyPaymentDay: 15, status: 'SETTLED', disbursedAt: '2024-08-15T09:30:00.000+08:00', settledAt: '2025-08-15T06:00:00.000+08:00',
    },
  ].map(item => Object.assign(item, { customerId: CUSTOMER_ID, currency: 'CNY' }));

  // 还款计划仅保留：已还期次 + 未来一期（SCHEDULED），不预生成整个剩余期限
  const SCHEDULED_UNTIL = '2026-09-30';
  const loanRepayments = []
    .concat(mortgage.rows.filter(row => row.dueDate <= SCHEDULED_UNTIL).map(row => ({ loanId: 'loan_mortgage_001', row })))
    .concat(kuaidai.rows.filter(row => row.dueDate <= SCHEDULED_UNTIL).map(row => ({ loanId: 'loan_kuaidai_001', row })))
    .concat(renovation.rows.map(row => ({ loanId: 'loan_renovation_001', row })))
    .map(({ loanId, row }) => ({
      id: 'lrp_' + loanId + '_' + String(row.periodNo).padStart(3, '0'),
      loanId, customerId: CUSTOMER_ID, periodNo: row.periodNo,
      dueDate: iso(row.dueDate, '06:00:00'),
      principalMinor: row.principalMinor, interestMinor: row.interestMinor, totalMinor: row.totalMinor,
      status: row.dueDate <= LAST_HISTORY_DAY ? 'PAID' : 'SCHEDULED',
      paidAt: row.dueDate <= LAST_HISTORY_DAY ? iso(row.dueDate, '06:00:00') : null,
    }));

  // ---- 信用卡（第三张卡与一年流水） ----
  const creditCards = [
    {
      id: CARD_BILIBILI, customerId: CUSTOMER_ID, maskedNo: '**** **** **** 6612',
      productName: '龙卡bilibili联名信用卡', network: 'UNIONPAY', cardType: 'PHYSICAL',
      status: 'ACTIVE', totalLimitMinor: 1500000, availableLimitMinor: 1476300, cashLimitMinor: 450000,
      statementDay: 22, dueDay: 10, createdAt: '2025-06-18T10:00:00.000+08:00',
    },
  ];
  const creditCardSettings = [
    { cardId: CARD_BILIBILI, onlineTransactionsEnabled: true, contactlessEnabled: true, overseasEnabled: false, singleTransactionLimitMinor: 100000, dailyLimitMinor: 500000 },
  ];
  const creditCardTransactions = cardTxns
    .sort((a, b) => (a.dayStr + a.time < b.dayStr + b.time ? -1 : 1))
    .map((item, index) => ({
      id: 'ctxn_' + item.dayStr.replace(/-/g, '') + '_' + String(index + 1).padStart(4, '0'),
      cardId: item.cardId, direction: item.direction, amountMinor: item.amountMinor,
      currency: 'CNY', description: item.description, status: 'POSTED',
      occurredAt: iso(item.dayStr, item.time + ':00'),
    }));

  return {
    products: PRODUCTS,
    opening: { salaryMinor: openingSalary, savingMinor: openingSaving },
    accountTransactions,
    ledgerTransactions,
    ledgerEntries,
    wealthHoldings,
    wealthTransactions,
    loans,
    loanRepayments,
    creditCards,
    creditCardSettings,
    creditCardTransactions,
  };
}

/**
 * 自动校准「发薪自动攒」额度：从低到高尝试，取第一组满足
 * （期初余额充足 + 全程不穿仓）的方案，保证结果确定性可复现。
 */
function calibrateWealthSeed() {
  const attempts = [];
  for (let autoSaveMinor = 300000; autoSaveMinor <= 1600000; autoSaveMinor += 100000) {
    const candidate = buildWealthSeed(autoSaveMinor);
    if (candidate) { candidate.autoSaveMinor = autoSaveMinor; return candidate; }
    attempts.push(autoSaveMinor / 100);
  }
  throw new Error('发薪自动攒额度校准失败，已尝试（元）: ' + attempts.join(','));
}

const WEALTH_SEED = calibrateWealthSeed();

module.exports = { WEALTH_SEED, buildWealthSeed, calibrateWealthSeed };
