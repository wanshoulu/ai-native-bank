import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from openjiuwen.core.single_agent import AgentCard, create_agent_session
from openjiuwen.harness.rails import SkillUseRail

from app.deepagent_factory import ModelStreamRetryRail

from app.config import RuntimeNotReadyError, RuntimeSettings
from app.business_loop import TodoSnapshotItem
from app.deepagent_factory import (
    BUSINESS_PROMPT,
    DeepAgentFactory,
    OpenJiuwenRunner,
    SessionRevisionConflict,
    USER_PROMPT,
    _normalize_user_intent_submission,
    _user_intent_submission_schema,
)
from app.node_gateway import AgentCapabilityInvoker
from app.openjiuwen_checkpointer import OpenJiuwenCheckpointerManager
from app.submission_tools import SubmissionCollector, create_submission_tool
from app.todo_ledger import TodoLedger


class SettingsTests(unittest.TestCase):
    def test_default_workspace_stays_inside_openjiuwen_work_directory(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = RuntimeSettings.from_env()

        self.assertEqual(settings.workspace_root, "/app/agent-workspaces")

    def test_settings_report_readiness_without_exposing_secrets(self):
        environment = {
            "AI_BANK_AGENT_RUNTIME_TOKEN": "runtime-top-secret",
            "AI_BANK_MODEL_PROVIDER": "openai",
            "AI_BANK_MODEL_NAME": "mock-model",
            "AI_BANK_MODEL_BASE_URL": "https://private-model.example/v1",
            "AI_BANK_MODEL_API_KEY": "model-top-secret",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = RuntimeSettings.from_env()

        self.assertTrue(settings.model_configured)
        status = settings.public_status(framework_available=True)
        serialized = str(status) + repr(settings)
        self.assertNotIn("runtime-top-secret", serialized)
        self.assertNotIn("model-top-secret", serialized)
        self.assertNotIn("private-model", serialized)
        self.assertEqual(status["provider"], "openjiuwen")
        self.assertTrue(status["ready"])

    def test_factory_fails_closed_without_model_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = RuntimeSettings.from_env()
        with self.assertRaisesRegex(RuntimeNotReadyError, "model_not_configured"):
            DeepAgentFactory(
                settings,
                session_lifecycle=OpenJiuwenCheckpointerManager(
                    "/tmp/factory-not-ready.db"
                ),
            ).create()

    def test_settings_load_private_token_files_with_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = os.path.join(directory, "runtime-token")
            with open(token_path, "w", encoding="utf-8") as token_file:
                token_file.write("file-runtime-secret")
            os.chmod(token_path, 0o400)
            environment = {
                "AI_BANK_AGENT_RUNTIME_TOKEN_FILE": token_path,
                "AI_BANK_MODEL_API_KEY_FILE": token_path,
                "AI_BANK_MODEL_PROVIDER": "openai",
                "AI_BANK_MODEL_NAME": "deepseek-chat",
                "AI_BANK_MODEL_BASE_URL": "http://ai-harbor:3000/api/ai-bank/v1/internal/model/v1",
            }
            with patch.dict(os.environ, environment, clear=True):
                file_settings = RuntimeSettings.from_env()
            with patch.dict(os.environ, {
                **environment,
                "AI_BANK_AGENT_RUNTIME_TOKEN": "environment-runtime-secret",
                "AI_BANK_MODEL_API_KEY": "environment-model-secret",
            }, clear=True):
                environment_settings = RuntimeSettings.from_env()

        self.assertEqual(file_settings.runtime_token, "file-runtime-secret")
        self.assertEqual(file_settings.model_api_key, "file-runtime-secret")
        self.assertEqual(environment_settings.runtime_token, "environment-runtime-secret")
        self.assertEqual(environment_settings.model_api_key, "environment-model-secret")
        serialized = repr(file_settings) + str(file_settings.public_status(framework_available=True))
        self.assertNotIn("file-runtime-secret", serialized)
        self.assertNotIn(token_path, serialized)


class SubmissionToolTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _semantic_submission():
        return {
            "schema_version": "1.0",
            "intent_shape": "non_task",
            "tasks": [],
            "ambiguities": [],
            "shared_slots": {},
            "predicted_next_intents": [],
        }

    @staticmethod
    def _intent_tool(collector):
        return create_submission_tool(
            name="submit_intent_task",
            description="submit",
            schema=_user_intent_submission_schema(),
            collector=collector,
            normalizer=_normalize_user_intent_submission,
        )

    async def test_user_submission_schema_excludes_server_owned_identity(self):
        schema = _user_intent_submission_schema()
        # model_json_schema() 以 camelCase alias 导出属性名，清理必须覆盖
        # 两种拼写，否则平台注入字段会带着 min_length 必填约束暴露给模型。
        server_owned = {
            "delegation_id", "trace_id", "conversation_id", "source_agent_run_id",
            "delegationId", "traceId", "conversationId", "sourceAgentRunId",
            "revision",
        }

        self.assertTrue(server_owned.isdisjoint(schema["properties"]))
        self.assertTrue(server_owned.isdisjoint(schema["required"]))
        # 传输层容忍未知/回显字段（与 LLMTolerantModel extra=ignore 同策略），
        # 剥离与校验由 normalizer 和领域模型负责。
        self.assertTrue(schema.get("additionalProperties"))
        for branch in schema.get("oneOf", []):
            branch_required = branch.get("required", [])
            self.assertTrue(server_owned.isdisjoint(branch_required))
            self.assertNotIn("intentShape", branch_required)

    async def test_trusted_tool_records_exactly_one_structured_submission(self):
        collector = SubmissionCollector("intent submission")
        tool = create_submission_tool(
            name="submit_intent_task",
            description="submit",
            schema={"type": "object", "additionalProperties": False, "properties": {}},
            collector=collector,
        )

        accepted = await tool.invoke({})
        self.assertTrue(accepted["accepted"])
        self.assertEqual(collector.take(), {})
        duplicate = await tool.invoke({})
        self.assertTrue(duplicate["accepted"])
        self.assertTrue(duplicate["duplicate"])
        # 重复提交不覆盖首个结果
        self.assertEqual(collector.take(), {})

        collector.reset()
        collector.submit({"required": "value", "optional": None})
        self.assertEqual(collector.take(), {"required": "value"})

    async def test_intent_tool_collects_direct_semantic_envelope(self):
        collector = SubmissionCollector("intent submission")
        tool = self._intent_tool(collector)
        semantic = self._semantic_submission()

        accepted = await tool.invoke(semantic)

        self.assertTrue(accepted["accepted"])
        self.assertEqual(collector.take(), semantic)

    async def test_intent_tool_normalizes_wrapped_semantic_envelope(self):
        collector = SubmissionCollector("intent submission")
        tool = self._intent_tool(collector)
        semantic = self._semantic_submission()

        accepted = await tool.invoke({"envelope": semantic})

        self.assertTrue(accepted["accepted"])
        self.assertEqual(collector.take(), semantic)

    async def test_intent_tool_accepts_only_bounded_explicit_memory_hints(self):
        collector = SubmissionCollector("intent submission")
        tool = self._intent_tool(collector)
        semantic = self._semantic_submission()
        hint = {
            "predicate": "response_style", "value": "concise",
            "evidence_kind": "user_explicit", "consent_granted": True,
        }
        accepted = await tool.invoke({**semantic, "memory_candidate_hints": [hint]})
        self.assertTrue(accepted["accepted"])
        self.assertEqual(collector.take()["memory_candidate_hints"], [hint])

        wrapped_collector = SubmissionCollector("intent submission")
        wrapped_tool = self._intent_tool(wrapped_collector)
        accepted = await wrapped_tool.invoke({
            "envelope": {**semantic, "memory_candidate_hints": []}
        })
        self.assertTrue(accepted["accepted"])
        self.assertEqual(wrapped_collector.take(), semantic)

        # 谓词已语义化（snake_case，不限预定义枚举）：payment_account
        # 这类语义谓词应被接受；仍拒绝违反已注册谓词值域的提示。
        semantic_collector = SubmissionCollector("intent submission")
        accepted = await self._intent_tool(semantic_collector).invoke({
            **semantic,
            "memory_candidate_hints": [{**hint, "predicate": "payment_account"}],
        })
        self.assertTrue(accepted["accepted"])
        self.assertEqual(
            semantic_collector.take()["memory_candidate_hints"],
            [{**hint, "predicate": "payment_account"}],
        )

        with self.assertRaises(Exception):
            await self._intent_tool(SubmissionCollector("intent submission")).invoke({
                **semantic,
                "memory_candidate_hints": [{**hint, "value": "not-in-domain"}],
            })

    async def test_intent_tool_mixed_transport_resolves_envelope_priority(self):
        # envelope 是显式包装器；模型偶发同时提交两种传输形态（重复投影）。
        # 传输层不再因混合形态整轮报错，normalizer 确定性采用 envelope。
        collector = SubmissionCollector("intent submission")
        tool = self._intent_tool(collector)
        semantic = self._semantic_submission()

        accepted = await tool.invoke({**semantic, "envelope": semantic})

        self.assertTrue(accepted["accepted"])
        self.assertEqual(collector.take(), semantic)

    async def test_intent_tool_drops_unknown_and_strips_server_owned_fields(self):
        # 未知字段静默丢弃（与领域模型 extra=ignore 同策略），不再触发
        # 工具报错 + 整轮修复。
        collector = SubmissionCollector("intent submission")
        accepted = await self._intent_tool(collector).invoke({
            **self._semantic_submission(), "unexpected": "not-allowed",
        })
        self.assertTrue(accepted["accepted"])
        self.assertEqual(collector.take(), self._semantic_submission())

        # 平台注入 id 与 revision 的回显（含 camelCase 拼写与空串）不携带
        # 信息，机械剥离；platform 侧解析后整体覆盖。
        stripped = SubmissionCollector("intent submission")
        accepted = await self._intent_tool(stripped).invoke({
            **self._semantic_submission(),
            "conversation_id": "conv_echo", "delegationId": "",
            "traceId": None, "source_agent_run_id": "run_echo", "revision": 7,
        })
        self.assertTrue(accepted["accepted"])
        taken = stripped.take()
        for field in (
            "conversation_id", "delegation_id", "trace_id",
            "source_agent_run_id", "revision",
        ):
            self.assertNotIn(field, taken)
        self.assertEqual(
            {key: value for key, value in taken.items()},
            self._semantic_submission(),
        )

    async def test_intent_tool_rejects_empty_submission(self):
        collector = SubmissionCollector("intent submission")
        tool = self._intent_tool(collector)

        with self.assertRaises(Exception):
            await tool.invoke({})
        self.assertIsNone(collector.take())


class RealFactoryCompatibilityTests(unittest.TestCase):
    def test_runner_restores_and_loads_durable_todo_ledger(self):
        with tempfile.TemporaryDirectory() as workdir:
            runner = OpenJiuwenRunner(
                agent=SimpleNamespace(),
                workspace=SimpleNamespace(root_path=workdir),
                collector=SubmissionCollector("test"),
                session_lifecycle=OpenJiuwenCheckpointerManager(
                    Path(workdir) / "sessions.db"
                ),
            )

            restored = runner.restore_todo_ledger(
                "business:conv_1:bwf_123",
                7,
                [{"id": "task_1", "status": "pending", "dependsOn": []}],
            )
            loaded = runner.load_todo_ledger(
                "business:conv_1:bwf_123",
                revision=7,
            )

        self.assertEqual(restored, loaded)

    def test_user_prompt_states_intent_shape_and_slot_invariants(self):
        self.assertIn("single_intent 必须且只能有 1 个 task", USER_PROMPT)
        self.assertIn("ambiguous_intent 和 non_task 的 tasks 必须为空", USER_PROMPT)
        self.assertIn("缺少账户、卡片等槽位不等于意图模糊", USER_PROMPT)
        self.assertIn("intent_candidates", USER_PROMPT)
        self.assertIn("候选为空时只能输出 non_task 或 ambiguous_intent", USER_PROMPT)
        self.assertIn(
            "没有表达手机银行业务对象或动作时必须输出 non_task",
            USER_PROMPT,
        )
        self.assertIn(
            "ambiguous_intent 只用于两个或以上手机银行业务意图均有合理可能",
            USER_PROMPT,
        )
        self.assertNotIn("首批账户余额意图种子", USER_PROMPT)
        self.assertIn("required_shape", USER_PROMPT)
        self.assertIn("expected_task_count", USER_PROMPT)
        self.assertIn("intent_shape_assessment", USER_PROMPT)
        self.assertNotIn("amount_scope", USER_PROMPT)
        self.assertIn("slotContract", USER_PROMPT)
        self.assertNotIn("purpose", USER_PROMPT)
        self.assertIn("可选槽位缺失时必须省略", USER_PROMPT)
        self.assertIn("最终状态由 SlotContractEvaluator 重算", USER_PROMPT)
        self.assertIn("safety_review", USER_PROMPT)
        self.assertIn("user_memory_context_packs", USER_PROMPT)
        self.assertIn("本轮明确输入 > 当前 Session/Workflow > 页面上下文 > 长期记忆 > 模型推断", USER_PROMPT)
        self.assertIn("记忆不得补齐账号、金额、付款账户、验证码、风险等级", USER_PROMPT)
        self.assertIn("submit_safety_clarification", USER_PROMPT)
        self.assertIn("不得调用任何业务能力", USER_PROMPT)

    def test_business_prompt_delegates_domain_method_to_openjiuwen_skills(self):
        self.assertIn("按需读取本次 execution_view 中注册的 OpenJiuwen Skill", BUSINESS_PROMPT)
        self.assertIn("slotContract、taskPolicy 和 capabilityIds 是唯一业务契约", BUSINESS_PROMPT)
        self.assertIn("观察 Todo 与工具事实—规划下一步—调用一个允许工具—校验结果—更新 Todo", BUSINESS_PROMPT)
        self.assertNotIn("bank.credit-card.list", BUSINESS_PROMPT)
        self.assertNotIn("bank.transfer.order.create", BUSINESS_PROMPT)
        self.assertNotIn("CARD_MISSING", BUSINESS_PROMPT)

    def test_business_prompt_forbids_knowledge_generation_and_capability_calls(self):
        self.assertIn("知识型任务不得编造规则或调用业务能力", BUSINESS_PROMPT)

    def test_business_prompt_requires_native_todo_loop_for_complex_goals(self):
        self.assertIn("每个 task_id 必须有同名顶层 Todo 覆盖", BUSINESS_PROMPT)
        self.assertIn("澄清、本人确认和外部等待只挂起", BUSINESS_PROMPT)
        self.assertIn("恢复后不得重复已有不可变回执", BUSINESS_PROMPT)
        self.assertIn("memory_context_packs 只属于其 taskId/intentId，不得跨任务传播", BUSINESS_PROMPT)
        self.assertIn("requiresCoreRefresh=true", BUSINESS_PROMPT)
        self.assertIn("当前核心结果与记忆冲突时以核心结果为准", BUSINESS_PROMPT)

    def test_factory_returns_two_real_distinct_deepagents(self):
        with tempfile.TemporaryDirectory() as workdir:
            settings = RuntimeSettings(
                runtime_token="test-runtime-token",
                model_provider="openai",
                model_name="mock-model",
                model_base_url="http://127.0.0.1:9/v1",
                model_api_key="test-model-key",
                workspace_root=workdir,
            )
            lifecycle = OpenJiuwenCheckpointerManager(
                Path(workdir) / "sessions.db"
            )
            bundle = DeepAgentFactory(
                settings,
                session_lifecycle=lifecycle,
            ).create()
            view = bundle.business.execution_view_for_payload({
                "intent_task_envelope": {
                    "tasks": [{"intent_id": "ccb.account.balance.query"}]
                }
            })
            business_runner = bundle.business.get_or_create(view)

            self.assertEqual(bundle.user.agent.__class__.__name__, "DeepAgent")
            self.assertEqual(business_runner.agent.__class__.__name__, "DeepAgent")
            self.assertEqual(bundle.user.agent.card.id, "ai-native-bank-user-agent")
            self.assertEqual(business_runner.agent.card.id, "ai-native-bank-business-agent")
            self.assertNotEqual(bundle.user.workspace.root_path, business_runner.workspace.root_path)
            self.assertTrue(str(bundle.user.workspace.root_path).endswith("user"))
            self.assertIn("business", str(business_runner.workspace.root_path))
            self.assertIs(bundle.user.session_lifecycle, lifecycle)
            self.assertIs(business_runner.session_lifecycle, lifecycle)

    def test_factory_registers_read_gateway_only_on_business_deepagent(self):
        with tempfile.TemporaryDirectory() as workdir, patch(
            "app.deepagent_factory.create_deep_agent",
            side_effect=lambda **kwargs: SimpleNamespace(card=kwargs["card"]),
        ) as create_agent:
            settings = RuntimeSettings(
                runtime_token="test-runtime-token",
                model_provider="openai",
                model_name="mock-model",
                model_base_url="http://127.0.0.1:9/v1",
                model_api_key="test-model-key",
                workspace_root=workdir,
                node_gateway_url="http://node.test/internal/agent-capability-invocations",
            )
            lifecycle = OpenJiuwenCheckpointerManager(
                Path(workdir) / "sessions.db"
            )
            bundle = DeepAgentFactory(
                settings,
                session_lifecycle=lifecycle,
            ).create()
            view = bundle.business.execution_view_for_payload({
                "intent_task_envelope": {
                    "tasks": [{"intent_id": "ccb.account.balance.query"}]
                }
            })
            bundle.business.get_or_create(view)

        self.assertIsInstance(bundle.capability_invoker, AgentCapabilityInvoker)
        user_tools = [tool.card.name for tool in create_agent.call_args_list[0].kwargs["tools"]]
        business_tools = [tool.card.name for tool in create_agent.call_args_list[1].kwargs["tools"]]
        self.assertEqual(user_tools, [
            "submit_intent_task", "submit_safety_clarification",
        ])
        self.assertCountEqual(business_tools, [
            "submit_business_loop_outcome", "invoke_read_capability",
            "invoke_authorized_capability",
        ])
        self.assertNotIn("submit_capability_plan", business_tools)
        self.assertEqual(
            create_agent.call_args_list[1].kwargs["skills"],
            [str(view.skill_paths[0])],
        )
        self.assertNotIn(
            "skill_tool", create_agent.call_args_list[0].kwargs["permissions"]["tools"]
        )
        self.assertEqual(
            create_agent.call_args_list[1].kwargs["permissions"]["tools"]["skill_tool"],
            "allow",
        )

        for call in create_agent.call_args_list:
            allowed = call.kwargs["permissions"]["tools"]
            self.assertEqual(allowed["todo_create"], "allow")
            self.assertEqual(allowed["todo_list"], "allow")
            self.assertEqual(allowed["todo_get"], "allow")
            self.assertEqual(allowed["todo_modify"], "allow")
            self.assertNotIn("todo_read", allowed)
            self.assertNotIn("todo_update", allowed)

    def test_business_agent_mounts_skill_rail_without_filesystem_fallback_tools(self):
        with tempfile.TemporaryDirectory() as workdir, patch(
            "app.deepagent_factory.create_deep_agent",
            side_effect=lambda **kwargs: SimpleNamespace(card=kwargs["card"]),
        ) as create_agent:
            settings = RuntimeSettings(
                runtime_token="test-runtime-token",
                model_provider="openai",
                model_name="mock-model",
                model_base_url="http://127.0.0.1:9/v1",
                model_api_key="test-model-key",
                workspace_root=workdir,
                node_gateway_url="http://node.test/internal/agent-capability-invocations",
            )
            lifecycle = OpenJiuwenCheckpointerManager(
                Path(workdir) / "sessions.db"
            )
            bundle = DeepAgentFactory(
                settings,
                session_lifecycle=lifecycle,
            ).create()
            view = bundle.business.execution_view_for_payload({
                "intent_task_envelope": {
                    "tasks": [{"intent_id": "ccb.account.balance.query"}]
                }
            })
            bundle.business.get_or_create(view)

            user_kwargs = create_agent.call_args_list[0].kwargs
            # 用户智能体仅挂中流断连重试 rail（无技能 rail）
            user_rails = user_kwargs.get("rails")
            self.assertEqual(len(user_rails), 1)
            self.assertIsInstance(user_rails[0], ModelStreamRetryRail)
            business_rails = create_agent.call_args_list[1].kwargs["rails"]
            # 业务智能体固定挂三道 rail：ModelStreamRetryRail（中流断连
            # 重试）+ SkillUseRail（技能内容加载）+ SkillUsageRail（使用
            # 情况记录），见 deepagent_factory。
            self.assertEqual(len(business_rails), 3)
            self.assertIsInstance(business_rails[0], ModelStreamRetryRail)
            rail = next(
                item for item in business_rails
                if isinstance(item, SkillUseRail)
            )
            self.assertIsInstance(rail, SkillUseRail)
            # read_file/bash 兜底工具不得出现在业务智能体的模型可见面
            self.assertFalse(rail.include_tools)
            self.assertEqual(rail.skill_mode, "all")
            # 技能已物化进业务 workspace（sysop 沙箱内），rail 只扫描该目录
            skills_dir = Path(rail.skills_dir[0])
            self.assertTrue(skills_dir.is_dir(), skills_dir)
            self.assertIn("business", str(skills_dir))
            for item in view.skill_paths:
                self.assertTrue((skills_dir / Path(item).name / "SKILL.md").exists())
            self.assertEqual(
                sorted(rail.enabled_skills),
                sorted(Path(item).name for item in view.skill_paths),
            )


class OpenJiuwenNativeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_safe_revision_reads_native_state(self):
        with tempfile.TemporaryDirectory() as workdir:
            class Guard:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *_args):
                    return False

            lifecycle = Mock()
            lifecycle.session_run_guard = Mock(return_value=Guard())
            session = Mock(
                pre_run=AsyncMock(),
                post_run=AsyncMock(),
                get_state=Mock(return_value=4),
            )
            runner = OpenJiuwenRunner(
                agent=Mock(card=Mock()),
                workspace=SimpleNamespace(root_path=workdir),
                collector=Mock(reset=Mock(), take=Mock()),
                session_lifecycle=lifecycle,
            )

            with patch(
                "app.deepagent_factory.create_agent_session",
                return_value=session,
            ):
                revision = await runner.current_safe_revision(
                    "user:conv_revision"
                )

            self.assertEqual(revision, 4)
            session.pre_run.assert_awaited_once()
            session.post_run.assert_awaited_once()

    async def test_current_safe_revision_defaults_to_zero_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as workdir:
            class Guard:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *_args):
                    return False

            lifecycle = Mock()
            lifecycle.session_run_guard = Mock(return_value=Guard())
            session = Mock(
                pre_run=AsyncMock(),
                post_run=AsyncMock(),
                get_state=Mock(return_value=None),
            )
            runner = OpenJiuwenRunner(
                agent=Mock(card=Mock()),
                workspace=SimpleNamespace(root_path=workdir),
                collector=Mock(reset=Mock(), take=Mock()),
                session_lifecycle=lifecycle,
            )
            with patch(
                "app.deepagent_factory.create_agent_session",
                return_value=session,
            ):
                self.assertEqual(
                    await runner.current_safe_revision("user:conv_new"), 0
                )
                session.get_state.return_value = True
                with self.assertRaises(SessionRevisionConflict):
                    await runner.current_safe_revision("user:conv_corrupt")

    async def test_safety_review_uses_the_dedicated_user_agent_submission_channel(self):
        with tempfile.TemporaryDirectory() as workdir:
            intent_collector = SubmissionCollector("intent")
            clarification_collector = SubmissionCollector("clarification")
            runner = OpenJiuwenRunner(
                agent=Mock(card=Mock()),
                workspace=SimpleNamespace(root_path=workdir),
                collector=intent_collector,
                clarification_collector=clarification_collector,
                session_lifecycle=Mock(),
            )
            expected = {
                "schemaVersion": "1.0",
                "title": "请说明用途",
                "body": "请说明本次请求的实际用途。",
            }
            with patch.object(
                runner,
                "_run_with_collector",
                new=AsyncMock(return_value=expected),
            ) as invoke:
                result = await runner.run_safety_clarification(
                    {"safety_review": {"reason_code": "ambiguous_risk_intent"}},
                    session_id="user:conv_review.attempt.1234567890abcdef",
                )

            self.assertEqual(result, expected)
            self.assertIs(
                invoke.await_args.kwargs["collector"],
                clarification_collector,
            )
            self.assertEqual(
                invoke.await_args.kwargs["session_id"],
                "user:conv_review.attempt.1234567890abcdef",
            )

    async def test_user_attempt_uses_isolated_session_and_can_be_released(self):
        with tempfile.TemporaryDirectory() as workdir:
            lifecycle = Mock(release=AsyncMock())
            runner = OpenJiuwenRunner(
                agent=Mock(card=Mock()),
                workspace=SimpleNamespace(root_path=workdir),
                collector=Mock(reset=Mock(), take=Mock()),
                session_lifecycle=lifecycle,
            )
            with patch.object(
                runner,
                "run",
                new=AsyncMock(return_value={"intent_shape": "single_intent"}),
            ) as invoke:
                result = await runner.run_attempt(
                    {"message": "查询余额"},
                    canonical_session_id="user:conv_attempt_1",
                    request_id="req_attempt_1",
                )

            self.assertRegex(
                result.attempt_session_id,
                r"^user:conv_attempt_1\.attempt\.[0-9a-f]{16}$",
            )
            self.assertEqual(result.submission, {"intent_shape": "single_intent"})
            invoke.assert_awaited_once_with(
                {"message": "查询余额"},
                session_id=result.attempt_session_id,
            )

            await runner.rollback_attempt(result.attempt_session_id)
            lifecycle.release.assert_awaited_once_with([result.attempt_session_id])

    async def test_safe_commit_writes_projection_only_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as workdir:
            class Guard:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *_args):
                    return False

            lifecycle = Mock()
            lifecycle.session_run_guard = Mock(return_value=Guard())
            session = Mock(
                pre_run=AsyncMock(),
                update_state=Mock(),
                post_run=AsyncMock(),
                get_state=Mock(return_value=None),
            )
            runner = OpenJiuwenRunner(
                agent=Mock(card=Mock()),
                workspace=SimpleNamespace(root_path=workdir),
                collector=Mock(reset=Mock(), take=Mock()),
                session_lifecycle=lifecycle,
            )
            payload = {
                "message": "不得写入正式Session的原始消息",
                "intent_task_envelope": {
                    "delegation_id": "dlg_safe_1",
                    "revision": 1,
                    "intent_shape": "single_intent",
                    "tasks": [{
                        "task_id": "task_balance",
                        "intent_id": "account.balance.query",
                        "slots": {"accountId": {"value": "acct_secret"}},
                    }],
                },
            }
            with patch(
                "app.deepagent_factory.create_agent_session",
                return_value=session,
            ) as create_session:
                first = await runner.commit_safe_state(
                    payload,
                    canonical_session_id="user:conv_commit_1",
                    expected_revision=0,
                    request_id="req_commit_1",
                )
                second = await runner.commit_safe_state(
                    payload,
                    canonical_session_id="user:conv_commit_1",
                    expected_revision=0,
                    request_id="req_commit_1",
                )

            self.assertEqual(first, second)
            create_session.assert_called_once()
            safe_state = session.update_state.call_args.args[0]["aiNativeBankRuntime"]
            serialized = json.dumps(safe_state, ensure_ascii=False)
            self.assertEqual(safe_state["envelopeRevision"], 1)
            self.assertNotIn("原始消息", serialized)
            self.assertNotIn("acct_secret", serialized)
            self.assertNotIn("slots", serialized)
            session.post_run.assert_awaited_once()

    async def test_corrupt_node_todo_digest_never_writes_or_invokes_model(self):
        with tempfile.TemporaryDirectory() as workdir:
            session_id = "business:cust_demo_001:bwf_" + "b" * 24
            manager = OpenJiuwenCheckpointerManager(Path(workdir) / "sessions.db")
            session = Mock(
                pre_run=AsyncMock(),
                update_state=Mock(),
                post_run=AsyncMock(),
            )
            runner = OpenJiuwenRunner(
                agent=Mock(card=Mock(), invoke=AsyncMock()),
                workspace=SimpleNamespace(root_path=workdir),
                collector=Mock(reset=Mock(), take=Mock()),
                session_lifecycle=manager,
            )
            old_items = (
                TodoSnapshotItem(
                    id="old_goal", status="in_progress", depends_on=[]
                ),
            )
            runner.restore_todo_ledger(
                session_id,
                2,
                [{"id": "old_goal", "status": "in_progress", "dependsOn": []}],
            )

            create_session = Mock(return_value=session)
            with (
                patch(
                    "app.deepagent_factory.create_agent_session",
                    create_session,
                ),
                patch.object(
                    runner,
                    "load_todo_ledger",
                    wraps=runner.load_todo_ledger,
                ) as load_ledger,
                patch.object(
                    runner,
                    "restore_todo_ledger",
                    wraps=runner.restore_todo_ledger,
                ) as restore_ledger,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "^authoritative todo digest is invalid$",
                ):
                    await runner.run(
                        {
                            "business_workflow_id": "bwf_" + "b" * 24,
                            "_authoritative_node_workflow": {
                                "todoRevision": 3,
                                "todoSnapshot": [{
                                    "id": "new_goal",
                                    "status": "in_progress",
                                    "dependsOn": [],
                                }],
                                "todoDigest": "0" * 64,
                            },
                        },
                        session_id=session_id,
                    )
                load_ledger.assert_not_called()
                restore_ledger.assert_not_called()

            self.assertEqual(
                runner.load_todo_ledger(session_id, revision=2).items,
                old_items,
            )
            create_session.assert_not_called()
            session.pre_run.assert_not_awaited()
            session.update_state.assert_not_called()
            session.post_run.assert_not_awaited()
            runner.agent.invoke.assert_not_awaited()

    async def test_node_todo_reconciliation_occurs_after_native_session_restore(self):
        with tempfile.TemporaryDirectory() as workdir:
            session_id = "business:cust_demo_001:bwf_" + "a" * 24
            manager = OpenJiuwenCheckpointerManager(
                Path(workdir) / "sessions.db"
            )
            session = Mock(
                pre_run=AsyncMock(),
                update_state=Mock(),
                post_run=AsyncMock(),
            )
            runner = OpenJiuwenRunner(
                agent=Mock(card=Mock()),
                workspace=SimpleNamespace(root_path=workdir),
                collector=Mock(reset=Mock(), take=Mock(return_value={"ok": True})),
                session_lifecycle=manager,
            )
            runner.restore_todo_ledger(
                session_id,
                2,
                [{"id": "old_goal", "status": "in_progress", "dependsOn": []}],
            )
            node_items = (
                TodoSnapshotItem(
                    id="task_transfer", status="completed", depends_on=[]
                ),
                TodoSnapshotItem(
                    id="task_transfer_next", status="in_progress", depends_on=[]
                ),
            )

            async def invoke(payload, *, session):
                session.pre_run.assert_awaited_once()
                restored = runner.load_todo_ledger(session_id, revision=3)
                self.assertEqual(restored.items, node_items)
                self.assertNotIn(
                    "_authoritative_node_workflow",
                    json.loads(payload["query"]),
                )

            runner.agent.invoke = AsyncMock(side_effect=invoke)
            restore_payload = {
                "todoRevision": 3,
                "todoSnapshot": [
                    {
                        "id": item.id,
                        "status": item.status,
                        "dependsOn": list(item.depends_on),
                    }
                    for item in node_items
                ],
                "todoDigest": TodoLedger.digest(node_items),
            }

            with patch(
                "app.deepagent_factory.create_agent_session",
                return_value=session,
            ):
                with self.assertLogs(
                    "ai_native_bank.agent_runtime.session", level="WARNING"
                ) as logs:
                    await runner.run(
                        {
                            "business_workflow_id": "bwf_" + "a" * 24,
                            "_authoritative_node_workflow": restore_payload,
                        },
                        session_id=session_id,
                    )

            self.assertTrue(any(
                "code=node_workflow_todo_authoritative" in item
                for item in logs.output
            ))

    async def test_same_session_id_restores_safe_runtime_state_from_native_checkpointer(self):
        card = AgentCard(id="session-test-agent", name="session-test-agent", description="test")
        session_id = "ai-bank-session-checkpointer-test"
        first = create_agent_session(session_id=session_id, card=card)
        await first.pre_run(inputs={"runtimeInvocation": {"schemaVersion": "1.0"}})
        first.update_state({"aiNativeBankRuntime": {"envelopeRevision": 4}})
        await first.commit()

        restored = create_agent_session(session_id=session_id, card=card)
        await restored.pre_run(inputs={"runtimeInvocation": {"schemaVersion": "1.0"}})

        self.assertEqual(
            restored.get_state("aiNativeBankRuntime.envelopeRevision"),
            4,
        )


if __name__ == "__main__":
    unittest.main()
