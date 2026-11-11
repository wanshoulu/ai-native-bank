# AI 原生手机银行（ai-native-bank）

Agent 驱动的手机银行原型：用户一句话 → 意图识别 → 任务编排 → 业务执行 → 下一步行动推荐。从 AI Harbor 分割出的独立项目，一个仓库即可运行。

## 这是什么

面向手机银行交互范式验证的原型系统（非真实银行系统，全部业务数据为 mock）：

- **对话入口**：用户用自然语言说事（"给妈妈转 500"），不用翻菜单
- **意图图引擎**：意图识别走规则化的意图图（v4 图 + 语义索引 + 别名包），不依赖训练模型也能跑通主链路
- **Agent 会话**：多轮对话、可恢复工作流、确认卡（转账这类敏感动作强制用户确认）
- **业务执行**：转账 / 理财 / 信用卡 / 贷款 / 生活缴费 / 现金流 / 待办履约等 10 个业务域的 mock 服务
- **下一步行动（Next Action）**：基于账户状态与行为的主动推荐
- **Python agent-runtime（可选）**：深度多轮意图与业务编排（fastapi + openjiuwen deepagent），不启动则对话降级为图引擎
- **Guardrail Lab（可选）**：第三方 finance-guardrail 的隔离验证环境（离线容器）

## 名词表

| 名词 | 含义 |
|---|---|
| 意图图（intent graph） | 意图的结构化描述（节点=意图，含 slot schema、追问话术、技能绑定），v1→v4 版本见 `data/ai-native-bank/intent-graph/` |
| Agent Session | 一次对话会话（30 分钟空闲过期），绑定客户身份与焦点 |
| Agent Run | 一次用户消息触发的智能处理（意图识别 → 编排 → 执行 → 回复） |
| Next Action | 主动推荐（还款提醒、理财建议等），来自信号引擎 + 排序引擎 |
| agent-runtime | Python 侧运行时（deepagent 业务编排），经 `AI_BANK_AGENT_RUNTIME_URL` 被主服务调用 |
| guardrail | 输入护栏（违禁词、注入检测），词表在 `data/ai-native-bank/guardrail/lexicon/` |
| capability | 智能体能力注册（哪些意图可以由哪个技能执行） |

## 2 行跑起来

```bash
npm install          # 4 个依赖：sql.js, xlsx, ajv, ajv-formats
npm start            # http://127.0.0.1:3000 （前端原型页）
```

启动后浏览器打开 http://127.0.0.1:3000/ 即是手机银行原型界面（mock 客户 cust_demo_001）。

要完整对话智能（Python 运行时）：

```bash
cd services/ai-native-bank/agent-runtime
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 18120
# 另一个终端：
AI_BANK_AGENT_RUNTIME_URL=http://127.0.0.1:18120 npm start
```

Docker 方式（runtime 容器化，镜像内自带意图图数据）：

```bash
docker build -t ai-native-bank-agent-runtime -f services/ai-native-bank/agent-runtime/Dockerfile .
```

## 架构

```
浏览器（tools/ai-native-bank.html，纯前端，10 个模块）
   │  /api/ai-bank/v1/*
   ▼
Node 主服务（server.js → routes/ai-native-bank-api.js）
   ├─ services/ai-native-bank/*（55+ 模块：业务域服务/会话/推荐/护栏/网关）
   ├─ data/ai-native-bank/（意图图、mock 核心库 SQLite、护栏词表、技能注册）
   ├─ agent-runtime（Python，可选）：深度意图 + 业务编排
   │     └─ 回调 /api/ai-bank/v1/internal/*（模型网关/能力调用，内部服务令牌鉴权）
   └─ config/api-keys.js（云 LLM 路由注册表，密钥全部走环境变量）
```

## 测试

```bash
npm test             # Node 侧：意图多轮/歧义 2 套
cd services/ai-native-bank/agent-runtime && python -m pytest tests/   # Python 侧
```

## 目录

```
server.js                     独立服务入口（静态白名单 + /api/ai-bank/v1）
routes/ai-native-bank-api.js  API 编排（4600 行：会话/意图/执行/推荐/内部网关）
routes/intent-evaluator-*.js  意图评测路由（多轮/歧义/图统计）
services/ai-native-bank/      业务服务层（56 文件）
services/.../agent-runtime/   Python 运行时（fastapi，281 文件）
services/.../guardrail-lab/   护栏隔离实验室（81 文件，独立 docker compose）
tools/                        前端原型（单页 + 10 个 JS 模块）
data/ai-native-bank/          意图图（v1/v2.1 当前版 + schema）/ mock 核心 / 词表 / 技能
```

## 安全说明

- 密钥一律环境变量（`.env.example` 模板，`.env` 不入库）
- 静态服务白名单：只有 `tools/` 可经 HTTP 读取，data/（含用户 mock 数据）与源码不可读
- 内部端点（`/internal/*`）需要内部服务令牌（`AI_BANK_AGENT_RUNTIME_TOKEN`）
- 全部客户数据为合成 mock（测试号段手机号），无真实个人信息

## License

MIT（agent-runtime 依赖 openjiuwen；guardrail-lab 上游为 Apache-2.0 隔离副本，见其 README）
