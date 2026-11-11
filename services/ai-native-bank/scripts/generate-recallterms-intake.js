'use strict';

/**
 * generate-recallterms-intake.js
 *
 * 从 v4.json 提取所有 leaf 意图，生成预填了 code（v1 intent id）和已有 recallTerms
 * 的 intake 兼容 Excel（一体化填写格式），让业务人员只需补齐缺失的召回关键词。
 *
 * 产出路径：data/knowledge-intake/recallterms-prefill-<date>.xlsx
 *
 * 用法：
 *   node services/ai-native-bank/scripts/generate-recallterms-intake.js
 *
 * 桥接路径（完整闭环）：
 *   本脚本生成 Excel → 业务人员补 recallTerms → intake 导入 → batch.json
 *   → bridge-intake-to-v4-supplements.js → sidecar → rebuild-v4-fixed.js → v4-fixed.json
 */

const fs = require('fs');
const path = require('path');
const XLSX = require('xlsx');

const { INTENT_RECALL_TERMS } = require('../intent-profiles.js');

const GRAPH_DIR = path.resolve(__dirname, '../../../data/ai-native-bank/intent-graph');
const V4_PATH = path.join(GRAPH_DIR, 'archive', 'ccb-mobile-bank-intents.v4.json');
const INTAKE_DIR = path.resolve(__dirname, '../../../data/knowledge-intake');

// 预填列（FLAT_COLUMNS 子集，只保留业务人员需要的列）
const COLUMNS = [
  { key: 'path',                name: '功能全路径' },
  { key: 'iname',               name: '意图名称' },
  { key: 'code',                name: '意图编码' },
  { key: 'idesc',               name: '意图描述' },
  { key: 'recallTerms',         name: '召回关键词' },
  { key: 'aliases',             name: '别名列表' },
  { key: 'negativeExamples',    name: '负例查询' },
  { key: 'iconfusable',         name: '易混意图' },
  { key: 'disambiguationHints', name: '消歧提示' },
  { key: 'lifecycle',           name: '生命周期' },
  { key: 'istatus',             name: '意图状态' },
  { key: 'note',                name: '备注' },
];

function readJSON(p) {
  return JSON.parse(fs.readFileSync(p, 'utf-8'));
}

function joinList(arr) {
  if (!Array.isArray(arr) || arr.length === 0) return '';
  return arr.join(', ');
}

function extractV1Id(leaf) {
  const srs = (leaf.observability && leaf.observability.sourceRecords) || [];
  for (const sr of srs) {
    if (sr.ref && sr.ref.startsWith('v2-source:')) {
      return sr.ref.slice('v2-source:'.length);
    }
  }
  return '';
}

function dedupe(arr) {
  const seen = new Set();
  const out = [];
  for (const item of arr) {
    const k = String(item).trim();
    if (k && !seen.has(k)) { seen.add(k); out.push(k); }
  }
  return out;
}

function generate() {
  const v4 = readJSON(V4_PATH);
  const leaves = (v4.intentTree || []).filter(function (n) { return n.node_role === 'leaf'; });

  const rows = [];
  let missingCode = 0;
  let missingRecall = 0;
  let enrichedFromProfiles = 0;

  for (const leaf of leaves) {
    const code = extractV1Id(leaf);
    if (!code) {
      missingCode++;
      continue;
    }

    const fullPath = leaf.path || leaf.id;
    const lastSlash = fullPath.lastIndexOf('/');
    const funcPath = lastSlash > 0 ? fullPath.substring(0, lastSlash) : fullPath;
    const iname = leaf.title || fullPath.substring(lastSlash + 1);

    const scene = (leaf.scenes && leaf.scenes[0]) || {};
    const rp = scene.retrieval_profile || {};
    const sd = scene.semantic_definition || {};

    // 合并 v4.json 已有 recallTerms + INTENT_RECALL_TERMS 查找
    const v4Terms = Array.isArray(rp.recallTerms) ? rp.recallTerms : [];
    const profileTerms = INTENT_RECALL_TERMS[code] || [];
    let recallTerms = dedupe(v4Terms.concat(profileTerms));
    if (profileTerms.length > 0 && v4Terms.length === 0) enrichedFromProfiles++;
    const recallStr = recallTerms.join(', ');
    if (!recallStr) missingRecall++;

    rows.push({
      path: funcPath,
      iname: iname,
      code: code,
      idesc: sd.description || '',
      recallTerms: recallStr,
      aliases: joinList(rp.aliases),
      negativeExamples: joinList(rp.negativeExamples),
      iconfusable: joinList(sd.confusableIntents),
      disambiguationHints: sd.disambiguationHints || '',
      lifecycle: leaf.lifecycle || '',
      istatus: '生效',
      note: recallStr ? '' : '请补充召回关键词',
    });
  }

  // 生成 Excel
  const header = COLUMNS.map(function (c) { return c.name; });
  const data = rows.map(function (r) {
    return COLUMNS.map(function (c) { return r[c.key] || ''; });
  });
  const wb = XLSX.utils.book_new();
  const ws = XLSX.utils.aoa_to_sheet([header].concat(data));
  ws['!cols'] = COLUMNS.map(function (c) {
    return { wch: Math.max(c.name.length * 2 + 4, 18) };
  });
  ws['!cols'][COLUMNS.findIndex(function (c) { return c.key === 'recallTerms'; })] = { wch: 40 };
  ws['!cols'][COLUMNS.findIndex(function (c) { return c.key === 'aliases'; })] = { wch: 30 };
  ws['!cols'][COLUMNS.findIndex(function (c) { return c.key === 'note'; })] = { wch: 24 };
  XLSX.utils.book_append_sheet(wb, ws, '一体化填写');

  // 输出
  if (!fs.existsSync(INTAKE_DIR)) {
    fs.mkdirSync(INTAKE_DIR, { recursive: true });
  }
  const ts = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const outPath = path.join(INTAKE_DIR, 'recallterms-prefill-' + ts + '.xlsx');
  XLSX.writeFile(wb, outPath);

  console.log('[generate-recallterms-intake] leaves total:        ' + leaves.length);
  console.log('[generate-recallterms-intake] leaves with code:    ' + rows.length);
  console.log('[generate-recallterms-intake] missing code (skip): ' + missingCode);
  console.log('[generate-recallterms-intake] missing recallTerms: ' + missingRecall);
  console.log('[generate-recallterms-intake] enriched from profiles: ' + enrichedFromProfiles);
  console.log('[generate-recallterms-intake] output: ' + outPath);
}

generate();
