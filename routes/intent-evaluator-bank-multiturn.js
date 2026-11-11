'use strict';

/**
 * 银行意图引擎多轮评测适配器：
 * 以 `user:eval-*` 会话直连银行运行时 /v1/agent-runs（真实 agent run，含 LLM 规划），
 * 把 delivery + conversationState 归一化成评测页既有的 MultiTurnResponse。
 * 会话注册表仅存「上一轮 conversationState 投影」用于续轮上下文回写。
 *
 * 澄清出口（对齐生产网关 business-replies 通道）：
 * 澄清态下的后续轮次不再新开 agent run，而是派生 businessWorkflowId
 * （customerId+conversationId+pendingEnvelopeId，与 runtime 侧逐位一致）
 * 走 resumeBusinessWorkflow(resumeKind='clarification', clarificationMessage=用户原话)，
 * 由 runtime 重新走槽位评估——否则 runtime 只会原样再问一遍，形成死循环。
 */

const crypto = require('crypto');
const { createAiBankAgentRuntimeClient } = require('../services/ai-native-bank/agent-runtime-client');
const capabilityRegistry = require('../services/ai-native-bank/capability-registry');
const { getDefaultServices: getServices, conversationStateToRuntimeContext, projectAgentCapability, deriveBusinessWorkflowId } = require('./ai-native-bank-api');
const { getBankGraphStats, getBankIntentTitles } = require('./intent-evaluator-graphs');
const { evaluateIntentWithBankEngine } = require('./intent-evaluator-bank');
const usageEventLog = require('../tools/usage-event-log');

// 与生产 POST /agent-runs 同款客户身份：gateway 实体解析/能力授权都按该客户校验
const EVAL_CUSTOMER_ID = 'cust_001';

const SESSION_TTL_MS = 30 * 60 * 1000;
const sessions = new Map(); // evalSessionId -> { bankConversationId, turnCount, state, history[], updatedAt }

let clientSingleton = null;
function getClient() {
  if (!clientSingleton) clientSingleton = createAiBankAgentRuntimeClient();
  return clientSingleton;
}

function sweepSessions(now) {
  Array.from(sessions.entries()).forEach(([key, entry]) => {
    if (now - entry.updatedAt > SESSION_TTL_MS) sessions.delete(key);
  });
}

function shortId() {
  return crypto.randomBytes(6).toString('hex');
}

function inferTurnType(entry, delivery, conversationState) {
  const status = delivery.status;
  const prev = entry.history && entry.history.length > 1;
  const prevStatus = entry.lastStatus;
  if (prev && prevStatus === 'clarification_required'
    && ['clarification', 'result', 'answer', 'confirmation'].includes(
      delivery.userResponse ? delivery.userResponse.type : '',
    )) {
    return 'clarification_choice';
  }
  if (status === 'awaiting_confirmation') return 'slot_update';
  if (status === 'clarification_required') {
    if (!prev) return 'new_intent';
    return 'same_intent_continue';
  }
  if (status === 'navigation_only') return 'navigation';
  if (['unsupported', 'blocked', 'unavailable'].includes(status)) return status;
  const goals = (conversationState && conversationState.goals) || [];
  if (!goals.length) return 'new_intent';
  if (goals.length > 1) {
    const uniqueIntents = new Set(goals.map((goal) => goal.intentId));
    return uniqueIntents.size > 1 ? 'multi_intent_turn' : 'multi_task_turn';
  }
  const prevGoals = (entry.lastGoals || []);
  if (!prevGoals.length) return 'new_intent';
  if (prevGoals.length === 1 && goals.length === 1
    && prevGoals[0].intentId === goals[0].intentId) {
    return entry.lastStatus === 'clarification_required' ? 'same_intent_continue' : 'same_intent_continue';
  }
  return 'intent_switch';
}

