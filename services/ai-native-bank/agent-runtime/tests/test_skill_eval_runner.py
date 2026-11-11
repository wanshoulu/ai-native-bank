import copy
import unittest
from pathlib import Path

from app.skill_eval_runner import (
    SkillEvalContractError,
    evaluate_runtime_observation,
    load_skill_eval_suite,
    observation_from_sse_events,
    validate_skill_eval_contracts,
)


ROOT = Path("/app") if Path("/app/data/ai-native-bank").exists() else Path(__file__).resolve().parents[4]


class SkillEvalRunnerTests(unittest.TestCase):
    def test_real_suite_exposes_high_frequency_runtime_scope_without_claiming_all_cases(self) -> None:
        suite = load_skill_eval_suite(ROOT)

        self.assertEqual(suite["summary"]["skills"], 9)
        self.assertEqual(suite["summary"]["declaredCases"], 65)
        self.assertEqual(suite["summary"]["runtimeCases"], 9)
        self.assertEqual(suite["summary"]["specOnlyCases"], 56)
        self.assertEqual(suite["graph"]["version"], "2.1.0")
        self.assertEqual(
            {item["category"] for item in suite["cases"]}, {"single_task"}
        )

    def test_runtime_observation_checks_intent_capability_status_and_confirmation(self) -> None:
        case = {
            "skillId": "bank.account-inquiry",
            "evalId": "balance-single",
            "expect": {
                "intentIds": ["ccb.account.balance.query"],
                "requiredCapabilityIds": ["bank.account.balance.get"],
                "allowedStatuses": ["succeeded"],
                "requiresConfirmation": False,
                "minimumTaskCount": 1,
            },
        }
        passed = evaluate_runtime_observation(case, {
            "status": "succeeded",
            "intentIds": ["ccb.account.balance.query"],
            "capabilityIds": ["bank.account.balance.get"],
            "taskCount": 1,
            "requiresConfirmation": False,
            "userResponseType": "result",
        })
        self.assertTrue(passed["passed"])
        self.assertEqual(passed["failures"], [])

        failed = evaluate_runtime_observation(case, {
            "status": "succeeded",
            "intentIds": ["ccb.account.balance.query"],
            "capabilityIds": ["bank.credit-card.bill.get"],
            "taskCount": 1,
            "requiresConfirmation": False,
            "userResponseType": "result",
        })
        self.assertFalse(failed["passed"])
        self.assertEqual(
            {item["code"] for item in failed["failures"]},
            {"required_capability_missing"},
        )

    def test_runtime_observation_contract_rejects_unknown_or_sensitive_fields(self) -> None:
        case = {
            "skillId": "bank.transfer-remittance",
            "evalId": "transfer-single",
            "expect": {
                "intentIds": ["ccb.transfer.create"],
                "requiredCapabilityIds": [],
                "allowedStatuses": ["clarification_required", "awaiting_confirmation"],
                "requiresConfirmation": None,
                "minimumTaskCount": 1,
            },
        }
        with self.assertRaisesRegex(SkillEvalContractError, "observation fields"):
            evaluate_runtime_observation(case, {
                "status": "awaiting_confirmation",
                "intentIds": ["ccb.transfer.create"],
                "capabilityIds": [],
                "taskCount": 1,
                "requiresConfirmation": True,
                "userResponseType": "confirmation",
                "confirmationToken": "secret",
            })

    def test_contracts_are_bound_to_graph_and_skill_digests(self) -> None:
        suite = load_skill_eval_suite(ROOT)
        stale = copy.deepcopy(suite["source"])
        stale["graph"]["digest"] = "sha256:" + "0" * 64

        with self.assertRaisesRegex(SkillEvalContractError, "graph digest"):
            validate_skill_eval_contracts(
                stale,
                graph=suite["authoritativeGraph"],
                skill_registry=suite["authoritativeSkills"],
                declared_evals=suite["declaredEvals"],
            )

    def test_safe_sse_projection_builds_a_machine_runtime_observation(self) -> None:
        events = [
            {
                "event": "trace",
                "data": {
                    "stage": "run.completed",
                    "summary": {
                        "result": {
                            "status": "succeeded",
                            "tasks": [{"intentId": "ccb.account.balance.query"}],
                            "steps": [{"capabilityId": "bank.account.balance.get"}],
                            "requiresConfirmation": False,
                        }
                    },
                },
            },
            {
                "event": "agent.result",
                "data": {
                    "status": "succeeded",
                    "userResponse": {"type": "result"},
                    "confirmation": None,
                },
            },
        ]

        self.assertEqual(observation_from_sse_events(events), {
            "status": "succeeded",
            "intentIds": ["ccb.account.balance.query"],
            "capabilityIds": ["bank.account.balance.get"],
            "taskCount": 1,
            "requiresConfirmation": False,
            "userResponseType": "result",
        })

        hostile = copy.deepcopy(events)
        hostile[0]["data"]["summary"]["result"]["tasks"][0]["accountId"] = "acct_private"
        with self.assertRaisesRegex(SkillEvalContractError, "task projection"):
            observation_from_sse_events(hostile)

        blocked = [
            {
                "event": "agent.trace",
                "data": {
                    "stage": "run.completed",
                    "summary": {"result": {
                        "kind": "agent-run",
                        "id": "abr_safe",
                        "conversationId": "conv_safe",
                        "status": "blocked",
                        "errorCode": "unsafe_content",
                        "failedStage": "input_rail",
                    }},
                },
            },
            {
                "event": "agent.result",
                "data": {
                    "status": "blocked",
                    "userResponse": {"type": "notice"},
                    "confirmation": None,
                },
            },
        ]
        self.assertEqual(observation_from_sse_events(blocked), {
            "status": "blocked",
            "intentIds": [],
            "capabilityIds": [],
            "taskCount": 0,
            "requiresConfirmation": False,
            "userResponseType": "notice",
        })

        failed = [
            {
                "event": "agent.trace",
                "data": {
                    "stage": "run.failed",
                    "status": "failed",
                    "error": {"code": "business_agent_contract_violation"},
                    "summary": {"title": "运行失败"},
                },
            },
            {
                "event": "agent.result",
                "data": {
                    "status": "blocked",
                    "userResponse": {"type": "notice"},
                    "confirmation": None,
                },
            },
        ]
        self.assertEqual(observation_from_sse_events(failed)["status"], "blocked")

    def test_safe_sse_projection_accepts_confirmation_terminal_trace(self) -> None:
        events = [
            {
                "event": "agent.trace",
                "data": {
                    "stage": "capability.invocation_completed",
                    "status": "completed",
                    "summary": {
                        "metrics": {
                            "capabilityId": "bank.obligation.list",
                            "outcome": "succeeded",
                        }
                    },
                },
            },
            {
                "event": "agent.trace",
                "data": {
                    "stage": "run.awaiting_confirmation",
                    "status": "awaiting_confirmation",
                    "summary": {
                        "result": {
                            "status": "awaiting_confirmation",
                            "tasks": [{"intentId": "ccb.obligation.repay-all"}],
                            "steps": [
                                {
                                    "capabilityId": "bank.credit-card.repayment.create",
                                    "requiresConfirmation": True,
                                }
                            ],
                            "requiresConfirmation": True,
                        }
                    },
                },
            },
            {
                "event": "agent.result",
                "data": {
                    "status": "awaiting_confirmation",
                    "userResponse": {"type": "confirmation"},
                    "confirmation": {"checkpointId": "redacted"},
                },
            },
        ]

        self.assertEqual(observation_from_sse_events(events), {
            "status": "awaiting_confirmation",
            "intentIds": ["ccb.obligation.repay-all"],
            "capabilityIds": [
                "bank.obligation.list",
                "bank.credit-card.repayment.create",
            ],
            "taskCount": 1,
            "requiresConfirmation": True,
            "userResponseType": "confirmation",
        })


if __name__ == "__main__":
    unittest.main()
