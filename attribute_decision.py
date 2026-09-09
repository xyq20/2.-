from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, Tuple

from canonical_fields import (
    EvidenceKind,
    FieldMappingError,
    map_platform_field,
    policy_for,
)


class DecisionStatus(str, Enum):
    AUTO_FILL_READY = "auto_fill_ready"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True)
class DecisionInput:
    canonical_field: str
    proposed_value_id: str
    evidence_kinds: Tuple[str, ...]
    source: str
    mature_rule: bool = False
    support_count: int = 0
    calibrated_acceptance_rate: float = 0.0
    has_conflict: bool = False
    expected_snapshot_version: Optional[str] = None
    # Accepted only so an upstream model's self-report can be explicitly ignored.
    model_confidence: Optional[float] = None


@dataclass(frozen=True)
class ValidatedDecision:
    status: DecisionStatus
    value_id: Optional[str]
    value_label: Optional[str]
    reason_code: str
    snapshot_version: str


class CandidateValueProtocol(Protocol):
    @property
    def value_id(self) -> str:
        ...

    @property
    def label(self) -> str:
        ...


class CandidateSnapshotProtocol(Protocol):
    @property
    def platform_id(self) -> str:
        ...

    @property
    def category_leaf_id(self) -> str:
        ...

    @property
    def field_id(self) -> str:
        ...

    @property
    def field_label(self) -> str:
        ...

    @property
    def values(self) -> Tuple[CandidateValueProtocol, ...]:
        ...

    @property
    def schema_version(self) -> str:
        ...

    @property
    def custom_allowed(self) -> bool:
        ...

    @property
    def snapshot_version(self) -> str:
        ...


def _review(reason_code: str, snapshot_version: str) -> ValidatedDecision:
    return ValidatedDecision(
        status=DecisionStatus.REVIEW_REQUIRED,
        value_id=None,
        value_label=None,
        reason_code=reason_code,
        snapshot_version=snapshot_version,
    )


def _evidence_names(decision: DecisionInput) -> Tuple[str, ...]:
    return tuple(
        evidence.value if isinstance(evidence, EvidenceKind) else str(evidence)
        for evidence in decision.evidence_kinds
    )


def validate_decision(
    decision: DecisionInput, snapshot: CandidateSnapshotProtocol
) -> ValidatedDecision:
    snapshot_version = str(snapshot.snapshot_version)

    try:
        snapshot_canonical_field = map_platform_field(
            snapshot.platform_id, snapshot.field_label
        )
    except FieldMappingError:
        return _review("field_mapping_required", snapshot_version)

    if decision.canonical_field != snapshot_canonical_field:
        return _review("canonical_field_mismatch", snapshot_version)

    try:
        policy = policy_for(snapshot_canonical_field)
    except FieldMappingError:
        return _review("field_mapping_required", snapshot_version)

    if (
        decision.expected_snapshot_version is not None
        and decision.expected_snapshot_version != snapshot_version
    ):
        return _review("snapshot_changed", snapshot_version)

    if decision.has_conflict:
        return _review("evidence_conflict", snapshot_version)

    evidence_names = set(_evidence_names(decision))
    for required in policy.required_evidence:
        if required.value not in evidence_names:
            return _review(
                "required_{}_evidence_missing".format(required.value),
                snapshot_version,
            )

    candidates = tuple(snapshot.values)
    if not candidates:
        return _review("candidate_empty", snapshot_version)

    candidate_ids = tuple(candidate.value_id for candidate in candidates)
    if any(
        not isinstance(candidate_id, str) or not candidate_id.strip()
        for candidate_id in candidate_ids
    ):
        return _review("candidate_invalid", snapshot_version)
    if len(candidate_ids) != len(set(candidate_ids)):
        return _review("candidate_ambiguous", snapshot_version)

    if (
        not isinstance(decision.proposed_value_id, str)
        or not decision.proposed_value_id.strip()
    ):
        return _review("proposed_value_invalid", snapshot_version)

    matches = tuple(
        candidate
        for candidate in candidates
        if candidate.value_id == decision.proposed_value_id
    )
    if not matches:
        is_label_only_value = any(
            candidate.label == decision.proposed_value_id for candidate in candidates
        )
        if is_label_only_value and not snapshot.custom_allowed:
            return _review("free_text_not_allowed", snapshot_version)
        return _review("candidate_missing", snapshot_version)

    source = decision.source
    if source == "explicit_text":
        if EvidenceKind.TEXT.value not in evidence_names:
            return _review("explicit_text_evidence_missing", snapshot_version)
    elif source == "human_override":
        pass
    elif source == "mature_rule":
        if not policy.allow_rule:
            return _review("rule_not_allowed", snapshot_version)
        if decision.mature_rule is not True:
            return _review("rule_not_mature", snapshot_version)
    elif source == "constrained_model":
        support_is_valid = (
            type(decision.support_count) is int and decision.support_count >= 3
        )
        rate = decision.calibrated_acceptance_rate
        rate_is_valid = (
            type(rate) in (int, float)
            and not isinstance(rate, bool)
            and math.isfinite(rate)
            and 0.95 <= rate <= 1.0
        )
        if not support_is_valid or not rate_is_valid:
            return _review("confidence_gate_not_met", snapshot_version)
    else:
        return _review("unknown_source", snapshot_version)

    candidate = matches[0]
    return ValidatedDecision(
        status=DecisionStatus.AUTO_FILL_READY,
        value_id=candidate.value_id,
        value_label=candidate.label,
        reason_code="validated",
        snapshot_version=snapshot_version,
    )


__all__ = [
    "CandidateSnapshotProtocol",
    "CandidateValueProtocol",
    "DecisionInput",
    "DecisionStatus",
    "ValidatedDecision",
    "validate_decision",
]
