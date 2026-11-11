'use strict';

const crypto = require('node:crypto');
const { BankApiError } = require('./api-contract');

const DEFAULT_TTL_MS = 24 * 60 * 60 * 1000;
const DEFAULT_START_RECOVERY_MS = 60 * 1000;

function createNextActionService(options = {}) {
  const repository = options.repository;
  const signalService = options.signalService;
  const engine = options.engine;
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  const assetProvider = typeof options.assetProvider === 'function'
    ? options.assetProvider : async () => ({ capabilityIds: [], skillIds: [] });
  const cooldownProvider = typeof options.cooldownProvider === 'function'
    ? options.cooldownProvider : async () => [];
  const resumeProvider = typeof options.resumeProvider === 'function'
    ? options.resumeProvider : async () => [];
  const memoryRecaller = options.memoryRecaller || null;
  const memoryPreferenceSink = typeof options.memoryPreferenceSink === 'function'
    ? options.memoryPreferenceSink : null;
  const registry = options.registry || { scenarios: [] };
  const ttlMs = options.ttlMs === undefined ? DEFAULT_TTL_MS : options.ttlMs;
  const startRecoveryMs = options.startRecoveryMs === undefined
    ? DEFAULT_START_RECOVERY_MS : options.startRecoveryMs;

  if (!repository || typeof repository.getNextActionPreference !== 'function'
    || !signalService || typeof signalService.collect !== 'function'
    || !engine || typeof engine.evaluate !== 'function') {
    throw new TypeError('next action service requires repository, signalService, and engine');
  }
  if (!Number.isSafeInteger(ttlMs) || ttlMs < 60_000 || ttlMs > 31 * 86400_000) {
    throw new TypeError('next action ttlMs is invalid');
  }
  if (!Number.isSafeInteger(startRecoveryMs)
    || startRecoveryMs < 10_000 || startRecoveryMs > 30 * 60_000) {
    throw new TypeError('next action startRecoveryMs is invalid');
  }

  async function evaluationContext(customerId, query, limit) {
    validateCustomerId(customerId);
    validateListQuery(query);
    const preference = await repository.getNextActionPreference(customerId);
    const collectionInput = {
      customerId,
      surface: query.surface,
      channel: query.channel,
      personalizationEnabled: preference.personalizationEnabled,
    };
    const resumeItemsPromise = query.surface === 'agent'
      ? resumeProvider(customerId, collectionInput)
      : Promise.resolve([]);
    const [snapshot, assets, cooldownScenarioIds, resumeItems] = await Promise.all([
      signalService.collect(collectionInput),
      assetProvider(customerId),
      cooldownProvider(customerId, collectionInput),
      resumeItemsPromise,
    ]);
    const mergedCooldownIds = await applyReshowCooldown(customerId, cooldownScenarioIds);
    validateEvaluationDependencies(assets, mergedCooldownIds, resumeItems);
    return {
      snapshot,
      personalizationEnabled: preference.personalizationEnabled === true,
      request: {
        surface: query.surface,
        channel: query.channel,
        limit,
        assets,
        cooldownScenarioIds: mergedCooldownIds,
        resumeItems,
      },
    };
  }

  // 展示频控（reshowAfterHours 声明驱动）：问候/提示类场景一次展示后 N 小时内不再
  // 生成，避免"天天同一张问候卡"。声明 null（持续提醒型，如账单到期）不受限。
  // 声明在 registry、时刻在仓储，此处按场景折算合并进引擎冷却输入。
  async function applyReshowCooldown(customerId, cooldownScenarioIds) {
    const declared = (registry.scenarios || []).filter(item => item.reshowAfterHours != null);
    if (!declared.length || typeof repository.listNextActionRecentlyShownScenarios !== 'function') {
      return cooldownScenarioIds;
    }
    const windowHours = Math.max(...declared.map(item => item.reshowAfterHours));
    const recentlyShown = await repository.listNextActionRecentlyShownScenarios(customerId, { windowHours });
    if (!recentlyShown.length) return cooldownScenarioIds;
    const byId = new Map(declared.map(item => [item.id, item]));
    const nowMs = Date.parse(now());
    const suppressed = recentlyShown
      .filter(item => {
        const scenario = byId.get(item.scenarioId);
        return scenario && Number.isFinite(Date.parse(item.lastShownAt))
          && nowMs - Date.parse(item.lastShownAt) < scenario.reshowAfterHours * 3600000;
      })
      .map(item => item.scenarioId);
    if (!suppressed.length) return cooldownScenarioIds;
    return [...new Set([...cooldownScenarioIds, ...suppressed])].sort();
  }

  // 曝光疲劳轮换（通用机制，非场景特例）：持续事实型场景（待还/归因/临近到期等，
  // 触发条件长真且无 reshow 频控）会以静态分长期占据 Top-N，池内低分候选永远无法
  // 露出。此处仅对非豁免候选按"连续曝光次数 → 最后曝光时刻 → 分数 → 场景ID"重排：
  // 未展示的候选（连续计数 0）优先轮入，展示后计数 +1、未展示清零（轮休归队），
  // 形成确定性轮换。豁免类（resume_task 进行中业务 + critical 履约提醒）保持原序
  // 置前，保证业务连续性——还款提醒不因轮换消失。
  const ROTATION_EXEMPT_TYPE = 'resume_task';
  const ROTATION_EXEMPT_PRIORITY = 'critical';
  async function applyExposureRotation(customerId, query, pool) {
    if (typeof repository.listNextActionExposures !== 'function') return pool;
    const exempt = [];
    const rotating = [];
    for (const item of pool) {
      if (item.type === ROTATION_EXEMPT_TYPE || item.priority === ROTATION_EXEMPT_PRIORITY) exempt.push(item);
      else rotating.push(item);
    }
    if (!rotating.length) return pool;
    const exposures = await repository.listNextActionExposures(customerId, { surface: query.surface });
    const byScenario = new Map(exposures.map(row => [row.scenarioId, row]));
    const servedAtMs = row => {
      if (!row || !Number.isFinite(Date.parse(row.lastServedAt))) return 0;
      return Date.parse(row.lastServedAt);
    };
    rotating.sort((left, right) => {
      const leftRow = byScenario.get(left.scenarioId);
      const rightRow = byScenario.get(right.scenarioId);
      const leftCount = leftRow ? leftRow.consecutiveCount : 0;
      const rightCount = rightRow ? rightRow.consecutiveCount : 0;
      if (leftCount !== rightCount) return leftCount - rightCount;
      const leftServed = servedAtMs(leftRow);
      const rightServed = servedAtMs(rightRow);
      if (leftServed !== rightServed) return leftServed - rightServed;
      if (right.score !== left.score) return right.score - left.score;
      return left.scenarioId.localeCompare(right.scenarioId);
    });
    return [...exempt, ...rotating];
  }

  // 决策回填（泛化修复）：引擎按分数切片时不知道决策状态，已决策（accept/dismiss/...）的
  // 候选会永久占据 Top-N 窗口导致首页清空。因此服务层取全量有序池（引擎保持纯函数），
  // 过滤已有决策记录的 stable-ID 后再截取 limit——低分候选得以回填，排序确定性不变。
  const POOL_LIMIT = 32;
  async function evaluateCandidatePool(snapshot, request) {
    if (typeof engine.evaluateAll === 'function') {
      return engine.evaluateAll(snapshot, { ...request, limit: POOL_LIMIT });
    }
    return engine.evaluate(snapshot, request);
  }

  async function list(customerId, query) {
    validateListQuery(query);
    const poolLimit = typeof engine.evaluateAll === 'function' ? POOL_LIMIT : query.limit;
    const context = await evaluationContext(customerId, query, poolLimit);
    const evaluated = await evaluateCandidatePool(context.snapshot, context.request);
    const generatedAt = requireTimestamp(evaluated.generatedAt, 'generatedAt');
    // R1 闸门（读侧 fail-closed，§4.1）：本地个性化开关关闭或记忆 recommendation_opt_out=true
    // 任一命中 → marketing 类场景全部不出；service 类（账单提醒等履约触达）不受影响。
    // 记忆面不可达时按已关闭处理（宁严勿松）。
    const marketingScenarioIds = new Set((registry.scenarios || [])
      .filter(item => item.complianceClass === 'marketing').map(item => item.id));
    let candidates = evaluated.items;
    if (marketingScenarioIds.size) {
      const localOptOut = context.personalizationEnabled !== true;
      // 本地开关已关闭时无需再问记忆面（省一次查询，行为不变：同样全部抑制）
      const memoryOptOut = localOptOut ? false
        : memoryRecaller ? await memoryRecaller.isMarketingOptOut(customerId) : false;
      if (localOptOut || memoryOptOut) {
        candidates = candidates.filter(item => !marketingScenarioIds.has(item.scenarioId));
      }
      console.log('[ai-native-bank:next-action] operation=list customer=%s surface=%s pool=%d kept=%d localOptOut=%s memoryOptOut=%s',
        customerId, query.surface, evaluated.items.length, candidates.length, localOptOut, memoryOptOut);
    }
    let pool = materializeRecommendations(
      customerId, { ...query, limit: poolLimit }, candidates, generatedAt, ttlMs, context.snapshot.signals,
    );
    if (pool.length && typeof repository.listNextActionDecidedRecommendationIds === 'function') {
      const decidedIds = new Set(await repository.listNextActionDecidedRecommendationIds(
        customerId, pool.map(item => item.recommendationId),
      ));
      pool = pool.filter(item => !decidedIds.has(item.recommendationId));
    }
    pool = await applyExposureRotation(customerId, query, pool);
    const items = pool.slice(0, query.limit).map((item, index) => ({ ...item, rank: index + 1 }));
    if (items.length && typeof repository.recordNextActionExposures === 'function') {
      await repository.recordNextActionExposures(customerId, { surface: query.surface },
        items.map(item => item.scenarioId), generatedAt);
    }
    const enriched = await enrichWithMemory(customerId, items, context.personalizationEnabled);
    await repository.replaceNextActionRecommendations(customerId, enriched);
    if (!items.length && typeof repository.expireNextActionLiveRows === 'function') {
      // 空批次（门控/决策过滤后无候选）也要清掉旧 shown 行，否则旧卡绕过重新评估存活到 TTL
      await repository.expireNextActionLiveRows(customerId, { surface: query.surface, channel: query.channel });
    }
    const page = await repository.listNextActionRecommendations(customerId, {
      surface: query.surface, channel: query.channel, limit: query.limit,
    });
    return deepFreeze({
      schemaVersion: '1.0',
      mode: evaluated.mode,
      generatedAt,
      personalizationEnabled: context.personalizationEnabled,
      items: page.items,
    });
  }

  // 场景 memory 块 → 召回 → 文案插值 + memoryFacts 投影。
  // 边界：只投 safeLabel/safeValueSummary/evidenceRef/requiresConfirmation；
  // 记忆故障绝不阻断推荐主链路，任何异常降级为原文案。
  async function enrichWithMemory(customerId, items, personalizationEnabled) {
    const scenarioById = new Map((registry.scenarios || []).map(item => [item.id, item]));
    return Promise.all(items.map(async item => {
      const scenario = scenarioById.get(item.scenarioId);
      if (!scenario || !scenario.memory || !memoryRecaller || !personalizationEnabled) return item;
      let recall;
      try {
        recall = await memoryRecaller.recallScenarioFacts(customerId, scenario.memory);
      } catch (error) {
        return item;
      }
      if (!recall || !recall.memoryEnriched) return item;
      const byPredicate = new Map(recall.facts.map(fact => [fact.predicate, fact]));
      const interpolated = interpolate(scenario.memory.reasonTextTemplate, byPredicate);
      return {
        ...item,
        reasonText: interpolated !== null ? interpolated : item.reasonText,
        memoryEnriched: true,
        memoryFacts: recall.facts.map(fact => ({
          predicate: fact.predicate,
          safeLabel: fact.safeLabel,
          safeValueSummary: fact.safeValueSummary,
          evidenceRef: fact.evidenceRef,
          requiresConfirmation: fact.requiresConfirmation,
        })),
      };
    }));
  }

  async function decide(input) {
    if (input && input.decision === 'disable_personalization') {
      if (typeof repository.disableNextActionPersonalization !== 'function') {
        throw new BankApiError(503, 'next_action_preference_unavailable', 'Personalization preference is unavailable');
      }
      const result = await repository.disableNextActionPersonalization(input);
      // R1 双写（§4.1）：本地关闭生效后向记忆控制面提交 recommendation_opt_out=true
      // 候选（source=user_explicit，consentGranted=true——点击即明示）。提交失败不阻塞
      // 本地开关，读侧闸门 fail-closed 兜底；控制面按幂等键 MERGE，重放安全。
      if (memoryPreferenceSink && result) {
        try {
          await memoryPreferenceSink({
            customerId: input.customerId,
            optOut: true,
            sourceRef: input.idempotencyKey,
          });
        } catch (error) {
          const code = error && typeof error.code === 'string' ? error.code : 'unknown';
          console.warn('[ai-native-bank:next-action] operation=memory_preference_sync outcome=degraded code=%s', code);
        }
      }
      return result;
    }
    return repository.recordNextActionDecision(input);
  }

  async function requireAcceptedForStart(recommendationId, customerId) {
    validateCustomerId(customerId);
    validateIdentifier(recommendationId, 'recommendationId');
    let recommendation = typeof repository.getNextActionRecommendationInternal === 'function'
      ? await repository.getNextActionRecommendationInternal(recommendationId, customerId)
      : await repository.getNextActionRecommendation(recommendationId, customerId);
    if (!recommendation) {
      throw new BankApiError(404, 'recommendation_not_found', 'Recommendation does not exist');
    }
    if (Date.parse(recommendation.expiresAt) <= Date.parse(requireTimestamp(now(), 'now'))) {
      throw new BankApiError(409, 'recommendation_expired', 'Recommendation has expired');
    }
    if (recommendation.status === 'started'
      && typeof repository.recoverInterruptedNextActionStart === 'function') {
      const recovered = await repository.recoverInterruptedNextActionStart(
        recommendationId, customerId, startRecoveryMs,
      );
      if (recovered) recommendation = recovered;
    }
    if (recommendation.status !== 'accepted') {
      throw new BankApiError(409, 'recommendation_not_accepted', 'Recommendation must be accepted before starting');
    }
    return recommendation;
  }

  async function requireAvailableForFocus(recommendationId, customerId) {
    validateCustomerId(customerId);
    validateIdentifier(recommendationId, 'recommendationId');
    const recommendation = typeof repository.getNextActionRecommendationInternal === 'function'
      ? await repository.getNextActionRecommendationInternal(recommendationId, customerId)
      : await repository.getNextActionRecommendation(recommendationId, customerId);
    if (!recommendation) {
      throw new BankApiError(404, 'recommendation_not_found', 'Recommendation does not exist');
    }
    const currentTime = requireTimestamp(now(), 'now');
    if (Date.parse(recommendation.expiresAt) <= Date.parse(currentTime)) {
      throw new BankApiError(409, 'recommendation_expired', 'Recommendation has expired');
    }
    // 轮换退役（批次替换/空批次清扫把行标成 expired）但 TTL 未到 ≠ 真过期：
    // 用户页面渲染的是上一批卡，任何一次新评估都会把未再次入选的卡退役，
    // 造成"看得见点不动"。咨询是只读操作，内容仍在声明有效期内即可用；
    // 用户决策（dismissed/snoozed/completed）与真过期（TTL 已过）仍然拒绝。
    if (!['generated', 'shown', 'accepted', 'started'].includes(recommendation.status)
      && !(recommendation.status === 'expired'
        && Date.parse(recommendation.expiresAt) > Date.parse(currentTime))) {
      throw new BankApiError(409, 'recommendation_unavailable', 'Recommendation is not available for consultation');
    }
    return recommendation;
  }

  async function listCompatibility(customerId) {
    if (typeof engine.evaluateAll !== 'function') {
      throw new TypeError('next action engine does not support compatibility evaluation');
    }
    const legacyByScenario = new Map((registry.scenarios || [])
      .filter(item => item.legacyType).map(item => [item.id, item.legacyType]));
    const limit = Math.max(1, Math.min(legacyByScenario.size || 1, 32));
    const query = { surface: 'home', channel: null, limit: Math.min(limit, 3) };
    const context = await evaluationContext(customerId, query, limit);
    const evaluated = await engine.evaluateAll(context.snapshot, {
      ...context.request, limit, resumeItems: [],
    });
    const generatedAt = requireTimestamp(evaluated.generatedAt, 'generatedAt');
    const compatibleCandidates = evaluated.items
      .filter(item => item.type === 'proactive_service' && legacyByScenario.has(item.scenarioId));
    const compatibleItems = materializeRecommendations(
      customerId,
      { surface: 'home', channel: null, limit },
      compatibleCandidates,
      generatedAt,
      ttlMs,
      context.snapshot.signals,
    ).map(item => ({ ...item, legacyType: legacyByScenario.get(item.scenarioId) }));
    return deepFreeze({
      generatedAt,
      items: compatibleItems,
    });
  }

  return Object.freeze({
    list, decide, requireAcceptedForStart, requireAvailableForFocus, listCompatibility,
  });
}

