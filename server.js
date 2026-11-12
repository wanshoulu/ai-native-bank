'use strict';
/**
 * ai-native-bank — AI 原生手机银行（独立版）
 *
 * 从 AI Harbor 分割出的独立服务：Agent 驱动的手机银行原型
 * （意图识别 → 任务编排 → 业务执行 → 下一步行动推荐）。
 *
 * 启动：
 *   npm install          # 0 个 npm 依赖（纯 Node 内置模块）
 *   npm start            # http://127.0.0.1:3012
 *   PORT=8080 npm start
 *
 * 可选 Python 运行时（多轮深意图 / agent-runtime）：
 *   cd services/ai-native-bank/agent-runtime
 *   pip install -r requirements.txt
 *   python -m uvicorn app.main:app --port 8000
 *   # 主服务通过 AI_BANK_AGENT_RUNTIME_URL 环境变量发现它
 *
 * 环境变量（均可选，见 .env.example）：
 *   HOST / PORT         服务监听
 *   QWEN_KEY 等         云 LLM 密钥（无则降级 mock 引擎）
 *   AI_BANK_AGENT_RUNTIME_URL  Python agent-runtime 地址
 */

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const PORT = Number(process.env.PORT || 3012);
const HOST = process.env.HOST || '127.0.0.1';

const { handleAiNativeBankRequest } = require('./routes/ai-native-bank-api');

// ── 极简静态文件服务（tools/ 白名单）──
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml',
  '.json': 'application/json; charset=utf-8', '.txt': 'text/plain; charset=utf-8',
};
function serveStatic(req, res) {
  const u = new URL(req.url, 'http://localhost');
  let p = path.normalize(u.pathname).replace(/^([/\\])+/, '');
  if (p === '' || p === '.') p = 'tools/ai-native-bank.html';   // 根路径直达银行原型
  // 静态文件只允许 tools/ 内（白名单）：
  // .env（密钥）、data/（含 mock-core.db 用户数据）、源码与测试一律不可经 HTTP 读取。
  const rel = path.relative(path.join(__dirname, 'tools'), path.join(__dirname, p));
  if (p !== 'tools/ai-native-bank.html' && (rel.startsWith('..') || path.isAbsolute(rel))) {
    res.writeHead(403); return res.end('forbidden');
  }
  const file = path.join(__dirname, p);
  fs.readFile(file, function (err, buf) {
    if (err) { res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }); return res.end('404: ' + p); }
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream',
      'Cache-Control': 'no-store',
    });
    res.end(buf);
  });
}

const server = http.createServer(function (req, res) {
  const u = new URL(req.url, 'http://localhost');
  const pathname = u.pathname;
  try {
    if (pathname === '/favicon.ico') { res.writeHead(204); return res.end(); }
    if (pathname === '/healthz' || pathname === '/api/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ ok: true, service: 'ai-native-bank', time: Date.now() }));
    }
    // 根路径 302 到工具页（页面脚本用相对路径引用，必须以 /tools/ 为基准访问）
    if (pathname === '/') {
      res.writeHead(302, { Location: '/tools/ai-native-bank.html' });
      return res.end();
    }
    // 银行 API（同步返回 false 表示未匹配，继续静态）
    if (handleAiNativeBankRequest(req, res)) return;
    return serveStatic(req, res);
  } catch (e) {
    if (!res.headersSent) {
      res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
    }
  }
});

server.listen(PORT, HOST, function () {
  console.log('[ai-native-bank] http://' + HOST + ':' + PORT + '/  （Ctrl+C 退出）');
});
