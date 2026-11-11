'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

// External runtime and event logging are outside this adapter unit test.
for (const [modulePath, exports] of [
  ['../services/ai-native-bank/agent-runtime-client', {}],
  ['../services/ai-native-bank/capability-registry', {}],
  ['../tools/usage-event-log', {}],
]) {
  const id = require.resolve(modulePath);
  require.cache[id] = { id, filename: id, loaded: true, exports };
}
const { adaptLadderToEvalResponse } = require('../routes/intent-evaluator-bank');

function constraint(intentId, title, extra = {}) {
  return { intentId, title, action: 'query', businessObject: 'account',
    score: 75, evidence: ['alias'], requiredSlots: [], ...extra };
}

function ladder(extra = {}) {
  return { shape: { shape: 'ambiguous_intent', taskCount: 1 },
    constraints: [constraint('balance', '查询余额'), constraint('transactions', '查询明细'),
      constraint('unjudged', '其他候选')],
    judgeResults: [{ intentId: 'transactions', confidence: .88 },
      { intentId: 'balance', confidence: .78 },
      { intentId: 'outside-graph', confidence: .99 }], ...extra };
}

test('ambiguous judged candidates become ordered answerable options without executable top1', () => {
  const result = adaptLadderToEvalResponse(ladder(), '查一下账户');
  const question = result.understanding.clarification.questions[0];
  assert.ok(question, 'ambiguity needs an answerable clarification even without graph hints');
  assert.ok(question.question.trim());
  assert.deepEqual(question.options.map((row) => row.intent_id), ['transactions', 'balance']);
  assert.deepEqual(question.options.map((row) => row.label), ['查询明细', '查询余额']);
  assert.deepEqual(question.options.map((row) => row.judge_confidence), [.88, .78]);
  assert.equal(result.final_result, null);
  assert.deepEqual(result.authoritative_results, []);
  assert.deepEqual(result.understanding.tasks, []);
});

test('ambiguity without judge uses recalled options and preserves hints', () => {
  const result = adaptLadderToEvalResponse(ladder({ judgeResults: null,
    constraints: [constraint('balance', '查询余额', { shapeAuthoritative: true,
      disambiguationHints: '您想看余额还是交易明细？' }), constraint('transactions', '查询明细')] }), '查账户');
  const question = result.understanding.clarification.questions[0];
  assert.deepEqual(question.options.map((row) => row.intent_id), ['balance', 'transactions']);
  assert.match(question.question, /余额还是交易明细/);
  assert.equal(result.final_result, null);
  assert.deepEqual(result.authoritative_results, []);
  assert.deepEqual(result.understanding.tasks, []);
});

test('resolved single and multi intent retain judged selections and ready tasks', () => {
  for (const [shape, count] of [['single_intent', 1], ['multi_intent', 2]]) {
    const result = adaptLadderToEvalResponse(ladder({ shape: { shape, taskCount: count } }), 'query');
    assert.equal(result.final_result.intent_id, 'transactions');
    assert.equal(result.authoritative_results.length, count);
    assert.equal(result.understanding.tasks.length, count);
    assert.ok(result.understanding.tasks.every((row) => row.status === 'ready'));
  }
});
