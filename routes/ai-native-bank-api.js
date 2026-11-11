'use strict';

const crypto = require('node:crypto');
const { createEmbeddingGateway, createModelGateway } = require('../services/ai-native-bank/model-gateway');
const { createMockCoreRepository } = require('../services/ai-native-bank/mock-core-repository');
const { createBankService } = require('../services/ai-native-bank/bank-service');
const { createTransferService } = require('../services/ai-native-bank/transfer-service');
const { createExecutionEngine } = require('../services/ai-native-bank/execution-engine');
const { createCreditCardService } = require('../services/ai-native-bank/credit-card-service');
const { createWealthService } = require('../services/ai-native-bank/wealth-service');
const { createLoanService } = require('../services/ai-native-bank/loan-service');
const { createUtilityService } = require('../services/ai-native-bank/utility-service');
const { createCashflowService } = require('../services/ai-native-bank/cashflow-service');
const { createObligationService } = require('../services/ai-native-bank/obligation-service');
const { createRecommendationService } = require('../services/ai-native-bank/recommendation-service');
const { createRecommendationArtifactProvider } = require('../services/ai-native-bank/recommendation-artifact-provider');
const { createRecommendationFocusService } = require('../services/ai-native-bank/recommendation-focus-service');
const { createRecommendationFocusRouter } = require('../services/ai-native-bank/recommendation-focus-router');
const { loadExperienceRegistry } = require('../services/ai-native-bank/experience-registry');
const { createBuiltInSignalProviders, createNextActionSignalService } = require('../services/ai-native-bank/next-action-signals');
const { createNextBestActionEngine } = require('../services/ai-native-bank/next-best-action-engine');
const { createNextActionService } = require('../services/ai-native-bank/next-action-service');
const { createAssistanceAdapter } = require('../services/ai-native-bank/assistance-adapter');
const { createSmartCompanionService } = require('../services/ai-native-bank/smart-companion-service');
const { createAgentSessionService } = require('../services/ai-native-bank/agent-session-service');
const { isSafeAgentSessionText } = require('../services/ai-native-bank/agent-session-projection');
const { createAgentCapabilityService } = require('../services/ai-native-bank/agent-capability-service');
const capabilityRegistry = require('../services/ai-native-bank/capability-registry');
const MOCK_PERSONAS = require('../data/ai-native-bank/mock-personas.v1.json');
const { createAiBankAgentRuntimeClient } = require('../services/ai-native-bank/agent-runtime-client');
const { buildIntentGraph } = require('../services/ai-native-bank/intent-graph-builder');
const { createWorkbenchService } = require('../services/ai-native-bank/workbench-service');
const { createAgentMemoryControlClient } = require('../services/ai-native-bank/agent-memory-control-client');
const { createRecommendationMemoryRecaller } = require('../services/ai-native-bank/recommendation-memory-recaller');
const { createMemoryWorkbenchService } = require('../services/ai-native-bank/memory-workbench-service');
const usageEventLog = require('../tools/usage-event-log');
const { createMemoryCommandWorker } = require('../services/ai-native-bank/memory-command-worker');
const { createMemoryCommandService } = require('../services/ai-native-bank/memory-command-service');
const { createProviderHealthService } = require('../services/ai-native-bank/provider-health-service');
const { loadServiceProgressRegistry } = require('../services/ai-native-bank/service-progress-registry');
const { loadServiceToken } = require('../services/ai-native-bank/service-token');
const {
  classifyConversationControl,
  createResumableWorkflowChoices,
  deriveWorkflowResumeAnchors,
  hasResumableWorkflowSelectionCode,
} = require('../services/ai-native-bank/conversation-control');
const {
  createSafeFailureEvent,
  createTraceState,
  validateTraceEvent,
} = require('../services/ai-native-bank/agent-trace');
const {
  BankApiError,
  createRequestMeta,
  successEnvelope,
  errorEnvelope,
  normalizeError,
  requireIdempotencyKey,
  requireString,
} = require('../services/ai-native-bank/api-contract');

const API_PREFIX = '/api/ai-bank/v1';
const DEFAULT_CUSTOMER_ID = 'cust_demo_001';
const STREAM_HANDLED = Symbol('stream-handled');
const CONVERSATION_SLOT_TTL_MS = 30 * 60 * 1000;
const TERMINAL_AGENT_WORKFLOW_STATUSES = new Set([
  'completed', 'partially_completed', 'cancelled', 'failed',
]);
// 成功类终态才把推荐标记办结；cancelled/failed 属失败类，回退推荐为可重试。
const SUCCEEDED_WORKFLOW_STATUSES = new Set(['completed', 'partially_completed']);
const PUBLIC_AGENT_RUNTIME_IDENTITIES = Object.freeze([
  'ai-native-bank-user-agent',
  'ai-native-bank-business-agent',
]);
const AGENT_RUNTIME_STATUS_FAILURE_CODES = new Set([
  'agent_runtime_unavailable',
  'agent_runtime_timeout',
  'agent_runtime_not_ready',
  'agent_runtime_response_invalid',
  'agent_runtime_failed',
]);

// 业务"笔数"只应统计有副作用的写操作（转账、申购、还款等），
// 只读查询（余额、账户列表等）是流程步骤而非业务成果。
// capability 的 sideEffect 字段由 capability-registry 统一声明，避免硬编码具体 id。
function isSideEffectingReceipt(receipt) {
  const capability = receipt && receipt.capabilityId
    ? capabilityRegistry.get(receipt.capabilityId)
    : null;
  return Boolean(capability && capability.sideEffect !== 'none');
}

function createAiNativeBankHandler(options) {
  const opts = options || {};
  const bodyLimit = Number(opts.bodyLimit || 64 * 1024);
  const allowedCustomerIds = new Set(opts.allowedCustomerIds || [opts.defaultCustomerId || DEFAULT_CUSTOMER_ID]);
  const defaultCustomerId = opts.defaultCustomerId || DEFAULT_CUSTOMER_ID;
  const getServices = opts.getServices || (() => Promise.resolve(opts.services));
  const agentRuntimeClient = opts.agentRuntimeClient || createAiBankAgentRuntimeClient(opts.agentRuntime || {});
  const modelGateway = opts.modelGateway || createModelGateway(opts.modelGatewayOptions || {});
  const embeddingGateway = opts.embeddingGateway || createEmbeddingGateway(opts.embeddingGatewayOptions || {});
  const intentGraph = opts.intentGraph || buildIntentGraph();
  const recommendationFocusRouter = opts.recommendationFocusRouter
    || createRecommendationFocusRouter({ intentGraph });
  const serviceProgressRegistry = opts.serviceProgressRegistry || loadServiceProgressRegistry();
  const pendingTerminalAgentSessions = opts.pendingTerminalAgentSessions instanceof Map
    ? opts.pendingTerminalAgentSessions : new Map();

  return function handleAiNativeBankRequest(req, res) {
    let url;
    try { url = new URL(req.url || '/', 'http://' + ((req.headers && req.headers.host) || 'localhost')); }
    catch (_) { return false; }
    if (url.pathname !== API_PREFIX && !url.pathname.startsWith(API_PREFIX + '/')) return false;

    if (req.method === 'OPTIONS') {
      res.writeHead(204, {
        'Access-Control-Allow-Methods': 'GET, POST, PUT, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization, Idempotency-Key, X-Request-Id, X-Trace-Id, X-Mock-Customer-Id, X-Actor-Type',
        'Access-Control-Expose-Headers': 'X-Agent-Session-Id',
        'Cache-Control': 'no-store',
      });
      res.end();
      return true;
    }

    if (req.method === 'POST' && url.pathname === API_PREFIX + '/internal/model/v1/chat/completions') {
      Promise.resolve(modelGateway.handle(req, res)).catch(() => {
        if (!res.writableEnded) {
          res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
          res.end(JSON.stringify({ error: { message: '模型服务暂时不可用', type: 'server_error', code: 'model_gateway_unavailable' } }));
        }
      });
      return true;
    }

    if (req.method === 'POST' && url.pathname === API_PREFIX + '/internal/model/v1/embeddings') {
      Promise.resolve(embeddingGateway.handle(req, res)).catch(() => {
        if (!res.writableEnded) {
          res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
          res.end(JSON.stringify({ error: { message: '模型服务暂时不可用', type: 'server_error', code: 'model_gateway_unavailable' } }));
        }
      });
      return true;
    }

    const meta = createRequestMeta(req);
    Promise.resolve(getServices())
      .then(services => dispatch(req, res, url, services, {
        bodyLimit,
        meta,
        defaultCustomerId,
        allowedCustomerIds,
        devMode: opts.devMode == null ? process.env.DEV_MODE === 'true' : Boolean(opts.devMode),
        internalExecutionToken: opts.internalExecutionToken == null ? process.env.BANK_INTERNAL_EXECUTION_TOKEN : opts.internalExecutionToken,
        agentRuntimeServiceToken: resolveAgentRuntimeServiceToken(opts),
        agentRuntimeClient,
        pendingTerminalAgentSessions,
        intentGraph,
        recommendationFocusRouter,
        serviceProgressRegistry,
        modelGateway,
        now: typeof opts.now === 'function' ? opts.now : () => new Date().toISOString(),
      }))
      .then(result => {
        if (result !== STREAM_HANDLED) sendJson(res, result.status, successEnvelope(result.data, meta, result.meta));
      })
      .catch(error => {
        if (res.headersSent) {
          if (!res.writableEnded) res.end();
          return;
        }
        if (!(error instanceof BankApiError) || error.status >= 500) {
          logSafeGatewayError('request', error, meta);
        }
        const normalized = normalizeError(error);
        sendJson(res, normalized.status, errorEnvelope(normalized, meta));
      });
    return true;
  };
}

