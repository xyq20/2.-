from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import mimetypes
from pathlib import Path
from typing import Optional, Tuple

import cv2


DEFAULT_THUMBNAIL_MAX_EDGE = 768
ORIGINAL_RETENTION_DAYS = 30


class LearningAssetError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedLearningAsset:
    kind: str
    sha256: str
    content_type: str
    body: bytes
    delete_after: Optional[str]


def build_learning_thumbnail(
    path: Path,
    max_edge: int = DEFAULT_THUMBNAIL_MAX_EDGE,
) -> bytes:
    if max_edge < 1:
        raise ValueError("max_edge must be positive")
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise LearningAssetError("无法读取商品图片")
    height, width = image.shape[:2]
    scale = min(1.0, max_edge / float(max(height, width)))
    target_size = (
        max(1, round(width * scale)),
        max(1, round(height * scale)),
    )
    if target_size != (width, height):
        image = cv2.resize(image, target_size, interpolation=cv2.INTER_AREA)
    encode_parameters = [
        cv2.IMWRITE_JPEG_QUALITY,
        82,
        cv2.IMWRITE_JPEG_PROGRESSIVE,
        0,
        cv2.IMWRITE_JPEG_OPTIMIZE,
        0,
    ]
    ok, encoded = cv2.imencode(".jpg", image, encode_parameters)
    if not ok:
        raise LearningAssetError("无法生成学习缩略图")
    return encoded.tobytes()


def prepare_learning_assets(
    path: Path,
    *,
    now: Optional[datetime] = None,
) -> Tuple[PreparedLearningAsset, PreparedLearningAsset]:
    source = path.read_bytes()
    thumbnail = build_learning_thumbnail(path)
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return (
        PreparedLearningAsset(
            kind="original",
            sha256=hashlib.sha256(source).hexdigest(),
            content_type=content_type,
            body=source,
            delete_after=(timestamp + timedelta(days=ORIGINAL_RETENTION_DAYS)).isoformat(),
        ),
        PreparedLearningAsset(
            kind="learning_thumbnail",
            sha256=hashlib.sha256(thumbnail).hexdigest(),
            content_type="image/jpeg",
            body=thumbnail,
            delete_after=None,
        ),
    )
