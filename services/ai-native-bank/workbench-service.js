'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { BankApiError } = require('./api-contract');
const capabilityRegistry = require('./capability-registry');
const { loadBusinessResourceRegistry } = require('./business-resource-registry');
const { auditAgentIntegration } = require('./agent-integration-audit');

const DEFAULT_SCENARIO_PATH = path.join(
  __dirname, '..', '..', 'data', 'ai-native-bank', 'registries', 'scenarios.v1.json',
);
const SCENARIO_KEYS = Object.freeze([
  'id', 'version', 'title', 'domain', 'owner', 'status', 'intentIds', 'menuIds',
  'requiredCapabilityIds', 'evalDatasetId',
]);
const ASSET_FILTERS = new Set(['type', 'domain', 'integrationStatus']);
const ASSET_TYPES = new Set(['all', 'capability', 'skill', 'provider']);
const INTEGRATION_STATUSES = new Set(['enabled', 'deferred', 'platform-only']);

function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

function cloneFreeze(value) {
  return deepFreeze(structuredClone(value));
}

function readScenarioManifest() {
  try {
    return JSON.parse(fs.readFileSync(DEFAULT_SCENARIO_PATH, 'utf8'));
  } catch (error) {
    throw scenarioError('场景清单无法加载');
  }
}

function scenarioError(message) {
  return new BankApiError(503, 'scenario_manifest_invalid', `scenario_manifest_invalid: ${message}`);
}

function exactKeys(value, expected) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === [...expected].sort()[index]);
}

function uniqueStrings(values, pattern, max) {
  return Array.isArray(values) && values.length <= max
    && values.every(value => typeof value === 'string' && pattern.test(value))
    && new Set(values).size === values.length;
}

function validateScenarioManifest(manifest, sources) {
  if (!exactKeys(manifest, ['schemaVersion', 'scenarios'])
    || manifest.schemaVersion !== '1.0'
    || !Array.isArray(manifest.scenarios)
    || manifest.scenarios.length < 1
    || manifest.scenarios.length > 32) {
    throw scenarioError('顶层契约无效');
  }
  const graphNodes = new Map(sources.intentGraph.nodes.map(node => [node.id, node]));
  const capabilityIds = new Set(sources.capabilities.map(item => item.id));
  const ids = new Set();
  manifest.scenarios.forEach(item => {
    if (!exactKeys(item, SCENARIO_KEYS)
      || !/^[a-z][a-z0-9-]{1,63}$/.test(item.id || '')
      || !/^\d+\.\d+\.\d+$/.test(item.version || '')
      || typeof item.title !== 'string' || item.title.length < 1 || item.title.length > 80
      || !/^[a-z][a-z0-9-]{1,63}$/.test(item.domain || '')
      || !/^[a-z][a-z0-9-]{1,63}$/.test(item.owner || '')
      || !['active', 'planned', 'retired'].includes(item.status)
      || !uniqueStrings(item.intentIds, /^ccb\.[a-z0-9.-]+$/, 64)
      || !uniqueStrings(item.menuIds, /^ccb\.menu\.[a-z0-9.-]+$/, 32)
      || !uniqueStrings(item.requiredCapabilityIds, /^bank\.[a-z0-9.-]+$/, 64)
      || !(item.evalDatasetId === null
        || typeof item.evalDatasetId === 'string' && item.evalDatasetId.length <= 128)
      || ids.has(item.id)) {
      throw scenarioError(`场景 ${String(item && item.id || 'unknown')} 契约无效`);
    }
    ids.add(item.id);
    item.intentIds.forEach(id => {
      if (!graphNodes.has(id) || graphNodes.get(id).type !== 'intent') {
        throw scenarioError(`场景 ${item.id} 引用了未知 Intent`);
      }
    });
    item.menuIds.forEach(id => {
      if (!graphNodes.has(id) || graphNodes.get(id).type !== 'menu') {
        throw scenarioError(`场景 ${item.id} 引用了未知菜单`);
      }
    });
    item.requiredCapabilityIds.forEach(id => {
      if (!capabilityIds.has(id)) throw scenarioError(`场景 ${item.id} 引用了未知能力`);
    });
  });
}

function count(items, predicate) {
  return items.reduce((total, item) => total + (predicate(item) ? 1 : 0), 0);
}

function ratio(ready, total) {
  return { ready, total, rate: total === 0 ? null : Math.round(ready * 10000 / total) / 100 };
}

function unverifiedProviderSummary(resources) {
  return {
    registered: resources.providers.length,
    declaredReady: count(resources.providers, item => item.health === 'ready'),
    liveHealthy: 0,
    liveUnavailable: 0,
    liveStale: 0,
    businessAvailable: 0,
    observedAt: null,
    status: 'unverified',
  };
}

