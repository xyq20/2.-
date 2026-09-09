from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple
import uuid

from attribute_decision import DecisionInput, DecisionStatus, validate_decision
from canonical_fields import FieldMappingError, map_platform_field
from learning_client import flush_outbox_async
from learning_models import CandidateSnapshot, CandidateValue, canonical_sha256
from learning_store import LearningStore


UNSUPPORTED_CONTROL_TYPES = frozenset({"shop", "logistics", "freight"})


@dataclass(frozen=True)
class AttributeRequest:
    platform_id: str
    category_leaf_id: str
    field_id: str
    field_label: str
    candidates: Tuple[CandidateValue, ...]
    excel_value: str
    evidence: Mapping[str, Any]
    custom_allowed: bool
    schema_version: str
    control_type: str = "select"


@dataclass(frozen=True)
class ResolvedAttribute:
    value_id: str
    label: str
    source: str
    snapshot_version: str


class ReviewRequired(RuntimeError):
    def __init__(
        self,
        review_id: str,
        request: AttributeRequest,
        reason_code: str,
        snapshot_version: str,
    ) -> None:
        super().__init__(f"属性需要人工审核：{request.field_label}")
        self.review_id = review_id
        self.request = request
        self.reason_code = reason_code
        self.snapshot_version = snapshot_version


class AttributeRuntime:
    def __init__(
        self,
        store: LearningStore,
        client: Any,
        run_id: str,
        product_version: str,
        *,
        device_id: str = "",
    ) -> None:
        self.store = store
        self.client = client
        self.run_id = run_id
        self.product_version = product_version
        self.device_id = device_id or store.get_or_create_device_id()

    async def _flush(self) -> None:
        await flush_outbox_async(
            self.store,
            self.client,
            datetime.now(timezone.utc),
        )

    async def _raise_review(
        self,
        request: AttributeRequest,
        snapshot: CandidateSnapshot,
        canonical_field: Optional[str],
        reason_code: str,
        suggested_value_id: Optional[str] = None,
    ) -> None:
        review_id = uuid.uuid4().hex
        evidence = {
            "kinds": sorted(str(key) for key in request.evidence),
            "snapshot_version": snapshot.snapshot_version,
        }
        payload = {
            "id": review_id,
            "run_id": self.run_id,
            "device_id": self.device_id,
            "product_version": self.product_version,
            "platform_id": request.platform_id,
            "category_leaf_id": request.category_leaf_id,
            "field_id": request.field_id,
            "field_label": request.field_label,
            "canonical_field": canonical_field,
            "snapshot_version": snapshot.snapshot_version,
            "suggested_value_id": suggested_value_id,
            "reason_code": reason_code,
            "evidence_json": evidence,
        }
        self.store.enqueue(
            f"review.created:{self.run_id}:{snapshot.snapshot_version}",
            "review.created",
            payload,
        )
        await self._flush()
        raise ReviewRequired(
            review_id,
            request,
            reason_code,
            snapshot.snapshot_version,
        )

    async def resolve(self, request: AttributeRequest) -> ResolvedAttribute:
        if request.control_type.casefold() in UNSUPPORTED_CONTROL_TYPES:
            raise ValueError("operational selector cannot use attribute learning")
        snapshot = CandidateSnapshot(
            request.platform_id,
            request.category_leaf_id,
            request.field_id,
            request.field_label,
            request.candidates,
            request.schema_version,
            request.custom_allowed,
        )
        self.store.save_candidate_snapshot(snapshot)
        try:
            canonical_field = map_platform_field(
                request.platform_id, request.field_label
            )
        except FieldMappingError:
            canonical_field = None
        self.store.enqueue(
            f"snapshot.created:{snapshot.snapshot_version}",
            "snapshot.created",
            {
                "snapshot_version": snapshot.snapshot_version,
                "platform_id": snapshot.platform_id,
                "category_leaf_id": snapshot.category_leaf_id,
                "field_id": snapshot.field_id,
                "field_label": snapshot.field_label,
                "schema_version": snapshot.schema_version,
                "custom_allowed": snapshot.custom_allowed,
                "canonical_field": canonical_field,
                "control_type": request.control_type,
                "options": [
                    {
                        "value_id": candidate.value_id,
                        "label": candidate.label,
                        "position": position,
                    }
                    for position, candidate in enumerate(snapshot.values)
                ],
            },
        )
        if canonical_field is None:
            await self._flush()
            await self._raise_review(
                request, snapshot, None, "field_mapping_required"
            )
        exact_excel = tuple(
            candidate
            for candidate in snapshot.values
            if request.excel_value.strip()
            and request.excel_value.strip()
            in {candidate.value_id, candidate.label}
        )
        if len(exact_excel) == 1:
            text_payload = {
                "product_version": self.product_version,
                "source": "excel",
                "payload_json": {
                    "values": {
                        canonical_field: {"value_id": exact_excel[0].value_id}
                    },
                    "text_tokens": [request.excel_value.strip()],
                },
            }
            self.store.enqueue(
                "text_facts.created:"
                + canonical_sha256(
                    {
                        "run_id": self.run_id,
                        "snapshot": snapshot.snapshot_version,
                        "value": exact_excel[0].value_id,
                    }
                ),
                "text_facts.created",
                text_payload,
            )
        await self._flush()
        response = await asyncio.to_thread(
            self.client.decide,
            {
                "product_version": self.product_version,
                "platform_id": request.platform_id,
                "category_leaf_id": request.category_leaf_id,
                "field_id": request.field_id,
                "canonical_field": canonical_field,
                "snapshot_version": snapshot.snapshot_version,
            },
        )
        if response.get("status") != "auto_fill_ready":
            await self._raise_review(
                request,
                snapshot,
                canonical_field,
                str(response.get("reason_code") or "insufficient_evidence"),
                response.get("value_id")
                if isinstance(response.get("value_id"), str)
                else None,
            )
        proposed = response.get("value_id")
        decision = validate_decision(
            DecisionInput(
                canonical_field=canonical_field,
                proposed_value_id=str(proposed or ""),
                evidence_kinds=tuple(
                    str(value) for value in response.get("evidence_kinds", ())
                ),
                source=str(response.get("source") or ""),
                mature_rule=response.get("mature_rule") is True,
                support_count=response.get("support_count", 0),
                calibrated_acceptance_rate=response.get(
                    "calibrated_acceptance_rate", 0.0
                ),
                expected_snapshot_version=str(
                    response.get("snapshot_version") or ""
                ),
            ),
            snapshot,
        )
        if decision.status is not DecisionStatus.AUTO_FILL_READY:
            await self._raise_review(
                request,
                snapshot,
                canonical_field,
                decision.reason_code,
                str(proposed) if isinstance(proposed, str) else None,
            )
        return ResolvedAttribute(
            decision.value_id or "",
            decision.value_label or "",
            str(response.get("source") or ""),
            decision.snapshot_version,
        )


__all__ = [
    "AttributeRequest",
    "AttributeRuntime",
    "ResolvedAttribute",
    "ReviewRequired",
]
