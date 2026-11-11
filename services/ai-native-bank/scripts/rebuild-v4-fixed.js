'use strict';

/**
 * rebuild-v4-fixed.js
 *
 * 把转换器产出的 v4.json 与人工维护的 sidecar 口语化数据合并，重建 v4-fixed.json。
 *
 * 输入：
 *   - data/ai-native-bank/intent-graph/archive/ccb-mobile-bank-intents.v4.json  （转换器产出，结构权威）
 *   - data/ai-native-bank/intent-graph/v4-intent-supplements.json       （sidecar，人工补充的口语化数据）
 *
 * 输出：
 *   - data/ai-native-bank/intent-graph/graph.ccb.intents.v2.1.0.json  （运行时实际消费）
 *
 * merge 策略（sidecar 存的是 bak7 的完整字段值，非增量）：
 *   - 数组字段（recallTerms / aliases / negativeExamples / confusableIntents）：sidecar 直接覆盖
 *     —— sidecar 提取的是 bak7 完整数组（与 v4.json 不同时存），覆盖即可还原 bak7
 *   - disambiguationHints：sidecar 直接覆盖
 *     —— sidecar 提取的是 bak7 完整值，覆盖即可还原 bak7
 *   - lifecycle：sidecar 直接覆盖
 *     —— 对齐 fix_graph_data_v2 的 set_lifecycle 语义
 *
 * 用法：
 *   node services/ai-native-bank/scripts/rebuild-v4-fixed.js
 */

const fs = require('fs');
const path = require('path');

const GRAPH_DIR = path.resolve(__dirname, '../../../data/ai-native-bank/intent-graph');
const V4_PATH = path.join(GRAPH_DIR, 'archive', 'ccb-mobile-bank-intents.v4.json');
const SIDECAR_PATH = path.join(GRAPH_DIR, 'v4-intent-supplements.json');
const OUTPUT_PATH = path.join(GRAPH_DIR, 'graph.ccb.intents.v2.1.0.json');
const BAK7_PATH = path.join(GRAPH_DIR, 'archive', 'ccb-mobile-bank-intents.v4-fixed.bak7.json');

function readJSON(p) {
  return JSON.parse(fs.readFileSync(p, 'utf-8'));
}

function union(arr1, arr2) {
  const set = new Set(arr1 || []);
  for (const v of (arr2 || [])) set.add(v);
  return Array.from(set);
}

// 严格数组相等（顺序敏感）—— 用于判断 sidecar 是否需要提取某字段
function arrEqual(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  return a.every((v, i) => v === b[i]);
}

/**
 * 从 bak7（原 v4-fixed.json）与 v4.json 的 diff 提取 sidecar。
 * sidecar 只存与 v4.json 不同的字段（人工补充数据），用于 merge 时回填。
 * 现在转换器 leaf id 已与 bak7 对齐（304 vs 304），提取会很干净。
 */
function extractSupplements() {
  const v4 = readJSON(V4_PATH);
  const bak = readJSON(BAK7_PATH);
  const v4LeafMap = new Map();
  for (const n of v4.intentTree) {
    if (n.node_role === 'leaf') v4LeafMap.set(n.id, n);
  }
  const sidecar = {};
  let missing = 0;
  for (const bakLeaf of bak.intentTree) {
    if (bakLeaf.node_role !== 'leaf') continue;
    const v4Leaf = v4LeafMap.get(bakLeaf.id);
    if (!v4Leaf) { missing++; continue; }
    const bakScene = bakLeaf.scenes && bakLeaf.scenes[0];
    const v4Scene = v4Leaf.scenes && v4Leaf.scenes[0];
    if (!bakScene || !v4Scene) continue;
    const bakRp = bakScene.retrieval_profile || {};
    const v4Rp = v4Scene.retrieval_profile || {};
    const bakSd = bakScene.semantic_definition || {};
    const v4Sd = v4Scene.semantic_definition || {};
    const sup = {};
    // 数组字段：与 v4.json 不同且非空才存
    if (!arrEqual(bakRp.recallTerms, v4Rp.recallTerms) && bakRp.recallTerms && bakRp.recallTerms.length) {
      sup.recallTerms = bakRp.recallTerms;
    }
    if (!arrEqual(bakRp.aliases, v4Rp.aliases) && bakRp.aliases && bakRp.aliases.length) {
      sup.aliases = bakRp.aliases;
    }
    if (!arrEqual(bakRp.negativeExamples, v4Rp.negativeExamples) && bakRp.negativeExamples && bakRp.negativeExamples.length) {
      sup.negativeExamples = bakRp.negativeExamples;
    }
    // disambiguationHints：与 v4.json 不同且非空才存
    if (bakSd.disambiguationHints !== v4Sd.disambiguationHints && bakSd.disambiguationHints) {
      sup.disambiguationHints = bakSd.disambiguationHints;
    }
    // confusableIntents：与 v4.json 不同且非空才存
    if (!arrEqual(bakSd.confusableIntents, v4Sd.confusableIntents) && bakSd.confusableIntents && bakSd.confusableIntents.length) {
      sup.confusableIntents = bakSd.confusableIntents;
    }
    // lifecycle：与 v4.json 不同才存
    if (bakLeaf.lifecycle !== v4Leaf.lifecycle && bakLeaf.lifecycle) {
      sup.lifecycle = bakLeaf.lifecycle;
    }
    if (Object.keys(sup).length > 0) sidecar[bakLeaf.id] = sup;
  }
  fs.writeFileSync(SIDECAR_PATH, JSON.stringify(sidecar, null, 2));
  console.log('[extract] bak7 leaf:', v4LeafMap.size, 'missing in v4:', missing);
  console.log('[extract] sidecar leaf count:', Object.keys(sidecar).length);
  const fields = { recallTerms: 0, aliases: 0, negativeExamples: 0, disambiguationHints: 0, confusableIntents: 0, lifecycle: 0 };
  for (const sup of Object.values(sidecar)) {
    for (const k of Object.keys(fields)) if (sup[k] !== undefined) fields[k]++;
  }
  console.log('[extract] field distribution:', JSON.stringify(fields));
  console.log('[extract] output:', SIDECAR_PATH);
}

