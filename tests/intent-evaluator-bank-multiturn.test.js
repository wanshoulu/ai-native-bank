const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

// 通过 require.cache 注入桩件：不依赖真实运行时/能力目录/埋点落盘
const ROOT = path.join(__dirname, '..');
function stub(modulePath, exports) {
  const resolved = require.resolve(path.join(ROOT, modulePath));
  require.cache[resolved] = { id: resolved, filename: resolved, loaded: true, exports };
}

const calls = []; // 每次 createAgentRun 的 input 记录
const resumes = []; // 每次 resumeBusinessWorkflow 的 (id, input) 记录
const released = [];
const createdRuns = []; // Node 侧落库的 run 记录
const convTurnCounter = new Map(); // conversationId -> 已调用次数（每个会话首轮回澄清）
const probes = []; // 每次 probeIntentEvaluation 的 input 记录（read_only 模式专用）
stub('services/ai-native-bank/agent-runtime-client.js', {
  createAiBankAgentRuntimeClient: () => ({
    async createAgentRun(input) {
      calls.push(input);
      const turnNo = (convTurnCounter.get(input.conversationId) || 0) + 1;
      convTurnCounter.set(input.conversationId, turnNo);
      if (turnNo === 1) {
        return {
          delivery: {
            agentRunId: input.agentRunId,
            status: 'clarification_required',
            userResponse: {
              schemaVersion: '1.0', type: 'clarification', title: '请确认转账方式',
              body: '请选择转账方式', source: 'runtime',
              actions: [
                { id: 'a1', label: '手机号转账', kind: 'reply', payload: { message: '用手机号转账' }, description: '按手机号' },
                { id: 'a2', label: '银行卡转账', kind: 'reply', payload: { message: '用银行卡转账' }, description: '按卡号' },
              ],
            },
            clarificationForm: {
              kind: 'transfer_full', clarificationId: 'clf_1', submissionToken: 'tok_secret',
              fields: [
                { name: 'payeeName', label: '收款人', required: true, value: '' },
                { name: 'amount', label: '转账金额（元）', required: true, value: '' },
              ],
            },
          },
          conversationState: {
            schemaVersion: '2.0',
            status: 'retain', pendingIntentId: 'intent.transfer', pendingEnvelopeId: 'env1', envelopeRevision: 1,
            intentShape: 'single_intent',
            goals: [{ taskId: 't1', intentId: 'intent.transfer', status: 'needs_slots', slots: [] }],
            sharedSlots: {}, ambiguities: [],
          },
        };
      }
      return {
        delivery: { agentRunId: input.agentRunId, status: 'succeeded', userResponse: { schemaVersion: '1.0', type: 'result', title: '已转账', body: '转账完成' } },
        conversationState: {
          schemaVersion: '2.0',
          status: 'retain', intentShape: 'single_intent',
          goals: [{ taskId: 't1', intentId: 'intent.transfer', status: 'ready', slots: [{ name: 'amount', value: '100' }] }],
          sharedSlots: { amount: '100' }, ambiguities: [],
        },
      };
    },
    async resumeBusinessWorkflow(businessWorkflowId, input) {
      resumes.push({ businessWorkflowId, input });
      return {
        status: 'succeeded',
        userResponse: { schemaVersion: '1.0', type: 'result', title: '已转账', body: '转账完成' },
        conversationState: {
          schemaVersion: '2.0',
          status: 'retain', intentShape: 'single_intent',
          goals: [{ taskId: 't1', intentId: 'intent.transfer', status: 'ready', slots: [{ name: 'amount', value: '100' }] }],
          sharedSlots: {}, ambiguities: [],
        },
      };
    },
    async releaseAgentSessions(ids) { released.push(...ids); },
    async probeIntentEvaluation(input) {
      probes.push(input);
      return {
        graphVersion: '2.1.0-test',
        graphDigest: 'sha256:test',
        judgeEnabled: false,
        shape: { shape: 'single_intent', taskCount: 1 },
        constraints: [
          {
            intentId: 'intent.transfer',
            intentName: '转账',
            actionCode: 'action.transfer',
            score: 95,
            evidence: ['转账关键词'],
            shapeAuthoritative: true,
            unresolvedSlotReadiness: 'needs_slots',
            disambiguationHints: '',
          },
        ],
        recallChannels: {
          lexical: { status: 'completed', candidates: [{ intentId: 'intent.transfer', score: 95 }] },
          vector: { status: 'skipped', candidates: [] },
          subgraph: { status: 'completed', candidates: [{ intentId: 'intent.transfer', score: 50 }] },
        },
        judgeResults: null,
        widened: false,
        faq: { matchedIntentIds: [] },
        timings: { shapeMs: 5, recallMs: 10, widenMs: 0, judgeMs: 0 },
      };
    },
  }),
});
stub('services/ai-native-bank/capability-registry.js', {
  projectForAgentRuntime: () => ({ plannableCapabilities: [{ id: 'cap.transfer' }], executableCapabilities: [] }),
});
stub('routes/ai-native-bank-api.js', {
  conversationStateToRuntimeContext: (state) => ({ fromState: true, goals: state.goals }),
  projectAgentCapability: (cap) => cap,
  deriveBusinessWorkflowId: (customerId, conversationId, envelopeId) => `bwf_stub_${envelopeId}`,
  getDefaultServices: async () => ({
    repository: {
      async createAgentRun(record) { createdRuns.push(record); },
      async getAgentWorkflow(id, conversationId, customerId) {
        return workflows.get(id) || null;
      },
    },
  }),
});
stub('tools/usage-event-log.js', { recordUsageEvent: () => undefined });

