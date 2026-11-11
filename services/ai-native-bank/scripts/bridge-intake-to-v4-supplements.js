'use strict';

/**
 * bridge-intake-to-v4-supplements.js
 *
 * 把 intake 知识导入工具产出的 batch.json 里的补充数据（召回词/别名/消歧提示等）
 * 转换为 v4-intent-supplements.json sidecar 格式，让 rebuild-v4-fixed.js 能 merge 到运行时。
 *
 * 桥接路径：
 *   intake Excel → batch.json (候选层) → [本脚本] → sidecar JSON → rebuild-v4-fixed.js → v4-fixed.json → 运行时
 *
 * 映射策略：
 *   intake intent 的 `code` 字段 = v1 intent id（如 ccb.cashflow.salary-plan）
 *   v4.json leaf 的 observability.sourceRecords[0].ref = "v2-source:<v1Id>"
 *   建立 v1Id → leafId 映射表，用 code 查找 leafId
 *
 * 用法：
 *   node services/ai-native-bank/scripts/bridge-intake-to-v4-supplements.js [path/to/batch.json] [--dry-run]
 *   不带参数时自动找最新的 batch.json
 *   --dry-run：只预览不写文件
 */

const fs = require('fs');
const path = require('path');

const GRAPH_DIR = path.resolve(__dirname, '../../../data/ai-native-bank/intent-graph');
const V4_PATH = path.join(GRAPH_DIR, 'archive', 'ccb-mobile-bank-intents.v4.json');
const SIDECAR_PATH = path.join(GRAPH_DIR, 'v4-intent-supplements.json');
const INTAKE_DIR = path.resolve(__dirname, '../../../data/knowledge-intake');

function readJSON(p) {
  return JSON.parse(fs.readFileSync(p, 'utf-8'));
}

function findLatestBatch() {
  if (!fs.existsSync(INTAKE_DIR)) return null;
  const dirs = fs.readdirSync(INTAKE_DIR).filter(d => d.startsWith('ki-')).sort().reverse();
  if (dirs.length === 0) return null;
  const batchPath = path.join(INTAKE_DIR, dirs[0], 'batch.json');
  return fs.existsSync(batchPath) ? batchPath : null;
}

/**
 * 从 v4.json leaf 的 observability.sourceRecords[0].ref 提取 v1 intent id
 * ref 格式: "v2-source:<v1Id>"（如 "v2-source:ccb.credit.apply.apply"）
 * @returns {Map<string, string>} v1Id → leafId
 */
function buildV1ToLeafMap(v4) {
  const map = new Map();
  for (const node of (v4.intentTree || [])) {
    if (node.node_role !== 'leaf') continue;
    const srs = (node.observability && node.observability.sourceRecords) || [];
    for (const sr of srs) {
      if (sr.ref && sr.ref.startsWith('v2-source:')) {
        const v1Id = sr.ref.slice('v2-source:'.length);
        map.set(v1Id, node.id);
      }
    }
  }
  return map;
}

// 多值列按逗号/分号切分
function splitList(val) {
  if (!val) return [];
  if (Array.isArray(val)) return val;
  return String(val).split(/[;；,，]/).map(s => s.trim()).filter(Boolean);
}

// 数组 union（去重保序）：intake 补充数据追加到 sidecar 已有值，不覆盖原有数据
function unionList(existing, incoming) {
  const seen = new Set(existing || []);
  const out = [...(existing || [])];
  for (const v of incoming) {
    if (!seen.has(v)) { seen.add(v); out.push(v); }
  }
  return out;
}

