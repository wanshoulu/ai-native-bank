import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.contracts import IntentTaskEnvelope, UserMemoryCandidateHint
from app.memory_candidates import build_canonical_episode_candidates, build_user_preference_candidates
from app.memory_runtime import IntentMemoryPolicyRegistry, UserMemoryPolicyRegistry


class MemoryCandidateTests(unittest.TestCase):
    def test_explicit_allowlisted_preference_hint_becomes_a_candidate(self):
        policies = UserMemoryPolicyRegistry({
            "schemaVersion": "1.0", "policyVersion": "user-v1",
            "defaults": {"read": None, "writeHint": None}, "reads": {},
            "writeHints": {"response_style": {"candidateType": "user_preference", "purpose": "service_personalization", "requiresConsent": True}},
        })
        result = build_user_preference_candidates(
            hints=[{"predicate": "response_style", "value": "concise", "evidence_kind": "user_explicit", "consent_granted": True}],
            policies=policies, tenant_id="bank", customer_id="cust_demo_001", agent_domain="ai-native-bank",
            agent_run_id="abr_1", occurred_at=datetime(2026, 9, 6, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["command"]["candidateType"], "user_preference")
        self.assertTrue(result[0]["command"]["consentGranted"])

    def test_unknown_predicate_uses_default_write_hint_when_available(self):
        policies = UserMemoryPolicyRegistry({
            "schemaVersion": "1.0", "policyVersion": "user-v1",
            "defaults": {"read": None, "writeHint": {"candidateType": "user_preference", "purpose": "service_personalization", "requiresConsent": True}},
            "reads": {},
            "writeHints": {"response_style": {"candidateType": "user_preference", "purpose": "service_personalization", "requiresConsent": True}},
        })
        result = build_user_preference_candidates(
            hints=[{"predicate": "dislikes_steady_growth_products", "value": True, "evidence_kind": "user_explicit", "consent_granted": True}],
            policies=policies, tenant_id="bank", customer_id="cust_demo_001", agent_domain="ai-native-bank",
            agent_run_id="abr_1", occurred_at=datetime(2026, 9, 6, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["command"]["predicate"], "dislikes_steady_growth_products")
        self.assertEqual(result[0]["command"]["scope"]["purpose"], "service_personalization")

    def test_unknown_predicate_skipped_when_no_default_write_hint(self):
        policies = UserMemoryPolicyRegistry({
            "schemaVersion": "1.0", "policyVersion": "user-v1",
            "defaults": {"read": None, "writeHint": None}, "reads": {},
            "writeHints": {"response_style": {"candidateType": "user_preference", "purpose": "service_personalization", "requiresConsent": True}},
        })
        result = build_user_preference_candidates(
            hints=[{"predicate": "dislikes_steady_growth_products", "value": True, "evidence_kind": "user_explicit", "consent_granted": True}],
            policies=policies, tenant_id="bank", customer_id="cust_demo_001", agent_domain="ai-native-bank",
            agent_run_id="abr_1", occurred_at=datetime(2026, 9, 6, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result), 0)

    def test_l2_budget_hint_becomes_candidate_with_bound_purpose(self):
        policies = UserMemoryPolicyRegistry({
            "schemaVersion": "1.0", "policyVersion": "user-v1",
            "defaults": {"read": None, "writeHint": None}, "reads": {},
            "writeHints": {"monthly_living_budget": {"candidateType": "user_preference", "purpose": "monthly_fund_planning", "requiresConsent": True}},
        })
        result = build_user_preference_candidates(
            hints=[{"predicate": "monthly_living_budget", "value": 7000, "evidence_kind": "user_explicit", "consent_granted": True}],
            policies=policies, tenant_id="bank", customer_id="cust_demo_001", agent_domain="ai-native-bank",
            agent_run_id="abr_1", occurred_at=datetime(2026, 9, 6, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["command"]["scope"]["purpose"], "monthly_fund_planning")
        self.assertEqual(result[0]["command"]["value"], 7000)
        self.assertEqual(result[0]["command"]["predicate"], "monthly_living_budget")

    def test_hint_contract_enforces_predicate_value_domains(self):
        hint = UserMemoryCandidateHint.model_validate({
            "predicate": "monthly_living_budget", "value": 7000,
            "evidence_kind": "user_explicit", "consent_granted": True,
        })
        self.assertEqual(hint.value, 7000)
        hint = UserMemoryCandidateHint.model_validate({
            "predicate": "investment_goal_preference", "value": "steady_growth",
            "evidence_kind": "user_explicit", "consent_granted": True,
        })
        self.assertEqual(hint.value, "steady_growth")
        for payload in (
            {"predicate": "investment_goal_preference", "value": "aggressive"},
            {"predicate": "spending_explanation_preference", "value": "random"},
            {"predicate": "monthly_living_budget", "value": 7000.5},
        ):
            with self.assertRaises(ValidationError):
                UserMemoryCandidateHint.model_validate({
                    **payload, "evidence_kind": "user_explicit", "consent_granted": True,
                })

    def test_free_semantic_predicates_accept_any_scalar_value(self):
        hint = UserMemoryCandidateHint.model_validate({
            "predicate": "dislikes_steady_growth_products", "value": True,
            "evidence_kind": "user_explicit", "consent_granted": True,
        })
        self.assertEqual(hint.predicate, "dislikes_steady_growth_products")
        self.assertEqual(hint.value, True)
        hint = UserMemoryCandidateHint.model_validate({
            "predicate": "prefers_high_yield_deposits", "value": "yes",
            "evidence_kind": "user_explicit", "consent_granted": True,
        })
        self.assertEqual(hint.value, "yes")
        hint = UserMemoryCandidateHint.model_validate({
            "predicate": "wants_budget_alerts", "value": 100,
            "evidence_kind": "user_explicit", "consent_granted": True,
        })
        self.assertEqual(hint.value, 100)

    def test_only_policy_bound_successful_tasks_create_value_bounded_candidates(self):
        policies = IntentMemoryPolicyRegistry({
            "schemaVersion": "1.0", "policyVersion": "bank-memory-intent-v1",
            "defaults": {"read": None, "write": None},
            "intents": {"ccb.account.balance.query": {"write": {
                "statuses": ["succeeded"], "candidateType": "canonical_service_episode",
                "predicate": "service_episode_summary", "purpose": "service_continuity",
            }}},
        })
        envelope = IntentTaskEnvelope.model_validate({
            "schema_version": "1.0", "delegation_id": "dlg_1", "trace_id": "trace_1",
            "conversation_id": "conv_1", "source_agent_run_id": "abr_1",
            "intent_shape": "single_intent", "tasks": [{
                "task_id": "task_1", "intent_id": "ccb.account.balance.query", "action": "QUERY",
                "business_object": "account_balance", "execution_readiness": "ready", "risk_level": "L0",
            }],
        })
        result = build_canonical_episode_candidates(
            envelope=envelope, policies=policies, tenant_id="bank", customer_id="cust_demo_001",
            agent_domain="ai-native-bank", agent_run_id="abr_1", status="succeeded",
            occurred_at=datetime(2026, 9, 6, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["command"]["value"], "ccb.account.balance.query:succeeded")
        self.assertEqual(result[0]["command"]["source"]["sourceRef"], "abr_1")
        self.assertNotIn("customer", result[0]["command"]["evidence"]["safeSummary"])

    def test_blocked_or_unconfigured_tasks_create_no_candidates(self):
        policies = IntentMemoryPolicyRegistry({
            "schemaVersion": "1.0", "policyVersion": "v1", "defaults": {"read": None, "write": None}, "intents": {},
        })
        envelope = IntentTaskEnvelope.model_validate({
            "schema_version": "1.0", "delegation_id": "dlg_1", "trace_id": "trace_1",
            "conversation_id": "conv_1", "source_agent_run_id": "abr_1", "intent_shape": "non_task", "tasks": [],
        })
        self.assertEqual(build_canonical_episode_candidates(
            envelope=envelope, policies=policies, tenant_id="bank", customer_id="cust_demo_001",
            agent_domain="ai-native-bank", agent_run_id="abr_1", status="blocked",
            occurred_at=datetime.now(timezone.utc),
        ), [])

    def test_same_predicate_hints_dedupe_to_first_candidate(self):
        policies = UserMemoryPolicyRegistry({
            "schemaVersion": "1.0", "policyVersion": "user-v1",
            "defaults": {"read": None, "writeHint": None}, "reads": {},
            "writeHints": {"response_style": {"candidateType": "user_preference", "purpose": "service_personalization", "requiresConsent": True}},
        })
        result = build_user_preference_candidates(
            hints=[
                {"predicate": "response_style", "value": "concise", "evidence_kind": "user_explicit", "consent_granted": True},
                {"predicate": "response_style", "value": "detailed", "evidence_kind": "user_explicit", "consent_granted": True},
            ],
            policies=policies, tenant_id="bank", customer_id="cust_demo_001", agent_domain="ai-native-bank",
            agent_run_id="abr_1", occurred_at=datetime(2026, 9, 6, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["command"]["value"], "concise")

    def test_idempotency_key_follows_five_tuple_composition(self):
        user_policies = UserMemoryPolicyRegistry({
            "schemaVersion": "1.0", "policyVersion": "user-v1",
            "defaults": {"read": None, "writeHint": None}, "reads": {},
            "writeHints": {"response_style": {"candidateType": "user_preference", "purpose": "service_personalization", "requiresConsent": True}},
        })
        intent_policies = IntentMemoryPolicyRegistry({
            "schemaVersion": "1.0", "policyVersion": "bank-memory-intent-v1",
            "defaults": {"read": None, "write": None},
            "intents": {"ccb.account.balance.query": {"write": {
                "statuses": ["succeeded"], "candidateType": "canonical_service_episode",
                "predicate": "service_episode_summary", "purpose": "service_continuity",
            }}},
        })
        occurred_at = datetime(2026, 9, 6, 10, tzinfo=timezone.utc)
        hints = [{"predicate": "response_style", "value": "concise", "evidence_kind": "user_explicit", "consent_granted": True}]
        envelope = IntentTaskEnvelope.model_validate({
            "schema_version": "1.0", "delegation_id": "dlg_1", "trace_id": "trace_1",
            "conversation_id": "conv_1", "source_agent_run_id": "abr_1",
            "intent_shape": "single_intent", "tasks": [{
                "task_id": "task_1", "intent_id": "ccb.account.balance.query", "action": "QUERY",
                "business_object": "account_balance", "execution_readiness": "ready", "risk_level": "L0",
            }],
        })

        preference = build_user_preference_candidates(
            hints=hints, policies=user_policies, tenant_id="bank", customer_id="cust_demo_001",
            agent_domain="ai-native-bank", agent_run_id="abr_1", occurred_at=occurred_at,
        )
        episode = build_canonical_episode_candidates(
            envelope=envelope, policies=intent_policies, tenant_id="bank", customer_id="cust_demo_001",
            agent_domain="ai-native-bank", agent_run_id="abr_1", status="succeeded", occurred_at=occurred_at,
        )
        replay = build_user_preference_candidates(
            hints=hints, policies=user_policies, tenant_id="bank", customer_id="cust_demo_001",
            agent_domain="ai-native-bank", agent_run_id="abr_1", occurred_at=occurred_at,
        )

        self.assertNotEqual(
            preference[0]["command"]["idempotencyKey"],
            episode[0]["command"]["idempotencyKey"],
        )
        self.assertEqual(
            preference[0]["command"]["idempotencyKey"],
            replay[0]["command"]["idempotencyKey"],
        )
