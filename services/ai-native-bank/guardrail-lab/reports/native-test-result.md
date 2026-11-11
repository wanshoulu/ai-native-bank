# Finance Guardrail 原生测试结果

执行日期：2026-09-02  
镜像：`ai-native-bank-guardrail-lab:test`  
Python：3.11.15  
安装：`uv sync --frozen`（170 个锁定包）  
网络：none  
模型密钥：未注入

## 上游主测试入口

命令：

```text
uv run --frozen pytest test/ -v
```

结果：`10 passed, 2 skipped, 0 failed`，耗时 10.23 秒。

跳过项：

- `test/test_database.py::test_database`
- `test/test_guardrails.py::test_security_check`

两项都是 `async def`，但未加 `@pytest.mark.asyncio`；上游固定的 pytest strict 模式将其作为未支持协程跳过。因此主入口“退出码 0”不能解释为数据库和 Agent 护栏都已通过。

## 隔离环境校正记录

首次运行结果为 `9 passed, 1 failed, 2 skipped`。失败同时显示 Presidio 尝试从 GitHub 下载 spaCy 模型。

根因证据：镜像中 `/app/data/zh_core_web_sm` 和 `/app/data/en_core_web_sm` 存在，但初版 Compose 把 `/app/data` 整体覆盖成 tmpfs，遮蔽了上游捆绑模型。仅将临时数据库迁到 `/app/runtime-data` 后，原失败用例 `test_multiple_pii_detection` 单独复跑通过，随后完整主入口达到上述 10/2/0。未修改上游源码或断言。

## 被跳过异步项的补充执行

命令：

```text
uv run --frozen pytest --asyncio-mode=auto test/test_database.py test/test_guardrails.py -vv
```

结果：`0 passed, 2 failed`。

1. 数据库测试失败：测试向 `RequestLogCRUD.create_log()` 传递已经不存在的 `block_reason` 参数，说明测试与当前 CRUD 契约不一致。
2. 护栏测试失败：创建 `LangChainAgent` 时因未配置 OpenAI-compatible 凭据而失败。该结果符合无密钥隔离阶段预期，但也说明测试本身没有 Mock 模型边界。

## README 指定安全规则测试

命令：

```text
uv run --frozen pytest test/testcase.py::TestSecurityRules -q
```

结果：`0 passed, 5 failed`。五项都在 `setUp()` 创建 `LangChainAgent` 时因缺少模型凭据失败，尚未执行到规则断言。

另一个文档一致性问题：`testcase.py` 文件名不符合默认 `test_*.py` 收集规则，所以 `pytest test/` 不会收集其中的 unittest/pytest 用例。

## 当前判断

- PII 正则/本地 Presidio 基础路径可以在无网络、无密钥容器中运行。
- 原生测试框架存在漏收集、错误跳过、过期参数和未隔离外部模型依赖，不能作为生产质量门禁。
- Chat 与完整 Agent 链必须通过 AI Harbor 受保护网关补充验证。
- 启动阶段默认启用 embedding 内容过滤，完整服务验证还需要受保护的 embedding 代理。