async function dispatch(req, res, url, services, context) {
  if (!services || !services.repository || !services.bankService || !services.transferService
    || !services.executionEngine || !services.creditCardService || !services.agentSessionService) {
    throw new Error('AI native bank services are unavailable');
  }
  const pathname = url.pathname;
  const relative = pathname.slice(API_PREFIX.length) || '/';
  if (req.method === 'POST' && relative === '/internal/agent-capability-invocations') {
    requireRuntimeServiceToken(req, context.agentRuntimeServiceToken);
    const body = await parseJsonBody(req, context.bodyLimit);
    const service = services.agentCapabilityService || createAgentCapabilityService(services);
    return ok(await service.invoke(body));
  }
  if (req.method === 'POST' && relative === '/internal/agent-memory-commands') {
    requireRuntimeServiceToken(req, context.agentRuntimeServiceToken);
    const body = await parseJsonBody(req, context.bodyLimit);
    if (!services.memoryCommandService) {
      throw new BankApiError(503, 'memory_command_service_unavailable', 'Memory command service is unavailable');
    }
    return created(await services.memoryCommandService.submit(body));
  }
  if (req.method === 'POST' && relative === '/internal/agent-run-model-calls') {
    requireRuntimeServiceToken(req, context.agentRuntimeServiceToken);
    const body = await parseJsonBody(req, context.bodyLimit);
    await services.repository.createAgentRunModelCall(body);
    return created({ accepted: true });
  }
  if (req.method === 'POST' && relative === '/internal/agent-action-checkpoints') {
    requireRuntimeServiceToken(req, context.agentRuntimeServiceToken);
    const body = await parseJsonBody(req, context.bodyLimit);
    const service = services.agentCapabilityService || createAgentCapabilityService(services);
    return created(await service.createCheckpoint(body));
  }
  if (req.method === 'POST' && relative === '/internal/agent-authorized-capability-invocations') {
    requireRuntimeServiceToken(req, context.agentRuntimeServiceToken);
    const body = await parseJsonBody(req, context.bodyLimit);
    const service = services.agentCapabilityService || createAgentCapabilityService(services);
    return ok(await service.invokeAuthorized(body));
  }
  if (req.method === 'POST' && relative === '/internal/agent-workflows') {
    requireRuntimeServiceToken(req, context.agentRuntimeServiceToken);
    const body = exactAgentWorkflowBody(await parseJsonBody(req, context.bodyLimit));
    const run = await services.repository.getAgentRun(body.agentRunId);
    if (!run) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
    const stored = await services.repository.replaceAgentWorkflow({
      id: body.businessWorkflowId,
      conversationId: run.conversationId,
      customerId: run.customerId,
      status: body.status,
      delegatedTaskIds: body.delegatedTaskIds,
      expectedRevision: body.expectedRevision,
      envelopeRevision: body.envelopeRevision,
      workflowRevision: body.workflowRevision,
      todoRevision: body.todoRevision,
      todoSnapshot: body.todoSnapshot,
      todoDigest: body.todoDigest,
      activeGoalTaskId: body.activeGoalTaskId,
      activeTodoId: body.activeTodoId,
      executionPolicy: body.executionPolicy,
      suspensionReason: body.suspensionReason,
      lastOutcome: body.lastOutcome,
      pendingCheckpointIds: body.pendingCheckpointIds,
    });
    await services.repository.bindAgentRunWorkflow({
      agentRunId: body.agentRunId,
      businessWorkflowId: stored.id,
      relation: 'created-or-updated',
    });
    if (typeof services.repository.attachNextActionBusinessWorkflow === 'function') {
      try {
        const recommendationBinding = await services.repository.attachNextActionBusinessWorkflow(
          body.agentRunId, run.customerId, stored.id,
        );
        if (recommendationBinding && TERMINAL_AGENT_WORKFLOW_STATUSES.has(stored.status)) {
          const outcome = { code: stored.status };
          // 成功终态才标记办结；失败/放弃终态把推荐回退到 accepted（可重试），
          // 避免未完成的业务被误标"已完成"。
          if (SUCCEEDED_WORKFLOW_STATUSES.has(stored.status)
            && typeof services.repository.completeNextActionRecommendation === 'function') {
            await services.repository.completeNextActionRecommendation(
              recommendationBinding.recommendationId,
              run.customerId,
              outcome,
            );
          } else if (typeof services.repository.failNextActionRecommendation === 'function') {
            await services.repository.failNextActionRecommendation(
              recommendationBinding.recommendationId,
              run.customerId,
              outcome,
            );
          }
        }
      } catch (_) {
        console.warn('[ai-native-bank] stage=next_action_workflow_telemetry code=write_failed');
      }
    }
    if (TERMINAL_AGENT_WORKFLOW_STATUSES.has(stored.status)) {
      rememberPendingTerminalAgentSession(context, body.agentRunId,
        businessAgentSessionId(stored.customerId, stored.id),
      );
    }
    return created({
      businessWorkflowId: stored.id,
      status: stored.status,
      workflowRevision: stored.workflowRevision,
      todoRevision: stored.todoRevision,
      activeGoalTaskId: stored.activeGoalTaskId,
    });
  }
  const internalWorkflowMatch = relative.match(/^\/internal\/agent-workflows\/([^/]+)$/);
  if (req.method === 'GET' && internalWorkflowMatch) {
    requireRuntimeServiceToken(req, context.agentRuntimeServiceToken);
    const agentRunId = requireString(url.searchParams.get('agentRunId'), 'agentRunId', {
      min: 1, max: 160,
    });
    const run = await services.repository.getAgentRun(agentRunId);
    if (!run) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
    const workflowId = decodePath(internalWorkflowMatch[1]);
    const stored = await services.repository.getAgentWorkflow(
      workflowId, run.conversationId, run.customerId,
    );
    if (!stored) throw new BankApiError(404, 'agent_workflow_not_found', 'Agent Workflow 不存在');
    return ok({
      businessWorkflowId: stored.id,
      status: stored.status,
      delegatedTaskIds: stored.delegatedTaskIds,
      envelopeRevision: stored.envelopeRevision,
      workflowRevision: stored.workflowRevision,
      todoRevision: stored.todoRevision,
      todoSnapshot: stored.todoSnapshot,
      todoDigest: stored.todoDigest,
      activeGoalTaskId: stored.activeGoalTaskId,
      activeTodoId: stored.activeTodoId,
      executionPolicy: stored.executionPolicy,
      suspensionReason: stored.suspensionReason,
      lastOutcome: stored.lastOutcome,
      pendingCheckpointIds: stored.pendingCheckpointIds,
    });
  }
  const identity = mockIdentity(req, context);

  if (req.method === 'GET' && relative === '/assistant-companions') {
    requireAllowedQuery(url, []);
    if (!services.smartCompanionService || typeof services.smartCompanionService.list !== 'function') {
      throw new BankApiError(503, 'smart_companion_unavailable', '智能陪办暂时不可用');
    }
    return ok(await services.smartCompanionService.list(identity.customerId));
  }

  if (req.method === 'GET' && relative === '/workbench/overview') {
    requireAllowedQuery(url, []);
    if (!services.workbenchService) {
      throw new BankApiError(503, 'workbench_service_unavailable', '工作台服务暂不可用');
    }
    return ok(await services.workbenchService.getOverview());
  }
  if (req.method === 'GET' && relative === '/workbench/scenarios') {
    requireAllowedQuery(url, []);
    if (!services.workbenchService) {
      throw new BankApiError(503, 'workbench_service_unavailable', '工作台服务暂不可用');
    }
    return ok(await services.workbenchService.listScenarios());
  }
  if (req.method === 'GET' && relative === '/workbench/assets') {
    const filters = requireAllowedQuery(url, ['type', 'domain', 'integrationStatus']);
    if (!services.workbenchService) {
      throw new BankApiError(503, 'workbench_service_unavailable', '工作台服务暂不可用');
    }
    return ok(await services.workbenchService.listAssets(filters));
  }
  if (req.method === 'GET' && relative === '/workbench/data-assets') {
    requireAllowedQuery(url, []);
    if (!services.workbenchService || typeof services.workbenchService.listDataAssets !== 'function') {
      throw new BankApiError(503, 'workbench_service_unavailable', '工作台服务暂不可用');
    }
    return ok(await services.workbenchService.listDataAssets(identity.customerId));
  }
  const dataAssetDetailMatch = relative.match(/^\/workbench\/data-assets\/([^/]+)$/);
  if (req.method === 'GET' && dataAssetDetailMatch) {
    requireAllowedQuery(url, []);
    if (!services.workbenchService || typeof services.workbenchService.getDataAssetDetail !== 'function') {
      throw new BankApiError(503, 'workbench_service_unavailable', '工作台服务暂不可用');
    }
    return ok(await services.workbenchService.getDataAssetDetail(
      identity.customerId, decodePath(dataAssetDetailMatch[1]),
    ));
  }
  if (req.method === 'GET' && relative === '/workbench/experience-scenarios') {
    requireAllowedQuery(url, []);
    if (!services.workbenchService) {
      throw new BankApiError(503, 'workbench_service_unavailable', '工作台服务暂不可用');
    }
    return ok(await services.workbenchService.listExperienceScenarios());
  }
  if (req.method === 'GET' && relative === '/workbench/memories/overview') {
    const filters = requireAllowedQuery(url, [
      'query', 'customerRef', 'recordKinds', 'statuses', 'sources', 'purposes',
      'validFrom', 'validTo', 'cursor', 'limit',
    ]);
    if (!services.memoryWorkbenchService) {
      throw new BankApiError(404, 'memory_workbench_disabled', '记忆专区未启用');
    }
    const overviewData = await services.memoryWorkbenchService.overview(filters);
    usageEventLog.recordUsageEvent({ carrier: 'memory', caller: 'memory-workbench', query: 'overview', hits: 1 });
    return ok(overviewData);
  }
  if (req.method === 'GET' && relative === '/workbench/memories') {
    const filters = requireAllowedQuery(url, [
      'query', 'customerRef', 'recordKinds', 'statuses', 'sources', 'purposes',
      'validFrom', 'validTo', 'cursor', 'limit',
    ]);
    if (!services.memoryWorkbenchService) {
      throw new BankApiError(404, 'memory_workbench_disabled', '记忆专区未启用');
    }
    const listData = await services.memoryWorkbenchService.list(filters);
    usageEventLog.recordUsageEvent({ carrier: 'memory', caller: 'memory-workbench', query: 'list', hits: 1 });
    return ok(listData);
  }
  const memoryDetailMatch = relative.match(/^\/workbench\/memories\/(memview_[A-Za-z0-9_-]{20,80})$/);
  if (req.method === 'GET' && memoryDetailMatch) {
    requireAllowedQuery(url, []);
    if (!services.memoryWorkbenchService) {
      throw new BankApiError(404, 'memory_workbench_disabled', '记忆专区未启用');
    }
    const detailData = await services.memoryWorkbenchService.detail(memoryDetailMatch[1]);
    usageEventLog.recordUsageEvent({ carrier: 'memory', caller: 'memory-workbench', query: 'detail:' + memoryDetailMatch[1].slice(0, 120), hits: 1 });
    return ok(detailData);
  }
  if (req.method === 'GET' && relative === '/agent-runs') {
    const query = requireAllowedQuery(url, ['status', 'limit', 'cursor']);
    return ok(await services.repository.listAgentRuns(identity.customerId, query));
  }
  if (req.method === 'GET' && relative === '/intent-recall-probe') {
    const query = requireAllowedQuery(url, ['message', 'tab', 'menuKey']);
    if (!context.agentRuntimeClient || typeof context.agentRuntimeClient.probeIntentRecall !== 'function') {
      throw new BankApiError(503, 'agent_runtime_unavailable', '智能服务运行时不可用');
    }
    const probe = await context.agentRuntimeClient.probeIntentRecall({
      message: query.message || '',
      tab: query.tab || '',
      menuKey: query.menuKey || '',
    });
    usageEventLog.recordUsageEvent({ carrier: 'graph', caller: 'intent-recall-probe', query: probe.graphVersion, hits: probe.candidates.length });
    return ok(probe);
  }
  const agentRunDetailMatch = relative.match(/^\/agent-runs\/([^/]+)$/);
  if (req.method === 'GET' && agentRunDetailMatch) {
    requireAllowedQuery(url, []);
    const detail = await services.repository.getAgentRunWorkbenchDetail(
      decodePath(agentRunDetailMatch[1]), identity.customerId,
    );
    if (!detail) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
    return ok(detail);
  }

  if (req.method === 'POST' && relative === '/agent-sessions') {
    requireEmptyObject(await parseJsonBody(req, context.bodyLimit));
    return created(await services.agentSessionService.create(identity.customerId));
  }
  if (req.method === 'POST' && relative === '/agent-sessions/recommendation') {
    requireUserAction(identity, 'Recommendation Session');
    requireIdempotencyKey(req);
    if (!services.recommendationFocusService
      || typeof services.recommendationFocusService.prepare !== 'function'
      || typeof services.agentSessionService.createFocused !== 'function') {
      throw new BankApiError(503, 'recommendation_focus_unavailable', '推荐咨询服务暂时不可用');
    }
    const body = exactRecommendationSessionBody(
      await parseJsonBody(req, context.bodyLimit),
    );
    const focus = await services.recommendationFocusService.prepare({
      recommendationId: body.recommendationId,
      customerId: identity.customerId,
      mode: body.mode,
    });
    return created(await services.agentSessionService.createFocused(
      identity.customerId, focus,
    ));
  }
  if (req.method === 'POST' && relative === '/agent-sessions/bootstrap') {
    requireUserAction(identity, 'Agent Session bootstrap');
    requireIdempotencyKey(req);
    const body = exactAgentSessionBootstrapBody(
      await parseJsonBody(req, context.bodyLimit),
    );
    if (body.sessionId) {
      try {
        const restored = await services.agentSessionService.get(
          body.sessionId, identity.customerId,
        );
        if (restored.status === 'active') return ok(restored);
      } catch (error) {
        if (!error || error.code !== 'agent_session_not_found') throw error;
      }
    }
    const fresh = await services.agentSessionService.create(identity.customerId);
    return created(Object.freeze({ ...fresh, turns: Object.freeze([]) }));
  }
  let agentSessionMatch = relative.match(/^\/agent-sessions\/([^/]+)$/);
  if (req.method === 'GET' && agentSessionMatch) {
    return ok(await services.agentSessionService.get(
      decodePath(agentSessionMatch[1]), identity.customerId,
    ));
  }
  agentSessionMatch = relative.match(/^\/agent-sessions\/([^/]+)\/close$/);
  if (req.method === 'POST' && agentSessionMatch) {
    requireUserAction(identity, '关闭 Agent Session');
    const idempotencyKey = requireIdempotencyKey(req);
    const sessionId = decodePath(agentSessionMatch[1]);
    const body = exactAgentSessionCloseBody(await parseJsonBody(req, context.bodyLimit));
    const request = { sessionId, reason: body.reason };
    const closed = await services.repository.closeAgentSessionIdempotent({
      id: sessionId,
      customerId: identity.customerId,
      closeReason: body.reason,
      idempotencyKey,
      requestHash: sha256(JSON.stringify(request)),
    });
    await releaseAgentSessionsBestEffort(context.agentRuntimeClient, [
      userAgentSessionId(sessionId),
    ]);
    return ok(closed);
  }
  agentSessionMatch = relative.match(/^\/agent-sessions\/([^/]+)\/resumable-workflows$/);
  if (req.method === 'GET' && agentSessionMatch) {
    const sessionId = decodePath(agentSessionMatch[1]);
    await services.agentSessionService.requireActive(sessionId, identity.customerId);
    return ok(await services.repository.listResumableAgentWorkflows(
      identity.customerId, context.intentGraph,
    ));
  }
  agentSessionMatch = relative.match(/^\/agent-sessions\/([^/]+)\/workflow-bindings$/);
  if (req.method === 'POST' && agentSessionMatch) {
    requireUserAction(identity, '恢复 Agent Workflow');
    const idempotencyKey = requireIdempotencyKey(req);
    const sessionId = decodePath(agentSessionMatch[1]);
    const body = exactAgentWorkflowBindingBody(await parseJsonBody(req, context.bodyLimit));
    const request = {
      sessionId,
      businessWorkflowId: body.businessWorkflowId,
      expectedWorkflowRevision: body.expectedWorkflowRevision,
    };
    const workflow = await services.repository.bindAgentWorkflowToSessionIdempotent({
      id: body.businessWorkflowId,
      customerId: identity.customerId,
      conversationId: sessionId,
      expectedRevision: body.expectedWorkflowRevision,
      idempotencyKey,
      requestHash: sha256(JSON.stringify(request)),
    });
    return ok(workflow);
  }

  const businessReplyMatch = relative.match(/^\/agent-runs\/([^/]+)\/business-replies$/);
  if (req.method === 'POST' && businessReplyMatch) {
    requireUserAction(identity, '业务办理信息补充');
    requireIdempotencyKey(req);
    const body = exactBusinessReplyBody(await parseJsonBody(req, context.bodyLimit));
    const agentRunId = decodePath(businessReplyMatch[1]);
    const run = await services.repository.getAgentRun(agentRunId);
    if (!run || run.customerId !== identity.customerId) {
      throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
    }
    let conversationContext = await loadConversationContext(
      services.repository, run.conversationId, run.customerId, context.now(),
    );
    if (!conversationContext.pendingEnvelopeId || !Array.isArray(conversationContext.goals)) {
      throw new BankApiError(409, 'agent_workflow_context_missing', '业务办理上下文已失效，请重新发起');
    }
    const businessWorkflowId = deriveBusinessWorkflowId(
      run.customerId, run.conversationId, conversationContext.pendingEnvelopeId,
    );
    const workflow = await services.repository.getAgentWorkflow(
      businessWorkflowId, run.conversationId, run.customerId,
    );
    if (!workflow || workflow.status !== 'awaiting_clarification'
      || !workflow.activeGoalTaskId) {
      throw new BankApiError(409, 'agent_workflow_not_awaiting_clarification', '当前业务任务不在等待补充信息');
    }
    await persistSafeSessionTurn(services.agentSessionService, {
      sessionId: run.conversationId,
      customerId: run.customerId,
      role: 'user',
      text: body.message,
      response: null,
    }, { phase: 'business_reply_user', requestId: run.id, traceId: run.traceId });
    conversationContext = await applyBusinessReplySelection(
      services, run, conversationContext, body.message, context.now(),
      workflow.activeGoalTaskId,
    );
    const releaseScope = beginPendingTerminalAgentSessionScope(context, run.id);
    try {
      const resumed = await invokeAgentRuntime(() => context.agentRuntimeClient.resumeBusinessWorkflow(
        businessWorkflowId,
        {
          resumeKind: 'clarification',
          clarificationMessage: body.message,
          agentRunId: run.id,
          conversationId: run.conversationId,
          customerId: run.customerId,
          taskId: workflow.activeGoalTaskId,
          grants: [],
          expectedWorkflowRevision: workflow.workflowRevision,
          conversationContext,
          executableCapabilityIds: run.executableCapabilityIds || [],
        },
      ));
      const convStatePromise = persistConversationState(
        services.repository, run.conversationId, run.customerId,
        resumed.conversationState, resumed.status,
      );
      const nextDelivery = {
      agentRunId: run.id,
      status: resumed.status,
      userResponse: resumed.userResponse || null,
      userResponses: resumed.userResponses || null,
      userResult: resumed.userResult || null,
      confirmation: resumed.confirmation || null,
      clarificationForm: resumed.clarificationForm || null,
      };
      if (resumed.status === 'awaiting_confirmation' || resumed.status === 'clarification_required') {
      const [, , traceEvents, responsePersisted] = await Promise.all([
        convStatePromise,
        persistAgentClarification(
          services.repository, nextDelivery, run.id, run.conversationId, run.customerId,
          {
            businessWorkflowId,
            taskId: resumed.activeGoalTaskId,
            expectedWorkflowRevision: resumed.workflowRevision,
          },
        ),
        persistWorkflowResumeTrace(
          services.repository, run, resumed.status, resumed, context.now(), 'business_reply',
        ),
        persistAssistantSessionTurn(
          services.agentSessionService, nextDelivery, run.conversationId,
          run.customerId, run.id, run.traceId,
        ),
      ]);
      await releaseScope.finish(responsePersisted);
      return ok({
        status: 'NEXT_ACTION_REQUIRED',
        workflowRevision: resumed.workflowRevision,
        taskProgress: resumed.taskProgress,
        nextDelivery,
        traceEvents,
      });
    }
    const [, traceEvents, responsePersisted] = await Promise.all([
      convStatePromise,
      persistWorkflowResumeTrace(
        services.repository, run,
        resumed.status === 'failed' ? 'failed' : 'succeeded',
        resumed, context.now(), 'business_reply',
      ),
      persistAssistantSessionTurn(
        services.agentSessionService, nextDelivery, run.conversationId,
        run.customerId, run.id, run.traceId,
      ),
    ]);
    await releaseScope.finish(responsePersisted);
    return ok({
      status: resumed.status === 'succeeded' ? 'SUCCEEDED' : 'FAILED',
      workflowRevision: resumed.workflowRevision,
      nextDelivery,
      traceEvents,
    });
    } finally {
      await releaseScope.finish(false);
    }
  }

  const clarificationMatch = relative.match(/^\/agent-runs\/([^/]+)\/clarifications$/);
  if (req.method === 'POST' && clarificationMatch) {
    requireUserAction(identity, '转账收款信息补录');
    requireIdempotencyKey(req);
    const body = exactAgentClarificationBody(await parseJsonBody(req, context.bodyLimit));
    const agentRunId = decodePath(clarificationMatch[1]);
    const run = await services.repository.getAgentRun(agentRunId);
    if (!run || run.customerId !== identity.customerId) {
      throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
    }
    const record = await services.repository.getAgentClarification(
      body.clarificationId, agentRunId, identity.customerId,
    );
    if (!record) throw new BankApiError(404, 'clarification_not_found', '补录任务不存在');
    const suppliedHash = sha256(body.submissionToken);
    if (!safeHashEqual(record.tokenHash, suppliedHash)) {
      throw new BankApiError(403, 'clarification_token_invalid', '补录凭证无效');
    }
    const accountNo = body.values.accountNo.replace(/[\s-]/g, '');
    // 当 accountNo 是掩码值（包含 *），说明收款人已保存，从数据库
    // 按姓名查找已保存的收款人记录，使用真实的 payeeId 和 targetAccountId
    let payeeId, targetAccountId, maskedNo;
    if (accountNo.includes('*')) {
      const savedPayees = await services.repository.listPayees(identity.customerId);
      const matched = savedPayees.find(
        p => p.name === body.values.payeeName && p.status === 'ACTIVE'
      );
      if (matched) {
        payeeId = matched.id;
        targetAccountId = matched.targetAccountId;
        maskedNo = matched.maskedNo;
      }
    }
    if (!payeeId) {
      const digest = crypto.createHmac(
        'sha256', String(context.internalExecutionToken || 'ai-bank-mock-clarification'),
      ).update(identity.customerId + ':' + accountNo).digest('hex');
      payeeId = 'payee_mock_' + digest.slice(0, 24);
      targetAccountId = 'acct_payee_' + digest.slice(0, 24);
      maskedNo = '****' + accountNo.slice(-4);
    }
    const consumed = await services.repository.consumeAgentClarification({
      id: body.clarificationId,
      agentRunId,
      customerId: identity.customerId,
      tokenHash: suppliedHash,
      payeeName: body.values.payeeName,
      bankName: body.values.bankName,
      maskedNo,
      targetAccountId,
      payeeId,
      savePayee: body.values.savePayee,
      referenceExpiresAt: new Date(Date.parse(context.now()) + 15 * 60 * 1000).toISOString(),
    });
    if (record.businessWorkflowId) {
      const workflow = await services.repository.getAgentWorkflow(
        record.businessWorkflowId, run.conversationId, run.customerId,
      );
      if (!workflow || workflow.workflowRevision !== record.expectedWorkflowRevision
        || workflow.activeGoalTaskId !== record.taskId) {
        throw new BankApiError(409, 'agent_workflow_revision_conflict', '业务办理状态已变化，请刷新后继续');
      }
      let conversationContext = await loadConversationContext(
        services.repository, run.conversationId, run.customerId, context.now(),
      );
      // 将用户选择的转出账户注入 conversationContext.sharedSlots，
      // source 为 user_selection，使后端 _sibling_from_accounts 能提取
      // 并通过 _assess（account_reference 要求 user_explicit/user_selection）。
      // 这与 applyBusinessReplySelection 的 sharedSlots 注入逻辑一致。
      const sharedIntentId = (conversationContext.goals && conversationContext.goals[0])
        ? conversationContext.goals[0].intentId
        : 'ccb.transfer.create';
      if (body.values.fromAccount) {
        conversationContext = Object.assign({}, conversationContext, {
          sharedSlots: (conversationContext.sharedSlots || [])
            .filter(item => item.name !== 'fromAccount')
            .concat({
              name: 'fromAccount', value: body.values.fromAccount, source: 'user_selection',
              sourceIntentId: sharedIntentId, updatedAt: context.now(),
            }),
        });
      } else {
        // 表单未携带 fromAccount（后端未渲染 select 字段，或前序轮次
        // 已选但本笔表单未包含）时，从已加载的 conversationContext 中
        // 恢复 fromAccount 并将 source 提升为 user_selection。批量转账
        // 中用户在首笔已选择付款账户，后续笔次表单可能不含该字段。
        const existingFa = (conversationContext.sharedSlots || [])
          .find(item => item.name === 'fromAccount' && item.value);
        if (existingFa) {
          conversationContext = Object.assign({}, conversationContext, {
            sharedSlots: (conversationContext.sharedSlots || [])
              .filter(item => item.name !== 'fromAccount')
              .concat({
                name: 'fromAccount', value: existingFa.value, source: 'user_selection',
                sourceIntentId: sharedIntentId, updatedAt: context.now(),
              }),
          });
        }
      }
      const releaseScope = beginPendingTerminalAgentSessionScope(context, run.id);
      try {
        // 后端用 clarification_message 作为"用户回复"重新走槽位评估。
        // amount 必须编码进消息，否则后端重新评估时 amount_minor 仍为 0，
        // 会再次生成 transfer_full 卡片导致死循环。
        // 注意：不能含完整卡号（后端 contracts 校验 \d{15,} 会拒绝）。
        const clarificationParts = [];
        const effectiveFromAccount = body.values.fromAccount
          || ((conversationContext.sharedSlots || []).find(item => item.name === 'fromAccount' && item.value) || {}).value;
        if (effectiveFromAccount) {
          clarificationParts.push('用' + effectiveFromAccount + '的账户');
        }
        if (body.values.payeeName) {
          clarificationParts.push('向' + body.values.payeeName);
        } else {
          clarificationParts.push('继续转账');
        }
        if (body.values.amountMinor > 0) {
          clarificationParts.push('转账' + body.values.amount + '元');
        } else {
          clarificationParts.push('转账');
        }
        const clarificationMessage = clarificationParts.join('');
        const resumed = await invokeAgentRuntime(() => context.agentRuntimeClient.resumeBusinessWorkflow(
          record.businessWorkflowId,
          {
            resumeKind: 'clarification',
            agentRunId: run.id,
            conversationId: run.conversationId,
            customerId: run.customerId,
            taskId: record.taskId,
            grants: [],
            expectedWorkflowRevision: workflow.workflowRevision,
            conversationContext,
            executableCapabilityIds: run.executableCapabilityIds || [],
            clarificationMessage,
          },
        ));
        // 表单提交后自动确认"本笔"检查点：用户已在表单中核对并提交
        // 全部转账信息（收款人、卡号、银行、金额、付款账户），不应再对
        // 同一笔要求二次确认。但批量转账的后续任务（第2笔及以后）是
        // 独立资金出口，必须返回确认卡由用户逐笔本人确认（sequential
        // confirmation），不得静默代办。因此 auto-confirm 上限为 1：
        // 确认完刚提交的这笔后，若出现下一笔的 awaiting_confirmation，
        // 作为 nextDelivery.confirmation 返还前端渲染确认卡。
        let finalResumed = resumed;
        const MAX_AUTO_CONFIRM = 1;
        const AUTO_CONFIRM_TIMEOUT_MS = 60000;
        const _autoConfirmStartedAt = Date.now();
        let autoConfirmAttempts = 0;
        // 批量转账逐笔在服务端自动执行，若不把每笔明细带回前端，
        // 用户只能看到"全部完成"却看不到第二笔（如李四）执行过。
        const executedTransfers = [];
        while (
          finalResumed.status === 'awaiting_confirmation'
          && finalResumed.confirmation
          && autoConfirmAttempts < MAX_AUTO_CONFIRM
          && (Date.now() - _autoConfirmStartedAt) < AUTO_CONFIRM_TIMEOUT_MS
        ) {
          autoConfirmAttempts += 1;
          try {
            const _pendingSummary = finalResumed.confirmation.summary || null;
            if (_pendingSummary && Array.isArray(_pendingSummary.transferDetails)) {
              const _pendingFa = _pendingSummary.fromAccount || null;
              _pendingSummary.transferDetails.forEach((_detail) => {
                executedTransfers.push({
                  payeeName: _detail.payeeName || null,
                  payeeBank: _detail.payeeBank || null,
                  payeeTail: _detail.payeeTail || null,
                  amount: _detail.amount || null,
                  fromAccountName: _pendingFa && _pendingFa.name || null,
                  fromAccountTail: _pendingFa && _pendingFa.tail || null,
                });
              });
            }
            const _capabilityService = services.agentCapabilityService || createAgentCapabilityService(services);
            const _authorization = await _capabilityService.confirm({
              agentRunId: run.id,
              checkpointId: finalResumed.confirmation.checkpointId,
              confirmationToken: finalResumed.confirmation.confirmationToken,
              customerId: identity.customerId,
              actorType: 'user',
            });
            if (_authorization.status !== 'AUTHORIZED') break;
            const _resumePromise = invokeAgentRuntime(() => context.agentRuntimeClient.resumeBusinessWorkflow(
              _authorization.businessWorkflowId,
              {
                resumeKind: 'confirmation',
                agentRunId: run.id,
                conversationId: run.conversationId,
                customerId: run.customerId,
                checkpointId: _authorization.checkpointId,
                grants: _authorization.grants,
                expectedWorkflowRevision: finalResumed.workflowRevision,
                conversationContext,
                executableCapabilityIds: run.executableCapabilityIds || [],
              },
            ));
            const _remainingBudget = AUTO_CONFIRM_TIMEOUT_MS - (Date.now() - _autoConfirmStartedAt);
            finalResumed = await Promise.race([
              _resumePromise,
              new Promise((_resolve, _reject) => setTimeout(
                () => _reject(new Error('auto-confirm resume timed out')),
                Math.max(3000, _remainingBudget),
              )),
            ]);
          } catch (_autoConfirmError) {
            console.warn('[ai-native-bank] clarification auto-confirm failed (attempt %d), falling back to confirmation card: %s',
              autoConfirmAttempts, _autoConfirmError && _autoConfirmError.message);
            break;
          }
        }
        if (autoConfirmAttempts > 0) {
          console.log('[ai-native-bank] clarification auto-confirm completed: attempts=%d elapsed=%dms status=%s',
            autoConfirmAttempts, Date.now() - _autoConfirmStartedAt, finalResumed.status);
        }
        await persistConversationState(
          services.repository, run.conversationId, run.customerId,
          finalResumed.conversationState, finalResumed.status,
        );
        const nextDelivery = {
          agentRunId: run.id,
          status: finalResumed.status,
          userResponse: finalResumed.userResponse || null,
          userResponses: finalResumed.userResponses || null,
          confirmation: finalResumed.confirmation || null,
          clarificationForm: finalResumed.clarificationForm || null,
        };
        if (finalResumed.status === 'awaiting_confirmation' || finalResumed.status === 'clarification_required') {
        await persistAgentClarification(
          services.repository, nextDelivery, run.id, run.conversationId, run.customerId,
          {
            businessWorkflowId: record.businessWorkflowId,
            taskId: finalResumed.activeGoalTaskId,
            expectedWorkflowRevision: finalResumed.workflowRevision,
          },
        );
        const traceEvents = await persistWorkflowResumeTrace(
          services.repository, run, finalResumed.status, finalResumed, context.now(), 'clarification',
        );
        const responsePersisted = await persistAssistantSessionTurn(
          services.agentSessionService, nextDelivery, run.conversationId,
          run.customerId, run.id, run.traceId,
        );
        await releaseScope.finish(responsePersisted);
        return ok({
          status: 'NEXT_ACTION_REQUIRED',
          workflowRevision: finalResumed.workflowRevision,
          taskProgress: finalResumed.taskProgress,
          payee: {
            name: consumed.payee.name,
            bankName: consumed.payee.bankName,
            maskedNo: consumed.payee.maskedNo,
            status: consumed.payee.status,
            saved: body.values.savePayee,
          },
          // 已自动执行的本笔明细：前端据此渲染"张三任务卡"，再弹下一
          // 笔（李四）的确认卡，形成逐任务的可见执行序列。
          executedTransfers,
          nextDelivery,
          traceEvents,
        });
      }
      const traceEvents = await persistWorkflowResumeTrace(
        services.repository, run,
        finalResumed.status === 'failed' ? 'failed' : 'succeeded',
        finalResumed, context.now(), 'clarification',
      );
      const responsePersisted = await persistAssistantSessionTurn(
        services.agentSessionService, nextDelivery, run.conversationId,
        run.customerId, run.id, run.traceId,
      );
      await releaseScope.finish(responsePersisted);
      // 终态响应必须带批次进度：前端任务卡编号（转账任务 X/Y）与
      // "共N个任务完成"文案都依赖 taskProgress.total。运行时终态
      // 响应不含该字段时，用会话目标数合成，避免编号退化为 1/1。
      const goalCount = (conversationContext.goals || []).length;
      const finalTaskProgress = finalResumed.taskProgress
        || (goalCount > 0 ? {
          mode: 'sequential', current: goalCount, total: goalCount,
          completed: goalCount, failed: 0, skipped: 0,
        } : null);
      return ok({
        status: finalResumed.status === 'succeeded' ? 'SUCCEEDED' : 'FAILED',
        workflowRevision: finalResumed.workflowRevision,
        taskProgress: finalTaskProgress,
        payee: {
          name: consumed.payee.name,
          bankName: consumed.payee.bankName,
          maskedNo: consumed.payee.maskedNo,
          status: consumed.payee.status,
          saved: body.values.savePayee,
        },
        // 成功/失败时也返回最终投递（含结果卡片），否则前端只能弹
        // 一个转瞬即逝的 toast，用户会误以为流程卡死。
        nextDelivery,
        executedTransfers,
        traceEvents,
      });
      } finally {
        await releaseScope.finish(false);
      }
    }
    return ok({
      status: 'accepted',
      payee: {
        name: consumed.payee.name,
        bankName: consumed.payee.bankName,
        maskedNo: consumed.payee.maskedNo,
        status: consumed.payee.status,
        saved: body.values.savePayee,
      },
      resumeMessage: '继续办理向' + consumed.payee.name + '转账',
    });
  }

  const confirmationMatch = relative.match(/^\/agent-runs\/([^/]+)\/confirmations$/);
  if (req.method === 'POST' && confirmationMatch) {
    requireUserAction(identity, 'Agent 操作确认');
    requireIdempotencyKey(req);
    const body = exactAgentConfirmationBody(await parseJsonBody(req, context.bodyLimit));
    const service = services.agentCapabilityService || createAgentCapabilityService(services);
    const agentRunId = decodePath(confirmationMatch[1]);
    const credentials = body.confirmations || [{
      checkpointId: body.checkpointId,
      confirmationToken: body.confirmationToken,
    }];
    const authorizations = [];
    for (const credential of credentials) {
      authorizations.push(await service.confirm({
        agentRunId, checkpointId: credential.checkpointId,
        confirmationToken: credential.confirmationToken,
        customerId: identity.customerId, actorType: identity.actorType,
      }));
    }
    if (authorizations.some(item => item.status !== 'AUTHORIZED')) {
      if (authorizations.length !== 1) {
        throw new BankApiError(409, 'batch_confirmation_conflict', '批次确认状态不一致，请重新发起');
      }
      return ok(authorizations[0]);
    }
    const workflowIds = new Set(authorizations.map(item => item.businessWorkflowId));
    if (workflowIds.size !== 1) {
      throw new BankApiError(409, 'batch_confirmation_conflict', '批次确认不属于同一业务流程');
    }
    const authorization = {
      checkpointId: authorizations[0].checkpointId,
      businessWorkflowId: authorizations[0].businessWorkflowId,
      grants: authorizations.flatMap(item => item.grants),
    };
    const run = await services.repository.getAgentRun(agentRunId);
    if (!run || run.customerId !== identity.customerId) {
      throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
    }
    const workflow = await services.repository.getAgentWorkflow(
      authorization.businessWorkflowId, run.conversationId, run.customerId,
    );
    if (!workflow) {
      throw new BankApiError(409, 'agent_workflow_not_found', '业务流程状态不存在');
    }
    const conversationContext = await loadConversationContext(
      services.repository, run.conversationId, run.customerId, context.now(),
    );
    const releaseScope = beginPendingTerminalAgentSessionScope(context, run.id);
    try {
      const resumed = await invokeAgentRuntime(() => context.agentRuntimeClient.resumeBusinessWorkflow(
        authorization.businessWorkflowId,
        {
          agentRunId: run.id,
          conversationId: run.conversationId,
          customerId: run.customerId,
          checkpointId: authorization.checkpointId,
          grants: authorization.grants,
          expectedWorkflowRevision: workflow.workflowRevision,
          conversationContext,
          executableCapabilityIds: run.executableCapabilityIds || [],
        },
      ));
      await persistConversationState(
        services.repository, run.conversationId, run.customerId,
        resumed.conversationState, resumed.status,
      );
      const completedUserResult = await buildCompletedUserResult(
        services.repository, authorization, resumed, run.customerId, workflow,
      );
      const nextDelivery = {
        agentRunId: run.id,
        status: resumed.status,
        userResult: completedUserResult || null,
        userResponse: resumed.userResponse || null,
        userResponses: resumed.userResponses || null,
        confirmation: resumed.confirmation || null,
        clarificationForm: resumed.clarificationForm || null,
      };
      if (resumed.status === 'awaiting_confirmation' || resumed.status === 'clarification_required') {
      await persistAgentClarification(
        services.repository, nextDelivery, run.id, run.conversationId, run.customerId,
        {
          businessWorkflowId: authorization.businessWorkflowId,
          taskId: resumed.activeGoalTaskId,
          expectedWorkflowRevision: resumed.workflowRevision,
        },
      );
      const traceEvents = await persistWorkflowResumeTrace(
        services.repository, run, resumed.status, resumed, context.now(),
      );
      const responsePersisted = await persistAssistantSessionTurn(
        services.agentSessionService, nextDelivery, run.conversationId,
        run.customerId, run.id, run.traceId,
      );
      await releaseScope.finish(responsePersisted);
      return ok({
        checkpointId: authorization.checkpointId,
        capabilityId: authorization.grants[0].capabilityId,
        status: 'NEXT_ACTION_REQUIRED',
        workflowRevision: resumed.workflowRevision,
        taskProgress: resumed.taskProgress,
        completedUserResult,
        nextDelivery,
        traceEvents,
      });
    }
    const traceEvents = await persistWorkflowResumeTrace(
      services.repository, run,
      resumed.status === 'failed' ? 'failed' : 'succeeded',
      resumed, context.now(),
    );
    const responsePersisted = await persistAssistantSessionTurn(
      services.agentSessionService, nextDelivery, run.conversationId,
      run.customerId, run.id, run.traceId,
    );
    await releaseScope.finish(responsePersisted);
    return ok({
      checkpointId: authorization.checkpointId,
      capabilityId: authorization.grants[0].capabilityId,
      status: resumed.status === 'succeeded' ? 'SUCCEEDED'
        : resumed.status === 'partial_success' ? 'PARTIAL_SUCCESS' : 'FAILED',
      completedUserResult,
      result: {
        status: resumed.status === 'succeeded' ? 'SUCCEEDED'
          : resumed.status === 'partial_success' ? 'PARTIAL_SUCCESS' : 'FAILED',
        completedTaskCount: resumed.receipts
          .filter(item => item.status === 'SUCCEEDED' && isSideEffectingReceipt(item)).length,
        failedTaskCount: resumed.receipts
          .filter(item => item.status === 'FAILED' && isSideEffectingReceipt(item)).length,
        taskCount: resumed.receipts.filter(isSideEffectingReceipt).length,
        items: resumed.receipts
          .filter(isSideEffectingReceipt)
          .map((item, index) => ({
            index: index + 1,
            status: item.status === 'SUCCEEDED' ? 'SUCCEEDED' : 'FAILED',
            queryable: true,
          })),
      },
      traceEvents,
    });
    } finally {
      await releaseScope.finish(false);
    }
  }

  if (req.method === 'GET' && relative === '/agent-runtime/status') {
    return ok(await readAgentRuntimeStatus(context.agentRuntimeClient));
  }
  if (req.method === 'GET' && relative === '/agent-runtime/service-progress-profiles') {
    requireAllowedQuery(url, []);
    return ok(context.serviceProgressRegistry.getPublicSnapshot());
  }
  if (req.method === 'POST' && relative === '/agent-runs') {
    const body = await parseJsonBody(req, context.bodyLimit);
    const normalized = normalizeAgentRunRequest(body);
    let session;
    try {
      session = await services.agentSessionService.requireActive(
        normalized.conversationId, identity.customerId,
      );
    } catch (error) {
      if (error && error.code === 'agent_session_not_found') {
        throw new BankApiError(409, 'agent_session_not_found', 'Agent Session 不存在或不属于当前客户');
      }
      throw error;
    }
    // 并行执行：getFocus + touch + listPendingActionCheckpoints（三者仅依赖 session.id，无相互依赖）
    const canQueryFocus = typeof services.agentSessionService.getFocus === 'function';
    const canQueryCheckpoints = !normalized.recommendationId
      && typeof services.repository.listPendingActionCheckpointsByConversation === 'function';
    const [focusResult, touchedSession, pendingCheckpoints] = await Promise.all([
      canQueryFocus
        ? services.agentSessionService.getFocus(session.id, identity.customerId, session)
        : Promise.resolve(null),
      services.agentSessionService.touch(session.id, identity.customerId),
      canQueryCheckpoints
        ? services.repository.listPendingActionCheckpointsByConversation(
            session.id, identity.customerId, context.now(),
          )
        : Promise.resolve([]),
    ]);
    let conversationFocus = focusResult;
    session = touchedSession;
    let sessionTransition = null;
    if (conversationFocus && !normalized.recommendationId) {
      const focusRoute = context.recommendationFocusRouter.route({
        focus: conversationFocus,
        message: normalized.message,
        workflowControl: normalized.workflowControl,
      });
      if (focusRoute.action === 'rotate') {
        session = await services.agentSessionService.create(identity.customerId);
        conversationFocus = null;
        sessionTransition = 'recommendation_focus_exit';
      }
    }
    // 确认卡待处理时，客户文本答复优先按「确认答复」处理：affirm → 直接授权并恢复执行
    // （轮转后 sessionTransition 非空，跳过旧会话的待确认检查点）
    if (canQueryCheckpoints && !sessionTransition && pendingCheckpoints.length) {
      const checkpoint = pendingCheckpoints[0];
      const decision = await classifyConfirmationReply(
        context.modelGateway, checkpoint, normalized.message,
      );
      console.log('[confirm-audit] router classify', JSON.stringify({
        conversationId: session.id, checkpointId: checkpoint.id,
        message: normalized.message, decision,
        at: new Date().toISOString(),
      }));
      if (decision === 'affirm') {
        const payload = await confirmByReplyAndResume({
          services, context, conversationId: session.id,
          customerId: identity.customerId, checkpoint, message: normalized.message,
        });
        // 恢复续流不是一次完整运行，绝不伪造全新 SSE 轨迹流：
        // 以 409+载荷交付，前端按 resumeConfirmation 通道续接原轮轨迹并渲染交付卡。
        throw new BankApiError(409, 'agent_confirmation_reply', '该消息已作为当前待确认操作的确认答复处理', [
          { field: 'confirmationReply', code: 'reply_resume_payload', data: payload },
        ]);
      }
    }
    const agentRunId = 'abr_' + crypto.randomUUID();
    const conversationId = session.id;
    const responseMeta = sessionTransition
      ? { effectiveConversationId: conversationId, sessionTransition }
      : undefined;
    let preparedAssistance = null;
    let runtimeMessage = normalized.message;
    let displayMessage = runtimeMessage;
    if (normalized.recommendationId) {
      if (!services.assistanceAdapter || typeof services.assistanceAdapter.prepare !== 'function') {
        throw new BankApiError(503, 'assistance_service_unavailable', 'Guided assistance is unavailable');
      }
      preparedAssistance = await services.assistanceAdapter.prepare({
        recommendationId: normalized.recommendationId,
        serviceActionId: normalized.serviceActionId,
        customerId: identity.customerId,
        conversationId,
        agentRunId,
      });
      runtimeMessage = preparedAssistance.userMessage;
      displayMessage = preparedAssistance.displayMessage || runtimeMessage;
    }
    // 并行执行：用户会话持久化 + 对话上下文加载 + 安全会话获取 + AgentRun 创建（均无相互依赖）
    const runtimeProjection = capabilityRegistry.projectForAgentRuntime({ environment: 'mock' });
    const [userTurnPersisted, conversationContext, safetySession] = await Promise.all([
      persistSafeSessionTurn(services.agentSessionService, {
        sessionId: conversationId,
        customerId: identity.customerId,
        role: 'user',
        text: displayMessage,
        response: null,
      }, { phase: 'user', requestId: agentRunId, traceId: context.meta.traceId }),
      loadConversationContext(
        services.repository,
        conversationId,
        identity.customerId,
        context.now(),
      ),
      typeof services.agentSessionService.get === 'function'
        ? services.agentSessionService.get(conversationId, identity.customerId)
        : Promise.resolve(null),
      services.repository.createAgentRun({
        id: agentRunId,
        customerId: identity.customerId,
        conversationId,
        traceId: context.meta.traceId,
        status: 'accepted',
        graphVersion: context.intentGraph.version,
        graphDigest: context.intentGraph.digest,
        executableCapabilityIds: runtimeProjection.executableCapabilities.map(item => item.id),
        plannableCapabilityIds: runtimeProjection.plannableCapabilities.map(item => item.id),
      }),
    ]);
    let recentSafetyMessages = [];
    if (safetySession) {
      const turns = Array.isArray(safetySession.turns) ? [...safetySession.turns] : [];
      if (
        turns.at(-1) && turns.at(-1).role === 'user'
        && (userTurnPersisted || turns.at(-1).text === displayMessage)
      ) {
        turns.pop();
      }
      recentSafetyMessages = turns
        .flatMap((turn, index) => {
          if (!turn || turn.role !== 'user' || typeof turn.text !== 'string') return [];
          const response = turns[index + 1];
          const endedByNotice = response && response.role === 'assistant'
            && response.response && response.response.type === 'notice';
          return endedByNotice ? [] : [turn.text];
        })
        .slice(-6);
    }
    const input = {
      agentRunId,
      conversationId,
      requestId: context.meta.requestId,
      traceId: context.meta.traceId,
      customerId: identity.customerId,
      actorType: 'agent',
      message: runtimeMessage,
      ...(preparedAssistance ? { entryGoal: preparedAssistance.goalEnvelope } : {}),
      pageContext: normalized.pageContext,
      conversationContext,
      safetyContext: { recentUserMessages: recentSafetyMessages },
      ...(conversationFocus ? { conversationFocus } : {}),
      executableCapabilities: runtimeProjection.executableCapabilities.map(projectAgentCapability),
      plannableCapabilities: runtimeProjection.plannableCapabilities.map(projectAgentCapability),
    };
    const releaseScope = beginPendingTerminalAgentSessionScope(context, agentRunId);
    try {
      const controlled = normalized.recommendationId ? null : await handleConversationControlRun({
      message: runtimeMessage,
      agentRunId,
      conversationId,
      customerId: identity.customerId,
      traceId: context.meta.traceId,
      repository: services.repository,
      runtimeClient: context.agentRuntimeClient,
      nowIso: context.now(),
      runtimeInput: input,
      workflowControl: normalized.workflowControl,
      intentGraph: context.intentGraph,
    });
      if (controlled) {
      await advanceAgentRun(
        services.repository, agentRunId, controlled.delivery.status,
        controlled.traceEvents.length,
      );
      const responsePersisted = await persistAssistantSessionTurn(
        services.agentSessionService, controlled.delivery, conversationId,
        identity.customerId, agentRunId, context.meta.traceId,
      );
      await releaseScope.finish(responsePersisted);
      if (responsePersisted && controlled.terminalWorkflow) {
        await releaseAgentSessionsBestEffort(context.agentRuntimeClient, [
          businessAgentSessionId(identity.customerId, controlled.terminalWorkflow.id),
        ]);
      }
      if (String(req.headers.accept || '').toLowerCase().includes('text/event-stream')) {
        return streamControlledAgentRun(res, controlled, conversationId);
      }
      return created(controlled.delivery, responseMeta);
    }
      const wantsSse = String(req.headers.accept || '').toLowerCase().includes('text/event-stream');
      try {
        if (wantsSse) {
          return await streamAgentRunResponse(
            req, res, context.agentRuntimeClient, input, services.repository,
            services.agentSessionService, releaseScope,
            conversationId,
          );
        }
        const result = await invokeAgentRuntime(() => context.agentRuntimeClient.createAgentRun(input));
        const delivery = result && result.delivery ? result.delivery : result;
        const conversationState = result && result.delivery ? result.conversationState : null;
        const [, , , , responsePersisted] = await Promise.all([
          suspendSupersededWorkflow(
            services.repository, conversationId, identity.customerId, conversationState,
          ),
          persistAgentClarification(
            services.repository, delivery, agentRunId, conversationId, identity.customerId,
            { conversationState },
          ),
          persistConversationState(
            services.repository, conversationId, identity.customerId,
            conversationState, delivery.status,
          ),
          advanceAgentRun(services.repository, agentRunId, delivery.status, 0),
          persistAssistantSessionTurn(
            services.agentSessionService, delivery, conversationId,
            identity.customerId, agentRunId, context.meta.traceId,
          ),
        ]);
        await releaseScope.finish(responsePersisted);
        return created(delivery, responseMeta);
      } catch (runtimeError) {
      if (input.entryGoal
        && runtimeError instanceof BankApiError
        && !res.headersSent
        && ['agent_runtime_unavailable', 'agent_runtime_not_ready', 'agent_runtime_timeout'].includes(runtimeError.code)
      ) {
        const fallback = await handleGuidedGoalFallback(input, services, context);
        if (fallback) {
            for (const event of fallback.traceEvents) {
              if (typeof services.repository.appendAgentRunEvent === 'function') {
                try {
                  await services.repository.appendAgentRunEvent({
                    agentRunId: event.agentRunId,
                    sequence: event.sequence,
                    stage: event.stage,
                    status: event.status,
                    durationMs: event.durationMs == null ? null : event.durationMs,
                    policyVersion: null,
                    safeSummary: event.summary,
                    createdAt: event.timestamp,
                  });
                } catch (_) { /* best-effort persistence */ }
              }
            }
            await advanceAgentRun(
              services.repository, agentRunId,
              fallback.delivery.status, fallback.traceEvents.length,
            );
            if (typeof services.repository.finishAgentRunTraceCapture === 'function') {
              try { await services.repository.finishAgentRunTraceCapture(agentRunId, 'complete'); }
              catch (_) { /* best-effort */ }
            }
            const responsePersisted = await persistAssistantSessionTurn(
              services.agentSessionService, fallback.delivery, conversationId,
              identity.customerId, agentRunId, context.meta.traceId,
            );
            await releaseScope.finish(responsePersisted);
            if (wantsSse) {
              return streamControlledAgentRun(res, fallback, conversationId);
            }
            return created(fallback.delivery, responseMeta);
          }
        }
        throw runtimeError;
      }
    } finally {
      await releaseScope.finish(false);
    }
  }

  if (req.method === 'GET' && relative === '/capabilities') {
    return ok(capabilityRegistry.listForActor({ actorType: identity.actorType, environment: 'mock' }));
  }
  const capabilityMatch = relative.match(/^\/capabilities\/([^/]+)$/);
  if (req.method === 'GET' && capabilityMatch) {
    const item = capabilityRegistry.getForActor(decodePath(capabilityMatch[1]), { actorType: identity.actorType, environment: 'mock' });
    if (!item) throw new BankApiError(404, 'not_found', '能力不存在');
    return ok(item);
  }

  const customerId = identity.customerId;
  const actor = { type: identity.actorType, id: identity.actorType === 'user' ? customerId : 'agent_mobile_bank', customerId, traceId: context.meta.traceId };

  /* ---- 财富域：产品目录 / 持仓 / 申赎 / 发薪理财 ---- */
  if (relative === '/financial-products' || relative.startsWith('/financial-products/')
    || relative === '/wealth/holdings' || relative === '/wealth/transactions'
    || relative === '/wealth/salary-insights' || relative === '/wealth/recommendations'
    || relative === '/wealth/purchases' || relative === '/wealth/redemptions') {
    if (!services.wealthService) throw new BankApiError(503, 'wealth_service_unavailable', '财富服务暂不可用');
    if (req.method === 'GET' && relative === '/financial-products') {
      return ok(await services.wealthService.listProducts({ category: url.searchParams.get('category') }));
    }
    const productMatch = relative.match(/^\/financial-products\/([^/]+)$/);
    if (req.method === 'GET' && productMatch) {
      return ok(await services.wealthService.getProduct(decodePath(productMatch[1])));
    }
    if (req.method === 'GET' && relative === '/wealth/holdings') return ok(await services.wealthService.listHoldings(customerId));
    if (req.method === 'GET' && relative === '/wealth/transactions') return ok(await services.wealthService.listTransactions(customerId));
    if (req.method === 'GET' && relative === '/wealth/salary-insights') return ok(await services.wealthService.getSalaryInsights(customerId));
    if (req.method === 'GET' && relative === '/wealth/recommendations') return ok(await services.wealthService.getRecommendations(customerId));
    if (req.method === 'POST' && relative === '/wealth/purchases') {
      requireUserAction(identity, '理财申购');
      return created(await services.wealthService.purchase(actor, await parseJsonBody(req, context.bodyLimit), requireIdempotencyKey(req)));
    }
    if (req.method === 'POST' && relative === '/wealth/redemptions') {
      requireUserAction(identity, '理财赎回');
      return created(await services.wealthService.redeem(actor, await parseJsonBody(req, context.bodyLimit), requireIdempotencyKey(req)));
    }
  }

  /* ---- 贷款域：我的贷款 / 还款计划 / 统一测额 ---- */
  if (relative === '/loans' || relative.startsWith('/loans/') || relative === '/loans') {
    if (!services.loanService) throw new BankApiError(503, 'loan_service_unavailable', '贷款服务暂不可用');
    if (req.method === 'GET' && relative === '/loans') return ok(await services.loanService.listLoans(customerId));
    if (req.method === 'POST' && relative === '/loans/precalculations') {
      return created(await services.loanService.precalculate(actor, await parseJsonBody(req, context.bodyLimit), requireIdempotencyKey(req)));
    }
    const loanRepaymentMatch = relative.match(/^\/loans\/([^/]+)\/repayments$/);
    if (req.method === 'GET' && loanRepaymentMatch) {
      return ok(await services.loanService.listRepayments(customerId, decodePath(loanRepaymentMatch[1])));
    }
    const loanMatch = relative.match(/^\/loans\/([^/]+)$/);
    if (req.method === 'GET' && loanMatch) {
      return ok(await services.loanService.getLoan(customerId, decodePath(loanMatch[1])));
    }
  }

  if (req.method === 'GET' && relative === '/customers/me') return ok(await services.bankService.getCustomer(customerId));
  if (req.method === 'GET' && relative === '/accounts') return ok(await services.bankService.listAccounts(customerId));
  if (req.method === 'GET' && relative === '/payees') return ok(await services.bankService.listPayees(customerId));
  if (req.method === 'GET' && relative === '/credit-cards') return ok(await services.creditCardService.listCards(customerId));

  let cardMatch = relative.match(/^\/credit-cards\/([^/]+)\/transactions$/);
  if (req.method === 'GET' && cardMatch) {
    return ok(await services.creditCardService.listTransactions(customerId, decodePath(cardMatch[1]), {
      cursor: url.searchParams.get('cursor') || null,
      limit: url.searchParams.has('limit') ? url.searchParams.get('limit') : undefined,
    }));
  }
  cardMatch = relative.match(/^\/credit-cards\/([^/]+)\/settings$/);
  if (req.method === 'GET' && cardMatch) return ok(await services.creditCardService.getSettings(customerId, decodePath(cardMatch[1])));
  cardMatch = relative.match(/^\/credit-cards\/([^/]+)\/bill$/);
  if (req.method === 'GET' && cardMatch) return ok(await services.creditCardService.getBill(customerId, decodePath(cardMatch[1])));
  cardMatch = relative.match(/^\/credit-cards\/([^/]+)\/repayments$/);
  if (req.method === 'POST' && cardMatch) {
    requireUserAction(identity, '信用卡还款');
    return created(await services.creditCardService.repay(
      actor, decodePath(cardMatch[1]),
      await parseJsonBody(req, context.bodyLimit), requireIdempotencyKey(req),
    ));
  }
  cardMatch = relative.match(/^\/credit-cards\/([^/]+)$/);
  if (req.method === 'GET' && cardMatch) return ok(await services.creditCardService.getCard(customerId, decodePath(cardMatch[1])));

  cardMatch = relative.match(/^\/credit-cards\/([^/]+)\/activations$/);
  if (req.method === 'POST' && cardMatch) {
    requireUserAction(identity, '信用卡激活');
    requireEmptyObject(await parseJsonBody(req, context.bodyLimit));
    return created(await services.creditCardService.activate(actor, decodePath(cardMatch[1]), requireIdempotencyKey(req)));
  }
  cardMatch = relative.match(/^\/credit-cards\/([^/]+)\/loss-reports$/);
  if (req.method === 'POST' && cardMatch) {
    requireUserAction(identity, '信用卡挂失');
    return created(await services.creditCardService.reportLoss(actor, decodePath(cardMatch[1]), await parseJsonBody(req, context.bodyLimit), requireIdempotencyKey(req)));
  }
  cardMatch = relative.match(/^\/credit-cards\/([^/]+)\/limit-change-requests$/);
  if (req.method === 'POST' && cardMatch) {
    requireUserAction(identity, '信用卡额度调整');
    return created(await services.creditCardService.requestLimitChange(actor, decodePath(cardMatch[1]), await parseJsonBody(req, context.bodyLimit), requireIdempotencyKey(req)));
  }
  cardMatch = relative.match(/^\/credit-cards\/([^/]+)\/security-settings$/);
  if (req.method === 'PUT' && cardMatch) {
    requireUserAction(identity, '信用卡安全设置变更');
    return ok(await services.creditCardService.updateSecuritySettings(actor, decodePath(cardMatch[1]), await parseJsonBody(req, context.bodyLimit), requireIdempotencyKey(req)));
  }

  let match = relative.match(/^\/accounts\/([^/]+)$/);
  if (req.method === 'GET' && match) return ok(await services.bankService.getAccount(customerId, decodePath(match[1])));
  match = relative.match(/^\/accounts\/([^/]+)\/balance$/);
  if (req.method === 'GET' && match) return ok(await services.bankService.getBalance(customerId, decodePath(match[1])));
  match = relative.match(/^\/accounts\/([^/]+)\/transactions$/);
  if (req.method === 'GET' && match) {
    return ok(await services.bankService.listTransactions(customerId, decodePath(match[1]), {
      cursor: url.searchParams.get('cursor') || null,
      limit: url.searchParams.has('limit') ? url.searchParams.get('limit') : undefined,
    }));
  }

  if (req.method === 'POST' && relative === '/payee-validations') {
    const key = requireIdempotencyKey(req);
    const body = await parseJsonBody(req, context.bodyLimit);
    const data = await services.bankService.validatePayee(customerId, body.payeeId);
    return created(Object.assign({ idempotencyKey: key }, data));
  }
  if (req.method === 'POST' && relative === '/transfer-orders') {
    const data = await services.transferService.createOrder(actor, await parseJsonBody(req, context.bodyLimit), requireIdempotencyKey(req));
    return created(data);
  }

  match = relative.match(/^\/transfer-orders\/([^/]+)\/validations$/);
  if (req.method === 'POST' && match) {
    await parseJsonBody(req, context.bodyLimit);
    return created(await services.transferService.validateOrder(actor, decodePath(match[1]), requireIdempotencyKey(req)));
  }
  match = relative.match(/^\/transfer-orders\/([^/]+)\/confirmation-challenges$/);
  if (req.method === 'POST' && match) {
    await parseJsonBody(req, context.bodyLimit);
    return created(await services.transferService.createConfirmationChallenge(actor, decodePath(match[1]), requireIdempotencyKey(req)));
  }
  match = relative.match(/^\/transfer-orders\/([^/]+)\/cancellations$/);
  if (req.method === 'POST' && match) {
    await parseJsonBody(req, context.bodyLimit);
    return created(await services.transferService.cancelOrder(actor, decodePath(match[1]), requireIdempotencyKey(req)));
  }
  match = relative.match(/^\/confirmation-challenges\/([^/]+)\/invalidations$/);
  if (req.method === 'POST' && match) {
    requireInternalToken(req, context.internalExecutionToken);
    const body = await parseJsonBody(req, context.bodyLimit);
    const systemActor = { type: 'system', id: 'bank-internal-api', traceId: context.meta.traceId };
    return created(await services.transferService.invalidateConfirmation(systemActor, decodePath(match[1]), body.reason, requireIdempotencyKey(req)));
  }
  match = relative.match(/^\/transfer-orders\/([^/]+)\/authorizations$/);
  if (req.method === 'POST' && match) {
    if (identity.actorType !== 'user') throw new BankApiError(403, 'forbidden', '转账授权必须来自明确的用户确认操作');
    const body = await parseJsonBody(req, context.bodyLimit);
    const authorization = await services.transferService.authorize(actor, decodePath(match[1]), body.challengeId, body.confirmationToken, requireIdempotencyKey(req));
    services.executionEngine.scheduleAuthorization(Object.assign({}, authorization, { traceId: context.meta.traceId }));
    return created(authorization);
  }
  match = relative.match(/^\/transfer-orders\/([^/]+)\/executions$/);
  if (req.method === 'POST' && match) {
    requireInternalToken(req, context.internalExecutionToken);
    await parseJsonBody(req, context.bodyLimit);
    const systemActor = { type: 'system', id: 'bank-internal-api', traceId: context.meta.traceId };
    return created(await services.transferService.execute(systemActor, decodePath(match[1]), requireIdempotencyKey(req)));
  }
  match = relative.match(/^\/transfer-orders\/([^/]+)$/);
  if (req.method === 'GET' && match) return ok(await services.transferService.getOrder(customerId, decodePath(match[1])));
  match = relative.match(/^\/transfer-executions\/([^/]+)$/);
  if (req.method === 'GET' && match) return ok(await services.transferService.getExecution(customerId, decodePath(match[1])));

  /* ---- 推荐服务：每日 Agent 任务生成的千人千面推荐 ---- */
  if (req.method === 'GET' && relative === '/next-actions') {
    if (!services.nextActionService) {
      throw new BankApiError(503, 'next_action_service_unavailable', 'Next Action service is unavailable');
    }
    return ok(await services.nextActionService.list(customerId, exactNextActionQuery(url)));
  }

  // 首页智能助手轮播提示语（后端驱动）：推荐条目与首页卡同源（同一评估管道、同一
  // R1 闸门），另加天气/优惠信号的一句话投影。不产生第二套推荐决策；任何数据不可
  // 用时该条目省略，前端保留本地问候兜底。
  if (req.method === 'GET' && relative === '/assistant-tips') {
    if (!services.repository || !services.signalService) {
      throw new BankApiError(503, 'assistant_tips_unavailable', 'Assistant tips are unavailable');
    }
    // 轮播文案 = 已服务页面的投影（读持久化推荐行），不重新评估：与首页卡严格同源，
    // 且不会触发曝光轮换/行替换——否则页面加载时第二次评估会把用户面前的卡挤下线
    // （点击已失效 ID 即 409 recommendation_unavailable）。
    const [page, snapshot] = await Promise.all([
      services.repository.listNextActionRecommendations(customerId, { surface: 'home', channel: null, limit: 3 }),
      services.signalService.collect({ customerId, surface: 'home', channel: null, personalizationEnabled: true }),
    ]);
    const servedItems = (page && Array.isArray(page.items)) ? page.items : [];
    const tips = servedItems
      .filter(item => typeof item.reasonText === 'string' && item.reasonText)
      .map(item => ({ kind: 'recommendation', scenarioId: item.scenarioId, text: item.reasonText }));
    const read = id => (snapshot.signals[id] ? snapshot.signals[id].value : null);
    const condition = read('life.weather_condition');
    const tempHigh = read('life.weather_temp_high');
    if (typeof condition === 'string' && Number.isInteger(tempHigh)) {
      const weatherText = {
        rain: '今天有雨，出门记得带伞，注意路滑',
        snow: '今天有雪，出行注意保暖防滑',
        clear: `今天天气晴朗，最高 ${tempHigh}°C，适合安排外出`,
        cloudy: `今天多云，最高 ${tempHigh}°C，体感舒适`,
      }[condition];
      if (weatherText) tips.push({ kind: 'life', scenarioId: 'life-weather', text: weatherText });
    }
    const expiringOffers = read('promo.expiring_soon_count');
    if (Number.isInteger(expiringOffers) && expiringOffers > 0) {
      tips.push({
        kind: 'promo', scenarioId: 'promo-offers-expiring',
        text: `有 ${expiringOffers} 项优惠即将到期，可在领券中心查看使用`,
      });
    }
    return ok({ generatedAt: servedItems.length && servedItems[0].generatedAt || context.now(), tips: tips.slice(0, 5) });
  }
  const nextActionDecisionMatch = relative.match(/^\/next-actions\/([^/]+)\/decisions$/);
  if (req.method === 'POST' && nextActionDecisionMatch) {
    if (!services.nextActionService) {
      throw new BankApiError(503, 'next_action_service_unavailable', 'Next Action service is unavailable');
    }
    requireUserAction(identity, 'Recommendation decision');
    const idempotencyKey = requireIdempotencyKey(req);
    const body = exactNextActionDecisionBody(await parseJsonBody(req, context.bodyLimit), context.now());
    return ok(await services.nextActionService.decide({
      recommendationId: decodePath(nextActionDecisionMatch[1]),
      customerId,
      decision: body.decision,
      ...(body.snoozeUntil ? { snoozeUntil: body.snoozeUntil } : {}),
      ...(body.feedbackCode ? { feedbackCode: body.feedbackCode } : {}),
      idempotencyKey,
      requestHash: sha256(JSON.stringify(body)),
    }));
  }

  if (req.method === 'GET' && relative === '/recommendations') {
    if (!services.recommendationService) throw new BankApiError(503, 'recommendation_service_unavailable', '推荐服务暂不可用');
    return ok(await services.recommendationService.getDailyRecommendations(customerId));
  }

  if (req.method === 'POST' && relative === '/__mock/reset' && context.devMode) {
    const body = await parseJsonBody(req, context.bodyLimit);
    requireIdempotencyKey(req);
    return ok(await services.repository.resetScenario(body.scenarioId || 'default'));
  }

  // Dev-only：向前端 mock persona 注入体验事件（如 salary_credited），让推荐侧
  // edge/日历信号可在真实时钟下即时验证。内存生效，无需重启；事件 id 幂等。
  if (req.method === 'POST' && relative === '/__mock/experience-event' && context.devMode) {
    const body = await parseJsonBody(req, context.bodyLimit);
    requireIdempotencyKey(req);
    if (!body || typeof body !== 'object' || Array.isArray(body)
      || !/^[A-Za-z0-9_.:-]{1,160}$/.test(body.customerId || '')
      || !/^[A-Za-z0-9_.:-]{1,160}$/.test(body.id || '')
      || !['salary_credited', 'card_bill_due', 'utility_bill_due', 'loan_payment_due', 'spending_anomaly'].includes(body.type)
      || !Number.isFinite(Date.parse(body.occurredAt))
      || !body.payload || typeof body.payload !== 'object' || Array.isArray(body.payload)
      || Object.keys(body.payload).length > 16) {
      throw new BankApiError(400, 'mock_experience_event_invalid', 'Mock 体验事件参数无效');
    }
    const persona = (MOCK_PERSONAS.personas || []).find(item => item.customerId === body.customerId);
    if (!persona) throw new BankApiError(404, 'persona_not_found', 'Mock persona 不存在');
    const existing = persona.events.find(item => item.id === body.id);
    if (existing) {
      return ok({ injected: false, customerId: body.customerId, eventId: body.id, personaEventCount: persona.events.length });
    }
    persona.events.push({
      id: body.id, type: body.type, occurredAt: new Date(Date.parse(body.occurredAt)).toISOString(),
      payload: body.payload,
    });
    return created({ injected: true, customerId: body.customerId, eventId: body.id, personaEventCount: persona.events.length });
  }

  // Dev-only：导出当前 Next Action 信号快照（值 + 过期时间），便于验证场景触发条件。
  if (req.method === 'GET' && relative === '/__mock/next-action-signals' && context.devMode) {
    return ok(await services.signalService.collect({
      customerId, surface: 'home', channel: null, personalizationEnabled: true,
    }));
  }

  // Dev-only：mock 前端的个性化总开关（正式 enable_personalization 产品 UI 缺位前的
  // 验收工具）。enabled=false 走本地关闭；enabled=true 恢复本地开关并向记忆控制面
  // 提交反向候选（recommendation_opt_out=false），与决策链路共用同一 sink。
  if (req.method === 'POST' && relative === '/__mock/personalization' && context.devMode) {
    const body = await parseJsonBody(req, context.bodyLimit);
    const idempotencyKey = requireIdempotencyKey(req);
    if (!body || typeof body !== 'object' || Array.isArray(body) || typeof body.enabled !== 'boolean') {
      throw new BankApiError(400, 'mock_personalization_invalid', 'Mock 个性化开关参数无效');
    }
    await services.repository.setNextActionPersonalization(customerId, body.enabled);
    let memorySync = 'skipped';
    if (services.memoryPreferenceSink) {
      try {
        await services.memoryPreferenceSink({
          customerId, optOut: !body.enabled, sourceRef: 'mock-toggle:' + idempotencyKey,
        });
        memorySync = 'ok';
      } catch (error) {
        memorySync = 'degraded';
      }
    }
    return ok({
      ...(await services.repository.getNextActionPreference(customerId)),
      memorySync,
    });
  }

  throw new BankApiError(404, 'not_found', '接口不存在');
}

