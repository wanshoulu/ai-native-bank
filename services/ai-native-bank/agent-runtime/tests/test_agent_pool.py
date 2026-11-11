import tempfile
import unittest
from pathlib import Path

from app.agent_pool import BusinessAgentPool
from app.resource_registry import BusinessResourceRegistry, ResourceRegistryError
from app.todo_ledger import TodoLedger


PROJECT_ROOT = Path("/app") if Path("/app/data/ai-native-bank").exists() else Path(__file__).resolve().parents[4]
REGISTRY_ROOT = PROJECT_ROOT / "data" / "ai-native-bank" / "registries"
SKILL_ROOT = PROJECT_ROOT / "data" / "ai-native-bank" / "skills"


class FakeRunner:
    def __init__(self, view):
        self.view = view
        self.calls = []

    async def run(self, payload, *, session_id):
        self.calls.append((payload, session_id))
        return {"status": "planned"}


class LedgerRunner(FakeRunner):
    def __init__(self, view, root):
        super().__init__(view)
        self.ledger = TodoLedger(root)

    async def run(self, payload, *, session_id):
        self.calls.append((payload, session_id))
        self.ledger.restore(
            session_id,
            2,
            [{"id": "task_001", "status": "in_progress", "dependsOn": []}],
        )
        return {"status": "awaiting_clarification", "todo_revision": 2}

    def load_todo_ledger(self, session_id, revision=1):
        return self.ledger.load(session_id, revision)

    def restore_todo_ledger(self, session_id, revision, items):
        return self.ledger.restore(session_id, revision, items)


class BusinessAgentPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_bound_session_reuses_execution_view_when_loop_payload_omits_envelope(self):
        with tempfile.TemporaryDirectory() as workspace:
            created = []

            def create_runner(view):
                runner = FakeRunner(view)
                created.append(runner)
                return runner

            pool = BusinessAgentPool(
                registry=BusinessResourceRegistry.load(REGISTRY_ROOT),
                skill_root=SKILL_ROOT,
                workspace_root=Path(workspace),
                runner_factory=create_runner,
            )
            session_id = "business:loop-continuation"
            await pool.run(
                {
                    "intent_task_envelope": {
                        "tasks": [{"intent_id": "ccb.account.balance.query"}]
                    }
                },
                session_id=session_id,
            )
            await pool.run(
                {
                    "business_workflow_id": "bwf_1234567890abcdef12345678",
                    "loop_iteration": 2,
                    "loop_feedback": {"active_goal_task_id": "task_001"},
                    "workflow_control": {"expected_todo_revision": 1},
                },
                session_id=session_id,
            )

        self.assertEqual(len(created), 1)
        self.assertEqual(len(created[0].calls), 2)
        first_payload = created[0].calls[0][0]
        second_payload = created[0].calls[1][0]
        self.assertEqual(
            second_payload["execution_view"]["digest"],
            first_payload["execution_view"]["digest"],
        )

    async def test_bound_session_rejects_a_changed_execution_view(self):
        with tempfile.TemporaryDirectory() as workspace:
            pool = BusinessAgentPool(
                registry=BusinessResourceRegistry.load(REGISTRY_ROOT),
                skill_root=SKILL_ROOT,
                workspace_root=Path(workspace),
                runner_factory=FakeRunner,
            )
            session_id = "business:immutable-view"
            await pool.run(
                {
                    "intent_task_envelope": {
                        "tasks": [{"intent_id": "ccb.account.balance.query"}]
                    }
                },
                session_id=session_id,
            )

            with self.assertRaises(ResourceRegistryError) as raised:
                await pool.run(
                    {
                        "intent_task_envelope": {
                            "tasks": [{"intent_id": "ccb.transfer.create"}]
                        }
                    },
                    session_id=session_id,
                )
            self.assertEqual(raised.exception.code, "session_view_conflict")

    async def test_pool_reads_the_bound_openjiuwen_runner_ledger(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as runner_workspace:
            pool = BusinessAgentPool(
                registry=BusinessResourceRegistry.load(REGISTRY_ROOT),
                skill_root=SKILL_ROOT,
                workspace_root=Path(workspace),
                runner_factory=lambda view: LedgerRunner(view, runner_workspace),
            )
            await pool.run(
                {
                    "intent_task_envelope": {
                        "tasks": [{"intent_id": "ccb.account.balance.query"}]
                    }
                },
                session_id="business:one",
            )

            snapshot = pool.load_todo_ledger("business:one", revision=2)

        self.assertEqual(snapshot.revision, 2)
        self.assertEqual([item.id for item in snapshot.items], ["task_001"])

    async def test_pool_builds_and_caches_a_least_privilege_agent_view(self):
        created = []
        runners = []

        def create_runner(view):
            created.append(view)
            runner = FakeRunner(view)
            runners.append(runner)
            return runner

        with tempfile.TemporaryDirectory() as workspace:
            pool = BusinessAgentPool(
                registry=BusinessResourceRegistry.load(REGISTRY_ROOT),
                skill_root=SKILL_ROOT,
                workspace_root=Path(workspace),
                runner_factory=create_runner,
            )
            payload = {
                "intent_task_envelope": {
                    "tasks": [{"intent_id": "ccb.account.balance.query"}]
                }
            }
            first = await pool.run(payload, session_id="business:one")
            second = await pool.run(payload, session_id="business:two")

        self.assertEqual(first, {"status": "planned"})
        self.assertEqual(second, {"status": "planned"})
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].skill_ids, ("bank.account-inquiry",))
        self.assertEqual(created[0].skill_paths, (str(SKILL_ROOT / "account-inquiry"),))
        self.assertEqual(
            created[0].capability_ids,
            ("bank.account.list", "bank.account.balance.get", "bank.account.detail.get"),
        )
        self.assertTrue(all(item.provider_type == "mcp" for item in created[0].providers))
        delivered_view = runners[0].calls[0][0]["execution_view"]
        self.assertEqual(delivered_view["userActionProviders"], [])
        self.assertEqual(
            [item["capabilityId"] for item in delivered_view["mcpProviders"]],
            ["bank.account.list", "bank.account.balance.get", "bank.account.detail.get"],
        )
        self.assertTrue(all(item["serverId"] == "ccb-mock-core" for item in delivered_view["mcpProviders"]))
        self.assertNotIn("requiredScopes", delivered_view["mcpProviders"][0])

    async def test_pool_rejects_direct_read_intent_without_skill_or_provider_exposure(self):
        captured = []
        with tempfile.TemporaryDirectory() as workspace:
            pool = BusinessAgentPool(
                registry=BusinessResourceRegistry.load(REGISTRY_ROOT),
                skill_root=SKILL_ROOT,
                workspace_root=Path(workspace),
                runner_factory=lambda view: captured.append(view) or FakeRunner(view),
            )
            with self.assertRaisesRegex(
                ResourceRegistryError,
                "No executable delegated intent has an active skill",
            ):
                # ccb.credit.bill.faq 无任何 Skill 受理（知识意图不走业务池），
                # 保持"未受理意图不得委派"的守卫语义。
                await pool.run(
                    {"intent_task_envelope": {"tasks": [{"intent_id": "ccb.credit.bill.faq"}]}},
                    session_id="business-payees",
                )

        self.assertEqual(captured, [])


if __name__ == "__main__":
    unittest.main()
