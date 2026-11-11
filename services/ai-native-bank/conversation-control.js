'use strict';

const crypto = require('node:crypto');
const { isSafeAgentSessionText } = require('./agent-session-projection');

const MAX_RESUMABLE_WORKFLOW_CHOICES = 8;

const SUSPEND_PHRASES = new Set([
  '退出', '暂停', '先暂停', '先退出', '先放一放', '稍后再办', '暂时不办',
]);
const CANCEL_PHRASES = new Set([
  '取消', '取消当前业务', '取消剩余业务', '不办了', '不用办了',
]);
const RESUME_PHRASES = new Set([
  '继续', '继续办理', '继续刚才的业务', '恢复办理', '恢复当前业务',
]);
const CONTEXTUAL_RESUME_PREFIXES = ['继续', '恢复', '接着'];

function normalizeControlText(value) {
  if (typeof value !== 'string') return '';
  return value.normalize('NFKC').trim().replace(/[\s，。！？、,.!?]+/gu, '');
}

function classifyConversationControl(message, context, structuredControl) {
  if (structuredControl && ['SUSPEND', 'CANCEL', 'RESUME'].includes(structuredControl.action)) {
    return { action: structuredControl.action, reason: 'structured_control' };
  }
  const text = normalizeControlText(message);
  const lifecycle = context && context.lifecycle === 'suspended' ? 'suspended' : 'active';
  if (!text) return { action: 'CONTINUE', reason: 'business_reply' };
  if (RESUME_PHRASES.has(text) && lifecycle === 'suspended') {
    return { action: 'RESUME', reason: 'user_requested_resume' };
  }
  const resumeAnchors = context && Array.isArray(context.resumeAnchors)
    ? context.resumeAnchors.map(normalizeControlText).filter(Boolean) : [];
  const contextualResume = text.length <= 200
    && CONTEXTUAL_RESUME_PREFIXES.some(prefix => text.startsWith(prefix))
    && resumeAnchors.some(anchor => text.includes(anchor));
  if (contextualResume && lifecycle === 'suspended') {
    return { action: 'RESUME', reason: 'user_requested_contextual_resume' };
  }
  if (text.length > 16) return { action: 'CONTINUE', reason: 'business_reply' };
  if (CANCEL_PHRASES.has(text)) {
    return { action: 'CANCEL', reason: 'user_requested_cancel' };
  }
  if (SUSPEND_PHRASES.has(text) && lifecycle !== 'suspended') {
    return { action: 'SUSPEND', reason: 'user_requested_pause' };
  }
  return { action: 'CONTINUE', reason: 'business_reply' };
}

function deriveWorkflowResumeAnchors(workflow) {
  const state = workflow && workflow.conversationState;
  if (!state || !Array.isArray(state.goals)) return [];
  const activeGoal = state.goals.find(goal => goal.taskId === workflow.activeGoalTaskId)
    || state.goals.find(goal => goal.status !== 'ready')
    || state.goals[0];
  if (!activeGoal || !Array.isArray(activeGoal.slots)) return [];
  return [...new Set(activeGoal.slots
    .map(slot => slot && slot.value)
    .filter(value => typeof value === 'string')
    .map(value => value.trim())
    .filter(value => value.length >= 2 && value.length <= 64 && !/^\d+(?:\.\d+)?$/.test(value))
  )];
}

function activeWorkflowIntentId(workflow) {
  const state = workflow && workflow.conversationState;
  if (!state || !Array.isArray(state.goals)) return null;
  const goal = state.goals.find(item => item.taskId === workflow.activeGoalTaskId)
    || state.goals.find(item => item.status !== 'ready')
    || state.goals[0];
  return goal && typeof goal.intentId === 'string' ? goal.intentId : null;
}

function safeGraphLabel(value, fallback) {
  if (typeof value !== 'string') return fallback;
  const text = value.normalize('NFKC').trim();
  if (!text || text.length > 96
    || /(?:[a-z][a-z0-9+.-]*:\/\/|www\.|(?:bwf|conv|acp|acg|acct|payee|credit)_)/iu.test(text)
    || /(?:\d[ -]?){15,}/u.test(text)
    || !isSafeAgentSessionText(text)) return fallback;
  return text;
}

function projectResumableWorkflowSummary(workflow, intentGraph) {
  const nodes = intentGraph && Array.isArray(intentGraph.nodes) ? intentGraph.nodes : [];
  const intentId = activeWorkflowIntentId(workflow);
  const intent = nodes.find(node => node && node.type === 'intent' && node.id === intentId) || null;
  const objectId = intent && typeof intent.businessObjectId === 'string'
    ? `object.${intent.businessObjectId}` : null;
  const object = objectId
    ? nodes.find(node => node && node.type === 'business-object' && node.id === objectId)
    : null;
  const todos = workflow && Array.isArray(workflow.todoSnapshot) ? workflow.todoSnapshot : [];
  return {
    businessTitle: safeGraphLabel(intent && intent.title, '未完成业务'),
    completedTodoCount: todos.filter(todo => todo && todo.status === 'completed').length,
    totalTodoCount: todos.length,
    updatedAt: typeof workflow.updatedAt === 'string' ? workflow.updatedAt : '',
    riskLevel: intent && /^L[0-3]$/.test(intent.riskLevel) ? intent.riskLevel : 'L0',
    maskedObject: `${safeGraphLabel(object && object.title, '业务对象')} ****`,
  };
}

