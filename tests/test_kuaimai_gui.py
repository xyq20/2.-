import threading
import tempfile
import unittest
from pathlib import Path

from kuaimai_gui import (
    BatchItemResult,
    BatchStatus,
    ProductChoice,
    build_product_command,
    discover_products,
    discover_products_root,
    run_batch,
)


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


class BatchExecutionTests(unittest.TestCase):
    def _product(self, root: Path, name: str) -> ProductChoice:
        directory = root / name
        directory.mkdir()
        excel_path = directory / "产品信息.xlsx"
        excel_path.write_bytes(b"xlsx")
        return ProductChoice(directory, excel_path)

    def test_configured_root_is_preferred_when_it_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured = root / "configured-products"
            configured.mkdir()
            volumes = root / "Volumes"
            (volumes / "volume" / "products").mkdir(parents=True)

            result = discover_products_root(configured, volumes)

            self.assertEqual(result, configured.resolve())

    def test_command_uses_existing_all_platform_publish_entrypoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            product = self._product(Path(temp_dir), "款1")
            command = build_product_command(
                Path("/tmp/project/.venv/bin/python"),
                Path("/tmp/project/kuaimai_erp.py"),
                product,
            )

            self.assertEqual(
                command,
                [
                    "/tmp/project/.venv/bin/python",
                    "/tmp/project/kuaimai_erp.py",
                    "--excel-url",
                    str(product.excel_path),
                    "--platform",
                    "all",
                    "--save",
                ],
            )

    def test_batch_runs_selected_products_in_order_and_continues_after_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            products = [self._product(root, name) for name in ("一", "二", "三")]
            started = []

            def fake_runner(product, emit):
                started.append(product.display_name)
                emit(f"处理 {product.display_name}")
                return 2 if product.display_name == "二" else 0

            results = run_batch(products, fake_runner, threading.Event())

            self.assertEqual(started, ["一", "二", "三"])
            self.assertEqual(
                [item.status for item in results],
                [BatchStatus.COMPLETED, BatchStatus.FAILED, BatchStatus.COMPLETED],
            )
            self.assertTrue(all(isinstance(item, BatchItemResult) for item in results))

    def test_stop_event_marks_unstarted_products_without_running_them(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            products = [self._product(root, name) for name in ("一", "二", "三")]
            started = []
            stop_event = threading.Event()

            def fake_runner(product, emit):
                started.append(product.display_name)
                stop_event.set()
                return 0

            results = run_batch(products, fake_runner, stop_event)

            self.assertEqual(started, ["一"])
            self.assertEqual(
                [item.status for item in results],
                [BatchStatus.COMPLETED, BatchStatus.NOT_STARTED, BatchStatus.NOT_STARTED],
            )


if __name__ == "__main__":
    unittest.main()