function projectOverview(sources, providerHealth) {
  const { capabilities, resources, intentGraph, integration } = sources;
  const intents = intentGraph.nodes.filter(node => node.type === 'intent');
  return cloneFreeze({
    graph: { id: intentGraph.graphId, version: intentGraph.version },
    capabilities: {
      registered: capabilities.length,
      modelCallable: count(capabilities, item => item.exposure === 'model-callable'),
      userAction: count(capabilities, item => item.exposure === 'user-action'),
      systemOnly: count(capabilities, item => item.exposure === 'system-only'),
      plannable: count(capabilities, item => item.exposure !== 'system-only'),
      executable: count(capabilities, item => item.exposure === 'model-callable' && item.sideEffect === 'none'),
      enabled: integration.summary.enabledCount,
      deferred: integration.summary.deferredCount,
      platformOnly: integration.summary.platformOnlyCount,
    },
    skills: {
      registered: resources.skills.length,
      active: count(resources.skills, item => item.status === 'active'),
    },
    providers: providerHealth && providerHealth.summary
      ? { ...providerHealth.summary }
      : unverifiedProviderSummary(resources),
    intents: {
      registered: intents.length,
      ready: count(intents, item => item.readiness === 'ready'),
    },
    entries: {
      channels: count(intentGraph.nodes, item => item.type === 'channel'),
      menus: integration.summary.menuItemCount,
      actions: integration.summary.featureActionCount,
      capabilityBackedActions: integration.summary.capabilityBackedActionCount,
      readyActions: integration.summary.readyFeatureActionCount,
    },
  });
}

function projectScenarios(manifest, sources) {
  const nodes = new Map(sources.intentGraph.nodes.map(node => [node.id, node]));
  const integration = new Map(sources.integration.capabilities.map(item => [item.capabilityId, item]));
  const result = manifest.scenarios.map(item => {
    const readyIntents = count(item.intentIds, id => nodes.get(id).readiness === 'ready');
    const readyCapabilities = count(
      item.requiredCapabilityIds,
      id => integration.get(id).integrationStatus === 'enabled',
    );
    const anyReady = readyIntents > 0 || readyCapabilities > 0;
    const allReady = item.intentIds.length > 0 && item.requiredCapabilityIds.length > 0
      && readyIntents === item.intentIds.length
      && readyCapabilities === item.requiredCapabilityIds.length;
    return {
      id: item.id,
      version: item.version,
      title: item.title,
      domain: item.domain,
      owner: item.owner,
      manifestStatus: item.status,
      readinessStatus: allReady ? 'ready' : anyReady ? 'partial' : 'planned',
      intentReadiness: ratio(readyIntents, item.intentIds.length),
      capabilityReadiness: ratio(readyCapabilities, item.requiredCapabilityIds.length),
      evaluation: {
        status: 'not_evaluated',
        passed: 0,
        executed: 0,
        rate: null,
        datasetId: item.evalDatasetId,
        evaluatedAt: null,
      },
      intentIds: [...item.intentIds],
      menuIds: [...item.menuIds],
      requiredCapabilityIds: [...item.requiredCapabilityIds],
      gaps: item.requiredCapabilityIds
        .filter(id => integration.get(id).integrationStatus !== 'enabled')
        .map(id => ({
          capabilityId: id,
          integrationStatus: integration.get(id).integrationStatus,
          reason: integration.get(id).reason,
        })),
    };
  });
  return cloneFreeze(result);
}

function normalizeFilters(filters) {
  const input = filters || {};
  if (!input || typeof input !== 'object' || Array.isArray(input)
    || Object.keys(input).some(key => !ASSET_FILTERS.has(key))) {
    throw new BankApiError(422, 'validation_error', '能力资产筛选条件无效');
  }
  const type = input.type || 'all';
  const domain = input.domain || null;
  const integrationStatus = input.integrationStatus || null;
  if (!ASSET_TYPES.has(type)
    || domain !== null && !/^[a-z][a-z0-9-]{1,63}$/.test(domain)
    || integrationStatus !== null && !INTEGRATION_STATUSES.has(integrationStatus)) {
    throw new BankApiError(422, 'validation_error', '能力资产筛选条件无效');
  }
  return { type, domain, integrationStatus };
}

function projectCapabilityAssets(sources) {
  const audit = new Map(sources.integration.capabilities.map(item => [item.capabilityId, item]));
  return sources.capabilities.map(item => {
    const decision = audit.get(item.id);
    return {
      type: 'capability',
      id: item.id,
      title: item.title,
      description: item.description,
      version: item.version,
      domain: item.domain,
      exposure: item.exposure,
      sideEffect: item.sideEffect,
      riskLevel: item.riskLevel,
      requiredScopes: [...item.requiredScopes],
      confirmationPolicy: item.confirmationPolicy,
      idempotencyPolicy: item.idempotencyPolicy,
      readiness: { ...item.readiness, blockers: [...item.readiness.blockers] },
      integrationStatus: decision.integrationStatus,
      integrationReason: decision.reason,
      providerIds: [...decision.providerIds],
      skillIds: [...decision.skillIds],
      intentIds: [...decision.intentIds],
      featureActionKeys: [...decision.featureActionKeys],
      inputFields: Object.keys(item.inputSchema.properties || {}).sort(),
      outputFields: Object.keys(item.outputSchema.properties || {}).sort(),
    };
  });
}

function projectSkillAssets(sources) {
  const providerByCapability = new Map(
    sources.resources.providers.map(item => [item.capabilityId, item.providerId]),
  );
  return sources.resources.skills.map(item => ({
    type: 'skill',
    id: item.id,
    title: item.id.split('.').at(-1).replaceAll('-', ' '),
    version: item.version,
    status: item.status,
    owner: item.owner,
    riskBoundary: item.riskBoundary,
    handledIntentIds: [...item.handledIntentIds],
    requiredCapabilityIds: [...item.requiredCapabilityIds],
    businessObjectIds: [...item.businessObjectIds],
    providerIds: item.requiredCapabilityIds.map(id => providerByCapability.get(id)).filter(Boolean),
  }));
}

