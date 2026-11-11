import asyncio
import json
import logging
import sys
import unittest
from types import SimpleNamespace

from adapters.hardened_runtime import (
    ANSWER_MODEL_UNAVAILABLE_SENTINEL,
    ANSWER_SAFETY_INSTRUCTION,
    EMPTY_ANSWER_SENTINEL,
    FOUR_STATE_SECURITY_PROMPT,
    HARDENED_PII_ENTITIES,
    INVALID_ANSWER_CONTRACT_SENTINEL,
    ClassificationEvidence,
    HardenedClassifierProxy,
    SensitiveLogFilter,
    arbitrate_input_evidence,
    begin_guardrail_request,
    current_input_evidence,
    disable_third_party_telemetry,
    end_guardrail_request,
    install_hardening,
    install_request_log_redaction,
    install_registration_hook,
    project_http_payload,
    sanitize_stream_chunk,
)


class FakeClassifier:
    def __init__(
        self,
        error=None,
        *,
        loaded=True,
        score=0.0,
        threshold=0.8,
        empty=False,
    ):
        self.error = error
        self._anchors_loaded = loaded
        self._thresholds = {"fraud_or_laundering": threshold}
        self.score = score
        self.empty = empty
        self.calls = 0

    async def classify(self, text, is_output=False):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(
            is_blocked=self.score >= self._thresholds["fraud_or_laundering"],
            triggered_categories=(
                ["fraud_or_laundering"]
                if self.score >= self._thresholds["fraud_or_laundering"]
                else []
            ),
            category_scores=(
                {} if self.empty else {"fraud_or_laundering": self.score}
            ),
            max_score=self.score,
            max_category="fraud_or_laundering",
        )


class FakeLLM:
    def __init__(
        self,
        content=(
            '{"decision":"allow","category":"safe","confidence":0.95,'
            '"semantic_role":"unrelated_or_benign"}'
        ),
        error=None,
    ):
        self.content = content
        self.error = error
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.content)


class FakeChain:
    def __init__(self, value="answer", error=None, values=None):
        self.value = value
        self.values = list(values or [])
        self.error = error
        self.payload = None
        self.payloads = []

    async def ainvoke(self, payload, config=None):
        self.payload = payload
        self.payloads.append(payload)
        if self.error:
            raise self.error
        if self.values:
            return self.values.pop(0)
        return self.value


class FakePIIService:
    def __init__(self):
        self.config = None

    def update_config(self, **config):
        self.config = config

    @staticmethod
    def _empty(text):
        return {
            "has_pii": False,
            "anonymized_text": text,
            "summary": {
                "has_pii": False,
                "total_count": 0,
                "entity_counts": {},
                "entities": [],
            },
            "original_text": text,
            "should_block": False,
        }

    def check_input(self, text):
        return self._empty(text)

    def check_output(self, text):
        return self._empty(text)


class FakeAgent:
    def __init__(
        self,
        *,
        llm=None,
        chain=None,
        classifier=None,
        pii_service=None,
        process_error=None,
        use_chain=False,
    ):
        self.llm = llm or FakeLLM()
        self.chain = chain or FakeChain()
        self.content_filter_service = classifier or FakeClassifier()
        self.pii_service = pii_service or FakePIIService()
        self.enable_prompt_defense = True
        self.process_error = process_error
        self.use_chain = use_chain
        self.loaded_whitelist = None
        self.loaded_pii = None
        self.loaded_prompt = None
        self.loaded_content_filter = None
        self.enable_content_filter = False

    async def process_message(self, message, conversation_id="default"):
        if self.process_error:
            raise self.process_error
        response = "answer"
        if self.use_chain:
            response = await self.chain.ainvoke({"input": message}, {})
        return {
            "response": response,
            "is_blocked": False,
            "conversation_id": conversation_id,
        }

    def reload_white_list_rules(self, patterns):
        self.loaded_whitelist = list(patterns)
        return True

    def reload_pii_config(self, config):
        self.loaded_pii = dict(config)
        self.pii_service.update_config(**config)
        return True

    def reload_prompt_defense_config(self, config):
        self.loaded_prompt = dict(config)
        self.enable_prompt_defense = bool(config.get("enabled"))
        return True

    def reload_content_filter_config(self, config):
        self.loaded_content_filter = dict(config)
        self.enable_content_filter = bool(
            config.get("input_enabled") or config.get("output_enabled")
        )
        return True


class HardenedRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def test_input_arbitration_matrix_requires_semantic_agreement_to_block(self):
        cases = (
            ("allow", "healthy", "allow", 0.95, "allow", None),
            ("allow", "healthy", "allow", 0.40, "allow", None),
            ("block", "healthy", "block", 0.95, "block", "fraud_or_laundering"),
            ("block", "healthy", "block", 0.40, "review", "insufficient_safety_context"),
            ("allow", "healthy", "block", 0.95, "review", "conflicting_safety_evidence"),
            ("block", "healthy", "allow", 0.95, "review", "conflicting_safety_evidence"),
            ("review", "healthy", "allow", 0.95, "review", "conflicting_safety_evidence"),
            ("allow", "skipped", "allow", 0.95, "allow", None),
            ("allow", "skipped", "block", 0.95, "review", "insufficient_safety_context"),
            ("unavailable", "unavailable", "allow", 0.95, "unavailable", "security_detector_unavailable"),
        )

        for evidence_decision, health, llm_decision, confidence, expected, reason in cases:
            with self.subTest(
                evidence=evidence_decision,
                health=health,
                llm=llm_decision,
            ):
                result = arbitrate_input_evidence(
                    ClassificationEvidence(
                        disposition=evidence_decision,
                        max_score=0.9,
                        threshold=0.8,
                        category="fraud_or_laundering",
                        health=health,
                    ),
                    llm_decision=llm_decision,
                    llm_category=(
                        "safe" if llm_decision == "allow" else "fraud_or_laundering"
                    ),
                    llm_confidence=confidence,
                    llm_semantic_role=(
                        "unrelated_or_benign"
                        if llm_decision == "allow"
                        else "prohibited_objective"
                    ),
                )

                self.assertEqual(result.decision, expected)
                self.assertEqual(result.reason_code, reason)

    def test_short_harmless_phrase_is_allowed_by_compositional_semantics(self):
        result = arbitrate_input_evidence(
            ClassificationEvidence(
                disposition="allow",
                max_score=0.0,
                threshold=None,
                category=None,
                health="skipped",
            ),
            llm_decision="allow",
            llm_category="safe",
            llm_confidence=0.95,
            llm_semantic_role="unrelated_or_benign",
        )

        self.assertEqual(result.decision, "allow")
        self.assertIsNone(result.reason_code)

    def test_skipped_classifier_preserves_semantic_risk_topic_for_clarification(self):
        result = arbitrate_input_evidence(
            ClassificationEvidence(
                disposition="allow",
                max_score=0.0,
                threshold=None,
                category=None,
                health="skipped",
            ),
            llm_decision="review",
            llm_category="ambiguous_risk_intent",
            llm_confidence=0.80,
            llm_semantic_role="underspecified_risk_topic",
        )

        self.assertEqual(result.decision, "review")
        self.assertEqual(result.reason_code, "ambiguous_risk_intent")

    async def test_input_classifier_block_is_evidence_not_terminal_result(self):
        token = begin_guardrail_request()
        try:
            classifier = FakeClassifier(score=0.95, threshold=0.8)
            result = await HardenedClassifierProxy(classifier).classify(
                "这是一个足够长的可疑金融行为请求",
                is_output=False,
            )

            self.assertFalse(result.is_blocked)
            self.assertEqual(current_input_evidence().disposition, "block")
            self.assertEqual(classifier.calls, 1)
        finally:
            end_guardrail_request(token)

    async def test_short_input_skips_embedding_and_requires_llm_arbitration(self):
        token = begin_guardrail_request()
        try:
            classifier = FakeClassifier(score=0.95, threshold=0.8)
            result = await HardenedClassifierProxy(classifier).classify(
                "色垃圾",
                is_output=False,
            )

            self.assertFalse(result.is_blocked)
            self.assertEqual(classifier.calls, 0)
            self.assertEqual(current_input_evidence().health, "skipped")
        finally:
            end_guardrail_request(token)

    async def test_security_llm_receives_only_bounded_prior_user_turns(self):
        llm = FakeLLM()
        agent = install_hardening(FakeAgent(llm=llm))
        history = [f"turn-{index}-" + ("x" * 900) for index in range(9)]
        token = begin_guardrail_request(history=history)
        try:
            await agent._check_with_llm_security("current request")
        finally:
            end_guardrail_request(token)

        last_message = llm.messages[-1]
        content = (
            last_message.content
            if hasattr(last_message, "content")
            else last_message["content"]
        )
        payload = json.loads(content)
        self.assertEqual(payload["currentInput"], "current request")
        self.assertLessEqual(len(payload["priorUserTurns"]), 6)
        self.assertNotIn("turn-0", str(payload))
        self.assertLessEqual(len(content), 3400)

    async def test_disables_guardrails_metrics_and_remote_inferencing(self):
        settings = SimpleNamespace(
            rc=SimpleNamespace(enable_metrics=True, use_remote_inferencing=True)
        )

        disable_third_party_telemetry(settings)

        self.assertFalse(settings.rc.enable_metrics)
        self.assertFalse(settings.rc.use_remote_inferencing)

    async def test_request_log_hook_does_not_persist_raw_input_or_answer(self):
        class FakeRequestLogCRUD:
            received = None

            @staticmethod
            async def create_log(*args, **kwargs):
                FakeRequestLogCRUD.received = kwargs
                return kwargs

        install_request_log_redaction(FakeRequestLogCRUD)

        await FakeRequestLogCRUD.create_log(
            source_ip="192.0.2.1",
            user_agent="synthetic-agent",
            user_input="SYNTHETIC_PRIVATE_INPUT",
            response_content="SYNTHETIC_PRIVATE_OUTPUT",
            security_risks={"private_rule": 0.99},
            conversation_id="synthetic-conversation",
        )

        received = FakeRequestLogCRUD.received
        self.assertIsNone(received["source_ip"])
        self.assertIsNone(received["user_agent"])
        self.assertIsNone(received["security_risks"])
        self.assertEqual(received["user_input"], "[REDACTED]")
        self.assertEqual(received["response_content"], "[REDACTED]")
        self.assertEqual(received["conversation_id"], "synthetic-conversation")

    async def test_security_prompt_declares_every_exact_contract_enum(self):
        required_literals = {
            "safe",
            "unsafe_content",
            "prompt_injection",
            "privacy_exfiltration",
            "fraud_or_laundering",
            "misleading_guarantee",
            "ambiguous_risk_intent",
            "conflicting_safety_evidence",
            "insufficient_safety_context",
            "clarify_safe_purpose",
            "unrelated_or_benign",
            "legitimate_safety_context",
            "underspecified_risk_topic",
            "prohibited_objective",
        }

        for literal in required_literals:
            self.assertIn(literal, FOUR_STATE_SECURITY_PROMPT)
        self.assertIn("Every output must include numeric confidence", FOUR_STATE_SECURITY_PROMPT)
        self.assertIn('"decision":"allow"', FOUR_STATE_SECURITY_PROMPT)
        self.assertIn('"decision":"block"', FOUR_STATE_SECURITY_PROMPT)
        self.assertIn('"decision":"review"', FOUR_STATE_SECURITY_PROMPT)
        self.assertIn("transaction structuring", FOUR_STATE_SECURITY_PROMPT)
        self.assertIn("intermediary movement of funds", FOUR_STATE_SECURITY_PROMPT)
        self.assertIn(
            "reduce, bypass, or avoid transaction review",
            FOUR_STATE_SECURITY_PROMPT,
        )
        self.assertIn(
            "official rules, compliance requirements, or controls to verify",
            FOUR_STATE_SECURITY_PROMPT,
        )
        self.assertIn(
            "A prior prohibited request is not evidence against a new self-contained request",
            FOUR_STATE_SECURITY_PROMPT,
        )
        self.assertIn("elliptical continuation", FOUR_STATE_SECURITY_PROMPT)

    async def test_security_messages_do_not_mix_the_legacy_status_contract(self):
        llm = FakeLLM()
        agent = FakeAgent(llm=llm)
        agent.security_check_prompt = "LEGACY_STATUS_CONTRACT"
        install_hardening(agent)

        result = await agent._check_with_llm_security("synthetic")

        self.assertTrue(result["is_safe"])
        rendered = str(llm.messages)
        self.assertIn("decision must be allow, block, or review", rendered)
        self.assertNotIn("LEGACY_STATUS_CONTRACT", rendered)

    async def test_unloaded_classifier_is_unavailable_for_normal_input(self):
        proxy = HardenedClassifierProxy(FakeClassifier(loaded=False))

        result = await proxy.classify(
            "请解释合成转账需要经过哪些合规检查",
            is_output=False,
        )

        self.assertTrue(result.is_blocked)
        self.assertEqual(result.max_category, "security_detector_unavailable")

    async def test_near_threshold_input_evidence_is_saved_without_immediate_block(self):
        token = begin_guardrail_request()
        try:
            proxy = HardenedClassifierProxy(FakeClassifier(score=0.78, threshold=0.8))
            result = await proxy.classify(
                "请解释合成转账需要经过哪些合规检查",
                is_output=False,
            )

            self.assertFalse(result.is_blocked)
            self.assertEqual(current_input_evidence().disposition, "review")
        finally:
            end_guardrail_request(token)

    async def test_empty_classifier_result_for_classifiable_input_is_unavailable(self):
        proxy = HardenedClassifierProxy(FakeClassifier(loaded=True, empty=True))

        result = await proxy.classify(
            "请解释合成转账需要经过哪些合规检查",
            is_output=False,
        )

        self.assertTrue(result.is_blocked)
        self.assertEqual(result.max_category, "security_detector_unavailable")

    async def test_concurrent_evidence_does_not_cross_requests(self):
        async def inspect(score):
            token = begin_guardrail_request()
            try:
                proxy = HardenedClassifierProxy(
                    FakeClassifier(score=score, threshold=0.8)
                )
                await proxy.classify(
                    "请解释合成转账需要经过哪些合规检查",
                    is_output=False,
                )
                return current_input_evidence().max_score
            finally:
                end_guardrail_request(token)

        self.assertEqual(
            await asyncio.gather(inspect(0.2), inspect(0.78)),
            [0.2, 0.78],
        )

    async def test_sequential_request_resets_previous_evidence(self):
        first = begin_guardrail_request()
        try:
            await HardenedClassifierProxy(FakeClassifier(score=0.78)).classify(
                "请解释合成转账需要经过哪些合规检查",
                is_output=False,
            )
            self.assertEqual(current_input_evidence().disposition, "review")
        finally:
            end_guardrail_request(first)

        second = begin_guardrail_request()
        try:
            self.assertIsNone(current_input_evidence())
        finally:
            end_guardrail_request(second)

    async def test_classifier_exception_returns_unavailable_category_for_input_and_output(self):
        proxy = HardenedClassifierProxy(FakeClassifier(RuntimeError("private failure")))

        for is_output in (False, True):
            result = await proxy.classify(
                "这是一个足够长的分类器异常测试",
                is_output=is_output,
            )
            self.assertTrue(result.is_blocked)
            self.assertEqual(
                result.triggered_categories,
                ("security_detector_unavailable",),
            )

    async def test_security_llm_exception_is_not_allowed(self):
        agent = install_hardening(FakeAgent(llm=FakeLLM(error=RuntimeError("secret"))))

        result = await agent._check_with_llm_security("synthetic")

        self.assertFalse(result["is_safe"])
        self.assertEqual(result["detected_issues"], "security_detector_unavailable")

    async def test_security_llm_invalid_contract_is_not_allowed(self):
        agent = install_hardening(FakeAgent(llm=FakeLLM(content="not json")))

        result = await agent._check_with_llm_security("synthetic")

        self.assertFalse(result["is_safe"])
        self.assertEqual(result["violated_rules"], ["security_detector_unavailable"])

    async def test_security_llm_accepts_only_strict_allow_or_block(self):
        allow_agent = install_hardening(
            FakeAgent(
                llm=FakeLLM(
                    content=json.dumps(
                        {
                            "decision": "allow",
                            "category": "safe",
                            "confidence": 0.95,
                            "semantic_role": "unrelated_or_benign",
                        }
                    )
                )
            )
        )
        block_agent = install_hardening(
            FakeAgent(
                llm=FakeLLM(
                    content=json.dumps(
                        {
                            "decision": "block",
                            "category": "fraud_or_laundering",
                            "confidence": 0.95,
                            "semantic_role": "prohibited_objective",
                        }
                    )
                )
            )
        )

        allowed = await allow_agent._check_with_llm_security("synthetic")
        token = begin_guardrail_request()
        try:
            await HardenedClassifierProxy(
                FakeClassifier(score=0.95, threshold=0.8)
            ).classify("这是一个足够长的可疑金融行为请求", is_output=False)
            blocked = await block_agent._check_with_llm_security("synthetic")
        finally:
            end_guardrail_request(token)

        self.assertTrue(allowed["is_safe"])
        self.assertFalse(blocked["is_safe"])
        self.assertEqual(blocked["violated_rules"], ["fraud_or_laundering"])

    async def test_security_llm_accepts_review_contract(self):
        agent = install_hardening(
            FakeAgent(
                llm=FakeLLM(
                    content=json.dumps(
                        {
                            "decision": "review",
                            "category": "ambiguous_risk_intent",
                            "confidence": 0.71,
                            "clarification_key": "clarify_safe_purpose",
                            "semantic_role": "underspecified_risk_topic",
                        }
                    )
                )
            )
        )

        result = await agent._check_with_llm_security("synthetic")

        self.assertFalse(result["is_safe"])
        self.assertEqual(result["risk_level"], "review")
        self.assertEqual(result["detected_issues"], "ambiguous_risk_intent")

    async def test_low_confidence_allow_with_near_threshold_classifier_becomes_review(self):
        token = begin_guardrail_request()
        try:
            proxy = HardenedClassifierProxy(FakeClassifier(score=0.78, threshold=0.8))
            await proxy.classify(
                "请解释合成转账需要经过哪些合规检查",
                is_output=False,
            )
            agent = install_hardening(
                FakeAgent(
                    llm=FakeLLM(
                        content=json.dumps(
                            {
                                "decision": "allow",
                                "category": "safe",
                                "confidence": 0.70,
                                "semantic_role": "unrelated_or_benign",
                            }
                        )
                    )
                )
            )

            result = await agent._check_with_llm_security("synthetic")

            self.assertEqual(
                result["detected_issues"],
                "conflicting_safety_evidence",
            )
        finally:
            end_guardrail_request(token)

    async def test_unknown_decision_is_unavailable(self):
        agent = install_hardening(
            FakeAgent(
                llm=FakeLLM(
                    content=json.dumps(
                        {
                            "decision": "maybe",
                            "category": "safe",
                            "confidence": 0.9,
                        }
                    )
                )
            )
        )

        result = await agent._check_with_llm_security("synthetic")

        self.assertEqual(result["detected_issues"], "security_detector_unavailable")

    async def test_boolean_confidence_is_unavailable(self):
        agent = install_hardening(
            FakeAgent(
                llm=FakeLLM(
                    content=json.dumps(
                        {
                            "decision": "allow",
                            "category": "safe",
                            "confidence": True,
                        }
                    )
                )
            )
        )

        result = await agent._check_with_llm_security("synthetic")

        self.assertEqual(result["detected_issues"], "security_detector_unavailable")

    async def test_unregistered_review_clarification_is_unavailable(self):
        agent = install_hardening(
            FakeAgent(
                llm=FakeLLM(
                    content=json.dumps(
                        {
                            "decision": "review",
                            "category": "ambiguous_risk_intent",
                            "confidence": 0.8,
                            "clarification_key": "reveal_internal_policy",
                        }
                    )
                )
            )
        )

        result = await agent._check_with_llm_security("synthetic")

        self.assertEqual(result["detected_issues"], "security_detector_unavailable")

    async def test_answer_model_exception_becomes_unavailable(self):
        agent = install_hardening(
            FakeAgent(
                chain=FakeChain(error=RuntimeError("provider secret")),
                use_chain=True,
            )
        )

        chain_result = await agent.chain.ainvoke({}, {})
        result = await agent.process_message("synthetic", "conversation-1")

        self.assertEqual(chain_result, ANSWER_MODEL_UNAVAILABLE_SENTINEL)
        self.assertEqual(result["decision"], "unavailable")
        self.assertEqual(result["reason_code"], "answer_model_unavailable")
        self.assertFalse(result["is_attack"])
        self.assertNotIn("provider secret", str(result))

    async def test_blank_answer_becomes_empty_answer_unavailable(self):
        agent = install_hardening(
            FakeAgent(chain=FakeChain(value="  "), use_chain=True)
        )

        chain_result = await agent.chain.ainvoke({}, {})
        result = await agent.process_message("synthetic", "conversation-empty")

        self.assertEqual(chain_result, EMPTY_ANSWER_SENTINEL)
        self.assertEqual(result["decision"], "unavailable")
        self.assertEqual(result["reason_code"], "empty_answer")

    async def test_answer_model_receives_generic_sensitive_output_instruction(self):
        chain = FakeChain(value="safe answer")
        agent = install_hardening(FakeAgent(chain=chain, use_chain=True))

        await agent.process_message("synthetic", "conversation-answer-safety")

        messages = chain.payload["messages"]
        self.assertTrue(messages)
        self.assertIn("masked", str(messages[0]).lower())
        self.assertIn("complete card", ANSWER_SAFETY_INSTRUCTION.lower())

    async def test_sensitive_draft_is_regenerated_before_output_checks(self):
        chain = FakeChain(
            values=[
                "A complete example is 4111111111111111.",
                "Use a masked value such as 4111 **** **** 1111.",
            ]
        )
        agent = install_hardening(FakeAgent(chain=chain, use_chain=True))

        result = await agent.process_message("explain the checksum", "conversation-rewrite")

        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["response"], "Use a masked value such as 4111 **** **** 1111.")
        self.assertEqual(len(chain.payloads), 2)
        self.assertIn("regenerate", str(chain.payloads[1]["messages"][0]).lower())

    async def test_safe_draft_does_not_trigger_regeneration(self):
        chain = FakeChain(value="Use only masked examples.")
        agent = install_hardening(FakeAgent(chain=chain, use_chain=True))

        result = await agent.process_message("explain the checksum", "conversation-safe-draft")

        self.assertEqual(result["decision"], "allow")
        self.assertEqual(len(chain.payloads), 1)

    async def test_repeated_sensitive_draft_uses_non_sensitive_safe_fallback(self):
        chain = FakeChain(
            values=[
                "A complete example is 4111111111111111.",
                "Another complete example is 5555555555554444.",
            ]
        )
        agent = install_hardening(FakeAgent(chain=chain, use_chain=True))

        result = await agent.process_message("explain the checksum", "conversation-fallback")

        self.assertEqual(result["decision"], "allow")
        self.assertEqual(len(chain.payloads), 2)
        self.assertNotIn("4111111111111111", result["response"])
        self.assertNotIn("5555555555554444", result["response"])
        self.assertIn("masked", result["response"].lower())

    async def test_non_text_answer_becomes_invalid_contract(self):
        agent = install_hardening(
            FakeAgent(
                chain=FakeChain(value={"unsafe": "shape"}),
                use_chain=True,
            )
        )

        chain_result = await agent.chain.ainvoke({}, {})
        result = await agent.process_message("synthetic", "conversation-invalid")

        self.assertEqual(chain_result, INVALID_ANSWER_CONTRACT_SENTINEL)
        self.assertEqual(result["decision"], "unavailable")
        self.assertEqual(result["reason_code"], "invalid_answer_contract")

    async def test_dependency_exception_details_are_not_written_to_logs(self):
        agent = install_hardening(
            FakeAgent(
                chain=FakeChain(error=RuntimeError("provider secret")),
                use_chain=True,
            )
        )

        with self.assertLogs("ai_bank_guardrail_hardening", level="ERROR") as logs:
            await agent.process_message("synthetic", "conversation-logs")

        rendered = "\n".join(logs.output)
        self.assertIn("answer_model_unavailable", rendered)
        self.assertNotIn("provider secret", rendered)

    async def test_top_level_exception_becomes_runtime_unavailable(self):
        agent = install_hardening(FakeAgent(process_error=RuntimeError("private stack")))

        result = await agent.process_message("synthetic", "conversation-2")

        self.assertEqual(result["reason_code"], "guardrail_runtime_error")
        self.assertNotIn("private stack", str(result))

    async def test_whitelist_is_forced_empty(self):
        agent = install_hardening(FakeAgent())

        agent.reload_white_list_rules([".*"])

        self.assertEqual(agent.loaded_whitelist, [])

    async def test_prompt_defense_cannot_be_disabled(self):
        agent = install_hardening(FakeAgent())

        agent.reload_prompt_defense_config({"enabled": False, "prompt_content": "safe"})

        self.assertTrue(agent.loaded_prompt["enabled"])
        self.assertTrue(agent.enable_prompt_defense)

    async def test_mandatory_content_filter_cannot_be_disabled(self):
        agent = install_hardening(FakeAgent())

        agent.reload_content_filter_config(
            {"input_enabled": False, "output_enabled": False, "action_mode": "detect"}
        )

        self.assertTrue(agent.loaded_content_filter["input_enabled"])
        self.assertTrue(agent.loaded_content_filter["output_enabled"])
        self.assertEqual(agent.loaded_content_filter["action_mode"], "block")
        self.assertTrue(agent.enable_content_filter)

    async def test_pii_policy_is_narrowed_and_forced_to_block(self):
        agent = install_hardening(FakeAgent())

        agent.reload_pii_config(
            {
                "input_enabled": False,
                "output_enabled": False,
                "input_entities": ["pii"],
                "output_entities": ["pii"],
                "use_presidio": True,
            }
        )

        self.assertEqual(agent.loaded_pii["input_entities"], list(HARDENED_PII_ENTITIES))
        self.assertEqual(agent.loaded_pii["output_entities"], list(HARDENED_PII_ENTITIES))
        self.assertTrue(agent.loaded_pii["input_enabled"])
        self.assertTrue(agent.loaded_pii["output_enabled"])
        self.assertFalse(agent.loaded_pii["use_presidio"])
        self.assertEqual(agent.loaded_pii["input_action_mode"], "block")
        self.assertEqual(agent.loaded_pii["output_action_mode"], "block")

    async def test_deterministic_pii_additions_detect_iban_and_ssn_without_broad_false_positives(self):
        agent = install_hardening(FakeAgent())

        iban = agent.pii_service.check_input("IBAN GB82 WEST 1234 5698 7654 32")
        ssn = agent.pii_service.check_output("SSN 123-45-6789")
        ordinary = agent.pii_service.check_input(
            "张三计划在2026-09-02去北京市，并查看https://example.com。"
        )

        self.assertTrue(iban["has_pii"])
        self.assertEqual(iban["summary"]["entities"][0]["type"], "IBAN_CODE")
        self.assertTrue(ssn["has_pii"])
        self.assertEqual(ssn["summary"]["entities"][0]["type"], "US_SSN")
        self.assertFalse(ordinary["has_pii"])

    async def test_invalid_luhn_number_is_removed_from_broad_bank_card_detection(self):
        class BroadBankCardPII(FakePIIService):
            def check_input(self, text):
                return {
                    "has_pii": True,
                    "anonymized_text": text,
                    "summary": {
                        "has_pii": True,
                        "total_count": 1,
                        "entity_counts": {"CN_BANK_CARD": 1},
                        "entities": [
                            {
                                "type": "CN_BANK_CARD",
                                "text": "4111111111111112",
                                "position": (4, 20),
                                "score": 1.0,
                            }
                        ],
                    },
                    "original_text": text,
                    "should_block": True,
                }

        agent = install_hardening(FakeAgent(pii_service=BroadBankCardPII()))

        result = agent.pii_service.check_input("示例4111111111111112无法通过校验")

        self.assertFalse(result["has_pii"])
        self.assertFalse(result["should_block"])
        self.assertEqual(result["summary"]["entities"], [])

    async def test_valid_cn_id_is_recovered_from_bank_card_overlap(self):
        class OverlappingBankCardPII(FakePIIService):
            def check_input(self, text):
                return {
                    "has_pii": True,
                    "anonymized_text": text,
                    "summary": {
                        "has_pii": True,
                        "total_count": 1,
                        "entity_counts": {"CN_BANK_CARD": 1},
                        "entities": [
                            {
                                "type": "CN_BANK_CARD",
                                "text": "110101199001011237",
                                "position": (4, 22),
                                "score": 1.0,
                            }
                        ],
                    },
                    "original_text": text,
                    "should_block": True,
                }

        agent = install_hardening(FakeAgent(pii_service=OverlappingBankCardPII()))

        result = agent.pii_service.check_input("测试证件110101199001011237仅用于校验")

        self.assertTrue(result["has_pii"])
        self.assertTrue(result["should_block"])
        self.assertEqual(result["summary"]["entities"][0]["type"], "CN_ID_CARD")

    async def test_installation_is_idempotent(self):
        agent = FakeAgent()

        first = install_hardening(agent)
        process_after_first = first.process_message
        second = install_hardening(agent)

        self.assertIs(first, second)
        self.assertIs(process_after_first, second.process_message)


