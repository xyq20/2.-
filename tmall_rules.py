"""Pure rules shared by the Tmall listing workflow."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Iterable, Optional, Tuple
from zoneinfo import ZoneInfo


class TmallRuleError(ValueError):
    pass


_NUMERIC_SHOE_SIZE_SUFFIX_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)码$")
_SHANGHAI_ZONE = ZoneInfo("Asia/Shanghai")
_NEW_PRODUCT_FIELD = "是否申报新品"


def normalize_tmall_spec_values(
    values: Iterable[str],
    *,
    is_footwear: bool,
    is_size_dimension: bool,
) -> Tuple[str, ...]:
    """Remove a trailing ``码`` only from numeric footwear size values."""

    normalized = []
    seen = set()
    for original in values:
        value = original
        if is_footwear and is_size_dimension:
            match = _NUMERIC_SHOE_SIZE_SUFFIX_RE.fullmatch(value)
            if match is not None:
                value = match.group(1)
        if value in seen:
            raise TmallRuleError("规格值归一化后重复：{0}".format(value))
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)


def shanghai_today(now: Optional[datetime] = None) -> date:
    """Return the calendar date in Asia/Shanghai for an aware instant."""

    if now is None:
        now = datetime.now(_SHANGHAI_ZONE)
    elif now.tzinfo is None or now.utcoffset() is None:
        raise TmallRuleError("注入的时间必须是 aware datetime")
    return now.astimezone(_SHANGHAI_ZONE).date()


def _normalize_parameter_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(normalized.split())


def select_size_parameters(
    *,
    page_supported: Iterable[str],
    source_headers: Iterable[str],
    required_parameters: Iterable[str],
) -> Tuple[str, ...]:
    """Return the normalized exact intersection, preserving page order."""

    supported = tuple(page_supported)
    source_keys = {
        normalized
        for normalized in (_normalize_parameter_label(item) for item in source_headers)
        if normalized
    }
    required = tuple(required_parameters)
    missing = tuple(
        item
        for item in required
        if _normalize_parameter_label(item) not in source_keys
    )
    if missing:
        raise TmallRuleError(
            "页面必填尺码参数缺少来源表头：{0}".format("、".join(missing))
        )

    return tuple(
        item
        for item in supported
        if _normalize_parameter_label(item) in source_keys
    )


def new_product_declaration_value(field_labels: Iterable[str]) -> Optional[str]:
    """Return the required value only when the conditional field is present."""

    target = _normalize_parameter_label(_NEW_PRODUCT_FIELD)
    if any(_normalize_parameter_label(label) == target for label in field_labels):
        return "是"
    return None
