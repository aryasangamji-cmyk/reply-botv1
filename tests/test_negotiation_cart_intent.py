import unittest

from app.negotiation_layer import _apply_reference_target, _candidate_mentions, _fallback_cart_intent


class CartIntentTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
        {
            "key": "batch:ayushi_ir",
            "name": "Ayushi Maam IR",
            "phrases": ["Ayushi IR", "Ayushi maam IR"],
            "source": "recent",
        },
        {
            "key": "batch:vision_psir",
            "name": "Vision IAS PSIR",
            "phrases": ["Vision PSIR", "PSIR"],
            "source": "recent",
        },
        {
            "key": "batch:jayant_economy",
            "name": "Jayant Economy",
            "phrases": ["Jayant sir"],
            "source": "current",
        },
        ]


    def test_target_match_keeps_subject_specific_candidate(self):
        self.assertEqual(_candidate_mentions("Ayushi maam IR wala chahiye", self.candidates), ["batch:ayushi_ir"])


    def test_replace_fallback_preserves_unrelated_cart_item(self):
        decision = _fallback_cart_intent(
        "Rahul wala nahi, Ayushi maam IR wala chahiye",
        ["batch:rahul_psir", "batch:jayant_economy"],
        self.candidates,
        None,
    )
        self.assertEqual(decision["action"], "REPLACE")
        self.assertTrue(decision["route_to_matcher"])
        self.assertEqual(decision["final_keys"], ["batch:jayant_economy"])


    def test_add_keeps_existing_items_and_deduplicates(self):
        decision = _fallback_cart_intent(
        "Ye bhi chahiye",
        ["batch:jayant_economy"],
        self.candidates,
        {"target_key": "batch:ayushi_ir"},
    )
        self.assertEqual(decision["action"], "ADD")
        self.assertEqual(decision["final_keys"], ["batch:jayant_economy", "batch:ayushi_ir"])

    def test_reference_in_bundle_does_not_collapse_cart(self):
        state = {"cart": [{"key": "a", "name": "A", "price": 300}, {"key": "b", "name": "B", "price": 500}]}
        updated, changed = _apply_reference_target(
            "/tmp/negotiation-test.sqlite3", "test-chat", state,
            {"target_key": "a", "target_name": "A", "target_price": 300},
        )
        self.assertFalse(changed)
        self.assertEqual([x["key"] for x in updated["cart"]], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
