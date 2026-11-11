'use strict';

function createRecommendationFocusRouter(options = {}) {
  const graph = options.intentGraph;
  if (!graph || !Array.isArray(graph.nodes)) {
    throw new TypeError('recommendation focus router requires an intent graph');
  }
  const candidates = graph.nodes
    .filter(node => node && node.type === 'intent' && node.lifecycle === 'active')
    .map(projectCandidate)
    .filter(candidate => candidate.phrases.length || candidate.termSets.length);

  function route(input = {}) {
    const focus = input.focus;
    if (!focus || typeof focus.targetIntentId !== 'string'
      || input.workflowControl != null) {
      return frozenDecision('retain', []);
    }
    const message = normalizeText(input.message);
    if (!message) return frozenDecision('retain', []);

    const matches = candidates
      .filter(candidate => candidate.id !== focus.targetIntentId)
      .map(candidate => matchCandidate(candidate, message))
      .filter(Boolean)
      .sort((left, right) => left.position - right.position || left.id.localeCompare(right.id));
    return frozenDecision(matches.length ? 'rotate' : 'retain', matches.map(item => item.id));
  }

  return Object.freeze({ route });
}

function projectCandidate(node) {
  const recallTerms = new Set((Array.isArray(node.recallTerms) ? node.recallTerms : [])
    .map(normalizeText).filter(Boolean));
  const phrases = unique(Array.isArray(node.aliases) ? node.aliases : [])
    .map(normalizeText)
    .filter(text => text.length >= 3 && !recallTerms.has(text));
  const termSets = (Array.isArray(node.recallTermSets) ? node.recallTermSets : [])
    .filter(Array.isArray)
    .map(set => unique(set).map(normalizeText).filter(Boolean))
    .filter(set => set.length >= 2);
  return Object.freeze({ id: node.id, phrases: Object.freeze(phrases), termSets: Object.freeze(termSets) });
}

function matchCandidate(candidate, message) {
  let position = Number.POSITIVE_INFINITY;
  candidate.phrases.forEach(phrase => {
    const found = message.indexOf(phrase);
    if (found >= 0) position = Math.min(position, found);
  });
  candidate.termSets.forEach(set => {
    const positions = set.map(term => message.indexOf(term));
    if (positions.every(found => found >= 0)) position = Math.min(position, ...positions);
  });
  return Number.isFinite(position) ? { id: candidate.id, position } : null;
}

function normalizeText(value) {
  return typeof value === 'string'
    ? value.normalize('NFKC').toLowerCase().replace(/[\s，。！？、,.!?；;：:（）()【】\[\]“”"'‘’]+/gu, '')
    : '';
}

function unique(values) {
  return [...new Set(values.filter(value => typeof value === 'string' && value.trim()))];
}

function frozenDecision(action, matchedIntentIds) {
  return Object.freeze({ action, matchedIntentIds: Object.freeze([...matchedIntentIds]) });
}

module.exports = { createRecommendationFocusRouter };
