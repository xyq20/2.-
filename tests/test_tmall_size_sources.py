from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from size_image_recognition import RecognitionError, SizeLength, SkuRecommendation
from tmall_data import parse_tmall_fields
import tmall_size_sources


class TmallSizeParsingTests(unittest.TestCase):
    def test_reads_exact_size_field_and_splits_only_ascii_or_fullwidth_slashes(self):
        fields = parse_tmall_fields({"尺码": " S / M／38码 "})

        self.assertEqual(
            tmall_size_sources.parse_tmall_sizes(fields),
            ("S", "M", "38码"),
        )

    def test_does_not_fuzzily_use_another_excel_field(self):
        fields = parse_tmall_fields({"商品尺码": "S/M"})

        with self.assertRaisesRegex(
            tmall_size_sources.TmallSizeSourceError,
            "精确字段“尺码”",
        ):
            tmall_size_sources.parse_tmall_sizes(fields)

    def test_rejects_empty_or_duplicate_size_segments(self):
        invalid_values = ("S//M", "/S", "S/", "S/M/s")
        for value in invalid_values:
            with self.subTest(value=value):
                fields = parse_tmall_fields({"尺码": value})
                with self.assertRaises(tmall_size_sources.TmallSizeSourceError):
                    tmall_size_sources.parse_tmall_sizes(fields)


class TmallCategoryClassificationTests(unittest.TestCase):
    def test_classifies_confirmed_leaf_without_treating_socks_as_footwear(self):
        fixtures = (
            ("男装/休闲裤/工装休闲裤", "pants"),
            ("男装/外套/夹克", "clothing"),
            ("鞋靴/男鞋/休闲鞋", "footwear"),
            ("服饰配件/袜子/中筒袜", "generic"),
            ("鞋类配件/鞋垫", "generic"),
        )

        for category_path, expected in fixtures:
            with self.subTest(category_path=category_path):
                self.assertEqual(
                    tmall_size_sources.classify_tmall_category(category_path),
                    expected,
                )


class TmallOptionalImageTests(unittest.TestCase):
    def test_optional_height_weight_directory_may_be_absent_or_have_one_image(self):
        with tempfile.TemporaryDirectory() as directory:
            product_dir = Path(directory)
            self.assertIsNone(
                tmall_size_sources.discover_height_weight_image(product_dir)
            )
            image_dir = product_dir / "身高体重推荐表"
            image_dir.mkdir()
            image = image_dir / "recommendation.JPEG"
            image.write_bytes(b"fixture")
            (image_dir / "readme.txt").write_text("ignored", encoding="utf-8")

            self.assertEqual(
                tmall_size_sources.discover_height_weight_image(product_dir),
                image,
            )

    def test_multiple_height_weight_images_require_review(self):
        with tempfile.TemporaryDirectory() as directory:
            image_dir = Path(directory) / "身高体重推荐表"
            image_dir.mkdir()
            (image_dir / "1.jpg").write_bytes(b"one")
            (image_dir / "2.png").write_bytes(b"two")

            with self.assertRaises(
                tmall_size_sources.TmallSizeReviewRequired
            ) as captured:
                tmall_size_sources.discover_height_weight_image(Path(directory))

        self.assertEqual(captured.exception.reason_code, "ambiguous_height_weight_image")


