from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, Mapping, Tuple

from platform_registry import canonical_platform_name


HistoryKey = Tuple[str, str]
VerifiedAttributeHistory = Mapping[HistoryKey, Tuple[str, ...]]
_VALIDATION_SUFFIX = "-after-save-validation.json"


def normalize_history_field_label(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = text.strip("*:：").strip()
    return re.sub(r"\s+", "", text)


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None


def load_verified_attribute_history(
    runs_dir: Path, style_code: str
) -> Dict[HistoryKey, Tuple[str, ...]]:
    """Load the newest save-readback values for one exact product style."""
    root = Path(runs_dir)
    wanted_style = str(style_code).strip()
    history: Dict[HistoryKey, Tuple[str, ...]] = {}
    if not wanted_style or not root.is_dir():
        return history

    for run_dir in sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    ):
        summary = _read_json(run_dir / "input-summary.json")
        if not isinstance(summary, dict) or str(summary.get("style_code") or "").strip() != wanted_style:
            continue
        for validation_path in sorted(run_dir.rglob(f"*{_VALIDATION_SUFFIX}")):
            filename = validation_path.name
            platform = canonical_platform_name(
                filename[: -len(_VALIDATION_SUFFIX)]
            )
            payload = _read_json(validation_path)
            attributes = payload.get("attributes") if isinstance(payload, dict) else None
            if not isinstance(attributes, dict):
                continue
            for raw_label, raw_values in attributes.items():
                label = normalize_history_field_label(raw_label)
                if not label:
                    continue
                values_source = raw_values if isinstance(raw_values, list) else [raw_values]
                values = tuple(
                    str(value).strip()
                    for value in values_source
                    if value is not None and str(value).strip()
                )
                key = (platform, label)
                if values and key not in history:
                    history[key] = values
    return history


__all__ = [
    "HistoryKey",
    "VerifiedAttributeHistory",
    "load_verified_attribute_history",
    "normalize_history_field_label",
]
