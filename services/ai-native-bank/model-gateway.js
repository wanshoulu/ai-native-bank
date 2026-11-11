'use strict';

const http = require('node:http');
const https = require('node:https');
const { Readable } = require('node:stream');
const { API_ROUTES } = require('../../config/api-keys');
const { loadServiceToken, verifyBearer } = require('./service-token');

const FIXED_MODEL = 'qwen3.7-flash';
const FIXED_FALLBACK_MODEL = 'qwen-plus';
const FIXED_EMBEDDING_MODEL = 'qwen3.7-text-embedding-flash';
const DEFAULT_BODY_LIMIT = 512 * 1024;
const DEFAULT_EMBEDDING_BODY_LIMIT = 128 * 1024;
const DEFAULT_EMBEDDING_RESPONSE_LIMIT = 2 * 1024 * 1024;
const DEFAULT_TIMEOUT_MS = 120000;
const DEFAULT_FIRST_CHUNK_TIMEOUT_MS = 15000;
const DEFAULT_UPSTREAM_ATTEMPTS = 3;
const RETRYABLE_UPSTREAM_STATUSES = new Set([502, 503, 504]);
// 上游返回 200 但流式首块迟迟不至（中转拥塞的典型形态）不算 HTTP 失败，
// 现有按状态码的 failover 永远不触发；调用方（openjiwen 流读取器）各自
// 以更长的首块超时烧满重试。网关在唯一卡点处以更短窗口探活首块，
// 停顿即弃连切备用路由。
class UpstreamStalledError extends Error {
  constructor() {
    super('upstream stream stalled before first chunk');
    this.name = 'UpstreamStalledError';
  }
}
const FAILOVER_UPSTREAM_STATUSES = new Set([
  401, 402, 403, 404, 408, 429, 500, 501, 502, 503, 504,
]);
const ALLOWED_FIELDS = new Set([
  'model', 'messages', 'tools', 'tool_choice', 'temperature', 'top_p', 'max_tokens',
  'stream', 'stream_options', 'parallel_tool_calls', 'response_format', 'stop',
  'frequency_penalty', 'presence_penalty', 'logprobs', 'top_logprobs', 'seed',
  'thinking',
]);
const STRIPPED_COMPATIBILITY_FIELDS = new Set([
  'enable_thinking', 'reasoning',
]);
const ALLOWED_THINKING_TYPES = new Set(['enabled', 'disabled']);
// 未显式声明思考开关的请求注入的默认值；置空串或非法值可恢复上游默认。
const DEFAULT_THINKING_TYPE = ALLOWED_THINKING_TYPES.has(
  (process.env.AI_BANK_MODEL_DEFAULT_THINKING || 'disabled').trim().toLowerCase(),
)
  ? (process.env.AI_BANK_MODEL_DEFAULT_THINKING || 'disabled').trim().toLowerCase()
  : '';

function normalizeThinking(value, path) {
  // 透传上游的思考开关（DeepSeek 风格 {"type":"enabled"|"disabled"}）。
  // 仅接受该形状，防止借道注入任意参数；其余厂商方言仍被剥离。
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).length !== 1
    || !ALLOWED_THINKING_TYPES.has(value.type)) {
    validation('thinking 字段无效 at ' + path);
  }
  return { type: value.type };
}
const ALLOWED_EMBEDDING_FIELDS = new Set(['model', 'input', 'encoding_format', 'dimensions']);
const MAX_EMBEDDING_BATCH = 10;
const MAX_EMBEDDING_TEXT_BYTES = 16 * 1024;
const MAX_EMBEDDING_TOTAL_BYTES = 64_000;
const FIXED_EMBEDDING_DIMENSIONS = 1024;

class ModelGatewayError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = 'ModelGatewayError';
    this.status = status;
    this.code = code;
    this.isOperational = true;
  }
}

function validation(message) {
  throw new ModelGatewayError(422, 'model_gateway_validation_error', message || '模型请求格式无效');
}

