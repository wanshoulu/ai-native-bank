'use strict';

const crypto = require('node:crypto');
const { BankApiError } = require('./api-contract');

const OPERATORS = new Set(['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'contains', 'exists']);
const SELECTED_OBLIGATION_INSTRUCTION = '处理已选择的账单；如需扣款，请先向我展示金额并确认';

function createAssistanceAdapter(options = {}) {
  const nextActionService = options.nextActionService;
  const experienceRegistry = options.experienceRegistry;
  const signalService = options.signalService;
  const repository = options.repository;
  const artifactProvider = options.artifactProvider;
  const assetProvider = options.assetProvider;
  const selectedActionContextProvider = options.selectedActionContextProvider;
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  if (!nextActionService || typeof nextActionService.requireAcceptedForStart !== 'function'
    || !experienceRegistry || !Array.isArray(experienceRegistry.scenarios)
    || !signalService || typeof signalService.collect !== 'function'
    || !repository || typeof repository.bindNextActionWorkflow !== 'function'
    || !artifactProvider || typeof artifactProvider.build !== 'function'
    || typeof assetProvider !== 'function') {
    throw new TypeError('assistance adapter dependencies are invalid');
  }

  async function prepare(input) {
    validateStartInput(input);
    const accepted = await nextActionService.requireAcceptedForStart(
      input.recommendationId, input.customerId,
    );
    const currentTime = requireTime(now());
    if (Date.parse(accepted.expiresAt) <= Date.parse(currentTime)) {
      throw new BankApiError(409, 'recommendation_expired', 'Recommendation has expired');
    }
    const scenario = experienceRegistry.scenarios.find(item => (
      item.id === accepted.scenarioId && item.version === accepted.scenarioVersion
    ));
    if (!scenario || scenario.status !== 'active'
      || scenario.type !== accepted.type
      || scenario.targetIntentId !== accepted.targetIntentId
      || scenario.workflowTemplateId !== accepted.workflowTemplateId) {
      unavailable();
    }
    const selectedItem = input.serviceActionId
      ? await resolveServiceAction(artifactProvider, input.customerId, accepted, input.serviceActionId)
      : null;
    const goalScenario = selectedItem
      ? resolveActionScenario(experienceRegistry, selectedItem.targetIntentId)
      : scenario;
    const [assets, snapshot] = await Promise.all([
      assetProvider(input.customerId),
      signalService.collect({
        customerId: input.customerId,
        surface: accepted.surface,
        channel: accepted.channel,
        personalizationEnabled: true,
      }),
    ]);
    const requiredScenarios = goalScenario === scenario ? [scenario] : [scenario, goalScenario];
    if (!assets || !Array.isArray(assets.capabilityIds) || !Array.isArray(assets.skillIds)
      || requiredScenarios.some(candidate => (
        candidate.requiredCapabilityIds.some(id => !assets.capabilityIds.includes(id))
        || candidate.requiredSkillIds.some(id => !assets.skillIds.includes(id))
      ))
      || !snapshot || !snapshot.signals
      || requiredScenarios.some(candidate => (
        !evaluateTrigger(candidate.trigger, snapshot.signals, Date.parse(currentTime))
      ))) {
      unavailable();
    }
    if (!isSafeText(goalScenario.title, 80)
      || !Array.isArray(accepted.contextRefs) || accepted.contextRefs.length > 32
      || accepted.contextRefs.some(ref => !isSafeId(ref))) {
      unavailable();
    }
    const trustedInputs = {};
    requiredScenarios.forEach(candidate => Object.assign(trustedInputs, projectTrustedInputs(
      candidate.trigger, accepted.contextRefs, snapshot.signals, Date.parse(currentTime),
    )));
    if (selectedItem) {
      trustedInputs.obligationId = selectedItem.id;
      trustedInputs.obligationType = selectedItem.type;
    }
    let selectedContext = null;
    if (selectedItem && selectedItem.targetIntentId === 'ccb.obligation.pay-selected') {
      if (typeof selectedActionContextProvider !== 'function') serviceActionUnavailable();
      selectedContext = await selectedActionContextProvider({
        customerId: input.customerId,
        selectedItemId: selectedItem.id,
        selectedItemType: selectedItem.type,
        targetIntentId: selectedItem.targetIntentId,
      });
      if (!selectedContext || !selectedContext.trustedInputs
        || Object.keys(selectedContext.trustedInputs).some(key => (
          !/^[a-z][a-zA-Z0-9_]{0,63}$/.test(key)
          || !isSafeScalar(selectedContext.trustedInputs[key])
        ))
        || !Array.isArray(selectedContext.contextRefs)
        || selectedContext.contextRefs.some(ref => !isSafeId(ref))) {
        serviceActionUnavailable();
      }
      Object.assign(trustedInputs, selectedContext.trustedInputs);
    }
    const baseContextRefs = selectedItem
      ? appendContextRef(accepted.contextRefs, selectedItem.id)
      : accepted.contextRefs.slice();
    const contextRefs = selectedContext
      ? selectedContext.contextRefs.reduce(appendContextRef, baseContextRefs)
      : baseContextRefs;
    const goalEnvelope = deepFreeze({
      goalId: buildGoalId(accepted.recommendationId, input.serviceActionId || null),
      scenarioId: goalScenario.id,
      scenarioVersion: goalScenario.version,
      recommendationId: accepted.recommendationId,
      targetIntentId: goalScenario.targetIntentId,
      mode: 'guided',
      contextRefs,
      workflowTemplateId: goalScenario.workflowTemplateId,
      ...(Object.keys(trustedInputs).length ? { trustedInputs } : {}),
    });
    await repository.bindNextActionWorkflow({
      recommendationId: accepted.recommendationId,
      customerId: input.customerId,
      agentSessionId: input.conversationId,
      agentRunId: input.agentRunId,
      businessWorkflowId: null,
      goalId: goalEnvelope.goalId,
    });
    return deepFreeze({
      userMessage: selectedItem
        ? SELECTED_OBLIGATION_INSTRUCTION
        : goalScenario.entryMessage,
      displayMessage: selectedItem ? `办理${selectedItem.title}` : goalScenario.title,
      goalEnvelope,
    });
  }

  return Object.freeze({ prepare });
}

function validateStartInput(input) {
  const requiredKeys = ['recommendationId', 'customerId', 'conversationId', 'agentRunId'];
  const allowedKeys = [...requiredKeys, 'serviceActionId'];
  if (!input || typeof input !== 'object' || Array.isArray(input)
    || Object.keys(input).some(key => !allowedKeys.includes(key))
    || requiredKeys.some(key => !isSafeId(input[key]))
    || (Object.hasOwn(input, 'serviceActionId') && input.serviceActionId !== null
      && !isSafeId(input.serviceActionId))) {
    throw new BankApiError(422, 'assistance_input_invalid', 'Assistance start input is invalid');
  }
}

async function resolveServiceAction(artifactProvider, customerId, accepted, serviceActionId) {
  let artifact;
  try {
    artifact = await artifactProvider.build(customerId, accepted);
  } catch (_) {
    serviceActionUnavailable();
  }
  const items = artifact && artifact.detail && Array.isArray(artifact.detail.items)
    ? artifact.detail.items : [];
  const selected = items.find(item => item && item.action && item.action.id === serviceActionId);
  if (!selected || !isSafeId(selected.id)
    || !['credit_card', 'loan', 'utility'].includes(selected.type)
    || !isSafeText(selected.title, 120)
    || !selected.availability || selected.availability.state !== 'available'
    || !selected.action || selected.action.kind !== 'agent_plan') {
    serviceActionUnavailable();
  }
  if (!isSafeId(selected.action.targetIntentId)) serviceActionUnavailable();
  return {
    id: selected.id,
    type: selected.type,
    title: selected.title,
    targetIntentId: selected.action.targetIntentId,
  };
}

function resolveActionScenario(experienceRegistry, targetIntentId) {
  const matches = experienceRegistry.scenarios.filter(item => (
    item && item.status === 'active' && item.type === 'next_intent'
    && item.targetIntentId === targetIntentId
    && isSafeId(item.id) && isSafeId(item.version)
    && isSafeId(item.workflowTemplateId) && isSafeText(item.title, 80)
    && Array.isArray(item.requiredCapabilityIds) && Array.isArray(item.requiredSkillIds)
  ));
  if (matches.length !== 1) serviceActionUnavailable();
  return matches[0];
}

function appendContextRef(contextRefs, selectedId) {
  if (contextRefs.includes(selectedId)) return contextRefs.slice();
  return [...contextRefs.slice(0, 31), selectedId];
}

function buildGoalId(recommendationId, serviceActionId) {
  if (!serviceActionId) return 'goal_' + recommendationId;
  const suffix = crypto.createHash('sha256').update(serviceActionId).digest('hex').slice(0, 16);
  return `goal_${recommendationId.slice(0, 137)}.${suffix}`;
}

function evaluateTrigger(node, signals, nowMs) {
  if (!node || typeof node !== 'object') return false;
  if (Array.isArray(node.all)) return node.all.every(item => evaluateTrigger(item, signals, nowMs));
  if (Array.isArray(node.any)) return node.any.some(item => evaluateTrigger(item, signals, nowMs));
  if (node.not) return !evaluateTrigger(node.not, signals, nowMs);
  if (!OPERATORS.has(node.operator)) return false;
  const record = signals[node.signal];
  const fresh = record && Number.isFinite(Date.parse(record.expiresAt)) && Date.parse(record.expiresAt) > nowMs;
  if (node.operator === 'exists') return Boolean(node.value) === Boolean(fresh);
  if (!fresh) return false;
  const actual = record.value;
  const expected = node.value;
  if (node.operator === 'eq') return actual === expected;
  if (node.operator === 'ne') return actual !== expected;
  if (node.operator === 'gt') return typeof actual === 'number' && actual > expected;
  if (node.operator === 'gte') return typeof actual === 'number' && actual >= expected;
  if (node.operator === 'lt') return typeof actual === 'number' && actual < expected;
  if (node.operator === 'lte') return typeof actual === 'number' && actual <= expected;
  if (node.operator === 'in') return Array.isArray(expected) && expected.includes(actual);
  if (node.operator === 'contains') {
    return (Array.isArray(actual) || typeof actual === 'string') && actual.includes(expected);
  }
  return false;
}

function projectTrustedInputs(trigger, contextRefs, signals, nowMs) {
  const signalIds = new Set();
  collectConditionSignals(trigger, signalIds);
  const result = {};
  for (const signalId of signalIds) {
    if (!signalId.startsWith('input.')) continue;
    const inputName = signalId.slice('input.'.length);
    const record = signals[signalId];
    const referenced = contextRefs.includes('signal:' + signalId);
    const fresh = record && Number.isFinite(Date.parse(record.expiresAt))
      && Date.parse(record.expiresAt) > nowMs;
    if (!referenced || !fresh || !/^[a-z][a-zA-Z0-9_]{0,63}$/.test(inputName)
      || !isSafeScalar(record.value)) unavailable();
    result[inputName] = record.value;
  }
  return result;
}

function collectConditionSignals(node, result) {
  if (!node || typeof node !== 'object') return;
  if (typeof node.signal === 'string') result.add(node.signal);
  if (Array.isArray(node.all)) node.all.forEach(item => collectConditionSignals(item, result));
  if (Array.isArray(node.any)) node.any.forEach(item => collectConditionSignals(item, result));
  if (node.not) collectConditionSignals(node.not, result);
}

function isSafeScalar(value) {
  return typeof value === 'boolean'
    || (typeof value === 'number' && Number.isFinite(value))
    || (typeof value === 'string' && value.length > 0 && value.length <= 160 && !/[<>]/.test(value));
}

function unavailable() {
  throw new BankApiError(409, 'recommendation_no_longer_available', 'Recommendation is no longer available');
}
function serviceActionUnavailable() {
  throw new BankApiError(409, 'service_action_unavailable', 'Selected service is no longer available');
}
function requireTime(value) {
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) throw new TypeError('now is invalid');
  return new Date(value).toISOString();
}
function isSafeId(value) { return typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value); }
function isSafeText(value, max) { return typeof value === 'string' && value.length > 0 && value.length <= max && !/[<>]/.test(value); }
function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

module.exports = { createAssistanceAdapter };
