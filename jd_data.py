"""Excel inputs for the FastMai JD product-information form."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Dict


class FrozenFields(Mapping[str, str]):
    """An immutable ordered mapping of non-empty spreadsheet values."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_items", tuple(values.items()))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("京东 Excel 字段不可修改")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("京东 Excel 字段不可修改")

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
class JdFields:
    fields: Mapping[str, str]


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_jd_fields(fields: Mapping[str, Any]) -> JdFields:
    """Keep every non-empty Excel value for exact page-label matching."""

    normalized = {
        str(key).strip(): _cell_text(value)
        for key, value in fields.items()
        if str(key).strip() and _cell_text(value)
    }
    return JdFields(fields=FrozenFields(normalized))


__all__ = ["JdFields", "parse_jd_fields"]
