'use strict';

const { BankApiError } = require('./api-contract');

const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const SAFE_PREDICATE = /^[a-z][a-z0-9_]{1,79}$/;
const CANDIDATE_TYPES = new Set(['user_preference', 'canonical_service_episode']);

function exactObject(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const allowed = new Set([...required, ...optional]);
  return required.every(key => Object.hasOwn(value, key))
    && Object.keys(value).every(key => allowed.has(key));
}

function invalid(code = 'memory_command_request_invalid') {
  throw new BankApiError(422, code, 'Memory command request is invalid');
}

function validateCandidate(candidate, request) {
  if (!exactObject(candidate, ['taskId', 'intentId', 'command'])) invalid();
  if (candidate.taskId != null && !SAFE_ID.test(candidate.taskId)) invalid();
  if (candidate.intentId != null && !SAFE_ID.test(candidate.intentId)) invalid();
  const command = candidate.command;
  if (!exactObject(command, [
    'schemaVersion', 'idempotencyKey', 'candidateType', 'scope', 'subjectRef',
    'predicate', 'value', 'validFrom', 'source', 'evidence', 'consentGranted', 'safetyStatus',
  ]) || command.schemaVersion !== '1.0' || !SAFE_ID.test(command.idempotencyKey || '')
    || !CANDIDATE_TYPES.has(command.candidateType) || !SAFE_PREDICATE.test(command.predicate || '')
    || !['string', 'number', 'boolean'].includes(typeof command.value)
    // Limits mirror the authoritative Python contract (agent-memory-control
    // memory_contracts.py: SafeTextValue max_length=120) so bad commands fail
    // fast here instead of dead-lettering after control-plane rejection.
    || (typeof command.value === 'string' && (command.value.length < 1 || command.value.length > 120))
    || !Number.isFinite(Date.parse(command.validFrom)) || typeof command.consentGranted !== 'boolean'
    || !['allow', 'blocked', 'review', 'unavailable'].includes(command.safetyStatus)) invalid();
  if (!exactObject(command.scope, ['tenantId', 'customerId', 'agentDomain', 'memorySpace', 'purpose'])
    || command.scope.tenantId !== 'bank' || command.scope.customerId !== request.customerId
    || command.scope.agentDomain !== 'ai-native-bank') invalid('memory_command_scope_mismatch');
  // Runtime intake only produces these two source types; the control-plane
  // contract is a deliberate superset (agent_observation / model_inference are
  // reserved for other intake channels such as workbench and future dreamed
  // candidates), so this narrower whitelist is a boundary policy, not drift.
  if (!exactObject(command.source, ['type', 'label', 'sourceRef', 'sourceRevision'])
    || !['user_explicit', 'canonical_run'].includes(command.source.type)
    || command.source.sourceRef !== request.agentRunId) invalid('memory_command_scope_mismatch');
  if (!exactObject(command.evidence, ['eventType', 'occurredAt', 'safeSummary', 'contentHashShort'])
    || !Number.isFinite(Date.parse(command.evidence.occurredAt))
    || typeof command.evidence.safeSummary !== 'string' || command.evidence.safeSummary.length > 240
    || !/^[a-f0-9]{8,16}$/.test(command.evidence.contentHashShort || '')) invalid();
  return candidate;
}

function project(item) {
  return {
    candidateType: item.candidateType, predicate: item.predicate, status: item.status,
    decision: item.decision || null, governanceStatus: item.governanceStatus || null,
    reason: item.reason || null, policyVersion: item.policyVersion || null,
    attempts: item.attempts,
  };
}

function createMemoryCommandService(options = {}) {
  const repository = options.repository;
  const worker = options.worker;
  if (!repository || !worker || typeof worker.processIds !== 'function') {
    throw new TypeError('memory command service dependencies are required');
  }
  return Object.freeze({
    async submit(request) {
      if (!exactObject(request, ['agentRunId', 'customerId', 'candidates'])
        || !SAFE_ID.test(request.agentRunId || '') || !SAFE_ID.test(request.customerId || '')
        || !Array.isArray(request.candidates) || request.candidates.length < 1 || request.candidates.length > 20) invalid();
      const candidates = request.candidates.map(candidate => validateCandidate(candidate, request));
      const run = await repository.getAgentRun(request.agentRunId);
      if (!run || run.customerId !== request.customerId) invalid('memory_command_scope_mismatch');
      const queued = [];
      for (const candidate of candidates) {
        queued.push(await repository.enqueueMemoryCommand({
          agentRunId: request.agentRunId, customerId: request.customerId,
          taskId: candidate.taskId, intentId: candidate.intentId, command: candidate.command,
        }));
      }
      await worker.processIds(queued.map(item => item.id));
      const current = await repository.listAgentRunMemoryUpdates(request.agentRunId);
      const byIdentity = new Map(current.map(item => [`${item.candidateType}:${item.predicate}`, item]));
      return { schemaVersion: '1.0', items: queued.map(item => project(byIdentity.get(`${item.candidateType}:${item.predicate}`) || item)) };
    },
  });
}

module.exports = { createMemoryCommandService };
