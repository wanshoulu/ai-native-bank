'use strict';

const { loadServiceToken } = require('./service-token');
const { isSafeAgentSessionText } = require('./agent-session-projection');
const {
  AgentTraceValidationError,
  createTraceState,
  validateTraceEvent,
} = require('./agent-trace');
const {
  isBoundedAmbiguity,
  isGraphDeclaredSemanticSlot,
} = require('./conversation-state-policy');

const MAX_TRACE_FRAME_BYTES = 64 * 1024;
const MAX_TRACE_STREAM_BYTES = 512 * 1024;
const MAX_AGENT_SESSION_ID_LENGTH = 160;
const MAX_AGENT_SESSION_RELEASE_BATCH = 32;
const AGENT_SESSION_RELEASE_TIMEOUT_MS = 5000;
const SAFE_AGENT_SESSION_ID = /^(?:user:[A-Za-z0-9][A-Za-z0-9_.-]{0,95}|business:[A-Za-z0-9][A-Za-z0-9_.-]{0,95}:[A-Za-z0-9][A-Za-z0-9_.-]{0,95})$/;
const USER_RESPONSE_TYPES = new Set(['result', 'clarification', 'answer', 'confirmation', 'navigation', 'notice']);
const USER_RESPONSE_STATUS_TYPES = Object.freeze({
  succeeded: new Set(['result', 'answer']),
  clarification_required: new Set(['clarification']),
  awaiting_confirmation: new Set(['confirmation']),
  navigation_only: new Set(['navigation']),
  unsupported: new Set(['notice']),
  blocked: new Set(['notice']),
  unavailable: new Set(['notice']),
});
const USER_TEXT_INTERNAL_MARKERS = Object.freeze([
  'confirmationtoken', 'tokenhash', 'authorizationtoken', 'accesstoken',
  'refreshtoken', 'systemprompt', '模型原文', 'accountid', 'cardid',
  'payeeid', 'capabilityid', 'intentid', 'taskid', 'stepid', 'agentrunid',
  'businessagentrunid', 'delegationid', 'traceid', 'requestid',
  'conversationid', 'errorcode', 'failedstage', 'risklevel',
  'executionreadiness', 'clarificationrequired', 'needsslots',
  'needsclarification', 'awaitingconfirmation', 'navigationonly',
  'bank', 'acct', 'credit', 'payee',
]);
const IDEOGRAPHIC_DOMAIN_PATTERN = /[\p{L}\p{N}-]+(?:。[\p{L}\p{N}-]+)+/giu;
const COMMON_IDN_TLDS = new Set([
  '中国', '公司', '网络', '网址', '在线', '商店', '中文网', '我爱你', '移动',
  '集团', '世界', '信息', '餐厅', '游戏', '企业', '娱乐', '香港', '台湾', '澳门', '政务',
]);

class AgentRuntimeClientError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = 'AgentRuntimeClientError';
    this.status = status;
    this.code = code;
    this.isOperational = true;
  }
}

function clientError(status, code, message) {
  return new AgentRuntimeClientError(status, code, message);
}

function logTransportFailure(operation, error) {
  const name = error && typeof error.name === 'string' ? error.name : 'Error';
  const code = error && typeof error.code === 'string'
    ? error.code
    : error && error.cause && typeof error.cause.code === 'string'
      ? error.cause.code
      : 'unclassified';
  console.warn(
    '[ai-native-bank:agent-runtime-client] operation=%s name=%s code=%s',
    operation, name, /^[A-Za-z0-9_.-]{1,80}$/.test(code) ? code : 'unclassified',
  );
}

