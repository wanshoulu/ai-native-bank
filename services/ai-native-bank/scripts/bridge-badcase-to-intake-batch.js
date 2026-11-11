'use strict';

/**
 * bridge-badcase-to-intake-batch.js
 *
 * 把 regress-intent-badcases.js 产出的 patch_sample.json 里仍失败的 case
 * 转换为 intake 兼容的 batch.json，让现有管道（bridge-intake-to-v4-supplements.js
 * → rebuild-v4-fixed.js）能把 badcase query 回流为 v4 leaf 的 aliases（说法）。
 *
 * 回流策略：
 *   仍失败 case 的 query → expected_intent 的 aliases（说法）
 *   仍失败 case 的 query → predicted(误召回) intent 的 negativeExamples（负例）
 *
 * 映射问题：
 *   patch_sample.json 的 expected_intents.intent_id 是 v2 图谱 ID（intent_eval_* / intent_scene_*）
 *   v4 leaf 的 ID 是 ccb.* 路径形式，二者无直接映射
 *   本脚本用 display_name + action_code 模糊匹配，把 badcase expected_intents
 *   映射到 v4 leaf，提取 v1Id（code）供下游 bridge 脚本使用
 *
 * 产出：
 *   data/knowledge-intake/badcase-reflow-<date>/batch.json  —— matched，可被 bridge 脚本消费
 *   data/knowledge-intake/badcase-reflow-<date>/unmatched.xlsx —— unmatched，业务人员补 code 列
 *
 * 用法：
 *   node services/ai-native-bank/scripts/bridge-badcase-to-intake-batch.js [path/to/patch_sample.json] [--dry-run]
 */

const fs = require('fs');
const path = require('path');
const XLSX = require('xlsx');

const ROOT = path.resolve(__dirname, '../../..');
const GRAPH_DIR = path.join(ROOT, 'data', 'ai-native-bank', 'intent-graph');
const V4_PATH = path.join(GRAPH_DIR, 'archive', 'ccb-mobile-bank-intents.v4.json');
const PATCH_DEFAULT = path.join(ROOT, 'data', 'eval', 'intent_eval_deepseek_badcase_patch_sample.json');
const INTAKE_DIR = path.join(ROOT, 'data', 'knowledge-intake');

// ---- 工具函数 ----

function readJSON(p) {
  return JSON.parse(fs.readFileSync(p, 'utf-8'));
}