function buildClarification(delivery) {
  const response = delivery.userResponse
    || (Array.isArray(delivery.userResponses) ? delivery.userResponses[delivery.userResponses.length - 1] : null);
  const needed = delivery.status === 'clarification_required'
    || (response && response.type === 'clarification');
  if (!needed) return { needed: false, questions: [] };
  const actions = (response && Array.isArray(response.actions)) ? response.actions : [];
  // 结构化补录表单（生产 App 的澄清答复通道）：原样暴露字段给评测页渲染，
  // 不暴露 submissionToken（评测页不走 /clarifications 提交通道）。
  const form = delivery.clarificationForm && Array.isArray(delivery.clarificationForm.fields)
    ? {
      kind: delivery.clarificationForm.kind || '',
      fields: delivery.clarificationForm.fields.map((field) => ({
        name: field.name,
        label: field.label || field.name,
        required: !!field.required,
        value: field.value == null ? '' : String(field.value),
        // select 字段（如转出账户=名下账户尾号）必须带选项，否则评测页渲染成自由文本框，
        // 用户填完整卡号会被 runtime 槽位校验拒绝（合法值=尾号XXXX）
        options: Array.isArray(field.options)
          ? field.options.map((opt) => ({
            label: (opt && (opt.label != null ? opt.label : opt.value != null ? opt.value : String(opt))) || '',
            value: (opt && opt.value != null ? opt.value : String(opt)) || '',
          })).filter((opt) => opt.value)
          : [],
      })),
    }
    : null;
  return {
    needed: true,
    questions: [{
      title: (response && response.title) || '请确认',
      question: (response && response.body) || '需要进一步确认用户意图。',
      options: actions.map((action) => ({
        label: (action.payload && action.payload.message) || action.label,
        description: action.description || '',
      })),
    }],
    form,
    default_action: 'reply',
  };
}

function pendingSlotsFrom(delivery, conversationState) {
  if (delivery.clarificationForm && Array.isArray(delivery.clarificationForm.fields)) {
    return delivery.clarificationForm.fields
      .filter((field) => field.required && !field.value)
      .map((field) => field.label || field.name);
  }
  const goals = (conversationState && conversationState.goals) || [];
  const needsSlots = goals.filter((goal) => goal.status === 'needs_slots');
  if (needsSlots.length) return [`${needsSlots.length} 个任务待补槽`];
  return [];
}

function buildEnvironmentInfo(entry, mode, projection) {
  // 沙箱会话模式：直连真实生产 agent runtime，本评测工具尚未独立沙箱，
  // 业务回放（QUERY/CONSULT 之外的 APPLY/MODIFY/TRANSFER）可能触发真实业务调用。
  // read_only 模式：不进入 Agent Run，仅做意图识别探查，不会产生副作用。
  if (mode === 'read_only') {
    return {
      mode: 'read_only',
      realRuntime: false,
      sandboxReady: false,
      customerId: EVAL_CUSTOMER_ID,
      conversationId: entry.bankConversationId,
      warning: '只读意图识别模式：不进入真实 Agent Run，未推进会话状态，不触发业务回放；如需观察真实业务流请切换到沙箱会话。',
    };
  }
  const executable = (projection && projection.executableCapabilities) || [];
  const plannable = (projection && projection.plannableCapabilities) || [];
  return {
    mode: 'sandbox_session',
    realRuntime: true,
    sandboxReady: false,
    customerId: EVAL_CUSTOMER_ID,
    conversationId: entry.bankConversationId,
    runtimeAgentRunId: entry.lastAgentRunId || '',
    graphVersion: (getBankGraphStats() || {}).version || '',
    executableCapabilityCount: executable.length,
    plannableCapabilityCount: plannable.length,
    warning: '沙箱尚未独立实现：本会话直连生产 agent runtime，可执行能力（APPLY/MODIFY/TRANSFER 等）可能触发真实业务调用；推荐仅做查询类（QUERY/CONSULT）意图评测。',
  };
}