function projectProviderAssets(sources) {
  const capabilityById = new Map(sources.capabilities.map(item => [item.id, item]));
  return sources.resources.providers.map(item => {
    const capability = capabilityById.get(item.capabilityId);
    return {
      type: 'provider',
      id: item.providerId,
      title: item.providerId.split('.').at(-1).replaceAll('-', ' '),
      providerType: item.providerType,
      exposure: item.exposure,
      health: item.health,
      version: item.version,
      domain: capability ? capability.domain : 'platform',
      capabilityId: item.capabilityId,
    };
  });
}

function projectAssets(filters, sources) {
  const normalized = normalizeFilters(filters);
  let items = [];
  if (normalized.type === 'all' || normalized.type === 'capability') {
    items.push(...projectCapabilityAssets(sources));
  }
  if (normalized.type === 'all' || normalized.type === 'skill') {
    items.push(...projectSkillAssets(sources));
  }
  if (normalized.type === 'all' || normalized.type === 'provider') {
    items.push(...projectProviderAssets(sources));
  }
  if (normalized.domain) items = items.filter(item => item.domain === normalized.domain);
  if (normalized.integrationStatus) {
    items = items.filter(item => item.integrationStatus === normalized.integrationStatus);
  }
  items.sort((left, right) => left.id.localeCompare(right.id));
  return cloneFreeze({ filters: normalized, total: items.length, items });
}

const TRIGGER_OPERATOR_SYMBOLS = Object.freeze({
  eq: '=', ne: '≠', gt: '高于', gte: '不低于', lt: '低于', lte: '不高于', in: '属于', contains: '包含',
});
const EXPERIENCE_TYPE_ORDER = Object.freeze({ resume_task: 0, next_intent: 1, proactive_service: 2, care: 3 });
const PRIORITY_ORDER = Object.freeze({ critical: 0, high: 1, normal: 2, low: 3 });

// 触发条件树 → 单行中文摘要。只翻译算子符号，信号名保持注册表原样，
// 新增场景/算子无需改这里（泛化：摘要能力来自结构遍历，不是信号白名单）。
function summarizeTrigger(node) {
  if (!node || typeof node !== 'object' || Array.isArray(node)) return '';
  if (Array.isArray(node.all)) return node.all.map(summarizeTrigger).join(' 且 ');
  if (Array.isArray(node.any)) return node.any.map(summarizeTrigger).join(' 或 ');
  if (node.not) return '非(' + summarizeTrigger(node.not) + ')';
  if (node.operator === 'exists') return `${node.signal} 存在`;
  const symbol = TRIGGER_OPERATOR_SYMBOLS[node.operator] || node.operator;
  const value = typeof node.value === 'string' ? node.value : JSON.stringify(node.value);
  return `${node.signal} ${symbol} ${value}`;
}

function projectExperienceScenarios(registry) {
  if (!registry || !Array.isArray(registry.scenarios)) {
    throw scenarioError('体验场景注册表不可用');
  }
  const result = registry.scenarios
    .filter(item => item.status === 'active')
    .map(item => ({
      id: item.id,
      version: item.version,
      title: item.title,
      type: item.type,
      priority: item.priority,
      complianceClass: item.complianceClass === undefined ? null : item.complianceClass,
      action: item.action,
      surfaces: [...item.surfaces],
      targetIntentId: item.targetIntentId === undefined ? null : item.targetIntentId,
      workflowTemplateId: item.workflowTemplateId === undefined ? null : item.workflowTemplateId,
      supersededBy: item.supersededBy === undefined ? null : item.supersededBy,
      requiredSkillIds: [...item.requiredSkillIds],
      requiredCapabilityIds: [...item.requiredCapabilityIds],
      reasonCode: item.reasonCode,
      reasonTextTemplate: item.reasonTextTemplate,
      entryMessage: item.entryMessage === undefined ? null : item.entryMessage,
      triggerSummary: summarizeTrigger(item.trigger),
      reshowAfterHours: item.reshowAfterHours === undefined ? null : item.reshowAfterHours,
      cooldownHours: item.cooldownHours,
      memoryEnabled: Boolean(item.memory),
    }));
  result.sort((left, right) => {
    const typeDelta = (EXPERIENCE_TYPE_ORDER[left.type] !== undefined ? EXPERIENCE_TYPE_ORDER[left.type] : 9)
      - (EXPERIENCE_TYPE_ORDER[right.type] !== undefined ? EXPERIENCE_TYPE_ORDER[right.type] : 9);
    if (typeDelta) return typeDelta;
    const priorityDelta = (PRIORITY_ORDER[left.priority] !== undefined ? PRIORITY_ORDER[left.priority] : 9)
      - (PRIORITY_ORDER[right.priority] !== undefined ? PRIORITY_ORDER[right.priority] : 9);
    if (priorityDelta) return priorityDelta;
    return left.id.localeCompare(right.id);
  });
  return cloneFreeze(result);
}