function merge() {
  const v4 = readJSON(V4_PATH);
  const sidecar = fs.existsSync(SIDECAR_PATH) ? readJSON(SIDECAR_PATH) : {};

  // 深拷贝 v4，作为 merge 基底
  const merged = JSON.parse(JSON.stringify(v4));

  const stats = {
    leafTotal: 0,
    leafSupplemented: 0,
    recallTermsMerged: 0,
    aliasesMerged: 0,
    negExMerged: 0,
    disambFilled: 0,
    confusMerged: 0,
    lifecycleSet: 0,
  };

  for (const leaf of merged.intentTree) {
    if (leaf.node_role !== 'leaf') continue;
    stats.leafTotal++;

    const sup = sidecar[leaf.id];
    if (!sup) continue;

    const scene = leaf.scenes && leaf.scenes[0];
    if (!scene) continue;

    scene.retrieval_profile = scene.retrieval_profile || {};
    scene.semantic_definition = scene.semantic_definition || {};
    const rp = scene.retrieval_profile;
    const sd = scene.semantic_definition;

    let touched = false;

    // 数组字段（retrieval_profile）：sidecar 直接覆盖（sidecar 存的是 bak7 完整数组）
    if (Array.isArray(sup.recallTerms)) {
      rp.recallTerms = sup.recallTerms;
      stats.recallTermsMerged++; touched = true;
    }
    if (Array.isArray(sup.aliases)) {
      rp.aliases = sup.aliases;
      stats.aliasesMerged++; touched = true;
    }
    if (Array.isArray(sup.negativeExamples)) {
      rp.negativeExamples = sup.negativeExamples;
      stats.negExMerged++; touched = true;
    }

    // confusableIntents（semantic_definition）：sidecar 直接覆盖
    if (Array.isArray(sup.confusableIntents)) {
      sd.confusableIntents = sup.confusableIntents;
      stats.confusMerged++; touched = true;
    }

    // disambiguationHints：sidecar 直接覆盖
    if (sup.disambiguationHints) {
      sd.disambiguationHints = sup.disambiguationHints;
      stats.disambFilled++; touched = true;
    }

    // lifecycle：sidecar 直接覆盖
    if (sup.lifecycle) {
      leaf.lifecycle = sup.lifecycle;
      stats.lifecycleSet++; touched = true;
    }

    if (touched) stats.leafSupplemented++;
  }

  fs.writeFileSync(OUTPUT_PATH, JSON.stringify(merged, null, 2));
  console.log('[rebuild-v4-fixed] merged v4.json + sidecar -> v4-fixed.json');
  console.log('[rebuild-v4-fixed] leaf total:', stats.leafTotal);
  console.log('[rebuild-v4-fixed] leaf supplemented:', stats.leafSupplemented);
  console.log('[rebuild-v4-fixed] recallTerms merged:', stats.recallTermsMerged);
  console.log('[rebuild-v4-fixed] aliases merged:', stats.aliasesMerged);
  console.log('[rebuild-v4-fixed] negEx merged:', stats.negExMerged);
  console.log('[rebuild-v4-fixed] disamb filled:', stats.disambFilled);
  console.log('[rebuild-v4-fixed] confus merged:', stats.confusMerged);
  console.log('[rebuild-v4-fixed] lifecycle set:', stats.lifecycleSet);
  console.log('[rebuild-v4-fixed] output:', OUTPUT_PATH);
}

// 子命令支持：--extract 时只提取 sidecar，不 merge；默认 merge
if (process.argv.includes('--extract')) {
  extractSupplements();
} else {
  merge();
}