class FakeStateModule:
    def __init__(self):
        self.received = None
        self.set_agent = self._set_agent

    def _set_agent(self, agent):
        self.received = agent


class RegistrationHookTest(unittest.TestCase):
    def test_registration_hook_hardens_before_delegating_and_is_idempotent(self):
        state = FakeStateModule()
        install_registration_hook(state)
        first_setter = state.set_agent
        install_registration_hook(state)
        agent = FakeAgent()

        state.set_agent(agent)

        self.assertIs(state.set_agent, first_setter)
        self.assertIs(state.received, agent)
        self.assertTrue(agent._ai_bank_hardening_installed)


class SensitiveLogFilterTest(unittest.TestCase):
    def test_redacts_user_text_from_success_and_whitelist_messages(self):
        for message in (
            "安全检查全部通过：SYNTHETIC_PRIVATE_INPUT",
            "白名单放行：SYNTHETIC_PRIVATE_INPUT",
        ):
            with self.subTest(message=message):
                record = logging.LogRecord(
                    name="security",
                    level=logging.INFO,
                    pathname=__file__,
                    lineno=1,
                    msg=message,
                    args=(),
                    exc_info=None,
                )

                SensitiveLogFilter().filter(record)

                self.assertEqual(
                    record.getMessage(),
                    "guardrail_event details_redacted",
                )

    def test_replaces_upstream_input_output_rule_and_score_details(self):
        record = logging.LogRecord(
            name="security",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=(
                "内容过滤拦截 - 类别：['private_rule'], 分数：{'private_rule': 0.99}, "
                "输入：SYNTHETIC_PRIVATE_INPUT, 输出：SYNTHETIC_PRIVATE_OUTPUT"
            ),
            args=(),
            exc_info=None,
        )

        SensitiveLogFilter().filter(record)

        rendered = record.getMessage()
        self.assertEqual(rendered, "guardrail_event details_redacted")
        self.assertNotIn("SYNTHETIC_PRIVATE_INPUT", rendered)
        self.assertNotIn("SYNTHETIC_PRIVATE_OUTPUT", rendered)
        self.assertNotIn("private_rule", rendered)
        self.assertNotIn("0.99", rendered)

    def test_redacts_high_sensitivity_values_from_upstream_log_messages(self):
        record = logging.LogRecord(
            name="security",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=(
                "输入: card 6222021234567890, phone 13812345678, "
                "email synthetic-contact@example.invalid, SSN 123-45-6789"
            ),
            args=(),
            exc_info=None,
        )

        SensitiveLogFilter().filter(record)

        rendered = record.getMessage()
        self.assertNotIn("6222021234567890", rendered)
        self.assertNotIn("13812345678", rendered)
        self.assertNotIn("synthetic-contact@example.invalid", rendered)
        self.assertNotIn("123-45-6789", rendered)
        self.assertEqual(rendered, "guardrail_event details_redacted")

    def test_removes_exception_message_and_traceback(self):
        try:
            raise RuntimeError("provider secret")
        except RuntimeError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="agent_service",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="处理消息出错：provider secret",
            args=(),
            exc_info=exc_info,
        )

        SensitiveLogFilter().filter(record)

        self.assertEqual(record.getMessage(), "guardrail_internal_error details_redacted")
        self.assertIsNone(record.exc_info)


