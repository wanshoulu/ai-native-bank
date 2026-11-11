'use strict';

const { AGENT_CAPABILITY_BINDINGS } = require('./agent-capability-service');
const { auditAgentIntegration } = require('./agent-integration-audit');
const { loadBusinessResourceRegistry } = require('./business-resource-registry');

function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

function safeNow(now) {
  const value = now();
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) {
    throw new TypeError('provider health clock must return an ISO timestamp');
  }
  return value;
}

function positiveDuration(value, fallback) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function runtimeCapabilityIds(bindings) {
  return new Set([
    ...((bindings && bindings.readonly) || []),
    ...((bindings && bindings.confirmed) || []),
  ]);
}

function statusFor(items) {
  if (!items.length || items.every(item => item.liveHealth === 'unverified')) return 'unverified';
  if (items.every(item => item.liveHealth === 'healthy')) return 'healthy';
  if (items.every(item => item.liveHealth === 'unavailable')) return 'unavailable';
  if (items.every(item => item.liveHealth === 'stale')) return 'stale';
  return 'degraded';
}

function snapshotFrom(items, observedAt) {
  return deepFreeze({
    summary: {
      registered: items.length,
      declaredReady: items.filter(item => item.declarationStatus === 'ready').length,
      liveHealthy: items.filter(item => item.liveHealth === 'healthy').length,
      liveUnavailable: items.filter(item => item.liveHealth === 'unavailable').length,
      liveStale: items.filter(item => item.liveHealth === 'stale').length,
      businessAvailable: items.filter(item => item.businessAvailability === 'available').length,
      observedAt,
      status: statusFor(items),
    },
    items,
  });
}

function createProviderHealthService(options = {}) {
  const providers = options.providers || loadBusinessResourceRegistry().providers;
  const integration = options.integration || auditAgentIntegration();
  const bindings = runtimeCapabilityIds(options.runtimeBindings || AGENT_CAPABILITY_BINDINGS);
  const probeRuntime = options.probeRuntime;
  const now = options.now || (() => new Date().toISOString());
  const ttlMs = positiveDuration(options.ttlMs, 60_000);
  const probeTimeoutMs = positiveDuration(options.probeTimeoutMs, 3_000);
  const environment = options.environment || 'mock';
  const decisions = new Map(integration.capabilities.map(item => [item.capabilityId, item]));
  let cached = null;
  let inFlight = null;

  if (!Array.isArray(providers) || !providers.length || typeof probeRuntime !== 'function') {
    throw new TypeError('provider health dependencies are required');
  }
  if (!/^[a-z][a-z0-9-]{1,31}$/.test(environment)) {
    throw new TypeError('provider health environment is invalid');
  }

  function projectItems(runtimeReady, observedAt, probeErrorCode = null) {
    return providers.map(provider => {
      const decision = decisions.get(provider.capabilityId);
      const discovered = bindings.has(provider.capabilityId);
      const liveHealth = runtimeReady && discovered ? 'healthy' : 'unavailable';
      const businessAvailable = provider.health === 'ready'
        && liveHealth === 'healthy'
        && decision && decision.integrationStatus === 'enabled';
      return {
        providerId: provider.providerId,
        capabilityId: provider.capabilityId,
        environment,
        declarationStatus: provider.health,
        publicationState: decision ? decision.integrationStatus : 'unclassified',
        liveHealth,
        businessAvailability: businessAvailable ? 'available' : 'unavailable',
        observedAt,
        probeKind: 'runtime-discovery',
        errorCode: probeErrorCode || (discovered
          ? (runtimeReady ? null : 'runtime_not_ready')
          : 'runtime_binding_missing'),
      };
    });
  }

  async function boundedRuntimeProbe() {
    let timer;
    try {
      return await Promise.race([
        Promise.resolve().then(probeRuntime),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error('provider health probe timeout')), probeTimeoutMs);
          if (typeof timer.unref === 'function') timer.unref();
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async function refresh() {
    if (inFlight) return inFlight;
    inFlight = (async () => {
      const observedAt = safeNow(now);
      try {
        const runtime = await boundedRuntimeProbe();
        cached = snapshotFrom(projectItems(Boolean(runtime && runtime.ready === true), observedAt), observedAt);
      } catch (_) {
        cached = snapshotFrom(projectItems(false, observedAt, 'runtime_probe_failed'), observedAt);
      }
      return cached;
    })();
    try {
      return await inFlight;
    } finally {
      inFlight = null;
    }
  }

  function getSnapshot() {
    if (!cached) {
      return snapshotFrom(providers.map(provider => ({
        providerId: provider.providerId,
        capabilityId: provider.capabilityId,
        environment,
        declarationStatus: provider.health,
        publicationState: (decisions.get(provider.capabilityId) || {}).integrationStatus || 'unclassified',
        liveHealth: 'unverified',
        businessAvailability: 'unavailable',
        observedAt: null,
        probeKind: 'runtime-discovery',
        errorCode: 'not_observed',
      })), null);
    }
    const current = safeNow(now);
    if (Date.parse(current) - Date.parse(cached.summary.observedAt) <= ttlMs) return cached;
    return snapshotFrom(cached.items.map(item => ({
      ...item,
      liveHealth: 'stale',
      businessAvailability: 'unavailable',
      errorCode: 'observation_stale',
    })), cached.summary.observedAt);
  }

  async function ensureFresh() {
    const snapshot = getSnapshot();
    return snapshot.summary.status === 'unverified' || snapshot.summary.status === 'stale'
      ? refresh()
      : snapshot;
  }

  return Object.freeze({ refresh, getSnapshot, ensureFresh });
}

module.exports = { createProviderHealthService };
