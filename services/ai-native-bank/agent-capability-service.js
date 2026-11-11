'use strict';

const crypto = require('node:crypto');
const Ajv = require('ajv');
const addFormats = require('ajv-formats');
const capabilityRegistry = require('./capability-registry');
const { BankApiError } = require('./api-contract');

const ACTIVE_AGENT_RUN_STATUSES = new Set([
  'accepted', 'interpreting', 'planning', 'executing',
  'awaiting_confirmation', 'clarification_required',
]);

// Machine-readable gateway coverage for the registration audit. A capability
// may only be marked Agent-enabled when it has one of these controlled paths.
const AGENT_CAPABILITY_BINDINGS = Object.freeze({
  readonly: Object.freeze([
    'bank.customer.profile.get',
    'bank.account.list',
    'bank.account.detail.get',
    'bank.account.balance.get',
    'bank.account.transactions.list',
    'bank.payee.list',
    'bank.payee.validate',
    'bank.transfer.status.get',
    'bank.transfer.receipt.get',
    'bank.credit-card.list',
    'bank.credit-card.detail.get',
    'bank.credit-card.transactions.list',
    'bank.credit-card.settings.get',
    'bank.credit-card.bill.get',
    'bank.wealth.product.list',
    'bank.wealth.product.detail.get',
    'bank.wealth.holding.list',
    'bank.wealth.transaction.list',
    'bank.wealth.salary-insights.get',
    'bank.wealth.recommendations.get',
    'bank.wealth.advisory.context.get',
    'bank.loan.list',
    'bank.loan.detail.get',
    'bank.loan.repayment.list',
    'bank.loan.precalculate.create',
    'bank.cashflow.salary-plan.get',
    'bank.cashflow.spending-analysis.get',
    'bank.obligation.list',
    'bank.utility.bill.list',
  ]),
  confirmed: Object.freeze([
    'bank.credit-card.activation.create',
    'bank.credit-card.loss-report.create',
    'bank.credit-card.limit-change.create',
    'bank.credit-card.security-settings.update',
    'bank.transfer.order.create',
    'bank.credit-card.repayment.create',
    'bank.wealth.purchase.create',
    'bank.wealth.redeem.create',
    'bank.loan.repayment.create',
    'bank.utility.payment.create',
  ]),
});

