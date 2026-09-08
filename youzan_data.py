"""Excel inputs for the FastMai Youzan product-information form."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Dict, Tuple


class YouzanDataError(ValueError):
    """The spreadsheet does not contain safe Youzan inputs."""


class FrozenFields(Mapping[str, str]):
    """An immutable ordered mapping of non-empty spreadsheet values."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_items", tuple(values.items()))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("有赞 Excel 字段不可修改")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("有赞 Excel 字段不可修改")

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
class YouzanFields:
    fields: Mapping[str, str]
    category_path: Tuple[str, ...]
    garment_kind: str


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _category_values(fields: Mapping[str, Any]) -> Tuple[str, ...]:
    values = []
    for raw_key, raw_value in fields.items():
        key = str(raw_key).strip()
        key_parts = tuple(
            part.strip() for part in re.split(r"[/／]", key) if part.strip()
        )
        if key != "商品分类" and "商品分类" not in key_parts:
            continue
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


def parse_youzan_fields(fields: Mapping[str, Any]) -> YouzanFields:
    """Normalize Excel values without breaking unrelated platform runs."""

    normalized = {
        str(key).strip(): _cell_text(value)
        for key, value in fields.items()
        if str(key).strip() and _cell_text(value)
    }
    sources = _category_values(fields)
    if not sources:
        return YouzanFields(FrozenFields(normalized), (), "unknown")

    paths = tuple(_category_path(value) for value in sources)
    unique_paths = tuple(dict.fromkeys(paths))
    if len(unique_paths) != 1:
        rendered = "；".join(" / ".join(path) for path in unique_paths)
        raise YouzanDataError("Excel 中有赞商品分类存在多个不一致路径：" + rendered)

    category_path = unique_paths[0]
    category_text = " ".join(category_path)
    has_pants = "裤" in category_text
    has_coat = any(token in category_text for token in ("外套", "皮衣"))
    garment_kind = (
        "pants"
        if has_pants and not has_coat
        else "coat"
        if has_coat and not has_pants
        else "unknown"
    )
    return YouzanFields(FrozenFields(normalized), category_path, garment_kind)


__all__ = ["YouzanDataError", "YouzanFields", "parse_youzan_fields"]
