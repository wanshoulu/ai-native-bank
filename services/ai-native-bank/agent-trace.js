'use strict';

const TRACE_STAGES = Object.freeze([
  'request.accepted',
  'user_agent.started',
  'user_agent.completed',
  'delegation.created',
  'business_agent.started',
  'business_agent.completed',
  'plan.validated',
  'run.completed',
]);
const OPTIONAL_TRACE_STAGES = Object.freeze([
  'input_rail.started',
  'input_rail.completed',
  'action_rail.started',
  'action_rail.completed',
  'output_rail.started',
  'output_rail.completed',
  'security_gate.decided',
  'user_agent.committed',
  'user_agent.rolled_back',
  'intent_graph.retrieved',
  'memory.user_context_recalled',
  'memory.task_context_recalled',
  'memory.candidates_built',
  'memory.governance_decided',
  'memory.persistence_completed',
  'business_skill.started',
  'business_skill.completed',
  'capability.invocation_started',
  'capability.invocation_completed',
  'confirmation.checkpoint_created',
  'session.created',
  'session.restored',
  'session.closed',
  'session.expired',
  'workflow.rebound',
]);

const LIFECYCLE_TRACE_TITLES = Object.freeze({
  'session.created': '会话已创建',
  'session.restored': '会话已恢复',
  'session.closed': '会话已关闭',
  'session.expired': '会话已过期',
  'workflow.rebound': '业务已安全绑定到当前会话',
});

const STAGE_STATUS = Object.freeze({
  'request.accepted': 'accepted',
  'input_rail.started': 'running',
  'input_rail.completed': 'completed',
  'action_rail.started': 'running',
  'action_rail.completed': 'completed',
  'output_rail.started': 'running',
  'output_rail.completed': 'completed',
  'security_gate.decided': 'completed',
  'user_agent.started': 'running',
  'user_agent.committed': 'completed',
  'user_agent.rolled_back': 'completed',
  'intent_graph.retrieved': 'completed',
  'memory.user_context_recalled': 'completed',
  'memory.task_context_recalled': 'completed',
  'memory.candidates_built': 'completed',
  'memory.governance_decided': 'completed',
  'memory.persistence_completed': 'completed',
  'user_agent.completed': 'completed',
  'delegation.created': 'completed',
  'business_agent.started': 'running',
  'business_skill.started': 'running',
  'business_skill.completed': 'completed',
  'business_agent.completed': 'completed',
  'capability.invocation_started': 'running',
  'capability.invocation_completed': 'completed',
  'plan.validated': 'completed',
  'confirmation.checkpoint_created': 'completed',
  'session.created': 'completed',
  'session.restored': 'completed',
  'session.closed': 'completed',
  'session.expired': 'completed',
  'workflow.rebound': 'completed',
  'run.awaiting_confirmation': 'awaiting_confirmation',
  'run.completed': 'completed',
  'run.failed': 'failed',
});

