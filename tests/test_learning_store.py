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

        self.assertEqual(self.store.schema_version(), 1)
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
            ),
        )
        self.assertIsNone(self.store.load_checkpoint("missing"))

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


if __name__ == "__main__":
    unittest.main()
