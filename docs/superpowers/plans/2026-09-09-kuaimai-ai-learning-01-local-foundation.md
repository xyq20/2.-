# Kuaimai AI Learning Phase 1: Local Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local SQLite-backed product fingerprint, checkpoint, verified-readback, and offline outbox foundation without changing current listing behavior.

**Architecture:** New focused Python modules sit beside the existing flat modules. `learning_models.py` owns immutable wire models, `learning_store.py` owns SQLite transactions, and `learning_client.py` owns idempotent Cloudflare HTTP calls. `kuaimai_erp.py` only creates the run context and records current stage results behind an explicit feature flag.

**Tech Stack:** Python 3, standard-library `sqlite3`, `hashlib`, `urllib.request`, dataclasses, `unittest`.

---

## File map

- Create `learning_models.py`: immutable product, checkpoint, stage, outbox and candidate snapshot models.
- Create `learning_store.py`: schema creation and transactional repository.
- Create `learning_client.py`: authenticated JSON client and outbox delivery.
- Create `learning_assets.py`: deterministic compressed learning thumbnails.
- Create `tests/test_learning_models.py`: canonical JSON and fingerprint tests.
- Create `tests/test_learning_store.py`: SQLite, idempotency and recovery tests.
- Create `tests/test_learning_client.py`: HTTP response and retry classification tests.
- Create `tests/test_learning_assets.py`: thumbnail size and hash tests.
- Modify `kuaimai_erp.py`: opt-in context creation and stage recording only.
- Modify `tests/test_kuaimai_erp.py`: prove disabled mode is unchanged and enabled mode records stages.
- Modify `.gitignore`: exclude local state database and Cloudflare build state.
- Modify `README.md`: document local state location and feature flag.

### Task 1: Define stable local wire models and product fingerprints

**Files:**
- Create: `learning_models.py`
- Create: `tests/test_learning_models.py`

- [ ] **Step 1: Write failing canonical serialization tests**

```python
import tempfile
import unittest
from pathlib import Path

from learning_models import AssetFingerprint, ProductFingerprint, canonical_sha256


class LearningModelTests(unittest.TestCase):
    def test_canonical_sha256_ignores_mapping_order(self):
        self.assertEqual(
            canonical_sha256({"b": 2, "a": 1}),
            canonical_sha256({"a": 1, "b": 2}),
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
```

- [ ] **Step 2: Run the tests and verify the module is absent**

Run: `.venv/bin/python -m unittest tests.test_learning_models -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'learning_models'`.

- [ ] **Step 3: Implement immutable models and canonical hashing**

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AssetFingerprint:
    path: str
    role: str
    sha256: str
    size: int

    @classmethod
    def from_path(cls, path: Path, role: str = "main") -> "AssetFingerprint":
        data = path.read_bytes()
        return cls(str(path), role, hashlib.sha256(data).hexdigest(), len(data))


@dataclass(frozen=True)
class ProductFingerprint:
    style_code: str
    title: str
    assets: Tuple[AssetFingerprint, ...]
    product_version: str

    @classmethod
    def from_inputs(
        cls, style_code: str, title: str, image_paths: Iterable[Path]
    ) -> "ProductFingerprint":
        assets = tuple(AssetFingerprint.from_path(path) for path in image_paths)
        payload = {
            "style_code": style_code,
            "title": title,
            "assets": [asdict(asset) for asset in assets],
        }
        return cls(style_code, title, assets, canonical_sha256(payload))


@dataclass(frozen=True)
class CandidateValue:
    value_id: str
    label: str


@dataclass(frozen=True)
class CandidateSnapshot:
    platform_id: str
    category_leaf_id: str
    field_id: str
    field_label: str
    values: Tuple[CandidateValue, ...]
    schema_version: str
    custom_allowed: bool = False

    @property
    def snapshot_version(self) -> str:
        return canonical_sha256(asdict(self))


@dataclass(frozen=True)
class RunCheckpoint:
    run_id: str
    product_version: str
    execution_mode: str
    platform_order: Tuple[str, ...]
    current_index: int
    status: str
    pending_review_id: Optional[str] = None


@dataclass(frozen=True)
class StageResult:
    run_id: str
    platform_id: str
    status: str
    expected: Mapping[str, Any]
    readback: Mapping[str, Any]
    verified: bool