function projectAgentCapability(item) {
  return {
    id: item.id,
    title: item.title,
    description: item.description,
    exposure: item.exposure,
    sideEffect: item.sideEffect,
    riskLevel: item.riskLevel,
    inputSchema: item.inputSchema,
    outputSchema: item.outputSchema,
    possibleErrors: item.possibleErrors,
    readiness: item.readiness,
  };
}

function exactAgentSessionCloseBody(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new BankApiError(422, 'validation_error', 'Session 关闭请求必须是 JSON 对象');
  }
  const keys = Object.keys(value);
  if (keys.length !== 1 || keys[0] !== 'reason') {
    const unknown = keys.filter(key => key !== 'reason');
    throw new BankApiError(422, 'validation_error', 'Session 关闭请求字段无效',
      (unknown.length ? unknown : ['reason']).map(field => ({ field, code: unknown.length ? 'unknown_field' : 'required' })));
  }
  const reason = requireString(value.reason, 'reason', { min: 1, max: 64 });
  if (!/^[A-Za-z0-9_.:-]+$/.test(reason)) {
    throw new BankApiError(422, 'validation_error', 'Session 关闭原因格式无效', [
      { field: 'reason', code: 'invalid_format' },
    ]);
  }
  return { reason };
}

function exactAgentSessionBootstrapBody(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).some(key => key !== 'sessionId')) {
    throw new BankApiError(422, 'validation_error', 'Agent Session 启动请求格式无效');
  }
  if (value.sessionId == null) return { sessionId: null };
  const sessionId = requireString(value.sessionId, 'sessionId', { min: 1, max: 160 });
  if (!/^[A-Za-z0-9._:-]+$/.test(sessionId)) {
    throw new BankApiError(422, 'validation_error', 'Agent Session ID 格式无效', [
      { field: 'sessionId', code: 'invalid_format' },
    ]);
  }
  return { sessionId };
}

