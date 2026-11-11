'use strict';

const fs = require('node:fs');
const path = require('node:path');

const { INTENT_PROFILES } = require('./intent-profiles');

const SLOT_NAME_PATTERN = /^[a-z][A-Za-z0-9]{0,63}$/;
const INTERNAL_SLOT_SUFFIX = /(?:Id|ID|Token|Secret|Credential|Grant)$/;
const AMBIGUITY_KEY_PATTERN = /^[a-z][a-z0-9_]{1,63}$/;
const AMBIGUITY_OPTION_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

// 网关内置 INTENT_PROFILES 与 runtime 侧图谱（TuGraph，契约源为
// graph.ccb.intents.v1.0.0.json 的 slotContract）可能漂移：图谱新增或
// 改名槽位后，内置表若未同步，runtime 已解析的语义槽位会被整条事件流
// 拒绝（Agent 私有会话槽位非法）。此处把图谱契约声明的语义槽位名合并
// 进白名单，图谱演进自动生效，无需手工同步两份数据。
const CONTRACT_SLOT_CACHE = new Map();

function declaredSlotNamesFromContract() {
  const contractPath = process.env.AI_BANK_INTENT_CONTRACT_PATH
    || path.join(__dirname, '..', '..', 'data', 'ai-native-bank', 'intent-graph', 'graph.ccb.intents.v1.0.0.json');
  const cached = CONTRACT_SLOT_CACHE.get(contractPath);
  if (cached) return cached;
  let declared = new Map();
  try {
    const graph = JSON.parse(fs.readFileSync(contractPath, 'utf8'));
    for (const node of graph.nodes || []) {
      if (!node || typeof node.id !== 'string' || node.type !== 'intent') continue;
      const slots = node.slotContract && Array.isArray(node.slotContract.slots)
        ? node.slotContract.slots
        : [];
      const names = new Set(slots
        .map(slot => (slot && typeof slot.name === 'string' ? slot.name : ''))
        .filter(name => SLOT_NAME_PATTERN.test(name) && !INTERNAL_SLOT_SUFFIX.test(name)));
      declared.set(node.id, names);
    }
  } catch (error) {
    console.warn('[ai-native-bank] intent contract slot policy unavailable', {
      contractPath,
      reason: error && error.message,
    });
    declared = new Map();
  }
  CONTRACT_SLOT_CACHE.set(contractPath, declared);
  return declared;
}

function slotNamesForIntent(intentId) {
  const profile = INTENT_PROFILES[intentId];
  const names = new Set(profile ? profile.slots.map(item => item.name) : []);
  const contractNames = declaredSlotNamesFromContract().get(intentId);
  if (contractNames) {
    for (const name of contractNames) names.add(name);
  }
  return names;
}

function isGraphDeclaredSemanticSlot(name, intentIds) {
  if (typeof name !== 'string' || !SLOT_NAME_PATTERN.test(name) || INTERNAL_SLOT_SUFFIX.test(name)) {
    return false;
  }
  return intentIds.some(intentId => slotNamesForIntent(intentId).has(name));
}

function isBoundedAmbiguity(type, options) {
  return typeof type === 'string'
    && AMBIGUITY_KEY_PATTERN.test(type)
    && Array.isArray(options)
    && options.length >= 2
    && options.length <= 8
    && new Set(options).size === options.length
    && options.every(option => typeof option === 'string' && AMBIGUITY_OPTION_PATTERN.test(option));
}

module.exports = {
  isBoundedAmbiguity,
  isGraphDeclaredSemanticSlot,
  slotNamesForIntent,
};
