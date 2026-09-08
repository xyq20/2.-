import unittest

from youzan_data import parse_youzan_fields


class YouzanDataTests(unittest.TestCase):
    def test_preserves_fields_and_ordered_category(self):
        parsed = parse_youzan_fields(
            {
                "商品分类": "休闲裤/男士休闲直筒裤/工装休闲裤",
                "价格/一口价": 586.0,
                "数量": 100.0,
            }
        )

        self.assertEqual(
            parsed.category_path,
            ("休闲裤", "男士休闲直筒裤", "工装休闲裤"),
        )
        self.assertEqual(parsed.fields["价格/一口价"], "586")
        self.assertEqual(parsed.fields["数量"], "100")
        self.assertEqual(parsed.garment_kind, "pants")

    def test_classifies_coat_and_preserves_unknown_kind(self):
        coat = parse_youzan_fields({"商品分类": "男装/外套/皮衣"})
        self.assertEqual(coat.garment_kind, "coat")

        unknown = parse_youzan_fields({"商品分类": "男装"})
        self.assertEqual(unknown.garment_kind, "unknown")

    def test_missing_category_is_deferred_to_youzan_preflight(self):
        parsed = parse_youzan_fields({"价格": "586"})

        self.assertEqual(parsed.category_path, ())
        self.assertEqual(parsed.garment_kind, "unknown")

    def test_fields_are_immutable(self):
        parsed = parse_youzan_fields({"商品分类": "休闲裤"})

        with self.assertRaises(TypeError):
            parsed.fields["价格"] = "1"


if __name__ == "__main__":
    unittest.main()
