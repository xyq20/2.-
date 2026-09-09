from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import unquote


class _JsonModel:
    def to_dict(self) -> Dict[str, Any]:
        return to_dict(self)


@dataclass(frozen=True)
class OptionSummary(_JsonModel):
    source: str
    count: int
    sample: Tuple[Tuple[str, str], ...]
    truncated: bool
    sha256: str

    def __post_init__(self) -> None:
        source = str(self.source)
        object.__setattr__(self, "source", source)
        if source.strip().lower() in _SAMPLE_HIDDEN_SOURCES:
            object.__setattr__(self, "sample", ())
            object.__setattr__(self, "truncated", self.count > 0)


@dataclass(frozen=True)
class FieldOption(_JsonModel):
    value_id: str
    label: str
    position: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "value_id", str(self.value_id))
        object.__setattr__(self, "label", str(self.label))
        object.__setattr__(self, "position", int(self.position))


@dataclass(frozen=True)
class EndpointObservation(_JsonModel):
    method: str
    path: str
    parameter_names: Tuple[str, ...] = ()
    status: str = "ok"
    http_status: Optional[int] = None
    sanitized_message: str = ""
    required_for: str = "auxiliary"


@dataclass(frozen=True)
class CategoryCandidate(_JsonModel):
    leaf_id: str
    path: Tuple[str, ...]
    rank: Optional[int] = None
    score: Optional[float] = None
    recommended: bool = False
    validation_status: str = "unverified"
    issue_code: str = ""

    def __post_init__(self) -> None:
        if self.leaf_id is not None:
            object.__setattr__(self, "leaf_id", str(self.leaf_id))


