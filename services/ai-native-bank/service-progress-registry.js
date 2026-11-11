'use strict';

const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_PATH = path.resolve(
  __dirname, '../../data/ai-native-bank/registries/service-progress-profiles.v1.json',
);
const ALLOWED_STAGES = new Set([
  'request.accepted', 'user_agent.completed', 'delegation.created',
  'business_agent.started', 'business_agent.completed',
  'business_skill.started', 'business_skill.completed',
  'capability.invocation_started', 'capability.invocation_completed',
  'plan.validated', 'run.awaiting_confirmation', 'run.completed', 'workflow.rebound',
]);
const ALLOWED_STEP_STATES = new Set(['running', 'completed', 'failed', 'skipped']);
const PROFILE_ID = /^[a-z][a-z0-9-]{2,79}$/;
const STEP_ID = /^[a-z][a-z0-9_]{1,63}$/;
const SKILL_ID = /^bank\.[a-z0-9.-]+$/;
const INTENT_ID = /^ccb\.[a-z0-9.-]+$/;
const MATCH_KEYS = new Set(['skillId', 'capabilityId']);

class ServiceProgressRegistryError extends Error {
  constructor(message) {
    super('service_progress_registry_invalid: ' + message);
    this.name = 'ServiceProgressRegistryError';
    this.code = 'service_progress_registry_invalid';
  }
}

function fail(message) { throw new ServiceProgressRegistryError(message); }

function exact(value, keys, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(label + ' must be an object');
  const actual = Object.keys(value);
  const unknown = actual.filter(key => !keys.includes(key));
  const missing = keys.filter(key => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length) fail(label + ' has unknown fields: ' + unknown.join(','));
  if (missing.length) fail(label + ' is missing fields: ' + missing.join(','));
}

function safeCopy(value, label, max = 180) {
  if (typeof value !== 'string' || !value.trim() || value.length > max
    || /[<>]/u.test(value) || /\d{12,}/u.test(value)
    || /(?:system\s*prompt|private\s*prompt|confirmation\s*token|access\s*token)/iu.test(value)) {
    fail(label + ' is unsafe');
  }
  return value.trim();
}

function uniqueStrings(values, pattern, label, max = 16) {
  if (!Array.isArray(values) || !values.length || values.length > max) fail(label + ' must be bounded');
  if (values.some(value => typeof value !== 'string' || !pattern.test(value))) fail(label + ' contains invalid id');
  if (new Set(values).size !== values.length) fail(label + ' contains duplicates');
  return values;
}

function validateManifest(input) {
  exact(input, ['schemaVersion', 'profiles'], 'manifest');
  if (input.schemaVersion !== '1.0') fail('schemaVersion is unsupported');
  if (!Array.isArray(input.profiles) || input.profiles.length > 64) fail('profiles must be a bounded array');
  const profileIds = new Set();
  const activeSkillIds = new Set();
  input.profiles.forEach((profile, profileIndex) => {
    const label = 'profiles[' + profileIndex + ']';
    exact(profile, [
      'id', 'version', 'status', 'title', 'initialText', 'skillIds', 'intentIds',
      'completionSummary', 'failureSummary', 'steps', 'transitions',
    ], label);
    if (!PROFILE_ID.test(profile.id) || profileIds.has(profile.id)) fail(label + '.id is invalid or duplicate');
    if (!/^\d+\.\d+\.\d+$/.test(profile.version)) fail(label + '.version is invalid');
    if (!['active', 'inactive'].includes(profile.status)) fail(label + '.status is invalid');
    safeCopy(profile.title, label + '.title', 80);
    if (profile.initialText != null) safeCopy(profile.initialText, label + '.initialText', 120);
    safeCopy(profile.completionSummary, label + '.completionSummary', 240);
    safeCopy(profile.failureSummary, label + '.failureSummary', 160);
    uniqueStrings(profile.skillIds, SKILL_ID, label + '.skillIds');
    uniqueStrings(profile.intentIds, INTENT_ID, label + '.intentIds');
    if (profile.status === 'active') profile.skillIds.forEach(skillId => {
      if (activeSkillIds.has(skillId)) fail('active skill has more than one profile: ' + skillId);
      activeSkillIds.add(skillId);
    });
    if (!Array.isArray(profile.steps) || !profile.steps.length || profile.steps.length > 24) {
      fail(label + '.steps must be bounded');
    }
    const stepIds = new Set();
    profile.steps.forEach((step, stepIndex) => {
      exact(step, ['id', 'label', 'completedLabel'], label + '.steps[' + stepIndex + ']');
      if (!STEP_ID.test(step.id) || stepIds.has(step.id)) fail(label + '.steps has duplicate or invalid id');
      safeCopy(step.label, label + '.steps.label', 100);
      safeCopy(step.completedLabel, label + '.steps.completedLabel', 100);
      stepIds.add(step.id);
    });
    if (!Array.isArray(profile.transitions) || !profile.transitions.length || profile.transitions.length > 48) {
      fail(label + '.transitions must be bounded');
    }
    profile.transitions.forEach((transition, transitionIndex) => {
      const transitionLabel = label + '.transitions[' + transitionIndex + ']';
      exact(transition, ['stage', 'match', 'updates'], transitionLabel);
      if (!ALLOWED_STAGES.has(transition.stage)) fail(transitionLabel + '.stage is invalid');
      if (!transition.match || typeof transition.match !== 'object' || Array.isArray(transition.match)) {
        fail(transitionLabel + '.match must be an object');
      }
      Object.keys(transition.match).forEach(key => {
        if (!MATCH_KEYS.has(key)) fail(transitionLabel + '.match contains unknown field');
        const pattern = key === 'skillId' ? SKILL_ID : SKILL_ID;
        if (typeof transition.match[key] !== 'string' || !pattern.test(transition.match[key])) {
          fail(transitionLabel + '.match is invalid');
        }
      });
      if (!Array.isArray(transition.updates) || !transition.updates.length || transition.updates.length > 24) {
        fail(transitionLabel + '.updates must be bounded');
      }
      transition.updates.forEach(update => {
        exact(update, ['stepId', 'state'], transitionLabel + '.update');
        if (!stepIds.has(update.stepId)) fail(transitionLabel + ' references unknown step ' + update.stepId);
        if (!ALLOWED_STEP_STATES.has(update.state)) fail(transitionLabel + '.state is invalid');
      });
    });
    profileIds.add(profile.id);
  });
  return input;
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

function publicProfile(profile) {
  return {
    id: profile.id,
    version: profile.version,
    title: profile.title,
    skillIds: profile.skillIds,
    intentIds: profile.intentIds,
    completionSummary: profile.completionSummary,
    failureSummary: profile.failureSummary,
    steps: profile.steps,
    transitions: profile.transitions,
  };
}

function loadServiceProgressRegistry(options = {}) {
  const source = options.manifest || JSON.parse(fs.readFileSync(options.filePath || DEFAULT_PATH, 'utf8'));
  const validated = validateManifest(clone(source));
  const snapshot = deepFreeze({
    schemaVersion: validated.schemaVersion,
    profiles: validated.profiles.filter(profile => profile.status === 'active').map(publicProfile),
  });
  const bySkillId = new Map();
  snapshot.profiles.forEach(profile => profile.skillIds.forEach(skillId => bySkillId.set(skillId, profile)));
  return Object.freeze({
    getPublicSnapshot: () => snapshot,
    getBySkillId: skillId => bySkillId.get(skillId) || null,
  });
}

module.exports = {
  ServiceProgressRegistryError,
  loadServiceProgressRegistry,
};
