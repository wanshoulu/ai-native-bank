'use strict';

/**
 * 银行意图引擎单轮评测适配器：
 * 调银行运行时 /v1/internal/intent-evaluate（ai-native-bank 意图引擎只读评测阶梯），
 * 直接使用引擎返回的真实字段构造 trace，对齐 evaluate_intent_ladder 的四层链路：
 *   1) 形态判定 analyze_intent_shape
 *   2) 图谱召回 recall_intent_constraints（词法/图谱）
 *   3) 向量语义拓宽 _widen_intent_constraints（弱召回时触发）
 *   4) LLM 候选内裁决 LLMJudgeV4（条件触发：non_task 跳过；single_intent 弱召回或强竞争触发；
 *      multi_intent / ambiguous_intent / same_intent_multi_task 始终触发）
 * 不伪造状态，不隐藏步骤，未触发时如实标注原因。
 */

const { createAiBankAgentRuntimeClient } = require('../services/ai-native-bank/agent-runtime-client');
const capabilityRegistry = require('../services/ai-native-bank/capability-registry');
const usageEventLog = require('../tools/usage-event-log');

let clientSingleton = null;
function getClient() {
  if (!clientSingleton) clientSingleton = createAiBankAgentRuntimeClient();
  return clientSingleton;
}

const SHAPE_TO_RESULT_TYPE = {
  single_intent: 'single_intent',
  same_intent_multi_task: 'multi_task',
  multi_intent: 'multi_intent_plan',
  ambiguous_intent: 'fuzzy',
  non_task: 'idle_chat',
};

const SHAPE_LABELS = {
  single_intent: '单意图',
  same_intent_multi_task: '同意图多任务',
  multi_intent: '多意图',
  ambiguous_intent: '模糊意图',
  non_task: '闲聊/非业务输入',
};

function projectConstraint(item, index) {
  return {
    intent_id: item.intentId,
    intent_name: item.title,
    display_name: item.title,
    eval_label: item.title,
    action_code: item.action,
    object: item.businessObject,
    domain: item.businessObject,
    score: item.score,
    rank: index + 1,
    source: 'bank_graph',
    evidence: item.evidence || [],
    authoritative: !!item.shapeAuthoritative,
    constraint: item.constraint,
    required_shape: item.requiredShape,
    expected_task_count: item.expectedTaskCount,
    execution_mode: item.executionMode,
    required_slots: item.requiredSlots || [],
    disambiguation_hints: item.disambiguationHints || '',
    unresolved_slot_readiness: item.unresolvedSlotReadiness || '',
  };
}

