# Finance Guardrail 上游静态安全审计

审计日期：2026-09-02  
审计范围：固定上游 commit 的源码、依赖锁、启动脚本、HTTP 接口、模型/embedding 调用、持久化、日志与测试。  
执行状态：尚未安装依赖、尚未启动上游代码。

## 结论

门禁结果：**conditional pass for isolated validation（有条件允许隔离验证）**。

该项目具备多层文本安全检测原型价值，但不能原样接入 AI 原生手机银行。只有满足以下条件才能进入动态验证：无真实客户数据、无宿主机端口映射、无宿主网络、关闭 Phoenix、使用临时 SQLite、限制 CPU/内存、只连接 AI Harbor 受保护模型网关、第三方进程不持有真实模型密钥。

许可证缺失、匿名管理接口、明文敏感日志和 fail-open 检测策略均是正式采用前的阻断项。

## 来源与完整性

| 项目 | 证据 |
| --- | --- |
| 仓库 | `https://gitcode.com/gcw_yE7jf8aN/finance-guardrail.git` |
| 固定 commit | `cba20eb4151df267dc91cde2ba20a49f6ff9db99` |
| 提交时间 | `2026-08-10T17:06:53+08:00` |
| Git 文件数 | 108 |
| 工作树 | detached HEAD，审计时无本地改动 |
| 项目许可证 | 根目录未发现 LICENSE/COPYING；仅内置 spaCy 模型目录有各自许可证，不能覆盖整个项目 |

许可证判断：许可证未明确前，可继续内部隔离技术验证，但不得据此批准产品内保留、修改、再分发或对外发布。

## 技术栈与复现性

- Python `>=3.10,<3.13`，计划使用 Python 3.11。
- 上游依赖入口为 `pyproject.toml`，锁文件为 `uv.lock`。
- 锁文件来源统计：176 个 PyPI registry 包，1 个当前项目 virtual source；未发现 Git、路径或任意 URL 依赖源。
- 关键依赖包括 FastAPI、Uvicorn、LangChain、LangGraph、Guardrails AI、Presidio、spaCy、SQLAlchemy、aiosqlite、OpenInference instrumentation。
- 上游声明的测试入口为 `uv run pytest test/ -v`；安装采用 `uv sync`。隔离验证固定为 `uv sync --frozen`，禁止运行时改锁或临时安装未审计包。
- `start.sh` 使用 `nohup` 后台启动并写日志，`start.bat` 直接启动；二者都不用于验证。

## 运行结构

核心是 LangGraph 八节点链，而不是 openJiuwen：

1. 白名单检查
2. 输入 PII 检查
3. Guardrails/正则检查
4. embedding 内容过滤
5. LLM 安全裁决
6. 业务问答模型调用
7. 输出内容过滤
8. 输出 PII 检查

项目把“护栏判断”和“生成最终回答”放在同一个 Agent 内。平台后续若采用，应提取框架无关的输入/输出检测契约，而非把该 LangGraph 对话 Agent 整体嵌入用户智能体。

## 模型、Embedding 与网络目标

| 调用 | 配置 | 审计结果 |
| --- | --- | --- |
| Chat/LLM 防御与回答 | `OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_API_KEY` | OpenAI-compatible；同一模型同时承担安全裁决和回答 |
| Embedding | `EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`，复用 `OPENAI_API_KEY` | 启动即尝试为默认锚点向量化，完整验证必须有 embedding 端点 |
| Phoenix | `PHOENIX_ENDPOINT` | 默认关闭；开启后会向 `/v1/traces` 发送 OTLP，验证中必须保持关闭 |

`.env.example` 默认指向 SiliconFlow。隔离适配不得使用该默认目标，也不得把 DeepSeek/Qwen 上游密钥交给第三方进程。Chat 可复用现有 AI Harbor 受保护内部网关；embedding 需要新增同等保护、固定 `text-embedding-v4` 的内部代理后才能完成全链验证。

## 高风险发现

### 严重：HTTP 管理面无鉴权

`/chat`、流式聊天、会话历史、仪表盘统计/日志、规则、白名单、PII、提示词防御、内容分类与锚点管理均未使用认证或授权依赖。若暴露端口，匿名调用者可以读取日志、修改安全策略或触发模型消耗。

处置：动态验证不映射宿主机端口。正式接入时不复用这套管理 API，只通过 AI Harbor 自有鉴权边界暴露最小检测契约。

### 严重：请求与响应明文持久化

