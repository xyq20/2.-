import dataclasses
import unittest

from canonical_fields import (
    EvidenceKind,
    FieldMappingError,
    FieldPolicy,
    build_alias_registry,
    is_learning_managed_field,
    map_platform_field,
    policy_for,
)


class CanonicalFieldTests(unittest.TestCase):
    def test_gender_is_shared_across_direct_platform_labels(self):
        self.assertEqual(map_platform_field("tb", "适用性别"), "gender")
        self.assertEqual(map_platform_field("yz", "性别"), "gender")

    def test_registered_platform_aliases_map_exactly(self):
        expected = {
            ("wxsph", "裤长"): "pants_length",
            ("jd", "裤长"): "pants_length",
            ("tb", "裤长"): "pants_length",
            ("yz", "厚薄"): "thickness",
            ("jd", "厚度"): "thickness",
            ("douyin", "厚度"): "thickness",
            ("douyin", "上市时间"): "listing_time",
            ("tb", "库存扣减方式"): "stock_deduction",
            ("tb", "上架时间"): "listing_state",
            ("tb", "运费模板"): "freight_template",
            ("tm", "厚薄"): "thickness",
            ("xhs", "厚薄"): "thickness",
            ("tb", "厚薄"): "thickness",
            ("wxsph", "面料材质成分含量"): "material_percentage",
            ("wxsph", "材质成分"): "material_composition",
            ("jd", "颜色"): "color",
            ("jd", "弹力"): "elasticity",
            ("tb", "弹力"): "elasticity",
            ("tm", "弹力等级"): "elasticity",
            ("douyin", "弹力"): "elasticity",
            ("pdd", "弹力"): "elasticity",
            ("xhs", "弹力"): "elasticity",
            ("yz", "弹力"): "elasticity",
            ("wxsph", "弹力"): "elasticity",
        }

        for key, canonical_name in expected.items():
            with self.subTest(key=key):
                self.assertEqual(map_platform_field(*key), canonical_name)

    def test_mapping_normalizes_nfkc_whitespace_and_required_punctuation_only(self):
        self.assertEqual(map_platform_field("ｊｄ", "  ＊裤长： "), "pants_length")

        with self.assertRaises(FieldMappingError):
            map_platform_field("jd", "裤子长度")

    def test_unknown_field_requires_review(self):
        with self.assertRaises(FieldMappingError):
            map_platform_field("jd", "平台新增神秘字段")

    def test_only_registered_dynamic_fields_enter_learning_gate(self):
        self.assertTrue(is_learning_managed_field("tb", "裤长"))
        self.assertFalse(is_learning_managed_field("tb", "款式细节"))

    def test_duplicate_normalized_alias_is_rejected(self):
        with self.assertRaises(FieldMappingError):
            build_alias_registry(
                (
                    (("jd", "裤长"), "pants_length"),
                    (("ｊｄ", " ＊裤长："), "pants_length"),
                )
            )

    def test_field_policies_are_immutable_and_conservative(self):
        expected = {
            "pants_length": ((EvidenceKind.VISUAL,), True),
            "thickness": ((EvidenceKind.VISUAL,), True),
            "color": ((EvidenceKind.VISUAL,), True),
            "elasticity": ((EvidenceKind.TEXT,), True),
            "material_composition": ((EvidenceKind.TEXT,), True),
            "material_percentage": ((EvidenceKind.TEXT,), False),
            "listing_time": ((EvidenceKind.TEXT,), True),
            "stock_deduction": ((EvidenceKind.TEXT,), True),
            "listing_state": ((EvidenceKind.TEXT,), True),
            "freight_template": ((EvidenceKind.TEXT,), True),
        }

        for canonical_name, (evidence, allow_rule) in expected.items():
            with self.subTest(canonical_name=canonical_name):
                policy = policy_for(canonical_name)
                self.assertEqual(policy.required_evidence, evidence)
                self.assertEqual(policy.allow_rule, allow_rule)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            FieldPolicy("color", (EvidenceKind.VISUAL,), True).allow_rule = False

    def test_unknown_policy_is_rejected(self):
        with self.assertRaises(FieldMappingError):
            policy_for("category_specific_guess")


if __name__ == "__main__":
    unittest.main()
