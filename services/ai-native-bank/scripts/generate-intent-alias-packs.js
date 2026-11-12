'use strict';

/**
 * LLM 批量生成意图口语别名包（对齐 AI Harbor intent-graph batch 方法论）。
 *
 * 规范依据（.claude/rules/intent-engine-generalization.md）：
 * - r6 禁止把 badcase 原文手工穷举进图谱；口语覆盖必须由构建管道系统化生成。
 * - r20 修复构建脚本而非生成物；本脚本产出 pack，由 intent-graph-builder.js 在
 *   构建期确定性合并进 aliases，并记录证据元数据。
 * - r8 评测期望语句不得回灌图谱：本脚本仅以意图的规范语义（title/description/
 *   已有别名）为输入，与任何评测语料无关。
 *
 * 鉴权：模型代理 Bearer token 在调用时从运行时容器 secret 文件读取，绝不持久化。
 *
 * Usage: node services/ai-native-bank/scripts/generate-intent-alias-packs.js
 * Env:
 *   AI_BANK_HARBOR_BASE_URL   default http://localhost:3012
 *   AI_BANK_ALIAS_PACK_OUT    default data/ai-native-bank/intent-graph/alias-packs.v2.1.json
 *   AI_BANK_RUNTIME_CONTAINER default ai-native-bank-agent-runtime
 *   AI_BANK_RUNTIME_TOKEN_FILE default /run/ai-bank-secrets/runtime-token
 *   AI_BANK_ALIAS_LLM_MODEL   default deepseek-chat
 */

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const GRAPH_ID = 'ccb-mobile-bank-intents';
const GRAPH_PATH = path.join(__dirname, '..', '..', '..', 'data', 'ai-native-bank', 'intent-graph', 'graph.ccb.intents.v1.0.0.json');
const BASE_URL = (process.env.AI_BANK_HARBOR_BASE_URL || 'http://localhost:3012').replace(/\/+$/, '');
const CHAT_URL = `${BASE_URL}/api/ai-bank/v1/internal/model/v1/chat/completions`;
const OUT_PATH = process.env.AI_BANK_ALIAS_PACK_OUT
  || path.join(__dirname, '..', '..', '..', 'data', 'ai-native-bank', 'intent-graph', 'alias-packs.v2.1.json');
const CONTAINER = process.env.AI_BANK_RUNTIME_CONTAINER || 'ai-native-bank-agent-runtime';
const TOKEN_FILE = process.env.AI_BANK_RUNTIME_TOKEN_FILE || '/run/ai-bank-secrets/runtime-token';
const MODEL = process.env.AI_BANK_ALIAS_LLM_MODEL || 'deepseek-chat';
const BATCH_SIZE = 8;
const ALIASES_PER_INTENT = 16;
const MAX_RETRIES = 2;
const ALIAS_MIN = 2;
const ALIAS_MAX = 24;

function readRuntimeToken() {
  const result = spawnSync('docker', ['exec', CONTAINER, 'cat', TOKEN_FILE], {
    encoding: 'utf8',
    timeout: 15000,
  });
  const token = String(result.stdout || '').trim();
  if (result.status !== 0 || !token) {
    throw new Error(`runtime token is unavailable from ${CONTAINER}:${TOKEN_FILE}`);
  }
  return token;
}

async function chatJson(token, messages) {
  const response = await fetch(CHAT_URL, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: MODEL,
      messages,
      temperature: 0.4,
      max_tokens: 8000,
    }),
  });
  if (!response.ok) {
    throw new Error(`chat upstream status ${response.status}: ${(await response.text()).slice(0, 200)}`);
  }
  const payload = await response.json();
  const content = payload.choices && payload.choices[0] && payload.choices[0].message
    ? payload.choices[0].message.content : '';
  return extractJsonArray(content);
}