function createAiBankAgentRuntimeClient(options) {
  const source = options || {};
  const configuredFetch = source.fetchImpl;
  const resolveFetch = () => configuredFetch || globalThis.fetch;
  const baseUrl = String(source.baseUrl || process.env.AI_BANK_AGENT_RUNTIME_URL || 'http://ai-native-bank-agent-runtime:18120').replace(/\/+$/, '');
  const tokenProvider = typeof source.tokenProvider === 'function'
    ? source.tokenProvider
    : () => loadServiceToken({
        envValue: source.token == null ? process.env.AI_BANK_AGENT_RUNTIME_TOKEN : source.token,
        tokenFile: source.tokenFile || process.env.AI_BANK_AGENT_RUNTIME_TOKEN_FILE,
      });
  const timeoutMs = positiveTimeout(
    source.timeoutMs == null ? process.env.AI_BANK_AGENT_RUNTIME_TIMEOUT_MS : source.timeoutMs,
    120000,
  );
  const healthTimeoutMs = positiveTimeout(
    source.healthTimeoutMs == null
      ? process.env.AI_BANK_AGENT_RUNTIME_HEALTH_TIMEOUT_MS
      : source.healthTimeoutMs,
    Math.min(timeoutMs, 3000),
  );
  const healthRetryDelayMs = positiveTimeout(source.healthRetryDelayMs, 100);

  if (typeof resolveFetch() !== 'function') {
    throw clientError(503, 'agent_runtime_unavailable', '智能服务运行时不可用');
  }

  async function request(path, requestOptions) {
    const controller = new AbortController();
    const duration = requestOptions && requestOptions.timeoutMs != null
      ? positiveTimeout(requestOptions.timeoutMs, timeoutMs)
      : requestOptions && requestOptions.health ? healthTimeoutMs : timeoutMs;
    const timer = setTimeout(() => controller.abort(), duration);
    try {
      const token = tokenProvider();
      const response = await resolveFetch()(baseUrl + path, {
        method: requestOptions && requestOptions.body ? 'POST' : 'GET',
        headers: {
          accept: 'application/json',
          authorization: 'Bearer ' + token,
          ...(requestOptions && requestOptions.body ? { 'content-type': 'application/json' } : {}),
        },
        body: requestOptions && requestOptions.body ? JSON.stringify(requestOptions.body) : undefined,
        signal: controller.signal,
      });
      let body;
      try {
        body = await response.json();
      } catch (error) {
        if (error && error.name === 'AbortError') throw error;
        throw clientError(502, 'agent_runtime_response_invalid', '智能服务返回结果无法识别');
      }
      if (!response.ok) {
        console.warn(
          '[ai-native-bank:agent-runtime-client] operation=%s upstreamStatus=%s',
          path === '/health' ? 'health' : 'request',
          Number.isInteger(response.status) ? response.status : 'invalid',
        );
        const code = response.status === 400 || response.status === 422
          ? 'agent_runtime_request_invalid'
          : response.status === 503
            ? 'agent_runtime_not_ready'
            : 'agent_runtime_failed';
        const status = response.status >= 400 && response.status <= 599 ? response.status : 502;
        throw clientError(status, code, code === 'agent_runtime_not_ready'
          ? '智能服务尚未就绪' : '智能服务请求失败');
      }
      return body;
    } catch (error) {
      if (error instanceof AgentRuntimeClientError) throw error;
      if (error && error.code && String(error.code).startsWith('service_token_')) {
        logTransportFailure(path === '/health' ? 'health-auth' : 'request-auth', error);
        throw clientError(503, 'agent_runtime_not_ready', '智能服务尚未就绪');
      }
      if (error && error.name === 'AbortError') {
        throw clientError(504, 'agent_runtime_timeout', '智能服务处理超时，请稍后查询或重试');
      }
      logTransportFailure(path === '/health' ? 'health' : 'request', error);
      throw clientError(503, 'agent_runtime_unavailable', '智能服务运行时不可用');
    } finally {
      clearTimeout(timer);
    }

  }

  async function getStatus() {
    let body;
    try {
      body = await request('/health', { health: true });
    } catch (error) {
      if (!error || !['agent_runtime_timeout', 'agent_runtime_unavailable'].includes(error.code)) {
        throw error;
      }
      await new Promise(resolve => setTimeout(resolve, healthRetryDelayMs));
      body = await request('/health', { health: true });
    }
    if (!isStatus(body)) {
      throw clientError(502, 'agent_runtime_response_invalid', '智能服务状态结果缺少必要字段');
    }
    return body;
  }

  async function probeIntentRecall(query) {
    const input = query || {};
    const message = typeof input.message === 'string' ? input.message.trim() : '';
    if (!message || message.length > 200) {
      throw clientError(400, 'agent_runtime_request_invalid', '召回探针消息非法');
    }
    const params = new URLSearchParams({ message });
    if (typeof input.tab === 'string' && input.tab.trim()) params.set('tab', input.tab.trim());
    if (typeof input.menuKey === 'string' && input.menuKey.trim()) params.set('menuKey', input.menuKey.trim());
    const body = await request('/v1/internal/intent-recall?' + params.toString(), { timeoutMs: 15000 });
    if (!body || body.schemaVersion !== '1.0'
      || typeof body.graphVersion !== 'string'
      || typeof body.graphDigest !== 'string'
      || !Array.isArray(body.candidates)) {
      throw clientError(502, 'agent_runtime_response_invalid', '意图召回探针结果缺少必要字段');
    }
    return body;
  }

  async function probeIntentEvaluation(query) {
    const input = query || {};
    const message = typeof input.message === 'string' ? input.message.trim() : '';
    if (!message || message.length > 2000) {
      throw clientError(400, 'agent_runtime_request_invalid', '评测消息非法');
    }
    const payload = { message };
    const page = input.pageContext || {};
    const pageContext = {};
    if (typeof page.tab === 'string' && page.tab.trim()) pageContext.tab = page.tab.trim();
    if (typeof page.menuKey === 'string' && page.menuKey.trim()) pageContext.menuKey = page.menuKey.trim();
    if (pageContext.tab || pageContext.menuKey) payload.pageContext = pageContext;
    if (Array.isArray(input.plannableCapabilityIds)) {
      payload.plannableCapabilityIds = input.plannableCapabilityIds
        .filter((item) => typeof item === 'string' && item).slice(0, 256);
    }
    const body = await request('/v1/internal/intent-evaluate', {
      body: payload,
      timeoutMs: 30000,
    });
    if (!body || body.schemaVersion !== '1.0'
      || typeof body.graphVersion !== 'string'
      || typeof body.graphDigest !== 'string'
      || !body.shape || typeof body.shape.shape !== 'string'
      || !Array.isArray(body.constraints)
      || !Array.isArray(body.faq ? body.faq.matchedIntentIds : null)) {
      throw clientError(502, 'agent_runtime_response_invalid', '意图评测阶梯结果缺少必要字段');
    }
    return body;
  }

  async function createAgentRun(input) {
    const body = await request('/v1/agent-runs', { body: validateEntryGoalInput(input || {}) });
    if (!isRun(body)) {
      throw clientError(502, 'agent_runtime_response_invalid', '智能服务运行结果缺少必要字段');
    }
    const delivery = projectAgentDelivery({
      agentRunId: body.agentRunId,
      status: body.status,
      userResult: body.userResult == null ? null : body.userResult,
      userResponse: body.userResponse == null ? null : body.userResponse,
      userResponses: body.userResponses == null ? null : body.userResponses,
      confirmation: body.confirmation == null ? null : body.confirmation,
      clarificationForm: body.clarificationForm == null ? null : body.clarificationForm,
    }, body.agentRunId);
    const conversationState = body.conversationState == null
      ? null
      : projectConversationState(body.conversationState);
    validateConversationStateDelivery(conversationState, delivery);
    return deepFreeze({ delivery, conversationState });
  }

  async function streamAgentRun(input, onEvent, requestOptions) {
    if (typeof onEvent !== 'function') {
      throw clientError(500, 'agent_runtime_callback_invalid', '智能服务事件处理器无效');
    }
    const payload = validateEntryGoalInput(input || {});
    const externalSignal = requestOptions && requestOptions.signal;
    const controller = new AbortController();
    const abortFromCaller = () => controller.abort();
    if (externalSignal) {
      if (externalSignal.aborted) controller.abort();
      else externalSignal.addEventListener('abort', abortFromCaller, { once: true });
    }
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const token = tokenProvider();
      const response = await resolveFetch()(baseUrl + '/v1/agent-runs', {
        method: 'POST',
        headers: {
          accept: 'text/event-stream',
          authorization: 'Bearer ' + token,
          'content-type': 'application/json',
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      if (!response.ok) {
        console.warn(
          '[ai-native-bank:agent-runtime-client] operation=stream upstreamStatus=%s',
          Number.isInteger(response.status) ? response.status : 'invalid',
        );
        throw responseError(response.status);
      }
      const contentType = response.headers && response.headers.get
        ? String(response.headers.get('content-type') || '').toLowerCase()
        : '';
      if (!contentType.startsWith('text/event-stream') || !response.body) {
        throw new AgentTraceValidationError('Runtime 未返回 SSE');
      }
      const state = createTraceState({
        agentRunId: payload.agentRunId,
        traceId: payload.traceId,
      });
      return await consumeTraceStream(
        response.body,
        state,
        onEvent,
        requestOptions && requestOptions.onResult,
        requestOptions && requestOptions.onState,
      );
    } catch (error) {
      if (error instanceof AgentRuntimeClientError) throw error;
      if (error && error.code && String(error.code).startsWith('service_token_')) {
        logTransportFailure('stream-auth', error);
        throw clientError(503, 'agent_runtime_not_ready', '智能服务尚未就绪');
      }
      if (error && error.name === 'AbortError') {
        if (externalSignal && externalSignal.aborted) {
          throw clientError(499, 'agent_runtime_cancelled', '智能服务请求已取消');
        }
        throw clientError(504, 'agent_runtime_timeout', '智能服务处理超时，请稍后重试');
      }
      if (error instanceof AgentTraceValidationError || error instanceof SyntaxError) {
        console.warn(
          '[ai-native-bank:agent-runtime-client] rejected runtime event stream:',
          error && error.message ? error.message : error.name,
        );
        throw clientError(502, 'agent_runtime_response_invalid', '智能服务事件流无法识别');
      }
      logTransportFailure('stream', error);
      throw clientError(503, 'agent_runtime_unavailable', '智能服务运行时不可用');
    } finally {
      clearTimeout(timer);
      if (externalSignal) externalSignal.removeEventListener('abort', abortFromCaller);
    }
  }

  async function resumeBusinessWorkflow(businessWorkflowId, input) {
    if (typeof businessWorkflowId !== 'string'
      || !/^[A-Za-z0-9_.:-]{1,160}$/.test(businessWorkflowId)) {
      throw clientError(400, 'agent_runtime_request_invalid', 'Workflow 标识不合法');
    }
    const body = await request(
      '/v1/business-workflows/' + encodeURIComponent(businessWorkflowId) + '/resume',
      { body: input || {} },
    );
    if (!isBusinessWorkflowResume(body, businessWorkflowId)) {
      console.warn('[ai-native-bank:agent-runtime-client] rejected workflow response shape', {
        schemaVersion: body && body.schemaVersion,
        status: body && body.status,
        keys: body && typeof body === 'object' ? Object.keys(body).sort() : [],
        receiptShapes: Array.isArray(body && body.receipts)
          ? body.receipts.map(item => Object.keys(item || {}).sort()) : [],
        conversationStateVersion: body && body.conversationState
          ? body.conversationState.schemaVersion : null,
        conversationStateStatus: body && body.conversationState
          ? body.conversationState.status : null,
        conversationStateKeys: body && body.conversationState
          ? Object.keys(body.conversationState).sort() : [],
      });
      throw clientError(502, 'agent_runtime_response_invalid', 'Workflow 恢复结果无法识别');
    }
    return deepFreeze(projectBusinessWorkflowResume(body));
  }

  async function releaseAgentSessions(sessionIds) {
    const normalized = validateAgentSessionReleaseIds(sessionIds);
    const body = await request('/v1/agent-sessions/release', {
      body: { sessionIds: normalized },
      timeoutMs: AGENT_SESSION_RELEASE_TIMEOUT_MS,
    });
    if (!body || typeof body !== 'object' || Array.isArray(body)
      || Object.keys(body).length !== 1 || !Object.hasOwn(body, 'released')
      || !Number.isInteger(body.released) || body.released !== normalized.length) {
      throw clientError(
        502, 'agent_runtime_response_invalid', '智能服务 Session 释放结果无法识别',
      );
    }
    return Object.freeze({ released: body.released });
  }

  return {
    getStatus,
    createAgentRun,
    streamAgentRun,
    resumeBusinessWorkflow,
    releaseAgentSessions,
    probeIntentRecall,
    probeIntentEvaluation,
  };
}

function validateAgentSessionReleaseIds(sessionIds) {
  if (!Array.isArray(sessionIds) || sessionIds.length < 1
    || sessionIds.length > MAX_AGENT_SESSION_RELEASE_BATCH) {
    throw clientError(400, 'agent_runtime_request_invalid', 'Agent Session 释放请求无效');
  }
  const unique = new Set();
  const normalized = [];
  for (const sessionId of sessionIds) {
    if (typeof sessionId !== 'string'
      || sessionId.length > MAX_AGENT_SESSION_ID_LENGTH
      || sessionId.includes('..')
      || !SAFE_AGENT_SESSION_ID.test(sessionId)
      || unique.has(sessionId)) {
      throw clientError(400, 'agent_runtime_request_invalid', 'Agent Session 释放请求无效');
    }
    unique.add(sessionId);
    normalized.push(sessionId);
  }
  return Object.freeze(normalized);
}

function responseError(statusValue) {
  const status = statusValue >= 400 && statusValue <= 599 ? statusValue : 502;
  const code = status === 400 || status === 422
    ? 'agent_runtime_request_invalid'
    : status === 503
      ? 'agent_runtime_not_ready'
      : 'agent_runtime_failed';
  return clientError(status, code, code === 'agent_runtime_not_ready' ? '智能服务尚未就绪' : '智能服务请求失败');
}

async function consumeTraceStream(body, state, onEvent, onResult, onState) {
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let buffer = '';
  let totalBytes = 0;
  let terminal = null;
  let delivery = null;
  let conversationState = null;
  let stateSeen = false;
  for await (const chunk of iterateBody(body)) {
    const bytes = typeof chunk === 'string' ? new TextEncoder().encode(chunk) : chunk;
    if (!(bytes instanceof Uint8Array)) throw new AgentTraceValidationError('SSE 分块非法');
    totalBytes += bytes.byteLength;
    if (totalBytes > MAX_TRACE_STREAM_BYTES) throw new AgentTraceValidationError('SSE 总量过大');
    buffer += decoder.decode(bytes, { stream: true });
    buffer = drainFrames(buffer, state, event => {
      terminal = ['run.completed', 'run.awaiting_confirmation', 'run.failed'].includes(event.stage) ? event : terminal;
      onEvent(event);
    }, item => {
      if (!terminal || !state.terminal) throw new AgentTraceValidationError('SSE 结果必须位于终态 Trace 之后');
      if (delivery) throw new AgentTraceValidationError('SSE 结果重复');
      delivery = item;
    }, onResult, item => {
      if (!terminal || !state.terminal || delivery || stateSeen) {
        throw new AgentTraceValidationError('SSE 私有状态顺序非法');
      }
      stateSeen = true;
      conversationState = item;
    });
    if (Buffer.byteLength(buffer, 'utf8') > MAX_TRACE_FRAME_BYTES) {
      throw new AgentTraceValidationError('SSE 帧过大');
    }
  }
  buffer += decoder.decode();
  buffer = drainFrames(buffer, state, event => {
    terminal = ['run.completed', 'run.awaiting_confirmation', 'run.failed'].includes(event.stage) ? event : terminal;
    onEvent(event);
  }, item => {
    if (!terminal || !state.terminal) throw new AgentTraceValidationError('SSE 结果必须位于终态 Trace 之后');
    if (delivery) throw new AgentTraceValidationError('SSE 结果重复');
    delivery = item;
  }, onResult, item => {
    if (!terminal || !state.terminal || delivery || stateSeen) {
      throw new AgentTraceValidationError('SSE 私有状态顺序非法');
    }
    stateSeen = true;
    conversationState = item;
  });
  if (buffer.trim()) throw new AgentTraceValidationError('SSE 尾帧不完整');
  if (!terminal || !state.terminal) throw new AgentTraceValidationError('SSE 缺少终态');
  if (delivery) {
    validateDeliveryTerminal(delivery, terminal);
    validateConversationStateDelivery(conversationState, delivery);
    if (conversationState && typeof onState === 'function') onState(conversationState);
    onResult(delivery);
  }
  return terminal;
}

async function* iterateBody(body) {
  if (body && typeof body[Symbol.asyncIterator] === 'function') {
    yield* body;
    return;
  }
  if (!body || typeof body.getReader !== 'function') throw new AgentTraceValidationError('SSE 响应体非法');
  const reader = body.getReader();
  try {
    while (true) {
      const item = await reader.read();
      if (item.done) return;
      yield item.value;
    }
  } finally {
    reader.releaseLock();
  }
}

function drainFrames(input, state, onEvent, onDelivery, onResult, onPrivateState) {
  let buffer = input;
  while (true) {
    const match = /\r?\n\r?\n/.exec(buffer);
    if (!match) return buffer;
    const frame = buffer.slice(0, match.index);
    buffer = buffer.slice(match.index + match[0].length);
    if (!frame.trim()) continue;
    if (Buffer.byteLength(frame, 'utf8') > MAX_TRACE_FRAME_BYTES) {
      throw new AgentTraceValidationError('SSE 帧过大');
    }
    const lines = frame.split(/\r?\n/);
    let eventName = null;
    let data = null;
    for (const line of lines) {
      if (line.startsWith('event:')) {
        if (eventName != null) throw new AgentTraceValidationError('SSE event 重复');
        eventName = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        if (data != null) throw new AgentTraceValidationError('SSE data 重复');
        data = line.slice(5).trimStart();
      } else if (line && !line.startsWith(':')) {
        throw new AgentTraceValidationError('SSE 字段非法');
      }
    }
    if (data == null || !['agent.trace', 'agent.state', 'agent.result'].includes(eventName)) throw new AgentTraceValidationError('SSE 事件非法');
    const parsed = JSON.parse(data);
    if (eventName === 'agent.result') {
      if (typeof onResult !== 'function') throw new AgentTraceValidationError('SSE 结果处理器缺失');
      console.info('[ai-native-bank:agent-runtime-client] agent.result delivery', {
        agentRunId: parsed && parsed.agentRunId,
        status: parsed && parsed.status,
        userResponseType: parsed && parsed.userResponse ? parsed.userResponse.type : null,
        userResponseTitle: parsed && parsed.userResponse ? parsed.userResponse.title : null,
        userResponses: Array.isArray(parsed && parsed.userResponses)
          ? parsed.userResponses.map(item => ({ type: item && item.type, title: item && item.title }))
          : null,
      });
      onDelivery(projectAgentDelivery(parsed, state.agentRunId));
    } else if (eventName === 'agent.state') {
      onPrivateState(projectConversationState(parsed));
    } else {
      const event = validateTraceEvent(parsed, state);
      onEvent(event);
    }
  }
}

function validateDeliveryTerminal(delivery, terminal) {
  if (terminal.stage === 'run.failed') {
    if (delivery.status !== 'blocked' || !delivery.userResponse || delivery.userResponse.type !== 'notice'
      || delivery.userResult || delivery.confirmation) {
      throw new AgentTraceValidationError('失败 Trace 与用户结果冲突');
    }
    return;
  }
  if (terminal.stage === 'run.awaiting_confirmation') {
    if (delivery.status !== 'awaiting_confirmation') throw new AgentTraceValidationError('等待确认 Trace 与用户结果冲突');
    return;
  }
  const terminalStatus = terminal.summary && terminal.summary.result && terminal.summary.result.status;
  if (!terminalStatus || terminalStatus === 'awaiting_confirmation' || delivery.status !== terminalStatus) {
    throw new AgentTraceValidationError('完成 Trace 与用户结果冲突');
  }
}

function projectConversationState(value) {
  if (value && value.schemaVersion === '2.0') {
    exactObject(value, [
      'schemaVersion', 'pendingIntentId', 'status', 'slots', 'pendingEnvelopeId',
      'envelopeRevision', 'intentShape', 'goals', 'sharedSlots', 'ambiguities',
    ]);
    const shapes = new Set(['single_intent', 'multi_intent', 'same_intent_multi_task', 'ambiguous_intent', 'non_task']);
    if (value.status !== 'retain' || value.pendingIntentId != null
      || (value.slots != null && (!Array.isArray(value.slots) || value.slots.length !== 0))
      || typeof value.pendingEnvelopeId !== 'string' || !/^[A-Za-z0-9_.:-]{1,96}$/.test(value.pendingEnvelopeId)
      || !Number.isInteger(value.envelopeRevision) || value.envelopeRevision < 1
      || !shapes.has(value.intentShape)
      || !Array.isArray(value.goals) || value.goals.length < 1 || value.goals.length > 16
      || !Array.isArray(value.sharedSlots) || value.sharedSlots.length > 16
      || !Array.isArray(value.ambiguities) || value.ambiguities.length > 8) {
      throw new AgentTraceValidationError('Agent 私有多目标会话状态非法');
    }
    const taskIds = new Set();
    const goals = value.goals.map(goal => {
      exactObject(goal, ['taskId', 'intentId', 'slots', 'status']);
      if (typeof goal.taskId !== 'string' || !/^[A-Za-z0-9_.:-]{1,96}$/.test(goal.taskId)
        || taskIds.has(goal.taskId)
        || typeof goal.intentId !== 'string' || !/^[a-z0-9.-]{1,160}$/.test(goal.intentId)
        || !['provisional', 'needs_slots', 'ready'].includes(goal.status)
        || !Array.isArray(goal.slots) || goal.slots.length > 16) {
        throw new AgentTraceValidationError('Agent 私有业务目标非法');
      }
      taskIds.add(goal.taskId);
      const names = new Set();
      return {
        taskId: goal.taskId, intentId: goal.intentId, status: goal.status,
        slots: goal.slots.map(slot => projectPrivateConversationSlot(
          slot, names, [goal.intentId],
        )),
      };
    });
    const sharedNames = new Set();
    const goalIntentIds = [...new Set(goals.map(goal => goal.intentId))];
    const sharedSlots = value.sharedSlots.map(slot => projectPrivateConversationSlot(
      slot, sharedNames, goalIntentIds,
    ));
    const ambiguities = value.ambiguities.map(item => {
      exactObject(item, ['type', 'taskIds', 'options']);
      if (!Array.isArray(item.taskIds)
        || item.taskIds.length < 2 || item.taskIds.length > 16
        || new Set(item.taskIds).size !== item.taskIds.length
        || item.taskIds.some(id => !taskIds.has(id))
        || !isBoundedAmbiguity(item.type, item.options)) {
        throw new AgentTraceValidationError('Agent 私有业务歧义非法');
      }
      return { type: item.type, taskIds: [...item.taskIds], options: [...item.options] };
    });
    return deepFreeze({
      schemaVersion: '2.0', pendingIntentId: null, slots: [],
      pendingEnvelopeId: value.pendingEnvelopeId, envelopeRevision: value.envelopeRevision,
      intentShape: value.intentShape,
      goals, sharedSlots, ambiguities, status: 'retain',
    });
  }
  // Pydantic's response serializer can include empty/default v2 collection
  // fields inside a v1 clear state when the outer endpoint retains defaults.
  // Accept only their empty/null representation and normalize back to v1.
  exactObject(value, [
    'schemaVersion', 'pendingIntentId', 'status', 'slots',
    'pendingEnvelopeId', 'envelopeRevision', 'intentShape',
    'goals', 'sharedSlots', 'ambiguities',
  ]);
  if (value.schemaVersion !== '1.0' || !['retain', 'clear'].includes(value.status)
    || !Array.isArray(value.slots) || value.slots.length > 16
    || value.pendingEnvelopeId != null || value.envelopeRevision != null
    || value.intentShape != null
    || (value.goals != null && (!Array.isArray(value.goals) || value.goals.length))
    || (value.sharedSlots != null && (!Array.isArray(value.sharedSlots) || value.sharedSlots.length))
    || (value.ambiguities != null && (!Array.isArray(value.ambiguities) || value.ambiguities.length))) {
    throw new AgentTraceValidationError('Agent 私有会话状态非法');
  }
  const names = new Set();
  const pendingIntentIds = typeof value.pendingIntentId === 'string'
    ? [value.pendingIntentId] : [];
  const slots = value.slots.map(slot => projectPrivateConversationSlot(
    slot, names, pendingIntentIds,
  ));
  if (value.status === 'retain') {
    if (!/^[a-z0-9.-]{1,160}$/.test(value.pendingIntentId || '') || slots.length === 0) {
      throw new AgentTraceValidationError('Agent 私有会话状态非法');
    }
  } else if (value.pendingIntentId != null || slots.length !== 0) {
    throw new AgentTraceValidationError('Agent 私有会话状态非法');
  }
  return deepFreeze({
    schemaVersion: '1.0', pendingIntentId: value.pendingIntentId == null
      ? null : value.pendingIntentId,
    status: value.status, slots,
  });
}

function projectPrivateConversationSlot(slot, names, intentIds) {
  exactObject(slot, ['name', 'value', 'source']);
  const allowedSources = new Set(['user_explicit', 'user_selection', 'context', 'intent_default']);
  const scalar = slot.value;
  if (!isGraphDeclaredSemanticSlot(slot.name, intentIds)
    || names.has(slot.name) || !allowedSources.has(slot.source)
    || !['string', 'number', 'boolean'].includes(typeof scalar)
    || (typeof scalar === 'number' && !Number.isFinite(scalar))
    || (typeof scalar === 'string' && (codePointLength(scalar) < 1 || codePointLength(scalar) > 240
      || !isSafeAgentSessionText(scalar)))) {
    console.warn('[ai-native-bank] rejected private conversation slot', {
      slotName: typeof slot.name === 'string' ? slot.name : '<invalid>',
      slotType: typeof scalar,
      source: typeof slot.source === 'string' ? slot.source : '<invalid>',
      intentIds,
    });
    throw new AgentTraceValidationError('Agent 私有会话槽位非法');
  }
  names.add(slot.name);
  return { name: slot.name, value: scalar, source: slot.source };
}

function validateConversationStateDelivery(state, delivery) {
  if (!state) return;
  const resumableStatuses = new Set(['clarification_required', 'awaiting_confirmation']);
  // 2.0 envelope 状态跟随持久化的业务 workflow：交付被阻塞（如账户引用无法解析的 notice）
  // 时 workflow 仍保持 awaiting_clarification 可续跑，retain + blocked 是合法配对，
  // 平台必须把 notice 送达用户而不是当作冲突拒绝。
  const envelopeRetained = state.status === 'retain' && state.pendingEnvelopeId != null;
  if (state.status === 'retain'
    && !resumableStatuses.has(delivery.status)
    && !(envelopeRetained && delivery.status === 'blocked')) {
    throw new AgentTraceValidationError('Agent 私有会话状态与结果冲突');
  }
  if (state.status === 'clear' && delivery.status === 'clarification_required'
    && state.slots.length !== 0) {
    throw new AgentTraceValidationError('Agent 私有会话状态与结果冲突');
  }
}

function projectAgentDelivery(value, expectedAgentRunId) {
  exactObject(value, ['agentRunId', 'status', 'userResult', 'userResponse', 'userResponses', 'confirmation', 'clarificationForm']);
  if (value.agentRunId !== expectedAgentRunId) throw new AgentTraceValidationError('Agent 结果标识不一致');
  const statuses = new Set(['planned', 'succeeded', 'awaiting_confirmation', 'clarification_required', 'navigation_only', 'unsupported', 'blocked', 'unavailable']);
  if (!statuses.has(value.status)) throw new AgentTraceValidationError('Agent 结果状态非法');
  const delivery = {
    agentRunId: value.agentRunId,
    status: value.status,
    userResult: null,
    userResponse: null,
    confirmation: null,
  };
  if (value.userResult != null) delivery.userResult = projectUserResult(value.userResult);
  if (value.userResponse != null) delivery.userResponse = projectUserResponse(value.userResponse);
  if (value.userResponses != null) delivery.userResponses = projectUserResponses(value.userResponses);
  if (value.confirmation != null) delivery.confirmation = projectConfirmation(value.confirmation);
  if (value.clarificationForm != null) delivery.clarificationForm = projectClarificationForm(value.clarificationForm);
  validateAgentDeliveryState(delivery);
  return deepFreeze(delivery);
}

function validateAgentDeliveryState(delivery) {
  if (delivery.userResponse && delivery.userResponses) {
    throw new AgentTraceValidationError('Agent 用户响应载荷冲突');
  }
  const terminalResponse = delivery.userResponses
    ? delivery.userResponses[delivery.userResponses.length - 1]
    : delivery.userResponse;
  if (delivery.status === 'planned') {
    if (delivery.userResult || terminalResponse || delivery.confirmation || delivery.clarificationForm) {
      throw new AgentTraceValidationError('Agent 规划状态不能携带终态结果');
    }
    return;
  }

  if (delivery.status === 'succeeded') {
    if (!delivery.userResult && !terminalResponse) throw new AgentTraceValidationError('Agent 成功状态缺少用户结果');
    if (terminalResponse && !USER_RESPONSE_STATUS_TYPES.succeeded.has(terminalResponse.type)) {
      throw new AgentTraceValidationError('Agent 用户响应状态冲突');
    }
  } else if (delivery.userResult) {
    throw new AgentTraceValidationError('Agent 用户结果状态冲突');
  }

  const expectedTypes = USER_RESPONSE_STATUS_TYPES[delivery.status];
  if (delivery.status !== 'succeeded'
    && (!terminalResponse || !expectedTypes || !expectedTypes.has(terminalResponse.type))) {
    throw new AgentTraceValidationError('Agent 用户响应状态冲突');
  }
  if ((delivery.status === 'awaiting_confirmation') !== Boolean(delivery.confirmation)) throw new AgentTraceValidationError('Agent 确认状态冲突');
  if (delivery.clarificationForm && (
    delivery.status !== 'clarification_required'
    || !terminalResponse
    || terminalResponse.type !== 'clarification'
    || terminalResponse.schemaVersion !== '1.1'
  )) throw new AgentTraceValidationError('Agent 澄清表单状态冲突');
}

function projectUserResponses(value) {
  if (!Array.isArray(value) || value.length < 1 || value.length > 8) {
    throw new AgentTraceValidationError('Agent 用户响应序列非法');
  }
  const projected = value.map(projectNativeResumeUserResponse);
  if (projected.slice(0, -1).some(item => !['result', 'notice'].includes(item.type) || item.actions.length)) {
    throw new AgentTraceValidationError('Agent 用户响应序列前缀非法');
  }
  return projected;
}

function projectUserResponse(value) {
  exactObject(value, ['schemaVersion', 'type', 'title', 'body', 'items', 'actions', 'source', 'taskProgress']);
  if (!['1.0', '1.1'].includes(value.schemaVersion) || !USER_RESPONSE_TYPES.has(value.type)) {
    throw new AgentTraceValidationError('Agent 用户响应类型非法');
  }
  if (!Array.isArray(value.items) || value.items.length > 20
    || !Array.isArray(value.actions) || value.actions.length > 8) {
    throw new AgentTraceValidationError('Agent 用户响应列表非法');
  }

  const actions = value.actions.map(projectUserResponseAction);
  if (new Set(actions.map(action => action.id)).size !== actions.length) {
    throw new AgentTraceValidationError('Agent 用户响应动作标识重复');
  }
  if (value.type === 'clarification' && actions.some(action => action.kind !== 'reply')) {
    throw new AgentTraceValidationError('Agent 澄清响应动作非法');
  }
  if (value.type === 'navigation' && (!actions.length || actions.some(action => action.kind !== 'navigate'))) {
    throw new AgentTraceValidationError('Agent 导航响应动作非法');
  }

  const source = value.source == null ? null : projectUserResponseSource(value.source);
  if ((value.type === 'answer') !== Boolean(source)) {
    throw new AgentTraceValidationError('Agent 知识来源状态冲突');
  }
  const taskProgress = value.taskProgress == null ? null : projectTaskProgress(value.taskProgress);
  if (taskProgress && (value.schemaVersion !== '1.1'
    || !['clarification', 'confirmation', 'result'].includes(value.type))) {
    throw new AgentTraceValidationError('Agent 任务进度状态冲突');
  }
  return {
    schemaVersion: value.schemaVersion,
    type: value.type,
    title: safeDeliveryText(value.title, 80),
    body: safeDeliveryText(value.body, 600),
    items: value.items.map(item => safeDeliveryText(item, 240)),
    actions,
    source,
    ...(taskProgress ? { taskProgress } : {}),
  };
}

function projectTaskProgress(value) {
  exactObject(value, ['mode', 'current', 'total', 'completed', 'failed', 'skipped']);
  const numbers = ['current', 'total', 'completed', 'failed', 'skipped'];
  if (value.mode !== 'sequential'
    || numbers.some(name => !Number.isInteger(value[name]))
    || value.current < 1 || value.total < 1 || value.total > 16
    || value.current > value.total
    || value.completed < 0 || value.failed < 0 || value.skipped < 0
    || value.completed + value.failed + value.skipped >= value.total) {
    throw new AgentTraceValidationError('Agent 任务进度非法');
  }
  return { mode: value.mode, current: value.current, total: value.total,
    completed: value.completed, failed: value.failed, skipped: value.skipped };
}

function projectClarificationForm(value) {
  exactObject(value, ['schemaVersion', 'clarificationId', 'submissionToken', 'expiresAt', 'kind', 'fields']);
  if (value.schemaVersion !== '1.0'
    || !/^acl_[A-Za-z0-9_-]{1,92}$/.test(value.clarificationId || '')
    || typeof value.submissionToken !== 'string' || value.submissionToken.length < 32 || value.submissionToken.length > 256
    || typeof value.expiresAt !== 'string' || !Number.isFinite(Date.parse(value.expiresAt))
    || !['transfer_payee', 'transfer_full'].includes(value.kind)
    || !Array.isArray(value.fields) || value.fields.length < 2 || value.fields.length > 8) {
    throw new AgentTraceValidationError('Agent 澄清表单非法');
  }
  const allowed = new Set(['payeeName', 'accountNo', 'bankName', 'purpose', 'savePayee', 'amount', 'fromAccount']);
  const fields = value.fields.map(field => {
    exactObject(field, ['name', 'label', 'type', 'required', 'value', 'editable', 'options']);
    if (!allowed.has(field.name) || typeof field.required !== 'boolean' || typeof field.editable !== 'boolean') {
      throw new AgentTraceValidationError('Agent 澄清字段非法');
    }
    const expectedTypes = { payeeName: 'text', accountNo: 'bank_account', bankName: 'bank_name', purpose: 'text', savePayee: 'boolean', amount: 'amount', fromAccount: 'select' };
    if (field.type !== expectedTypes[field.name]
      || (field.name === 'savePayee' ? typeof field.value !== 'boolean' : typeof field.value !== 'string')
      || (field.name === 'accountNo' && field.value !== '' && !/^\*[\*\s]*\d{0,4}$/.test(field.value))) {
      throw new AgentTraceValidationError('Agent 澄清字段非法');
    }
    const projected = { name: field.name, label: safeDeliveryText(field.label, 40), type: field.type,
      required: field.required, value: field.value, editable: field.editable };
    if (field.name === 'fromAccount' && Array.isArray(field.options)) {
      projected.options = field.options.map(opt => {
        if (!opt || typeof opt !== 'object'
          || typeof opt.label !== 'string' || opt.label.length < 1 || opt.label.length > 60
          || typeof opt.value !== 'string' || opt.value.length < 1 || opt.value.length > 60) {
          throw new AgentTraceValidationError('Agent 澄清字段选项非法');
        }
        return { label: opt.label, value: opt.value };
      });
    }
    return projected;
  });
  const names = fields.map(field => field.name);
  const requiredFields = value.kind === 'transfer_full'
    ? ['payeeName', 'accountNo', 'amount', 'bankName']
    : ['payeeName', 'accountNo', 'bankName'];
  if (new Set(names).size !== names.length || requiredFields.some(name => !names.includes(name))) {
    throw new AgentTraceValidationError('Agent 澄清字段非法');
  }
  return { schemaVersion: '1.0', clarificationId: value.clarificationId,
    submissionToken: value.submissionToken, expiresAt: value.expiresAt,
    kind: value.kind, fields };
}

function projectUserResponseActionForm(value) {
  if (value == null) return null;
  exactObject(value, ['fields']);
  if (!Array.isArray(value.fields) || value.fields.length < 1 || value.fields.length > 6) {
    throw new AgentTraceValidationError('Agent 动作表单非法');
  }
  return {
    fields: value.fields.map(field => {
      exactObject(field, ['name', 'label', 'type', 'required', 'options', 'placeholder']);
      if (typeof field.name !== 'string' || !/^[a-zA-Z][a-zA-Z0-9_]{0,31}$/.test(field.name)
        || !['text', 'number', 'select'].includes(field.type)
        || typeof field.required !== 'boolean') {
        throw new AgentTraceValidationError('Agent 动作表单字段非法');
      }
      const options = field.options == null ? [] : field.options;
      if (!Array.isArray(options) || options.length > 8
        || options.some(option => typeof option !== 'string' || !option || option.length > 40)
        || (field.type === 'select' ? options.length < 2 : options.length > 0)) {
        throw new AgentTraceValidationError('Agent 动作表单字段非法');
      }
      return {
        name: field.name,
        label: safeDeliveryText(field.label, 40),
        type: field.type,
        required: field.required,
        options,
        placeholder: field.placeholder == null ? null : safeDeliveryText(field.placeholder, 60),
      };
    }),
  };
}

function projectUserResponseAction(value) {
  exactObject(value, ['id', 'label', 'description', 'kind', 'interaction', 'payload', 'form']);
  if (!/^[a-z][a-z0-9-]{0,63}$/.test(value.id)
    || !['reply', 'navigate', 'confirm', 'dismiss'].includes(value.kind)) {
    throw new AgentTraceValidationError('Agent 用户响应动作非法');
  }
  const action = {
    id: value.id,
    label: safeDeliveryText(value.label, 80),
    description: value.description == null ? null : safeDeliveryText(value.description, 160, { allowEmpty: true }),
    kind: value.kind,
    interaction: null,
    payload: null,
  };
  exactObject(value.payload, value.kind === 'reply'
    ? ['message']
    : value.kind === 'navigate'
      ? ['channel', 'featureId', 'actionId']
      : []);
  if (value.kind === 'reply') {
    if (value.interaction != null && !['submit', 'compose'].includes(value.interaction)) {
      throw new AgentTraceValidationError('Agent 用户响应动作交互语义非法');
    }
    action.interaction = value.interaction || 'compose';
    action.payload = { message: safeDeliveryText(value.payload.message, 240) };
    // form 仅在运行时显式声明时输出，保持旧载荷形状稳定。
    const form = projectUserResponseActionForm(Object.hasOwn(value, 'form') ? value.form : null);
    if (form != null) {
      if (action.interaction !== 'compose') {
        throw new AgentTraceValidationError('Agent 动作表单仅限 compose 交互');
      }
      action.form = form;
    }
  } else if (value.kind === 'navigate') {
    if (value.interaction != null) {
      throw new AgentTraceValidationError('Agent 用户响应动作交互语义非法');
    }
    if (!/^[a-z][a-z0-9-]{0,31}$/.test(value.payload.channel)
      || !/^[a-z][a-z0-9-]{0,63}$/.test(value.payload.featureId)
      || !/^[a-z][a-z0-9-]{0,63}$/.test(value.payload.actionId)) {
      throw new AgentTraceValidationError('Agent 用户响应导航目标非法');
    }
    action.payload = {
      channel: value.payload.channel,
      featureId: value.payload.featureId,
      actionId: value.payload.actionId,
    };
  } else {
    if (value.interaction != null) {
      throw new AgentTraceValidationError('Agent 用户响应动作交互语义非法');
    }
    action.payload = {};
  }
  return action;
}

function projectUserResponseSource(value) {
  exactObject(value, ['id', 'version', 'label']);
  if (typeof value.id !== 'string' || value.id.length > 128 || !/^faq\.[a-z0-9.-]+$/.test(value.id)
    || typeof value.version !== 'string' || value.version.length > 32 || !/^\d+\.\d+\.\d+$/.test(value.version)
    || value.label !== 'Mock 业务知识') {
    throw new AgentTraceValidationError('Agent 用户响应知识来源非法');
  }
  return { id: value.id, version: value.version, label: value.label };
}

function longestDigitChain(value) {
  // 账号形态检测：数字组被不超过 2 个非数字字符衔接时视为同一串，返回最长串长度。
  // 收益率/费率等金融文本数字密集但呈短串分布（组间有真实文字），不会被误判。
  let best = 0;
  let current = 0;
  let gap = 0;
  let inDigits = false;
  for (const character of value) {
    if (/\p{Nd}/u.test(character)) {
      current += 1;
      if (current > best) best = current;
      gap = 0;
      inDigits = true;
    } else if (inDigits) {
      gap += 1;
      if (gap > 2) {
        current = 0;
        inDigits = false;
      }
    }
  }
  return best;
}

function safeDeliveryMultiLineText(value, maximum) {
  if (typeof value !== 'string' || !value || codePointLength(value) > maximum) {
    throw new AgentTraceValidationError('Agent 结果文本非法');
  }
  const canonical = value.normalize('NFKC').toLowerCase();
  const forms = decodedSecurityForms(canonical);
  if (forms.some(candidate => /[<>\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(candidate))) throw new AgentTraceValidationError('Agent 结果文本非法');
  if (containsUrlLikeText(canonical)) throw new AgentTraceValidationError('Agent 结果文本包含任意链接');
  if (forms.some(candidate => {
    const compact = candidate.replace(/[\p{P}\p{Z}\p{S}\p{M}\p{C}]/gu, '');
    return USER_TEXT_INTERNAL_MARKERS.some(marker => compact.includes(marker));
  })) {
    throw new AgentTraceValidationError('Agent 结果文本包含内部字段');
  }
  if (forms.some(candidate => {
    const compact = candidate.replace(/[\p{P}\p{Z}\p{S}\p{M}\p{C}]/gu, '');
    return longestDigitChain(compact) >= 15;
  })) {
    throw new AgentTraceValidationError('Agent 结果文本包含未脱敏账号');
  }
  return value;
}

function projectUserFacingItemMeta(value) {
  exactObject(value, ['label', 'value']);
  return {
    label: safeDeliveryText(value.label, 40),
    value: safeDeliveryText(value.value, 120),
  };
}

function projectUserFacingItemAction(value) {
  exactObject(value, ['label', 'message', 'detail', 'interaction', 'form']);
  const form = projectUserResponseActionForm(value.form == null ? null : value.form);
  if (form != null && value.interaction !== 'compose') {
    throw new AgentTraceValidationError('Agent 表单仅限 compose 交互');
  }
  return {
    label: safeDeliveryText(value.label, 40),
    message: value.message == null ? null : safeDeliveryText(value.message, 240),
    detail: value.detail == null ? null : safeDeliveryMultiLineText(value.detail, 2000),
    // 旧快照无该字段时按确定性提交处理；compose 仅由运行时合同显式声明。
    interaction: value.interaction === 'compose' ? 'compose' : 'submit',
    form,
  };
}

function projectUserFacingItemCard(value) {
  exactObject(value, ['id', 'title', 'subtitle', 'metas', 'action']);
  return {
    id: value.id == null ? null : safeDeliveryText(value.id, 80),
    title: safeDeliveryText(value.title, 120),
    subtitle: value.subtitle == null ? null : safeDeliveryText(value.subtitle, 80),
    metas: !Array.isArray(value.metas) ? [] : value.metas.slice(0, 8).map(projectUserFacingItemMeta),
    action: value.action == null ? null : projectUserFacingItemAction(value.action),
  };
}

function projectUserFacingPlanAllocation(value) {
  exactObject(value, [
    'productId', 'productName', 'productCategory', 'productCategoryLabel',
    'portfolioRole', 'portfolioRoleLabel', 'amountMinor', 'amount',
    'ratioBasisPoints', 'riskLevel', 'liquidity', 'suitability', 'rateNote',
    'detail', 'productActionLabel',
  ]);
  if (typeof value.productId !== 'string' || !/^prod_[A-Za-z0-9_-]{1,63}$/.test(value.productId)
    || !Number.isInteger(value.amountMinor) || value.amountMinor < 100
    || !Number.isInteger(value.ratioBasisPoints)
    || value.ratioBasisPoints < 1 || value.ratioBasisPoints > 10000) {
    throw new AgentTraceValidationError('财富方案配置非法');
  }
  return {
    productId: value.productId,
    productName: safeDeliveryText(value.productName, 120),
    productCategory: safeDeliveryText(value.productCategory, 40),
    productCategoryLabel: safeDeliveryText(value.productCategoryLabel, 40),
    portfolioRole: safeDeliveryText(value.portfolioRole, 40),
    portfolioRoleLabel: safeDeliveryText(value.portfolioRoleLabel, 40),
    amountMinor: value.amountMinor,
    amount: safeDeliveryText(value.amount, 40),
    ratioBasisPoints: value.ratioBasisPoints,
    riskLevel: safeDeliveryText(value.riskLevel, 20),
    liquidity: safeDeliveryText(value.liquidity, 240),
    suitability: safeDeliveryText(value.suitability, 240),
    rateNote: value.rateNote == null ? null : safeDeliveryText(value.rateNote, 400),
    detail: value.detail == null ? null : safeDeliveryMultiLineText(value.detail, 2000),
    productActionLabel: safeDeliveryText(value.productActionLabel, 40),
  };
}

function projectUserFacingPlan(value) {
  exactObject(value, [
    'id', 'title', 'summary', 'totalAmountMinor', 'totalAmount', 'isPrimary',
    'allocations', 'rationale', 'selectLabel', 'selectMessage',
  ]);
  if (typeof value.id !== 'string' || !/^plan_[A-Za-z0-9_-]{1,63}$/.test(value.id)
    || !Number.isInteger(value.totalAmountMinor) || value.totalAmountMinor < 100
    || typeof value.isPrimary !== 'boolean'
    || !Array.isArray(value.allocations) || value.allocations.length < 1 || value.allocations.length > 5
    || !Array.isArray(value.rationale) || value.rationale.length > 8) {
    throw new AgentTraceValidationError('财富方案非法');
  }
  const allocations = value.allocations.map(projectUserFacingPlanAllocation);
  if (new Set(allocations.map(item => item.productId)).size !== allocations.length
    || allocations.reduce((sum, item) => sum + item.amountMinor, 0) !== value.totalAmountMinor
    || allocations.reduce((sum, item) => sum + item.ratioBasisPoints, 0) !== 10000) {
    throw new AgentTraceValidationError('财富方案金额或比例非法');
  }
  return {
    id: value.id,
    title: safeDeliveryText(value.title, 80),
    summary: safeDeliveryText(value.summary, 240),
    totalAmountMinor: value.totalAmountMinor,
    totalAmount: safeDeliveryText(value.totalAmount, 40),
    isPrimary: value.isPrimary,
    allocations,
    rationale: value.rationale.map(item => safeDeliveryText(item, 240)),
    selectLabel: safeDeliveryText(value.selectLabel, 40),
    selectMessage: safeDeliveryText(value.selectMessage, 240),
  };
}

function projectUserResult(value) {
  exactObject(value, ['kind', 'title', 'summary', 'items', 'itemCards', 'plans', 'primaryAction', 'actions']);
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(value.kind) || !Array.isArray(value.items) || value.items.length > 20) throw new AgentTraceValidationError('用户结果非法');
  const plans = !Array.isArray(value.plans) ? [] : value.plans.slice(0, 3).map(projectUserFacingPlan);
  if (plans.length && plans.filter(plan => plan.isPrimary).length !== 1) {
    throw new AgentTraceValidationError('财富方案主方案非法');
  }
  const result = {
    kind: value.kind,
    title: safeDeliveryText(value.title, 80),
    summary: safeDeliveryText(value.summary, 240),
    items: value.items.map(item => safeDeliveryText(item, 240)),
    itemCards: !Array.isArray(value.itemCards) ? [] : value.itemCards.slice(0, 20).map(projectUserFacingItemCard),
    plans,
    primaryAction: value.primaryAction == null ? null : safeDeliveryText(value.primaryAction, 80),
    actions: !Array.isArray(value.actions) ? [] : value.actions.slice(0, 6).map(projectUserFacingItemAction),
  };
  return result;
}

function projectConfirmation(value) {
  exactObject(value, ['checkpointId', 'capabilityId', 'confirmationToken', 'expiresAt', 'summary', 'confirmations']);
  exactObject(value.summary, ['title', 'description', 'riskLevel', 'items', 'taskCount', 'totalAmountMinor', 'transferDetails', 'fromAccount']);
  const taskCount = value.summary.taskCount == null ? 1 : value.summary.taskCount;
  const summaryItems = value.summary.items == null ? [] : value.summary.items;
  const confirmationsValue = value.confirmations == null ? [] : value.confirmations;
  if (typeof value.checkpointId !== 'string' || value.checkpointId.length > 96 || !/^acp_[A-Za-z0-9_-]+$/.test(value.checkpointId)
    || typeof value.capabilityId !== 'string' || value.capabilityId.length > 128 || !/^bank\.[a-z0-9.-]+$/.test(value.capabilityId)
    || typeof value.confirmationToken !== 'string' || value.confirmationToken.length < 32 || value.confirmationToken.length > 256
    || typeof value.expiresAt !== 'string' || codePointLength(value.expiresAt) < 20 || codePointLength(value.expiresAt) > 40
    || !Number.isFinite(Date.parse(value.expiresAt)) || !['L0', 'L1', 'L2', 'L3'].includes(value.summary.riskLevel)
    || !Number.isInteger(taskCount) || taskCount < 1 || taskCount > 16
    || !Array.isArray(summaryItems) || summaryItems.length > 16
    || (taskCount > 1 && summaryItems.length !== taskCount)
    || (value.summary.totalAmountMinor != null && (!Number.isInteger(value.summary.totalAmountMinor) || value.summary.totalAmountMinor <= 0 || value.summary.totalAmountMinor > 800000000))
    || !Array.isArray(confirmationsValue) || confirmationsValue.length > 16) {
    throw new AgentTraceValidationError('确认视图非法');
  }
  const confirmations = confirmationsValue.map(item => {
    exactObject(item, ['checkpointId', 'confirmationToken']);
    if (!/^acp_[A-Za-z0-9_-]{1,92}$/.test(item.checkpointId || '')
      || typeof item.confirmationToken !== 'string' || item.confirmationToken.length < 32 || item.confirmationToken.length > 256) {
      throw new AgentTraceValidationError('确认视图非法');
    }
    return { checkpointId: item.checkpointId, confirmationToken: item.confirmationToken };
  });
  if ((confirmations.length && confirmations.length !== taskCount)
    || (!confirmations.length && taskCount !== 1)
    || (confirmations.length && (confirmations[0].checkpointId !== value.checkpointId
      || confirmations[0].confirmationToken !== value.confirmationToken))
    || new Set(confirmations.map(item => item.checkpointId)).size !== confirmations.length) {
    throw new AgentTraceValidationError('确认视图非法');
  }
  return {
    checkpointId: value.checkpointId, capabilityId: value.capabilityId,
    confirmationToken: value.confirmationToken, expiresAt: value.expiresAt,
    ...(confirmations.length ? { confirmations } : {}),
    summary: {
      title: safeDeliveryText(value.summary.title, 80),
      description: safeDeliveryText(value.summary.description, 200),
      riskLevel: value.summary.riskLevel,
      ...(value.summary.items == null ? {} : {
        items: summaryItems.map(item => safeDeliveryText(item, 240)),
      }),
      ...(value.summary.taskCount == null ? {} : { taskCount }),
      ...(value.summary.totalAmountMinor == null ? {} : {
        totalAmountMinor: value.summary.totalAmountMinor,
      }),
      // transferDetails 是批量转账逐笔执行回执的唯一数据源：自动确认
      // 循环靠它把每笔（收款人/尾号/金额）带回前端渲染，丢失会导致
      // 用户看不到第二笔及以后的执行明细。
      ...(Array.isArray(value.summary.transferDetails) ? {
        transferDetails: value.summary.transferDetails.map(item => ({
          sequence: item.sequence,
          payeeName: item.payeeName,
          payeeBank: item.payeeBank == null ? null : item.payeeBank,
          payeeTail: item.payeeTail == null ? null : item.payeeTail,
          amount: item.amount,
          amountMinor: item.amountMinor,
        })),
      } : {}),
      // fromAccount（付款账户）与 transferDetails 同为前端渲染提示，
      // 丢失会让确认卡退化成纯文本 description，与任务卡风格不一致。
      ...(value.summary.fromAccount == null ? {} : {
        fromAccount: {
          name: value.summary.fromAccount.name,
          tail: value.summary.fromAccount.tail == null ? null : value.summary.fromAccount.tail,
        },
      }),
    },
  };
}

function exactObject(value, allowed) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new AgentTraceValidationError('Agent 结果对象非法');
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) throw new AgentTraceValidationError('Agent 结果对象原型非法');
  const unknownKeys = Object.keys(value).filter(key => !allowed.includes(key));
  if (unknownKeys.length) throw new AgentTraceValidationError('Agent 结果包含未知字段: ' + unknownKeys.join(',') + ' (allowed: ' + allowed.join(',') + ')');
}

