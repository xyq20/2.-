import unittest

from money_values import MoneyValueError, normalize_money_value


class MoneyValueTests(unittest.TestCase):
    def test_accepts_plain_currency_symbol_unit_and_grouping(self):
        self.assertEqual(normalize_money_value("690"), "690")
        self.assertEqual(normalize_money_value("690元"), "690")
        self.assertEqual(normalize_money_value("￥ 1,280.00 元"), "1280")

    def test_rejects_ambiguous_or_malformed_values(self):
        for value in ("690元起", "58,6", "面议", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(MoneyValueError):
                    normalize_money_value(value)


if __name__ == "__main__":
    unittest.main()
