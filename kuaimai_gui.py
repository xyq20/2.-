"""Tkinter launcher for selecting and running Kuaimai product folders."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
import threading
from typing import Callable, List, Optional, Sequence, Tuple


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


class BatchStatus(str, Enum):
    COMPLETED = "已完成/已提交"
    FAILED = "失败"
    NOT_STARTED = "未启动"


@dataclass(frozen=True)
class BatchItemResult:
    product: ProductChoice
    status: BatchStatus
    return_code: Optional[int] = None
    error: str = ""


def discover_products_root(
    configured_root: Optional[Path],
    volumes_root: Path = Path("/Volumes"),
) -> Optional[Path]:
    candidates = []
    if configured_root is not None:
        candidates.append(Path(configured_root).expanduser())

    volumes = Path(volumes_root)
    if volumes.is_dir():
        for volume in sorted(volumes.iterdir(), key=lambda item: natural_sort_key(item.name)):
            if not volume.is_dir():
                continue
            candidates.extend(
                (
                    volume / "共享文件" / "谭" / "products",
                    volume / "谭" / "products",
                    volume / "products",
                )
            )

    seen = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_dir():
            return resolved
    return None


def build_product_command(
    python_executable: Path,
    script_path: Path,
    product: ProductChoice,
) -> List[str]:
    return [
        str(python_executable),
        str(script_path),
        "--excel-url",
        str(product.excel_path),
        "--platform",
        "all",
        "--save",
    ]


Runner = Callable[[ProductChoice, Callable[[str], None]], int]
StartCallback = Callable[[int, int, ProductChoice], None]


def run_batch(
    products: Sequence[ProductChoice],
    runner: Runner,
    stop_event: threading.Event,
    emit: Optional[Callable[[str], None]] = None,
    on_start: Optional[StartCallback] = None,
) -> List[BatchItemResult]:
    output = emit or (lambda _line: None)
    results: List[BatchItemResult] = []
    for index, product in enumerate(products):
        if stop_event.is_set():
            results.extend(
                BatchItemResult(item, BatchStatus.NOT_STARTED)
                for item in products[index:]
            )
            break
        if on_start is not None:
            on_start(index, len(products), product)
        try:
            return_code = runner(product, output)
        except Exception as exc:
            results.append(BatchItemResult(product, BatchStatus.FAILED, error=str(exc)))
            continue
        results.append(
            BatchItemResult(
                product,
                BatchStatus.COMPLETED if return_code == 0 else BatchStatus.FAILED,
                return_code=return_code,
            )
        )
    return results
