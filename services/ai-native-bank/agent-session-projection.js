'use strict';

const { BankApiError } = require('./api-contract');
const { MAX_AGENT_SESSION_TURN_BYTES } = require('./agent-session-contract');

const REDACTION = '敏感内容已隐藏';
const MAX_TEXT_BYTES = MAX_AGENT_SESSION_TURN_BYTES;
const ALLOWED_ROLES = new Set(['user', 'assistant']);
const ALLOWED_RESPONSE_TYPES = new Set([
  'result', 'clarification', 'answer', 'confirmation', 'navigation', 'notice',
]);
const INTERNAL_ID_PATTERN = /\b(?:acct|acp|acg|bwf|cap|card|challenge|chk|checkpoint|conv|cust|dlg|execution|grant|order|payee|req|run|task|tok|token|trace|transfer|workflow)_[a-z0-9._:-]+\b/iu;
const INTERNAL_TEXT_MARKERS = Object.freeze([
  'confirmationtoken', 'tokenhash', 'authorizationtoken', 'accesstoken',
  'refreshtoken', 'grantid', 'checkpointid', 'businessworkflowid',
  'workflowid', 'agentrunid', 'conversationid', 'customerid', 'accountid',
]);
const NON_NETWORK_TLDS = new Set(['js']);
const DOMAIN_TOKEN_PATTERN = /[\p{L}\p{N}](?:[\p{L}\p{N}-]{0,62}\.){1,8}(?:xn--[a-z0-9-]{2,59}|[\p{L}]{2,63})(?::\d{1,5})?(?:[/?#][^\s<>{}\[\]]{0,2048})?/giu;
const IDEOGRAPHIC_DOMAIN_PATTERN = /[\p{L}\p{N}-]+(?:。[\p{L}\p{N}-]+)+/giu;
const COMMON_IDN_TLDS = new Set([
  '中国', '公司', '网络', '网址', '在线', '商店', '中文网', '我爱你', '移动',
  '集团', '世界', '信息', '餐厅', '游戏', '企业', '娱乐', '香港', '台湾', '澳门', '政务',
]);
const MAX_PERCENT_DECODE_PASSES = 8;
const MAX_PERCENT_RUN_BYTES = Math.floor(MAX_TEXT_BYTES / 3);
const ENCODED_URL_SCHEME_PATTERN = /\b(?:https?|wss?|ftp|file|mailto|javascript|data|blob)%(?:25){0,16}(?:3a|2f)/iu;
const EXPLICIT_CODEPOINT_ESCAPE_PATTERN = /%u[0-9a-f]{4}|\\u[0-9a-f]{4}|\\u\{[0-9a-f]{1,6}\}|\\x[0-9a-f]{2}|&#x[0-9a-f]{1,6};?|&#[0-9]{1,7};?|&(?:colon|sol|period);/iu;

function invalidTurn(message) {
  return new BankApiError(400, 'invalid_agent_session_turn', message);
}

function assertExactKeys(value, keys, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw invalidTurn(`${label}格式无效`);
  }
  const expected = new Set(keys);
  if (Object.keys(value).length !== keys.length
    || Object.keys(value).some(key => !expected.has(key))) {
    // 附带实际键集：exact-key 违约只报「未知字段」无法定位来源
    throw invalidTurn(`${label}包含未知字段（实际键=${Object.keys(value).join(',')}）`);
  }
}

function assertAllowedKeys(value, allowedKeys, requiredKeys, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw invalidTurn(`${label}格式无效`);
  }
  const allowed = new Set(allowedKeys);
  if (Object.keys(value).some(key => !allowed.has(key))
    || requiredKeys.some(key => !Object.prototype.hasOwnProperty.call(value, key))) {
    throw invalidTurn(`${label}包含未知字段或缺少必填字段`);
  }
}

