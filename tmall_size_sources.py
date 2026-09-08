"""Build verified, page-independent size rows for Tmall listings.

This module deliberately stops at evidence extraction.  It never owns a
Playwright page and cannot fill, save, or publish an ERP form.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Optional, Tuple

from size_image_recognition import (
    RecognitionError,
    recognize_recommendations,
    recognize_size_lengths,
)
from tmall_data import IMAGE_SUFFIXES, TmallFields
from tmall_rules import TmallRuleError, normalize_tmall_spec_values


CATEGORY_PANTS = "pants"
CATEGORY_CLOTHING = "clothing"
CATEGORY_FOOTWEAR = "footwear"
CATEGORY_GENERIC = "generic"


class TmallSizeSourceError(ValueError):
    """An invalid or ambiguous local Tmall size source."""


class TmallSizeReviewRequired(TmallSizeSourceError):
    """A source cannot be resolved safely without a human decision."""

    status = "review_required"

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class TmallSizeSources:
    category_kind: str
    sizes: Tuple[str, ...]
    headers: Tuple[str, ...]
    rows: Tuple[Mapping[str, Any], ...]
    evidence: Tuple[str, ...]


_CATEGORY_SEPARATORS = re.compile(r"[>/／＞→»]+")

# These accessory leaves must be checked before the footwear suffixes.  In
# particular, a category path containing "鞋" is not evidence that its leaf is a
# shoe product.
_GENERIC_ACCESSORY_MARKERS = (
    "袜",
    "鞋垫",
    "鞋带",
    "鞋套",
    "鞋撑",
    "鞋油",
    "鞋刷",
    "鞋护理",
    "护鞋",
    "鞋盒",
)
_FOOTWEAR_SUFFIXES = ("鞋", "靴", "鞋子", "靴子", "鞋靴")
_PANTS_SUFFIXES = (
    "休闲裤",
    "牛仔裤",
    "西裤",
    "运动裤",
    "工装裤",
    "直筒裤",
    "哈伦裤",
    "阔腿裤",
    "铅笔裤",
    "打底裤",
    "长裤",
    "短裤",
    "九分裤",
    "七分裤",
    "五分裤",
    "沙滩裤",
    "滑雪裤",
    "骑行裤",
    "裤装",
    "裤子",
)
_CLOTHING_SUFFIXES = (
    "外套",
    "夹克",
    "风衣",
    "大衣",
    "西服",
    "西装",
    "棉服",
    "羽绒服",
    "皮衣",
    "冲锋衣",
    "防晒衣",
    "T恤",
    "t恤",
    "卫衣",
    "衬衫",
    "毛衣",
    "针织衫",
    "背心",
    "马甲",
    "连衣裙",
    "半身裙",
    "裙装",
    "内衣",
    "内裤",
    "睡衣",
    "家居服",
)


def _field_mapping(fields: Any) -> Mapping[str, Any]:
    if isinstance(fields, TmallFields):
        return fields.fields
    candidate = getattr(fields, "fields", fields)
    if not isinstance(candidate, Mapping):
        raise TmallSizeSourceError("天猫尺码来源必须是 Excel 字段映射")
    return candidate


def parse_tmall_sizes(fields: Any) -> Tuple[str, ...]:
    """Read the exact Excel field ``尺码`` and preserve its item order."""

    source = _field_mapping(fields)
    if "尺码" not in source:
        raise TmallSizeSourceError("Excel 中缺少天猫精确字段“尺码”")
    raw_value = source["尺码"]
    if raw_value is None:
        raise TmallSizeSourceError("Excel 天猫尺码为空")
    raw = str(raw_value).strip()
    if not raw:
        raise TmallSizeSourceError("Excel 天猫尺码为空")

    parts = tuple(part.strip() for part in re.split(r"[/／]", raw))
    if not parts or any(not part for part in parts):
        raise TmallSizeSourceError(
            "Excel 天猫尺码含空项；请使用 / 或 ／ 分隔完整尺码"
        )

    seen = set()
    for part in parts:
        key = part.casefold()
        if key in seen:
            raise TmallSizeSourceError("Excel 天猫尺码重复：{0}".format(part))
        seen.add(key)
    return parts


def tmall_category_leaf(category_path: str) -> str:
    parts = tuple(
        part.strip()
        for part in _CATEGORY_SEPARATORS.split(str(category_path or ""))
        if part.strip()
    )
    if not parts:
        raise TmallSizeSourceError("天猫推荐类目叶子为空")
    return parts[-1]


def classify_tmall_category(category_path: str) -> str:
    """Classify only the confirmed leaf; parent-path words are not evidence."""

    leaf = tmall_category_leaf(category_path)
    if any(marker in leaf for marker in _GENERIC_ACCESSORY_MARKERS):
        return CATEGORY_GENERIC
    if leaf.endswith(_FOOTWEAR_SUFFIXES):
        return CATEGORY_FOOTWEAR
    if leaf.endswith(_PANTS_SUFFIXES):
        return CATEGORY_PANTS
    if leaf.endswith(_CLOTHING_SUFFIXES):
        return CATEGORY_CLOTHING
    return CATEGORY_GENERIC


def discover_height_weight_image(product_dir: Path) -> Optional[Path]:
    """Return one optional recommendation image, rejecting ambiguous folders."""

    directory = Path(product_dir) / "身高体重推荐表"
    if not directory.exists():
        return None
    if not directory.is_dir():
        raise TmallSizeReviewRequired(
            "身高体重推荐表路径不是文件夹",
            reason_code="invalid_height_weight_directory",
        )
    images = tuple(
        sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES
            ),
            key=lambda path: path.name.casefold(),
        )
    )
    if not images:
        return None
    if len(images) != 1:
        raise TmallSizeReviewRequired(
            "身高体重推荐表必须至多 1 张图片，当前为 {0} 张".format(
                len(images)
            ),
            reason_code="ambiguous_height_weight_image",
        )
    return images[0]


def _required_size_chart(size_chart_image: Optional[Path]) -> Path:
    if size_chart_image is None or not Path(size_chart_image).is_file():
        raise TmallSizeReviewRequired(
            "当前天猫类目需要唯一可读的尺码信息表",
            reason_code="missing_size_chart_image",
        )
    return Path(size_chart_image)


def _require_recognized_sizes(
    recognized: Tuple[Any, ...], expected_sizes: Tuple[str, ...]
) -> None:
    actual_sizes = tuple(str(getattr(item, "size", "")).strip() for item in recognized)
    if actual_sizes != expected_sizes:
        raise TmallSizeReviewRequired(
            "OCR 尺码与 Excel 尺码集合或顺序不一致",
            reason_code="ocr_size_mismatch",
        )


def resolve_tmall_size_sources(
    fields: Any,
    category_path: str,
    *,
    product_dir: Path,
    size_chart_image: Optional[Path] = None,
) -> TmallSizeSources:
    """Resolve Tmall size-table rows from Excel and category-specific evidence."""

    sizes = parse_tmall_sizes(fields)
    category_kind = classify_tmall_category(category_path)

    if category_kind == CATEGORY_FOOTWEAR:
        try:
            sizes = normalize_tmall_spec_values(
                sizes,
                is_footwear=True,
                is_size_dimension=True,
            )
        except TmallRuleError as error:
            raise TmallSizeSourceError(str(error)) from error

    if category_kind == CATEGORY_PANTS:
        size_chart = _required_size_chart(size_chart_image)
        height_weight = discover_height_weight_image(product_dir)
        if height_weight is None:
            raise TmallSizeReviewRequired(
                "裤装天猫尺码需要身高体重推荐表，当前没有唯一可用图片",
                reason_code="missing_height_weight_image",
            )
        try:
            recognized = recognize_recommendations(size_chart, height_weight, sizes)
        except RecognitionError as error:
            raise TmallSizeReviewRequired(
                "裤装天猫尺码 OCR 无法安全解析：{0}".format(error),
                reason_code="size_ocr_failed",
            ) from error
        recognized = tuple(recognized)
        _require_recognized_sizes(recognized, sizes)
        headers = (
            "尺码",
            "身高(cm)",
            "体重(kg)",
            "腰围(cm)",
            "臀围(cm)",
            "裤长(cm)",
        )
        rows = tuple(
            {
                "尺码": item.size,
                "身高(cm)": (item.height_min, item.height_max),
                "体重(kg)": (item.weight_min, item.weight_max),
                "腰围(cm)": item.waist,
                "臀围(cm)": item.hip,
                "裤长(cm)": item.length,
            }
            for item in recognized
        )
        evidence = (
            "excel:尺码",
            "ocr:尺码信息表",
            "ocr:身高体重推荐表",
        )
    elif category_kind == CATEGORY_CLOTHING:
        size_chart = _required_size_chart(size_chart_image)
        try:
            recognized = recognize_size_lengths(size_chart, sizes, "clothing")
        except RecognitionError as error:
            raise TmallSizeReviewRequired(
                "服装天猫衣长 OCR 无法安全解析：{0}".format(error),
                reason_code="size_ocr_failed",
            ) from error
        recognized = tuple(recognized)
        _require_recognized_sizes(recognized, sizes)
        headers = ("尺码", "衣长(cm)")
        rows = tuple(
            {"尺码": item.size, "衣长(cm)": item.length}
            for item in recognized
        )
        evidence = ("excel:尺码", "ocr:尺码信息表")
    else:
        headers = ("尺码",)
        rows = tuple({"尺码": size} for size in sizes)
        evidence = ("excel:尺码",)

    return TmallSizeSources(
        category_kind=category_kind,
        sizes=sizes,
        headers=headers,
        rows=rows,
        evidence=evidence,
    )