@dataclass(frozen=True)
class OutboxEvent:
    id: int
    idempotency_key: str
    event_type: str
    payload: Mapping[str, Any]
    attempts: int
    available_at: str
    last_error: Optional[str]
```

- [ ] **Step 4: Run the model tests**

Run: `.venv/bin/python -m unittest tests.test_learning_models -v`

Expected: PASS.

- [ ] **Step 5: Commit the models**

```bash
git add learning_models.py tests/test_learning_models.py
git commit -m "feat: add learning data models and fingerprints"
```

### Task 2: Create the local SQLite schema and repository

**Files:**
- Create: `learning_store.py`
- Create: `tests/test_learning_store.py`

- [ ] **Step 1: Write failing migration and idempotency tests**

```python
import tempfile
import unittest
from pathlib import Path

from learning_store import LearningStore


class LearningStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temporary.name) / "learning.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_schema_is_migrated_once(self):
        self.store.migrate()
        self.store.migrate()
        self.assertEqual(self.store.schema_version(), 1)

    def test_outbox_idempotency_key_is_unique(self):
        self.store.migrate()
        first = self.store.enqueue("same-key", "review.created", {"a": 1})
        second = self.store.enqueue("same-key", "review.created", {"a": 1})
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.pending_outbox()), 1)
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_learning_store -v`

Expected: FAIL importing `learning_store`.

- [ ] **Step 3: Implement versioned schema creation**

Create `LearningStore.migrate()` with one `BEGIN IMMEDIATE` transaction and these exact tables:

```sql
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
  product_version TEXT PRIMARY KEY,
  style_code TEXT NOT NULL,
  title TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_checkpoints (
  run_id TEXT PRIMARY KEY,
  product_version TEXT NOT NULL,
  execution_mode TEXT NOT NULL,
  platform_order_json TEXT NOT NULL,
  current_index INTEGER NOT NULL,
  status TEXT NOT NULL,
  pending_review_id TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_results (
  run_id TEXT NOT NULL,
  platform_id TEXT NOT NULL,
  status TEXT NOT NULL,
  expected_json TEXT NOT NULL,
  readback_json TEXT NOT NULL,
  verified INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, platform_id)
);
CREATE TABLE IF NOT EXISTS candidate_snapshots (
  snapshot_version TEXT PRIMARY KEY,
  platform_id TEXT NOT NULL,
  category_leaf_id TEXT NOT NULL,
  field_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL,
  last_error TEXT,
  delivered_at TEXT
);
```

Use `sqlite3.connect(path)`, `row_factory = sqlite3.Row`, `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, UTC ISO timestamps, and SQLite UPSERT only where the newest state should replace the old state.

- [ ] **Step 4: Add exact repository methods**

