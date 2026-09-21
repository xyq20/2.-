"""Shared, conservative product-category decisions.

Excel category cells are supplier hints.  They are not assumed to be the
literal taxonomy path of every marketplace.  Platform adapters may search the
hints, but must still prove one exact API/DOM candidate before clicking it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple


PANTS_MARKERS = ("裤",)
FOOTWEAR_MARKERS = ("鞋", "靴")
CLOTHING_MARKERS = (
    "衣",
    "服",
    "夹克",
    "外套",
    "衫",
    "裙",
    "背心",
    "马甲",
    "卫衣",
    "西装",
    "风衣",
    "棉服",
    "羽绒",
)


@dataclass(frozen=True)
class CategoryProfile:
    hints: Tuple[str, ...]
    garment_kind: str

    @property
    def supports_letter_size_chart(self) -> bool:
        return self.garment_kind in {"pants", "clothing"}

    @property
    def uses_pants_recommendation_table(self) -> bool:
        return self.garment_kind == "pants"


def category_profile(hints: Iterable[str], title: str = "") -> CategoryProfile:
    cleaned = tuple(dict.fromkeys(str(value).strip() for value in hints if str(value).strip()))
    text = " ".join((*cleaned, str(title or "")))
    has_pants = any(marker in text for marker in PANTS_MARKERS)
    has_footwear = any(marker in text for marker in FOOTWEAR_MARKERS)
    has_clothing = any(marker in text for marker in CLOTHING_MARKERS)
    if has_pants and not has_footwear:
        kind = "pants"
    elif has_footwear and not has_pants:
        kind = "footwear"
    elif has_clothing and not has_footwear:
        kind = "clothing"
    else:
        kind = "generic"
    return CategoryProfile(cleaned, kind)


def category_search_terms(hints: Iterable[str]) -> Tuple[str, ...]:
    """Return distinct hints from most specific to broadest.

    A longer exact label is normally the marketplace leaf (for example
    ``男士休闲夹克`` before ``夹克``).  Original order is retained for equal
    lengths so the result is deterministic.
    """

    cleaned = tuple(dict.fromkeys(str(value).strip() for value in hints if str(value).strip()))
    return tuple(
        value
        for _index, value in sorted(
            enumerate(cleaned), key=lambda item: (-len(item[1]), item[0])
        )
    )


def preferred_category_leaf(hints: Iterable[str]) -> str:
    terms = category_search_terms(hints)
    return terms[0] if terms else ""


__all__ = [
    "CategoryProfile",
    "category_profile",
    "category_search_terms",
    "preferred_category_leaf",
]