const ID = /^[A-Za-z0-9_.:-]+$/;
const CAPABILITY_ID = /^bank\.[a-z0-9.-]+$/;
const ERROR_CODE = /^[a-z0-9_]+$/;
const UUID_TEXT = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const AGENT_RUN_LONG_ID = new RegExp('^(?:' + UUID_TEXT + '|abr_' + UUID_TEXT + ')$', 'i');
const TRACE_LONG_ID = new RegExp('^(?:' + UUID_TEXT + '|trace_' + UUID_TEXT + '|phone-trace-' + UUID_TEXT + ')$', 'i');
const CONVERSATION_LONG_ID = new RegExp('^(?:' + UUID_TEXT + '|conv_(?:phone_)?' + UUID_TEXT + ')$', 'i');
const DELEGATION_LONG_ID = new RegExp('^(?:' + UUID_TEXT + '|dlg_(?:' + UUID_TEXT + '|[0-9a-f]{32}))$', 'i');
const TASK_LONG_ID = new RegExp(
  '^(?:' + UUID_TEXT + '|task_' + UUID_TEXT + '|task_guided_[0-9a-f]{20})$', 'i',
);
const STEP_LONG_ID = new RegExp('^(?:' + UUID_TEXT + '|step_' + UUID_TEXT + ')$', 'i');
const ACTIONS = new Set(['ASK', 'QUERY', 'NAVIGATE', 'DO', 'CHANGE', 'CANCEL', 'HELP', 'TRANSFER', 'REPAY']);
const READINESS = new Set(['ready', 'needs_slots', 'needs_clarification', 'navigation_only', 'unsupported']);
const RISKS = new Set(['L0', 'L1', 'L2', 'L3']);
const SHAPES = new Set(['single_intent', 'multi_intent', 'same_intent_multi_task', 'ambiguous_intent', 'non_task']);
const RUN_STATUSES = new Set(['planned', 'succeeded', 'awaiting_confirmation', 'clarification_required', 'navigation_only', 'unsupported', 'blocked', 'unavailable']);
const MEMORY_SCOPES = new Set(['user', 'task']);
const MEMORY_DETAIL_STATUSES = new Set(['available', 'empty', 'redacted', 'degraded']);
const MEMORY_KINDS = new Set(['working', 'semantic', 'episodic', 'procedural']);
const MEMORY_APPLICATION_STATUSES = new Set(['applied', 'not_applied_conflict', 'not_applied_policy']);
const MEMORY_CONTROLS = new Set(['current', 'requires_core_refresh', 'requires_confirmation']);
const INTENT_GRAPH_ACTIONS = new Set(['ASK', 'QUERY', 'DO', 'CHANGE', 'CANCEL', 'HELP', 'NAVIGATE', 'TRANSFER', 'REPAY']);
const INTENT_GRAPH_EXECUTION_MODES = new Set(['read-execute', 'confirm-required', 'knowledge-only', 'navigation-only', 'unavailable', 'multi_intent']);
const INTENT_GRAPH_CONSTRAINTS = new Set(['required', 'allowed']);
const INTENT_GRAPH_LIFECYCLES = new Set(['active', 'inactive', 'deprecated']);
const INTENT_GRAPH_READINESS = new Set(['ready', 'mock-only', 'planned']);
const INTENT_GRAPH_FULFILLMENT_KINDS = new Set(['unconfigured', 'answer', 'query', 'page', 'menu', 'skill', 'workflow', 'agent']);
const TRACE_FIELD_NAMES = new Set([
  'message', 'pageContext', 'capabilitySummaries', 'agentRunId', 'traceId',
  'candidateCount', 'capabilityBindings', 'intentShape', 'taskCount', 'tasks',
  'delegationId', 'dependencies', 'intentTaskEnvelope', 'capabilities',
  'planStatus', 'stepCount', 'outputKeys', 'capabilityPlan', 'validated',
  'confirmationCount', 'checkpointStatus', 'status', 'steps', 'errorCode',
  'loopIteration', 'todoRevision', 'currentTodoStatus', 'delegatedTaskCount',
  'coveredTaskCount', 'grantCount', 'succeededCount', 'failedCount',
  'ambiguityType', 'ambiguities',
  'railDecision', 'decision', 'intentSummary', 'executionPermit', 'envelopeRevision',
  'userResponse', 'contextPackCount', 'conversationContext',
  'memoryContext', 'memoryCandidates', 'governanceDecision', 'memoryUpdateStatus',
]);
const TRACE_SECRET_MARKERS = Object.freeze([
  'confirmationtoken', 'tokenhash', 'authorization', 'accesstoken', 'accesskey',
  'refreshtoken', 'apikey', 'bearer', 'password', 'secret', 'systemprompt',
  'rawresponse', 'modelraw',
]);
const TRACE_INTERNAL_ID_PREFIXES = Object.freeze([
  'acp', 'bwf', 'conv', 'abr', 'dlg', 'trace', 'checkpoint', 'grant',
  'card', 'payee', 'execution', 'order', 'task', 'transfer',
]);
const SAFE_TRACE_ERROR_MESSAGES = new Set([
  '待还事项暂时无法查询',
  '业务智能体查询步骤未通过契约校验',
  '智能服务运行失败', '意图图谱与能力目录不兼容', '用户智能体执行失败',
  '用户意图结果未通过契约校验', '用户意图结果与权威候选不一致',
  '用户响应未通过安全投影', '余额查询能力暂时不可用', '业务智能体执行失败',
  '知识问答未通过能力隔离校验', '业务能力计划未通过契约校验',
  '账户发现服务暂时不可用', '账户发现未通过安全校验', '交易实体解析失败',
  '业务计划未满足只读账户发现策略', '能力计划未通过安全校验',
  '账户查询结果未通过安全校验', '余额查询服务暂时不可用',
  '只读原子能力执行失败', '确认计划未通过安全校验', '确认能力未通过安全校验',
  '确认检查点创建失败', '查询结果未通过安全投影',
  '业务智能体任务状态恢复失败',
  '安全授权已失效', '会话状态提交失败', '会话状态发生冲突，请重新发起',
  '当前业务槽位契约不可用',
  '业务智能体未覆盖全部业务目标', '业务智能体任务循环暂时不可用',
  '业务智能体任务状态保存失败', '已完成查询未产生可信结果',
  '待还事项查询未产生可信结果', '信用卡账单核对未产生可信结果',
  '推荐办理入口与当前业务能力不匹配', '查询能力暂时不可用',
  '账户发现结果未通过安全校验',
  '业务对象发现结果未通过安全校验', '业务对象发现服务暂时不可用',
  '业务对象发现未通过安全校验',
  '信用卡账单或付款账户暂时无法核对',
  '业务智能体执行超时', '当前服务入口暂时无法安全展示',
  '待还事项确认计划未通过安全校验',
  '业务循环活动任务无效',
  '业务处理暂时无法完成，请重新描述您的需求后重试',
  '智能服务运行遇到内部错误', '智能服务事件流异常',
]);
const SAFE_TRACE_ERROR_CODES = new Set([
  'obligation_discovery_failed',
  'intent_graph_incompatible', 'agent_runtime_failed', 'user_agent_contract_violation',
  'user_agent_candidate_conflict', 'user_response_projection_failed',
  'balance_capability_unavailable', 'knowledge_only_capability_violation',
  'business_agent_contract_violation', 'balance_discovery_unavailable',
  'balance_discovery_policy_violation', 'business_agent_readonly_discovery_conflict',
  'business_loop_invariant',
  'capability_plan_invalid', 'balance_account_data_invalid',
  'balance_capability_invocation_failed', 'checkpoint_creation_failed',
  'user_result_projection_failed', 'agent_runtime_unavailable', 'agent_runtime_timeout',
  'agent_runtime_not_ready', 'agent_runtime_request_invalid',
  'agent_runtime_response_invalid',
  'business_workflow_restore_failed',
  'execution_permit_invalid', 'agent_session_commit_failed',
  'agent_session_revision_conflict',
  'task_slot_contract_invalid',
  'business_agent_todo_goal_conflict', 'business_agent_todo_loop_unavailable',
  'business_workflow_persistence_failed', 'completed_query_result_invalid',
  'guided_entry_invalid', 'query_capability_unavailable',
  'slot_resolver_data_invalid', 'slot_resolver_invocation_failed',
  'slot_resolver_policy_violation', 'slot_resolver_unavailable',
  'credit_card_repayment_context_unavailable',
  'user_agent_shape_conflict',
  'unhandled_exception', 'stream_error',
]);
const FALLBACK_FAILED_STAGES = new Set(['unhandled_exception', 'stream_error']);
const TRACE_METRIC_KEYS = Object.freeze({
  'request.accepted': ['messageLength'],
  'input_rail.started': ['parallelGroup'],
  'input_rail.completed': ['decision', 'reasonCode', 'policyVersion', 'parallelGroup'],
  'action_rail.started': ['actionCount'],
  'action_rail.completed': ['decision', 'reasonCode', 'policyVersion', 'actionCount'],
  'output_rail.started': [],
  'output_rail.completed': ['decision', 'reasonCode', 'policyVersion'],
  'security_gate.decided': ['decision', 'reasonCode', 'policyVersion', 'permitType'],
  'user_agent.started': ['capabilityCount'],
  'user_agent.committed': ['permitType', 'envelopeRevision'],
  'user_agent.rolled_back': ['reasonCode'],
  'intent_graph.retrieved': ['candidateCount', 'graphVersion', 'lifecycle', 'readiness', 'orchestrationMode', 'fulfillmentKind'],
  'memory.user_context_recalled': ['outcome', 'contextPackCount', 'factCount', 'conflictCount', 'omissionCount', 'requiresCoreRefreshCount', 'requiresConfirmationCount', 'policyVersion', 'intentId'],
  'memory.task_context_recalled': ['outcome', 'contextPackCount', 'factCount', 'conflictCount', 'omissionCount', 'requiresCoreRefreshCount', 'requiresConfirmationCount', 'policyVersion', 'taskCount', 'inputDefaultCount'],
  'memory.candidates_built': ['candidateCount', 'canonicalEpisodeCount', 'userPreferenceCount'],
  'memory.governance_decided': ['addCount', 'mergeCount', 'supersedeCount', 'contestedCount', 'rejectCount'],
  'memory.persistence_completed': ['persistedCount', 'pendingRetryCount', 'deadLetterCount'],
  'user_agent.completed': ['intentShape', 'taskCount', 'candidateConstraint', 'userRepairCount', 'intentSummary', 'slotSummary', 'ambiguityType'],
  'delegation.created': ['taskCount', 'dependencyCount'],
  'business_agent.started': ['capabilityCount', 'loopIteration', 'todoRevision', 'delegatedTaskCount'],
  'business_skill.started': ['skillId', 'skillVersion', 'skillCount', 'capabilityCount', 'providerCount'],
  'business_skill.completed': ['skillId', 'skillVersion', 'skillCount', 'outcome', 'stepCount'],
  'business_agent.completed': ['planStatus', 'stepCount', 'slotResolution', 'businessRepairCount', 'ambiguityType', 'loopIteration', 'todoRevision', 'currentTodoStatus', 'delegatedTaskCount', 'coveredTaskCount'],
  'capability.invocation_started': ['capabilityId'],
  'capability.invocation_completed': ['capabilityId', 'outcome'],
  'plan.validated': ['validated', 'confirmationCount'],
  'confirmation.checkpoint_created': ['capabilityId', 'riskLevel', 'checkpointStatus', 'grantCount'],
  'run.awaiting_confirmation': ['status', 'taskCount', 'stepCount', 'checkpointStatus', 'grantCount'],
  'run.completed': ['status', 'taskCount', 'stepCount', 'grantCount', 'succeededCount', 'failedCount'],
  'run.failed': ['failedStage'],
  'session.created': ['idleAgeMs', 'resumableWorkflowCount'],
  'session.restored': ['idleAgeMs', 'resumableWorkflowCount'],
  'session.closed': ['idleAgeMs', 'resumableWorkflowCount'],
  'session.expired': ['idleAgeMs', 'resumableWorkflowCount'],
  'workflow.rebound': ['workflowRevision', 'completedTaskCount', 'taskCount'],
});

