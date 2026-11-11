'use strict';

const { loadServiceToken } = require('./service-token');

class MemoryControlClientError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = 'MemoryControlClientError';
    this.status = status;
    this.code = code;
    this.isOperational = true;
  }
}

function fail(status, code, message) {
  throw new MemoryControlClientError(status, code, message);
}

function positiveInteger(value, fallback) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function createAgentMemoryControlClient(options = {}) {
  const fetchImpl = options.fetchImpl || globalThis.fetch;
  const baseUrl = String(
    options.baseUrl || process.env.AGENT_MEMORY_CONTROL_URL || 'http://127.0.0.1:18130',
  ).replace(/\/+$/, '');
  const timeoutMs = positiveInteger(options.timeoutMs, 2500);
  const maxResponseBytes = positiveInteger(options.maxResponseBytes, 256 * 1024);
  const tokenProvider = typeof options.tokenProvider === 'function'
    ? options.tokenProvider
    : () => loadServiceToken({
        envValue: options.serviceToken == null
          ? process.env.AGENT_MEMORY_CONTROL_SERVICE_TOKEN
          : options.serviceToken,
        tokenFile: options.tokenFile || process.env.AGENT_MEMORY_CONTROL_SERVICE_TOKEN_FILE,
      });
  if (typeof fetchImpl !== 'function') {
    fail(503, 'memory_control_unavailable', '记忆控制面暂时不可用');
  }

  async function post(path, payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const token = tokenProvider();
      if (!token) fail(503, 'memory_control_unavailable', '记忆控制面暂时不可用');
      const response = await fetchImpl(baseUrl + path, {
        method: 'POST',
        headers: {
          accept: 'application/json',
          authorization: 'Bearer ' + token,
          'content-type': 'application/json',
        },
        body: JSON.stringify(payload || {}),
        signal: controller.signal,
      });
      const contentType = response.headers && typeof response.headers.get === 'function'
        ? String(response.headers.get('content-type') || '').toLowerCase()
        : '';
      if (!contentType.startsWith('application/json')) {
        fail(502, 'memory_control_invalid_response', '记忆控制面返回结果无法识别');
      }
      const text = await response.text();
      if (Buffer.byteLength(text, 'utf8') > maxResponseBytes) {
        fail(502, 'memory_control_invalid_response', '记忆控制面返回结果无法识别');
      }
      let body;
      try {
        body = JSON.parse(text);
      } catch (_error) {
        fail(502, 'memory_control_invalid_response', '记忆控制面返回结果无法识别');
      }
      if (!response.ok) {
        if (response.status === 403) fail(403, 'memory_workbench_forbidden', '无权访问记忆专区');
        if (response.status === 404) fail(404, 'memory_view_not_found', '记忆详情不存在或已过期');
        if (response.status === 429) fail(429, 'memory_workbench_rate_limited', '记忆请求过于频繁，请稍后再试');
        if (response.status === 400 || response.status === 422) {
          fail(422, 'memory_control_request_invalid', '记忆专区查询参数无效');
        }
        fail(503, 'memory_control_unavailable', '记忆控制面暂时不可用');
      }
      return body;
    } catch (error) {
      if (error instanceof MemoryControlClientError) throw error;
      if (error && error.name === 'AbortError') {
        fail(504, 'memory_control_timeout', '记忆控制面查询超时');
      }
      const safeCode = error && typeof error.code === 'string' && /^[A-Za-z0-9_.-]{1,80}$/.test(error.code)
        ? error.code
        : 'transport_error';
      console.warn('[ai-native-bank:memory-control] operation=post code=%s', safeCode);
      fail(503, 'memory_control_unavailable', '记忆控制面暂时不可用');
    } finally {
      clearTimeout(timer);
    }
  }

  return Object.freeze({
    overview: filters => post('/v1/workbench/memories:overview', filters),
    search: filters => post('/v1/workbench/memories:search', filters),
    detail: viewId => post('/v1/workbench/memories:detail', { viewId }),
    submitCandidate: command => post('/v1/candidates:submit', command),
    queryContext: plan => post('/v1/context:query', plan),
  });
}

module.exports = { MemoryControlClientError, createAgentMemoryControlClient };
