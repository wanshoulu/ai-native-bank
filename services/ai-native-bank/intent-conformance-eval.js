'use strict';

const crypto = require('node:crypto');

const { buildIntentGraph } = require('./intent-graph-builder');

const DATASET_ID = 'ccb-intent-conformance-v1';

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => (
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    )).join(',')}}`;
  }
  return JSON.stringify(value);
}

function digest(value) {
  return `sha256:${crypto.createHash('sha256').update(canonicalJson(value)).digest('hex')}`;
}

function uniqueStrings(values) {
  return [...new Set(values.filter(value => typeof value === 'string' && value.trim())
    .map(value => value.trim()))];
}

function similarityScore(source, candidate) {
  let score = 0;
  if (source.domain === candidate.domain) score += 100;
  if (source.menuKey === candidate.menuKey) score += 80;
  if (source.businessObject === candidate.businessObject) score += 40;
  if (source.action === candidate.action) score += 10;
  if (source.executionMode === candidate.executionMode) score += 5;
  return score;
}

function confusingIntentIds(intent, intents) {
  return intents
    .filter(candidate => candidate.id !== intent.id)
    .map(candidate => ({
      id: candidate.id,
      score: similarityScore(intent, candidate),
    }))
    .sort((left, right) => right.score - left.score || left.id.localeCompare(right.id))
    .slice(0, 3)
    .map(item => item.id);
}

function positiveUtterances(intent) {
  const candidates = uniqueStrings([
    ...(intent.aliases || []),
    intent.description,
    intent.title,
  ]);
  return candidates.slice(0, Math.max(2, Math.min(candidates.length, 6)));
}

function buildIntentConformanceEvaluation(options = {}) {
  const graph = options.graph || buildIntentGraph();
  const intents = graph.nodes
    .filter(node => node.type === 'intent')
    .sort((left, right) => left.id.localeCompare(right.id));
  const cases = intents.map(intent => ({
    caseId: `contract-smoke:${intent.id}`,
    intentId: intent.id,
    pageContext: {
      tab: intent.domain,
      menuKey: intent.menuKey,
    },
    positiveUtterances: positiveUtterances(intent),
    negativeUtterances: uniqueStrings(intent.negativeExamples || []),
    confusingIntentIds: confusingIntentIds(intent, intents),
    expected: {
      semanticVersion: intent.semanticVersion,
      lifecycle: intent.lifecycle,
      readiness: intent.readiness,
      executionMode: intent.executionMode,
      responsePolicy: intent.responsePolicy,
    },
  }));
  const unsigned = {
    schemaVersion: '1.0',
    datasetId: DATASET_ID,
    coverageKind: 'contract-smoke',
    generatedAt: graph.publishedAt,
    graph: {
      graphId: graph.graphId,
      version: graph.version,
      digest: graph.digest,
    },
    cases,
    results: null,
  };
  return { ...unsigned, digest: digest(unsigned) };
}

function validationError(code, message, details = undefined) {
  return { code, message, ...(details === undefined ? {} : { details }) };
}

function validateIntentConformanceEvaluation(dataset, options = {}) {
  const graph = options.graph || buildIntentGraph();
  const intentIds = graph.nodes.filter(node => node.type === 'intent').map(node => node.id);
  const expected = new Set(intentIds);
  const cases = dataset && Array.isArray(dataset.cases) ? dataset.cases : [];
  const actualIds = cases.map(item => item && item.intentId).filter(Boolean);
  const actual = new Set(actualIds);
  const missing = intentIds.filter(intentId => !actual.has(intentId));
  const unknown = [...actual].filter(intentId => !expected.has(intentId));
  const duplicates = [...new Set(actualIds.filter((intentId, index) => (
    actualIds.indexOf(intentId) !== index
  )))];
  const errors = [];

  if (missing.length || unknown.length || duplicates.length || actual.size !== expected.size) {
    errors.push(validationError('intent_coverage_incomplete', '评测集必须与当前发布图谱中的意图稳定 ID 一一对应', {
      missing,
      unknown,
      duplicates,
    }));
  }
  cases.forEach(item => {
    if (!item || !Array.isArray(item.positiveUtterances) || item.positiveUtterances.length < 2) {
      errors.push(validationError('positive_utterances_insufficient', item && item.intentId || 'unknown'));
    }
    if (!item || !Array.isArray(item.confusingIntentIds) || item.confusingIntentIds.length < 1) {
      errors.push(validationError('confusing_intents_missing', item && item.intentId || 'unknown'));
    }
  });
  if (dataset && dataset.graph && dataset.graph.digest !== graph.digest) {
    errors.push(validationError('graph_digest_mismatch', '评测集绑定的图谱 digest 已过期'));
  }

  return {
    valid: errors.length === 0,
    errors,
    summary: {
      graphIntentIds: expected.size,
      datasetIntentIds: actual.size,
      cases: cases.length,
    },
  };
}

module.exports = {
  DATASET_ID,
  buildIntentConformanceEvaluation,
  validateIntentConformanceEvaluation,
};
