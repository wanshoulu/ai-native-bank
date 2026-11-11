'use strict';

function positiveInteger(value, fallback) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function safeReceipt(receipt) {
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)
    || receipt.schemaVersion !== '1.0'
    || !['ADD', 'MERGE', 'SUPERSEDE', 'CONTESTED', 'REJECT'].includes(receipt.decision)
    || typeof receipt.status !== 'string'
    || typeof receipt.reason !== 'string'
    || typeof receipt.policyVersion !== 'string') {
    const error = new Error('invalid memory governance receipt');
    error.code = 'memory_control_invalid_response';
    throw error;
  }
  return {
    decision: receipt.decision,
    governanceStatus: receipt.status,
    reason: receipt.reason,
    policyVersion: receipt.policyVersion,
  };
}

function createMemoryCommandWorker(options = {}) {
  const repository = options.repository;
  const memoryControlClient = options.memoryControlClient;
  const maxAttempts = positiveInteger(options.maxAttempts, 5);
  const batchSize = positiveInteger(options.batchSize, 10);
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  if (!repository || !memoryControlClient || typeof memoryControlClient.submitCandidate !== 'function') {
    throw new TypeError('memory command worker dependencies are required');
  }

  async function processRows(rows) {
    const summary = { processedCount: 0, persistedCount: 0, pendingRetryCount: 0, deadLetterCount: 0 };
    for (const row of rows) {
      summary.processedCount += 1;
      try {
        const receipt = safeReceipt(await memoryControlClient.submitCandidate(row.command));
        const completed = await repository.completeMemoryCommand(row.id, receipt);
        if (completed.status === 'PERSISTED') summary.persistedCount += 1;
      } catch (error) {
        const errorCode = typeof error?.code === 'string' ? error.code : 'memory_control_unavailable';
        if (row.attempts >= maxAttempts || ['memory_control_request_invalid', 'memory_control_invalid_response'].includes(errorCode)) {
          await repository.deadLetterMemoryCommand(row.id, { errorCode });
          summary.deadLetterCount += 1;
        } else {
          const delayMs = Math.min(5 * 60 * 1000, 1000 * (2 ** Math.max(0, row.attempts - 1)));
          await repository.retryMemoryCommand(row.id, {
            errorCode,
            retryAt: new Date(Date.parse(now()) + delayMs).toISOString(),
          });
          summary.pendingRetryCount += 1;
        }
      }
    }
    return summary;
  }

  return Object.freeze({
    processDue: async () => processRows(await repository.claimDueMemoryCommands({ limit: batchSize })),
    processIds: async ids => processRows(await repository.claimDueMemoryCommands({ ids, limit: Math.min(ids.length || 1, batchSize) })),
  });
}

module.exports = { createMemoryCommandWorker };
