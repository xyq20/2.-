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


def reconcile_candidates(
    api_values: Iterable[FieldOption],
    dom_values: Iterable[DomCandidate],
) -> Tuple[FieldOption, ...]:
    authoritative = tuple(api_values)
    if not authoritative:
        raise CandidateSourceError("api_candidates_unavailable")

    api_ids = tuple(value.value_id for value in authoritative)
    if any(not value_id.strip() for value_id in api_ids) or any(
        not _normalize_label(value.label) for value in authoritative
    ):
        raise CandidateSourceError("api_candidate_invalid")
    if len(api_ids) != len(set(api_ids)):
        raise CandidateSourceError("api_candidate_duplicate_id")

    visible = tuple(value for value in dom_values if value.enabled)
    if not visible:
        raise CandidateSourceError("dom_candidates_unavailable")

    consumed = set()
    for api_value in authoritative:
        matches = []
        for index, dom_value in enumerate(visible):
            if dom_value.value_id:
                matches_id = dom_value.value_id == api_value.value_id
                matches_label = _normalize_label(dom_value.label) == _normalize_label(
                    api_value.label
                )
                if matches_id and matches_label:
                    matches.append(index)
            elif _normalize_label(dom_value.label) == _normalize_label(api_value.label):
                matches.append(index)

        if len(matches) > 1:
            raise CandidateSourceError("dom_candidate_ambiguous")
        if not matches or matches[0] in consumed:
            raise CandidateSourceError("candidate_source_mismatch")
        consumed.add(matches[0])

    if len(consumed) != len(visible):
        raise CandidateSourceError("candidate_source_mismatch")
    return authoritative


__all__ = [
    "CandidateSourceError",
    "DomCandidate",
    "reconcile_candidates",
]