function exactRecommendationSessionBody(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).length !== 2
    || !Object.prototype.hasOwnProperty.call(value, 'recommendationId')
    || !Object.prototype.hasOwnProperty.call(value, 'mode')) {
    throw new BankApiError(422, 'validation_error', '推荐会话请求格式无效');
  }
  const recommendationId = requireString(
    value.recommendationId, 'recommendationId', { min: 1, max: 160 },
  );
  if (!/^[A-Za-z0-9._:-]+$/.test(recommendationId) || value.mode !== 'consult') {
    throw new BankApiError(422, 'validation_error', '推荐会话请求字段无效');
  }
  return { recommendationId, mode: 'consult' };
}

function exactAgentWorkflowBindingBody(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new BankApiError(422, 'validation_error', 'Workflow 绑定请求必须是 JSON 对象');
  }
  const allowed = new Set(['businessWorkflowId', 'expectedWorkflowRevision']);
  const keys = Object.keys(value);
  const unknown = keys.filter(key => !allowed.has(key));
  const missing = [...allowed].filter(key => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) {
    throw new BankApiError(422, 'validation_error', 'Workflow 绑定请求字段无效', [
      ...unknown.map(field => ({ field, code: 'unknown_field' })),
      ...missing.map(field => ({ field, code: 'required' })),
    ]);
  }
  const businessWorkflowId = requireString(value.businessWorkflowId, 'businessWorkflowId', {
    min: 1, max: 160,
  });
  if (!/^[A-Za-z0-9_.:-]+$/.test(businessWorkflowId)
    || !Number.isInteger(value.expectedWorkflowRevision)
    || value.expectedWorkflowRevision < 1) {
    throw new BankApiError(422, 'validation_error', 'Workflow 绑定请求格式无效');
  }
  return {
    businessWorkflowId,
    expectedWorkflowRevision: value.expectedWorkflowRevision,
  };
}

function normalizeAgentRunRequest(body) {
  const input = body && typeof body === 'object' && !Array.isArray(body) ? body : {};
  const allowed = new Set([
    'conversationId', 'message', 'pageContext', 'workflowControl',
    'recommendationId', 'serviceActionId',
  ]);
  const unknown = Object.keys(input).filter(key => !allowed.has(key));
  if (unknown.length) {
    throw new BankApiError(422, 'validation_error', '智能服务请求包含不允许的字段', unknown.map(field => ({ field, code: 'unknown_field' })));
  }

  let workflowControl = null;
  if (input.workflowControl != null) {
    if (!input.workflowControl || typeof input.workflowControl !== 'object'
      || Array.isArray(input.workflowControl)
      || Object.keys(input.workflowControl).some(key => key !== 'action')
      || !['SUSPEND', 'CANCEL', 'RESUME'].includes(input.workflowControl.action)) {
      throw new BankApiError(422, 'validation_error', 'workflowControl 格式不合法', [
        { field: 'workflowControl', code: 'invalid_control' },
      ]);
    }
    workflowControl = { action: input.workflowControl.action };
  }
  const conversationalMode = typeof input.message === 'string' || workflowControl !== null;
  const recommendationMode = typeof input.recommendationId === 'string';
  if (conversationalMode === recommendationMode) {
    throw new BankApiError(422, 'validation_error', 'message/workflowControl and recommendationId are mutually exclusive');
  }
  const recommendationId = recommendationMode
    ? requireString(input.recommendationId, 'recommendationId', { min: 1, max: 160 }) : null;
  if (recommendationId && !/^[A-Za-z0-9._:-]+$/.test(recommendationId)) {
    throw new BankApiError(422, 'validation_error', 'recommendationId format is invalid', [
      { field: 'recommendationId', code: 'invalid_format' },
    ]);
  }
  const serviceActionId = input.serviceActionId == null ? null
    : requireString(input.serviceActionId, 'serviceActionId', { min: 1, max: 160 });
  if (serviceActionId && (!recommendationId || !/^[A-Za-z0-9._:-]+$/.test(serviceActionId))) {
    throw new BankApiError(422, 'validation_error', 'serviceActionId format is invalid', [
      { field: 'serviceActionId', code: recommendationId ? 'invalid_format' : 'recommendation_required' },
    ]);
  }
  const message = recommendationMode ? null : input.message == null && workflowControl
    ? ({ SUSPEND: '暂停当前业务', CANCEL: '取消当前业务', RESUME: '继续当前业务' })[workflowControl.action]
    : requireString(input.message, 'message', { min: 1, max: 2000 });
  const conversationId = requireString(input.conversationId, 'conversationId', { min: 1, max: 96 });
  if (!/^[A-Za-z0-9._:-]+$/.test(conversationId)) {
    throw new BankApiError(422, 'validation_error', 'conversationId 格式不合法', [{ field: 'conversationId', code: 'invalid_format' }]);
  }

  let pageContext = {};
  if (input.pageContext != null) {
    if (!input.pageContext || typeof input.pageContext !== 'object' || Array.isArray(input.pageContext)) {
      throw new BankApiError(422, 'validation_error', 'pageContext 格式不合法', [{ field: 'pageContext', code: 'object_required' }]);
    }
    const pageAllowed = new Set(['tab', 'menuKey']);
    const pageUnknown = Object.keys(input.pageContext).filter(key => !pageAllowed.has(key));
    if (pageUnknown.length) {
      throw new BankApiError(422, 'validation_error', 'pageContext 包含不允许的字段', pageUnknown.map(field => ({ field: 'pageContext.' + field, code: 'unknown_field' })));
    }
    if (input.pageContext.tab != null) pageContext.tab = requireString(input.pageContext.tab, 'pageContext.tab', { max: 64 });
    if (input.pageContext.menuKey != null) pageContext.menuKey = requireString(input.pageContext.menuKey, 'pageContext.menuKey', { max: 128 });
  }
  return {
    conversationId, message, pageContext, workflowControl, recommendationId, serviceActionId,
  };
}

function exactAgentConfirmationBody(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new BankApiError(422, 'validation_error', '确认请求必须是 JSON 对象');
  }
  const allowed = new Set(['checkpointId', 'confirmationToken', 'confirmations']);
  const unknown = Object.keys(value).filter(key => !allowed.has(key));
  if (unknown.length) throw new BankApiError(422, 'validation_error', '确认请求包含不允许的字段', unknown.map(field => ({ field, code: 'unknown_field' })));
  if (value.confirmations != null) {
    if (!Array.isArray(value.confirmations) || value.confirmations.length < 2 || value.confirmations.length > 16) {
      throw new BankApiError(422, 'validation_error', '批次确认凭证格式不合法');
    }
    const confirmations = value.confirmations.map((item, index) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)
        || Object.keys(item).length !== 2
        || !Object.prototype.hasOwnProperty.call(item, 'checkpointId')
        || !Object.prototype.hasOwnProperty.call(item, 'confirmationToken')) {
        throw new BankApiError(422, 'validation_error', '批次确认凭证格式不合法');
      }
      return {
        checkpointId: requireString(item.checkpointId, `confirmations.${index}.checkpointId`, { min: 1, max: 128 }),
        confirmationToken: requireString(item.confirmationToken, `confirmations.${index}.confirmationToken`, { min: 16, max: 256 }),
      };
    });
    if (new Set(confirmations.map(item => item.checkpointId)).size !== confirmations.length) {
      throw new BankApiError(422, 'validation_error', '批次确认凭证重复');
    }
    return { confirmations };
  }
  return {
    checkpointId: requireString(value.checkpointId, 'checkpointId', { min: 1, max: 128 }),
    confirmationToken: requireString(value.confirmationToken, 'confirmationToken', { min: 16, max: 256 }),
  };
}

function exactBusinessReplyBody(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new BankApiError(422, 'validation_error', '业务回复请求必须是 JSON 对象');
  }
  if (Object.keys(value).length !== 1 || !Object.prototype.hasOwnProperty.call(value, 'message')) {
    throw new BankApiError(422, 'validation_error', '业务回复请求包含不允许的字段');
  }
  const message = requireString(value.message, 'message', { min: 1, max: 240 });
  if (/\d(?:[\s-]?\d){14,}/.test(message)) {
    throw new BankApiError(422, 'validation_error', '业务回复不能包含完整账号');
  }
  return { message };
}

function exactAgentClarificationBody(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new BankApiError(422, 'validation_error', '补录请求必须是 JSON 对象');
  }
  const allowed = new Set(['clarificationId', 'submissionToken', 'values']);
  const unknown = Object.keys(value).filter(key => !allowed.has(key));
  if (unknown.length || Object.keys(value).length !== allowed.size) {
    throw new BankApiError(422, 'validation_error', '补录请求包含不允许的字段');
  }
  const values = value.values;
  if (!values || typeof values !== 'object' || Array.isArray(values)) {
    throw new BankApiError(422, 'validation_error', '补录字段格式不合法');
  }
  const valueAllowed = new Set(['accountNo', 'bankName', 'purpose', 'savePayee', 'amount', 'payeeName', 'fromAccount']);
  const valueUnknown = Object.keys(values).filter(key => !valueAllowed.has(key));
  if (valueUnknown.length) {
    throw new BankApiError(422, 'validation_error', '补录字段包含不允许的内容');
  }
  const accountNo = requireString(values.accountNo, 'values.accountNo', { min: 4, max: 40 });
  const normalizedAccountNo = accountNo.replace(/[\s-]/g, '');
  // 掩码卡号（如 **** **** **** 7319）用于已保存的常用收款人，允许通过
  if (!normalizedAccountNo.includes('*')) {
    if (!/^\d{12,32}$/.test(normalizedAccountNo) || /^(\d)\1+$/.test(normalizedAccountNo)) {
      throw new BankApiError(422, 'validation_error', '收款账号格式不正确');
    }
  }
  const bankName = requireString(values.bankName, 'values.bankName', { min: 2, max: 40 });
  if (/[<>\u0000-\u001f\u007f]/.test(bankName)) {
    throw new BankApiError(422, 'validation_error', '收款银行格式不正确');
  }
  const savePayee = values.savePayee == null ? false : values.savePayee;
  if (typeof savePayee !== 'boolean') {
    throw new BankApiError(422, 'validation_error', '常用收款人选项格式不正确');
  }
  let purpose = '';
  if (values.purpose != null && values.purpose !== '') {
    purpose = requireString(values.purpose, 'values.purpose', { max: 60 });
  }
  let payeeName = '';
  if (values.payeeName != null && values.payeeName !== '') {
    payeeName = requireString(values.payeeName, 'values.payeeName', { min: 1, max: 40 });
    if (/[<>\u0000-\u001f\u007f]/.test(payeeName)) {
      throw new BankApiError(422, 'validation_error', '收款人格式不正确');
    }
  }
  let fromAccount = '';
  if (values.fromAccount != null && values.fromAccount !== '') {
    fromAccount = requireString(values.fromAccount, 'values.fromAccount', { min: 4, max: 40 });
    if (!/^\d{4}$|^\u5c3e\u53f7\d{4}$/.test(fromAccount)) {
      throw new BankApiError(422, 'validation_error', '转出账户格式不正确');
    }
    if (!fromAccount.startsWith('\u5c3e\u53f7')) {
      fromAccount = '\u5c3e\u53f7' + fromAccount;
    }
  }
  let amountMinor = 0;
  let amountText = '';
  if (values.amount != null && values.amount !== '') {
    amountText = requireString(values.amount, 'values.amount', { min: 1, max: 12 });
    if (!/^\d+(\.\d{1,2})?$/.test(amountText) || parseFloat(amountText) <= 0) {
      throw new BankApiError(422, 'validation_error', '转账金额格式不正确');
    }
    const cents = Math.round(parseFloat(amountText) * 100);
    if (!Number.isFinite(cents) || cents <= 0 || cents > 99999999999) {
      throw new BankApiError(422, 'validation_error', '转账金额超出允许范围');
    }
    amountMinor = cents;
  }
  return {
    clarificationId: requireString(value.clarificationId, 'clarificationId', { min: 5, max: 96 }),
    submissionToken: requireString(value.submissionToken, 'submissionToken', { min: 32, max: 256 }),
    values: {
      accountNo: normalizedAccountNo, bankName, purpose, savePayee,
      payeeName, amount: amountText, amountMinor, fromAccount,
    },
  };
}

function exactAgentWorkflowBody(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new BankApiError(422, 'validation_error', 'Agent Workflow 请求必须是 JSON 对象');
  }
  const allowed = new Set([
    'agentRunId', 'businessWorkflowId', 'status', 'delegatedTaskIds',
    'expectedRevision', 'envelopeRevision', 'workflowRevision', 'todoRevision',
    'todoSnapshot', 'todoDigest', 'activeGoalTaskId', 'activeTodoId',
    'executionPolicy', 'suspensionReason', 'lastOutcome', 'pendingCheckpointIds',
  ]);
  const unknown = Object.keys(value).filter(key => !allowed.has(key));
  if (unknown.length || Object.keys(value).length !== allowed.size) {
    throw new BankApiError(422, 'validation_error', 'Agent Workflow 请求包含不允许的字段');
  }
  return {
    agentRunId: requireString(value.agentRunId, 'agentRunId', { min: 1, max: 160 }),
    businessWorkflowId: requireString(value.businessWorkflowId, 'businessWorkflowId', { min: 1, max: 160 }),
    status: value.status,
    delegatedTaskIds: value.delegatedTaskIds,
    expectedRevision: value.expectedRevision,
    envelopeRevision: value.envelopeRevision,
    workflowRevision: value.workflowRevision,
    todoRevision: value.todoRevision,
    todoSnapshot: value.todoSnapshot,
    todoDigest: value.todoDigest,
    activeGoalTaskId: value.activeGoalTaskId,
    activeTodoId: value.activeTodoId,
    executionPolicy: value.executionPolicy,
    suspensionReason: value.suspensionReason,
    lastOutcome: value.lastOutcome,
    pendingCheckpointIds: value.pendingCheckpointIds,
  };
}

async function loadConversationContext(repository, conversationId, customerId, nowIso) {
  if (repository.getConversationState) {
    const record = await repository.getConversationState(conversationId, customerId);
    if (record) {
      const nowMs = Date.parse(nowIso);
      const updatedMs = Date.parse(record.updatedAt);
      const age = nowMs - updatedMs;
      const valid = Number.isFinite(nowMs) && Number.isFinite(updatedMs)
        && age >= -5 * 60 * 1000 && age <= CONVERSATION_SLOT_TTL_MS;
      if (!valid) {
        if (repository.clearConversationState) {
          await repository.clearConversationState(conversationId, customerId);
        }
      } else {
        return conversationStateToRuntimeContext(record.state, record.updatedAt);
      }
    }
  }
  if (!repository.listConversationSlots) return { pendingIntentId: null, slots: [] };
  const rows = await repository.listConversationSlots(conversationId, customerId);
  if (!rows.length) return { pendingIntentId: null, slots: [] };
  const nowMs = Date.parse(nowIso);
  const intents = new Set(rows.map(item => item.sourceIntentId));
  const valid = Number.isFinite(nowMs)
    && intents.size === 1
    && rows.every(item => {
      const updatedMs = Date.parse(item.updatedAt);
      const age = nowMs - updatedMs;
      return Number.isFinite(updatedMs) && age >= -5 * 60 * 1000 && age <= CONVERSATION_SLOT_TTL_MS;
    });
  if (!valid) {
    if (repository.clearConversationSlots) {
      await repository.clearConversationSlots(conversationId, customerId);
    }
    return { pendingIntentId: null, slots: [] };
  }
  return {
    pendingIntentId: rows[0].sourceIntentId,
    slots: rows.map(item => ({
      name: item.slotName,
      value: item.value,
      source: item.source || 'context',
      sourceIntentId: item.sourceIntentId,
      updatedAt: item.updatedAt,
    })),
  };
}

function conversationStateToRuntimeContext(state, updatedAt) {
  if (!state || state.schemaVersion !== '2.0' || !Array.isArray(state.goals)
    || !state.goals.length) {
    throw new BankApiError(409, 'agent_workflow_context_missing', '业务办理上下文已失效，请重新发起');
  }
  const timestamp = updatedAt || new Date().toISOString();
  const goalIntents = new Map(state.goals.map(goal => [goal.taskId, goal.intentId]));
  const sharedIntentId = state.goals[0].intentId;
  return {
    pendingEnvelopeId: state.pendingEnvelopeId,
    envelopeRevision: state.envelopeRevision || 1,
    intentShape: state.intentShape,
    goals: state.goals.map(goal => ({
      taskId: goal.taskId,
      intentId: goal.intentId,
      status: goal.status,
      slots: goal.slots.map(slot => ({
        name: slot.name, value: slot.value,
        source: slot.source || 'context',
        sourceIntentId: goalIntents.get(goal.taskId), updatedAt: timestamp,
      })),
    })),
    sharedSlots: state.sharedSlots.map(slot => ({
      name: slot.name, value: slot.value,
      source: slot.source || 'context',
      sourceIntentId: sharedIntentId, updatedAt: timestamp,
    })),
    ambiguities: state.ambiguities,
  };
}

function truncatePublicWorkflowText(value, maximum) {
  return Array.from(String(value || '')).slice(0, maximum).join('');
}

function formatPublicWorkflowUpdatedAt(value) {
  const match = typeof value === 'string'
    ? value.match(/^\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})/u)
    : null;
  return match ? `${match[1]}-${match[2]} ${match[3]}:${match[4]}` : '更新时间未知';
}

function createPublicResumableWorkflowItem(choice) {
  const riskLabels = { L0: '低风险', L1: '较低风险', L2: '中风险', L3: '较高风险' };
  const summary = choice.summary;
  const progress = `${summary.completedTodoCount}/${summary.totalTodoCount}`;
  const updated = formatPublicWorkflowUpdatedAt(summary.updatedAt);
  const candidate = `${truncatePublicWorkflowText(summary.businessTitle, 64)} · 进度 ${progress} · ${riskLabels[summary.riskLevel] || '风险待核对'} · ${truncatePublicWorkflowText(summary.maskedObject, 64)} · 更新于 ${updated}`;
  if (candidate.length <= 240 && isSafeAgentSessionText(candidate)) return candidate;
  return `未完成业务 · 进度 ${progress} · 风险待核对 · 业务对象 **** · 更新于 ${updated}`;
}

function createPublicResumableWorkflowAction(choice, index) {
  const selectionAnchor = Array.isArray(choice.selectionAnchors)
    ? choice.selectionAnchors.find(value => /^选择码[a-f0-9]{10}$/u.test(value))
    : null;
  if (!selectionAnchor) {
    throw new BankApiError(
      500, 'agent_workflow_selection_projection_failed', '可恢复业务选择暂不可用',
    );
  }
  const progress = `${choice.summary.completedTodoCount}/${choice.summary.totalTodoCount}`;
  const candidateLabel = `${index + 1}. ${truncatePublicWorkflowText(choice.summary.businessTitle, 48)} · ${progress}`;
  const label = isSafeAgentSessionText(candidateLabel)
    ? candidateLabel : `${index + 1}. 未完成业务 · ${progress}`;
  return {
    id: `resume-saved-work-${index + 1}`,
    label,
    description: `已完成 ${progress}，选择后系统将重新核对`,
    kind: 'reply',
    payload: { message: `继续第${index + 1}项（${selectionAnchor}）` },
  };
}