function safeDeliveryText(value, maximum, options) {
  const allowEmpty = Boolean(options && options.allowEmpty);
  if (typeof value !== 'string' || (!allowEmpty && !value) || codePointLength(value) > maximum) {
    throw new AgentTraceValidationError('Agent 结果文本非法');
  }
  const canonical = value.normalize('NFKC').toLowerCase();
  const forms = decodedSecurityForms(canonical);
  if (forms.some(candidate => /[<>\p{C}]/u.test(candidate))) throw new AgentTraceValidationError('Agent 结果文本非法');
  if (containsUrlLikeText(canonical)) throw new AgentTraceValidationError('Agent 结果文本包含任意链接');
  if (forms.some(candidate => {
    const compact = candidate.replace(/[\p{P}\p{Z}\p{S}\p{M}\p{C}]/gu, '');
    return USER_TEXT_INTERNAL_MARKERS.some(marker => compact.includes(marker));
  })) {
    throw new AgentTraceValidationError('Agent 结果文本包含内部字段');
  }
  if (forms.some(candidate => {
    const compact = candidate.replace(/[\p{P}\p{Z}\p{S}\p{M}\p{C}]/gu, '');
    return longestDigitChain(compact) >= 15;
  })) {
    throw new AgentTraceValidationError('Agent 结果文本包含未脱敏账号');
  }
  return value;
}