function extractJsonArray(content) {
  const text = String(content || '').replace(/```json/gi, '```').replace(/```/g, '');
  const start = text.indexOf('{');
  const fallbackStart = text.indexOf('[');
  const candidate = start >= 0 ? text.slice(start) : (fallbackStart >= 0 ? text.slice(fallbackStart) : '');
  if (!candidate) throw new Error('model response contains no JSON');
  try {
    return JSON.parse(candidate.trim());
  } catch (error) {
    const firstBracket = candidate.indexOf('[');
    const lastBracket = candidate.lastIndexOf(']');
    if (firstBracket >= 0 && lastBracket > firstBracket) {
      return JSON.parse(candidate.slice(firstBracket, lastBracket + 1));
    }
    throw error;
  }
}

function intentPrompt(batch) {
  const descriptions = batch.map(intent => {
    const existing = [...(intent.aliases || []), ...(intent.recallTerms || [])].slice(0, 12).join('、');
    return [
      `- intentId: ${intent.id}`,
      `  标题: ${intent.title}`,
      `  语义: ${(intent.description || intent.title).slice(0, 120)}`,
      `  已有别名: ${existing || '（无）'}`,
    ].join('\n');
  }).join('\n');
  return [
    '你是银行手机 App 意图召回的语料专家。下面给出若干个意图节点，每个意图代表用户的一个目标。',
    '请为每个意图生成用户可能说的口语化说法（中文，短句）。',
    '',
    '硬性要求：',
    `1. 每个意图生成 ${ALIASES_PER_INTENT} 条，必须表达与该意图完全相同的目标，不得混入其他意图的目标；`,
    '2. 说法必须自然、口语化、多样化（动宾式、名词式、疑问式等结构都要有），不要只改换同义词；',
    '3. 必须覆盖不同的说法家族，各条要摊到尽可能多的家族上，禁止一半以上挤在同一批同义词里：',
    '   - 词群家族：同一目标下用户会用不同词群表达（例如查交易类：交易/流水/明细/进出账/消费记录/账目 各算一个家族）；',
    '   - 操作家族：若该意图含办理/管理/设置属性，必须包含"怎么开通/怎么办理/怎么申请/怎么设置/怎么取消"等操作型说法，禁止全部是查记录看进度类；',
    '4. 不得与"已有别名"重复；不得照抄标题；不要出现具体卡号、金额、日期占位符；',
    '5. 不得包含其他意图的标志性词汇（例如转账类意图不要出现"账单"，账单类意图不要出现"转账"）；',
    `6. 每条长度 ${ALIAS_MIN}-${ALIAS_MAX} 个字符；只输出 JSON，不要任何解释。`,
    '',
    '输出格式：',
    '{"packs":[{"intentId":"<id>","aliases":["..."]}]}',
    '',
    '意图列表：',
    descriptions,
  ].join('\n');
}

function normalizeAlias(value) {
  return String(value || '').trim().replace(/\s+/g, '');
}

function usableAlias(alias) {
  return alias.length >= ALIAS_MIN && alias.length <= ALIAS_MAX
    && !/[0-9]{3,}/.test(alias)
    && !/[，。！？；：、""''【】<>{}[\]()]/.test(alias);
}

function loadPreviousPacks(outPath) {
  if (!fs.existsSync(outPath)) return {};
  try {
    const previous = JSON.parse(fs.readFileSync(outPath, 'utf8'));
    return previous.graphId === GRAPH_ID && previous.packs && typeof previous.packs === 'object'
      ? previous.packs : {};
  } catch (error) {
    console.warn(`  previous pack file unreadable, starting fresh: ${error.message}`);
    return {};
  }
}

async function main() {
  const graph = JSON.parse(fs.readFileSync(GRAPH_PATH, 'utf8'));
  if (graph.graphId !== GRAPH_ID) throw new Error('graph id is invalid');
  const intents = graph.nodes
    .filter(node => node.type === 'intent' && node.lifecycle === 'active')
    .sort((left, right) => left.id.localeCompare(right.id, 'en'));
  console.log(`intents=${intents.length} model=${MODEL} endpoint=${CHAT_URL}`);

  const token = readRuntimeToken();
  const batches = [];
  for (let start = 0; start < intents.length; start += BATCH_SIZE) {
    batches.push(intents.slice(start, start + BATCH_SIZE));
  }

  // 存量 pack 结转：再生成时保留旧别名（否则整文件替换会丢历史覆盖），跨意图所有权沿用旧判定。
  const previousPacks = loadPreviousPacks(OUT_PATH);
  const carriedByIntent = new Map();
  const ownerByAlias = new Map();
  for (const intent of intents) {
    const owned = normalizeAliasList(previousPacks[intent.id] && previousPacks[intent.id].aliases);
    if (!owned.length) continue;
    carriedByIntent.set(intent.id, owned);
    for (const alias of owned) ownerByAlias.set(alias, intent.id);
  }

  const packs = {};
  let generated = 0;
  let carried = 0;
  let droppedFormat = 0;
  const knownByIntent = new Map(intents.map(intent => [intent.id, new Set([
    ...normalizeAliasList(intent.aliases),
    ...normalizeAliasList(intent.recallTerms),
    normalizeAlias(intent.title),
    ...(carriedByIntent.get(intent.id) || []),
  ])]));

  for (let index = 0; index < batches.length; index += 1) {
    const batch = batches[index];
    const batchId = `batch${String(index + 1).padStart(2, '0')}`;
    let parsed = null;
    let lastError = null;
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt += 1) {
      try {
        parsed = await chatJson(token, [
          { role: 'system', content: '你只输出严格合法的 JSON。' },
          { role: 'user', content: intentPrompt(batch) },
        ]);
        break;
      } catch (error) {
        lastError = error;
        console.warn(`  ${batchId} attempt ${attempt + 1} failed: ${error.message}`);
      }
    }
    if (!parsed) {
      throw new Error(`batch ${batchId} failed after retries: ${lastError && lastError.message}`);
    }
    const rows = Array.isArray(parsed) ? parsed : (parsed.packs || []);
    const byId = new Map(rows.filter(row => row && row.intentId).map(row => [row.intentId, row.aliases || []]));
    for (const intent of batch) {
      const known = knownByIntent.get(intent.id);
      const accepted = [];
      for (const raw of byId.get(intent.id) || []) {
        const alias = normalizeAlias(raw);
        if (!usableAlias(alias) || known.has(alias)) { droppedFormat += 1; continue; }
        if (ownerByAlias.has(alias)) {
          // 跨意图重复说法有歧义，全部丢弃，避免穷举召回污染裁决。
          console.warn(`  drop cross-intent duplicate "${alias}" (${intent.id} vs ${ownerByAlias.get(alias)})`);
          continue;
        }
        ownerByAlias.set(alias, intent.id);
        known.add(alias);
        accepted.push(alias);
      }
      const carriedAliases = carriedByIntent.get(intent.id) || [];
      carried += carriedAliases.length;
      generated += accepted.length;
      packs[intent.id] = { aliases: [...new Set([...carriedAliases, ...accepted])], batch: batchId };
    }
    process.stdout.write(`  ${batchId} done (${index + 1}/${batches.length})\n`);
  }

  const pack = {
    schemaVersion: '1.0',
    graphId: GRAPH_ID,
    baseGraphVersion: graph.version,
    generatedAt: new Date().toISOString(),
    generator: {
      type: 'llm-batch',
      model: MODEL,
      endpoint: CHAT_URL,
      batchSize: BATCH_SIZE,
      aliasesPerIntent: ALIASES_PER_INTENT,
    },
    packs,
    stats: {
      intents: intents.length,
      batches: batches.length,
      generated,
      carried,
      droppedFormatOrDuplicate: droppedFormat,
    },
  };
  fs.mkdirSync(path.dirname(OUT_PATH), { recursive: true });
  fs.writeFileSync(OUT_PATH, `${JSON.stringify(pack, null, 2)}\n`, 'utf8');
  console.log(`pack written: ${OUT_PATH}`);
  console.log(`stats: ${JSON.stringify(pack.stats)}`);
}

function normalizeAliasList(values) {
  return (Array.isArray(values) ? values : []).map(normalizeAlias).filter(Boolean);
}

main().catch(error => {
  console.error('GENERATION FAILED:', error.message);
  process.exit(1);
});
