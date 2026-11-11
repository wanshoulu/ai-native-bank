'use strict';

const { BankApiError } = require('./api-contract');

const MAX_AGENT_SESSION_TURN_BYTES = 32 * 1024;

// 焦点上下文的唯一权威校验器：agent-session-service（消费方）与
// recommendation-focus-service（生产方）共用，保证生产者的规范输出必然
// 通过消费者校验，字段模式不允许在多处复制。
function validateFocus(value) {
  const keys = [
    'schemaVersion', 'recommendationId', 'mode', 'scenarioId', 'scenarioVersion',
    'targetIntentId', 'contextDigest', 'expiresAt', 'context',
  ];
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).length !== keys.length
    || Object.keys(value).some(key => !keys.includes(key))
    || value.schemaVersion !== '1.0' || value.mode !== 'consult'
    || !/^[A-Za-z0-9_.:-]{1,160}$/.test(value.recommendationId || '')
    || !/^[a-z0-9-]{1,96}$/.test(value.scenarioId || '')
    || !/^\d+\.\d+\.\d+$/.test(value.scenarioVersion || '')
    || !/^[a-z0-9.-]{1,160}$/.test(value.targetIntentId || '')
    || !/^[a-f0-9]{64}$/.test(value.contextDigest || '')
    || !Number.isFinite(Date.parse(value.expiresAt))
    || !value.context || typeof value.context !== 'object' || Array.isArray(value.context)) {
    throw new BankApiError(400, 'invalid_agent_session_focus', '推荐会话上下文无效');
  }
  const contextKeys = ['title', 'headline', 'metrics', 'items', 'insights', 'suggestion'];
  const context = value.context;
  if (Object.keys(context).length !== contextKeys.length
    || Object.keys(context).some(key => !contextKeys.includes(key))
    || typeof context.title !== 'string' || !context.title || context.title.length > 120
    || typeof context.headline !== 'string' || !context.headline || context.headline.length > 320
    || typeof context.suggestion !== 'string' || !context.suggestion || context.suggestion.length > 320
    || !Array.isArray(context.metrics) || context.metrics.length > 8
    || !Array.isArray(context.items) || context.items.length > 32
    || !Array.isArray(context.insights) || context.insights.length > 8) {
    throw new BankApiError(400, 'invalid_agent_session_focus', '推荐会话展示上下文无效');
  }
  const safeText = (text, max) => typeof text === 'string' && text.length > 0
    && text.length <= max && !/[<>]/.test(text);
  if (context.metrics.some(item => !item || Object.keys(item).length !== 2
    || !safeText(item.label, 80) || !safeText(item.value, 160))
    || context.items.some(item => !item || Object.keys(item).length !== 3
      || !safeText(item.title, 120) || !safeText(item.detail, 160)
      || !safeText(item.amount, 80))
    || context.insights.some(item => !safeText(item, 320))) {
    throw new BankApiError(400, 'invalid_agent_session_focus', '推荐会话内容无效');
  }
  return deepFreeze(JSON.parse(JSON.stringify(value)));
}

function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

module.exports = { MAX_AGENT_SESSION_TURN_BYTES, validateFocus };
