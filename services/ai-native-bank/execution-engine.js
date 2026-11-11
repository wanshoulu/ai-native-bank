'use strict';

function createExecutionEngine(options) {
  const transferService = options.transferService;
  const systemActor = { type: 'system', id: 'bank-execution-engine' };
  const jobs = new Map();

  async function executeAuthorization(authorization) {
    return transferService.execute(
      Object.assign({}, systemActor, { traceId: authorization.traceId || 'trace_' + authorization.id }),
      authorization.transferOrderId,
      'authorization:' + authorization.id
    );
  }

  function scheduleAuthorization(authorization) {
    if (!authorization || !authorization.id || !authorization.transferOrderId) {
      throw new Error('authorization id and transferOrderId are required');
    }
    const existing = jobs.get(authorization.id);
    if (existing) return publicJob(existing);
    const job = {
      authorizationId: authorization.id,
      transferOrderId: authorization.transferOrderId,
      status: 'SCHEDULED',
      executionId: null,
      errorCode: null,
      promise: null,
    };
    jobs.set(authorization.id, job);
    job.promise = Promise.resolve()
      .then(() => executeAuthorization(authorization))
      .then(execution => {
        job.status = 'SUCCEEDED';
        job.executionId = execution.id;
        return execution;
      })
      .catch(error => {
        job.status = 'FAILED';
        job.errorCode = safeErrorCode(error);
        return null;
      });
    return publicJob(job);
  }

  function waitForAuthorization(authorizationId) {
    const job = jobs.get(authorizationId);
    return job ? job.promise : Promise.resolve(null);
  }

  function getJob(authorizationId) {
    const job = jobs.get(authorizationId);
    return job ? publicJob(job) : null;
  }

  return { executeAuthorization, scheduleAuthorization, waitForAuthorization, getJob };
}

function publicJob(job) {
  return {
    authorizationId: job.authorizationId,
    transferOrderId: job.transferOrderId,
    status: job.status,
    executionId: job.executionId,
    errorCode: job.errorCode,
  };
}

function safeErrorCode(error) {
  return error && /^[a-z0-9_]+$/.test(String(error.code || '')) ? String(error.code) : 'internal_error';
}

module.exports = { createExecutionEngine };
