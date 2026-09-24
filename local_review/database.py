from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator, Optional

from .config import Settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def connect(settings: Settings) -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def migrate(settings: Settings) -> None:
    settings.assets_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    migrations_dir = Path(__file__).resolve().parent.parent / "cloudflare_review" / "migrations"
    with connect(settings) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS local_review_migrations("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM local_review_migrations"
            ).fetchall()
        }
        for migration in sorted(migrations_dir.glob("*.sql")):
            if migration.name in applied:
                continue
            connection.executescript(migration.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO local_review_migrations(name,applied_at) VALUES(?,?)",
                (migration.name, utc_now()),
            )
        connection.commit()


@contextmanager
def transaction(settings: Settings) -> Iterator[sqlite3.Connection]:
    connection = connect(settings)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def backup_if_due(settings: Settings, *, force: bool = False) -> Optional[Path]:
    migrate(settings)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    destination = settings.backups_dir / f"review-{day}.sqlite3"
    if destination.exists() and not force:
        return None
    temporary = destination.with_suffix(".sqlite3.tmp")
    source = connect(settings)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    temporary.replace(destination)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for path in settings.backups_dir.glob("review-*.sqlite3"):
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
            path.unlink()
    return destination


def remove_expired_originals(settings: Settings) -> int:
    removed = 0
    with transaction(settings) as connection:
        rows = connection.execute(
            "SELECT id,r2_key FROM assets WHERE kind='original' "
            "AND delete_after IS NOT NULL AND delete_after<=? LIMIT 100",
            (utc_now(),),
        ).fetchall()
        for row in rows:
            path = settings.assets_dir / str(row["r2_key"])
            if path.is_file():
                path.unlink()
            connection.execute("DELETE FROM assets WHERE id=?", (row["id"],))
            removed += 1
    return removed
