"""Inputs for the Pinduoduo listing form.

The platform adapter owns DOM matching.  This module deliberately preserves
every non-empty Excel key/value pair so PDD's category-specific fields can be
matched against the fields actually rendered by the ERP.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Dict


class FrozenFields(Mapping[str, str]):
    """An immutable, ordered view of the Excel inputs."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_items", tuple(values.items()))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("拼多多 Excel 字段不可修改")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("拼多多 Excel 字段不可修改")

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
class PddFields:
    fields: Mapping[str, str]


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_pdd_fields(fields: Mapping[str, Any]) -> PddFields:
    """Keep all non-empty values; exact field matching happens in the UI."""
    normalized = {
        str(key).strip(): _cell_text(value)
        for key, value in fields.items()
        if str(key).strip() and _cell_text(value)
    }
    return PddFields(fields=FrozenFields(normalized))