function createResumableWorkflowClarification(input, choices, reason) {
  const delivery = {
    agentRunId: input.agentRunId,
    status: 'clarification_required',
    userResult: null,
    userResponse: {
      schemaVersion: '1.0', type: 'clarification',
      title: '请选择要继续办理的业务',
      body: '找到多项未完成业务，请根据业务名称、进度和更新时间选择；系统会再次核对选择是否仍然有效',
      items: choices.map(createPublicResumableWorkflowItem),
      actions: choices.map(createPublicResumableWorkflowAction),
      source: null, taskProgress: null,
    },
    confirmation: null,
    clarificationForm: null,
  };
  return {
    delivery,
    traceEvents: createConversationControlTrace(
      input, { action: 'RESUME', reason }, delivery, false, null,
    ),
  };
}

function createExpiredWorkflowSelectionResult(input) {
  const delivery = {
    agentRunId: input.agentRunId,
    status: 'unsupported',
    userResult: null,
    userResponse: {
      schemaVersion: '1.0', type: 'notice',
      title: '未找到可恢复业务',
      body: '之前选择的业务已发生变化，请重新发起需要办理的业务',
      items: [], actions: [], source: null, taskProgress: null,
    },
    confirmation: null,
    clarificationForm: null,
  };
  return {
    delivery,
    traceEvents: createConversationControlTrace(
      input, { action: 'RESUME', reason: 'resume_selection_expired' }, delivery, false, null,
    ),
  };
}

async function handleConversationControlRun(input) {
  const repository = input.repository;
  if (!repository || typeof repository.listAgentWorkflows !== 'function'
    || typeof repository.transitionAgentWorkflowControl !== 'function') return null;
  const workflows = await repository.listAgentWorkflows(input.conversationId, input.customerId);
  const active = workflows.find(item => [
    'running', 'awaiting_clarification', 'awaiting_confirmation', 'waiting_external',
  ].includes(item.status));
  const currentSuspended = workflows.filter(
    item => item.status === 'suspended' && item.conversationState,
  );
  const lifecycle = !active && currentSuspended.length ? 'suspended' : 'active';
  let control = classifyConversationControl(input.message, {
    lifecycle,
    resumeAnchors: currentSuspended.length === 1
      ? deriveWorkflowResumeAnchors(currentSuspended[0]) : [],
  }, input.workflowControl);
  let workflow = active || (currentSuspended.length === 1 ? currentSuspended[0] : null);
  let requiresBinding = false;

  if (!active && (currentSuspended.length
    || typeof repository.listResumableAgentWorkflowRecordsInternal === 'function')) {
    const records = currentSuspended.length
      ? currentSuspended
      : await repository.listResumableAgentWorkflowRecordsInternal(input.customerId);
    const choices = createResumableWorkflowChoices(records, input.intentGraph);
    const genericResume = classifyConversationControl(
      input.message, { lifecycle: 'suspended', resumeAnchors: [] }, input.workflowControl,
    ).action === 'RESUME';
    const matching = genericResume && choices.length === 1
      ? choices
      : genericResume
        ? []
        : choices.filter(choice => classifyConversationControl(input.message, {
          lifecycle: 'suspended',
          resumeAnchors: [
            ...deriveWorkflowResumeAnchors(choice.workflow),
            ...choice.selectionAnchors,
          ],
        }).action === 'RESUME');
    const staleSelection = matching.length === 0
      && hasResumableWorkflowSelectionCode(input.message);
    const ambiguous = matching.length > 1
      ? matching
      : (genericResume && choices.length > 1) || staleSelection
        ? choices : [];
    if (ambiguous.length) {
      return createResumableWorkflowClarification(
        input, ambiguous, 'multiple_resume_candidates',
      );
    }
    if (matching.length === 1) {
      workflow = matching[0].workflow;
      requiresBinding = workflow.boundConversationId !== input.conversationId;
      control = classifyConversationControl(input.message, {
        lifecycle: 'suspended',
        resumeAnchors: [
          ...deriveWorkflowResumeAnchors(workflow),
          ...matching[0].selectionAnchors,
        ],
      }, input.workflowControl);
    }
  }
  if (control.action === 'CONTINUE') return null;
  if (control.action === 'RESUME' && (!workflow || workflow.status !== 'suspended')) return null;
  if (!workflow) return null;

  if (control.action === 'RESUME') {
    if (requiresBinding) {
      try {
        workflow = await repository.bindAgentWorkflowToSession({
          id: workflow.id, customerId: input.customerId,
          conversationId: input.conversationId, expectedRevision: workflow.workflowRevision,
        });
      } catch (error) {
        if (!error || error.code !== 'agent_workflow_revision_conflict') throw error;
        const refreshedRecords = typeof repository.listResumableAgentWorkflowRecordsInternal === 'function'
          ? await repository.listResumableAgentWorkflowRecordsInternal(input.customerId)
          : [];
        const refreshedChoices = createResumableWorkflowChoices(
          refreshedRecords, input.intentGraph,
        );
        if (!refreshedChoices.length) return createExpiredWorkflowSelectionResult(input);
        return createResumableWorkflowClarification(
          input, refreshedChoices, 'resume_selection_revision_conflict',
        );
      }
    }
    const conversationContext = conversationStateToRuntimeContext(
      workflow.conversationState, workflow.updatedAt || input.nowIso,
    );
    const resumed = await input.runtimeClient.resumeBusinessWorkflow(workflow.id, {
      resumeKind: 'clarification', clarificationMessage: '继续办理',
      agentRunId: input.agentRunId, conversationId: input.conversationId,
      customerId: input.customerId, taskId: workflow.activeGoalTaskId,
      grants: [], expectedWorkflowRevision: workflow.workflowRevision,
      conversationContext,
      executableCapabilityIds: input.runtimeInput.executableCapabilities.map(item => item.id),
    });
    const delivery = {
      agentRunId: input.agentRunId,
      status: resumed.status,
      userResult: null,
      userResponse: resumed.userResponse || null,
      confirmation: resumed.confirmation || null,
      clarificationForm: resumed.clarificationForm || null,
    };
    const retainedState = structuredClone(workflow.conversationState);
    retainedState.status = 'retain';
    await repository.replaceConversationState({
      conversationId: input.conversationId, customerId: input.customerId,
      state: retainedState,
    });
    await persistAgentClarification(
      repository, delivery, input.agentRunId, input.conversationId, input.customerId,
      {
        businessWorkflowId: workflow.id,
        taskId: resumed.activeGoalTaskId || workflow.activeGoalTaskId,
        expectedWorkflowRevision: resumed.workflowRevision || workflow.workflowRevision,
      },
    );
    return {
      delivery,
      traceEvents: createConversationControlTrace(input, control, delivery, true, {
        ...workflow,
        todoRevision: resumed.todoRevision || workflow.todoRevision,
      }, requiresBinding),
    };
  }

  const stateRecord = await repository.getConversationState(
    input.conversationId, input.customerId,
  );
  const resumableState = stateRecord && stateRecord.state
    ? stateRecord.state : workflow.conversationState;
  if (['SUSPEND', 'SWITCH'].includes(control.action) && !resumableState) return null;
  const transitionAction = control.action === 'SWITCH' ? 'SUSPEND' : control.action;
  const transitioned = await repository.transitionAgentWorkflowControl({
    id: workflow.id, conversationId: input.conversationId, customerId: input.customerId,
    action: transitionAction, expectedRevision: workflow.workflowRevision,
    conversationState: transitionAction === 'SUSPEND' ? resumableState : null,
  });
  if (repository.clearConversationState) {
    await repository.clearConversationState(input.conversationId, input.customerId);
  }
  if (repository.clearConversationSlots) {
    await repository.clearConversationSlots(input.conversationId, input.customerId);
  }
  if (control.action === 'SWITCH') {
    input.runtimeInput.conversationContext = { pendingIntentId: null, slots: [] };
    return null;
  }
  const delivery = createConversationControlDelivery(input.agentRunId, control, transitioned);
  return {
    delivery,
    traceEvents: createConversationControlTrace(input, control, delivery, false, transitioned),
    terminalWorkflow: TERMINAL_AGENT_WORKFLOW_STATUSES.has(transitioned.status)
      ? { id: transitioned.id, status: transitioned.status }
      : null,
  };
}

function createConversationControlDelivery(agentRunId, control, workflow) {
  const completed = workflow.todoSnapshot.filter(item => item.status === 'completed').length;
  const total = workflow.todoSnapshot.length;
  const suspended = control.action === 'SUSPEND';
  return {
    agentRunId,
    status: 'succeeded',
    userResult: null,
    userResponse: {
      schemaVersion: '1.0', type: 'result',
      title: suspended ? '已暂时退出当前业务' : '当前业务已取消',
      body: suspended
        ? `当前进度已安全保留，已完成 ${completed}/${total} 项，可随时继续办理。`
        : completed
          ? `已完成的 ${completed} 项不受影响，其他未执行事项已取消。`
          : '当前业务尚未执行，所有待办事项均已取消。',
      items: [],
      actions: suspended ? [
        {
          id: 'resume-current-workflow', label: '继续办理', description: null,
          kind: 'reply', payload: { message: '继续刚才的业务' },
        },
        {
          id: 'cancel-current-workflow', label: '取消当前业务', description: null,
          kind: 'reply', payload: { message: '取消当前业务' },
        },
      ] : [],
      source: null, taskProgress: null,
    },
    confirmation: null,
    clarificationForm: null,
  };
}

function createConversationControlTrace(input, control, delivery, resumed, workflow, rebound) {
  const delegationId = 'dlg_' + crypto.randomUUID();
  const definitions = [
    ['request.accepted', 'accepted', '请求已接入', ['message', 'pageContext'], ['agentRunId', 'traceId'], { messageLength: input.message.length }],
    ['user_agent.started', 'running', '用户智能体开始识别', ['message', 'pageContext'], [], { capabilityCount: 0 }],
    ['user_agent.completed', 'completed', '用户智能体识别会话控制', [], ['intentShape', 'taskCount'], {
      intentShape: 'non_task', taskCount: 0, candidateConstraint: 'matched',
      intentSummary: control.action === 'SUSPEND' ? '暂停当前业务'
        : control.action === 'RESUME' ? '恢复当前业务' : '取消当前业务',
    }],
    ['delegation.created', 'completed', '会话控制已交由平台处理', [], ['delegationId'], { taskCount: 0, dependencyCount: 0 }],
  ];
  if (rebound) {
    const todos = workflow && Array.isArray(workflow.todoSnapshot) ? workflow.todoSnapshot : [];
    definitions.push(['workflow.rebound', 'completed', '业务已安全绑定到当前会话', [], [], {
      workflowRevision: workflow.workflowRevision,
      completedTaskCount: todos.filter(todo => todo && todo.status === 'completed').length,
      taskCount: todos.length,
    }]);
  }
  if (resumed) {
    definitions.push(['business_agent.started', 'running', '业务智能体从断点恢复', [], [], {
      capabilityCount: 0, loopIteration: 1,
      todoRevision: workflow && workflow.todoRevision ? workflow.todoRevision : 1,
      delegatedTaskCount: workflow && Array.isArray(workflow.todoSnapshot)
        ? workflow.todoSnapshot.length : 0,
    }]);
  }
  definitions.push(
    ['business_agent.completed', 'completed', resumed ? '业务智能体已恢复当前待办' : '业务智能体本次无需启动', [], ['planStatus', 'stepCount'], {
      planStatus: resumed ? 'resumed' : 'not_applicable', stepCount: 0,
    }],
    ['plan.validated', 'completed', '会话状态迁移已校验', [], ['validated'], { validated: true, confirmationCount: 0 }],
  );
  const result = {
    id: input.agentRunId, conversationId: input.conversationId, delegationId,
    status: delivery.status, intentShape: 'non_task', tasks: [], steps: [],
    predictedNextIntents: [], requiresConfirmation: delivery.status === 'awaiting_confirmation',
  };
  definitions.push(['run.completed', 'completed', '会话控制处理完成', [], ['status'], {
    status: delivery.status, taskCount: 0, stepCount: 0,
  }, result]);
  const traceState = createTraceState({ agentRunId: input.agentRunId, traceId: input.traceId });
  return definitions.map((item, index) => validateTraceEvent({
    schemaVersion: '1.0', sequence: index + 1, timestamp: input.nowIso,
    stage: item[0], status: item[1], agentRunId: input.agentRunId, traceId: input.traceId,
    summary: {
      title: item[2], input: item[3], output: item[4], metrics: item[5],
      ...(item[6] ? { result: item[6] } : {}),
    },
  }, traceState));
}

function streamControlledAgentRun(res, controlled, conversationId) {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no', Connection: 'keep-alive',
    'X-Agent-Session-Id': conversationId,
    'Access-Control-Expose-Headers': 'X-Agent-Session-Id',
  });
  res.flushHeaders();
  if (res.socket && typeof res.socket.setNoDelay === 'function') {
    res.socket.setNoDelay(true);
  }
  controlled.traceEvents.forEach(event => {
    res.write('event: agent.trace\ndata: ' + JSON.stringify(event) + '\n\n');
  });
  res.write('event: agent.result\ndata: ' + JSON.stringify(controlled.delivery) + '\n\n');
  res.end();
  return STREAM_HANDLED;
}

const OBLIGATION_CAPABILITY_MAP = Object.freeze({
  credit_card: 'bank.credit-card.repayment.create',
  loan: 'bank.loan.repayment.create',
  utility: 'bank.utility.payment.create',
});

async function handleGuidedGoalFallback(input, services, context) {
  const entryGoal = input.entryGoal;
  if (!entryGoal || entryGoal.targetIntentId !== 'ccb.obligation.pay-selected') {
    return null;
  }
  const trusted = entryGoal.trustedInputs || {};
  if (!trusted.obligationId || !trusted.obligationType) {
    return null;
  }
  const capabilityId = OBLIGATION_CAPABILITY_MAP[trusted.obligationType];
  if (!capabilityId) {
    return null;
  }

  const obligations = await services.obligationService.list(input.customerId);
  const selected = obligations.items.find(item =>
    item.id === trusted.obligationId
    && item.type === trusted.obligationType
    && item.executionStatus === 'available',
  );
  if (!selected) {
    return null;
  }

  const sourceRef = selected.sourceRefs && selected.sourceRefs[0];
  if (!sourceRef) return null;
  const accountId = obligations.accountId;
  const amountMinor = selected.amount.amountMinor;

  let capabilityInput;
  if (trusted.obligationType === 'credit_card') {
    capabilityInput = { cardId: sourceRef, fromAccountId: accountId, amountMinor };
  } else if (trusted.obligationType === 'loan') {
    capabilityInput = { repaymentId: sourceRef, fromAccountId: accountId, amountMinor };
  } else {
    capabilityInput = { billId: sourceRef, accountId };
  }

  const capabilityService = services.agentCapabilityService || createAgentCapabilityService(services);
  const checkpoint = await capabilityService.createCheckpoint({
    agentRunId: input.agentRunId,
    capabilityId,
    input: capabilityInput,
    summary: {
      title: selected.displayName,
      description: '确认办理 ' + selected.displayName + '，扣款 ' + (amountMinor / 100).toFixed(2) + ' 元',
      riskLevel: 'L2',
      items: [selected.displayName + ' · ¥' + (amountMinor / 100).toFixed(2)],
      taskCount: 1,
      totalAmountMinor: amountMinor,
    },
  });

  const delivery = {
    agentRunId: input.agentRunId,
    status: 'awaiting_confirmation',
    userResult: null,
    userResponse: {
      schemaVersion: '1.0',
      type: 'confirmation',
      title: selected.displayName,
      body: '即将从付款账户扣款 ' + (amountMinor / 100).toFixed(2) + ' 元，请本人确认后办理。',
      items: [],
      actions: [],
      source: null,
      taskProgress: null,
    },
    confirmation: {
      checkpointId: checkpoint.checkpointId,
      capabilityId: checkpoint.capabilityId,
      confirmationToken: checkpoint.confirmationToken,
      expiresAt: checkpoint.expiresAt,
      summary: checkpoint.summary,
      confirmations: [],
    },
  };

  const traceEvents = createGuidedGoalTraceEvents(input, delivery, {
    capabilityId, selected, amountMinor, accountId, capabilityInput,
  });

  return { traceEvents, delivery };
}

function createGuidedGoalTraceEvents(input, delivery, ctx) {
  const delegationId = 'dlg_' + crypto.randomUUID();
  const taskId = 'task_guided_' + crypto.randomBytes(10).toString('hex');
  const stepId = 'step_' + crypto.randomUUID();
  const nowIso = new Date().toISOString();
  const intentId = 'ccb.obligation.pay-selected';
  const amountYuan = (ctx.amountMinor / 100).toFixed(2);

  const task = {
    id: taskId,
    intentId,
    action: 'DO',
    businessObject: 'obligation',
    readiness: 'ready',
    risk: 'L2',
    dependencies: [],
    slotNames: ['obligationId', 'obligationType'],
  };
  const step = {
    id: stepId,
    taskId,
    capabilityId: ctx.capabilityId,
    inputKeys: Object.keys(ctx.capabilityInput),
    requiresConfirmation: true,
    dependencies: [],
  };
  const result = {
    id: input.agentRunId,
    conversationId: input.conversationId,
    delegationId,
    status: 'awaiting_confirmation',
    intentShape: 'single_intent',
    tasks: [task],
    steps: [step],
    predictedNextIntents: [],
    requiresConfirmation: true,
  };

  const definitions = [
    ['request.accepted', 'accepted', '请求已接入', ['message', 'pageContext'], ['agentRunId', 'traceId'], { messageLength: input.message.length }],
    ['user_agent.started', 'running', '用户智能体开始识别', ['message', 'pageContext'], [], { capabilityCount: 0 }],
    ['user_agent.completed', 'completed', '用户智能体已识别办理意图', [], ['intentShape', 'taskCount'], {
      intentShape: 'single_intent', taskCount: 1, candidateConstraint: 'matched',
      intentSummary: '办理选定的待还事项',
    }],
    ['delegation.created', 'completed', '业务目标已委派', [], ['delegationId', 'tasks'], { taskCount: 1, dependencyCount: 0 }],
    ['business_agent.started', 'running', '业务智能体开始规划', [], ['capabilities'], {
      capabilityCount: 1, loopIteration: 1, todoRevision: 1, delegatedTaskCount: 1,
    }],
    ['business_agent.completed', 'completed', '业务智能体已完成能力规划', [], ['planStatus', 'stepCount'], {
      planStatus: 'ready', stepCount: 1, slotResolution: 'resolved', businessRepairCount: 0,
      loopIteration: 1, todoRevision: 1, currentTodoStatus: 'ready', delegatedTaskCount: 1, coveredTaskCount: 1,
    }],
    ['plan.validated', 'completed', '业务计划已校验', [], ['validated', 'confirmationCount'], { validated: true, confirmationCount: 1 }],
    ['confirmation.checkpoint_created', 'completed', '确认检查点已创建', [], ['checkpointStatus'], {
      capabilityId: ctx.capabilityId, riskLevel: 'L2', checkpointStatus: 'created', grantCount: 0,
    }],
    ['run.awaiting_confirmation', 'awaiting_confirmation', '待本人确认后办理', [], ['status'], {
      status: 'awaiting_confirmation', taskCount: 1, stepCount: 1, checkpointStatus: 'created', grantCount: 0,
    }, result],
  ];

  const traceState = createTraceState({ agentRunId: input.agentRunId, traceId: input.traceId });
  return definitions.map((item, index) => validateTraceEvent({
    schemaVersion: '1.0',
    sequence: index + 1,
    timestamp: nowIso,
    stage: item[0],
    status: item[1],
    agentRunId: input.agentRunId,
    traceId: input.traceId,
    summary: {
      title: item[2],
      input: item[3],
      output: item[4],
      metrics: item[5],
      ...(item[6] ? { result: item[6] } : {}),
    },
  }, traceState));
}

async function applyBusinessReplySelection(
  services, run, conversationContext, message, nowIso, activeGoalTaskId,
) {
  if (!conversationContext || !Array.isArray(conversationContext.goals)
    || !Array.isArray(conversationContext.sharedSlots)) return conversationContext;
  const normalized = String(message || '').normalize('NFKC').trim();
  const activeGoal = conversationContext.goals.find(goal => (
    goal && goal.taskId === activeGoalTaskId
  ));
  if (!activeGoal) return conversationContext;
  const repaymentAmountReply = /全额|最低|(?:还款|还|金额)\s*[¥￥]?\s*\d/u.test(normalized);
  const repaymentReferenceReply = normalized.match(
    /^选择尾号(\d{4})的(账户|信用卡)$/u,
  );
  if (activeGoal.intentId === 'ccb.home.repayment.repay'
    && Array.isArray(activeGoal.slots) && repaymentReferenceReply) {
    const tail = repaymentReferenceReply[1];
    const referenceType = repaymentReferenceReply[2];
    const candidates = referenceType === '信用卡'
      ? await services.creditCardService.listCards(run.customerId)
      : await services.bankService.listAccounts(run.customerId);
    const matched = candidates.filter(item => (
      item && item.status === 'ACTIVE'
      && String(item.maskedNo || '').replace(/\D/g, '').endsWith(tail)
    ));
    if (matched.length !== 1) {
      throw new BankApiError(
        422, 'business_reply_selection_invalid',
        referenceType === '信用卡'
          ? '所选信用卡当前不可用，请重新选择'
          : '所选付款账户当前不可用，请重新选择',
      );
    }
    const selected = matched[0];
    const slotName = referenceType === '信用卡' ? 'card' : 'fromAccount';
    const label = referenceType === '信用卡' ? selected.productName : selected.name;
    if (typeof label !== 'string' || !label.trim()) {
      throw new BankApiError(
        422, 'business_reply_selection_invalid', '所选业务对象当前不可用，请重新选择',
      );
    }
    const nextSlot = {
      name: slotName, value: `${label.trim()} · 尾号${tail}`,
      source: 'user_selection', sourceIntentId: activeGoal.intentId,
      updatedAt: nowIso,
    };
    const updatedGoal = Object.assign({}, activeGoal, {
      slots: activeGoal.slots.filter(item => item.name !== slotName).concat(nextSlot),
    });
    const updatedContext = Object.assign({}, conversationContext, {
      goals: conversationContext.goals.map(goal => (
        goal.taskId === updatedGoal.taskId ? updatedGoal : goal
      )),
    });
    if (services.repository.getConversationState && services.repository.replaceConversationState) {
      const record = await services.repository.getConversationState(
        run.conversationId, run.customerId,
      );
      if (record && record.state && record.state.schemaVersion === '2.0') {
        const state = structuredClone(record.state);
        state.goals = state.goals.map(goal => goal.taskId === updatedGoal.taskId
          ? Object.assign({}, goal, {
            slots: (goal.slots || []).filter(item => item.name !== slotName).concat({
              name: slotName, value: nextSlot.value, source: 'user_selection',
            }),
          }) : goal);
        await services.repository.replaceConversationState({
          conversationId: run.conversationId,
          customerId: run.customerId,
          state,
        });
      }
    }
    return updatedContext;
  }
  if (activeGoal.intentId === 'ccb.home.repayment.repay'
    && Array.isArray(activeGoal.slots) && repaymentAmountReply) {
    const creditGoal = activeGoal;
    const slotValue = name => {
      const slot = creditGoal.slots.find(item => item && item.name === name);
      return slot && slot.value;
    };
    const cardReference = slotValue('card');
    const accountReference = slotValue('fromAccount');
    if (typeof cardReference !== 'string' || typeof accountReference !== 'string') {
      throw new BankApiError(409, 'agent_workflow_context_missing', '信用卡还款上下文已失效，请重新发起');
    }
    const parseMaskedReference = value => {
      const match = String(value).normalize('NFKC').trim()
        .match(/^(.{1,40}?)\s*·?\s*尾号\s*(\d{4})$/u);
      return match ? { label: match[1].trim(), tail: match[2] } : null;
    };
    const cardSelection = parseMaskedReference(cardReference);
    const accountSelection = parseMaskedReference(accountReference);
    if (!cardSelection || !accountSelection) {
      throw new BankApiError(409, 'agent_workflow_context_missing', '信用卡还款上下文已失效，请重新发起');
    }
    const [cards, accounts] = await Promise.all([
      services.creditCardService.listCards(run.customerId),
      services.bankService.listAccounts(run.customerId),
    ]);
    const selectedCards = cards.filter(card => (
      card.status === 'ACTIVE'
      && card.productName === cardSelection.label
      && String(card.maskedNo || '').replace(/\D/g, '').endsWith(cardSelection.tail)
    ));
    const selectedAccounts = accounts.filter(account => (
      account.status === 'ACTIVE'
      && account.name === accountSelection.label
      && String(account.maskedNo || '').replace(/\D/g, '').endsWith(accountSelection.tail)
    ));
    if (selectedCards.length !== 1 || selectedAccounts.length !== 1) {
      throw new BankApiError(409, 'agent_workflow_context_missing', '信用卡还款上下文已失效，请重新发起');
    }
    const cardId = selectedCards[0].id;
    const fromAccountId = selectedAccounts[0].id;
    const [bill, account, balance] = await Promise.all([
      services.creditCardService.getBill(run.customerId, cardId),
      services.bankService.getAccount(run.customerId, fromAccountId),
      services.bankService.getBalance(run.customerId, fromAccountId),
    ]);
    let amountMinor = null;
    if (/全额/u.test(normalized)) amountMinor = bill.outstanding.amountMinor;
    else if (/最低/u.test(normalized)) amountMinor = bill.minimumPayment.amountMinor;
    else {
      const amountMatch = normalized.match(/(?:还款|还|金额)?\s*[¥￥]?\s*(\d{1,9}(?:\.\d{1,2})?)\s*(?:元|块)?/u);
      if (amountMatch && /还|金额/u.test(normalized)) {
        amountMinor = Math.round(Number(amountMatch[1]) * 100);
      }
    }
    if (amountMinor !== null) {
      if (!Number.isSafeInteger(amountMinor) || amountMinor <= 0
        || amountMinor > bill.outstanding.amountMinor) {
        throw new BankApiError(422, 'credit_card_repayment_amount_invalid', '还款金额需大于0且不能超过本期未还金额');
      }
      if (!balance.available || balance.available.amountMinor < amountMinor) {
        throw new BankApiError(422, 'insufficient_funds', '付款账户可用余额不足，请更换账户或调整金额');
      }
      const nextSlot = {
        name: 'amount', value: (amountMinor / 100).toFixed(2),
        source: 'user_selection', sourceIntentId: creditGoal.intentId,
        updatedAt: nowIso,
      };
      const updatedGoal = Object.assign({}, creditGoal, {
        slots: creditGoal.slots.filter(item => item.name !== 'amount').concat(nextSlot),
      });
      const updatedContext = Object.assign({}, conversationContext, {
        goals: conversationContext.goals.map(goal => (
          goal.taskId === updatedGoal.taskId ? updatedGoal : goal
        )),
      });
      if (services.repository.getConversationState && services.repository.replaceConversationState) {
        const record = await services.repository.getConversationState(
          run.conversationId, run.customerId,
        );
        if (record && record.state && record.state.schemaVersion === '2.0') {
          const state = structuredClone(record.state);
          state.goals = state.goals.map(goal => goal.taskId === updatedGoal.taskId
            ? Object.assign({}, goal, {
              slots: (goal.slots || []).filter(item => item.name !== 'amount').concat({
                name: 'amount', value: (amountMinor / 100).toFixed(2), source: 'user_selection',
              }),
            }) : goal);
          await services.repository.replaceConversationState({
            conversationId: run.conversationId,
            customerId: run.customerId,
            state,
          });
        }
      }
      return updatedContext;
    }
    // “输入其他金额”仅请求下一轮自然语言补充，不改变任何执行参数。
    return conversationContext;
  }
  if (activeGoal.intentId !== 'ccb.transfer.create') return conversationContext;
  const match = normalized.match(/^使用(.{1,20})尾号(\d{4})转账$/u);
  if (!match) return conversationContext;
  const accountName = match[1].trim();
  const lastFour = match[2];
  const accounts = await services.bankService.listAccounts(run.customerId);
  const matched = accounts.filter(account => {
    const digits = String(account.maskedNo || '').replace(/\D/g, '');
    return account.name === accountName && digits.endsWith(lastFour);
  });
  if (matched.length !== 1) {
    throw new BankApiError(422, 'business_reply_selection_invalid', '所选转出账户当前不可用，请重新选择');
  }
  const reference = accountName + '尾号' + lastFour;
  const intentId = activeGoal.intentId;
  const updatedContext = Object.assign({}, conversationContext, {
    sharedSlots: conversationContext.sharedSlots
      .filter(item => item.name !== 'fromAccount')
      .concat({
        name: 'fromAccount', value: reference, source: 'user_selection',
        sourceIntentId: intentId, updatedAt: nowIso,
      }),
  });
  if (services.repository.getConversationState && services.repository.replaceConversationState) {
    const record = await services.repository.getConversationState(
      run.conversationId, run.customerId,
    );
    if (record && record.state && record.state.schemaVersion === '2.0') {
      const state = structuredClone(record.state);
      state.sharedSlots = state.sharedSlots
        .filter(item => item.name !== 'fromAccount')
        .concat({
          name: 'fromAccount', value: reference, source: 'user_selection',
        });
      await services.repository.replaceConversationState({
        conversationId: run.conversationId,
        customerId: run.customerId,
        state,
      });
    }
  }
  return updatedContext;
}

