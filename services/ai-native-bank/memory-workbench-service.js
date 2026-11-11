'use strict';

const { BankApiError } = require('./api-contract');
const { MemoryControlClientError } = require('./agent-memory-control-client');

const FILTER_KEYS = new Set([
  'query', 'customerRef', 'recordKinds', 'statuses', 'sources', 'purposes',
  'validFrom', 'validTo', 'cursor', 'limit',
]);
const RECORD_KINDS = new Set(['working', 'semantic', 'episodic', 'procedural']);
const STATUSES = new Set(['active', 'pending', 'contested', 'revoked', 'expired', 'rejected']);
// 与控制面写侧全量关系值域一致（含 consent 撤回与偏好墓碑），缺值会让详情投影整体拒绝。
const RELATIONS = new Set([
  'current', 'supersedes', 'superseded_by', 'contests', 'late_arrival',
  'revoked_by_consent', 'tombstoned',
]);
const DISPLAY_TYPES = new Set(['text', 'currency', 'integer', 'boolean', 'date', 'enum', 'masked_reference']);
const SENSITIVITIES = new Set(['L0', 'L1', 'L2', 'L3']);
const MEMORY_MODES = new Set(['off', 'shadow', 'read_only', 'governed_write', 'graph_projection']);
const HEALTH_VALUES = new Set(['healthy', 'degraded', 'unavailable', 'disabled']);
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SAFE_REF = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$/;
const MASKED_CUSTOMER = /^[A-Za-z0-9][A-Za-z0-9._:•*-]{0,127}$/;

function invalid(message = '记忆控制面返回结果无法识别') {
  throw new BankApiError(502, 'memory_control_invalid_response', message);
}

function validationError() {
  throw new BankApiError(422, 'validation_error', '记忆专区筛选条件无效');
}

// 客户端的操作性错误（含上游 404=句柄过期/不存在）必须保持原状态码透传，
// 否则会被兜底错误处理折叠成 500 internal_error，前端无法区分"过期"与"故障"。
function translateClientError(error) {
  if (!(error instanceof MemoryControlClientError)) throw error;
  const status = [400, 403, 404, 422, 429, 502, 503, 504].includes(error.status) ? error.status : 503;
  throw new BankApiError(status, error.code, error.message);
}

function exact(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function safeText(value, maximum = 240) {
  return typeof value === 'string' && value.length >= 1 && value.length <= maximum;
}

function isoDate(value) {
  return typeof value === 'string' && value.length <= 40 && Number.isFinite(Date.parse(value));
}

function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

function listInput(value, allowed, pattern) {
  if (value == null || value === '') return [];
  const items = Array.isArray(value) ? value : String(value).split(',');
  if (items.length > 12 || new Set(items).size !== items.length) validationError();
  return items.map(item => String(item).trim()).filter(Boolean).map(item => {
    if (allowed ? !allowed.has(item) : !pattern.test(item)) validationError();
    return item;
  });
}

function normalizeFilters(input) {
  const source = input || {};
  if (!source || typeof source !== 'object' || Array.isArray(source)
    || Object.keys(source).some(key => !FILTER_KEYS.has(key))) validationError();
  const output = {};
  if (source.query != null && String(source.query).trim()) {
    const query = String(source.query).trim();
    if (query.length > 120) validationError();
    output.query = query;
  }
  if (source.customerRef != null && source.customerRef !== '') {
    const customerRef = String(source.customerRef).trim();
    if (!MASKED_CUSTOMER.test(customerRef)) validationError();
    output.customerRef = customerRef;
  }
  const lists = [
    ['recordKinds', RECORD_KINDS, null], ['statuses', STATUSES, null],
    ['sources', null, SAFE_ID], ['purposes', null, SAFE_ID],
  ];
  lists.forEach(([key, allowed, pattern]) => {
    const values = listInput(source[key], allowed, pattern);
    if (values.length) output[key] = values;
  });
  ['validFrom', 'validTo'].forEach(key => {
    if (source[key] != null && source[key] !== '') {
      const value = String(source[key]);
      if (!isoDate(value)) validationError();
      output[key] = new Date(value).toISOString();
    }
  });
  if (output.validFrom && output.validTo && output.validFrom >= output.validTo) validationError();
  if (source.cursor != null && source.cursor !== '') {
    const cursor = String(source.cursor);
    if (!/^memcursor_[A-Za-z0-9_-]{20,80}$/.test(cursor)) validationError();
    output.cursor = cursor;
  }
  const limit = source.limit == null || source.limit === '' ? 20 : Number(source.limit);
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) validationError();
  output.limit = limit;
  return output;
}

function projectTimeRange(value) {
  if (!exact(value, ['from', 'to']) || !isoDate(value.from) || !(value.to === null || isoDate(value.to))) invalid();
  return { from: value.from, to: value.to };
}