```python
def upsert_product(self, fingerprint: ProductFingerprint) -> None:
    now = utc_now()
    payload = canonical_json(asdict(fingerprint))
    with self.connection:
        self.connection.execute(
            "INSERT INTO products(product_version, style_code, title, payload_json, created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(product_version) DO UPDATE SET "
            "style_code=excluded.style_code,title=excluded.title,payload_json=excluded.payload_json",
            (fingerprint.product_version, fingerprint.style_code, fingerprint.title, payload, now),
        )


def save_checkpoint(self, checkpoint: RunCheckpoint) -> None:
    with self.connection:
        self.connection.execute(
            "INSERT INTO run_checkpoints VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET current_index=excluded.current_index,"
            "status=excluded.status,pending_review_id=excluded.pending_review_id,updated_at=excluded.updated_at",
            (checkpoint.run_id, checkpoint.product_version, checkpoint.execution_mode,
             canonical_json(checkpoint.platform_order), checkpoint.current_index,
             checkpoint.status, checkpoint.pending_review_id, utc_now()),
        )


def load_checkpoint(self, run_id: str) -> Optional[RunCheckpoint]:
    row = self.connection.execute("SELECT * FROM run_checkpoints WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    return RunCheckpoint(row["run_id"], row["product_version"], row["execution_mode"],
                         tuple(json.loads(row["platform_order_json"])), row["current_index"],
                         row["status"], row["pending_review_id"])


def record_stage(self, result: StageResult) -> None:
    with self.connection:
        self.connection.execute(
            "INSERT INTO stage_results VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id,platform_id) DO UPDATE SET status=excluded.status,"
            "expected_json=excluded.expected_json,readback_json=excluded.readback_json,"
            "verified=excluded.verified,updated_at=excluded.updated_at",
            (result.run_id, result.platform_id, result.status, canonical_json(result.expected),
             canonical_json(result.readback), int(result.verified), utc_now()),
        )


def completed_platforms(self, run_id: str) -> Tuple[str, ...]:
    checkpoint = self.load_checkpoint(run_id)
    if checkpoint is None:
        return ()
    rows = self.connection.execute(
        "SELECT platform_id FROM stage_results WHERE run_id=? AND verified=1", (run_id,)
    ).fetchall()
    verified = {row["platform_id"] for row in rows}
    return tuple(platform for platform in checkpoint.platform_order if platform in verified)


def save_candidate_snapshot(self, snapshot: CandidateSnapshot) -> None:
    with self.connection:
        self.connection.execute(
            "INSERT OR IGNORE INTO candidate_snapshots VALUES(?,?,?,?,?,?)",
            (snapshot.snapshot_version, snapshot.platform_id, snapshot.category_leaf_id,
             snapshot.field_id, canonical_json(asdict(snapshot)), utc_now()),
        )


def enqueue(self, idempotency_key: str, event_type: str, payload: Mapping[str, Any]) -> int:
    with self.connection:
        self.connection.execute(
            "INSERT OR IGNORE INTO sync_outbox(idempotency_key,event_type,payload_json,available_at) VALUES(?,?,?,?)",
            (idempotency_key, event_type, canonical_json(payload), utc_now()),
        )
    row = self.connection.execute(
        "SELECT id FROM sync_outbox WHERE idempotency_key=?", (idempotency_key,)
    ).fetchone()
    return int(row["id"])


def pending_outbox(self, limit: int = 50) -> Tuple[OutboxEvent, ...]:
    rows = self.connection.execute(
        "SELECT * FROM sync_outbox WHERE delivered_at IS NULL AND available_at<=? ORDER BY id LIMIT ?",
        (utc_now(), limit),
    ).fetchall()
    return tuple(OutboxEvent(row["id"], row["idempotency_key"], row["event_type"],
                             json.loads(row["payload_json"]), row["attempts"],
                             row["available_at"], row["last_error"]) for row in rows)


def mark_delivered(self, event_id: int) -> None:
    with self.connection:
        self.connection.execute("UPDATE sync_outbox SET delivered_at=? WHERE id=?", (utc_now(), event_id))


def mark_retry(self, event_id: int, error: str, available_at: str) -> None:
    with self.connection:
        self.connection.execute(
            "UPDATE sync_outbox SET attempts=attempts+1,last_error=?,available_at=? WHERE id=?",
            (error[:200], available_at, event_id),
        )
```

- [ ] **Step 5: Add checkpoint recovery tests**

```python
def test_only_verified_stages_are_completed(self):
    self.store.migrate()
    self.store.save_checkpoint(RunCheckpoint(
        "run-1", "product-1", "save_only", ("tm", "pdd"), 0, "running"
    ))
    self.store.record_stage(StageResult("run-1", "tm", "saved", {}, {}, False))
    self.store.record_stage(StageResult("run-1", "pdd", "verified", {}, {"ok": True}, True))
    self.assertEqual(self.store.completed_platforms("run-1"), ("pdd",))
```

- [ ] **Step 6: Run store tests**

Run: `.venv/bin/python -m unittest tests.test_learning_models tests.test_learning_store -v`

Expected: PASS.

- [ ] **Step 7: Commit the store**

```bash
git add learning_models.py learning_store.py tests/test_learning_store.py
git commit -m "feat: add local learning store and checkpoints"
```

### Task 3: Add an idempotent Cloudflare client and offline delivery

**Files:**
- Create: `learning_client.py`
- Create: `tests/test_learning_client.py`

- [ ] **Step 1: Write failing response classification tests**

