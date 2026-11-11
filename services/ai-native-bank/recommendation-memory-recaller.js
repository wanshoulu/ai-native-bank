'use strict';

const crypto = require('node:crypto');

// 信息来源四分法（见 2026-09-12 事件×记忆设计文档 §1）：
// 用户明示偏好只认记忆库；本模块是推荐侧唯一的记忆召回与 opt-out 闸门入口。
// 语义召回靠模型/策略，边界靠确定性规则：任何失败一律降级（memoryEnriched=false /
// marketing 视为已关闭），绝不把失败放大成异常。
const MARKETING_OPT_OUT_PREDICATE = 'recommendation_opt_out';

function createRecommendationMemoryRecaller(options = {}) {
  const client = options.memoryControlClient;
  const tenantId = options.tenantId || 'bank';
  const agentDomain = options.agentDomain || 'ai-native-bank';
  const memorySpace = options.memorySpace || 'customer-service';
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  if (!client || typeof client.queryContext !== 'function') {
    throw new TypeError('recommendation memory recaller requires a memory control client with queryContext');
  }

  async function query(customerId, plan) {
    const payload = {
      schemaVersion: '1.0',
      taskId: 'recsys:' + plan.purpose + ':' + crypto.randomUUID(),
      scope: {
        tenantId,
        customerId,
        agentDomain,
        memorySpace,
        purpose: plan.purpose,
      },
      memoryTypes: ['semantic_memory'],
      predicates: plan.predicates,
      temporal: { mode: 'current', asOf: now() },
      maxItems: plan.maxItems,
      maxTokens: plan.maxTokens,
      sensitivityCeiling: plan.sensitivityCeiling,
    };
    return client.queryContext(payload);
  }

  function toFact(fact) {
    const value = typeof fact.value === 'number' || typeof fact.value === 'string' ? fact.value : null;
    return {
      predicate: fact.predicate,
      value,
      display: formatValue(fact.displayType, fact.value, fact.display),
      safeLabel: fact.safeLabel,
      safeValueSummary: fact.safeValueSummary,
      evidenceRef: fact.evidenceRef,
      requiresConfirmation: fact.requiresConfirmation === true,
    };
  }

  // 显示值优先取控制面策略投影（枚举已过 enumLabels、货币已格式化），
  // 旧载荷缺 display 时回退本地格式化——枚举映射不得在本层复制。
  function formatValue(displayType, value, projected) {
    if (typeof projected === 'string' && projected && !/[<>]/.test(projected) && projected.length <= 160) {
      return projected;
    }
    if (typeof value !== 'number' && typeof value !== 'string') return null;
    if (displayType === 'currency' && typeof value === 'number' && Number.isSafeInteger(value)) {
      return '¥' + value.toLocaleString('zh-CN');
    }
    if (typeof value === 'string' && !/[<>]/.test(value) && value.length <= 160) return value;
    return null;
  }

  return Object.freeze({
    // 场景级偏好召回：按场景 registry 声明的 memory 块（purpose/predicates/天花板）查包，
    // 投影为文案可用的 facts。任何错误 → 降级 { memoryEnriched:false, facts:[] }。
    async recallScenarioFacts(customerId, memoryBlock) {
      if (!memoryBlock || !Array.isArray(memoryBlock.predicates) || !memoryBlock.predicates.length) {
        return { memoryEnriched: false, facts: [] };
      }
      try {
        const pack = await query(customerId, {
          purpose: memoryBlock.purpose,
          predicates: memoryBlock.predicates,
          sensitivityCeiling: memoryBlock.sensitivityCeiling || 'L2',
          maxItems: Math.min(memoryBlock.maxItems || 4, 4),
          maxTokens: 600,
        });
        const facts = (Array.isArray(pack.facts) ? pack.facts : [])
          .map(toFact)
          .filter(fact => fact.display !== null);
        return { memoryEnriched: facts.length > 0, facts };
      } catch (_) {
        return { memoryEnriched: false, facts: [] };
      }
    },

    // 资金规划策略召回（cashflow policyProvider 用）：把记忆值换算为 minor 金额。
    // 返回 null = 无记忆可用，调用方落回静态策略。requiresConfirmation 谓词天然带
    // "按你设定" 标注要求，由 artifact 层负责。
    async recallFundPlanningPolicy(customerId, predicates) {
      try {
        const pack = await query(customerId, {
          purpose: 'monthly_fund_planning',
          predicates,
          sensitivityCeiling: 'L2',
          maxItems: 4,
          maxTokens: 600,
        });
        const byPredicate = new Map();
        (Array.isArray(pack.facts) ? pack.facts : []).forEach(fact => {
          if (typeof fact.value === 'number' && Number.isSafeInteger(fact.value) && fact.value >= 0) {
            byPredicate.set(fact.predicate, fact.value * 100);
          }
        });
        return byPredicate;
      } catch (_) {
        return new Map();
      }
    },

    // R1 闸门（fail-closed）：deny_when_true 谓词为 true 时控制面会静默滤除该事实，
    // pack.deniedPredicates 是唯一可信信号。记忆面不可用时对 marketing 类按已关闭处理。
    async isMarketingOptOut(customerId) {
      try {
        const pack = await query(customerId, {
          purpose: 'service_personalization',
          predicates: [MARKETING_OPT_OUT_PREDICATE],
          sensitivityCeiling: 'L1',
          maxItems: 2,
          maxTokens: 200,
        });
        return Array.isArray(pack.deniedPredicates) && pack.deniedPredicates.includes(MARKETING_OPT_OUT_PREDICATE);
      } catch (_) {
        return true;
      }
    },
  });
}

module.exports = { createRecommendationMemoryRecaller };