function buildCapabilitiesInfo(projection) {
  if (!projection) return { executable: [], plannable: [] };
  const shape = (items) => (items || []).map((item) => ({
    id: item.id,
    label: item.label || item.title || item.id,
    action: item.action || '',
    domain: item.domain || '',
    businessObject: item.businessObject || '',
  }));
  return {
    executable: shape(projection.executableCapabilities),
    plannable: shape(projection.plannableCapabilities),
  };
}

function normalizeRun(entry, query, startedAt, delivery, conversationState, projection) {
  const goals = (conversationState && Array.isArray(conversationState.goals)) ? conversationState.goals : [];
  const turnType = inferTurnType(entry, delivery, conversationState);
  const response = delivery.userResponse
    || (Array.isArray(delivery.userResponses) ? delivery.userResponses[delivery.userResponses.length - 1] : null);
  const latencyMs = Date.now() - startedAt;
  const titles = getBankIntentTitles() || {};
  const intentName = (intentId) => titles[intentId] || intentId;
  const statusReasons = {
    planned: '任务已规划，等待执行。',
    succeeded: '本轮任务已完成。',
    awaiting_confirmation: '存在风险或关键参数，等待用户确认后执行。',
    clarification_required: '意图或参数需要澄清，已生成澄清问题。',
    navigation_only: '返回页面导航指引。',
    unsupported: '当前意图暂不支持办理。',
    blocked: '触达安全/风控边界，本轮被拦截。',
    unavailable: '依赖能力暂不可用。',
  };

  return {
    success: true,
    engine: 'bank',
    mode: 'sandbox_session',
    session_id: entry.sessionId,
    query,
    turn: {
      index: entry.turnCount,
      type: turnType,
      reason: statusReasons[delivery.status] || delivery.status,
      latency_ms: latencyMs,
    },
    state_after: {
      active_tasks: goals.map((goal) => ({
        task_id: goal.taskId,
        intent_id: goal.intentId,
        name: intentName(goal.intentId),
        status: goal.status,
        slots: (goal.slots || []).map((slot) => `${slot.name}=${slot.value}`),
      })),
      pending_slots: pendingSlotsFrom(delivery, conversationState),
      clarification: buildClarification(delivery),
      last_result_summary: {
        final_name: goals.length ? intentName(goals[0].intentId) : ((response && response.title) || ''),
        intent_id: goals.length ? goals[0].intentId : '',
        result_type: goals.length > 1 ? 'multi' : 'single',
        answer: response && ['answer', 'result'].includes(response.type) ? response.body : '',
        status: delivery.status,
      },
      intent_shape: {
        cardinality: (conversationState && conversationState.intentShape) || 'single_intent',
        clarification_needed: delivery.status === 'clarification_required',
      },
      status: delivery.status,
    },
    turn_intents: goals.map((goal) => ({
      name: intentName(goal.intentId),
      intent_id: goal.intentId,
      query,
      action: '',
      status: goal.status,
      score: null,
    })),
    trace: [
      {
        step: 'bank_agent_run',
        title: '银行引擎 Agent Run（真实会话）',
        conclusion: `status=${delivery.status} · 活动任务 ${goals.length} 个 · 响应类型 ${(response && response.type) || '-'}`,
        data: {
          agentRunId: delivery.agentRunId,
          status: delivery.status,
          userResponseType: response ? response.type : null,
          conversationState: conversationState ? {
            intentShape: conversationState.intentShape,
            goals: goals.map((goal) => ({ taskId: goal.taskId, intentId: goal.intentId, status: goal.status })),
            sharedSlots: conversationState.sharedSlots,
            ambiguities: conversationState.ambiguities,
          } : null,
          userResponse: response ? { type: response.type, title: response.title, body: response.body } : null,
        },
        latency_ms: latencyMs,
      },
      // ── 同轮召回通道分解（P1-3）──
      // 多轮走真实 agent run，runtime 不会回传逐通道埋点；这里按 conversationState.goals 推断：
      //   - lexical/subgraph：bank 引擎通过图谱阶梯双通道命中，goals 即为已落地的候选；
      //     无独立埋点 → status='completed_unobserved'，候选数=goals 数，明确告知"已命中但未拆分"。
      //   - vector：多轮真实会话不触发向量拓宽（runtime production path）→ status='unobserved'。
      // 评测者据此能区分"未命中" vs "命中但未拆通道" vs "向量没跑"，避免把同轮 trace 看成黑盒。
      {
        step: 'recall_channels_breakdown',
        title: '召回通道分解（同轮证据）',
        conclusion: goals.length
          ? `lexical/subgraph：completed_unobserved（命中 ${goals.length} 个任务，未拆通道）· vector：unobserved`
          : 'lexical/subgraph：skipped（无活动任务）· vector：unobserved',
        data: {
          lexical: {
            status: goals.length ? 'completed_unobserved' : 'skipped',
            candidates: goals.map((goal) => intentName(goal.intentId)),
            contributedCount: goals.length,
            reason: goals.length
              ? 'bank 引擎通过图谱阶梯词法通道命中；runtime 未回传逐通道埋点，候选即 goals 投影。'
              : '本轮无词法命中（用户消息未触发图谱阶梯）。',
          },
          vector: {
            status: 'unobserved',
            candidates: [],
            contributedCount: 0,
            reason: '多轮真实 agent run 走生产链路，不触发向量拓宽；如需观察向量召回请走单次评测。',
          },
          subgraph: {
            status: goals.length ? 'completed_unobserved' : 'skipped',
            candidates: goals.map((goal) => intentName(goal.intentId)),
            contributedCount: goals.length,
            reason: goals.length
              ? 'bank 引擎通过图谱阶梯关系扩展通道命中；runtime 未回传逐通道埋点，候选即 goals 投影。'
              : '本轮无关系扩展命中。',
          },
          // 同轮是否走过澄清/续跑出口（影响 trace 解读：续跑轮的 candidates 是上一轮残留 + 本轮补槽结果）
          clarificationFlow: entry.lastStatus === 'clarification_required'
            ? { resumedFrom: 'previous_turn_clarification', workflowBound: !!(entry.workflow) }
            : null,
        },
        latency_ms: 0,
      },
    ],
    environment: buildEnvironmentInfo(entry, 'sandbox_session', projection),
    capabilities: buildCapabilitiesInfo(projection),
    latency_ms: latencyMs,
  };
}

