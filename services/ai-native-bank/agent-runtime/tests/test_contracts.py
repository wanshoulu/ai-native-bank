import copy
import json
import unittest

from pydantic import ValidationError

from app.contracts import (
    AgentSessionReleaseRequest,
    BusinessWorkflowResumeRequest,
    CapabilityPlan,
    ClarificationForm,
    ConfirmationSummary,
    ConversationState,
    IntentTaskEnvelope,
    RuntimeAgentRunRequest,
    RuntimeAgentRunResponse,
    UserFacingResult,
    UserResponse,
)


class ConfirmationSummaryContractTests(unittest.TestCase):
    def test_single_transaction_can_show_multiple_verified_detail_lines(self):
        summary = ConfirmationSummary.model_validate({
            "title": "请确认信用卡还款",
            "description": "确认后才会实际扣款。",
            "riskLevel": "L2",
            "items": [
                "信用卡：尾号8891",
                "还款金额：14,220.00元",
                "付款账户：尾号6217",
            ],
            "taskCount": 1,
            "totalAmountMinor": 1_422_000,
        })

        self.assertEqual(summary.task_count, 1)
        self.assertEqual(len(summary.items), 3)

    def test_batch_still_requires_one_summary_line_per_task(self):
        with self.assertRaises(ValidationError):
            ConfirmationSummary.model_validate({
                "title": "请确认批量办理",
                "description": "确认后逐项办理。",
                "riskLevel": "L2",
                "items": ["第一笔", "第二笔", "多余详情"],
                "taskCount": 2,
                "totalAmountMinor": 20_000,
            })


class AgentSessionIdentityContractTests(unittest.TestCase):
    def test_release_contract_matches_the_160_character_business_session_limit(self):
        maximum = "business:" + "c" * 96 + ":" + "w" * 54
        accepted = AgentSessionReleaseRequest.model_validate({
            "sessionIds": [maximum]
        })

        self.assertEqual(accepted.session_ids, [maximum])
        for invalid in (
            "business:" + "c" * 96 + ":" + "w" * 55,
            "business:" + "c" * 96 + ":" + "w" * 96,
            "business:客户:bwf_" + "a" * 24,
            "business:cust..other:bwf_" + "a" * 24,
        ):
            with self.subTest(session_id=invalid):
                with self.assertRaises(ValidationError):
                    AgentSessionReleaseRequest.model_validate({
                        "sessionIds": [invalid]
                    })


def request_payload():
    return {
        "agentRunId": "abr_001",
        "conversationId": "conv_001",
        "requestId": "req_001",
        "traceId": "trace_001",
        "customerId": "cust_demo_001",
        "actorType": "agent",
        "message": "给张三转 500 元",
        "pageContext": {"tab": "home", "menuKey": "home.transfer"},
        "executableCapabilities": [
            {
                "id": capability_id,
                "title": capability_id,
                "description": "Transfer entity discovery",
                "exposure": "model-callable",
                "sideEffect": "none",
                "riskLevel": "L1",
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
                "possibleErrors": [],
                "readiness": {"status": "ready", "environment": "mock", "blockers": []},
            }
            for capability_id in (
                "bank.account.list",
                "bank.payee.list",
                "bank.payee.validate",
                "bank.transfer.status.get",
            )
        ],
        "plannableCapabilities": [
            *[
                {
                    "id": capability_id,
                    "title": capability_id,
                    "description": "Transfer entity discovery",
                    "exposure": "model-callable",
                    "sideEffect": "none",
                    "riskLevel": "L1",
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "object"},
                    "possibleErrors": [],
                    "readiness": {"status": "ready", "environment": "mock", "blockers": []},
                }
                for capability_id in (
                    "bank.account.list",
                    "bank.payee.list",
                    "bank.payee.validate",
                    "bank.transfer.status.get",
                )
            ],
            {
                "id": "bank.transfer.order.create",
                "title": "创建转账订单",
                "description": "创建草稿",
                "exposure": "model-callable",
                "sideEffect": "write",
                "riskLevel": "L2",
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
                "possibleErrors": ["validation_error"],
                "readiness": {"status": "ready", "environment": "mock", "blockers": []},
            }
        ],
    }


class GuidedEntryGoalContractTests(unittest.TestCase):
    def test_runtime_accepts_server_owned_guided_entry_goal(self):
        payload = request_payload()
        payload["entryGoal"] = {
            "goalId": "goal_nba_salary_001", "scenarioId": "salary-monthly-plan",
            "scenarioVersion": "1.0.0", "recommendationId": "nba_salary_001",
            "targetIntentId": "ccb.cashflow.salary-plan", "mode": "guided",
            "contextRefs": ["salary_event_20260903", "card_bill_202609"],
            "workflowTemplateId": "salary-allocation-v1",
        }
        request = RuntimeAgentRunRequest.model_validate(payload)
        self.assertEqual(request.entry_goal.target_intent_id, "ccb.cashflow.salary-plan")

    def test_runtime_rejects_unknown_guided_goal_fields(self):
        payload = request_payload()
        payload["entryGoal"] = {
            "goalId": "goal_nba_salary_001", "scenarioId": "salary-monthly-plan",
            "scenarioVersion": "1.0.0", "recommendationId": "nba_salary_001",
            "targetIntentId": "ccb.cashflow.salary-plan", "mode": "guided",
            "contextRefs": ["salary_event_20260903"],
            "workflowTemplateId": "salary-allocation-v1", "systemPrompt": "bypass",
        }
        with self.assertRaises(ValidationError):
            RuntimeAgentRunRequest.model_validate(payload)