function adaptLadderToEvalResponse(ladder, query) {
  const shape = ladder.shape || {};
  const constraints = Array.isArray(ladder.constraints) ? ladder.constraints : [];
  const candidates = constraints.map(projectConstraint);
  const judgeResults = ladder.judgeResults || null;
  const isAmbiguous = shape.shape === 'ambiguous_intent';
  let clarificationCandidates = candidates;

  // LLM 裁决结果可用时，按 confidence 排序选出权威候选
  let authoritative;
  if (judgeResults && judgeResults.length > 0) {
    const confMap = new Map(judgeResults.map((j) => [j.intentId, j.confidence]));
    // 按 LLM confidence 降序排列
    const ranked = candidates
      .filter((c) => confMap.has(c.intent_id))
      .map((c) => ({ ...c, judge_confidence: confMap.get(c.intent_id) }))
      .sort((a, b) => b.judge_confidence - a.judge_confidence);
    if (ranked.length) clarificationCandidates = ranked;
    // multi_intent: 取 taskCount 个；其他取 top1
    const taskCount = shape.taskCount || 1;
    const take = shape.shape === 'multi_intent' ? Math.max(taskCount, 1) : 1;
    authoritative = isAmbiguous ? [] : ranked.slice(0, take).map((c, i) => ({ ...c, rank: i + 1, authoritative: true }));
  } else {
    // 无 LLM 裁决时回退到 shapeAuthoritative 过滤
    const authoritativeConstraints = constraints.filter((item) => item.shapeAuthoritative);
    authoritative = isAmbiguous ? [] : authoritativeConstraints.map(projectConstraint);
  }

  const head = candidates[0] ? candidates[0] : null;
  const finalResult = isAmbiguous ? null : (authoritative.length ? authoritative[0] : head);
  const resultType = SHAPE_TO_RESULT_TYPE[shape.shape] || 'fuzzy';
  const faq = ladder.faq || {};
  const timings = ladder.timings || {};

  const evidenceText = finalResult && finalResult.evidence.length
    ? finalResult.evidence.join('、')
    : '无召回证据';
  let reason;
  if (shape.shape === 'ambiguous_intent') {
    reason = `银行意图引擎判定意图模糊（候选 ${constraints.length} 条，证据重叠或不足），需要澄清后继续。`;
  } else if (shape.shape === 'non_task') {
    reason = '银行意图引擎判定为闲聊/非业务输入，不进入业务路由。';
  } else if (authoritative.length === 1) {
    reason = `权威收敛：图谱召回唯一候选「${authoritative[0].intent_name}」（证据：${evidenceText}）。`;
  } else if (authoritative.length > 1) {
    reason = `权威收敛：${authoritative.length} 个同形态候选「${authoritative.map((item) => item.intent_name).join('、')}」。`;
  } else if (head) {
    reason = `召回候选「${head.intent_name}」未达权威阈值，仅作证据展示（证据：${evidenceText}）。`;
  } else {
    reason = '图谱与语义召回均未命中候选。';
  }

  const needsClarification = shape.shape === 'ambiguous_intent'
    || !!(finalResult && finalResult.unresolved_slot_readiness === 'needs_clarification');
  const clarification = {
    needed: needsClarification,
    questions: [],
  };
  if (isAmbiguous) {
    const options = Array.from(new Map(clarificationCandidates.map((item) => [item.intent_id, {
      ...item,
      label: item.display_name || item.intent_name,
      value: item.intent_id,
      authoritative: false,
    }])).values());
    const hint = clarificationCandidates.find((item) => item.disambiguation_hints);
    clarification.questions = [{
      title: '请确认您要办理的业务',
      question: hint ? hint.disambiguation_hints
        : options.length ? '您想办理以下哪项业务？' : '您想办理什么业务？请补充具体需求。',
      options,
    }];
  } else if (needsClarification && finalResult && finalResult.disambiguation_hints) {
    clarification.questions = [{
      title: `请确认「${finalResult.intent_name}」`,
      question: finalResult.disambiguation_hints,
      options: [],
    }];
  } else if (needsClarification && head && head.disambiguation_hints) {
    clarification.questions = [{
      title: `请确认「${head.intent_name}」`,
      question: head.disambiguation_hints,
      options: [],
    }];
  }

  const baseForTasks = isAmbiguous ? [] : (authoritative.length ? authoritative : (head ? [head] : []));
  let tasks;
  if (shape.shape === 'same_intent_multi_task' && (shape.taskCount || 0) > 1 && baseForTasks.length > 0) {
    // 同意图多任务：同一意图 × taskCount 个执行对象（如给多人转账），各任务独立补槽
    tasks = Array.from({ length: shape.taskCount }, (_, i) => ({
      task_index: i + 1,
      intent_id: baseForTasks[0].intent_id,
      intent_name: baseForTasks[0].intent_name,
      action_code: baseForTasks[0].action_code,
      status: baseForTasks[0].unresolved_slot_readiness === 'needs_slots' ? 'needs_slots' : 'ready',
    }));
  } else {
    tasks = baseForTasks.map((item, index) => ({
      task_index: index + 1,
      intent_id: item.intent_id,
      intent_name: item.intent_name,
      action_code: item.action_code,
      status: item.unresolved_slot_readiness === 'needs_slots' ? 'needs_slots' : 'ready',
    }));
  }

  const totalMs = Math.round((timings.shapeMs || 0) + (timings.recallMs || 0) + (timings.widenMs || 0) + (timings.judgeMs || 0)) || ladder.latencyMs || 0;

  // —— 对齐 ai-native-bank 引擎 evaluate_intent_ladder 的真实四层链路 ——
  // 引擎源码：services/ai-native-bank/agent-runtime/app/service.py 第 1462-1618 行
  // 阈值常量（来自 service.py / subgraph_reasoning_v4.py）：
  //   _SCORE_WEAK_RECALL_THRESHOLD = 100  （弱召回阈值，低于此触发向量拓宽）
  //   _STRONG_MATCH_THRESHOLD = 80        （强匹配阈值，single_intent 强匹配跳过 LLM）
  //   _SUPER_STRONG_MATCH_THRESHOLD = 145 （超强匹配保护，LLM 重排后强制保留原 top1）
  // LLM 触发条件（service.py 第 1518-1576 行）：
  //   - shape == non_task → 跳过
  //   - shape == single_intent → top1 < 80 或 top2 >= 60 时触发
  //   - 其他形态（multi_intent / ambiguous_intent / same_intent_multi_task）→ 始终触发
  //   - 同时要求 self._v4_judge.enabled（API key 已配置）且 constraints 非空
  const top1Score = candidates.length ? candidates[0].score : 0;
  const top2Score = candidates.length > 1 ? candidates[1].score : 0;
  const STRONG_MATCH_THRESHOLD = 80;
  const TOP2_COMPETITION_THRESHOLD = 60;
  const judgeTriggered = !!(judgeResults && judgeResults.length > 0);
  let judgeSkipReason = null;
  if (!judgeTriggered) {
    if (shape.shape === 'non_task') {
      judgeSkipReason = '形态为 non_task（闲聊/非业务输入），引擎设计不触发 LLM 裁决。';
    } else if (!candidates.length) {
      judgeSkipReason = '召回候选为空，无可裁决对象，跳过 LLM 裁决。';
    } else if (ladder.judgeEnabled === false) {
      // 运行时真源：LLMJudgeV4.enabled=false，即 API key 未加载成功（四级回退均失败）
      judgeSkipReason = '运行时 LLM 裁决未启用（API key 未加载成功），引擎回退到 shapeAuthoritative 收敛。';
    } else if (shape.shape === 'single_intent'
      && top1Score >= STRONG_MATCH_THRESHOLD
      && top2Score < TOP2_COMPETITION_THRESHOLD) {
      judgeSkipReason = `single_intent 词法强匹配（top1=${top1Score} ≥ ${STRONG_MATCH_THRESHOLD}，top2=${top2Score} < ${TOP2_COMPETITION_THRESHOLD}），引擎设计不触发 LLM 裁决。`;
    } else {
      judgeSkipReason = `LLM 裁决已启用${ladder.judgeModel ? `（模型 ${ladder.judgeModel}）` : ''}但本次未返回结果（调用超时/失败）；引擎回退到 shapeAuthoritative 收敛。`;
    }
  }

  const steps = [
    {
      step: 'intent_shape',
      title: '① 意图形态判定（analyze_intent_shape）',
      conclusion: `${SHAPE_LABELS[shape.shape] || shape.shape} · 任务数 ${shape.taskCount || 0}`,
      data: { shape: shape.shape, taskCount: shape.taskCount },
      timing_ms: timings.shapeMs || 0,
    },
    {
      step: 'graph_recall',
      title: '② 图谱召回（recall_intent_constraints）',
      conclusion: `候选 ${candidates.length} 条${(faq.matchedIntentIds || []).length ? ` · FAQ 命中 ${faq.matchedIntentIds.length} 条` : ''}`,
      data: {
        candidates: candidates.slice(0, 5),
        faqMatchedIntentIds: faq.matchedIntentIds || [],
        recallChannels: ladder.recallChannels || null,
        top1Score,
        top2Score,
      },
      selected: head || null,
      timing_ms: timings.recallMs || 0,
    },
    {
      step: 'semantic_widening',
      title: '③ 向量语义拓宽（_widen_intent_constraints）',
      conclusion: ladder.widened
        ? '词法/图谱召回 top1 < 100（弱召回阈值），触发 text-embedding-v4 语义扩召，扩召候选仅作证据，不改判权威意图。'
        : `词法/图谱召回 top1=${top1Score} ≥ 100（弱召回阈值），引擎跳过向量拓宽。`,
      data: {
        triggered: !!ladder.widened,
        threshold: 100,
        top1Score,
        vectorChannel: ladder.recallChannels?.vector || null,
        subgraphChannel: ladder.recallChannels?.subgraph || null,
      },
      timing_ms: timings.widenMs || 0,
    },
    {
      step: 'deepseek_judge',
      title: '④ LLM 候选内裁决（LLMJudgeV4）',
      conclusion: judgeTriggered
        ? `LLM 对 ${candidates.length} 条候选重排序，选出 ${authoritative.length} 个权威意图（模型：qwen-plus，候选内裁决，不允许生成候选外意图）。`
        : judgeSkipReason,
      data: judgeTriggered
        ? {
            triggered: true,
            decision_mode: 'llm_judge',
            selected_source: 'llm_ranked',
            degraded: false,
            repaired: false,
            judgeResults,
            authoritativeCount: authoritative.length,
            candidateCount: candidates.length,
            judgeMs: timings.judgeMs || 0,
          }
        : {
            triggered: false,
            decision_mode: 'bank_deterministic_ladder',
            selected_source: 'bank_graph',
            degraded: false,
            repaired: false,
            skip_reason: judgeSkipReason,
            shape: shape.shape,
            top1Score,
            top2Score,
            judgeMs: 0,
          },
      timing_ms: timings.judgeMs || 0,
    },
    {
      step: 'authority_shortcut',
      title: '⑤ 权威收敛与裁决',
      conclusion: authoritative.length === 1
        ? `唯一权威候选「${authoritative[0].intent_name}」，收敛为 required。`
        : authoritative.length > 1
          ? `${authoritative.length} 个候选${judgeTriggered ? '经 LLM 裁决' : '经 shapeAuthoritative 过滤'}为权威，交给后续任务规划拆分。`
          : '无权威候选，保留证据供澄清或复核。',
      data: {
        authoritativeCount: authoritative.length,
        candidateCount: candidates.length,
        decisionSource: judgeTriggered ? 'llm_judge' : 'shape_authoritative',
      },
      timing_ms: 0,
    },
    {
      step: 'final_result',
      title: '⑥ 最终结果',
      conclusion: finalResult
        ? `${finalResult.intent_name}（${finalResult.action_code} · ${finalResult.object}）`
        : '未形成明确业务意图',
      data: { resultType, taskCount: tasks.length },
      selected: finalResult || null,
      timing_ms: totalMs,
    },
  ];

  return {
    query,
    result_type: resultType,
    latency_ms: totalMs,
    final_result: finalResult,
    authoritative_results: authoritative,
    understanding: {
      cardinality: shape.shape === 'multi_intent' ? 'multi_intent'
        : shape.shape === 'same_intent_multi_task' ? 'multi_task_intent'
          : resultType,
      task_count: shape.taskCount || tasks.length,
      final_is_non_leaf: false,
      reason,
      clarification,
      tasks,
    },
    methods: {
      bank: {
        engine: 'bank-runtime-intent-engine',
        graphVersion: ladder.graphVersion,
        graphDigest: ladder.graphDigest,
        shape: shape.shape,
        shapeTaskCount: shape.taskCount,
        faq,
        timings,
        widened: !!ladder.widened,
        recallChannels: ladder.recallChannels || null,
        candidates,
        judgeResults: judgeResults || [],
      },
      llm_judge: {
        decision_mode: judgeTriggered ? 'llm_judge' : 'bank_deterministic_ladder',
        selected_source: judgeTriggered ? 'llm_ranked' : 'bank_graph',
        triggered: judgeTriggered,
        enabled: ladder.judgeEnabled !== false,
        model: ladder.judgeModel || '',
        degraded: false,
        repaired: false,
        reason: judgeTriggered
          ? `LLM 候选内裁决（LLMJudgeV4）：对 ${candidates.length} 条候选重排序，选出 ${authoritative.length} 个权威意图（模型 ${ladder.judgeModel || '运行时配置'}，候选内裁决，不允许生成候选外意图）。`
          : judgeSkipReason,
        decision_trace: { steps },
      },
    },
    attribution: {
      signals: [
        {
          type: 'agreement',
          level: authoritative.length === 1 ? 'strong' : 'info',
          message: authoritative.length === 1
            ? '银行引擎图谱召回唯一权威候选，确定性收敛。'
            : `银行引擎保留 ${candidates.length} 条候选（权威 ${authoritative.length} 条）。`,
        },
        {
          type: 'semantic_recall',
          level: 'info',
          message: ladder.recallChannels?.vector
            ? `向量召回状态：${ladder.recallChannels.vector.status}，本路结果 ${(ladder.recallChannels.vector.candidates || []).length} 条；新增 ${ladder.recallChannels.vector.contributedCount || 0} 条。`
            : ladder.widened
              ? '扩召候选已并入证据；旧响应没有逐路执行诊断。'
              : '旧响应未记录向量执行状态，请查看候选来源；不能仅凭 widened=false 判断未执行。',
        },
      ],
      recommendation: authoritative.length >= 1
        ? '可重点检查边界金额/多任务拆分在下游规划中的表现。'
        : '未命中权威候选：优先检查图谱别名/recallTerms 覆盖。',
    },
    segment_results: [],
    answers: [],
    engine: 'bank',
  };
}

