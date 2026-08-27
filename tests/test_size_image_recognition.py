import dataclasses
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from size_image_recognition import (
    MIN_OCR_CONFIDENCE,
    OCRToken,
    RecognitionError,
    vision_ocr,
)


SAMPLE_IMAGE = Path(
    "/Volumes/共享文件/谭/products/绿巨人+NGBL-10588/尺码信息表/1_09(1).jpg"
)


def _completed_process(payload, *, returncode=0, stderr=""):
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class VisionOCRUnitTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.image_path = Path(self.temporary_directory.name) / "size chart.jpg"
        self.image_path.write_bytes(b"test image placeholder")

    @patch("size_image_recognition.subprocess.run")
    def test_returns_frozen_tokens_from_valid_payload(self, run):
        run.return_value = _completed_process(
            [
                {
                    "text": "WAISTLINE",
                    "confidence": 0.91,
                    "x": 0.1,
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.1,
                },
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

        self.assertEqual(
            tokens,
            (OCRToken("WAISTLINE", 0.91, 0.1, 0.2, 0.3, 0.1),),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            tokens[0].text = "changed"
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["xcrun", "swift"])
        self.assertIn("vision_ocr.swift", [Path(part).name for part in command])
        self.assertEqual(command[-1], str(self.image_path))
        self.assertEqual(
            run.call_args.kwargs,
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 60,
            },
        )

    @patch("size_image_recognition.subprocess.run")
    def test_reports_swift_process_error(self, run):
        run.return_value = _completed_process("", returncode=2, stderr="无法读取图片")

        with self.assertRaisesRegex(RecognitionError, "Vision OCR 执行失败.*无法读取图片"):
            vision_ocr(self.image_path)

    @patch("size_image_recognition.subprocess.run")
    def test_reports_timeout(self, run):
        run.side_effect = subprocess.TimeoutExpired(["xcrun", "swift"], 60)

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
    def test_rejects_malformed_token_schema(self, run):
        valid = {
            "text": "S",
            "confidence": 0.9,
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.1,
        }
        invalid_items = [
            {key: value for key, value in valid.items() if key != "height"},
            {**valid, "text": ""},
            {**valid, "confidence": "high"},
            {**valid, "extra": 1},
            "not an object",
        ]
        for item in invalid_items:
            with self.subTest(item=item):
                run.return_value = _completed_process([item])
                with self.assertRaisesRegex(RecognitionError, "OCR 输出格式无效"):
                    vision_ocr(self.image_path)

    @patch("size_image_recognition.subprocess.run")
    def test_rejects_nonfinite_and_out_of_range_numbers(self, run):
        valid = {
            "text": "S",
            "confidence": 0.9,
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.1,
        }
        invalid_items = [
            {**valid, "confidence": math.nan},
            {**valid, "confidence": 1.01},
            {**valid, "x": -0.01},
            {**valid, "y": math.inf},
            {**valid, "width": 1.01},
            {**valid, "height": -0.01},
            {**valid, "x": 0.9, "width": 0.2},
            {**valid, "y": 0.95, "height": 0.1},
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
    def test_rejects_missing_image_before_starting_swift(self, run):
        missing = Path(self.temporary_directory.name) / "missing.jpg"

        with self.assertRaisesRegex(RecognitionError, "OCR 图片不存在"):
            vision_ocr(missing)

        run.assert_not_called()


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("xcrun") is not None,
    "需要 macOS Vision 和 xcrun 才能运行本地 OCR 集成测试",
)
class VisionOCRIntegrationTests(unittest.TestCase):
    def test_local_vision_reads_meaningful_size_chart_labels(self):
        tokens = vision_ocr(SAMPLE_IMAGE)
        recognized = " ".join(token.text.upper() for token in tokens)
        compact = "".join(character for character in recognized if character.isalnum())
        normalized_tokens = {
            re.sub(r"[^A-Z0-9]", "", token.text.upper()) for token in tokens
        }

        for size in ("S", "M", "L", "XL", "2XL"):
            with self.subTest(size=size):
                self.assertIn(size, normalized_tokens)
        for row_label in ("WAISTLINE", "HIPLINE", "LENGTH"):
            with self.subTest(row_label=row_label):
                self.assertIn(row_label, compact)


if __name__ == "__main__":
    unittest.main()