function materializeRecommendations(customerId, query, candidates, generatedAt, ttlMs, signals) {
  if (!Array.isArray(candidates) || candidates.length > query.limit) {
    throw new BankApiError(500, 'next_action_engine_contract_invalid', 'Next Action engine returned an invalid candidate set');
  }
  const expiresAt = new Date(Date.parse(generatedAt) + ttlMs).toISOString();
  return candidates.map((candidate, index) => {
    const contextRefs = Array.isArray(candidate.contextRefs) ? candidate.contextRefs.slice().sort() : [];
    const contextDigest = sha256(JSON.stringify({
      period: generatedAt.slice(0, 7),
      facts: contextRefs.map(ref => [ref, referencedSignalValue(ref, signals)]),
    }));
    const stableKey = [
      customerId, candidate.scenarioId, candidate.scenarioVersion,
      contextDigest, query.surface, query.channel || '',
    ].join('|');
    return {
      recommendationId: 'nba_' + sha256(stableKey).slice(0, 24),
      scenarioId: candidate.scenarioId,
      scenarioVersion: candidate.scenarioVersion,
      type: candidate.type,
      title: candidate.title,
      priority: candidate.priority,
      surface: query.surface,
      channel: query.channel,
      status: 'generated',
      score: candidate.score,
      rank: index + 1,
      reasonCodes: candidate.reasonCodes,
      reasonText: candidate.reasonText,
      action: candidate.action,
      targetIntentId: candidate.targetIntentId,
      workflowTemplateId: candidate.workflowTemplateId,
      requiredSkillIds: candidate.requiredSkillIds,
      requiredCapabilityIds: candidate.requiredCapabilityIds,
      contextRefs,
      contextDigest,
      catalogAction: candidate.catalogAction,
      generatedAt,
      expiresAt,
    };
  });
}