async function resumeClarifiedSession(entry, message, projection, values) {
  const services = await getServices();
  const workflow = await services.repository.getAgentWorkflow(
    entry.workflow.businessWorkflowId, entry.bankConversationId, EVAL_CUSTOMER_ID,
  );
  if (!workflow || workflow.status !== 'awaiting_clarification' || !workflow.activeGoalTaskId) {
    entry.workflow = null;
    const error = new Error('当前评测会话的业务工作流不在等待补充信息状态');
    error.code = 'bank_workflow_not_awaiting_clarification';
    throw error;
  }
  const nowIso = new Date().toISOString();
  let conversationContext = conversationStateToRuntimeContext(entry.state, nowIso);
  // 与生产 /agent-runs/:id/clarifications 一致：用户选择的转出账户注入 sharedSlots
  // （source=user_selection）。槽位契约 account_reference 只认 user_explicit/user_selection
  // 来源，仅靠消息文本会被 "fromAccount not user-selected" 拒绝、再次出补录卡片。
  if (values && values.fromAccount) {
    const sharedIntentId = (conversationContext.goals && conversationContext.goals[0])
      ? conversationContext.goals[0].intentId
      : 'ccb.transfer.create';
    conversationContext = Object.assign({}, conversationContext, {
      sharedSlots: (conversationContext.sharedSlots || [])
        .filter((item) => item.name !== 'fromAccount')
        .concat({
          name: 'fromAccount', value: values.fromAccount, source: 'user_selection',
          sourceIntentId: sharedIntentId, updatedAt: nowIso,
        }),
    });
  }
  const input = {
    resumeKind: 'clarification',
    clarificationMessage: message,
    agentRunId: entry.lastAgentRunId,
    conversationId: entry.bankConversationId,
    customerId: EVAL_CUSTOMER_ID,
    taskId: workflow.activeGoalTaskId,
    grants: [],
    expectedWorkflowRevision: workflow.workflowRevision,
    conversationContext,
    executableCapabilityIds: (projection.executableCapabilities || []).map((item) => item.id),
  };
  const startedAt = Date.now();
  let resumed;
  try {
    resumed = await getClient().resumeBusinessWorkflow(entry.workflow.businessWorkflowId, input);
  } catch (error) {
    // agent-runtime-client 把上游 4xx/5xx 的具体原因吞成固定文案（如 422=槽位校验拒绝，
    // 常见为转出账户非名下账户）。评测工具必须把引擎的真实拒绝原因还给测试者。
    if (error && error.name === 'AgentRuntimeClientError' && (error.status === 400 || error.status === 422)) {
      const wrapped = new Error(
        `银行引擎拒绝了本次答复（HTTP ${error.status}）。常见原因：转出账户必须选择评测客户名下账户（尾号形式）；请检查补录值后重试。`,
      );
      wrapped.code = 'bank_clarify_rejected';
      throw wrapped;
    }
    throw error;
  }
  return {
    delivery: {
      agentRunId: entry.lastAgentRunId,
      status: resumed.status,
      userResponse: resumed.userResponse || null,
      userResponses: resumed.userResponses || null,
      userResult: resumed.userResult || null,
      confirmation: resumed.confirmation || null,
      clarificationForm: resumed.clarificationForm || null,
    },
    conversationState: resumed.conversationState || null,
    startedAt,
  };
}

