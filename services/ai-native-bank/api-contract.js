'use strict';

const crypto = require('crypto');

class BankApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.name = 'BankApiError';
    this.status = status;
    this.code = code;
    this.details = Array.isArray(details) ? details : [];
    this.isOperational = true;
  }
}

function header(request, name) {
  const headers = (request && request.headers) || {};
  return headers[String(name).toLowerCase()] || headers[name] || '';
}

function createRequestMeta(request) {
  return {
    requestId: String(header(request, 'x-request-id') || 'req_' + crypto.randomUUID()),
    traceId: String(header(request, 'x-trace-id') || 'trace_' + crypto.randomUUID()),
  };
}

function successEnvelope(data, meta, extraMeta) {
  return { data, meta: Object.assign({}, meta, extraMeta || {}) };
}

function normalizeError(error) {
  if (error instanceof BankApiError) return error;
  return new BankApiError(500, 'internal_error', '服务暂时不可用');
}

function errorEnvelope(input, meta) {
  const error = normalizeError(input);
  return {
    error: {
      code: error.code,
      message: error.message,
      details: error.details,
    },
    meta: Object.assign({}, meta),
  };
}

function requireIdempotencyKey(request) {
  const key = String(header(request, 'idempotency-key') || '').trim();
  if (!key) throw new BankApiError(400, 'missing_idempotency_key', '缺少 Idempotency-Key 请求头');
  if (key.length > 128 || !/^[A-Za-z0-9._:-]+$/.test(key)) {
    throw new BankApiError(400, 'invalid_idempotency_key', 'Idempotency-Key 格式不合法');
  }
  return key;
}

function requirePositiveMinor(value, field) {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new BankApiError(422, 'validation_error', '金额必须是大于零的整数分', [
      { field: field || 'amountMinor', code: 'positive_integer_required' },
    ]);
  }
  return value;
}

function requireString(value, field, options) {
  const opts = options || {};
  const text = typeof value === 'string' ? value.trim() : '';
  const min = opts.min == null ? 1 : opts.min;
  const max = opts.max == null ? 128 : opts.max;
  if (text.length < min || text.length > max) {
    throw new BankApiError(422, 'validation_error', (field || '字段') + '不合法', [
      { field: field || 'value', code: 'invalid_length' },
    ]);
  }
  return text;
}

module.exports = {
  BankApiError,
  createRequestMeta,
  successEnvelope,
  errorEnvelope,
  normalizeError,
  requireIdempotencyKey,
  requirePositiveMinor,
  requireString,
};

