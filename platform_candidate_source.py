from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable, Tuple

from platform_schema import FieldOption


class CandidateSourceError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class DomCandidate:
    value_id: str
    label: str
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "value_id", str(self.value_id or ""))
        object.__setattr__(self, "label", str(self.label))


def _normalize_label(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip()


def first_per_label(values):
    """Keep the first entry for identical names, preserving source order."""
    seen = set()
    result = []
    for value in values:
        label = value.label.strip()
        if label not in seen:
            seen.add(label)
            result.append(value)
    return tuple(result)


def reconcile_candidates(
    api_values: Iterable[FieldOption],
    dom_values: Iterable[DomCandidate],
) -> Tuple[FieldOption, ...]:
    """Return the uniquely clickable candidates visible in the current DOM.

    API JSON is used to retain stable platform IDs when its label can be
    correlated with the page.  Dynamic product forms may return a partial,
    delayed or differently encoded API candidate list, so set/ID drift alone
    is not a write blocker.  The current visible DOM remains the actionable
    source; identical visible labels retain the first enabled entry.
    """

    authoritative = tuple(api_values)
    visible = first_per_label(value for value in dom_values if value.enabled)
    if not visible:
        raise CandidateSourceError("dom_candidates_unavailable")

    normalized_dom_labels = tuple(_normalize_label(value.label) for value in visible)
    if any(not label for label in normalized_dom_labels):
        raise CandidateSourceError("dom_candidate_invalid")
    if len(normalized_dom_labels) != len(set(normalized_dom_labels)):
        raise CandidateSourceError("dom_candidate_ambiguous")

    valid_api = tuple(
        value
        for value in authoritative
        if value.value_id.strip() and _normalize_label(value.label)
    )
    reconciled = []
    for position, dom_value in enumerate(visible):
        normalized_label = _normalize_label(dom_value.label)
        label_matches = tuple(
            value
            for value in valid_api
            if _normalize_label(value.label) == normalized_label
        )
        id_and_label_matches = tuple(
            value
            for value in label_matches
            if dom_value.value_id and value.value_id == dom_value.value_id
        )
        if len(id_and_label_matches) == 1:
            matched = id_and_label_matches[0]
            reconciled.append(FieldOption(matched.value_id, matched.label, position))
        elif len(label_matches) == 1:
            # Some Element UI controls expose their visible label as ``value``
            # while the API carries an opaque ID.  An exact unique label keeps
            # the API identity without requiring impossible DOM ID equality.
            matched = label_matches[0]
            reconciled.append(FieldOption(matched.value_id, matched.label, position))
        else:
            reconciled.append(
                FieldOption(
                    dom_value.value_id.strip() or dom_value.label,
                    dom_value.label,
                    position,
                )
            )
    return tuple(reconciled)


def validate_observed_selection(
    api_values: Iterable[FieldOption],
    selected_labels: Iterable[str],
) -> Tuple[FieldOption, ...]:
    """Validate saved DOM selections against JSON without opening a dropdown.

    A saved product already exposes the chosen labels in the live DOM.  When
    every selected label maps to one unique API candidate, the complete API
    candidate set can be recorded for learning without paying the cost of
    opening and scraping the same dropdown again.  This is deliberately only
    a fast path: incomplete or ambiguous JSON falls back to the adapter's
    established DOM flow and never blocks a valid saved value.
    """

    authoritative = first_per_label(api_values)
    selected = tuple(_normalize_label(value) for value in selected_labels)
    if not selected or any(not value for value in selected):
        raise CandidateSourceError("dom_selection_unavailable")

    valid_api = tuple(
        value
        for value in authoritative
        if value.value_id.strip() and _normalize_label(value.label)
    )
    if len(valid_api) != len(authoritative) or not valid_api:
        raise CandidateSourceError("api_candidate_invalid")
    value_ids = tuple(value.value_id.strip() for value in valid_api)
    if len(value_ids) != len(set(value_ids)):
        raise CandidateSourceError("api_candidate_ambiguous")

    for selected_label in selected:
        matches = tuple(
            value
            for value in valid_api
            if _normalize_label(value.label) == selected_label
        )
        if len(matches) != 1:
            raise CandidateSourceError("observed_selection_not_unique_in_api")
    return valid_api


__all__ = [
    "CandidateSourceError",
    "DomCandidate",
    "reconcile_candidates",
    "validate_observed_selection",
]