function projectCustomer(value) {
  if (!exact(value, ['displayName', 'maskedId']) || !safeText(value.displayName, 120)
    || !safeText(value.maskedId, 120) || !MASKED_CUSTOMER.test(value.maskedId)) invalid();
  return { displayName: value.displayName, maskedId: value.maskedId };
}

function projectPredicate(value) {
  if (value === null) return null;
  if (!exact(value, ['id', 'label']) || !SAFE_ID.test(value.id || '') || !safeText(value.label, 120)) invalid();
  return { id: value.id, label: value.label };
}

function projectSource(value) {
  if (!exact(value, ['type', 'label']) || !SAFE_ID.test(value.type || '') || !safeText(value.label, 120)) invalid();
  return { type: value.type, label: value.label };
}

function projectRow(value) {
  const keys = ['id', 'customer', 'recordKind', 'predicate', 'safeValueSummary', 'displayType', 'source', 'status', 'validTime', 'systemTime', 'sensitivity'];
  if (!exact(value, keys) || !/^memview_[A-Za-z0-9_-]{20,80}$/.test(value.id || '')
    || !RECORD_KINDS.has(value.recordKind) || !safeText(value.safeValueSummary)
    || !DISPLAY_TYPES.has(value.displayType) || !STATUSES.has(value.status)
    || !SENSITIVITIES.has(value.sensitivity)) invalid();
  return {
    id: value.id,
    customer: projectCustomer(value.customer),
    recordKind: value.recordKind,
    predicate: projectPredicate(value.predicate),
    safeValueSummary: value.safeValueSummary,
    displayType: value.displayType,
    source: projectSource(value.source),
    status: value.status,
    validTime: projectTimeRange(value.validTime),
    systemTime: projectTimeRange(value.systemTime),
    sensitivity: value.sensitivity,
  };
}

function projectOverview(value) {
  if (!exact(value, ['schemaVersion', 'generatedAt', 'mode', 'policyVersion', 'health', 'counts'])
    || value.schemaVersion !== '1.0' || !isoDate(value.generatedAt)
    || !MEMORY_MODES.has(value.mode) || !SAFE_ID.test(value.policyVersion || '')
    || !exact(value.health, ['controlPlane', 'relationalStore', 'index', 'graph'])
    || !exact(value.counts, ['total', 'active', 'pending', 'contested', 'revokedOrExpired'])) invalid();
  if (!['controlPlane', 'relationalStore', 'index', 'graph'].every(key => HEALTH_VALUES.has(value.health[key]))) invalid();
  if (!Object.values(value.counts).every(count => Number.isSafeInteger(count) && count >= 0)) invalid();
  return deepFreeze(structuredClone(value));
}

function projectPage(value) {
  if (!exact(value, ['schemaVersion', 'generatedAt', 'policyVersion', 'items', 'nextCursor'])
    || value.schemaVersion !== '1.0' || !isoDate(value.generatedAt)
    || !SAFE_ID.test(value.policyVersion || '') || !Array.isArray(value.items) || value.items.length > 100
    || !(value.nextCursor === null || /^memcursor_[A-Za-z0-9_-]{20,80}$/.test(value.nextCursor))) invalid();
  return deepFreeze({
    schemaVersion: value.schemaVersion,
    generatedAt: value.generatedAt,
    policyVersion: value.policyVersion,
    items: value.items.map(projectRow),
    nextCursor: value.nextCursor,
  });
}

function projectDetailOverview(value) {
  const keys = ['customer', 'predicate', 'safeValueSummary', 'displayType', 'memoryType', 'status', 'sensitivity', 'sourceAuthority', 'confidence', 'validTime', 'systemTime', 'allowedPurposes', 'usagePolicy', 'requiresCoreRefresh', 'requiresConfirmation', 'policyVersion', 'retentionLabel'];
  if (!exact(value, keys) || !safeText(value.safeValueSummary) || !DISPLAY_TYPES.has(value.displayType)
    || !RECORD_KINDS.has(value.memoryType) || !STATUSES.has(value.status) || !SENSITIVITIES.has(value.sensitivity)
    || !exact(value.sourceAuthority, ['label', 'rank']) || !safeText(value.sourceAuthority.label, 120)
    || !Number.isInteger(value.sourceAuthority.rank) || value.sourceAuthority.rank < 0 || value.sourceAuthority.rank > 100
    || typeof value.confidence !== 'number' || value.confidence < 0 || value.confidence > 1
    || !Array.isArray(value.allowedPurposes) || value.allowedPurposes.length > 20
    || !value.allowedPurposes.every(item => SAFE_ID.test(item)) || !SAFE_ID.test(value.usagePolicy || '')
    || typeof value.requiresCoreRefresh !== 'boolean' || typeof value.requiresConfirmation !== 'boolean'
    || !SAFE_ID.test(value.policyVersion || '') || !safeText(value.retentionLabel, 120)) invalid();
  return {
    customer: projectCustomer(value.customer), predicate: projectPredicate(value.predicate),
    safeValueSummary: value.safeValueSummary, displayType: value.displayType, memoryType: value.memoryType,
    status: value.status, sensitivity: value.sensitivity, sourceAuthority: { ...value.sourceAuthority },
    confidence: value.confidence, validTime: projectTimeRange(value.validTime),
    systemTime: projectTimeRange(value.systemTime), allowedPurposes: [...value.allowedPurposes],
    usagePolicy: value.usagePolicy, requiresCoreRefresh: value.requiresCoreRefresh,
    requiresConfirmation: value.requiresConfirmation, policyVersion: value.policyVersion,
    retentionLabel: value.retentionLabel,
  };
}

