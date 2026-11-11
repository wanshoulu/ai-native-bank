'use strict';

const fs = require('node:fs');
const path = require('node:path');

const REGISTRY_DIR = path.resolve(__dirname, '../../data/ai-native-bank/registries');
const PERSONA_PATH = path.resolve(__dirname, '../../data/ai-native-bank/mock-personas.v1.json');
const ALLOWED_OPERATORS = new Set(['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'contains', 'exists']);
const ALLOWED_SURFACES = new Set(['home', 'agent', 'channel']);
const ALLOWED_ACTIONS = new Set(['START_ASSISTANCE', 'OPEN_CATALOG']);
const ALLOWED_TYPES = new Set(['proactive_service', 'next_intent', 'care']);
const ALLOWED_PRIORITIES = new Set(['critical', 'high', 'normal', 'low']);
const ALLOWED_SIGNAL_TYPES = new Set(['boolean', 'integer', 'number', 'string', 'string_array']);
const ALLOWED_CLASSIFICATIONS = new Set(['financial', 'context', 'preference', 'workflow']);
const PLANNED_INTENT_IDS = new Set([
  'ccb.cashflow.salary-plan', 'ccb.cashflow.spending-explain', 'ccb.obligation.repay-all',
]);
const PLANNED_SKILL_IDS = new Set([
  'bank.cashflow-planning', 'bank.spending-analysis', 'bank.obligation-management',
]);
const PLANNED_CAPABILITY_IDS = new Set([
  'bank.cashflow.salary-plan.get', 'bank.cashflow.spending-analysis.get', 'bank.obligation.list',
  'bank.loan.repayment.create', 'bank.utility.bill.list', 'bank.utility.payment.create',
]);

class ExperienceRegistryError extends Error {
  constructor(message) {
    super('experience_registry_invalid: ' + message);
    this.name = 'ExperienceRegistryError';
    this.code = 'experience_registry_invalid';
  }
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function readDefaultManifests() {
  return {
    scenarios: readJson(path.join(REGISTRY_DIR, 'experience-scenarios.v1.json')),
    signals: readJson(path.join(REGISTRY_DIR, 'signal-providers.v1.json')),
    workflows: readJson(path.join(REGISTRY_DIR, 'workflow-templates.v1.json')),
    personas: readJson(PERSONA_PATH),
  };
}

function exact(value, keys, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(label + ' must be an object');
  const allowed = new Set(keys);
  const unknown = Object.keys(value).filter(key => !allowed.has(key));
  if (unknown.length) fail(label + ' has unknown fields: ' + unknown.join(','));
  const missing = keys.filter(key => !Object.prototype.hasOwnProperty.call(value, key));
  if (missing.length) fail(label + ' is missing fields: ' + missing.join(','));
}

function fail(message) { throw new ExperienceRegistryError(message); }
function identifier(value, pattern, label) {
  if (typeof value !== 'string' || !pattern.test(value)) fail(label + ' is invalid');
}
function array(value, max, label) {
  if (!Array.isArray(value) || value.length > max) fail(label + ' must be a bounded array');
  if (new Set(value).size !== value.length) fail(label + ' must contain unique values');
}

function validateCondition(node, signalIds, label) {
  if (!node || typeof node !== 'object' || Array.isArray(node)) fail(label + ' must be an object');
  const keys = Object.keys(node);
  if (keys.length === 1 && ['all', 'any'].includes(keys[0])) {
    const children = node[keys[0]];
    if (!Array.isArray(children) || children.length < 1 || children.length > 16) fail(label + ' group is invalid');
    children.forEach((child, index) => validateCondition(child, signalIds, label + '.' + keys[0] + '[' + index + ']'));
    return;
  }
  if (keys.length === 1 && keys[0] === 'not') {
    validateCondition(node.not, signalIds, label + '.not');
    return;
  }
  exact(node, ['signal', 'operator', 'value'], label);
  identifier(node.signal, /^[a-z][a-z0-9_.-]{2,95}$/, label + '.signal');
  if (!signalIds.has(node.signal)) fail(label + '.signal references unknown signal ' + node.signal);
  if (!ALLOWED_OPERATORS.has(node.operator)) fail(label + '.operator is invalid');
}

function validateSignals(manifest) {
  exact(manifest, ['schemaVersion', 'signals'], 'signals manifest');
  if (manifest.schemaVersion !== '1.0') fail('signals schemaVersion is unsupported');
  if (!Array.isArray(manifest.signals) || manifest.signals.length < 1 || manifest.signals.length > 128) fail('signals must be a bounded non-empty array');
  const ids = new Set();
  manifest.signals.forEach((item, index) => {
    exact(item, ['id', 'providerId', 'valueType', 'classification', 'freshnessSeconds'], 'signals[' + index + ']');
    identifier(item.id, /^[a-z][a-z0-9_.-]{2,95}$/, 'signal id');
    identifier(item.providerId, /^[a-z][a-z0-9_.-]{1,95}$/, 'provider id');
    if (ids.has(item.id)) fail('duplicate signal ' + item.id);
    if (!ALLOWED_SIGNAL_TYPES.has(item.valueType)) fail('signal valueType is invalid');
    if (!ALLOWED_CLASSIFICATIONS.has(item.classification)) fail('signal classification is invalid');
    if (!Number.isInteger(item.freshnessSeconds) || item.freshnessSeconds < 1 || item.freshnessSeconds > 2678400) fail('signal freshnessSeconds is invalid');
    ids.add(item.id);
  });
  return ids;
}

function validateWorkflows(manifest, capabilityIds, allowPlannedAssets) {
  exact(manifest, ['schemaVersion', 'workflowTemplates'], 'workflow manifest');
  if (manifest.schemaVersion !== '1.0') fail('workflow schemaVersion is unsupported');
  if (!Array.isArray(manifest.workflowTemplates) || manifest.workflowTemplates.length < 1 || manifest.workflowTemplates.length > 64) fail('workflowTemplates must be bounded');
  const ids = new Set();
  manifest.workflowTemplates.forEach((template, templateIndex) => {
    exact(template, ['id', 'version', 'title', 'assistancePolicy', 'steps'], 'workflowTemplates[' + templateIndex + ']');
    identifier(template.id, /^[a-z0-9-]+-v[0-9]+$/, 'workflow template id');
    identifier(template.version, /^[0-9]+\.[0-9]+\.[0-9]+$/, 'workflow template version');
    if (ids.has(template.id)) fail('duplicate workflow template ' + template.id);
    if (typeof template.title !== 'string' || !template.title || template.title.length > 80) fail('workflow title is invalid');
    validateAssistancePolicy(template.assistancePolicy);
    if (!Array.isArray(template.steps) || template.steps.length < 1 || template.steps.length > 16) fail('workflow steps are invalid');
    const stepIds = new Set();
    template.steps.forEach((step, stepIndex) => {
      exact(step, ['id', 'title', 'dependsOn', 'requiredCapabilityIds', 'confirmationLevel', 'optional', 'order'], 'workflow step');
      identifier(step.id, /^[a-z][a-z0-9_]{2,63}$/, 'workflow step id');
      if (stepIds.has(step.id)) fail('duplicate workflow step ' + step.id);
      array(step.dependsOn, 8, 'workflow dependsOn');
      array(step.requiredCapabilityIds, 16, 'workflow requiredCapabilityIds');
      step.requiredCapabilityIds.forEach(id => requireCapability(id, capabilityIds, allowPlannedAssets));
      if (!['none', 'plan', 'transaction'].includes(step.confirmationLevel)) fail('confirmationLevel is invalid');
      if (typeof step.optional !== 'boolean' || !Number.isInteger(step.order)) fail('workflow step state is invalid');
      stepIds.add(step.id);
      if (step.order !== stepIndex + 1) fail('workflow step order must be contiguous');
    });
    template.steps.forEach(step => step.dependsOn.forEach(id => {
      if (!stepIds.has(id)) fail('workflow dependency references unknown step ' + id);
    }));
    ids.add(template.id);
  });
  return ids;
}

function validateAssistancePolicy(policy) {
  if (policy === null) return;
  exact(policy, [
    'mode', 'activation', 'complexityReasons', 'homeVisible', 'maxInactiveHours',
  ], 'workflow assistancePolicy');
  if (policy.mode !== 'smart_companion' || policy.activation !== 'on_user_start'
    || policy.homeVisible !== true
    || !Number.isInteger(policy.maxInactiveHours)
    || policy.maxInactiveHours < 1 || policy.maxInactiveHours > 720) {
    fail('workflow assistancePolicy is invalid');
  }
  const allowedReasons = new Set([
    'cross_product', 'multi_step', 'decision_support', 'complex_materials', 'high_impact',
  ]);
  if (!Array.isArray(policy.complexityReasons) || policy.complexityReasons.length < 1
    || policy.complexityReasons.length > 5
    || new Set(policy.complexityReasons).size !== policy.complexityReasons.length
    || policy.complexityReasons.some(reason => !allowedReasons.has(reason))) {
    fail('workflow assistancePolicy complexityReasons are invalid');
  }
}

function requireCapability(id, capabilityIds, allowPlannedAssets) {
  identifier(id, /^bank\.[a-z0-9.-]+$/, 'capability id');
  if (!capabilityIds.has(id) && !(allowPlannedAssets && PLANNED_CAPABILITY_IDS.has(id))) {
    fail('requiredCapabilityIds references unknown capability ' + id);
  }
}

function validateScenarioMemory(memory, label) {
  if (memory === undefined) return;
  if (!memory || typeof memory !== 'object' || Array.isArray(memory)) fail(label + ' is invalid');
  exact(memory, ['purpose', 'predicates', 'sensitivityCeiling', 'maxItems', 'reasonTextTemplate'], label);
  identifier(memory.purpose, /^[a-z][a-z0-9_]{2,63}$/, label + '.purpose');
  if (!Array.isArray(memory.predicates) || memory.predicates.length < 1 || memory.predicates.length > 4
    || memory.predicates.some(id => !/^[a-z][a-z0-9_]{2,63}$/.test(id))) fail(label + '.predicates is invalid');
  if (!['L1', 'L2'].includes(memory.sensitivityCeiling)) fail(label + '.sensitivityCeiling is invalid');
  if (!Number.isInteger(memory.maxItems) || memory.maxItems < 1 || memory.maxItems > 4) fail(label + '.maxItems is invalid');
  if (typeof memory.reasonTextTemplate !== 'string' || !memory.reasonTextTemplate
    || memory.reasonTextTemplate.length > 160 || /[<>]/.test(memory.reasonTextTemplate)) {
    fail(label + '.reasonTextTemplate is invalid');
  }
  const placeholders = memory.reasonTextTemplate.match(/\{[a-z0-9_]+\}/g) || [];
  if (placeholders.some(token => !memory.predicates.includes(token.slice(1, -1)))) {
    fail(label + '.reasonTextTemplate references unknown predicate placeholder');
  }
}

function validateScenarios(manifest, references) {
  exact(manifest, ['schemaVersion', 'scenarios'], 'scenario manifest');
  if (manifest.schemaVersion !== '1.0') fail('scenario schemaVersion is unsupported');
  if (!Array.isArray(manifest.scenarios) || manifest.scenarios.length < 1 || manifest.scenarios.length > 128) fail('scenarios must be bounded');
  const ids = new Set();
  manifest.scenarios.forEach((item, index) => {
    exact(item, [
      'id', 'version', 'title', 'status', 'type', 'targetIntentId', 'requiredSkillIds',
      'requiredCapabilityIds', 'workflowTemplateId', 'trigger', 'surfaces', 'cooldownHours',
      'action', 'priority', 'reasonCode', 'reasonTextTemplate', 'entryMessage', 'legacyType', 'catalogAction',
      'supersededBy', 'reshowAfterHours',
      ...(item.complianceClass !== undefined ? ['complianceClass'] : []),
      ...(item.memory !== undefined ? ['memory'] : []),
    ], 'scenarios[' + index + ']');
    identifier(item.id, /^[a-z][a-z0-9-]{2,95}$/, 'scenario id');
    identifier(item.version, /^[0-9]+\.[0-9]+\.[0-9]+$/, 'scenario version');
    if (ids.has(item.id)) fail('duplicate scenario ' + item.id);
    if (typeof item.title !== 'string' || !item.title || item.title.length > 80) fail('scenario title is invalid');
    if (!['active', 'inactive'].includes(item.status) || !ALLOWED_TYPES.has(item.type)) fail('scenario lifecycle is invalid');
    if (!ALLOWED_ACTIONS.has(item.action) || !ALLOWED_PRIORITIES.has(item.priority)) fail('scenario action or priority is invalid');
    array(item.requiredSkillIds, 16, 'requiredSkillIds');
    array(item.requiredCapabilityIds, 32, 'requiredCapabilityIds');
    array(item.surfaces, 3, 'surfaces');
    if (!item.surfaces.length || item.surfaces.some(surface => !ALLOWED_SURFACES.has(surface))) fail('scenario surfaces are invalid');
    if (!Number.isInteger(item.cooldownHours) || item.cooldownHours < 0 || item.cooldownHours > 8760) fail('cooldownHours is invalid');
    // 展示频控：声明后该场景一次展示会抑制同场景在 N 小时内重新生成（问候/提示类防打扰），
    // null 表示持续提醒型（如账单到期）不做展示冷却。
    if (item.reshowAfterHours !== null
      && (!Number.isInteger(item.reshowAfterHours) || item.reshowAfterHours < 1 || item.reshowAfterHours > 168)) {
      fail('reshowAfterHours is invalid');
    }
    if (item.targetIntentId != null && !references.intentIds.has(item.targetIntentId)
      && !(references.allowPlannedAssets && PLANNED_INTENT_IDS.has(item.targetIntentId))) {
      fail('targetIntentId references unknown intent ' + item.targetIntentId);
    }
    item.requiredSkillIds.forEach(id => {
      if (!references.skillIds.has(id) && !(references.allowPlannedAssets && PLANNED_SKILL_IDS.has(id))) fail('requiredSkillIds references unknown skill ' + id);
    });
    item.requiredCapabilityIds.forEach(id => requireCapability(id, references.capabilityIds, references.allowPlannedAssets));
    if (item.workflowTemplateId != null && !references.workflowIds.has(item.workflowTemplateId)) fail('workflowTemplateId references unknown template ' + item.workflowTemplateId);
    if (item.action === 'START_ASSISTANCE' && (!item.targetIntentId || !item.workflowTemplateId)) fail('guided scenario requires targetIntentId and workflowTemplateId');
    if (item.action === 'START_ASSISTANCE'
      && (typeof item.entryMessage !== 'string' || !item.entryMessage || item.entryMessage.length > 240 || /[<>]/.test(item.entryMessage))) {
      fail('guided scenario entryMessage is invalid');
    }
    if (item.action !== 'START_ASSISTANCE' && item.entryMessage !== null) fail('catalog scenario entryMessage must be null');
    if (item.action === 'OPEN_CATALOG' && (!item.catalogAction || typeof item.catalogAction !== 'object')) fail('catalog scenario requires catalogAction');
    if (item.catalogAction != null) exact(item.catalogAction, ['channel', 'item', 'action'], 'catalogAction');
    identifier(item.reasonCode, /^[A-Z][A-Z0-9_]{2,95}$/, 'reasonCode');
    if (typeof item.reasonTextTemplate !== 'string' || !item.reasonTextTemplate || item.reasonTextTemplate.length > 160) fail('reasonTextTemplate is invalid');
    if (item.complianceClass !== undefined && !['service', 'marketing', 'risk'].includes(item.complianceClass)) {
      fail('scenario complianceClass is invalid');
    }
    validateScenarioMemory(item.memory, 'scenarios[' + index + '].memory');
    validateCondition(item.trigger, references.signalIds, 'scenario trigger');
    // 覆盖关系声明：声明本场景被聚合场景覆盖（聚合场景在候选中时本场景被抑制），
    // 例如信用卡账单/贷款提醒被「全部待还事项」聚合卡覆盖。引用完整性在此 fail-closed。
    if (item.supersededBy !== null) {
      identifier(item.supersededBy, /^[a-z][a-z0-9-]{2,95}$/, 'scenario supersededBy');
      if (item.supersededBy === item.id) fail('scenario supersededBy must reference another scenario');
    }
    ids.add(item.id);
  });
  manifest.scenarios.forEach(item => {
    if (item.supersededBy != null && !ids.has(item.supersededBy)) {
      fail('supersededBy references unknown scenario ' + item.supersededBy);
    }
  });
}

function validatePersonas(manifest, scenarioIds) {
  exact(manifest, ['schemaVersion', 'personas'], 'persona manifest');
  if (manifest.schemaVersion !== '1.0' || !Array.isArray(manifest.personas) || manifest.personas.length > 16) fail('persona manifest is invalid');
  manifest.personas.forEach((item, index) => {
    exact(item, ['id', 'customerId', 'clock', 'resetVersion', 'cashflowPolicy', 'events', 'expectedRecommendationScenarioIds'], 'personas[' + index + ']');
    identifier(item.id, /^[a-z][a-z0-9-]{2,63}$/, 'persona id');
    identifier(item.customerId, /^[A-Za-z0-9_.:-]+$/, 'persona customerId');
    identifier(item.resetVersion, /^[0-9]+\.[0-9]+\.[0-9]+$/, 'persona resetVersion');
    if (Number.isNaN(Date.parse(item.clock))) fail('persona clock is invalid');
    exact(item.cashflowPolicy, ['livingBudgetMinor', 'emergencyReserveMinor'], 'persona cashflowPolicy');
    if (!Number.isSafeInteger(item.cashflowPolicy.livingBudgetMinor)
      || item.cashflowPolicy.livingBudgetMinor < 0
      || !Number.isSafeInteger(item.cashflowPolicy.emergencyReserveMinor)
      || item.cashflowPolicy.emergencyReserveMinor < 0) {
      fail('persona cashflowPolicy is invalid');
    }
    array(item.expectedRecommendationScenarioIds, 16, 'expectedRecommendationScenarioIds');
    item.expectedRecommendationScenarioIds.forEach(id => { if (!scenarioIds.has(id)) fail('persona references unknown scenario ' + id); });
    if (!Array.isArray(item.events) || item.events.length > 64) fail('persona events are invalid');
    item.events.forEach(event => {
      exact(event, ['id', 'type', 'occurredAt', 'payload'], 'persona event');
      if (!['salary_credited', 'card_bill_due', 'utility_bill_due', 'loan_payment_due', 'spending_anomaly'].includes(event.type)) fail('persona event type is invalid');
      if (Number.isNaN(Date.parse(event.occurredAt)) || !event.payload || typeof event.payload !== 'object' || Array.isArray(event.payload) || Object.keys(event.payload).length > 16) fail('persona event is invalid');
    });
  });
}

function skillIdsFromDefaultRegistry() {
  const registry = readJson(path.join(REGISTRY_DIR, 'skills.v1.json'));
  return new Set((registry.skills || []).map(item => item.id));
}

function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function sortById(items) { return [...items].sort((left, right) => left.id.localeCompare(right.id)); }

function loadExperienceRegistry(options = {}) {
  const manifests = clone(options.manifests || readDefaultManifests());
  const signalIds = validateSignals(manifests.signals);
  const capabilityIds = new Set((options.capabilities || []).map(item => item.id));
  const workflowIds = validateWorkflows(manifests.workflows, capabilityIds, Boolean(options.allowPlannedAssets));
  const intentIds = new Set(((options.intentGraph && options.intentGraph.nodes) || [])
    .filter(item => item.type === 'intent').map(item => item.id));
  const skillIds = options.skillIds ? new Set(options.skillIds) : skillIdsFromDefaultRegistry();
  validateScenarios(manifests.scenarios, {
    signalIds, capabilityIds, workflowIds, intentIds, skillIds,
    allowPlannedAssets: Boolean(options.allowPlannedAssets),
  });
  const scenarioIds = new Set(manifests.scenarios.scenarios.map(item => item.id));
  validatePersonas(manifests.personas, scenarioIds);
  return deepFreeze({
    schemaVersion: '1.0',
    scenarios: sortById(manifests.scenarios.scenarios),
    signals: sortById(manifests.signals.signals),
    workflowTemplates: sortById(manifests.workflows.workflowTemplates),
    personas: sortById(manifests.personas.personas),
  });
}

module.exports = { ExperienceRegistryError, loadExperienceRegistry };
