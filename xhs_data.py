"""Excel inputs for the FastMai Xiaohongshu product-information form.

The form adapter owns page-specific matching.  This module keeps every
non-empty Excel input intact and preserves the ``商品分类`` segments as
category hints.  The adapter first supports the historic ordered route and can
also prove one exact marketplace leaf from cross-platform hints.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Dict, Tuple


class XhsDataError(ValueError):
    """The spreadsheet does not contain safe Xiaohongshu inputs."""


class FrozenFields(Mapping[str, str]):
    """An immutable ordered mapping so adapters cannot mutate Excel data."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_items", tuple(values.items()))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("小红书 Excel 字段不可修改")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("小红书 Excel 字段不可修改")

    def __getitem__(self, key: str) -> str:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __deepcopy__(self, _memo: Dict[int, Any]) -> "FrozenFields":
        return self


@dataclass(frozen=True)
class XhsFields:
    fields: Mapping[str, str]
    category_path: Tuple[str, ...]


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _category_sources(fields: Mapping[str, Any]) -> Tuple[str, ...]:
    values = []
    for raw_key, raw_value in fields.items():
        key = str(raw_key).strip()
        # Historic sheets may group aliases in the same heading.  Do not use
        # a substring check: e.g. ``同步商品分类`` would be an unrelated key.
        key_parts = tuple(
            part.strip() for part in re.split(r"[/／]", key) if part.strip()
        )
        if key == "商品分类" or "商品分类" in key_parts:
            value = _cell_text(raw_value)
            if value and value not in values:
                values.append(value)
    return tuple(values)


def _category_path(value: str) -> Tuple[str, ...]:
    return tuple(
        part.strip()
        for part in re.split(r"[/／>＞]", value)
        if part.strip()
    )


def parse_xhs_fields(fields: Mapping[str, Any]) -> XhsFields:
    """Normalize Excel values and retain one deterministic category hint set."""
    normalized = {
        str(key).strip(): _cell_text(value)
        for key, value in fields.items()
        if str(key).strip() and _cell_text(value)
    }
    sources = _category_sources(fields)
    if not sources:
        # ProductData is also read for base-only and other-platform jobs.
        # Keep those jobs usable; the XHS runner turns this into a targeted
        # preflight error only when Xiaohongshu has actually been selected.
        return XhsFields(fields=FrozenFields(normalized), category_path=())
    paths = tuple(_category_path(value) for value in sources)
    if any(not path for path in paths):
        raise XhsDataError("Excel 中的小红书商品分类为空")
    unique_paths = tuple(dict.fromkeys(paths))
    if len(unique_paths) != 1:
        rendered = "；".join(" / ".join(path) for path in unique_paths)
        raise XhsDataError("Excel 中小红书商品分类存在多个不一致路径：" + rendered)
    return XhsFields(fields=FrozenFields(normalized), category_path=unique_paths[0])