function createAgentCapabilityService(options) {
  const source = options || {};
  const repository = source.repository;
  const bankService = source.bankService;
  const transferService = source.transferService;
  const creditCardService = source.creditCardService;
  const executionEngine = source.executionEngine;
  const wealthService = source.wealthService;
  const loanService = source.loanService;
  const cashflowService = source.cashflowService;
  const obligationService = source.obligationService;
  const utilityService = source.utilityService;
  const registry = source.capabilityRegistry || capabilityRegistry;
  const now = source.now || (() => new Date().toISOString());
  const ajv = addFormats(new Ajv({ allErrors: true, strict: false }));
  const validators = new Map();

  if (!repository || !bankService || !transferService || !creditCardService || !executionEngine) {
    throw new TypeError('agent capability service dependencies are required');
  }

  const dispatch = Object.freeze({
    'bank.customer.profile.get': async (run) => {
      const customer = await bankService.getCustomer(run.customerId);
      return { id: customer.id, displayName: customer.name, mobileMasked: '', customerLevel: customer.status };
    },
    'bank.account.list': async run => ({ accounts: await bankService.listAccounts(run.customerId) }),
    'bank.account.detail.get': async (run, input) => bankService.getAccount(run.customerId, input.accountId),
    'bank.account.balance.get': async (run, input) => {
      const value = await bankService.getBalance(run.customerId, input.accountId);
      return { accountId: value.accountId, available: value.available, ledger: value.ledger, version: value.version, asOf: value.updatedAt };
    },
    'bank.account.transactions.list': async (run, input) => bankService.listTransactions(run.customerId, input.accountId, input),
    'bank.payee.list': async run => {
      const payees = await bankService.listPayees(run.customerId, { includeTemporary: true });
      return { payees };
    },
    'bank.payee.validate': async (run, input) => {
      const validation = await bankService.validatePayee(run.customerId, input.payeeId);
      return {
        payeeId: validation.payee.id,
        valid: validation.valid,
        status: validation.payee.status,
        bankName: validation.payee.bankName,
        maskedNo: validation.payee.maskedNo,
        checkedAt: validation.validatedAt,
      };
    },
    'bank.transfer.status.get': async (run, input) => transferService.getOrder(run.customerId, input.transferOrderId),
    'bank.transfer.receipt.get': async (run, input) => transferService.getExecution(run.customerId, input.executionId),
    'bank.credit-card.list': async run => ({ cards: await creditCardService.listCards(run.customerId) }),
    'bank.credit-card.detail.get': async (run, input) => creditCardService.getCard(run.customerId, input.cardId),
    'bank.credit-card.transactions.list': async (run, input) => creditCardService.listTransactions(run.customerId, input.cardId, input),
    'bank.credit-card.settings.get': async (run, input) => creditCardService.getSettings(run.customerId, input.cardId),
    'bank.credit-card.bill.get': async (run, input) => creditCardService.getBill(run.customerId, input.cardId),
    'bank.wealth.product.list': async (run, input) => ({
      products: (await wealthService.listProducts(input || {})).map(toAgentWealthProduct),
    }),
    'bank.wealth.product.detail.get': async (run, input) => toAgentWealthProduct(
      await wealthService.getProduct(input.productId)
    ),
    'bank.wealth.holding.list': async run => wealthService.listHoldings(run.customerId),
    'bank.wealth.transaction.list': async run => wealthService.listTransactions(run.customerId),
    'bank.wealth.salary-insights.get': async run => wealthService.getSalaryInsights(run.customerId),
    'bank.wealth.recommendations.get': async run => wealthService.getRecommendations(run.customerId),
    'bank.wealth.advisory.context.get': async (run, input) => wealthService.getAdvisoryContext(run.customerId, input),
    'bank.loan.list': async run => loanService.listLoans(run.customerId),
    'bank.loan.detail.get': async (run, input) => loanService.getLoan(run.customerId, input.loanId),
    'bank.loan.repayment.list': async (run, input) => loanService.listRepayments(run.customerId, input.loanId),
    'bank.loan.precalculate.create': async (run, input) => {
      const syntheticActor = { type: 'agent', id: run.id, customerId: run.customerId, traceId: run.traceId };
      return loanService.precalculate(syntheticActor, input, 'invoke:' + run.id + ':precalculate');
    },
    'bank.cashflow.salary-plan.get': async run => cashflowService.getSalaryPlan(run.customerId),
    'bank.cashflow.spending-analysis.get': async (run, input) => cashflowService.getSpendingAnalysis(run.customerId, input),
    'bank.obligation.list': async run => obligationService.list(run.customerId),
    'bank.utility.bill.list': async run => utilityService.listBills(run.customerId),
  });

  function validator(capability, direction) {
    const key = capability.id + ':' + direction;
    if (!validators.has(key)) validators.set(key, ajv.compile(direction === 'input' ? capability.inputSchema : capability.outputSchema));
    return validators.get(key);
  }

  // 模型侧 invoke_read_capability 是泛化工具（自由 input 对象），模型只能
  // 猜测各能力的入参契约；422 只回笼统文案时模型拿不到任何修正线索，
  // 会在同一槽位反复空转（如 payee.validate 传 payeeName 而契约要求 payeeId）。
  // 这里把契约差异（缺哪些必填、多哪些未支持字段、可用字段全集）写进 message，
  // 由 runtime 透传成工具层 guidance，模型一次失败即可自纠。
  function inputContractHint(capability, input) {
    const schema = capability.inputSchema || {};
    const props = Object.keys(schema.properties || {});
    const required = schema.required || [];
    const received = Object.keys(input || {});
    const parts = [];
    const missing = required.filter((key) => !received.includes(key));
    if (missing.length) parts.push('缺少必填字段: ' + missing.join(', '));
    const extra = received.filter((key) => !props.includes(key));
    if (extra.length) parts.push('不支持的字段: ' + extra.join(', '));
    if (!parts.length) parts.push('字段值类型或格式不符合契约');
    return parts.join('；') + '；该能力可用字段: ' + (props.join(', ') || '无');
  }

  async function invoke(request) {
    const body = exactInvocation(request);
    const run = await repository.getAgentRun(body.agentRunId);
    if (!run) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
    if (!ACTIVE_AGENT_RUN_STATUSES.has(run.status)) {
      throw new BankApiError(409, 'agent_run_not_active', 'Agent Run 当前不可调用能力');
    }
    const capability = registry.get(body.capabilityId);
    if (!capability || capability.readiness.status !== 'ready') {
      throw new BankApiError(404, 'capability_not_found', '能力不存在或未就绪');
    }
    if (!run.executableCapabilityIds.includes(capability.id)) {
      throw new BankApiError(403, 'capability_not_bound', '能力未绑定到当前 Agent Run');
    }
    if (capability.exposure !== 'model-callable' || capability.sideEffect !== 'none') {
      throw new BankApiError(403, 'capability_not_readonly', '业务智能体只能调用只读能力');
    }
    const execute = dispatch[capability.id];
    if (!execute) throw new BankApiError(403, 'capability_dispatch_forbidden', '能力没有受控分派实现');
    if (!validator(capability, 'input')(body.input)) {
      throw new BankApiError(422, 'capability_input_invalid', '能力输入未通过契约校验（' + inputContractHint(capability, body.input) + '）');
    }

    const raw = normalizeContractValues(await execute(run, body.input));
    const output = projectSchemaValue(raw, capability.outputSchema);
    if (!validator(capability, 'output')(output)) {
      throw new BankApiError(502, 'capability_output_invalid', '能力输出未通过契约校验');
    }
    await repository.recordCapabilityInvocation({
      id: 'aci_' + crypto.randomUUID(),
      capabilityId: capability.id,
      actorType: 'agent',
      actorId: run.id,
      status: 'SUCCEEDED',
      inputHash: sha256(body.input),
      output: { outputKeys: Object.keys(output).sort() },
      traceId: run.traceId,
      createdAt: now(),
    });
    return output;
  }

  async function createCheckpoint(request) {
    const body = exactCheckpointRequest(request);
    const run = await repository.getAgentRun(body.agentRunId);
    if (!run) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
    if (!ACTIVE_AGENT_RUN_STATUSES.has(run.status)) {
      throw new BankApiError(409, 'agent_run_not_active', 'Agent Run 当前不可创建确认检查点');
    }
    const capability = registry.get(body.capabilityId);
    if (!capability || capability.readiness.status !== 'ready') {
      throw new BankApiError(404, 'capability_not_found', '能力不存在或未就绪');
    }
    if (!run.plannableCapabilityIds.includes(capability.id)) {
      throw new BankApiError(403, 'capability_not_bound', '能力未绑定到当前 Agent Run');
    }
    const confirmableTransfer = capability.id === 'bank.transfer.order.create';
    if ((!confirmableTransfer && capability.exposure !== 'user-action')
      || !['state-change', 'money-movement'].includes(capability.sideEffect)) {
      throw new BankApiError(403, 'capability_not_confirmable', '能力不能创建本人确认检查点');
    }
    if (!validator(capability, 'input')(body.input)) {
      throw new BankApiError(422, 'capability_input_invalid', '能力输入未通过契约校验（' + inputContractHint(capability, body.input) + '）');
    }
    let workflow = null;
    if (body.businessWorkflowId) {
      workflow = await repository.getAgentWorkflow(
        body.businessWorkflowId, run.conversationId, run.customerId,
      );
      if (!workflow || !workflow.delegatedTaskIds.includes(body.taskId)) {
        throw new BankApiError(404, 'agent_workflow_not_found', 'Agent Workflow 或业务目标不存在');
      }
    }
    await validateCheckpointOwnership(run.customerId, capability.id, body.input);
    const checkpointId = 'acp_' + crypto.randomUUID().replace(/-/g, '');
    const confirmationToken = crypto.randomBytes(32).toString('base64url');
    const createdAt = new Date(now());
    const expiresAt = new Date(createdAt.getTime() + 5 * 60 * 1000).toISOString();
    await repository.createActionCheckpoint({
      id: checkpointId,
      agentRunId: run.id,
      customerId: run.customerId,
      businessWorkflowId: body.businessWorkflowId,
      taskId: body.taskId,
      capabilityId: capability.id,
      input: body.input,
      inputHash: sha256(body.input),
      summary: body.summary,
      tokenHash: sha256(confirmationToken),
      idempotencyKey: 'agent-checkpoint:' + checkpointId,
      expiresAt,
    });
    return {
      checkpointId,
      capabilityId: capability.id,
      confirmationToken,
      expiresAt,
      summary: body.summary,
    };
  }

  async function validateCheckpointOwnership(customerId, capabilityId, input) {
    if (capabilityId.startsWith('bank.credit-card.')) {
      await creditCardService.getCard(customerId, input.cardId);
      return;
    }
    if (capabilityId === 'bank.transfer.order.create') {
      await bankService.getAccount(customerId, input.fromAccountId);
      await bankService.validatePayee(customerId, input.payeeId);
      return;
    }
    if (capabilityId === 'bank.wealth.purchase.create') {
      await bankService.getAccount(customerId, input.accountId);
      return;
    }
    if (capabilityId === 'bank.wealth.redeem.create') {
      const holdings = await wealthService.listHoldings(customerId);
      if (!holdings.holdings.some(h => h.id === input.holdingId)) {
        throw new BankApiError(404, 'not_found', '持仓不存在');
      }
      return;
    }
    if (capabilityId === 'bank.loan.repayment.create') {
      const repayments = await repository.listLoanRepayments(customerId);
      if (!repayments.some(item => item.id === input.repaymentId)) throw new BankApiError(404, 'not_found', '贷款还款计划不存在');
      await bankService.getAccount(customerId, input.fromAccountId);
      return;
    }
    if (capabilityId === 'bank.utility.payment.create') {
      const bills = await repository.listUtilityBills(customerId);
      if (!bills.some(item => item.id === input.billId)) throw new BankApiError(404, 'not_found', '生活缴费账单不存在');
      await bankService.getAccount(customerId, input.accountId);
      return;
    }
    if (input.transferOrderId) {
      await transferService.getOrder(customerId, input.transferOrderId);
    }
  }

  async function confirm(request) {
    const body = exactConfirmationRequest(request);
    console.log('[confirm-audit] POST /confirm', JSON.stringify({
      checkpointId: body.checkpointId, agentRunId: body.agentRunId,
      actorType: body.actorType, idempotencyKey: body.idempotencyKey || null,
      at: new Date().toISOString(),
    }));
    if (body.actorType !== 'user') throw new BankApiError(403, 'forbidden', '确认操作必须由客户本人发起');
    const run = await repository.getAgentRun(body.agentRunId);
    if (!run || run.customerId !== body.customerId) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
    const checkpoint = await repository.getActionCheckpointForExecution(body.checkpointId);
    if (!checkpoint || checkpoint.agentRunId !== run.id || checkpoint.customerId !== body.customerId) {
      throw new BankApiError(404, 'checkpoint_not_found', '确认检查点不存在');
    }
    if (!safeEqual(checkpoint.tokenHash, sha256(body.confirmationToken))) {
      throw new BankApiError(403, 'invalid_confirmation_token', '确认凭证无效');
    }
    if (checkpoint.status !== 'SUCCEEDED' && new Date(checkpoint.expiresAt).getTime() <= new Date(now()).getTime()) {
      if (checkpoint.status === 'PENDING') await repository.completeActionCheckpoint(checkpoint.id, 'EXPIRED', { errorCode: 'confirmation_expired' });
      throw new BankApiError(410, 'confirmation_expired', '确认已过期，请重新发起');
    }
    return authorizeValidatedCheckpoint(checkpoint, run);
  }

  // 确认方式（token 或文本答复）已验证通过后的共用授权/执行逻辑
  async function authorizeValidatedCheckpoint(checkpoint, run) {
    if (checkpoint.businessWorkflowId && checkpoint.taskId) {
      const authorization = await repository.authorizeWorkflowCheckpoint(
        checkpoint.id,
        checkpoint.customerId,
        {
          id: 'acg_' + crypto.randomUUID().replace(/-/g, ''),
          businessWorkflowId: checkpoint.businessWorkflowId,
          taskId: checkpoint.taskId,
          capabilityId: checkpoint.capabilityId,
          inputHash: checkpoint.inputHash,
          checkpointId: checkpoint.id,
          expiresAt: checkpoint.expiresAt,
        },
      );
      const grant = authorization.grant;
      return {
        checkpointId: checkpoint.id,
        businessWorkflowId: checkpoint.businessWorkflowId,
        status: 'AUTHORIZED',
        grants: [{
          grantId: grant.id,
          capabilityId: grant.capabilityId,
          taskId: grant.taskId,
          expiresAt: grant.expiresAt,
        }],
      };
    }
    const claimed = await repository.claimActionCheckpoint(checkpoint.id, checkpoint.customerId);
    if (claimed.replay) return claimed.checkpoint.result;

    let result;
    try {
      await advanceRunForConfirmation(run.id, 'executing');
      result = await executeConfirmed(run, claimed.checkpoint);
    } catch (error) {
      await Promise.allSettled([
        repository.completeActionCheckpoint(checkpoint.id, 'FAILED', { errorCode: safeErrorCode(error) }),
        repository.recordCapabilityInvocation({
          id: 'aci_' + crypto.randomUUID(), capabilityId: checkpoint.capabilityId,
          actorType: 'user', actorId: checkpoint.customerId, status: 'FAILED',
          inputHash: checkpoint.inputHash, output: { errorCode: safeErrorCode(error) },
          traceId: run.traceId, createdAt: now(),
        }),
        advanceRunForConfirmation(run.id, 'failed'),
      ]);
      throw error;
    }

    const traceEvents = buildConfirmationTraceEvents(run, checkpoint.capabilityId, now());
    const response = { checkpointId: checkpoint.id, capabilityId: checkpoint.capabilityId, status: 'SUCCEEDED', result, traceEvents };
    await repository.completeActionCheckpoint(checkpoint.id, 'SUCCEEDED', response);

    // The checkpoint result is the authoritative business outcome. Telemetry and
    // lifecycle projections are best-effort after that commit and must never turn
    // an already executed mutation into a false failure response.
    const postExecution = [
      repository.recordCapabilityInvocation({
        id: 'aci_' + crypto.randomUUID(), capabilityId: checkpoint.capabilityId,
        actorType: 'user', actorId: checkpoint.customerId, status: 'SUCCEEDED',
        inputHash: checkpoint.inputHash, output: { outputKeys: Object.keys(result).sort() },
        traceId: run.traceId, createdAt: now(),
      }),
      advanceRunForConfirmation(run.id, 'succeeded'),
    ];
    if (repository.updateAgentRunTraceSequence) {
      postExecution.push(repository.updateAgentRunTraceSequence(run.id, traceEvents[traceEvents.length - 1].sequence));
    }
    await Promise.allSettled(postExecution);
    return response;
  }

  // 确认卡出现后客户以文本答复（如「只还信用贷款」「就按这个办」）同意办理：
  // 与按钮确认同等授权，凭证换为已验证的语义判定结论（答复文本已作为会话轮次留存）。
  async function confirmByReply(request) {
    const body = exactConfirmationReplyRequest(request);
    console.log('[confirm-audit] confirmByReply', JSON.stringify({
      checkpointId: body.checkpointId, agentRunId: body.agentRunId,
      actorType: body.actorType, replyEvidence: body.replyEvidence || null,
      at: new Date().toISOString(),
    }));
    if (body.actorType !== 'user') throw new BankApiError(403, 'forbidden', '确认操作必须由客户本人发起');
    const run = await repository.getAgentRun(body.agentRunId);
    if (!run || run.customerId !== body.customerId) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
    const checkpoint = await repository.getActionCheckpointForExecution(body.checkpointId);
    if (!checkpoint || checkpoint.agentRunId !== run.id || checkpoint.customerId !== body.customerId) {
      throw new BankApiError(404, 'checkpoint_not_found', '确认检查点不存在');
    }
    if (checkpoint.status !== 'SUCCEEDED' && new Date(checkpoint.expiresAt).getTime() <= new Date(now()).getTime()) {
      if (checkpoint.status === 'PENDING') await repository.completeActionCheckpoint(checkpoint.id, 'EXPIRED', { errorCode: 'confirmation_expired' });
      throw new BankApiError(410, 'confirmation_expired', '确认已过期，请重新发起');
    }
    return authorizeValidatedCheckpoint(checkpoint, run);
  }

  async function invokeAuthorized(request) {
    const body = exactAuthorizedInvocation(request);
    const run = await repository.getAgentRun(body.agentRunId);
    if (!run) throw new BankApiError(404, 'agent_run_not_found', 'Agent Run 不存在');
    const workflow = await repository.getAgentWorkflow(
      body.businessWorkflowId, run.conversationId, run.customerId,
    );
    if (!workflow || !workflow.delegatedTaskIds.includes(body.taskId)) {
      throw new BankApiError(404, 'agent_workflow_not_found', 'Agent Workflow 或业务目标不存在');
    }
    const grantRecord = await repository.getCapabilityGrant(body.grantId);
    if (!grantRecord || grantRecord.customerId !== run.customerId
      || grantRecord.businessWorkflowId !== workflow.id
      || grantRecord.taskId !== body.taskId) {
      throw new BankApiError(404, 'capability_grant_not_found', '能力授权不存在');
    }
    const checkpoint = await repository.getActionCheckpointForExecution(
      grantRecord.checkpointId,
    );
    if (!checkpoint || checkpoint.agentRunId !== run.id
      || checkpoint.inputHash !== grantRecord.inputHash
      || checkpoint.capabilityId !== grantRecord.capabilityId) {
      throw new BankApiError(409, 'capability_grant_binding_conflict', '能力授权绑定不一致');
    }
    const claimed = await repository.claimCapabilityGrant(body.grantId, {
      customerId: run.customerId,
      businessWorkflowId: workflow.id,
      taskId: body.taskId,
      capabilityId: grantRecord.capabilityId,
    });
    if (claimed.replay) return claimed.grant.receipt;
    try {
      await advanceRunForConfirmation(run.id, 'executing');
      const result = await executeConfirmed(run, checkpoint);
      const receipt = {
        grantId: grantRecord.id,
        taskId: grantRecord.taskId,
        capabilityId: grantRecord.capabilityId,
        status: 'SUCCEEDED',
        result,
      };
      await repository.completeCapabilityGrant(grantRecord.id, 'CONSUMED', receipt);
      await repository.completeActionCheckpoint(checkpoint.id, 'SUCCEEDED', receipt);
      await Promise.allSettled([
        repository.recordCapabilityInvocation({
          id: 'aci_' + crypto.randomUUID(), capabilityId: checkpoint.capabilityId,
          actorType: 'agent', actorId: run.id, status: 'SUCCEEDED',
          inputHash: checkpoint.inputHash, output: { outputKeys: Object.keys(result).sort() },
          traceId: run.traceId, createdAt: now(),
        }),
      ]);
      return receipt;
    } catch (error) {
      const receipt = {
        grantId: grantRecord.id,
        taskId: grantRecord.taskId,
        capabilityId: grantRecord.capabilityId,
        status: 'FAILED',
        errorCode: safeErrorCode(error),
      };
      await Promise.allSettled([
        repository.completeCapabilityGrant(grantRecord.id, 'FAILED', receipt),
        repository.completeActionCheckpoint(checkpoint.id, 'FAILED', receipt),
        repository.recordCapabilityInvocation({
          id: 'aci_' + crypto.randomUUID(), capabilityId: checkpoint.capabilityId,
          actorType: 'agent', actorId: run.id, status: 'FAILED',
          inputHash: checkpoint.inputHash,
          output: { errorCode: receipt.errorCode },
          traceId: run.traceId, createdAt: now(),
        }),
      ]);
      // A business failure is an authoritative per-task receipt, not a gateway
      // failure. Returning it lets the resumed Agent loop continue independent
      // tasks and aggregate partial_success without replaying successful grants.
      return receipt;
    }
  }

  async function advanceRunForConfirmation(agentRunId, target) {
    let current = await repository.getAgentRun(agentRunId);
    if (!current || ['succeeded', 'failed'].includes(current.status)) return;
    if (target === 'executing') {
      const paths = {
        accepted: ['interpreting', 'planning', 'executing'],
        interpreting: ['planning', 'executing'],
        planning: ['executing'], awaiting_confirmation: ['executing'], executing: [],
      };
      for (const status of paths[current.status] || []) {
        current = await repository.transitionAgentRun(agentRunId, status);
      }
      return;
    }
    if (current.status === 'executing') await repository.transitionAgentRun(agentRunId, target);
  }

  async function executeConfirmed(run, checkpoint) {
    const actor = { type: 'user', id: run.customerId, customerId: run.customerId, traceId: run.traceId };
    const agentActor = { type: 'agent', id: run.id, customerId: run.customerId, traceId: run.traceId };
    const input = checkpoint.input;
    const key = checkpoint.idempotencyKey;
    switch (checkpoint.capabilityId) {
      case 'bank.credit-card.activation.create':
        return creditCardService.activate(actor, input.cardId, key);
      case 'bank.credit-card.loss-report.create':
        return creditCardService.reportLoss(actor, input.cardId, { reason: input.reason }, key);
      case 'bank.credit-card.limit-change.create':
        return creditCardService.requestLimitChange(actor, input.cardId, { requestedLimitMinor: input.requestedLimitMinor }, key);
      case 'bank.credit-card.security-settings.update': {
        const settings = Object.assign({}, input);
        delete settings.cardId;
        return creditCardService.updateSecuritySettings(actor, input.cardId, settings, key);
      }
      case 'bank.transfer.order.create': {
        const order = await transferService.createOrder(agentActor, input, key + ':order');
        await transferService.validateOrder(agentActor, order.id, key + ':validate');
        const challenge = await transferService.createConfirmationChallenge(agentActor, order.id, key + ':challenge');
        const authorization = await transferService.authorize(actor, order.id, challenge.id, challenge.confirmationToken, key + ':authorize');
        executionEngine.scheduleAuthorization(Object.assign({}, authorization, { traceId: run.traceId }));
        const execution = await executionEngine.waitForAuthorization(authorization.id);
        if (!execution) throw new BankApiError(502, 'transfer_execution_failed', '转账执行失败，请查询交易状态');
        return { order: await transferService.getOrder(run.customerId, order.id), receipt: await transferService.getExecution(run.customerId, execution.id) };
      }
      case 'bank.credit-card.repayment.create':
        return creditCardService.repay(actor, input.cardId, { fromAccountId: input.fromAccountId, amountMinor: input.amountMinor }, key);
      case 'bank.wealth.purchase.create':
        return wealthService.purchase(actor, { productId: input.productId, accountId: input.accountId, amountMinor: input.amountMinor }, key);
      case 'bank.wealth.redeem.create':
        return wealthService.redeem(actor, { holdingId: input.holdingId }, key);
      case 'bank.loan.repayment.create':
        return loanService.repay(actor, input, key);
      case 'bank.utility.payment.create':
        return utilityService.pay(actor, input, key);
      default:
        throw new BankApiError(403, 'capability_dispatch_forbidden', '该能力没有受控确认执行实现');
    }
  }

  return { invoke, createCheckpoint, confirm, confirmByReply, invokeAuthorized };
}