`RequestLog` 保存 source IP、User-Agent、完整用户输入和完整响应；普通日志也记录截断后的输入与响应。聊天/PII API 还可能直接返回原始文本。银行场景中会造成客户信息、卡号和对话内容扩散。

处置：仅使用合成数据和临时数据库；正式实现必须默认脱敏、字段级允许列表、短保留期、访问审计，并禁止记录原文。

### 高：LLM 安全裁决 fail-open

只有模型返回可解析且精确的 `status=reject` 才阻断。模型异常、输出无法解析或无法识别时默认放行；提示词自身还采用“存疑即过/无罪推定”。检测器不可用与检测通过没有可靠区分。

处置：平台契约必须返回 `allow/block/review/error` 四态和检测器健康度；高风险场景错误应 fail-closed 或转人工，不能转换为 allow。

### 高：Embedding 内容分类 fail-open

锚点未加载、embedding 请求失败、文本短于阈值、英文比例较高或节点异常时均可能直接放行。默认内容过滤 input/output 均启用并为 block 模式，但底层错误不会稳定阻断。

处置：验证报告必须单独统计“检测不可用”；多语言、短文本和 embedding 故障必须纳入用例。

### 高：规则与业务例外硬编码

安全提示词和默认正则包含大量银行产品、人物、收益率、账户风控和业务问法的具体例外。这种规则可能对演示集有效，但会造成跨业务泛化差、策略冲突和维护不可追溯。

处置：只把它视为候选策略数据集。后续把风险本体、政策版本、规则来源、适用范围、例外、严重度和处置从代码/系统提示词中拆出，通过可版本化策略注册表管理。

### 中：缺少输入与配置边界

`ChatRequest.message`、`conversation_id` 以及多类规则/锚点/提示词写入模型未设置明确长度上限；`validate_message` 仅判断非空。可能造成模型费用、内存、日志与正则计算放大。

处置：隔离容器限制资源；正式适配在 AI Harbor 边界限制请求体、字段、文本长度、批量数、超时和并发。

### 中：CORS 与错误信息

服务允许凭据，允许请求头 `*`，但没有认证/CSRF 保护；流式错误会把截断的异常字符串发送到客户端。

处置：验证不提供浏览器入口；正式接入使用平台 CORS/CSRF 策略与统一错误码，禁止透传内部异常。

## 动态执行与供应链检查

- 未发现 `eval`、`exec`、`os.system` 或业务代码中的任意 shell 命令执行路径。
- 未发现 pickle/yaml 不安全反序列化入口。
- 运行时远程调用集中在 OpenAI-compatible Chat、Embedding，以及可选 Phoenix OTLP。
- 上游 README 在 Guardrails AI 安装失败时建议手工下载 wheel；验证中禁止该旁路，只接受锁文件的冻结安装。
- 启动会创建/更新 SQLite 数据库、规则、默认内容分类、锚点和 LangGraph checkpointer；全部数据必须留在临时容器文件系统。

## 测试质量审计

- `test_pii.py` 包含实际断言，可用于基础 PII 检测验证。
- `test_guardrails.py` 主要通过打印符号展示结果，没有用断言锁定安全结论，不能作为回归门禁。
- `testcase.py` 混合 unittest、pytest、FastAPI TestClient 与 Mock；部分测试在模块导入或 fixture 初始化时创建 Agent，可能依赖外部模型与数据库生命周期。
- README 引用了当前仓库中不存在的 `test_checkpointer.py`、`test_agent_checkpointer.py`、`test_checkpoint_cleanup.py`，文档与固定 commit 不一致。

动态阶段必须原样记录收集错误、失败、跳过和外部依赖，不修改失败断言来制造通过结果。

## 隔离运行许可条件

- Python 3.11 + `uv sync --frozen`。
- 不映射宿主机端口；不使用 `network_mode: host`。
- 只挂载 guardrail-lab 自身，不挂载 AI Harbor 数据库、源码工作区或客户文件。
- 限制 CPU、内存、进程数，并设置只读根文件系统与临时可写目录（如上游依赖允许）。
- `PHOENIX_ENABLED=False`。
- 数据库和 checkpoint 使用容器临时路径，测试后可直接销毁。
- 模型访问只通过服务令牌保护的 AI Harbor 内部网关；不注入真实模型密钥。
- 仅使用 `cases/` 中的合成测试数据。

满足以上条件后，允许进入镜像构建、原生测试和最小样例验证；这不等于批准生产接入。
