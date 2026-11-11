'use strict';

const ACTIVE_ASSISTANCE_STATUSES = new Set([
  'running', 'awaiting_clarification', 'awaiting_confirmation',
]);

const STAGE_PRESENTATION = Object.freeze({
  running: Object.freeze({
    order: 2,
    summary: '正在为你整理信息和准备方案',
    nextStep: '查看当前分析和办理步骤',
  }),
  awaiting_clarification: Object.freeze({
    order: 1,
    summary: '办理还需要你补充一项信息',
    nextStep: '补充信息后继续生成办理方案',
  }),
  awaiting_confirmation: Object.freeze({
    order: 0,
    summary: '方案已准备好，正在等待你确认',
    nextStep: '确认后继续完成后续办理',
  }),
});

function createSmartCompanionService(options = {}) {
  const repository = options.repository;
  const workflowTemplates = options.workflowTemplates;
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  if (!repository || typeof repository.listSmartCompanionCandidates !== 'function'
    || !Array.isArray(workflowTemplates)) {
    throw new TypeError('smart companion dependencies are invalid');
  }
  const templates = new Map();
  workflowTemplates.forEach(template => {
    if (!template || !safeId(template.id) || !safeVersion(template.version)
      || !safeText(template.title, 80) || templates.has(template.id)) {
      throw new TypeError('smart companion workflow template is invalid');
    }
    templates.set(template.id, template);
  });

  async function list(customerId) {
    if (!safeId(customerId)) throw new TypeError('smart companion customerId is invalid');
    const currentTime = Date.parse(requireTimestamp(now()));
    const candidates = await repository.listSmartCompanionCandidates(customerId);
    if (!Array.isArray(candidates)) throw new TypeError('smart companion candidates are invalid');
    const seen = new Set();
    const projected = candidates.flatMap(candidate => {
      const template = candidate && templates.get(candidate.workflowTemplateId);
      const policy = template && template.assistancePolicy;
      if (!candidate || !template || !policy || policy.mode !== 'smart_companion'
        || policy.activation !== 'on_user_start' || policy.homeVisible !== true
        || candidate.workflowTemplateVersion !== template.version
        || !ACTIVE_ASSISTANCE_STATUSES.has(candidate.status)
        || !safeId(candidate.businessWorkflowId) || seen.has(candidate.businessWorkflowId)
        || !safeId(candidate.sessionId) || !Array.isArray(candidate.todoSnapshot)
        || candidate.todoSnapshot.length < 1 || candidate.todoSnapshot.length > 128
        || !Number.isFinite(Date.parse(candidate.updatedAt))) return [];
      const maxAgeMs = policy.maxInactiveHours * 60 * 60 * 1000;
      if (currentTime - Date.parse(candidate.updatedAt) > maxAgeMs) return [];
      seen.add(candidate.businessWorkflowId);
      const completed = candidate.todoSnapshot.filter(todo => todo && todo.status === 'completed').length;
      const stage = STAGE_PRESENTATION[candidate.status];
      return [{
        companionId: candidate.businessWorkflowId,
        sessionId: candidate.sessionId,
        title: template.title,
        stage: candidate.status,
        progressText: `已完成 ${completed}/${candidate.todoSnapshot.length} 个步骤`,
        summary: stage.summary,
        nextStep: stage.nextStep,
        actionLabel: '继续陪办',
        updatedAt: new Date(candidate.updatedAt).toISOString(),
      }];
    }).sort((left, right) => {
      const stageOrder = STAGE_PRESENTATION[left.stage].order - STAGE_PRESENTATION[right.stage].order;
      return stageOrder || Date.parse(right.updatedAt) - Date.parse(left.updatedAt)
        || left.companionId.localeCompare(right.companionId);
    }).slice(0, 2);
    return deepFreeze(projected);
  }

  return Object.freeze({ list });
}

function safeId(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value);
}
function safeVersion(value) {
  return typeof value === 'string' && /^[0-9]+\.[0-9]+\.[0-9]+$/.test(value);
}
function safeText(value, maximum) {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum && !/[<>]/.test(value);
}
function requireTimestamp(value) {
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) {
    throw new TypeError('smart companion clock is invalid');
  }
  return new Date(value).toISOString();
}
function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

module.exports = { createSmartCompanionService };