async function persistConversationState(
  repository, conversationId, customerId, state, deliveryStatus,
) {
  if (!state) return;
  const resumableStatuses = new Set([
    'clarification_required', 'awaiting_confirmation',
  ]);
  if (state.status !== 'retain' || !resumableStatuses.has(deliveryStatus)) {
    if (repository.clearConversationState) {
      await repository.clearConversationState(conversationId, customerId);
    }
    if (repository.clearConversationSlots) {
      await repository.clearConversationSlots(conversationId, customerId);
    }
    return;
  }
  if (state.schemaVersion === '2.0') {
    if (!repository.replaceConversationState) return;
    await repository.replaceConversationState({ conversationId, customerId, state });
    if (repository.getAgentWorkflow && repository.archiveAgentWorkflowConversationState) {
      const workflowId = deriveBusinessWorkflowId(
        customerId, conversationId, state.pendingEnvelopeId,
      );
      const workflow = await repository.getAgentWorkflow(
        workflowId, conversationId, customerId,
      );
      if (workflow) {
        await repository.archiveAgentWorkflowConversationState({
          id: workflowId, conversationId, customerId,
          expectedRevision: workflow.workflowRevision,
          conversationState: state,
        });
      }
    }
    if (repository.clearConversationSlots) {
      await repository.clearConversationSlots(conversationId, customerId);
    }
    return;
  }
  if (repository.clearConversationState) {
    await repository.clearConversationState(conversationId, customerId);
  }
  if (!repository.replaceConversationSlots) return;
  await repository.replaceConversationSlots({
    conversationId,
    customerId,
    sourceIntentId: state.pendingIntentId,
    slots: state.slots,
  });
}

async function persistAgentClarification(
  repository, delivery, agentRunId, conversationId, customerId, bindingSource,
) {
  const form = delivery && delivery.clarificationForm;
  if (!form) return;
  const payeeField = Array.isArray(form.fields)
    ? form.fields.find(item => item && item.name === 'payeeName')
    : null;
  // transfer_payee（已知收款人补账号）要求 payeeName 必须非空；
  // transfer_full（用户在卡片里一次补全，收款人可能为空）允许空值。
  if (form.kind === 'transfer_payee'
    && (!payeeField || typeof payeeField.value !== 'string' || !payeeField.value.trim())) {
    throw new BankApiError(502, 'agent_runtime_response_invalid', '智能服务补录卡片无效');
  }
  let binding = bindingSource || {};
  const state = binding.conversationState;
  if (
    !binding.businessWorkflowId
    && state && state.schemaVersion === '2.0' && state.status === 'retain'
    && typeof state.pendingEnvelopeId === 'string'
  ) {
    const businessWorkflowId = deriveBusinessWorkflowId(
      customerId, conversationId, state.pendingEnvelopeId,
    );
    const workflow = await repository.getAgentWorkflow(
      businessWorkflowId, conversationId, customerId,
    );
    if (workflow) {
      binding = {
        businessWorkflowId,
        taskId: workflow.activeGoalTaskId,
        expectedWorkflowRevision: workflow.workflowRevision,
      };
    }
  }
  await repository.createAgentClarification({
    id: form.clarificationId,
    agentRunId,
    customerId,
    conversationId,
    kind: form.kind,
    payeeName: payeeField && typeof payeeField.value === 'string' ? payeeField.value.trim() : '',
    tokenHash: sha256(form.submissionToken),
    expiresAt: form.expiresAt,
    businessWorkflowId: binding.businessWorkflowId || null,
    taskId: binding.taskId || null,
    expectedWorkflowRevision: binding.expectedWorkflowRevision || null,
  });
}

async function suspendSupersededWorkflow(repository, conversationId, customerId, nextState) {
  if (!repository || typeof repository.listAgentWorkflows !== 'function'
    || typeof repository.transitionAgentWorkflowControl !== 'function'
    || typeof repository.getConversationState !== 'function') return;
  const workflows = await repository.listAgentWorkflows(conversationId, customerId);
  const activeStatuses = new Set([
    'running', 'awaiting_clarification', 'awaiting_confirmation', 'waiting_external',
  ]);
  const nextWorkflowId = nextState && nextState.schemaVersion === '2.0'
    && nextState.status === 'retain' && typeof nextState.pendingEnvelopeId === 'string'
    ? deriveBusinessWorkflowId(
        customerId, conversationId, nextState.pendingEnvelopeId,
      )
    : null;
  // 本次 run 对应的新 workflow 永远不属于"被取代"对象：澄清回答导致 envelope 变化时，
  // 新 workflow（awaiting_clarification）是唯一活跃项，若被挂起后续 business-replies
  // 会全部 409，流程卡死。只允许挂起旧的业务工作流。
  const activeCandidates = workflows.filter(item => (
    activeStatuses.has(item.status) && item.id !== nextWorkflowId
  ));
  if (!activeCandidates.length) return;
  const currentRecord = await repository.getConversationState(conversationId, customerId);
  const persistedCurrentState = currentRecord && currentRecord.state
    ? currentRecord.state : null;
  const active = activeCandidates.find(item => (
    persistedCurrentState && item.conversationState
    && item.conversationState.pendingEnvelopeId === persistedCurrentState.pendingEnvelopeId
  )) || activeCandidates[0];
  const currentState = persistedCurrentState || active.conversationState;
  if (!currentState || currentState.schemaVersion !== '2.0') return;
  const continuesSameEnvelope = nextState && nextState.schemaVersion === '2.0'
    && nextState.status === 'retain'
    && nextState.pendingEnvelopeId === currentState.pendingEnvelopeId;
  if (continuesSameEnvelope) return;
  await repository.transitionAgentWorkflowControl({
    id: active.id,
    conversationId,
    customerId,
    action: 'SUSPEND',
    expectedRevision: active.workflowRevision,
    conversationState: currentState,
  });
}

function deriveBusinessWorkflowId(customerId, conversationId, envelopeId) {
  const safeId = /^[A-Za-z0-9_.:-]{1,160}$/;
  if (![customerId, conversationId, envelopeId].every(value => (
    typeof value === 'string' && safeId.test(value)
  ))) {
    throw new BankApiError(502, 'agent_runtime_response_invalid', '智能服务工作流标识无效');
  }
  // 与 runtime 侧 business_workflow_id 逐位一致：滑动取摘要中不含 13-19 位连续
  // 数字串（卡号探测规则）的 24 位窗口，保证持久化校验不会误拒派生 id。
  const base = customerId + '\x1f' + conversationId + '\x1f' + envelopeId;
  for (let attempt = 0; attempt < 9; attempt += 1) {
    const seed = attempt === 0 ? base : `${base}\x1f${attempt}`;
    const digest = crypto.createHash('sha256').update(seed).digest('hex');
    for (let offset = 0; offset + 24 <= digest.length; offset += 1) {
      const candidate = digest.slice(offset, offset + 24);
      if (!/(?:\d[ -]?){13,19}/.test(candidate)) return 'bwf_' + candidate;
    }
  }
  throw new BankApiError(502, 'agent_runtime_response_invalid', '智能服务工作流标识无效');
}

function sha256(value) {
  return crypto.createHash('sha256').update(String(value), 'utf8').digest('hex');
}

