"""Small, explicit business policies shared by platform form adapters.

These policies describe field scope and actions, never infer product facts
from a previous style or a coarse garment category.
"""
import re
import unicodedata
from typing import Mapping, Sequence, Tuple


CLEARED_CATEGORY_FIELDS = {"yz": frozenset({"尺码"})}


def skip_color_attribute(label: str) -> bool:
    """Color attributes are user-excluded; JD sale-spec colors are separate."""
    normalized = re.sub(r"[\s*＊：:]", "", str(label)).split("#", 1)[0]
    return normalized in {"颜色", "颜色分类", "商品颜色", "色彩", "色系"}


def without_color_attributes(items: Mapping) -> dict:
    return {key: item for key, item in items.items() if not skip_color_attribute(item[0])}


REQUIRED_SERVICES = {
    "yz": ("参加会员折扣", "支持买家申请换货", "7天无理由退货"),
}

# Freight update scope is distinct from publish targets and authorized shops.
# Never extend this list merely because another shop is visible on the page.
FREIGHT_TEMPLATE_SHOPS = {
    "wxsph": ("NEIGBORL钊叔制鞋服",),
    "xhs": ("钊叔的店", "啊亮製NEIGBORL的店", "钊叔制NEIGBORL的店"),
}


def clear_category_field(platform: str, label: str) -> bool:
    return label.strip().rstrip("：:") in CLEARED_CATEGORY_FIELDS.get(platform, ())


def freight_alternatives(fields: Mapping[str, str]) -> Tuple[str, ...]:
    """Resolve aliases without splitting punctuation inside template names."""
    values = set()
    for key, value in fields.items():
        aliases = {part.strip() for part in re.split(r"[/／]", str(key))}
        if aliases.intersection({"运费设置", "运费模板", "运费模版"}) and str(value).strip():
            values.add(str(value).strip())
    if len(values) > 1:
        raise ValueError("运费设置匹配到相互冲突的 Excel 字段")
    if not values:
        return ()
    return tuple(dict.fromkeys(part.strip() for part in re.split(r"[/／]", values.pop()) if part.strip()))


def is_single_material_expression(value: str) -> bool:
    # Slash alternatives are OR, whereas explicit composition separators may
    # represent multiple materials. Never remove rows for an unparsed mixture.
    if not str(value).strip() or re.search(r"[、,，;；+＋]", value):
        return False
    component = r"[^\d%％/／]+?(?:\s*[（(]?\s*\d+(?:\.\d+)?\s*[%％]\s*[）)]?)?"
    return all(re.fullmatch(component, part.strip()) is not None
               for part in re.split(r"[/／]", value))


def _normalize_option_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    return re.sub(r"[\s，,;；、・·\-_（）()]", "", text).casefold()


def _normalized_parts(value: object) -> frozenset:
    """Split raw '氨纶(聚氨酯弹性纤维)' into {'氨纶', '聚氨酯弹性纤维'}.

    Bracket splitting must happen on the raw text BEFORE punctuation is
    stripped, otherwise the annotation structure is lost. Splitting first
    also makes 京东语序“俗名(化学名)” and 天猫语序“化学名(俗名)” resolve
    to the same part set.
    """
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    match = re.fullmatch(r"([^（）()]*)[（(]([^（）()]*)[）)]", text)
    parts = (
        (match.group(1), match.group(2)) if match else (text,)
    )
    normalized = {
        _normalize_option_text(part) for part in parts
    }
    return frozenset(part for part in normalized if part)


# Known material/fabric spellings outrank broad substring matches. Keep them
# here so JD's numeric-ID writer and other platforms' DOM writers agree.
# Do not strip arbitrary fabric suffixes: 棉麻/珠地棉/牛仔布 add distinct facts.
MATERIAL_OPTION_EQUIVALENTS = (frozenset({"棉", "棉布"}),)


def match_option_candidates(
    desired_labels: Sequence[str],
    candidate_labels: Sequence[str],
) -> Tuple[int, ...]:
    """Tiered Excel-to-platform option matching shared by form adapters.

    Tier 0 (exact): the candidate equals a desired label. Excel 棉 must win
    over 京东's lookalike 木棉 even though both "contain" the name.
    Tier 1 (annotated): the candidate is the desired name plus or minus a
    parenthetical annotation, compared as whole name parts — Excel 氨纶 vs
    京东候选 氨纶(聚氨酯弹性纤维), or the reversed 天猫 alias
    聚氨酯弹性纤维(氨纶). Part equality keeps 木棉 (a different fiber that
    merely contains 棉) and 弹性纤维 (a substring of the alias, not a part)
    out of this tier.
    Tier 2 (known spelling): 棉 and 棉布 refer to the same material in platform
    dictionaries; unrelated cotton fabrics must not make that match ambiguous.
    Tier 3 (component): plain substring in either direction, e.g. Excel 羊绒
    inside 京东候选 山羊绒. Compound names legitimately contain shorter
    option names, so tier-3 only counts when no stronger candidate exists.
    Returns indices into ``candidate_labels``; the caller decides whether a
    non-unique result is an error or a review deferral.
    """
    desired = tuple(
        text
        for text in (_normalize_option_text(label) for label in desired_labels)
        if text
    )
    normalized = tuple(_normalize_option_text(label) for label in candidate_labels)
    desired_parts = tuple(_normalized_parts(label) for label in desired_labels)
    candidate_parts = tuple(
        _normalized_parts(label) for label in candidate_labels
    )

    def annotated_equivalent(candidate: frozenset, wanted: frozenset) -> bool:
        if not candidate or not wanted:
            return False
        if candidate == wanted and len(candidate) > 1:
            # Same parts, flipped annotation order (天猫 vs 京东 style).
            return True
        return len(candidate) != len(wanted) and (
            candidate < wanted or wanted < candidate
        )

    exact = tuple(
        index
        for index, text in enumerate(normalized)
        if text and any(text == want for want in desired)
    )
    if exact:
        return exact
    annotated = tuple(
        index
        for index, parts in enumerate(candidate_parts)
        if any(annotated_equivalent(parts, wanted) for wanted in desired_parts)
    )
    if annotated:
        return annotated
    known_spellings = tuple(
        index
        for index, text in enumerate(normalized)
        if text and any(
            text in group and any(want in group for want in desired)
            for group in MATERIAL_OPTION_EQUIVALENTS
        )
    )
    if known_spellings:
        return known_spellings
    return tuple(
        index
        for index, (raw_label, text) in enumerate(zip(candidate_labels, normalized))
        if text
        and not (
            "混纺" in _normalize_option_text(raw_label)
            or re.search(r"[与和+＋&]", str(raw_label))
        )
        and any(text in want or want in text for want in desired)
    )