function containsUrlLikeText(value) {
  for (const candidate of decodedSecurityForms(value)) {
    if (/[a-z][a-z0-9+.-]{0,31}:/iu.test(candidate)
      || /\/\//u.test(candidate)
      || /(?:www\.)/iu.test(candidate)
      || /(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?/u.test(candidate)
      || /\[[0-9a-f:.]+\](?::\d{1,5})?/iu.test(candidate)
      || /localhost(?::\d{1,5})?/iu.test(candidate)
      || /[\p{L}\p{N}._%+-]+@(?:[\p{L}\p{N}-]+\.)+[\p{L}]{2,63}/iu.test(candidate)
      || /[\p{L}\p{N}](?:[\p{L}\p{N}-]{0,62}\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})(?::\d{1,5})?(?:[/?#][^\s]*)?/iu.test(candidate)
      || containsIdeographicBareDomain(candidate)) {
      return true;
    }
  }
  return false;
}

function containsIdeographicBareDomain(value) {
  IDEOGRAPHIC_DOMAIN_PATTERN.lastIndex = 0;
  let match;
  while ((match = IDEOGRAPHIC_DOMAIN_PATTERN.exec(value)) !== null) {
    const before = match.index > 0 ? value[match.index - 1] : '';
    const after = value[match.index + match[0].length] || '';
    if (before === '。' || after === '。') continue;
    const labels = match[0].split('。');
    const tld = labels[labels.length - 1];
    if (/[a-z0-9]/iu.test(match[0]) || COMMON_IDN_TLDS.has(tld)) return true;
  }
  return false;
}

function decodedSecurityForms(value) {
  const forms = [];
  let current = String(value).normalize('NFKC').toLowerCase().replace(/[\uff0e\uff61]/gu, '.');
  for (let pass = 0; pass < 4; pass += 1) {
    if (!forms.includes(current)) forms.push(current);
    try {
      const decoded = decodeURIComponent(current).normalize('NFKC').toLowerCase().replace(/[\uff0e\uff61]/gu, '.');
      if (decoded === current) break;
      current = decoded;
    } catch (_) {
      break;
    }
  }
  return forms;
}

function codePointLength(value) {
  return [...value].length;
}

function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

function positiveTimeout(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function validateEntryGoalInput(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    throw clientError(422, 'agent_runtime_request_invalid', 'Agent Runtime request is invalid');
  }
  if (!Object.prototype.hasOwnProperty.call(input, 'entryGoal')) return input;
  const goal = input.entryGoal;
  const requiredKeys = [
    'goalId', 'scenarioId', 'scenarioVersion', 'recommendationId',
    'targetIntentId', 'mode', 'contextRefs', 'workflowTemplateId',
  ];
  const allowedKeys = new Set([...requiredKeys, 'trustedInputs']);
  const safeId = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value);
  if (!goal || typeof goal !== 'object' || Array.isArray(goal)
    || requiredKeys.some(key => !Object.prototype.hasOwnProperty.call(goal, key))
    || Object.keys(goal).some(key => !allowedKeys.has(key))
    || !safeId(goal.goalId) || !safeId(goal.scenarioId)
    || typeof goal.scenarioVersion !== 'string' || !/^\d+\.\d+\.\d+$/.test(goal.scenarioVersion)
    || !safeId(goal.recommendationId) || !safeId(goal.targetIntentId)
    || goal.mode !== 'guided'
    || !Array.isArray(goal.contextRefs) || goal.contextRefs.length > 32
    || goal.contextRefs.some(ref => !safeId(ref))
    || !validTrustedInputs(goal.trustedInputs)
    || !safeId(goal.workflowTemplateId)) {
    throw clientError(422, 'agent_runtime_request_invalid', 'Guided goal envelope is invalid');
  }
  return input;
}

function validTrustedInputs(value) {
  if (value === undefined) return true;
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).length > 16) return false;
  return Object.entries(value).every(([key, item]) => (
    /^[a-z][a-zA-Z0-9_]{0,63}$/.test(key)
    && (
      typeof item === 'boolean'
      || (typeof item === 'number' && Number.isFinite(item))
      || (typeof item === 'string' && item.length > 0 && item.length <= 160 && !/[<>]/.test(item))
    )
  ));
}