function safeHashEqual(expected, supplied) {
  if (typeof expected !== 'string' || typeof supplied !== 'string') return false;
  const left = Buffer.from(expected, 'utf8');
  const right = Buffer.from(supplied, 'utf8');
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

function projectPlansForSession(userResult) {
  if (!userResult || !Array.isArray(userResult.plans) || !userResult.plans.length) return null;
  return userResult.plans.slice(0, 3).map(plan => ({
    id: plan.id || null,
    title: plan.title,
    summary: plan.summary || null,
    totalAmount: plan.totalAmount || null,
    isPrimary: Boolean(plan.isPrimary),
    selectLabel: plan.selectLabel || null,
    selectMessage: plan.selectMessage || null,
    allocations: (Array.isArray(plan.allocations) ? plan.allocations : []).slice(0, 5)
      .map(allocation => ({
        productId: allocation.productId || null,
        productName: allocation.productName,
        productCategoryLabel: allocation.productCategoryLabel || null,
        portfolioRoleLabel: allocation.portfolioRoleLabel || null,
        riskLevel: allocation.riskLevel || null,
        amount: allocation.amount || null,
        suitability: allocation.suitability || null,
        detail: allocation.detail ? String(allocation.detail).slice(0, 300) : null,
        productActionLabel: allocation.productActionLabel || null,
      })),
  }));
}

function projectDeliveryForSession(delivery) {
  if (!delivery || typeof delivery !== 'object') return null;
  const orderedResponse = Array.isArray(delivery.userResponses) && delivery.userResponses.length
    ? delivery.userResponses[delivery.userResponses.length - 1]
    : null;
  const visibleResponse = delivery.userResponse || orderedResponse;
  const visible = visibleResponse || delivery.userResult;
  if (!visible) return null;
  // 产品卡片与财富方案来自 userResult（与 userResponse 并存时也要投影，供历史消息恢复渲染）
  const userResult = delivery.userResult && typeof delivery.userResult === 'object'
    ? delivery.userResult
    : null;
  const cards = Array.isArray(userResult && userResult.itemCards) && userResult.itemCards.length
    ? userResult.itemCards.slice(0, 5).map(card => ({
        id: card.id || null,
        title: card.title,
        subtitle: card.subtitle || null,
        metas: (Array.isArray(card.metas) ? card.metas : []).slice(0, 4)
          .map(meta => ({ label: meta.label, value: meta.value })),
        detail: card.action && card.action.detail
          ? String(card.action.detail).slice(0, 400)
          : null,
      }))
    : null;
  const plans = projectPlansForSession(userResult);
  if (visibleResponse) {
    const items = visible.items.map((value, index) => (
      value && typeof value === 'object' && !Array.isArray(value)
        ? { label: value.label, value: value.value }
        : { label: `项目${index + 1}`, value }
    ));
    return {
      text: visible.body,
      response: {
        type: visible.type,
        title: visible.title,
        body: visible.body,
        items,
        ...(cards ? { cards } : {}),
        ...(plans ? { plans } : {}),
      },
    };
  }
  return {
    text: visible.summary,
    response: {
      type: 'result',
      title: visible.title,
      body: visible.summary,
      items: visible.items.map((value, index) => ({
        label: `项目${index + 1}`,
        value,
      })),
      ...(cards ? { cards } : {}),
      ...(plans ? { plans } : {}),
    },
  };
}

async function persistSafeSessionTurn(agentSessionService, turn, context) {
  try {
    await agentSessionService.appendTurn(turn);
    return true;
  } catch (error) {
    const projectionFailure = error && [
      'invalid_agent_session_turn', 'agent_session_turn_too_large',
    ].includes(error.code);
    if (!projectionFailure && !(context && context.nonBlocking)) throw error;
    logSafeGatewayError('session_turn_projection', new BankApiError(
      500, 'session_turn_projection_failed', 'Session 公开轮次投影或持久化失败',
    ), {
      requestId: context && context.requestId,
      traceId: context && context.traceId,
    });
    // 根因日志：上层通用错误不携带原始校验信息，排障需要真实抛点
    logSafeGatewayError('session_turn_projection_cause', error, {
      requestId: context && context.requestId,
      traceId: context && context.traceId,
    });
    return false;
  }
}

async function persistAssistantSessionTurn(
  agentSessionService, delivery, sessionId, customerId, requestId, traceId,
) {
  let projected;
  try {
    projected = projectDeliveryForSession(delivery);
  } catch (error) {
    logSafeGatewayError('session_turn_projection', new BankApiError(
      500, 'session_turn_projection_failed', 'Session 公开轮次投影或持久化失败',
    ), { requestId, traceId });
    // 根因日志：上层通用错误不携带原始校验信息，排障需要真实抛点
    logSafeGatewayError('session_turn_projection_cause', error, { requestId, traceId });
    return false;
  }
  if (!projected) return false;
  return persistSafeSessionTurn(agentSessionService, {
    sessionId,
    customerId,
    role: 'assistant',
    text: projected.text,
    response: projected.response,
  }, { phase: 'assistant', requestId, traceId, nonBlocking: true });
}

async function streamAgentRunResponse(
  req, res, runtimeClient, input, repository, agentSessionService,
  releaseScope, effectiveConversationId,
) {
  const publicState = createTraceState({
    agentRunId: input.agentRunId,
    traceId: input.traceId,
  });
  const controller = new AbortController();
  let started = false;
  let delivery = null;
  let terminalStage = null;
  let conversationState = null;
  let captureFailed = false;
  const pendingEventRecords = [];
  const abortOnClose = () => {
    if (!res.writableEnded) controller.abort();
  };
  res.once('close', abortOnClose);
  try {
    if (typeof repository.markAgentRunTraceCapture === 'function') {
      repository.markAgentRunTraceCapture(input.agentRunId, 'recording')
        .catch(error => { captureFailed = true; logSafeTracePersistenceError(error, input); });
    }
    await runtimeClient.streamAgentRun(input, rawEvent => {
      const event = validateTraceEvent(rawEvent, publicState);
      if (typeof repository.appendAgentRunEvent === 'function') {
        pendingEventRecords.push({
          agentRunId: event.agentRunId,
          sequence: event.sequence,
          stage: event.stage,
          status: event.status,
          durationMs: event.durationMs == null ? null : event.durationMs,
          policyVersion: event.summary && event.summary.metrics
            && typeof event.summary.metrics.policyVersion === 'string'
            ? event.summary.metrics.policyVersion : null,
          safeSummary: event.summary,
          createdAt: event.timestamp,
        });
      }
      if (['run.completed', 'run.awaiting_confirmation', 'run.failed'].includes(event.stage)) {
        terminalStage = event.stage;
      }
      if (!started) {
        started = true;
        res.writeHead(200, {
          'Content-Type': 'text/event-stream; charset=utf-8',
          'Cache-Control': 'no-store',
          'X-Accel-Buffering': 'no',
          'X-Agent-Session-Id': effectiveConversationId,
          'Access-Control-Expose-Headers': 'X-Agent-Session-Id',
          Connection: 'keep-alive',
        });
        res.flushHeaders();
        if (res.socket && typeof res.socket.setNoDelay === 'function') {
          res.socket.setNoDelay(true);
        }
      }
      if (!res.writableEnded) {
        res.write('event: agent.trace\ndata: ' + JSON.stringify(event) + '\n\n');
      }
    }, { signal: controller.signal, onState: state => {
      conversationState = state;
    }, onResult: result => {
      delivery = result;
    } });
    if (!started) {
      const missing = Object.assign(new Error('runtime stream missing'), {
        status: 502,
        code: 'agent_runtime_response_invalid',
        isOperational: true,
      });
      await invokeAgentRuntime(() => Promise.reject(missing));
    }
    if (!publicState.terminal) throw new Error('runtime stream missing terminal event');
    // 先发送终端结果事件并关闭 SSE 连接，再在后台执行持久化清理，
    // 将终端持久化链从客户端关键路径上移除以压减 SSE 响应时延。
    if (delivery && !res.writableEnded) {
      res.write('event: agent.result\ndata: ' + JSON.stringify(delivery) + '\n\n');
    }
    if (!res.writableEnded) res.end();
    // 持久化清理：必须 await 以确保 releaseScope.finish(responsePersisted) 在调用方 finally 块的 finish(false) 之前执行，
    // 否则 finish(false) 先到达会导致 allResponsesDurable=false，阻止工作流会话释放。
    // 客户端已在 res.end() 收到完整响应，此处 await 仅影响服务端清理时序。
    await (async () => {
      const captureWrites = pendingEventRecords.map(record =>
        typeof repository.appendAgentRunEvent === 'function'
          ? repository.appendAgentRunEvent(record).catch(error => {
              captureFailed = true;
              logSafeTracePersistenceError(error, input);
            })
          : Promise.resolve()
      );
      await Promise.all(captureWrites);
      if (delivery) {
        await suspendSupersededWorkflow(
          repository, input.conversationId, input.customerId, conversationState,
        );
        await persistAgentClarification(
          repository, delivery, input.agentRunId, input.conversationId, input.customerId,
          { conversationState },
        );
        await persistConversationState(
          repository, input.conversationId, input.customerId,
          conversationState, delivery.status,
        );
        const responsePersisted = await persistAssistantSessionTurn(
          agentSessionService, delivery, input.conversationId, input.customerId,
          input.agentRunId, input.traceId,
        );
        await releaseScope.finish(responsePersisted);
      }
      await advanceAgentRun(
        repository,
        input.agentRunId,
        delivery ? delivery.status : terminalStage === 'run.failed' ? 'failed' : 'planned',
        publicState.sequence,
      );
      await finishTraceCaptureBestEffort(repository, input, captureFailed ? 'incomplete' : 'complete');
    })().catch(error => {
      logSafeGatewayError('stream_postprocess', error, {
        requestId: input.agentRunId,
        traceId: input.traceId,
      });
    });
    return STREAM_HANDLED;
  } catch (error) {
    logSafeGatewayError('stream_postprocess', error, {
      requestId: input.agentRunId,
      traceId: input.traceId,
    });
    if (controller.signal.aborted && error && error.code === 'agent_runtime_cancelled') {
      return STREAM_HANDLED;
    }
    if (!started) {
      await invokeAgentRuntime(() => Promise.reject(error));
    }
    if (!publicState.terminal && !res.writableEnded) {
      const allowed = new Set([
        'agent_runtime_unavailable',
        'agent_runtime_timeout',
        'agent_runtime_not_ready',
        'agent_runtime_request_invalid',
        'agent_runtime_response_invalid',
        'agent_runtime_failed',
      ]);
      const code = error && allowed.has(error.code) ? error.code : 'agent_runtime_failed';
      const failure = validateTraceEvent(
        createSafeFailureEvent(publicState, code, '智能服务运行失败'),
        publicState,
      );
      terminalStage = failure.stage;
      if (typeof repository.appendAgentRunEvent === 'function') {
        pendingEventRecords.push({
          agentRunId: failure.agentRunId,
          sequence: failure.sequence,
          stage: failure.stage,
          status: failure.status,
          durationMs: failure.durationMs == null ? null : failure.durationMs,
          policyVersion: null,
          safeSummary: failure.summary,
          createdAt: failure.timestamp,
        });
      }
      res.write('event: agent.trace\ndata: ' + JSON.stringify(failure) + '\n\n');
    }
    const captureWrites = pendingEventRecords.map(record =>
      typeof repository.appendAgentRunEvent === 'function'
        ? repository.appendAgentRunEvent(record).catch(error => {
            captureFailed = true;
            logSafeTracePersistenceError(error, input);
          })
        : Promise.resolve()
    );
    await Promise.all(captureWrites);
    if (terminalStage === 'run.failed') {
      await advanceAgentRun(repository, input.agentRunId, 'failed', publicState.sequence);
    }
    await finishTraceCaptureBestEffort(
      repository, input,
      captureFailed || controller.signal.aborted ? 'incomplete' : 'complete',
    );
    if (!res.writableEnded) res.end();
    return STREAM_HANDLED;
  } finally {
    res.removeListener('close', abortOnClose);
  }
}

function logSafeTracePersistenceError(error, input) {
  logSafeGatewayError('trace_persistence', new BankApiError(
    500, 'trace_persistence_failed', '运行轨迹持久化失败',
  ), {
    requestId: input && input.agentRunId,
    traceId: input && input.traceId,
  });
}

async function finishTraceCaptureBestEffort(repository, input, status) {
  if (typeof repository.markAgentRunTraceCapture !== 'function') return;
  try {
    await repository.markAgentRunTraceCapture(input.agentRunId, status);
  } catch (error) {
    logSafeTracePersistenceError(error, input);
  }
}

function userAgentSessionId(conversationId) {
  return `user:${conversationId}`;
}

function businessAgentSessionId(customerId, workflowId) {
  return `business:${customerId}:${workflowId}`;
}

async function releaseAgentSessionsBestEffort(runtimeClient, sessionIds) {
  try {
    if (!runtimeClient || typeof runtimeClient.releaseAgentSessions !== 'function') {
      throw new Error('release client unavailable');
    }
    await runtimeClient.releaseAgentSessions(sessionIds);
    return true;
  } catch (_) {
    console.warn('[ai-native-bank] code=agent_session_release_failed');
    return false;
  }
}

function rememberPendingTerminalAgentSession(context, agentRunId, sessionId) {
  const pending = context && context.pendingTerminalAgentSessions;
  if (!(pending instanceof Map) || typeof agentRunId !== 'string') return;
  const entry = pending.get(agentRunId);
  if (!entry || entry.activeCount < 1 || !(entry.sessionIds instanceof Set)) return;
  if (entry.sessionIds.size < 32) entry.sessionIds.add(sessionId);
}

function beginPendingTerminalAgentSessionScope(context, agentRunId) {
  const pending = context && context.pendingTerminalAgentSessions;
  if (!(pending instanceof Map) || typeof agentRunId !== 'string') {
    return Object.freeze({ finish: async () => false });
  }
  let entry = pending.get(agentRunId);
  if (!entry) {
    entry = { activeCount: 0, allResponsesDurable: true, sessionIds: new Set() };
    pending.set(agentRunId, entry);
  }
  entry.activeCount += 1;
  let finished = false;
  return Object.freeze({
    finish: async responsePersisted => {
      if (finished) return false;
      finished = true;
      entry.allResponsesDurable = entry.allResponsesDurable && Boolean(responsePersisted);
      entry.activeCount -= 1;
      if (entry.activeCount > 0) return false;
      if (pending.get(agentRunId) === entry) pending.delete(agentRunId);
      if (!entry.allResponsesDurable || entry.sessionIds.size === 0) return false;
      return releaseAgentSessionsBestEffort(
        context.agentRuntimeClient, [...entry.sessionIds],
      );
    },
  });
}

function logSafeGatewayError(stage, error, meta) {
  const code = error && typeof error.code === 'string' ? error.code : 'unclassified';
  const name = error && typeof error.name === 'string' ? error.name : 'Error';
  const message = error && typeof error.message === 'string'
    ? error.message.replace(/[\r\n\t]+/g, ' ').slice(0, 240)
    : 'unknown';
  const requestId = meta && typeof meta.requestId === 'string' ? meta.requestId : '-';
  const traceId = meta && typeof meta.traceId === 'string' ? meta.traceId : '-';
  console.error(
    '[ai-native-bank] stage=%s code=%s name=%s requestId=%s traceId=%s message=%s',
    stage, code, name, requestId, traceId, message,
  );
  if (!(error instanceof BankApiError) && error && typeof error.stack === 'string') {
    const safeStack = error.stack.split(/\r?\n/).slice(1, 7)
      .map(line => line.trim().replace(/[\r\n\t]+/g, ' ').slice(0, 240))
      .join(' | ');
    console.error('[ai-native-bank] stage=%s stack=%s', stage, safeStack);
  }
}

async function advanceAgentRun(repository, agentRunId, runtimeStatus, sequence) {
  if (repository.updateAgentRunTraceSequence) await repository.updateAgentRunTraceSequence(agentRunId, sequence);
  const paths = {
    planned: {
      accepted: ['interpreting', 'planning'], interpreting: ['planning'], planning: [],
    },
    succeeded: {
      accepted: ['interpreting', 'planning', 'executing', 'succeeded'],
      interpreting: ['planning', 'executing', 'succeeded'],
      planning: ['executing', 'succeeded'], awaiting_confirmation: ['executing', 'succeeded'],
      clarification_required: ['executing', 'succeeded'],
      executing: ['succeeded'], succeeded: [],
    },
    failed: {
      accepted: ['failed'], interpreting: ['failed'], planning: ['failed'],
      awaiting_confirmation: ['executing', 'failed'], executing: ['failed'], failed: [],
      clarification_required: ['executing', 'failed'],
    },
    awaiting_confirmation: {
      accepted: ['interpreting', 'planning', 'awaiting_confirmation'],
      interpreting: ['planning', 'awaiting_confirmation'],
      planning: ['awaiting_confirmation'], awaiting_confirmation: [],
      clarification_required: ['awaiting_confirmation'],
    },
    clarification_required: {
      accepted: ['interpreting', 'clarification_required'],
      interpreting: ['clarification_required'], planning: ['clarification_required'],
      awaiting_confirmation: ['clarification_required'], clarification_required: [],
    },
    navigation_only: {
      accepted: ['interpreting', 'navigation_only'],
      interpreting: ['navigation_only'], navigation_only: [],
    },
    // runtime `unsupported` = non_task 闲聊/问候，已正常回复引导语；非政策拦截
    // （政策拦截走 rails 的 blocked 路径），运行结果按正常完成计。
    unsupported: {
      accepted: ['interpreting', 'succeeded'], interpreting: ['succeeded'], succeeded: [],
    },
    blocked: {
      accepted: ['interpreting', 'blocked'], interpreting: ['blocked'], blocked: [],
    },
  };
  const current = await repository.getAgentRun(agentRunId);
  if (!current) return;
  const transitions = (paths[runtimeStatus] || paths.failed)[current.status];
  if (!transitions) return;
  for (const nextStatus of transitions) {
    await repository.transitionAgentRun(agentRunId, nextStatus);
  }
}

async function invokeAgentRuntime(operation) {
  try {
    return await operation();
  } catch (error) {
    const allowedCodes = new Set([
      'agent_runtime_unavailable',
      'agent_runtime_timeout',
      'agent_runtime_not_ready',
      'agent_runtime_request_invalid',
      'agent_runtime_response_invalid',
      'agent_runtime_failed',
    ]);
    if (!error || !error.isOperational || !allowedCodes.has(error.code)) throw error;
    console.warn(
      '[ai-native-bank] stage=agent_runtime code=%s upstreamStatus=%s',
      error.code,
      Number.isInteger(error.status) ? error.status : 'invalid',
    );
    const status = [400, 422, 502, 503, 504].includes(error.status) ? error.status : 502;
    const safeMessages = {
      agent_runtime_unavailable: '智能服务运行时不可用',
      agent_runtime_timeout: '智能服务处理超时，请稍后重试',
      agent_runtime_not_ready: '智能服务尚未就绪',
      agent_runtime_request_invalid: '智能服务无法处理当前请求',
      agent_runtime_response_invalid: '智能服务返回结果无法识别',
      agent_runtime_failed: '智能服务处理失败',
    };
    throw new BankApiError(status, error.code, safeMessages[error.code]);
  }
}

async function readAgentRuntimeStatus(agentRuntimeClient) {
  try {
    return await invokeAgentRuntime(() => agentRuntimeClient.getStatus());
  } catch (error) {
    if (!(error instanceof BankApiError)
      || !AGENT_RUNTIME_STATUS_FAILURE_CODES.has(error.code)) throw error;
    return Object.freeze({
      provider: 'openjiuwen',
      version: null,
      ready: false,
      checkpointerProvider: null,
      modelConfigured: false,
      modelProvider: null,
      modelName: null,
      agents: PUBLIC_AGENT_RUNTIME_IDENTITIES,
      runtimeState: 'temporarily_unavailable',
    });
  }
}

function buildWorkflowResumeTraceEvents(run, resumed, timestamp, resumeKind) {
  const successful = resumed.receipts.filter(item => item.status === 'SUCCEEDED');
  const failed = resumed.receipts.filter(item => item.status === 'FAILED');
  const definitions = [
    ['confirmation.checkpoint_accepted', resumeKind === 'clarification' ? '结构化补录已安全接收' : '本人确认已接收', {
      checkpointStatus: resumeKind === 'clarification' ? 'not_applicable' : 'authorized',
      grantCount: resumed.receipts.length,
    }],
    ['capability.execution_completed', resumeKind === 'clarification' ? '业务智能体已恢复当前任务' : '受控业务执行完成', {
      grantCount: resumed.receipts.length, succeededCount: successful.length,
      failedCount: failed.length,
    }],
    ['run.completed', '智能服务运行完成', {
      status: resumed.status, grantCount: resumed.receipts.length,
      succeededCount: successful.length, failedCount: failed.length,
    }],
  ];
  if (resumeKind === 'business_reply') {
    definitions[0][1] = '业务澄清已接收';
    definitions[0][2].checkpointStatus = 'not_applicable';
    definitions[1][1] = '业务智能体已恢复当前任务';
  }
  return definitions.map((item, index) => ({
    schemaVersion: '1.0', sequence: run.lastTraceSequence + index + 1,
    timestamp, stage: item[0],
    status: resumed.status === 'failed' ? 'failed' : 'completed',
    agentRunId: run.id, traceId: run.traceId,
    summary: { title: item[1], input: [], output: [], metrics: item[2] },
  }));
}

async function persistWorkflowResumeTrace(
  repository, run, runtimeStatus, resumed, timestamp, resumeKind,
) {
  const traceEvents = buildWorkflowResumeTraceEvents(run, resumed, timestamp, resumeKind);
  const lastSequence = traceEvents[traceEvents.length - 1].sequence;
  await advanceAgentRun(repository, run.id, runtimeStatus, lastSequence);
  return traceEvents;
}

// ---- 确认卡文本答复：语义判定 + 授权恢复 ----

const CONFIRMATION_REPLY_DECISIONS = new Set(['affirm', 'decline', 'adjust', 'unrelated']);

function buildConfirmationReplyPrompt(checkpoint, message) {
  const summary = checkpoint && checkpoint.summary || {};
  const lines = ['你是一个银行智能服务的确认判定组件。客户屏幕上有如下待确认操作：'];
  if (typeof summary.title === 'string' && summary.title) lines.push('标题：' + summary.title);
  if (typeof summary.description === 'string' && summary.description) lines.push('说明：' + summary.description);
  if (Array.isArray(summary.items) && summary.items.length) {
    lines.push('明细：' + summary.items.filter(item => typeof item === 'string').join('；'));
  }
  lines.push('');
  lines.push('客户最新回复：「' + String(message).slice(0, 240) + '」');
  lines.push('');
  lines.push('请只输出一个 JSON 对象：{"decision":"affirm"}、{"decision":"decline"}、{"decision":"adjust"} 或 {"decision":"unrelated"}。');
  lines.push('affirm=客户明确同意按上述摘要办理本次操作（如「确认办理」「同意转账」「就按这个办」）；');
  lines.push('decline=客户明确拒绝或暂缓办理；');
  lines.push('adjust=客户在补充、指定或修改本次操作的执行参数（如指定转出账户或收款账户、说明金额构成、补充附言用途），既没有同意也没有拒绝——资金类操作绝不能把参数补充当作同意；');
  lines.push('unrelated=与本次确认无关的新请求或新问题。');
  return lines.join('\n');
}

function parseConfirmationReplyDecision(content) {
  if (typeof content !== 'string') return 'unrelated';
  const match = String(content).match(/\{[^{}]*"decision"\s*:\s*"([a-z]+)"[^{}]*\}/i);
  const decision = match && match[1] ? match[1].toLowerCase() : '';
  return CONFIRMATION_REPLY_DECISIONS.has(decision) ? decision : 'unrelated';
}

async function classifyConfirmationReply(modelGateway, checkpoint, message) {
  if (!modelGateway || typeof modelGateway.complete !== 'function') return 'unrelated';
  try {
    const prompt = buildConfirmationReplyPrompt(checkpoint, message);
    const result = await modelGateway.complete({
      messages: [
        { role: 'system', content: '你只输出一个 JSON 判定结果，不输出其他内容。' },
        { role: 'user', content: prompt },
      ],
      temperature: 0,
      max_tokens: 64,
    });
    const content = result && Array.isArray(result.choices) && result.choices[0]
      && result.choices[0].message ? result.choices[0].message.content : null;
    return parseConfirmationReplyDecision(content);
  } catch (error) {
    logSafeGatewayError('confirmation_reply_classification', error, {});
    return 'unrelated';
  }
}

// 文本确认答复命中待确认检查点：授权并恢复业务流程，返回 JSON 载荷
// （前端按 resumeConfirmation 通道把 traceEvents 续接到原轮轨迹，delivery 渲染成交付卡）
async function confirmByReplyAndResume(options) {
  const { services, context, conversationId, customerId, checkpoint, message } = options;
  const repository = services.repository;
  const run = await repository.getAgentRun(checkpoint.agentRunId);
  if (!run || run.customerId !== customerId || run.conversationId !== conversationId) {
    throw new BankApiError(409, 'agent_run_conflict', '待确认操作不属于当前会话');
  }
  await persistSafeSessionTurn(services.agentSessionService, {
    sessionId: conversationId,
    customerId,
    role: 'user',
    text: message,
    response: null,
  }, { phase: 'user', requestId: checkpoint.agentRunId, traceId: context.meta.traceId });
  const capabilityService = services.agentCapabilityService || createAgentCapabilityService(services);
  const authorization = await capabilityService.confirmByReply({
    agentRunId: checkpoint.agentRunId,
    checkpointId: checkpoint.id,
    customerId,
    actorType: 'user',
    replyEvidence: message,
  });
  const workflow = await repository.getAgentWorkflow(
    authorization.businessWorkflowId, run.conversationId, run.customerId,
  );
  if (!workflow) {
    throw new BankApiError(409, 'agent_workflow_not_found', '业务流程状态不存在');
  }
  const conversationContext = await loadConversationContext(
    repository, run.conversationId, run.customerId, context.now(),
  );
  const releaseScope = beginPendingTerminalAgentSessionScope(context, run.id);
  try {
    const resumed = await invokeAgentRuntime(() => context.agentRuntimeClient.resumeBusinessWorkflow(
      authorization.businessWorkflowId,
      {
        agentRunId: run.id,
        conversationId: run.conversationId,
        customerId: run.customerId,
        checkpointId: authorization.checkpointId,
        grants: authorization.grants,
        expectedWorkflowRevision: workflow.workflowRevision,
        conversationContext,
        executableCapabilityIds: run.executableCapabilityIds || [],
      },
    ));
    await persistConversationState(
      repository, run.conversationId, run.customerId,
      resumed.conversationState, resumed.status,
    );
    const completedUserResult = await buildCompletedUserResult(
      repository, authorization, resumed, run.customerId, workflow,
    );
    const nextDelivery = {
      agentRunId: run.id,
      status: resumed.status,
      // 交付契约：非 succeeded 状态不得携带 userResult（张三的成功回执经 userResponses/最终轮交付呈现）
      userResult: resumed.status === 'succeeded' ? (completedUserResult || null) : null,
      userResponse: resumed.userResponse || null,
      userResponses: resumed.userResponses || null,
      confirmation: resumed.confirmation || null,
      clarificationForm: resumed.clarificationForm || null,
    };
    if (resumed.status === 'awaiting_confirmation' || resumed.status === 'clarification_required') {
      await persistAgentClarification(
        repository, nextDelivery, run.id, run.conversationId, run.customerId,
        {
          businessWorkflowId: authorization.businessWorkflowId,
          taskId: resumed.activeGoalTaskId,
          expectedWorkflowRevision: resumed.workflowRevision,
        },
      );
    }
    const traceEvents = await persistWorkflowResumeTrace(
      repository, run, resumed.status, resumed, context.now(),
    );
    const responsePersisted = await persistAssistantSessionTurn(
      services.agentSessionService, nextDelivery, run.conversationId,
      run.customerId, run.id, run.traceId,
    );
    await releaseScope.finish(responsePersisted);
    return {
      status: resumed.status === 'succeeded' ? 'SUCCEEDED'
        : resumed.status === 'failed' ? 'FAILED' : 'NEXT_ACTION_REQUIRED',
      workflowRevision: resumed.workflowRevision,
      taskProgress: resumed.taskProgress || null,
      nextDelivery,
      completedUserResult: completedUserResult || null,
      traceEvents,
    };
  } catch (error) {
    await releaseScope.finish(false);
    throw error;
  }
}

function mockIdentity(req, context) {
  const customerId = String(req.headers['x-mock-customer-id'] || context.defaultCustomerId || '').trim();
  if (!customerId || !context.allowedCustomerIds.has(customerId)) {
    throw new BankApiError(401, 'invalid_mock_identity', 'Mock 客户身份无效');
  }
  const actorType = String(req.headers['x-actor-type'] || 'agent').trim().toLowerCase();
  if (!['agent', 'user'].includes(actorType)) throw new BankApiError(403, 'forbidden', '调用方类型无效');
  return { customerId, actorType };
}

function requireInternalToken(req, expectedToken) {
  const supplied = String(req.headers.authorization || '');
  const expected = expectedToken ? 'Bearer ' + expectedToken : '';
  const left = Buffer.from(supplied);
  const right = Buffer.from(expected);
  if (!expected || left.length !== right.length || !crypto.timingSafeEqual(left, right)) {
    throw new BankApiError(403, 'forbidden', '最终执行接口仅供受控执行引擎调用');
  }
}

function requireRuntimeServiceToken(req, expectedToken) {
  const supplied = String(req.headers.authorization || '');
  const expected = expectedToken ? 'Bearer ' + expectedToken : '';
  const left = Buffer.from(supplied);
  const right = Buffer.from(expected);
  if (!expected || left.length !== right.length || !crypto.timingSafeEqual(left, right)) {
    throw new BankApiError(403, 'forbidden', '内部能力接口仅供 Agent Runtime 调用');
  }
}

function resolveAgentRuntimeServiceToken(options) {
  if (options.agentRuntimeServiceToken != null) return String(options.agentRuntimeServiceToken);
  try {
    return loadServiceToken({
      envValue: process.env.AI_BANK_AGENT_RUNTIME_TOKEN,
      tokenFile: process.env.AI_BANK_AGENT_RUNTIME_TOKEN_FILE,
    });
  } catch (_) {
    return '';
  }
}

function requireUserAction(identity, actionName) {
  if (!identity || identity.actorType !== 'user') {
    throw new BankApiError(403, 'forbidden', actionName + '必须来自明确的用户确认操作');
  }
}

function requireEmptyObject(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    throw new BankApiError(422, 'validation_error', '请求体必须是 JSON 对象', [{ field: 'body', code: 'object_required' }]);
  }
  const unknown = Object.keys(input);
  if (unknown.length) {
    throw new BankApiError(422, 'validation_error', '请求包含不允许的字段', unknown.map(field => ({ field, code: 'unknown_field' })));
  }
  return input;
}

function parseJsonBody(req, maximumBytes) {
  return new Promise((resolve, reject) => {
    const contentLength = Number(req.headers['content-length'] || 0);
    if (Number.isFinite(contentLength) && contentLength > maximumBytes) {
      reject(new BankApiError(413, 'request_body_too_large', '请求体超过大小限制'));
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
        reject(new BankApiError(413, 'request_body_too_large', '请求体超过大小限制'));
        return;
      }
      chunks.push(buffer);
    });
    req.on('end', () => {
      if (settled) return;
      settled = true;
      if (!chunks.length) return resolve({});
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch (_) { reject(new BankApiError(400, 'invalid_json', '请求体必须是有效 JSON')); }
    });
    req.on('error', error => {
      if (settled) return;
      settled = true;
      reject(error);
    });
  });
}

function decodePath(value) {
  try { return decodeURIComponent(value); }
  catch (_) { throw new BankApiError(400, 'invalid_path_parameter', '路径参数无效'); }
}

function requireAllowedQuery(url, allowedKeys) {
  const allowed = new Set(allowedKeys);
  const result = {};
  for (const [key, value] of url.searchParams.entries()) {
    if (!allowed.has(key) || Object.prototype.hasOwnProperty.call(result, key)) {
      throw new BankApiError(422, 'validation_error', '查询参数无效', [
        { field: key, code: allowed.has(key) ? 'duplicate_field' : 'unknown_field' },
      ]);
    }
    result[key] = value;
  }
  return result;
}

function exactNextActionQuery(url) {
  const query = requireAllowedQuery(url, ['surface', 'channel', 'limit']);
  const surface = query.surface || 'home';
  if (!['home', 'agent', 'channel'].includes(surface)) {
    throw new BankApiError(422, 'validation_error', 'surface is invalid', [
      { field: 'surface', code: 'enum' },
    ]);
  }
  const channel = query.channel || null;
  if ((surface === 'channel') !== Boolean(channel)
    || (channel && !/^[A-Za-z0-9_.:-]{1,80}$/.test(channel))) {
    throw new BankApiError(422, 'validation_error', 'channel is invalid for the selected surface', [
      { field: 'channel', code: surface === 'channel' ? 'required' : 'not_allowed' },
    ]);
  }
  const limitText = query.limit === undefined ? '3' : query.limit;
  if (!/^[1-3]$/.test(limitText)) {
    throw new BankApiError(422, 'validation_error', 'limit must be between 1 and 3', [
      { field: 'limit', code: 'range' },
    ]);
  }
  return { surface, channel, limit: Number(limitText) };
}

function exactNextActionDecisionBody(value, currentTime) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new BankApiError(422, 'validation_error', 'Decision body must be an object');
  }
  const allowed = new Set(['decision', 'snoozeUntil', 'feedbackCode']);
  const unknown = Object.keys(value).filter(key => !allowed.has(key));
  if (unknown.length) {
    throw new BankApiError(422, 'validation_error', 'Decision body contains unknown fields',
      unknown.map(field => ({ field, code: 'unknown_field' })));
  }
  if (!['accept', 'dismiss', 'snooze', 'disable_personalization'].includes(value.decision)) {
    throw new BankApiError(422, 'validation_error', 'decision is invalid', [
      { field: 'decision', code: 'enum' },
    ]);
  }
  if (value.feedbackCode !== undefined
    && (typeof value.feedbackCode !== 'string' || !/^[A-Za-z0-9_.:-]{1,80}$/.test(value.feedbackCode))) {
    throw new BankApiError(422, 'validation_error', 'feedbackCode is invalid', [
      { field: 'feedbackCode', code: 'format' },
    ]);
  }
  if (value.decision === 'snooze') {
    if (typeof value.snoozeUntil !== 'string' || !Number.isFinite(Date.parse(value.snoozeUntil))
      || Date.parse(value.snoozeUntil) <= Date.parse(currentTime)) {
      throw new BankApiError(422, 'validation_error', 'snoozeUntil must be a future timestamp', [
        { field: 'snoozeUntil', code: 'future_timestamp_required' },
      ]);
    }
  } else if (value.snoozeUntil !== undefined) {
    throw new BankApiError(422, 'validation_error', 'snoozeUntil is only valid for snooze', [
      { field: 'snoozeUntil', code: 'not_allowed' },
    ]);
  }
  return {
    decision: value.decision,
    ...(value.snoozeUntil ? { snoozeUntil: new Date(value.snoozeUntil).toISOString() } : {}),
    ...(value.feedbackCode ? { feedbackCode: value.feedbackCode } : {}),
  };
}

// 回执展示时间用人类可读格式。禁止输出 ISO-8601 原文（如 2026-09-12T08:48:43.576Z）：
// 前端 trace 投影的防链接检查会把 "T08:48" 中的 t08: 判定为 URL scheme，
// 导致整个 completedUserResult 被拒渲染（点确认后误报「办理未完成」）。
function formatReceiptTime(value) {
  const date = new Date(value);
  const pad = number => String(number).padStart(2, '0');
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`
    + ` ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
}

