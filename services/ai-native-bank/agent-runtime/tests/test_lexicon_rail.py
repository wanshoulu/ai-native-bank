import tempfile
import unittest
from pathlib import Path

from app.lexicon_rail import ChainedInputRail, LexiconInputRail
from app.safety_gate import RailDecision


class FixedSemanticRail:
    def __init__(self, decision: str):
        self.decision = decision
        self.calls = 0

    async def evaluate_input(self, **_kwargs):
        self.calls += 1
        reason = None
        response = None
        if self.decision == "review":
            reason = "ambiguous_risk_intent"
        elif self.decision == "block":
            reason = "unsafe_content"
            response = "security.block.unsafe_content"
        return RailDecision(
            railId="semantic-safety",
            stage="input",
            decision=self.decision,
            reasonCode=reason,
            policyVersion="test-v1",
            responseKey=response,
            durationMs=1,
        )


class LexiconAdvisoryTests(unittest.IsolatedAsyncioTestCase):
    def make_lexicon(self, directory: str) -> LexiconInputRail:
        Path(directory, "abuse.txt").write_text("垃圾\n", encoding="utf-8")
        return LexiconInputRail(directory)

    async def test_input_lexicon_hit_is_advisory_and_semantic_allow_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            lexicon = self.make_lexicon(directory)
            semantic = FixedSemanticRail("allow")
            rail = ChainedInputRail([lexicon, semantic])

            result = await rail.evaluate_input(
                message="黄色的垃圾",
                request_id="req_lexicon_allow",
                conversation_id="conv_lexicon_allow",
                history=[],
            )

        self.assertEqual(semantic.calls, 1)
        self.assertEqual(result.decision, "allow")

    async def test_input_lexicon_hit_does_not_weaken_semantic_block(self):
        with tempfile.TemporaryDirectory() as directory:
            lexicon = self.make_lexicon(directory)
            semantic = FixedSemanticRail("block")
            rail = ChainedInputRail([lexicon, semantic])

            result = await rail.evaluate_input(
                message="明确的危险请求和垃圾词",
                request_id="req_lexicon_block",
                conversation_id="conv_lexicon_block",
                history=[],
            )

        self.assertEqual(semantic.calls, 1)
        self.assertEqual(result.decision, "block")

    async def test_output_lexicon_hit_remains_an_independent_block(self):
        with tempfile.TemporaryDirectory() as directory:
            lexicon = self.make_lexicon(directory)

            result = await lexicon.evaluate_output(
                text="垃圾",
                request_id="req_output_block",
                conversation_id="conv_output_block",
            )

        self.assertEqual(result.decision, "block")


if __name__ == "__main__":
    unittest.main()