// 面向用户展示的 mock 数据资产：类别是声明式目录，新增类别只需扩展目录和
// 对应 builder，不改列表/详情通道的结构（records 统一为 fields 键值投影）。
const DATA_ASSET_CATEGORIES = Object.freeze([
  {
    id: 'wealth-holdings',
    title: '理财持仓',
    description: '持仓快照：本金、市值与累计收益，来自财富核心 mock 数据。',
  },
  {
    id: 'bills',
    title: '账单',
    description: '生活缴费账单与贷款还款计划的当前状态。',
  },
  {
    id: 'transactions',
    title: '交易记录',
    description: '账户与信用卡的近期交易流水（每账户/卡取最近若干笔）。',
  },
  {
    id: 'customer-profile',
    title: '用户画像',
    description: '由权威数据构成的基础画像：客户资料、积分、风险测评、账户与信用卡。',
  },
  {
    id: 'wealth-transactions',
    title: '理财交易',
    description: '理财申购、赎回、到期兑付与分红的确认流水。',
  },
  {
    id: 'market-products',
    title: '在售产品',
    description: '货架上的存款、理财、基金与信用卡/贷款产品目录。',
  },
  {
    id: 'loans',
    title: '贷款账户',
    description: '名下贷款合同：本金、利率、剩余期数与月供。',
  },
  {
    id: 'payees',
    title: '常用收款人',
    description: '已保存的转账收款人名册。',
  },
  {
    id: 'merchant-offers',
    title: '商户优惠',
    description: '当前有效期内的商户权益与满减活动。',
  },
  {
    id: 'card-settings',
    title: '卡片设置',
    description: '信用卡的交易开关与限额配置。',
  },
]);

const MAX_TRANSACTION_RECORDS = 30;
const MAX_TRANSACTIONS_PER_SOURCE = 8;

const DATA_STATUS_LABELS = {
  HOLDING: '持有中', SETTLED: '已结清', CLOSED: '已关闭',
  UNPAID: '待缴', PAID: '已缴', OVERDUE: '已逾期',
  DUE: '待还', PARTIALLY_PAID: '部分还款',
  ACTIVE: '正常', INACTIVE: '停用', FROZEN: '冻结', EXPIRED: '已过期',
  CONFIRMED: '已确认',
  ON_SALE: '在售', LIMITED: '限量发售', SOLD_OUT: '售罄', OFF_SHELF: '已下架',
};
const DATA_CATEGORY_LABELS = {
  DEPOSIT: '存款', CERTIFICATE_OF_DEPOSIT: '大额存单', CASH_MANAGEMENT: '现金管理类',
  FIXED_INCOME_WEALTH: '固定收益类', FUND: '基金', WEALTH: '理财', INSURANCE: '保险',
  CREDIT_CARD: '信用卡', CREDIT_CARD_SERVICE: '信用卡服务', LOAN: '贷款',
};
const DATA_DIRECTION_LABELS = { CREDIT: '收入', DEBIT: '支出' };
const DATA_WEALTH_TX_DIRECTION_LABELS = {
  PURCHASE: '申购', REDEMPTION: '赎回', MATURITY: '到期兑付', DIVIDEND: '分红',
};
const DATA_PRODUCT_TERM_LABELS = {
  DEMAND: '活期', FIXED: '固定期限', OPEN_DAILY: '每日开放',
  OPEN_MONTHLY: '每月开放', NO_FIXED: '无固定期限',
};
const DATA_PRODUCT_CHANNEL_LABELS = {
  BOTH: '手机银行/柜台', MOBILE_APP: '手机银行', COUNTER: '柜台',
};
const DATA_LOAN_TYPE_LABELS = {
  MORTGAGE: '住房按揭', CREDIT: '信用贷款', CONSUMPTION: '消费贷款',
};
const DATA_REPAYMENT_METHOD_LABELS = {
  EQUAL_PAYMENT: '等额本息', EQUAL_PRINCIPAL: '等额本金',
};
const DATA_OFFER_CATEGORY_LABELS = {
  dining: '美食', movie: '影视', car: '汽车', shopping: '购物',
};
const DATA_ACCOUNT_TYPE_LABELS = {
  DEBIT_CARD: '借记卡', SAVINGS: '储蓄账户', CREDIT_CARD: '信用卡', SYSTEM: '系统账户',
};

function dataLabel(map, value, fallback) {
  if (value == null || value === '') return fallback == null ? '—' : fallback;
  return map[value] || String(value);
}

function dataMoney(minor) {
  const value = (Math.abs(Number(minor) || 0) / 100).toFixed(2);
  const withCommas = value.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const sign = Number(minor) < 0 ? '-' : '';
  return sign + '¥' + withCommas;
}

function dataSignedMoney(minor) {
  return (Number(minor) > 0 ? '+' : '') + dataMoney(minor);
}

function dataDate(value) {
  return typeof value === 'string' && value.length >= 10 ? value.slice(0, 10) : '—';
}

function dataField(label, value) {
  return { label, value: value == null || value === '' ? '—' : String(value) };
}

function dataRate(bp) {
  const num = Number(bp);
  if (!Number.isFinite(num)) return '—';
  return (num / 100).toFixed(2) + '%';
}