function isStatus(value) {
  return Boolean(value && typeof value === 'object'
    && value.provider === 'openjiuwen'
    && typeof value.version === 'string'
    && typeof value.ready === 'boolean'
    && typeof value.modelConfigured === 'boolean'
    && Array.isArray(value.agents));
}

function isRun(value) {
  if (!(value && typeof value === 'object'
    && typeof value.agentRunId === 'string'
    && typeof value.conversationId === 'string'
    && typeof value.status === 'string')) return false;
  if (['blocked', 'unavailable', 'clarification_required'].includes(value.status)
    && value.intentTaskEnvelope == null && value.capabilityPlan == null) return true;
  return Boolean(value.intentTaskEnvelope && typeof value.intentTaskEnvelope === 'object'
    && value.capabilityPlan && typeof value.capabilityPlan === 'object');
}

function projectNativeResumeUserResponse(value) {
  exactObject(value, [
    'schemaVersion', 'type', 'title', 'body', 'items', 'actions', 'source', 'taskProgress',
  ]);
  const actions = Object.hasOwn(value, 'actions') ? value.actions : [];
  if (!Array.isArray(actions)) return projectUserResponse(value);
  const normalizedActions = actions.map(action => {
    // 与 projectUserResponseAction 的字段白名单保持一致：reply 动作携带 interaction
    // （submit/compose）是运行时契约的一部分，漏掉会在内层校验前被这里误判为未知字段。
    exactObject(action, ['id', 'label', 'description', 'kind', 'interaction', 'payload', 'form']);
    if (!Object.hasOwn(action, 'payload') && ['confirm', 'dismiss'].includes(action.kind)) {
      return Object.assign({}, action, { payload: {} });
    }
    return action;
  });
  return projectUserResponse(Object.assign({}, value, {
    items: Object.hasOwn(value, 'items') ? value.items : [],
    actions: normalizedActions,
  }));
}