function requirePublicText(value, label) {
  if (typeof value !== 'string') throw invalidTurn(`${label}必须是字符串`);
  if (value.includes('�')) {
    // 客户端编码错误（GBK 等非 UTF-8 字节流被按 UTF-8 解码）会产生替换符，坏文本拒绝入库
    throw new BankApiError(400, 'agent_session_turn_invalid', `${label}包含无效字符（客户端编码错误）`);
  }
  if (Buffer.byteLength(value, 'utf8') > MAX_TEXT_BYTES) {
    throw new BankApiError(413, 'agent_session_turn_too_large', `${label}超过存储上限`);
  }
  return value;
}

function redactSensitiveText(value) {
  const text = requirePublicText(value, '公开文本');
  const { forms: securityForms, unsafePercentEncoding } = decodedSecurityForms(text);
  const compactSecurityForms = securityForms.map(form => (
    form.replace(/[\p{P}\p{S}\p{C}\p{M}]/gu, '')
  ));
  if (unsafePercentEncoding
    || securityForms.some(containsExplicitCodepointEscape)
    || securityForms.some(containsUrlLikeText)
    || securityForms.some(containsSuspiciousEncodedUrl)
    || securityForms.some(containsFullAccountNumber)
    || compactSecurityForms.some(containsFullAccountNumber)
    || securityForms.some(containsInternalText)
    || securityForms.some(containsOpaqueSecret)
    || compactSecurityForms.some(containsOpaqueSecret)) {
    return REDACTION;
  }
  let redacted = text;
  redacted = redacted.replace(/\b(?:https?|ftp):\/\/[^\s<>{}\[\]]+/giu, REDACTION);
  redacted = redacted.replace(/\b(?:mailto|data|javascript):[^\s<>{}\[\]]+/giu, REDACTION);
  redacted = redacted.replace(/\bwww\.[^\s<>{}\[\]]+/giu, REDACTION);
  redacted = redacted.replace(
    /\b(?:acct|acp|acg|bwf|cap|card|challenge|chk|checkpoint|conv|cust|dlg|execution|grant|order|payee|req|run|task|tok|token|trace|transfer|workflow)_[A-Za-z0-9._:-]+\b/giu,
    REDACTION,
  );
  redacted = redacted.replace(
    /(?:confirmation\s*token|确认\s*(?:token|令牌)|token|grant|checkpoint|workflowId|businessWorkflowId|internal\s*id|内部\s*ID)\s*[:=]?\s*[A-Za-z0-9._:-]+/giu,
    REDACTION,
  );
  return redacted;
}

function isSafeAgentSessionText(value) {
  if (typeof value !== 'string') return false;
  try {
    return redactSensitiveText(value) === value;
  } catch (error) {
    return false;
  }
}

function normalizeSecurityText(value) {
  return String(value).normalize('NFKC').toLowerCase()
    .replace(/[\uff0e\uff61]/gu, '.');
}

function isHexDigit(character) {
  return (character >= '0' && character <= '9')
    || (character >= 'a' && character <= 'f')
    || (character >= 'A' && character <= 'F');
}

function isPercentByteAt(value, index) {
  return value[index] === '%'
    && index + 2 < value.length
    && isHexDigit(value[index + 1])
    && isHexDigit(value[index + 2]);
}

function decodePercentRun(run) {
  try {
    return decodeURIComponent(run);
  } catch (_) {
    return null;
  }
}

function decodeValidPercentRuns(value) {
  const parts = [];
  let unchangedStart = 0;
  let index = 0;
  let invalidRun = false;
  while (index < value.length) {
    if (!isPercentByteAt(value, index)) {
      index += 1;
      continue;
    }
    const runStart = index;
    let byteCount = 0;
    while (byteCount < MAX_PERCENT_RUN_BYTES && isPercentByteAt(value, index)) {
      index += 3;
      byteCount += 1;
    }
    const run = value.slice(runStart, index);
    const decoded = decodePercentRun(run);
    if (decoded === null) invalidRun = true;
    parts.push(value.slice(unchangedStart, runStart), decoded === null ? run : decoded);
    unchangedStart = index;
  }
  if (unchangedStart === 0) return { value, changed: false, invalidRun: false };
  parts.push(value.slice(unchangedStart));
  const decodedValue = parts.join('');
  return { value: decodedValue, changed: decodedValue !== value, invalidRun };
}