async function finishBankTurn(entry, query, outcome, projection) {
  const delivery = outcome.delivery || {};
  const conversationState = outcome.conversationState || null;
  const response = normalizeRun(entry, query, outcome.startedAt, delivery, conversationState, projection);
  entry.state = conversationState;
  entry.lastGoals = (conversationState && conversationState.goals) || [];
  entry.lastStatus = delivery.status || '';
  entry.lastAgentRunId = delivery.agentRunId || entry.lastAgentRunId || '';
  entry.workflow = null;
  if (delivery.status === 'clarification_required' && conversationState
    && conversationState.schemaVersion === '2.0' && conversationState.status === 'retain'
    && typeof conversationState.pendingEnvelopeId === 'string') {
    // 澄清态：派生并绑定业务工作流，下一轮走 resume 出口（与生产 business-replies 一致）。
    // 同步等待绑定结果：下一轮到达时 entry.workflow 必须已就绪，否则会落回死循环通道。
    try {
      const services = await getServices();
      if (typeof services.repository.getAgentWorkflow === 'function') {
        const businessWorkflowId = deriveBusinessWorkflowId(
          EVAL_CUSTOMER_ID, entry.bankConversationId, conversationState.pendingEnvelopeId,
        );
        const workflow = await services.repository.getAgentWorkflow(
          businessWorkflowId, entry.bankConversationId, EVAL_CUSTOMER_ID,
        );
        if (workflow && workflow.status === 'awaiting_clarification' && workflow.activeGoalTaskId) {
          entry.workflow = {
            businessWorkflowId,
            workflowRevision: workflow.workflowRevision,
            taskId: workflow.activeGoalTaskId,
          };
        }
      }
    } catch (error) {
      // pendingEnvelopeId 非法等派生失败：下一轮落回全新 agent run
    }
  }
  sessions.set(entry.sessionId, entry);
  usageEventLog.recordUsageEvent({
    carrier: 'graph',
    caller: 'intent-eval-bank-multiturn',
    query: `turn:${entry.turnCount}`,
    hits: entry.lastGoals.length,
  });
  return response;
}

