'use strict';

const fs = require('node:fs');
const path = require('node:path');

const capabilityRegistry = require('./capability-registry');
const { buildIntentGraph } = require('./intent-graph-builder');
const {
  validateIntentConformanceEvaluation,
} = require('./intent-conformance-eval');

const PROJECT_ROOT = path.join(__dirname, '..', '..');
const REGISTRY_ROOT = path.join(PROJECT_ROOT, 'data', 'ai-native-bank', 'registries');
const DEFAULT_EVAL_PATH = path.join(
  PROJECT_ROOT, 'data', 'eval', 'ccb-intent-conformance.v1.json',
);
const EXECUTABLE_MODES = new Set(['read-execute', 'confirm-required']);

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function registry(name) {
  return readJson(path.join(REGISTRY_ROOT, name));
}

function issue(code, subject, message, severity = 'error', details = undefined) {
  return {
    code,
    subject,
    message,
    severity,
    ...(details === undefined ? {} : { details }),
  };
}

function stableIntentReferences(payload) {
  const cases = payload && Array.isArray(payload.cases) ? payload.cases : [];
  const ids = new Set();
  cases.forEach(item => {
    if (item && typeof item.intentId === 'string' && item.intentId) {
      ids.add(item.intentId);
    }
    if (!item || !Array.isArray(item.expected_intents)) return;
    item.expected_intents.forEach(expected => {
      const id = expected && (expected.intent_id || expected.id);
      if (typeof id === 'string' && id) ids.add(id);
    });
  });
  return { cases, ids };
}

function hasConversationPolicies(intent) {
  return Boolean(
    intent
      && intent.taskPolicy && typeof intent.taskPolicy === 'object'
      && intent.contextPolicy && typeof intent.contextPolicy === 'object'
      && typeof intent.responsePolicy === 'string' && intent.responsePolicy.trim(),
  );
}

