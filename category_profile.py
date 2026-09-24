"""Shared, conservative product-category decisions.

Excel category cells are supplier hints.  They are not assumed to be the
literal taxonomy path of every marketplace.  Platform adapters may search the
hints, but must still prove one exact API/DOM candidate before clicking it.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Optional, Tuple
import unicodedata


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


def category_path_parts(value: object) -> Tuple[str, ...]:
    """Normalize a marketplace category path into ordered exact segments."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return tuple(
        re.sub(r"[\s*:：]+", "", part).casefold()
        for part in re.split(r"[>＞/／]", text)
        if re.sub(r"[\s*:：]+", "", part)
    )


def category_gender_roots(hints: Iterable[str]) -> Tuple[str, ...]:
    text = " ".join(str(value) for value in hints)
    if any(marker in text for marker in ("男士", "男装", "男性")):
        return ("男装", "男士", "男款")
    if any(marker in text for marker in ("女士", "女装", "女性")):
        return ("女装", "女士", "女款")
    return ()


def choose_category_candidate(
    result_texts: Iterable[str], hints: Iterable[str]
) -> Tuple[str, str]:
    """Pick one exact category result using supplier hints.

    Platform taxonomies can omit supplier-only middle levels. Matching uses
    exact path segments, with gender as a tie-breaker. An empty result means
    the caller should keep searching or raise a platform-specific error.
    """
    paths = tuple(dict.fromkeys(str(text).strip() for text in result_texts if str(text).strip()))
    hint_values = tuple(str(value).strip() for value in hints if str(value).strip())
    if not paths or not hint_values:
        return "", ""
    expected = tuple(re.sub(r"[\s*:：]+", "", value).casefold() for value in hint_values)
    terms = tuple(
        re.sub(r"[\s*:：]+", "", value).casefold()
        for value in category_search_terms(hint_values)
    )
    gender_roots = tuple(root.casefold() for root in category_gender_roots(hint_values))

    def has_gender_root(parts: Tuple[str, ...]) -> bool:
        if not gender_roots:
            return True
        # Supplier hints such as “男士休闲夹克” carry a gender signal even
        # when the marketplace path uses the shorter “男装” segment.  A
        # candidate from the opposite branch, or a generic branch with the
        # same leaf, must not win merely because it shares “夹克”.
        return any(root in parts for root in gender_roots)

    complete = []
    for text in paths:
        parts = category_path_parts(text)
        if not has_gender_root(parts):
            continue
        cursor = 0
        for wanted in expected:
            while cursor < len(parts) and parts[cursor] != wanted:
                cursor += 1
            if cursor == len(parts):
                break
            cursor += 1
        else:
            complete.append(text)
    if len(complete) == 1:
        return complete[0], "full_ordered_path"
    if len(complete) > 1:
        return "", "ambiguous_full_path"

    scored = []
    for text in paths:
        parts = category_path_parts(text)
        if not has_gender_root(parts):
            continue
        score = 0
        for index, wanted in enumerate(terms):
            if wanted in parts:
                score += 1000 - index * 10 + len(wanted)
        if gender_roots and any(root in parts for root in gender_roots):
            score += 5000
        if score:
            # When the Excel hint only identifies a lower branch (for example
            # 男装/夹克), a prediction may prepend unrelated levels such as
            # 新制造. Prefer the shortest matching path; it is the candidate
            # that introduces the fewest category levels absent from Excel.
            scored.append((score, len(parts), text))
    if not scored:
        return "", ""
    best_score = max(score for score, _path_length, _text in scored)
    score_winners = tuple(
        (path_length, text)
        for score, path_length, text in scored
        if score == best_score
    )
    shortest_path = min(path_length for path_length, _text in score_winners)
    winners = tuple(
        text for path_length, text in score_winners if path_length == shortest_path
    )
    if len(winners) != 1:
        return "", "ambiguous_hint"
    return winners[0], "excel_hint_exact"


def choose_category_object(
    candidates: Iterable[object], hints: Iterable[str]
) -> Tuple[Optional[object], str]:
    """Choose one object with a ``path`` sequence using the shared matcher."""
    values = tuple(candidates)
    rendered = tuple(
        " > ".join(str(part) for part in getattr(candidate, "path", ()) if str(part).strip())
        for candidate in values
    )
    chosen, strategy = choose_category_candidate(rendered, hints)
    if not chosen or strategy.startswith("ambiguous"):
        return None, strategy
    matches = [
        candidate
        for candidate, path in zip(values, rendered)
        if path == chosen
    ]
    return (matches[0], strategy) if len(matches) == 1 else (None, "ambiguous_object")


__all__ = [
    "CategoryProfile",
    "category_profile",
    "category_search_terms",
    "preferred_category_leaf",
    "category_path_parts",
    "category_gender_roots",
    "choose_category_candidate",
    "choose_category_object",
]
