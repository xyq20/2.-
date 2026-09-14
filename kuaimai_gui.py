"""Tkinter launcher for selecting and running Kuaimai product folders."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Tuple


PRODUCT_EXCEL_NAME = "产品信息.xlsx"


@dataclass(frozen=True)
class ProductChoice:
    directory: Path
    excel_path: Path

    @property
    def display_name(self) -> str:
        return self.directory.name


def natural_sort_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    )


def discover_products(products_root: Path) -> Tuple[ProductChoice, ...]:
    root = Path(products_root).expanduser()
    if not root.is_dir():
        return ()

    choices = []
    for child in sorted(root.iterdir(), key=lambda item: natural_sort_key(item.name)):
        if not child.is_dir():
            continue
        excel_path = child / PRODUCT_EXCEL_NAME
        if excel_path.is_file():
            choices.append(ProductChoice(child, excel_path))
    return tuple(choices)
