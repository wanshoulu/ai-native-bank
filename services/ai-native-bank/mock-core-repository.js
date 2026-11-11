'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const initSqlJs = require('sql.js');
const { BankApiError } = require('./api-contract');
const { isSafeAgentSessionText, projectSessionTurn } = require('./agent-session-projection');
const { MAX_AGENT_SESSION_TURN_BYTES } = require('./agent-session-contract');
const {
  MAX_RESUMABLE_WORKFLOW_CHOICES,
  projectResumableWorkflowSummary,
  selectResumableWorkflowRecords,
} = require('./conversation-control');
const { SEED } = require('./seed-data');
const { WEALTH_SEED } = require('./seed-wealth-data');
const { LIFE_SEED } = require('./seed-life-data');
const MOCK_PERSONAS = require('../../data/ai-native-bank/mock-personas.v1.json');
const {
  isBoundedAmbiguity,
  isGraphDeclaredSemanticSlot,
} = require('./conversation-state-policy');

const DEFAULT_DB_PATH = path.join(__dirname, '..', '..', 'data', 'ai-native-bank', 'mock-core.db');
const ATOMIC_IDEMPOTENCY_OPERATIONS = Object.freeze({
  close: 'agent-session.close',
  workflowBinding: 'agent-session.workflow-binding',
});

const AGENT_RUN_TRANSITIONS = Object.freeze({
  accepted: Object.freeze(['interpreting', 'failed']),
  interpreting: Object.freeze(['planning', 'clarification_required', 'navigation_only', 'blocked', 'failed', 'succeeded']),
  planning: Object.freeze(['executing', 'awaiting_confirmation', 'clarification_required', 'navigation_only', 'blocked', 'failed']),
  awaiting_confirmation: Object.freeze(['executing', 'clarification_required', 'failed']),
  executing: Object.freeze(['succeeded', 'failed']),
  succeeded: Object.freeze([]),
  failed: Object.freeze([]),
  clarification_required: Object.freeze(['awaiting_confirmation', 'executing', 'failed']),
  navigation_only: Object.freeze([]),
  blocked: Object.freeze([]),
});

const AGENT_RUN_TRACE_CAPTURE_STATUSES = Object.freeze([
  'recording',
  'complete',
  'incomplete',
  'legacy_unavailable',
]);

const NEXT_ACTION_TRANSITIONS = Object.freeze({
  generated: Object.freeze(['shown', 'accepted', 'dismissed', 'snoozed', 'expired']),
  shown: Object.freeze(['accepted', 'dismissed', 'snoozed', 'expired']),
  accepted: Object.freeze(['started', 'expired']),
  started: Object.freeze(['completed', 'accepted']),
  dismissed: Object.freeze([]),
  snoozed: Object.freeze([]),
  expired: Object.freeze([]),
  completed: Object.freeze([]),
});
const NEXT_ACTION_DECISION_STATUS = Object.freeze({
  accept: 'accepted',
  dismiss: 'dismissed',
  snooze: 'snoozed',
});