```python
import unittest
from unittest.mock import patch

from learning_client import CloudLearningClient, PermanentCloudError, RetryableCloudError


class LearningClientTests(unittest.TestCase):
    def test_409_is_returned_as_existing_idempotent_result(self):
        client = CloudLearningClient("https://review.example", "device-token")
        with patch.object(client, "_request", return_value=(409, {"task_id": "r-1"})):
            result = client.post_event("key-1", "review.created", {"x": 1})
        self.assertEqual(result["task_id"], "r-1")

    def test_503_is_retryable(self):
        client = CloudLearningClient("https://review.example", "device-token")
        with patch.object(client, "_request", return_value=(503, {})):
            with self.assertRaises(RetryableCloudError):
                client.post_event("key-1", "review.created", {})
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_learning_client -v`

Expected: FAIL importing `learning_client`.

- [ ] **Step 3: Implement the standard-library HTTP client**

```python
class CloudLearningClient:
    def __init__(self, base_url: str, device_token: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.device_token = device_token
        self.timeout = timeout

    def post_event(self, key: str, event_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        status, body = self._request(
            "POST",
            "/api/device/events",
            {"idempotency_key": key, "event_type": event_type, "payload": payload},
        )
        if status in (200, 201, 409):
            return body
        if status == 429 or status >= 500:
            raise RetryableCloudError("cloud status {0}".format(status))
        raise PermanentCloudError("cloud status {0}".format(status))
```

Implement `_request` with `urllib.request.Request`, JSON encoding, `Authorization: Device <token>`, `Content-Type: application/json`, and no logging of headers or token values.

- [ ] **Step 4: Implement outbox flushing**

```python
def flush_outbox(store: LearningStore, client: CloudLearningClient, now: datetime) -> int:
    delivered = 0
    for event in store.pending_outbox(limit=50):
        try:
            client.post_event(event.idempotency_key, event.event_type, event.payload)
        except RetryableCloudError as error:
            delay = min(300, 2 ** min(event.attempts, 8))
            store.mark_retry(event.id, str(error), (now + timedelta(seconds=delay)).isoformat())
            continue
        except PermanentCloudError:
            raise
        store.mark_delivered(event.id)
        delivered += 1
    return delivered
```

Add `upload_asset(product_version, sha256, kind, content_type, body)` to `CloudLearningClient`. It must use `PUT /api/device/assets/<sha256>`, set the product/version headers, treat `200`, `201`, and `409` as idempotent success, and classify `429`/`5xx` as retryable. The caller sends original image bytes only after the asset metadata event has been stored in the local outbox.

- [ ] **Step 5: Test token redaction and backoff**

Assert exception strings contain only status codes, not request headers, payload credentials, or device tokens. Assert attempts 0, 1, and 8 schedule delays of 1, 2, and 256 seconds.

- [ ] **Step 6: Run client and store tests**

Run: `.venv/bin/python -m unittest tests.test_learning_client tests.test_learning_store -v`

Expected: PASS.

- [ ] **Step 7: Commit the client**

```bash
git add learning_client.py tests/test_learning_client.py
git commit -m "feat: add offline cloud event delivery"
```

### Task 4: Generate upload-safe learning thumbnails

**Files:**
- Create: `learning_assets.py`
- Create: `tests/test_learning_assets.py`

- [ ] **Step 1: Write failing thumbnail tests**

Create a temporary 1200×800 JPEG with OpenCV. Assert the output is JPEG, its longest edge is 640, its aspect ratio is preserved, repeated conversion returns identical bytes, and the source file remains unchanged.

- [ ] **Step 2: Run the test and verify failure**

Run: `.venv/bin/python -m unittest tests.test_learning_assets -v`

Expected: FAIL importing `learning_assets`.

- [ ] **Step 3: Implement deterministic conversion**

```python
def build_learning_thumbnail(path: Path, max_edge: int = 640) -> bytes:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise LearningAssetError("无法读取商品图片")
    height, width = image.shape[:2]
    scale = min(1.0, max_edge / float(max(height, width)))
    resized = cv2.resize(
        image,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    ok, encoded = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 82])
    if not ok:
        raise LearningAssetError("无法生成学习缩略图")
    return encoded.tobytes()
```