function validateJsonShape(value, depth, state, path = '$') {
  if (depth > 256) validation('模型请求嵌套层级过深 at ' + path);
  state.nodes += 1;
  if (state.nodes > 10000) validation('模型请求结构过大 at ' + path);
  if (typeof value === 'string') {
    state.stringBytes += Buffer.byteLength(value, 'utf8');
    if (value.length > 100000 || state.stringBytes > 400000) validation('模型请求文本过长 at ' + path);
    return;
  }
  if (value == null || typeof value === 'boolean') return;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) validation('模型请求包含非有限数字 at ' + path);
    return;
  }
  if (Array.isArray(value)) {
    if (value.length > 512) validation('模型请求数组过大 at ' + path);
    value.forEach((item, index) => validateJsonShape(item, depth + 1, state, `${path}[${index}]`));
    return;
  }
  if (typeof value !== 'object') validation('模型请求包含非法值类型 at ' + path);
  const keys = Object.keys(value);
  if (keys.length > 128) validation('模型请求对象过大 at ' + path);
  keys.forEach(key => {
    if (key.length > 128) validation('模型请求键过长 at ' + path);
    validateJsonShape(value[key], depth + 1, state, `${path}.${key}`);
  });
}

function normalizeChatCompletion(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) validation();
  const unknown = Object.keys(payload).filter(key => !ALLOWED_FIELDS.has(key) && !STRIPPED_COMPATIBILITY_FIELDS.has(key));
  if (unknown.length) validation('模型请求包含不允许的字段：' + unknown.sort().join(','));
  // 会话跨轮持久化（task loop 多轮 ReAct）会累积历史消息；128 条在多目
  // 标业务工作流中不够用，放宽到 240 并同步放宽数组上限（仍有界）。
  if (!Array.isArray(payload.messages) || payload.messages.length < 1 || payload.messages.length > 240) {
    validation('messages 数量无效');
  }
  if (payload.tools != null && (!Array.isArray(payload.tools) || payload.tools.length > 64)) {
    validation('tools 数量无效');
  }
  if (payload.stop != null && Array.isArray(payload.stop) && payload.stop.length > 16) validation('stop 数量无效');
  if (payload.stream != null && typeof payload.stream !== 'boolean') validation('stream 类型无效');
  validateJsonShape(payload, 0, { nodes: 0, stringBytes: 0 });
  const normalized = {};
  for (const key of ALLOWED_FIELDS) {
    if (key === 'thinking') continue;
    if (Object.prototype.hasOwnProperty.call(payload, key)) normalized[key] = payload[key];
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'thinking')) {
    normalized.thinking = normalizeThinking(payload.thinking, '$.thinking');
  } else if (DEFAULT_THINKING_TYPE) {
    // 网关侧统一默认：未显式声明思考开关的调用方（如安全 rail 的
    // 文本分析）默认关闭思考，避免隐藏的思考延迟进入请求链路。
    // 显式传 enabled/disabled 的调用方不受影响。
    normalized.thinking = { type: DEFAULT_THINKING_TYPE };
  }
  // 尊重请求 model（有可用路由时），否则回落 FIXED_MODEL——selectUpstream 的
  // requestedModelOf 做最终仲裁，这里保留原值即可
  normalized.model = typeof payload.model === 'string' && payload.model.trim()
    ? payload.model.trim()
    : FIXED_MODEL;
  return normalized;
}