const workflows = new Map(); // businessWorkflowId -> 工作流记录（awaiting_clarification 时可续）

const { evaluateBankMultiTurn, resetBankMultiTurnSession, submitBankClarificationForm } = require('../routes/intent-evaluator-bank-multiturn');

test('银行多轮：第一轮真实 agent run 归一化为澄清轮', async () => {
  const res = await evaluateBankMultiTurn({ query: '我要转100', mode: 'sandbox_session' });
  assert.equal(res.engine, 'bank');
  assert.equal(res.success, true);
  assert.ok(res.session_id);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].conversationId.startsWith('user:eval-'), true);
  assert.equal(calls[0].customerId, 'cust_001');
  assert.deepEqual(calls[0].plannableCapabilities, [{ id: 'cap.transfer' }]);
  assert.ok(!calls[0].conversationContext, '首轮不应携带会话上下文');
  // gateway 回查依赖的 Node 侧 run 记录已落库
  assert.equal(createdRuns.length, 1);
  assert.equal(createdRuns[0].id, calls[0].agentRunId);
  assert.equal(createdRuns[0].customerId, 'cust_001');
  assert.equal(createdRuns[0].status, 'accepted');
  assert.deepEqual(createdRuns[0].plannableCapabilityIds, ['cap.transfer']);
  assert.equal(res.turn.type, 'new_intent');
  assert.equal(res.state_after.clarification.needed, true);
  assert.equal(res.state_after.clarification.questions[0].options[0].label, '用手机号转账');
  assert.equal(res.state_after.active_tasks.length, 1);
  assert.equal(res.state_after.active_tasks[0].status, 'needs_slots');
  assert.equal(res.state_after.intent_shape.cardinality, 'single_intent');
});

test('银行多轮：第二轮续上下文并判为澄清选择轮', async () => {
  const callsBefore = calls.length;
  const first = await evaluateBankMultiTurn({ query: '我要转100', mode: 'sandbox_session' });
  assert.equal(calls.length, callsBefore + 1);
  const res = await evaluateBankMultiTurn({ query: '用手机号转账', session_id: first.session_id, mode: 'sandbox_session' });
  assert.equal(calls.length, callsBefore + 2);
  const prevCall = calls[callsBefore];
  const thisCall = calls[callsBefore + 1];
  assert.equal(thisCall.conversationId, prevCall.conversationId, '同一银行会话');
  assert.deepEqual(thisCall.conversationContext, { fromState: true, goals: [{ taskId: 't1', intentId: 'intent.transfer', status: 'needs_slots', slots: [] }] });
  assert.ok(thisCall.safetyContext.recentUserMessages.includes('用手机号转账'));
  assert.equal(res.session_id, first.session_id);
  assert.equal(res.turn.type, 'clarification_choice');
  assert.equal(res.state_after.active_tasks[0].status, 'ready');
  assert.equal(res.state_after.active_tasks[0].slots[0], 'amount=100');
  assert.equal(res.state_after.last_result_summary.status, 'succeeded');
});