Use SHA-256 of the encoded bytes as the thumbnail asset ID. Upload both `original` and `learning_thumbnail` through `CloudLearningClient.upload_asset`; only the original receives a 30-day expiry.

- [ ] **Step 4: Run asset tests**

Run: `.venv/bin/python -m unittest tests.test_learning_assets -v`

Expected: PASS.

- [ ] **Step 5: Commit asset preparation**

```bash
git add learning_assets.py tests/test_learning_assets.py
git commit -m "feat: prepare private learning thumbnails"
```

### Task 5: Add opt-in run context without changing default behavior

**Files:**
- Modify: `kuaimai_erp.py:4584-4937`
- Modify: `tests/test_kuaimai_erp.py:169-280`
- Modify: `.gitignore`
- Modify: `README.md`

- [ ] **Step 1: Write failing CLI and stage-recording tests**

Add parser assertions for:

```python
args = build_parser().parse_args(["--platform", "all"])
self.assertFalse(args.learning_enabled)
self.assertEqual(args.learning_db, ".local-state/learning.sqlite3")
```

Add an orchestrator test with a fake store and assert `record_stage` is called only after a successful platform run. A failed platform must leave that stage unverified and prevent later platforms from running.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_kuaimai_erp.AsyncRegressionTests tests.test_kuaimai_erp.ExecutionModeTests -v`

Expected: FAIL because learning CLI arguments and store injection are absent.

- [ ] **Step 3: Add CLI arguments**

```python
parser.add_argument("--learning-enabled", action="store_true", help="启用 AI 学习检查点和审核服务")
parser.add_argument("--learning-db", default=".local-state/learning.sqlite3", help="本地学习缓存数据库")
parser.add_argument("--learning-api-url", default=os.environ.get("KUAIMAI_LEARNING_API_URL", ""))
```

Read the device token only from `KUAIMAI_LEARNING_DEVICE_TOKEN`; never add a password or token CLI option because command lines are visible to other local processes.

- [ ] **Step 4: Add a minimal run-context factory**

```python
@dataclass
class LearningRunContext:
    store: LearningStore
    run_id: str
    product_version: str


def create_learning_context(args: argparse.Namespace, product: ProductData) -> Optional[LearningRunContext]:
    if not args.learning_enabled:
        return None
    fingerprint = ProductFingerprint.from_inputs(
        product.style_code,
        product.title,
        tuple(product.main_images) + tuple(product.main_images_34) + tuple(product.detail_images),
    )
    store = LearningStore(Path(args.learning_db))
    store.migrate()
    store.upsert_product(fingerprint)
    return LearningRunContext(store, uuid.uuid4().hex, fingerprint.product_version)
```

Pass the optional context into `run_all_implemented_platforms`. After `run_browser_automation` returns, record a `StageResult` with `verified=True` only when the platform's existing after-save validation artifact proves readback success. Preview mode records `status="previewed"` and `verified=False`.

- [ ] **Step 5: Preserve default behavior**

Keep `run_all_implemented_platforms(args, product, artifact_dir, logger)` valid by adding `learning_context: Optional[LearningRunContext] = None`. Existing tests must not need new arguments and existing JSON artifacts must remain byte-compatible when learning is disabled.

- [ ] **Step 6: Ignore local state and document activation**

Append to `.gitignore`:

```gitignore
.local-state/
cloudflare_review/node_modules/
cloudflare_review/.wrangler/
cloudflare_review/dist/
```

Document these environment variables in `README.md` without example secrets:

```text
KUAIMAI_LEARNING_API_URL
KUAIMAI_LEARNING_DEVICE_TOKEN
```

- [ ] **Step 7: Run the full Python test suite**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: PASS with learning disabled by default and no browser required for unit tests.

- [ ] **Step 8: Commit the opt-in integration**

```bash
git add kuaimai_erp.py tests/test_kuaimai_erp.py .gitignore README.md
git commit -m "feat: record opt-in learning run checkpoints"
```

## Phase 1 exit criteria

- Current launch commands behave exactly as before unless `--learning-enabled` is present.
- Product and image changes produce new versions.
- SQLite migrations are repeatable.
- Only verified saves count as completed stages.
- Offline events survive process restart and are idempotent.
- No credential is written to SQLite, JSON artifacts, logs, or command-line arguments.
