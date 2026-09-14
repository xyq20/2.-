import tempfile
import unittest
from pathlib import Path

from kuaimai_gui import ProductChoice, discover_products


class ProductDiscoveryTests(unittest.TestCase):
    def test_only_directories_with_excel_are_listed_in_natural_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "款10").mkdir()
            (root / "款10" / "产品信息.xlsx").write_bytes(b"xlsx")
            (root / "款2").mkdir()
            (root / "款2" / "产品信息.xlsx").write_bytes(b"xlsx")
            (root / "缺少Excel").mkdir()
            (root / "products.txt").write_text("ignore", encoding="utf-8")

            result = discover_products(root)

            self.assertEqual([item.display_name for item in result], ["款2", "款10"])
            self.assertEqual(result[0].excel_path, root / "款2" / "产品信息.xlsx")
            self.assertIsInstance(result[0], ProductChoice)

    def test_missing_root_returns_empty_tuple(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = discover_products(Path(temp_dir) / "not-found")

            self.assertEqual(result, ())


if __name__ == "__main__":
    unittest.main()