function auditCrossContractConformance(options = {}) {
  const graph = options.graph || buildIntentGraph();
  const skills = options.skills || registry('skills.v1.json').skills;
  const providers = options.providers || registry('providers.v1.json').providers;
  const scenarios = options.scenarios || registry('scenarios.v1.json').scenarios;
  const capabilities = options.capabilities
    || capabilityRegistry.list({ audience: 'all' });
  const semanticPayload = options.semanticEvaluation || readJson(DEFAULT_EVAL_PATH);
  const liveProviderHealth = Array.isArray(options.liveProviderHealth)
    ? options.liveProviderHealth : [];

  const issues = [];
  const intents = graph.nodes.filter(item => item.type === 'intent');
  const intentById = new Map(intents.map(item => [item.id, item]));
  const skillById = new Map(skills.map(item => [item.id, item]));

  const nodeCounts = new Map();
  graph.nodes.forEach(node => nodeCounts.set(node.id, (nodeCounts.get(node.id) || 0) + 1));
  [...nodeCounts].filter(([, count]) => count > 1).forEach(([id, count]) => {
    issues.push(issue('graph_node_id_duplicate', id, '图谱节点 ID 不唯一', 'error', { count }));
  });

  skills.forEach(skill => {
    skill.handledIntentIds.forEach(intentId => {
      const intent = intentById.get(intentId);
      if (!intent) {
        issues.push(issue(
          'skill_graph_binding_asymmetric',
          intentId,
          'Skill 注册表声明处理该意图，但发布图谱中不存在该意图',
          'error',
          { skillId: skill.id, graphSkillId: null },
        ));
        return;
      }
      const binding = intent.skillBinding;
      // pureRead（read-execute 无槽位）和 knowledge-only 意图直接走 Capability
      // 或知识回答，图谱不绑定 Skill。Skill 注册表可声明业务负责，不视为不对称。
      if (!binding) return;
      if (binding.skillId !== skill.id) {
        issues.push(issue(
          'skill_graph_binding_asymmetric',
          intentId,
          'Skill 注册表声明处理该意图，但发布图谱绑定了不同的 Skill',
          'error',
          { skillId: skill.id, graphSkillId: binding.skillId },
        ));
        return;
      }
      if (binding.version !== skill.version || binding.digest !== skill.digest) {
        issues.push(issue(
          'skill_graph_version_mismatch',
          intentId,
          'Skill 版本或 digest 与图谱绑定不一致',
          'error',
          { skillId: skill.id },
        ));
      }
      const skillCapabilities = [...(skill.intentCapabilityBindings[intentId] || [])].sort();
      const graphCapabilities = [...(intent.capabilityBindings || [])].sort();
      if (JSON.stringify(skillCapabilities) !== JSON.stringify(graphCapabilities)) {
        issues.push(issue(
          'skill_graph_capability_mismatch',
          intentId,
          'Skill 与图谱对该意图声明了不同的 Capability 集合',
          'error',
          { skillId: skill.id, skillCapabilities, graphCapabilities },
        ));
      }
    });
  });

  intents.filter(item => item.skillBinding).forEach(intent => {
    const skill = skillById.get(intent.skillBinding.skillId);
    if (!skill || !skill.handledIntentIds.includes(intent.id)) {
      issues.push(issue(
        'graph_skill_binding_unregistered',
        intent.id,
        '图谱 Skill 绑定没有对应的注册表职责声明',
        'error',
        { skillId: intent.skillBinding.skillId },
      ));
    }
  });

  const completeConversationPolicies = intents.filter(hasConversationPolicies).length;
  if (completeConversationPolicies !== intents.length) {
    issues.push(issue(
      'conversation_policy_missing',
      graph.graphId,
      '部分意图缺少显式 task/context/response policy',
      'error',
      { missing: intents.length - completeConversationPolicies },
    ));
  }

  const executable = intents.filter(item => EXECUTABLE_MODES.has(item.executionMode));
  const executableWithSlotContract = executable.filter(item => item.slotContract).length;
  if (executableWithSlotContract !== executable.length) {
    issues.push(issue(
      'executable_slot_contract_missing',
      graph.graphId,
      '部分可执行意图缺少显式 slot contract',
      'error',
      {
        intentIds: executable.filter(item => !item.slotContract).map(item => item.id),
      },
    ));
  }
  executable.filter(item => !Array.isArray(item.capabilityBindings)
    || item.capabilityBindings.length === 0).forEach(intent => {
    issues.push(issue(
      'executable_capability_binding_missing',
      intent.id,
      '可执行意图没有 Capability 绑定',
    ));
  });

  const semantic = stableIntentReferences(semanticPayload);
  const currentIntentIds = new Set(intents.map(item => item.id));
  const currentIntentIdsCovered = [...semantic.ids]
    .filter(intentId => currentIntentIds.has(intentId)).length;
  if (currentIntentIdsCovered !== intents.length) {
    issues.push(issue(
      'semantic_eval_current_graph_uncovered',
      graph.graphId,
      '语义评测没有覆盖当前发布图谱的全部稳定意图 ID',
      'error',
      { covered: currentIntentIdsCovered, total: intents.length },
    ));
  }
  const semanticValidation = validateIntentConformanceEvaluation(semanticPayload, { graph });
  if (semanticValidation.errors.some(item => item.code === 'graph_digest_mismatch')) {
    issues.push(issue(
      'semantic_eval_graph_digest_mismatch',
      semanticPayload.datasetId || 'semantic-evaluation',
      '语义评测集绑定的图谱 digest 与当前发布图谱不一致',
      'error',
    ));
  }

  const healthByProvider = new Map(liveProviderHealth
    .filter(item => item && typeof item.providerId === 'string' && item.observedAt)
    .map(item => [item.providerId, item]));
  const liveHealthEvidence = providers.filter(item => healthByProvider.has(item.providerId)).length;
  if (liveHealthEvidence !== providers.length) {
    issues.push(issue(
      'provider_live_health_unverified',
      'provider-registry',
      'Provider 声明状态没有完整的实时健康证据',
      'warning',
      { evidenced: liveHealthEvidence, total: providers.length },
    ));
  }

  const scenariosWithoutEval = scenarios.filter(item => item.evalDatasetId == null);
  if (scenariosWithoutEval.length) {
    issues.push(issue(
      'scenario_eval_dataset_missing',
      'scenario-registry',
      '部分场景没有绑定评测数据集',
      'warning',
      { scenarioIds: scenariosWithoutEval.map(item => item.id) },
    ));
  }
  const scenariosWithUnknownEval = scenarios.filter(item => (
    item.evalDatasetId != null && item.evalDatasetId !== semanticPayload.datasetId
  ));
  if (scenariosWithUnknownEval.length) {
    issues.push(issue(
      'scenario_eval_dataset_unknown',
      'scenario-registry',
      '部分场景绑定的评测数据集不属于当前发布评测基线',
      'warning',
      { scenarioIds: scenariosWithUnknownEval.map(item => item.id) },
    ));
  }

  const readiness = value => intents.filter(item => item.readiness === value).length;
  const declaredReady = providers.filter(item => item.health === 'ready').length;
  const errorCount = issues.filter(item => item.severity === 'error').length;

  return {
    summary: {
      intents: {
        total: intents.length,
        ready: readiness('ready'),
        mockOnly: readiness('mock-only'),
        planned: readiness('planned'),
        executable: executable.length,
      },
      skills: skills.length,
      providers: providers.length,
      capabilities: capabilities.length,
      errors: errorCount,
      warnings: issues.length - errorCount,
    },
    semanticEvaluation: {
      datasetId: semanticPayload.datasetId || null,
      coverageKind: semanticPayload.coverageKind || 'legacy-semantic',
      datasetCases: semantic.cases.length,
      referencedIntentIds: semantic.ids.size,
      currentIntentIdsCovered,
      currentIntentIdsTotal: intents.length,
      coverageRate: intents.length ? currentIntentIdsCovered / intents.length : 0,
    },
    contractCoverage: {
      completeConversationPolicies,
      missingConversationPolicies: intents.length - completeConversationPolicies,
      executableWithSlotContract,
      executableWithoutSlotContract: executable.length - executableWithSlotContract,
    },
    providerCoverage: {
      declaredReady,
      liveHealthEvidence,
      total: providers.length,
    },
    issues,
  };
}

module.exports = { auditCrossContractConformance };
