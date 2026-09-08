"""Inputs and local assets for the Tmall listing form."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple


IMAGE_SUFFIXES = frozenset(
    (".jpg", ".jpeg", ".jfif", ".png", ".gif", ".webp", ".bmp")
)


class TmallDataError(ValueError):
    pass


class FrozenFields(Mapping[str, str]):
    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_items", tuple(values.items()))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("天猫 Excel 字段不可修改")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("天猫 Excel 字段不可修改")

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
class TmallFields:
    fields: Mapping[str, str]


@dataclass(frozen=True)
class TmallAssets:
    vertical_image: Path
    transparent_image: Path
    parameter_image: Path


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_tmall_fields(fields: Mapping[str, Any]) -> TmallFields:
    normalized = {
        str(key).strip(): _cell_text(value)
        for key, value in fields.items()
        if str(key).strip() and _cell_text(value)
    }
    return TmallFields(fields=FrozenFields(normalized))


def _exactly_one_image(directory: Path, label: str) -> Path:
    if not directory.is_dir():
        raise TmallDataError("找不到{0}文件夹：{1}".format(label, directory))
    images = tuple(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES
    )
    if len(images) != 1:
        raise TmallDataError(
            "{0}文件夹必须恰好 1 张支持的图片，当前为 {1} 张：{2}".format(
                label,
                len(images),
                directory,
            )
        )
    return images[0]


def _first_asset_directory(
    product_dir: Path,
    names: Tuple[str, ...],
    label: str,
) -> Path:
    for name in names:
        directory = product_dir / name
        if directory.is_dir():
            return directory
    raise TmallDataError(
        "找不到{0}文件夹，已尝试：{1}".format(label, "、".join(names))
    )


def read_tmall_assets(product_dir: Path) -> TmallAssets:
    product_dir = Path(product_dir)
    vertical_dir = _first_asset_directory(
        product_dir,
        ("2:3", "2：3", "2:3图", "2：3图"),
        "2:3",
    )
    transparent_dir = product_dir / "透明素材图"
    if not transparent_dir.is_dir():
        transparent_dir = _first_asset_directory(
            product_dir,
            ("1:1", "1：1", "1:1图", "1：1图"),
            "1:1透明素材",
        )
    return TmallAssets(
        vertical_image=_exactly_one_image(vertical_dir, "2:3"),
        transparent_image=_exactly_one_image(transparent_dir, "1:1透明素材"),
        parameter_image=_exactly_one_image(product_dir / "尺码信息表", "尺码信息表"),
    )
