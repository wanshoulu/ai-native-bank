'use strict';

/**
 * 财富域服务：理财产品目录、持仓、申赎交易，以及发薪理财画像与推荐。
 * 遵循系统既有模式：moneyValue 金额投影、幂等重放、乐观锁余额、复式记账、审计。
 */

const crypto = require('node:crypto');
const { BankApiError, requireString, requirePositiveMinor } = require('./api-contract');
const { moneyValue } = require('./bank-service');
const { buildWealthAdvisoryPlans } = require('./wealth-advisory-planner');

const PURCHASABLE_STATUS = new Set(['ON_SALE', 'LIMITED']);
const MAX_PRODUCTS_PER_CATEGORY = 50;
const MAX_HOLDINGS = 32;
const RISK_LEVEL_RANK = Object.freeze({ R1: 1, R2: 2, R3: 3, R4: 4, R5: 5 });
const CUSTOMER_RISK_LIMIT = Object.freeze({ C1: 1, C2: 2, C3: 3, C4: 4, C5: 5 });

function createWealthService(options) {
  const opts = options || {};
  const repository = opts.repository;
  const bankService = opts.bankService;
  const now = typeof opts.now === 'function' ? opts.now : () => new Date().toISOString();
  if (!repository || !bankService) throw new Error('wealth service requires repository and bankService');

  const CATEGORY_WHITELIST = new Set([
    'DEPOSIT', 'CERTIFICATE_OF_DEPOSIT', 'CASH_MANAGEMENT', 'FIXED_INCOME_WEALTH', 'FUND',
  ]);

  /* ---------------- 只读查询 ---------------- */

  async function listProducts(filter) {
    const input = filter || {};
    if (input.category != null && !CATEGORY_WHITELIST.has(input.category)) {
      throw new BankApiError(422, 'validation_error', '产品类别无效', [{ field: 'category', code: 'invalid' }]);
    }
    const products = await repository.listFinancialProducts({ category: input.category || undefined });
    return products.slice(0, MAX_PRODUCTS_PER_CATEGORY).map(toPublicProduct);
  }

  async function getProduct(productId) {
    const product = await repository.getFinancialProduct(requireString(productId, 'productId', { max: 64 }));
    if (!product) throw new BankApiError(404, 'product_not_found', '产品不存在或已下架');
    return toPublicProduct(product);
  }

  async function listHoldings(customerId) {
    await bankService.getCustomer(customerId);
    const holdings = (await repository.listWealthHoldings(customerId)).filter(item => item.status === 'HOLDING');
    const totalValueMinor = holdings.reduce((sum, item) => sum + item.currentValueMinor, 0);
    const totalPrincipalMinor = holdings.reduce((sum, item) => sum + item.principalMinor, 0);
    const totalEarningsMinor = holdings.reduce((sum, item) => sum + item.accumulatedEarningsMinor, 0);
    return {
      holdingCount: holdings.length,
      totalValue: moneyValue(totalValueMinor, 'CNY'),
      totalPrincipal: moneyValue(totalPrincipalMinor, 'CNY'),
      totalEarnings: moneyValue(totalEarningsMinor, 'CNY'),
      holdings: holdings.map(toPublicHolding),
    };
  }

  async function listTransactions(customerId) {
    await bankService.getCustomer(customerId);
    const rows = await repository.listWealthTransactions(customerId);
    return {
      data: rows.map(item => ({
        id: item.id,
        productName: item.productName,
        direction: item.direction,
        amount: moneyValue(item.amountMinor, item.currency),
        earnings: moneyValue(item.earningsMinor, item.currency),
        status: item.status,
        occurredAt: item.occurredAt,
      })),
    };
  }

  /* ---------------- 发薪理财画像与推荐 ---------------- */

  async function loadCustomerMonthlyFlows(customerId) {
    const accounts = await bankService.listAccounts(customerId);
    const monthly = new Map();
    let salaryCount = 0;
    let salaryTotal = 0;
    let wealthInvestTotal = 0;
    for (const account of accounts) {
      try {
        let cursor = null;
        for (;;) {
          const page = await repository.listAccountTransactionDetails(customerId, account.id, { limit: 100, cursor });
          for (const item of page.data) {
            const month = String(item.occurredAt).slice(0, 7);
            const bucket = monthly.get(month) || { month, income: 0, expense: 0, salary: 0, wealthInvest: 0 };
            if (item.direction === 'CREDIT') bucket.income += item.amountMinor;
            else bucket.expense += item.amountMinor;
            if (item.businessType === 'SALARY' || item.businessType === 'BONUS') {
              bucket.salary += item.amountMinor;
              salaryCount += 1;
              salaryTotal += item.amountMinor;
            }
            if (item.businessType === 'WEALTH_PURCHASE' || item.businessType === 'FUND_PURCHASE') {
              bucket.wealthInvest += item.amountMinor;
              wealthInvestTotal += item.amountMinor;
            }
            monthly.set(month, bucket);
          }
          cursor = page.nextCursor;
          if (!cursor) break;
        }
      } catch (_err) {
        // 单个账户交易明细查询失败时跳过该账户，不影响其他账户的现金流聚合
      }
    }
    const months = [...monthly.values()].sort((a, b) => (a.month < b.month ? -1 : 1));
    return { months, salaryCount, salaryTotal, wealthInvestTotal };
  }

  async function getSalaryInsights(customerId) {
    await bankService.getCustomer(customerId);
    const { months, salaryCount, salaryTotal, wealthInvestTotal } = await loadCustomerMonthlyFlows(customerId);
    const monthCount = Math.max(1, months.length);
    const monthlyAvgSalary = Math.round(salaryTotal / monthCount);
    const monthlyAvgExpense = Math.round(months.reduce((sum, item) => sum + item.expense, 0) / monthCount);
    const monthlyAvgNet = monthlyAvgSalary - monthlyAvgExpense;
    const wealthMonthlyInvest = Math.round(wealthInvestTotal / monthCount);
    return {
      months: months.map(item => ({
        month: item.month,
        income: moneyValue(item.income, 'CNY'),
        expense: moneyValue(item.expense, 'CNY'),
        salary: moneyValue(item.salary, 'CNY'),
        wealthInvest: moneyValue(item.wealthInvest, 'CNY'),
      })),
      monthlyAvgSalary: moneyValue(monthlyAvgSalary, 'CNY'),
      monthlyAvgExpense: moneyValue(monthlyAvgExpense, 'CNY'),
      monthlyAvgNet: moneyValue(monthlyAvgNet, 'CNY'),
      wealthMonthlyInvest: moneyValue(wealthMonthlyInvest, 'CNY'),
      paydayHint: '每月 28 日前后发放，遇周末顺延至下一工作日',
      summary: '近 ' + monthCount + ' 个月月均工资 ' + moneyValue(monthlyAvgSalary, 'CNY').display
        + '，月均支出 ' + moneyValue(monthlyAvgExpense, 'CNY').display
        + '，月均结余 ' + moneyValue(monthlyAvgNet, 'CNY').display
        + '，月均理财投入 ' + moneyValue(wealthMonthlyInvest, 'CNY').display + '。',
    };
  }

  async function getAdvisoryContext(customerId, input) {
    await bankService.getCustomer(customerId);
    const preferences = validateAdvisoryInput(input);
    const generatedAt = now();
    const [riskResult, accountsResult, holdingsResult, transactionsResult, insightsResult, catalogResult] = await Promise.allSettled([
      repository.getWealthRiskAssessment(customerId),
      bankService.listAccounts(customerId),
      repository.listWealthHoldings(customerId, 'HOLDING'),
      repository.listWealthTransactions(customerId),
      getSalaryInsights(customerId),
      repository.listFinancialProducts(),
    ]);
    const riskRecord = riskResult.status === 'fulfilled' ? riskResult.value : null;
    const accounts = accountsResult.status === 'fulfilled' ? accountsResult.value : [];
    const holdings = holdingsResult.status === 'fulfilled' ? holdingsResult.value : [];
    const transactions = transactionsResult.status === 'fulfilled' ? transactionsResult.value : [];
    const catalog = catalogResult.status === 'fulfilled' ? catalogResult.value : [];
    const insights = insightsResult.status === 'fulfilled'
      ? insightsResult.value
      : {
          months: [],
          monthlyAvgSalary: moneyValue(0, 'CNY'),
          monthlyAvgExpense: moneyValue(0, 'CNY'),
          monthlyAvgNet: moneyValue(0, 'CNY'),
          wealthMonthlyInvest: moneyValue(0, 'CNY'),
          paydayHint: '',
          summary: '现金流画像暂时不可用，推荐基于持仓和风险测评生成。',
        };
    const balanceResults = await Promise.allSettled(accounts.map(account => repository.getBalance(account.id)));
    const balances = balanceResults
      .filter(r => r.status === 'fulfilled' && r.value)
      .map(r => r.value);
    const availableMinor = balances.filter(Boolean).reduce((sum, item) => sum + item.availableMinor, 0);
    const holdingValueMinor = holdings.reduce((sum, item) => sum + item.currentValueMinor, 0);
    const emergencyReserveMinor = Math.max(0, insights.monthlyAvgExpense.amountMinor * 6);
    const riskAssessment = projectRiskAssessment(riskRecord, generatedAt);
    const missingPreferenceFields = [];
    if (!preferences.investmentGoal) missingPreferenceFields.push('investmentGoal');
    if (!preferences.horizonMonths) missingPreferenceFields.push('horizonMonths');
    const plannedAmountMinor = preferences.plannedAmountMinor || 0;
    const profile = {
      availableAssets: moneyValue(availableMinor, 'CNY'),
      holdingValue: moneyValue(holdingValueMinor, 'CNY'),
      emergencyReserveTarget: moneyValue(emergencyReserveMinor, 'CNY'),
      monthlyAvgExpense: insights.monthlyAvgExpense,
      monthlyAvgNet: insights.monthlyAvgNet,
      holdingCount: holdings.length,
      investmentTransactionCount: transactions.length,
      asOf: generatedAt,
    };
    const projectedPreferences = {
      investmentGoal: preferences.investmentGoal || '',
      horizonMonths: preferences.horizonMonths || 0,
      plannedAmount: moneyValue(plannedAmountMinor, 'CNY'),
      productScope: preferences.productScope,
    };
    const dataSources = [
      source('core_data', 'risk_assessment', riskAssessment.status === 'VALID' ? 'fresh' : riskAssessment.status.toLowerCase(), riskRecord ? 1 : 0, generatedAt),
      source('core_data', 'account_balance', 'fresh', balances.filter(Boolean).length, generatedAt),
      source('core_data', 'wealth_holdings', 'fresh', holdings.length, generatedAt),
      source('core_data', 'wealth_transactions', 'fresh', transactions.length, generatedAt),
      source('derived_profile', 'cashflow_profile', 'derived', insights.months.length, generatedAt),
      source('product_knowledge', 'financial_product_catalog', 'fresh', catalog.length, generatedAt),
    ];
    const base = {
      riskAssessment,
      profile,
      preferences: projectedPreferences,
      missingPreferenceFields,
      dataSources,
      requiredActions: [],
      recommendations: [],
      generatedAt,
      disclaimer: '推荐基于当前风险测评、资金、持仓和历史行为生成，仅供本次决策参考；理财非存款，产品有风险，投资须谨慎。',
    };

    if (riskAssessment.status !== 'VALID') {
      return Object.assign(base, {
        recommendationStatus: 'risk_assessment_required',
        requiredActions: [{
          code: 'COMPLETE_RISK_ASSESSMENT',
          title: riskAssessment.status === 'EXPIRED' ? '更新风险承受能力测评' : '完成风险承受能力测评',
          message: '完成风险承受能力测评后，再为我推荐适合的理财产品',
        }],
      });
    }
    if (missingPreferenceFields.length) {
      return Object.assign(base, {
        recommendationStatus: 'preferences_required',
        requiredActions: [{
          code: 'PROVIDE_INVESTMENT_PREFERENCES',
          title: '补充投资目标和预计持有期限',
          message: '我希望稳健增值，预计持有12个月',
        }],
      });
    }

    const investableMinor = Math.max(
      0,
      preferences.plannedAmountMinor || Math.min(
        Math.max(0, availableMinor - emergencyReserveMinor),
        Math.max(0, insights.monthlyAvgNet.amountMinor * 3),
      ),
    );
    if (!preferences.plannedAmountMinor && investableMinor === 0) {
      return Object.assign(base, {
        recommendationStatus: 'preferences_required',
        missingPreferenceFields: ['plannedAmountMinor'],
        requiredActions: [{
          code: 'PROVIDE_PLANNED_AMOUNT',
          title: '补充本次计划投资金额',
          message: '我这次计划投入10000元，请继续推荐',
        }],
      });
    }
    const riskLimit = CUSTOMER_RISK_LIMIT[riskAssessment.level] || 0;
    const heldProductIds = new Set(holdings.map(item => item.productId));
    const recommendations = catalog
      .filter(product => PURCHASABLE_STATUS.has(product.status))
      .filter(product => CATEGORY_WHITELIST.has(product.category))
      .filter(product => preferences.productScope !== 'wealth_only'
        || !['DEPOSIT', 'CERTIFICATE_OF_DEPOSIT'].includes(product.category))
      .filter(product => (RISK_LEVEL_RANK[product.riskLevel] || 99) <= riskLimit)
      .filter(product => product.minPurchaseMinor <= investableMinor)
      .filter(product => productFitsHorizon(product, preferences.horizonMonths))
      .map(product => rankRecommendation(product, {
        availableMinor,
        emergencyReserveMinor,
        investableMinor,
        investmentGoal: preferences.investmentGoal,
        horizonMonths: preferences.horizonMonths,
        riskAssessment,
        alreadyHeld: heldProductIds.has(product.id),
      }))
      .sort((left, right) => right.score - left.score || right.annualRateBp - left.annualRateBp)
      .slice(0, 5)
      .map(({ score, ...item }) => item);

    const planned = buildWealthAdvisoryPlans({
      products: catalog,
      plannedAmountMinor: investableMinor,
      horizonMonths: preferences.horizonMonths,
      investmentGoal: preferences.investmentGoal,
      productScope: preferences.productScope,
      riskLimit,
      heldProductIds,
    });
    const projectAllocation = item => ({
      productId: item.productId,
      productName: item.productName,
      productCategory: item.productCategory,
      productCategoryLabel: item.productCategoryLabel,
      portfolioRole: item.portfolioRole,
      portfolioRoleLabel: item.portfolioRoleLabel,
      amount: moneyValue(item.amountMinor, 'CNY'),
      ratioBasisPoints: item.ratioBasisPoints,
      riskLevel: item.riskLevel,
      termDays: item.termDays,
      liquidity: item.liquidity,
      suitability: item.suitability,
      annualRateBp: item.annualRateBp,
      rateNote: item.rateNote,
      reasons: item.reasons,
    });
    const projectPlan = plan => plan && ({
      planId: plan.planId,
      title: plan.title,
      summary: plan.summary,
      kind: plan.kind,
      totalAmount: moneyValue(plan.totalAmountMinor, 'CNY'),
      allocations: plan.allocations.map(projectAllocation),
      rationale: plan.rationale,
    });

    return Object.assign(base, {
      recommendationStatus: planned.primaryPlan ? 'ready' : 'no_suitable_plan',
      recommendations,
      primaryPlan: projectPlan(planned.primaryPlan),
      alternativePlans: planned.alternativePlans.map(projectPlan),
      planMetrics: {
        candidateCount: planned.candidateCount,
        feasiblePlanCount: planned.feasiblePlanCount,
        amountConserved: Boolean(planned.primaryPlan),
      },
      requiredActions: planned.primaryPlan ? [] : [{
        code: 'ADJUST_INVESTMENT_PREFERENCES',
        title: '调整计划金额或持有期限',
        message: '帮我调整计划金额或持有期限后重新分析',
      }],
    });
  }

  function validateAdvisoryInput(input) {
    const value = input || {};
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new BankApiError(422, 'validation_error', '财富建议参数必须是 JSON 对象');
    }
    const allowed = new Set(['investmentGoal', 'horizonMonths', 'plannedAmountMinor', 'productScope']);
    if (Object.keys(value).some(key => !allowed.has(key))) {
      throw new BankApiError(422, 'validation_error', '财富建议参数包含未知字段');
    }
    const goals = new Set(['capital_preservation', 'steady_growth', 'long_term_growth']);
    if (value.investmentGoal != null && !goals.has(value.investmentGoal)) {
      throw new BankApiError(422, 'validation_error', '投资目标无效');
    }
    if (value.horizonMonths != null && (!Number.isInteger(value.horizonMonths) || value.horizonMonths < 1 || value.horizonMonths > 600)) {
      throw new BankApiError(422, 'validation_error', '预计持有期限无效');
    }
    if (value.plannedAmountMinor != null && (!Number.isInteger(value.plannedAmountMinor) || value.plannedAmountMinor < 100 || value.plannedAmountMinor > 1000000000)) {
      throw new BankApiError(422, 'validation_error', '计划投资金额无效');
    }
    const productScopes = new Set(['wealth_only', 'wealth_and_conservative_alternatives']);
    if (value.productScope != null && !productScopes.has(value.productScope)) {
      throw new BankApiError(422, 'validation_error', '推荐产品范围无效');
    }
    return {
      investmentGoal: value.investmentGoal || null,
      horizonMonths: value.horizonMonths || null,
      plannedAmountMinor: value.plannedAmountMinor || null,
      productScope: value.productScope || 'wealth_only',
    };
  }

  function projectRiskAssessment(record, timestamp) {
    if (!record) return { status: 'MISSING', level: 'UNKNOWN', assessedAt: null, expiresAt: null, source: 'CORE_WEALTH_SUITABILITY' };
    if (record.status !== 'ACTIVE') return { status: 'INACTIVE', level: record.level, assessedAt: record.assessedAt, expiresAt: record.expiresAt, source: record.source };
    if (Date.parse(record.expiresAt) <= Date.parse(timestamp)) return { status: 'EXPIRED', level: record.level, assessedAt: record.assessedAt, expiresAt: record.expiresAt, source: record.source };
    return { status: 'VALID', level: record.level, assessedAt: record.assessedAt, expiresAt: record.expiresAt, source: record.source };
  }

  function source(kind, name, status, recordCount, asOf) {
    return { kind, source: name, status, recordCount, asOf };
  }

  function productFitsHorizon(product, horizonMonths) {
    if (product.termType === 'NO_FIXED' && horizonMonths < 36) return false;
    if (!product.termDays) return true;
    return product.termDays <= horizonMonths * 30;
  }

  // 组合角色分配：同角色产品共享仓位上限，形成「主推+流动性+底仓」的真实组合
  // 而非每只产品独立占用 35%-50% 的候选列表。
  const PORTFOLIO_ROLES = {
    FIXED_INCOME_WEALTH: { label: '主推核心', capRatio: 0.5 },
    CASH_MANAGEMENT: { label: '流动性备用', capRatio: 0.2 },
    DEPOSIT: { label: '稳健底仓', capRatio: 0.2 },
  };

  function rankRecommendation(product, context) {
    const goalWeights = {
      capital_preservation: { R1: 50, R2: 10 },
      steady_growth: { R1: 25, R2: 50 },
      long_term_growth: { R1: 10, R2: 35, R3: 55, R4: 45, R5: 30 },
    };
    const liquidity = product.termType === 'OPEN_DAILY' || product.termType === 'DEMAND'
      ? '高流动性，可按产品规则申赎'
      : (product.termDays ? `建议至少持有 ${product.termDays} 天` : '按产品规则持有');
    const role = PORTFOLIO_ROLES[product.category] || { label: '补充配置', capRatio: 0.35 };
    const maximumMinor = Math.max(
      product.minPurchaseMinor,
      Math.min(context.investableMinor, Math.floor(context.investableMinor * role.capRatio / 100) * 100),
    );
    const reasons = [
      `组合角色：${role.label}（同类产品共享 ${Math.round(role.capRatio * 100)}% 仓位上限）`,
      `产品风险等级 ${product.riskLevel} 未超过当前测评 ${context.riskAssessment.level} 的适配上限`,
      product.termDays
        ? `产品期限 ${product.termDays} 天处于预计持有 ${context.horizonMonths} 个月范围内`
        : '产品无固定封闭期限，可保留一定资金灵活性',
    ];
    if (context.alreadyHeld) reasons.push('当前已有同类持仓，建议控制追加比例并关注集中度');
    else reasons.push('可用于分散现有持仓结构，具体比例仍可调整');
    if (context.availableMinor < context.emergencyReserveMinor) reasons.push('当前流动资金低于应急储备目标，建议仅考虑小额配置');
    const score = (goalWeights[context.investmentGoal][product.riskLevel] || 0)
      + Math.min(20, (product.annualRateBp || 0) / 20)
      + (product.termDays ? 5 : 12)
      - (context.alreadyHeld ? 12 : 0);
    return {
      score,
      productId: product.id,
      productName: product.name,
      category: product.category,
      riskLevel: product.riskLevel,
      annualRateBp: product.annualRateBp || 0,
      rateNote: product.rateNote,
      termDays: product.termDays || 0,
      liquidity,
      suitability: `适配当前 ${context.riskAssessment.level} 风险测评和 ${context.horizonMonths} 个月持有计划`,
      suggestedAmountRange: {
        minimum: moneyValue(product.minPurchaseMinor, 'CNY'),
        maximum: moneyValue(maximumMinor, 'CNY'),
      },
      reasons,
    };
  }

  /**
   * 发薪理财推荐：基于收支画像的确定性规则（非模型生成），只引用产品目录中的真实在售产品。
   */
  async function getRecommendations(customerId) {
    await bankService.getCustomer(customerId);
    const insights = await getSalaryInsights(customerId);
    const accounts = await bankService.listAccounts(customerId);
    let liquidMinor = 0;
    for (const account of accounts) {
      const balance = await repository.getBalance(account.id);
      if (balance) liquidMinor += balance.availableMinor;
    }
    const holdings = await repository.listWealthHoldings(customerId);
    const monthlyNet = insights.monthlyAvgNet.amountMinor;
    const monthlyExpense = Math.max(1, insights.monthlyAvgExpense.amountMinor);
    const emergencyTarget = monthlyExpense * 6;
    const emergencyGap = Math.max(0, emergencyTarget - liquidMinor);

    const items = [];
    if (emergencyGap > 0) {
      items.push(recommendation('prod_w_lqb10', Math.max(100000, Math.ceil(emergencyGap / 2 / 100) * 100),
        'emergency', '活钱备用：当前活期资金低于 6 个月支出，建议优先补足随取随用的现金管理仓位'));
    }
    if (monthlyNet > 100000) {
      const steady = Math.ceil(monthlyNet * 0.5 / 100) * 100;
      items.push(recommendation('prod_w_anxin', steady, 'steady',
        '工资一半结余配置低波动固收，按日开放可应对用款，长期平滑利率下行影响'));
      const dca = Math.ceil(monthlyNet * 0.2 / 100) * 100;
      items.push(recommendation('prod_fund_000051', dca, 'growth',
        '发薪日次月 2 日定投宽基指数，长期分摊成本、提升组合弹性（当前已有定投习惯，可顺势提高）'));
    } else if (monthlyNet > 0) {
      items.push(recommendation('prod_w_lqb10', Math.max(100, Math.ceil(monthlyNet / 2 / 100) * 100),
        'emergency', '结余有限，先以零钱理财积累应急与机动资金'));
    }
    const holdingProducts = new Set(holdings.filter(item => item.status === 'HOLDING').map(item => item.productId));
    if (liquidMinor > monthlyExpense * 12 && !holdingProducts.has('prod_cd_3y')) {
      items.push(recommendation('prod_cd_3y', 20000000, 'lock_in',
        '闲置资金超过一年支出，可用大额存单锁定 3 年 1.55% 利率，按月付息补充现金流'));
    }
    const catalog = new Map((await repository.listFinancialProducts()).map(item => [item.id, item]));
    return {
      profile: {
        monthlyAvgSalary: insights.monthlyAvgSalary,
        monthlyAvgExpense: insights.monthlyAvgExpense,
        monthlyAvgNet: insights.monthlyAvgNet,
        liquidAssets: moneyValue(liquidMinor, 'CNY'),
        emergencyTarget: moneyValue(emergencyTarget, 'CNY'),
        monthsAnalyzed: insights.months.length,
      },
      items: items
        .filter(item => catalog.has(item.productId))
        .map(item => {
          const product = catalog.get(item.productId);
          return Object.assign(item, {
            productName: product.name,
            category: product.category,
            riskLevel: product.riskLevel,
            annualRateBp: product.annualRateBp,
            rateNote: product.rateNote,
            suggestedAmount: moneyValue(item.suggestedAmountMinor, 'CNY'),
          });
        }),
      generatedAt: now(),
      disclaimer: '以上为基于本人收支画像的规则化演示建议，非投资承诺；理财非存款、产品有风险、投资须谨慎。',
    };
  }

  function recommendation(productId, suggestedAmountMinor, tag, reason) {
    return { productId, suggestedAmountMinor, tag, reason };
  }

  /* ---------------- 申购 / 赎回（写事务） ---------------- */

  async function purchase(actor, input, idempotencyKey) {
    requireUserActor(actor);
    const body = validatePurchaseInput(input);
    const product = await repository.getFinancialProduct(body.productId);
    if (!product || !PURCHASABLE_STATUS.has(product.status)) {
      throw new BankApiError(422, 'product_not_purchasable', '产品当前不可申购');
    }
    if (product.category === 'LOAN' || product.category === 'CREDIT_CARD' || product.category === 'CREDIT_CARD_SERVICE') {
      throw new BankApiError(422, 'product_not_purchasable', '该类目不支持在财富通道申购');
    }
    if (body.amountMinor < product.minPurchaseMinor) {
      throw new BankApiError(422, 'below_min_purchase', '申购金额低于起购金额', [
        { field: 'amountMinor', code: 'min_purchase', detail: String(product.minPurchaseMinor) },
      ]);
    }
    if ((body.amountMinor - product.minPurchaseMinor) % product.minIncrementMinor !== 0) {
      throw new BankApiError(422, 'invalid_increment', '申购金额不符合递增单位', [
        { field: 'amountMinor', code: 'increment', detail: String(product.minIncrementMinor) },
      ]);
    }
    const account = await bankService.requireOwnedAccount(actor.customerId, body.accountId);
    if (account.status !== 'ACTIVE') throw new BankApiError(422, 'account_unavailable', '付款账户当前不可用');
    const balance = await repository.getBalance(account.id);
    if (balance.availableMinor < body.amountMinor) {
      throw new BankApiError(422, 'insufficient_funds', '账户可用余额不足');
    }
    const requestHash = hash({ customerId: actor.customerId, productId: body.productId, accountId: body.accountId, amountMinor: body.amountMinor });

    return repository.withTransaction(async tx => {
      const replay = idempotentReplay(tx, 'wealth.purchase', idempotencyKey, requestHash);
      if (replay) return replay;
      const timestamp = now();
      const orderId = id('wpay');
      const newAvailable = balance.availableMinor - body.amountMinor;
      tx.updateBalance(account.id, newAvailable, balance.ledgerMinor - body.amountMinor, balance.version);
      tx.insertLedgerTransaction({ id: 'lg_' + orderId, businessType: 'WEALTH_PURCHASE', businessId: orderId, currency: account.currency, postedAt: timestamp });
      tx.insertLedgerEntry({ id: 'le_' + orderId + '_a', ledgerTransactionId: 'lg_' + orderId, accountId: account.id, direction: 'DEBIT', amountMinor: body.amountMinor, description: '申购-' + product.name });
      tx.insertLedgerEntry({ id: 'le_' + orderId + '_b', ledgerTransactionId: 'lg_' + orderId, accountId: 'acct_external_clearing', direction: 'CREDIT', amountMinor: body.amountMinor, description: product.name + '申购清算' });
      tx.run('INSERT INTO bank_account_transactions (id,account_id,customer_id,direction,amount_minor,currency,business_type,category,counterparty,description,channel,balance_after_minor,occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', [
        'atxn_' + orderId, account.id, actor.customerId, 'DEBIT', body.amountMinor, account.currency,
        'WEALTH_PURCHASE', 'WEALTH_INVEST', product.issuer, '申购-' + product.name, 'MOBILE_APP', newAvailable, timestamp,
      ]);
      const existing = tx.queryOne("SELECT * FROM bank_wealth_holdings WHERE customer_id = ? AND product_id = ? AND status = 'HOLDING'", [actor.customerId, product.id]);
      let holdingId;
      if (existing) {
        holdingId = existing.id;
        tx.run('UPDATE bank_wealth_holdings SET principal_minor = principal_minor + ?, current_value_minor = current_value_minor + ?, last_valued_at = ? WHERE id = ?', [body.amountMinor, body.amountMinor, timestamp, holdingId]);
      } else {
        holdingId = id('wh');
        tx.run('INSERT INTO bank_wealth_holdings (id,customer_id,account_id,product_id,product_name,category,principal_minor,current_value_minor,accumulated_earnings_minor,currency,status,opened_at,matures_at,last_valued_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', [
          holdingId, actor.customerId, account.id, product.id, product.name, product.category,
          body.amountMinor, body.amountMinor, 0, account.currency, 'HOLDING', timestamp, null, timestamp,
        ]);
      }
      tx.run('INSERT INTO bank_wealth_transactions (id,customer_id,account_id,product_id,product_name,direction,amount_minor,earnings_minor,currency,status,occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)', [
        id('wtxn'), actor.customerId, account.id, product.id, product.name, 'PURCHASE',
        body.amountMinor, 0, account.currency, 'CONFIRMED', timestamp,
      ]);
      const response = {
        id: orderId, orderId, holdingId, productId: product.id, productName: product.name,
        amount: moneyValue(body.amountMinor, account.currency),
        accountId: account.id, status: 'CONFIRMED', occurredAt: timestamp,
      };
      tx.saveIdempotency(record('wealth.purchase', idempotencyKey, requestHash, response, timestamp));
      audit(tx, actor, 'bank.wealth.purchase', 'wealth-order', orderId, 'SUCCEEDED', { productId: product.id, amountMinor: body.amountMinor }, timestamp);
      return response;
    });
  }

  async function redeem(actor, input, idempotencyKey) {
    requireUserActor(actor);
    const body = validateRedeemInput(input);
    const holding = await repository.listWealthHoldings(actor.customerId, 'HOLDING')
      .then(list => list.find(item => item.id === body.holdingId));
    if (!holding) throw new BankApiError(404, 'holding_not_found', '持仓不存在或已了结');
    if (holding.category === 'DEPOSIT' || holding.category === 'CERTIFICATE_OF_DEPOSIT') {
      if (!holding.maturesAt) throw new BankApiError(422, 'not_redeemable', '该持仓不支持提前赎回，请到期后兑付');
      if (String(holding.maturesAt) > now()) throw new BankApiError(422, 'not_matured', '定期类持仓未到期，提前支取将按活期计息，请到柜台办理');
    }
    const balance = await repository.getBalance(holding.accountId);
    if (!balance) throw new BankApiError(422, 'account_unavailable', '入账账户不可用');
    const amountMinor = holding.currentValueMinor;
    const earningsMinor = holding.accumulatedEarningsMinor;
    const requestHash = hash({ customerId: actor.customerId, holdingId: body.holdingId });

    return repository.withTransaction(async tx => {
      const replay = idempotentReplay(tx, 'wealth.redeem', idempotencyKey, requestHash);
      if (replay) return replay;
      const timestamp = now();
      const orderId = id('wred');
      const newAvailable = balance.availableMinor + amountMinor;
      tx.updateBalance(holding.accountId, newAvailable, balance.ledgerMinor + amountMinor, balance.version);
      tx.insertLedgerTransaction({ id: 'lg_' + orderId, businessType: 'WEALTH_REDEMPTION', businessId: orderId, currency: holding.currency, postedAt: timestamp });
      tx.insertLedgerEntry({ id: 'le_' + orderId + '_a', ledgerTransactionId: 'lg_' + orderId, accountId: holding.accountId, direction: 'CREDIT', amountMinor, description: '赎回-' + holding.productName });
      tx.insertLedgerEntry({ id: 'le_' + orderId + '_b', ledgerTransactionId: 'lg_' + orderId, accountId: 'acct_opening_equity', direction: 'DEBIT', amountMinor, description: holding.productName + '赎回清算' });
      tx.run('INSERT INTO bank_account_transactions (id,account_id,customer_id,direction,amount_minor,currency,business_type,category,counterparty,description,channel,balance_after_minor,occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', [
        'atxn_' + orderId, holding.accountId, actor.customerId, 'CREDIT', amountMinor, holding.currency,
        'WEALTH_REDEMPTION', 'INCOME_WEALTH', '中国建设银行', '赎回-' + holding.productName, 'MOBILE_APP', newAvailable, timestamp,
      ]);
      tx.run("UPDATE bank_wealth_holdings SET status = 'REDEEMED', current_value_minor = 0, last_valued_at = ? WHERE id = ? AND status = 'HOLDING'", [timestamp, holding.id]);
      tx.run('INSERT INTO bank_wealth_transactions (id,customer_id,account_id,product_id,product_name,direction,amount_minor,earnings_minor,currency,status,occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)', [
        id('wtxn'), actor.customerId, holding.accountId, holding.productId, holding.productName, 'REDEMPTION',
        amountMinor, earningsMinor, holding.currency, 'CONFIRMED', timestamp,
      ]);
      const response = {
        orderId, holdingId: holding.id, productId: holding.productId, productName: holding.productName,
        amount: moneyValue(amountMinor, holding.currency),
        earnings: moneyValue(earningsMinor, holding.currency),
        accountId: holding.accountId, status: 'SETTLED', occurredAt: timestamp,
      };
      tx.saveIdempotency(record('wealth.redeem', idempotencyKey, requestHash, response, timestamp));
      audit(tx, actor, 'bank.wealth.redeem', 'wealth-order', orderId, 'SUCCEEDED', { holdingId: holding.id, amountMinor }, timestamp);
      return response;
    });
  }

  /* ---------------- 校验与工具 ---------------- */

  function validatePurchaseInput(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) {
      throw new BankApiError(422, 'validation_error', '申购请求必须是 JSON 对象');
    }
    const allowed = new Set(['productId', 'accountId', 'amountMinor']);
    const unknown = Object.keys(input).filter(key => !allowed.has(key));
    if (unknown.length || Object.keys(input).length !== allowed.size) {
      throw new BankApiError(422, 'validation_error', '申购请求字段无效',
        unknown.map(field => ({ field, code: 'unknown_field' })));
    }
    return {
      productId: requireString(input.productId, 'productId', { max: 64 }),
      accountId: requireString(input.accountId, 'accountId', { max: 64 }),
      amountMinor: requirePositiveMinor(input.amountMinor, 'amountMinor'),
    };
  }

  function validateRedeemInput(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) {
      throw new BankApiError(422, 'validation_error', '赎回请求必须是 JSON 对象');
    }
    const allowed = new Set(['holdingId']);
    const unknown = Object.keys(input).filter(key => !allowed.has(key));
    if (unknown.length || Object.keys(input).length !== allowed.size) {
      throw new BankApiError(422, 'validation_error', '赎回请求字段无效',
        unknown.map(field => ({ field, code: 'unknown_field' })));
    }
    return { holdingId: requireString(input.holdingId, 'holdingId', { max: 64 }) };
  }

  function requireUserActor(actor) {
    if (!actor || actor.type !== 'user' || !actor.customerId) {
      throw new BankApiError(403, 'forbidden', '理财交易必须由客户本人发起');
    }
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

  function toPublicProduct(product) {
    return {
      id: product.id, category: product.category, name: product.name, issuer: product.issuer,
      productCode: product.productCode, riskLevel: product.riskLevel,
      minPurchase: moneyValue(product.minPurchaseMinor, 'CNY'),
      minIncrement: moneyValue(product.minIncrementMinor, 'CNY'),
      termType: product.termType, termDays: product.termDays,
      annualRateBp: product.annualRateBp, rateType: product.rateType, rateNote: product.rateNote,
      purchaseChannel: product.purchaseChannel, status: product.status, features: product.features || {},
    };
  }

  function toPublicHolding(holding) {
    return {
      id: holding.id, productName: holding.productName, category: holding.category,
      principal: moneyValue(holding.principalMinor, holding.currency),
      currentValue: moneyValue(holding.currentValueMinor, holding.currency),
      accumulatedEarnings: moneyValue(holding.accumulatedEarningsMinor, holding.currency),
      status: holding.status, openedAt: holding.openedAt, maturesAt: holding.maturesAt,
      lastValuedAt: holding.lastValuedAt, accountId: holding.accountId,
    };
  }

  return {
    listProducts, getProduct, listHoldings, listTransactions,
    getSalaryInsights, getRecommendations, getAdvisoryContext, purchase, redeem,
  };
}

module.exports = { createWealthService };