async function evaluateBankMultiTurn(payload) {
  const query = String((payload && payload.query) || '').trim();
  if (!query) throw new Error('query is required');
  const now = Date.now();
  sweepSessions(now);

  // P1-5：顶层显式选择"只读意图识别/沙箱会话"。
  //   - read_only（默认）：不进入真实 Agent Run，仅做意图识别探查，不推进会话状态、不触发业务回放。
  //   - sandbox_session：直连生产 agent runtime，可执行能力可能触发真实业务调用（沙箱尚未独立实现）。
  // 默认走 read_only：避免用户以为只是识别评测，实际上进入 Agent 运行流程。
  const mode = (payload && (payload.mode || (payload.options && payload.options.mode))) === 'sandbox_session'
    ? 'sandbox_session'
    : 'read_only';

  const requestedId = String((payload && payload.session_id) || '').trim();
  let entry = requestedId ? sessions.get(requestedId) : null;
  if (!entry) {
    entry = {
      sessionId: requestedId || `es_${shortId()}`,
      bankConversationId: `user:eval-${shortId()}`,
      turnCount: 0,
      state: null,
      history: [],
      lastGoals: [],
      lastStatus: '',
      lastAgentRunId: '',
      workflow: null,
      updatedAt: now,
    };
  }
  entry.updatedAt = now;
  entry.turnCount += 1;
  entry.history.push(query);

  const projection = capabilityRegistry.projectForAgentRuntime();

  // 只读模式：不进入真实 Agent Run，调用单次银行意图识别（probeIntentEvaluation），
  // 把结果包装成多轮响应结构，但 conversation_state 不推进、不绑定业务工作流。
  if (mode === 'read_only') {
    return finishReadOnlyProbe(entry, query, projection);
  }

  // 澄清出口：上一轮要求补录且有绑定的业务工作流 → 本轮原话作为 clarificationMessage 续跑，
  // runtime 用消息重新做槽位评估；新开 agent run 只会原样再问一遍（死循环）。
  if (entry.lastStatus === 'clarification_required' && entry.workflow) {
    let outcome;
    try {
      outcome = await resumeClarifiedSession(entry, query, projection);
    } catch (error) {
      if (!error || error.code !== 'bank_workflow_not_awaiting_clarification') {
        entry.turnCount -= 1;
        entry.history.pop();
        throw error;
      }
      // 工作流记录失效（重启/清理）：落回全新 agent run
    }
    if (outcome) return finishBankTurn(entry, query, outcome, projection);
  }

  const executableCapabilityIds = (projection.executableCapabilities || []).map((item) => item.id);
  const plannableCapabilityIds = (projection.plannableCapabilities || []).map((item) => item.id);
  const runId = `eval-run-${shortId()}-${entry.turnCount}`;
  const traceId = `eval-trace-${shortId()}`;
  const input = {
    agentRunId: runId,
    conversationId: entry.bankConversationId,
    requestId: `${runId}-req`,
    traceId,
    customerId: EVAL_CUSTOMER_ID,
    actorType: 'agent',
    message: query,
    pageContext: {},
    safetyContext: { recentUserMessages: entry.history.slice(-6) },
    executableCapabilities: (projection.executableCapabilities || []).map(projectAgentCapability),
    plannableCapabilities: (projection.plannableCapabilities || []).map(projectAgentCapability),
  };
  if (entry.state) {
    input.conversationContext = conversationStateToRuntimeContext(entry.state, new Date().toISOString());
  }

  // 与生产链路对齐：runtime node_gateway 会按 agentRunId 回查 Node 侧 run 记录
  // （缺失 → 404 → node_gateway_rejected），先落库一条 accepted run。
  const bankStats = getBankGraphStats() || {};
  const services = await getServices();
  try {
    await services.repository.createAgentRun({
      id: runId,
      customerId: EVAL_CUSTOMER_ID,
      conversationId: entry.bankConversationId,
      traceId,
      status: 'accepted',
      graphVersion: bankStats.version || '2.1.0',
      graphDigest: bankStats.sha256 ? `sha256:${bankStats.sha256}` : '',
      executableCapabilityIds,
      plannableCapabilityIds,
    });
  } catch (error) {
    entry.turnCount -= 1;
    entry.history.pop();
    throw error;
  }

  const startedAt = Date.now();
  let result;
  try {
    result = await getClient().createAgentRun(input);
  } catch (error) {
    entry.turnCount -= 1;
    entry.history.pop();
    throw error;
  }

  return finishBankTurn(entry, query, {
    delivery: result.delivery || {},
    conversationState: result.conversationState || null,
    startedAt,
  }, projection);
}

