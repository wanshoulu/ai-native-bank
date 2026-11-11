# Finance Guardrail 隔离可行性实验室

本目录保存第三方 `finance-guardrail` 的隔离副本，以及 AI 原生手机银行专用的加固适配层。上游代码不是生产组件；正式主链路只通过内部 HTTP 调用本目录的输入 Rail 适配端点，不直接导入或改写上游实现。

## 固定来源

- 上游仓库：`https://gitcode.com/gcw_yE7jf8aN/finance-guardrail.git`
- 固定 commit：`cba20eb4151df267dc91cde2ba20a49f6ff9db99`
- 上游提交时间：`2026-08-10T17:06:53+08:00`
- 本地下载日期：`2026-09-02`
- 本地位置：`upstream/`
- 状态：detached HEAD，原始工作树保持干净

## 隔离边界

1. 禁止直接改写 `upstream/`。平台适配、样例、运行配置和报告分别放在本目录的 `adapters/`、`cases/` 和 `reports/`。
2. 禁止使用真实客户数据、真实卡号、身份证号、手机号、对话记录或业务 Trace；所有验证数据必须为显式合成数据。
3. 禁止把 DeepSeek、Qwen 或其他上游模型密钥注入第三方进程。第三方进程只能持有 AI Harbor 的短期内部服务令牌，并通过受保护的 OpenAI-compatible 网关访问固定模型。
4. 禁止直接运行上游 `start.sh`/`start.bat`；验证必须以前台、无公网端口、资源受限、数据卷隔离的容器运行。
5. `PHOENIX_ENABLED=False`，除 AI Harbor 受保护模型端点外不得配置额外遥测或网络目标。
6. 上游根目录未发现项目级许可证。许可证澄清前，只允许内部技术审计和隔离验证，不得复制到正式产品、分发或对外发布。
7. 当前阶段只验证文本问答输入/输出护栏；“智能体行动”护栏与业务智能体接入不在本阶段范围内。

## 当前门禁结论

静态审计结论为：**不允许直接集成上游，只允许通过隔离加固适配层调用**。主要阻断项包括未鉴权的管理接口、明文请求/响应日志、LLM 与 embedding 检测异常时默认放行、银行业务规则硬编码、缺少输入长度限制，以及项目许可证不明确。详见 `reports/upstream-audit.md`。

本地 `adapters/` 已增加隔离增强层，用于验证明显缺陷的可修复性：

- 四态内部安全结果：`allow`、`review`、`block`、`unavailable`；
- 必需检测器和回答模型异常时安全终止，不再静默放行；
- 白名单不允许绕过输入/输出安全链；
- 提示词防御、内容过滤和高敏 PII 不能被运行时配置关闭；
- PII 排除日期、地点、普通姓名、URL 等宽泛误杀类型；
- 用户响应和日志不暴露内部规则、异常详情或完整高敏字段。

该增强层以独立 Rail 服务接入 AI 原生手机银行 Agent Runtime：Rail 与 User DeepAgent 并行运行，Rail 只做安全判定、不生成业务回答；`review` 时由同一 User DeepAgent 在受限续跑中生成澄清问题；`allow` 后才允许提交意图并创建 A2A 委派。业务智能体行动护栏仍不在本期范围内。

## 构建、验证与停止

```powershell
docker compose -f services/ai-native-bank/guardrail-lab/docker-compose.guardrail-lab.yml build guardrail-runtime
docker compose -f services/ai-native-bank/guardrail-lab/docker-compose.guardrail-lab.yml run --rm guardrail-native-tests `
  uv run --frozen pytest /opt/guardrail-tests -q
docker compose -f services/ai-native-bank/guardrail-lab/docker-compose.guardrail-lab.yml up -d guardrail-runtime
docker exec ai-native-bank-guardrail-lab-guardrail-runtime-1 `
  uv run --frozen python /opt/guardrail-adapters/verify_workflow_closure.py `
  --mode all --base-url http://127.0.0.1:8000
docker compose -f services/ai-native-bank/guardrail-lab/docker-compose.guardrail-lab.yml down
```

Compose 不发布护栏端口；模型和 Embedding 只通过 AI Harbor 内部网络访问。停止命令只作用于 `ai-native-bank-guardrail-lab` Compose 项目。

## 原生复现入口

上游声明的唯一锁定安装路径为：

```text
uv sync --frozen
uv run --frozen pytest test/ -v
```

实际命令只能从本目录提供的隔离 Compose 执行。完成容器基线前，不在宿主机安装依赖或启动服务。
