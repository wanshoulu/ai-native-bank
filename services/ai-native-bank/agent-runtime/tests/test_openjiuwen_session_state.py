import json
import unittest

from app.openjiuwen_session_state import project_safe_session_state


class OpenJiuwenSessionStateTests(unittest.TestCase):
    def test_projection_keeps_only_structural_workflow_facts(self):
        payload = {
            "agent_run_id": "abr_001",
            "graph_context": {"version": "2.0.0", "digest": "sha256:" + "a" * 64},
            "intent_task_envelope": {
                "delegation_id": "dlg_001",
                "revision": 3,
                "intent_shape": "same_intent_multi_task",
                "tasks": [
                    {
                        "task_id": "task_one",
                        "intent_id": "ccb.transfer.create",
                        "task_status": "running",
                        "slots": {"payee": {"value": "张三"}},
                    },
                    {
                        "task_id": "task_two",
                        "intent_id": "ccb.transfer.create",
                        "task_status": "pending",
                        "slots": {"payee": {"value": "李四"}},
                    },
                ],
            },
            "execution_view": {
                "digest": "sha256:" + "b" * 64,
                "skillIds": ["bank.transfer-remittance"],
                "capabilityIds": ["bank.account.list"],
                "providerIds": ["provider.mock.account-list"],
            },
            "slot_assessment": {"recognized": ["payee"], "value": "张三"},
        }

        state = project_safe_session_state(payload)
        serialized = json.dumps(state, ensure_ascii=False)

        self.assertEqual(state["envelopeRevision"], 3)
        self.assertEqual(state["tasks"][0]["taskId"], "task_one")
        self.assertEqual(state["executionView"]["skillIds"], ["bank.transfer-remittance"])
        self.assertNotIn("张三", serialized)
        self.assertNotIn("李四", serialized)
        self.assertNotIn("slots", serialized)


if __name__ == "__main__":
    unittest.main()
