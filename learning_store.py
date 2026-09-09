from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Optional, Tuple

from learning_models import (
    CandidateSnapshot,
    OutboxEvent,
    ProductFingerprint,
    RunCheckpoint,
    StageResult,
    canonical_json,
)


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LearningStore:
    """Local cache, recovery index, and offline outbox; never the authority."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._closed = False

    def migrate(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS products (
              product_version TEXT PRIMARY KEY,
              style_code TEXT NOT NULL,
              title TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS run_checkpoints (
              run_id TEXT PRIMARY KEY,
              product_version TEXT NOT NULL,
              execution_mode TEXT NOT NULL,
              platform_order_json TEXT NOT NULL,
              current_index INTEGER NOT NULL,
              status TEXT NOT NULL,
              pending_review_id TEXT,
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS stage_results (
              run_id TEXT NOT NULL,
              platform_id TEXT NOT NULL,
              status TEXT NOT NULL,
              expected_json TEXT NOT NULL,
              readback_json TEXT NOT NULL,
              verified INTEGER NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (run_id, platform_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS candidate_snapshots (
              snapshot_version TEXT PRIMARY KEY,
              platform_id TEXT NOT NULL,
              category_leaf_id TEXT NOT NULL,
              field_id TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sync_outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              idempotency_key TEXT NOT NULL UNIQUE,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              available_at TEXT NOT NULL,
              last_error TEXT,
              delivered_at TEXT
            )
            """,
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                self.connection.execute(statement)
            self.connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("schema_version", str(SCHEMA_VERSION)),
            )
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def schema_version(self) -> int:
        row = self.connection.execute(
            "SELECT value FROM schema_meta WHERE key=?", ("schema_version",)
        ).fetchone()
        return int(row["value"]) if row is not None else 0

    def upsert_product(self, fingerprint: ProductFingerprint) -> None:
        payload = canonical_json(asdict(fingerprint))
        with self.connection:
            self.connection.execute(
                "INSERT INTO products(product_version, style_code, title, payload_json, created_at) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(product_version) DO UPDATE SET "
                "style_code=excluded.style_code, title=excluded.title, "
                "payload_json=excluded.payload_json",
                (
                    fingerprint.product_version,
                    fingerprint.style_code,
                    fingerprint.title,
                    payload,
                    utc_now(),
                ),
            )

    def save_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO run_checkpoints("
                "run_id, product_version, execution_mode, platform_order_json, "
                "current_index, status, pending_review_id, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "current_index=excluded.current_index, status=excluded.status, "
                "pending_review_id=excluded.pending_review_id, updated_at=excluded.updated_at",
                (
                    checkpoint.run_id,
                    checkpoint.product_version,
                    checkpoint.execution_mode,
                    canonical_json(checkpoint.platform_order),
                    checkpoint.current_index,
                    checkpoint.status,
                    checkpoint.pending_review_id,
                    utc_now(),
                ),
            )

    def load_checkpoint(self, run_id: str) -> Optional[RunCheckpoint]:
        row = self.connection.execute(
            "SELECT run_id, product_version, execution_mode, platform_order_json, "
            "current_index, status, pending_review_id "
            "FROM run_checkpoints WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return RunCheckpoint(
            row["run_id"],
            row["product_version"],
            row["execution_mode"],
            tuple(json.loads(row["platform_order_json"])),
            row["current_index"],
            row["status"],
            row["pending_review_id"],
        )

    def record_stage(self, result: StageResult) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO stage_results("
                "run_id, platform_id, status, expected_json, readback_json, verified, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, platform_id) DO UPDATE SET "
                "status=excluded.status, expected_json=excluded.expected_json, "
                "readback_json=excluded.readback_json, verified=excluded.verified, "
                "updated_at=excluded.updated_at",
                (
                    result.run_id,
                    result.platform_id,
                    result.status,
                    canonical_json(result.expected),
                    canonical_json(result.readback),
                    int(result.verified),
                    utc_now(),
                ),
            )

    def completed_platforms(self, run_id: str) -> Tuple[str, ...]:
        checkpoint = self.load_checkpoint(run_id)
        if checkpoint is None:
            return ()
        rows = self.connection.execute(
            "SELECT platform_id FROM stage_results WHERE run_id=? AND verified=1",
            (run_id,),
        ).fetchall()
        verified = {row["platform_id"] for row in rows}
        return tuple(
            platform for platform in checkpoint.platform_order if platform in verified
        )

    def save_candidate_snapshot(self, snapshot: CandidateSnapshot) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO candidate_snapshots("
                "snapshot_version, platform_id, category_leaf_id, field_id, payload_json, created_at"
                ") VALUES(?, ?, ?, ?, ?, ?)",
                (
                    snapshot.snapshot_version,
                    snapshot.platform_id,
                    snapshot.category_leaf_id,
                    snapshot.field_id,
                    canonical_json(asdict(snapshot)),
                    utc_now(),
                ),
            )

    def enqueue(
        self,
        idempotency_key: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> int:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO sync_outbox("
                "idempotency_key, event_type, payload_json, available_at"
                ") VALUES(?, ?, ?, ?)",
                (idempotency_key, event_type, canonical_json(payload), utc_now()),
            )
        row = self.connection.execute(
            "SELECT id FROM sync_outbox WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("outbox insert did not return an event id")
        return int(row["id"])

    def pending_outbox(self, limit: int = 50) -> Tuple[OutboxEvent, ...]:
        rows = self.connection.execute(
            "SELECT id, idempotency_key, event_type, payload_json, attempts, "
            "available_at, last_error FROM sync_outbox "
            "WHERE delivered_at IS NULL AND available_at<=? ORDER BY id LIMIT ?",
            (utc_now(), limit),
        ).fetchall()
        return tuple(
            OutboxEvent(
                row["id"],
                row["idempotency_key"],
                row["event_type"],
                json.loads(row["payload_json"]),
                row["attempts"],
                row["available_at"],
                row["last_error"],
            )
            for row in rows
        )

    def mark_delivered(self, event_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE sync_outbox SET delivered_at=? WHERE id=?",
                (utc_now(), event_id),
            )

    def mark_retry(self, event_id: int, error: str, available_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE sync_outbox SET attempts=attempts+1, last_error=?, "
                "available_at=? WHERE id=?",
                (error[:200], available_at, event_id),
            )

    def close(self) -> None:
        if not self._closed:
            self.connection.close()
            self._closed = True