class TmallSizeSourceResolutionTests(unittest.TestCase):
    @staticmethod
    def _fields(sizes: str = "S/M"):
        return parse_tmall_fields({"尺码": sizes})

    @staticmethod
    def _image(directory: Path, child: str, name: str = "1.jpg") -> Path:
        image_dir = directory / child
        image_dir.mkdir(parents=True, exist_ok=True)
        image = image_dir / name
        image.write_bytes(b"fixture")
        return image

    def test_pants_fixture_uses_recommendation_ocr_and_keeps_full_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            product_dir = Path(directory)
            size_chart = self._image(product_dir, "尺码信息表")
            height_weight = self._image(product_dir, "身高体重推荐表")
            recognized = (
                SkuRecommendation("S", 155, 160, 45, 50, 68, 96, 99),
                SkuRecommendation("M", 160, 165, 50, 55, 72, 100, 100),
            )
            with patch(
                "tmall_size_sources.recognize_recommendations",
                return_value=recognized,
            ) as recognize, patch(
                "tmall_size_sources.recognize_size_lengths"
            ) as recognize_lengths:
                result = tmall_size_sources.resolve_tmall_size_sources(
                    self._fields(),
                    "男装 > 休闲裤 > 工装休闲裤",
                    product_dir=product_dir,
                    size_chart_image=size_chart,
                )

        recognize.assert_called_once_with(size_chart, height_weight, ("S", "M"))
        recognize_lengths.assert_not_called()
        self.assertEqual(result.category_kind, "pants")
        self.assertEqual(
            result.headers,
            (
                "尺码",
                "身高(cm)",
                "体重(kg)",
                "腰围(cm)",
                "臀围(cm)",
                "裤长(cm)",
            ),
        )
        self.assertEqual(
            result.rows[0],
            {
                "尺码": "S",
                "身高(cm)": (155, 160),
                "体重(kg)": (45, 50),
                "腰围(cm)": 68,
                "臀围(cm)": 96,
                "裤长(cm)": 99,
            },
        )

    def test_pants_without_height_weight_image_is_review_required_before_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            product_dir = Path(directory)
            size_chart = self._image(product_dir, "尺码信息表")
            with patch("tmall_size_sources.recognize_recommendations") as recognize:
                with self.assertRaises(
                    tmall_size_sources.TmallSizeReviewRequired
                ) as captured:
                    tmall_size_sources.resolve_tmall_size_sources(
                        self._fields(),
                        "工装休闲裤",
                        product_dir=product_dir,
                        size_chart_image=size_chart,
                    )

        recognize.assert_not_called()
        self.assertEqual(captured.exception.reason_code, "missing_height_weight_image")

    def test_coat_fixture_uses_clothing_length_ocr_only(self):
        with tempfile.TemporaryDirectory() as directory:
            product_dir = Path(directory)
            size_chart = self._image(product_dir, "尺码信息表")
            recognized = (SizeLength("S", 68), SizeLength("M", 70))
            with patch(
                "tmall_size_sources.recognize_size_lengths",
                return_value=recognized,
            ) as recognize_lengths, patch(
                "tmall_size_sources.recognize_recommendations"
            ) as recognize_recommendations:
                result = tmall_size_sources.resolve_tmall_size_sources(
                    self._fields(),
                    "男装/外套/夹克",
                    product_dir=product_dir,
                    size_chart_image=size_chart,
                )

        recognize_lengths.assert_called_once_with(size_chart, ("S", "M"), "clothing")
        recognize_recommendations.assert_not_called()
        self.assertEqual(result.category_kind, "clothing")
        self.assertEqual(result.headers, ("尺码", "衣长(cm)"))
        self.assertEqual(
            result.rows,
            (
                {"尺码": "S", "衣长(cm)": 68},
                {"尺码": "M", "衣长(cm)": 70},
            ),
        )

    def test_shoe_fixture_normalizes_only_numeric_ma_suffix_and_never_calls_ocr(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "tmall_size_sources.recognize_size_lengths"
        ) as recognize_lengths, patch(
            "tmall_size_sources.recognize_recommendations"
        ) as recognize_recommendations:
            result = tmall_size_sources.resolve_tmall_size_sources(
                self._fields("38码/39码/均码"),
                "男鞋/休闲鞋",
                product_dir=Path(directory),
            )

        recognize_lengths.assert_not_called()
        recognize_recommendations.assert_not_called()
        self.assertEqual(result.category_kind, "footwear")
        self.assertEqual(result.headers, ("尺码",))
        self.assertEqual(result.sizes, ("38", "39", "均码"))
        self.assertEqual(
            result.rows,
            ({"尺码": "38"}, {"尺码": "39"}, {"尺码": "均码"}),
        )

    def test_shoe_fixture_rejects_duplicate_after_numeric_suffix_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                tmall_size_sources.TmallSizeSourceError,
                "归一化后重复",
            ):
                tmall_size_sources.resolve_tmall_size_sources(
                    self._fields("38码/38"),
                    "男鞋/休闲鞋",
                    product_dir=Path(directory),
                )

    def test_socks_fixture_is_generic_and_never_calls_ocr(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "tmall_size_sources.recognize_size_lengths"
        ) as recognize_lengths, patch(
            "tmall_size_sources.recognize_recommendations"
        ) as recognize_recommendations:
            result = tmall_size_sources.resolve_tmall_size_sources(
                self._fields("均码"),
                "袜子/中筒袜",
                product_dir=Path(directory),
            )

        recognize_lengths.assert_not_called()
        recognize_recommendations.assert_not_called()
        self.assertEqual(result.category_kind, "generic")
        self.assertEqual(result.rows, ({"尺码": "均码"},))

    def test_ocr_failure_is_exposed_as_review_required(self):
        with tempfile.TemporaryDirectory() as directory:
            product_dir = Path(directory)
            size_chart = self._image(product_dir, "尺码信息表")
            with patch(
                "tmall_size_sources.recognize_size_lengths",
                side_effect=RecognitionError("fixture unreadable"),
            ):
                with self.assertRaises(
                    tmall_size_sources.TmallSizeReviewRequired
                ) as captured:
                    tmall_size_sources.resolve_tmall_size_sources(
                        self._fields(),
                        "外套",
                        product_dir=product_dir,
                        size_chart_image=size_chart,
                    )

        self.assertEqual(captured.exception.reason_code, "size_ocr_failed")


if __name__ == "__main__":
    unittest.main()
