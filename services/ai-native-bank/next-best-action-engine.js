'use strict';

// Resumable work remains first. Guided next-intent cards rank ahead of passive
// catalogue notices so a bounded three-card surface can actually start the
// registered assistance workflows instead of being saturated by alerts.
// care 排在被动通知之后：个性化关怀填充余位，不挤占业务提醒。
const TYPE_WEIGHT = Object.freeze({ resume_task: 1000, next_intent: 700, proactive_service: 400, care: 350 });
const PRIORITY_WEIGHT = Object.freeze({ critical: 300, high: 200, normal: 100, low: 0 });
const ALLOWED_SURFACES = new Set(['home', 'agent', 'channel']);
const ALLOWED_OPERATORS = new Set(['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'contains', 'exists']);
const SAFE_ACTIONS = new Set(['START_ASSISTANCE', 'OPEN_CATALOG', 'RESUME_WORKFLOW']);

class NextActionEvaluationError extends Error {
  constructor(message) {
    super('next_action_evaluation_invalid: ' + message);
    this.name = 'NextActionEvaluationError';
    this.code = 'next_action_evaluation_invalid';
  }
}

function createNextBestActionEngine(options = {}) {
  const registry = options.registry;
  if (!registry || !Array.isArray(registry.scenarios)) throw new TypeError('registry.scenarios is required');
  const ranker = typeof options.ranker === 'function' ? options.ranker : null;
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  async function evaluate(snapshot, request, maximumLimit) {
      const input = validateEvaluation(snapshot, request, now(), maximumLimit);
      const candidates = buildCandidates(registry.scenarios, snapshot, input);
      const resumes = input.resumeItems.map(item => projectResumeCandidate(item));
      let ordered = [...resumes, ...candidates].sort(compareCandidates);
      let mode = 'deterministic';
      if (ranker && ordered.length) {
        const bounded = ordered.slice(0, 5);
        try {
          const ranking = await ranker(deepFreeze({
            candidateIds: bounded.map(item => item.scenarioId),
            candidates: bounded.map(item => ({
              id: item.scenarioId,
              type: item.type,
              priority: item.priority,
              title: item.title,
              reasonCodes: item.reasonCodes,
            })),
          }));
          ordered = applyBoundedRanking(ordered, ranking);
          mode = 'hybrid';
        } catch (_) {
          mode = 'deterministic';
        }
      }
      return deepFreeze({
        schemaVersion: '1.0',
        mode,
        generatedAt: input.nowIso,
        items: ordered.slice(0, input.limit).map(projectPublicCandidate),
      });
  }
  return Object.freeze({
    evaluate: (snapshot, request) => evaluate(snapshot, request, 3),
    evaluateAll: (snapshot, request) => evaluate(snapshot, request, 32),
  });
}

function validateEvaluation(snapshot, request, nowIso, maximumLimit = 3) {
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)
    || !snapshot.signals || typeof snapshot.signals !== 'object'
    || typeof snapshot.customerId !== 'string') fail('snapshot is invalid');
  if (!request || typeof request !== 'object' || Array.isArray(request)) fail('request is invalid');
  const allowed = new Set(['surface', 'channel', 'limit', 'assets', 'cooldownScenarioIds', 'resumeItems']);
  const unknown = Object.keys(request).filter(key => !allowed.has(key));
  if (unknown.length) fail('request has unknown fields');
  if (!ALLOWED_SURFACES.has(request.surface) || request.surface !== snapshot.surface) fail('surface is invalid');
  const channel = request.channel == null ? null : request.channel;
  if (channel !== snapshot.channel) fail('channel does not match snapshot');
  if (!Number.isInteger(request.limit) || request.limit < 1 || request.limit > maximumLimit) fail('limit is invalid');
  const assets = request.assets;
  if (!assets || !Array.isArray(assets.capabilityIds) || !Array.isArray(assets.skillIds)) fail('assets are invalid');
  if (!Array.isArray(request.cooldownScenarioIds) || !Array.isArray(request.resumeItems)) fail('candidate controls are invalid');
  if (request.cooldownScenarioIds.length > 128 || request.resumeItems.length > 8) fail('candidate controls are too large');
  if (Number.isNaN(Date.parse(nowIso))) fail('now is invalid');
  return {
    surface: request.surface,
    channel,
    limit: request.limit,
    capabilityIds: new Set(request.assets.capabilityIds),
    skillIds: new Set(request.assets.skillIds),
    cooldownScenarioIds: new Set(request.cooldownScenarioIds),
    resumeItems: request.resumeItems,
    nowIso,
    nowMs: Date.parse(nowIso),
  };
}