function interpolate(template, factsByPredicate) {
  let unresolved = false;
  const text = template.replace(/\{([a-z0-9_]+)\}/g, (_, predicate) => {
    const fact = factsByPredicate.get(predicate);
    if (!fact || !fact.display) { unresolved = true; return ''; }
    return fact.display;
  });
  return unresolved || !text || text.length > 160 || /[<>]/.test(text) ? null : text;
}

function referencedSignalValue(ref, signals) {
  if (typeof ref !== 'string' || !ref.startsWith('signal:')
    || !signals || typeof signals !== 'object' || Array.isArray(signals)) return null;
  const record = signals[ref.slice('signal:'.length)];
  if (!record || typeof record !== 'object' || !Object.hasOwn(record, 'value')) return null;
  const value = record.value;
  if (typeof value === 'boolean' || typeof value === 'string'
    || (typeof value === 'number' && Number.isFinite(value))) return value;
  if (Array.isArray(value) && value.length <= 16
    && value.every(item => typeof item === 'string')) return value.slice();
  return null;
}

function validateListQuery(query) {
  if (!query || typeof query !== 'object' || Array.isArray(query)) {
    throw new BankApiError(400, 'next_action_query_invalid', 'Next Action query is invalid');
  }
  const allowed = new Set(['surface', 'channel', 'limit', 'personalizationEnabled']);
  if (Object.keys(query).some(key => !allowed.has(key))
    || !['home', 'agent', 'channel'].includes(query.surface)
    || (query.channel !== null && typeof query.channel !== 'string')
    || !Number.isInteger(query.limit) || query.limit < 1 || query.limit > 3
    || (query.personalizationEnabled !== undefined && typeof query.personalizationEnabled !== 'boolean')) {
    throw new BankApiError(400, 'next_action_query_invalid', 'Next Action query is invalid');
  }
}

function validateEvaluationDependencies(assets, cooldownScenarioIds, resumeItems) {
  if (!assets || !Array.isArray(assets.capabilityIds) || !Array.isArray(assets.skillIds)
    || !Array.isArray(cooldownScenarioIds) || !Array.isArray(resumeItems)) {
    throw new BankApiError(500, 'next_action_dependency_invalid', 'Next Action dependency returned invalid data');
  }
}

function validateCustomerId(value) { validateIdentifier(value, 'customerId'); }
function validateIdentifier(value, field) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(value)) {
    throw new BankApiError(400, 'next_action_input_invalid', `${field} is invalid`);
  }
}
function requireTimestamp(value, field) {
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) {
    throw new TypeError(`${field} must be an ISO timestamp`);
  }
  return new Date(Date.parse(value)).toISOString();
}
function sha256(value) { return crypto.createHash('sha256').update(value).digest('hex'); }
function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

module.exports = { createNextActionService };
