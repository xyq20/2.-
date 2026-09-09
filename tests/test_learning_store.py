import dataclasses
from datetime import datetime, timedelta, timezone
import json
import tempfile
import unittest
from pathlib import Path

from learning_models import (
    CandidateSnapshot,
    CandidateValue,
    ProductFingerprint,
    RunCheckpoint,
    StageResult,
)
from learning_store import LearningStore


class LearningStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state" / "learning.sqlite3"
        self.store = LearningStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_schema_is_migrated_once_with_wal_and_foreign_keys(self):
        self.store.migrate()
        self.store.migrate()

        self.assertEqual(self.store.schema_version(), 2)
        journal_mode = self.store.connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(journal_mode.casefold(), "wal")
        self.assertEqual(foreign_keys, 1)

    def test_upsert_product_stores_canonical_payload_without_credentials(self):
        self.store.migrate()
        fingerprint = ProductFingerprint("NGBL-1", "标题", (), "version-1")

        self.store.upsert_product(fingerprint)
        row = self.store.connection.execute(
            "SELECT product_version, style_code, title, payload_json, created_at "
            "FROM products WHERE product_version=?",
            ("version-1",),
        ).fetchone()

        self.assertEqual(tuple(row[:3]), ("version-1", "NGBL-1", "标题"))
        self.assertEqual(json.loads(row["payload_json"])["assets"], [])
        self.assertEqual(datetime.fromisoformat(row["created_at"]).tzinfo, timezone.utc)
        self.assertNotIn("token", row["payload_json"].casefold())

    def test_checkpoint_round_trip_and_mutable_state_update(self):
        self.store.migrate()
        checkpoint = RunCheckpoint(
            "run-1", "product-1", "save_only", ("base", "tmall"), 0, "running"
        )
        self.store.save_checkpoint(checkpoint)
        self.store.save_checkpoint(
            dataclasses.replace(
                checkpoint, current_index=1, status="waiting_review", pending_review_id="r-1"
            )
        )

        self.assertEqual(
            self.store.load_checkpoint("run-1"),
            RunCheckpoint(
                "run-1",
                "product-1",
                "save_only",
                ("base", "tmall"),
                1,
                "waiting_review",
                "r-1",
                2,
            ),
        )
        self.assertIsNone(self.store.load_checkpoint("missing"))

    def test_checkpoint_version_increments_on_change_and_survives_restart(self):
        self.store.migrate()
        initial = RunCheckpoint(
            "run-1", "product-1", "save_only", ("base", "pdd"), 0, "running"
        )
        persisted = self.store.save_checkpoint(initial)
        self.assertEqual(persisted.version, 1)
        self.assertEqual(self.store.load_checkpoint("run-1").version, 1)

        self.store.save_checkpoint(initial)
        self.assertEqual(self.store.load_checkpoint("run-1").version, 1)

        persisted = self.store.save_checkpoint(
            dataclasses.replace(initial, current_index=1)
        )
        self.assertEqual(persisted.version, 2)
        self.assertEqual(self.store.load_checkpoint("run-1").version, 2)
        self.store.close()
        self.store = LearningStore(self.path)
        self.store.migrate()
        self.store.save_checkpoint(
            dataclasses.replace(initial, current_index=1, status="completed")
        )
        self.assertEqual(self.store.load_checkpoint("run-1").version, 3)

    def test_checkpoint_rejects_changed_recovery_identity(self):
        self.store.migrate()
        initial = RunCheckpoint(
            "run-1",
            "product-1",
            "save_only",
            ("base", "pdd"),
            0,
            "running",
            device_id="device-1",
            image_version="images-1",
        )
        self.store.save_checkpoint(initial)

        for changed in (
            dataclasses.replace(initial, product_version="product-2"),
            dataclasses.replace(initial, execution_mode="preview"),
            dataclasses.replace(initial, platform_order=("pdd",)),
            dataclasses.replace(initial, device_id="device-2"),
            dataclasses.replace(initial, image_version="images-2"),
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                ValueError, "recovery identity"
            ):
                self.store.save_checkpoint(changed)

        self.assertEqual(self.store.load_checkpoint("run-1"), initial)

    def test_device_id_is_opaque_and_stable_across_reopen(self):
        self.store.migrate()
        first = self.store.get_or_create_device_id()
        self.assertRegex(first, r"^[0-9a-f]{32}$")

        self.store.close()
        self.store = LearningStore(self.path)
        self.store.migrate()

        self.assertEqual(self.store.get_or_create_device_id(), first)

    def test_only_verified_stages_are_completed_in_checkpoint_order(self):
        self.store.migrate()
        self.store.save_checkpoint(
            RunCheckpoint(
                "run-1", "product-1", "save_only", ("tmall", "pdd"), 0, "running"
            )
        )
        self.store.record_stage(StageResult("run-1", "tmall", "saved", {}, {}, False))
        self.store.record_stage(
            StageResult("run-1", "pdd", "verified", {}, {"ok": True}, True)
        )

        self.assertEqual(self.store.completed_platforms("run-1"), ("pdd",))
        self.assertEqual(self.store.completed_platforms("missing"), ())

    def test_record_stage_updates_latest_readback(self):
        self.store.migrate()
        self.store.record_stage(StageResult("run-1", "tmall", "saved", {}, {}, False))
        self.store.record_stage(
            StageResult(
                "run-1", "tmall", "verified", {"fabric": "棉"}, {"fabric": "棉"}, True
            )
        )

        row = self.store.connection.execute(
            "SELECT status, expected_json, readback_json, verified "
            "FROM stage_results WHERE run_id=? AND platform_id=?",
            ("run-1", "tmall"),
        ).fetchone()
        self.assertEqual(row["status"], "verified")
        self.assertEqual(json.loads(row["expected_json"]), {"fabric": "棉"})
        self.assertEqual(json.loads(row["readback_json"]), {"fabric": "棉"})
        self.assertEqual(row["verified"], 1)

    def test_verified_stage_cannot_be_downgraded_or_rewritten(self):
        self.store.migrate()
        self.store.save_checkpoint(
            RunCheckpoint(
                "run-1", "product-1", "save_only", ("tmall",), 0, "running"
            )
        )
        verified = StageResult(
            "run-1", "tmall", "readback_verified", {"fabric": "cotton"},
            {"fabric": "cotton"}, True
        )
        self.store.record_stage(verified)
        self.store.record_stage(verified)

        for changed in (
            dataclasses.replace(verified, verified=False),
            dataclasses.replace(verified, readback={"fabric": "polyester"}),
            dataclasses.replace(verified, status="saved"),
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                ValueError, "cannot be rewritten"
            ):
                self.store.record_stage(changed)

        self.assertEqual(self.store.completed_platforms("run-1"), ("tmall",))

    def test_candidate_snapshot_is_saved_once_by_stable_version(self):
        self.store.migrate()
        snapshot = CandidateSnapshot(
            "tmall",
            "leaf-1",
            "fabric",
            "面料",
            (CandidateValue("cotton", "棉"),),
            "schema-1",
        )

        self.store.save_candidate_snapshot(snapshot)
        self.store.save_candidate_snapshot(snapshot)

        row = self.store.connection.execute(
            "SELECT snapshot_version, payload_json FROM candidate_snapshots"
        ).fetchone()
        count = self.store.connection.execute(
            "SELECT COUNT(*) FROM candidate_snapshots"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(row["snapshot_version"], snapshot.snapshot_version)
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["schema_version"], "schema-1")
        self.assertFalse(payload["custom_allowed"])

    def test_outbox_idempotency_survives_reopen(self):
        self.store.migrate()
        first = self.store.enqueue("same-key", "review.created", {"a": 1})
        self.store.close()
        self.store = LearningStore(self.path)
        self.store.migrate()
        second = self.store.enqueue("same-key", "review.created", {"a": 2})

        events = self.store.pending_outbox()
        self.assertEqual(first, second)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload, {"a": 1})

    def test_retry_and_delivery_update_only_selected_event(self):
        self.store.migrate()
        first = self.store.enqueue("key-1", "checkpoint.updated", {"run": "1"})
        second = self.store.enqueue("key-2", "stage.completed", {"run": "1"})
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

        self.store.mark_retry(first, "x" * 500, future)
        pending = self.store.pending_outbox()
        self.assertEqual(tuple(event.id for event in pending), (second,))
        retry_row = self.store.connection.execute(
            "SELECT attempts, last_error FROM sync_outbox WHERE id=?", (first,)
        ).fetchone()
        self.assertEqual(retry_row["attempts"], 1)
        self.assertEqual(len(retry_row["last_error"]), 200)

        self.store.mark_delivered(second)
        self.assertEqual(self.store.pending_outbox(), ())

    def test_sensitive_payload_keys_are_rejected_before_database_write(self):
        self.store.migrate()
        with self.assertRaisesRegex(ValueError, "sensitive field"):
            self.store.enqueue(
                "unsafe",
                "review.created",
                {"request": {"Authorization": "Device real-secret"}},
            )
        with self.assertRaisesRegex(ValueError, "sensitive field"):
            self.store.record_stage(
                StageResult(
                    "run-1",
                    "tmall",
                    "verified",
                    {},
                    {"access_token": "real-secret"},
                    True,
                )
            )

        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM stage_results").fetchone()[0],
            0,
        )

    def test_camel_case_and_fullwidth_sensitive_keys_are_rejected(self):
        self.store.migrate()
        for key in ("apiKey", "accessToken", "chromeProfile", "ＴＯＫＥＮ"):
            with self.subTest(key=key), self.assertRaisesRegex(
                ValueError, "sensitive field"
            ):
                self.store.enqueue(
                    f"unsafe-{key}", "review.created", {key: "must-not-store"}
                )


if __name__ == "__main__":
    unittest.main()