function bridge(batchPath, dryRun) {
  const batch = readJSON(batchPath);
  const v4 = readJSON(V4_PATH);
  const v1ToLeaf = buildV1ToLeafMap(v4);

  // 加载已有 sidecar（增量 merge：intake 数据覆盖同 leaf 的同字段）
  const sidecar = fs.existsSync(SIDECAR_PATH) ? readJSON(SIDECAR_PATH) : {};

  const stats = {
    batchId: batch.batchId,
    total: 0,
    mapped: 0,
    unmapped: 0,
    unmappedDetails: [],
    fieldsUpdated: { recallTerms: 0, aliases: 0, negativeExamples: 0, disambiguationHints: 0, confusableIntents: 0, lifecycle: 0 },
    existingSidecarLeaves: Object.keys(sidecar).length,
  };

  const intents = (batch.rows && batch.rows.intents) || [];
  for (const intent of intents) {
    stats.total++;

    // 映射策略：用 code 字段（v1 intent id）查找 leaf id
    const code = (intent.code || '').trim();
    if (!code) {
      stats.unmapped++;
      stats.unmappedDetails.push({
        name: intent.name,
        ref: intent.ref,
        reason: 'missing code field (v1 intent id required, e.g. ccb.cashflow.salary-plan)',
      });
      continue;
    }

    const leafId = v1ToLeaf.get(code);
    if (!leafId) {
      stats.unmapped++;
      stats.unmappedDetails.push({
        name: intent.name,
        code,
        reason: `code "${code}" not found in v4.json sourceRecords`,
      });
      continue;
    }

    // 映射成功，提取补充字段（union 语义：intake 数据追加到 sidecar 已有值，不覆盖原有数据）
    const sup = sidecar[leafId] ? { ...sidecar[leafId] } : {};
    let touched = false;

    const recallTerms = splitList(intent.recallTerms);
    if (recallTerms.length) { sup.recallTerms = unionList(sup.recallTerms, recallTerms); stats.fieldsUpdated.recallTerms++; touched = true; }

    const aliases = splitList(intent.aliases);
    if (aliases.length) { sup.aliases = unionList(sup.aliases, aliases); stats.fieldsUpdated.aliases++; touched = true; }

    const negEx = splitList(intent.negativeExamples);
    if (negEx.length) { sup.negativeExamples = unionList(sup.negativeExamples, negEx); stats.fieldsUpdated.negativeExamples++; touched = true; }

    if (intent.disambiguationHints && intent.disambiguationHints.trim()) {
      sup.disambiguationHints = intent.disambiguationHints.trim();
      stats.fieldsUpdated.disambiguationHints++;
      touched = true;
    }

    // confusableIntents: intake 值可能是中文路径引用或 v1 intent id
    // 尽力而为：如果值能在 v1ToLeaf 里找到，转为 leaf id；否则保留原始值
    const confusRaw = splitList(intent.confusableIntents);
    if (confusRaw.length) {
      const confusResolved = confusRaw.map(c => v1ToLeaf.get(c) || c);
      const unresolved = confusResolved.filter(c => !v1ToLeaf.has(confusRaw[confusResolved.indexOf(c)]));
      sup.confusableIntents = unionList(sup.confusableIntents, confusResolved);
      stats.fieldsUpdated.confusableIntents++;
      touched = true;
    }

    if (intent.lifecycle && intent.lifecycle.trim()) {
      sup.lifecycle = intent.lifecycle.trim();
      stats.fieldsUpdated.lifecycle++;
      touched = true;
    }

    if (touched) {
      sidecar[leafId] = sup;
      stats.mapped++;
    }
  }

  if (!dryRun) {
    fs.writeFileSync(SIDECAR_PATH, JSON.stringify(sidecar, null, 2));
  }

  console.log('[bridge-intake] batch:', stats.batchId);
  console.log('[bridge-intake] total intents:', stats.total);
  console.log('[bridge-intake] mapped:', stats.mapped);
  console.log('[bridge-intake] unmapped:', stats.unmapped);
  console.log('[bridge-intake] fields updated:', JSON.stringify(stats.fieldsUpdated));
  console.log('[bridge-intake] sidecar leaves (before):', stats.existingSidecarLeaves);
  console.log('[bridge-intake] sidecar leaves (after):', Object.keys(sidecar).length);
  console.log('[bridge-intake] dry-run:', dryRun);
  if (!dryRun) {
    console.log('[bridge-intake] output:', SIDECAR_PATH);
  }

  if (stats.unmapped > 0) {
    console.warn(`\n[bridge-intake] WARNING: ${stats.unmapped} intent(s) could not be mapped to v4 leaf id.`);
    console.warn('[bridge-intake] Unmapped details:');
    for (const d of stats.unmappedDetails) {
      console.warn(`  - ${d.name} (${d.ref || d.code || 'no code'}): ${d.reason}`);
    }
    console.warn('\n[bridge-intake] To fix: fill the "code" (意图编码) column in intake Excel');
    console.warn('[bridge-intake] with the v1 intent id (e.g. ccb.cashflow.salary-plan).');
    console.warn('[bridge-intake] The v1 intent id can be found in graph.ccb.intents.v1.0.0.json');
    console.warn('[bridge-intake] nodes[].id where type === "intent".');
  }

  return stats;
}

// CLI entry
const dryRun = process.argv.includes('--dry-run');
const arg = process.argv.filter(a => !a.startsWith('--')).pop();
const batchPath = arg && arg.endsWith('.json') && arg.includes('batch')
  ? path.resolve(arg)
  : findLatestBatch();

if (!batchPath || !fs.existsSync(batchPath)) {
  console.error('[bridge-intake] No batch.json found.');
  console.error('[bridge-intake] Usage: node services/ai-native-bank/scripts/bridge-intake-to-v4-supplements.js [path/to/batch.json] [--dry-run]');
  process.exit(1);
}

console.log(`[bridge-intake] Reading batch: ${batchPath}`);
bridge(batchPath, dryRun);
