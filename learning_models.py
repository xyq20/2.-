from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value in a stable, UTF-8-friendly form."""
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
        cls,
        style_code: str,
        title: str,
        image_paths: Iterable[Path],
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
