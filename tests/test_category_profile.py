import unittest

from category_profile import (
    category_profile,
    category_search_terms,
    choose_category_candidate,
    preferred_category_leaf,
)


class CategoryProfileTests(unittest.TestCase):
    def test_classifies_existing_pants_and_new_outerwear_without_product_codes(self):
        self.assertEqual(
            category_profile(("男装", "男士休闲裤", "男士休闲直筒裤")).garment_kind,
            "pants",
        )
        self.assertEqual(
            category_profile(("夹克", "外套", "男士休闲夹克", "其他夹克")).garment_kind,
            "clothing",
        )

    def test_keeps_unknown_future_categories_generic(self):
        profile = category_profile(("数码配件",), "新品")
        self.assertEqual(profile.garment_kind, "generic")
        self.assertFalse(profile.supports_letter_size_chart)

    def test_ranks_exact_category_hints_by_specificity(self):
        hints = ("夹克", "外套", "男士休闲夹克", "其他夹克")
        self.assertEqual(
            category_search_terms(hints),
            ("男士休闲夹克", "其他夹克", "夹克", "外套"),
        )
        self.assertEqual(preferred_category_leaf(hints), "男士休闲夹克")

    def test_category_choice_uses_excel_gender_when_recommendations_share_leaf(self):
        hints = ("夹克", "外套", "男士休闲夹克", "其他夹克")
        candidates = (
            "童装/婴儿装/亲子装 > 外套/夹克/大衣 > 夹克/皮衣",
            "男装 > 夹克",
        )
        self.assertEqual(
            choose_category_candidate(candidates, hints),
            ("男装 > 夹克", "excel_hint_exact"),
        )

    def test_category_choice_rejects_wrong_gender_when_only_leaf_matches(self):
        hints = ("夹克", "外套", "男士休闲夹克", "其他夹克")
        self.assertEqual(
            choose_category_candidate(
                ("童装/婴儿装/亲子装 > 外套/夹克/大衣 > 夹克/皮衣",),
                hints,
            ),
            ("", ""),
        )


if __name__ == "__main__":
    unittest.main()