// 最近一次真实评测看到的运行时真源口径（版本随真实评测刷新）
let bankRuntimeMeta = { graphVersion: '', graphDigest: '', llmJudge: null, at: 0 };

async function evaluateIntentWithBankEngine(query, options) {
  const client = getClient();
  const plannableCapabilities = capabilityRegistry.projectForAgentRuntime().plannableCapabilities || [];
  const ladder = await client.probeIntentEvaluation({
    message: String(query || ''),
    plannableCapabilityIds: plannableCapabilities.map((item) => item.id),
  });
  // 记录运行时真源口径（版本/digest/LLM 裁决状态随每次真实评测刷新），供状态端点展示
  if (ladder.graphVersion || ladder.graphDigest) {
    bankRuntimeMeta = {
      graphVersion: ladder.graphVersion || bankRuntimeMeta.graphVersion,
      graphDigest: ladder.graphDigest || bankRuntimeMeta.graphDigest,
      llmJudge: {
        enabled: ladder.judgeEnabled !== false,
        model: ladder.judgeModel || '',
        triggered: !!(ladder.judgeResults && ladder.judgeResults.length > 0),
      },
      at: Date.now(),
    };
  }
  const response = adaptLadderToEvalResponse(ladder, String(query || ''));
  usageEventLog.recordUsageEvent({
    carrier: 'graph',
    caller: 'intent-eval-bank',
    query: ladder.graphVersion || 'bank-intent-graph',
    hits: (Array.isArray(ladder.constraints) ? ladder.constraints.length : 0),
  });
  return response;
}

function getBankRuntimeMeta() {
  return bankRuntimeMeta;
}

module.exports = { evaluateIntentWithBankEngine, adaptLadderToEvalResponse, getBankRuntimeMeta };
