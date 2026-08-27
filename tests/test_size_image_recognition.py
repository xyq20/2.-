import dataclasses
from concurrent.futures import ThreadPoolExecutor, wait
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import size_image_recognition
from size_image_recognition import (
    MIN_OCR_CONFIDENCE,
    OCRToken,
    RecognitionError,
    SkuRecommendation,
    parse_measurement_table,
    recognize_recommendations,
    vision_ocr,
)


FIXTURE_IMAGE = Path(__file__).with_name("fixtures") / "vision_ocr_oriented.jpg"
PRODUCT = Path("/Volumes/共享文件/谭/products/绿巨人+NGBL-10588")


def _token(text, x, y, *, width=0.04, height=0.04):
    return OCRToken(text, 1.0, x, y, width, height)


def _measurement_tokens(*, sizes=("S", "M")):
    tokens = [
        _token("腰围/WAISTLINE", 0.05, 0.30, width=0.20),
        _token("裤长/LENGTH", 0.05, 0.50, width=0.20),
        _token("臀围/HIPLINE", 0.05, 0.70, width=0.20),
    ]
    values = ((80, 104, 106), (84, 106, 110))
    for column, (size, column_values) in enumerate(zip(sizes, values)):
        x = 0.40 + column * 0.20
        tokens.append(_token(size, x, 0.10))
        for y, value in zip((0.30, 0.50, 0.70), column_values):
            tokens.append(_token(str(value), x, y))
    return tuple(tokens)