function normalize(s) {
  return String(s || '')
    .toLowerCase()
    .replace(/\s+/g, '')
    .replace(/[·,，。！？、,.!?;；:："'""''（）()【】\[\]{}<>《》/-]/g, '');
}

function extractV1Id(leaf) {
  const srs = (leaf.observability && leaf.observability.sourceRecords) || [];
  for (const sr of srs) {
    if (sr.ref && sr.ref.startsWith('v2-source:')) return sr.ref.slice('v2-source:'.length);
  }
  return '';
}

function extractActionCode(leaf) {
  const scene = (leaf.scenes && leaf.scenes[0]) || {};
  const tb = scene.task_binding || {};
  return (tb.actionCode || '').toLowerCase();
}

function extractSceneType(leaf) {
  const scene = (leaf.scenes && leaf.scenes[0]) || {};
  return (scene.scene_type || '').toLowerCase();
}

// action_code → scene_type 兜底映射（badcase action_code 到 v4 scene_type）
function actionToScene(actionCode) {
  const a = String(actionCode || '').toLowerCase();
  const map = {
    query: 'query',
    ask: 'help',
    help: 'help',
    apply: 'do',
    repay: 'do',
    transfer: 'do',
    open: 'do',
    change: 'change',
    set: 'change',
    close: 'do',
    cancel: 'do',
  };
  return map[a] || '';
}

// 从 v4 leaf 提取可用于匹配的 display 候选词
function displayCandidates(leaf) {
  const candidates = new Set();
  const title = leaf.title || '';
  // title 按 · 切分，取各段
  for (const seg of title.split(/[·・]/)) {
    const n = normalize(seg);
    if (n && n.length >= 2) candidates.add(n);
  }
  // path 最后一段
  const fullPath = leaf.path || '';
  const lastSlash = fullPath.lastIndexOf('/');
  const lastSeg = lastSlash > 0 ? fullPath.substring(lastSlash + 1) : fullPath;
  for (const seg of lastSeg.split(/[·・]/)) {
    const n = normalize(seg);
    if (n && n.length >= 2) candidates.add(n);
  }
  return [...candidates];
}

// 从 path 提取 domain 段（倒数第二段）
function extractDomain(leaf) {
  const fullPath = leaf.path || '';
  const parts = fullPath.split('/').filter(Boolean);
  // path 格式: 建设银行手机银行/<domain>/<...>/<title>
  // domain 是倒数第二到第三段
  if (parts.length >= 3) return normalize(parts[parts.length - 2] || '');
  return '';
}

// ---- 构建 leaves 列表 + 精确索引 ----

function buildLeafList(v4) {
  const leaves = (v4.intentTree || []).filter(n => n.node_role === 'leaf');
  const list = [];
  const exactIndex = new Map(); // normalized display_name → [leafIdx]
  for (const leaf of leaves) {
    const v1Id = extractV1Id(leaf);
    if (!v1Id) continue;
    const actionCode = extractActionCode(leaf);
    const sceneType = extractSceneType(leaf);
    const domain = extractDomain(leaf);
    const names = displayCandidates(leaf);
    const entry = {
      leafId: leaf.id,
      v1Id,
      actionCode,
      sceneType,
      domain,
      title: leaf.title || '',
      path: leaf.path || '',
      names, // display 候选词数组
    };
    const idx = list.length;
    list.push(entry);
    for (const name of names) {
      if (!exactIndex.has(name)) exactIndex.set(name, []);
      exactIndex.get(name).push(idx);
    }
  }
  return { list, exactIndex };
}

// 三层匹配：返回 {match, confidence, candidates}
function matchExpectedTiered(intent, list, exactIndex) {
  const dn = normalize(intent.display_name || intent.intent_name || '');
  if (!dn) return { match: null, confidence: 'none', candidates: [] };
  const actionCode = String(intent.action_code || '').toLowerCase();
  const sceneGuess = actionToScene(actionCode);
  const domain = normalize(intent.domain || '');

  // Tier 1: 精确 display_name 匹配
  const exactHits = (exactIndex.get(dn) || []).map(i => list[i]);
  if (exactHits.length === 1) return { match: exactHits[0], confidence: 'exact-unique', candidates: exactHits };
  if (exactHits.length > 1) {
    // 多候选：用 action_code / scene_type 消歧
    const byAction = exactHits.filter(e => e.actionCode === actionCode);
    if (byAction.length === 1) return { match: byAction[0], confidence: 'exact+action', candidates: exactHits };
    if (byAction.length > 1) return { match: byAction[0], confidence: 'exact+action(first)', candidates: exactHits };
    if (sceneGuess) {
      const byScene = exactHits.filter(e => e.sceneType === sceneGuess);
      if (byScene.length >= 1) return { match: byScene[0], confidence: 'exact+scene(first)', candidates: exactHits };
    }
    // 精确匹配但无法消歧 → 给候选，标记低置信
    return { match: exactHits[0], confidence: 'exact-ambiguous(first)', candidates: exactHits };
  }

  // Tier 2: 子串匹配（display_name 是 title segment 的子串，或反过来）
  const substrHits = [];
  for (const entry of list) {
    for (const name of entry.names) {
      if (name.includes(dn) || dn.includes(name)) {
        substrHits.push(entry);
        break;
      }
    }
  }
  if (substrHits.length === 0) return { match: null, confidence: 'none', candidates: [] };

  // 子串匹配消歧：action_code + domain
  const substrByAction = substrHits.filter(e => e.actionCode === actionCode);
  const substrByActionDomain = substrByAction.filter(e => domain && e.domain === domain);

  if (substrByActionDomain.length === 1) return { match: substrByActionDomain[0], confidence: 'substr+action+domain', candidates: substrHits };
  if (substrByActionDomain.length > 1) return { match: substrByActionDomain[0], confidence: 'substr+action+domain(first)', candidates: substrHits };
  if (substrByAction.length === 1) return { match: substrByAction[0], confidence: 'substr+action', candidates: substrHits };
  if (substrByAction.length > 1) {
    // 子串+action 仍多候选 → 不自动匹配，给候选列表
    return { match: null, confidence: 'substr+action-ambiguous', candidates: substrByAction };
  }
  if (sceneGuess) {
    const byScene = substrHits.filter(e => e.sceneType === sceneGuess);
    if (byScene.length === 1) return { match: byScene[0], confidence: 'substr+scene', candidates: substrHits };
  }

  // 子串匹配但无法消歧 → 不自动匹配
  return { match: null, confidence: 'substr-ambiguous', candidates: substrHits };
}

// 旧接口兼容（单参数 index → 新接口 list+exactIndex）
function matchExpected(intent, index) {
  // index 是 {list, exactIndex} 形式
  const result = matchExpectedTiered(intent, index.list, index.exactIndex);
  return result.match;
}

// ---- 主逻辑 ----

function bridge(patchPath, dryRun) {
  const patch = readJSON(patchPath);
  const v4 = readJSON(V4_PATH);
  const { list, exactIndex } = buildLeafList(v4);

  const rows = (patch.rows || []).filter(r => r.score && !r.score.relaxed_correct && !r.error);
  console.log('[badcase-reflow] patch rows total:', (patch.rows || []).length);
  console.log('[badcase-reflow] still-failing rows:', rows.length);

  // 按 leaf 聚合：每个 leaf 收集新增 aliases（expected 方向）和 negativeExamples（predicted 方向）
  const leafUpdates = new Map(); // leafId → {v1Id, title, path, aliasesAdd: Set, negativesAdd: Set, cases: []}
  const unmatched = []; // {case_id, query, display_name, action_code, intent_name, candidates: []}
  const confidenceCounts = {};

  function bumpConfidence(conf) {
    confidenceCounts[conf] = (confidenceCounts[conf] || 0) + 1;
  }

  for (const row of rows) {
    const query = (row.query || '').trim();
    if (!query) continue;

    // expected_intents → 给这些 leaf 加 alias（说法）
    const expectedMatched = [];
    for (const exp of (row.expected_intents || [])) {
      const result = matchExpectedTiered(exp, list, exactIndex);
      bumpConfidence(result.confidence);
      if (result.match) {
        expectedMatched.push(result.match);
        if (!leafUpdates.has(result.match.leafId)) {
          leafUpdates.set(result.match.leafId, {
            v1Id: result.match.v1Id,
            title: result.match.title,
            path: result.match.path,
            aliasesAdd: new Set(),
            negativesAdd: new Set(),
            cases: [],
          });
        }
        const u = leafUpdates.get(result.match.leafId);
        u.aliasesAdd.add(query);
        u.cases.push({ case_id: row.case_id, query, direction: 'expected', display_name: exp.display_name, confidence: result.confidence });
      } else {
        unmatched.push({
          case_id: row.case_id,
          query,
          display_name: exp.display_name || '',
          action_code: exp.action_code || '',
          intent_name: exp.intent_name || '',
          domain: exp.domain || '',
          direction: 'expected',
          confidence: result.confidence,
          candidates: (result.candidates || []).slice(0, 5).map(c => ({ leafId: c.leafId, v1Id: c.v1Id, title: c.title, actionCode: c.actionCode })),
        });
      }
    }

    // predicted(误召回) → 给这些 leaf 加 negativeExample
    for (const pred of (row.authoritative_results || [])) {
      const predResult = matchExpectedTiered({
        display_name: pred.display_name || pred.intent_name,
        action_code: pred.action_code,
        intent_name: pred.intent_name,
      }, list, exactIndex);
      bumpConfidence(predResult.confidence);
      if (predResult.match) {
        const isAlsoExpected = expectedMatched.some(m => m.leafId === predResult.match.leafId);
        if (isAlsoExpected) continue;
        if (!leafUpdates.has(predResult.match.leafId)) {
          leafUpdates.set(predResult.match.leafId, {
            v1Id: predResult.match.v1Id,
            title: predResult.match.title,
            path: predResult.match.path,
            aliasesAdd: new Set(),
            negativesAdd: new Set(),
            cases: [],
          });
        }
        const u = leafUpdates.get(predResult.match.leafId);
        u.negativesAdd.add(query);
        u.cases.push({ case_id: row.case_id, query, direction: 'predicted(误召回)', display_name: pred.display_name, confidence: predResult.confidence });
      }
    }
  }

  // 生成 intake 兼容 batch.json
  const ts = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const batchId = 'badcase-reflow-' + ts;
  const outDir = path.join(INTAKE_DIR, batchId);

  const intents = [];
  for (const [leafId, u] of leafUpdates) {
    intents.push({
      name: u.title,
      funcPath: '',
      intentType: '',
      description: '',
      goal: '',
      domain: '',
      menuKey: '',
      action: '',
      sourceAction: '',
      aliases: [...u.aliasesAdd].join(','),
      recallTerms: '',
      recallTermSets: '',
      negativeExamples: [...u.negativesAdd].join(','),
      semanticPriority: '',
      triggerScene: '',
      scopeIn: '',
      scopeOut: '',
      confusableIntents: '',
      disambiguationHints: '',
      executionMode: '',
      riskLevel: '',
      readiness: '',
      requiredSlots: '',
      answerRef: '',
      queryRef: '',
      menuRef: '',
      skillRef: '',
      workflowRef: '',
      agentRef: '',
      roles: '',
      businessObjects: '',
      keySlots: '',
      businessObjectId: '',
      answerForm: '',
      heat: '',
      taskPolicyShapes: '',
      taskPolicyMode: '',
      taskPolicyMaxTasks: '',
      contextPolicyRetain: '',
      contextPolicySwitch: '',
      contextPolicyResumable: '',
      responsePolicy: '',
      orchestrationMode: '',
      status: 'pending',
      lifecycle: '',
      semanticVersion: '',
      owner: '',
      sourceChannel: '',
      sourceMenuId: '',
      sourceActionId: '',
      code: u.v1Id,
      note: 'badcase回流: ' + u.cases.length + ' cases',
      rowId: 'i-bc-' + leafId.replace(/[^a-zA-Z0-9]/g, '').substring(0, 12),
      sourceStatus: '生效',
      ref: u.path,
    });
  }

  const batch = {
    batchId,
    fileName: 'badcase-reflow-' + ts,
    importedBy: 'badcase-bridge',
    importedAt: new Date().toISOString(),
    templateSheets: { functions: '', intents: '', utterances: '' },
    rejected: [],
    warnings: [],
    rows: {
      functions: [],
      intents,
      utterances: [],
    },
  };

  // 写文件
  if (!dryRun) {
    if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
    const batchPath = path.join(outDir, 'batch.json');
    fs.writeFileSync(batchPath, JSON.stringify(batch, null, 2), 'utf-8');
    console.log('[badcase-reflow] batch output:', batchPath);

    // unmatched → Excel（业务人员补 code 列）
    if (unmatched.length > 0) {
      const umCols = [
        { key: 'case_id', name: 'Case ID' },
        { key: 'query', name: '查询语句' },
        { key: 'display_name', name: '期望意图名称' },
        { key: 'action_code', name: '动作编码' },
        { key: 'intent_name', name: '意图全名' },
        { key: 'code', name: '意图编码(待补)' },
        { key: 'note', name: '备注' },
      ];
      const header = umCols.map(c => c.name);
      const data = unmatched.map(r => umCols.map(c => r[c.key] || ''));
      const wb = XLSX.utils.book_new();
      const ws = XLSX.utils.aoa_to_sheet([header].concat(data));
      ws['!cols'] = umCols.map(c => ({ wch: Math.max(c.name.length * 2 + 4, 18) }));
      ws['!cols'][1] = { wch: 40 };
      XLSX.utils.book_append_sheet(wb, ws, '待补意图编码');
      const xlsxPath = path.join(outDir, 'unmatched.xlsx');
      XLSX.writeFile(wb, xlsxPath);
      console.log('[badcase-reflow] unmatched Excel:', xlsxPath);
    }
  }

  // 统计
  const stats = {
    patchRows: (patch.rows || []).length,
    stillFailing: rows.length,
    matchedLeaves: leafUpdates.size,
    unmatchedIntents: unmatched.length,
    aliasesAdded: 0,
    negativesAdded: 0,
    confidence: confidenceCounts,
  };
  for (const u of leafUpdates.values()) {
    stats.aliasesAdded += u.aliasesAdd.size;
    stats.negativesAdded += u.negativesAdd.size;
  }
  console.log('[badcase-reflow] stats:', JSON.stringify(stats, null, 2));
  console.log('[badcase-reflow] dry-run:', dryRun);

  if (unmatched.length > 0) {
    console.warn('[badcase-reflow] WARNING: ' + unmatched.length + ' expected intents could not be matched to v4 leaf.');
    console.warn('[badcase-reflow] They are in the unmatched.xlsx for human review (fill the 意图编码 column).');
  }

  return stats;
}

// CLI
const dryRun = process.argv.includes('--dry-run');
const arg = process.argv.filter(a => !a.startsWith('--')).pop();
const patchPath = (arg && arg.endsWith('.json')) ? path.resolve(arg) : PATCH_DEFAULT;

if (!fs.existsSync(patchPath)) {
  console.error('[badcase-reflow] patch_sample.json not found:', patchPath);
  process.exit(1);
}

console.log('[badcase-reflow] reading patch:', patchPath);
bridge(patchPath, dryRun);