function normalizeEmbeddingRequest(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) validation();
  const unknown = Object.keys(payload).filter(key => !ALLOWED_EMBEDDING_FIELDS.has(key));
  if (unknown.length) validation('Embedding 请求包含不允许的字段：' + unknown.sort().join(','));

  const values = typeof payload.input === 'string' ? [payload.input] : payload.input;
  if (!Array.isArray(values) || values.length < 1 || values.length > MAX_EMBEDDING_BATCH) {
    validation('Embedding input 数量无效');
  }
  let totalBytes = 0;
  values.forEach(value => {
    if (typeof value !== 'string' || value.length < 1) validation('Embedding input 必须是非空文本');
    const bytes = Buffer.byteLength(value, 'utf8');
    if (bytes > MAX_EMBEDDING_TEXT_BYTES) validation('Embedding 单条文本过长');
    totalBytes += bytes;
  });
  if (totalBytes > MAX_EMBEDDING_TOTAL_BYTES) validation('Embedding 文本总量过大');
  if (payload.encoding_format != null && payload.encoding_format !== 'float') {
    validation('Embedding encoding_format 仅支持 float');
  }
  if (payload.dimensions != null && payload.dimensions !== FIXED_EMBEDDING_DIMENSIONS) {
    validation('Embedding dimensions 无效');
  }

  return {
    model: FIXED_EMBEDDING_MODEL,
    input: typeof payload.input === 'string' ? payload.input : values.slice(),
    encoding_format: 'float',
    dimensions: FIXED_EMBEDDING_DIMENSIONS,
  };
}

function readBoundedJson(req, maximumBytes) {
  return new Promise((resolve, reject) => {
    const declared = Number(req.headers['content-length'] || 0);
    if (Number.isFinite(declared) && declared > maximumBytes) {
      reject(new ModelGatewayError(413, 'model_gateway_body_too_large', '模型请求体过大'));
      return;
    }
    const chunks = [];
    let size = 0;
    let settled = false;
    req.on('data', chunk => {
      if (settled) return;
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      size += buffer.length;
      if (size > maximumBytes) {
        settled = true;
        reject(new ModelGatewayError(413, 'model_gateway_body_too_large', '模型请求体过大'));
        return;
      }
      chunks.push(buffer);
    });
    req.on('end', () => {
      if (settled) return;
      settled = true;
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch (_) {
        reject(new ModelGatewayError(400, 'model_gateway_invalid_json', '模型请求体必须是有效 JSON'));
      }
    });
    req.on('error', () => {
      if (!settled) reject(new ModelGatewayError(400, 'model_gateway_request_failed', '模型请求读取失败'));
    });
  });
}

function defaultUpstreamRequest(options) {
  return new Promise((resolve, reject) => {
    const transport = options.protocol === 'http:' ? http : https;
    // 部分网络环境下容器直连公网模型端点会被路径重置（TCP 可通、TLS
    // 握手黑洞，宿主机正常）。AI_BANK_MODEL_CONNECT_RELAY 按域名指定
    // 宿主机端口转发中继，TCP 连中继地址，TLS SNI 与 Host 头保持原域名：
    //   "maas.qianwenaiapi.com=host.docker.internal:18443;..."
    const relay = options.protocol !== 'http:'
      ? CONNECT_RELAYS.get(String(options.hostname || '').toLowerCase())
      : null;
    // 走中继时绝不能传 hostname：node https.request 会优先用 hostname
    // 做 TCP 连接（绕过中继直连被黑洞的端点）。只用 host（TCP 目标）
    // + servername（TLS SNI）+ 显式 Host 头。
    const requestOptions = {
      protocol: options.protocol || 'https:',
      port: relay ? relay.port : options.port,
      method: 'POST',
      path: options.path,
      servername: options.hostname,
      headers: Object.assign({}, options.headers, {
        host: String(options.hostname || ''),
      }),
    };
    if (relay) requestOptions.host = relay.host;
    else requestOptions.hostname = options.hostname;
    const request = transport.request(requestOptions, response => resolve({
      status: response.statusCode || 502,
      headers: response.headers || {},
      body: response,
    }));
    request.setTimeout(options.timeoutMs, () => request.destroy(new Error('timeout')));
    request.on('error', reject);
    request.end(options.body);
  });
}