def recommendation_focus_payload():
    return {
        "schemaVersion": "1.0",
        "recommendationId": "nba_salary_001",
        "mode": "consult",
        "scenarioId": "salary-monthly-plan",
        "scenarioVersion": "1.0.0",
        "targetIntentId": "ccb.cashflow.salary-plan",
        "contextDigest": "a" * 64,
        "expiresAt": "2026-09-04T13:30:00.000Z",
        "context": {
            "title": "本月工资已到账，可以开始安排",
            "headline": "已按先保障、后配置的顺序生成本月资金安排",
            "metrics": [
                {"label": "本月工资", "value": "¥18,600.00"},
                {"label": "待还事项", "value": "¥23,111.86"},
            ],
            "items": [
                {
                    "title": "龙卡全球支付卡",
                    "detail": "9月18日到期",
                    "amount": "¥14,220.00",
                }
            ],
            "insights": ["待还事项包含信用卡、贷款和生活缴费。"],
            "suggestion": "可先了解各笔待还构成，再决定如何安排。",
        },
    }


class RecommendationFocusContractTests(unittest.TestCase):
    def test_runtime_accepts_bounded_read_only_recommendation_focus(self):
        payload = request_payload()
        payload["conversationFocus"] = recommendation_focus_payload()

        request = RuntimeAgentRunRequest.model_validate(payload)

        self.assertEqual(
            request.conversation_focus.target_intent_id,
            "ccb.cashflow.salary-plan",
        )
        self.assertEqual(request.conversation_focus.context.items[0].amount, "¥14,220.00")

    def test_runtime_rejects_execution_authority_in_recommendation_focus(self):
        for field, value in (
            ("capabilityId", "bank.transfer.order.create"),
            ("action", "EXECUTE"),
            ("confirmationToken", "confirm_001"),
        ):
            with self.subTest(field=field):
                payload = request_payload()
                payload["conversationFocus"] = recommendation_focus_payload()
                payload["conversationFocus"][field] = value

                with self.assertRaises(ValidationError):
                    RuntimeAgentRunRequest.model_validate(payload)


def envelope_payload(shape="single_intent", tasks=None):
    if tasks is None:
        tasks = [
            {
                "task_id": "task_001",
                "intent_id": "ccb.transfer.create",
                "action": "DO",
                "business_object": "transfer_order",
                "slots": {},
                "dependencies": [],
                "execution_readiness": "needs_slots",
                "risk_level": "L2",
            }
        ]
    return {
        "schema_version": "1.0",
        "delegation_id": "dlg_001",
        "trace_id": "trace_001",
        "conversation_id": "conv_001",
        "source_agent_run_id": "abr_001",
        "intent_shape": shape,
        "tasks": tasks,
        "predicted_next_intents": [],
    }


def clarification_response_payload():
    return {
        "schemaVersion": "1.0",
        "type": "clarification",
        "title": "选择查询账户",
        "body": "您想查询哪张卡的余额？",
        "items": [],
        "actions": [{
            "id": "account-option-1",
            "label": "工资卡",
            "description": "尾号 6217",
            "kind": "reply",
            "payload": {"message": "查询工资卡尾号 6217 的余额"},
        }],
        "source": None,
    }


def user_response_payload(response_type="result"):
    payload = {
        "schemaVersion": "1.0",
        "type": response_type,
        "title": "办理结果",
        "body": "业务信息已安全展示。",
        "items": [],
        "actions": [],
        "source": None,
    }
    if response_type == "answer":
        payload["source"] = {
            "id": "faq.transfer.arrival-time",
            "version": "1.0.0",
            "label": "Mock 业务知识",
        }
    if response_type == "navigation":
        payload["actions"] = [{
            "id": "open-account",
            "label": "账户总览",
            "description": None,
            "kind": "navigate",
            "payload": {"channel": "home", "featureId": "account", "actionId": "overview"},
        }]
    return payload


def runtime_response_payload(status="succeeded", response_type=None, legacy_result=False):
    payload = {
        "agentRunId": "abr_001",
        "conversationId": "conv_001",
        "status": status,
        "intentTaskEnvelope": envelope_payload(),
        "capabilityPlan": {
            "schema_version": "1.0",
            "business_agent_run_id": "bar_001",
            "delegation_id": "dlg_001",
            "status": "planned",
            "steps": [{
                "step_id": "step_001",
                "task_id": "task_001",
                "capability_id": "bank.account.balance.get",
                "input_bindings": {},
                "requires_user_confirmation": False,
                "depends_on": [],
            }],
        },
        "userResult": None,
        "userResponse": user_response_payload(response_type) if response_type else None,
        "confirmation": None,
    }
    if legacy_result:
        payload["userResult"] = {
            "kind": "account_balance",
            "title": "账户余额",
            "summary": "余额查询成功",
            "items": [],
            "primaryAction": None,
        }
    if status == "awaiting_confirmation":
        payload["confirmation"] = {
            "checkpointId": "acp_001",
            "capabilityId": "bank.transfer.order.create",
            "confirmationToken": "a" * 32,
            "expiresAt": "2026-08-30T12:00:00Z",
            "summary": {
                "title": "确认转账",
                "description": "提交前请核对交易信息",
                "riskLevel": "L2",
            },
        }
    return payload