function stableSemanticValue(value) {
  if (Array.isArray(value)) return value.map(stableSemanticValue);
  if (!value || typeof value !== 'object') return value;
  return Object.keys(value).sort().reduce((result, key) => {
    result[key] = stableSemanticValue(value[key]);
    return result;
  }, {});
}

function resumableWorkflowSemanticKey(workflow) {
  const state = workflow && workflow.conversationState;
  const goals = state && Array.isArray(state.goals) ? state.goals : [];
  const semanticGoals = goals.map(goal => ({
    intentId: goal && typeof goal.intentId === 'string' ? goal.intentId : '',
    slots: Array.isArray(goal && goal.slots) ? goal.slots.map(slot => ({
      name: slot && typeof slot.name === 'string' ? slot.name : '',
      value: stableSemanticValue(slot && slot.value),
    })).sort((left, right) => left.name.localeCompare(right.name)) : [],
  })).filter(goal => goal.intentId);
  if (!semanticGoals.length) return `workflow:${workflow && workflow.id}`;
  const sharedSlots = Array.isArray(state.sharedSlots) ? state.sharedSlots.map(slot => ({
    name: slot && typeof slot.name === 'string' ? slot.name : '',
    value: stableSemanticValue(slot && slot.value),
  })).sort((left, right) => left.name.localeCompare(right.name)) : [];
  return JSON.stringify({
    intentShape: typeof state.intentShape === 'string' ? state.intentShape : '',
    goals: semanticGoals,
    sharedSlots,
  });
}

function selectResumableWorkflowRecords(workflows, limit) {
  const boundedLimit = Number.isSafeInteger(limit) && limit > 0
    ? Math.min(limit, MAX_RESUMABLE_WORKFLOW_CHOICES)
    : MAX_RESUMABLE_WORKFLOW_CHOICES;
  const seen = new Set();
  return (Array.isArray(workflows) ? workflows : [])
    .filter(workflow => workflow && workflow.status === 'suspended')
    .sort((left, right) => {
      const updated = Date.parse(right.updatedAt) - Date.parse(left.updatedAt);
      if (Number.isFinite(updated) && updated !== 0) return updated;
      const created = Date.parse(right.createdAt) - Date.parse(left.createdAt);
      if (Number.isFinite(created) && created !== 0) return created;
      return String(right.id).localeCompare(String(left.id));
    })
    .filter(workflow => {
      const key = resumableWorkflowSemanticKey(workflow);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, boundedLimit);
}

function createResumableWorkflowChoices(workflows, intentGraph) {
  return (Array.isArray(workflows) ? workflows : [])
    .slice(0, MAX_RESUMABLE_WORKFLOW_CHOICES).map((workflow, index) => {
    const summary = projectResumableWorkflowSummary(workflow, intentGraph);
    const position = index + 1;
    const updated = summary.updatedAt.replace('T', ' ').replace(/\.\d{3}Z$/, 'Z');
    const label = `${position}. ${summary.businessTitle} · ${summary.completedTodoCount}/${summary.totalTodoCount} · ${updated}`;
    const summaryFingerprint = JSON.stringify([
      summary.businessTitle, summary.completedTodoCount, summary.totalTodoCount,
      summary.updatedAt, summary.riskLevel, summary.maskedObject,
    ]);
    const selector = crypto.createHash('sha256').update(
      `${workflow.id}\x1f${workflow.createdAt}\x1f${workflow.workflowRevision}\x1f${summaryFingerprint}`,
      'utf8',
    ).digest('hex').slice(0, 10);
    const message = `继续第${position}项：${summary.businessTitle}（更新于 ${updated}，选择码 ${selector}）`;
    return {
      workflow,
      summary,
      label,
      message,
      selectionAnchors: [`选择码${selector}`],
    };
  });
}

function hasResumableWorkflowSelectionCode(value) {
  return typeof value === 'string' && /选择码\s*[a-f0-9]{10}/iu.test(value);
}

module.exports = {
  classifyConversationControl,
  createResumableWorkflowChoices,
  deriveWorkflowResumeAnchors,
  hasResumableWorkflowSelectionCode,
  MAX_RESUMABLE_WORKFLOW_CHOICES,
  normalizeControlText,
  projectResumableWorkflowSummary,
  selectResumableWorkflowRecords,
};
