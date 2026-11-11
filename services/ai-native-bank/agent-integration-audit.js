'use strict';

const fs = require('node:fs');
const path = require('node:path');

const capabilityRegistry = require('./capability-registry');
const { buildIntentGraph } = require('./intent-graph-builder');
const { AGENT_CAPABILITY_BINDINGS } = require('./agent-capability-service');
const bankModel = require('../../tools/ai-native-bank-model');
const featureActions = require('../../tools/ai-native-bank-feature-actions');

const ROOT = path.join(__dirname, '..', '..', 'data', 'ai-native-bank', 'registries');
const VALID_STATUSES = new Set(['enabled', 'deferred', 'platform-only']);

function readJson(fileName) {
  return JSON.parse(fs.readFileSync(path.join(ROOT, fileName), 'utf8'));
}

function issue(code, subject, message, severity = 'error') {
  return { code, subject, message, severity };
}

function auditAgentIntegration(options = {}) {
  const capabilities = capabilityRegistry.list({ audience: 'all' });
  const graph = options.graph || buildIntentGraph();
  const skills = (options.skills || readJson('skills.v1.json')).skills;
  const providers = (options.providers || readJson('providers.v1.json')).providers;
  const decisions = (options.decisions || readJson('agent-integration.v1.json')).capabilities;
  const runtimeBindings = options.runtimeBindings || AGENT_CAPABILITY_BINDINGS;
  const issues = [];

  const catalog = options.catalog || bankModel.getChannelCatalog();
  const actionSurfaces = [];
  Object.entries(catalog).forEach(([channel, group]) => {
    group.items.forEach(item => {
      featureActions.listActions(channel, item).forEach(action => {
        actionSurfaces.push({
          channel,
          menuId: item.id,
          actionId: action.id,
          actionKey: action.key,
          capabilityId: action.capabilityId,
          readiness: action.readiness,
        });
      });
    });
  });

  const capabilityById = new Map(capabilities.map(item => [item.id, item]));
  const providersByCapability = groupBy(providers, item => item.capabilityId);
  const intents = graph.nodes.filter(item => item.type === 'intent');
  const intentsByCapability = new Map();
  intents.forEach(intent => (intent.capabilityBindings || []).forEach(capabilityId => {
    if (!intentsByCapability.has(capabilityId)) intentsByCapability.set(capabilityId, []);
    intentsByCapability.get(capabilityId).push(intent);
  }));
  const skillByIntent = new Map();
  const skillsByCapability = new Map();
  skills.filter(item => item.status === 'active').forEach(skill => {
    skill.handledIntentIds.forEach(intentId => skillByIntent.set(intentId, skill));
    skill.requiredCapabilityIds.forEach(capabilityId => {
      if (!skillsByCapability.has(capabilityId)) skillsByCapability.set(capabilityId, []);
      skillsByCapability.get(capabilityId).push(skill);
    });
  });
  const decisionByCapability = new Map();
  decisions.forEach(decision => {
    if (decisionByCapability.has(decision.capabilityId)) {
      issues.push(issue('integration_decision_duplicate', decision.capabilityId, '能力存在重复集成决策'));
    }
    decisionByCapability.set(decision.capabilityId, decision);
    if (!VALID_STATUSES.has(decision.status) || typeof decision.reason !== 'string' || decision.reason.trim().length < 8) {
      issues.push(issue('integration_decision_invalid', decision.capabilityId, '集成状态或原因无效'));
    }
  });

  const actionKeys = new Set();
  actionSurfaces.forEach(action => {
    if (actionKeys.has(action.actionKey)) {
      issues.push(issue('feature_action_duplicate', action.actionKey, '手机银行功能动作重复'));
    }
    actionKeys.add(action.actionKey);
    if (!action.capabilityId) return;
    if (!capabilityById.has(action.capabilityId)) {
      issues.push(issue(
        'feature_action_capability_missing', action.actionKey,
        `功能动作引用了不存在的原子能力 ${action.capabilityId}`,
      ));
      return;
    }
    const decision = decisionByCapability.get(action.capabilityId);
    if (!decision) {
      issues.push(issue(
        'feature_action_integration_unclassified', action.actionKey,
        `功能动作的原子能力 ${action.capabilityId} 缺少 Agent 集成决策`,
      ));
    } else if (action.readiness === 'ready' && decision.status !== 'enabled') {
      issues.push(issue(
        'ready_feature_action_not_enabled', action.actionKey,
        `标记 ready 的功能动作尚未完成 Agent 集成：${action.capabilityId}`,
      ));
    }
    if (action.readiness === 'ready') {
      const intent = intents.find(item => item.source
        && item.source.channel === action.channel
        && item.source.menuId === action.menuId
        && item.source.actionId === action.actionId);
      if (!intent || intent.readiness !== 'ready') {
        issues.push(issue(
          'ready_feature_action_intent_missing', action.actionKey,
          '标记 ready 的功能动作缺少对应的 ready 意图节点',
        ));
      }
    }
  });

  capabilities.forEach(capability => {
    if (!decisionByCapability.has(capability.id)) {
      issues.push(issue('integration_decision_missing', capability.id, '原子能力缺少 Agent 集成决策'));
    }
  });
  decisions.forEach(decision => {
    if (!capabilityById.has(decision.capabilityId)) {
      issues.push(issue('integration_decision_orphan', decision.capabilityId, '集成决策引用了不存在的原子能力'));
    }
  });

  const runtimeReadonly = new Set(runtimeBindings.readonly || []);
  const runtimeConfirmed = new Set(runtimeBindings.confirmed || []);
  decisions.filter(item => item.status === 'enabled').forEach(decision => {
    const capability = capabilityById.get(decision.capabilityId);
    if (!capability) return;
    const boundProviders = providersByCapability.get(capability.id) || [];
    const boundSkills = skillsByCapability.get(capability.id) || [];
    const boundIntents = intentsByCapability.get(capability.id) || [];
    const readyIntents = boundIntents.filter(item => item.readiness === 'ready');
    if (boundProviders.length !== 1 || boundProviders[0].health !== 'ready') {
      issues.push(issue('enabled_provider_missing', capability.id, '已接入能力必须且只能有一个健康 Provider'));
    }
    const requiresSkill = readyIntents.some(intent => intent.executionMode !== 'read-execute');
    if (requiresSkill && boundSkills.length === 0) {
      issues.push(issue('enabled_skill_missing', capability.id, '已接入能力缺少活动 Skill'));
    }
    if (readyIntents.length === 0) {
      issues.push(issue('enabled_intent_missing', capability.id, '已接入能力缺少 ready 意图'));
    }
    const expectedRuntime = capability.sideEffect === 'none' ? runtimeReadonly : runtimeConfirmed;
    if (!expectedRuntime.has(capability.id)) {
      issues.push(issue('enabled_runtime_handler_missing', capability.id, '已接入能力缺少受控 Runtime handler'));
    }
  });

  skills.filter(item => item.status === 'active').forEach(skill => {
    skill.requiredCapabilityIds.forEach(capabilityId => {
      const decision = decisionByCapability.get(capabilityId);
      if (!decision || decision.status !== 'enabled') {
        issues.push(issue('active_skill_uses_disabled_capability', skill.id, `活动 Skill 使用未启用能力 ${capabilityId}`));
      }
    });
  });

  intents.filter(item => item.readiness === 'ready').forEach(intent => {
    const skill = skillByIntent.get(intent.id);
    // read-execute（pureRead）和 knowledge-only 意图直接走 Capability 或知识回答，
    // 不需要专用 Skill。只有需要编排/确认的意图才强制要求专用 Skill。
    if (intent.executionMode !== 'read-execute'
        && intent.executionMode !== 'knowledge-only' && !skill) {
      issues.push(issue('ready_intent_skill_missing', intent.id, 'ready 编排意图缺少活动 Skill'));
    }
    (intent.capabilityBindings || []).forEach(capabilityId => {
      const decision = decisionByCapability.get(capabilityId);
      if (!decision || decision.status !== 'enabled') {
        issues.push(issue('ready_intent_uses_disabled_capability', intent.id, `ready 意图使用未启用能力 ${capabilityId}`));
      }
    });
  });

  const rows = capabilities.map(capability => {
    const decision = decisionByCapability.get(capability.id) || {};
    return {
      capabilityId: capability.id,
      domain: capability.domain,
      integrationStatus: decision.status || 'unclassified',
      reason: decision.reason || '',
      providerIds: (providersByCapability.get(capability.id) || []).map(item => item.providerId),
      skillIds: (skillsByCapability.get(capability.id) || []).map(item => item.id),
      intentIds: (intentsByCapability.get(capability.id) || [])
        .filter(item => item.readiness === 'ready')
        .map(item => item.id),
      runtimeBinding: runtimeReadonly.has(capability.id)
        ? 'readonly'
        : runtimeConfirmed.has(capability.id) ? 'confirmed' : 'none',
      featureActionKeys: actionSurfaces
        .filter(item => item.capabilityId === capability.id)
        .map(item => item.actionKey),
    };
  });
  return {
    summary: {
      menuItemCount: Object.values(catalog)
        .reduce((count, group) => count + group.items.length, 0),
      featureActionCount: actionSurfaces.length,
      capabilityBackedActionCount: actionSurfaces
        .filter(item => item.capabilityId).length,
      readyFeatureActionCount: actionSurfaces
        .filter(item => item.readiness === 'ready').length,
      capabilityCount: capabilities.length,
      classifiedCount: rows.filter(item => item.integrationStatus !== 'unclassified').length,
      enabledCount: rows.filter(item => item.integrationStatus === 'enabled').length,
      deferredCount: rows.filter(item => item.integrationStatus === 'deferred').length,
      platformOnlyCount: rows.filter(item => item.integrationStatus === 'platform-only').length,
      errorCount: issues.filter(item => item.severity === 'error').length,
    },
    capabilities: rows,
    issues,
  };
}

function groupBy(items, key) {
  const result = new Map();
  items.forEach(item => {
    const value = key(item);
    if (!result.has(value)) result.set(value, []);
    result.get(value).push(item);
  });
  return result;
}

module.exports = { auditAgentIntegration };