class ContractTests(unittest.TestCase):
    def test_confirmation_resume_accepts_distinct_grants_for_one_guided_goal(self):
        request = BusinessWorkflowResumeRequest.model_validate({
            "resumeKind": "confirmation",
            "agentRunId": "abr_001",
            "conversationId": "conv_001",
            "customerId": "cust_demo_001",
            "checkpointId": "acp_001",
            "grants": [
                {
                    "grantId": "acg_001",
                    "taskId": "task_obligations",
                    "capabilityId": "bank.credit-card.repayment.create",
                    "expiresAt": "2026-09-03T18:00:00.000Z",
                },
                {
                    "grantId": "acg_002",
                    "taskId": "task_obligations",
                    "capabilityId": "bank.utility.payment.create",
                    "expiresAt": "2026-09-03T18:00:00.000Z",
                },
            ],
            "expectedWorkflowRevision": 1,
            "conversationContext": {"pendingIntentId": None, "slots": []},
            "executableCapabilityIds": [],
        })

        self.assertEqual(len(request.grants), 2)
        self.assertEqual(request.grants[0].task_id, request.grants[1].task_id)

    def test_business_clarification_resume_accepts_safe_choice_and_rejects_account_number(self):
        payload = {
            "resumeKind": "clarification",
            "agentRunId": "abr_001",
            "conversationId": "conv_001",
            "customerId": "cust_demo_001",
            "taskId": "task_transfer",
            "clarificationMessage": "使用工资卡尾号6217转账",
            "grants": [],
            "expectedWorkflowRevision": 1,
            "conversationContext": {"pendingIntentId": None, "slots": []},
            "executableCapabilityIds": ["bank.account.list"],
        }

        request = BusinessWorkflowResumeRequest.model_validate(payload)

        self.assertEqual(request.clarification_message, "使用工资卡尾号6217转账")
        unsafe = copy.deepcopy(payload)
        unsafe["clarificationMessage"] = "使用卡号 6217 0000 1234 5678 转账"
        with self.assertRaises(ValidationError):
            BusinessWorkflowResumeRequest.model_validate(unsafe)

    def test_intent_envelope_has_a_positive_revision(self):
        envelope = IntentTaskEnvelope.model_validate(envelope_payload())

        self.assertEqual(envelope.revision, 1)

        invalid = envelope_payload()
        invalid["revision"] = 0
        with self.assertRaises(ValidationError):
            IntentTaskEnvelope.model_validate(invalid)

    def test_intent_lifecycle_revision_is_monotonic_and_completed_goals_are_immutable(self):
        from app.intent_lifecycle import (
            merge_envelope_revision,
            next_envelope_revision,
        )

        self.assertEqual(next_envelope_revision(None), 1)
        self.assertEqual(next_envelope_revision(3), 4)
        with self.assertRaises(ValueError):
            merge_envelope_revision(
                previous_tasks={
                    "transfer_zhangsan": {"amountMinor": 20000},
                },
                completed_task_ids={"transfer_zhangsan"},
                revised_tasks={
                    "transfer_zhangsan": {"amountMinor": 30000},
                },
            )

    def test_private_conversation_state_v2_preserves_multiple_business_goals(self):
        state = ConversationState.model_validate({
            "schemaVersion": "2.0",
            "pendingEnvelopeId": "dlg_multi_001",
            "intentShape": "same_intent_multi_task",
            "goals": [
                {
                    "taskId": "task_zhang",
                    "intentId": "ccb.transfer.create",
                    "status": "needs_slots",
                    "slots": [{"name": "payee", "value": "张帅", "source": "user_explicit"}],
                },
                {
                    "taskId": "task_li",
                    "intentId": "ccb.transfer.create",
                    "status": "needs_slots",
                    "slots": [{"name": "payee", "value": "李四", "source": "user_explicit"}],
                },
            ],
            "sharedSlots": [
                {"name": "amount", "value": "100", "source": "user_explicit"},
            ],
            "ambiguities": [{
                "type": "amount_scope",
                "taskIds": ["task_zhang", "task_li"],
                "options": ["per_task", "total"],
            }],
            "status": "retain",
        })

        self.assertEqual(state.schema_version, "2.0")
        self.assertEqual(state.envelope_revision, 1)
        self.assertEqual([goal.task_id for goal in state.goals], ["task_zhang", "task_li"])
        self.assertEqual(state.shared_slots[0].value, "100")
        self.assertEqual(state.ambiguities[0].type, "amount_scope")

    def test_private_conversation_state_v2_rejects_unknown_ambiguity_goal(self):
        with self.assertRaises(ValidationError):
            ConversationState.model_validate({
                "schemaVersion": "2.0",
                "pendingEnvelopeId": "dlg_multi_001",
                "intentShape": "same_intent_multi_task",
                "goals": [{
                    "taskId": "task_zhang", "intentId": "ccb.transfer.create",
                    "status": "needs_slots", "slots": [],
                }],
                "sharedSlots": [],
                "ambiguities": [{
                    "type": "amount_scope",
                    "taskIds": ["task_zhang", "task_missing"],
                    "options": ["per_task", "total"],
                }],
                "status": "retain",
            })

    def test_private_conversation_state_accepts_only_bounded_semantic_slots(self):
        state = ConversationState.model_validate({
            "schemaVersion": "1.0",
            "pendingIntentId": "ccb.transfer.create",
            "status": "retain",
            "slots": [
                {"name": "payee", "value": "张三", "source": "user_explicit"},
                {"name": "amount", "value": "100", "source": "user_explicit"},
            ],
        })
        self.assertEqual([item.name for item in state.slots], ["payee", "amount"])

        for invalid_slot in (
            {"name": "payeeId", "value": "secret", "source": "context"},
            {"name": "payee", "value": {"id": "nested"}, "source": "context"},
            {"name": "confirmationToken", "value": "secret", "source": "context"},
        ):
            with self.subTest(invalid_slot=invalid_slot), self.assertRaises(ValidationError):
                ConversationState.model_validate({
                    "schemaVersion": "1.0",
                    "pendingIntentId": "ccb.transfer.create",
                    "status": "retain",
                    "slots": [invalid_slot],
                })

    def test_private_conversation_state_accepts_graph_defined_non_transfer_slots(self):
        state = ConversationState.model_validate({
            "schemaVersion": "2.0",
            "pendingEnvelopeId": "dlg_card_001",
            "envelopeRevision": 3,
            "intentShape": "same_intent_multi_task",
            "goals": [
                {
                    "taskId": "task_card_8891",
                    "intentId": "ccb.credit.card.lost-report",
                    "status": "ready",
                    "slots": [
                        {"name": "cardReference", "value": "尾号8891", "source": "user_explicit"},
                        {"name": "lossReason", "value": "卡片遗失", "source": "user_explicit"},
                    ],
                },
                {
                    "taskId": "task_card_2268",
                    "intentId": "ccb.credit.card.lost-report",
                    "status": "needs_slots",
                    "slots": [
                        {"name": "cardReference", "value": "尾号2268", "source": "user_explicit"},
                    ],
                },
            ],
            "sharedSlots": [
                {"name": "lossReason", "value": "卡片遗失", "source": "context"},
            ],
            "ambiguities": [{
                "type": "execution_scope",
                "taskIds": ["task_card_8891", "task_card_2268"],
                "options": ["sequential", "all_eligible"],
            }],
            "status": "retain",
        })

        self.assertEqual(state.goals[0].slots[0].name, "cardReference")
        self.assertEqual(state.ambiguities[0].type, "execution_scope")

    def test_private_conversation_state_rejects_internal_slot_names_across_domains(self):
        for slot_name in ("cardId", "accountId", "providerToken", "checkpointId"):
            with self.subTest(slot_name=slot_name), self.assertRaises(ValidationError):
                ConversationState.model_validate({
                    "schemaVersion": "1.0",
                    "pendingIntentId": "ccb.credit.card.lost-report",
                    "status": "retain",
                    "slots": [{
                        "name": slot_name,
                        "value": "opaque-secret",
                        "source": "context",
                    }],
                })

    def test_runtime_request_rejects_unknown_or_browser_owned_authority(self):
        payload = request_payload()
        payload["authorizationToken"] = "must-not-pass"
        with self.assertRaises(ValidationError):
            RuntimeAgentRunRequest.model_validate(payload)

    def test_runtime_request_accepts_bounded_semantic_conversation_context(self):
        payload = request_payload()
        payload["conversationContext"] = {
            "pendingIntentId": "ccb.transfer.create",
            "slots": [{
                "name": "amount",
                "value": "100",
                "source": "user_explicit",
                "sourceIntentId": "ccb.transfer.create",
                "updatedAt": "2026-08-30T12:00:00Z",
            }],
        }

        request = RuntimeAgentRunRequest.model_validate(payload)
        self.assertEqual(request.conversation_context.slots[0].name, "amount")

    def test_runtime_request_accepts_only_bounded_redacted_safety_context(self):
        payload = request_payload()
        payload["safetyContext"] = {
            "recentUserMessages": ["上一轮问题", "当前问题"],
        }
        request = RuntimeAgentRunRequest.model_validate(payload)
        self.assertEqual(
            request.safety_context.recent_user_messages,
            ["上一轮问题", "当前问题"],
        )

        payload["safetyContext"]["recentUserMessages"] = ["x"] * 7
        with self.assertRaises(ValidationError):
            RuntimeAgentRunRequest.model_validate(payload)

    def test_runtime_request_accepts_v2_multi_goal_conversation_context(self):
        payload = request_payload()
        payload["message"] = "每人100元"
        payload["conversationContext"] = {
            "pendingEnvelopeId": "dlg_multi_001",
            "intentShape": "same_intent_multi_task",
            "goals": [
                {
                    "taskId": "task_zhang",
                    "intentId": "ccb.transfer.create",
                    "status": "needs_slots",
                    "slots": [{
                        "name": "payee", "value": "张帅", "source": "context",
                        "sourceIntentId": "ccb.transfer.create",
                        "updatedAt": "2026-08-30T12:00:00Z",
                    }],
                },
                {
                    "taskId": "task_li",
                    "intentId": "ccb.transfer.create",
                    "status": "needs_slots",
                    "slots": [{
                        "name": "payee", "value": "李四", "source": "context",
                        "sourceIntentId": "ccb.transfer.create",
                        "updatedAt": "2026-08-30T12:00:00Z",
                    }],
                },
            ],
            "sharedSlots": [{
                "name": "amount", "value": "100", "source": "context",
                "sourceIntentId": "ccb.transfer.create",
                "updatedAt": "2026-08-30T12:00:00Z",
            }],
            "ambiguities": [{
                "type": "amount_scope",
                "taskIds": ["task_zhang", "task_li"],
                "options": ["per_task", "total"],
            }],
        }

        request = RuntimeAgentRunRequest.model_validate(payload)

        self.assertEqual(request.conversation_context.intent_shape, "same_intent_multi_task")
        self.assertEqual(len(request.conversation_context.goals), 2)

    def test_runtime_request_rejects_internal_nested_or_cross_intent_slots(self):
        invalid_slots = (
            {"name": "payeeId", "value": "internal"},
            {"name": "amount", "value": {"nested": True}},
            {"name": "amount", "value": "100", "sourceIntentId": "ccb.account.balance.query"},
        )
        for invalid in invalid_slots:
            payload = request_payload()
            payload["conversationContext"] = {
                "pendingIntentId": "ccb.transfer.create",
                "slots": [{
                    "name": invalid["name"],
                    "value": invalid["value"],
                    "source": "context",
                    "sourceIntentId": invalid.get("sourceIntentId", "ccb.transfer.create"),
                    "updatedAt": "2026-08-30T12:00:00Z",
                }],
            }
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                RuntimeAgentRunRequest.model_validate(payload)

    def test_intent_shape_enforces_task_cardinality(self):
        with self.assertRaises(ValidationError):
            IntentTaskEnvelope.model_validate(envelope_payload(tasks=[]))
        with self.assertRaises(ValidationError):
            IntentTaskEnvelope.model_validate(envelope_payload("non_task"))

        non_task = IntentTaskEnvelope.model_validate(envelope_payload("non_task", []))
        self.assertEqual(non_task.tasks, [])

    def test_multi_task_envelope_preserves_task_level_ambiguity_and_shared_slots(self):
        first = envelope_payload()["tasks"][0]
        first["task_id"] = "task_zhang"
        first["slots"] = {
            "payee": {"value": "张帅", "source": "user_explicit", "confidence": 1}
        }
        second = copy.deepcopy(first)
        second["task_id"] = "task_li"
        second["slots"]["payee"]["value"] = "李四"
        payload = envelope_payload("same_intent_multi_task", [first, second])
        payload["ambiguities"] = [{
            "type": "amount_scope",
            "task_ids": ["task_zhang", "task_li"],
            "options": ["per_task", "total"],
        }]
        payload["shared_slots"] = {
            "amount": {"value": "100", "source": "user_explicit", "confidence": 1}
        }

        envelope = IntentTaskEnvelope.model_validate(payload)

        self.assertEqual(len(envelope.tasks), 2)
        self.assertEqual(envelope.tasks[0].task_status, "pending")
        self.assertEqual(envelope.ambiguities[0].task_ids, ["task_zhang", "task_li"])
        self.assertEqual(envelope.shared_slots["amount"].value, "100")

    def test_multi_task_envelope_rejects_unknown_or_duplicate_ambiguity_targets(self):
        first = envelope_payload()["tasks"][0]
        first["task_id"] = "task_zhang"
        second = copy.deepcopy(first)
        second["task_id"] = "task_li"
        base = envelope_payload("same_intent_multi_task", [first, second])

        for task_ids in (["task_zhang", "task_missing"], ["task_zhang", "task_zhang"]):
            payload = copy.deepcopy(base)
            payload["ambiguities"] = [{
                "type": "amount_scope",
                "task_ids": task_ids,
                "options": ["per_task", "total"],
            }]
            with self.subTest(task_ids=task_ids), self.assertRaises(ValidationError):
                IntentTaskEnvelope.model_validate(payload)

    def test_intent_schema_exposes_cross_field_rules_to_the_model(self):
        description = IntentTaskEnvelope.model_json_schema().get("description", "")
        self.assertIn("single_intent requires exactly one task", description)
        self.assertIn("multi_intent and same_intent_multi_task require at least two tasks", description)
        self.assertIn("ambiguous_intent and non_task require tasks=[]", description)
        self.assertIn("missing slots do not make an intent ambiguous", description)

    def test_capability_plan_is_strict_and_never_contains_execution_result(self):
        payload = {
            "schema_version": "1.0",
            "business_agent_run_id": "bar_001",
            "delegation_id": "dlg_001",
            "status": "planned",
            "steps": [
                {
                    "step_id": "step_001",
                    "task_id": "task_001",
                    "capability_id": "bank.transfer.order.create",
                    "input_bindings": {"amountMinor": {"slot": "amount_minor"}},
                    "requires_user_confirmation": False,
                    "depends_on": [],
                }
            ],
        }
        plan = CapabilityPlan.model_validate(payload)
        self.assertEqual(plan.steps[0].capability_id, "bank.transfer.order.create")

        payload["executionResult"] = {"receiptNo": "should-not-exist"}
        with self.assertRaises(ValidationError):
            CapabilityPlan.model_validate(payload)


