"""淘宝资料页可复用的 Excel 字段输入。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Dict


class FrozenFields(Mapping[str, str]):
    """保留 Excel 原始字段顺序，避免平台适配器改写输入。"""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_items", tuple(values.items()))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("淘宝 Excel 字段不可修改")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("淘宝 Excel 字段不可修改")

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
class TaobaoFields:
    fields: Mapping[str, str]


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_taobao_fields(fields: Mapping[str, Any]) -> TaobaoFields:
    """保留所有非空键值，具体匹配在动态类目渲染后完成。"""
    normalized = {
        str(key).strip(): _cell_text(value)
        for key, value in fields.items()
        if str(key).strip() and _cell_text(value)
    }
    return TaobaoFields(fields=FrozenFields(normalized))