def _completed_process(payload="", *, returncode=0, stderr=""):
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class VisionOCRUnitTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.image_path = Path(self.temporary_directory.name) / "size chart.jpg"
        self.image_path.write_bytes(b"test image placeholder")
        bridge_patcher = patch("size_image_recognition._bridge_command")
        self.bridge_command = bridge_patcher.start()
        self.addCleanup(bridge_patcher.stop)
        self.bridge_command.return_value.__enter__.return_value = [
            "/cached/vision_ocr",
            str(self.image_path),
        ]

    @patch("size_image_recognition.subprocess.run")
    def test_returns_frozen_tokens_from_valid_payload(self, run):
        run.return_value = _completed_process(
            [
                {"text": "WAISTLINE", "confidence": 0.91, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1},
                {
                    "text": "noise",
                    "confidence": MIN_OCR_CONFIDENCE - 0.01,
                    "x": 0.0,
                    "y": 0.0,
                    "width": 0.1,
                    "height": 0.1,
                },
            ]
        )

        tokens = vision_ocr(self.image_path)

        self.assertEqual(tokens, (OCRToken("WAISTLINE", 0.91, 0.1, 0.2, 0.3, 0.1),))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            tokens[0].text = "changed"
        run.assert_called_once_with(
            ["/cached/vision_ocr", str(self.image_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @patch("size_image_recognition.subprocess.run")
    def test_reports_swift_process_error(self, run):
        run.return_value = _completed_process("", returncode=2, stderr="无法读取图片")
        with self.assertRaisesRegex(RecognitionError, "Vision OCR 执行失败.*无法读取图片"):
            vision_ocr(self.image_path)

    @patch("size_image_recognition.subprocess.run")
    def test_reports_timeout(self, run):
        run.side_effect = subprocess.TimeoutExpired(["/cached/vision_ocr"], 60)
        with self.assertRaisesRegex(RecognitionError, "Vision OCR 执行超时.*60"):
            vision_ocr(self.image_path)

    @patch("size_image_recognition.subprocess.run")
    def test_rejects_malformed_json_and_non_array_payload(self, run):
        for payload in ("not json", json.dumps({"text": "S"})):
            with self.subTest(payload=payload):
                run.return_value = _completed_process(payload)
                with self.assertRaisesRegex(RecognitionError, "OCR 输出.*JSON"):
                    vision_ocr(self.image_path)

    @patch("size_image_recognition.subprocess.run")
    def test_schema_error_names_missing_fields(self, run):
        run.return_value = _completed_process(
            [{"text": "S", "confidence": 0.9, "x": 0.1, "y": 0.2, "width": 0.3}]
        )
        with self.assertRaisesRegex(RecognitionError, "缺少字段：height"):
            vision_ocr(self.image_path)

    @patch("size_image_recognition.subprocess.run")
    def test_schema_error_names_unexpected_fields(self, run):
        run.return_value = _completed_process(
            [{"text": "S", "confidence": 0.9, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1, "extra": 1}]
        )
        with self.assertRaisesRegex(RecognitionError, "未知字段：extra"):
            vision_ocr(self.image_path)

    @patch("size_image_recognition.subprocess.run")
    def test_rejects_other_malformed_token_schema(self, run):
        valid = {"text": "S", "confidence": 0.9, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1}
        for item in ({**valid, "text": ""}, {**valid, "confidence": "high"}, "not an object"):
            with self.subTest(item=item):
                run.return_value = _completed_process([item])
                with self.assertRaisesRegex(RecognitionError, "OCR 输出格式无效"):
                    vision_ocr(self.image_path)

    @patch("size_image_recognition.subprocess.run")
    def test_rejects_nonfinite_and_out_of_range_numbers(self, run):
        valid = {"text": "S", "confidence": 0.9, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1}
        invalid_items = [
            {**valid, "confidence": math.nan}, {**valid, "confidence": 1.01},
            {**valid, "x": -0.01}, {**valid, "y": math.inf},
            {**valid, "width": 1.01}, {**valid, "height": -0.01},
            {**valid, "x": 0.9, "width": 0.2}, {**valid, "y": 0.95, "height": 0.1},
        ]
        for item in invalid_items:
            with self.subTest(item=item):
                run.return_value = _completed_process([item])
                with self.assertRaisesRegex(RecognitionError, "OCR 输出格式无效"):
                    vision_ocr(self.image_path)

    @patch("size_image_recognition.subprocess.run")
    def test_rejects_when_all_tokens_are_below_confidence_threshold(self, run):
        run.return_value = _completed_process(
            [
                {
                    "text": "noise",
                    "confidence": MIN_OCR_CONFIDENCE - 0.01,
                    "x": 0.1,
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.1,
                }
            ]
        )
        with self.assertRaisesRegex(RecognitionError, "OCR 未识别到可信文字"):
            vision_ocr(self.image_path)

    @patch("size_image_recognition.subprocess.run")
    def test_rejects_missing_image_before_starting_bridge(self, run):
        missing = Path(self.temporary_directory.name) / "missing.jpg"
        with self.assertRaisesRegex(RecognitionError, "OCR 图片不存在"):
            vision_ocr(missing)
        self.bridge_command.assert_not_called()
        run.assert_not_called()


class SwiftBridgeCompilationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.script = self.root / "vision_ocr.swift"
        self.script.write_text("import Vision\n", encoding="utf-8")
        self.toolchain = self._make_toolchain()

    def _make_toolchain(self):
        toolchain_usr = self.root / "SelectedXcode" / "Toolchains" / "XcodeDefault.xctoolchain" / "usr"
        resource = toolchain_usr / "lib" / "swift"
        resource.mkdir(parents=True)
        swift_include = toolchain_usr / "include" / "swift"
        swift_include.mkdir(parents=True)
        module_files = (
            ("module.modulemap", "module SwiftBridging {}"),
            ("bridging.modulemap", "module SwiftBridging {}"),
            ("bridging", ""),
        )
        for name, content in module_files:
            (swift_include / name).write_text(content, encoding="utf-8")
        for version in ("9.0.0", "17.0.0"):
            (toolchain_usr / "lib" / "clang" / version / "include").mkdir(parents=True)
        return size_image_recognition._Toolchain(
            swiftc=self.root / "SelectedXcode" / "usr" / "bin" / "swiftc",
            version="Apple Swift 6.1.2",
            target_info="target metadata",
            resource_path=resource,
            identity="active-toolchain-id",
        )

    @patch("size_image_recognition.subprocess.run")
    def test_toolchain_paths_come_from_active_xcrun_metadata(self, run):
        target_info = json.dumps(
            {"paths": {"runtimeResourcePath": str(self.toolchain.resource_path)}}
        )
        run.side_effect = [
            _completed_process(str(self.toolchain.swiftc)),
            _completed_process(self.toolchain.version),
            _completed_process(target_info),
        ]

        discovered = size_image_recognition._active_toolchain()

        self.assertEqual(discovered.swiftc, self.toolchain.swiftc)
        self.assertEqual(discovered.resource_path, self.toolchain.resource_path)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["xcrun", "--find", "swiftc"],
                ["xcrun", "swiftc", "--version"],
                ["xcrun", "swiftc", "-print-target-info"],
            ],
        )

    @patch("size_image_recognition.subprocess.run")
    def test_normal_compile_uses_active_xcrun_without_workaround(self, run):
        output = self.root / "vision_ocr"
        def compile_success(command, **_kwargs):
            Path(command[command.index("-o") + 1]).write_bytes(b"binary")
            return _completed_process()
        run.side_effect = compile_success

        size_image_recognition._compile_bridge(self.script, output, self.toolchain)

        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["xcrun", "swiftc"])
        self.assertNotIn("-resource-dir", command)
        self.assertEqual(run.call_count, 1)

    @patch("size_image_recognition.subprocess.run")
    def test_duplicate_module_map_error_retries_with_selected_toolchain_paths(self, run):
        output = self.root / "vision_ocr"
        def compile_attempt(command, **_kwargs):
            if run.call_count == 1:
                return _completed_process(
                    returncode=1,
                    stderr="error: redefinition of module 'SwiftBridging'",
                )
            Path(command[command.index("-o") + 1]).write_bytes(b"binary")
            return _completed_process()
        run.side_effect = compile_attempt

        size_image_recognition._compile_bridge(self.script, output, self.toolchain)

        first_command = run.call_args_list[0].args[0]
        retry_command = run.call_args_list[1].args[0]
        self.assertNotIn("-resource-dir", first_command)
        self.assertIn("-resource-dir", retry_command)
        clang_17 = self.toolchain.resource_path.parents[1] / "lib" / "clang" / "17.0.0" / "include"
        self.assertIn(str(clang_17), retry_command)
        self.assertNotIn("/Library/Developer/CommandLineTools", " ".join(retry_command))

    @patch("size_image_recognition.subprocess.run")
    def test_other_compile_error_does_not_retry(self, run):
        run.return_value = _completed_process(returncode=1, stderr="ordinary compiler error")
        with self.assertRaisesRegex(RecognitionError, "Swift OCR 编译失败.*ordinary"):
            size_image_recognition._compile_bridge(self.script, self.root / "vision_ocr", self.toolchain)
        self.assertEqual(run.call_count, 1)

    @patch("size_image_recognition._compile_bridge")
    @patch("size_image_recognition._active_toolchain")
    @patch("size_image_recognition._cache_directory")
    def test_cached_binary_is_atomically_reused(self, cache_directory, active_toolchain, compile_bridge):
        cache_directory.return_value = self.root / "cache"
        active_toolchain.return_value = self.toolchain
        compile_bridge.side_effect = (
            lambda _script, output, _toolchain: output.write_bytes(b"complete binary")
        )

        first = size_image_recognition._cached_bridge(self.script)
        second = size_image_recognition._cached_bridge(self.script)

        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), b"complete binary")
        self.assertEqual(compile_bridge.call_count, 1)
        self.assertNotEqual(compile_bridge.call_args.args[1], first)

    @patch("size_image_recognition._compile_bridge")
    @patch("size_image_recognition._active_toolchain")
    @patch("size_image_recognition._cache_directory")
    def test_concurrent_call_cannot_observe_partial_binary(
        self,
        cache_directory,
        active_toolchain,
        compile_bridge,
    ):
        cache_directory.return_value = self.root / "concurrent-cache"
        active_toolchain.return_value = self.toolchain
        compile_started = threading.Event()
        finish_compile = threading.Event()

        def delayed_compile(_script, output, _toolchain):
            output.write_bytes(b"partial")
            compile_started.set()
            self.assertTrue(finish_compile.wait(1))
            output.write_bytes(b"complete binary")

        compile_bridge.side_effect = delayed_compile
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(size_image_recognition._cached_bridge, self.script)
            self.assertTrue(compile_started.wait(1))
            second = executor.submit(size_image_recognition._cached_bridge, self.script)
            _, blocked = wait([second], timeout=0.05)
            self.assertEqual(blocked, {second})
            finish_compile.set()
            first_path = first.result(timeout=1)
            second_path = second.result(timeout=1)

        self.assertEqual(first_path, second_path)
        self.assertEqual(second_path.read_bytes(), b"complete binary")
        self.assertEqual(compile_bridge.call_count, 1)

    @patch("size_image_recognition._compile_bridge")
    @patch("size_image_recognition._active_toolchain")
    @patch("size_image_recognition._cached_bridge")
    def test_cache_failure_falls_back_to_transient_compilation(
        self,
        cached_bridge,
        active_toolchain,
        compile_bridge,
    ):
        cached_bridge.side_effect = size_image_recognition._CacheUnavailable("read-only cache")
        active_toolchain.return_value = self.toolchain
        compile_bridge.side_effect = (
            lambda _script, output, _toolchain: output.write_bytes(b"transient binary")
        )
        image = self.root / "image.jpg"
        image.write_bytes(b"image")

        with size_image_recognition._bridge_command(image) as command:
            transient_binary = Path(command[0])
            self.assertTrue(transient_binary.is_file())
            self.assertEqual(command[1], str(image))
        self.assertFalse(transient_binary.exists())


