from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, Tuple


class FieldMappingError(ValueError):
    """A platform field cannot be mapped to one unambiguous canonical field."""


class EvidenceKind(str, Enum):
    VISUAL = "visual"
    TEXT = "text"


@dataclass(frozen=True)
class FieldPolicy:
    canonical_name: str
    required_evidence: Tuple[EvidenceKind, ...]
    allow_rule: bool


AliasKey = Tuple[str, str]
AliasRegistration = Tuple[AliasKey, str]
_REQUIRED_MARKER_PUNCTUATION = "*:"


def _normalize_component(value: str, *, field_label: bool = False) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    if field_label:
        normalized = normalized.strip(_REQUIRED_MARKER_PUNCTUATION).strip()
    return normalized


def build_alias_registry(
    registrations: Iterable[AliasRegistration],
) -> Dict[AliasKey, str]:
    registry: Dict[AliasKey, str] = {}
    for (platform, field_label), canonical_name in registrations:
        key = (
            _normalize_component(platform),
            _normalize_component(field_label, field_label=True),
        )
        if key in registry:
            raise FieldMappingError("duplicate platform field mapping: {!r}".format(key))
        registry[key] = canonical_name
    return registry


ALIAS_REGISTRATIONS: Tuple[AliasRegistration, ...] = (
    (("wxsph", "裤长"), "pants_length"),
    (("jd", "裤长"), "pants_length"),
    (("yz", "厚薄"), "thickness"),
    (("jd", "厚度"), "thickness"),
    (("wxsph", "面料材质成分含量"), "material_percentage"),
    (("wxsph", "材质成分"), "material_composition"),
    (("jd", "颜色"), "color"),
)
ALIASES = build_alias_registry(ALIAS_REGISTRATIONS)


POLICIES = {
    "pants_length": FieldPolicy(
        "pants_length", (EvidenceKind.VISUAL,), allow_rule=True
    ),
    "thickness": FieldPolicy("thickness", (EvidenceKind.VISUAL,), allow_rule=True),
    "color": FieldPolicy("color", (EvidenceKind.VISUAL,), allow_rule=True),
    "material_composition": FieldPolicy(
        "material_composition", (EvidenceKind.TEXT,), allow_rule=True
    ),
    "material_percentage": FieldPolicy(
        "material_percentage", (EvidenceKind.TEXT,), allow_rule=False
    ),
}


def map_platform_field(platform: str, field_label: str) -> str:
    key = (
        _normalize_component(platform),
        _normalize_component(field_label, field_label=True),
    )
    try:
        return ALIASES[key]
    except KeyError as exc:
        raise FieldMappingError("unknown platform field mapping: {!r}".format(key)) from exc


def policy_for(canonical_name: str) -> FieldPolicy:
    try:
        return POLICIES[canonical_name]
    except KeyError as exc:
        raise FieldMappingError(
            "unknown canonical field policy: {!r}".format(canonical_name)
        ) from exc


__all__ = [
    "ALIASES",
    "EvidenceKind",
    "FieldMappingError",
    "FieldPolicy",
    "build_alias_registry",
    "map_platform_field",
    "policy_for",
]
