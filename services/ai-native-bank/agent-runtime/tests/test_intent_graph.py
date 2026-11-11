from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from app.intent_graph import IntentGraphError, IntentGraphRepository


GRAPH_PATH = Path(
    os.getenv(
        "AI_BANK_INTENT_GRAPH_PATH",
        "/app/data/ai-native-bank/intent-graph/graph.ccb.intents.v1.0.0.json",
    )
)


class IntentGraphRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = IntentGraphRepository.load(GRAPH_PATH)

    def test_loads_verified_ccb_graph_and_exposes_version(self):
        self.assertEqual(self.repository.graph_id, "ccb-mobile-bank-intents")
        self.assertEqual(self.repository.version, "2.1.0")
        self.assertTrue(self.repository.digest.startswith("sha256:"))
        self.assertEqual(self.repository.menu_count, 96)

    def test_transfer_candidate_exposes_graph_slot_contract(self):
        candidate = self.repository.candidate_for_intent(
            "ccb.transfer.create", score=240, evidence=["test"]
        )

        self.assertEqual(candidate.slot_contract.version, "2.0")
        self.assertEqual(
            candidate.slot_contract.clarification_order,
            ("payee", "fromAccount", "amount"),
        )
        self.assertFalse(candidate.slot_contract.slot("purpose").required)
        prompt = candidate.to_prompt_dict()
        self.assertEqual(prompt["slotContract"]["slots"][0]["name"], "payee")

    def test_ready_candidate_exposes_versioned_skill_and_task_policy(self):
        candidate = self.repository.candidate_for_intent(
            "ccb.credit.card.loss-report", score=240, evidence=["test"]
        )

        prompt = candidate.to_prompt_dict()
        self.assertEqual(prompt["skillBinding"]["skillId"], "bank.credit-card-management")
        self.assertEqual(
            prompt["taskPolicy"]["supportedShapes"],
            ["single_intent", "same_intent_multi_task"],
        )

    def test_candidate_slot_contract_is_deeply_immutable(self):
        candidate = self.repository.candidate_for_intent(
            "ccb.transfer.create", score=240, evidence=["test"]
        )

        with self.assertRaises((TypeError, AttributeError)):
            candidate.slot_contract.slots[0].extraction_keys += ("payeeId",)

    def test_rejects_tampered_digest(self):
        payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        payload["nodes"][0]["title"] = "tampered"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "graph.json"
            target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(IntentGraphError, "digest"):
                IntentGraphRepository.load(target)

    def test_recall_uses_intent_semantics_and_page_context_without_defaulting_balance(self):
        card = self.repository.recall(
            "把全球支付卡挂失",
            page_context={"tab": "credit", "menuKey": "credit.card-manage"},
        )
        self.assertEqual(card[0].intent_id, "ccb.credit.card.loss-report")
        self.assertEqual(card[0].execution_mode, "confirm-required")
        self.assertEqual(card[0].capability_bindings[0], "bank.credit-card.list")
        self.assertIn("bank.credit-card.loss-report.create", card[0].capability_bindings)

        balance = self.repository.recall("查一下工资卡余额", page_context={"tab": "home"})
        self.assertEqual(balance[0].intent_id, "ccb.account.balance.query")
        self.assertEqual(balance[0].title, "账户查询·查询与管理")
        self.assertEqual(balance[0].to_prompt_dict()["title"], "账户查询·查询与管理")
        self.assertEqual(
            balance[0].capability_bindings,
            ["bank.account.list", "bank.account.balance.get", "bank.account.detail.get"],
        )
        self.assertEqual(self.repository.recall("今天天气怎样", page_context={}), [])

    def test_recall_keeps_bounded_candidates_for_multi_intent_and_negative_examples(self):
        candidates = self.repository.recall(
            "把全球支付卡挂失，再看看工资卡余额",
            page_context={"tab": "credit"},
        )
        ids = [item.intent_id for item in candidates]
        self.assertIn("ccb.credit.card.loss-report", ids)
        self.assertIn("ccb.account.balance.query", ids)
        self.assertLessEqual(len(candidates), 8)
        self.assertNotEqual(ids[0], "ccb.credit.card.transactions.query")

    def test_dragon_money_purchase_language_ranks_purchase_intent_first(self):
        candidates = self.repository.recall(
            "\u8d2d\u4e70\u5929\u5929\u5229\uff08\u9f99\u94b1\u5b9d\uff09",
            page_context={
                "tab": "wealth",
                "menuKey": "wealth.dragon-money",
            },
        )

        self.assertTrue(candidates)
        self.assertEqual(
            candidates[0].intent_id, "ccb.wealth.dragon-money.buy"
        )
        candidate_ids = {item.intent_id for item in candidates}
        # Graph expansion may add menu siblings (overview, faq) with lower
        # scores; verify they rank below the primary purchase intent.
        for item in candidates:
            if item.intent_id != "ccb.wealth.dragon-money.buy":
                self.assertLess(item.score, candidates[0].score)
        self.assertNotIn("ccb.account.balance.query", candidate_ids)

    def test_generic_wealth_discovery_prefers_product_list_not_purchase(self):
        candidates = self.repository.recall(
            "有哪些理财可以买",
            page_context={"tab": "wealth"},
        )

        self.assertTrue(candidates)
        self.assertEqual(
            candidates[0].intent_id,
            "ccb.wealth.wealth-product.overview",
        )
        self.assertNotIn(
            "ccb.wealth.dragon-money.buy",
            {item.intent_id for item in candidates},
        )

    def test_wealth_advice_catalogue_and_purchase_remain_separate(self):
        advice = self.repository.recall(
            "给我推荐理财产品",
            page_context={"tab": "wealth"},
        )
        catalogue = self.repository.recall(
            "有哪些在售理财产品",
            page_context={"tab": "wealth"},
        )
        purchase = self.repository.recall(
            "购买龙钱宝",
            page_context={"tab": "wealth", "menuKey": "wealth.dragon-money"},
        )

        self.assertEqual(advice[0].intent_id, "ccb.wealth.advisor.recommend")
        self.assertEqual(catalogue[0].intent_id, "ccb.wealth.wealth-product.overview")
        self.assertEqual(purchase[0].intent_id, "ccb.wealth.dragon-money.buy")
        self.assertEqual(advice[0].skill_binding.skill_id, "bank.wealth-advisory")
        self.assertEqual(
            advice[0].capability_bindings,
            ["bank.wealth.advisory.context.get"],
        )
        self.assertEqual(advice[0].orchestration_mode, "agent")

    def test_selected_wealth_plan_handoff_prefers_governed_purchase_intent(self):
        candidates = self.repository.recall(
            "申购理财产品：乾元安鑫 10000元，请逐项核验并进入申购前确认",
            page_context={"tab": "wealth"},
        )

        self.assertTrue(candidates)
        candidate = candidates[0]
        self.assertEqual(candidate.intent_id, "ccb.wealth.wealth-product.buy")
        self.assertEqual(candidate.execution_mode, "confirm-required")
        self.assertEqual(candidate.skill_binding.skill_id, "bank.wealth-management")
        self.assertEqual(
            candidate.capability_bindings,
            [
                "bank.wealth.product.list",
                "bank.wealth.product.detail.get",
                "bank.wealth.holding.list",
                "bank.wealth.transaction.list",
                "bank.wealth.salary-insights.get",
                "bank.wealth.recommendations.get",
                "bank.account.list",
                "bank.wealth.purchase.create",
            ],
        )
        self.assertEqual(
            candidate.slot_contract.clarification_order,
            ("productReference", "fromAccount", "amount"),
        )

    def test_explicit_advisory_goal_precedes_overlapping_catalogue_language(self):
        cases = (
            "有哪些理财产品推荐",
            "可以推荐几个理财产品吗",
            "理财产品里有什么适合我的",
        )

        for message in cases:
            with self.subTest(message=message):
                candidates = self.repository.recall(
                    message,
                    page_context={"tab": "wealth"},
                )

                self.assertTrue(candidates)
                self.assertEqual(
                    candidates[0].intent_id,
                    "ccb.wealth.advisor.recommend",
                )
                self.assertGreater(candidates[0].semantic_priority, 0)

        catalogue = self.repository.recall(
            "有哪些在售理财产品",
            page_context={"tab": "wealth"},
        )
        self.assertEqual(
            catalogue[0].intent_id,
            "ccb.wealth.wealth-product.overview",
        )
        self.assertEqual(catalogue[0].semantic_priority, 0)

    def test_runtime_capability_catalog_must_cover_every_recalled_binding(self):
        candidates = self.repository.recall("把信用卡挂失", page_context={"tab": "credit"})
        with self.assertRaisesRegex(IntentGraphError, "capability binding"):
            self.repository.validate_capability_bindings(candidates, {"bank.credit-card.list"})

    def test_projects_authoritative_candidate_and_registered_action_keys(self):
        candidate = self.repository.candidate_for_intent(
            "ccb.home.transfer.faq", score=240, evidence=["faq_pattern"]
        )

        self.assertEqual(candidate.intent_id, "ccb.home.transfer.faq")
        self.assertEqual(candidate.execution_mode, "knowledge-only")
        self.assertEqual(candidate.score, 240)
        self.assertEqual(candidate.evidence, ["faq_pattern"])
        self.assertEqual(
            self.repository.action_key_for_intent("ccb.home.transfer.faq"),
            "home.transfer.faq",
        )
        self.assertIn("home.transfer.apply", self.repository.action_keys)

    def test_loaded_graph_and_navigation_snapshots_are_deeply_immutable(self):
        transfer = next(
            node
            for node in self.repository._payload["nodes"]
            if node.get("id") == "ccb.transfer.create"
        )
        original = self.repository.action_key_for_intent("ccb.transfer.create")

        with self.assertRaises(TypeError):
            transfer["source"]["actionId"] = "execute"
        with self.assertRaises(AttributeError):
            transfer["aliases"].append("恶意别名")
        with self.assertRaises(TypeError):
            self.repository._intents_by_id["ccb.transfer.create"] = transfer
        with self.assertRaises(AttributeError):
            self.repository._action_keys.add("home.transfer.execute")
        with self.assertRaises(AttributeError):
            self.repository.version = "9.9.9"

        self.assertEqual(original, "home.transfer.apply")
        self.assertEqual(
            self.repository.action_key_for_intent("ccb.transfer.create"),
            "home.transfer.apply",
        )
        self.assertNotIn("home.transfer.execute", self.repository.action_keys)

    def test_missing_intent_binding_fails_closed(self):
        with self.assertRaisesRegex(IntentGraphError, "FAQ intent binding is missing"):
            self.repository.candidate_for_intent(
                "ccb.missing.faq", score=240, evidence=["faq_pattern"]
            )
        with self.assertRaisesRegex(IntentGraphError, "navigation intent binding is missing"):
            self.repository.action_key_for_intent("ccb.missing.navigation")

    def test_rejects_knowledge_only_intent_with_slots_or_capability_bindings(self):
        for field, value in (
            ("requiredSlots", ["accountId"]),
            ("capabilityBindings", ["bank.account.list"]),
        ):
            with self.subTest(field=field):
                payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
                node = next(
                    item for item in payload["nodes"]
                    if item["id"] == "ccb.home.transfer.faq"
                )
                node[field] = value
                unsigned = dict(payload)
                unsigned.pop("digest")
                canonical = json.dumps(
                    unsigned,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                payload["digest"] = "sha256:" + hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest()
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory) / "graph.json"
                    target.write_text(
                        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                    )
                    with self.assertRaisesRegex(
                        IntentGraphError, "knowledge-only intent isolation"
                    ):
                        IntentGraphRepository.load(target)


    def test_graph_expand_adds_sibling_intents_via_offers_edge(self):
        """Graph expansion adds menu siblings that lexical matching missed."""
        candidates = self.repository.recall(
            "\u8d2d\u4e70\u9f99\u94b1\u5b9d",
            page_context={"tab": "wealth", "menuKey": "wealth.dragon-money"},
        )
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].intent_id, "ccb.wealth.dragon-money.buy")
        expanded = [
            c for c in candidates if any("graph_expand" in e for e in c.evidence)
        ]
        self.assertTrue(expanded, "graph expansion must add sibling intents")
        for item in expanded:
            self.assertLess(item.score, candidates[0].score)

    def test_graph_expand_disabled_returns_lexical_only(self):
        """graph_expand=False preserves pre-expansion behaviour (rollback)."""
        enabled = self.repository.recall(
            "\u8d2d\u4e70\u9f99\u94b1\u5b9d",
            page_context={"tab": "wealth", "menuKey": "wealth.dragon-money"},
        )
        disabled = self.repository.recall(
            "\u8d2d\u4e70\u9f99\u94b1\u5b9d",
            page_context={"tab": "wealth", "menuKey": "wealth.dragon-money"},
            graph_expand=False,
        )
        for item in disabled:
            self.assertFalse(any("graph_expand" in e for e in item.evidence))
        self.assertGreaterEqual(len(enabled), len(disabled))