async function buildWealthHoldingsAsset(repository, customerId) {
  const [holdings, risk] = await Promise.all([
    repository.listWealthHoldings(customerId),
    repository.getWealthRiskAssessment(customerId),
  ]);
  const records = holdings.map(item => ({
    id: item.id,
    title: item.productName,
    subtitle: dataLabel(DATA_CATEGORY_LABELS, item.category) + ' · ' + item.id,
    statusLabel: dataLabel(DATA_STATUS_LABELS, item.status),
    fields: [
      dataField('本金', dataMoney(item.principalMinor)),
      dataField('当前市值', dataMoney(item.currentValueMinor)),
      dataField('累计收益', dataSignedMoney(item.accumulatedEarningsMinor)),
      dataField('起息日', dataDate(item.openedAt)),
      dataField('到期日', dataDate(item.maturesAt)),
      dataField('最新估值', dataDate(item.lastValuedAt)),
    ],
  }));
  const totalValue = holdings.reduce((sum, item) => sum + item.currentValueMinor, 0);
  const totalEarnings = holdings.reduce((sum, item) => sum + item.accumulatedEarningsMinor, 0);
  const metrics = [
    { label: '持仓笔数', value: String(holdings.length) },
    { label: '总市值', value: dataMoney(totalValue) },
    { label: '累计收益', value: dataSignedMoney(totalEarnings) },
  ];
  if (risk) {
    metrics.push({ label: '风险测评', value: risk.level + ' · ' + dataLabel(DATA_STATUS_LABELS, risk.status) });
  }
  return { records, metrics };
}

async function buildBillsAsset(repository, customerId) {
  const [bills, loans] = await Promise.all([
    repository.listUtilityBills(customerId),
    repository.listLoans(customerId),
  ]);
  const repaymentGroups = await Promise.all(
    loans.map(async loan => ({ loan, items: await repository.listLoanRepayments(customerId, loan.id) })),
  );
  const isUnpaid = status => !['PAID', 'SETTLED'].includes(status);
  const records = [
    ...bills.map(item => ({
      id: item.id,
      title: item.name,
      subtitle: '生活缴费 · ' + (item.period || '—'),
      statusLabel: dataLabel(DATA_STATUS_LABELS, item.status),
      fields: [
        dataField('金额', dataMoney(item.amountMinor)),
        dataField('账期', item.period),
        dataField('到期日', dataDate(item.dueDate)),
      ],
    })),
    ...repaymentGroups.flatMap(({ loan, items }) => items.map(item => ({
      id: item.id,
      title: (loan ? loan.productName : '贷款') + ' 第 ' + item.periodNo + ' 期',
      subtitle: '还款计划 · 到期 ' + dataDate(item.dueDate),
      statusLabel: dataLabel(DATA_STATUS_LABELS, item.status),
      fields: [
        dataField('应还总额', dataMoney(item.totalMinor)),
        dataField('本金', dataMoney(item.principalMinor)),
        dataField('利息', dataMoney(item.interestMinor)),
        dataField('到期日', dataDate(item.dueDate)),
        dataField('实际还款日', dataDate(item.paidAt)),
      ],
    }))),
  ];
  const unpaidBills = bills.filter(item => isUnpaid(item.status));
  const unpaidRepayments = repaymentGroups.flatMap(({ items }) => items
    .filter(item => isUnpaid(item.status)));
  const unpaidCount = unpaidBills.length + unpaidRepayments.length;
  const unpaidSum = unpaidBills.reduce((sum, item) => sum + item.amountMinor, 0)
    + unpaidRepayments.reduce((sum, item) => sum + item.totalMinor, 0);
  return {
    records,
    metrics: [
      { label: '账单/期次', value: String(records.length) },
      { label: '待缴笔数', value: String(unpaidCount) },
      { label: '待缴合计', value: dataMoney(unpaidSum) },
    ],
  };
}

async function buildTransactionsAsset(repository, customerId) {
  const [accounts, cards] = await Promise.all([
    repository.listAccounts(customerId),
    repository.listCreditCards(customerId),
  ]);
  const sources = await Promise.all([
    ...accounts.map(async account => ({
      key: account.maskedNo,
      rows: (await repository.listAccountTransactionDetails(customerId, account.id,
        { limit: MAX_TRANSACTIONS_PER_SOURCE })).data
        .map(item => ({
          occurredAt: item.occurredAt,
          id: item.id,
          title: item.counterparty || item.description,
          subtitle: account.name + ' · ' + account.maskedNo,
          statusLabel: null,
          fields: [
            dataField('交易时间', item.occurredAt),
            dataField('方向', dataLabel(DATA_DIRECTION_LABELS, item.direction)),
            dataField('金额', dataMoney(item.amountMinor)),
            dataField('对方', item.counterparty),
            dataField('分类', item.category),
            dataField('渠道', item.channel),
            dataField('交易后余额', dataMoney(item.balanceAfterMinor)),
          ],
          direction: item.direction,
          amountMinor: item.amountMinor,
        })),
    })),
    ...cards.map(async card => ({
      key: card.maskedNo,
      rows: (await repository.listCreditCardTransactions(card.id,
        { limit: MAX_TRANSACTIONS_PER_SOURCE })).data
        .map(item => ({
          occurredAt: item.occurredAt,
          id: item.id,
          title: item.description,
          subtitle: card.productName + ' · ' + card.maskedNo,
          statusLabel: null,
          fields: [
            dataField('交易时间', item.occurredAt),
            dataField('方向', dataLabel(DATA_DIRECTION_LABELS, item.direction)),
            dataField('金额', dataMoney(item.amountMinor)),
            dataField('状态', item.status),
          ],
          direction: item.direction,
          amountMinor: item.amountMinor,
        })),
    })),
  ]);
  const allRows = sources.flatMap(source => source.rows)
    .sort((left, right) => String(right.occurredAt).localeCompare(String(left.occurredAt)));
  const inflow = allRows
    .filter(item => item.direction === 'CREDIT')
    .reduce((sum, item) => sum + item.amountMinor, 0);
  const outflow = allRows
    .filter(item => item.direction === 'DEBIT')
    .reduce((sum, item) => sum + item.amountMinor, 0);
  const records = allRows.slice(0, MAX_TRANSACTION_RECORDS).map(item => ({
    id: item.id,
    title: item.title,
    subtitle: item.subtitle,
    statusLabel: item.statusLabel,
    fields: item.fields,
  }));
  return {
    records,
    metrics: [
      { label: '展示笔数', value: String(records.length) },
      { label: '流入合计', value: dataMoney(inflow) },
      { label: '流出合计', value: dataMoney(outflow) },
    ],
  };
}