class AgentTraceValidationError extends Error {
  constructor(message) {
    super(message || 'Agent Trace 非法');
    this.name = 'AgentTraceValidationError';
  }
}

function fail(message) {
  throw new AgentTraceValidationError(message);
}

function plain(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(label + ' 格式非法');
  return value;
}

function exactKeys(value, allowed, label) {
  const unknown = Object.keys(value).filter(key => !allowed.includes(key));
  if (unknown.length) fail(label + ' 包含不允许字段');
}

function safeText(value, label, maximum, pattern) {
  if (typeof value !== 'string' || !value || value.length > maximum || /[<>\u0000-\u001f\u007f]/.test(value)) {
    fail(label + ' 文本非法');
  }
  if (pattern && !pattern.test(value)) fail(label + ' 格式非法');
  return value;
}

function safeTraceStringArray(value, label, maximum, pattern, allowedLongId) {
  if (!Array.isArray(value) || value.length > maximum) fail(label + ' 格式非法');
  const projected = value.map(item => safeTraceText(item, label, 160, pattern, allowedLongId));
  if (new Set(projected).size !== projected.length) fail(label + ' 包含重复项');
  return projected;
}

function decodedSecurityForms(value) {
  const forms = [];
  let current = String(value).normalize('NFKC').toLowerCase().replace(/[\u3002\uff0e\uff61]/gu, '.');
  for (let pass = 0; pass < 8; pass += 1) {
    if (!forms.includes(current)) forms.push(current);
    let invalidRun = false;
    const decodedValue = current.replace(/(?:%[0-9a-f]{2})+/giu, run => {
      try {
        return decodeURIComponent(run);
      } catch (_) {
        invalidRun = true;
        return run;
      }
    });
    if (invalidRun) fail('Trace 包含非法编码');
    const decoded = decodedValue.normalize('NFKC').toLowerCase()
      .replace(/[\u3002\uff0e\uff61]/gu, '.');
    if (decoded === current) break;
    current = decoded;
    if (!forms.includes(current)) forms.push(current);
    if (pass === 7) {
      let probeInvalid = false;
      const probe = current.replace(/(?:%[0-9a-f]{2})+/giu, run => {
        try {
          return decodeURIComponent(run);
        } catch (_) {
          probeInvalid = true;
          return run;
        }
      });
      if (probeInvalid || probe !== current) fail('Trace 包含过度编码');
    }
  }
  return forms;
}