function parseConnectRelay() {
  const map = new Map();
  String(process.env.AI_BANK_MODEL_CONNECT_RELAY || '').split(';').forEach(part => {
    const entry = part.trim();
    if (!entry) return;
    const eq = entry.indexOf('=');
    if (eq <= 0) return;
    const host = entry.slice(0, eq).trim().toLowerCase();
    const target = entry.slice(eq + 1).trim();
    const colon = target.lastIndexOf(':');
    if (!host || colon <= 0) return;
    const relayHost = target.slice(0, colon).trim();
    const relayPort = Number(target.slice(colon + 1));
    if (!relayHost || !Number.isInteger(relayPort) || relayPort < 1 || relayPort > 65535) return;
    map.set(host, { host: relayHost, port: relayPort });
  });
  return map;
}

const CONNECT_RELAYS = parseConnectRelay();

function readBoundedResponseBody(body, maximumBytes) {
  if (!isReadable(body)) {
    const buffer = Buffer.isBuffer(body) ? body : Buffer.from(String(body == null ? '' : body));
    if (buffer.length > maximumBytes) {
      throw new ModelGatewayError(502, 'model_gateway_bad_response', 'bad response');
    }
    return Promise.resolve(buffer);
  }
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    body.on('data', chunk => {
      if (settled) return;
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      size += buffer.length;
      if (size > maximumBytes) {
        settled = true;
        body.destroy();
        reject(new ModelGatewayError(502, 'model_gateway_bad_response', 'bad response'));
        return;
      }
      chunks.push(buffer);
    });
    body.on('end', () => {
      if (settled) return;
      settled = true;
      resolve(Buffer.concat(chunks));
    });
    body.on('error', () => {
      if (!settled) reject(new ModelGatewayError(502, 'model_gateway_bad_response', 'bad response'));
    });
  });
}

function validateEmbeddingResponse(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || !Array.isArray(value.data)
    || value.data.length < 1 || value.data.length > MAX_EMBEDDING_BATCH) {
    throw new ModelGatewayError(502, 'model_gateway_bad_response', 'bad response');
  }
  value.data.forEach(item => {
    if (!item || typeof item !== 'object' || !Array.isArray(item.embedding)
      || item.embedding.length < 1 || item.embedding.length > 4096
      || item.embedding.some(number => typeof number !== 'number' || !Number.isFinite(number))) {
      throw new ModelGatewayError(502, 'model_gateway_bad_response', 'bad response');
    }
  });
  return value;
}

function sendError(res, error) {
  if (res.writableEnded) return;
  const known = error instanceof ModelGatewayError;
  const status = known ? error.status : 502;
  const code = known ? error.code : 'model_gateway_unavailable';
  const messages = {
    model_gateway_unauthorized: '内部模型服务身份无效',
    model_gateway_validation_error: '模型请求格式无效',
    model_gateway_body_too_large: '模型请求体过大',
    model_gateway_invalid_json: '模型请求体必须是有效 JSON',
    model_gateway_request_failed: '模型请求读取失败',
    model_gateway_not_ready: '模型服务尚未就绪',
    model_gateway_upstream_failed: '模型服务暂时不可用',
    model_gateway_bad_response: '模型服务返回无效结果',
    model_gateway_unavailable: '模型服务暂时不可用',
  };
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify({ error: { message: messages[code] || messages.model_gateway_unavailable, type: 'server_error', code } }));
}

function isReadable(value) {
  return value instanceof Readable || Boolean(value && typeof value.pipe === 'function');
}

function boundedFirstChunkTimeout(value) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 1000 && parsed <= 60000
    ? parsed
    : DEFAULT_FIRST_CHUNK_TIMEOUT_MS;
}

function openUpstreamIterator(upstream) {
  return isReadable(upstream.body) ? upstream.body[Symbol.asyncIterator]() : null;
}

async function nextUpstreamChunk(iterator, timeoutMs) {
  let timer = null;
  try {
    return await Promise.race([
      iterator.next(),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new UpstreamStalledError()), timeoutMs);
      }),
    ]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