function projectWorkflowReceipt(value) {
  exactObject(value, [
    'grantId', 'taskId', 'capabilityId', 'status', 'outputKeys', 'errorCode',
  ]);
  const outputKeys = Object.hasOwn(value, 'outputKeys') ? value.outputKeys : [];
  const errorCode = Object.hasOwn(value, 'errorCode') ? value.errorCode : null;
  if (!/^acg_[A-Za-z0-9_-]{1,96}$/.test(value.grantId || '')
    || !/^[A-Za-z0-9_.:-]{1,96}$/.test(value.taskId || '')
    || !/^bank\.[a-z0-9.-]{1,123}$/.test(value.capabilityId || '')
    || !['SUCCEEDED', 'FAILED'].includes(value.status)
    || !Array.isArray(outputKeys) || outputKeys.length > 32
    || outputKeys.some(key => typeof key !== 'string' || !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(key))
    || (errorCode != null && (typeof errorCode !== 'string' || !/^[a-z0-9_]{1,96}$/.test(errorCode)))) {
    throw new AgentTraceValidationError('Workflow 执行回执非法');
  }
  return {
    grantId: value.grantId,
    taskId: value.taskId,
    capabilityId: value.capabilityId,
    status: value.status,
    outputKeys: [...outputKeys],
    errorCode,
  };
}