test('银行多轮：reset 释放会话并清除注册表', async () => {
  const callsBefore = calls.length;
  const first = await evaluateBankMultiTurn({ query: '查余额', mode: 'sandbox_session' });
  const res = await resetBankMultiTurnSession(first.session_id);
  assert.equal(res.success, true);
  assert.ok(released.includes(calls[callsBefore].conversationId));
  // reset 后同一 session_id 视为新会话：turnCount 重置 + 新的银行 conversationId
  const again = await evaluateBankMultiTurn({ query: '查余额', session_id: first.session_id, mode: 'sandbox_session' });
  assert.equal(again.session_id, first.session_id);
  assert.equal(again.turn.index, 1);
  assert.notEqual(calls[calls.length - 1].conversationId, calls[callsBefore].conversationId, 'reset 后开新银行会话');
});

test('银行多轮：澄清态自由文本答复走 resume 出口而非新开 agent run', async () => {
  workflows.set('bwf_stub_env1', {
    status: 'awaiting_clarification', activeGoalTaskId: 't1', workflowRevision: 3,
  });
  const first = await evaluateBankMultiTurn({ query: '我要转100', mode: 'sandbox_session' });
  assert.equal(first.state_after.clarification.needed, true);
  // 结构化补录表单已透出，且不暴露 submissionToken
  const form = first.state_after.clarification.form;
  assert.ok(form, '澄清响应应携带补录表单');
  assert.equal(form.kind, 'transfer_full');
  assert.deepEqual(form.fields.map((f) => f.name), ['payeeName', 'amount']);
  assert.equal(JSON.stringify(form).includes('tok_secret'), false, '不应暴露提交凭证');

  const callsBefore = calls.length;
  const resumesBefore = resumes.length;
  const second = await evaluateBankMultiTurn({ query: '给张三转100', session_id: first.session_id, mode: 'sandbox_session' });
  assert.equal(resumes.length, resumesBefore + 1, '应走 resumeBusinessWorkflow');
  assert.equal(calls.length, callsBefore, '不应新开 agent run（生产证明该通道会死循环）');
  const resume = resumes[resumes.length - 1];
  assert.equal(resume.businessWorkflowId, 'bwf_stub_env1');
  assert.equal(resume.input.resumeKind, 'clarification');
  assert.equal(resume.input.clarificationMessage, '给张三转100', '用户原话作为澄清答复消息');
  assert.equal(resume.input.taskId, 't1');
  assert.equal(resume.input.expectedWorkflowRevision, 3);
  assert.ok(resume.input.conversationContext, '续跑必须携带会话上下文');
  assert.equal(second.turn.type, 'clarification_choice');
  assert.equal(second.state_after.last_result_summary.status, 'succeeded');
  assert.equal(second.session_id, first.session_id);
});

test('银行多轮：结构化表单提交按生产规则编码消息并 resume', async () => {
  const first = await evaluateBankMultiTurn({ query: '我要转100', mode: 'sandbox_session' });
  const resumesBefore = resumes.length;
  const res = await submitBankClarificationForm({
    session_id: first.session_id,
    values: { payeeName: '张三', amount: 100, fromAccount: '工资卡' },
  });
  assert.equal(resumes.length, resumesBefore + 1);
  const resume = resumes[resumes.length - 1];
  // 与生产 /agent-runs/:id/clarifications 的 clarificationMessage 编码逐字一致：
  // 值不编码进消息 → runtime 重新评估时槽位仍为空 → 再次出补录卡片死循环
  assert.equal(resume.input.clarificationMessage, '用工资卡的账户向张三转账100元');
  const injected = (resume.input.conversationContext.sharedSlots || []).find((s) => s.name === 'fromAccount');
  assert.ok(injected, '转出账户必须以 user_selection 来源注入 sharedSlots');
  assert.equal(injected.source, 'user_selection');
  assert.equal(injected.value, '工资卡');
  assert.equal(res.state_after.last_result_summary.status, 'succeeded');
  assert.equal(res.turn.index, 2);
});

