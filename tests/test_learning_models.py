import dataclasses
import tempfile
import unittest
from pathlib import Path

from learning_models import (
    AssetFingerprint,
    CandidateSnapshot,
    CandidateValue,
    OutboxEvent,
    ProductFingerprint,
    RunCheckpoint,
    StageResult,
    canonical_json,
    canonical_sha256,
)


class LearningModelTests(unittest.TestCase):
    def test_canonical_json_and_hash_ignore_mapping_order(self):
        self.assertEqual(canonical_json({"b": 2, "a": 1}), '{"a":1,"b":2}')
        self.assertEqual(
            canonical_sha256({"b": 2, "a": 1}),
            canonical_sha256({"a": 1, "b": 2}),
        )

    def test_asset_fingerprint_records_bytes_size_path_and_role(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "1.jpg"
            image.write_bytes(b"first")
            asset = AssetFingerprint.from_path(image, role="detail")

        self.assertEqual(asset.path, str(image))
        self.assertEqual(asset.role, "detail")
        self.assertEqual(asset.size, 5)
        self.assertEqual(
            asset.sha256,
            "a7937b64b8caa58f03721bb6bacf5c78cb235febe0e7"
            "0b1b84cd99541461a08e",
        )

    def test_product_fingerprint_changes_when_image_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "1.jpg"
            image.write_bytes(b"first")
            first = ProductFingerprint.from_inputs("NGBL-1", "title", [image])
            image.write_bytes(b"second")
            second = ProductFingerprint.from_inputs("NGBL-1", "title", [image])

        self.assertNotEqual(first.product_version, second.product_version)
        self.assertEqual(first.assets[0].role, "main")

    def test_product_version_is_stable_for_same_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "1.jpg"
            image.write_bytes(b"same")
            first = ProductFingerprint.from_inputs("NGBL-1", "标题", [image])
            second = ProductFingerprint.from_inputs("NGBL-1", "标题", [image])

        self.assertEqual(first, second)

    def test_product_version_does_not_depend_on_local_image_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "first.jpg"
            second_path = root / "moved" / "second.jpg"
            second_path.parent.mkdir()
            first_path.write_bytes(b"same-image")
            second_path.write_bytes(b"same-image")
            first = ProductFingerprint.from_inputs(
                "NGBL-1", "标题", [first_path]
            )
            second = ProductFingerprint.from_inputs(
                "NGBL-1", "标题", [second_path]
            )

        self.assertEqual(first.product_version, second.product_version)
        self.assertEqual(first.image_version, second.image_version)

    def test_image_version_excludes_title_but_covers_image_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "1.jpg"
            image.write_bytes(b"same")
            first = ProductFingerprint.from_inputs("NGBL-1", "标题一", [image])
            renamed = ProductFingerprint.from_inputs("NGBL-1", "标题二", [image])
            image.write_bytes(b"changed")
            changed = ProductFingerprint.from_inputs("NGBL-1", "标题二", [image])

        self.assertEqual(first.image_version, renamed.image_version)
        self.assertNotEqual(first.image_version, changed.image_version)

    def test_candidate_snapshot_version_covers_schema_and_candidates(self):
        first = CandidateSnapshot(
            "tmall",
            "leaf-1",
            "fabric",
            "面料",
            (CandidateValue("cotton", "棉"),),
            "schema-1",
            False,
        )
        second = dataclasses.replace(first, custom_allowed=True)

        self.assertEqual(first.snapshot_version, first.snapshot_version)
        self.assertNotEqual(first.snapshot_version, second.snapshot_version)

    def test_checkpoint_version_is_positive_and_defaults_to_one(self):
        checkpoint = RunCheckpoint(
            "run", "product", "preview", ("pdd",), 0, "running"
        )
        self.assertEqual(checkpoint.version, 1)
        with self.assertRaises(ValueError):
            RunCheckpoint(
                "run", "product", "preview", ("pdd",), 0, "running", version=0
            )

    def test_all_wire_models_are_immutable_dataclasses(self):
        instances = (
            AssetFingerprint("/tmp/a", "main", "sha", 1),
            ProductFingerprint("code", "title", (), "version"),
            CandidateValue("id", "label"),
            CandidateSnapshot("p", "c", "f", "label", (), "schema"),
            RunCheckpoint("run", "product", "preview", ("p",), 0, "running"),
            StageResult("run", "p", "previewed", {}, {}, False),
            OutboxEvent(1, "key", "event", {}, 0, "2026-09-09T00:00:00+00:00", None),
        )
        for instance in instances:
            with self.subTest(model=type(instance).__name__):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    instance.unexpected = True


if __name__ == "__main__":
    unittest.main()