async function buildCompletedUserResult(repository, authorization, resumed, customerId, workflow) {
  if (!repository || typeof repository.getCapabilityGrant !== 'function'
    || !authorization || !Array.isArray(authorization.grants)
    || !resumed || !Array.isArray(resumed.receipts)) return null;
  const successful = new Map(resumed.receipts
    .filter(item => item && item.status === 'SUCCEEDED')
    .map(item => [item.grantId, item]));
  for (const grant of authorization.grants) {
    const runtimeReceipt = successful.get(grant.grantId);
    if (!runtimeReceipt || runtimeReceipt.taskId !== grant.taskId
      || runtimeReceipt.capabilityId !== grant.capabilityId
      || grant.capabilityId !== 'bank.loan.repayment.create') continue;
    const record = await repository.getCapabilityGrant(grant.grantId);
    if (!record || record.customerId !== customerId
      || record.businessWorkflowId !== authorization.businessWorkflowId
      || record.taskId !== grant.taskId || record.capabilityId !== grant.capabilityId
      || record.status !== 'CONSUMED' || !record.receipt
      || record.receipt.status !== 'SUCCEEDED' || !record.receipt.result) continue;
    const payment = record.receipt.result;
    const amountMinor = payment.paid && payment.paid.amountMinor;
    if (payment.status !== 'SUCCEEDED'
      || !Number.isInteger(amountMinor) || amountMinor < 1 || amountMinor > 800000000
      || !payment.paid || payment.paid.currency !== 'CNY'
      || typeof payment.loanId !== 'string' || typeof payment.accountId !== 'string'
      || typeof payment.id !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(payment.id)
      || typeof payment.paidAt !== 'string' || !Number.isFinite(Date.parse(payment.paidAt))) continue;
    const [loan, account] = await Promise.all([
      typeof repository.getLoan === 'function' ? repository.getLoan(customerId, payment.loanId) : null,
      typeof repository.getAccount === 'function' ? repository.getAccount(customerId, payment.accountId) : null,
    ]);
    const accountLast4 = account && typeof account.maskedNo === 'string'
      ? (account.maskedNo.match(/([0-9]{4})$/) || [])[1] : null;
    if (!loan || !account || !accountLast4
      || typeof loan.productName !== 'string' || !loan.productName) continue;
    const amountText = (amountMinor / 100).toLocaleString('zh-CN', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    return {
      kind: 'loan_repayment_execution', title: '贷款还款成功',
      summary: `${loan.productName} 本期已还款 ${amountText}元。`,
      items: [
        `付款账户：${account.name} · 尾号${accountLast4}`,
        `还款时间：${formatReceiptTime(payment.paidAt)}`,
        `还款单号：••••••${payment.id.slice(-6).toUpperCase()}`,
      ],
      primaryAction: '查看贷款还款记录',
    };
  }
  for (const grant of authorization.grants) {
    const runtimeReceipt = successful.get(grant.grantId);
    if (!runtimeReceipt || runtimeReceipt.taskId !== grant.taskId
      || runtimeReceipt.capabilityId !== grant.capabilityId
      || grant.capabilityId !== 'bank.utility.payment.create') continue;
    const record = await repository.getCapabilityGrant(grant.grantId);
    if (!record || record.customerId !== customerId
      || record.businessWorkflowId !== authorization.businessWorkflowId
      || record.taskId !== grant.taskId || record.capabilityId !== grant.capabilityId
      || record.status !== 'CONSUMED' || !record.receipt
      || record.receipt.status !== 'SUCCEEDED' || !record.receipt.result) continue;
    const payment = record.receipt.result;
    const amountMinor = payment.paid && payment.paid.amountMinor;
    if (payment.status !== 'SUCCEEDED'
      || !Number.isInteger(amountMinor) || amountMinor < 1 || amountMinor > 800000000
      || !payment.paid || payment.paid.currency !== 'CNY'
      || typeof payment.billId !== 'string' || typeof payment.accountId !== 'string'
      || typeof payment.receiptNo !== 'string' || payment.receiptNo.length > 80
      || typeof payment.paidAt !== 'string' || !Number.isFinite(Date.parse(payment.paidAt))) continue;
    const [bills, account] = await Promise.all([
      typeof repository.listUtilityBills === 'function' ? repository.listUtilityBills(customerId) : [],
      typeof repository.getAccount === 'function' ? repository.getAccount(customerId, payment.accountId) : null,
    ]);
    const bill = Array.isArray(bills) ? bills.find(item => item && item.id === payment.billId) : null;
    const accountLast4 = account && typeof account.maskedNo === 'string'
      ? (account.maskedNo.match(/([0-9]{4})$/) || [])[1] : null;
    if (!bill || !account || !accountLast4
      || typeof bill.name !== 'string' || !bill.name
      || typeof bill.period !== 'string' || !bill.period) continue;
    const amountText = (amountMinor / 100).toLocaleString('zh-CN', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    return {
      kind: 'utility_payment_execution', title: '生活缴费成功',
      summary: `${bill.name} · ${bill.period} 已缴费 ${amountText}元。`,
      items: [
        `付款账户：${account.name} · 尾号${accountLast4}`,
        `缴费时间：${formatReceiptTime(payment.paidAt)}`,
        `电子回单号：••••••${payment.receiptNo.slice(-8).toUpperCase()}`,
      ],
      primaryAction: '查看生活缴费记录',
    };
  }
  for (const grant of authorization.grants) {
    const runtimeReceipt = successful.get(grant.grantId);
    if (!runtimeReceipt || runtimeReceipt.taskId !== grant.taskId
      || runtimeReceipt.capabilityId !== grant.capabilityId
      || grant.capabilityId !== 'bank.credit-card.repayment.create') continue;
    const record = await repository.getCapabilityGrant(grant.grantId);
    if (!record || record.customerId !== customerId
      || record.businessWorkflowId !== authorization.businessWorkflowId
      || record.taskId !== grant.taskId || record.capabilityId !== grant.capabilityId
      || record.status !== 'CONSUMED' || !record.receipt
      || record.receipt.status !== 'SUCCEEDED' || !record.receipt.result) continue;
    const repayment = record.receipt.result;
    const amountMinor = repayment.paid && repayment.paid.amountMinor;
    const remainingMinor = repayment.remainingOutstanding
      && repayment.remainingOutstanding.amountMinor;
    const currency = repayment.paid && repayment.paid.currency;
    const fromAccount = repayment.fromAccount;
    if (repayment.status !== 'SUCCEEDED'
      || !Number.isInteger(amountMinor) || amountMinor < 1 || amountMinor > 800000000
      || !Number.isInteger(remainingMinor) || remainingMinor < 0
      || currency !== 'CNY'
      || !fromAccount || typeof fromAccount.name !== 'string' || !fromAccount.name
      || typeof fromAccount.id !== 'string'
      || !/^[A-Za-z0-9_.:-]{1,160}$/.test(fromAccount.id)
      || typeof repayment.id !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(repayment.id)
      || typeof repayment.cardId !== 'string'
      || typeof repayment.paidAt !== 'string' || !Number.isFinite(Date.parse(repayment.paidAt))) continue;
    const [card, account] = await Promise.all([
      typeof repository.getCreditCard === 'function'
        ? repository.getCreditCard(customerId, repayment.cardId) : null,
      typeof repository.getAccount === 'function'
        ? repository.getAccount(customerId, fromAccount.id) : null,
    ]);
    const cardLast4 = card && typeof card.maskedNo === 'string'
      ? (card.maskedNo.match(/([0-9]{4})$/) || [])[1] : null;
    const accountLast4 = account && typeof account.maskedNo === 'string'
      ? (account.maskedNo.match(/([0-9]{4})$/) || [])[1] : null;
    if (!card || !account || !cardLast4 || !accountLast4
      || typeof card.productName !== 'string' || !card.productName) continue;
    const money = value => (value / 100).toLocaleString('zh-CN', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    return {
      kind: 'credit_card_repayment_execution',
      title: '信用卡还款成功',
      summary: `${card.productName} · 尾号${cardLast4} 已还款 ${money(amountMinor)}元。`,
      items: [
        `付款账户：${account.name} · 尾号${accountLast4}`,
        `剩余未还：${money(remainingMinor)}元`,
        `还款时间：${formatReceiptTime(repayment.paidAt)}`,
        `还款单号：••••••${repayment.id.slice(-6).toUpperCase()}`,
      ],
      primaryAction: '查看还款记录',
    };
  }
  for (const grant of authorization.grants) {
    const runtimeReceipt = successful.get(grant.grantId);
    if (!runtimeReceipt || runtimeReceipt.taskId !== grant.taskId
      || runtimeReceipt.capabilityId !== grant.capabilityId
      || grant.capabilityId !== 'bank.wealth.purchase.create') continue;
    const record = await repository.getCapabilityGrant(grant.grantId);
    if (!record || record.customerId !== customerId
      || record.businessWorkflowId !== authorization.businessWorkflowId
      || record.taskId !== grant.taskId || record.capabilityId !== grant.capabilityId
      || record.status !== 'CONSUMED' || !record.receipt
      || record.receipt.status !== 'SUCCEEDED' || !record.receipt.result) continue;
    const purchase = record.receipt.result;
    const amountMinor = purchase.amount && purchase.amount.amountMinor;
    const currency = purchase.amount && purchase.amount.currency;
    // The grant receipt owns execution success; the wealth domain result uses
    // CONFIRMED for a booked purchase order.
    if (purchase.status !== 'CONFIRMED'
      || !Number.isInteger(amountMinor) || amountMinor < 1 || amountMinor > 800000000
      || currency !== 'CNY'
      || typeof purchase.productName !== 'string' || !purchase.productName
      || purchase.productName.length > 120) continue;
    const account = await repository.getAccount(customerId, purchase.accountId);
    const accountLast4 = account && typeof account.maskedNo === 'string'
      ? (account.maskedNo.match(/([0-9]{4})$/) || [])[1] : null;
    if (!account || !accountLast4) continue;
    return {
      kind: 'wealth_purchase_execution',
      title: '理财申购成功',
      summary: `已申购${purchase.productName} ${(amountMinor / 100).toFixed(2)}元。`,
      items: [
        `付款账户：${account.name} · 尾号${accountLast4}`,
        '申购状态：已确认',
      ],
      primaryAction: '查看理财持仓',
    };
  }
  for (const grant of authorization.grants) {
    const runtimeReceipt = successful.get(grant.grantId);
    if (!runtimeReceipt || runtimeReceipt.taskId !== grant.taskId
      || runtimeReceipt.capabilityId !== grant.capabilityId
      || grant.capabilityId !== 'bank.transfer.order.create') continue;
    const record = await repository.getCapabilityGrant(grant.grantId);
    if (!record || record.customerId !== customerId
      || record.businessWorkflowId !== authorization.businessWorkflowId
      || record.taskId !== grant.taskId || record.capabilityId !== grant.capabilityId
      || record.status !== 'CONSUMED' || !record.receipt
      || record.receipt.status !== 'SUCCEEDED' || !record.receipt.result) continue;
    const order = record.receipt.result.order;
    const execution = record.receipt.result.receipt;
    const amountMinor = order && order.amount && order.amount.amountMinor;
    const currency = order && order.amount && order.amount.currency;
    if (!order || !execution || execution.status !== 'SUCCEEDED'
      || !Number.isInteger(amountMinor) || amountMinor < 1 || amountMinor > 800000000
      || currency !== 'CNY'
      || !/^RCPT-[0-9]{14}-[A-F0-9]{6}$/.test(execution.receiptNo || '')) continue;
    const [account, payee] = await Promise.all([
      repository.getAccount(customerId, order.fromAccountId),
      repository.getPayee(customerId, order.payeeId),
    ]);
    const accountLast4 = account && typeof account.maskedNo === 'string'
      ? (account.maskedNo.match(/([0-9]{4})$/) || [])[1] : null;
    if (!account || !payee || !accountLast4) continue;
    // 「第N笔」按声明顺序中同一能力族（同 capabilityId）目标的序位计，
    // 而不是 delegatedTaskIds 全集下标——跨域工作流（如 查余额+转账）的
    // 前置查询目标会把转账序号整体推后，与确认卡的转账内编号互相矛盾。
    let sequence = 1;
    if (workflow && Array.isArray(workflow.delegatedTaskIds)) {
      const familyGrants = await repository.listCapabilityGrantsByWorkflow(
        authorization.businessWorkflowId, customerId,
      );
      const familyTaskIds = workflow.delegatedTaskIds.filter(taskId =>
        familyGrants.some(item => item.taskId === taskId
          && item.capabilityId === grant.capabilityId));
      const taskIndex = familyTaskIds.indexOf(grant.taskId) + 1;
      if (taskIndex > 0) sequence = taskIndex;
      else if (resumed.taskProgress && Number.isInteger(resumed.taskProgress.completed)) {
        sequence = Math.max(1, resumed.taskProgress.completed);
      }
    } else if (resumed.taskProgress && Number.isInteger(resumed.taskProgress.completed)) {
      sequence = Math.max(1, resumed.taskProgress.completed);
    }
    return {
      kind: 'transfer_execution',
      title: `第${sequence}笔转账成功`,
      summary: `已向${payee.name}转账${(amountMinor / 100).toFixed(2)}元。`,
      items: [
        `付款账户：${account.name} · 尾号${accountLast4}`,
        `电子回单号：RCPT-••••-${execution.receiptNo.slice(-6)}`,
      ],
      primaryAction: null,
    };
  }
  return null;
}

function ok(data, meta) { return { status: 200, data, meta }; }
function created(data, meta) { return { status: 201, data, meta }; }

async function createNextActionResumeItems(repository, customerId) {
  const workflows = typeof repository.listResumableAgentWorkflowRecordsInternal === 'function'
    ? await repository.listResumableAgentWorkflowRecordsInternal(customerId) : [];
  return workflows.slice(0, 3).map(workflow => ({
    scenarioId: 'resume-' + workflow.id,
    scenarioVersion: '1.0.0',
    type: 'resume_task',
    title: '继续办理未完成业务',
    priority: 'critical',
    reasonCode: 'WORKFLOW_RESUMABLE',
    reasonText: '有一项业务等待继续办理',
    action: 'RESUME_WORKFLOW',
    targetIntentId: null,
    workflowTemplateId: null,
    requiredSkillIds: [],
    requiredCapabilityIds: [],
    contextRefs: ['workflow:' + workflow.id],
    catalogAction: null,
  }));
}

function sendJson(res, status, body) {
  if (res.writableEnded) return;
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(body));
}

function nextMonthlyDueDate(dueDay, currentTime) {
  if (!Number.isInteger(dueDay) || dueDay < 1 || dueDay > 31
    || typeof currentTime !== 'string' || !Number.isFinite(Date.parse(currentTime))) {
    throw new BankApiError(422, 'obligation_input_invalid', '信用卡到期日无效');
  }
  const current = new Date(currentTime);
  let year = current.getUTCFullYear();
  let month = current.getUTCMonth();
  if (dueDay < current.getUTCDate()) {
    month += 1;
    if (month > 11) { month = 0; year += 1; }
  }
  const lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  return new Date(Date.UTC(year, month, Math.min(dueDay, lastDay))).toISOString().slice(0, 10);
}

let defaultServicesPromise;
function getDefaultServices() {
  if (!defaultServicesPromise) {
    defaultServicesPromise = createMockCoreRepository().then(repository => {
      const now = () => new Date().toISOString();
      const bankService = createBankService({ repository });
      const transferService = createTransferService({ repository, bankService });
      const executionEngine = createExecutionEngine({ transferService });
      const creditCardService = createCreditCardService({ repository, now });
      const wealthService = createWealthService({ repository, bankService, now });
      const loanService = createLoanService({ repository, bankService, wealthService, now });
      const utilityService = createUtilityService({ repository, bankService });
      const persona = (MOCK_PERSONAS.personas || []).find(item => item.customerId === 'cust_demo_001');
      const obligationService = createObligationService({
        now,
        accountProvider: async customerId => {
          const accounts = await bankService.listAccounts(customerId);
          const account = accounts.find(item => item.status === 'ACTIVE');
          if (!account) return null;
          const balance = await repository.getBalance(account.id);
          return { id: account.id, availableMinor: balance.availableMinor };
        },
        cardProvider: async customerId => {
          const cards = await creditCardService.listCards(customerId);
          return Promise.all(cards.map(async card => {
            const bill = await creditCardService.getBill(customerId, card.id);
            return {
              id: card.id,
              displayName: bill.productName + ' · ' + bill.maskedNo,
              amountMinor: bill.outstanding.amountMinor,
              dueAt: nextMonthlyDueDate(bill.dueDay, now()),
              executionStatus: bill.billStatus === 'PAYMENT_DUE' ? 'available' : 'unavailable',
            };
          })).then(items => items.filter(item => item.amountMinor > 0));
        },
        loanProvider: async customerId => {
          const [loanPage, repayments] = await Promise.all([
            loanService.listLoans(customerId), repository.listLoanRepayments(customerId),
          ]);
          const loans = new Map(loanPage.data.map(item => [item.id, item]));
          return repayments.filter(item => ['SCHEDULED', 'DUE', 'PENDING'].includes(item.status))
            .map(item => ({
              id: item.id,
              displayName: ((loans.get(item.loanId) || {}).productName || '贷款还款') + ' · 第 ' + item.periodNo + ' 期',
              amountMinor: item.totalMinor,
              dueAt: item.dueDate,
              executionStatus: 'available',
            }));
        },
        utilityProvider: async customerId => (await repository.listUtilityBills(customerId))
          .filter(item => item.status === 'UNPAID')
          .map(item => ({
            id: item.id,
            displayName: item.name + ' · ' + item.period,
            amountMinor: item.amountMinor,
            dueAt: item.dueDate,
            executionStatus: 'available',
          })),
      });
      const memoryControlClient = createAgentMemoryControlClient();
      const recommendationMemoryRecaller = createRecommendationMemoryRecaller({
        memoryControlClient, now,
      });
      const cashflowService = createCashflowService({
        now,
        policy: persona.cashflowPolicy,
        // 记忆召回的月度预算/应急资金优先（用户明示偏好），persona 静态策略兜底。
        // 个性化开关关闭时跳过记忆召回，与首页富化（next-action-service）口径一致。
        policyProvider: async customerId => {
          const preference = await repository.getNextActionPreference(customerId);
          if (preference.personalizationEnabled !== true) return null;
          const values = await recommendationMemoryRecaller.recallFundPlanningPolicy(customerId, [
            'monthly_living_budget', 'emergency_reserve_preference',
          ]);
          if (!values.size) return null;
          return {
            livingBudgetMinor: values.get('monthly_living_budget'),
            emergencyReserveMinor: values.get('emergency_reserve_preference'),
          };
        },
        salaryProvider: async customerId => {
          const source = (MOCK_PERSONAS.personas || []).find(item => item.customerId === customerId);
          const event = source && source.events.find(item => item.type === 'salary_credited');
          if (!event) throw new BankApiError(404, 'salary_event_not_found', '未找到最近工资入账');
          return { amountMinor: event.payload.amountMinor, currency: 'CNY', sourceRef: event.id };
        },
        supplementaryAccountProvider: async customerId => {
          // 补足来源：除主付款账户（首个 ACTIVE 账户，与 obligationService 的账户选择一致）外的其他客户账户
          const accounts = await bankService.listAccounts(customerId);
          const active = accounts.filter(item => item.status === 'ACTIVE' && item.type !== 'SYSTEM');
          if (active.length < 2) return [];
          return Promise.all(active.slice(1).map(async account => {
            const balance = await repository.getBalance(account.id);
            return {
              accountId: account.id,
              accountName: account.name,
              availableMinor: balance ? balance.availableMinor : 0,
              currency: account.currency || 'CNY',
            };
          }));
        },
        obligationProvider: async customerId => {
          const result = await obligationService.list(customerId);
          return {
            amountMinor: result.total.amountMinor,
            availableBalanceMinor: result.availableBalance.amountMinor,
            sourceRefs: result.items.map(item => item.id),
            items: result.items.map(item => ({
              id: item.id,
              type: item.type,
              displayName: item.displayName,
              amountMinor: item.amount.amountMinor,
              dueAt: item.dueAt,
              executionStatus: item.executionStatus,
              sourceRefs: item.sourceRefs,
            })),
          };
        },
        transactionProvider: async customerId => {
          const accounts = await bankService.listAccounts(customerId);
          const pages = await Promise.all(accounts.map(account => repository.listAccountTransactionDetails(customerId, account.id, { limit: 100 })));
          return pages.flatMap(page => page.data).map(item => ({
            id: item.id, amountMinor: item.amountMinor, occurredAt: item.occurredAt,
            category: typeof item.category === 'string' && /^[A-Za-z0-9_]{1,40}$/.test(item.category)
              ? item.category : 'OTHER',
            kind: item.businessType === 'TRANSFER' ? 'internal_transfer'
              : /REFUND|REVERSAL/.test(item.businessType) ? 'refund'
                : item.direction === 'DEBIT' ? 'expense' : 'income',
          }));
        },
      });
      const intentGraph = buildIntentGraph();
      const capabilityAssets = capabilityRegistry.list({ audience: 'all' });
      const skillManifest = require('../data/ai-native-bank/registries/skills.v1.json');
      const registry = loadExperienceRegistry({
        capabilities: capabilityAssets,
        intentGraph,
        skillIds: skillManifest.skills.filter(item => item.status === 'active').map(item => item.id),
        allowPlannedAssets: true,
      });
      const signalService = createNextActionSignalService({
        signalDefinitions: registry.signals,
        providers: createBuiltInSignalProviders({
          repository, bankService, wealthService, creditCardService, loanService, intentGraph, now,
          experienceEventProvider: async customerId => {
            const source = (MOCK_PERSONAS.personas || []).find(item => item.customerId === customerId);
            return source ? source.events : [];
          },
          edgeEventConsumer: async (customerId, event) => {
            await repository.recordExperienceEventConsumption({
              customerId, eventId: event.id, eventType: event.type, occurredAt: event.occurredAt,
            });
          },
        }),
        now,
      });
      const nextActionAssetProvider = async () => ({
        capabilityIds: capabilityAssets.filter(item => item.readiness.status === 'ready').map(item => item.id),
        skillIds: skillManifest.skills.filter(item => item.status === 'active').map(item => item.id),
      });
      // R4 反馈冷却（设计 §4.3）：30 天内同场景 dismiss ≥3 次进入冷却，引擎跳过该场景；
      // 近 30 天办结（completed）的场景同样冷却一次办理周期，避免刚办完即复现。
      const nextActionCooldownProvider = customerId => repository.listNextActionCooldownScenarioIds(customerId, {
        windowDays: 30, minimumDismissals: 3, completedWindowDays: 30,
      });
      // R1 双写（§4.1）：把"关闭个性化推荐"作为用户明示偏好写入记忆控制面
      // （recommendation_opt_out=true，deny_when_true 策略驱动读侧闸门）
      const nextActionMemoryPreferenceSink = async input => {
        const digest = sha256(String(input.sourceRef || ''));
        const occurredAt = now();
        return memoryControlClient.submitCandidate({
          schemaVersion: '1.0',
          idempotencyKey: 'nbaopt.' + digest.slice(0, 40),
          candidateType: 'user_preference',
          scope: {
            tenantId: 'bank',
            customerId: input.customerId,
            agentDomain: 'ai-native-bank',
            memorySpace: 'customer-service',
            purpose: 'service_personalization',
          },
          subjectRef: 'recommendation-consent',
          predicate: 'recommendation_opt_out',
          value: input.optOut === true,
          validFrom: occurredAt,
          source: {
            type: 'user_explicit',
            label: 'explicit personalization preference',
            sourceRef: 'nbaopt:src:' + digest.slice(0, 48),
            sourceRevision: '1',
          },
          evidence: {
            eventType: 'recommendation_opt_out_declared',
            occurredAt,
            safeSummary: '用户在推荐卡片上关闭个性化推荐，同步 recommendation_opt_out=true 偏好',
            contentHashShort: digest.slice(0, 12),
          },
          consentGranted: true,
          safetyStatus: 'allow',
        });
      };
      const nextActionService = createNextActionService({
        repository,
        signalService,
        engine: createNextBestActionEngine({ registry, now }),
        registry,
        now,
        assetProvider: nextActionAssetProvider,
        resumeProvider: customerId => createNextActionResumeItems(repository, customerId),
        memoryRecaller: recommendationMemoryRecaller,
        cooldownProvider: nextActionCooldownProvider,
        memoryPreferenceSink: nextActionMemoryPreferenceSink,
      });
      const recommendationArtifactProvider = createRecommendationArtifactProvider({
        cashflowService, obligationService, signalService, repository, now, wealthService,
      });
      const assistanceAdapter = createAssistanceAdapter({
        nextActionService,
        experienceRegistry: registry,
        signalService,
        repository,
        artifactProvider: recommendationArtifactProvider,
        assetProvider: nextActionAssetProvider,
        selectedActionContextProvider: async input => {
          if (input.targetIntentId !== 'ccb.obligation.pay-selected') return {
            trustedInputs: {}, contextRefs: [],
          };
          const obligations = await obligationService.list(input.customerId);
          const selected = obligations.items.find(item => (
            item.id === input.selectedItemId
            && item.type === input.selectedItemType
            && item.executionStatus === 'available'
          ));
          if (!selected) throw new BankApiError(
            409, 'service_action_unavailable', '所选账单当前不可办理'
          );
          const account = await bankService.getAccount(
            input.customerId, obligations.accountId,
          );
          if (account.status !== 'ACTIVE') {
            throw new BankApiError(
              409, 'service_action_unavailable', '所选账单当前不可办理'
            );
          }
          return {
            trustedInputs: {
              obligationId: selected.id,
              obligationType: selected.type,
            },
            contextRefs: [selected.id, account.id],
          };
        },
        now,
      });
      const recommendationService = createRecommendationService({
        nextActionService,
        experienceRegistry: registry,
        artifactProvider: recommendationArtifactProvider,
        repository,
        bankService,
        wealthService,
        creditCardService,
        loanService,
        now,
      });
      const agentSessionService = createAgentSessionService({
        repository,
        now,
        idleTtlMs: 30 * 60 * 1000,
        absoluteTtlMs: 8 * 60 * 60 * 1000,
      });
      const recommendationFocusService = createRecommendationFocusService({
        nextActionService,
        experienceRegistry: registry,
        artifactProvider: recommendationArtifactProvider,
        now,
      });
      const providerHealthRuntimeClient = createAiBankAgentRuntimeClient();
      const providerHealthService = createProviderHealthService({
        probeRuntime: () => providerHealthRuntimeClient.getStatus(),
        now,
        environment: 'mock',
      });
      const memoryWorkbenchEnabled = process.env.AI_BANK_MEMORY_WORKBENCH_ENABLED === 'true'
        || process.env.NODE_ENV !== 'production'
          && process.env.AI_BANK_MEMORY_WORKBENCH_ENABLED !== 'false';
      const memoryCommandWorker = createMemoryCommandWorker({
        repository, memoryControlClient,
      });
      const services = {
        repository, bankService, transferService, executionEngine, creditCardService,
        wealthService, loanService, utilityService, cashflowService, obligationService,
        recommendationService, nextActionService, assistanceAdapter, agentSessionService,
        recommendationFocusService,
        signalService,
        memoryPreferenceSink: nextActionMemoryPreferenceSink,
        providerHealthService,
        workbenchService: createWorkbenchService({ intentGraph, providerHealthService, experienceRegistry: registry, repository }),
        memoryWorkbenchService: createMemoryWorkbenchService({
          client: memoryControlClient,
          enabled: memoryWorkbenchEnabled,
          environment: process.env.NODE_ENV || 'mock',
        }),
        memoryCommandService: createMemoryCommandService({
          repository, worker: memoryCommandWorker,
        }),
        smartCompanionService: createSmartCompanionService({
          repository, workflowTemplates: registry.workflowTemplates, now,
        }),
      };
      const memoryRetryTimer = setInterval(() => {
        memoryCommandWorker.processDue().catch(error => {
          const code = typeof error?.code === 'string' ? error.code : 'memory_worker_failed';
          console.warn('[ai-native-bank:memory-worker] code=%s', code);
        });
      }, 5000);
      if (typeof memoryRetryTimer.unref === 'function') memoryRetryTimer.unref();
      services.agentCapabilityService = createAgentCapabilityService(services);
      return services;
    });
  }
  return defaultServicesPromise;
}

const handleAiNativeBankRequest = createAiNativeBankHandler({
  getServices: getDefaultServices,
  defaultCustomerId: 'cust_demo_001',
  allowedCustomerIds: ['cust_demo_001', 'cust_001'],
});

module.exports = {
  buildCompletedUserResult,
  createAiNativeBankHandler,
  handleAiNativeBankRequest,
  parseJsonBody,
  requireInternalToken,
  normalizeAgentRunRequest,
  buildConfirmationReplyPrompt,
  parseConfirmationReplyDecision,
  classifyConfirmationReply,
  deriveBusinessWorkflowId,
  conversationStateToRuntimeContext,
  projectAgentCapability,
  getDefaultServices,
};