function buildCandidates(scenarios, snapshot, input) {
  const ids = new Set();
  const items = [];
  for (const scenario of scenarios) {
    if (!scenario || ids.has(scenario.id)) continue;
    ids.add(scenario.id);
    if (scenario.status !== 'active' || !scenario.surfaces.includes(input.surface)) continue;
    if (input.cooldownScenarioIds.has(scenario.id)) continue;
    if (scenario.requiredCapabilityIds.some(id => !input.capabilityIds.has(id))) continue;
    if (scenario.requiredSkillIds.some(id => !input.skillIds.has(id))) continue;
    const usedSignals = new Set();
    if (!evaluateTrigger(scenario.trigger, snapshot.signals, input.nowMs, usedSignals)) continue;
    items.push(projectScenarioCandidate(scenario, snapshot, input, [...usedSignals].sort()));
  }
  // 覆盖抑制（注册表声明的 supersededBy）：聚合场景在候选中时，被其覆盖的
  // 成员场景不再重复占位（如账单聚合卡与单类账单提醒）。聚合场景被资产/冷却
  // 过滤掉时成员自然回补，声明关系是数据而非运行时特例。
  const present = new Set(items.map(item => item.scenarioId));
  return items.filter(item => item.supersededBy == null || !present.has(item.supersededBy));
}

function evaluateTrigger(node, signals, nowMs, usedSignals) {
  if (node.all) return node.all.every(item => evaluateTrigger(item, signals, nowMs, usedSignals));
  if (node.any) return node.any.some(item => evaluateTrigger(item, signals, nowMs, usedSignals));
  if (node.not) return !evaluateTrigger(node.not, signals, nowMs, usedSignals);
  if (!ALLOWED_OPERATORS.has(node.operator)) return false;
  const record = signals[node.signal];
  if (node.operator === 'exists') {
    const exists = Boolean(record) && Date.parse(record.expiresAt) > nowMs;
    if (exists) usedSignals.add(node.signal);
    return Boolean(node.value) === exists;
  }
  if (!record || Date.parse(record.expiresAt) <= nowMs) return false;
  usedSignals.add(node.signal);
  return compareSignal(record.value, node.operator, node.value);
}

function compareSignal(actual, operator, expected) {
  if (operator === 'eq') return actual === expected;
  if (operator === 'ne') return actual !== expected;
  if (operator === 'gt') return typeof actual === 'number' && actual > expected;
  if (operator === 'gte') return typeof actual === 'number' && actual >= expected;
  if (operator === 'lt') return typeof actual === 'number' && actual < expected;
  if (operator === 'lte') return typeof actual === 'number' && actual <= expected;
  if (operator === 'in') return Array.isArray(expected) && expected.includes(actual);
  if (operator === 'contains') return (Array.isArray(actual) || typeof actual === 'string') && actual.includes(expected);
  return false;
}

function projectScenarioCandidate(scenario, snapshot, input, signalIds) {
  const preference = snapshot.personalizationEnabled
    && snapshot.signals['preference.frequent_intent_ids']
    && Array.isArray(snapshot.signals['preference.frequent_intent_ids'].value)
    && snapshot.signals['preference.frequent_intent_ids'].value.includes(scenario.targetIntentId) ? 25 : 0;
  const context = input.channel && scenario.catalogAction && scenario.catalogAction.channel === input.channel ? 25 : 0;
  const urgency = scenario.priority === 'critical' ? 75 : scenario.priority === 'high' ? 50 : 0;
  return {
    scenarioId: scenario.id,
    scenarioVersion: scenario.version,
    type: scenario.type,
    title: scenario.title,
    priority: scenario.priority,
    reasonCodes: [scenario.reasonCode],
    reasonText: scenario.reasonTextTemplate,
    action: scenario.action,
    targetIntentId: scenario.targetIntentId,
    workflowTemplateId: scenario.workflowTemplateId,
    supersededBy: scenario.supersededBy == null ? null : scenario.supersededBy,
    requiredSkillIds: [...scenario.requiredSkillIds],
    requiredCapabilityIds: [...scenario.requiredCapabilityIds],
    contextRefs: signalIds.map(id => 'signal:' + id),
    catalogAction: scenario.catalogAction ? { ...scenario.catalogAction } : null,
    score: TYPE_WEIGHT[scenario.type] + PRIORITY_WEIGHT[scenario.priority] + urgency + context + preference,
  };
}

