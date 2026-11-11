'use strict';

const crypto = require('node:crypto');
const { BankApiError } = require('./api-contract');
const { validateFocus } = require('./agent-session-contract');

function createRecommendationFocusService(options = {}) {
  const nextActionService = options.nextActionService;
  const experienceRegistry = options.experienceRegistry;
  const artifactProvider = options.artifactProvider;
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  if (!nextActionService || typeof nextActionService.requireAvailableForFocus !== 'function'
    || !experienceRegistry || !Array.isArray(experienceRegistry.scenarios)
    || !artifactProvider || typeof artifactProvider.build !== 'function') {
    throw new TypeError('recommendation focus service dependencies are invalid');
  }

  async function prepare(input) {
    validateInput(input);
    const recommendation = await nextActionService.requireAvailableForFocus(
      input.recommendationId, input.customerId,
    );
    const currentMs = Date.parse(now());
    if (!Number.isFinite(currentMs) || Date.parse(recommendation.expiresAt) <= currentMs) {
      unavailable();
    }
    const scenario = experienceRegistry.scenarios.find(item => (
      item.id === recommendation.scenarioId
      && item.version === recommendation.scenarioVersion
    ));
    if (!scenario || scenario.status !== 'active'
      || scenario.targetIntentId !== recommendation.targetIntentId) {
      unavailable();
    }
    let artifact;
    try {
      artifact = await artifactProvider.build(input.customerId, recommendation);
    } catch (_) {
      unavailable();
    }
    const context = projectContext(artifact, recommendation);
    const contextDigest = crypto.createHash('sha256')
      .update(JSON.stringify(context)).digest('hex');
    // 生产者契约：组装好的焦点必须先通过下游 Agent Session 的权威校验器，
    // 违约在本服务内降级为 409 友好提示，而不是把 400 透传给前端。
    let focus;
    try {
      focus = validateFocus({
        schemaVersion: '1.0',
        recommendationId: recommendation.recommendationId,
        mode: 'consult',
        scenarioId: recommendation.scenarioId,
        scenarioVersion: recommendation.scenarioVersion,
        targetIntentId: recommendation.targetIntentId,
        contextDigest,
        expiresAt: new Date(Date.parse(recommendation.expiresAt)).toISOString(),
        context,
      });
    } catch (_) {
      unavailable();
    }
    return focus;
  }

  return Object.freeze({ prepare });
}

function validateInput(input) {
  const keys = ['recommendationId', 'customerId', 'mode'];
  if (!input || typeof input !== 'object' || Array.isArray(input)
    || Object.keys(input).length !== keys.length
    || Object.keys(input).some(key => !keys.includes(key))
    || !isId(input.recommendationId) || !isId(input.customerId)
    || input.mode !== 'consult') {
    throw new BankApiError(422, 'recommendation_focus_invalid', '推荐咨询上下文请求无效');
  }
}

function projectContext(artifact, recommendation) {
  const detail = artifact && artifact.detail;
  const card = artifact && artifact.card;
  // 场景未注册 artifact 构建器（如账单提醒类推荐）时，上下文直接取推荐自身的
  // 安全字段，保证「问问智能体」在所有推荐上可用，而不是一律 409。
  if (!artifact) {
    return {
      title: requireText(recommendation && recommendation.title, 120),
      headline: requireText(recommendation && recommendation.reasonText, 320),
      metrics: [],
      items: [],
      insights: [],
      suggestion: '建议先核对推荐内容，再决定是否办理。',
    };
  }
  if (!detail || !card || !Array.isArray(detail.metrics)
    || !Array.isArray(detail.items) || !Array.isArray(detail.insights)) unavailable();
  const context = {
    title: requireText(card.title, 120),
    headline: requireText(detail.headline, 320),
    metrics: detail.metrics.slice(0, 8).map(item => ({
      label: requireText(item.label, 80), value: requireText(item.value, 160),
    })),
    items: detail.items.slice(0, 32).map(item => ({
      title: requireText(item.title, 120),
      detail: requireText(item.subtitle || item.status || '待处理', 160),
      amount: requireText(item.amount && item.amount.display, 80),
    })),
    insights: detail.insights.slice(0, 8).map(item => requireText(item, 320)),
    suggestion: requireText(detail.suggestion, 320),
  };
  if (Buffer.byteLength(JSON.stringify(context), 'utf8') > 24 * 1024) unavailable();
  return context;
}

function requireText(value, maximum) {
  if (typeof value !== 'string' || !value || value.length > maximum || /[<>]/.test(value)) unavailable();
  return value;
}

function isId(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value);
}

function unavailable() {
  throw new BankApiError(409, 'recommendation_unavailable', '推荐依据已变化，请刷新后重试');
}

module.exports = { createRecommendationFocusService };

