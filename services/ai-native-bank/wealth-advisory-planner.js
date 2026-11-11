'use strict';

const RISK_RANK = Object.freeze({ R1: 1, R2: 2, R3: 3, R4: 4, R5: 5 });
const PURCHASE_STATUS = new Set(['ON_SALE', 'LIMITED']);
const CATEGORY_ROLE = Object.freeze({
  FIXED_INCOME_WEALTH: 'core',
  CASH_MANAGEMENT: 'liquidity',
  DEPOSIT: 'stable_floor',
  CERTIFICATE_OF_DEPOSIT: 'stable_floor',
  FUND: 'growth',
});
const ROLE_LABEL = Object.freeze({
  core: '核心配置',
  liquidity: '流动性配置',
  stable_floor: '稳健底仓',
  growth: '增值配置',
});
const CATEGORY_LABEL = Object.freeze({
  FIXED_INCOME_WEALTH: '银行理财',
  CASH_MANAGEMENT: '现金管理',
  DEPOSIT: '存款',
  CERTIFICATE_OF_DEPOSIT: '大额存单',
  FUND: '基金',
});
const ROLE_CAP = Object.freeze({ core: 0.5, liquidity: 0.3, stable_floor: 0.3, growth: 0.35 });

function positiveInteger(value, fallback) {
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

function acceptsAmount(product, amountMinor) {
  const minimum = positiveInteger(product.minPurchaseMinor, 1);
  const increment = positiveInteger(product.minIncrementMinor, 1);
  return amountMinor >= minimum && (amountMinor - minimum) % increment === 0;
}

function fitsHorizon(product, horizonMonths) {
  if (product.termType === 'NO_FIXED' && horizonMonths < 36) return false;
  if (!product.termDays) return true;
  return product.termDays <= horizonMonths * 30;
}

function allowedByScope(product, productScope) {
  if (productScope !== 'wealth_only') return true;
  return !['DEPOSIT', 'CERTIFICATE_OF_DEPOSIT'].includes(product.category);
}

function productScore(product, input) {
  const riskRank = RISK_RANK[product.riskLevel] || 99;
  const targetRisk = input.investmentGoal === 'capital_preservation' ? 1 : input.riskLimit;
  const riskFit = Math.max(0, 60 - Math.abs(targetRisk - riskRank) * 18);
  const liquidity = ['OPEN_DAILY', 'DEMAND'].includes(product.termType) ? 18 : 8;
  const yieldSignal = Math.min(12, positiveInteger(product.annualRateBp, 0) / 25);
  const heldPenalty = input.heldProductIds.has(product.id) ? 14 : 0;
  return riskFit + liquidity + yieldSignal - heldPenalty;
}

function allocationFrom(product, amountMinor, input) {
  const role = CATEGORY_ROLE[product.category] || 'growth';
  const alreadyHeld = input.heldProductIds.has(product.id);
  const reasons = [
    `${CATEGORY_LABEL[product.category] || '财富产品'}用于${ROLE_LABEL[role] || '补充配置'}`,
    `产品风险等级 ${product.riskLevel} 未超过本次适配上限`,
    product.termDays
      ? `产品期限 ${product.termDays} 天不超过计划持有期`
      : '产品无固定封闭期限，可保留资金灵活性',
    alreadyHeld ? '已有同类持仓，本次方案已降低集中配置优先级' : '当前未持有该产品，可用于分散现有持仓',
  ];
  return {
    productId: product.id,
    productName: product.name,
    productCategory: product.category,
    productCategoryLabel: CATEGORY_LABEL[product.category] || '财富产品',
    portfolioRole: role,
    portfolioRoleLabel: ROLE_LABEL[role] || '补充配置',
    amountMinor,
    ratioBasisPoints: Math.round(amountMinor * 10000 / input.plannedAmountMinor),
    minPurchaseMinor: positiveInteger(product.minPurchaseMinor, 1),
    minIncrementMinor: positiveInteger(product.minIncrementMinor, 1),
    riskLevel: product.riskLevel,
    termDays: product.termDays || 0,
    liquidity: ['OPEN_DAILY', 'DEMAND'].includes(product.termType)
      ? '流动性较高，可按产品规则申赎或支取'
      : `建议至少持有 ${product.termDays || input.horizonMonths * 30} 天`,
    suitability: `适配当前风险等级和 ${input.horizonMonths} 个月持有计划`,
    annualRateBp: product.annualRateBp || 0,
    rateNote: product.rateNote || '',
    reasons,
  };
}

function normalizeAllocationRatios(allocations, plannedAmountMinor) {
  let assigned = 0;
  return allocations.map((allocation, index) => {
    const isLast = index === allocations.length - 1;
    const ratioBasisPoints = isLast
      ? 10_000 - assigned
      : Math.round(allocation.amountMinor * 10_000 / plannedAmountMinor);
    assigned += ratioBasisPoints;
    return { ...allocation, ratioBasisPoints };
  });
}

function assertPlanInvariant(plan, plannedAmountMinor) {
  if (!plan || !Array.isArray(plan.allocations) || !plan.allocations.length) {
    throw new TypeError('wealth advisory plan requires allocations');
  }
  const ids = new Set();
  const roleTotals = new Map();
  let total = 0;
  let ratioTotal = 0;
  for (const item of plan.allocations) {
    if (ids.has(item.productId)) throw new TypeError('wealth advisory plan repeats a product');
    ids.add(item.productId);
    if (!Number.isInteger(item.amountMinor) || item.amountMinor < item.minPurchaseMinor) {
      throw new TypeError('wealth advisory allocation below minimum');
    }
    if ((item.amountMinor - item.minPurchaseMinor) % item.minIncrementMinor !== 0) {
      throw new TypeError('wealth advisory allocation increment mismatch');
    }
    total += item.amountMinor;
    if (!Number.isInteger(item.ratioBasisPoints) || item.ratioBasisPoints <= 0) {
      throw new TypeError('wealth advisory allocation ratio invalid');
    }
    ratioTotal += item.ratioBasisPoints;
    roleTotals.set(item.portfolioRole, (roleTotals.get(item.portfolioRole) || 0) + item.amountMinor);
  }
  if (total !== plannedAmountMinor || plan.totalAmountMinor !== plannedAmountMinor) {
    throw new TypeError('wealth advisory plan amount mismatch');
  }
  if (ratioTotal !== 10_000) {
    throw new TypeError('wealth advisory plan ratio mismatch');
  }
  if (plan.allocations.length > 1) {
    for (const [role, amount] of roleTotals) {
      const cap = ROLE_CAP[role];
      if (cap && amount > Math.floor(plannedAmountMinor * cap)) {
        throw new TypeError('wealth advisory role cap exceeded');
      }
    }
  }
  return plan;
}

function singleProductPlan(product, input) {
  const allocation = allocationFrom(product, input.plannedAmountMinor, input);
  const titleByRole = {
    core: '稳健整笔配置',
    liquidity: '流动性优先方案',
    stable_floor: '保守替代方案',
    growth: '成长配置方案',
  };
  const plan = {
    planId: `plan_single_${product.id.replace(/^prod_/, '')}`,
    title: titleByRole[allocation.portfolioRole] || '单产品配置方案',
    summary: `整笔配置于${product.name}，适合希望方案简洁、便于跟踪的安排`,
    totalAmountMinor: input.plannedAmountMinor,
    allocations: [allocation],
    rationale: [
      '本方案总金额与计划投入金额一致',
      '该产品的最低起购金额使其更适合作为独立完整方案，而不是与其他产品强行拼接',
    ],
    score: productScore(product, input),
    kind: 'single_product',
  };
  return assertPlanInvariant(plan, input.plannedAmountMinor);
}

function floorToIncrement(target, product) {
  const minimum = positiveInteger(product.minPurchaseMinor, 1);
  const increment = positiveInteger(product.minIncrementMinor, 1);
  if (target < minimum) return 0;
  return minimum + Math.floor((target - minimum) / increment) * increment;
}

function buildDiversifiedPlan(products, input) {
  const byRole = new Map();
  for (const product of products) {
    const role = CATEGORY_ROLE[product.category] || 'growth';
    if (!byRole.has(role)) byRole.set(role, product);
  }
  const roleOrder = ['core', 'liquidity', 'stable_floor', 'growth'];
  const selected = roleOrder.map(role => byRole.get(role)).filter(Boolean).slice(0, 3);
  if (selected.length < 3) return null;
  const allocations = [];
  let remaining = input.plannedAmountMinor;
  selected.forEach((product, index) => {
    const role = CATEGORY_ROLE[product.category] || 'growth';
    const isLast = index === selected.length - 1;
    const capMinor = Math.floor(input.plannedAmountMinor * ROLE_CAP[role]);
    const target = isLast ? remaining : Math.min(remaining, capMinor);
    const amountMinor = isLast && acceptsAmount(product, target)
      ? target
      : floorToIncrement(target, product);
    if (amountMinor > 0) {
      allocations.push(allocationFrom(product, amountMinor, input));
      remaining -= amountMinor;
    }
  });
  if (remaining !== 0 || allocations.length < 2) return null;
  const plan = {
    planId: 'plan_diversified',
    title: '分散配置方案',
    summary: '在风险适配范围内兼顾核心配置、流动性与稳健底仓',
    totalAmountMinor: input.plannedAmountMinor,
    allocations: normalizeAllocationRatios(allocations, input.plannedAmountMinor),
    rationale: ['不同类别承担不同组合角色', '同一角色的配置金额按方案汇总校验'],
    score: allocations.reduce((sum, item) => {
      const source = products.find(product => product.id === item.productId);
      return sum + productScore(source, input);
    }, 0) / allocations.length + 8,
    kind: 'diversified',
  };
  try {
    return assertPlanInvariant(plan, input.plannedAmountMinor);
  } catch (_error) {
    return null;
  }
}

function buildWealthAdvisoryPlans(rawInput) {
  const input = {
    ...rawInput,
    heldProductIds: rawInput.heldProductIds instanceof Set ? rawInput.heldProductIds : new Set(),
    productScope: rawInput.productScope || 'wealth_only',
  };
  if (!Number.isInteger(input.plannedAmountMinor) || input.plannedAmountMinor <= 0) {
    throw new TypeError('planned amount must be a positive integer');
  }
  const candidates = (Array.isArray(input.products) ? input.products : [])
    .filter(product => PURCHASE_STATUS.has(product.status))
    .filter(product => CATEGORY_ROLE[product.category])
    .filter(product => allowedByScope(product, input.productScope))
    .filter(product => (RISK_RANK[product.riskLevel] || 99) <= input.riskLimit)
    .filter(product => fitsHorizon(product, input.horizonMonths))
    .filter(product => positiveInteger(product.minPurchaseMinor, 1) <= input.plannedAmountMinor)
    .sort((left, right) => productScore(right, input) - productScore(left, input)
      || left.id.localeCompare(right.id));

  const plans = [];
  const diversified = buildDiversifiedPlan(candidates, input);
  if (diversified) plans.push(diversified);
  candidates.filter(product => acceptsAmount(product, input.plannedAmountMinor))
    .forEach(product => plans.push(singleProductPlan(product, input)));

  const unique = [];
  const signatures = new Set();
  for (const plan of plans.sort((left, right) => right.score - left.score || left.planId.localeCompare(right.planId))) {
    const signature = plan.allocations.map(item => `${item.productId}:${item.amountMinor}`).join('|');
    if (signatures.has(signature)) continue;
    signatures.add(signature);
    unique.push(plan);
  }
  const publicPlan = (plan, isPrimary) => plan && ({
    planId: plan.planId,
    title: isPrimary ? plan.title : (plan.kind === 'diversified' ? '分散配置备选' : plan.title),
    summary: plan.summary,
    totalAmountMinor: plan.totalAmountMinor,
    allocations: plan.allocations,
    rationale: plan.rationale,
    kind: plan.kind,
  });
  return {
    primaryPlan: publicPlan(unique[0] || null, true),
    alternativePlans: unique.slice(1, 3).map(plan => publicPlan(plan, false)),
    candidateCount: candidates.length,
    feasiblePlanCount: unique.length,
  };
}

module.exports = {
  buildWealthAdvisoryPlans,
  assertPlanInvariant,
  CATEGORY_LABEL,
  ROLE_LABEL,
};