function projectTimeline(value) {
  if (!exact(value, ['safeValueSummary', 'status', 'validTime', 'systemTime', 'relation'])
    || !safeText(value.safeValueSummary) || !STATUSES.has(value.status)
    || !RELATIONS.has(value.relation)) invalid();
  return { safeValueSummary: value.safeValueSummary, status: value.status,
    validTime: projectTimeRange(value.validTime), systemTime: projectTimeRange(value.systemTime), relation: value.relation };
}

function projectEvidence(value) {
  if (!exact(value, ['episodeType', 'occurredAt', 'recordedAt', 'sourceLabel', 'contentHashShort', 'safeSummary', 'sourceLink'])
    || !SAFE_ID.test(value.episodeType || '') || !isoDate(value.occurredAt) || !isoDate(value.recordedAt)
    || !safeText(value.sourceLabel, 120) || !/^[a-f0-9]{8,16}$/.test(value.contentHashShort || '')
    || !safeText(value.safeSummary)) invalid();
  let sourceLink = null;
  if (value.sourceLink !== null) {
    if (!exact(value.sourceLink, ['type', 'targetId']) || value.sourceLink.type !== 'agent_run'
      || !SAFE_REF.test(value.sourceLink.targetId || '')) invalid();
    sourceLink = { ...value.sourceLink };
  }
  return { episodeType: value.episodeType, occurredAt: value.occurredAt, recordedAt: value.recordedAt,
    sourceLabel: value.sourceLabel, contentHashShort: value.contentHashShort,
    safeSummary: value.safeSummary, sourceLink };
}

function projectUse(value) {
  if (!exact(value, ['usedAt', 'purpose', 'outcome', 'runRef']) || !isoDate(value.usedAt)
    || !SAFE_ID.test(value.purpose || '')
    || !new Set(['included', 'filtered', 'core_refresh_required', 'confirmation_required']).has(value.outcome)
    || !(value.runRef === null || SAFE_REF.test(value.runRef))) invalid();
  return { ...value };
}

function projectDetail(value) {
  if (!exact(value, ['schemaVersion', 'id', 'overview', 'timeline', 'evidence', 'uses'])
    || value.schemaVersion !== '1.0' || !/^memview_[A-Za-z0-9_-]{20,80}$/.test(value.id || '')
    || !Array.isArray(value.timeline) || value.timeline.length > 50
    || !Array.isArray(value.evidence) || value.evidence.length > 50
    || !Array.isArray(value.uses) || value.uses.length > 50) invalid();
  return deepFreeze({ schemaVersion: value.schemaVersion, id: value.id,
    overview: projectDetailOverview(value.overview), timeline: value.timeline.map(projectTimeline),
    evidence: value.evidence.map(projectEvidence), uses: value.uses.map(projectUse) });
}

function createMemoryWorkbenchService(options = {}) {
  const client = options.client;
  if (!client || typeof client.overview !== 'function') throw new TypeError('memory control client is required');
  const enabled = options.enabled === true;
  function requireEnabled() {
    if (!enabled) throw new BankApiError(404, 'memory_workbench_disabled', '记忆专区未启用');
  }
  return Object.freeze({
    overview: async filters => {
      requireEnabled();
      try {
        const normalized = normalizeFilters(filters);
        const raw = await client.overview(normalized);
        return projectOverview(raw);
      } catch (error) {
        translateClientError(error);
      }
    },
    list: async filters => {
      requireEnabled();
      try {
        const normalized = normalizeFilters(filters);
        const raw = await client.search(normalized);
        return projectPage(raw);
      } catch (error) {
        translateClientError(error);
      }
    },
    detail: async viewId => {
      requireEnabled();
      if (!/^memview_[A-Za-z0-9_-]{20,80}$/.test(String(viewId || ''))) validationError();
      try {
        return projectDetail(await client.detail(viewId));
      } catch (error) {
        translateClientError(error);
      }
    },
  });
}

module.exports = {
  createMemoryWorkbenchService,
  normalizeMemoryWorkbenchFilters: normalizeFilters,
  projectMemoryWorkbenchDetail: projectDetail,
  projectMemoryWorkbenchOverview: projectOverview,
  projectMemoryWorkbenchPage: projectPage,
};

