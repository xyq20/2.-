import unittest

from wxsph_data import parse_wxsph_fields


class WxsphDataTests(unittest.TestCase):
    def test_preserves_nonempty_excel_fields(self):
        parsed = parse_wxsph_fields(
            {
                "价格/市场价/售卖价": 586,
                "数量": 100.0,
                "材质成分/材质": "棉（100%）",
                "空值": "  ",
            }
        )

        self.assertEqual(parsed.fields["价格/市场价/售卖价"], "586")
        self.assertEqual(parsed.fields["数量"], "100")
        self.assertEqual(parsed.fields["材质成分/材质"], "棉（100%）")
        self.assertNotIn("空值", parsed.fields)


if __name__ == "__main__":
    unittest.main()