class UserResponseContractTests(unittest.TestCase):
    def test_clarification_choices_must_be_reply_actions(self):
        response = UserResponse.model_validate(clarification_response_payload())
        self.assertEqual(response.actions[0].kind, "reply")
        invalid = clarification_response_payload()
        invalid["actions"][0] = {
            "id": "open-account", "label": "账户总览", "description": None,
            "kind": "navigate",
            "payload": {"channel": "home", "featureId": "account", "actionId": "overview"},
        }
        with self.assertRaises(ValidationError):
            UserResponse.model_validate(invalid)

    def test_answer_requires_governed_source(self):
        payload = clarification_response_payload()
        payload.update({
            "type": "answer", "title": "转账到账时间",
            "body": "请以转账记录显示的处理状态为准。",
            "actions": [],
            "source": {"id": "faq.transfer.arrival-time", "version": "1.0.0", "label": "Mock 业务知识"},
        })
        self.assertEqual(UserResponse.model_validate(payload).source.id, "faq.transfer.arrival-time")
        payload["source"] = None
        with self.assertRaises(ValidationError):
            UserResponse.model_validate(payload)

    def test_response_rejects_internal_ids_full_accounts_and_unknown_fields(self):
        for bad_text in (
            "acct_salary_6217",
            "6217000012345678",
            "6217 0000 1234 5678",
            "6217-0000-1234-5678",
            "bank.account.balance.get",
            "tokenHash",
            "confirmationToken",
        ):
            payload = clarification_response_payload()
            payload["body"] = bad_text
            with self.assertRaises(ValidationError):
                UserResponse.model_validate(payload)
        payload = clarification_response_payload()
        payload["debug"] = True
        with self.assertRaises(ValidationError):
            UserResponse.model_validate(payload)

    def test_response_normalizes_unicode_before_rejecting_sensitive_text(self):
        for bad_text in (
            "6217\u200b0000\u200b1234\u200b5678",
            "6217—0000—1234—5678",
            "6217。0000。1234。5678",
            "6227💳0000💳1234💳6217",
            "6227\u03010000\u03011234\u03016217",
            "62270000a12346217",
            "62270000中12346217",
            "６２２７💳００００\u0301１２３４中６２１７",
            "accountId",
            "account\u200bId",
            "cardId",
            "payeeId",
            "capabilityId",
            "clarification_required",
            "needs_slots",
            "errorCode",
            "failedStage",
            "confirmation%54oken",
            "account%49d",
            "confirmation%2554oken",
            "3782💳822463💳10005",
            "３７８２💳８２２４６３💳１０００５",
            "3782%F0%9F%92%B3822463%F0%9F%92%B310005",
        ):
            payload = user_response_payload()
            payload["body"] = bad_text
            with self.subTest(bad_text=bad_text), self.assertRaises(ValidationError):
                UserResponse.model_validate(payload)

    def test_legacy_result_uses_the_same_unicode_pan_boundary(self):
        for bad_text in (
            "6227💳0000💳1234💳6217",
            "6227\u03010000\u03011234\u03016217",
            "62270000a12346217",
            "62270000中12346217",
            "６２２７💳００００\u0301１２３４中６２１７",
            "confirmation%54oken",
            "account%49d",
            "confirmation%2554oken",
            "3782💳822463💳10005",
            "3782%F0%9F%92%B3822463%F0%9F%92%B310005",
        ):
            with self.subTest(bad_text=bad_text), self.assertRaises(ValidationError):
                UserFacingResult(
                    kind="balance",
                    title="余额查询结果",
                    summary=bad_text,
                )

    def test_digit_guard_blocks_account_chains_but_allows_scattered_financial_figures(self):
        # 账号形态：连续数字串（含标点/空白/零宽/emoji/单字母填充分隔）仍被拦截
        for bad_text in (
            "6222ㅤ0202ㅤ0202ㅤ0202",
            "6227-0000-1234-6217",
            "6222 0212 3456 7890",
            "62220000a12346217",
        ):
            payload = user_response_payload()
            payload["body"] = bad_text
            with self.subTest(bad_text=bad_text), self.assertRaises(ValidationError):
                UserResponse.model_validate(payload)
        # 金融文本数字密集但呈短串分布（组间有真实文字），不得误杀
        for good_text in (
            "精选 50 只货币基金，七日年化 0.863%-1.74%，综合日内快赎额度 50 万元",
            "建议金额 ¥100,000.00 起 · 至多 ¥200,000.00",
            "乾元-建行龙宝（按日）开放式净值型理财产品 · R2 · 建议金额 ¥100,000.00 起 · 至多 ¥200,000.00",
        ):
            payload = user_response_payload()
            payload["body"] = good_text
            with self.subTest(good_text=good_text):
                UserResponse.model_validate(payload)

    def test_items_require_non_empty_text_up_to_240_characters(self):
        payload = user_response_payload()
        payload["items"] = ["余" * 240]
        self.assertEqual(len(UserResponse.model_validate(payload).items[0]), 240)

        for invalid_item in ("", "余" * 241):
            invalid = user_response_payload()
            invalid["items"] = [invalid_item]
            with self.subTest(length=len(invalid_item)), self.assertRaises(ValidationError):
                UserResponse.model_validate(invalid)

    def test_reply_action_requires_and_only_accepts_message(self):
        payload = clarification_response_payload()
        self.assertEqual(UserResponse.model_validate(payload).actions[0].kind, "reply")

        missing_message = clarification_response_payload()
        missing_message["actions"][0]["payload"] = {}
        with self.assertRaises(ValidationError):
            UserResponse.model_validate(missing_message)

        target_leak = clarification_response_payload()
        target_leak["actions"][0]["payload"].update({
            "channel": "home", "featureId": "account", "actionId": "overview",
        })
        with self.assertRaises(ValidationError):
            UserResponse.model_validate(target_leak)

    def test_reply_action_accepts_explicit_interaction_and_rejects_it_for_other_kinds(self):
        payload = clarification_response_payload()
        payload["actions"][0]["interaction"] = "submit"
        action = UserResponse.model_validate(payload).actions[0]
        self.assertEqual(action.interaction, "submit")

        payload["actions"][0]["interaction"] = "compose"
        self.assertEqual(
            UserResponse.model_validate(payload).actions[0].interaction,
            "compose",
        )

        payload["actions"][0]["interaction"] = "unknown"
        with self.assertRaises(ValidationError):
            UserResponse.model_validate(payload)

        navigation = user_response_payload("navigation")
        navigation["actions"][0]["interaction"] = "submit"
        with self.assertRaises(ValidationError):
            UserResponse.model_validate(navigation)

    def test_navigate_action_requires_and_only_accepts_registered_target(self):
        payload = user_response_payload("navigation")
        payload["actions"] = [{
            "id": "open-account",
            "label": "账户总览",
            "description": None,
            "kind": "navigate",
            "payload": {"channel": "home", "featureId": "account", "actionId": "overview"},
        }]
        self.assertEqual(UserResponse.model_validate(payload).actions[0].kind, "navigate")

        for missing_field in ("channel", "featureId", "actionId"):
            invalid = user_response_payload("navigation")
            invalid_action = {**payload["actions"][0], "payload": dict(payload["actions"][0]["payload"])}
            invalid_action["payload"].pop(missing_field)
            invalid["actions"] = [invalid_action]
            with self.subTest(missing_field=missing_field), self.assertRaises(ValidationError):
                UserResponse.model_validate(invalid)

        message_leak = user_response_payload("navigation")
        message_leak["actions"] = [{
            **payload["actions"][0],
            "payload": {**payload["actions"][0]["payload"], "message": "查询余额"},
        }]
        with self.assertRaises(ValidationError):
            UserResponse.model_validate(message_leak)

    def test_navigation_response_requires_at_least_one_and_only_navigate_actions(self):
        payload = user_response_payload("navigation")
        response = UserResponse.model_validate(payload)
        self.assertEqual([action.kind for action in response.actions], ["navigate"])

        no_actions = user_response_payload("navigation")
        no_actions["actions"] = []

        mixed_actions = user_response_payload("navigation")
        mixed_actions["actions"].append({
            "id": "reply-instead",
            "label": "回复",
            "description": None,
            "kind": "reply",
            "payload": {"message": "查询余额"},
        })
        for case, invalid in (("empty", no_actions), ("mixed", mixed_actions)):
            with self.subTest(case=case), self.assertRaises(ValidationError):
                UserResponse.model_validate(invalid)

    def test_confirm_action_accepts_only_empty_payload(self):
        payload = user_response_payload("confirmation")
        payload["actions"] = [{
            "id": "confirm-transfer", "label": "确认", "description": None,
            "kind": "confirm", "payload": {},
        }]
        self.assertEqual(UserResponse.model_validate(payload).actions[0].kind, "confirm")

        for forbidden_payload in (
            {"message": "确认"},
            {"channel": "home", "featureId": "transfer", "actionId": "apply"},
        ):
            invalid = user_response_payload("confirmation")
            invalid["actions"] = [{**payload["actions"][0], "payload": forbidden_payload}]
            with self.subTest(payload=forbidden_payload), self.assertRaises(ValidationError):
                UserResponse.model_validate(invalid)

    def test_dismiss_action_accepts_only_empty_payload(self):
        payload = user_response_payload("notice")
        payload["actions"] = [{
            "id": "dismiss-notice", "label": "知道了", "description": None,
            "kind": "dismiss", "payload": {},
        }]
        self.assertEqual(UserResponse.model_validate(payload).actions[0].kind, "dismiss")

        for forbidden_payload in (
            {"message": "关闭"},
            {"channel": "home", "featureId": "account", "actionId": "overview"},
        ):
            invalid = user_response_payload("notice")
            invalid["actions"] = [{**payload["actions"][0], "payload": forbidden_payload}]
            with self.subTest(payload=forbidden_payload), self.assertRaises(ValidationError):
                UserResponse.model_validate(invalid)

    def test_all_six_response_types_are_exact(self):
        for response_type in (
            "result", "clarification", "answer", "confirmation", "navigation", "notice",
        ):
            with self.subTest(response_type=response_type):
                self.assertEqual(
                    UserResponse.model_validate(user_response_payload(response_type)).type,
                    response_type,
                )
        with self.assertRaises(ValidationError):
            UserResponse.model_validate(user_response_payload("internal-debug"))

    def test_action_ids_are_unique(self):
        payload = clarification_response_payload()
        payload["actions"].append({
            **payload["actions"][0],
            "payload": dict(payload["actions"][0]["payload"]),
        })

        with self.assertRaises(ValidationError):
            UserResponse.model_validate(payload)

    def test_response_and_nested_public_values_are_deeply_immutable(self):
        payload = clarification_response_payload()
        payload["items"] = ["已验证项目"]
        response = UserResponse.model_validate(payload)

        with self.assertRaises(ValidationError):
            response.body = "被修改"
        with self.assertRaises(AttributeError):
            response.items.append("新增项目")
        with self.assertRaises(AttributeError):
            response.actions.append(response.actions[0])
        with self.assertRaises(ValidationError):
            response.actions[0].label = "被修改"
        with self.assertRaises(ValidationError):
            response.actions[0].payload.message = "被修改"

        serialized = response.model_dump_json(by_alias=True)
        self.assertIn('"items":["已验证项目"]', serialized)
        self.assertIn('"actions":[{', serialized)

    def test_answer_source_and_legacy_result_are_immutable(self):
        answer = UserResponse.model_validate(user_response_payload("answer"))
        legacy = UserFacingResult(
            kind="balance",
            title="余额查询结果",
            summary="查询完成。",
            items=["尾号 6217"],
        )

        with self.assertRaises(ValidationError):
            answer.source.version = "9.9.9"
        with self.assertRaises(ValidationError):
            legacy.summary = "被修改"
        with self.assertRaises(AttributeError):
            legacy.items.append("新增项目")