test('银行多轮：工作流记录缺失时回落全新 agent run', async () => {
  const first = await evaluateBankMultiTurn({ query: '我要转100', mode: 'sandbox_session' });
  workflows.delete('bwf_stub_env1'); // 模拟重启/清理后记录不在
  const callsBefore = calls.length;
  const second = await evaluateBankMultiTurn({ query: '给张三转100', session_id: first.session_id, mode: 'sandbox_session' });
  assert.equal(calls.length, callsBefore + 1, '无工作流记录 → 落回全新 agent run');
  assert.equal(second.turn.type, 'clarification_choice');
});

test('银行多轮：read_only 模式不进 Agent Run，走单次意图识别探查', async () => {
  const callsBefore = calls.length;
  const resumesBefore = resumes.length;
  const probesBefore = probes.length;
  const res = await evaluateBankMultiTurn({ query: '我要转100', mode: 'read_only' });
  // 未创建 agent run，未 resume
  assert.equal(calls.length, callsBefore, 'read_only 不应创建 agent run');
  assert.equal(resumes.length, resumesBefore, 'read_only 不应 resume 业务工作流');
  // 调用了单次意图识别探查（probeIntentEvaluation）
  assert.equal(probes.length, probesBefore + 1, 'read_only 应调用 probeIntentEvaluation');
  assert.equal(probes[probesBefore].message, '我要转100');
  // 响应结构
  assert.equal(res.engine, 'bank');
  assert.equal(res.success, true);
  assert.equal(res.mode, 'read_only');
  assert.equal(res.turn.type, 'read_only_probe');
  assert.ok(res.session_id, 'read_only 也应返回 session_id 用于跨轮展示');
  // 环境边界：不连真实 runtime
  assert.equal(res.environment.mode, 'read_only');
  assert.equal(res.environment.realRuntime, false);
  assert.equal(res.environment.sandboxReady, false);
  assert.equal(res.environment.customerId, 'cust_001');
  assert.equal(res.environment.conversationId.startsWith('user:eval-'), true);
  // 状态不推进会话：state_after.status 为 read_only_probe
  assert.equal(res.state_after.status, 'read_only_probe');
  // trace 含 bank_read_only_probe 步骤
  const traceSteps = (res.trace || []).map((t) => t.step);
  assert.ok(traceSteps.includes('bank_read_only_probe'), 'trace 应含 bank_read_only_probe 步骤');
  // 单轮结果透出供前端展开
  assert.ok(res.single_turn_result, '应携带 single_turn_result 供前端展开');
  // 多轮 read_only：session_id 延续，turn.index 递增，仍不创建 agent run
  const second = await evaluateBankMultiTurn({ query: '给张三转', session_id: res.session_id, mode: 'read_only' });
  assert.equal(second.session_id, res.session_id, 'read_only 多轮应延续同一 session_id');
  assert.equal(second.turn.index, res.turn.index + 1, 'turn.index 应递增');
  assert.equal(second.turn.type, 'read_only_probe');
  assert.equal(calls.length, callsBefore, 'read_only 多轮都不应创建 agent run');
  assert.equal(probes.length, probesBefore + 2, '第二轮应再次调用 probeIntentEvaluation');
  // read_only 的 session 不会绑定 workflow，不会进入 resume 出口
  assert.equal(resumes.length, resumesBefore, 'read_only 整个生命周期都不应触发 resume');
});

test('银行多轮：默认 mode 为 read_only（与沙箱运行边界显式区分）', async () => {
  const callsBefore = calls.length;
  const res = await evaluateBankMultiTurn({ query: '我要转100' });
  assert.equal(res.mode, 'read_only', '未显式传 mode 时默认走只读探查');
  assert.equal(res.turn.type, 'read_only_probe');
  assert.equal(calls.length, callsBefore, '默认 read_only 不应创建 agent run');
});