class IntentRecallHeldOutRegressionTest(unittest.TestCase):
    """留出回归：口语说法必须召回正确意图，寒暄必须零召回。

    留出性质（r8）：测试语句独立编写，setUpClass 断言每条语句都不是图谱
    aliases/recallTerms/title 的精确条目——图谱不得收录评测原句。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = IntentGraphRepository.load(GRAPH_PATH)
        payload = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.graph_terms: set[str] = set()
        for node in payload["nodes"]:
            if node.get("type") != "intent":
                continue
            cls.graph_terms.add(str(node.get("title") or ""))
            for field in ("aliases", "recallTerms"):
                for term in node.get(field) or []:
                    cls.graph_terms.add(str(term))

    def test_colloquial_paraphrases_reach_expected_intents(self):
        cases = [
            ("看下我卡里还有多少钱", "ccb.account.balance.query"),
            ("把钱转给我妈", "ccb.transfer.create"),
            ("我想看看理财收益", "ccb.wealth.dragon-money.overview"),
            ("信用卡丢了怎么挂失", "ccb.credit.card.loss-report"),
            ("帮我把零钱理财赎回来", "ccb.wealth.redeem"),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertNotIn(message, self.graph_terms)
                candidates = self.repository.recall(message, page_context={})
                self.assertTrue(candidates, "held-out utterance must recall lexically")
                self.assertEqual(candidates[0].intent_id, expected)

    def test_chitchat_produces_zero_recall(self):
        for message in ("你好", "在吗", "今天天气怎么样", "讲个笑话"):
            with self.subTest(message=message):
                self.assertEqual(self.repository.recall(message, page_context={}), [])


if __name__ == "__main__":
    unittest.main()
