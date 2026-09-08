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
            ],
        )

    def test_cli_choices_include_all_and_public_names(self):
        registry = _load_registry(self)

        self.assertEqual(
            registry.platform_cli_choices(),
            ("all", "base", "douyin", "taobao", "tmall", "pdd", "wxsph", "xhs", "youzan"),
        )

    def test_lookup_supports_public_name_and_unique_platform_id(self):
        registry = _load_registry(self)

        self.assertEqual(registry.get_platform_spec("douyin").platform_id, "fxg")
        self.assertEqual(registry.get_platform_spec("tm").cli_name, "tmall")

    def test_all_expands_only_enabled_implemented_platforms(self):
        registry = _load_registry(self)

        self.assertEqual(
            tuple(spec.cli_name for spec in registry.expand_platform_selection("all")),
            ("douyin", "taobao", "tmall", "pdd", "wxsph", "xhs", "youzan"),
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


if __name__ == "__main__":
    unittest.main()