async function buildProfileAsset(repository, customerId) {
  const [customer, points, risk, accounts, cards, holdings] = await Promise.all([
    repository.getCustomer(customerId),
    repository.getCustomerPoints(customerId),
    repository.getWealthRiskAssessment(customerId),
    repository.listAccounts(customerId),
    repository.listCreditCards(customerId),
    repository.listWealthHoldings(customerId),
  ]);
  const balances = await Promise.all(accounts.map(async account => ({
    account, balance: await repository.getBalance(account.id),
  })));
  const records = [];
  if (customer) {
    records.push({
      id: 'profile-basic',
      title: customer.name,
      subtitle: '客户资料 · ' + customer.id,
      statusLabel: dataLabel(DATA_STATUS_LABELS, customer.status),
      fields: [
        dataField('客户号', customer.id),
        dataField('城市', customer.city),
        dataField('客户状态', dataLabel(DATA_STATUS_LABELS, customer.status)),
      ],
    });
  }
  if (points) {
    records.push({
      id: 'profile-points',
      title: '积分',
      subtitle: '客户权益',
      statusLabel: null,
      fields: [
        dataField('当前积分', points.balance),
        dataField('即将过期', points.expiringAmount),
        dataField('到期日', dataDate(points.expiryDate)),
      ],
    });
  }
  if (risk) {
    records.push({
      id: 'profile-risk',
      title: '财富风险测评',
      subtitle: '测评版本 v' + risk.version,
      statusLabel: dataLabel(DATA_STATUS_LABELS, risk.status),
      fields: [
        dataField('风险等级', risk.level),
        dataField('测评状态', dataLabel(DATA_STATUS_LABELS, risk.status)),
        dataField('测评时间', dataDate(risk.assessedAt)),
        dataField('有效期至', dataDate(risk.expiresAt)),
      ],
    });
  }
  balances.forEach(({ account, balance }) => {
    records.push({
      id: 'profile-account-' + account.id,
      title: account.name + ' ' + account.maskedNo,
      subtitle: dataLabel(DATA_ACCOUNT_TYPE_LABELS, account.type) + ' · ' + account.currency,
      statusLabel: dataLabel(DATA_STATUS_LABELS, account.status),
      fields: balance ? [
        dataField('可用余额', dataMoney(balance.availableMinor)),
        dataField('账面余额', dataMoney(balance.ledgerMinor)),
        dataField('更新时间', balance.updatedAt),
      ] : [dataField('状态', dataLabel(DATA_STATUS_LABELS, account.status))],
    });
  });
  cards.forEach(card => {
    records.push({
      id: 'profile-card-' + card.id,
      title: card.productName + ' ' + card.maskedNo,
      subtitle: card.network + ' · ' + card.cardType,
      statusLabel: dataLabel(DATA_STATUS_LABELS, card.status),
      fields: [
        dataField('总额度', dataMoney(card.totalLimitMinor)),
        dataField('可用额度', dataMoney(card.availableLimitMinor)),
        dataField('取现额度', dataMoney(card.cashLimitMinor)),
        dataField('账单日', '每月 ' + card.statementDay + ' 日'),
        dataField('还款日', '每月 ' + card.dueDay + ' 日'),
      ],
    });
  });
  const totalValue = holdings.reduce((sum, item) => sum + item.currentValueMinor, 0);
  records.push({
    id: 'profile-asset-overview',
    title: '资产概览',
    subtitle: '由理财持仓汇总',
    statusLabel: null,
    fields: [
      dataField('持仓笔数', holdings.length),
      dataField('持仓总市值', dataMoney(totalValue)),
      dataField('账户数', accounts.length),
    ],
  });
  return {
    records,
    metrics: [
      { label: '账户数', value: String(accounts.length) },
      { label: '信用卡数', value: String(cards.length) },
      { label: '积分余额', value: points ? String(points.balance) : '—' },
      { label: '风险等级', value: risk ? risk.level : '—' },
    ],
  };
}

