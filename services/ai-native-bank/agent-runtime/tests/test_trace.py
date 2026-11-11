import json
import unittest

from pydantic import ValidationError

from app.contracts import CapabilityPlan, IntentTaskEnvelope, RuntimeAgentRunResponse
from app.trace import AgentTraceEvent, build_trace_event, project_safe_run
from tests.test_contracts import envelope_payload
from tests.test_service import plan_payload


def completed_run():
    envelope = envelope_payload()
    envelope["tasks"][0]["slots"] = {
        "accountId": {
            "value": "acct_salary_6217",
            "source": "user_selection",
            "confidence": 0.98,
        }
    }
    plan = plan_payload("bank.transfer.order.create")
    plan["steps"][0]["input_bindings"] = {
        "accountId": {"slot": "accountId"},
        "amountMinor": 50000,
    }
    intent = IntentTaskEnvelope.model_validate(envelope)
    capability_plan = CapabilityPlan.model_validate(plan)
    return RuntimeAgentRunResponse(
        agentRunId="abr_001",
        conversationId="conv_001",
        status="planned",
        intentTaskEnvelope=intent,
        capabilityPlan=capability_plan,
    )


class TraceContractTests(unittest.TestCase):
    def test_business_skill_lifecycle_exposes_only_registered_identity_and_counts(self):
        started = build_trace_event(
            sequence=6,
            stage="business_skill.started",
            status="running",
            agent_run_id="abr_001",
            trace_id="trace_001",
            title="业务 Skill 执行视图已装载",
            input_fields=[],
            output_fields=[],
            metrics={
                "skillId": "bank.wealth-advisory",
                "skillVersion": "1.0.0",
                "skillCount": 1,
                "capabilityCount": 1,
                "providerCount": 1,
            },
        )
        completed = build_trace_event(
            sequence=7,
            stage="business_skill.completed",
            status="completed",
            agent_run_id="abr_001",
            trace_id="trace_001",
            title="业务 Skill 已完成本轮编排",
            input_fields=[],
            output_fields=[],
            metrics={
                "skillId": "bank.wealth-advisory",
                "skillVersion": "1.0.0",
                "skillCount": 1,
                "outcome": "applied",
                "stepCount": 1,
            },
        )

        self.assertEqual(started.summary.metrics["skillId"], "bank.wealth-advisory")
        self.assertEqual(completed.summary.metrics["outcome"], "applied")
        self.assertNotIn("digest", started.model_dump_json(by_alias=True))

    def test_memory_lifecycle_events_use_registered_safe_trace_contracts(self):
        recalled = build_trace_event(
            sequence=1,
            stage="memory.candidates_built",
            status="completed",
            agent_run_id="abr_001",
            trace_id="trace_001",
            title="任务记忆召回完成",
            input_fields=["intentTaskEnvelope"],
            output_fields=["memoryCandidates"],
            metrics={"candidateCount": 2, "canonicalEpisodeCount": 2, "userPreferenceCount": 0},
        )
        recorded = build_trace_event(
            sequence=2,
            stage="memory.persistence_completed",
            status="completed",
            agent_run_id="abr_001",
            trace_id="trace_001",
            title="记忆候选已完成治理裁决",
            input_fields=["intentTaskEnvelope", "status"],
            output_fields=["memoryUpdateStatus"],
            metrics={"persistedCount": 1, "pendingRetryCount": 0, "deadLetterCount": 0},
        )

        self.assertEqual(recalled.summary.metrics["candidateCount"], 2)
        self.assertEqual(recorded.summary.metrics["persistedCount"], 1)

    def test_dual_stage_memory_events_use_bounded_metrics(self):
        memory_recall = {
            "schemaVersion": "1.0",
            "scope": "user",
            "detailStatus": "available",
            "groups": [{
                "key": "service_personalization",
                "label": "个性化偏好",
                "items": [{
                    "label": "回答风格",
                    "displayValue": "简洁",
                    "memoryKind": "semantic",
                    "sourceLabel": "用户明确设置",
                    "validFrom": "2026-09-01T00:00:00Z",
                    "applicationStatus": "applied",
                    "controls": ["current"],
                }],
            }],
            "conflicts": [],
            "omissions": [],
        }
        user_recalled = build_trace_event(
            sequence=1, stage="memory.user_context_recalled", status="completed",
            agent_run_id="abr_001", trace_id="trace_001",
            title="user memory recalled", input_fields=["conversationContext"],
            output_fields=["memoryContext"], metrics={
                "outcome": "applied", "contextPackCount": 2, "factCount": 3,
                "conflictCount": 0, "omissionCount": 1,
                "requiresCoreRefreshCount": 0, "requiresConfirmationCount": 0,
                "policyVersion": "bank-memory-user-v1",
            },
            memory_recall=memory_recall,
        )
        task_recalled = build_trace_event(
            sequence=2, stage="memory.task_context_recalled", status="completed",
            agent_run_id="abr_001", trace_id="trace_001",
            title="task memory recalled", input_fields=["intentTaskEnvelope"],
            output_fields=["memoryContext"], metrics={
                "outcome": "empty", "contextPackCount": 1, "factCount": 0,
                "conflictCount": 0, "omissionCount": 0,
                "requiresCoreRefreshCount": 0, "requiresConfirmationCount": 0,
                "policyVersion": "bank-memory-intent-v1",
                "inputDefaultCount": 2,
            },
            memory_recall={
                "schemaVersion": "1.0", "scope": "task", "detailStatus": "empty",
                "groups": [], "conflicts": [], "omissions": [],
            },
        )
        persistence = build_trace_event(
            sequence=3, stage="memory.persistence_completed", status="completed",
            agent_run_id="abr_001", trace_id="trace_001",
            title="memory persistence completed", input_fields=["memoryCandidates"],
            output_fields=["memoryUpdateStatus"], metrics={
                "persistedCount": 1, "pendingRetryCount": 0, "deadLetterCount": 0,
            },
        )

        self.assertEqual(user_recalled.summary.metrics["factCount"], 3)
        self.assertEqual(user_recalled.summary.memory_recall.groups[0].items[0].display_value, "简洁")
        self.assertEqual(task_recalled.summary.metrics["outcome"], "empty")
        self.assertEqual(task_recalled.summary.metrics["inputDefaultCount"], 2)
        self.assertEqual(persistence.summary.metrics["persistedCount"], 1)

    def test_memory_recall_detail_is_bound_only_to_memory_stages(self):
        detail = {
            "schemaVersion": "1.0", "scope": "user", "detailStatus": "empty",
            "groups": [], "conflicts": [], "omissions": [],
        }
        with self.assertRaises(ValidationError):
            build_trace_event(
                sequence=1, stage="request.accepted", status="accepted",
                agent_run_id="abr_001", trace_id="trace_001", title="request",
                input_fields=[], output_fields=[], metrics={}, memory_recall=detail,
            )
        with self.assertRaises(ValidationError):
            build_trace_event(
                sequence=1, stage="memory.user_context_recalled", status="completed",
                agent_run_id="abr_001", trace_id="trace_001", title="memory",
                input_fields=[], output_fields=[], metrics={
                    "outcome": "empty", "contextPackCount": 0, "factCount": 0,
                    "conflictCount": 0, "omissionCount": 0,
                    "requiresCoreRefreshCount": 0, "requiresConfirmationCount": 0,
                },
            )

    def test_event_contract_is_strict_and_bounds_status_duration_and_error(self):
        event = AgentTraceEvent.model_validate(
            {
                "schemaVersion": "1.0",
                "sequence": 1,
                "timestamp": "2026-08-29T10:00:00Z",
                "stage": "request.accepted",
                "status": "accepted",
                "agentRunId": "abr_001",
                "traceId": "trace_001",
                "summary": {
                    "title": "请求已接入",
                    "input": ["message"],
                    "output": ["traceId"],
                    "metrics": {},
                },
                "error": None,
            }
        )
        self.assertEqual(event.sequence, 1)
        self.assertEqual(event.stage, "request.accepted")

        invalid_cases = [
            {"durationMs": -1},
            {"stage": "model.raw.completed"},
            {"status": "waiting"},
            {"unexpected": "field"},
        ]
        base = event.model_dump(by_alias=True, mode="json")
        for changes in invalid_cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                AgentTraceEvent.model_validate({**base, **changes})

        with self.assertRaises(ValidationError):
            AgentTraceEvent.model_validate(
                {
                    **base,
                    "stage": "run.failed",
                    "status": "failed",
                    "error": None,
                }
            )

    def test_safe_run_projection_contains_names_and_graphs_but_never_values(self):
        view = project_safe_run(completed_run())
        serialized = json.dumps(view.model_dump(by_alias=True), ensure_ascii=False)

        self.assertEqual(view.tasks[0].slot_names, ["accountId"])
        self.assertEqual(view.steps[0].input_keys, ["accountId", "amountMinor"])
        self.assertEqual(view.steps[0].capability_id, "bank.transfer.order.create")
        self.assertNotIn('"value"', serialized)
        self.assertNotIn("acct_salary_6217", serialized)
        self.assertNotIn("input_bindings", serialized)
        self.assertNotIn("50000", serialized)

    def test_event_builder_assigns_safe_summary_and_rejects_free_form_metrics(self):
        event = build_trace_event(
            sequence=2,
            stage="user_agent.started",
            status="running",
            agent_run_id="abr_001",
            trace_id="trace_001",
            title="用户智能体开始识别",
            input_fields=["message", "pageContext"],
            output_fields=[],
            metrics={"capabilityCount": 0},
        )
        self.assertEqual(event.sequence, 2)
        self.assertEqual(event.summary.metrics, {"capabilityCount": 0})

        with self.assertRaises(ValidationError):
            build_trace_event(
                sequence=3,
                stage="user_agent.completed",
                status="completed",
                agent_run_id="abr_001",
                trace_id="trace_001",
                title="用户智能体识别完成",
                input_fields=[],
                output_fields=["intentShape"],
                metrics={"taskCount": {"account": "acct_salary_6217"}},
            )

    def test_event_builder_enforces_stage_metric_allowlist(self):
        user = build_trace_event(
            sequence=3,
            stage="user_agent.completed",
            status="completed",
            agent_run_id="abr_001",
            trace_id="trace_001",
            title="用户意图识别完成",
            input_fields=[],
            output_fields=[],
            metrics={
                "intentShape": "single_intent",
                "taskCount": 1,
                "candidateConstraint": "matched",
                "userRepairCount": 1,
                "intentSummary": "账户查询·查询与管理",
                "slotSummary": "槽位完整度 2/3 · 已识别 收款人、金额 · 待补充 转出账户",
            },
        )
        self.assertEqual(user.summary.metrics["candidateConstraint"], "matched")
        self.assertEqual(user.summary.metrics["intentSummary"], "账户查询·查询与管理")
        self.assertEqual(
            user.summary.metrics["slotSummary"],
            "槽位完整度 2/3 · 已识别 收款人、金额 · 待补充 转出账户",
        )

        with self.assertRaises(ValueError):
            build_trace_event(
                sequence=3,
                stage="user_agent.completed",
                status="completed",
                agent_run_id="abr_001",
                trace_id="trace_001",
                title="用户意图识别完成",
                input_fields=[],
                output_fields=[],
                metrics={"accountId": "acct_private"},
            )
        with self.assertRaises(ValueError):
            build_trace_event(
                sequence=1,
                stage="request.accepted",
                status="accepted",
                agent_run_id="abr_001",
                trace_id="trace_001",
                title="请求接入",
                input_fields=[],
                output_fields=[],
                metrics={"userRepairCount": 1},
            )
        with self.assertRaises(ValueError):
            build_trace_event(
                sequence=1,
                stage="request.accepted",
                status="accepted",
                agent_run_id="abr_001",
                trace_id="trace_001",
                title="请求接入",
                input_fields=[],
                output_fields=[],
                metrics={"intentSummary": "账户查询·查询与管理"},
            )

    def test_loop_and_confirmation_trace_use_only_safe_operational_counts(self):
        safe_metrics = {
            "loopIteration": 3,
            "todoRevision": 5,
            "currentTodoStatus": "awaiting_confirmation",
            "delegatedTaskCount": 2,
            "coveredTaskCount": 2,
        }
        event = build_trace_event(
            sequence=7,
            stage="business_agent.completed",
            status="completed",
            agent_run_id="abr_001",
            trace_id="trace_001",
            title="业务智能体循环已挂起",
            input_fields=[],
            output_fields=[],
            metrics=safe_metrics,
        )
        self.assertEqual(event.summary.metrics, safe_metrics)

        checkpoint = build_trace_event(
            sequence=9,
            stage="confirmation.checkpoint_created",
            status="completed",
            agent_run_id="abr_001",
            trace_id="trace_001",
            title="批次确认检查点已创建",
            input_fields=[],
            output_fields=["checkpointStatus"],
            metrics={
                "checkpointStatus": "pending",
                "grantCount": 0,
            },
        )
        self.assertEqual(checkpoint.summary.metrics["grantCount"], 0)

        forbidden = {
            "payee": "张帅",
            "amount": 10000,
            "accountId": "acct_salary_6217",
            "grantToken": "secret",
            "prompt": "raw prompt",
            "modelRawResponse": "raw model response",
        }
        for key, value in forbidden.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                build_trace_event(
                    sequence=7,
                    stage="business_agent.completed",
                    status="completed",
                    agent_run_id="abr_001",
                    trace_id="trace_001",
                    title="业务智能体循环完成",
                    input_fields=[],
                    output_fields=[],
                    metrics={key: value},
                )


if __name__ == "__main__":
    unittest.main()