@dataclass(frozen=True)
class CategoryResolution(_JsonModel):
    status: str
    source: str = ""
    selected: Optional[CategoryCandidate] = None
    candidates: Tuple[CategoryCandidate, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class DomLocatorHint(_JsonModel):
    strategy: str
    section_label: str
    anchor: str
    occurrence: int = 1
    control_type: str = "unknown"


@dataclass(frozen=True)
class FieldSchema(_JsonModel):
    schema_key: str
    source_id: Optional[str] = None
    label: str = ""
    section: str = ""
    control_type: str = "unknown"
    value_type: str = "string"
    required: Optional[bool] = None
    multiple: Optional[bool] = None
    custom_allowed: Optional[bool] = None
    option_summary: Optional[OptionSummary] = None
    option_values: Tuple[FieldOption, ...] = ()
    dependencies: Tuple[str, ...] = ()
    api_paths: Tuple[str, ...] = ()
    dom_locator_hint: Optional[DomLocatorHint] = None
    presence: str = "api_only"

    def __post_init__(self) -> None:
        if self.source_id is not None:
            object.__setattr__(self, "source_id", str(self.source_id))
        summary = self.option_summary
        if (
            summary is not None
            and summary.source.strip().lower() in _SAMPLE_HIDDEN_SOURCES
        ):
            object.__setattr__(self, "option_values", ())


@dataclass(frozen=True)
class SectionSchema(_JsonModel):
    key: str
    label: str
    order: int = 0
    visible_when: Tuple[str, ...] = ()
    fields: Tuple[FieldSchema, ...] = ()


@dataclass(frozen=True)
class PlatformSchema(_JsonModel):
    platform_id: str
    capture_status: str
    category: CategoryResolution
    fixed_sections: Tuple[SectionSchema, ...] = ()
    dynamic_sections: Tuple[SectionSchema, ...] = ()
    endpoints: Tuple[EndpointObservation, ...] = ()
    issues: Tuple[str, ...] = ()
    schema_version: int = 1


_URL_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://|www\.)[^\s<>\"']+",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[ \t-]*)?1[3-9]\d(?:[ \t-]?\d{4}){2}(?!\d)")
_LONG_NUMBER_RE = re.compile(r"(?<!\d)\d{6,}(?!\d)")
_CREDENTIAL_MARKER_RE = re.compile(
    r"authorization|cookie|token|secret|password|session|csrf|credential|"
    r"(?:access|private|api|x[\s_-]*api)[\s_-]*key|"
    r"\bkey(?=\s*[:=])|\b(?:bearer|basic|digest)\b",
    re.IGNORECASE,
)
_SHORT_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9A-Fa-f]{4})")
_LONG_UNICODE_ESCAPE_RE = re.compile(r"\\U([0-9A-Fa-f]{8})")
_HEX_ESCAPE_RE = re.compile(r"\\x([0-9A-Fa-f]{2})")
_SIMPLE_ESCAPE_RE = re.compile(r"\\([\\\"'/bfnrt])")
_SIMPLE_ESCAPE_VALUES = {
    "\\": "\\",
    '"': '"',
    "'": "'",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_SAMPLE_HIDDEN_SOURCES = frozenset(("shop", "logistics", "freight"))
_MAX_CREDENTIAL_DECODE_ROUNDS = 5


def _option_pair(option: Any) -> Tuple[str, str]:
    if isinstance(option, Mapping):
        option_id = option.get("id")
        if option_id is None:
            option_id = option.get("value_id")
        if option_id is None:
            option_id = option.get("value")

        label = option.get("label")
        if label is None:
            label = option.get("name")
        if label is None:
            label = option.get("displayName")
        if label is None:
            label = option.get("display_name")
        if option_id is None and label is not None:
            option_id = label
        if label is None and option_id is not None:
            label = option_id
        return ("" if option_id is None else str(option_id), "" if label is None else str(label))

    if isinstance(option, (tuple, list)) and len(option) >= 2:
        return (str(option[0]), str(option[1]))

    value = str(option)
    return (value, value)


def option_summary(
    options: Iterable[Any],
    include_samples: bool = True,
    limit: int = 20,
    source: str = "inline",
) -> OptionSummary:
    if limit < 0 or limit > 20:
        raise ValueError("limit must be between 0 and 20")

    unique = tuple(sorted(set(_option_pair(option) for option in options)))
    canonical = json.dumps(unique, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    source = str(source)
    samples_allowed = (
        include_samples and source.strip().lower() not in _SAMPLE_HIDDEN_SOURCES
    )
    sample = unique[:limit] if samples_allowed else ()
    return OptionSummary(
        source=source,
        count=len(unique),
        sample=sample,
        truncated=len(sample) < len(unique),
        sha256=digest,
    )


def field_options(
    options: Iterable[Any],
    source: str = "api",
) -> Tuple[FieldOption, ...]:
    if str(source).strip().lower() in _SAMPLE_HIDDEN_SOURCES:
        return ()
    return tuple(
        FieldOption(value_id, label, position)
        for position, (value_id, label) in enumerate(_option_pair(option) for option in options)
    )


def _decode_unicode_escape(match: "re.Match[str]") -> str:
    codepoint = int(match.group(1), 16)
    if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
        return "\ufffd"
    return chr(codepoint)


def _credential_detection_view(message: str) -> str:
    current = str(message)
    for _attempt in range(_MAX_CREDENTIAL_DECODE_ROUNDS):
        previous = current
        current = html.unescape(current)
        current = unquote(current, errors="replace")
        current = _LONG_UNICODE_ESCAPE_RE.sub(_decode_unicode_escape, current)
        current = _SHORT_UNICODE_ESCAPE_RE.sub(_decode_unicode_escape, current)
        current = _HEX_ESCAPE_RE.sub(_decode_unicode_escape, current)
        current = _SIMPLE_ESCAPE_RE.sub(
            lambda match: _SIMPLE_ESCAPE_VALUES[match.group(1)],
            current,
        )
        current = unicodedata.normalize("NFKC", current)
        current = "".join(
            character
            for character in current
            if unicodedata.category(character) not in ("Cf", "Mn")
        )
        if current == previous:
            return current
    # Deeply nested encodings are not useful in a safe diagnostic summary.  If
    # decoding cannot stabilize within the fixed budget, fail closed.
    return "credential"


def sanitize_message(message: Any) -> str:
    if message is None:
        return ""
    sanitized = str(message)
    if _CREDENTIAL_MARKER_RE.search(_credential_detection_view(sanitized)):
        return "[redacted-credential-message]"
    sanitized = _URL_RE.sub("[redacted-url]", sanitized)
    sanitized = _EMAIL_RE.sub("[redacted-email]", sanitized)
    sanitized = _PHONE_RE.sub("[redacted-phone]", sanitized)
    sanitized = _LONG_NUMBER_RE.sub("[redacted-number]", sanitized)
    return sanitized[:200]


def to_dict(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_dict(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_dict(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_dict(item) for item in value]
    return value


__all__ = [
    "CategoryCandidate",
    "CategoryResolution",
    "DomLocatorHint",
    "EndpointObservation",
    "FieldSchema",
    "OptionSummary",
    "PlatformSchema",
    "SectionSchema",
    "option_summary",
    "sanitize_message",
    "to_dict",
]