// 只读意图识别探查：调用单次银行意图引擎（probeIntentEvaluation，不进 Agent Run），
// 把 IntentEvalResponse 适配为 MultiTurnResponse 形态。
// 关键：conversation_state 不推进，不绑定业务工作流，不触发业务回放；会话历史保留用于跨轮上下文展示。
async function finishReadOnlyProbe(entry, query, projection) {
  const startedAt = Date.now();
  const single = await evaluateIntentWithBankEngine(query, {});
  const latencyMs = Date.now() - startedAt;
  const titles = getBankIntentTitles() || {};
  const intentName = (intentId) => titles[intentId] || intentId;
  // 单次识别返回的 authoritative_results 即本轮识别收敛候选；不进入 Agent Run，故 goals 不落库。
  const authoritative = Array.isArray(single.authoritative_results) ? single.authoritative_results : [];
  const head = Array.isArray(single.final_results) && single.final_results[0] ? single.final_results[0] : null;
  const finalResult = single.final_result || head || (authoritative[0] || null);
  const finalName = finalResult && (finalResult.intent_name || finalResult.display_name) || '';
  const finalIntentId = finalResult && (finalResult.intent_id || '') || '';
  const recallChannels = single.recall_channels || single.recallChannels || {};
  const shape = single.shape || {};
  const resultType = single.result_type || '';
  // 不推进会话状态：active_tasks/last_result_summary 仅用于本轮展示
  const stateAfter = {
    active_tasks: authoritative.map((item, idx) => ({
      task_id: `read_only_${idx + 1}`,
      intent_id: item.intent_id || '',
      name: item.intent_name || item.display_name || '',
      status: 'read_only_probe',
      slots: [],
    })),
    pending_slots: [],
    clarification: single.clarification || { needed: false, questions: [] },
    last_result_summary: {
      final_name: finalName,
      intent_id: finalIntentId,
      result_type: resultType || (authoritative.length > 1 ? 'multi' : 'single'),
      answer: (finalResult && finalResult.answer) || '',
      status: 'read_only_probe',
    },
    intent_shape: {
      cardinality: shape.shape || 'single_intent',
      clarification_needed: !!(single.clarification && single.clarification.needed),
    },
    status: 'read_only_probe',
  };
  const turnIntents = authoritative.map((item) => ({
    name: item.intent_name || item.display_name || '',
    intent_id: item.intent_id || '',
    query,
    action: item.action_code || item.runtime_action_code || '',
    status: 'read_only_probe',
    score: item.score == null ? null : item.score,
  }));
  const environment = buildEnvironmentInfo(entry, 'read_only', projection);
  const capabilities = buildCapabilitiesInfo(projection);

  sessions.set(entry.sessionId, entry);
  usageEventLog.recordUsageEvent({
    carrier: 'graph',
    caller: 'intent-eval-bank-multiturn:read_only',
    query: `turn:${entry.turnCount}`,
    hits: authoritative.length,
  });

  return {
    success: true,
    engine: 'bank',
    mode: 'read_only',
    session_id: entry.sessionId,
    query,
    turn: {
      index: entry.turnCount,
      type: 'read_only_probe',
      reason: '只读意图识别：调用单次银行意图引擎（probeIntentEvaluation），未进入 Agent Run，未推进会话状态。',
      latency_ms: latencyMs,
    },
    state_after: stateAfter,
    turn_intents: turnIntents,
    trace: [
      {
        step: 'bank_read_only_probe',
        title: '银行引擎只读识别探查（不进 Agent Run）',
        conclusion: `识别候选 ${authoritative.length} 个 · result_type=${resultType || '-'} · shape=${shape.shape || '-'}`,
        data: {
          query,
          finalIntentId,
          finalIntentName: finalName,
          authoritativeCount: authoritative.length,
          recallChannels: {
            lexical: { status: (recallChannels.lexical && recallChannels.lexical.status) || 'unobserved', candidates: (recallChannels.lexical && recallChannels.lexical.candidates) || [] },
            vector: { status: (recallChannels.vector && recallChannels.vector.status) || 'unobserved', candidates: (recallChannels.vector && recallChannels.vector.candidates) || [] },
            subgraph: { status: (recallChannels.subgraph && recallChannels.subgraph.status) || 'unobserved', candidates: (recallChannels.subgraph && recallChannels.subgraph.candidates) || [] },
          },
          shape: shape.shape || null,
          judgeEnabled: !!(shape.judgeEnabled !== false),
        },
        latency_ms: latencyMs,
      },
    ],
    environment,
    capabilities,
    single_turn_result: single,
    latency_ms: latencyMs,
  };
}

