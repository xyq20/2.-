import importlib
import importlib.util
import unittest


def _load_registry(test_case):
    test_case.assertIsNotNone(
        importlib.util.find_spec("platform_registry"),
        "platform_registry must be implemented",
    )
    return importlib.import_module("platform_registry")


class PlatformRegistryTests(unittest.TestCase):
    def test_registry_has_fixed_order_and_capabilities(self):
        registry = _load_registry(self)

        actual = [
            (
                spec.cli_name,
                spec.platform_id,
                spec.display_name,
                spec.tab_label,
                spec.lifecycle,
                spec.enabled_in_all,
                spec.supports_inspect,
                spec.save_policy,
                spec.publish_allowed,
                spec.discovery_adapter,
            )
            for spec in registry.PLATFORM_SPECS
        ]
        self.assertEqual(
            actual,
            [
                ("base", "base", "基础资料", "基础资料", "implemented", False, False, "allowed", False, None),
                ("douyin", "fxg", "抖音资料", "抖音资料", "implemented", True, False, "allowed", True, None),
                ("taobao", "tb", "淘宝资料", "淘宝资料", "implemented", True, False, "explicit_once", True, None),
                ("tmall", "tm", "天猫资料", "天猫资料", "implemented", True, True, "allowed", True, "tm_listing:TmallListing"),
                ("pdd", "pdd", "拼多多资料", "拼多多资料", "implemented", True, True, "allowed", True, "pdd_listing:PddListing"),
                ("wxsph", "wxsph", "微信小店（视频号）资料", "微信小店（视频号）资料", "implemented", True, True, "allowed", True, "wxsph_listing:WxsphListing"),
                ("xhs", "xhs", "小红书资料", "小红书资料", "implemented", True, True, "allowed", True, "xhs_listing:XhsListing"),
                ("youzan", "yz", "有赞资料", "有赞资料", "implemented", True, False, "allowed", True, None),
                ("jd", "jd", "京东资料", "京东资料", "implemented", True, False, "allowed", True, None),
            ],
        )

    def test_cli_choices_include_all_and_public_names(self):
        registry = _load_registry(self)

        self.assertEqual(
            registry.platform_cli_choices(),
            ("all", "base", "douyin", "taobao", "tmall", "pdd", "wxsph", "xhs", "youzan", "jd"),
        )

    def test_lookup_supports_public_name_and_unique_platform_id(self):
        registry = _load_registry(self)

        self.assertEqual(registry.get_platform_spec("douyin").platform_id, "fxg")
        self.assertEqual(registry.get_platform_spec("tm").cli_name, "tmall")

    def test_all_expands_only_enabled_implemented_platforms(self):
        registry = _load_registry(self)

        self.assertEqual(
            tuple(spec.cli_name for spec in registry.expand_platform_selection("all")),
            ("douyin", "taobao", "tmall", "pdd", "wxsph", "xhs", "youzan", "jd"),
        )
        self.assertEqual(
            tuple(spec.cli_name for spec in registry.expand_platform_selection("taobao")),
            ("taobao",),
        )

    def test_unknown_platform_is_rejected(self):
        registry = _load_registry(self)

        with self.assertRaisesRegex(ValueError, "unknown platform"):
            registry.get_platform_spec("unknown")
        with self.assertRaisesRegex(ValueError, "unknown platform"):
            registry.expand_platform_selection("unknown")

    def test_combination_keeps_given_order_and_drops_duplicates(self):
        registry = _load_registry(self)

        self.assertEqual(
            tuple(
                spec.cli_name
                for spec in registry.expand_platform_selection("pdd,tmall,xhs")
            ),
            ("pdd", "tmall", "xhs"),
        )
        self.assertEqual(
            tuple(
                spec.cli_name
                for spec in registry.expand_platform_selection("jd,pdd,jd,pdd")
            ),
            ("jd", "pdd"),
        )
        self.assertEqual(
            tuple(
                spec.cli_name
                for spec in registry.expand_platform_selection("base,tmall,base")
            ),
            ("base", "tmall"),
        )

    def test_combination_accepts_mixed_separators_and_platform_ids(self):
        registry = _load_registry(self)

        for value in ("tmall,pdd", "tmall，pdd", "tmall、pdd", "tmall pdd"):
            with self.subTest(value=value):
                self.assertEqual(
                    tuple(
                        spec.cli_name
                        for spec in registry.expand_platform_selection(value)
                    ),
                    ("tmall", "pdd"),
                )
        self.assertEqual(
            tuple(
                spec.cli_name for spec in registry.expand_platform_selection("tm,fxg")
            ),
            ("tmall", "douyin"),
        )

    def test_combination_rejects_unknown_member(self):
        registry = _load_registry(self)

        with self.assertRaisesRegex(ValueError, "unknown platform"):
            registry.expand_platform_selection("tmall,unknown")
        with self.assertRaisesRegex(ValueError, "unknown platform"):
            registry.expand_platform_selection("")


class PlatformSelectionKindTests(unittest.TestCase):
    def test_only_literal_all_counts_as_all_selection(self):
        registry = _load_registry(self)

        self.assertTrue(registry.is_all_platform_selection("all"))
        self.assertTrue(registry.is_all_platform_selection(" all "))
        self.assertFalse(registry.is_all_platform_selection("base"))
        self.assertFalse(registry.is_all_platform_selection("tmall,pdd"))
        self.assertFalse(
            registry.is_all_platform_selection(
                "douyin,taobao,tmall,pdd,wxsph,xhs,youzan,jd"
            )
        )

    def test_empty_selection_is_not_treated_as_all(self):
        registry = _load_registry(self)

        with self.assertRaises(ValueError):
            registry.is_all_platform_selection("")


if __name__ == "__main__":
    unittest.main()