function containsTraceUrlLikeText(forms) {
  return forms.some(candidate => (
    /(?:https?|ftp|file|data|javascript|vbscript|ws|wss):/iu.test(candidate)
    || /\/\//u.test(candidate)
    || /(?:www\.)/iu.test(candidate)
    || /(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?/u.test(candidate)
    || /localhost(?::\d{1,5})?/iu.test(candidate)
    || /[\p{L}\p{N}._%+-]+@(?:[\p{L}\p{N}-]+\.)+[\p{L}]{2,63}/iu.test(candidate)
  ));
}

function containsTraceInternalId(forms) {
  return forms.some(candidate => {
    const compact = candidate.replace(/[\p{P}\p{Z}\p{S}\p{M}\p{C}]/gu, '');
    return TRACE_INTERNAL_ID_PREFIXES.some(prefix => (
      compact.includes(prefix + 'private') || compact.includes(prefix + 'internal')
    )) || /(?:^|[^a-z0-9])bwf_[a-f0-9]{24}(?:$|[^a-z0-9])/iu.test(candidate);
  });
}

function safeTraceText(value, label, maximum, pattern, allowedLongId) {
  const projected = safeText(value, label, maximum);
  const forms = decodedSecurityForms(projected);
  const canonical = forms[0];
  const containsSensitiveMarker = forms.some(candidate => {
    const compact = candidate.replace(/[\p{P}\p{Z}\p{S}\p{M}\p{C}]/gu, '');
    return TRACE_SECRET_MARKERS.some(marker => compact.includes(marker));
  });
  const containsLongCredential = forms.some(candidate => /[A-Za-z0-9_-]{32,}/u.test(candidate));
  const isControlledLongId = Boolean(containsLongCredential && allowedLongId && allowedLongId.test(projected));
  const containsPaymentCardDigits = forms.some(candidate => {
    const digits = candidate.match(/\p{Nd}/gu);
    return digits && digits.length >= 15;
  });
  const containsInternalId = containsTraceInternalId(forms);
  if (containsSensitiveMarker
    || containsTraceUrlLikeText(forms)
    || (containsInternalId && !isControlledLongId)
    || (containsLongCredential && !isControlledLongId)
    || (containsPaymentCardDigits && !isControlledLongId)) {
    fail(label + ' 包含敏感信息');
  }
  if (pattern) safeText(projected, label, maximum, pattern);
  return projected;
}

function safeTraceFieldArray(value, label) {
  if (!Array.isArray(value) || value.length > 32) fail(label + ' 格式非法');
  const projected = value.map(item => {
    const field = safeTraceText(item, label, 64);
    if (!TRACE_FIELD_NAMES.has(field)) fail(label + ' 字段非法');
    return field;
  });
  if (new Set(projected).size !== projected.length) fail(label + ' 包含重复项');
  return projected;
}

function projectMetrics(value, stage) {
  plain(value, 'metrics');
  exactKeys(value, TRACE_METRIC_KEYS[stage] || [], 'metrics');
  if (Object.keys(value).length > 32) fail('metrics 过大');
  const projected = {};
  for (const [key, item] of Object.entries(value)) {
    safeText(key, 'metric key', 64, ID);
    if (item == null || typeof item === 'boolean') projected[key] = item;
    else if (typeof item === 'number' && Number.isFinite(item)) projected[key] = item;
    else if (typeof item === 'string') {
      if (stage === 'run.failed' && key === 'failedStage') {
        if (!SAFE_TRACE_ERROR_CODES.has(item)) fail('failedStage 不是稳定错误码');
        projected[key] = safeText(item, 'metric value', 96, ERROR_CODE);
      } else {
        projected[key] = safeTraceText(item, 'metric value', 160);
      }
    }
    else fail('metrics 只允许安全标量');
  }
  return projected;
}

function projectMemoryNotice(value, label) {
  plain(value, label);
  exactKeys(value, ['label', 'count'], label);
  if (!Number.isSafeInteger(value.count) || value.count < 1 || value.count > 100000) {
    fail(label + ' 数量非法');
  }
  return { label: safeTraceText(value.label, label, 80), count: value.count };
}

function projectMemoryRecall(value, stage, metrics) {
  plain(value, 'memory recall');
  exactKeys(value, ['schemaVersion', 'scope', 'detailStatus', 'groups', 'conflicts', 'omissions'], 'memory recall');
  if (value.schemaVersion !== '1.0' || !MEMORY_SCOPES.has(value.scope)
    || !MEMORY_DETAIL_STATUSES.has(value.detailStatus)) fail('memory recall 枚举非法');
  const expectedScope = stage === 'memory.user_context_recalled' ? 'user' : 'task';
  if (value.scope !== expectedScope) fail('memory recall 与阶段不匹配');
  if (!Array.isArray(value.groups) || value.groups.length > 8
    || !Array.isArray(value.conflicts) || value.conflicts.length > 12
    || !Array.isArray(value.omissions) || value.omissions.length > 10) {
    fail('memory recall 列表非法');
  }
  let itemCount = 0;
  const signatures = new Set();
  const groups = value.groups.map(group => {
    plain(group, 'memory recall group');
    exactKeys(group, ['key', 'label', 'items'], 'memory recall group');
    if (!Array.isArray(group.items) || group.items.length > 20) fail('memory recall items 非法');
    const items = group.items.map(item => {
      plain(item, 'memory recall item');
      exactKeys(item, ['label', 'displayValue', 'memoryKind', 'sourceLabel', 'validFrom', 'applicationStatus', 'controls'], 'memory recall item');
      if (!MEMORY_KINDS.has(item.memoryKind)
        || !MEMORY_APPLICATION_STATUSES.has(item.applicationStatus)
        || typeof item.validFrom !== 'string' || item.validFrom.length > 40
        || !Number.isFinite(Date.parse(item.validFrom))
        || !Array.isArray(item.controls) || item.controls.length > 3
        || item.controls.some(control => !MEMORY_CONTROLS.has(control))
        || new Set(item.controls).size !== item.controls.length
        || (item.applicationStatus === 'applied' && !item.controls.includes('current'))) {
        fail('memory recall item 枚举非法');
      }
      const projected = {
        label: safeTraceText(item.label, 'memory label', 80),
        displayValue: safeTraceText(item.displayValue, 'memory display value', 160),
        memoryKind: item.memoryKind,
        sourceLabel: safeTraceText(item.sourceLabel, 'memory source label', 80),
        validFrom: item.validFrom,
        applicationStatus: item.applicationStatus,
        controls: [...item.controls],
      };
      const signature = JSON.stringify(projected);
      if (signatures.has(signature)) fail('memory recall item 重复');
      signatures.add(signature);
      itemCount += 1;
      return projected;
    });
    return {
      key: safeText(group.key, 'memory group key', 64, ID),
      label: safeTraceText(group.label, 'memory group label', 80),
      items,
    };
  });
  if (itemCount > 20) fail('memory recall item 超出预算');
  const keys = groups.map(group => group.key);
  if (new Set(keys).size !== keys.length) fail('memory recall group 重复');
  if (value.detailStatus === 'available' && itemCount === 0) fail('memory recall 可用状态缺少内容');
  if ((value.detailStatus === 'empty' || value.detailStatus === 'degraded')
    && (groups.length || value.conflicts.length || value.omissions.length)) fail('memory recall 空状态携带内容');
  if (!Number.isSafeInteger(metrics.factCount) || metrics.factCount < itemCount) fail('memory recall 数量不一致');
  const outcomeStatus = metrics.outcome === 'degraded'
    ? 'degraded' : (metrics.outcome === 'empty' || metrics.outcome === 'not_applicable') ? 'empty' : null;
  if ((outcomeStatus && value.detailStatus !== outcomeStatus)
    || (!outcomeStatus && !['available', 'redacted'].includes(value.detailStatus))) {
    fail('memory recall 结果状态不一致');
  }
  return {
    schemaVersion: '1.0', scope: value.scope, detailStatus: value.detailStatus,
    groups,
    conflicts: value.conflicts.map(item => projectMemoryNotice(item, 'memory conflict')),
    omissions: value.omissions.map(item => projectMemoryNotice(item, 'memory omission')),
  };
}

function projectIntentGraphDetail(value, stage, metrics) {
  plain(value, 'intent graph detail');
  exactKeys(value, ['schemaVersion', 'graphVersion', 'candidates'], 'intent graph detail');
  if (value.schemaVersion !== '1.0') fail('intent graph 版本非法');
  if (typeof value.graphVersion !== 'string' || !/^\d+\.\d+\.\d+$/.test(value.graphVersion)) fail('intent graph 版本号非法');
  if (!Array.isArray(value.candidates) || value.candidates.length > 8) fail('intent graph 候选非法');
  const candidates = value.candidates.map(candidate => {
    plain(candidate, 'intent graph candidate');
    exactKeys(candidate, ['intentId', 'title', 'action', 'businessObject', 'executionMode', 'capabilityBindings', 'score', 'evidence', 'constraint', 'lifecycle', 'readiness', 'orchestrationMode', 'fulfillmentKind'], 'intent graph candidate');
    if (!INTENT_GRAPH_ACTIONS.has(candidate.action)
      || !INTENT_GRAPH_EXECUTION_MODES.has(candidate.executionMode)
      || !INTENT_GRAPH_CONSTRAINTS.has(candidate.constraint)) {
      fail('intent graph candidate 枚举非法');
    }
    if (candidate.lifecycle != null && !INTENT_GRAPH_LIFECYCLES.has(candidate.lifecycle)) {
      fail('intent graph candidate lifecycle 非法');
    }
    if (candidate.readiness != null && !INTENT_GRAPH_READINESS.has(candidate.readiness)) {
      fail('intent graph candidate readiness 非法');
    }
    if (candidate.fulfillmentKind != null && !INTENT_GRAPH_FULFILLMENT_KINDS.has(candidate.fulfillmentKind)) {
      fail('intent graph candidate fulfillment kind 非法');
    }
    if (!Array.isArray(candidate.capabilityBindings) || candidate.capabilityBindings.length > 32
      || !Array.isArray(candidate.evidence) || candidate.evidence.length > 16) {
      fail('intent graph candidate 列表非法');
    }
    return {
      intentId: safeText(candidate.intentId, 'intent id', 160, ID),
      title: safeTraceText(candidate.title, 'intent title', 80),
      action: candidate.action,
      businessObject: safeText(candidate.businessObject, 'business object', 128, ID),
      executionMode: candidate.executionMode,
      capabilityBindings: candidate.capabilityBindings.map(id => safeText(id, 'capability binding', 128, ID)),
      score: Number.isInteger(candidate.score) && candidate.score >= 0 && candidate.score <= 1000
        ? candidate.score : fail('intent graph score 非法'),
      evidence: candidate.evidence.map(e => safeTraceText(e, 'evidence', 80)),
      constraint: candidate.constraint,
      ...(candidate.lifecycle != null ? { lifecycle: candidate.lifecycle } : {}),
      ...(candidate.readiness != null ? { readiness: candidate.readiness } : {}),
      ...(candidate.orchestrationMode != null ? { orchestrationMode: candidate.orchestrationMode } : {}),
      ...(candidate.fulfillmentKind != null ? { fulfillmentKind: candidate.fulfillmentKind } : {}),
    };
  });
  if (!Number.isSafeInteger(metrics.candidateCount) || metrics.candidateCount !== candidates.length) {
    fail('intent graph 候选数量不一致');
  }
  return { schemaVersion: '1.0', graphVersion: value.graphVersion, candidates };
}

function validateLifecycleTraceSummary(stage, summary) {
  if (!(stage in LIFECYCLE_TRACE_TITLES)) return;
  if (summary.title !== LIFECYCLE_TRACE_TITLES[stage]
    || summary.input.length !== 0 || summary.output.length !== 0) {
    fail('生命周期 Trace 摘要非法');
  }
  const metrics = summary.metrics;
  const boundedInteger = (value, minimum, maximum) => Number.isSafeInteger(value)
    && value >= minimum && value <= maximum;
  if (stage.startsWith('session.')) {
    if (!boundedInteger(metrics.idleAgeMs, 0, 31_536_000_000)
      || !boundedInteger(metrics.resumableWorkflowCount, 0, 8)) {
      fail('Session 生命周期指标非法');
    }
    return;
  }
  if (!boundedInteger(metrics.workflowRevision, 1, 1_000_000_000)
    || !boundedInteger(metrics.completedTaskCount, 0, 128)
    || !boundedInteger(metrics.taskCount, 1, 128)
    || metrics.completedTaskCount > metrics.taskCount) {
    fail('Workflow 绑定指标非法');
  }
}

function projectTask(value) {
  plain(value, 'task');
  exactKeys(value, ['id', 'intentId', 'action', 'businessObject', 'readiness', 'risk', 'dependencies', 'slotNames'], 'task');
  if (!ACTIONS.has(value.action) || !READINESS.has(value.readiness) || !RISKS.has(value.risk)) fail('task 枚举非法');
  return {
    id: safeTraceText(value.id, 'task id', 96, ID, TASK_LONG_ID),
    intentId: safeTraceText(value.intentId, 'intent id', 160, ID),
    action: value.action,
    businessObject: safeTraceText(value.businessObject, 'business object', 128, ID),
    readiness: value.readiness,
    risk: value.risk,
    dependencies: safeTraceStringArray(value.dependencies, 'task dependencies', 32, ID, TASK_LONG_ID),
    slotNames: safeTraceStringArray(value.slotNames, 'slot names', 64, ID),
  };
}

function projectStep(value) {
  plain(value, 'step');
  exactKeys(value, ['id', 'taskId', 'capabilityId', 'inputKeys', 'requiresConfirmation', 'dependencies'], 'step');
  if (typeof value.requiresConfirmation !== 'boolean') fail('step confirmation 非法');
  return {
    id: safeTraceText(value.id, 'step id', 96, ID, STEP_LONG_ID),
    taskId: safeTraceText(value.taskId, 'step task id', 96, ID, TASK_LONG_ID),
    capabilityId: safeTraceText(value.capabilityId, 'capability id', 128, CAPABILITY_ID),
    inputKeys: safeTraceStringArray(value.inputKeys, 'input keys', 64, ID),
    requiresConfirmation: value.requiresConfirmation,
    dependencies: safeTraceStringArray(value.dependencies, 'step dependencies', 32, ID, STEP_LONG_ID),
  };
}

function validateGraph(items, dependencyKey, label) {
  const ids = items.map(item => item.id);
  if (new Set(ids).size !== ids.length) fail(label + ' 标识重复');
  for (const item of items) {
    for (const dependency of item[dependencyKey]) {
      if (dependency === item.id || !ids.includes(dependency)) fail(label + ' 依赖非法');
    }
  }
}

function projectSafeResult(value, expected) {
  plain(value, 'result');
  if (value.kind === 'security_gate') {
    exactKeys(value, ['kind', 'id', 'conversationId', 'status'], 'result');
    if (!['blocked', 'unavailable', 'clarification_required'].includes(value.status)) fail('result 状态非法');
    const id = safeTraceText(value.id, 'result id', 96, ID, AGENT_RUN_LONG_ID);
    if (id !== expected.agentRunId) fail('result 运行标识不一致');
    return {
      kind: 'security_gate',
      id,
      conversationId: safeTraceText(value.conversationId, 'conversation id', 96, ID, CONVERSATION_LONG_ID),
      status: value.status,
    };
  }
  exactKeys(value, ['id', 'conversationId', 'delegationId', 'status', 'intentShape', 'tasks', 'steps', 'predictedNextIntents', 'requiresConfirmation'], 'result');
  if (!RUN_STATUSES.has(value.status) || !SHAPES.has(value.intentShape)) fail('result 状态非法');
  if (typeof value.requiresConfirmation !== 'boolean') fail('result confirmation 非法');
  if (!Array.isArray(value.tasks) || value.tasks.length > 16) fail('tasks 格式非法');
  if (!Array.isArray(value.steps) || value.steps.length > 64) fail('steps 格式非法');
  const tasks = value.tasks.map(projectTask);
  const steps = value.steps.map(projectStep);
  validateGraph(tasks, 'dependencies', 'task');
  validateGraph(steps, 'dependencies', 'step');
  const taskIds = tasks.map(item => item.id);
  if (steps.some(item => !taskIds.includes(item.taskId))) fail('step 引用未知任务');
  if (value.intentShape === 'single_intent' && tasks.length !== 1) fail('single intent 任务数非法');
  if ((value.intentShape === 'multi_intent' || value.intentShape === 'same_intent_multi_task') && tasks.length < 2) fail('multi intent 任务数非法');
  if ((value.intentShape === 'ambiguous_intent' || value.intentShape === 'non_task') && tasks.length) fail('non task 任务数非法');
  if (value.status === 'planned' && !steps.length) fail('planned 缺少步骤');
  const id = safeTraceText(value.id, 'result id', 96, ID, AGENT_RUN_LONG_ID);
  if (id !== expected.agentRunId) fail('result 运行标识不一致');
  return {
    id,
    conversationId: safeTraceText(value.conversationId, 'conversation id', 96, ID, CONVERSATION_LONG_ID),
    delegationId: safeTraceText(value.delegationId, 'delegation id', 96, ID, DELEGATION_LONG_ID),
    status: value.status,
    intentShape: value.intentShape,
    tasks,
    steps,
    predictedNextIntents: safeTraceStringArray(value.predictedNextIntents, 'predicted intents', 8, ID),
    requiresConfirmation: value.requiresConfirmation,
  };
}

function createTraceState(expected) {
  plain(expected, 'trace identity');
  return {
    sequence: 0,
    stageIndex: 0,
    agentRunId: safeTraceText(expected.agentRunId, 'agentRunId', 96, ID, AGENT_RUN_LONG_ID),
    traceId: safeTraceText(expected.traceId, 'traceId', 128, ID, TRACE_LONG_ID),
    terminal: false,
    resumable: false,
    openCapabilityInvocation: null,
    sessionLifecycleObserved: false,
    workflowReboundObserved: false,
  };
}

function validateTraceEvent(value, state) {
  plain(state, 'trace state');
  if (state.terminal) fail('Trace 已进入终态');
  plain(value, 'trace event');
  exactKeys(value, ['schemaVersion', 'sequence', 'timestamp', 'stage', 'status', 'durationMs', 'agentRunId', 'traceId', 'summary', 'error'], 'trace event');
  if (value.schemaVersion !== '1.0') fail('Trace 版本非法');
  if (!Number.isInteger(value.sequence) || value.sequence !== state.sequence + 1 || value.sequence > 64) fail('Trace 序号非法');
  if (!Number.isFinite(Date.parse(value.timestamp))) fail('Trace 时间非法');
  if (!(value.stage in STAGE_STATUS) || value.status !== STAGE_STATUS[value.stage]) fail('Trace 阶段状态非法');
  if (value.agentRunId !== state.agentRunId || value.traceId !== state.traceId) fail('Trace 运行标识不一致');
  if (value.durationMs != null && (!Number.isInteger(value.durationMs) || value.durationMs < 0 || value.durationMs > 600000)) fail('Trace 耗时非法');

  const summary = plain(value.summary, 'summary');
  exactKeys(summary, ['title', 'input', 'output', 'metrics', 'memoryRecall', 'intentGraphDetail', 'result'], 'summary');
  const projectedSummary = {
    title: safeTraceText(summary.title, 'summary title', 128),
    input: safeTraceFieldArray(summary.input, 'summary input'),
    output: safeTraceFieldArray(summary.output, 'summary output'),
    metrics: projectMetrics(summary.metrics, value.stage),
  };
  const memoryStage = value.stage === 'memory.user_context_recalled'
    || value.stage === 'memory.task_context_recalled';
  if (memoryStage !== (summary.memoryRecall != null)) fail('memory recall 阶段绑定非法');
  if (memoryStage) {
    projectedSummary.memoryRecall = projectMemoryRecall(
      summary.memoryRecall, value.stage, projectedSummary.metrics,
    );
  }
  const intentGraphStage = value.stage === 'intent_graph.retrieved';
  if (summary.intentGraphDetail != null && !intentGraphStage) fail('intent graph detail 阶段绑定非法');
  if (summary.intentGraphDetail != null) {
    projectedSummary.intentGraphDetail = projectIntentGraphDetail(
      summary.intentGraphDetail, value.stage, projectedSummary.metrics,
    );
  }
  validateLifecycleTraceSummary(value.stage, projectedSummary);

  let projectedError;
  if (value.stage === 'run.failed') {
    const error = plain(value.error, 'trace error');
    exactKeys(error, ['code', 'message'], 'trace error');
    projectedError = {
      code: safeText(error.code, 'error code', 96, ERROR_CODE),
      message: safeTraceText(error.message, 'error message', 200),
    };
    const failedStage = projectedSummary.metrics.failedStage;
    const stageMatches = failedStage === projectedError.code
      || (FALLBACK_FAILED_STAGES.has(failedStage) && projectedError.code === 'agent_runtime_failed');
    if (!SAFE_TRACE_ERROR_CODES.has(projectedError.code)
      || !SAFE_TRACE_ERROR_MESSAGES.has(projectedError.message)
      || !stageMatches) {
      fail('trace error 不是稳定公开错误');
    }
    if (summary.result != null) fail('失败事件不允许结果');
  } else {
    if (value.error != null) fail('非失败事件不允许错误');
    const optional = OPTIONAL_TRACE_STAGES.includes(value.stage);
    const securityGateTerminal = value.stage === 'run.completed'
      && summary.result && summary.result.kind === 'security_gate';
    const skippedDelegationAndBusinessStart = value.stage === 'business_agent.completed'
      && TRACE_STAGES[state.stageIndex] === 'delegation.created';
    const skippedBusinessStart = value.stage === 'business_agent.completed'
      && TRACE_STAGES[state.stageIndex] === 'business_agent.started';
    const skippedPlanValidation = value.stage === 'run.completed'
      && TRACE_STAGES[state.stageIndex] === 'plan.validated';
    if (optional) validateOptionalStage(value, state);
    else if (value.stage === 'run.failed') {
      // run.failed 可在任意阶段出现，标记终态后跳过顺序校验
    } else if (value.stage === 'run.awaiting_confirmation') {
      if (TRACE_STAGES[state.stageIndex] !== 'run.completed') fail('Trace 等待确认阶段顺序非法');
    } else if (!securityGateTerminal && !skippedDelegationAndBusinessStart
      && !skippedBusinessStart && !skippedPlanValidation
      && value.stage !== TRACE_STAGES[state.stageIndex]) fail('Trace 阶段顺序非法');
    if (value.stage === 'run.completed' || value.stage === 'run.awaiting_confirmation') {
      projectedSummary.result = projectSafeResult(summary.result, state);
    } else if (summary.result != null) {
      fail('非终态事件不允许结果');
    }
  }

  const projected = {
    schemaVersion: '1.0',
    sequence: value.sequence,
    timestamp: value.timestamp,
    stage: value.stage,
    status: value.status,
    ...(value.durationMs == null ? {} : { durationMs: value.durationMs }),
    agentRunId: state.agentRunId,
    traceId: state.traceId,
    summary: projectedSummary,
    ...(projectedError ? { error: projectedError } : {}),
  };

  state.sequence = value.sequence;
  if (value.stage === 'run.failed') state.terminal = true;
  else if (value.stage === 'run.awaiting_confirmation') {
    state.terminal = true;
    state.resumable = true;
  }
  else {
    if (!OPTIONAL_TRACE_STAGES.includes(value.stage)) {
      const skippedDelegationAndBusinessStart = value.stage === 'business_agent.completed'
        && TRACE_STAGES[state.stageIndex] === 'delegation.created';
      const skippedBusinessStart = value.stage === 'business_agent.completed'
        && TRACE_STAGES[state.stageIndex] === 'business_agent.started';
      const skippedPlanValidation = value.stage === 'run.completed'
        && TRACE_STAGES[state.stageIndex] === 'plan.validated';
      state.stageIndex += skippedDelegationAndBusinessStart
        ? 3
        : skippedBusinessStart || skippedPlanValidation ? 2 : 1;
    }
    if (value.stage === 'run.completed') state.terminal = true;
  }
  return deepFreeze(projected);
}

function validateOptionalStage(value, state) {
  const positions = {
    'input_rail.started': [1],
    'input_rail.completed': [2],
    'action_rail.started': [7],
    'action_rail.completed': [7],
    'output_rail.started': [6, 7],
    'output_rail.completed': [6, 7],
    'security_gate.decided': [2, 3, 6, 7],
    'user_agent.committed': [2],
    'user_agent.rolled_back': [2, 3],
    'intent_graph.retrieved': [2],
    'memory.user_context_recalled': [4, 5],
    'memory.task_context_recalled': [5],
    'memory.candidates_built': [7],
    'memory.governance_decided': [7],
    'memory.persistence_completed': [7],
    'business_skill.started': [5],
    'business_skill.completed': [5],
    'capability.invocation_started': [6],
    'capability.invocation_completed': [6],
    'confirmation.checkpoint_created': [7],
    'session.created': [1],
    'session.restored': [1],
    'session.closed': [1],
    'session.expired': [1],
    'workflow.rebound': [4],
  };
  if (!positions[value.stage].includes(state.stageIndex)) fail('Trace 可选阶段顺序非法');
  if (value.stage.startsWith('session.')) {
    if (state.sessionLifecycleObserved) fail('Session 生命周期 Trace 重复');
    state.sessionLifecycleObserved = true;
  }
  if (value.stage === 'workflow.rebound') {
    if (state.workflowReboundObserved) fail('Workflow 绑定 Trace 重复');
    state.workflowReboundObserved = true;
  }
  const capabilityId = value.summary && value.summary.metrics && value.summary.metrics.capabilityId;
  if (value.stage === 'capability.invocation_started') {
    if (state.openCapabilityInvocation || !CAPABILITY_ID.test(capabilityId || '')) fail('能力调用开始事件非法');
    state.openCapabilityInvocation = capabilityId;
  }
  if (value.stage === 'capability.invocation_completed') {
    if (!state.openCapabilityInvocation || state.openCapabilityInvocation !== capabilityId) fail('能力调用完成事件未配对');
    state.openCapabilityInvocation = null;
  }
  if (value.stage === 'confirmation.checkpoint_created' && state.openCapabilityInvocation) fail('确认检查点前存在未完成能力调用');
}

function createSafeFailureEvent(state, code, message) {
  const safeCode = safeText(code, 'error code', 96, ERROR_CODE);
  const safeMessage = safeTraceText(message, 'error message', 200);
  if (!SAFE_TRACE_ERROR_CODES.has(safeCode) || !SAFE_TRACE_ERROR_MESSAGES.has(safeMessage)) fail('trace error 不是稳定公开错误');
  return {
    schemaVersion: '1.0',
    sequence: state.sequence + 1,
    timestamp: new Date().toISOString(),
    stage: 'run.failed',
    status: 'failed',
    agentRunId: state.agentRunId,
    traceId: state.traceId,
    summary: { title: '智能服务运行失败', input: [], output: ['errorCode'], metrics: { failedStage: safeCode } },
    error: {
      code: safeCode,
      message: safeMessage,
    },
  };
}

function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.freeze(value);
  Object.values(value).forEach(deepFreeze);
  return value;
}

module.exports = {
  AgentTraceValidationError,
  TRACE_STAGES,
  createSafeFailureEvent,
  createTraceState,
  validateTraceEvent,
};