// 结构化补录表单提交：与生产 /agent-runs/:id/clarifications 同构的消息编码
// （值必须编码进消息，否则 runtime 重新评估时槽位仍为空，会再次出补录卡片死循环）。
async function submitBankClarificationForm(payload) {
  const sessionId = String((payload && payload.session_id) || '').trim();
  const entry = sessionId ? sessions.get(sessionId) : null;
  if (!entry) throw new Error('评测会话不存在或已过期，请重开会话');
  if (entry.lastStatus !== 'clarification_required' || !entry.workflow) {
    throw new Error('当前会话不在等待补录状态');
  }
  const values = (payload && payload.values) || {};
  const amountYuan = values.amount != null && Number(values.amount) > 0
    ? Number(values.amount) : (values.amountMinor > 0 ? Math.round(values.amountMinor / 100) : 0);
  const parts = [];
  if (values.fromAccount) parts.push(`用${values.fromAccount}的账户`);
  if (values.payeeName) parts.push(`向${values.payeeName}`);
  else parts.push('继续转账');
  parts.push(amountYuan > 0 ? `转账${amountYuan}元` : '转账');
  const message = parts.join('');

  entry.updatedAt = Date.now();
  entry.turnCount += 1;
  entry.history.push(message);
  const projection = capabilityRegistry.projectForAgentRuntime();
  let outcome;
  try {
    outcome = await resumeClarifiedSession(entry, message, projection, values);
  } catch (error) {
    entry.turnCount -= 1;
    entry.history.pop();
    throw error;
  }
  return finishBankTurn(entry, message, outcome);
}

async function resetBankMultiTurnSession(sessionId) {
  const key = String(sessionId || '').trim();
  const entry = key ? sessions.get(key) : null;
  if (entry) {
    sessions.delete(key);
    try {
      await getClient().releaseAgentSessions([entry.bankConversationId]);
    } catch (error) {
      // 释放失败不阻塞重置：TTL 兜底回收。
    }
  }
  return { success: true, engine: 'bank' };
}

module.exports = { evaluateBankMultiTurn, resetBankMultiTurnSession, submitBankClarificationForm };
