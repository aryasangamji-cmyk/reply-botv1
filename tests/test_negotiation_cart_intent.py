import unittest

from app.negotiation_layer import _candidate_mentions, _fallback_cart_intent


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


if __name__ == "__main__":
    unittest.main()