function projectResumeCandidate(item) {
  if (!item || item.type !== 'resume_task' || !safeId(item.scenarioId)
    || !safeText(item.title, 80) || !safeText(item.reasonText, 160)
    || !SAFE_ACTIONS.has(item.action) || !Array.isArray(item.contextRefs)) fail('resume item is invalid');
  return {
    scenarioId: item.scenarioId,
    scenarioVersion: item.scenarioVersion,
    type: 'resume_task',
    title: item.title,
    priority: item.priority,
    reasonCodes: [item.reasonCode],
    reasonText: item.reasonText,
    action: item.action,
    targetIntentId: item.targetIntentId,
    workflowTemplateId: item.workflowTemplateId,
    requiredSkillIds: [...item.requiredSkillIds],
    requiredCapabilityIds: [...item.requiredCapabilityIds],
    contextRefs: item.contextRefs.slice(0, 16),
    catalogAction: item.catalogAction,
    score: TYPE_WEIGHT.resume_task + PRIORITY_WEIGHT[item.priority],
  };
}

function applyBoundedRanking(items, ranking) {
  if (!ranking || typeof ranking !== 'object' || !Array.isArray(ranking.orderedIds)) return items;
  const allowed = new Set(items.slice(0, 5).map(item => item.scenarioId));
  const order = new Map();
  ranking.orderedIds.forEach((id, index) => { if (allowed.has(id) && !order.has(id)) order.set(id, index); });
  const reasons = ranking.reasons && typeof ranking.reasons === 'object' ? ranking.reasons : {};
  const updated = items.map(item => {
    const reason = reasons[item.scenarioId];
    return safeText(reason, 160) ? { ...item, reasonText: reason } : item;
  });
  return updated.sort((left, right) => {
    const precedence = comparePrecedence(left, right);
    if (precedence !== 0) return precedence;
    const leftOrder = order.has(left.scenarioId) ? order.get(left.scenarioId) : 999;
    const rightOrder = order.has(right.scenarioId) ? order.get(right.scenarioId) : 999;
    return leftOrder - rightOrder || compareCandidates(left, right);
  });
}

function comparePrecedence(left, right) {
  return TYPE_WEIGHT[right.type] - TYPE_WEIGHT[left.type]
    || PRIORITY_WEIGHT[right.priority] - PRIORITY_WEIGHT[left.priority];
}

function compareCandidates(left, right) {
  return right.score - left.score || left.scenarioId.localeCompare(right.scenarioId);
}

function projectPublicCandidate(item) {
  return {
    scenarioId: item.scenarioId,
    scenarioVersion: item.scenarioVersion,
    type: item.type,
    title: item.title,
    priority: item.priority,
    reasonCodes: item.reasonCodes.slice(),
    reasonText: item.reasonText,
    action: item.action,
    targetIntentId: item.targetIntentId,
    workflowTemplateId: item.workflowTemplateId,
    requiredSkillIds: item.requiredSkillIds.slice(),
    requiredCapabilityIds: item.requiredCapabilityIds.slice(),
    contextRefs: item.contextRefs.slice(),
    catalogAction: item.catalogAction ? { ...item.catalogAction } : null,
    score: item.score,
  };
}

function safeId(value) { return typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value); }
function safeText(value, max) { return typeof value === 'string' && value.length > 0 && value.length <= max && !/[<>]/.test(value); }
function fail(message) { throw new NextActionEvaluationError(message); }
function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

module.exports = { NextActionEvaluationError, createNextBestActionEngine };
