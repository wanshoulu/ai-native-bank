import asyncio
import json
import unittest

import httpx

from app.business_loop import TodoSnapshotItem
from app.node_gateway import (
    DEFAULT_READ_CALL_BUDGET,
    AgentCapabilityInvocationError,
    AgentCapabilityInvoker,
)
from app.safety_gate import ExecutionPermit, RailDecision
from app.todo_ledger import TodoLedger


def todo_digest(items):
    return TodoLedger.digest(tuple(
        TodoSnapshotItem(
            id=item["id"],
            status=item["status"],
            depends_on=item["dependsOn"],
        )
        for item in items
    ))


class AgentCapabilityInvokerTests(unittest.IsolatedAsyncioTestCase):
    def _execution_permit(self):
        return ExecutionPermit.issue(
            request_id="req_permit",
            conversation_id="conv_permit",
            session_revision=4,
            rail_decision=RailDecision(
                railId="finance-text-safety",
                decision="allow",
                policyVersion="test-v1",
                durationMs=3,
            ),
            ttl_seconds=30,
        )

    async def test_a2a_delegation_permit_is_bound_and_single_use(self):
        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _request: httpx.Response(500))
            ),
        )
        self.addAsyncCleanup(invoker.close)
        permit = self._execution_permit()

        invoker.consume_delegation_permit(
            permit,
            request_id="req_permit",
            conversation_id="conv_permit",
            session_revision=4,
        )

        with self.assertRaisesRegex(
            AgentCapabilityInvocationError, "execution_permit_replayed"
        ):
            invoker.consume_delegation_permit(
                permit,
                request_id="req_permit",
                conversation_id="conv_permit",
                session_revision=4,
            )

    async def test_a2a_delegation_permit_rejects_wrong_binding_before_io(self):
        requests = []
        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: requests.append(request) or httpx.Response(200)
                )
            ),
        )
        self.addAsyncCleanup(invoker.close)

        with self.assertRaisesRegex(
            AgentCapabilityInvocationError, "execution_permit_invalid"
        ):
            invoker.consume_delegation_permit(
                self._execution_permit(),
                request_id="req_other",
                conversation_id="conv_permit",
                session_revision=4,
            )

        self.assertEqual(requests, [])

    async def test_context_is_required_and_capability_must_be_allowlisted(self):
        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/api/ai-bank/v1/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500))),
        )
        self.addAsyncCleanup(invoker.close)

        with self.assertRaisesRegex(AgentCapabilityInvocationError, "context_not_active"):
            await invoker.invoke_read_capability("bank.account.list", {})
        with invoker.activate("abr_one", {"bank.account.list"}):
            with self.assertRaisesRegex(AgentCapabilityInvocationError, "capability_not_allowed"):
                await invoker.invoke_read_capability("bank.credit-card.list", {})

    async def test_request_contains_only_service_identity_run_capability_and_input(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"data": {"accounts": []}, "meta": {"requestId": "req_test", "traceId": "trace_test"}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/api/ai-bank/v1/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        with invoker.activate("abr_one", {"bank.account.list"}):
            result = await invoker.invoke_read_capability("bank.account.list", {})

        request = captured[0]
        self.assertEqual(request.headers["authorization"], "Bearer runtime-service-secret")
        self.assertEqual(json.loads(request.content), {
            "agentRunId": "abr_one",
            "capabilityId": "bank.account.list",
            "input": {},
        })
        self.assertEqual(result, {"accounts": []})
        self.assertNotIn("customer", request.content.decode("utf-8").lower())

    async def test_governed_memory_defaults_fill_missing_read_inputs_but_never_override_current_input(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"ok": True}, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        with invoker.activate(
            "abr_wealth", {"bank.wealth.advisory.context.get"},
            input_defaults={
                "bank.wealth.advisory.context.get": {
                    "investmentGoal": "steady_growth", "horizonMonths": 12,
                }
            },
        ):
            await invoker.invoke_read_capability(
                "bank.wealth.advisory.context.get",
                {"investmentGoal": "capital_preservation"},
            )

        self.assertEqual(captured[0]["input"], {
            "investmentGoal": "capital_preservation",
            "horizonMonths": 12,
        })

    async def test_concurrent_runs_keep_independent_context_allowlists(self):
        seen = []

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            seen.append((payload["agentRunId"], payload["capabilityId"]))
            await asyncio.sleep(0)
            return httpx.Response(200, json={"data": {"ok": True}, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)

        async def invoke(run_id: str, capability_id: str):
            with invoker.activate(run_id, {capability_id}):
                return await invoker.invoke_read_capability(capability_id, {})

        await asyncio.gather(
            invoke("abr_accounts", "bank.account.list"),
            invoke("abr_cards", "bank.credit-card.list"),
        )
        self.assertCountEqual(seen, [
            ("abr_accounts", "bank.account.list"),
            ("abr_cards", "bank.credit-card.list"),
        ])

    async def test_read_budget_allows_bounded_retries_per_context(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": {"accounts": []}, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)

        # 读能力默认预算 = DEFAULT_READ_CALL_BUDGET（1 次首发 + 纠错重试）；
        # 超过预算后才拒绝，防止失败读把能力锁死导致循环撞预算。
        with invoker.activate("abr_budget", {"bank.account.list"}):
            for _ in range(DEFAULT_READ_CALL_BUDGET):
                await invoker.invoke_read_capability("bank.account.list", {})
            with self.assertRaisesRegex(
                AgentCapabilityInvocationError, "capability_call_budget_exceeded"
            ):
                await invoker.invoke_read_capability("bank.account.list", {})

        self.assertEqual(len(requests), DEFAULT_READ_CALL_BUDGET)

    async def test_explicit_zero_budget_rejects_before_http(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": {}, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)

        with invoker.activate(
            "abr_zero",
            {"bank.account.list"},
            call_budgets={"bank.account.list": 0},
        ):
            with self.assertRaisesRegex(
                AgentCapabilityInvocationError, "capability_call_budget_exceeded"
            ):
                await invoker.invoke_read_capability("bank.account.list", {})

        self.assertEqual(requests, [])

    async def test_budget_cannot_raise_a_capability_above_default(self):
        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/internal",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _request: httpx.Response(500))
            ),
        )
        self.addAsyncCleanup(invoker.close)

        with self.assertRaisesRegex(
            AgentCapabilityInvocationError, "capability_call_budget_invalid"
        ):
            with invoker.activate(
                "abr_raised_budget",
                {"bank.account.list"},
                call_budgets={
                    "bank.account.list": DEFAULT_READ_CALL_BUDGET + 1
                },
            ):
                pass

    async def test_persist_agent_workflow_sends_only_safe_todo_projection(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={
                "data": {
                    "businessWorkflowId": "bwf_001",
                    "status": "awaiting_clarification",
                    "workflowRevision": 2,
                    "todoRevision": 2,
                    "activeGoalTaskId": "task_zhang",
                },
                "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/api/ai-bank/v1/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)

        projected_todos = [
            {"id": "task_zhang", "status": "in_progress", "dependsOn": []},
            {"id": "task_li", "status": "pending", "dependsOn": []},
        ]
        result = await invoker.persist_agent_workflow(
            agent_run_id="abr_one",
            business_workflow_id="bwf_001",
            status="awaiting_clarification",
            delegated_task_ids=["task_zhang", "task_li"],
            expected_revision=1,
            envelope_revision=2,
            workflow_revision=2,
            todo_revision=2,
            todo_snapshot=projected_todos,
            todo_digest=todo_digest(projected_todos),
            active_goal_task_id="task_zhang",
            active_todo_id="task_zhang:collect",
            suspension_reason="awaiting_clarification",
            last_outcome={"status": "awaiting_clarification"},
            pending_checkpoint_ids=[],
        )

        self.assertEqual(
            str(captured[0].url),
            "http://node.test/api/ai-bank/v1/internal/agent-workflows",
        )
        self.assertEqual(json.loads(captured[0].content), {
            "agentRunId": "abr_one",
            "businessWorkflowId": "bwf_001",
            "status": "awaiting_clarification",
            "delegatedTaskIds": ["task_zhang", "task_li"],
            "expectedRevision": 1,
            "envelopeRevision": 2,
            "workflowRevision": 2,
            "todoRevision": 2,
            "todoSnapshot": projected_todos,
            "todoDigest": todo_digest(projected_todos),
            "activeGoalTaskId": "task_zhang",
            "activeTodoId": "task_zhang:collect",
            "executionPolicy": "sequential",
            "suspensionReason": "awaiting_clarification",
            "lastOutcome": {"status": "awaiting_clarification"},
            "pendingCheckpointIds": [],
        })
        self.assertEqual(result["workflowRevision"], 2)
        self.assertEqual(result["todoRevision"], 2)

    async def test_single_goal_workflow_accepts_openjiuwen_native_todo_ids(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(201, json={
                "data": {
                    "businessWorkflowId": "bwf_native_single",
                    "status": "awaiting_clarification",
                    "workflowRevision": 1,
                    "todoRevision": 3,
                    "activeGoalTaskId": "transfer_task",
                },
                "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/api/ai-bank/v1/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)

        projected_todos = [
            {"id": "resolve_from_account", "status": "in_progress", "dependsOn": []},
            {"id": "validate_payee", "status": "pending", "dependsOn": ["resolve_from_account"]},
        ]
        await invoker.persist_agent_workflow(
            agent_run_id="abr_native_single",
            business_workflow_id="bwf_native_single",
            status="awaiting_clarification",
            delegated_task_ids=["transfer_task"],
            expected_revision=0,
            envelope_revision=1,
            workflow_revision=1,
            todo_revision=3,
            todo_snapshot=projected_todos,
            todo_digest=todo_digest(projected_todos),
            active_goal_task_id="transfer_task",
            active_todo_id="resolve_from_account",
            suspension_reason="awaiting_clarification",
            last_outcome={"status": "awaiting_clarification"},
            pending_checkpoint_ids=[],
        )

        self.assertEqual(captured[0]["activeTodoId"], "resolve_from_account")

    async def test_workflow_revision_conflict_has_a_stable_error_code(self):
        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/api/ai-bank/v1/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _request: httpx.Response(409, json={"error": {"code": "agent_workflow_revision_conflict"}})
            )),
        )
        self.addAsyncCleanup(invoker.close)

        with self.assertRaisesRegex(
            AgentCapabilityInvocationError, "agent_workflow_revision_conflict"
        ):
            projected_todos = [
                {"id": "task_zhang", "status": "in_progress", "dependsOn": []}
            ]
            await invoker.persist_agent_workflow(
                agent_run_id="abr_one", business_workflow_id="bwf_001",
                status="awaiting_clarification", delegated_task_ids=["task_zhang"],
                expected_revision=1, envelope_revision=1, workflow_revision=2,
                todo_revision=1,
                todo_snapshot=projected_todos,
                todo_digest=todo_digest(projected_todos), active_goal_task_id="task_zhang",
                active_todo_id="task_zhang", suspension_reason="awaiting_clarification",
                last_outcome={"status": "awaiting_clarification"}, pending_checkpoint_ids=[],
            )

    async def test_load_agent_workflow_returns_only_safe_restore_fields(self):
        captured = []
        response_data = {
            "businessWorkflowId": "bwf_001",
            "status": "awaiting_clarification",
            "delegatedTaskIds": ["task_zhang", "task_li"],
            "envelopeRevision": 2,
            "workflowRevision": 3,
            "todoRevision": 2,
            "todoSnapshot": [
                {"id": "task_zhang", "status": "in_progress", "dependsOn": []},
                {"id": "task_li", "status": "pending", "dependsOn": []},
            ],
            "todoDigest": TodoLedger.digest((
                TodoSnapshotItem(
                    id="task_zhang", status="in_progress", depends_on=[]
                ),
                TodoSnapshotItem(
                    id="task_li", status="pending", depends_on=[]
                ),
            )),
            "activeGoalTaskId": "task_zhang",
            "activeTodoId": "task_zhang:collect",
            "executionPolicy": "sequential",
            "suspensionReason": "awaiting_clarification",
            "lastOutcome": {"status": "awaiting_clarification"},
            "pendingCheckpointIds": [],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"data": response_data, "meta": {}})

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/api/ai-bank/v1/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)

        result = await invoker.load_agent_workflow(
            agent_run_id="abr_one", business_workflow_id="bwf_001",
        )

        self.assertEqual(result, response_data)
        self.assertEqual(captured[0].method, "GET")
        self.assertEqual(
            str(captured[0].url),
            "http://node.test/api/ai-bank/v1/internal/agent-workflows/bwf_001?agentRunId=abr_one",
        )

    async def test_load_agent_workflow_rejects_unsafe_todo_graphs(self):
        base = {
            "businessWorkflowId": "bwf_001",
            "status": "awaiting_clarification",
            "delegatedTaskIds": ["task_one", "task_two"],
            "envelopeRevision": 2,
            "workflowRevision": 3,
            "todoRevision": 2,
            "todoSnapshot": [
                {"id": "task_one", "status": "in_progress", "dependsOn": []},
                {"id": "task_two", "status": "pending", "dependsOn": ["task_one"]},
            ],
            "todoDigest": "0" * 64,
            "activeGoalTaskId": "task_one",
            "activeTodoId": "task_one",
            "executionPolicy": "sequential",
            "suspensionReason": "awaiting_clarification",
            "lastOutcome": {"status": "awaiting_clarification"},
            "pendingCheckpointIds": [],
        }
        invalid_snapshots = (
            [{"id": "task_one", "status": "in_progress", "dependsOn": [{"token": "sk-secret"}]}],
            [{"id": "task_one", "status": "in_progress", "dependsOn": [], "secret": "hidden"}],
            [{"id": "task_one", "status": "in_progress", "dependsOn": []}] * 2,
            [{"id": "task_one", "status": "in_progress", "dependsOn": ["missing"]}],
            [
                {"id": "task_one", "status": "in_progress", "dependsOn": []},
                {"id": "task_two", "status": "pending", "dependsOn": ["task_one", "task_one"]},
            ],
            [{"id": "task_one", "status": "in_progress", "dependsOn": ["x" * 161]}],
            [
                *[
                    {"id": f"dependency_{index:02d}", "status": "pending", "dependsOn": []}
                    for index in range(33)
                ],
                {
                    "id": "task_one",
                    "status": "in_progress",
                    "dependsOn": [f"dependency_{index:02d}" for index in range(33)],
                },
            ],
            [
                {"id": "task_one", "status": "in_progress", "dependsOn": ["task_two"]},
                {"id": "task_two", "status": "pending", "dependsOn": ["task_one"]},
            ],
            [
                {"id": f"task_{index:03d}", "status": "pending", "dependsOn": []}
                for index in range(129)
            ],
        )

        for snapshot in invalid_snapshots:
            with self.subTest(size=len(snapshot)):
                response_data = dict(base, todoSnapshot=snapshot)
                invoker = AgentCapabilityInvoker(
                    endpoint="http://node.test/api/ai-bank/v1/internal/agent-capability-invocations",
                    service_token="runtime-service-secret",
                    client=httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda _request, data=response_data: httpx.Response(
                            200, json={"data": data, "meta": {}}
                        )
                    )),
                )
                self.addAsyncCleanup(invoker.close)
                with self.assertRaisesRegex(
                    AgentCapabilityInvocationError,
                    "^node_gateway_response_invalid$",
                ):
                    await invoker.load_agent_workflow(
                        agent_run_id="abr_one",
                        business_workflow_id="bwf_001",
                    )

    async def test_load_agent_workflow_accepts_64_65_and_128_todos(self):
        for count in (64, 65, 128):
            with self.subTest(count=count):
                todos = [
                    {
                        "id": f"task_{index:03d}",
                        "status": "in_progress" if index == 0 else "pending",
                        "dependsOn": [],
                    }
                    for index in range(count)
                ]
                response_data = {
                    "businessWorkflowId": "bwf_001",
                    "status": "awaiting_clarification",
                    "delegatedTaskIds": ["goal_one"],
                    "envelopeRevision": 2,
                    "workflowRevision": 3,
                    "todoRevision": 2,
                    "todoSnapshot": todos,
                    "todoDigest": todo_digest(todos),
                    "activeGoalTaskId": "goal_one",
                    "activeTodoId": "task_000",
                    "executionPolicy": "sequential",
                    "suspensionReason": "awaiting_clarification",
                    "lastOutcome": {"status": "awaiting_clarification"},
                    "pendingCheckpointIds": [],
                }
                invoker = AgentCapabilityInvoker(
                    endpoint="http://node.test/api/ai-bank/v1/internal/agent-capability-invocations",
                    service_token="runtime-service-secret",
                    client=httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda _request, data=response_data: httpx.Response(
                            200, json={"data": data, "meta": {}}
                        )
                    )),
                )
                self.addAsyncCleanup(invoker.close)

                result = await invoker.load_agent_workflow(
                    agent_run_id="abr_one",
                    business_workflow_id="bwf_001",
                )

                self.assertEqual(len(result["todoSnapshot"]), count)

    async def test_authorized_tool_uses_context_bound_grant_without_model_input(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={
                "data": {
                    "grantId": "acg_one", "taskId": "task_loss",
                    "capabilityId": "bank.credit-card.loss-report.create",
                    "status": "SUCCEEDED", "result": {"status": "LOST"},
                },
                "meta": {},
            })

        invoker = AgentCapabilityInvoker(
            endpoint="http://node.test/api/ai-bank/v1/internal/agent-capability-invocations",
            service_token="runtime-service-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(invoker.close)
        grants = [{
            "grantId": "acg_one", "taskId": "task_loss",
            "capabilityId": "bank.credit-card.loss-report.create",
            "expiresAt": "2026-08-31T10:05:00.000Z",
        }]

        with invoker.activate_authorized(
            "abr_one", "bwf_one", grants,
        ):
            receipt = await invoker.invoke_authorized_capability(
                "acg_one", "task_loss",
            )
            with self.assertRaisesRegex(
                AgentCapabilityInvocationError, "capability_grant_not_allowed"
            ):
                await invoker.invoke_authorized_capability("acg_other", "task_loss")

        self.assertEqual(receipt["status"], "SUCCEEDED")
        self.assertEqual(
            str(captured[0].url),
            "http://node.test/api/ai-bank/v1/internal/agent-authorized-capability-invocations",
        )
        self.assertEqual(json.loads(captured[0].content), {
            "agentRunId": "abr_one",
            "businessWorkflowId": "bwf_one",
            "taskId": "task_loss",
            "grantId": "acg_one",
        })


if __name__ == "__main__":
    unittest.main()