// 返回首个分块（null 表示流已空终结）；停顿抛 UpstreamStalledError。
// 首块交还调用方写出，迭代器续用，不丢字节。
async function firstUpstreamChunk(iterator, timeoutMs) {
  if (!iterator) return null;
  const head = await nextUpstreamChunk(iterator, timeoutMs);
  if (head.done) return null;
  return Buffer.isBuffer(head.value) ? head.value : Buffer.from(head.value);
}

function toChunkBuffer(value) {
  return Buffer.isBuffer(value) ? value : Buffer.from(value);
}

function boundedAttempts(value) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 1 && parsed <= 3
    ? parsed
    : DEFAULT_UPSTREAM_ATTEMPTS;
}

function defaultRetryDelay(attempt) {
  const delayMs = attempt === 1 ? 100 : 250;
  return new Promise(resolve => setTimeout(resolve, delayMs));
}

function createModelGateway(options) {
  const source = options || {};
  const getServiceToken = source.getServiceToken || (() => loadServiceToken({
    envValue: process.env.AI_BANK_AGENT_RUNTIME_TOKEN,
    tokenFile: process.env.AI_BANK_AGENT_RUNTIME_TOKEN_FILE,
  }));
  // 模型路由：尊重请求的 model 字段（该模型在 API_ROUTES 有路由且 key 可用时优先），
  // 否则回落 FIXED_MODEL——旧实现把所有请求强制改写为 FIXED_MODEL(qwen3.7-flash)，
  // agent 场景（如 rsi-agent 指定 qwen-plus）会被降级到弱模型，多轮工具调用能力受损
  const requestedModelOf = (input) => {
    const name = input && input.model && API_ROUTES[input.model]
      && typeof API_ROUTES[input.model].getKey === 'function'
      && API_ROUTES[input.model].getKey() ? input.model : null;
    return name || FIXED_MODEL;
  };
  const getModelRoute = source.getModelRoute || ((input) => API_ROUTES[requestedModelOf(input)] || API_ROUTES[FIXED_MODEL]);
  const getFallbackModelRoute = source.getFallbackModelRoute
    || (() => API_ROUTES[FIXED_FALLBACK_MODEL]);
  const requestUpstream = source.requestUpstream || defaultUpstreamRequest;
  const timeoutMs = Number(source.timeoutMs || DEFAULT_TIMEOUT_MS);
  const bodyLimit = Number(source.bodyLimit || DEFAULT_BODY_LIMIT);
  const maxUpstreamAttempts = boundedAttempts(source.maxUpstreamAttempts);
  const retryDelay = source.retryDelay || defaultRetryDelay;
  const firstChunkTimeoutMs = boundedFirstChunkTimeout(
    source.firstChunkTimeoutMs ?? process.env.AI_BANK_MODEL_FIRST_CHUNK_TIMEOUT_MS,
  );

  async function requestRoute(route, model, input) {
    const upstreamKey = route && typeof route.getKey === 'function'
      ? String(route.getKey() || '') : '';
    if (!route || !route.host || !route.path || !upstreamKey) {
      return { configured: false, upstream: null, networkFailure: false };
    }
    const routedInput = { ...input, model };
    const body = Buffer.from(JSON.stringify(routedInput));
    const upstreamOptions = {
      protocol: route.protocol || 'https:',
      hostname: route.host,
      port: route.port,
      path: route.path,
      timeoutMs,
      headers: {
        authorization: 'Bearer ' + upstreamKey,
        'content-type': 'application/json',
        accept: input.stream ? 'text/event-stream' : 'application/json',
        'content-length': body.length,
      },
      body,
    };
    let upstream = null;
    let networkFailure = false;
    for (let attempt = 1; attempt <= maxUpstreamAttempts; attempt += 1) {
      try {
        upstream = await requestUpstream(upstreamOptions);
        networkFailure = false;
      } catch (_) {
        upstream = null;
        networkFailure = true;
        if (attempt < maxUpstreamAttempts) await retryDelay(attempt);
        continue;
      }
      const shouldRetry = RETRYABLE_UPSTREAM_STATUSES.has(upstream.status)
        && attempt < maxUpstreamAttempts;
      if (!shouldRetry) break;
      if (isReadable(upstream.body)) upstream.body.resume();
      await retryDelay(attempt);
    }
    return { configured: true, upstream, networkFailure };
  }

  async function selectUpstream(input) {
    const requestedModel = requestedModelOf(input);
    const primary = await requestRoute(getModelRoute(input), requestedModel, input);
    if (!primary.configured && !getFallbackModelRoute()) {
      throw new ModelGatewayError(503, 'model_gateway_not_ready', 'not ready');
    }
    let selected = primary;
    let selectedModel = requestedModel;
    const primaryUnavailable = !primary.configured || primary.networkFailure
      || primary.upstream && FAILOVER_UPSTREAM_STATUSES.has(primary.upstream.status);
    if (primaryUnavailable) {
      const fallback = await requestRoute(
        getFallbackModelRoute(), FIXED_FALLBACK_MODEL, input,
      );
      if (fallback.configured) {
        if (primary.upstream && isReadable(primary.upstream.body)) {
          primary.upstream.body.resume();
        }
        selected = fallback;
        selectedModel = FIXED_FALLBACK_MODEL;
        console.warn(
          '[ai-native-bank:model-gateway] primary unavailable; fixed fallback selected status=%s',
          primary.upstream ? primary.upstream.status : 'network',
        );
      }
    }
    if (!selected.configured) {
      throw new ModelGatewayError(503, 'model_gateway_not_ready', 'not ready');
    }
    const upstream = selected.upstream;
    if (!upstream || upstream.status < 200 || upstream.status >= 300 || upstream.body == null) {
      if (upstream && isReadable(upstream.body)) upstream.body.resume();
      throw new ModelGatewayError(502, 'model_gateway_upstream_failed', 'upstream failed');
    }
    return { model: selectedModel, upstream };
  }

  function destroyUpstreamBody(upstream) {
    if (upstream && isReadable(upstream.body)) {
      try { upstream.body.destroy(); } catch (_) { /* 已关闭 */ }
    }
  }

  async function selectStalledFallback(input) {
    const fallback = await requestRoute(getFallbackModelRoute(), FIXED_FALLBACK_MODEL, input);
    if (!fallback.configured || !fallback.upstream
      || fallback.upstream.status < 200 || fallback.upstream.status >= 300
      || fallback.upstream.body == null) {
      throw new ModelGatewayError(502, 'model_gateway_upstream_failed', 'upstream failed');
    }
    return { model: FIXED_FALLBACK_MODEL, upstream: fallback.upstream };
  }

  // 主路由流式首块停顿：弃连并强制尝试备用路由（与按状态码的
  // selectUpstream failover 互补，覆盖「200 但流挂起」形态）。
  async function acquireStreamingUpstream(input) {
    let selection = await selectUpstream(input);
    let iterator = openUpstreamIterator(selection.upstream);
    let firstChunk;
    try {
      firstChunk = await firstUpstreamChunk(iterator, firstChunkTimeoutMs);
    } catch (error) {
      if (!(error instanceof UpstreamStalledError)) throw error;
      destroyUpstreamBody(selection.upstream);
      console.warn(
        '[ai-native-bank:model-gateway] primary stream stalled before first chunk'
        + '; fixed fallback selected model=%s',
        selection.model,
      );
      selection = await selectStalledFallback(input);
      iterator = openUpstreamIterator(selection.upstream);
      firstChunk = await firstUpstreamChunk(iterator, firstChunkTimeoutMs);
    }
    return { selection, iterator, firstChunk };
  }

  async function writeUpstreamResponse(res, input, selection, iterator, firstChunk) {
    const { upstream } = selection;
    if (!iterator && !input.stream) {
      try { JSON.parse(Buffer.from(upstream.body).toString('utf8')); }
      catch (_) { throw new ModelGatewayError(502, 'model_gateway_bad_response', 'bad response'); }
    }
    const contentType = String((upstream.headers || {})['content-type'] || '');
    const responseType = input.stream && /^text\/event-stream\b/i.test(contentType)
      ? 'text/event-stream; charset=utf-8'
      : 'application/json; charset=utf-8';
    res.writeHead(200, { 'Content-Type': responseType, 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no' });
    if (!iterator) {
      res.end(upstream.body);
      return;
    }
    res.on('close', () => {
      if (!res.writableEnded) destroyUpstreamBody(upstream);
    });
    if (firstChunk && firstChunk.length) res.write(firstChunk);
    try {
      for (;;) {
        const { done, value } = await iterator.next();
        if (done) break;
        res.write(toChunkBuffer(value));
      }
      res.end();
    } catch (_) {
      if (!res.writableEnded) res.destroy();
    }
  }

  async function handle(req, res) {
    try {
      let serviceToken = '';
      try { serviceToken = getServiceToken(); } catch (_) { serviceToken = ''; }
      if (!verifyBearer(req.headers.authorization, serviceToken)) {
        throw new ModelGatewayError(401, 'model_gateway_unauthorized', 'unauthorized');
      }
      const input = normalizeChatCompletion(await readBoundedJson(req, bodyLimit));
      const { selection, iterator, firstChunk } = await acquireStreamingUpstream(input);
      await writeUpstreamResponse(res, input, selection, iterator, firstChunk);
    } catch (error) {
      if (error instanceof ModelGatewayError && error.code === 'model_gateway_validation_error') {
        console.warn('[ai-native-bank:model-gateway] rejected request:', error.message);
      }
      sendError(res, error);
    }
  }

  async function collectUpstreamText(selection, iterator, firstChunk) {
    const { upstream } = selection;
    if (!iterator) {
      const buffer = Buffer.isBuffer(upstream.body)
        ? upstream.body
        : Buffer.from(String(upstream.body == null ? '' : upstream.body));
      return buffer.toString('utf8');
    }
    const chunks = firstChunk && firstChunk.length ? [firstChunk] : [];
    for (;;) {
      const { done, value } = await iterator.next();
      if (done) break;
      chunks.push(toChunkBuffer(value));
    }
    return Buffer.concat(chunks).toString('utf8');
  }

  async function complete(payload) {
    // 程序化调用（网关内部语义判定用）：非流式，返回解析后的上游 JSON
    const input = normalizeChatCompletion({ ...payload, stream: false });
    let selection = await selectUpstream(input);
    let iterator = openUpstreamIterator(selection.upstream);
    let text;
    try {
      const firstChunk = await firstUpstreamChunk(iterator, firstChunkTimeoutMs);
      text = await collectUpstreamText(selection, iterator, firstChunk);
    } catch (error) {
      if (!(error instanceof UpstreamStalledError)) throw error;
      destroyUpstreamBody(selection.upstream);
      console.warn(
        '[ai-native-bank:model-gateway] primary stream stalled before first chunk'
        + '; fixed fallback selected model=%s',
        selection.model,
      );
      selection = await selectStalledFallback(input);
      iterator = openUpstreamIterator(selection.upstream);
      const fallbackChunk = await firstUpstreamChunk(iterator, firstChunkTimeoutMs);
      text = await collectUpstreamText(selection, iterator, fallbackChunk);
    }
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (_) {
      throw new ModelGatewayError(502, 'model_gateway_bad_response', 'bad response');
    }
    return { model: selection.model, ...parsed };
  }

  return { handle, complete };
}

function createEmbeddingGateway(options) {
  const source = options || {};
  const getServiceToken = source.getServiceToken || (() => loadServiceToken({
    envValue: process.env.AI_BANK_AGENT_RUNTIME_TOKEN,
    tokenFile: process.env.AI_BANK_AGENT_RUNTIME_TOKEN_FILE,
  }));
  const getModelRoute = source.getModelRoute || (() => API_ROUTES[FIXED_EMBEDDING_MODEL]);
  const requestUpstream = source.requestUpstream || defaultUpstreamRequest;
  const timeoutMs = Number(source.timeoutMs || DEFAULT_TIMEOUT_MS);
  const bodyLimit = Number(source.bodyLimit || DEFAULT_EMBEDDING_BODY_LIMIT);
  const responseBodyLimit = Number(source.responseBodyLimit || DEFAULT_EMBEDDING_RESPONSE_LIMIT);
  const maxUpstreamAttempts = boundedAttempts(source.maxUpstreamAttempts);
  const retryDelay = source.retryDelay || defaultRetryDelay;

  async function handle(req, res) {
    try {
      let serviceToken = '';
      try { serviceToken = getServiceToken(); } catch (_) { serviceToken = ''; }
      if (!verifyBearer(req.headers.authorization, serviceToken)) {
        throw new ModelGatewayError(401, 'model_gateway_unauthorized', 'unauthorized');
      }
      const route = getModelRoute();
      const upstreamKey = route && typeof route.getKey === 'function' ? String(route.getKey() || '') : '';
      if (!route || !route.host || !route.path || !upstreamKey) {
        throw new ModelGatewayError(503, 'model_gateway_not_ready', 'not ready');
      }
      const input = normalizeEmbeddingRequest(await readBoundedJson(req, bodyLimit));
      const body = Buffer.from(JSON.stringify(input));
      const upstreamOptions = {
        protocol: route.protocol || 'https:',
        hostname: route.host,
        port: route.port,
        method: 'POST',
        path: route.path,
        timeoutMs,
        headers: {
          authorization: 'Bearer ' + upstreamKey,
          'content-type': 'application/json',
          accept: 'application/json',
          'content-length': body.length,
        },
        body,
      };
      let upstream;
      for (let attempt = 1; attempt <= maxUpstreamAttempts; attempt += 1) {
        try {
          upstream = await requestUpstream(upstreamOptions);
        } catch (_) {
          if (attempt === maxUpstreamAttempts) {
            throw new ModelGatewayError(502, 'model_gateway_upstream_failed', 'upstream failed');
          }
          await retryDelay(attempt);
          continue;
        }
        const shouldRetry = upstream
          && RETRYABLE_UPSTREAM_STATUSES.has(upstream.status)
          && attempt < maxUpstreamAttempts;
        if (!shouldRetry) break;
        if (isReadable(upstream.body)) upstream.body.resume();
        await retryDelay(attempt);
      }
      if (!upstream || upstream.status < 200 || upstream.status >= 300 || upstream.body == null) {
        if (upstream && isReadable(upstream.body)) upstream.body.resume();
        throw new ModelGatewayError(502, 'model_gateway_upstream_failed', 'upstream failed');
      }
      const responseBody = await readBoundedResponseBody(upstream.body, responseBodyLimit);
      let parsed;
      try { parsed = JSON.parse(responseBody.toString('utf8')); }
      catch (_) { throw new ModelGatewayError(502, 'model_gateway_bad_response', 'bad response'); }
      validateEmbeddingResponse(parsed);
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
      res.end(responseBody);
    } catch (error) {
      if (error instanceof ModelGatewayError && error.code === 'model_gateway_validation_error') {
        console.warn('[ai-native-bank:embedding-gateway] rejected request:', error.message);
      }
      sendError(res, error);
    }
  }

  return { handle };
}

module.exports = {
  FIXED_EMBEDDING_MODEL,
  FIXED_MODEL,
  ModelGatewayError,
  createEmbeddingGateway,
  createModelGateway,
  normalizeChatCompletion,
  normalizeEmbeddingRequest,
};