function exactInvocation(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalidInvocation();
  const keys = Object.keys(value);
  const allowed = new Set(['agentRunId', 'capabilityId', 'input']);
  if (keys.some(key => !allowed.has(key))) invalidInvocation();
  for (const key of ['agentRunId', 'capabilityId']) {
    if (typeof value[key] !== 'string' || !value[key] || value[key].length > 128) invalidInvocation();
  }
  if (!value.input || typeof value.input !== 'object' || Array.isArray(value.input)) invalidInvocation();
  return { agentRunId: value.agentRunId, capabilityId: value.capabilityId, input: value.input };
}

function invalidInvocation() {
  throw new BankApiError(422, 'validation_error', '内部能力调用请求不合法');
}

function exactCheckpointRequest(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalidInvocation();
  const allowed = new Set([
    'agentRunId', 'businessWorkflowId', 'taskId', 'capabilityId', 'input', 'summary',
  ]);
  if (Object.keys(value).some(key => !allowed.has(key))) invalidInvocation();
  const invocation = exactInvocation({
    agentRunId: value.agentRunId,
    capabilityId: value.capabilityId,
    input: value.input,
  });
  const summary = value.summary;
  if (!summary || typeof summary !== 'object' || Array.isArray(summary)) invalidInvocation();
  const summaryKeys = new Set([
    'title', 'description', 'riskLevel', 'items', 'taskCount', 'totalAmountMinor',
  ]);
  if (Object.keys(summary).some(key => !summaryKeys.has(key))) invalidInvocation();
  if (typeof summary.title !== 'string' || !summary.title || summary.title.length > 80
    || typeof summary.description !== 'string' || !summary.description || summary.description.length > 200
    || !['L0', 'L1', 'L2', 'L3'].includes(summary.riskLevel)) invalidInvocation();
  const hasTaskSummary = summary.items != null
    || summary.taskCount != null || summary.totalAmountMinor != null;
  if (hasTaskSummary && (
    !Array.isArray(summary.items)
    || summary.items.length < 1 || summary.items.length > 16
    || !Number.isInteger(summary.taskCount)
    || (summary.taskCount > 1 && summary.taskCount !== summary.items.length)
    || (summary.taskCount > 1 && summary.totalAmountMinor == null)
    || (summary.totalAmountMinor != null && (
      !Number.isInteger(summary.totalAmountMinor)
      || summary.totalAmountMinor < 1 || summary.totalAmountMinor > 800000000
    ))
    || summary.items.some(item => (
      typeof item !== 'string' || !item || item.length > 120
      || /[<>\u0000-\u001f\u007f]/.test(item)
      || /\d(?:[\s-]?\d){14,}/.test(item)
    ))
  )) invalidInvocation();
  const hasWorkflow = value.businessWorkflowId != null || value.taskId != null;
  if (hasWorkflow && (
    typeof value.businessWorkflowId !== 'string'
    || !/^[A-Za-z0-9_.:-]{1,160}$/.test(value.businessWorkflowId)
    || typeof value.taskId !== 'string'
    || !/^[A-Za-z0-9_.:-]{1,96}$/.test(value.taskId)
  )) invalidInvocation();
  return Object.assign(invocation, {
    businessWorkflowId: hasWorkflow ? value.businessWorkflowId : null,
    taskId: hasWorkflow ? value.taskId : null,
    summary: {
      title: summary.title,
      description: summary.description,
      riskLevel: summary.riskLevel,
      ...(hasTaskSummary ? {
        items: [...summary.items],
        taskCount: summary.taskCount,
        ...(summary.totalAmountMinor == null
          ? {} : { totalAmountMinor: summary.totalAmountMinor }),
      } : {}),
    } });
}