async function createMockCoreRepository(options) {
  const opts = options || {};
  const SQL = await initSqlJs();
  const dbPath = path.resolve(opts.dbPath || DEFAULT_DB_PATH);
  const persistEnabled = opts.persist !== false;
  const now = opts.now || (() => new Date().toISOString());
  const faultInjector = typeof opts.faultInjector === 'function'
    ? opts.faultInjector
    : async () => undefined;
  const persistFaultInjector = typeof opts.persistFaultInjector === 'function'
    ? opts.persistFaultInjector
    : () => undefined;
  let db;
  let writeTail = Promise.resolve();

  if (persistEnabled && fs.existsSync(dbPath)) {
    try {
      db = new SQL.Database(fs.readFileSync(dbPath));
      createSchema(db);
    } catch (_loadError) {
      console.warn('[mock-core] DB file corrupted (%s), recreating from scratch', _loadError && _loadError.message);
      db = new SQL.Database();
      createSchema(db);
    }
  } else {
    db = new SQL.Database();
    createSchema(db);
  }
  if (!queryOne(db, 'SELECT id FROM bank_customers LIMIT 1')) seedSync(db, now());
  else {
    seedTransferReferenceDataSync(db, now());
    seedCreditCardDomainSync(db, now());
    seedUtilityDomainSync(db, now());
    seedLifeDomainSync(db, now());
  }
  // 确保 cust_001 存在（记忆数据存储在此客户 ID 下，KV 38 条 + 治理标签 35 条）
  if (!queryOne(db, 'SELECT id FROM bank_customers WHERE id = ?', ['cust_001'])) {
    run(db, 'INSERT INTO bank_customers (id,name,status,city,created_at) VALUES (?,?,?,?,?)', ['cust_001', '测试客户', 'ACTIVE', '北京', now()]);
  }
  // cust_001 账户幂等插入（INSERT OR IGNORE，已存在则跳过）
  run(db, 'INSERT OR IGNORE INTO bank_accounts (id,customer_id,masked_no,name,type,currency,status,visibility,sort_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)', ['acct_test_salary_0001', 'cust_001', '**** **** **** 0001', '工资卡', 'DEBIT_CARD', 'CNY', 'ACTIVE', 'CUSTOMER', 99, now()]);
  run(db, 'INSERT OR IGNORE INTO bank_accounts (id,customer_id,masked_no,name,type,currency,status,visibility,sort_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)', ['acct_test_saving_8032', 'cust_001', '**** **** **** 8032', '储蓄卡', 'SAVINGS', 'CNY', 'ACTIVE', 'CUSTOMER', 98, now()]);
  run(db, 'INSERT OR IGNORE INTO bank_balance_snapshots (account_id,available_minor,ledger_minor,version,updated_at) VALUES (?,?,?,?,?)', ['acct_test_salary_0001', 2000000, 2000000, 1, now()]);
  run(db, 'INSERT OR IGNORE INTO bank_balance_snapshots (account_id,available_minor,ledger_minor,version,updated_at) VALUES (?,?,?,?,?)', ['acct_test_saving_8032', 7582000, 7582000, 1, now()]);
  seedWealthRiskAssessmentSync(db);
  if (persistEnabled) await persist();

  async function persist() {
    if (!persistEnabled) return;
    fs.mkdirSync(path.dirname(dbPath), { recursive: true });
    const tempPath = dbPath + '.tmp';
    const payload = Buffer.from(db.export());
    // 宿主机与容器可能各持一个写入方，9p 挂载上 open/rename 会瞬时 ENOENT（对方正在
    // rename 同名临时文件），对这类可重试错误做退避重试，避免整个仓库初始化失败。
    let lastError = null;
    for (let attempt = 0; attempt < 4; attempt += 1) {
      try {
        await fs.promises.writeFile(tempPath, payload);
        await persistFaultInjector('mock-core.persist.before-rename', { dbPath, tempPath });
        await fs.promises.rename(tempPath, dbPath);
        return;
      } catch (error) {
        lastError = error;
        if (!error || !['ENOENT', 'EACCES', 'EBUSY', 'EPERM'].includes(error.code)) throw error;
        await new Promise(resolve => setTimeout(resolve, 120 * (attempt + 1)));
      }
    }
    throw lastError;
  }

  // 防抖异步持久化：事务提交后仅标记 dirty，延迟合并写入，不阻塞关键路径
  let persistTimer = null;
  let persistDirty = false;
  const PERSIST_DEBOUNCE_MS = 50;

  function schedulePersist() {
    if (!persistEnabled) return;
    persistDirty = true;
    if (persistTimer) return;
    persistTimer = setTimeout(() => {
      persistTimer = null;
      if (!persistDirty) return;
      persistDirty = false;
      persist().catch(() => { /* best-effort: 内存 DB 为运行时唯一真相源 */ });
    }, PERSIST_DEBOUNCE_MS);
  }

  async function flushPersist() {
    if (persistTimer) {
      clearTimeout(persistTimer);
      persistTimer = null;
    }
    if (persistDirty) {
      persistDirty = false;
      await persist();
    }
  }

  function restoreCommittedSnapshot(snapshot) {
    const committedDb = db;
    const restoredDb = new SQL.Database(snapshot);
    db = restoredDb;
    try { committedDb.close(); } catch (closeError) { /* restored DB is authoritative */ }
    try { fs.unlinkSync(dbPath + '.tmp'); } catch (unlinkError) {
      if (unlinkError && unlinkError.code !== 'ENOENT') throw unlinkError;
    }
    // 将回滚后的快照同步写入磁盘，确保磁盘与内存一致（包含之前防抖异步 persist 尚未落盘的非 durable 事务修改）
    if (persistEnabled) {
      try { fs.writeFileSync(dbPath, Buffer.from(snapshot)); } catch (writeError) { /* best-effort: 内存 DB 为运行时唯一真相源 */ }
    }
  }

  function enqueueWrite(operation) {
    const result = writeTail.then(operation, operation);
    writeTail = result.catch(() => undefined);
    return result;
  }

  async function withTransaction(work, options) {
    const durable = options && options.durable;
    return enqueueWrite(async function () {
      if (durable) {
        // 幂等操作要求同步持久化 + 失败回滚，确保内存与磁盘一致
        const committedSnapshot = persistEnabled ? db.export() : null;
        let committed = false;
        db.run('BEGIN IMMEDIATE');
        try {
          const result = await work(createTransactionFacade(db, now));
          db.run('COMMIT');
          committed = true;
          try {
            await persist();
          } catch (error) {
            restoreCommittedSnapshot(committedSnapshot);
            throw error;
          }
          return result;
        } catch (error) {
          if (!committed) {
            try { db.run('ROLLBACK'); } catch (rollbackError) { /* transaction already closed */ }
          }
          throw error;
        }
      }
      // 普通事务：提交后防抖异步持久化，不阻塞关键路径
      db.run('BEGIN IMMEDIATE');
      try {
        const result = await work(createTransactionFacade(db, now));
        db.run('COMMIT');
        schedulePersist();
        return result;
      } catch (error) {
        try { db.run('ROLLBACK'); } catch (rollbackError) { /* transaction already closed */ }
        throw error;
      }
    });
  }

  async function resetScenario(scenarioId) {
    if (scenarioId !== 'default') throw new BankApiError(404, 'scenario_not_found', 'Mock 场景不存在');
    return withTransaction(async function () {
      clearTables(db);
      seedSync(db, now());
      return { scenarioId: 'default', resetAt: now() };
    });
  }

  async function createAgentSession(item) {
    validateAgentSession(item);
    return withTransaction(async tx => {
      const customer = tx.queryOne('SELECT id FROM bank_customers WHERE id = ?', [item.customerId]);
      if (!customer) return null;
      try {
        tx.run(`INSERT INTO bank_agent_sessions
          (id,customer_id,status,created_at,last_active_at,idle_expires_at,absolute_expires_at,closed_at,close_reason)
          VALUES (?,?,?,?,?,?,?,?,?)`, [
          item.id, item.customerId, item.status, item.createdAt, item.lastActiveAt,
          item.idleExpiresAt, item.absoluteExpiresAt, null, null,
        ]);
      } catch (error) {
        if (String(error && error.message).includes('UNIQUE constraint failed')) {
          throw new BankApiError(409, 'agent_session_conflict', 'Agent Session 已存在');
        }
        throw error;
      }
      return mapAgentSession(tx.queryOne(
        'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
        [item.id, item.customerId],
      ));
    });
  }

  async function createFocusedAgentSession(item) {
    validateFocusedAgentSession(item);
    return withTransaction(async tx => {
      const customer = tx.queryOne('SELECT id FROM bank_customers WHERE id = ?', [item.session.customerId]);
      if (!customer) return null;
      const currentTime = transactionTimestamp();
      const reusable = mapAgentSession(tx.queryOne(`SELECT session.*
        FROM bank_agent_sessions session
        INNER JOIN bank_agent_session_focus focus ON focus.session_id = session.id
        WHERE session.customer_id = ? AND session.status = 'active'
          AND focus.recommendation_id = ? AND focus.mode = ?
          AND focus.context_digest = ? AND focus.target_intent_id = ?
          AND session.idle_expires_at > ? AND session.absolute_expires_at > ?
          AND focus.expires_at > ?
        ORDER BY session.last_active_at DESC LIMIT 1`, [
        item.session.customerId, item.focus.recommendationId, item.focus.mode,
        item.focus.contextDigest, item.focus.targetIntentId,
        currentTime.iso, currentTime.iso, currentTime.iso,
      ]));
      if (reusable) return reusable;
      tx.run(`INSERT INTO bank_agent_sessions
        (id,customer_id,status,created_at,last_active_at,idle_expires_at,absolute_expires_at,closed_at,close_reason)
        VALUES (?,?,?,?,?,?,?,?,?)`, [
        item.session.id, item.session.customerId, item.session.status,
        item.session.createdAt, item.session.lastActiveAt, item.session.idleExpiresAt,
        item.session.absoluteExpiresAt, null, null,
      ]);
      tx.run(`INSERT INTO bank_agent_session_focus
        (session_id,customer_id,recommendation_id,mode,target_intent_id,context_digest,focus_json,expires_at,created_at)
        VALUES (?,?,?,?,?,?,?,?,?)`, [
        item.session.id, item.session.customerId, item.focus.recommendationId,
        item.focus.mode, item.focus.targetIntentId, item.focus.contextDigest,
        JSON.stringify(item.focus), item.focus.expiresAt, item.session.createdAt,
      ]);
      return mapAgentSession(tx.queryOne(
        'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
        [item.session.id, item.session.customerId],
      ));
    });
  }

  async function getAgentSessionFocus(sessionId, customerId) {
    validateAgentSessionLookup({ id: sessionId, customerId });
    const row = queryOne(db, `SELECT focus.focus_json
      FROM bank_agent_session_focus focus
      INNER JOIN bank_agent_sessions session ON session.id = focus.session_id
      WHERE focus.session_id = ? AND focus.customer_id = ? AND session.customer_id = ?`,
    [sessionId, customerId, customerId]);
    return row ? JSON.parse(row.focus_json) : null;
  }

  function transactionTimestamp() {
    const value = now();
    const milliseconds = value instanceof Date ? value.getTime() : Date.parse(value);
    if (!Number.isFinite(milliseconds)) throw new TypeError('Mock Core 时钟返回值无效');
    return { milliseconds, iso: new Date(milliseconds).toISOString() };
  }

  function expireAgentSessionIfDue(tx, session, current) {
    if (!session || session.status !== 'active') return null;
    let closeReason = null;
    if (Date.parse(session.absoluteExpiresAt) <= current.milliseconds) {
      closeReason = 'absolute_ttl_expired';
    } else if (Date.parse(session.idleExpiresAt) <= current.milliseconds) {
      closeReason = 'idle_ttl_expired';
    }
    if (!closeReason) return null;
    tx.run(`UPDATE bank_agent_sessions
      SET status = 'expired', closed_at = ?, close_reason = ?
      WHERE id = ? AND customer_id = ? AND status = 'active'`, [
      current.iso, closeReason, session.id, session.customerId,
    ]);
    suspendBoundWorkflowsTx(tx, session.id, session.customerId, 'session_expired', current.iso);
    return mapAgentSession(tx.queryOne(
      'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
      [session.id, session.customerId],
    ));
  }

  async function requireActiveAgentSession(item) {
    validateAgentSessionLookup(item);
    return withTransaction(async tx => {
      const currentTime = transactionTimestamp();
      const current = mapAgentSession(tx.queryOne(
        'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
        [item.id, item.customerId],
      ));
      if (!current || current.status !== 'active') return current;
      return expireAgentSessionIfDue(tx, current, currentTime) || current;
    });
  }

  async function touchAgentSession(item) {
    validateAgentSessionTouch(item);
    return withTransaction(async tx => {
      const currentTime = transactionTimestamp();
      const current = mapAgentSession(tx.queryOne(
        'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
        [item.id, item.customerId],
      ));
      if (!current || current.status !== 'active') return current;
      const expired = expireAgentSessionIfDue(tx, current, currentTime);
      if (expired) return expired;
      const idleExpiresAt = new Date(Math.min(
        currentTime.milliseconds + item.idleTtlMs,
        Date.parse(current.absoluteExpiresAt),
      )).toISOString();
      tx.run(`UPDATE bank_agent_sessions SET last_active_at = ?, idle_expires_at = ?
        WHERE id = ? AND customer_id = ? AND status = 'active'`, [
        currentTime.iso, idleExpiresAt, item.id, item.customerId,
      ]);
      return mapAgentSession(tx.queryOne(
        'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
        [item.id, item.customerId],
      ));
    });
  }

  async function closeAgentSession(item) {
    validateAgentSessionClose(item);
    return withTransaction(async tx => {
      const current = mapAgentSession(tx.queryOne(
        'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
        [item.id, item.customerId],
      ));
      if (!current || current.status !== 'active') return current;
      tx.run(`UPDATE bank_agent_sessions SET status = ?, closed_at = ?, close_reason = ?
        WHERE id = ? AND customer_id = ? AND status = 'active'`, [
        item.status, item.closedAt, item.closeReason, item.id, item.customerId,
      ]);
      suspendBoundWorkflowsTx(tx, item.id, item.customerId, 'session_closed', item.closedAt);
      return mapAgentSession(tx.queryOne(
        'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
        [item.id, item.customerId],
      ));
    });
  }

  async function closeAgentSessionIdempotent(item) {
    validateAtomicSessionClose(item);
    const outcome = await withTransaction(async tx => {
      const replay = requireIdempotencyReplayTx(tx, item, ATOMIC_IDEMPOTENCY_OPERATIONS.close);
      if (replay) return { response: replay };
      const currentTime = transactionTimestamp();
      let current = mapAgentSession(tx.queryOne(
        'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
        [item.id, item.customerId],
      ));
      if (!current) throw new BankApiError(404, 'agent_session_not_found', 'Agent Session 不存在');
      if (current.status === 'expired') {
        throw new BankApiError(409, 'agent_session_expired', 'Agent Session 已过期');
      }
      if (current.status === 'active') {
        const expired = expireAgentSessionIfDue(tx, current, currentTime);
        if (expired) return { expired };
        tx.run(`UPDATE bank_agent_sessions SET status = 'closed', closed_at = ?, close_reason = ?
          WHERE id = ? AND customer_id = ? AND status = 'active'`, [
          currentTime.iso, item.closeReason, item.id, item.customerId,
        ]);
        suspendBoundWorkflowsTx(
          tx, item.id, item.customerId, 'session_closed', currentTime.iso,
        );
        current = mapAgentSession(tx.queryOne(
          'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
          [item.id, item.customerId],
        ));
      }
      const response = projectAgentSessionCommandResponse(current);
      await faultInjector('agent-session.close.before-idempotency-save', {
        sessionId: item.id, customerId: item.customerId,
      });
      saveIdempotencyResponseTx(tx, item, response, ATOMIC_IDEMPOTENCY_OPERATIONS.close);
      return { response };
    }, { durable: true });
    if (outcome.expired) {
      throw new BankApiError(409, 'agent_session_expired', 'Agent Session 已过期');
    }
    return outcome.response;
  }

  async function appendAgentSessionTurn(item) {
    const projected = validateAgentSessionTurn(item);
    const outcome = await withTransaction(async tx => {
      const currentTime = transactionTimestamp();
      const session = mapAgentSession(tx.queryOne(
        'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
        [item.sessionId, item.customerId],
      ));
      if (!session) throw new BankApiError(404, 'agent_session_not_found', 'Agent Session 不存在');
      if (session.status !== 'active') {
        const code = session.status === 'expired' ? 'agent_session_expired' : 'agent_session_closed';
        throw new BankApiError(409, code, 'Agent Session 不可写入');
      }
      const expired = expireAgentSessionIfDue(tx, session, currentTime);
      if (expired) return { expired };
      const sequence = Number((tx.queryOne(
        'SELECT COALESCE(MAX(sequence), 0) AS sequence FROM bank_agent_session_turns WHERE session_id = ?',
        [item.sessionId],
      ) || {}).sequence || 0) + 1;
      tx.run(`INSERT INTO bank_agent_session_turns
        (session_id,sequence,role,text,response_json,created_at) VALUES (?,?,?,?,?,?)`, [
        item.sessionId, sequence, projected.role, projected.text,
        projected.response == null ? null : JSON.stringify(projected.response), currentTime.iso,
      ]);
      tx.run('DELETE FROM bank_agent_session_turns WHERE session_id = ? AND sequence <= ?', [
        item.sessionId, sequence - 40,
      ]);
      return { turn: mapAgentSessionTurn(tx.queryOne(
        'SELECT * FROM bank_agent_session_turns WHERE session_id = ? AND sequence = ?',
        [item.sessionId, sequence],
      )) };
    });
    if (outcome.expired) {
      throw new BankApiError(409, 'agent_session_expired', 'Agent Session 已过期');
    }
    return outcome.turn;
  }

  async function createAgentRun(item) {
    const capabilityProjection = validateAgentRun(item);
    return withTransaction(async tx => {
      try {
        tx.run(`INSERT INTO bank_agent_runs
          (id,customer_id,conversation_id,trace_id,status,graph_version,graph_digest,last_trace_sequence,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?)`, [
          item.id, item.customerId, item.conversationId, item.traceId, item.status,
          item.graphVersion, item.graphDigest, 0, now(), now(),
        ]);
        capabilityProjection.executableCapabilityIds.forEach(capabilityId => tx.run(
          'INSERT INTO bank_agent_run_capabilities (agent_run_id,capability_id) VALUES (?,?)',
          [item.id, capabilityId]
        ));
        capabilityProjection.plannableCapabilityIds.forEach(capabilityId => tx.run(
          'INSERT INTO bank_agent_run_plannable_capabilities (agent_run_id,capability_id) VALUES (?,?)',
          [item.id, capabilityId]
        ));
      } catch (error) {
        if (String(error && error.message).includes('UNIQUE constraint failed')) {
          throw new BankApiError(409, 'agent_run_conflict', 'Agent Run 已存在');
        }
        throw error;
      }
      return getAgentRunRecord(db, item.id);
    });
  }

  async function markAgentRunTraceCapture(agentRunId, traceCaptureStatus) {
    assertWorkbenchId(agentRunId, 'agentRunId');
    if (!AGENT_RUN_TRACE_CAPTURE_STATUSES.includes(traceCaptureStatus)) {
      throw new BankApiError(400, 'invalid_trace_capture_status', '运行事件采集状态无效');
    }
    return withTransaction(async tx => {
      if (!tx.queryOne('SELECT id FROM bank_agent_runs WHERE id = ?', [agentRunId])) {
        throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
      }
      tx.run(
        'UPDATE bank_agent_runs SET trace_capture_status = ?, updated_at = ? WHERE id = ?',
        [traceCaptureStatus, now(), agentRunId],
      );
      return getAgentRunRecord(db, agentRunId);
    });
  }

  async function appendAgentRunEvent(item) {
    return withTransaction(async tx => appendAgentRunEventRecord(tx, item, now()));
  }

  async function createAgentRunModelCall(item) {
    return withTransaction(async tx => createAgentRunModelCallRecord(tx, item));
  }

  async function bindAgentRunWorkflow(item) {
    return withTransaction(async tx => bindAgentRunWorkflowRecord(tx, item, now()));
  }

  async function listAgentRuns(customerId, options) {
    return listAgentRunRecords(db, customerId, options || {});
  }

  async function getAgentRunWorkbenchDetail(agentRunId, customerId) {
    return getAgentRunWorkbenchDetailRecord(db, agentRunId, customerId);
  }

  async function enqueueMemoryCommand(item) {
    validateMemoryCommandEnvelope(item);
    return withTransaction(async tx => {
      const run = tx.queryOne(
        'SELECT id,customer_id FROM bank_agent_runs WHERE id = ? AND customer_id = ?',
        [item.agentRunId, item.customerId],
      );
      if (!run) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run does not exist');
      const commandJson = stableJson(item.command);
      const commandHash = crypto.createHash('sha256').update(commandJson).digest('hex');
      const existing = tx.queryOne(
        'SELECT * FROM bank_agent_memory_commands WHERE idempotency_key = ?',
        [item.command.idempotencyKey],
      );
      if (existing) {
        if (existing.command_hash !== commandHash) {
          throw new BankApiError(409, 'memory_command_idempotency_conflict', 'Memory command idempotency key conflict');
        }
        return mapMemoryCommandInternal(existing);
      }
      const timestamp = now();
      const id = 'amc_' + crypto.randomUUID().replace(/-/g, '');
      tx.run(`INSERT INTO bank_agent_memory_commands
        (id,idempotency_key,agent_run_id,customer_id,task_id,intent_id,candidate_type,predicate,
         command_json,command_hash,status,attempts,next_attempt_at,decision,governance_status,
         reason,policy_version,error_code,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, [
        id, item.command.idempotencyKey, item.agentRunId, item.customerId,
        item.taskId || null, item.intentId || null, item.command.candidateType,
        item.command.predicate, commandJson, commandHash, 'QUEUED', 0, timestamp,
        null, null, null, null, null, timestamp, timestamp,
      ]);
      return mapMemoryCommandInternal(tx.queryOne(
        'SELECT * FROM bank_agent_memory_commands WHERE id = ?', [id],
      ));
    });
  }

  async function claimDueMemoryCommands(options = {}) {
    const limit = Math.max(1, Math.min(50, Number(options.limit) || 10));
    const ids = Array.isArray(options.ids) ? options.ids.filter(item => typeof item === 'string') : [];
    return withTransaction(async tx => {
      const timestamp = now();
      const staleBefore = options.staleBefore || new Date(Date.parse(timestamp) - 5 * 60 * 1000).toISOString();
      tx.run(`UPDATE bank_agent_memory_commands SET status = 'PENDING_RETRY', next_attempt_at = ?,
        error_code = 'processing_lease_expired', updated_at = ?
        WHERE status = 'PROCESSING' AND updated_at <= ?`, [timestamp, timestamp, staleBefore]);
      const idClause = ids.length ? ` AND id IN (${ids.map(() => '?').join(',')})` : '';
      const rows = tx.queryAll(`SELECT * FROM bank_agent_memory_commands
        WHERE status IN ('QUEUED','PENDING_RETRY') AND next_attempt_at <= ?${idClause}
        ORDER BY created_at,id LIMIT ?`, [timestamp, ...ids, limit]);
      const claimed = [];
      for (const row of rows) {
        tx.run(`UPDATE bank_agent_memory_commands SET status = 'PROCESSING', attempts = attempts + 1,
          updated_at = ? WHERE id = ? AND status IN ('QUEUED','PENDING_RETRY')`, [timestamp, row.id]);
        if (db.getRowsModified() === 1) claimed.push(mapMemoryCommandInternal(tx.queryOne(
          'SELECT * FROM bank_agent_memory_commands WHERE id = ?', [row.id],
        )));
      }
      return claimed;
    });
  }

  async function completeMemoryCommand(id, receipt) {
    validateMemoryReceipt(receipt);
    const status = receipt.decision === 'CONTESTED'
      ? 'CONTESTED' : receipt.decision === 'REJECT' ? 'REJECTED' : 'PERSISTED';
    return transitionMemoryCommand(id, status, receipt);
  }

  async function retryMemoryCommand(id, retry) {
    if (!retry || typeof retry.errorCode !== 'string' || !Number.isFinite(Date.parse(retry.retryAt))) {
      throw new BankApiError(400, 'memory_command_retry_invalid', 'Memory command retry is invalid');
    }
    return transitionMemoryCommand(id, 'PENDING_RETRY', retry);
  }

  async function deadLetterMemoryCommand(id, failure) {
    if (!failure || typeof failure.errorCode !== 'string') {
      throw new BankApiError(400, 'memory_command_failure_invalid', 'Memory command failure is invalid');
    }
    return transitionMemoryCommand(id, 'DEAD_LETTER', failure);
  }

  // 事件消费留痕（keep-pull 消费点审计）：同一 (customer, event) 实例只记一次。
  // 卡片幂等由 stable-ID 保证，本表负责"哪个事件实例被哪些信号消费过"的审计可见性。
  async function recordExperienceEventConsumption(item) {
    if (!item || !/^[A-Za-z0-9_.:-]{1,160}$/.test(String(item.customerId || ''))
      || !/^[A-Za-z0-9_.:-]{1,160}$/.test(String(item.eventId || ''))) {
      throw new BankApiError(400, 'experience_event_input_invalid', 'Experience event input is invalid');
    }
    const eventType = String(item.eventType || '');
    const occurredAt = String(item.occurredAt || '');
    if (!eventType || !Number.isFinite(Date.parse(occurredAt))) {
      throw new BankApiError(400, 'experience_event_input_invalid', 'Experience event input is invalid');
    }
    return withTransaction(async tx => {
      const existing = tx.queryOne(
        'SELECT * FROM bank_experience_events_consumed WHERE customer_id = ? AND event_id = ?',
        [item.customerId, item.eventId],
      );
      if (existing) {
        return {
          id: existing.id, customerId: existing.customer_id, eventId: existing.event_id,
          eventType: existing.event_type, occurredAt: existing.occurred_at,
          consumedAt: existing.consumed_at, created: false,
        };
      }
      const timestamp = now();
      const id = 'bec_' + crypto.randomUUID().replace(/-/g, '');
      tx.run(`INSERT INTO bank_experience_events_consumed
        (id,customer_id,event_id,event_type,occurred_at,consumed_at) VALUES (?,?,?,?,?,?)`, [
        id, item.customerId, item.eventId, eventType, occurredAt, timestamp,
      ]);
      return {
        id, customerId: item.customerId, eventId: item.eventId,
        eventType, occurredAt, consumedAt: timestamp, created: true,
      };
    });
  }

  async function listConsumedExperienceEvents(customerId, options = {}) {
    if (!/^[A-Za-z0-9_.:-]{1,160}$/.test(String(customerId || ''))) {
      throw new BankApiError(400, 'experience_event_input_invalid', 'customerId is invalid');
    }
    const limit = Math.max(1, Math.min(100, Number(options.limit) || 20));
    const rows = queryAll(
      db,
      'SELECT * FROM bank_experience_events_consumed WHERE customer_id = ? ORDER BY consumed_at DESC, id LIMIT ?',
      [customerId, limit],
    );
    return rows.map(row => ({
      id: row.id, customerId: row.customer_id, eventId: row.event_id,
      eventType: row.event_type, occurredAt: row.occurred_at,
      consumedAt: row.consumed_at,
    }));
  }

  async function transitionMemoryCommand(id, status, detail) {
    if (!/^[A-Za-z0-9_.:-]{1,160}$/.test(String(id || ''))) {
      throw new BankApiError(400, 'memory_command_id_invalid', 'Memory command id is invalid');
    }
    return withTransaction(async tx => {
      const current = tx.queryOne('SELECT * FROM bank_agent_memory_commands WHERE id = ?', [id]);
      if (!current) throw new BankApiError(404, 'memory_command_not_found', 'Memory command does not exist');
      if (current.status !== 'PROCESSING') {
        throw new BankApiError(409, 'memory_command_state_conflict', 'Memory command state conflict');
      }
      const timestamp = now();
      tx.run(`UPDATE bank_agent_memory_commands SET status = ?, next_attempt_at = ?, decision = ?,
        governance_status = ?, reason = ?, policy_version = ?, error_code = ?, updated_at = ? WHERE id = ?`, [
        status, status === 'PENDING_RETRY' ? detail.retryAt : timestamp,
        detail.decision || null, detail.governanceStatus || null, detail.reason || null,
        detail.policyVersion || null, detail.errorCode || null, timestamp, id,
      ]);
      return mapMemoryCommandInternal(tx.queryOne(
        'SELECT * FROM bank_agent_memory_commands WHERE id = ?', [id],
      ));
    });
  }

  async function listAgentRunMemoryUpdates(agentRunId) {
    assertWorkbenchId(agentRunId, 'agentRunId');
    return queryAll(db, `SELECT * FROM bank_agent_memory_commands WHERE agent_run_id = ?
      ORDER BY updated_at DESC,id DESC LIMIT 20`, [agentRunId]).map(mapMemoryCommandPublic);
  }

  async function createAgentClarification(item) {
    const workflowFields = [
      item.businessWorkflowId, item.taskId, item.expectedWorkflowRevision,
    ];
    const hasWorkflowBinding = workflowFields.every(value => value != null);
    if (!hasWorkflowBinding && workflowFields.some(value => value != null)) {
      throw new BankApiError(400, 'invalid_agent_clarification', '补录任务工作流绑定不完整');
    }
    if (hasWorkflowBinding && (
      !/^[A-Za-z0-9_.:-]{1,160}$/.test(item.businessWorkflowId)
      || !/^[A-Za-z0-9_.:-]{1,96}$/.test(item.taskId)
      || !Number.isInteger(item.expectedWorkflowRevision)
      || item.expectedWorkflowRevision < 1
    )) {
      throw new BankApiError(400, 'invalid_agent_clarification', '补录任务工作流绑定无效');
    }
    return withTransaction(async tx => {
      const runRecord = mapAgentRun(tx.queryOne(
        'SELECT * FROM bank_agent_runs WHERE id = ? AND customer_id = ?',
        [item.agentRunId, item.customerId],
      ));
      if (!runRecord) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
      try {
        tx.run(`INSERT INTO bank_agent_clarifications
          (id,agent_run_id,customer_id,conversation_id,kind,payee_name,token_hash,status,expires_at,
           business_workflow_id,task_id,expected_workflow_revision,consumed_at,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, [
          item.id, item.agentRunId, item.customerId, item.conversationId,
          item.kind, item.payeeName, item.tokenHash, 'PENDING', item.expiresAt,
          item.businessWorkflowId || null, item.taskId || null,
          item.expectedWorkflowRevision || null, null, now(), now(),
        ]);
      } catch (error) {
        if (String(error && error.message).includes('UNIQUE constraint failed')) {
          throw new BankApiError(409, 'clarification_conflict', '补录任务已存在');
        }
        throw error;
      }
      return mapAgentClarification(tx.queryOne(
        'SELECT * FROM bank_agent_clarifications WHERE id = ?', [item.id],
      ));
    });
  }

  async function consumeAgentClarification(item) {
    return withTransaction(async tx => {
      const current = mapAgentClarification(tx.queryOne(
        `SELECT * FROM bank_agent_clarifications
         WHERE id = ? AND agent_run_id = ? AND customer_id = ?`,
        [item.id, item.agentRunId, item.customerId],
      ));
      if (!current) throw new BankApiError(404, 'clarification_not_found', '补录任务不存在');
      if (current.status !== 'PENDING') {
        throw new BankApiError(409, 'clarification_consumed', '该补录任务已提交');
      }
      if (current.tokenHash !== item.tokenHash) {
        throw new BankApiError(403, 'clarification_token_invalid', '补录凭证无效');
      }
      // 防御 NaN：若 current.expiresAt 或 now() 是非法字符串，
      // Date.parse 返回 NaN，NaN <= NaN 为 false 会绕过过期检查。
      const expiresMs = Date.parse(current.expiresAt);
      const nowMs = Date.parse(now());
      if (!Number.isFinite(expiresMs) || !Number.isFinite(nowMs) || expiresMs <= nowMs) {
        throw new BankApiError(409, 'clarification_expired', '补录任务已过期，请重新发起');
      }

      // payeeId 由 customerId+账号 HMAC 派生：同一账号换户名再次补录时
      // 必须复用既有收款人，否则主键冲突会以 500 告终。
      // 优先使用用户在卡片里填写的 payeeName（transfer_full 卡片收款人可能为空），
      // 回退到创建卡片时持久化的 current.payeeName。
      const effectivePayeeName = item.payeeName || current.payeeName || '';
      // 收款人去重：同名 + 卡号尾号4位一致即视为同一收款人。掩码存在
      // 两种格式（带空格 19 位卡 "**** **** **** 7319" 与紧凑 16 位卡
      // "****7319"），星号数量随卡长变化，精确或去空格比对都会漏匹配，
      // 导致同名收款人重复插入，进而在转账实体唯一匹配时产生歧义死胡同。
      const maskedTail = String(item.maskedNo || '').replace(/\s+/g, '').slice(-4);
      const existing = mapPayee(tx.queryOne(
        `SELECT * FROM bank_payees
         WHERE customer_id = ?
           AND ((name = ? AND LENGTH(REPLACE(masked_no, ' ', '')) >= 4
                AND SUBSTR(REPLACE(masked_no, ' ', ''), -4) = ?) OR id = ?)
           AND (is_saved = 1 OR expires_at > ?)`,
        [item.customerId, effectivePayeeName, maskedTail, item.payeeId, now()],
      ));
      let payee = existing;
      if (!payee) {
        if (!tx.queryOne('SELECT id FROM bank_accounts WHERE id = ?', [item.targetAccountId])) {
          tx.run(`INSERT INTO bank_accounts
            (id,customer_id,masked_no,name,type,currency,status,visibility,sort_order,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)`, [
            item.targetAccountId, null, item.maskedNo, effectivePayeeName + '收款账户',
            'DEBIT', 'CNY', 'ACTIVE', 'SYSTEM', 0, now(),
          ]);
          tx.run(`INSERT INTO bank_balance_snapshots
            (account_id,available_minor,ledger_minor,version,updated_at)
            VALUES (?,?,?,?,?)`, [item.targetAccountId, 0, 0, 1, now()]);
        }
        // payeeId 由 (customerId, accountNo) HMAC 确定性派生：同一客户对同一
        // 卡号再次补录（或上次临时收款人已过期）时 id 必然复现。上面的候选
        // 查询带 (is_saved=1 OR 未过期) 过滤，会把这类历史行漏成"不存在"，
        // 直接 INSERT 就撞 bank_payees.id 唯一键 500。补录提交必须幂等：
        // 先按 id 复活刷新历史行，没有可刷新行才真正插入。
        tx.run(`UPDATE bank_payees
          SET name = ?, bank_name = ?, masked_no = ?, status = 'ACTIVE',
              target_account_id = ?, is_saved = ?, expires_at = ?
          WHERE id = ? AND customer_id = ?`, [
          effectivePayeeName, item.bankName, item.maskedNo, item.targetAccountId,
          item.savePayee ? 1 : 0,
          item.savePayee ? null : item.referenceExpiresAt,
          item.payeeId, item.customerId,
        ]);
        if (!db.getRowsModified()) {
          const sortOrder = Number((tx.queryOne(
            'SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM bank_payees WHERE customer_id = ?',
            [item.customerId],
          ) || {}).next_order || 0);
          tx.run(`INSERT INTO bank_payees
            (id,customer_id,name,bank_name,masked_no,status,target_account_id,sort_order,is_saved,expires_at,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)`, [
            item.payeeId, item.customerId, effectivePayeeName, item.bankName,
            item.maskedNo, 'ACTIVE', item.targetAccountId, sortOrder,
            item.savePayee ? 1 : 0,
            item.savePayee ? null : item.referenceExpiresAt,
            now(),
          ]);
        }
        payee = mapPayee(tx.queryOne('SELECT * FROM bank_payees WHERE id = ?', [item.payeeId]));
      }
      tx.run(`UPDATE bank_agent_clarifications
        SET status = 'CONSUMED', consumed_at = ?, updated_at = ?
        WHERE id = ? AND status = 'PENDING'`, [now(), now(), item.id]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'clarification_consumed', '该补录任务已提交');
      }
      return { clarification: Object.assign({}, current, { status: 'CONSUMED' }), payee };
    });
  }

  async function transitionAgentRun(id, nextStatus) {
    if (!Object.prototype.hasOwnProperty.call(AGENT_RUN_TRANSITIONS, nextStatus)) {
      throw new BankApiError(400, 'invalid_agent_run_status', 'Agent Run 目标状态无效');
    }
    return withTransaction(async tx => {
      const current = getAgentRunRecord(db, id);
      if (!current) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
      if (!AGENT_RUN_TRANSITIONS[current.status].includes(nextStatus)) {
        throw new BankApiError(409, 'agent_run_state_conflict', 'Agent Run 状态不允许当前转换');
      }
      tx.run('UPDATE bank_agent_runs SET status = ?, updated_at = ? WHERE id = ? AND status = ?', [nextStatus, now(), id, current.status]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'agent_run_state_conflict', 'Agent Run 状态已发生变化');
      }
      return getAgentRunRecord(db, id);
    });
  }

  async function upsertConversationSlot(item) {
    validateConversationSlot(item);
    return withTransaction(async tx => {
      tx.run(`INSERT INTO bank_agent_conversation_slots
        (conversation_id,customer_id,slot_name,value_json,source,source_intent_id,updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(conversation_id,customer_id,slot_name) DO UPDATE SET
          value_json = excluded.value_json,
          source = excluded.source,
          source_intent_id = excluded.source_intent_id,
          updated_at = excluded.updated_at`, [
        item.conversationId, item.customerId, item.slotName, JSON.stringify(item.value), item.source || 'context', item.sourceIntentId, now(),
      ]);
      return mapConversationSlot(tx.queryOne(
        'SELECT * FROM bank_agent_conversation_slots WHERE conversation_id = ? AND customer_id = ? AND slot_name = ?',
        [item.conversationId, item.customerId, item.slotName]
      ));
    });
  }

  async function replaceConversationSlots(item) {
    const normalized = validateConversationSlotReplacement(item);
    return withTransaction(async tx => {
      tx.run(
        'DELETE FROM bank_agent_conversation_slots WHERE conversation_id = ? AND customer_id = ?',
        [normalized.conversationId, normalized.customerId],
      );
      normalized.slots.forEach(slot => tx.run(`INSERT INTO bank_agent_conversation_slots
        (conversation_id,customer_id,slot_name,value_json,source,source_intent_id,updated_at)
        VALUES (?,?,?,?,?,?,?)`, [
        normalized.conversationId, normalized.customerId, slot.name,
        JSON.stringify(slot.value), slot.source, normalized.sourceIntentId, now(),
      ]));
      return tx.queryAll(
        'SELECT * FROM bank_agent_conversation_slots WHERE conversation_id = ? AND customer_id = ? ORDER BY slot_name',
        [normalized.conversationId, normalized.customerId],
      ).map(mapConversationSlot);
    });
  }

  async function clearConversationSlots(conversationId, customerId) {
    requireNonEmptyString(conversationId, 'conversationId');
    requireNonEmptyString(customerId, 'customerId');
    return withTransaction(async tx => {
      tx.run(
        'DELETE FROM bank_agent_conversation_slots WHERE conversation_id = ? AND customer_id = ?',
        [conversationId, customerId],
      );
      return { cleared: true };
    });
  }

  async function replaceConversationState(item) {
    const normalized = validateConversationStateReplacement(item);
    return withTransaction(async tx => {
      tx.run(`INSERT INTO bank_agent_conversation_states
        (conversation_id,customer_id,schema_version,state_json,updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(conversation_id,customer_id) DO UPDATE SET
          schema_version = excluded.schema_version,
          state_json = excluded.state_json,
          updated_at = excluded.updated_at`, [
        normalized.conversationId, normalized.customerId, '2.0',
        JSON.stringify(normalized.state), now(),
      ]);
      return mapConversationState(tx.queryOne(
        'SELECT * FROM bank_agent_conversation_states WHERE conversation_id = ? AND customer_id = ?',
        [normalized.conversationId, normalized.customerId],
      ));
    });
  }

  async function clearConversationState(conversationId, customerId) {
    requireNonEmptyString(conversationId, 'conversationId');
    requireNonEmptyString(customerId, 'customerId');
    return withTransaction(async tx => {
      tx.run(
        'DELETE FROM bank_agent_conversation_states WHERE conversation_id = ? AND customer_id = ?',
        [conversationId, customerId],
      );
      return { cleared: true };
    });
  }

  async function replaceAgentWorkflow(item) {
    const normalized = validateAgentWorkflow(item);
    return withTransaction(async tx => {
      const existing = mapAgentWorkflow(tx.queryOne(
        'SELECT * FROM bank_agent_workflows WHERE id = ?', [normalized.id],
      ));
      if (existing && (existing.customerId !== normalized.customerId
        || existing.conversationId !== normalized.conversationId)) {
        throw new BankApiError(404, 'agent_workflow_not_found', 'Agent Workflow 不存在或不属于当前客户');
      }
      if (!existing) {
        if (normalized.expectedRevision !== 0 || normalized.workflowRevision !== 1) {
          throw new BankApiError(409, 'agent_workflow_revision_conflict', 'Agent Workflow 版本已发生变化');
        }
        tx.run(`INSERT INTO bank_agent_workflows
          (id,conversation_id,origin_conversation_id,bound_conversation_id,customer_id,status,delegated_task_ids_json,envelope_revision,
           workflow_revision,todo_revision,todo_snapshot_json,todo_digest,active_goal_task_id,
           active_todo_id,execution_policy,suspension_reason,last_outcome_json,
           pending_checkpoint_ids_json,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, [
          normalized.id, normalized.conversationId, normalized.conversationId,
          normalized.conversationId, normalized.customerId,
          normalized.status, JSON.stringify(normalized.delegatedTaskIds),
          normalized.envelopeRevision, normalized.workflowRevision,
          normalized.todoRevision, JSON.stringify(normalized.todoSnapshot),
          normalized.todoDigest, normalized.activeGoalTaskId, normalized.activeTodoId,
          normalized.executionPolicy, normalized.suspensionReason,
          JSON.stringify(normalized.lastOutcome),
          JSON.stringify(normalized.pendingCheckpointIds), now(), now(),
        ]);
      } else {
        if (normalized.expectedRevision !== existing.workflowRevision
          || normalized.workflowRevision !== normalized.expectedRevision + 1
          || normalized.envelopeRevision < existing.envelopeRevision
          || normalized.todoRevision < existing.todoRevision) {
          throw new BankApiError(409, 'agent_workflow_revision_conflict', 'Agent Workflow 版本已发生变化');
        }
        tx.run(`UPDATE bank_agent_workflows SET
          status = ?, delegated_task_ids_json = ?, envelope_revision = ?,
          workflow_revision = ?, todo_revision = ?, todo_snapshot_json = ?, todo_digest = ?,
          active_goal_task_id = ?, active_todo_id = ?, execution_policy = ?,
          suspension_reason = ?, last_outcome_json = ?, pending_checkpoint_ids_json = ?,
          updated_at = ?
          WHERE id = ? AND bound_conversation_id = ? AND customer_id = ? AND workflow_revision = ?`, [
          normalized.status, JSON.stringify(normalized.delegatedTaskIds),
          normalized.envelopeRevision, normalized.workflowRevision,
          normalized.todoRevision, JSON.stringify(normalized.todoSnapshot),
          normalized.todoDigest, normalized.activeGoalTaskId, normalized.activeTodoId,
          normalized.executionPolicy, normalized.suspensionReason,
          JSON.stringify(normalized.lastOutcome), JSON.stringify(normalized.pendingCheckpointIds),
          now(), normalized.id, normalized.conversationId, normalized.customerId,
          normalized.expectedRevision,
        ]);
        if (db.getRowsModified() !== 1) {
          throw new BankApiError(409, 'agent_workflow_revision_conflict', 'Agent Workflow 版本已发生变化');
        }
      }
      const eventType = existing ? 'workflow.updated' : 'workflow.created';
      const safePayload = {
        status: normalized.status,
        activeGoalTaskId: normalized.activeGoalTaskId,
        activeTodoId: normalized.activeTodoId,
        todoRevision: normalized.todoRevision,
        todoDigest: normalized.todoDigest,
        suspensionReason: normalized.suspensionReason,
      };
      const eventId = 'wfe_' + crypto.createHash('sha256').update(
        `${normalized.id}\x1f${normalized.workflowRevision}\x1f${eventType}`,
      ).digest('hex').slice(0, 24);
      tx.run(`INSERT INTO bank_agent_workflow_events
        (id,business_workflow_id,workflow_revision,event_type,safe_payload_json,created_at)
        VALUES (?,?,?,?,?,?)`, [
        eventId, normalized.id, normalized.workflowRevision, eventType,
        JSON.stringify(safePayload), now(),
      ]);
      return mapAgentWorkflow(tx.queryOne(
        'SELECT * FROM bank_agent_workflows WHERE id = ? AND bound_conversation_id = ? AND customer_id = ?',
        [normalized.id, normalized.conversationId, normalized.customerId],
      ));
    });
  }

  function revokeWorkflowAuthorizationsTx(tx, workflow, timestamp) {
    tx.run(`UPDATE bank_agent_action_checkpoints
      SET status = 'CANCELLED', consumed_at = ?, updated_at = ?
      WHERE business_workflow_id = ? AND customer_id = ?
        AND status IN ('PENDING','AUTHORIZED')`, [
      timestamp, timestamp, workflow.id, workflow.customerId,
    ]);
    tx.run(`UPDATE bank_agent_capability_grants
      SET status = 'REVOKED', consumed_at = ?, updated_at = ?
      WHERE business_workflow_id = ? AND customer_id = ? AND status = 'ACTIVE'`, [
      timestamp, timestamp, workflow.id, workflow.customerId,
    ]);
  }

  function transitionWorkflowTx(tx, workflow, action, conversationState, options) {
    const opts = options || {};
    const timestamp = opts.timestamp || now();
    const nextRevision = workflow.workflowRevision + 1;
    let status;
    let suspensionReason;
    let todoSnapshot;
    let activeGoalTaskId;
    let activeTodoId;
    let contextState;
    let lastOutcome;
    let pendingCheckpointIds;
    if (action === 'SUSPEND') {
      status = 'suspended';
      suspensionReason = opts.suspensionReason || 'user_suspended';
      todoSnapshot = workflow.todoSnapshot;
      activeGoalTaskId = workflow.activeGoalTaskId;
      activeTodoId = workflow.activeTodoId;
      contextState = conversationState || workflow.conversationState;
      lastOutcome = workflow.lastOutcome;
      pendingCheckpointIds = opts.revokeAuthorizations ? [] : workflow.pendingCheckpointIds;
      if (opts.revokeAuthorizations) revokeWorkflowAuthorizationsTx(tx, workflow, timestamp);
    } else {
      todoSnapshot = workflow.todoSnapshot.map(todo => ({
        ...todo,
        status: todo.status === 'completed' ? 'completed' : 'cancelled',
      }));
      status = todoSnapshot.some(todo => todo.status === 'completed')
        ? 'partially_completed' : 'cancelled';
      suspensionReason = null;
      activeGoalTaskId = null;
      activeTodoId = null;
      contextState = null;
      lastOutcome = { status, controlAction: 'cancel' };
      pendingCheckpointIds = [];
      revokeWorkflowAuthorizationsTx(tx, workflow, timestamp);
    }
    const canonical = todoSnapshot.map(todo => ({
      dependsOn: [...todo.dependsOn].sort(), id: todo.id, status: todo.status,
    }));
    const todoDigest = crypto.createHash('sha256')
      .update(JSON.stringify(canonical)).digest('hex');
    tx.run(`UPDATE bank_agent_workflows SET
      status = ?, workflow_revision = ?, todo_snapshot_json = ?, todo_digest = ?,
      active_goal_task_id = ?, active_todo_id = ?, suspension_reason = ?,
      last_outcome_json = ?, pending_checkpoint_ids_json = ?,
      conversation_state_json = ?, updated_at = ?
      WHERE id = ? AND bound_conversation_id = ? AND customer_id = ? AND workflow_revision = ?`, [
      status, nextRevision, JSON.stringify(todoSnapshot), todoDigest,
      activeGoalTaskId, activeTodoId, suspensionReason,
      JSON.stringify(lastOutcome), JSON.stringify(pendingCheckpointIds),
      contextState ? JSON.stringify(contextState) : null, timestamp, workflow.id,
      workflow.boundConversationId, workflow.customerId, workflow.workflowRevision,
    ]);
    if (db.getRowsModified() !== 1) {
      throw new BankApiError(409, 'agent_workflow_revision_conflict', 'Agent Workflow revision changed');
    }
    tx.run(`INSERT INTO bank_agent_workflow_events
      (id,business_workflow_id,workflow_revision,event_type,safe_payload_json,created_at)
      VALUES (?,?,?,?,?,?)`, [
      'awfe_' + crypto.randomUUID(), workflow.id, nextRevision,
      action === 'SUSPEND' ? 'workflow.suspended' : 'workflow.cancelled',
      JSON.stringify({ status, todoRevision: workflow.todoRevision, suspensionReason }), timestamp,
    ]);
    return mapAgentWorkflow(tx.queryOne(
      'SELECT * FROM bank_agent_workflows WHERE id = ?', [workflow.id],
    ));
  }

  function suspendBoundWorkflowsTx(tx, conversationId, customerId, reason, timestamp) {
    const terminal = ['completed', 'partially_completed', 'cancelled', 'failed'];
    tx.queryAll(`SELECT * FROM bank_agent_workflows
      WHERE bound_conversation_id = ? AND customer_id = ?
        AND status NOT IN (?,?,?,?) ORDER BY id`, [
      conversationId, customerId, ...terminal,
    ]).map(mapAgentWorkflow).forEach(workflow => transitionWorkflowTx(
      tx, workflow, 'SUSPEND', workflow.conversationState, {
        timestamp, suspensionReason: reason, revokeAuthorizations: true,
      },
    ));
  }

  async function transitionAgentWorkflowControl(item) {
    const safeId = /^[A-Za-z0-9_.:-]{1,160}$/;
    if (!item || typeof item !== 'object' || Array.isArray(item)
      || ![item.id, item.conversationId, item.customerId].every(value => (
        typeof value === 'string' && safeId.test(value)
      ))
      || !['SUSPEND', 'CANCEL'].includes(item.action)
      || !Number.isInteger(item.expectedRevision) || item.expectedRevision < 1
      || !Object.prototype.hasOwnProperty.call(item, 'conversationState')) {
      throw new BankApiError(400, 'invalid_agent_workflow_control', 'Agent Workflow 控制请求无效');
    }
    let archivedState = null;
    if (item.action === 'SUSPEND') {
      if (!item.conversationState) {
        throw new BankApiError(400, 'invalid_agent_workflow_control', '暂停业务缺少可恢复上下文');
      }
      archivedState = validateConversationStateReplacement({
        conversationId: item.conversationId,
        customerId: item.customerId,
        state: item.conversationState,
      }).state;
    } else if (item.conversationState !== null) {
      throw new BankApiError(400, 'invalid_agent_workflow_control', '取消业务不能写入会话上下文');
    }
    return withTransaction(async tx => {
      const existing = mapAgentWorkflow(tx.queryOne(
        'SELECT * FROM bank_agent_workflows WHERE id = ? AND bound_conversation_id = ? AND customer_id = ?',
        [item.id, item.conversationId, item.customerId],
      ));
      if (!existing) {
        throw new BankApiError(404, 'agent_workflow_not_found', 'Agent Workflow 不存在');
      }
      if (existing.workflowRevision !== item.expectedRevision) {
        throw new BankApiError(409, 'agent_workflow_revision_conflict', 'Agent Workflow 版本已发生变化');
      }
      const terminal = new Set(['completed', 'partially_completed', 'cancelled', 'failed']);
      if (terminal.has(existing.status)
        || (item.action === 'SUSPEND' && existing.status === 'suspended')) {
        throw new BankApiError(409, 'agent_workflow_transition_invalid', '当前业务状态不允许该操作');
      }
      return transitionWorkflowTx(
        tx, existing, item.action, archivedState,
        { suspensionReason: 'user_suspended', revokeAuthorizations: item.action === 'CANCEL' },
      );
      
    });
  }

  async function archiveAgentWorkflowConversationState(item) {
    const safeId = /^[A-Za-z0-9_.:-]{1,160}$/;
    if (!item || typeof item !== 'object' || Array.isArray(item)
      || ![item.id, item.conversationId, item.customerId].every(value => (
        typeof value === 'string' && safeId.test(value)
      ))
      || !Number.isInteger(item.expectedRevision) || item.expectedRevision < 1) {
      throw new BankApiError(400, 'invalid_agent_workflow_control', 'Agent Workflow 上下文归档请求无效');
    }
    const state = validateConversationStateReplacement({
      conversationId: item.conversationId,
      customerId: item.customerId,
      state: item.conversationState,
    }).state;
    return withTransaction(async tx => {
      const existing = mapAgentWorkflow(tx.queryOne(
        'SELECT * FROM bank_agent_workflows WHERE id = ? AND bound_conversation_id = ? AND customer_id = ?',
        [item.id, item.conversationId, item.customerId],
      ));
      if (!existing) {
        throw new BankApiError(404, 'agent_workflow_not_found', 'Agent Workflow 不存在');
      }
      if (existing.workflowRevision !== item.expectedRevision) {
        throw new BankApiError(409, 'agent_workflow_revision_conflict', 'Agent Workflow 版本已发生变化');
      }
      if (['completed', 'partially_completed', 'cancelled', 'failed'].includes(existing.status)) {
        throw new BankApiError(409, 'agent_workflow_transition_invalid', '终态业务不能归档活动上下文');
      }
      tx.run(`UPDATE bank_agent_workflows SET conversation_state_json = ?, updated_at = ?
        WHERE id = ? AND bound_conversation_id = ? AND customer_id = ? AND workflow_revision = ?`, [
        JSON.stringify(state), now(), existing.id, existing.conversationId,
        existing.customerId, existing.workflowRevision,
      ]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'agent_workflow_revision_conflict', 'Agent Workflow 版本已发生变化');
      }
      tx.run(`INSERT OR IGNORE INTO bank_agent_workflow_events
        (id,business_workflow_id,workflow_revision,event_type,safe_payload_json,created_at)
        VALUES (?,?,?,?,?,?)`, [
        'awfe_' + crypto.randomUUID(), existing.id, existing.workflowRevision,
        'workflow.context_archived', JSON.stringify({ status: existing.status }), now(),
      ]);
      return mapAgentWorkflow(tx.queryOne(
        'SELECT * FROM bank_agent_workflows WHERE id = ?', [existing.id],
      ));
    });
  }

  async function createActionCheckpoint(item) {
    validateActionCheckpoint(item);
    return withTransaction(async tx => {
      const runRecord = mapAgentRun(tx.queryOne('SELECT * FROM bank_agent_runs WHERE id = ? AND customer_id = ?', [item.agentRunId, item.customerId]));
      if (!runRecord) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在或不属于当前客户');
      try {
        tx.run(`INSERT INTO bank_agent_action_checkpoints
          (id,agent_run_id,customer_id,business_workflow_id,task_id,capability_id,input_json,input_hash,summary_json,token_hash,idempotency_key,status,expires_at,consumed_at,result_json,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, [
          item.id, item.agentRunId, item.customerId,
          item.businessWorkflowId || null, item.taskId || null,
          item.capabilityId, JSON.stringify(item.input), item.inputHash,
          JSON.stringify(item.summary), item.tokenHash, item.idempotencyKey, 'PENDING', item.expiresAt,
          null, null, now(), now(),
        ]);
      } catch (error) {
        if (String(error && error.message).includes('UNIQUE constraint failed')) {
          throw new BankApiError(409, 'checkpoint_conflict', '确认检查点已存在');
        }
        throw error;
      }
      return mapActionCheckpointPublic(mapActionCheckpointInternal(tx.queryOne('SELECT * FROM bank_agent_action_checkpoints WHERE id = ?', [item.id])));
    });
  }

  async function consumeActionCheckpoint(id, result) {
    return withTransaction(async tx => {
      const current = mapActionCheckpointInternal(tx.queryOne('SELECT * FROM bank_agent_action_checkpoints WHERE id = ?', [id]));
      if (!current) throw new BankApiError(404, 'checkpoint_not_found', '确认检查点不存在');
      if (current.status !== 'PENDING') throw new BankApiError(409, 'checkpoint_consumed', '确认检查点已消费');
      tx.run(`UPDATE bank_agent_action_checkpoints
        SET status = 'CONSUMED', consumed_at = ?, result_json = ?, updated_at = ?
        WHERE id = ? AND status = 'PENDING'`, [now(), JSON.stringify(result || {}), now(), id]);
      if (db.getRowsModified() !== 1) throw new BankApiError(409, 'checkpoint_consumed', '确认检查点已消费');
      return mapActionCheckpointPublic(mapActionCheckpointInternal(tx.queryOne('SELECT * FROM bank_agent_action_checkpoints WHERE id = ?', [id])));
    });
  }

  async function claimActionCheckpoint(id, customerId) {
    return withTransaction(async tx => {
      const current = mapActionCheckpointInternal(tx.queryOne(
        'SELECT * FROM bank_agent_action_checkpoints WHERE id = ? AND customer_id = ?', [id, customerId]
      ));
      if (!current) throw new BankApiError(404, 'checkpoint_not_found', '确认检查点不存在');
      if (current.status === 'SUCCEEDED') return { replay: true, checkpoint: mapActionCheckpointPublic(current) };
      if (current.status === 'EXPIRED') throw new BankApiError(410, 'confirmation_expired', '确认已过期，请重新发起');
      if (current.status === 'CANCELLED') throw new BankApiError(409, 'confirmation_cancelled', '本次办理已取消，原确认凭证已失效');
      if (current.status === 'FAILED') throw new BankApiError(409, 'checkpoint_failed', '该确认操作执行失败，请重新发起');
      if (current.status !== 'PENDING') throw new BankApiError(409, 'confirmation_in_progress', '确认操作正在执行，请勿重复提交');
      tx.run(`UPDATE bank_agent_action_checkpoints SET status = 'EXECUTING', consumed_at = ?, updated_at = ?
        WHERE id = ? AND customer_id = ? AND status = 'PENDING'`, [now(), now(), id, customerId]);
      if (db.getRowsModified() !== 1) throw new BankApiError(409, 'confirmation_in_progress', '确认操作正在执行，请勿重复提交');
      return { replay: false, checkpoint: mapActionCheckpointInternal(tx.queryOne('SELECT * FROM bank_agent_action_checkpoints WHERE id = ?', [id])) };
    });
  }

  async function listPendingActionCheckpointsByConversation(conversationId, customerId, asOf) {
    const cutoff = typeof asOf === 'string' && Number.isFinite(Date.parse(asOf))
      ? asOf
      : now();
    return queryAll(db, `SELECT cp.* FROM bank_agent_action_checkpoints cp
      JOIN bank_agent_runs r ON r.id = cp.agent_run_id
      WHERE r.conversation_id = ? AND cp.customer_id = ?
        AND cp.status = 'PENDING' AND cp.expires_at > ?
      ORDER BY cp.created_at DESC`, [
      conversationId, customerId, cutoff,
    ]).map(mapActionCheckpointInternal);
  }

  async function completeActionCheckpoint(id, status, result) {
    if (!['SUCCEEDED', 'FAILED', 'EXPIRED'].includes(status)) {
      throw new BankApiError(400, 'invalid_checkpoint_status', '确认检查点目标状态无效');
    }
    return withTransaction(async tx => {
      const current = mapActionCheckpointInternal(tx.queryOne('SELECT * FROM bank_agent_action_checkpoints WHERE id = ?', [id]));
      if (!current) throw new BankApiError(404, 'checkpoint_not_found', '确认检查点不存在');
      const allowed = status === 'EXPIRED' ? ['PENDING'] : ['EXECUTING', 'AUTHORIZED'];
      if (!allowed.includes(current.status)) throw new BankApiError(409, 'checkpoint_state_conflict', '确认检查点状态已变化');
      tx.run(`UPDATE bank_agent_action_checkpoints SET status = ?, result_json = ?, updated_at = ?
        WHERE id = ? AND status = ?`, [status, JSON.stringify(result || {}), now(), id, current.status]);
      if (db.getRowsModified() !== 1) throw new BankApiError(409, 'checkpoint_state_conflict', '确认检查点状态已变化');
      return mapActionCheckpointPublic(mapActionCheckpointInternal(tx.queryOne('SELECT * FROM bank_agent_action_checkpoints WHERE id = ?', [id])));
    });
  }

  async function authorizeWorkflowCheckpoint(id, customerId, grant) {
    validateCapabilityGrant(grant);
    return withTransaction(async tx => {
      const current = mapActionCheckpointInternal(tx.queryOne(
        'SELECT * FROM bank_agent_action_checkpoints WHERE id = ? AND customer_id = ?',
        [id, customerId],
      ));
      if (!current) throw new BankApiError(404, 'checkpoint_not_found', '确认检查点不存在');
      if (!current.businessWorkflowId || !current.taskId
        || current.businessWorkflowId !== grant.businessWorkflowId
        || current.taskId !== grant.taskId
        || current.capabilityId !== grant.capabilityId
        || current.inputHash !== grant.inputHash
        || current.id !== grant.checkpointId) {
        throw new BankApiError(409, 'capability_grant_binding_conflict', '能力授权绑定不一致');
      }
      const replay = mapCapabilityGrant(tx.queryOne(
        'SELECT * FROM bank_agent_capability_grants WHERE checkpoint_id = ?', [id],
      ));
      if (replay) return { replay: true, grant: replay };
      if (current.status !== 'PENDING') {
        throw new BankApiError(409, 'checkpoint_state_conflict', '确认检查点状态已变化');
      }
      tx.run(`UPDATE bank_agent_action_checkpoints SET status = 'AUTHORIZED', consumed_at = ?, updated_at = ?
        WHERE id = ? AND customer_id = ? AND status = 'PENDING'`, [now(), now(), id, customerId]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'checkpoint_state_conflict', '确认检查点状态已变化');
      }
      tx.run(`INSERT INTO bank_agent_capability_grants
        (id,customer_id,business_workflow_id,task_id,capability_id,input_hash,checkpoint_id,status,expires_at,consumed_at,receipt_json,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`, [
        grant.id, customerId, grant.businessWorkflowId, grant.taskId,
        grant.capabilityId, grant.inputHash, grant.checkpointId, 'ACTIVE',
        grant.expiresAt, null, null, now(), now(),
      ]);
      return { replay: false, grant: mapCapabilityGrant(tx.queryOne(
        'SELECT * FROM bank_agent_capability_grants WHERE id = ?', [grant.id],
      )) };
    });
  }

  async function claimCapabilityGrant(id, binding) {
    return withTransaction(async tx => {
      const current = mapCapabilityGrant(tx.queryOne(
        'SELECT * FROM bank_agent_capability_grants WHERE id = ? AND customer_id = ?',
        [id, binding.customerId],
      ));
      if (!current || current.businessWorkflowId !== binding.businessWorkflowId
        || current.taskId !== binding.taskId || current.capabilityId !== binding.capabilityId) {
        throw new BankApiError(404, 'capability_grant_not_found', '能力授权不存在');
      }
      if (current.status === 'CONSUMED') return { replay: true, grant: current };
      if (current.status !== 'ACTIVE') {
        throw new BankApiError(409, 'capability_grant_state_conflict', '能力授权正在使用或已失效');
      }
      if (Date.parse(current.expiresAt) <= Date.parse(now())) {
        tx.run(`UPDATE bank_agent_capability_grants SET status = 'EXPIRED', updated_at = ?
          WHERE id = ? AND status = 'ACTIVE'`, [now(), id]);
        throw new BankApiError(410, 'capability_grant_expired', '能力授权已过期');
      }
      tx.run(`UPDATE bank_agent_capability_grants SET status = 'CLAIMED', updated_at = ?
        WHERE id = ? AND status = 'ACTIVE'`, [now(), id]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'capability_grant_state_conflict', '能力授权状态已变化');
      }
      return { replay: false, grant: mapCapabilityGrant(tx.queryOne(
        'SELECT * FROM bank_agent_capability_grants WHERE id = ?', [id],
      )) };
    });
  }

  async function completeCapabilityGrant(id, status, receipt) {
    if (!['CONSUMED', 'FAILED'].includes(status)) {
      throw new BankApiError(400, 'invalid_capability_grant_status', '能力授权目标状态无效');
    }
    return withTransaction(async tx => {
      tx.run(`UPDATE bank_agent_capability_grants
        SET status = ?, consumed_at = ?, receipt_json = ?, updated_at = ?
        WHERE id = ? AND status = 'CLAIMED'`, [
        status, now(), JSON.stringify(receipt || {}), now(), id,
      ]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'capability_grant_state_conflict', '能力授权状态已变化');
      }
      return mapCapabilityGrant(tx.queryOne(
        'SELECT * FROM bank_agent_capability_grants WHERE id = ?', [id],
      ));
    });
  }

  async function listResumableAgentWorkflowRecordsInternal(customerId) {
    requireNonEmptyString(customerId, 'customerId');
    const candidates = queryAll(db, `SELECT * FROM bank_agent_workflows
      WHERE customer_id = ? AND status = 'suspended'
      ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT ?`, [
      customerId, MAX_RESUMABLE_WORKFLOW_CHOICES * 8,
    ]).map(mapAgentWorkflow);
    return selectResumableWorkflowRecords(candidates, MAX_RESUMABLE_WORKFLOW_CHOICES);
  }

  async function listSmartCompanionCandidates(customerId) {
    requireNextActionId(customerId, 'customerId');
    return queryAll(db, `SELECT workflow.*, binding.agent_session_id,
        recommendation.scenario_version, recommendation.safe_payload_json
      FROM bank_agent_workflows workflow
      INNER JOIN bank_next_action_workflow_bindings binding
        ON binding.business_workflow_id = workflow.id AND binding.customer_id = workflow.customer_id
      INNER JOIN bank_next_action_recommendations recommendation
        ON recommendation.id = binding.recommendation_id AND recommendation.customer_id = workflow.customer_id
      WHERE workflow.customer_id = ? AND binding.business_workflow_id IS NOT NULL
        AND binding.status = 'started'
      ORDER BY workflow.updated_at DESC, workflow.id DESC`, [customerId]).flatMap(row => {
      let payload;
      try { payload = JSON.parse(row.safe_payload_json); } catch (_) { return []; }
      if (!payload || typeof payload.workflowTemplateId !== 'string') return [];
      const workflow = mapAgentWorkflow(row);
      return [{
        businessWorkflowId: workflow.id,
        sessionId: workflow.boundConversationId,
        customerId: workflow.customerId,
        workflowTemplateId: payload.workflowTemplateId,
        workflowTemplateVersion: row.scenario_version,
        status: workflow.status,
        todoSnapshot: workflow.todoSnapshot,
        updatedAt: workflow.updatedAt,
      }];
    });
  }

  async function listResumableAgentWorkflows(customerId, intentGraph) {
    const workflows = await listResumableAgentWorkflowRecordsInternal(customerId);
    return workflows.map(workflow => projectResumableWorkflowSummary(workflow, intentGraph));
  }

  async function bindAgentWorkflowToSession(item) {
    const safeId = /^[A-Za-z0-9_.:-]{1,160}$/;
    if (!item || typeof item !== 'object' || Array.isArray(item)
      || Object.keys(item).length !== 4
      || ![item.id, item.customerId, item.conversationId].every(value => (
        typeof value === 'string' && safeId.test(value)
      ))
      || !Number.isInteger(item.expectedRevision) || item.expectedRevision < 1) {
      throw new BankApiError(400, 'invalid_agent_workflow_binding', 'Agent Workflow binding is invalid');
    }
    const outcome = await withTransaction(async tx => {
      const currentTime = transactionTimestamp();
      const session = mapAgentSession(tx.queryOne(
        'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
        [item.conversationId, item.customerId],
      ));
      if (!session) {
        throw new BankApiError(404, 'agent_session_not_found', 'Agent Session not found');
      }
      if (session.status !== 'active') {
        if (session.status === 'expired') {
          throw new BankApiError(409, 'agent_session_expired', 'Agent Session expired');
        }
        throw new BankApiError(409, 'agent_session_closed', 'Agent Session is not active');
      }
      const expired = expireAgentSessionIfDue(tx, session, currentTime);
      if (expired) return { expired };
      const workflow = mapAgentWorkflow(tx.queryOne(
        'SELECT * FROM bank_agent_workflows WHERE id = ? AND customer_id = ?',
        [item.id, item.customerId],
      ));
      if (!workflow) {
        throw new BankApiError(404, 'agent_workflow_not_found', 'Agent Workflow not found');
      }
      if (workflow.workflowRevision !== item.expectedRevision) {
        throw new BankApiError(409, 'agent_workflow_revision_conflict', 'Agent Workflow revision changed');
      }
      if (workflow.status !== 'suspended') {
        throw new BankApiError(409, 'agent_workflow_transition_invalid', 'Agent Workflow is not resumable');
      }
      const nextRevision = workflow.workflowRevision + 1;
      const timestamp = currentTime.iso;
      tx.run(`UPDATE bank_agent_workflows
        SET bound_conversation_id = ?, workflow_revision = ?, updated_at = ?
        WHERE id = ? AND customer_id = ? AND status = 'suspended' AND workflow_revision = ?`, [
        item.conversationId, nextRevision, timestamp,
        workflow.id, item.customerId, workflow.workflowRevision,
      ]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'agent_workflow_revision_conflict', 'Agent Workflow revision changed');
      }
      tx.run(`INSERT INTO bank_agent_workflow_events
        (id,business_workflow_id,workflow_revision,event_type,safe_payload_json,created_at)
        VALUES (?,?,?,?,?,?)`, [
        'awfe_' + crypto.randomUUID(), workflow.id, nextRevision, 'workflow.rebound',
        JSON.stringify({ status: 'suspended' }), timestamp,
      ]);
      return { workflow: mapAgentWorkflow(tx.queryOne(
        'SELECT * FROM bank_agent_workflows WHERE id = ? AND customer_id = ?',
        [workflow.id, item.customerId],
      )) };
    });
    if (outcome.expired) {
      throw new BankApiError(409, 'agent_session_expired', 'Agent Session expired');
    }
    return outcome.workflow;
  }

  async function bindAgentWorkflowToSessionIdempotent(item) {
    validateAtomicWorkflowBinding(item);
    const outcome = await withTransaction(async tx => {
      const replay = requireIdempotencyReplayTx(
        tx, item, ATOMIC_IDEMPOTENCY_OPERATIONS.workflowBinding,
      );
      if (replay) return { response: replay };
      const currentTime = transactionTimestamp();
      const session = mapAgentSession(tx.queryOne(
        'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
        [item.conversationId, item.customerId],
      ));
      if (!session) {
        throw new BankApiError(404, 'agent_session_not_found', 'Agent Session not found');
      }
      if (session.status !== 'active') {
        if (session.status === 'expired') {
          throw new BankApiError(409, 'agent_session_expired', 'Agent Session expired');
        }
        throw new BankApiError(409, 'agent_session_closed', 'Agent Session is not active');
      }
      const expired = expireAgentSessionIfDue(tx, session, currentTime);
      if (expired) return { expired };
      const workflow = mapAgentWorkflow(tx.queryOne(
        'SELECT * FROM bank_agent_workflows WHERE id = ? AND customer_id = ?',
        [item.id, item.customerId],
      ));
      if (!workflow) {
        throw new BankApiError(404, 'agent_workflow_not_found', 'Agent Workflow not found');
      }
      if (workflow.workflowRevision !== item.expectedRevision) {
        throw new BankApiError(409, 'agent_workflow_revision_conflict', 'Agent Workflow revision changed');
      }
      if (workflow.status !== 'suspended') {
        throw new BankApiError(409, 'agent_workflow_transition_invalid', 'Agent Workflow is not resumable');
      }
      const nextRevision = workflow.workflowRevision + 1;
      tx.run(`UPDATE bank_agent_workflows
        SET bound_conversation_id = ?, workflow_revision = ?, updated_at = ?
        WHERE id = ? AND customer_id = ? AND status = 'suspended' AND workflow_revision = ?`, [
        item.conversationId, nextRevision, currentTime.iso,
        workflow.id, item.customerId, workflow.workflowRevision,
      ]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'agent_workflow_revision_conflict', 'Agent Workflow revision changed');
      }
      tx.run(`INSERT INTO bank_agent_workflow_events
        (id,business_workflow_id,workflow_revision,event_type,safe_payload_json,created_at)
        VALUES (?,?,?,?,?,?)`, [
        'awfe_' + crypto.randomUUID(), workflow.id, nextRevision, 'workflow.rebound',
        JSON.stringify({ status: 'suspended' }), currentTime.iso,
      ]);
      const response = {
        businessWorkflowId: workflow.id,
        status: 'suspended',
        workflowRevision: nextRevision,
      };
      await faultInjector('agent-session.bind.before-idempotency-save', {
        sessionId: item.conversationId, customerId: item.customerId,
      });
      saveIdempotencyResponseTx(
        tx, item, response, ATOMIC_IDEMPOTENCY_OPERATIONS.workflowBinding,
      );
      return { response };
    }, { durable: true });
    if (outcome.expired) {
      throw new BankApiError(409, 'agent_session_expired', 'Agent Session expired');
    }
    return outcome.response;
  }

  // 已有决策记录的推荐 ID（用于服务层回填：已决策的候选不得占用可见窗口）
  async function listNextActionDecidedRecommendationIds(customerId, recommendationIds) {
    requireNextActionId(customerId, 'customerId');
    if (!Array.isArray(recommendationIds) || recommendationIds.length > 64
      || recommendationIds.some(id => {
        try { requireNextActionId(id, 'recommendationId'); return false; } catch (error) { return true; }
      })) {
      throw new BankApiError(400, 'next_action_recommendation_invalid', 'Recommendation id list is invalid');
    }
    const uniqueIds = Array.from(new Set(recommendationIds));
    if (!uniqueIds.length) return [];
    const decided = new Set();
    for (let offset = 0; offset < uniqueIds.length; offset += 32) {
      const chunk = uniqueIds.slice(offset, offset + 32);
      const placeholders = chunk.map(() => '?').join(',');
      for (const row of queryAll(db,
        `SELECT DISTINCT recommendation_id FROM bank_next_action_decisions
         WHERE customer_id = ? AND recommendation_id IN (${placeholders})`,
        [customerId, ...chunk])) {
        decided.add(row.recommendation_id);
      }
    }
    return Array.from(decided);
  }

  async function replaceNextActionRecommendations(customerId, recommendations) {
    requireNextActionId(customerId, 'customerId');
    if (!Array.isArray(recommendations) || recommendations.length > 3) {
      throw new BankApiError(400, 'next_action_recommendation_invalid', 'Recommendation batch is invalid');
    }
    recommendations.forEach(validateNextActionRecommendation);
    if (recommendations.some(item => item.status !== 'generated')) {
      throw new BankApiError(400, 'next_action_recommendation_invalid', 'New recommendations must be generated');
    }
    const scopes = new Set(recommendations.map(item => `${item.surface}\u0000${item.channel || ''}`));
    if (scopes.size > 1) {
      throw new BankApiError(400, 'next_action_recommendation_invalid', 'Recommendation batch must share a surface and channel');
    }
    return withTransaction(async tx => {
      let writtenCount = 0;
      if (!tx.queryOne('SELECT id FROM bank_customers WHERE id = ?', [customerId])) {
        throw new BankApiError(404, 'customer_not_found', 'Customer does not exist');
      }
      if (recommendations.length) {
        const scope = recommendations[0];
        tx.run(`UPDATE bank_next_action_recommendations
          SET status = 'expired', updated_at = ?
          WHERE customer_id = ? AND surface = ? AND COALESCE(channel, '') = COALESCE(?, '')
            AND status IN ('generated', 'shown')`, [now(), customerId, scope.surface, scope.channel]);
      }
      for (const item of recommendations) {
        const payload = projectNextActionPayload(item);
        const existing = tx.queryOne('SELECT * FROM bank_next_action_recommendations WHERE id = ?', [item.recommendationId]);
        if (existing) {
          if (existing.customer_id !== customerId) {
            throw new BankApiError(409, 'recommendation_conflict', 'Recommendation already exists');
          }
          const decided = tx.queryOne(
            'SELECT id FROM bank_next_action_decisions WHERE recommendation_id = ? LIMIT 1',
            [item.recommendationId],
          );
          if (decided) continue;
          if (existing.status !== 'expired') {
            throw new BankApiError(409, 'recommendation_conflict', 'Recommendation already exists');
          }
          tx.run(`UPDATE bank_next_action_recommendations SET
            scenario_id = ?, scenario_version = ?, recommendation_type = ?, surface = ?, channel = ?, status = ?,
            deterministic_score = ?, final_rank = ?, reason_codes_json = ?, reason_text = ?, context_refs_json = ?,
            context_digest = ?, safe_payload_json = ?, outcome_json = NULL, generated_at = ?, expires_at = ?,
            shown_at = NULL, accepted_at = NULL, completed_at = NULL, updated_at = ?
            WHERE id = ? AND customer_id = ? AND status = 'expired'`, [
            item.scenarioId, item.scenarioVersion, item.type, item.surface, item.channel, item.status,
            item.score, item.rank, JSON.stringify(item.reasonCodes), item.reasonText,
            JSON.stringify(item.contextRefs), item.contextDigest, JSON.stringify(payload),
            item.generatedAt, item.expiresAt, now(), item.recommendationId, customerId,
          ]);
          writtenCount += 1;
          continue;
        }
        try {
          tx.run(`INSERT INTO bank_next_action_recommendations
            (id,customer_id,scenario_id,scenario_version,recommendation_type,surface,channel,status,
             deterministic_score,final_rank,reason_codes_json,reason_text,context_refs_json,context_digest,
             safe_payload_json,outcome_json,generated_at,expires_at,shown_at,accepted_at,completed_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, [
            item.recommendationId, customerId, item.scenarioId, item.scenarioVersion, item.type,
            item.surface, item.channel, item.status, item.score, item.rank,
            JSON.stringify(item.reasonCodes), item.reasonText, JSON.stringify(item.contextRefs),
            item.contextDigest, JSON.stringify(payload), null, item.generatedAt, item.expiresAt,
            null, null, null, now(),
          ]);
          writtenCount += 1;
        } catch (error) {
          if (String(error && error.message).includes('UNIQUE constraint failed')) {
            throw new BankApiError(409, 'recommendation_conflict', 'Recommendation already exists');
          }
          throw error;
        }
      }
      return { count: writtenCount };
    });
  }

  async function listNextActionRecommendations(customerId, options) {
    requireNextActionId(customerId, 'customerId');
    validateNextActionListOptions(options);
    return withTransaction(async tx => {
      const timestamp = now();
      tx.run(`UPDATE bank_next_action_recommendations
        SET status = 'expired', updated_at = ?
        WHERE customer_id = ? AND surface = ? AND COALESCE(channel, '') = COALESCE(?, '')
          AND status IN ('generated', 'shown') AND expires_at <= ?`, [
        timestamp, customerId, options.surface, options.channel, timestamp,
      ]);
      const rows = tx.queryAll(`SELECT * FROM bank_next_action_recommendations
        WHERE customer_id = ? AND surface = ? AND COALESCE(channel, '') = COALESCE(?, '')
          AND status IN ('generated', 'shown') AND expires_at > ?
        ORDER BY final_rank, id LIMIT ?`, [
        customerId, options.surface, options.channel, timestamp, options.limit,
      ]);
      const generatedIds = rows.filter(row => row.status === 'generated').map(row => row.id);
      for (const id of generatedIds) {
        transitionNextActionRow(tx, id, customerId, 'generated', 'shown', timestamp, { shownAt: timestamp });
      }
      return {
        items: rows.map(row => projectNextActionRecommendation({
          ...row,
          status: row.status === 'generated' ? 'shown' : row.status,
          shown_at: row.status === 'generated' ? timestamp : row.shown_at,
        })),
      };
    });
  }

  async function getNextActionRecommendation(recommendationId, customerId) {
    requireNextActionId(recommendationId, 'recommendationId');
    requireNextActionId(customerId, 'customerId');
    return projectNextActionRecommendation(queryOne(
      db,
      'SELECT * FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
      [recommendationId, customerId],
    ));
  }

  async function getNextActionRecommendationInternal(recommendationId, customerId) {
    requireNextActionId(recommendationId, 'recommendationId');
    requireNextActionId(customerId, 'customerId');
    const row = queryOne(
      db,
      'SELECT * FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
      [recommendationId, customerId],
    );
    if (!row) return null;
    return {
      ...projectNextActionRecommendation(row),
      contextRefs: JSON.parse(row.context_refs_json),
      contextDigest: row.context_digest,
    };
  }

  async function recordNextActionDecision(input) {
    validateNextActionDecision(input);
    const outcome = await withTransaction(async tx => {
      const existing = tx.queryOne(`SELECT decision.*, recommendation.status AS current_status
        FROM bank_next_action_decisions decision
        INNER JOIN bank_next_action_recommendations recommendation ON recommendation.id = decision.recommendation_id
        WHERE decision.customer_id = ? AND decision.idempotency_key = ?`, [
        input.customerId, input.idempotencyKey,
      ]);
      if (existing) {
        if (existing.request_hash !== input.requestHash) {
          throw new BankApiError(409, 'idempotency_conflict', 'The idempotency key was reused for another request');
        }
        return projectNextActionDecision(existing);
      }
      const recommendation = tx.queryOne(
        'SELECT * FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
        [input.recommendationId, input.customerId],
      );
      if (!recommendation) {
        throw new BankApiError(404, 'recommendation_not_found', 'Recommendation does not exist');
      }
      const timestamp = now();
      if (Date.parse(recommendation.expires_at) <= Date.parse(timestamp)) {
        if (['generated', 'shown', 'accepted'].includes(recommendation.status)) {
          transitionNextActionRow(
            tx, input.recommendationId, input.customerId, recommendation.status, 'expired', timestamp,
          );
        }
        return { expired: true };
      }
      const targetStatus = NEXT_ACTION_DECISION_STATUS[input.decision];
      assertNextActionTransition(recommendation.status, targetStatus);
      transitionNextActionRow(
        tx, input.recommendationId, input.customerId, recommendation.status, targetStatus, timestamp,
        targetStatus === 'accepted' ? { acceptedAt: timestamp } : {},
      );
      const id = 'nad_' + crypto.randomUUID();
      tx.run(`INSERT INTO bank_next_action_decisions
        (id,recommendation_id,customer_id,decision,snooze_until,feedback_code,idempotency_key,request_hash,created_at)
        VALUES (?,?,?,?,?,?,?,?,?)`, [
        id, input.recommendationId, input.customerId, input.decision,
        input.snoozeUntil || null, input.feedbackCode || null,
        input.idempotencyKey, input.requestHash, timestamp,
      ]);
      return projectNextActionDecision({
        id, recommendation_id: input.recommendationId, decision: input.decision,
        snooze_until: input.snoozeUntil || null, feedback_code: input.feedbackCode || null,
        created_at: timestamp, current_status: targetStatus,
      });
    });
    if (outcome && outcome.expired) {
      throw new BankApiError(409, 'recommendation_expired', 'Recommendation has expired');
    }
    return outcome;
  }

  async function bindNextActionWorkflow(input) {
    validateNextActionBinding(input);
    return withTransaction(async tx => {
      const recommendation = tx.queryOne(
        'SELECT * FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
        [input.recommendationId, input.customerId],
      );
      if (!recommendation) {
        throw new BankApiError(404, 'recommendation_not_found', 'Recommendation does not exist');
      }
      const existing = tx.queryOne(
        'SELECT * FROM bank_next_action_workflow_bindings WHERE recommendation_id = ? AND customer_id = ?',
        [input.recommendationId, input.customerId],
      );
      if (existing) {
        if (!nextActionBindingMatches(existing, input)) {
          throw new BankApiError(409, 'recommendation_binding_conflict', 'Recommendation is bound to another workflow');
        }
        return projectNextActionBinding(existing, recommendation.status);
      }
      assertNextActionTransition(recommendation.status, 'started');
      const timestamp = now();
      transitionNextActionRow(
        tx, input.recommendationId, input.customerId, recommendation.status, 'started', timestamp,
      );
      tx.run(`INSERT INTO bank_next_action_workflow_bindings
        (recommendation_id,customer_id,agent_session_id,agent_run_id,business_workflow_id,goal_id,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)`, [
        input.recommendationId, input.customerId, input.agentSessionId, input.agentRunId,
        input.businessWorkflowId || null, input.goalId, 'started', timestamp, timestamp,
      ]);
      return projectNextActionBinding({
        recommendation_id: input.recommendationId, agent_session_id: input.agentSessionId,
        agent_run_id: input.agentRunId, business_workflow_id: input.businessWorkflowId || null,
        goal_id: input.goalId, status: 'started', created_at: timestamp, updated_at: timestamp,
      }, 'started');
    });
  }

  async function recoverInterruptedNextActionStart(recommendationId, customerId, minimumAgeMs) {
    requireNextActionId(recommendationId, 'recommendationId');
    requireNextActionId(customerId, 'customerId');
    if (!Number.isSafeInteger(minimumAgeMs) || minimumAgeMs < 10_000 || minimumAgeMs > 30 * 60_000) {
      throw new BankApiError(400, 'next_action_input_invalid', 'minimumAgeMs is invalid');
    }
    return withTransaction(async tx => {
      const recommendation = tx.queryOne(
        'SELECT * FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
        [recommendationId, customerId],
      );
      if (!recommendation || recommendation.status !== 'started') return null;
      const binding = tx.queryOne(
        'SELECT * FROM bank_next_action_workflow_bindings WHERE recommendation_id = ? AND customer_id = ?',
        [recommendationId, customerId],
      );
      if (!binding || binding.business_workflow_id) return null;

      const run = tx.queryOne(
        'SELECT * FROM bank_agent_runs WHERE id = ? AND customer_id = ?',
        [binding.agent_run_id, customerId],
      );
      const timestamp = now();
      const runAgeMs = run ? Date.parse(timestamp) - Date.parse(run.updated_at) : Number.POSITIVE_INFINITY;
      const canFailRun = run && AGENT_RUN_TRANSITIONS[run.status]
        && AGENT_RUN_TRANSITIONS[run.status].includes('failed');
      const recoverable = !run || run.status === 'failed'
        || (canFailRun && Number.isFinite(runAgeMs) && runAgeMs >= minimumAgeMs);
      if (!recoverable) return null;

      if (run && run.status !== 'failed') {
        tx.run(`UPDATE bank_agent_runs
          SET status = 'failed', trace_capture_status = 'incomplete', updated_at = ?
          WHERE id = ? AND customer_id = ? AND status = ?`, [
          timestamp, run.id, customerId, run.status,
        ]);
        if (db.getRowsModified() !== 1) return null;
      }
      tx.run(`DELETE FROM bank_next_action_workflow_bindings
        WHERE recommendation_id = ? AND customer_id = ?
          AND business_workflow_id IS NULL AND agent_run_id = ?`, [
        recommendationId, customerId, binding.agent_run_id,
      ]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'recommendation_recovery_conflict', 'Recommendation binding changed concurrently');
      }
      tx.run(`UPDATE bank_next_action_recommendations
        SET status = 'accepted', updated_at = ?
        WHERE id = ? AND customer_id = ? AND status = 'started'`, [
        timestamp, recommendationId, customerId,
      ]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'recommendation_recovery_conflict', 'Recommendation status changed concurrently');
      }
      const recovered = tx.queryOne(
        'SELECT * FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
        [recommendationId, customerId],
      );
      return {
        ...projectNextActionRecommendation(recovered),
        contextRefs: JSON.parse(recovered.context_refs_json),
        contextDigest: recovered.context_digest,
      };
    });
  }

  // 办理失败回退（started→accepted）：工作流以失败/放弃类终态收场时，推荐回到
  // 可重试状态而非误标"已完成"；失败原因写 outcome 供展示。用户也可 dismiss 永久关闭。
  async function failNextActionRecommendation(recommendationId, customerId, outcome) {
    requireNextActionId(recommendationId, 'recommendationId');
    requireNextActionId(customerId, 'customerId');
    validateNextActionOutcome(outcome);
    return withTransaction(async tx => {
      const recommendation = tx.queryOne(
        'SELECT * FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
        [recommendationId, customerId],
      );
      if (!recommendation) return null;
      if (recommendation.status === 'accepted') return projectNextActionRecommendation(recommendation);
      assertNextActionTransition(recommendation.status, 'accepted');
      const timestamp = now();
      tx.run(`UPDATE bank_next_action_recommendations
        SET status = 'accepted', outcome_json = ?, updated_at = ?
        WHERE id = ? AND customer_id = ? AND status = ?`, [
        JSON.stringify(outcome), timestamp, recommendationId, customerId, recommendation.status,
      ]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'recommendation_transition_invalid', 'Recommendation status changed concurrently');
      }
      tx.run(`UPDATE bank_next_action_workflow_bindings SET status = 'failed', updated_at = ?
        WHERE recommendation_id = ? AND customer_id = ? AND status = 'started'`, [
        timestamp, recommendationId, customerId,
      ]);
      return projectNextActionRecommendation(tx.queryOne(
        'SELECT * FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
        [recommendationId, customerId],
      ));
    });
  }

  async function attachNextActionBusinessWorkflow(agentRunId, customerId, businessWorkflowId) {
    requireNextActionId(agentRunId, 'agentRunId');
    requireNextActionId(customerId, 'customerId');
    requireNextActionId(businessWorkflowId, 'businessWorkflowId');
    return withTransaction(async tx => {
      const binding = tx.queryOne(`SELECT binding.*, recommendation.status AS recommendation_status
        FROM bank_next_action_workflow_bindings binding
        INNER JOIN bank_next_action_recommendations recommendation
          ON recommendation.id = binding.recommendation_id
        WHERE binding.agent_run_id = ? AND binding.customer_id = ?`, [agentRunId, customerId]);
      if (!binding) return null;
      if (binding.business_workflow_id && binding.business_workflow_id !== businessWorkflowId) {
        throw new BankApiError(409, 'recommendation_binding_conflict', 'Recommendation is bound to another workflow');
      }
      if (!binding.business_workflow_id) {
        tx.run(`UPDATE bank_next_action_workflow_bindings
          SET business_workflow_id = ?, updated_at = ?
          WHERE recommendation_id = ? AND customer_id = ? AND business_workflow_id IS NULL`, [
          businessWorkflowId, now(), binding.recommendation_id, customerId,
        ]);
      }
      return projectNextActionBinding({
        ...binding,
        business_workflow_id: businessWorkflowId,
        updated_at: now(),
      }, binding.recommendation_status);
    });
  }

  async function completeNextActionRecommendation(recommendationId, customerId, outcome) {
    requireNextActionId(recommendationId, 'recommendationId');
    requireNextActionId(customerId, 'customerId');
    validateNextActionOutcome(outcome);
    return withTransaction(async tx => {
      const recommendation = tx.queryOne(
        'SELECT * FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
        [recommendationId, customerId],
      );
      if (!recommendation) return null;
      if (recommendation.status === 'completed') {
        const existingOutcome = recommendation.outcome_json ? JSON.parse(recommendation.outcome_json) : {};
        if (JSON.stringify(existingOutcome) !== JSON.stringify(outcome)) {
          throw new BankApiError(409, 'recommendation_transition_invalid', 'Completed recommendation outcome cannot change');
        }
        return projectNextActionRecommendation(recommendation);
      }
      assertNextActionTransition(recommendation.status, 'completed');
      const timestamp = now();
      tx.run(`UPDATE bank_next_action_recommendations
        SET status = 'completed', outcome_json = ?, completed_at = ?, updated_at = ?
        WHERE id = ? AND customer_id = ? AND status = ?`, [
        JSON.stringify(outcome), timestamp, timestamp, recommendationId, customerId, recommendation.status,
      ]);
      if (db.getRowsModified() !== 1) {
        throw new BankApiError(409, 'recommendation_transition_invalid', 'Recommendation status changed concurrently');
      }
      tx.run(`UPDATE bank_next_action_workflow_bindings SET status = 'completed', updated_at = ?
        WHERE recommendation_id = ? AND customer_id = ?`, [timestamp, recommendationId, customerId]);
      return projectNextActionRecommendation(tx.queryOne(
        'SELECT * FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
        [recommendationId, customerId],
      ));
    });
  }

  async function getNextActionPreference(customerId) {
    requireNextActionId(customerId, 'customerId');
    const row = queryOne(db, 'SELECT * FROM bank_next_action_preferences WHERE customer_id = ?', [customerId]);
    return row ? {
      personalizationEnabled: Boolean(row.personalization_enabled), updatedAt: row.updated_at,
    } : { personalizationEnabled: true, updatedAt: null };
  }

  async function setNextActionPersonalization(customerId, enabled) {
    requireNextActionId(customerId, 'customerId');
    if (typeof enabled !== 'boolean') {
      throw new BankApiError(400, 'next_action_preference_invalid', 'Personalization preference must be boolean');
    }
    return withTransaction(async tx => {
      if (!tx.queryOne('SELECT id FROM bank_customers WHERE id = ?', [customerId])) {
        throw new BankApiError(404, 'customer_not_found', 'Customer does not exist');
      }
      const timestamp = now();
      tx.run(`INSERT INTO bank_next_action_preferences (customer_id,personalization_enabled,updated_at)
        VALUES (?,?,?) ON CONFLICT(customer_id) DO UPDATE SET
        personalization_enabled = excluded.personalization_enabled, updated_at = excluded.updated_at`, [
        customerId, enabled ? 1 : 0, timestamp,
      ]);
      return { personalizationEnabled: enabled, updatedAt: timestamp };
    });
  }

  async function disableNextActionPersonalization(input) {
    validateDisableNextActionPersonalization(input);
    return withTransaction(async tx => {
      const existing = tx.queryOne(`SELECT * FROM bank_next_action_decisions
        WHERE customer_id = ? AND idempotency_key = ?`, [input.customerId, input.idempotencyKey]);
      if (existing) {
        if (existing.request_hash !== input.requestHash) {
          throw new BankApiError(409, 'idempotency_conflict', 'The idempotency key was reused for another request');
        }
        return projectNextActionDecision(existing);
      }
      const recommendation = tx.queryOne(
        'SELECT * FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
        [input.recommendationId, input.customerId],
      );
      if (!recommendation) {
        throw new BankApiError(404, 'recommendation_not_found', 'Recommendation does not exist');
      }
      if (!['generated', 'shown'].includes(recommendation.status)) {
        throw new BankApiError(409, 'recommendation_transition_invalid', 'Recommendation can no longer change personalization');
      }
      const timestamp = now();
      tx.run(`INSERT INTO bank_next_action_preferences (customer_id,personalization_enabled,updated_at)
        VALUES (?,0,?) ON CONFLICT(customer_id) DO UPDATE SET
        personalization_enabled = 0, updated_at = excluded.updated_at`, [input.customerId, timestamp]);
      transitionNextActionRow(
        tx, input.recommendationId, input.customerId, recommendation.status, 'dismissed', timestamp,
      );
      tx.run(`UPDATE bank_next_action_recommendations SET status = 'expired', updated_at = ?
        WHERE customer_id = ? AND status IN ('generated','shown') AND context_refs_json = ?`, [
        timestamp, input.customerId, JSON.stringify(['signal:preference.frequent_intent_ids']),
      ]);
      const id = 'nad_' + crypto.randomUUID();
      tx.run(`INSERT INTO bank_next_action_decisions
        (id,recommendation_id,customer_id,decision,snooze_until,feedback_code,idempotency_key,request_hash,created_at)
        VALUES (?,?,?,?,?,?,?,?,?)`, [
        id, input.recommendationId, input.customerId, 'disable_personalization', null,
        input.feedbackCode || null, input.idempotencyKey, input.requestHash, timestamp,
      ]);
      return projectNextActionDecision({
        id, recommendation_id: input.recommendationId, decision: 'disable_personalization',
        snooze_until: null, feedback_code: input.feedbackCode || null, created_at: timestamp,
      });
    });
  }

  // R4 反馈冷却（2026-09-12 事件×记忆设计 §4.3）：统计窗口内同场景 dismiss 次数，
  // 达到阈值的场景 ID 交由引擎 cooldownScenarioIds 跳过。disable_personalization
  // 决策不是 dismiss，不计入。completedWindowDays 声明时，近期办结的场景也进入
  // 冷却——避免"刚办完下月同卡立即复现"的打扰。
  async function listNextActionCooldownScenarioIds(customerId, options) {
    requireNextActionId(customerId, 'customerId');
    const opts = options || {};
    if (!Number.isSafeInteger(opts.windowDays) || opts.windowDays < 1 || opts.windowDays > 365) {
      throw new BankApiError(400, 'next_action_input_invalid', 'windowDays is invalid');
    }
    if (!Number.isSafeInteger(opts.minimumDismissals) || opts.minimumDismissals < 1 || opts.minimumDismissals > 128) {
      throw new BankApiError(400, 'next_action_input_invalid', 'minimumDismissals is invalid');
    }
    const cutoff = new Date(Date.parse(now()) - opts.windowDays * 86400000).toISOString();
    const cooled = new Set(queryAll(db, `SELECT recommendation.scenario_id AS scenario_id, COUNT(*) AS dismiss_count
      FROM bank_next_action_decisions decision
      INNER JOIN bank_next_action_recommendations recommendation
        ON recommendation.id = decision.recommendation_id
      WHERE decision.customer_id = ? AND decision.decision = 'dismiss' AND decision.created_at >= ?
      GROUP BY recommendation.scenario_id`, [customerId, cutoff])
      .filter(row => row.dismiss_count >= opts.minimumDismissals)
      .map(row => row.scenario_id));
    if (Number.isSafeInteger(opts.completedWindowDays) && opts.completedWindowDays >= 1) {
      const completedCutoff = new Date(Date.parse(now()) - opts.completedWindowDays * 86400000).toISOString();
      queryAll(db, `SELECT DISTINCT scenario_id FROM bank_next_action_recommendations
        WHERE customer_id = ? AND status = 'completed' AND completed_at >= ?`,
        [customerId, completedCutoff]).forEach(row => cooled.add(row.scenario_id));
    }
    return [...cooled].sort();
  }

  // 展示频控数据源：窗口内出现过"已展示级"状态（shown/accepted/started/completed）
  // 的场景及其最后展示时刻。服务层按各场景 reshowAfterHours 声明折算冷却，
  // 用于问候/提示类场景的防打扰（持续提醒型声明 null 不受限）。
  async function listNextActionRecentlyShownScenarios(customerId, options) {
    requireNextActionId(customerId, 'customerId');
    const opts = options || {};
    if (!Number.isSafeInteger(opts.windowHours) || opts.windowHours < 1 || opts.windowHours > 720) {
      throw new BankApiError(400, 'next_action_input_invalid', 'windowHours is invalid');
    }
    const cutoff = new Date(Date.parse(now()) - opts.windowHours * 3600000).toISOString();
    return queryAll(db, `SELECT scenario_id, MAX(shown_at) AS last_shown_at
      FROM bank_next_action_recommendations
      WHERE customer_id = ? AND status IN ('shown', 'accepted', 'started', 'completed')
        AND shown_at IS NOT NULL AND shown_at >= ?
      GROUP BY scenario_id`, [customerId, cutoff])
      .map(row => ({ scenarioId: row.scenario_id, lastShownAt: row.last_shown_at }))
      .sort((left, right) => left.scenarioId.localeCompare(right.scenarioId));
  }

  // 空批次清扫：list() 因门控/决策过滤得到空池时，仍需把同 surface 的未决策行
  // （generated/shown——能处于该状态即未决策）过期，避免旧卡绕过重新评估存活到 TTL。
  async function expireNextActionLiveRows(customerId, scope) {
    requireNextActionId(customerId, 'customerId');
    const surface = scope && scope.surface;
    if (!['home', 'agent', 'channel'].includes(surface)) {
      throw new BankApiError(400, 'next_action_input_invalid', 'surface is invalid');
    }
    const channel = scope.channel == null ? null : String(scope.channel);
    return withTransaction(async tx => {
      tx.run(`UPDATE bank_next_action_recommendations
        SET status = 'expired', updated_at = ?
        WHERE customer_id = ? AND surface = ? AND COALESCE(channel, '') = COALESCE(?, '')
          AND status IN ('generated', 'shown')`, [now(), customerId, surface, channel]);
      return { expired: true };
    });
  }

  // 曝光疲劳数据源（服务层轮换排序）：同客户同 surface 各场景的连续展示计数与
  // 最后展示时刻。与 reshow 频控互补——reshow 管"多久内不许再出"，曝光计数管
  // "连出的让位轮休"。
  async function listNextActionExposures(customerId, scope) {
    requireNextActionId(customerId, 'customerId');
    const surface = scope && scope.surface;
    if (!['home', 'agent', 'channel'].includes(surface)) {
      throw new BankApiError(400, 'next_action_input_invalid', 'surface is invalid');
    }
    return queryAll(db, `SELECT scenario_id, consecutive_count, total_count, last_served_at
      FROM bank_next_action_exposures
      WHERE customer_id = ? AND surface = ?`, [customerId, surface])
      .map(row => ({
        scenarioId: row.scenario_id,
        consecutiveCount: Number(row.consecutive_count),
        totalCount: Number(row.total_count),
        lastServedAt: row.last_served_at,
      }))
      .sort((left, right) => left.scenarioId.localeCompare(right.scenarioId));
  }

  // 记录一次服务曝光：命中场景连续计数 +1、总数 +1、更新时刻；未命中场景连续计数
  // 清零（轮休后回到轮换队首），总数保留作观测。
  async function recordNextActionExposures(customerId, scope, scenarioIds, servedAt) {
    requireNextActionId(customerId, 'customerId');
    const surface = scope && scope.surface;
    if (!['home', 'agent', 'channel'].includes(surface)) {
      throw new BankApiError(400, 'next_action_input_invalid', 'surface is invalid');
    }
    if (!Array.isArray(scenarioIds)) {
      throw new BankApiError(400, 'next_action_input_invalid', 'scenarioIds is invalid');
    }
    const served = [...new Set(scenarioIds.filter(id => typeof id === 'string' && id))];
    const stamp = typeof servedAt === 'string' && servedAt ? servedAt : now();
    const prevRows = queryAll(db, `SELECT scenario_id, consecutive_count
      FROM bank_next_action_exposures WHERE customer_id = ? AND surface = ?`, [customerId, surface]);
    const prevByScenario = new Map(prevRows.map(row => [row.scenario_id, Number(row.consecutive_count)]));
    return withTransaction(async tx => {
      if (served.length) {
        const placeholders = served.map(() => '?').join(',');
        tx.run(`UPDATE bank_next_action_exposures SET consecutive_count = 0
          WHERE customer_id = ? AND surface = ? AND scenario_id NOT IN (${placeholders})`,
          [customerId, surface, ...served]);
      } else {
        tx.run(`UPDATE bank_next_action_exposures SET consecutive_count = 0
          WHERE customer_id = ? AND surface = ?`, [customerId, surface]);
      }
      for (const scenarioId of served) {
        tx.run('DELETE FROM bank_next_action_exposures WHERE customer_id = ? AND scenario_id = ? AND surface = ?',
          [customerId, scenarioId, surface]);
        tx.run(`INSERT INTO bank_next_action_exposures
          (customer_id, scenario_id, surface, consecutive_count, total_count, last_served_at)
          VALUES (?, ?, ?, ?, 1, ?)`,
          [customerId, scenarioId, surface, (prevByScenario.get(scenarioId) || 0) + 1, stamp]);
      }
      return { recorded: served.length };
    });
  }

  // R2 偏好加成数据源（设计 §4.2）：近 30 天工作流的意图频次 Top N。
  // 意图来源两级：会话状态 goals[].intentId（挂起工作流保留状态）；状态被清空的
  // 终态工作流回退推荐绑定的 targetIntentId。每个工作流只计一次。
  async function listFrequentIntentIds(customerId, limit) {
    requireNextActionId(customerId, 'customerId');
    const bounded = Number.isSafeInteger(limit) && limit > 0 ? Math.min(limit, 32) : 8;
    const cutoff = new Date(Date.parse(now()) - 30 * 86400000).toISOString();
    const rows = queryAll(db, `SELECT workflow.id, workflow.conversation_state_json,
        recommendation.safe_payload_json AS bound_payload
      FROM bank_agent_workflows workflow
      LEFT JOIN bank_next_action_workflow_bindings binding
        ON binding.business_workflow_id = workflow.id AND binding.customer_id = workflow.customer_id
      LEFT JOIN bank_next_action_recommendations recommendation
        ON recommendation.id = binding.recommendation_id
      WHERE workflow.customer_id = ? AND workflow.updated_at >= ?
      ORDER BY workflow.updated_at DESC, workflow.id DESC LIMIT 256`, [customerId, cutoff]);
    const counts = new Map();
    const seenWorkflows = new Set();
    const intentPattern = /^[a-z0-9.-]{1,160}$/;
    for (const row of rows) {
      if (seenWorkflows.has(row.id)) continue;
      seenWorkflows.add(row.id);
      const intents = new Set();
      let state = null;
      if (row.conversation_state_json) {
        try { state = JSON.parse(row.conversation_state_json); } catch (_) { state = null; }
      }
      ((state && Array.isArray(state.goals)) ? state.goals : []).forEach(goal => {
        if (goal && typeof goal.intentId === 'string' && intentPattern.test(goal.intentId)) {
          intents.add(goal.intentId);
        }
      });
      if (!intents.size && row.bound_payload) {
        try {
          const payload = JSON.parse(row.bound_payload);
          if (payload && typeof payload.targetIntentId === 'string'
            && intentPattern.test(payload.targetIntentId)) {
            intents.add(payload.targetIntentId);
          }
        } catch (_) { /* 绑定载荷不可解析时仅跳过回退 */ }
      }
      intents.forEach(id => counts.set(id, (counts.get(id) || 0) + 1));
    }
    return [...counts.entries()]
      .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
      .slice(0, bounded)
      .map(([id]) => id);
  }

  async function payUtilityBill(input) {
    if (!input || typeof input !== 'object') throw new BankApiError(422, 'validation_error', '缴费请求无效');
    const { customerId, billId, accountId, idempotencyKey, requestHash, traceId, actorType, actorId } = input;
    for (const value of [customerId, billId, accountId, idempotencyKey, requestHash]) {
      if (typeof value !== 'string' || !value) throw new BankApiError(422, 'validation_error', '缴费请求字段无效');
    }
    return withTransaction(async tx => {
      const replay = tx.getIdempotency('utility.payment.create', idempotencyKey);
      if (replay) {
        if (replay.requestHash !== requestHash) throw new BankApiError(409, 'idempotency_conflict', '幂等键已用于不同请求');
        return replay.response;
      }
      const bill = tx.queryOne('SELECT * FROM bank_utility_bills WHERE id = ? AND customer_id = ?', [billId, customerId]);
      if (!bill) throw new BankApiError(404, 'not_found', '缴费账单不存在');
      if (bill.status !== 'UNPAID') throw new BankApiError(409, 'utility_bill_unavailable', '缴费账单当前不可支付');
      const account = tx.queryOne("SELECT * FROM bank_accounts WHERE id = ? AND customer_id = ? AND visibility = 'CUSTOMER'", [accountId, customerId]);
      if (!account || account.status !== 'ACTIVE') throw new BankApiError(404, 'not_found', '付款账户不存在或不可用');
      const balance = tx.getBalance(accountId);
      const amountMinor = Number(bill.amount_minor);
      if (!balance || balance.availableMinor < amountMinor) throw new BankApiError(422, 'insufficient_funds', '付款账户可用余额不足');
      const timestamp = now();
      const paymentId = 'utility_payment_' + crypto.randomUUID().replace(/-/g, '');
      const ledgerId = 'lg_' + paymentId;
      const receiptNo = 'UTILITY-' + timestamp.slice(0, 10).replace(/-/g, '') + '-' + paymentId.slice(-8).toUpperCase();
      const nextAvailable = balance.availableMinor - amountMinor;
      tx.updateBalance(accountId, nextAvailable, balance.ledgerMinor - amountMinor, balance.version);
      tx.insertLedgerTransaction({ id: ledgerId, businessType: 'UTILITY_PAYMENT', businessId: paymentId, currency: bill.currency, postedAt: timestamp });
      tx.insertLedgerEntry({ id: 'le_' + paymentId + '_a', ledgerTransactionId: ledgerId, accountId, direction: 'DEBIT', amountMinor, description: bill.name + '缴费' });
      tx.insertLedgerEntry({ id: 'le_' + paymentId + '_b', ledgerTransactionId: ledgerId, accountId: 'acct_external_clearing', direction: 'CREDIT', amountMinor, description: bill.name + '缴费清算' });
      tx.run(`INSERT INTO bank_account_transactions
        (id,account_id,customer_id,direction,amount_minor,currency,business_type,category,counterparty,description,channel,balance_after_minor,occurred_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`, [
        'atxn_' + paymentId, accountId, customerId, 'DEBIT', amountMinor, bill.currency,
        'UTILITY_PAYMENT', 'UTILITY', bill.name, bill.name + '缴费', 'AGENT', nextAvailable, timestamp,
      ]);
      tx.run("UPDATE bank_utility_bills SET status = 'PAID', updated_at = ? WHERE id = ? AND customer_id = ? AND status = 'UNPAID'", [timestamp, billId, customerId]);
      tx.run(`INSERT INTO bank_utility_payments
        (id,bill_id,customer_id,account_id,amount_minor,currency,status,receipt_no,paid_at,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)`, [paymentId, billId, customerId, accountId, amountMinor, bill.currency, 'SUCCEEDED', receiptNo, timestamp, timestamp]);
      const response = {
        id: paymentId, billId, accountId, status: 'SUCCEEDED', receiptNo,
        paid: { amountMinor, currency: bill.currency }, paidAt: timestamp,
      };
      tx.saveIdempotency({ scope: 'utility.payment.create', key: idempotencyKey, requestHash, response, status: 'SUCCEEDED', createdAt: timestamp });
      tx.insertAudit({
        id: 'audit_' + crypto.randomUUID(), traceId: traceId || 'trace_utility_payment',
        actorType: actorType || 'user', actorId: actorId || customerId,
        action: 'bank.utility.payment.create', resourceType: 'utility-bill', resourceId: billId,
        outcome: 'SUCCEEDED', details: { paymentId, accountId, amountMinor }, createdAt: timestamp,
      });
      return response;
    });
  }

  const repository = {
    dbPath,
    getCustomer: async id => mapCustomer(queryOne(db, 'SELECT * FROM bank_customers WHERE id = ?', [id])),
    listAccounts: async customerId => queryAll(db, "SELECT * FROM bank_accounts WHERE customer_id = ? AND visibility = 'CUSTOMER' ORDER BY sort_order, id", [customerId]).map(mapAccount),
    getAccount: async (customerId, accountId) => mapAccount(queryOne(db, "SELECT * FROM bank_accounts WHERE id = ? AND customer_id = ? AND visibility = 'CUSTOMER'", [accountId, customerId])),
    getAccountById: async accountId => mapAccount(queryOne(db, 'SELECT * FROM bank_accounts WHERE id = ?', [accountId])),
    getBalance: async accountId => mapBalance(queryOne(db, 'SELECT * FROM bank_balance_snapshots WHERE account_id = ?', [accountId])),
    getCustomerPoints: async customerId => {
      const row = queryOne(db, 'SELECT * FROM bank_customer_points WHERE customer_id = ?', [customerId]);
      return row ? { balance: Number(row.balance), expiringAmount: Number(row.expiring_amount), expiryDate: row.expiry_date } : null;
    },
    // 生活服务域：天气按当日日期对 7 天序列取模循环（mock 长期有效）；优惠按自然日计算临期
    getWeatherForecast: async () => {
      const city = LIFE_SEED.city;
      const todayMs = Date.parse(now().slice(0, 10) + 'T00:00:00.000Z');
      if (!Number.isFinite(todayMs)) return [];
      const baseOffset = Math.floor(todayMs / 86400000) % 7;
      const rows = queryAll(db, 'SELECT * FROM bank_weather_forecast WHERE city = ? ORDER BY offset_day', [city]);
      return [0, 1].map(dayAhead => {
        const row = rows.find(item => item.offset_day === (baseOffset + dayAhead) % 7);
        if (!row) return null;
        const date = new Date(todayMs + dayAhead * 86400000).toISOString().slice(0, 10);
        return { city, date, condition: row.condition, tempLow: Number(row.temp_low), tempHigh: Number(row.temp_high) };
      }).filter(Boolean);
    },
    listActiveOffers: async () => {
      const today = now().slice(0, 10);
      const todayMs = Date.parse(today + 'T00:00:00.000Z');
      return queryAll(db, 'SELECT * FROM bank_merchant_offers WHERE valid_from <= ? AND valid_to >= ? ORDER BY valid_to, id', [today, today])
        .map(row => {
          const expiresMs = Date.parse(row.valid_to + 'T00:00:00.000Z');
          return {
            id: row.id, merchant: row.merchant, category: row.category, title: row.title,
            discountText: row.discount_text, validFrom: row.valid_from, validTo: row.valid_to,
            daysToExpiry: Number.isFinite(expiresMs) ? Math.max(0, Math.round((expiresMs - todayMs) / 86400000)) : 0,
          };
        });
    },
    listPayees: async (customerId, options) => {
      const includeTemporary = Boolean(options && options.includeTemporary);
      return queryAll(
        db,
        includeTemporary
          ? 'SELECT * FROM bank_payees WHERE customer_id = ? AND (is_saved = 1 OR expires_at > ?) ORDER BY sort_order, id'
          : 'SELECT * FROM bank_payees WHERE customer_id = ? AND is_saved = 1 ORDER BY sort_order, id',
        includeTemporary ? [customerId, now()] : [customerId],
      ).map(mapPayee);
    },
    getPayee: async (customerId, payeeId) => mapPayee(queryOne(db, 'SELECT * FROM bank_payees WHERE id = ? AND customer_id = ?', [payeeId, customerId])),
    listCreditCards: async customerId => queryAll(db, 'SELECT * FROM bank_credit_cards WHERE customer_id = ? ORDER BY sort_order, id', [customerId]).map(mapCreditCard),
    getCreditCard: async (customerId, cardId) => mapCreditCard(queryOne(db, 'SELECT * FROM bank_credit_cards WHERE id = ? AND customer_id = ?', [cardId, customerId])),
    listCreditCardTransactions: async (cardId, paging) => listCreditCardTransactions(db, cardId, paging),
    getCreditCardSettings: async cardId => mapCreditCardSettings(queryOne(db, 'SELECT * FROM bank_credit_card_settings WHERE card_id = ?', [cardId])),
    listTransactions: async (accountId, paging) => listTransactions(db, accountId, paging),
    listFinancialProducts: async (filter) => {
      const opts = filter || {};
      if (opts.category) return queryAll(db, 'SELECT * FROM bank_financial_products WHERE category = ? ORDER BY sort_order, id', [opts.category]).map(mapFinancialProduct);
      return queryAll(db, 'SELECT * FROM bank_financial_products ORDER BY sort_order, id').map(mapFinancialProduct);
    },
    getFinancialProduct: async id => mapFinancialProduct(queryOne(db, 'SELECT * FROM bank_financial_products WHERE id = ?', [id])),
    listAccountTransactionDetails: async (customerId, accountId, paging) => listAccountTransactionDetails(db, customerId, accountId, paging),
    listWealthHoldings: async (customerId, status) => queryAll(db, status
      ? 'SELECT * FROM bank_wealth_holdings WHERE customer_id = ? AND status = ? ORDER BY status, opened_at'
      : 'SELECT * FROM bank_wealth_holdings WHERE customer_id = ? ORDER BY status, opened_at', status ? [customerId, status] : [customerId]).map(mapWealthHolding),
    listWealthTransactions: async customerId => queryAll(db, 'SELECT * FROM bank_wealth_transactions WHERE customer_id = ? ORDER BY occurred_at, id', [customerId]).map(mapWealthTransaction),
    getWealthRiskAssessment: async customerId => mapWealthRiskAssessment(queryOne(db,
      'SELECT * FROM bank_wealth_risk_assessments WHERE customer_id = ? ORDER BY assessed_at DESC LIMIT 1',
      [customerId],
    )),
    listLoans: async customerId => queryAll(db, 'SELECT * FROM bank_loans WHERE customer_id = ? ORDER BY status, disbursed_at', [customerId]).map(mapLoan),
    getLoan: async (customerId, loanId) => mapLoan(queryOne(db, 'SELECT * FROM bank_loans WHERE id = ? AND customer_id = ?', [loanId, customerId])),
    listLoanRepayments: async (customerId, loanId) => queryAll(db,
      loanId
        ? 'SELECT repayment.* FROM bank_loan_repayments repayment INNER JOIN bank_loans loan ON loan.id = repayment.loan_id WHERE repayment.customer_id = ? AND loan.id = ? ORDER BY repayment.period_no'
        : 'SELECT * FROM bank_loan_repayments WHERE customer_id = ? ORDER BY loan_id, period_no',
      loanId ? [customerId, loanId] : [customerId]).map(mapLoanRepayment),
    listUtilityBills: async customerId => queryAll(db,
      'SELECT * FROM bank_utility_bills WHERE customer_id = ? ORDER BY due_date, id', [customerId]
    ).map(mapUtilityBill),
    payUtilityBill,
    sumLedgerByCurrency: async currency => Number((queryOne(db, "SELECT COALESCE(SUM(CASE direction WHEN 'CREDIT' THEN amount_minor ELSE -amount_minor END), 0) AS total FROM bank_ledger_entries e JOIN bank_ledger_transactions t ON t.id = e.ledger_transaction_id WHERE t.currency = ?", [currency]) || {}).total || 0),
    countLedgerEntries: async () => Number((queryOne(db, 'SELECT COUNT(*) AS total FROM bank_ledger_entries') || {}).total || 0),
    listLedgerEntries: async ledgerTransactionId => queryAll(db, 'SELECT * FROM bank_ledger_entries WHERE ledger_transaction_id = ? ORDER BY id', [ledgerTransactionId]).map(mapLedgerEntry),
    getTransferOrder: async id => mapTransferOrder(queryOne(db, 'SELECT * FROM bank_transfer_orders WHERE id = ?', [id])),
    getChallenge: async id => mapChallenge(queryOne(db, 'SELECT * FROM bank_confirmation_challenges WHERE id = ?', [id])),
    getTransferExecution: async id => mapExecution(queryOne(db, 'SELECT * FROM bank_transfer_executions WHERE id = ?', [id])),
    getExecutionByOrder: async orderId => mapExecution(queryOne(db, 'SELECT * FROM bank_transfer_executions WHERE transfer_order_id = ?', [orderId])),
    listAuditEvents: async traceId => queryAll(db, 'SELECT * FROM bank_audit_events WHERE trace_id = ? ORDER BY created_at, id', [traceId]).map(mapAudit),
    createAgentRun,
    markAgentRunTraceCapture,
    appendAgentRunEvent,
    createAgentRunModelCall,
    bindAgentRunWorkflow,
    listAgentRuns,
    getAgentRunWorkbenchDetail,
    enqueueMemoryCommand,
    claimDueMemoryCommands,
    recordExperienceEventConsumption,
    listConsumedExperienceEvents,
    completeMemoryCommand,
    retryMemoryCommand,
    deadLetterMemoryCommand,
    listAgentRunMemoryUpdates,
    createAgentClarification,
    consumeAgentClarification,
    getAgentClarification: async (id, agentRunId, customerId) => mapAgentClarification(queryOne(
      db,
      'SELECT * FROM bank_agent_clarifications WHERE id = ? AND agent_run_id = ? AND customer_id = ?',
      [id, agentRunId, customerId],
    )),
    getAgentRun: async id => getAgentRunRecord(db, id),
    createAgentSession,
    createFocusedAgentSession,
    getAgentSessionFocus,
    getAgentSession: async (id, customerId) => mapAgentSession(queryOne(
      db,
      'SELECT * FROM bank_agent_sessions WHERE id = ? AND customer_id = ?',
      [id, customerId],
    )),
    requireActiveAgentSession,
    touchAgentSession,
    closeAgentSession,
    closeAgentSessionIdempotent,
    appendAgentSessionTurn,
    listAgentSessionTurns: async (sessionId, customerId) => queryAll(
      db,
      `SELECT turn.* FROM bank_agent_session_turns turn
       INNER JOIN bank_agent_sessions session ON session.id = turn.session_id
       WHERE turn.session_id = ? AND session.customer_id = ?
       ORDER BY turn.sequence`,
      [sessionId, customerId],
    ).map(mapAgentSessionTurn),
    transitionAgentRun,
    updateAgentRunTraceSequence: async (id, sequence) => withTransaction(async tx => {
      if (!Number.isInteger(sequence) || sequence < 0 || sequence > 128) throw new BankApiError(400, 'invalid_trace_sequence', 'Trace 序号无效');
      tx.run('UPDATE bank_agent_runs SET last_trace_sequence = ?, updated_at = ? WHERE id = ? AND last_trace_sequence <= ?', [sequence, now(), id, sequence]);
      if (db.getRowsModified() !== 1) throw new BankApiError(409, 'trace_sequence_conflict', 'Trace 序号已发生变化');
      return getAgentRunRecord(db, id);
    }),
    upsertConversationSlot,
    replaceConversationSlots,
    clearConversationSlots,
    listConversationSlots: async (conversationId, customerId) => queryAll(db,
      'SELECT * FROM bank_agent_conversation_slots WHERE conversation_id = ? AND customer_id = ? ORDER BY slot_name',
      [conversationId, customerId]
    ).map(mapConversationSlot),
    replaceConversationState,
    clearConversationState,
    getConversationState: async (conversationId, customerId) => mapConversationState(queryOne(
      db,
      'SELECT * FROM bank_agent_conversation_states WHERE conversation_id = ? AND customer_id = ?',
      [conversationId, customerId],
    )),
    replaceAgentWorkflow,
    transitionAgentWorkflowControl,
    archiveAgentWorkflowConversationState,
    listResumableAgentWorkflows,
    listResumableAgentWorkflowRecordsInternal,
    listSmartCompanionCandidates,
    bindAgentWorkflowToSession,
    bindAgentWorkflowToSessionIdempotent,
    getAgentWorkflow: async (id, conversationId, customerId) => mapAgentWorkflow(queryOne(
      db,
      'SELECT * FROM bank_agent_workflows WHERE id = ? AND bound_conversation_id = ? AND customer_id = ?',
      [id, conversationId, customerId],
    )),
    listAgentWorkflows: async (conversationId, customerId) => queryAll(
      db,
      'SELECT * FROM bank_agent_workflows WHERE bound_conversation_id = ? AND customer_id = ? ORDER BY updated_at DESC, id DESC',
      [conversationId, customerId],
    ).map(mapAgentWorkflow),
    listAgentWorkflowEvents: async (id, conversationId, customerId) => queryAll(
      db,
      `SELECT event.* FROM bank_agent_workflow_events event
       INNER JOIN bank_agent_workflows workflow ON workflow.id = event.business_workflow_id
       WHERE workflow.id = ? AND workflow.bound_conversation_id = ? AND workflow.customer_id = ?
       ORDER BY event.workflow_revision, event.event_type`,
      [id, conversationId, customerId],
    ).map(mapAgentWorkflowEvent),
    createActionCheckpoint,
    getActionCheckpointForExecution: async id => mapActionCheckpointInternal(queryOne(db, 'SELECT * FROM bank_agent_action_checkpoints WHERE id = ?', [id])),
    consumeActionCheckpoint,
    claimActionCheckpoint,
    completeActionCheckpoint,
    authorizeWorkflowCheckpoint,
    listPendingActionCheckpointsByConversation,
    claimCapabilityGrant,
    completeCapabilityGrant,
    getCapabilityGrant: async id => mapCapabilityGrant(queryOne(
      db, 'SELECT * FROM bank_agent_capability_grants WHERE id = ?', [id],
    )),
    listCapabilityGrantsByWorkflow: async (businessWorkflowId, customerId) => queryAll(
      db,
      'SELECT * FROM bank_agent_capability_grants WHERE business_workflow_id = ? AND customer_id = ? ORDER BY created_at, id',
      [businessWorkflowId, customerId],
    ).map(mapCapabilityGrant),
    recordCapabilityInvocation: async item => withTransaction(async tx => {
      tx.insertCapabilityInvocation(item);
      return mapCapabilityInvocation(tx.queryOne('SELECT * FROM bank_capability_invocations WHERE id = ?', [item.id]));
    }),
    listCapabilityInvocations: async traceId => queryAll(db,
      'SELECT * FROM bank_capability_invocations WHERE trace_id = ? ORDER BY created_at, id', [traceId]
    ).map(mapCapabilityInvocation),
    replaceNextActionRecommendations,
    listNextActionRecommendations,
    listNextActionDecidedRecommendationIds,
    getNextActionRecommendation,
    getNextActionRecommendationInternal,
    recordNextActionDecision,
    bindNextActionWorkflow,
    recoverInterruptedNextActionStart,
    attachNextActionBusinessWorkflow,
    completeNextActionRecommendation,
    failNextActionRecommendation,
    getNextActionPreference,
    setNextActionPersonalization,
    disableNextActionPersonalization,
    listNextActionCooldownScenarioIds,
    listNextActionRecentlyShownScenarios,
    listNextActionExposures,
    recordNextActionExposures,
    expireNextActionLiveRows,
    listFrequentIntentIds,
    withTransaction,
    resetScenario,
    close: async () => {
      await writeTail;
      await flushPersist();
      db.close();
    },
  };

  const publicReadMethods = [
    'getCustomer',
    'listAccounts',
    'getAccount',
    'getAccountById',
    'getBalance',
    'getCustomerPoints',
    'getWeatherForecast',
    'listActiveOffers',
    'listPayees',
    'getPayee',
    'listCreditCards',
    'getCreditCard',
    'listCreditCardTransactions',
    'getCreditCardSettings',
    'listTransactions',
    'listFinancialProducts',
    'getFinancialProduct',
    'listAccountTransactionDetails',
    'listWealthHoldings',
    'listWealthTransactions',
    'listLoans',
    'getLoan',
    'listLoanRepayments',
    'listUtilityBills',
    'sumLedgerByCurrency',
    'countLedgerEntries',
    'listLedgerEntries',
    'getTransferOrder',
    'getChallenge',
    'getTransferExecution',
    'getExecutionByOrder',
    'listAuditEvents',
    'getAgentClarification',
    'getAgentRun',
    'listAgentRuns',
    'getAgentRunWorkbenchDetail',
    'getAgentSession',
    'listAgentSessionTurns',
    'listConversationSlots',
    'getConversationState',
    'listResumableAgentWorkflows',
    'listResumableAgentWorkflowRecordsInternal',
    'getAgentWorkflow',
    'listAgentWorkflows',
    'listAgentWorkflowEvents',
    'getActionCheckpointForExecution',
    'getCapabilityGrant',
    'listCapabilityInvocations',
    'getNextActionRecommendation',
    'getNextActionRecommendationInternal',
    'getNextActionPreference',
    'listNextActionCooldownScenarioIds',
    'listNextActionRecentlyShownScenarios',
    'listNextActionExposures',
    'listFrequentIntentIds',
  ];
  publicReadMethods.forEach(method => {
    const read = repository[method];
    repository[method] = async (...args) => {
      const barrier = writeTail;
      await barrier;
      return read(...args);
    };
  });
  return repository;
}

function createSchema(db) {
  db.run(`
    PRAGMA foreign_keys = ON;
    CREATE TABLE IF NOT EXISTS bank_customers (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, city TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bank_accounts (
      id TEXT PRIMARY KEY, customer_id TEXT, masked_no TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
      currency TEXT NOT NULL, status TEXT NOT NULL, visibility TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_balance_snapshots (
      account_id TEXT PRIMARY KEY, available_minor INTEGER NOT NULL, ledger_minor INTEGER NOT NULL,
      version INTEGER NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(account_id) REFERENCES bank_accounts(id)
    );
    CREATE TABLE IF NOT EXISTS bank_customer_points (
      customer_id TEXT PRIMARY KEY, balance INTEGER NOT NULL, expiring_amount INTEGER NOT NULL,
      expiry_date TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_weather_forecast (
      city TEXT NOT NULL, offset_day INTEGER NOT NULL, condition TEXT NOT NULL,
      temp_low INTEGER NOT NULL, temp_high INTEGER NOT NULL,
      PRIMARY KEY(city, offset_day)
    );
    CREATE TABLE IF NOT EXISTS bank_merchant_offers (
      id TEXT PRIMARY KEY, merchant TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
      discount_text TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bank_payees (
      id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, name TEXT NOT NULL, bank_name TEXT NOT NULL,
      masked_no TEXT NOT NULL, status TEXT NOT NULL, target_account_id TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
      is_saved INTEGER NOT NULL DEFAULT 1, expires_at TEXT,
      created_at TEXT NOT NULL, FOREIGN KEY(customer_id) REFERENCES bank_customers(id),
      FOREIGN KEY(target_account_id) REFERENCES bank_accounts(id)
    );
    CREATE TABLE IF NOT EXISTS bank_credit_cards (
      id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, masked_no TEXT NOT NULL, product_name TEXT NOT NULL,
      network TEXT NOT NULL, card_type TEXT NOT NULL, status TEXT NOT NULL, total_limit_minor INTEGER NOT NULL,
      available_limit_minor INTEGER NOT NULL, cash_limit_minor INTEGER NOT NULL, statement_day INTEGER NOT NULL,
      due_day INTEGER NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_credit_card_settings (
      card_id TEXT PRIMARY KEY, online_transactions_enabled INTEGER NOT NULL, contactless_enabled INTEGER NOT NULL,
      overseas_enabled INTEGER NOT NULL, single_transaction_limit_minor INTEGER NOT NULL,
      daily_limit_minor INTEGER NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(card_id) REFERENCES bank_credit_cards(id)
    );
    CREATE TABLE IF NOT EXISTS bank_credit_card_transactions (
      id TEXT PRIMARY KEY, card_id TEXT NOT NULL, direction TEXT NOT NULL, amount_minor INTEGER NOT NULL,
      currency TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL, occurred_at TEXT NOT NULL,
      FOREIGN KEY(card_id) REFERENCES bank_credit_cards(id)
    );
    CREATE TABLE IF NOT EXISTS bank_credit_card_limit_change_requests (
      id TEXT PRIMARY KEY, card_id TEXT NOT NULL, customer_id TEXT NOT NULL, requested_limit_minor INTEGER NOT NULL,
      status TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(card_id) REFERENCES bank_credit_cards(id), FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_transfer_orders (
      id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, from_account_id TEXT NOT NULL, payee_id TEXT NOT NULL,
      amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL,
      balance_version INTEGER NOT NULL, request_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id), FOREIGN KEY(from_account_id) REFERENCES bank_accounts(id),
      FOREIGN KEY(payee_id) REFERENCES bank_payees(id)
    );
    CREATE TABLE IF NOT EXISTS bank_transfer_validations (
      id TEXT PRIMARY KEY, transfer_order_id TEXT NOT NULL, result TEXT NOT NULL, checks_json TEXT NOT NULL,
      fee_minor INTEGER NOT NULL DEFAULT 0, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(transfer_order_id) REFERENCES bank_transfer_orders(id)
    );
    CREATE TABLE IF NOT EXISTS bank_confirmation_challenges (
      id TEXT PRIMARY KEY, transfer_order_id TEXT NOT NULL, token_hash TEXT NOT NULL, summary_hash TEXT NOT NULL,
      expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE', used_at TEXT,
      invalidated_at TEXT, invalidation_reason TEXT, created_at TEXT NOT NULL,
      FOREIGN KEY(transfer_order_id) REFERENCES bank_transfer_orders(id)
    );
    CREATE TABLE IF NOT EXISTS bank_transfer_authorizations (
      id TEXT PRIMARY KEY, transfer_order_id TEXT NOT NULL UNIQUE, challenge_id TEXT NOT NULL, actor_id TEXT NOT NULL,
      summary_hash TEXT NOT NULL, authorized_at TEXT NOT NULL,
      FOREIGN KEY(transfer_order_id) REFERENCES bank_transfer_orders(id),
      FOREIGN KEY(challenge_id) REFERENCES bank_confirmation_challenges(id)
    );
    CREATE TABLE IF NOT EXISTS bank_transfer_executions (
      id TEXT PRIMARY KEY, transfer_order_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, ledger_transaction_id TEXT,
      receipt_no TEXT, executed_at TEXT, failure_code TEXT, created_at TEXT NOT NULL,
      FOREIGN KEY(transfer_order_id) REFERENCES bank_transfer_orders(id)
    );
    CREATE TABLE IF NOT EXISTS bank_ledger_transactions (
      id TEXT PRIMARY KEY, business_type TEXT NOT NULL, business_id TEXT NOT NULL, currency TEXT NOT NULL, posted_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bank_ledger_entries (
      id TEXT PRIMARY KEY, ledger_transaction_id TEXT NOT NULL, account_id TEXT NOT NULL, direction TEXT NOT NULL,
      amount_minor INTEGER NOT NULL CHECK(amount_minor > 0), description TEXT NOT NULL,
      FOREIGN KEY(ledger_transaction_id) REFERENCES bank_ledger_transactions(id),
      FOREIGN KEY(account_id) REFERENCES bank_accounts(id)
    );
    CREATE TABLE IF NOT EXISTS bank_idempotency_records (
      scope TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
      status TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(scope, key)
    );
    CREATE TABLE IF NOT EXISTS bank_capability_invocations (
      id TEXT PRIMARY KEY, capability_id TEXT NOT NULL, actor_type TEXT NOT NULL, actor_id TEXT,
      status TEXT NOT NULL, input_hash TEXT NOT NULL, output_json TEXT, trace_id TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bank_agent_runs (
      id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, conversation_id TEXT NOT NULL, trace_id TEXT NOT NULL,
      status TEXT NOT NULL, graph_version TEXT NOT NULL, graph_digest TEXT NOT NULL,
      last_trace_sequence INTEGER NOT NULL DEFAULT 0,
      trace_capture_status TEXT NOT NULL DEFAULT 'legacy_unavailable',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_sessions (
      id TEXT PRIMARY KEY,
      customer_id TEXT NOT NULL,
      status TEXT NOT NULL,
      created_at TEXT NOT NULL,
      last_active_at TEXT NOT NULL,
      idle_expires_at TEXT NOT NULL,
      absolute_expires_at TEXT NOT NULL,
      closed_at TEXT,
      close_reason TEXT,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_session_focus (
      session_id TEXT PRIMARY KEY,
      customer_id TEXT NOT NULL,
      recommendation_id TEXT NOT NULL,
      mode TEXT NOT NULL,
      target_intent_id TEXT NOT NULL,
      context_digest TEXT NOT NULL,
      focus_json TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(session_id) REFERENCES bank_agent_sessions(id),
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_session_turns (
      session_id TEXT NOT NULL,
      sequence INTEGER NOT NULL,
      role TEXT NOT NULL,
      text TEXT NOT NULL,
      response_json TEXT,
      created_at TEXT NOT NULL,
      PRIMARY KEY(session_id,sequence),
      FOREIGN KEY(session_id) REFERENCES bank_agent_sessions(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_clarifications (
      id TEXT PRIMARY KEY, agent_run_id TEXT NOT NULL, customer_id TEXT NOT NULL,
      conversation_id TEXT NOT NULL, kind TEXT NOT NULL, payee_name TEXT NOT NULL,
      token_hash TEXT NOT NULL, status TEXT NOT NULL, expires_at TEXT NOT NULL,
      business_workflow_id TEXT, task_id TEXT, expected_workflow_revision INTEGER,
      consumed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(agent_run_id) REFERENCES bank_agent_runs(id),
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_conversation_slots (
      conversation_id TEXT NOT NULL, customer_id TEXT NOT NULL, slot_name TEXT NOT NULL,
      value_json TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'context', source_intent_id TEXT NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY(conversation_id,customer_id,slot_name),
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_conversation_states (
      conversation_id TEXT NOT NULL, customer_id TEXT NOT NULL,
      schema_version TEXT NOT NULL, state_json TEXT NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY(conversation_id,customer_id),
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_workflows (
      id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
      origin_conversation_id TEXT, bound_conversation_id TEXT, customer_id TEXT NOT NULL,
      status TEXT NOT NULL, delegated_task_ids_json TEXT NOT NULL,
      envelope_revision INTEGER NOT NULL DEFAULT 1,
      workflow_revision INTEGER NOT NULL DEFAULT 1,
      todo_revision INTEGER NOT NULL DEFAULT 0, todo_snapshot_json TEXT NOT NULL,
      todo_digest TEXT NOT NULL DEFAULT '', active_goal_task_id TEXT, active_todo_id TEXT,
      execution_policy TEXT NOT NULL DEFAULT 'sequential', suspension_reason TEXT,
      last_outcome_json TEXT NOT NULL DEFAULT '{}',
      conversation_state_json TEXT,
      pending_checkpoint_ids_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_workflow_events (
      id TEXT PRIMARY KEY, business_workflow_id TEXT NOT NULL,
      workflow_revision INTEGER NOT NULL, event_type TEXT NOT NULL,
      safe_payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(business_workflow_id) REFERENCES bank_agent_workflows(id),
      UNIQUE(business_workflow_id,workflow_revision,event_type)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_run_capabilities (
      agent_run_id TEXT NOT NULL, capability_id TEXT NOT NULL,
      PRIMARY KEY(agent_run_id,capability_id),
      FOREIGN KEY(agent_run_id) REFERENCES bank_agent_runs(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_run_plannable_capabilities (
      agent_run_id TEXT NOT NULL, capability_id TEXT NOT NULL,
      PRIMARY KEY(agent_run_id,capability_id),
      FOREIGN KEY(agent_run_id) REFERENCES bank_agent_runs(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_run_events (
      agent_run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
      stage TEXT NOT NULL, status TEXT NOT NULL, duration_ms INTEGER,
      policy_version TEXT, safe_summary_json TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(agent_run_id,sequence),
      FOREIGN KEY(agent_run_id) REFERENCES bank_agent_runs(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_run_workflows (
      agent_run_id TEXT NOT NULL, business_workflow_id TEXT NOT NULL,
      relation TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(agent_run_id,business_workflow_id),
      FOREIGN KEY(agent_run_id) REFERENCES bank_agent_runs(id),
      FOREIGN KEY(business_workflow_id) REFERENCES bank_agent_workflows(id)
    );
    -- Memory command outbox 归属本仓库：与 bank_agent_runs 等 bank_* schema 同源同库。
    -- 写入方 enqueueMemoryCommand；消费方 claimDueMemoryCommands 认领后 POST 到
    -- agent-memory-control /v1/candidates:submit（幂等键两边各自唯一约束兜底）。
    CREATE TABLE IF NOT EXISTS bank_agent_memory_commands (
      id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
      agent_run_id TEXT NOT NULL, customer_id TEXT NOT NULL,
      task_id TEXT, intent_id TEXT, candidate_type TEXT NOT NULL, predicate TEXT NOT NULL,
      command_json TEXT NOT NULL, command_hash TEXT NOT NULL,
      status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt_at TEXT NOT NULL, decision TEXT, governance_status TEXT,
      reason TEXT, policy_version TEXT, error_code TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(agent_run_id) REFERENCES bank_agent_runs(id),
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE INDEX IF NOT EXISTS idx_bank_agent_memory_commands_due
      ON bank_agent_memory_commands(status,next_attempt_at,created_at);
    CREATE INDEX IF NOT EXISTS idx_bank_agent_memory_commands_run
      ON bank_agent_memory_commands(agent_run_id,updated_at);
    -- 模型调用观测（LangSmith 式运维层）：独立于 safe trace，仅供工作台详情。
    CREATE TABLE IF NOT EXISTS bank_agent_run_model_calls (
      id TEXT PRIMARY KEY, agent_run_id TEXT NOT NULL,
      phase TEXT NOT NULL, model TEXT,
      prompt_json TEXT, response_text TEXT,
      tools_count INTEGER, duration_ms INTEGER,
      status TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(agent_run_id) REFERENCES bank_agent_runs(id)
    );
    CREATE INDEX IF NOT EXISTS idx_bank_agent_run_model_calls_run
      ON bank_agent_run_model_calls(agent_run_id,created_at,id);
    CREATE TABLE IF NOT EXISTS bank_experience_events_consumed (
      id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, event_id TEXT NOT NULL,
      event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, consumed_at TEXT NOT NULL,
      UNIQUE(customer_id, event_id),
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_action_checkpoints (
      id TEXT PRIMARY KEY, agent_run_id TEXT NOT NULL, customer_id TEXT NOT NULL,
      business_workflow_id TEXT, task_id TEXT, capability_id TEXT NOT NULL,
      input_json TEXT NOT NULL, input_hash TEXT NOT NULL, summary_json TEXT NOT NULL, token_hash TEXT NOT NULL,
      idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, expires_at TEXT NOT NULL,
      consumed_at TEXT, result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(agent_run_id) REFERENCES bank_agent_runs(id),
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_agent_capability_grants (
      id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, business_workflow_id TEXT NOT NULL,
      task_id TEXT NOT NULL, capability_id TEXT NOT NULL, input_hash TEXT NOT NULL,
      checkpoint_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, expires_at TEXT NOT NULL,
      consumed_at TEXT, receipt_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id),
      FOREIGN KEY(business_workflow_id) REFERENCES bank_agent_workflows(id),
      FOREIGN KEY(checkpoint_id) REFERENCES bank_agent_action_checkpoints(id)
    );
    CREATE TABLE IF NOT EXISTS bank_audit_events (
      id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, actor_type TEXT NOT NULL, actor_id TEXT, action TEXT NOT NULL,
      resource_type TEXT NOT NULL, resource_id TEXT, outcome TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bank_financial_products (
      id TEXT PRIMARY KEY, category TEXT NOT NULL, name TEXT NOT NULL, issuer TEXT NOT NULL,
      product_code TEXT NOT NULL, currency TEXT NOT NULL, risk_level TEXT NOT NULL,
      min_purchase_minor INTEGER NOT NULL, min_increment_minor INTEGER NOT NULL,
      term_type TEXT NOT NULL, term_days INTEGER, annual_rate_bp INTEGER,
      rate_type TEXT NOT NULL, rate_note TEXT NOT NULL, purchase_channel TEXT NOT NULL,
      status TEXT NOT NULL, features_json TEXT NOT NULL DEFAULT '{}',
      sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bank_account_transactions (
      id TEXT PRIMARY KEY, account_id TEXT NOT NULL, customer_id TEXT NOT NULL,
      direction TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
      currency TEXT NOT NULL, business_type TEXT NOT NULL, category TEXT NOT NULL,
      counterparty TEXT NOT NULL, description TEXT NOT NULL, channel TEXT NOT NULL,
      balance_after_minor INTEGER NOT NULL, occurred_at TEXT NOT NULL,
      FOREIGN KEY(account_id) REFERENCES bank_accounts(id),
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_wealth_holdings (
      id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, account_id TEXT NOT NULL,
      product_id TEXT NOT NULL, product_name TEXT NOT NULL, category TEXT NOT NULL,
      principal_minor INTEGER NOT NULL, current_value_minor INTEGER NOT NULL,
      accumulated_earnings_minor INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL,
      status TEXT NOT NULL, opened_at TEXT NOT NULL, matures_at TEXT, last_valued_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id),
      FOREIGN KEY(account_id) REFERENCES bank_accounts(id),
      FOREIGN KEY(product_id) REFERENCES bank_financial_products(id)
    );
    CREATE TABLE IF NOT EXISTS bank_wealth_transactions (
      id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, account_id TEXT NOT NULL,
      product_id TEXT NOT NULL, product_name TEXT NOT NULL, direction TEXT NOT NULL,
      amount_minor INTEGER NOT NULL, earnings_minor INTEGER NOT NULL DEFAULT 0,
      currency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'CONFIRMED', occurred_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id),
      FOREIGN KEY(account_id) REFERENCES bank_accounts(id),
      FOREIGN KEY(product_id) REFERENCES bank_financial_products(id)
    );
    CREATE TABLE IF NOT EXISTS bank_wealth_risk_assessments (
      id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, level TEXT NOT NULL,
      status TEXT NOT NULL, assessed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
      source TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_loans (
      id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, product_id TEXT NOT NULL,
      product_name TEXT NOT NULL, loan_type TEXT NOT NULL, repayment_account_id TEXT NOT NULL,
      contract_no TEXT NOT NULL, principal_minor INTEGER NOT NULL,
      outstanding_principal_minor INTEGER NOT NULL, currency TEXT NOT NULL,
      annual_rate_bp INTEGER NOT NULL, rate_type TEXT NOT NULL, term_months INTEGER NOT NULL,
      remaining_months INTEGER NOT NULL, repayment_method TEXT NOT NULL,
      monthly_payment_minor INTEGER NOT NULL, monthly_payment_day INTEGER NOT NULL,
      status TEXT NOT NULL, disbursed_at TEXT NOT NULL, settled_at TEXT,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id),
      FOREIGN KEY(product_id) REFERENCES bank_financial_products(id),
      FOREIGN KEY(repayment_account_id) REFERENCES bank_accounts(id)
    );
    CREATE TABLE IF NOT EXISTS bank_loan_repayments (
      id TEXT PRIMARY KEY, loan_id TEXT NOT NULL, customer_id TEXT NOT NULL,
      period_no INTEGER NOT NULL, due_date TEXT NOT NULL,
      principal_minor INTEGER NOT NULL, interest_minor INTEGER NOT NULL,
      total_minor INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PAID', paid_at TEXT,
      FOREIGN KEY(loan_id) REFERENCES bank_loans(id),
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_utility_bills (
      id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, bill_type TEXT NOT NULL, name TEXT NOT NULL,
      period TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor > 0), currency TEXT NOT NULL,
      due_date TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_utility_payments (
      id TEXT PRIMARY KEY, bill_id TEXT NOT NULL UNIQUE, customer_id TEXT NOT NULL, account_id TEXT NOT NULL,
      amount_minor INTEGER NOT NULL CHECK(amount_minor > 0), currency TEXT NOT NULL, status TEXT NOT NULL,
      receipt_no TEXT NOT NULL UNIQUE, paid_at TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(bill_id) REFERENCES bank_utility_bills(id),
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id), FOREIGN KEY(account_id) REFERENCES bank_accounts(id)
    );
    CREATE TABLE IF NOT EXISTS bank_next_action_recommendations (
      id TEXT PRIMARY KEY,
      customer_id TEXT NOT NULL,
      scenario_id TEXT NOT NULL,
      scenario_version TEXT NOT NULL,
      recommendation_type TEXT NOT NULL,
      surface TEXT NOT NULL,
      channel TEXT,
      status TEXT NOT NULL,
      deterministic_score INTEGER NOT NULL,
      final_rank INTEGER NOT NULL,
      reason_codes_json TEXT NOT NULL,
      reason_text TEXT NOT NULL,
      context_refs_json TEXT NOT NULL,
      context_digest TEXT NOT NULL,
      safe_payload_json TEXT NOT NULL,
      outcome_json TEXT,
      generated_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      shown_at TEXT,
      accepted_at TEXT,
      completed_at TEXT,
      updated_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES bank_customers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_next_action_decisions (
      id TEXT PRIMARY KEY,
      recommendation_id TEXT NOT NULL,
      customer_id TEXT NOT NULL,
      decision TEXT NOT NULL,
      snooze_until TEXT,
      feedback_code TEXT,
      idempotency_key TEXT NOT NULL,
      request_hash TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(customer_id,idempotency_key),
      FOREIGN KEY(recommendation_id) REFERENCES bank_next_action_recommendations(id)
    );
    CREATE TABLE IF NOT EXISTS bank_next_action_workflow_bindings (
      recommendation_id TEXT PRIMARY KEY,
      customer_id TEXT NOT NULL,
      agent_session_id TEXT NOT NULL,
      agent_run_id TEXT NOT NULL,
      business_workflow_id TEXT,
      goal_id TEXT NOT NULL,
      status TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY(recommendation_id) REFERENCES bank_next_action_recommendations(id)
    );
    CREATE TABLE IF NOT EXISTS bank_next_action_preferences (
      customer_id TEXT PRIMARY KEY,
      personalization_enabled INTEGER NOT NULL DEFAULT 1,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bank_next_action_exposures (
      customer_id TEXT NOT NULL,
      scenario_id TEXT NOT NULL,
      surface TEXT NOT NULL,
      consecutive_count INTEGER NOT NULL DEFAULT 0,
      total_count INTEGER NOT NULL DEFAULT 0,
      last_served_at TEXT NOT NULL,
      PRIMARY KEY(customer_id, scenario_id, surface)
    );
    CREATE INDEX IF NOT EXISTS idx_bank_accounts_customer ON bank_accounts(customer_id, visibility);
    CREATE INDEX IF NOT EXISTS idx_bank_entries_account ON bank_ledger_entries(account_id, id);
    CREATE INDEX IF NOT EXISTS idx_bank_credit_cards_customer ON bank_credit_cards(customer_id, sort_order);
    CREATE INDEX IF NOT EXISTS idx_bank_credit_card_transactions ON bank_credit_card_transactions(card_id, occurred_at, id);
    CREATE INDEX IF NOT EXISTS idx_bank_transfers_customer ON bank_transfer_orders(customer_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_bank_agent_runs_owner ON bank_agent_runs(customer_id, conversation_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_bank_agent_run_workflows_run ON bank_agent_run_workflows(agent_run_id, created_at);
    DROP INDEX IF EXISTS idx_bank_agent_one_active_session;
    CREATE INDEX IF NOT EXISTS idx_bank_agent_sessions_owner_status
      ON bank_agent_sessions(customer_id, status, last_active_at);
    CREATE INDEX IF NOT EXISTS idx_bank_agent_session_focus_owner
      ON bank_agent_session_focus(customer_id,recommendation_id,mode,expires_at);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_agent_clarification_run ON bank_agent_clarifications(agent_run_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_agent_workflow_owner ON bank_agent_workflows(customer_id, conversation_id, id);
    CREATE INDEX IF NOT EXISTS idx_bank_agent_checkpoints_run ON bank_agent_action_checkpoints(agent_run_id, status);
    CREATE INDEX IF NOT EXISTS idx_bank_audit_trace ON bank_audit_events(trace_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_bank_products_category ON bank_financial_products(category, sort_order);
    CREATE INDEX IF NOT EXISTS idx_bank_account_txn_account ON bank_account_transactions(account_id, occurred_at, id);
    CREATE INDEX IF NOT EXISTS idx_bank_account_txn_business ON bank_account_transactions(account_id, business_type);
    CREATE INDEX IF NOT EXISTS idx_bank_wealth_holdings_customer ON bank_wealth_holdings(customer_id, status);
    CREATE INDEX IF NOT EXISTS idx_bank_wealth_txn_customer ON bank_wealth_transactions(customer_id, occurred_at);
    CREATE INDEX IF NOT EXISTS idx_bank_wealth_risk_customer ON bank_wealth_risk_assessments(customer_id, assessed_at);
    CREATE INDEX IF NOT EXISTS idx_bank_loans_customer ON bank_loans(customer_id, status);
    CREATE INDEX IF NOT EXISTS idx_bank_loan_repayments_loan ON bank_loan_repayments(loan_id, period_no);
    CREATE INDEX IF NOT EXISTS idx_bank_utility_bills_customer ON bank_utility_bills(customer_id, status, due_date);
    CREATE INDEX IF NOT EXISTS idx_bank_utility_payments_customer ON bank_utility_payments(customer_id, paid_at);
    CREATE INDEX IF NOT EXISTS idx_bank_next_action_owner_surface
      ON bank_next_action_recommendations(customer_id,surface,channel,status,final_rank);
    CREATE INDEX IF NOT EXISTS idx_bank_next_action_expiry
      ON bank_next_action_recommendations(customer_id,status,expires_at);
    CREATE INDEX IF NOT EXISTS idx_bank_next_action_decision_owner
      ON bank_next_action_decisions(customer_id,recommendation_id,created_at);
    CREATE INDEX IF NOT EXISTS idx_bank_next_action_binding_owner
      ON bank_next_action_workflow_bindings(customer_id,status,updated_at);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_next_action_binding_run
      ON bank_next_action_workflow_bindings(agent_run_id);
  `);
  ensureColumn(db, 'bank_confirmation_challenges', 'status', "TEXT NOT NULL DEFAULT 'ACTIVE'");
  ensureColumn(db, 'bank_confirmation_challenges', 'invalidated_at', 'TEXT');
  ensureColumn(db, 'bank_confirmation_challenges', 'invalidation_reason', 'TEXT');
  ensureColumn(db, 'bank_agent_conversation_slots', 'source', "TEXT NOT NULL DEFAULT 'context'");
  ensureColumn(db, 'bank_agent_runs', 'trace_capture_status', "TEXT NOT NULL DEFAULT 'legacy_unavailable'");
  ensureColumn(db, 'bank_agent_action_checkpoints', 'business_workflow_id', 'TEXT');
  ensureColumn(db, 'bank_agent_action_checkpoints', 'task_id', 'TEXT');
  ensureColumn(db, 'bank_agent_workflows', 'envelope_revision', 'INTEGER NOT NULL DEFAULT 1');
  ensureColumn(db, 'bank_agent_workflows', 'workflow_revision', 'INTEGER NOT NULL DEFAULT 1');
  ensureColumn(db, 'bank_agent_workflows', 'todo_digest', "TEXT NOT NULL DEFAULT ''");
  ensureColumn(db, 'bank_agent_workflows', 'active_goal_task_id', 'TEXT');
  ensureColumn(db, 'bank_agent_workflows', 'active_todo_id', 'TEXT');
  ensureColumn(db, 'bank_agent_workflows', 'execution_policy', "TEXT NOT NULL DEFAULT 'sequential'");
  ensureColumn(db, 'bank_agent_workflows', 'suspension_reason', 'TEXT');
  ensureColumn(db, 'bank_agent_workflows', 'last_outcome_json', "TEXT NOT NULL DEFAULT '{}'");
  ensureColumn(db, 'bank_agent_workflows', 'conversation_state_json', 'TEXT');
  ensureColumn(db, 'bank_agent_workflows', 'origin_conversation_id', 'TEXT');
  ensureColumn(db, 'bank_agent_workflows', 'bound_conversation_id', 'TEXT');
  db.run(`UPDATE bank_agent_workflows
    SET origin_conversation_id = COALESCE(origin_conversation_id, conversation_id),
        bound_conversation_id = COALESCE(bound_conversation_id, conversation_id)`);
  db.run(`CREATE INDEX IF NOT EXISTS idx_bank_agent_workflow_bound
    ON bank_agent_workflows(customer_id, bound_conversation_id, updated_at)`);
  ensureColumn(db, 'bank_agent_clarifications', 'business_workflow_id', 'TEXT');
  ensureColumn(db, 'bank_agent_clarifications', 'task_id', 'TEXT');
  ensureColumn(db, 'bank_agent_clarifications', 'expected_workflow_revision', 'INTEGER');
  ensureColumn(db, 'bank_payees', 'is_saved', 'INTEGER NOT NULL DEFAULT 1');
  ensureColumn(db, 'bank_payees', 'expires_at', 'TEXT');
}

function ensureColumn(db, table, column, definition) {
  const columns = queryAll(db, 'PRAGMA table_info(' + table + ')');
  if (!columns.some(item => item.name === column)) db.run('ALTER TABLE ' + table + ' ADD COLUMN ' + column + ' ' + definition);
}

function seedSync(db, timestamp) {
  const c = SEED.customer;
  run(db, 'INSERT INTO bank_customers (id,name,status,city,created_at) VALUES (?,?,?,?,?)', [c.id, c.name, c.status, c.city, timestamp]);
  // 添加 cust_001：记忆数据（KV 存储 38 条 + 治理标签 35 条）存储在此客户下
  run(db, 'INSERT OR IGNORE INTO bank_customers (id,name,status,city,created_at) VALUES (?,?,?,?,?)', ['cust_001', '测试客户', 'ACTIVE', '北京', timestamp]);
  SEED.accounts.forEach((account, index) => run(db, 'INSERT INTO bank_accounts (id,customer_id,masked_no,name,type,currency,status,visibility,sort_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)', [account.id, account.customerId, account.maskedNo, account.name, account.type, account.currency, account.status, account.visibility, index, timestamp]));
  // cust_001 基础账户（记忆召回测试用）
  run(db, 'INSERT OR IGNORE INTO bank_accounts (id,customer_id,masked_no,name,type,currency,status,visibility,sort_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)', ['acct_test_salary_0001', 'cust_001', '**** **** **** 0001', '工资卡', 'DEBIT_CARD', 'CNY', 'ACTIVE', 'CUSTOMER', 99, timestamp]);
  run(db, 'INSERT OR IGNORE INTO bank_accounts (id,customer_id,masked_no,name,type,currency,status,visibility,sort_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)', ['acct_test_saving_8032', 'cust_001', '**** **** **** 8032', '储蓄卡', 'SAVINGS', 'CNY', 'ACTIVE', 'CUSTOMER', 98, timestamp]);
  run(db, 'INSERT OR IGNORE INTO bank_balance_snapshots (account_id,available_minor,ledger_minor,version,updated_at) VALUES (?,?,?,?,?)', ['acct_test_salary_0001', 2000000, 2000000, 1, timestamp]);
  run(db, 'INSERT OR IGNORE INTO bank_balance_snapshots (account_id,available_minor,ledger_minor,version,updated_at) VALUES (?,?,?,?,?)', ['acct_test_saving_8032', 7582000, 7582000, 1, timestamp]);
  SEED.balances.forEach(balance => run(db, 'INSERT INTO bank_balance_snapshots (account_id,available_minor,ledger_minor,version,updated_at) VALUES (?,?,?,?,?)', [balance.accountId, balance.availableMinor, balance.ledgerMinor, balance.version, timestamp]));
  SEED.payees.forEach((payee, index) => run(db, 'INSERT INTO bank_payees (id,customer_id,name,bank_name,masked_no,status,target_account_id,sort_order,created_at) VALUES (?,?,?,?,?,?,?,?,?)', [payee.id, payee.customerId, payee.name, payee.bankName, payee.maskedNo, payee.status, payee.targetAccountId, index, timestamp]));
  (SEED.customerPoints || []).forEach(item => {
    // 幂等：scenario reset 复用 seedSync 但不清静态种子表，重复插入会触发 UNIQUE 冲突
    if (queryOne(db, 'SELECT customer_id FROM bank_customer_points WHERE customer_id = ?', [item.customerId])) return;
    run(db, 'INSERT INTO bank_customer_points (customer_id,balance,expiring_amount,expiry_date,updated_at) VALUES (?,?,?,?,?)', [item.customerId, item.balance, item.expiringAmount, item.expiryDate, timestamp]);
  });
  seedCreditCardsSync(db, timestamp);
  seedUtilityDomainSync(db, timestamp);
  seedLifeDomainSync(db, timestamp);
  // 富裕域种子：期初总账 + 一年收支流水/发薪 + 理财 + 贷款 + 信用卡历史流水（截至 2026-08-26）
  seedWealthDomainSync(db, timestamp);
  // 原 seed 的 8 月末流水（2026-08-27~29，时间上晚于历史窗口）
  SEED.ledgerTransactions.forEach(item => run(db, 'INSERT INTO bank_ledger_transactions (id,business_type,business_id,currency,posted_at) VALUES (?,?,?,?,?)', [item.id, item.businessType, item.businessId, item.currency, item.postedAt]));
  SEED.ledgerEntries.forEach(item => run(db, 'INSERT INTO bank_ledger_entries (id,ledger_transaction_id,account_id,direction,amount_minor,description) VALUES (?,?,?,?,?,?)', [item.id, item.ledgerTransactionId, item.accountId, item.direction, item.amountMinor, item.description]));
  SEED.creditCardTransactions.forEach(item => run(db, 'INSERT INTO bank_credit_card_transactions (id,card_id,direction,amount_minor,currency,description,status,occurred_at) VALUES (?,?,?,?,?,?,?,?)', [item.id, item.cardId, item.direction, item.amountMinor, item.currency, item.description, item.status, item.occurredAt]));
}

function seedUtilityDomainSync(db, timestamp) {
  if (queryOne(db, 'SELECT id FROM bank_utility_bills LIMIT 1')) return;
  for (const persona of MOCK_PERSONAS.personas || []) {
    for (const event of persona.events || []) {
      if (event.type !== 'utility_bill_due') continue;
      if (!queryOne(db, 'SELECT id FROM bank_customers WHERE id = ?', [persona.customerId])) continue;
      const due = new Date(Date.parse(event.occurredAt) + Number(event.payload.daysToDue || 0) * 86400000);
      const reference = String(event.payload.billRef || 'utility');
      const names = { electricity: '电费', water: '水费', gas: '燃气费' };
      run(db, `INSERT INTO bank_utility_bills
        (id,customer_id,bill_type,name,period,amount_minor,currency,due_date,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)`, [
        event.id, persona.customerId, reference.toUpperCase(), names[reference] || '生活缴费',
        event.occurredAt.slice(0, 7), Number(event.payload.amountMinor), 'CNY',
        due.toISOString().slice(0, 10), 'UNPAID', timestamp, timestamp,
      ]);
    }
  }
}

function seedCreditCardsSync(db, timestamp) {
  if (queryOne(db, 'SELECT id FROM bank_credit_cards LIMIT 1')) return;
  SEED.creditCards.forEach((card, index) => run(db, 'INSERT INTO bank_credit_cards (id,customer_id,masked_no,product_name,network,card_type,status,total_limit_minor,available_limit_minor,cash_limit_minor,statement_day,due_day,sort_order,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', [card.id, card.customerId, card.maskedNo, card.productName, card.network, card.cardType, card.status, card.totalLimitMinor, card.availableLimitMinor, card.cashLimitMinor, card.statementDay, card.dueDay, index, timestamp, timestamp]));
  SEED.creditCardSettings.forEach(item => run(db, 'INSERT INTO bank_credit_card_settings (card_id,online_transactions_enabled,contactless_enabled,overseas_enabled,single_transaction_limit_minor,daily_limit_minor,updated_at) VALUES (?,?,?,?,?,?,?)', [item.cardId, item.onlineTransactionsEnabled ? 1 : 0, item.contactlessEnabled ? 1 : 0, item.overseasEnabled ? 1 : 0, item.singleTransactionLimitMinor, item.dailyLimitMinor, timestamp]));
}

function seedWealthDomainSync(db, timestamp) {
  const seed = WEALTH_SEED;
  seed.products.forEach(item => run(db, `INSERT INTO bank_financial_products
    (id,category,name,issuer,product_code,currency,risk_level,min_purchase_minor,min_increment_minor,term_type,term_days,annual_rate_bp,rate_type,rate_note,purchase_channel,status,features_json,sort_order,created_at,updated_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, [item.id, item.category, item.name, item.issuer, item.productCode, 'CNY', item.riskLevel, item.minPurchaseMinor, item.minIncrementMinor, item.termType, item.termDays, item.annualRateBp, item.rateType, item.rateNote, item.purchaseChannel, item.status, JSON.stringify(item.features || {}), item.sortOrder, timestamp, timestamp]));
  seed.ledgerTransactions.forEach(item => run(db, 'INSERT INTO bank_ledger_transactions (id,business_type,business_id,currency,posted_at) VALUES (?,?,?,?,?)', [item.id, item.businessType, item.businessId, item.currency, item.postedAt]));
  seed.ledgerEntries.forEach(item => run(db, 'INSERT INTO bank_ledger_entries (id,ledger_transaction_id,account_id,direction,amount_minor,description) VALUES (?,?,?,?,?,?)', [item.id, item.ledgerTransactionId, item.accountId, item.direction, item.amountMinor, item.description]));
  seed.accountTransactions.forEach(item => run(db, `INSERT INTO bank_account_transactions
    (id,account_id,customer_id,direction,amount_minor,currency,business_type,category,counterparty,description,channel,balance_after_minor,occurred_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`, [item.id, item.accountId, item.customerId, item.direction, item.amountMinor, item.currency, item.businessType, item.category, item.counterparty, item.description, item.channel, item.balanceAfterMinor, item.occurredAt]));
  seed.wealthHoldings.forEach(item => run(db, `INSERT INTO bank_wealth_holdings
    (id,customer_id,account_id,product_id,product_name,category,principal_minor,current_value_minor,accumulated_earnings_minor,currency,status,opened_at,matures_at,last_valued_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, [item.id, item.customerId, item.accountId, item.productId, item.productName, item.category, item.principalMinor, item.currentValueMinor, item.accumulatedEarningsMinor, item.currency, item.status, item.openedAt, item.maturesAt, item.lastValuedAt]));
  seed.wealthTransactions.forEach(item => run(db, `INSERT INTO bank_wealth_transactions
    (id,customer_id,account_id,product_id,product_name,direction,amount_minor,earnings_minor,currency,status,occurred_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)`, [item.id, item.customerId, item.accountId, item.productId, item.productName, item.direction, item.amountMinor, item.earningsMinor, item.currency, item.status, item.occurredAt]));
  seed.loans.forEach(item => run(db, `INSERT INTO bank_loans
    (id,customer_id,product_id,product_name,loan_type,repayment_account_id,contract_no,principal_minor,outstanding_principal_minor,currency,annual_rate_bp,rate_type,term_months,remaining_months,repayment_method,monthly_payment_minor,monthly_payment_day,status,disbursed_at,settled_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, [item.id, item.customerId, item.productId, item.productName, item.loanType, item.repaymentAccountId, item.contractNo, item.principalMinor, item.outstandingPrincipalMinor, item.currency, item.annualRateBp, item.rateType, item.termMonths, item.remainingMonths, item.repaymentMethod, item.monthlyPaymentMinor, item.monthlyPaymentDay, item.status, item.disbursedAt, item.settledAt]));
  seed.loanRepayments.forEach(item => run(db, `INSERT INTO bank_loan_repayments
    (id,loan_id,customer_id,period_no,due_date,principal_minor,interest_minor,total_minor,status,paid_at)
    VALUES (?,?,?,?,?,?,?,?,?,?)`, [item.id, item.loanId, item.customerId, item.periodNo, item.dueDate, item.principalMinor, item.interestMinor, item.totalMinor, item.status, item.paidAt]));
  // 信用卡数据由 seedCreditCardsSync 统一种子，避免重复插入导致数量不一致
  if (!queryOne(db, 'SELECT id FROM bank_credit_cards LIMIT 1')) {
    seed.creditCards.forEach((card, index) => run(db, 'INSERT INTO bank_credit_cards (id,customer_id,masked_no,product_name,network,card_type,status,total_limit_minor,available_limit_minor,cash_limit_minor,statement_day,due_day,sort_order,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', [card.id, card.customerId, card.maskedNo, card.productName, card.network, card.cardType, card.status, card.totalLimitMinor, card.availableLimitMinor, card.cashLimitMinor, card.statementDay, card.dueDay, SEED.creditCards.length + index, card.createdAt, card.createdAt]));
    seed.creditCardSettings.forEach(item => run(db, 'INSERT INTO bank_credit_card_settings (card_id,online_transactions_enabled,contactless_enabled,overseas_enabled,single_transaction_limit_minor,daily_limit_minor,updated_at) VALUES (?,?,?,?,?,?,?)', [item.cardId, item.onlineTransactionsEnabled ? 1 : 0, item.contactlessEnabled ? 1 : 0, item.overseasEnabled ? 1 : 0, item.singleTransactionLimitMinor, item.dailyLimitMinor, timestamp]));
    seed.creditCardTransactions.forEach(item => run(db, 'INSERT INTO bank_credit_card_transactions (id,card_id,direction,amount_minor,currency,description,status,occurred_at) VALUES (?,?,?,?,?,?,?,?)', [item.id, item.cardId, item.direction, item.amountMinor, item.currency, item.description, item.status, item.occurredAt]));
  }
}

function seedWealthRiskAssessmentSync(db) {
  if (queryOne(db, 'SELECT id FROM bank_wealth_risk_assessments WHERE customer_id = ?', ['cust_demo_001'])) return;
  if (!queryOne(db, 'SELECT id FROM bank_customers WHERE id = ?', ['cust_demo_001'])) return;
  run(db, `INSERT INTO bank_wealth_risk_assessments
    (id,customer_id,level,status,assessed_at,expires_at,source,version)
    VALUES (?,?,?,?,?,?,?,?)`, [
    'wra_demo_001', 'cust_demo_001', 'C2', 'ACTIVE',
    '2026-03-12T09:30:00.000+08:00', '2027-03-12T23:59:59.000+08:00',
    'CORE_WEALTH_SUITABILITY', 1,
  ]);
}

function seedCreditCardDomainSync(db, timestamp) {
  if (queryOne(db, 'SELECT id FROM bank_credit_cards LIMIT 1')) return;
  SEED.creditCards.forEach((card, index) => run(db, 'INSERT INTO bank_credit_cards (id,customer_id,masked_no,product_name,network,card_type,status,total_limit_minor,available_limit_minor,cash_limit_minor,statement_day,due_day,sort_order,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', [card.id, card.customerId, card.maskedNo, card.productName, card.network, card.cardType, card.status, card.totalLimitMinor, card.availableLimitMinor, card.cashLimitMinor, card.statementDay, card.dueDay, index, timestamp, timestamp]));
  SEED.creditCardSettings.forEach(item => run(db, 'INSERT INTO bank_credit_card_settings (card_id,online_transactions_enabled,contactless_enabled,overseas_enabled,single_transaction_limit_minor,daily_limit_minor,updated_at) VALUES (?,?,?,?,?,?,?)', [item.cardId, item.onlineTransactionsEnabled ? 1 : 0, item.contactlessEnabled ? 1 : 0, item.overseasEnabled ? 1 : 0, item.singleTransactionLimitMinor, item.dailyLimitMinor, timestamp]));
  SEED.creditCardTransactions.forEach(item => run(db, 'INSERT INTO bank_credit_card_transactions (id,card_id,direction,amount_minor,currency,description,status,occurred_at) VALUES (?,?,?,?,?,?,?,?)', [item.id, item.cardId, item.direction, item.amountMinor, item.currency, item.description, item.status, item.occurredAt]));
}

function seedTransferReferenceDataSync(db, timestamp) {
  SEED.payees.forEach((payee, index) => {
    if (queryOne(db, 'SELECT id FROM bank_payees WHERE id = ?', [payee.id])) return;
    if (!queryOne(db, 'SELECT id FROM bank_customers WHERE id = ?', [payee.customerId])) return;
    if (!queryOne(db, 'SELECT id FROM bank_accounts WHERE id = ?', [payee.targetAccountId])) return;
    run(db, 'INSERT INTO bank_payees (id,customer_id,name,bank_name,masked_no,status,target_account_id,sort_order,created_at) VALUES (?,?,?,?,?,?,?,?,?)', [payee.id, payee.customerId, payee.name, payee.bankName, payee.maskedNo, payee.status, payee.targetAccountId, index, timestamp]);
  });
  // 积分种子对已有库也要回填（新表对旧库为空，getCustomerPoints 会静默返回 null）
  (SEED.customerPoints || []).forEach(item => {
    if (queryOne(db, 'SELECT customer_id FROM bank_customer_points WHERE customer_id = ?', [item.customerId])) return;
    if (!queryOne(db, 'SELECT id FROM bank_customers WHERE id = ?', [item.customerId])) return;
    run(db, 'INSERT INTO bank_customer_points (customer_id,balance,expiring_amount,expiry_date,updated_at) VALUES (?,?,?,?,?)', [item.customerId, item.balance, item.expiringAmount, item.expiryDate, timestamp]);
  });
}

// 生活服务域种子（天气/优惠）：无 FK 依赖、reset 不清表，逐行 skip 保证幂等；
// 空库 seedSync 与旧库启动回填共用，任何路径下重复执行都不会触发约束冲突。
function seedLifeDomainSync(db, timestamp) {
  LIFE_SEED.weatherForecast.forEach(item => {
    if (queryOne(db, 'SELECT city FROM bank_weather_forecast WHERE city = ? AND offset_day = ?', [LIFE_SEED.city, item.offset])) return;
    run(db, 'INSERT INTO bank_weather_forecast (city,offset_day,condition,temp_low,temp_high) VALUES (?,?,?,?,?)', [LIFE_SEED.city, item.offset, item.condition, item.tempLow, item.tempHigh]);
  });
  LIFE_SEED.merchantOffers.forEach(item => {
    if (queryOne(db, 'SELECT id FROM bank_merchant_offers WHERE id = ?', [item.id])) return;
    run(db, 'INSERT INTO bank_merchant_offers (id,merchant,category,title,discount_text,valid_from,valid_to) VALUES (?,?,?,?,?,?,?)', [item.id, item.merchant, item.category, item.title, item.discountText, item.validFrom, item.validTo]);
  });
}

function clearTables(db) {
  [
    'bank_next_action_workflow_bindings', 'bank_next_action_decisions', 'bank_next_action_recommendations',
    'bank_next_action_exposures',
    'bank_utility_payments', 'bank_utility_bills',
    'bank_loan_repayments', 'bank_loans', 'bank_wealth_risk_assessments', 'bank_wealth_transactions', 'bank_wealth_holdings',
    'bank_financial_products', 'bank_account_transactions',
    'bank_audit_events', 'bank_agent_capability_grants', 'bank_agent_action_checkpoints', 'bank_agent_memory_commands', 'bank_agent_session_turns', 'bank_agent_session_focus', 'bank_agent_sessions', 'bank_agent_run_plannable_capabilities',
    'bank_agent_run_capabilities', 'bank_agent_run_events', 'bank_agent_run_workflows', 'bank_agent_workflow_events', 'bank_agent_workflows', 'bank_agent_conversation_states', 'bank_agent_conversation_slots', 'bank_agent_clarifications', 'bank_agent_runs',
    'bank_capability_invocations', 'bank_idempotency_records', 'bank_transfer_executions',
    'bank_transfer_authorizations', 'bank_confirmation_challenges', 'bank_transfer_validations', 'bank_transfer_orders',
    'bank_credit_card_limit_change_requests', 'bank_credit_card_transactions', 'bank_credit_card_settings', 'bank_credit_cards',
    'bank_ledger_entries', 'bank_ledger_transactions', 'bank_payees', 'bank_customer_points', 'bank_balance_snapshots', 'bank_accounts', 'bank_customers',
  ].forEach(table => db.run('DELETE FROM ' + table));
}

function createTransactionFacade(db, now) {
  return {
    queryOne: (sql, params) => queryOne(db, sql, params),
    queryAll: (sql, params) => queryAll(db, sql, params),
    run: (sql, params) => run(db, sql, params),
    getBalance: accountId => mapBalance(queryOne(db, 'SELECT * FROM bank_balance_snapshots WHERE account_id = ?', [accountId])),
    updateBalance: (accountId, availableMinor, ledgerMinor, expectedVersion) => {
      run(db, 'UPDATE bank_balance_snapshots SET available_minor = ?, ledger_minor = ?, version = version + 1, updated_at = ? WHERE account_id = ? AND version = ?', [availableMinor, ledgerMinor, now(), accountId, expectedVersion]);
      if (db.getRowsModified() !== 1) throw new BankApiError(409, 'account_version_conflict', '账户余额已发生变化，请重新确认');
      return mapBalance(queryOne(db, 'SELECT * FROM bank_balance_snapshots WHERE account_id = ?', [accountId]));
    },
    insertLedgerTransaction: item => {
      run(db, 'INSERT INTO bank_ledger_transactions (id,business_type,business_id,currency,posted_at) VALUES (?,?,?,?,?)', [item.id, item.businessType, item.businessId, item.currency, item.postedAt]);
      return item.id;
    },
    insertLedgerEntry: item => {
      run(db, 'INSERT INTO bank_ledger_entries (id,ledger_transaction_id,account_id,direction,amount_minor,description) VALUES (?,?,?,?,?,?)', [item.id, item.ledgerTransactionId, item.accountId, item.direction, item.amountMinor, item.description]);
      return item.id;
    },
    insertTransferOrder: item => insertTransferOrder(db, item),
    getTransferOrder: id => mapTransferOrder(queryOne(db, 'SELECT * FROM bank_transfer_orders WHERE id = ?', [id])),
    updateTransferStatus: (id, fromStatus, toStatus) => {
      run(db, 'UPDATE bank_transfer_orders SET status = ?, updated_at = ? WHERE id = ? AND status = ?', [toStatus, now(), id, fromStatus]);
      if (db.getRowsModified() !== 1) throw new BankApiError(409, 'state_conflict', '转账指令状态不允许当前操作');
      return mapTransferOrder(queryOne(db, 'SELECT * FROM bank_transfer_orders WHERE id = ?', [id]));
    },
    insertValidation: item => insertValidation(db, item),
    getLatestValidation: orderId => mapValidation(queryOne(db, 'SELECT * FROM bank_transfer_validations WHERE transfer_order_id = ? ORDER BY created_at DESC, id DESC LIMIT 1', [orderId])),
    insertChallenge: item => insertChallenge(db, item),
    getChallenge: id => mapChallenge(queryOne(db, 'SELECT * FROM bank_confirmation_challenges WHERE id = ?', [id])),
    markChallengeUsed: (id, usedAt) => {
      run(db, "UPDATE bank_confirmation_challenges SET status = 'USED', used_at = ? WHERE id = ? AND status = 'ACTIVE'", [usedAt, id]);
      if (db.getRowsModified() !== 1) throw new BankApiError(409, 'state_conflict', '确认挑战状态不允许当前操作');
    },
    invalidateChallenge: (id, invalidatedAt, reason) => {
      run(db, "UPDATE bank_confirmation_challenges SET status = 'INVALIDATED', invalidated_at = ?, invalidation_reason = ? WHERE id = ? AND status = 'ACTIVE'", [invalidatedAt, reason, id]);
      if (db.getRowsModified() !== 1) throw new BankApiError(409, 'state_conflict', '确认挑战状态不允许当前操作');
      return mapChallenge(queryOne(db, 'SELECT * FROM bank_confirmation_challenges WHERE id = ?', [id]));
    },
    invalidateActiveChallenges: (orderId, invalidatedAt, reason) => {
      run(db, "UPDATE bank_confirmation_challenges SET status = 'INVALIDATED', invalidated_at = ?, invalidation_reason = ? WHERE transfer_order_id = ? AND status = 'ACTIVE'", [invalidatedAt, reason, orderId]);
      return db.getRowsModified();
    },
    insertAuthorization: item => insertAuthorization(db, item),
    getAuthorization: orderId => mapAuthorization(queryOne(db, 'SELECT * FROM bank_transfer_authorizations WHERE transfer_order_id = ?', [orderId])),
    insertExecution: item => insertExecution(db, item),
    getExecutionByOrder: orderId => mapExecution(queryOne(db, 'SELECT * FROM bank_transfer_executions WHERE transfer_order_id = ?', [orderId])),
    getAccountById: id => mapAccount(queryOne(db, 'SELECT * FROM bank_accounts WHERE id = ?', [id])),
    getPayeeById: id => mapPayee(queryOne(db, 'SELECT * FROM bank_payees WHERE id = ?', [id])),
    getCreditCard: id => mapCreditCard(queryOne(db, 'SELECT * FROM bank_credit_cards WHERE id = ?', [id])),
    updateCreditCardStatus: (id, fromStatus, toStatus) => {
      run(db, 'UPDATE bank_credit_cards SET status = ?, updated_at = ? WHERE id = ? AND status = ?', [toStatus, now(), id, fromStatus]);
      if (db.getRowsModified() !== 1) throw new BankApiError(409, 'state_conflict', '信用卡状态不允许当前操作');
      return mapCreditCard(queryOne(db, 'SELECT * FROM bank_credit_cards WHERE id = ?', [id]));
    },
    insertCreditCardLimitChange: item => {
      run(db, 'INSERT INTO bank_credit_card_limit_change_requests (id,card_id,customer_id,requested_limit_minor,status,created_at) VALUES (?,?,?,?,?,?)', [item.id, item.cardId, item.customerId, item.requestedLimitMinor, item.status, item.createdAt]);
      return mapCreditCardLimitChange(queryOne(db, 'SELECT * FROM bank_credit_card_limit_change_requests WHERE id = ?', [item.id]));
    },
    updateCreditCardSettings: (cardId, item) => {
      run(db, 'UPDATE bank_credit_card_settings SET online_transactions_enabled = ?, contactless_enabled = ?, overseas_enabled = ?, single_transaction_limit_minor = ?, daily_limit_minor = ?, updated_at = ? WHERE card_id = ?', [item.onlineTransactionsEnabled ? 1 : 0, item.contactlessEnabled ? 1 : 0, item.overseasEnabled ? 1 : 0, item.singleTransactionLimitMinor, item.dailyLimitMinor, now(), cardId]);
      if (db.getRowsModified() !== 1) throw new BankApiError(404, 'not_found', '资源不存在');
      return mapCreditCardSettings(queryOne(db, 'SELECT * FROM bank_credit_card_settings WHERE card_id = ?', [cardId]));
    },
    getIdempotency: (scope, key) => mapIdempotency(queryOne(db, 'SELECT * FROM bank_idempotency_records WHERE scope = ? AND key = ?', [scope, key])),
    saveIdempotency: item => run(db, 'INSERT INTO bank_idempotency_records (scope,key,request_hash,response_json,status,created_at) VALUES (?,?,?,?,?,?)', [item.scope, item.key, item.requestHash, JSON.stringify(item.response), item.status || 'SUCCEEDED', item.createdAt || now()]),
    insertAudit: item => run(db, 'INSERT INTO bank_audit_events (id,trace_id,actor_type,actor_id,action,resource_type,resource_id,outcome,details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)', [item.id, item.traceId, item.actorType, item.actorId || null, item.action, item.resourceType, item.resourceId || null, item.outcome, JSON.stringify(item.details || {}), item.createdAt || now()]),
    insertCapabilityInvocation: item => run(db, 'INSERT INTO bank_capability_invocations (id,capability_id,actor_type,actor_id,status,input_hash,output_json,trace_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)', [item.id, item.capabilityId, item.actorType, item.actorId || null, item.status, item.inputHash, JSON.stringify(item.output || {}), item.traceId, item.createdAt || now()]),
  };
}

function insertTransferOrder(db, item) {
  run(db, 'INSERT INTO bank_transfer_orders (id,customer_id,from_account_id,payee_id,amount_minor,currency,purpose,status,balance_version,request_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', [item.id, item.customerId, item.fromAccountId, item.payeeId, item.amountMinor, item.currency, item.purpose, item.status, item.balanceVersion, item.requestHash, item.createdAt, item.updatedAt]);
  return mapTransferOrder(queryOne(db, 'SELECT * FROM bank_transfer_orders WHERE id = ?', [item.id]));
}

function insertValidation(db, item) {
  run(db, 'INSERT INTO bank_transfer_validations (id,transfer_order_id,result,checks_json,fee_minor,expires_at,created_at) VALUES (?,?,?,?,?,?,?)', [item.id, item.transferOrderId, item.result, JSON.stringify(item.checks), item.feeMinor || 0, item.expiresAt, item.createdAt]);
  return mapValidation(queryOne(db, 'SELECT * FROM bank_transfer_validations WHERE id = ?', [item.id]));
}

function insertChallenge(db, item) {
  run(db, 'INSERT INTO bank_confirmation_challenges (id,transfer_order_id,token_hash,summary_hash,expires_at,status,created_at) VALUES (?,?,?,?,?,?,?)', [item.id, item.transferOrderId, item.tokenHash, item.summaryHash, item.expiresAt, 'ACTIVE', item.createdAt]);
  return mapChallenge(queryOne(db, 'SELECT * FROM bank_confirmation_challenges WHERE id = ?', [item.id]));
}

function insertAuthorization(db, item) {
  run(db, 'INSERT INTO bank_transfer_authorizations (id,transfer_order_id,challenge_id,actor_id,summary_hash,authorized_at) VALUES (?,?,?,?,?,?)', [item.id, item.transferOrderId, item.challengeId, item.actorId, item.summaryHash, item.authorizedAt]);
  return mapAuthorization(queryOne(db, 'SELECT * FROM bank_transfer_authorizations WHERE id = ?', [item.id]));
}

function insertExecution(db, item) {
  run(db, 'INSERT INTO bank_transfer_executions (id,transfer_order_id,status,ledger_transaction_id,receipt_no,executed_at,failure_code,created_at) VALUES (?,?,?,?,?,?,?,?)', [item.id, item.transferOrderId, item.status, item.ledgerTransactionId || null, item.receiptNo || null, item.executedAt || null, item.failureCode || null, item.createdAt]);
  return mapExecution(queryOne(db, 'SELECT * FROM bank_transfer_executions WHERE id = ?', [item.id]));
}

function listTransactions(db, accountId, paging) {
  const opts = paging || {};
  const limit = Math.max(1, Math.min(Number(opts.limit) || 20, 100));
  const cursor = opts.cursor ? Number(Buffer.from(String(opts.cursor), 'base64url').toString('utf8')) : Number.MAX_SAFE_INTEGER;
  const rows = queryAll(db, `SELECT e.rowid AS cursor_id, e.id, e.direction, e.amount_minor, e.description,
    t.business_type, t.business_id, t.currency, t.posted_at
    FROM bank_ledger_entries e JOIN bank_ledger_transactions t ON t.id = e.ledger_transaction_id
    WHERE e.account_id = ? AND e.rowid < ? ORDER BY e.rowid DESC LIMIT ?`, [accountId, cursor, limit + 1]);
  const hasNext = rows.length > limit;
  const pageRows = rows.slice(0, limit);
  return {
    data: pageRows.map(row => ({ id: row.id, direction: row.direction, amountMinor: Number(row.amount_minor), signedMinor: row.direction === 'CREDIT' ? Number(row.amount_minor) : -Number(row.amount_minor), currency: row.currency, description: row.description, businessType: row.business_type, businessId: row.business_id, postedAt: row.posted_at })),
    nextCursor: hasNext ? Buffer.from(String(pageRows[pageRows.length - 1].cursor_id)).toString('base64url') : null,
  };
}

function listAccountTransactionDetails(db, customerId, accountId, paging) {
  const opts = paging || {};
  const limit = Math.max(1, Math.min(Number(opts.limit) || 20, 100));
  const cursor = opts.cursor ? Number(Buffer.from(String(opts.cursor), 'base64url').toString('utf8')) : Number.MAX_SAFE_INTEGER;
  const filters = [];
  const params = [customerId, accountId];
  if (opts.businessType) { filters.push('business_type = ?'); params.push(opts.businessType); }
  if (opts.category) { filters.push('category = ?'); params.push(opts.category); }
  const whereClause = filters.length ? ' AND ' + filters.join(' AND ') : '';
  const rows = queryAll(db, `SELECT rowid AS cursor_id, * FROM bank_account_transactions
    WHERE customer_id = ? AND account_id = ?${whereClause} AND rowid < ? ORDER BY rowid DESC LIMIT ?`,
  params.concat([cursor, limit + 1]));
  const hasNext = rows.length > limit;
  const pageRows = rows.slice(0, limit);
  return {
    data: pageRows.map(mapAccountTransactionDetail),
    nextCursor: hasNext ? Buffer.from(String(pageRows[pageRows.length - 1].cursor_id)).toString('base64url') : null,
  };
}

function mapAccountTransactionDetail(row) {
  return {
    id: row.id, accountId: row.account_id, customerId: row.customer_id,
    direction: row.direction, amountMinor: Number(row.amount_minor), currency: row.currency,
    businessType: row.business_type, category: row.category, counterparty: row.counterparty,
    description: row.description, channel: row.channel,
    balanceAfterMinor: Number(row.balance_after_minor), occurredAt: row.occurred_at,
  };
}

function mapFinancialProduct(row) {
  if (!row) return null;
  let features = {};
  try { features = JSON.parse(row.features_json || '{}'); } catch (parseError) { features = {}; }
  return {
    id: row.id, category: row.category, name: row.name, issuer: row.issuer,
    productCode: row.product_code, currency: row.currency, riskLevel: row.risk_level,
    minPurchaseMinor: Number(row.min_purchase_minor), minIncrementMinor: Number(row.min_increment_minor),
    termType: row.term_type, termDays: row.term_days == null ? null : Number(row.term_days),
    annualRateBp: row.annual_rate_bp == null ? null : Number(row.annual_rate_bp),
    rateType: row.rate_type, rateNote: row.rate_note, purchaseChannel: row.purchase_channel,
    status: row.status, features, sortOrder: Number(row.sort_order),
  };
}

function mapWealthHolding(row) {
  if (!row) return null;
  return {
    id: row.id, customerId: row.customer_id, accountId: row.account_id,
    productId: row.product_id, productName: row.product_name, category: row.category,
    principalMinor: Number(row.principal_minor), currentValueMinor: Number(row.current_value_minor),
    accumulatedEarningsMinor: Number(row.accumulated_earnings_minor), currency: row.currency,
    status: row.status, openedAt: row.opened_at, maturesAt: row.matures_at, lastValuedAt: row.last_valued_at,
  };
}

function mapWealthTransaction(row) {
  if (!row) return null;
  return {
    id: row.id, customerId: row.customer_id, accountId: row.account_id,
    productId: row.product_id, productName: row.product_name, direction: row.direction,
    amountMinor: Number(row.amount_minor), earningsMinor: Number(row.earnings_minor),
    currency: row.currency, status: row.status, occurredAt: row.occurred_at,
  };
}

function mapWealthRiskAssessment(row) {
  if (!row) return null;
  return {
    id: row.id,
    customerId: row.customer_id,
    level: row.level,
    status: row.status,
    assessedAt: row.assessed_at,
    expiresAt: row.expires_at,
    source: row.source,
    version: Number(row.version),
  };
}

function mapLoan(row) {
  if (!row) return null;
  return {
    id: row.id, customerId: row.customer_id, productId: row.product_id, productName: row.product_name,
    loanType: row.loan_type, repaymentAccountId: row.repayment_account_id, contractNo: row.contract_no,
    principalMinor: Number(row.principal_minor), outstandingPrincipalMinor: Number(row.outstanding_principal_minor),
    currency: row.currency, annualRateBp: Number(row.annual_rate_bp), rateType: row.rate_type,
    termMonths: Number(row.term_months), remainingMonths: Number(row.remaining_months),
    repaymentMethod: row.repayment_method, monthlyPaymentMinor: Number(row.monthly_payment_minor),
    monthlyPaymentDay: Number(row.monthly_payment_day), status: row.status,
    disbursedAt: row.disbursed_at, settledAt: row.settled_at,
  };
}

function mapLoanRepayment(row) {
  if (!row) return null;
  return {
    id: row.id, loanId: row.loan_id, customerId: row.customer_id, periodNo: Number(row.period_no),
    dueDate: row.due_date, principalMinor: Number(row.principal_minor),
    interestMinor: Number(row.interest_minor), totalMinor: Number(row.total_minor),
    status: row.status, paidAt: row.paid_at,
  };
}

function mapUtilityBill(row) {
  if (!row) return null;
  return {
    id: row.id, type: row.bill_type, name: row.name, period: row.period,
    amountMinor: Number(row.amount_minor), currency: row.currency,
    dueDate: row.due_date, status: row.status,
  };
}

function listCreditCardTransactions(db, cardId, paging) {
  const opts = paging || {};
  const limit = Math.max(1, Math.min(Number(opts.limit) || 20, 100));
  const cursor = opts.cursor ? Number(Buffer.from(String(opts.cursor), 'base64url').toString('utf8')) : Number.MAX_SAFE_INTEGER;
  const rows = queryAll(db, `SELECT rowid AS cursor_id, * FROM bank_credit_card_transactions
    WHERE card_id = ? AND rowid < ? ORDER BY rowid DESC LIMIT ?`, [cardId, cursor, limit + 1]);
  const hasNext = rows.length > limit;
  const pageRows = rows.slice(0, limit);
  return {
    data: pageRows.map(mapCreditCardTransaction),
    nextCursor: hasNext ? Buffer.from(String(pageRows[pageRows.length - 1].cursor_id)).toString('base64url') : null,
  };
}

function run(db, sql, params) {
  db.run(sql, params || []);
}

function queryOne(db, sql, params) {
  const rows = queryAll(db, sql, params);
  return rows.length ? rows[0] : null;
}

function queryAll(db, sql, params) {
  const stmt = db.prepare(sql);
  try {
    stmt.bind(params || []);
    const rows = [];
    while (stmt.step()) rows.push(stmt.getAsObject());
    return rows;
  } finally {
    stmt.free();
  }
}

function requireNonEmptyString(value, fieldName) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new BankApiError(400, 'invalid_agent_execution_record', fieldName + ' 不能为空');
  }
}

function requireNextActionId(value, field) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(value)) {
    throw new BankApiError(400, 'next_action_input_invalid', `${field} is invalid`);
  }
}

function assertExactOrOptionalKeys(item, required, optional, code) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) {
    throw new BankApiError(400, code, 'Input must be an object');
  }
  const allowed = new Set([...required, ...optional]);
  if (required.some(key => !Object.prototype.hasOwnProperty.call(item, key))
    || Object.keys(item).some(key => !allowed.has(key))) {
    throw new BankApiError(400, code, 'Input fields are invalid');
  }
}

function isSafeNextActionText(value, maximum) {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum && !/[<>]/.test(value);
}

function validateNextActionRecommendation(item) {
  const keys = [
    'recommendationId', 'scenarioId', 'scenarioVersion', 'type', 'title', 'priority',
    'surface', 'channel', 'status', 'score', 'rank', 'reasonCodes', 'reasonText',
    'action', 'targetIntentId', 'workflowTemplateId', 'requiredSkillIds',
    'requiredCapabilityIds', 'contextRefs', 'contextDigest', 'catalogAction',
    'generatedAt', 'expiresAt',
  ];
  const optionalKeys = ['memoryEnriched', 'memoryFacts'];
  assertExactOrOptionalKeys(item, keys, optionalKeys, 'next_action_recommendation_invalid');
  ['recommendationId', 'scenarioId', 'scenarioVersion']
    .forEach(field => requireNextActionId(item[field], field));
  if (item.targetIntentId !== null) requireNextActionId(item.targetIntentId, 'targetIntentId');
  if (item.workflowTemplateId !== null) requireNextActionId(item.workflowTemplateId, 'workflowTemplateId');
  if (!['resume_task', 'proactive_service', 'next_intent', 'care'].includes(item.type)
    || !['critical', 'high', 'normal', 'low'].includes(item.priority)
    || !['home', 'agent', 'channel'].includes(item.surface)
    || !['START_ASSISTANCE', 'OPEN_CATALOG', 'RESUME_WORKFLOW'].includes(item.action)
    || item.status !== 'generated') {
    throw new BankApiError(400, 'next_action_recommendation_invalid', 'Recommendation enum is invalid');
  }
  if (item.channel !== null) requireNextActionId(item.channel, 'channel');
  if (!isSafeNextActionText(item.title, 80) || !isSafeNextActionText(item.reasonText, 160)
    || !Number.isSafeInteger(item.score) || item.score < 0
    || !Number.isInteger(item.rank) || item.rank < 1 || item.rank > 3) {
    throw new BankApiError(400, 'next_action_recommendation_invalid', 'Recommendation value is invalid');
  }
  for (const [field, maximum] of [
    ['reasonCodes', 8], ['requiredSkillIds', 16], ['requiredCapabilityIds', 32], ['contextRefs', 32],
  ]) {
    if (!Array.isArray(item[field]) || item[field].length > maximum) {
      throw new BankApiError(400, 'next_action_recommendation_invalid', `${field} is invalid`);
    }
    item[field].forEach(value => requireNextActionId(value, field));
  }
  if (!/^[a-f0-9]{64}$/.test(item.contextDigest)
    || !Number.isFinite(Date.parse(item.generatedAt))
    || !Number.isFinite(Date.parse(item.expiresAt))
    || Date.parse(item.expiresAt) <= Date.parse(item.generatedAt)) {
    throw new BankApiError(400, 'next_action_recommendation_invalid', 'Recommendation evidence or lifetime is invalid');
  }
  validateNextActionCatalogAction(item.catalogAction);
  validateNextActionMemoryFacts(item);
}

function validateNextActionMemoryFacts(item) {
  if (item.memoryEnriched === undefined && item.memoryFacts === undefined) return;
  if (typeof item.memoryEnriched !== 'boolean' || !Array.isArray(item.memoryFacts)
    || item.memoryFacts.length < 1 || item.memoryFacts.length > 4) {
    throw new BankApiError(400, 'next_action_recommendation_invalid', 'Memory projection is invalid');
  }
  if (item.memoryEnriched !== true) {
    throw new BankApiError(400, 'next_action_recommendation_invalid', 'Memory projection is invalid');
  }
  item.memoryFacts.forEach(fact => {
    assertExactOrOptionalKeys(fact, ['predicate', 'safeLabel', 'safeValueSummary', 'evidenceRef', 'requiresConfirmation'], [],
      'next_action_recommendation_invalid');
    requireNextActionId(fact.predicate, 'memoryFacts.predicate');
    requireNextActionId(fact.evidenceRef, 'memoryFacts.evidenceRef');
    if (!isSafeNextActionText(fact.safeLabel, 80) || !isSafeNextActionText(fact.safeValueSummary, 160)
      || typeof fact.requiresConfirmation !== 'boolean') {
      throw new BankApiError(400, 'next_action_recommendation_invalid', 'Memory projection is invalid');
    }
  });
}

function validateNextActionCatalogAction(value) {
  if (value === null) return;
  assertExactOrOptionalKeys(value, ['channel', 'item', 'action'], [], 'next_action_recommendation_invalid');
  ['channel', 'item', 'action'].forEach(field => requireNextActionId(value[field], `catalogAction.${field}`));
}

function validateNextActionListOptions(options) {
  assertExactOrOptionalKeys(options, ['surface', 'channel', 'limit'], [], 'next_action_query_invalid');
  if (!['home', 'agent', 'channel'].includes(options.surface)
    || !Number.isInteger(options.limit) || options.limit < 1 || options.limit > 3) {
    throw new BankApiError(400, 'next_action_query_invalid', 'Recommendation query is invalid');
  }
  if (options.channel !== null) requireNextActionId(options.channel, 'channel');
}

function validateNextActionDecision(input) {
  assertExactOrOptionalKeys(input, [
    'recommendationId', 'customerId', 'decision', 'idempotencyKey', 'requestHash',
  ], ['snoozeUntil', 'feedbackCode'], 'next_action_decision_invalid');
  requireNextActionId(input.recommendationId, 'recommendationId');
  requireNextActionId(input.customerId, 'customerId');
  requireNextActionId(input.idempotencyKey, 'idempotencyKey');
  if (!Object.prototype.hasOwnProperty.call(NEXT_ACTION_DECISION_STATUS, input.decision)
    || typeof input.requestHash !== 'string' || !/^[a-f0-9]{64}$/.test(input.requestHash)) {
    throw new BankApiError(400, 'next_action_decision_invalid', 'Decision is invalid');
  }
  if (input.snoozeUntil !== undefined && !Number.isFinite(Date.parse(input.snoozeUntil))) {
    throw new BankApiError(400, 'next_action_decision_invalid', 'Snooze time is invalid');
  }
  if (input.feedbackCode !== undefined) requireNextActionId(input.feedbackCode, 'feedbackCode');
  if (input.decision === 'snooze' && input.snoozeUntil === undefined) {
    throw new BankApiError(400, 'next_action_decision_invalid', 'Snooze time is required');
  }
}

function validateDisableNextActionPersonalization(input) {
  assertExactOrOptionalKeys(input, [
    'recommendationId', 'customerId', 'decision', 'idempotencyKey', 'requestHash',
  ], ['feedbackCode'], 'next_action_decision_invalid');
  requireNextActionId(input.recommendationId, 'recommendationId');
  requireNextActionId(input.customerId, 'customerId');
  requireNextActionId(input.idempotencyKey, 'idempotencyKey');
  if (input.decision !== 'disable_personalization'
    || typeof input.requestHash !== 'string' || !/^[a-f0-9]{64}$/.test(input.requestHash)) {
    throw new BankApiError(400, 'next_action_decision_invalid', 'Personalization decision is invalid');
  }
  if (input.feedbackCode !== undefined) requireNextActionId(input.feedbackCode, 'feedbackCode');
}

function validateNextActionBinding(input) {
  assertExactOrOptionalKeys(input, [
    'recommendationId', 'customerId', 'agentSessionId', 'agentRunId',
    'businessWorkflowId', 'goalId',
  ], [], 'next_action_binding_invalid');
  ['recommendationId', 'customerId', 'agentSessionId', 'agentRunId', 'goalId']
    .forEach(field => requireNextActionId(input[field], field));
  if (input.businessWorkflowId !== null) requireNextActionId(input.businessWorkflowId, 'businessWorkflowId');
}

function validateNextActionOutcome(outcome) {
  if (!outcome || typeof outcome !== 'object' || Array.isArray(outcome)
    || Object.getPrototypeOf(outcome) !== Object.prototype) {
    throw new BankApiError(400, 'next_action_outcome_invalid', 'Outcome is invalid');
  }
  let encoded;
  try { encoded = JSON.stringify(outcome); } catch (_) { encoded = ''; }
  if (!encoded || Buffer.byteLength(encoded, 'utf8') > 4096 || /[<>]/.test(encoded)) {
    throw new BankApiError(400, 'next_action_outcome_invalid', 'Outcome is unsafe or too large');
  }
}

function projectNextActionPayload(item) {
  const payload = {
    recommendationId: item.recommendationId,
    scenarioId: item.scenarioId,
    scenarioVersion: item.scenarioVersion,
    type: item.type,
    title: item.title,
    priority: item.priority,
    reasonCodes: item.reasonCodes.slice(),
    reasonText: item.reasonText,
    action: item.action,
    targetIntentId: item.targetIntentId,
    workflowTemplateId: item.workflowTemplateId,
    requiredSkillIds: item.requiredSkillIds.slice(),
    requiredCapabilityIds: item.requiredCapabilityIds.slice(),
    catalogAction: item.catalogAction ? { ...item.catalogAction } : null,
  };
  if (item.memoryEnriched === true) {
    payload.memoryEnriched = true;
    payload.memoryFacts = item.memoryFacts.map(fact => ({ ...fact }));
  }
  return payload;
}

function projectNextActionRecommendation(row) {
  if (!row) return null;
  const payload = JSON.parse(row.safe_payload_json);
  return {
    ...payload,
    recommendationId: row.id,
    scenarioId: row.scenario_id,
    scenarioVersion: row.scenario_version,
    type: row.recommendation_type,
    surface: row.surface,
    channel: row.channel || null,
    status: row.status,
    score: Number(row.deterministic_score),
    rank: Number(row.final_rank),
    reasonCodes: JSON.parse(row.reason_codes_json),
    reasonText: row.reason_text,
    generatedAt: row.generated_at,
    expiresAt: row.expires_at,
    shownAt: row.shown_at || null,
    acceptedAt: row.accepted_at || null,
    completedAt: row.completed_at || null,
    outcome: row.outcome_json ? JSON.parse(row.outcome_json) : null,
  };
}

function projectNextActionDecision(row) {
  return {
    decisionId: row.id,
    recommendationId: row.recommendation_id,
    decision: row.decision,
    status: row.decision === 'disable_personalization'
      ? 'disabled' : NEXT_ACTION_DECISION_STATUS[row.decision],
    snoozeUntil: row.snooze_until || null,
    feedbackCode: row.feedback_code || null,
    createdAt: row.created_at,
  };
}

function assertNextActionTransition(fromStatus, toStatus) {
  if (!NEXT_ACTION_TRANSITIONS[fromStatus] || !NEXT_ACTION_TRANSITIONS[fromStatus].includes(toStatus)) {
    throw new BankApiError(409, 'recommendation_transition_invalid', `Cannot transition recommendation from ${fromStatus} to ${toStatus}`);
  }
}

function transitionNextActionRow(tx, id, customerId, fromStatus, toStatus, timestamp, fields = {}) {
  assertNextActionTransition(fromStatus, toStatus);
  tx.run(`UPDATE bank_next_action_recommendations SET status = ?, shown_at = COALESCE(?, shown_at),
    accepted_at = COALESCE(?, accepted_at), updated_at = ?
    WHERE id = ? AND customer_id = ? AND status = ?`, [
    toStatus, fields.shownAt || null, fields.acceptedAt || null, timestamp, id, customerId, fromStatus,
  ]);
  const updated = tx.queryOne(
    'SELECT status FROM bank_next_action_recommendations WHERE id = ? AND customer_id = ?',
    [id, customerId],
  );
  if (!updated || updated.status !== toStatus) {
    throw new BankApiError(409, 'recommendation_transition_invalid', 'Recommendation status changed concurrently');
  }
}

function nextActionBindingMatches(row, input) {
  return row.agent_session_id === input.agentSessionId
    && row.agent_run_id === input.agentRunId
    && (row.business_workflow_id || null) === (input.businessWorkflowId || null)
    && row.goal_id === input.goalId;
}

function projectNextActionBinding(row, status) {
  return {
    recommendationId: row.recommendation_id,
    agentSessionId: row.agent_session_id,
    agentRunId: row.agent_run_id,
    businessWorkflowId: row.business_workflow_id || null,
    goalId: row.goal_id,
    status,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

function assertSessionObject(item, keys) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) {
    throw new BankApiError(400, 'invalid_agent_session', 'Agent Session 数据无效');
  }
  const expected = new Set(keys);
  if (Object.keys(item).length !== keys.length
    || Object.keys(item).some(key => !expected.has(key))) {
    throw new BankApiError(400, 'invalid_agent_session', 'Agent Session 数据包含未知字段');
  }
}

function requireSessionIdentifier(value, field) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(value)) {
    throw new BankApiError(400, 'invalid_agent_session', `${field}无效`);
  }
}

function requireIsoTimestamp(value, field) {
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) {
    throw new BankApiError(400, 'invalid_agent_session', `${field}无效`);
  }
}

function validateAgentSession(item) {
  const keys = [
    'id', 'customerId', 'status', 'createdAt', 'lastActiveAt',
    'idleExpiresAt', 'absoluteExpiresAt',
  ];
  assertSessionObject(item, keys);
  requireSessionIdentifier(item.id, 'id');
  requireSessionIdentifier(item.customerId, 'customerId');
  if (item.status !== 'active') throw new BankApiError(400, 'invalid_agent_session', 'Session 初始状态无效');
  ['createdAt', 'lastActiveAt', 'idleExpiresAt', 'absoluteExpiresAt']
    .forEach(field => requireIsoTimestamp(item[field], field));
  if (Date.parse(item.lastActiveAt) < Date.parse(item.createdAt)
    || Date.parse(item.idleExpiresAt) <= Date.parse(item.lastActiveAt)
    || Date.parse(item.absoluteExpiresAt) <= Date.parse(item.createdAt)) {
    throw new BankApiError(400, 'invalid_agent_session', 'Agent Session 生命周期无效');
  }
}

function validateFocusedAgentSession(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)
    || Object.keys(item).length !== 2
    || !Object.prototype.hasOwnProperty.call(item, 'session')
    || !Object.prototype.hasOwnProperty.call(item, 'focus')) {
    throw new BankApiError(400, 'invalid_agent_session_focus', '推荐会话格式无效');
  }
  validateAgentSession(item.session);
  const focus = item.focus;
  if (!focus || typeof focus !== 'object' || Array.isArray(focus)
    || typeof focus.recommendationId !== 'string'
    || typeof focus.mode !== 'string'
    || typeof focus.targetIntentId !== 'string'
    || typeof focus.contextDigest !== 'string'
    || typeof focus.expiresAt !== 'string') {
    throw new BankApiError(400, 'invalid_agent_session_focus', '推荐会话上下文无效');
  }
}

function validateAgentSessionLookup(item) {
  assertSessionObject(item, ['id', 'customerId']);
  requireSessionIdentifier(item.id, 'id');
  requireSessionIdentifier(item.customerId, 'customerId');
}

function validateAgentSessionTouch(item) {
  assertSessionObject(item, ['id', 'customerId', 'idleTtlMs']);
  requireSessionIdentifier(item.id, 'id');
  requireSessionIdentifier(item.customerId, 'customerId');
  if (!Number.isSafeInteger(item.idleTtlMs) || item.idleTtlMs <= 0) {
    throw new BankApiError(400, 'invalid_agent_session', '空闲有效期无效');
  }
}

function validateAgentSessionClose(item) {
  assertSessionObject(item, ['id', 'customerId', 'status', 'closedAt', 'closeReason']);
  requireSessionIdentifier(item.id, 'id');
  requireSessionIdentifier(item.customerId, 'customerId');
  requireSessionIdentifier(item.closeReason, 'closeReason');
  requireIsoTimestamp(item.closedAt, 'closedAt');
  if (!['closed', 'expired'].includes(item.status)) {
    throw new BankApiError(400, 'invalid_agent_session', 'Session 关闭状态无效');
  }
}

function validateAtomicIdempotencyFields(item) {
  if (typeof item.idempotencyKey !== 'string'
    || !/^[A-Za-z0-9._:-]{1,128}$/.test(item.idempotencyKey)) {
    throw new BankApiError(400, 'invalid_idempotency_key', 'Idempotency-Key 格式不合法');
  }
  if (typeof item.requestHash !== 'string' || !/^[a-f0-9]{64}$/.test(item.requestHash)) {
    throw new BankApiError(400, 'invalid_agent_session', '幂等请求摘要无效');
  }
}

function validateAtomicSessionClose(item) {
  assertSessionObject(item, [
    'id', 'customerId', 'closeReason',
    'idempotencyKey', 'requestHash',
  ]);
  requireSessionIdentifier(item.id, 'id');
  requireSessionIdentifier(item.customerId, 'customerId');
  requireSessionIdentifier(item.closeReason, 'closeReason');
  validateAtomicIdempotencyFields(item);
}

function validateAtomicWorkflowBinding(item) {
  assertSessionObject(item, [
    'id', 'customerId', 'conversationId', 'expectedRevision',
    'idempotencyKey', 'requestHash',
  ]);
  requireSessionIdentifier(item.id, 'id');
  requireSessionIdentifier(item.customerId, 'customerId');
  requireSessionIdentifier(item.conversationId, 'conversationId');
  if (!Number.isInteger(item.expectedRevision) || item.expectedRevision < 1) {
    throw new BankApiError(400, 'invalid_agent_workflow_binding', 'Agent Workflow binding is invalid');
  }
  validateAtomicIdempotencyFields(item);
}

function requireIdempotencyReplayTx(tx, item, operation) {
  const existing = tx.getIdempotency(deriveAtomicIdempotencyScope(operation, item.customerId), item.idempotencyKey);
  if (!existing) return null;
  if (existing.requestHash !== item.requestHash) {
    throw new BankApiError(409, 'idempotency_conflict', '同一幂等键对应了不同请求');
  }
  return existing.response;
}

function saveIdempotencyResponseTx(tx, item, response, operation) {
  tx.saveIdempotency({
    scope: deriveAtomicIdempotencyScope(operation, item.customerId),
    key: item.idempotencyKey,
    requestHash: item.requestHash,
    response,
    status: 'SUCCEEDED',
  });
}

function deriveAtomicIdempotencyScope(operation, customerId) {
  return `${operation}:${customerId}`;
}

function projectAgentSessionCommandResponse(session) {
  return {
    id: session.id,
    status: session.status,
    createdAt: session.createdAt,
    lastActiveAt: session.lastActiveAt,
    idleExpiresAt: session.idleExpiresAt,
    absoluteExpiresAt: session.absoluteExpiresAt,
    closedAt: session.closedAt,
    closeReason: session.closeReason,
  };
}

function validateAgentSessionTurn(item) {
  const keys = ['sessionId', 'customerId', 'role', 'text', 'response'];
  if (Object.prototype.hasOwnProperty.call(item || {}, 'createdAt')) keys.push('createdAt');
  assertSessionObject(item, keys);
  requireSessionIdentifier(item.sessionId, 'sessionId');
  requireSessionIdentifier(item.customerId, 'customerId');
  if (Object.prototype.hasOwnProperty.call(item, 'createdAt')) {
    requireIsoTimestamp(item.createdAt, 'createdAt');
  }
  const rawBytes = Buffer.byteLength(JSON.stringify({
    role: item.role,
    text: item.text,
    response: item.response,
  }), 'utf8');
  if (rawBytes > MAX_AGENT_SESSION_TURN_BYTES) {
    throw new BankApiError(413, 'agent_session_turn_too_large', 'Session 轮次超过存储上限');
  }
  const projected = projectSessionTurn({
    role: item.role,
    text: item.text,
    response: item.response,
  });
  const projectedBytes = Buffer.byteLength(JSON.stringify(projected), 'utf8');
  if (projectedBytes > MAX_AGENT_SESSION_TURN_BYTES) {
    throw new BankApiError(413, 'agent_session_turn_too_large', 'Session 轮次超过存储上限');
  }
  return projected;
}

function validateAgentRun(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) {
    throw new BankApiError(400, 'invalid_agent_execution_record', 'Agent Run 数据无效');
  }
  ['id', 'customerId', 'conversationId', 'traceId', 'status', 'graphVersion', 'graphDigest']
    .forEach(field => requireNonEmptyString(item[field], field));
  if (!Object.prototype.hasOwnProperty.call(AGENT_RUN_TRANSITIONS, item.status)) {
    throw new BankApiError(400, 'invalid_agent_run_status', 'Agent Run 状态无效');
  }
  if (!/^sha256:[a-f0-9]{64}$/i.test(item.graphDigest)) {
    throw new BankApiError(400, 'invalid_graph_digest', '意图图谱摘要格式无效');
  }
  const executableCapabilityIds = validateCapabilityIdList(item.executableCapabilityIds);
  const plannableCapabilityIds = validateCapabilityIdList(item.plannableCapabilityIds);
  if (!executableCapabilityIds || !plannableCapabilityIds) {
    throw new BankApiError(400, 'invalid_agent_execution_record', 'Agent Run 能力白名单无效');
  }
  return { executableCapabilityIds, plannableCapabilityIds };
}

function validateCapabilityIdList(value) {
  const items = value == null ? [] : value;
  if (!Array.isArray(items) || items.length > 64 || new Set(items).size !== items.length
    || items.some(id => typeof id !== 'string' || !/^bank\.[a-z0-9.-]+$/.test(id))) return null;
  return [...items];
}

function validateConversationSlot(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) {
    throw new BankApiError(400, 'invalid_agent_execution_record', '会话槽位数据无效');
  }
  ['conversationId', 'customerId', 'slotName', 'sourceIntentId']
    .forEach(field => requireNonEmptyString(item[field], field));
  if (item.value === undefined) {
    throw new BankApiError(400, 'invalid_agent_execution_record', 'value 不能为空');
  }
  if (!isGraphDeclaredSemanticSlot(item.slotName, [item.sourceIntentId])) {
    throw new BankApiError(400, 'invalid_agent_execution_record', '会话槽位不属于当前意图图谱');
  }
}

function validateConversationSlotReplacement(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) {
    throw new BankApiError(400, 'invalid_agent_execution_record', '会话槽位数据无效');
  }
  ['conversationId', 'customerId', 'sourceIntentId']
    .forEach(field => requireNonEmptyString(item[field], field));
  if (!/^[a-z0-9.-]{1,160}$/.test(item.sourceIntentId)
    || !Array.isArray(item.slots) || item.slots.length > 16) {
    throw new BankApiError(400, 'invalid_agent_execution_record', '会话槽位契约无效');
  }
  const allowedSources = new Set(['user_explicit', 'user_selection', 'context', 'intent_default']);
  const names = new Set();
  const slots = item.slots.map(slot => {
    if (!slot || typeof slot !== 'object' || Array.isArray(slot)
      || !isGraphDeclaredSemanticSlot(slot.name, [item.sourceIntentId]) || names.has(slot.name)
      || !allowedSources.has(slot.source)) {
      throw new BankApiError(400, 'invalid_agent_execution_record', '会话槽位契约无效');
    }
    const scalar = slot.value;
    if (!['string', 'number', 'boolean'].includes(typeof scalar)
      || (typeof scalar === 'string' && (scalar.length < 1 || scalar.length > 240))
      || (typeof scalar === 'number' && !Number.isFinite(scalar))) {
      throw new BankApiError(400, 'invalid_agent_execution_record', '会话槽位值无效');
    }
    names.add(slot.name);
    return { name: slot.name, value: scalar, source: slot.source };
  });
  return {
    conversationId: item.conversationId,
    customerId: item.customerId,
    sourceIntentId: item.sourceIntentId,
    slots,
  };
}

function validateConversationStateReplacement(item) {
  assertExactObjectKeys(item, ['conversationId', 'customerId', 'state']);
  requireNonEmptyString(item.conversationId, 'conversationId');
  requireNonEmptyString(item.customerId, 'customerId');
  const state = item.state;
  assertExactObjectKeys(state, [
    'schemaVersion', 'pendingIntentId', 'slots', 'pendingEnvelopeId', 'envelopeRevision', 'intentShape',
    'goals', 'sharedSlots', 'ambiguities', 'status',
  ]);
  const allowedShapes = new Set(['single_intent', 'multi_intent', 'same_intent_multi_task', 'ambiguous_intent', 'non_task']);
  if (state.schemaVersion !== '2.0' || state.pendingIntentId !== null
    || !Array.isArray(state.slots) || state.slots.length !== 0
    || state.status !== 'retain'
    || typeof state.pendingEnvelopeId !== 'string' || !/^[A-Za-z0-9_.:-]{1,96}$/.test(state.pendingEnvelopeId)
    || !Number.isInteger(state.envelopeRevision) || state.envelopeRevision < 1
    || !allowedShapes.has(state.intentShape)
    || !Array.isArray(state.goals) || state.goals.length < 1 || state.goals.length > 16
    || !Array.isArray(state.sharedSlots) || state.sharedSlots.length > 16
    || !Array.isArray(state.ambiguities) || state.ambiguities.length > 8) {
    throw new BankApiError(400, 'invalid_agent_execution_record', '多目标会话状态无效');
  }
  const taskIds = new Set();
  const goals = state.goals.map(goal => {
    assertExactObjectKeys(goal, ['taskId', 'intentId', 'status', 'slots']);
    if (typeof goal.taskId !== 'string' || !/^[A-Za-z0-9_.:-]{1,96}$/.test(goal.taskId)
      || taskIds.has(goal.taskId)
      || typeof goal.intentId !== 'string' || !/^[a-z0-9.-]{1,160}$/.test(goal.intentId)
      || !['provisional', 'needs_slots', 'ready'].includes(goal.status)
      || !Array.isArray(goal.slots) || goal.slots.length > 16) {
      throw new BankApiError(400, 'invalid_agent_execution_record', '多目标会话目标无效');
    }
    taskIds.add(goal.taskId);
    return {
      taskId: goal.taskId,
      intentId: goal.intentId,
      status: goal.status,
      slots: validatePrivateConversationSlots(goal.slots, [goal.intentId]),
    };
  });
  const goalIntentIds = [...new Set(goals.map(goal => goal.intentId))];
  const sharedSlots = validatePrivateConversationSlots(state.sharedSlots, goalIntentIds);
  const ambiguities = state.ambiguities.map(ambiguity => {
    assertExactObjectKeys(ambiguity, ['type', 'taskIds', 'options']);
    if (!Array.isArray(ambiguity.taskIds)
      || ambiguity.taskIds.length < 2 || ambiguity.taskIds.length > 16
      || new Set(ambiguity.taskIds).size !== ambiguity.taskIds.length
      || ambiguity.taskIds.some(taskId => !taskIds.has(taskId))
      || !isBoundedAmbiguity(ambiguity.type, ambiguity.options)) {
      throw new BankApiError(400, 'invalid_agent_execution_record', '多目标会话歧义无效');
    }
    return { type: ambiguity.type, taskIds: [...ambiguity.taskIds], options: [...ambiguity.options] };
  });
  return {
    conversationId: item.conversationId,
    customerId: item.customerId,
    state: {
      schemaVersion: '2.0', pendingIntentId: null, slots: [],
      pendingEnvelopeId: state.pendingEnvelopeId,
      envelopeRevision: state.envelopeRevision, intentShape: state.intentShape,
      goals, sharedSlots, ambiguities, status: 'retain',
    },
  };
}

function validatePrivateConversationSlots(value, intentIds) {
  const allowedSources = new Set(['user_explicit', 'user_selection', 'context', 'intent_default']);
  const names = new Set();
  return value.map(slot => {
    assertExactObjectKeys(slot, ['name', 'value', 'source']);
    const scalar = slot.value;
    if (!isGraphDeclaredSemanticSlot(slot.name, intentIds)
      || names.has(slot.name) || !allowedSources.has(slot.source)
      || !['string', 'number', 'boolean'].includes(typeof scalar)
      || (typeof scalar === 'number' && !Number.isFinite(scalar))
    || (typeof scalar === 'string' && (scalar.length < 1 || scalar.length > 240
        || !isSafeAgentSessionText(scalar)))) {
      throw new BankApiError(400, 'invalid_agent_execution_record', '多目标会话槽位无效');
    }
    names.add(slot.name);
    return { name: slot.name, value: scalar, source: slot.source };
  });
}

function todoBelongsToDelegatedTask(todoId, delegatedTaskIds) {
  if (delegatedTaskIds.length === 1) return true;
  return delegatedTaskIds.some(taskId => todoId === taskId
    || todoId.startsWith(taskId + ':') || todoId.startsWith(taskId + '.'));
}

function validateAgentWorkflow(item) {
  try {
    assertExactObjectKeys(item, [
      'id', 'conversationId', 'customerId', 'status', 'delegatedTaskIds',
      'expectedRevision', 'envelopeRevision', 'workflowRevision',
      'todoRevision', 'todoSnapshot', 'todoDigest',
      'activeGoalTaskId', 'activeTodoId', 'executionPolicy',
      'suspensionReason', 'lastOutcome', 'pendingCheckpointIds',
    ]);
  } catch (error) {
    throw new BankApiError(400, 'invalid_agent_workflow', 'Agent Workflow 对象契约无效');
  }
  const safeId = /^[A-Za-z0-9_.:-]{1,160}$/;
  const statuses = new Set([
    'running', 'awaiting_clarification', 'awaiting_confirmation', 'waiting_external',
    'completed', 'partially_completed', 'failed',
  ]);
  if (![item.id, item.conversationId, item.customerId].every(value => typeof value === 'string' && safeId.test(value))
    || !statuses.has(item.status)
    || !Number.isInteger(item.expectedRevision) || item.expectedRevision < 0 || item.expectedRevision > 1000000
    || !Number.isInteger(item.envelopeRevision) || item.envelopeRevision < 1 || item.envelopeRevision > 1000000
    || !Number.isInteger(item.workflowRevision) || item.workflowRevision !== item.expectedRevision + 1
    || !Number.isInteger(item.todoRevision) || item.todoRevision < 1 || item.todoRevision > 1000000
    || !Array.isArray(item.delegatedTaskIds) || item.delegatedTaskIds.length < 1
    || item.delegatedTaskIds.length > 16 || new Set(item.delegatedTaskIds).size !== item.delegatedTaskIds.length
    || item.delegatedTaskIds.some(id => typeof id !== 'string' || !safeId.test(id))
    || !Array.isArray(item.todoSnapshot) || item.todoSnapshot.length < 1 || item.todoSnapshot.length > 128
    || !Array.isArray(item.pendingCheckpointIds) || item.pendingCheckpointIds.length > 16
    || new Set(item.pendingCheckpointIds).size !== item.pendingCheckpointIds.length
    || item.pendingCheckpointIds.some(id => typeof id !== 'string' || !safeId.test(id))
    || typeof item.todoDigest !== 'string' || !/^[a-f0-9]{64}$/.test(item.todoDigest)
    || item.executionPolicy !== 'sequential'
    || (item.activeGoalTaskId !== null
      && (typeof item.activeGoalTaskId !== 'string' || !safeId.test(item.activeGoalTaskId)))
    || (item.activeTodoId !== null
      && (typeof item.activeTodoId !== 'string' || !safeId.test(item.activeTodoId)))
    || (item.suspensionReason !== null
      && !['awaiting_clarification', 'awaiting_confirmation', 'waiting_external'].includes(item.suspensionReason))) {
    throw new BankApiError(400, 'invalid_agent_workflow', 'Agent Workflow 数据无效');
  }
  const delegatedTaskIds = [...item.delegatedTaskIds];
  const todoIds = new Set();
  const todoSnapshot = item.todoSnapshot.map(todo => {
    try {
      assertExactObjectKeys(todo, ['id', 'status', 'dependsOn']);
    } catch (error) {
      throw new BankApiError(400, 'invalid_agent_workflow', 'Todo 快照对象契约无效');
    }
    if (typeof todo.id !== 'string' || !safeId.test(todo.id) || todoIds.has(todo.id)
      || !['pending', 'in_progress', 'completed', 'cancelled'].includes(todo.status)
      || !Array.isArray(todo.dependsOn) || todo.dependsOn.length > 32
      || new Set(todo.dependsOn).size !== todo.dependsOn.length
      || todo.dependsOn.some(id => typeof id !== 'string' || !safeId.test(id))
      || !todoBelongsToDelegatedTask(todo.id, delegatedTaskIds)) {
      throw new BankApiError(400, 'invalid_agent_workflow', 'Todo 快照数据无效');
    }
    todoIds.add(todo.id);
    return { id: todo.id, status: todo.status, dependsOn: [...todo.dependsOn] };
  });
  if (todoSnapshot.some(todo => todo.dependsOn.some(id => !todoIds.has(id)))) {
    throw new BankApiError(400, 'invalid_agent_workflow', 'Todo 依赖不存在');
  }
  if (JSON.stringify(todoSnapshot).length > 32768) {
    throw new BankApiError(400, 'invalid_agent_workflow', 'Todo 快照过大');
  }
  const canonicalTodo = todoSnapshot.map(todo => ({
    dependsOn: [...todo.dependsOn].sort(), id: todo.id, status: todo.status,
  }));
  const calculatedDigest = crypto.createHash('sha256')
    .update(JSON.stringify(canonicalTodo)).digest('hex');
  if (calculatedDigest !== item.todoDigest
    || (item.activeGoalTaskId !== null && !delegatedTaskIds.includes(item.activeGoalTaskId))
    || (item.activeTodoId !== null && (item.activeGoalTaskId === null
      || !todoBelongsToDelegatedTask(item.activeTodoId, delegatedTaskIds)))
    || (['running', 'awaiting_clarification', 'awaiting_confirmation', 'waiting_external'].includes(item.status)
      && (item.activeGoalTaskId === null || item.activeTodoId === null))
    || (item.status === 'completed'
      && (item.activeGoalTaskId !== null || item.activeTodoId !== null))) {
    throw new BankApiError(400, 'invalid_agent_workflow', 'Agent Workflow 活动目标或摘要无效');
  }
  validateSafeWorkflowOutcome(item.lastOutcome);
  return {
    id: item.id,
    conversationId: item.conversationId,
    customerId: item.customerId,
    status: item.status,
    delegatedTaskIds,
    expectedRevision: item.expectedRevision,
    envelopeRevision: item.envelopeRevision,
    workflowRevision: item.workflowRevision,
    todoRevision: item.todoRevision,
    todoSnapshot,
    todoDigest: item.todoDigest,
    activeGoalTaskId: item.activeGoalTaskId,
    activeTodoId: item.activeTodoId,
    executionPolicy: item.executionPolicy,
    suspensionReason: item.suspensionReason,
    lastOutcome: item.lastOutcome,
    pendingCheckpointIds: [...item.pendingCheckpointIds],
  };
}

function validateSafeWorkflowOutcome(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || JSON.stringify(value).length > 8192) {
    throw new BankApiError(400, 'invalid_agent_workflow', 'Agent Workflow 结果无效');
  }
  const forbiddenKey = /(?:token|secret|password|account|card|input|description|content|grant)/i;
  const visit = (candidate, depth) => {
    if (depth > 4) throw new BankApiError(400, 'invalid_agent_workflow', 'Agent Workflow 结果过深');
    if (candidate === null || typeof candidate === 'boolean') return;
    if (typeof candidate === 'number' && Number.isFinite(candidate)) return;
    if (typeof candidate === 'string') {
      if (candidate.length > 240 || /(?:\d[ -]?){13,19}/.test(candidate)) {
        throw new BankApiError(400, 'invalid_agent_workflow', 'Agent Workflow 结果包含敏感数据');
      }
      return;
    }
    if (Array.isArray(candidate)) {
      if (candidate.length > 32) throw new BankApiError(400, 'invalid_agent_workflow', 'Agent Workflow 结果过大');
      candidate.forEach(item => visit(item, depth + 1));
      return;
    }
    if (!candidate || typeof candidate !== 'object') {
      throw new BankApiError(400, 'invalid_agent_workflow', 'Agent Workflow 结果无效');
    }
    Object.entries(candidate).forEach(([key, nested]) => {
      if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(key) || forbiddenKey.test(key)) {
        throw new BankApiError(400, 'invalid_agent_workflow', 'Agent Workflow 结果字段无效');
      }
      visit(nested, depth + 1);
    });
  };
  visit(value, 0);
}

function assertExactObjectKeys(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new BankApiError(400, 'invalid_agent_execution_record', '对象契约无效');
  }
  const expected = new Set(keys);
  if (Object.keys(value).length !== keys.length
    || Object.keys(value).some(key => !expected.has(key))) {
    throw new BankApiError(400, 'invalid_agent_execution_record', '对象包含未知字段');
  }
}

function workbenchValidationError(field, code) {
  return new BankApiError(422, 'validation_error', '运行记录查询参数无效', [
    { field, code: code || 'invalid_value' },
  ]);
}

function assertWorkbenchId(value, field) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(value)) {
    throw workbenchValidationError(field, 'invalid_identifier');
  }
}

function decodeAgentRunCursor(value) {
  if (value == null || value === '') return null;
  try {
    const parsed = JSON.parse(Buffer.from(String(value), 'base64url').toString('utf8'));
    if (!parsed || Object.keys(parsed).length !== 2
      || typeof parsed.createdAt !== 'string' || !Number.isFinite(Date.parse(parsed.createdAt))
      || typeof parsed.id !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(parsed.id)) {
      throw new Error('invalid cursor');
    }
    return parsed;
  } catch (error) {
    throw workbenchValidationError('cursor', 'invalid_cursor');
  }
}

function encodeAgentRunCursor(row) {
  return Buffer.from(JSON.stringify({ createdAt: row.created_at, id: row.id })).toString('base64url');
}

function projectAgentRunSummary(row) {
  const createdMs = Date.parse(row.created_at);
  const updatedMs = Date.parse(row.updated_at);
  return {
    id: row.id,
    conversationId: row.conversation_id,
    traceId: row.trace_id,
    status: row.status,
    graphVersion: row.graph_version,
    traceAvailability: row.trace_capture_status || 'legacy_unavailable',
    lastTraceSequence: Number(row.last_trace_sequence),
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    durationMs: Number.isFinite(createdMs) && Number.isFinite(updatedMs)
      ? Math.max(0, updatedMs - createdMs) : null,
    capabilityInvocationCount: Number(row.invocation_count || 0),
    confirmationCount: Number(row.confirmation_count || 0),
  };
}

function listAgentRunRecords(db, customerId, options) {
  assertWorkbenchId(customerId, 'customerId');
  if (!options || typeof options !== 'object' || Array.isArray(options)
    || Object.keys(options).some(key => !['status', 'limit', 'cursor'].includes(key))) {
    throw workbenchValidationError('query', 'unknown_field');
  }
  const status = options.status == null || options.status === '' ? null : String(options.status);
  if (status && !Object.prototype.hasOwnProperty.call(AGENT_RUN_TRANSITIONS, status)) {
    throw workbenchValidationError('status', 'unsupported_status');
  }
  const limit = options.limit == null || options.limit === '' ? 20 : Number(options.limit);
  if (!Number.isInteger(limit) || limit < 1 || limit > 50) {
    throw workbenchValidationError('limit', 'out_of_range');
  }
  const cursor = decodeAgentRunCursor(options.cursor);
  const clauses = ['run.customer_id = ?'];
  const params = [customerId];
  if (status) {
    clauses.push('run.status = ?');
    params.push(status);
  }
  if (cursor) {
    clauses.push('(run.created_at < ? OR (run.created_at = ? AND run.id < ?))');
    params.push(cursor.createdAt, cursor.createdAt, cursor.id);
  }
  params.push(limit + 1);
  const rows = queryAll(db, `SELECT run.*,
      (SELECT COUNT(*) FROM bank_capability_invocations invocation
       WHERE invocation.trace_id = run.trace_id) AS invocation_count,
      (SELECT COUNT(*) FROM bank_agent_action_checkpoints checkpoint
       WHERE checkpoint.agent_run_id = run.id) AS confirmation_count
    FROM bank_agent_runs run
    WHERE ${clauses.join(' AND ')}
    ORDER BY run.created_at DESC, run.id DESC
    LIMIT ?`, params);
  const hasNext = rows.length > limit;
  const pageRows = rows.slice(0, limit);
  return {
    items: pageRows.map(projectAgentRunSummary),
    nextCursor: hasNext ? encodeAgentRunCursor(pageRows[pageRows.length - 1]) : null,
  };
}

function assertSafeRunSummary(value, depth) {
  const level = depth || 0;
  // A validated memory Trace has summary -> memoryRecall -> groups -> items ->
  // controls, which reaches depth 8 including the leaf scalar. The separate
  // shape validator, list limits and 16 KiB byte cap remain the authority for
  // what may be persisted; this guard only prevents pathological nesting.
  if (level > 8 || value === undefined || typeof value === 'function' || typeof value === 'symbol') {
    throw workbenchValidationError('safeSummary', 'unsafe_value');
  }
  if (value == null || typeof value === 'boolean') return;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw workbenchValidationError('safeSummary', 'unsafe_value');
    return;
  }
  if (typeof value === 'string') {
    if (value.length > 1000) throw workbenchValidationError('safeSummary', 'value_too_large');
    return;
  }
  if (Array.isArray(value)) {
    if (value.length > 64) throw workbenchValidationError('safeSummary', 'value_too_large');
    value.forEach(item => assertSafeRunSummary(item, level + 1));
    return;
  }
  if (Object.getPrototypeOf(value) !== Object.prototype || Object.keys(value).length > 64) {
    throw workbenchValidationError('safeSummary', 'unsafe_value');
  }
  Object.entries(value).forEach(([key, item]) => {
    if (/token|secret|password|prompt|stack|database.?path|input.?hash|graph.?digest|raw.?content/i.test(key)) {
      throw workbenchValidationError('safeSummary.' + key, 'sensitive_field');
    }
    assertSafeRunSummary(item, level + 1);
  });
}

const MODEL_CALL_BUDGET_PER_RUN = 256;
const MODEL_CALL_TEXT_MAX_BYTES = 65536;
function createAgentRunModelCallRecord(tx, item) {
  const required = [
    'agentRunId', 'phase', 'model', 'promptJson', 'responseText',
    'toolsCount', 'durationMs', 'status', 'occurredAt',
  ];
  if (!item || typeof item !== 'object' || Array.isArray(item)
    || Object.keys(item).length !== required.length
    || Object.keys(item).some(key => !required.includes(key))) {
    throw workbenchValidationError('modelCall', 'invalid_shape');
  }
  assertWorkbenchId(item.agentRunId, 'agentRunId');
  if (!tx.queryOne('SELECT id FROM bank_agent_runs WHERE id = ?', [item.agentRunId])) {
    throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
  }
  if (typeof item.phase !== 'string' || !/^[a-z][a-z0-9_]{1,63}$/.test(item.phase)) {
    throw workbenchValidationError('phase', 'invalid_value');
  }
  if (item.model != null
    && (typeof item.model !== 'string' || !/^[A-Za-z0-9_.:-]{1,96}$/.test(item.model))) {
    throw workbenchValidationError('model', 'invalid_value');
  }
  ['promptJson', 'responseText'].forEach(field => {
    if (item[field] != null) {
      if (typeof item[field] !== 'string') {
        throw workbenchValidationError(field, 'invalid_type');
      }
      if (Buffer.byteLength(item[field], 'utf8') > MODEL_CALL_TEXT_MAX_BYTES) {
        throw workbenchValidationError(field, 'value_too_large');
      }
    }
  });
  if (!Number.isInteger(item.toolsCount) || item.toolsCount < 0 || item.toolsCount > 512
    || !(item.durationMs === null
      || Number.isInteger(item.durationMs) && item.durationMs >= 0 && item.durationMs <= 3600000)) {
    throw workbenchValidationError('toolsCountOrDurationMs', 'out_of_range');
  }
  if (item.status !== 'ok' && item.status !== 'error') {
    throw workbenchValidationError('status', 'invalid_value');
  }
  if (typeof item.occurredAt !== 'string' || !Number.isFinite(Date.parse(item.occurredAt))) {
    throw workbenchValidationError('occurredAt', 'invalid_timestamp');
  }
  const budgetRow = tx.queryOne(
    'SELECT COUNT(*) AS total FROM bank_agent_run_model_calls WHERE agent_run_id = ?',
    [item.agentRunId],
  );
  if (Number(budgetRow && budgetRow.total) >= MODEL_CALL_BUDGET_PER_RUN) {
    throw new BankApiError(409, 'model_call_budget_exhausted', '该运行的模型调用记录已达上限');
  }
  tx.run(`INSERT INTO bank_agent_run_model_calls
    (id,agent_run_id,phase,model,prompt_json,response_text,tools_count,duration_ms,status,created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?)`, [
    'armc_' + crypto.randomUUID().replace(/-/g, ''), item.agentRunId, item.phase,
    item.model, item.promptJson, item.responseText, item.toolsCount,
    item.durationMs, item.status, item.occurredAt,
  ]);
}

function appendAgentRunEventRecord(tx, item, timestamp) {
  const required = [
    'agentRunId', 'sequence', 'stage', 'status', 'durationMs',
    'policyVersion', 'safeSummary', 'createdAt',
  ];
  if (!item || typeof item !== 'object' || Array.isArray(item)
    || Object.keys(item).length !== required.length
    || Object.keys(item).some(key => !required.includes(key))) {
    throw workbenchValidationError('event', 'invalid_shape');
  }
  assertWorkbenchId(item.agentRunId, 'agentRunId');
  if (!Number.isInteger(item.sequence) || item.sequence < 1 || item.sequence > 128) {
    throw workbenchValidationError('sequence', 'out_of_range');
  }
  if (!tx.queryOne('SELECT id FROM bank_agent_runs WHERE id = ?', [item.agentRunId])) {
    throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
  }
  if (tx.queryOne(
    'SELECT sequence FROM bank_agent_run_events WHERE agent_run_id = ? AND sequence = ?',
    [item.agentRunId, item.sequence],
  )) {
    throw new BankApiError(409, 'agent_run_event_conflict', '运行事件序号已存在');
  }
  if (typeof item.stage !== 'string' || !/^[a-z][a-z0-9_.-]{1,63}$/.test(item.stage)
    || typeof item.status !== 'string' || !/^[a-z][a-z0-9_.-]{1,31}$/.test(item.status)) {
    throw workbenchValidationError('event', 'invalid_stage_or_status');
  }
  if (item.durationMs != null
    && (!Number.isInteger(item.durationMs) || item.durationMs < 0 || item.durationMs > 600000)) {
    throw workbenchValidationError('durationMs', 'out_of_range');
  }
  if (item.policyVersion != null
    && (typeof item.policyVersion !== 'string' || !/^[A-Za-z0-9_.:-]{1,96}$/.test(item.policyVersion))) {
    throw workbenchValidationError('policyVersion', 'invalid_value');
  }
  if (typeof item.createdAt !== 'string' || !Number.isFinite(Date.parse(item.createdAt))) {
    throw workbenchValidationError('createdAt', 'invalid_timestamp');
  }
  assertSafeRunSummary(item.safeSummary);
  const summaryJson = JSON.stringify(item.safeSummary);
  if (Buffer.byteLength(summaryJson, 'utf8') > 16384) {
    throw workbenchValidationError('safeSummary', 'value_too_large');
  }
  tx.run(`INSERT INTO bank_agent_run_events
    (agent_run_id,sequence,stage,status,duration_ms,policy_version,safe_summary_json,created_at)
    VALUES (?,?,?,?,?,?,?,?)`, [
    item.agentRunId, item.sequence, item.stage, item.status, item.durationMs,
    item.policyVersion, summaryJson, item.createdAt || timestamp,
  ]);
  tx.run(`UPDATE bank_agent_runs SET last_trace_sequence = CASE
      WHEN last_trace_sequence < ? THEN ? ELSE last_trace_sequence END,
      updated_at = ? WHERE id = ?`,
  [item.sequence, item.sequence, timestamp, item.agentRunId]);
  return {
    agentRunId: item.agentRunId,
    sequence: item.sequence,
    stage: item.stage,
    status: item.status,
    durationMs: item.durationMs,
    policyVersion: item.policyVersion,
    summary: JSON.parse(summaryJson),
    createdAt: item.createdAt,
  };
}

function bindAgentRunWorkflowRecord(tx, item, timestamp) {
  const keys = ['agentRunId', 'businessWorkflowId', 'relation'];
  if (!item || typeof item !== 'object' || Array.isArray(item)
    || Object.keys(item).length !== keys.length
    || Object.keys(item).some(key => !keys.includes(key))) {
    throw workbenchValidationError('workflowBinding', 'invalid_shape');
  }
  assertWorkbenchId(item.agentRunId, 'agentRunId');
  assertWorkbenchId(item.businessWorkflowId, 'businessWorkflowId');
  if (!['created-or-updated', 'resumed', 'confirmed'].includes(item.relation)) {
    throw workbenchValidationError('relation', 'unsupported_relation');
  }
  const ownership = tx.queryOne(`SELECT run.customer_id AS run_owner,
      workflow.customer_id AS workflow_owner
    FROM bank_agent_runs run
    INNER JOIN bank_agent_workflows workflow ON workflow.id = ?
    WHERE run.id = ?`, [item.businessWorkflowId, item.agentRunId]);
  if (!ownership) throw new BankApiError(404, 'agent_run_workflow_not_found', '运行或工作流不存在');
  if (ownership.run_owner !== ownership.workflow_owner) {
    throw new BankApiError(404, 'agent_run_workflow_not_found', '运行或工作流不存在');
  }
  tx.run(`INSERT OR REPLACE INTO bank_agent_run_workflows
    (agent_run_id,business_workflow_id,relation,created_at) VALUES (?,?,?,?)`, [
    item.agentRunId, item.businessWorkflowId, item.relation, timestamp,
  ]);
  return { ...item, createdAt: timestamp };
}

function mapAgentRunEventPublic(row) {
  return {
    sequence: Number(row.sequence), stage: row.stage, status: row.status,
    durationMs: row.duration_ms == null ? null : Number(row.duration_ms),
    policyVersion: row.policy_version || null,
    summary: JSON.parse(row.safe_summary_json), createdAt: row.created_at,
  };
}

function getAgentRunWorkbenchDetailRecord(db, agentRunId, customerId) {
  assertWorkbenchId(agentRunId, 'agentRunId');
  assertWorkbenchId(customerId, 'customerId');
  const row = queryOne(db, `SELECT run.*,
      (SELECT COUNT(*) FROM bank_capability_invocations invocation
       WHERE invocation.trace_id = run.trace_id) AS invocation_count,
      (SELECT COUNT(*) FROM bank_agent_action_checkpoints checkpoint
       WHERE checkpoint.agent_run_id = run.id) AS confirmation_count
    FROM bank_agent_runs run WHERE run.id = ? AND run.customer_id = ?`,
  [agentRunId, customerId]);
  if (!row) return null;
  const workflows = queryAll(db, `SELECT workflow.*, binding.relation
    FROM bank_agent_run_workflows binding
    INNER JOIN bank_agent_workflows workflow ON workflow.id = binding.business_workflow_id
    WHERE binding.agent_run_id = ? AND workflow.customer_id = ?
    ORDER BY workflow.updated_at DESC, workflow.id DESC`, [agentRunId, customerId]).map(item => ({
    id: item.id,
    conversationId: item.bound_conversation_id || item.conversation_id,
    status: item.status,
    relation: item.relation,
    workflowRevision: Number(item.workflow_revision),
    todoRevision: Number(item.todo_revision),
    pendingConfirmationCount: JSON.parse(item.pending_checkpoint_ids_json).length,
    createdAt: item.created_at,
    updatedAt: item.updated_at,
  }));
  return {
    run: projectAgentRunSummary(row),
    traceAvailability: row.trace_capture_status || 'legacy_unavailable',
    turns: queryAll(db, `SELECT turn.role, turn.text, turn.created_at
      FROM bank_agent_session_turns turn
      INNER JOIN bank_agent_sessions session ON session.id = turn.session_id
      WHERE turn.session_id = ? AND session.customer_id = ?
      ORDER BY turn.sequence DESC LIMIT 20`, [row.conversation_id, customerId])
      .reverse().map(item => ({
        role: item.role,
        text: String(item.text || '').slice(0, 1000),
        createdAt: item.created_at,
      })),
    events: queryAll(db,
      'SELECT * FROM bank_agent_run_events WHERE agent_run_id = ? ORDER BY sequence',
      [agentRunId],
    ).map(mapAgentRunEventPublic),
    workflows,
    invocations: queryAll(db, `SELECT capability_id,status,created_at
      FROM bank_capability_invocations WHERE trace_id = ? ORDER BY created_at,id`, [row.trace_id]).map(item => ({
      capabilityId: item.capability_id, status: item.status, createdAt: item.created_at,
    })),
    confirmations: queryAll(db, `SELECT capability_id,status,expires_at,consumed_at,created_at
      FROM bank_agent_action_checkpoints WHERE agent_run_id = ? AND customer_id = ?
      ORDER BY created_at,id`, [agentRunId, customerId]).map(item => ({
      capabilityId: item.capability_id, status: item.status, expiresAt: item.expires_at,
      consumedAt: item.consumed_at || null, createdAt: item.created_at,
    })),
    audits: queryAll(db, `SELECT action,resource_type,outcome,created_at
      FROM bank_audit_events WHERE trace_id = ? ORDER BY created_at,id`, [row.trace_id]).map(item => ({
      action: item.action, resourceType: item.resource_type,
      outcome: item.outcome, createdAt: item.created_at,
    })),
    memoryUpdates: queryAll(db, `SELECT * FROM bank_agent_memory_commands
      WHERE agent_run_id = ? ORDER BY updated_at DESC,id DESC LIMIT 20`, [agentRunId])
      .map(mapMemoryCommandPublic),
    modelCalls: queryAll(db, `SELECT phase,model,prompt_json,response_text,tools_count,duration_ms,status,created_at
      FROM bank_agent_run_model_calls WHERE agent_run_id = ?
      ORDER BY created_at,id LIMIT 64`, [agentRunId]).map(mapAgentRunModelCallPublic),
  };
}

function stableJson(value) {
  if (Array.isArray(value)) return '[' + value.map(stableJson).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + stableJson(value[key])).join(',') + '}';
  }
  return JSON.stringify(value);
}

function validateMemoryCommandEnvelope(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)
    || !item.command || typeof item.command !== 'object' || Array.isArray(item.command)) {
    throw new BankApiError(400, 'memory_command_invalid', 'Memory command is invalid');
  }
  ['agentRunId', 'customerId'].forEach(field => requireNonEmptyString(item[field], field));
  if (item.taskId != null) requireNonEmptyString(item.taskId, 'taskId');
  if (item.intentId != null) requireNonEmptyString(item.intentId, 'intentId');
  ['idempotencyKey', 'candidateType', 'predicate'].forEach(field => requireNonEmptyString(item.command[field], field));
  const encoded = stableJson(item.command);
  if (Buffer.byteLength(encoded, 'utf8') > 16 * 1024) {
    throw new BankApiError(413, 'memory_command_too_large', 'Memory command is too large');
  }
}

function validateMemoryReceipt(receipt) {
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)
    || !['ADD', 'MERGE', 'SUPERSEDE', 'CONTESTED', 'REJECT'].includes(receipt.decision)) {
    throw new BankApiError(400, 'memory_receipt_invalid', 'Memory receipt is invalid');
  }
  ['governanceStatus', 'reason', 'policyVersion'].forEach(field => requireNonEmptyString(receipt[field], field));
}

function mapMemoryCommandInternal(row) {
  if (!row) return null;
  return {
    id: row.id, idempotencyKey: row.idempotency_key, agentRunId: row.agent_run_id,
    customerId: row.customer_id, taskId: row.task_id || null, intentId: row.intent_id || null,
    candidateType: row.candidate_type, predicate: row.predicate,
    command: JSON.parse(row.command_json), status: row.status, attempts: Number(row.attempts),
    nextAttemptAt: row.next_attempt_at, decision: row.decision || null,
    governanceStatus: row.governance_status || null, reason: row.reason || null,
    policyVersion: row.policy_version || null, errorCode: row.error_code || null,
    createdAt: row.created_at, updatedAt: row.updated_at,
  };
}

function mapMemoryCommandPublic(row) {
  if (!row) return null;
  return {
    candidateType: row.candidate_type, predicate: row.predicate, status: row.status,
    decision: row.decision || null, governanceStatus: row.governance_status || null,
    reason: row.reason || null, policyVersion: row.policy_version || null,
    attempts: Number(row.attempts), updatedAt: row.updated_at,
  };
}

function mapAgentRunModelCallPublic(row) {
  return {
    phase: row.phase, model: row.model || null,
    prompt: row.prompt_json || null, response: row.response_text || null,
    toolsCount: Number(row.tools_count || 0),
    durationMs: row.duration_ms == null ? null : Number(row.duration_ms),
    status: row.status, occurredAt: row.created_at,
  };
}

function validateActionCheckpoint(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) {
    throw new BankApiError(400, 'invalid_agent_execution_record', '确认检查点数据无效');
  }
  ['id', 'agentRunId', 'customerId', 'capabilityId', 'inputHash', 'tokenHash', 'idempotencyKey', 'expiresAt']
    .forEach(field => requireNonEmptyString(item[field], field));
  if (!item.input || typeof item.input !== 'object' || Array.isArray(item.input)) {
    throw new BankApiError(400, 'invalid_agent_execution_record', 'input 必须是对象');
  }
  if (!item.summary || typeof item.summary !== 'object' || Array.isArray(item.summary)) {
    throw new BankApiError(400, 'invalid_agent_execution_record', 'summary 必须是对象');
  }
  if ((item.businessWorkflowId == null) !== (item.taskId == null)) {
    throw new BankApiError(400, 'invalid_agent_execution_record', 'Workflow 与任务绑定必须同时提供');
  }
  if (item.businessWorkflowId != null) {
    if (!/^[A-Za-z0-9_.:-]{1,160}$/.test(item.businessWorkflowId)
      || !/^[A-Za-z0-9_.:-]{1,96}$/.test(item.taskId)) {
      throw new BankApiError(400, 'invalid_agent_execution_record', 'Workflow 与任务绑定无效');
    }
  }
}

function validateCapabilityGrant(item) {
  assertExactObjectKeys(item, [
    'id', 'businessWorkflowId', 'taskId', 'capabilityId',
    'inputHash', 'checkpointId', 'expiresAt',
  ]);
  if (!/^acg_[A-Za-z0-9_-]{1,96}$/.test(item.id)
    || !/^[A-Za-z0-9_.:-]{1,160}$/.test(item.businessWorkflowId)
    || !/^[A-Za-z0-9_.:-]{1,96}$/.test(item.taskId)
    || !/^bank\.[a-z0-9.-]{1,123}$/.test(item.capabilityId)
    || !/^sha256:[a-f0-9]{64}$/i.test(item.inputHash)
    || !/^acp_[A-Za-z0-9_-]{1,96}$/.test(item.checkpointId)
    || !Number.isFinite(Date.parse(item.expiresAt))) {
    throw new BankApiError(400, 'invalid_capability_grant', '能力授权数据无效');
  }
}

function mapCustomer(row) { return row ? { id: row.id, name: row.name, status: row.status, city: row.city } : null; }
function mapAccount(row) { return row ? { id: row.id, customerId: row.customer_id || null, maskedNo: row.masked_no, name: row.name, type: row.type, currency: row.currency, status: row.status, visibility: row.visibility } : null; }
function mapBalance(row) { return row ? { accountId: row.account_id, availableMinor: Number(row.available_minor), ledgerMinor: Number(row.ledger_minor), version: Number(row.version), updatedAt: row.updated_at } : null; }
function mapPayee(row) { return row ? { id: row.id, customerId: row.customer_id, name: row.name, bankName: row.bank_name, maskedNo: row.masked_no, status: row.status, targetAccountId: row.target_account_id } : null; }
function mapCreditCard(row) { return row ? { id: row.id, customerId: row.customer_id, maskedNo: row.masked_no, productName: row.product_name, network: row.network, cardType: row.card_type, status: row.status, totalLimitMinor: Number(row.total_limit_minor), availableLimitMinor: Number(row.available_limit_minor), cashLimitMinor: Number(row.cash_limit_minor), statementDay: Number(row.statement_day), dueDay: Number(row.due_day), createdAt: row.created_at, updatedAt: row.updated_at } : null; }
function mapCreditCardSettings(row) { return row ? { cardId: row.card_id, onlineTransactionsEnabled: Boolean(row.online_transactions_enabled), contactlessEnabled: Boolean(row.contactless_enabled), overseasEnabled: Boolean(row.overseas_enabled), singleTransactionLimitMinor: Number(row.single_transaction_limit_minor), dailyLimitMinor: Number(row.daily_limit_minor), updatedAt: row.updated_at } : null; }
function mapCreditCardTransaction(row) { return row ? { id: row.id, cardId: row.card_id, direction: row.direction, amountMinor: Number(row.amount_minor), currency: row.currency, description: row.description, status: row.status, occurredAt: row.occurred_at } : null; }
function mapCreditCardLimitChange(row) { return row ? { id: row.id, cardId: row.card_id, customerId: row.customer_id, requestedLimitMinor: Number(row.requested_limit_minor), status: row.status, createdAt: row.created_at } : null; }
function mapLedgerEntry(row) { return row ? { id: row.id, ledgerTransactionId: row.ledger_transaction_id, accountId: row.account_id, direction: row.direction, amountMinor: Number(row.amount_minor), description: row.description } : null; }
function mapTransferOrder(row) { return row ? { id: row.id, customerId: row.customer_id, fromAccountId: row.from_account_id, payeeId: row.payee_id, amountMinor: Number(row.amount_minor), currency: row.currency, purpose: row.purpose, status: row.status, balanceVersion: Number(row.balance_version), requestHash: row.request_hash, createdAt: row.created_at, updatedAt: row.updated_at } : null; }
function mapValidation(row) { return row ? { id: row.id, transferOrderId: row.transfer_order_id, result: row.result, checks: JSON.parse(row.checks_json), feeMinor: Number(row.fee_minor), expiresAt: row.expires_at, createdAt: row.created_at } : null; }
function mapChallenge(row) { return row ? { id: row.id, transferOrderId: row.transfer_order_id, tokenHash: row.token_hash, summaryHash: row.summary_hash, expiresAt: row.expires_at, status: row.status || (row.used_at ? 'USED' : 'ACTIVE'), usedAt: row.used_at || null, invalidatedAt: row.invalidated_at || null, invalidationReason: row.invalidation_reason || null, createdAt: row.created_at } : null; }
function mapAuthorization(row) { return row ? { id: row.id, transferOrderId: row.transfer_order_id, challengeId: row.challenge_id, actorId: row.actor_id, summaryHash: row.summary_hash, authorizedAt: row.authorized_at } : null; }
function mapExecution(row) { return row ? { id: row.id, transferOrderId: row.transfer_order_id, status: row.status, ledgerTransactionId: row.ledger_transaction_id || null, receiptNo: row.receipt_no || null, executedAt: row.executed_at || null, failureCode: row.failure_code || null, createdAt: row.created_at } : null; }
function mapIdempotency(row) { return row ? { scope: row.scope, key: row.key, requestHash: row.request_hash, response: JSON.parse(row.response_json), status: row.status, createdAt: row.created_at } : null; }
function mapAudit(row) { return row ? { id: row.id, traceId: row.trace_id, actorType: row.actor_type, actorId: row.actor_id, action: row.action, resourceType: row.resource_type, resourceId: row.resource_id, outcome: row.outcome, details: JSON.parse(row.details_json), createdAt: row.created_at } : null; }
function mapAgentRun(row) { return row ? { id: row.id, customerId: row.customer_id, conversationId: row.conversation_id, traceId: row.trace_id, status: row.status, graphVersion: row.graph_version, graphDigest: row.graph_digest, lastTraceSequence: Number(row.last_trace_sequence), traceCaptureStatus: row.trace_capture_status || 'legacy_unavailable', createdAt: row.created_at, updatedAt: row.updated_at } : null; }
function mapAgentSession(row) {
  return row ? {
    id: row.id,
    customerId: row.customer_id,
    status: row.status,
    createdAt: row.created_at,
    lastActiveAt: row.last_active_at,
    idleExpiresAt: row.idle_expires_at,
    absoluteExpiresAt: row.absolute_expires_at,
    closedAt: row.closed_at || null,
    closeReason: row.close_reason || null,
  } : null;
}
function mapAgentSessionTurn(row) {
  return row ? {
    sessionId: row.session_id,
    sequence: Number(row.sequence),
    role: row.role,
    text: row.text,
    response: row.response_json ? JSON.parse(row.response_json) : null,
    createdAt: row.created_at,
  } : null;
}
function mapAgentClarification(row) {
  return row ? {
    id: row.id, agentRunId: row.agent_run_id, customerId: row.customer_id,
    conversationId: row.conversation_id, kind: row.kind, payeeName: row.payee_name,
    tokenHash: row.token_hash, status: row.status, expiresAt: row.expires_at,
    businessWorkflowId: row.business_workflow_id || null,
    taskId: row.task_id || null,
    expectedWorkflowRevision: row.expected_workflow_revision == null
      ? null : Number(row.expected_workflow_revision),
    consumedAt: row.consumed_at, createdAt: row.created_at, updatedAt: row.updated_at,
  } : null;
}
function mapConversationSlot(row) { return row ? { slotName: row.slot_name, value: JSON.parse(row.value_json), source: row.source || 'context', sourceIntentId: row.source_intent_id, updatedAt: row.updated_at } : null; }
function mapConversationState(row) { return row ? { state: JSON.parse(row.state_json), updatedAt: row.updated_at } : null; }
function mapAgentWorkflow(row) {
  return row ? {
    id: row.id,
    conversationId: row.bound_conversation_id || row.conversation_id,
    originConversationId: row.origin_conversation_id || row.conversation_id,
    boundConversationId: row.bound_conversation_id || row.conversation_id,
    customerId: row.customer_id,
    status: row.status,
    delegatedTaskIds: JSON.parse(row.delegated_task_ids_json),
    envelopeRevision: Number(row.envelope_revision),
    workflowRevision: Number(row.workflow_revision),
    todoRevision: Number(row.todo_revision),
    todoSnapshot: JSON.parse(row.todo_snapshot_json),
    todoDigest: row.todo_digest,
    activeGoalTaskId: row.active_goal_task_id || null,
    activeTodoId: row.active_todo_id || null,
    executionPolicy: row.execution_policy,
    suspensionReason: row.suspension_reason || null,
    lastOutcome: JSON.parse(row.last_outcome_json),
    conversationState: row.conversation_state_json ? JSON.parse(row.conversation_state_json) : null,
    pendingCheckpointIds: JSON.parse(row.pending_checkpoint_ids_json),
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  } : null;
}
function mapAgentWorkflowEvent(row) {
  return row ? {
    id: row.id,
    businessWorkflowId: row.business_workflow_id,
    workflowRevision: Number(row.workflow_revision),
    eventType: row.event_type,
    safePayload: JSON.parse(row.safe_payload_json),
    createdAt: row.created_at,
  } : null;
}
function mapActionCheckpointInternal(row) { return row ? { id: row.id, agentRunId: row.agent_run_id, customerId: row.customer_id, businessWorkflowId: row.business_workflow_id || null, taskId: row.task_id || null, capabilityId: row.capability_id, input: JSON.parse(row.input_json), inputHash: row.input_hash, summary: JSON.parse(row.summary_json), tokenHash: row.token_hash, idempotencyKey: row.idempotency_key, status: row.status, expiresAt: row.expires_at, consumedAt: row.consumed_at || null, result: row.result_json ? JSON.parse(row.result_json) : null, createdAt: row.created_at, updatedAt: row.updated_at } : null; }
function mapActionCheckpointPublic(item) { return item ? { id: item.id, agentRunId: item.agentRunId, customerId: item.customerId, businessWorkflowId: item.businessWorkflowId, taskId: item.taskId, capabilityId: item.capabilityId, summary: item.summary, status: item.status, expiresAt: item.expiresAt, consumedAt: item.consumedAt, result: item.result, createdAt: item.createdAt, updatedAt: item.updatedAt } : null; }
function mapCapabilityGrant(row) { return row ? { id: row.id, customerId: row.customer_id, businessWorkflowId: row.business_workflow_id, taskId: row.task_id, capabilityId: row.capability_id, inputHash: row.input_hash, checkpointId: row.checkpoint_id, status: row.status, expiresAt: row.expires_at, consumedAt: row.consumed_at || null, receipt: row.receipt_json ? JSON.parse(row.receipt_json) : null, createdAt: row.created_at, updatedAt: row.updated_at } : null; }
function mapCapabilityInvocation(row) { return row ? { id: row.id, capabilityId: row.capability_id, actorType: row.actor_type, actorId: row.actor_id || null, status: row.status, inputHash: row.input_hash, output: row.output_json ? JSON.parse(row.output_json) : null, traceId: row.trace_id, createdAt: row.created_at } : null; }

function getAgentRunRecord(db, id) {
  const item = mapAgentRun(queryOne(db, 'SELECT * FROM bank_agent_runs WHERE id = ?', [id]));
  if (!item) return null;
  item.executableCapabilityIds = queryAll(db,
    'SELECT capability_id FROM bank_agent_run_capabilities WHERE agent_run_id = ? ORDER BY capability_id', [id]
  ).map(row => row.capability_id);
  item.plannableCapabilityIds = queryAll(db,
    'SELECT capability_id FROM bank_agent_run_plannable_capabilities WHERE agent_run_id = ? ORDER BY capability_id', [id]
  ).map(row => row.capability_id);
  return item;
}

module.exports = { createMockCoreRepository };