class RuntimeAgentRunResponseContractTests(unittest.TestCase):
    def test_ordered_responses_allow_completed_result_before_active_clarification(self):
        payload = runtime_response_payload(
            status="clarification_required",
            response_type=None,
        )
        payload["userResponses"] = [
            user_response_payload("result"),
            user_response_payload("clarification"),
        ]

        result = RuntimeAgentRunResponse.model_validate(payload)

        self.assertIsNone(result.user_response)
        self.assertEqual(
            [item.type for item in result.user_responses],
            ["result", "clarification"],
        )

    def test_ordered_responses_enforce_exclusive_bounded_terminal_contract(self):
        valid = runtime_response_payload(
            status="clarification_required",
            response_type=None,
        )
        valid["userResponses"] = [
            user_response_payload("result"),
            user_response_payload("clarification"),
        ]

        invalid_payloads = []
        both = copy.deepcopy(valid)
        both["userResponse"] = user_response_payload("clarification")
        invalid_payloads.append(both)
        empty = copy.deepcopy(valid)
        empty["userResponses"] = []
        invalid_payloads.append(empty)
        too_many = copy.deepcopy(valid)
        too_many["userResponses"] = [user_response_payload("result") for _ in range(8)] + [
            user_response_payload("clarification")
        ]
        invalid_payloads.append(too_many)
        wrong_terminal = copy.deepcopy(valid)
        wrong_terminal["userResponses"] = [user_response_payload("result")]
        invalid_payloads.append(wrong_terminal)
        actionable_prefix = copy.deepcopy(valid)
        actionable_prefix["userResponses"][0]["actions"] = [{
            "id": "continue-next",
            "label": "继续",
            "description": None,
            "kind": "reply",
            "payload": {"message": "继续办理"},
        }]
        invalid_payloads.append(actionable_prefix)

        for invalid in invalid_payloads:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                RuntimeAgentRunResponse.model_validate(invalid)

    def test_v11_clarification_carries_task_progress_and_separate_form(self):
        payload = runtime_response_payload(
            status="clarification_required",
            response_type="clarification",
        )
        payload["userResponse"].update({
            "schemaVersion": "1.1",
            "title": "补充第2笔收款信息",
            "body": "请补充李四的收款账号。",
            "items": ["转出账户：工资卡 · 尾号6217", "金额：200.00元"],
            "taskProgress": {
                "mode": "sequential", "current": 2, "total": 2,
                "completed": 1, "failed": 0, "skipped": 0,
            },
        })
        payload["clarificationForm"] = {
            "schemaVersion": "1.0", "clarificationId": "acl_001",
            "submissionToken": "x" * 43,
            "expiresAt": "2026-08-31T10:05:00.000Z",
            "kind": "transfer_payee",
            "fields": [
                {"name": "payeeName", "label": "收款人姓名", "type": "text", "required": True, "value": "李四", "editable": False},
                {"name": "accountNo", "label": "收款账号", "type": "bank_account", "required": True, "value": "", "editable": True},
                {"name": "bankName", "label": "收款银行", "type": "bank_name", "required": True, "value": "中国建设银行", "editable": True},
                {"name": "savePayee", "label": "保存为常用收款人", "type": "boolean", "required": False, "value": True, "editable": True},
            ],
        }

        response = RuntimeAgentRunResponse.model_validate(payload)

        self.assertEqual(response.user_response.task_progress.current, 2)
        self.assertEqual(response.clarification_form.kind, "transfer_payee")

    def test_form_never_accepts_account_number_as_server_prefill(self):
        payload = {
            "schemaVersion": "1.0", "clarificationId": "acl_001",
            "submissionToken": "x" * 43,
            "expiresAt": "2026-08-31T10:05:00.000Z",
            "kind": "transfer_payee",
            "fields": [
                {"name": "payeeName", "label": "收款人姓名", "type": "text", "required": True, "value": "李四", "editable": False},
                {"name": "accountNo", "label": "收款账号", "type": "bank_account", "required": True, "value": "6222021234567890", "editable": True},
            ],
        }

        with self.assertRaises(ValidationError):
            ClarificationForm.model_validate(payload)

    def test_frozen_user_response_serializes_as_json_arrays_in_runtime_envelope(self):
        payload = runtime_response_payload(
            status="clarification_required",
            response_type="clarification",
        )
        payload["userResponse"] = clarification_response_payload()
        response = RuntimeAgentRunResponse.model_validate(payload)

        serialized = json.loads(response.model_dump_json(by_alias=True))

        self.assertIsInstance(serialized["userResponse"]["items"], list)
        self.assertIsInstance(serialized["userResponse"]["actions"], list)
        self.assertEqual(
            serialized["userResponse"]["actions"][0]["payload"]["message"],
            "查询工资卡尾号 6217 的余额",
        )

    def test_succeeded_supports_legacy_and_new_result_migration(self):
        legacy = RuntimeAgentRunResponse.model_validate(
            runtime_response_payload(legacy_result=True)
        )
        self.assertIsNotNone(legacy.user_result)
        self.assertIsNone(legacy.user_response)

        for response_type in ("result", "answer"):
            with self.subTest(response_type=response_type):
                response = RuntimeAgentRunResponse.model_validate(
                    runtime_response_payload(response_type=response_type)
                )
                self.assertEqual(response.user_response.type, response_type)

    def test_succeeded_requires_terminal_result_and_rejects_non_result_response(self):
        with self.assertRaises(ValidationError):
            RuntimeAgentRunResponse.model_validate(runtime_response_payload())

        for response_type in ("clarification", "notice"):
            with self.subTest(response_type=response_type), self.assertRaises(ValidationError):
                RuntimeAgentRunResponse.model_validate(
                    runtime_response_payload(response_type=response_type)
                )

    def test_non_succeeded_statuses_require_their_exact_response_type(self):
        mappings = (
            ("clarification_required", "clarification"),
            ("awaiting_confirmation", "confirmation"),
            ("navigation_only", "navigation"),
            ("unsupported", "notice"),
            ("blocked", "notice"),
        )
        for status, response_type in mappings:
            with self.subTest(status=status, valid=response_type):
                response = RuntimeAgentRunResponse.model_validate(
                    runtime_response_payload(status=status, response_type=response_type)
                )
                self.assertEqual(response.user_response.type, response_type)

            conflicting_type = "result" if response_type != "result" else "notice"
            with self.subTest(status=status, invalid=conflicting_type), self.assertRaises(ValidationError):
                RuntimeAgentRunResponse.model_validate(
                    runtime_response_payload(status=status, response_type=conflicting_type)
                )

    def test_security_terminal_response_does_not_forge_agent_artifacts(self):
        for status in ("blocked", "unavailable"):
            with self.subTest(status=status):
                response = RuntimeAgentRunResponse.model_validate({
                    "agentRunId": "abr_001",
                    "conversationId": "conv_001",
                    "status": status,
                    "userResponse": user_response_payload("notice"),
                })
                self.assertIsNone(response.intent_task_envelope)
                self.assertIsNone(response.capability_plan)

        with self.assertRaises(ValidationError):
            RuntimeAgentRunResponse.model_validate({
                "agentRunId": "abr_001",
                "conversationId": "conv_001",
                "status": "planned",
                "userResponse": user_response_payload("notice"),
            })


if __name__ == "__main__":
    unittest.main()