function decodedSecurityForms(value) {
  const forms = [];
  let current = normalizeSecurityText(value);
  for (let pass = 0; pass <= MAX_PERCENT_DECODE_PASSES; pass += 1) {
    const detectionForm = current.replace(/[\p{Cf}\p{Mn}\p{Me}]/gu, '');
    if (!forms.includes(detectionForm)) forms.push(detectionForm);
    const probe = decodeValidPercentRuns(current);
    if (probe.invalidRun) return { forms, unsafePercentEncoding: true };
    if (pass === MAX_PERCENT_DECODE_PASSES) {
      return { forms, unsafePercentEncoding: probe.changed };
    }
    if (!probe.changed) return { forms, unsafePercentEncoding: false };
    current = normalizeSecurityText(probe.value);
  }
  return { forms, unsafePercentEncoding: false };
}

function containsSuspiciousEncodedUrl(value) {
  return ENCODED_URL_SCHEME_PATTERN.test(value);
}

function containsExplicitCodepointEscape(value) {
  return EXPLICIT_CODEPOINT_ESCAPE_PATTERN.test(value);
}

function containsUrlLikeText(value) {
  if (/\b[a-z][a-z0-9+.-]{0,31}:\/\//iu.test(value)
    || /\b(?:https?|wss?|ftp|file|mailto|javascript|data|blob):/iu.test(value)
    || /\/\//u.test(value)
    || /(?:www\.)/iu.test(value)
    || /(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?/u.test(value)
    || /\[[0-9a-f:.]+\](?::\d{1,5})?/iu.test(value)
    || /localhost(?::\d{1,5})?/iu.test(value)
    || /[\p{L}\p{N}._%+-]{1,128}@(?:[\p{L}\p{N}-]{1,63}\.){1,8}[\p{L}]{2,63}/iu.test(value)
    || containsIdeographicBareDomain(value)) {
    return true;
  }
  DOMAIN_TOKEN_PATTERN.lastIndex = 0;
  let match;
  while ((match = DOMAIN_TOKEN_PATTERN.exec(value)) !== null) {
    const token = match[0];
    const explicitUrlIndex = token.search(/[/?#]/u);
    const hasExplicitPort = /:\d{1,5}(?:[/?#]|$)/u.test(token);
    if (explicitUrlIndex >= 0 || hasExplicitPort) return true;
    const authority = token.replace(/:\d{1,5}$/u, '');
    const tld = authority.slice(authority.lastIndexOf('.') + 1).toLowerCase();
    if (tld.startsWith('xn--') || !NON_NETWORK_TLDS.has(tld)) return true;
  }
  return false;
}

function containsIdeographicBareDomain(value) {
  IDEOGRAPHIC_DOMAIN_PATTERN.lastIndex = 0;
  let match;
  while ((match = IDEOGRAPHIC_DOMAIN_PATTERN.exec(value)) !== null) {
    const before = match.index > 0 ? value[match.index - 1] : '';
    const after = value[match.index + match[0].length] || '';
    if (before === '。' || after === '。') continue;
    const labels = match[0].split('。');
    const tld = labels[labels.length - 1];
    if (/[a-z0-9]/iu.test(match[0]) || COMMON_IDN_TLDS.has(tld)) return true;
  }
  return false;
}

function containsFullAccountNumber(value) {
  // 账号形态检测：去除装饰性分隔符（标点/空白/符号/不可见字符）后，
  // 数字组被不超过 2 个非数字字符衔接时视为同一串，连续 ≥15 位视为疑似完整账号。
  // 不再按全文数字总数判定——收益率/费率等金融文本数字密集但呈短串分布
  // （组间有真实文字），总数判定会误杀。
  const compact = value.replace(/[\p{P}\p{Z}\p{S}\p{M}\p{C}]/gu, '');
  let best = 0;
  let current = 0;
  let gap = 0;
  let inDigits = false;
  for (const character of compact) {
    if (/\p{Nd}/u.test(character)) {
      current += 1;
      if (current > best) best = current;
      gap = 0;
      inDigits = true;
    } else if (inDigits) {
      gap += 1;
      if (gap > 2) {
        current = 0;
        inDigits = false;
      }
    }
  }
  return best >= 15;
}

function containsInternalText(value) {
  if (INTERNAL_ID_PATTERN.test(value)) return true;
  const compact = value.replace(/[\p{P}\p{Z}\p{S}\p{M}\p{C}]/gu, '');
  return INTERNAL_TEXT_MARKERS.some(marker => compact.includes(marker));
}

function isBase64UrlCharacter(character) {
  return (character >= 'a' && character <= 'z')
    || (character >= '0' && character <= '9')
    || character === '_' || character === '-';
}

function base64UrlRunEnd(value, start, maximum) {
  let index = start;
  const limit = Math.min(value.length, start + maximum + 1);
  while (index < limit && isBase64UrlCharacter(value[index])) index += 1;
  return index;
}

function containsJwt(value) {
  const MAX_SEGMENT_CHARS = 2048;
  for (let start = 0; start < value.length; start += 1) {
    if (!isBase64UrlCharacter(value[start])) continue;
    const firstEnd = base64UrlRunEnd(value, start, MAX_SEGMENT_CHARS);
    const firstLength = firstEnd - start;
    if (firstLength < 8 || firstLength > MAX_SEGMENT_CHARS || value[firstEnd] !== '.') {
      start = firstEnd - 1;
      continue;
    }
    const secondStart = firstEnd + 1;
    const secondEnd = base64UrlRunEnd(value, secondStart, MAX_SEGMENT_CHARS);
    const secondLength = secondEnd - secondStart;
    if (secondLength < 8 || secondLength > MAX_SEGMENT_CHARS || value[secondEnd] !== '.') {
      start = firstEnd;
      continue;
    }
    const thirdStart = secondEnd + 1;
    const thirdEnd = base64UrlRunEnd(value, thirdStart, MAX_SEGMENT_CHARS);
    const thirdLength = thirdEnd - thirdStart;
    if (thirdLength >= 16 && thirdLength <= MAX_SEGMENT_CHARS) return true;
    start = secondEnd;
  }
  return false;
}

function containsOpaqueSecret(value) {
  const MIN_BASE64URL_TOKEN_CHARS = 42;
  const MIN_SK_SECRET_CHARS = 20;
  let base64UrlRun = 0;
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (isBase64UrlCharacter(character)) {
      base64UrlRun += 1;
      if (base64UrlRun >= MIN_BASE64URL_TOKEN_CHARS) return true;
    } else {
      base64UrlRun = 0;
    }
    if (value.startsWith('sk-', index)) {
      const secretEnd = base64UrlRunEnd(value, index + 3, 512);
      if (secretEnd - (index + 3) >= MIN_SK_SECRET_CHARS) return true;
    }
  }
  return containsJwt(value);
}

function preflightResponse(response) {
  // cards / plans 为可选扩展字段：历史轮次（4 键）与带卡片/方案的新轮次都必须能通过投影
  assertAllowedKeys(response, ['type', 'title', 'body', 'items', 'cards', 'plans'], ['type', 'title', 'body', 'items'], '公开响应');
  const type = requirePublicText(response.type, '响应类型');
  if (!ALLOWED_RESPONSE_TYPES.has(type)) throw invalidTurn('响应类型无效');
  requirePublicText(response.title, '响应标题');
  requirePublicText(response.body, '响应正文');
  if (!Array.isArray(response.items) || response.items.length > 20) {
    throw invalidTurn('响应项目无效');
  }
  response.items.forEach(item => {
    assertExactKeys(item, ['label', 'value'], '响应项目');
    requirePublicText(item.label, '响应项目标签');
    requirePublicText(item.value, '响应项目值');
  });
  if (response.cards !== undefined) {
    if (!Array.isArray(response.cards) || response.cards.length > 5) {
      throw invalidTurn('响应卡片无效');
    }
    response.cards.forEach(card => {
      // projectResponse 会剥掉 null 键（id/detail 缺省），repository 层会对已投影轮次二次校验；
      // 校验必须接受投影器的规范输出，否则无 id/detail 的卡片（支出归因、信用卡账单等）整轮被拒。
      assertAllowedKeys(card, ['id', 'title', 'subtitle', 'metas', 'detail'], ['title', 'metas'], '响应卡片');
      requirePublicText(card.title, '响应卡片标题');
      if (card.id != null) requirePublicText(card.id, '响应卡片标识');
      if (card.subtitle != null) requirePublicText(card.subtitle, '响应卡片副标题');
      if (card.detail != null) requirePublicText(card.detail, '响应卡片详情');
      if (!Array.isArray(card.metas) || card.metas.length > 4) {
        throw invalidTurn('响应卡片元信息无效');
      }
      card.metas.forEach(meta => {
        assertExactKeys(meta, ['label', 'value'], '响应卡片元信息');
        requirePublicText(meta.label, '响应卡片元信息标签');
        requirePublicText(meta.value, '响应卡片元信息值');
      });
    });
  }
  if (response.plans !== undefined) {
    if (!Array.isArray(response.plans) || response.plans.length > 3) {
      throw invalidTurn('响应方案无效');
    }
    response.plans.forEach(plan => {
      assertAllowedKeys(plan, ['id', 'title', 'summary', 'totalAmount', 'isPrimary', 'selectLabel', 'selectMessage', 'allocations'], ['title', 'isPrimary', 'allocations'], '响应方案');
      requirePublicText(plan.title, '响应方案标题');
      if (plan.id != null) requirePublicText(plan.id, '响应方案标识');
      if (plan.summary != null) requirePublicText(plan.summary, '响应方案摘要');
      if (plan.totalAmount != null) requirePublicText(plan.totalAmount, '响应方案金额');
      if (plan.selectLabel != null) requirePublicText(plan.selectLabel, '响应方案选择标签');
      if (plan.selectMessage != null) requirePublicText(plan.selectMessage, '响应方案选择消息');
      if (typeof plan.isPrimary !== 'boolean') throw invalidTurn('响应方案主次标识无效');
      if (!Array.isArray(plan.allocations) || plan.allocations.length > 5) {
        throw invalidTurn('响应方案配置无效');
      }
      plan.allocations.forEach(allocation => {
        assertAllowedKeys(allocation, ['productId', 'productName', 'productCategoryLabel', 'portfolioRoleLabel', 'riskLevel', 'amount', 'suitability', 'detail', 'productActionLabel'], ['productName'], '响应方案配置');
        requirePublicText(allocation.productName, '响应方案配置产品名');
        if (allocation.productId != null) requirePublicText(allocation.productId, '响应方案配置产品标识');
        if (allocation.productCategoryLabel != null) requirePublicText(allocation.productCategoryLabel, '响应方案配置类别');
        if (allocation.portfolioRoleLabel != null) requirePublicText(allocation.portfolioRoleLabel, '响应方案配置角色');
        if (allocation.riskLevel != null) requirePublicText(allocation.riskLevel, '响应方案配置风险');
        if (allocation.amount != null) requirePublicText(allocation.amount, '响应方案配置金额');
        if (allocation.suitability != null) requirePublicText(allocation.suitability, '响应方案配置适配');
        if (allocation.detail != null) requirePublicText(allocation.detail, '响应方案配置详情');
        if (allocation.productActionLabel != null) requirePublicText(allocation.productActionLabel, '响应方案配置操作标签');
      });
    });
  }
  return type;
}

function projectResponse(response) {
  const type = preflightResponse(response);
  return {
    type,
    title: redactSensitiveText(response.title),
    body: redactSensitiveText(response.body),
    items: response.items.map(item => {
      assertExactKeys(item, ['label', 'value'], '响应项目');
      return {
        label: redactSensitiveText(item.label),
        value: redactSensitiveText(item.value),
      };
    }),
    ...(response.cards !== undefined ? {
      cards: response.cards.map(card => ({
        ...(card.id == null ? {} : { id: redactSensitiveText(card.id) }),
        title: redactSensitiveText(card.title),
        ...(card.subtitle == null ? {} : { subtitle: redactSensitiveText(card.subtitle) }),
        metas: card.metas.map(meta => ({
          label: redactSensitiveText(meta.label),
          value: redactSensitiveText(meta.value),
        })),
        ...(card.detail == null ? {} : { detail: redactSensitiveText(card.detail) }),
      })),
    } : {}),
    ...(response.plans !== undefined ? {
      plans: response.plans.map(plan => ({
        ...(plan.id == null ? {} : { id: redactSensitiveText(plan.id) }),
        title: redactSensitiveText(plan.title),
        ...(plan.summary == null ? {} : { summary: redactSensitiveText(plan.summary) }),
        ...(plan.totalAmount == null ? {} : { totalAmount: redactSensitiveText(plan.totalAmount) }),
        isPrimary: plan.isPrimary,
        ...(plan.selectLabel == null ? {} : { selectLabel: redactSensitiveText(plan.selectLabel) }),
        ...(plan.selectMessage == null ? {} : { selectMessage: redactSensitiveText(plan.selectMessage) }),
        allocations: plan.allocations.map(allocation => ({
          ...(allocation.productId == null ? {} : { productId: redactSensitiveText(allocation.productId) }),
          productName: redactSensitiveText(allocation.productName),
          ...(allocation.productCategoryLabel == null ? {} : { productCategoryLabel: redactSensitiveText(allocation.productCategoryLabel) }),
          ...(allocation.portfolioRoleLabel == null ? {} : { portfolioRoleLabel: redactSensitiveText(allocation.portfolioRoleLabel) }),
          ...(allocation.riskLevel == null ? {} : { riskLevel: redactSensitiveText(allocation.riskLevel) }),
          ...(allocation.amount == null ? {} : { amount: redactSensitiveText(allocation.amount) }),
          ...(allocation.suitability == null ? {} : { suitability: redactSensitiveText(allocation.suitability) }),
          ...(allocation.detail == null ? {} : { detail: redactSensitiveText(allocation.detail) }),
          ...(allocation.productActionLabel == null ? {} : { productActionLabel: redactSensitiveText(allocation.productActionLabel) }),
        })),
      })),
    } : {}),
  };
}

function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

function projectSessionTurn(input) {
  assertAllowedKeys(input, ['role', 'text', 'response'], ['role', 'text'], 'Session 轮次');
  if (!ALLOWED_ROLES.has(input.role)) throw invalidTurn('Session 轮次角色无效');
  requirePublicText(input.text, 'Session 轮次文本');
  if (input.response !== null && input.response !== undefined && input.role !== 'assistant') {
    throw invalidTurn('用户轮次不能包含助手响应');
  }
  if (input.response != null) preflightResponse(input.response);
  const response = input.response == null ? null : projectResponse(input.response);
  return deepFreeze({
    role: input.role,
    text: redactSensitiveText(input.text),
    response,
  });
}

module.exports = {
  isSafeAgentSessionText,
  projectSessionTurn,
  redactSensitiveText,
};
