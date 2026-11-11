import unittest

from app.card_reference_resolution import (
    CardReferenceResolutionError,
    resolve_card_reference,
)


class CardReferenceResolutionTests(unittest.TestCase):
    def setUp(self):
        self.cards = [
            {
                "id": "credit_global_8891",
                "productName": "龙卡全球支付卡",
                "maskedNo": "**** **** **** 8891",
                "status": "ACTIVE",
            },
            {
                "id": "credit_digital_2268",
                "productName": "龙卡数字信用卡",
                "maskedNo": "**** **** **** 2268",
                "status": "ACTIVE",
            },
        ]

    def test_generic_request_with_multiple_cards_returns_safe_replayable_choices(self):
        result = resolve_card_reference("还信用卡", self.cards)

        self.assertEqual(result.state, "ambiguous")
        self.assertIsNone(result.card_id)
        self.assertEqual([item.tail for item in result.options], ["8891", "2268"])
        self.assertEqual(result.options[0].reply_message, "选择尾号8891的信用卡")
        self.assertNotIn("credit_global_8891", repr(result))

    def test_name_or_tail_selects_one_active_card(self):
        by_name = resolve_card_reference("还龙卡全球支付卡", self.cards)
        by_tail = resolve_card_reference("选择尾号2268的信用卡", self.cards)

        self.assertEqual(by_name.card_id, "credit_global_8891")
        self.assertEqual(by_tail.card_id, "credit_digital_2268")
        self.assertEqual(by_name.semantic_reference, "龙卡全球支付卡 · 尾号8891")

    def test_single_active_card_is_selected_without_inventing_a_card_id(self):
        cards = [self.cards[0], {**self.cards[1], "status": "INACTIVE"}]
        result = resolve_card_reference("还信用卡", cards)

        self.assertEqual(result.state, "selected")
        self.assertEqual(result.card_id, "credit_global_8891")
        self.assertEqual(result.semantic_reference, "龙卡全球支付卡 · 尾号8891")

    def test_malformed_or_duplicate_provider_data_fails_closed(self):
        with self.assertRaises(CardReferenceResolutionError):
            resolve_card_reference("还信用卡", [{**self.cards[0], "maskedNo": "6222000011118891"}])
        with self.assertRaises(CardReferenceResolutionError):
            resolve_card_reference("还信用卡", [self.cards[0], self.cards[0]])


if __name__ == "__main__":
    unittest.main()