class SizeRecommendationParserTests(unittest.TestCase):
    @unittest.skipUnless(PRODUCT.is_dir(), "需要当前商品图片样例")
    def test_current_product_images_are_parsed_exactly(self):
        result = recognize_recommendations(
            PRODUCT / "尺码信息表/1_09(1).jpg",
            PRODUCT / "身高体重推荐表/1.jpg",
            ("S", "M", "L", "XL", "2XL"),
        )

        self.assertEqual(
            result,
            (
                SkuRecommendation("S", 155, 160, 50, 60, 80, 106, 104),
                SkuRecommendation("M", 155, 170, 50, 70, 84, 110, 106),
                SkuRecommendation("L", 155, 180, 50, 80, 88, 114, 108),
                SkuRecommendation("XL", 155, 190, 50, 90, 92, 118, 110),
                SkuRecommendation("2XL", 155, 200, 50, 100, 96, 122, 112),
            ),
        )

    def test_measurement_table_rejects_missing_and_extra_sizes(self):
        with self.assertRaisesRegex(
            RecognitionError,
            r"尺码信息表.*缺少.*M.*多出.*L",
        ):
            parse_measurement_table(
                _measurement_tokens(sizes=("S", "L")),
                ("S", "M"),
                source="尺码信息表 synthetic.jpg",
            )

    @patch("size_image_recognition.parse_height_weight_chart")
    @patch("size_image_recognition.parse_measurement_table")
    @patch("size_image_recognition.vision_ocr")
    def test_merge_rejects_nonmonotonic_measurements(
        self,
        ocr,
        parse_measurements,
        parse_height_weight,
    ):
        ocr.return_value = (_token("S", 0.1, 0.1),)
        parse_measurements.return_value = {
            "S": {"waist": 84, "hip": 110, "length": 106},
            "M": {"waist": 80, "hip": 106, "length": 104},
        }
        parse_height_weight.return_value = {
            "S": (155, 160, 50, 60),
            "M": (155, 170, 50, 70),
        }

        with self.assertRaisesRegex(RecognitionError, r"尺码顺序.*腰围.*递减"):
            recognize_recommendations("size.jpg", "height.jpg", ("S", "M"))


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("xcrun") is not None,
    "需要 macOS Vision 和 xcrun 才能运行本地 OCR 集成测试",
)
class VisionOCRIntegrationTests(unittest.TestCase):
    def test_oriented_fixture_recognizes_labels_and_top_left_y_coordinates(self):
        tokens = vision_ocr(FIXTURE_IMAGE)
        recognized = " ".join(token.text.upper() for token in tokens)
        compact = "".join(character for character in recognized if character.isalnum())

        for label in ("S", "M", "L", "XL", "2XL", "WAISTLINE", "HIPLINE", "LENGTH"):
            with self.subTest(label=label):
                self.assertRegex(
                    recognized,
                    rf"(?<![A-Z0-9]){re.escape(label)}(?![A-Z0-9])",
                )
        top = next(token for token in tokens if "TOP" in token.text.upper())
        bottom = next(token for token in tokens if "BOTTOM" in token.text.upper())
        self.assertLess(top.y, bottom.y)
        self.assertLess(top.y, 0.35)
        self.assertGreater(bottom.y, 0.65)
        for row_label in ("WAISTLINE", "HIPLINE", "LENGTH"):
            self.assertIn(row_label, compact)


if __name__ == "__main__":
    unittest.main()
