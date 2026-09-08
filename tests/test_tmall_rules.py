import importlib.util
import unittest
from datetime import date, datetime, timezone

import tmall_rules


class TmallRulesModuleTests(unittest.TestCase):
    def test_tmall_rules_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("tmall_rules"))

    def test_rule_error_is_a_value_error(self):
        error_type = getattr(tmall_rules, "TmallRuleError", None)
        self.assertIsNotNone(error_type)
        self.assertTrue(issubclass(error_type, ValueError))


class TmallSpecValueRulesTests(unittest.TestCase):
    def _normalizer(self):
        normalizer = getattr(tmall_rules, "normalize_tmall_spec_values", None)
        self.assertTrue(callable(normalizer))
        return normalizer

    def test_footwear_size_dimension_removes_one_suffix_from_numeric_sizes_only(self):
        actual = self._normalizer()(
            ("38码", "38.5码", "39", "均码", "38码码", "尺码38码"),
            is_footwear=True,
            is_size_dimension=True,
        )

        self.assertEqual(
            actual,
            ("38", "38.5", "39", "均码", "38码码", "尺码38码"),
        )

    def test_non_footwear_values_are_unchanged(self):
        actual = self._normalizer()(
            ("38码", "38.5码", "均码"),
            is_footwear=False,
            is_size_dimension=True,
        )

        self.assertEqual(actual, ("38码", "38.5码", "均码"))

    def test_non_size_dimension_values_are_unchanged(self):
        actual = self._normalizer()(
            ("38码", "39码"),
            is_footwear=True,
            is_size_dimension=False,
        )

        self.assertEqual(actual, ("38码", "39码"))

    def test_duplicate_created_by_size_normalization_is_rejected(self):
        error_type = getattr(tmall_rules, "TmallRuleError", ValueError)

        with self.assertRaisesRegex(error_type, "38"):
            self._normalizer()(
                ("38码", "38"),
                is_footwear=True,
                is_size_dimension=True,
            )


class ShanghaiTodayRulesTests(unittest.TestCase):
    def _today(self):
        today = getattr(tmall_rules, "shanghai_today", None)
        self.assertTrue(callable(today))
        return today

    def test_aware_datetime_is_converted_to_shanghai_before_taking_date(self):
        instant = datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc)

        self.assertEqual(self._today()(instant), date(2026, 9, 2))

    def test_naive_datetime_is_rejected(self):
        error_type = getattr(tmall_rules, "TmallRuleError", ValueError)

        with self.assertRaisesRegex(error_type, "aware"):
            self._today()(datetime(2026, 9, 2, 0, 30))


class SizeParameterRulesTests(unittest.TestCase):
    def _selector(self):
        selector = getattr(tmall_rules, "select_size_parameters", None)
        self.assertTrue(callable(selector))
        return selector

    def test_selects_normalized_exact_intersection_in_page_order(self):
        selected = self._selector()(
            page_supported=(
                "身高（cm）",
                "裤长 ( cm )",
                "裤侧长(cm)",
                "体重（kg）",
            ),
            source_headers=(" 体重 ( KG ) ", "身高(cm)", "裤长(cm)", "裤侧长"),
            required_parameters=("身高(cm)", "体重(kg)"),
        )

        self.assertEqual(selected, ("身高（cm）", "裤长 ( cm )", "体重（kg）"))

    def test_similar_but_not_exact_header_is_not_guessed(self):
        selected = self._selector()(
            page_supported=("裤长(cm)", "裤侧长(cm)"),
            source_headers=("裤长",),
            required_parameters=(),
        )

        self.assertEqual(selected, ())

    def test_missing_source_for_page_required_parameter_is_rejected(self):
        error_type = getattr(tmall_rules, "TmallRuleError", ValueError)

        with self.assertRaisesRegex(error_type, "身高"):
            self._selector()(
                page_supported=("身高(cm)", "体重(kg)"),
                source_headers=("体重(kg)",),
                required_parameters=("身高(cm)",),
            )


class ConditionalFieldRulesTests(unittest.TestCase):
    def _resolver(self):
        resolver = getattr(tmall_rules, "new_product_declaration_value", None)
        self.assertTrue(callable(resolver))
        return resolver

    def test_returns_exact_yes_when_new_product_field_is_present(self):
        self.assertEqual(
            self._resolver()(("发票", " 是否申报新品 ", "发布类型")),
            "是",
        )

    def test_returns_none_when_new_product_field_is_absent(self):
        self.assertIsNone(self._resolver()(("发票", "发布类型")))

    def test_does_not_treat_longer_similar_label_as_the_field(self):
        self.assertIsNone(self._resolver()(("是否申报新品说明",)))


if __name__ == "__main__":
    unittest.main()
