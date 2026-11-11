'use strict';

const crypto = require('crypto');
const { BankApiError } = require('./api-contract');
const { projectSessionTurn } = require('./agent-session-projection');
const { MAX_AGENT_SESSION_TURN_BYTES, validateFocus } = require('./agent-session-contract');

function createAgentSessionService(options) {
  const opts = options || {};
  const repository = opts.repository;
  const now = opts.now;
  const idleTtlMs = opts.idleTtlMs;
  const absoluteTtlMs = opts.absoluteTtlMs;
  if (!repository || typeof repository !== 'object' || typeof now !== 'function'
    || !Number.isSafeInteger(idleTtlMs) || idleTtlMs <= 0
    || !Number.isSafeInteger(absoluteTtlMs) || absoluteTtlMs <= 0) {
    throw new TypeError('Agent Session 服务配置无效');
  }

  function timestamp() {
    const value = now();
    const milliseconds = value instanceof Date ? value.getTime() : Date.parse(value);
    if (!Number.isFinite(milliseconds)) throw new TypeError('Agent Session 时钟返回值无效');
    return { milliseconds, iso: new Date(milliseconds).toISOString() };
  }

  function requireIdentifier(value, field) {
    const text = typeof value === 'string' ? value.trim() : '';
    if (!/^[A-Za-z0-9_.:-]{1,160}$/.test(text)) {
      throw new BankApiError(400, 'invalid_agent_session', `${field}无效`);
    }
    return text;
  }

  function notFound() {
    return new BankApiError(404, 'agent_session_not_found', 'Agent Session 不存在');
  }

  function terminalError(session) {
    if (session.status === 'expired') {
      return new BankApiError(409, 'agent_session_expired', 'Agent Session 已过期');
    }
    return new BankApiError(409, 'agent_session_closed', 'Agent Session 已关闭');
  }

  function projectSession(session, turns) {
    const result = {
      id: session.id,
      status: session.status,
      createdAt: session.createdAt,
      lastActiveAt: session.lastActiveAt,
      idleExpiresAt: session.idleExpiresAt,
      absoluteExpiresAt: session.absoluteExpiresAt,
      closedAt: session.closedAt,
      closeReason: session.closeReason,
    };
    if (turns) result.turns = turns;
    return Object.freeze(result);
  }

  async function create(customerId) {
    const owner = requireIdentifier(customerId, 'customerId');
    const current = timestamp();
    const created = await repository.createAgentSession({
      id: `conv_${crypto.randomUUID()}`,
      customerId: owner,
      status: 'active',
      createdAt: current.iso,
      lastActiveAt: current.iso,
      idleExpiresAt: new Date(current.milliseconds + idleTtlMs).toISOString(),
      absoluteExpiresAt: new Date(current.milliseconds + absoluteTtlMs).toISOString(),
    });
    if (!created) throw notFound();
    return projectSession(created);
  }

  async function createFocused(customerId, inputFocus) {
    const owner = requireIdentifier(customerId, 'customerId');
    const focus = validateFocus(inputFocus);
    const current = timestamp();
    if (Date.parse(focus.expiresAt) <= current.milliseconds) {
      throw new BankApiError(409, 'agent_session_focus_expired', '推荐上下文已过期');
    }
    const created = await repository.createFocusedAgentSession({
      session: {
        id: `conv_${crypto.randomUUID()}`,
        customerId: owner,
        status: 'active',
        createdAt: current.iso,
        lastActiveAt: current.iso,
        idleExpiresAt: new Date(current.milliseconds + idleTtlMs).toISOString(),
        absoluteExpiresAt: new Date(current.milliseconds + absoluteTtlMs).toISOString(),
      },
      focus,
    });
    if (!created) throw notFound();
    return projectSession(created);
  }

  async function getFocus(sessionId, customerId, knownSession) {
    const session = knownSession || await requireActive(sessionId, customerId);
    const focus = await repository.getAgentSessionFocus(session.id, customerId);
    if (!focus) return null;
    const validated = validateFocus(focus);
    if (Date.parse(validated.expiresAt) <= timestamp().milliseconds) return null;
    return validated;
  }

  async function requireActive(sessionId, customerId) {
    const id = requireIdentifier(sessionId, 'sessionId');
    const owner = requireIdentifier(customerId, 'customerId');
    const session = await repository.requireActiveAgentSession({ id, customerId: owner });
    if (!session) throw notFound();
    if (session.status !== 'active') throw terminalError(session);
    return projectSession(session);
  }

  async function get(sessionId, customerId) {
    const id = requireIdentifier(sessionId, 'sessionId');
    const owner = requireIdentifier(customerId, 'customerId');
    const session = await repository.requireActiveAgentSession({ id, customerId: owner });
    if (!session) throw notFound();
    if (session.status !== 'active') return projectSession(session, Object.freeze([]));
    const storedTurns = await repository.listAgentSessionTurns(session.id, owner);
    const turns = Object.freeze(storedTurns.map(turn => projectSessionTurn({
      role: turn.role,
      text: turn.text,
      response: turn.response,
    })));
    return projectSession(session, turns);
  }

  async function touch(sessionId, customerId) {
    const id = requireIdentifier(sessionId, 'sessionId');
    const owner = requireIdentifier(customerId, 'customerId');
    const touched = await repository.touchAgentSession({
      id,
      customerId: owner,
      idleTtlMs,
    });
    if (!touched) throw notFound();
    if (touched.status !== 'active') throw terminalError(touched);
    return projectSession(touched);
  }

  async function close(sessionId, customerId, reason) {
    const id = requireIdentifier(sessionId, 'sessionId');
    const owner = requireIdentifier(customerId, 'customerId');
    const closeReason = requireIdentifier(reason, 'closeReason');
    const current = timestamp();
    const closed = await repository.closeAgentSession({
      id,
      customerId: owner,
      status: 'closed',
      closedAt: current.iso,
      closeReason,
    });
    if (!closed) throw notFound();
    return projectSession(closed);
  }

  async function appendTurn(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) {
      throw new BankApiError(400, 'invalid_agent_session_turn', 'Session 轮次格式无效');
    }
    const expected = new Set(['sessionId', 'customerId', 'role', 'text', 'response']);
    const required = ['sessionId', 'customerId', 'role', 'text'];
    if (Object.keys(input).some(key => !expected.has(key))
      || required.some(key => !Object.prototype.hasOwnProperty.call(input, key))) {
      throw new BankApiError(400, 'invalid_agent_session_turn', 'Session 轮次包含未知字段');
    }
    const sessionId = requireIdentifier(input.sessionId, 'sessionId');
    const customerId = requireIdentifier(input.customerId, 'customerId');
    const rawTurn = {
      role: input.role,
      text: input.text,
      response: input.response == null ? null : input.response,
    };
    if (Buffer.byteLength(JSON.stringify(rawTurn), 'utf8') > MAX_AGENT_SESSION_TURN_BYTES) {
      throw new BankApiError(413, 'agent_session_turn_too_large', 'Session 轮次超过存储上限');
    }
    const projected = projectSessionTurn({
      role: input.role,
      text: input.text,
      response: input.response,
    });
    if (Buffer.byteLength(JSON.stringify(projected), 'utf8') > MAX_AGENT_SESSION_TURN_BYTES) {
      throw new BankApiError(413, 'agent_session_turn_too_large', 'Session 轮次超过存储上限');
    }
    return repository.appendAgentSessionTurn({
      sessionId,
      customerId,
      role: projected.role,
      text: projected.text,
      response: projected.response,
    });
  }

  return Object.freeze({
    create: customerId => create(customerId),
    createFocused: (customerId, focus) => createFocused(customerId, focus),
    getFocus: (sessionId, customerId, knownSession) => getFocus(sessionId, customerId, knownSession),
    get: (sessionId, customerId) => get(sessionId, customerId),
    requireActive: (sessionId, customerId) => requireActive(sessionId, customerId),
    touch: (sessionId, customerId) => touch(sessionId, customerId),
    close: (sessionId, customerId, reason) => close(sessionId, customerId, reason),
    appendTurn: input => appendTurn(input),
  });
}

module.exports = { createAgentSessionService };