function exactConfirmationRequest(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalidInvocation();
  const allowed = new Set(['agentRunId', 'checkpointId', 'confirmationToken', 'customerId', 'actorType']);
  if (Object.keys(value).some(key => !allowed.has(key))) invalidInvocation();
  for (const key of allowed) {
    if (typeof value[key] !== 'string' || !value[key] || value[key].length > 256) invalidInvocation();
  }
  return value;
}

function exactConfirmationReplyRequest(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalidInvocation();
  const allowed = new Set(['agentRunId', 'checkpointId', 'customerId', 'actorType', 'replyEvidence']);
  if (Object.keys(value).some(key => !allowed.has(key))) invalidInvocation();
  for (const key of allowed) {
    if (typeof value[key] !== 'string' || !value[key] || value[key].length > 256) invalidInvocation();
  }
  return value;
}

function exactAuthorizedInvocation(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalidInvocation();
  const allowed = new Set(['agentRunId', 'businessWorkflowId', 'taskId', 'grantId']);
  if (Object.keys(value).length !== allowed.size
    || Object.keys(value).some(key => !allowed.has(key))) invalidInvocation();
  if (!/^[A-Za-z0-9_.:-]{1,160}$/.test(value.agentRunId || '')
    || !/^[A-Za-z0-9_.:-]{1,160}$/.test(value.businessWorkflowId || '')
    || !/^[A-Za-z0-9_.:-]{1,96}$/.test(value.taskId || '')
    || !/^acg_[A-Za-z0-9_-]{1,96}$/.test(value.grantId || '')) invalidInvocation();
  return value;
}