async function buildWealthTransactionsAsset(repository, customerId) {
  const rows = await repository.listWealthTransactions(customerId);
  const sorted = rows.slice()
    .sort((left, right) => String(right.occurredAt).localeCompare(String(left.occurredAt)));
  const records = sorted.map(item => ({
    id: item.id,
    title: dataLabel(DATA_WEALTH_TX_DIRECTION_LABELS, item.direction) + ' · ' + item.productName,
    subtitle: '理财交易 · ' + item.id,
    statusLabel: dataLabel(DATA_STATUS_LABELS, item.status),
    fields: [
      dataField('发生时间', item.occurredAt),
      dataField('方向', dataLabel(DATA_WEALTH_TX_DIRECTION_LABELS, item.direction)),
      dataField('金额', dataMoney(item.amountMinor)),
      dataField('确认收益', dataSignedMoney(item.earningsMinor)),
      dataField('状态', dataLabel(DATA_STATUS_LABELS, item.status)),
    ],
  }));
  const sumBy = direction => sorted
    .filter(item => item.direction === direction)
    .reduce((sum, item) => sum + item.amountMinor, 0);
  return {
    records,
    metrics: [
      { label: '交易笔数', value: String(records.length) },
      { label: '申购合计', value: dataMoney(sumBy('PURCHASE')) },
      { label: '赎回合计', value: dataMoney(sumBy('REDEMPTION')) },
    ],
  };
}

async function buildMarketProductsAsset(repository) {
  const products = await repository.listFinancialProducts();
  const sorted = products.slice()
    .sort((left, right) => left.sortOrder - right.sortOrder || left.id.localeCompare(right.id));
  const records = sorted.map(item => ({
    id: item.id,
    title: item.name,
    subtitle: dataLabel(DATA_CATEGORY_LABELS, item.category) + ' · ' + (item.issuer || item.productCode),
    statusLabel: dataLabel(DATA_STATUS_LABELS, item.status),
    fields: [
      dataField('业绩基准', item.annualRateBp == null ? item.rateNote : dataRate(item.annualRateBp)),
      dataField('风险等级', item.riskLevel),
      dataField('起购金额', dataMoney(item.minPurchaseMinor)),
      dataField('期限', item.termDays != null
        ? item.termDays + ' 天'
        : dataLabel(DATA_PRODUCT_TERM_LABELS, item.termType, item.termType)),
      dataField('购买渠道', dataLabel(DATA_PRODUCT_CHANNEL_LABELS, item.purchaseChannel)),
      dataField('发行方', item.issuer),
    ],
  }));
  const onSale = sorted.filter(item => ['ON_SALE', 'LIMITED'].includes(item.status));
  const rates = onSale.map(item => item.annualRateBp).filter(bp => Number.isFinite(Number(bp)));
  const maxRate = rates.length ? Math.max(...rates) : null;
  const lowRisk = onSale.filter(item => ['R1', 'R2'].includes(item.riskLevel)).length;
  return {
    records,
    metrics: [
      { label: '产品总数', value: String(records.length) },
      { label: '在售/限量', value: String(onSale.length) },
      { label: '低风险(R1-R2)', value: String(lowRisk) },
      { label: '最高业绩基准', value: maxRate == null ? '—' : dataRate(maxRate) },
    ],
  };
}

async function buildLoansAsset(repository, customerId) {
  const loans = await repository.listLoans(customerId);
  const records = loans.map(item => ({
    id: item.id,
    title: item.productName,
    subtitle: dataLabel(DATA_LOAN_TYPE_LABELS, item.loanType, item.loanType) + ' · 合同 ' + item.contractNo,
    statusLabel: dataLabel(DATA_STATUS_LABELS, item.status),
    fields: [
      dataField('贷款本金', dataMoney(item.principalMinor)),
      dataField('剩余本金', dataMoney(item.outstandingPrincipalMinor)),
      dataField('年利率', dataRate(item.annualRateBp)),
      dataField('还款方式', dataLabel(DATA_REPAYMENT_METHOD_LABELS, item.repaymentMethod, item.repaymentMethod)),
      dataField('总期数', item.termMonths + ' 期'),
      dataField('剩余期数', item.remainingMonths + ' 期'),
      dataField('月供', dataMoney(item.monthlyPaymentMinor) + ' · 每月 ' + item.monthlyPaymentDay + ' 日'),
      dataField('放款日', dataDate(item.disbursedAt)),
      dataField('结清日', dataDate(item.settledAt)),
    ],
  }));
  const active = loans.filter(item => item.status !== 'SETTLED');
  const outstanding = active.reduce((sum, item) => sum + item.outstandingPrincipalMinor, 0);
  return {
    records,
    metrics: [
      { label: '贷款笔数', value: String(loans.length) },
      { label: '未结清', value: String(active.length) },
      { label: '剩余本金合计', value: dataMoney(outstanding) },
    ],
  };
}

async function buildPayeesAsset(repository, customerId) {
  const payees = await repository.listPayees(customerId);
  const records = payees.map(item => ({
    id: item.id,
    title: item.name,
    subtitle: (item.bankName || '—') + ' · ' + item.maskedNo,
    statusLabel: dataLabel(DATA_STATUS_LABELS, item.status),
    fields: [
      dataField('收款人', item.name),
      dataField('开户行', item.bankName),
      dataField('账号', item.maskedNo),
      dataField('状态', dataLabel(DATA_STATUS_LABELS, item.status)),
    ],
  }));
  return {
    records,
    metrics: [
      { label: '收款人数', value: String(records.length) },
      { label: '可用', value: String(payees.filter(item => item.status === 'ACTIVE').length) },
    ],
  };
}