function projectBusinessWorkflowResume(value) {
  if (value.schemaVersion === '1.0') {
    return {
      schemaVersion: '1.0',
      agentRunId: value.agentRunId,
      businessWorkflowId: value.businessWorkflowId,
      status: value.status,
      receipts: value.receipts.map(projectWorkflowReceipt),
    };
  }
  return {
    schemaVersion: '2.0',
    agentRunId: value.agentRunId,
    businessWorkflowId: value.businessWorkflowId,
    status: value.status,
    receipts: value.receipts.map(projectWorkflowReceipt),
    workflowRevision: value.workflowRevision,
    activeGoalTaskId: value.activeGoalTaskId == null ? null : value.activeGoalTaskId,
    taskProgress: value.taskProgress == null ? null : projectTaskProgress(value.taskProgress),
    userResponse: value.userResponse == null ? null : projectNativeResumeUserResponse(value.userResponse),
    userResponses: value.userResponses == null ? null : projectUserResponses(value.userResponses),
    userResult: value.userResult == null ? null : projectUserResult(value.userResult),
    confirmation: value.confirmation == null ? null : projectConfirmation(value.confirmation),
    clarificationForm: value.clarificationForm == null ? null : projectClarificationForm(value.clarificationForm),
    conversationState: value.conversationState == null
      ? null : projectConversationState(value.conversationState),
  };
}