class StreamProjectionTest(unittest.TestCase):
    def test_review_stream_contains_only_safe_projection(self):
        chunk = (
            'data: {"status":"reject","blocked_by":"llm",'
            '"detected_issues":"ambiguous_risk_intent"}\n\n'
        )

        result = sanitize_stream_chunk(chunk)

        self.assertIn('"decision": "review"', result)
        self.assertNotIn("ambiguous_risk_intent", result)

    def test_sanitizes_reject_metadata(self):
        chunk = (
            'data: {"status":"reject","blocked_by":"llm",'
            '"risk_level":"high","detected_issues":"prompt_injection",'
            '"violated_rules":["private_rule"]}\n\n'
        )

        result = sanitize_stream_chunk(chunk)

        self.assertIn('"status": "reject"', result)
        self.assertIn("response_message", result)
        self.assertNotIn("risk_level", result)
        self.assertNotIn("private_rule", result)

    def test_replaces_answer_model_sentinel(self):
        result = sanitize_stream_chunk(
            f"data: {ANSWER_MODEL_UNAVAILABLE_SENTINEL}\n\n"
        )

        self.assertNotIn(ANSWER_MODEL_UNAVAILABLE_SENTINEL, result)
        self.assertIn("智能服务暂时不可用", result)

    def test_leaves_safe_stream_content_unchanged(self):
        chunk = "data: 正常内容\n\n"
        self.assertEqual(sanitize_stream_chunk(chunk), chunk)


class HttpProjectionTest(unittest.TestCase):
    def test_projects_legacy_http_payload_to_four_states(self):
        allowed = project_http_payload({"is_blocked": False, "response": "safe"})
        review = project_http_payload(
            {
                "is_blocked": True,
                "detect_reason": "ambiguous_risk_intent",
                "response": "safe clarification",
            }
        )
        unavailable = project_http_payload(
            {
                "is_blocked": True,
                "detect_reason": "security_detector_unavailable",
                "response": "safe unavailable",
            }
        )
        blocked = project_http_payload(
            {
                "is_blocked": True,
                "detect_reason": "sensitive_input",
                "response": "safe block",
            }
        )

        self.assertEqual((allowed["decision"], allowed["reason_code"]), ("allow", None))
        self.assertEqual(
            (review["decision"], review["reason_code"]),
            ("review", "ambiguous_risk_intent"),
        )
        self.assertEqual(
            (unavailable["decision"], unavailable["reason_code"]),
            ("unavailable", "security_detector_unavailable"),
        )
        self.assertEqual(
            (blocked["decision"], blocked["reason_code"]),
            ("block", "sensitive_input"),
        )


if __name__ == "__main__":
    unittest.main()
