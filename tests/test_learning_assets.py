from datetime import datetime, timedelta, timezone
import hashlib
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from learning_assets import (
    LearningAssetError,
    build_learning_thumbnail,
    prepare_learning_assets,
)


class LearningAssetTests(unittest.TestCase):
    def _write_source(self, directory: str, width: int = 1200, height: int = 800) -> Path:
        source = Path(directory) / "source.jpg"
        x = np.arange(width, dtype=np.uint16)[None, :]
        y = np.arange(height, dtype=np.uint16)[:, None]
        image = np.empty((height, width, 3), dtype=np.uint8)
        image[:, :, 0] = (x % 251).astype(np.uint8)
        image[:, :, 1] = (y % 241).astype(np.uint8)
        image[:, :, 2] = ((x + y) % 239).astype(np.uint8)
        self.assertTrue(cv2.imwrite(str(source), image))
        return source

    def test_thumbnail_is_deterministic_jpeg_with_768_longest_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._write_source(directory)
            source_before = source.read_bytes()

            first = build_learning_thumbnail(source)
            second = build_learning_thumbnail(source)

            self.assertEqual(first, second)
            self.assertTrue(first.startswith(b"\xff\xd8"))
            decoded = cv2.imdecode(np.frombuffer(first, dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(decoded.shape[:2], (512, 768))
            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(
                hashlib.sha256(first).hexdigest(),
                hashlib.sha256(second).hexdigest(),
            )

    def test_thumbnail_does_not_upscale_small_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._write_source(directory, width=320, height=200)
            thumbnail = build_learning_thumbnail(source)

        decoded = cv2.imdecode(np.frombuffer(thumbnail, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape[:2], (200, 320))

    def test_prepared_assets_record_kind_hash_and_retention(self):
        now = datetime(2026, 9, 9, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            source = self._write_source(directory)
            source_bytes = source.read_bytes()

            original, thumbnail = prepare_learning_assets(source, now=now)

        self.assertEqual(original.kind, "original")
        self.assertEqual(original.body, source_bytes)
        self.assertEqual(original.content_type, "image/jpeg")
        self.assertEqual(original.sha256, hashlib.sha256(source_bytes).hexdigest())
        self.assertEqual(original.delete_after, (now + timedelta(days=30)).isoformat())
        self.assertEqual(thumbnail.kind, "learning_thumbnail")
        self.assertEqual(thumbnail.content_type, "image/jpeg")
        self.assertEqual(thumbnail.sha256, hashlib.sha256(thumbnail.body).hexdigest())
        self.assertIsNone(thumbnail.delete_after)

    def test_unreadable_image_raises_sanitized_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "broken.jpg"
            source.write_bytes(b"not an image")
            with self.assertRaises(LearningAssetError) as caught:
                build_learning_thumbnail(source)
        self.assertEqual(str(caught.exception), "无法读取商品图片")


if __name__ == "__main__":
    unittest.main()