function isBusinessWorkflowResume(value, expectedWorkflowId) {
  try {
    if (value && value.schemaVersion === '2.0') {
      exactObject(value, [
        'schemaVersion', 'agentRunId', 'businessWorkflowId', 'status', 'receipts',
        'workflowRevision', 'activeGoalTaskId', 'taskProgress', 'userResponse',
        'userResponses', 'userResult', 'confirmation', 'clarificationForm', 'conversationState',
      ]);
      if (typeof value.agentRunId !== 'string' || value.agentRunId.length < 1 || value.agentRunId.length > 96
        || value.businessWorkflowId !== expectedWorkflowId
        || !['succeeded', 'failed', 'awaiting_confirmation', 'clarification_required'].includes(value.status)
        || !Number.isInteger(value.workflowRevision) || value.workflowRevision < 1
        || !Array.isArray(value.receipts) || value.receipts.length > 16) return false;
      const grantIds = new Set();
      for (const receipt of value.receipts) {
        const projected = projectWorkflowReceipt(receipt);
        if (grantIds.has(projected.grantId)) return false;
        grantIds.add(projected.grantId);
      }
      if (value.status === 'awaiting_confirmation' || value.status === 'clarification_required') {
        if (!/^[A-Za-z0-9_.:-]{1,96}$/.test(value.activeGoalTaskId || '')) return false;
        projectTaskProgress(value.taskProgress);
        if ((value.userResponse == null) === (value.userResponses == null)) return false;
        const terminalResponse = value.userResponses == null
          ? projectNativeResumeUserResponse(value.userResponse)
          : projectUserResponses(value.userResponses).at(-1);
        if (value.status === 'clarification_required' && terminalResponse.type !== 'clarification') return false;
        if (value.status === 'awaiting_confirmation' && terminalResponse.type !== 'confirmation') return false;
      }
      if (value.status === 'awaiting_confirmation') {
        if (!value.confirmation
          || (Object.hasOwn(value.confirmation, 'confirmations')
            && (!Array.isArray(value.confirmation.confirmations)
              || value.confirmation.confirmations.length))) return false;
        projectConfirmation(value.confirmation);
      } else if (value.confirmation != null) return false;
      if (value.clarificationForm != null) {
        if (value.status !== 'clarification_required') return false;
        projectClarificationForm(value.clarificationForm);
      }
      if (value.conversationState == null) return false;
      const conversationState = projectConversationState(value.conversationState);
      const resumable = value.status === 'awaiting_confirmation'
        || value.status === 'clarification_required';
      if ((conversationState.status === 'retain') !== resumable) return false;
      if (value.userResult != null) {
        if (value.status !== 'succeeded') return false;
        projectUserResult(value.userResult);
      }
      return true;
    }
    exactObject(value, [
      'schemaVersion', 'agentRunId', 'businessWorkflowId', 'status', 'receipts',
    ]);
    if (value.schemaVersion !== '1.0'
      || typeof value.agentRunId !== 'string' || value.agentRunId.length < 1 || value.agentRunId.length > 96
      || value.businessWorkflowId !== expectedWorkflowId
      || !['succeeded', 'partial_success', 'failed'].includes(value.status)
      || !Array.isArray(value.receipts) || value.receipts.length < 1 || value.receipts.length > 16) return false;
    const grantIds = new Set();
    for (const receipt of value.receipts) {
      const projected = projectWorkflowReceipt(receipt);
      if (grantIds.has(projected.grantId)) return false;
      grantIds.add(projected.grantId);
    }
    return true;
  } catch (error) {
    return false;
  }
}

module.exports = { AgentRuntimeClientError, createAiBankAgentRuntimeClient };