function normalizeContractValues(value) {
  if (Array.isArray(value)) return value.map(normalizeContractValues);
  if (!value || typeof value !== 'object') return value;
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    result[key === 'display' ? 'formatted' : key] = normalizeContractValues(item);
  }
  return result;
}

function toAgentWealthProduct(product) {
  const features = product && product.features;
  return Object.assign({}, product, {
    termDays: Number.isInteger(product && product.termDays) ? product.termDays : 0,
    // The public product catalogue uses null when a product has no meaningful
    // annualised rate (for example net-value funds and card services).  The
    // Agent contract deliberately stays scalar so the resolver can compare
    // catalogue rows without a nullable branch; zero means "not applicable",
    // not a promised yield.
    annualRateBp: Number.isInteger(product && product.annualRateBp)
      ? product.annualRateBp : 0,
    features: Array.isArray(features)
      ? features.filter(item => typeof item === 'string')
      : Object.keys(features || {}).sort(),
  });
}

function projectSchemaValue(value, schema) {
  if (value == null || !schema) return value;
  if (Array.isArray(value)) return value.map(item => projectSchemaValue(item, schema.items));
  if (typeof value !== 'object' || schema.type !== 'object') return value;
  const result = {};
  for (const [key, childSchema] of Object.entries(schema.properties || {})) {
    if (Object.prototype.hasOwnProperty.call(value, key)) result[key] = projectSchemaValue(value[key], childSchema);
  }
  return result;
}

function sha256(value) {
  return 'sha256:' + crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
}

function safeEqual(left, right) {
  const a = Buffer.from(String(left));
  const b = Buffer.from(String(right));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function safeErrorCode(error) {
  return error && /^[a-z0-9_]+$/.test(String(error.code || '')) ? String(error.code) : 'internal_error';
}

function buildConfirmationTraceEvents(run, capabilityId, timestamp) {
  const definitions = [
    ['confirmation.checkpoint_accepted', '本人确认已接收', { capabilityId, outcome: 'accepted' }],
    ['capability.execution_completed', '受控业务执行完成', { capabilityId, outcome: 'succeeded' }],
    ['run.completed', '智能服务运行完成', { status: 'succeeded' }],
  ];
  return definitions.map((item, index) => ({
    schemaVersion: '1.0', sequence: run.lastTraceSequence + index + 1, timestamp,
    stage: item[0], status: 'completed', agentRunId: run.id, traceId: run.traceId,
    summary: { title: item[1], input: [], output: [], metrics: item[2] },
  }));
}

module.exports = { AGENT_CAPABILITY_BINDINGS, createAgentCapabilityService };
