import unittest

from adapters.hardened_runtime import evaluate_action_rail, evaluate_output_rail


class FakeClassification:
    def __init__(self, blocked=False):
        self.is_blocked = blocked
        self.max_category = "fraud" if blocked else "safe"
        self.category_scores = {self.max_category: 0.99}


class FakeClassifier:
    def __init__(self, blocked=False, error=None):
        self.blocked = blocked
        self.error = error
        self.calls = []

    async def classify(self, text, is_output=False):
        self.calls.append((text, is_output))
        if self.error:
            raise self.error
        return FakeClassification(self.blocked)


class FakePII:
    def __init__(self, should_block=False):
        self.should_block = should_block

    def check_output(self, _text):
        return {"should_block": self.should_block, "has_pii": self.should_block}


class FakeAgent:
    def __init__(self, *, pii=False, content=False, error=None):
        self.pii_service = FakePII(pii)
        self.content_filter_service = FakeClassifier(content, error)


class OutputRailTests(unittest.IsolatedAsyncioTestCase):
    async def test_deterministic_sensitive_output_blocks_before_classifier(self):
        agent = FakeAgent()
        result = await evaluate_output_rail(
            agent,
            text="客户卡号 4111 1111 1111 1111",
            request_id="req_output_1",
            conversation_id="conv_output_1",
        )
        self.assertEqual(result["stage"], "output")
        self.assertEqual(result["decision"], "block")
        self.assertEqual(result["reasonCode"], "sensitive_output")
        self.assertEqual(agent.content_filter_service.calls, [])

    async def test_upstream_output_pii_and_content_are_enforced(self):
        pii = await evaluate_output_rail(
            FakeAgent(pii=True), text="synthetic", request_id="req_pii", conversation_id="conv"
        )
        content = await evaluate_output_rail(
            FakeAgent(content=True), text="synthetic", request_id="req_content", conversation_id="conv"
        )
        self.assertEqual((pii["decision"], pii["reasonCode"]), ("block", "sensitive_output"))
        self.assertEqual((content["decision"], content["reasonCode"]), ("block", "unsafe_output"))

    async def test_output_dependency_failure_is_unavailable(self):
        result = await evaluate_output_rail(
            FakeAgent(error=RuntimeError("private stack")),
            text="synthetic",
            request_id="req_error",
            conversation_id="conv",
        )
        self.assertEqual(result["decision"], "unavailable")
        self.assertEqual(result["reasonCode"], "security_detector_unavailable")
        self.assertNotIn("private stack", str(result))


class ActionRailTests(unittest.IsolatedAsyncioTestCase):
    async def test_evasion_action_blocks(self):
        result = await evaluate_action_rail(
            capability_id="bank.transfer.order.create",
            risk_level="L3",
            summary="拆分转账来绕过限额和审核",
            requires_confirmation=True,
            confirmed=True,
            input_decision="allow",
        )
        self.assertEqual(result["stage"], "action")
        self.assertEqual(result["decision"], "block")
        self.assertEqual(result["reasonCode"], "financial_control_evasion")

    async def test_unconfirmed_side_effect_reviews(self):
        result = await evaluate_action_rail(
            capability_id="bank.transfer.order.create",
            risk_level="L3",
            summary="向已选择的收款人转账",
            requires_confirmation=True,
            confirmed=False,
            input_decision="allow",
        )
        self.assertEqual(result["decision"], "review")
        self.assertEqual(result["reasonCode"], "action_confirmation_required")


if __name__ == "__main__":
    unittest.main()