async function buildMerchantOffersAsset(repository) {
  const offers = await repository.listActiveOffers();
  const records = offers.map(item => ({
    id: item.id,
    title: item.merchant,
    subtitle: dataLabel(DATA_OFFER_CATEGORY_LABELS, item.category, item.category) + ' · ' + item.discountText,
    statusLabel: '剩 ' + item.daysToExpiry + ' 天',
    fields: [
      dataField('商户', item.merchant),
      dataField('优惠内容', item.discountText),
      dataField('类别', dataLabel(DATA_OFFER_CATEGORY_LABELS, item.category, item.category)),
      dataField('生效日', dataDate(item.validFrom)),
      dataField('到期日', dataDate(item.validTo)),
      dataField('剩余天数', item.daysToExpiry + ' 天'),
    ],
  }));
  const expiringSoon = offers.filter(item => item.daysToExpiry <= 7).length;
  return {
    records,
    metrics: [
      { label: '有效优惠', value: String(records.length) },
      { label: '7 天内到期', value: String(expiringSoon) },
    ],
  };
}

async function buildCardSettingsAsset(repository, customerId) {
  const cards = await repository.listCreditCards(customerId);
  const settings = await Promise.all(cards.map(card => repository.getCreditCardSettings(card.id)));
  const records = cards.map((card, index) => {
    const setting = settings[index];
    return {
      id: 'card-settings-' + card.id,
      title: card.productName + ' ' + card.maskedNo,
      subtitle: '卡片设置 · ' + card.network,
      statusLabel: null,
      fields: [
        dataField('线上交易', setting && setting.onlineTransactionsEnabled ? '已开启' : '已关闭'),
        dataField('闪付', setting && setting.contactlessEnabled ? '已开启' : '已关闭'),
        dataField('境外交易', setting && setting.overseasEnabled ? '已开启' : '已关闭'),
        dataField('单笔限额', setting ? dataMoney(setting.singleTransactionLimitMinor) : '—'),
        dataField('日累计限额', setting ? dataMoney(setting.dailyLimitMinor) : '—'),
        dataField('更新时间', setting ? setting.updatedAt : '—'),
      ],
    };
  });
  const onlineOn = settings.filter(item => item && item.onlineTransactionsEnabled).length;
  return {
    records,
    metrics: [
      { label: '信用卡数', value: String(records.length) },
      { label: '线上交易已开启', value: String(onlineOn) },
    ],
  };
}

const DATA_ASSET_BUILDERS = {
  'wealth-holdings': buildWealthHoldingsAsset,
  bills: buildBillsAsset,
  transactions: buildTransactionsAsset,
  'customer-profile': buildProfileAsset,
  'wealth-transactions': buildWealthTransactionsAsset,
  'market-products': buildMarketProductsAsset,
  loans: buildLoansAsset,
  payees: buildPayeesAsset,
  'merchant-offers': buildMerchantOffersAsset,
  'card-settings': buildCardSettingsAsset,
};

async function buildDataAsset(repository, customerId, category) {
  if (!repository) {
    throw new BankApiError(503, 'data_assets_unavailable', '数据资产服务暂不可用');
  }
  const builder = DATA_ASSET_BUILDERS[category.id];
  if (!builder) {
    throw new BankApiError(503, 'data_assets_unavailable', '数据资产类别缺少构建器');
  }
  return builder(repository, customerId);
}

function createWorkbenchService(options = {}) {
  if (!options.intentGraph || !Array.isArray(options.intentGraph.nodes)) {
    throw new TypeError('intentGraph is required');
  }
  const scenarioManifest = options.scenarioManifest || readScenarioManifest();
  const integration = options.integration || auditAgentIntegration();
  const resources = options.resources || loadBusinessResourceRegistry();
  const capabilities = options.capabilities || capabilityRegistry.list({ audience: 'all' });
  const providerHealthService = options.providerHealthService || null;
  const experienceRegistry = options.experienceRegistry || null;
  const repository = options.repository || null;
  validateScenarioManifest(scenarioManifest, { intentGraph: options.intentGraph, capabilities });
  const sources = Object.freeze({
    intentGraph: options.intentGraph,
    integration,
    resources,
    capabilities,
  });
  return Object.freeze({
    getOverview: async () => {
      const providerHealth = providerHealthService
        ? await (typeof providerHealthService.ensureFresh === 'function'
          ? providerHealthService.ensureFresh()
          : providerHealthService.getSnapshot())
        : null;
      return projectOverview(sources, providerHealth);
    },
    listScenarios: async () => projectScenarios(scenarioManifest, sources),
    listAssets: async filters => projectAssets(filters, sources),
    listExperienceScenarios: async () => projectExperienceScenarios(experienceRegistry),
    listDataAssets: async customerId => {
      const assets = await Promise.all(
        DATA_ASSET_CATEGORIES.map(async category => {
          const built = await buildDataAsset(repository, customerId, category);
          return {
            id: category.id,
            title: category.title,
            description: category.description,
            recordCount: built.records.length,
            metrics: built.metrics,
          };
        }),
      );
      return cloneFreeze({ total: assets.length, items: assets });
    },
    getDataAssetDetail: async (customerId, categoryId) => {
      const category = DATA_ASSET_CATEGORIES.find(item => item.id === categoryId);
      if (!category) {
        throw new BankApiError(404, 'data_asset_category_not_found', '数据资产类别不存在');
      }
      const built = await buildDataAsset(repository, customerId, category);
      return cloneFreeze({
        category: { id: category.id, title: category.title, description: category.description },
        recordCount: built.records.length,
        metrics: built.metrics,
        records: built.records,
      });
    },
  });
}

module.exports = { createWorkbenchService };
