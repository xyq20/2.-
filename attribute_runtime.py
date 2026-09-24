from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple
import uuid

from attribute_decision import DecisionInput, DecisionStatus, validate_decision
from canonical_fields import FieldMappingError, map_platform_field
from historical_readbacks import (
    VerifiedAttributeHistory,
    normalize_history_field_label,
)
from learning_client import flush_outbox_async
from learning_models import CandidateSnapshot, CandidateValue, canonical_sha256
from learning_store import LearningStore
from platform_registry import canonical_platform_name
from platform_candidate_source import first_per_label


UNSUPPORTED_CONTROL_TYPES = frozenset({"shop", "logistics", "freight"})

MULTI_CHOICE_SEPARATORS = r"[,，、;；]"
EXCEL_ALTERNATIVE_SEPARATORS = r"[,，、;；/／]"
MULTI_SELECT_CONTROL_TYPES = frozenset({"multi_select", "multi-select", "multiselect"})


def _normalize_choice_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def split_multi_choice(value: object) -> Tuple[str, ...]:
    """Split a confirmed/review value into its chosen parts.

    审核确认值兼容多选：云端可在 final_value_id 里用逗号分隔多个
    valueId 或 label（例如“101,102”或“男,女”）。逗号与 Excel 的
    “分组全选”语法一致，消费端 selection_value_groups 会按同样规则
    拆组后全选。
    """
    return tuple(
        part.strip()
        for part in re.split(MULTI_CHOICE_SEPARATORS, str(value or ""))
        if part.strip()
    )


def excel_alternative_set(excel_value: object) -> frozenset:
    """All normalized alternatives (comma groups + slash ORs) in Excel text."""
    return frozenset(
        _normalize_choice_text(part)
        for part in re.split(
            EXCEL_ALTERNATIVE_SEPARATORS, str(excel_value or "")
        )
        if part.strip()
    )


def _matched_excel_variants(
    excel_value: object,
    candidates: Tuple[CandidateValue, ...],
    *,
    control_type: str,
) -> Tuple[Tuple[CandidateValue, ...], ...]:
    """Resolve ordered Excel alternatives against live platform candidates.

    For a proven multi-select control, slash separates alternative plans and
    commas separate the values required by one plan.  Thus
    ``秋季，冬季/春秋冬/春秋`` means ``[秋季 + 冬季]`` first, then either
    one-value fallback.  Single-select controls keep commas inside the label.
    """

    text = str(excel_value or "").strip()
    if not text:
        return ()
    if str(control_type).casefold() in MULTI_SELECT_CONTROL_TYPES:
        raw_variants = tuple(
            tuple(
                part.strip()
                for part in re.split(MULTI_CHOICE_SEPARATORS, alternative)
                if part.strip()
            )
            for alternative in re.split(r"[/／]", text)
            if alternative.strip()
        )
    else:
        raw_variants = ((text,),)

    matched_variants = []
    for variant in raw_variants:
        matched = []
        for part in variant:
            found = tuple(
                candidate
                for candidate in candidates
                if part in {candidate.value_id, candidate.label}
            )
            if len(found) != 1 or found[0] in matched:
                matched = []
                break
            matched.append(found[0])
        if matched:
            matched_variants.append(tuple(matched))
    return tuple(matched_variants)


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


class ReviewBatchRequired(RuntimeError):
    """Raised after one platform has collected every unresolved attribute."""

    def __init__(self, reviews: Tuple[ReviewRequired, ...]) -> None:
        if not reviews:
            raise ValueError("review batch cannot be empty")
        labels = "、".join(review.request.field_label for review in reviews)
        super().__init__(f"平台属性需要人工审核：{labels}")
        self.reviews = reviews


class AttributeRuntime:
    def __init__(
        self,
        store: LearningStore,
        client: Any,
        run_id: str,
        product_version: str,
        *,
        device_id: str = "",
        verified_history: Optional[VerifiedAttributeHistory] = None,
    ) -> None:
        self.store = store
        self.client = client
        self.run_id = run_id
        self.product_version = product_version
        self.device_id = device_id or store.get_or_create_device_id()
        self.verified_history = dict(verified_history or {})
        self._flush_lock = asyncio.Lock()
        self._background_flush: Optional[asyncio.Task[int]] = None
        self._collecting_platform_id = ""
        self._deferred_reviews: dict[str, ReviewRequired] = {}
        self._resolved_attributes: dict[
            tuple[str, str], tuple[AttributeRequest, ResolvedAttribute]
        ] = {}

    def begin_review_collection(self, platform_id: str) -> None:
        """Start a fresh platform pass that defers reviews until its boundary."""
        self._collecting_platform_id = canonical_platform_name(platform_id)
        self._deferred_reviews.clear()

    @property
    def deferred_reviews(self) -> Tuple[ReviewRequired, ...]:
        return tuple(self._deferred_reviews.values())

    @property
    def has_deferred_reviews(self) -> bool:
        return bool(self._deferred_reviews)

    def raise_deferred_reviews(self) -> None:
        if self._deferred_reviews:
            raise ReviewBatchRequired(self.deferred_reviews)

    async def _flush(self) -> int:
        # All event delivery uses one ordered worker.  Snapshot/review/checkpoint
        # dependencies therefore keep their durable SQLite order even while the
        # browser continues filling the next field.
        async with self._flush_lock:
            return await flush_outbox_async(
                self.store,
                self.client,
                datetime.now(timezone.utc),
            )

    def _schedule_flush(self) -> None:
        """Start best-effort delivery without blocking a verified local choice."""
        if self._background_flush is not None and self._background_flush.done():
            # Never replace a completed worker before observing its result.
            # Permanent API failures must stop the run instead of becoming an
            # unhandled background-task warning while browser filling continues.
            self._background_flush.result()
            self._background_flush = None
        if self._background_flush is None:
            self._background_flush = asyncio.create_task(self._flush())

    async def drain(self) -> None:
        """Finish ordered delivery at a platform/checkpoint boundary."""
        background = self._background_flush
        if background is not None:
            await background
        # Events can be added while the previous worker is awaiting the network.
        # One final locked pass includes those events and the newest checkpoint.
        while await self._flush():
            pass

    async def _raise_review(
        self,
        request: AttributeRequest,
        snapshot: CandidateSnapshot,
        canonical_field: Optional[str],
        reason_code: str,
        suggested_value_id: Optional[str] = None,
    ) -> None:
        existing = self._deferred_reviews.get(snapshot.snapshot_version)
        if existing is not None:
            return
        review_id = uuid.uuid4().hex
        evidence = {
            **dict(request.evidence),
            "kinds": sorted(str(key) for key in request.evidence),
            "snapshot_version": snapshot.snapshot_version,
            "reuse_context": {
                "excel_candidates": [v.strip() for v in re.split(r'[/／]', request.excel_value) if v.strip()],
                "control_type": request.control_type,
            },
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
        review = ReviewRequired(
            review_id,
            request,
            reason_code,
            snapshot.snapshot_version,
        )
        if self._collecting_platform_id:
            request_platform = canonical_platform_name(request.platform_id)
            if request_platform != self._collecting_platform_id:
                raise ValueError(
                    "attribute review platform changed during collection"
                )
            self._deferred_reviews[snapshot.snapshot_version] = review
            self._schedule_flush()
            return
        await self._flush()
        raise review

    def confirmed_choice(self, request: AttributeRequest) -> Optional[ResolvedAttribute]:
        """Read a product/field/snapshot scoped approval without attempting a write."""
        request = replace(request, candidates=first_per_label(request.candidates))
        snapshot = CandidateSnapshot(
            request.platform_id, request.category_leaf_id, request.field_id,
            request.field_label, request.candidates, request.schema_version,
            request.custom_allowed,
        )
        value = self.store.load_review_resolution(
            product_version=self.product_version,
            platform_id=canonical_platform_name(request.platform_id),
            snapshot_version=snapshot.snapshot_version,
        )
        if value is None:
            return None
        matches = [candidate for candidate in request.candidates
                   if value in (candidate.value_id, candidate.label)]
        if len(matches) == 1:
            return ResolvedAttribute(matches[0].value_id, matches[0].label,
                                     "human_override", snapshot.snapshot_version)
        if not matches and request.custom_allowed:
            return ResolvedAttribute(value, value, "human_override", snapshot.snapshot_version)
        return None

    def reusable_choice(self, request: AttributeRequest) -> Optional[ResolvedAttribute]:
        """Read-only preflight before an adapter attempts Excel input.

        An approval is exact for the same product/snapshot, and the same
        product may reuse its approval across category snapshots.  A different
        product's review is only training evidence: it must pass the learning
        service's confidence gate before this runtime can use it.
        """
        approved = self.confirmed_choice(request)
        if approved is not None:
            return approved
        same_product_review = self._cross_product_choice(
            request, same_product_only=True
        )
        if same_product_review is not None:
            return same_product_review
        matched_excel = _matched_excel_variants(
            request.excel_value,
            request.candidates,
            control_type=request.control_type,
        )
        exact = matched_excel[0] if matched_excel else ()
        labels = self.verified_history.get((canonical_platform_name(request.platform_id),
                                           normalize_history_field_label(request.field_label)), ())
        if labels:
            matches = [c for c in request.candidates if c.label in labels]
            # 多选字段的历史是多个 label（如 适用季节=秋季,冬季）：每个都唯一
            # 命中当前候选时组合复用，与 resolve 的多值历史语义保持一致。
            if (
                len(matches) == len(labels)
                and len(set(matches)) == len(matches)
                and (not exact or all(match in exact for match in matches))
            ):
                return ResolvedAttribute(
                    ",".join(match.value_id for match in matches),
                    ",".join(match.label for match in matches),
                    'verified_history',
                    '',
                )
        if exact:
            return None
        return None

    def _cross_product_choice(
        self, request: AttributeRequest, *, same_product_only: bool = False
    ) -> Optional[ResolvedAttribute]:
        request = replace(request, candidates=first_per_label(request.candidates))
        # A selection_only request means the confirmed value was already tried
        # against the live control and could not be written. Reusing it would
        # repeat the same failed click instead of opening a fresh review.
        if request.evidence.get("selection_only"):
            return None
        snapshot = CandidateSnapshot(
            request.platform_id, request.category_leaf_id, request.field_id,
            request.field_label, request.candidates, request.schema_version,
            request.custom_allowed,
        )
        try:
            canonical_field = map_platform_field(
                request.platform_id, request.field_label
            )
        except FieldMappingError:
            canonical_field = None
        label = self.store.reusable_review_label(
            snapshot,
            request.excel_value,
            request.control_type,
            canonical_field,
            self.product_version if same_product_only else None,
        )
        parts = split_multi_choice(label)
        matches = []
        for part in parts:
            part_matches = [v for v in request.candidates if v.label == part]
            if len(part_matches) != 1 or part_matches[0] in matches:
                return None
            matches.append(part_matches[0])
        if not matches:
            return None
        return ResolvedAttribute(
            ",".join(match.value_id for match in matches),
            ",".join(match.label for match in matches),
                                 'cross_product_human', snapshot.snapshot_version)

    def _remember_resolved(
        self, request: AttributeRequest, resolved: ResolvedAttribute
    ) -> ResolvedAttribute:
        self._resolved_attributes[
            (
                canonical_platform_name(request.platform_id),
                normalize_history_field_label(request.field_label),
            )
        ] = (request, resolved)
        return resolved

    def _queue_candidate_snapshot(
        self,
        snapshot: CandidateSnapshot,
        canonical_field: Optional[str],
        control_type: str,
    ) -> None:
        self.store.save_candidate_snapshot(snapshot)
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
                "control_type": control_type,
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

    def record_verified_readbacks(
        self, platform_id: str, attributes: Mapping[str, Any]
    ) -> int:
        """Queue field-level learning only after save/reopen readback succeeds."""
        platform = canonical_platform_name(platform_id)
        recorded = 0
        for raw_label, raw_values in attributes.items():
            values_source = (
                raw_values
                if isinstance(raw_values, (list, tuple))
                else (raw_values,)
            )
            values = tuple(
                str(value).strip()
                for value in values_source
                if value is not None and str(value).strip()
            )
            remembered = self._resolved_attributes.get(
                (platform, normalize_history_field_label(raw_label))
            )
            if remembered is None:
                continue
            request, resolved = remembered
            resolved_ids = split_multi_choice(resolved.value_id)
            is_multi_choice = len(resolved_ids) > 1 and all(
                sum(
                    1
                    for candidate in request.candidates
                    if candidate.value_id == value_id
                )
                == 1
                for value_id in resolved_ids
            )
            if is_multi_choice:
                actual_labels = tuple(
                    part
                    for value in values
                    for part in split_multi_choice(value)
                )
                resolved_labels = split_multi_choice(resolved.label)
                if Counter(
                    normalize_history_field_label(value)
                    for value in actual_labels
                ) != Counter(
                    normalize_history_field_label(value)
                    for value in resolved_labels
                ):
                    continue
                actual_label = resolved.label
            else:
                if len(values) != 1 or normalize_history_field_label(
                    values[0]
                ) != normalize_history_field_label(resolved.label):
                    continue
                actual_label = values[0]
            payload = {
                "run_id": self.run_id,
                "product_version": self.product_version,
                "platform_id": platform,
                "category_leaf_id": request.category_leaf_id,
                "field_id": request.field_id,
                "snapshot_version": resolved.snapshot_version,
                "actual_value_id": resolved.value_id,
                "actual_label": actual_label,
                "verified": True,
                "payload_json": {"source": "save_readback"},
            }
            self.store.enqueue(
                "readback.recorded:"
                + canonical_sha256(payload),
                "readback.recorded",
                payload,
            )
            recorded += 1
        return recorded

    async def resolve(
        self, request: AttributeRequest
    ) -> Optional[ResolvedAttribute]:
        request = replace(request, candidates=first_per_label(request.candidates))
        if request.control_type.casefold() in UNSUPPORTED_CONTROL_TYPES:
            raise ValueError("operational selector cannot use attribute learning")
        requested_candidates = tuple(request.candidates)
        matched_excel_variants = _matched_excel_variants(
            request.excel_value,
            requested_candidates,
            control_type=request.control_type,
        )
        exact_excel = matched_excel_variants[0] if matched_excel_variants else ()
        historical_labels = self.verified_history.get(
            (
                canonical_platform_name(request.platform_id),
                normalize_history_field_label(request.field_label),
            ),
            (),
        )
        historical_candidates = tuple(
            candidate
            for candidate in requested_candidates
            if any(candidate.label == label for label in historical_labels)
        )
        # Multi-select fields persist several verified labels per field; only
        # reuse the group when every historical label resolves to exactly one
        # live candidate and the whole group stays within the Excel intent.
        excel_alternatives = excel_alternative_set(request.excel_value)
        historical_group_valid = bool(historical_labels) and bool(historical_candidates) and (
            len({candidate.label for candidate in historical_candidates})
            == len(historical_candidates)
        ) and (
            any(
                {candidate.label for candidate in historical_candidates}
                == {candidate.label for candidate in variant}
                for variant in matched_excel_variants
            )
            if matched_excel_variants
            else (
                not excel_alternatives
                or all(
                    _normalize_choice_text(candidate.label) in excel_alternatives
                    for candidate in historical_candidates
                )
            )
        )
        historical_candidate = (
            historical_candidates[0]
            if len(historical_candidates) == 1
            and (not exact_excel or historical_candidates[0] in exact_excel)
            else None
        )
        # Very large remote dictionaries (brands are the common example) can
        # contain tens of thousands of values.  When Excel already identifies
        # one unique live API candidate, transmit only that verified choice to
        # the review service.  It keeps the snapshot bounded without weakening
        # the click-time DOM/readback validation performed by the adapter.
        snapshot_candidates = (
            exact_excel if exact_excel and not request.evidence.get("force_review") else requested_candidates
        )
        snapshot = CandidateSnapshot(
            request.platform_id,
            request.category_leaf_id,
            request.field_id,
            request.field_label,
            snapshot_candidates,
            request.schema_version,
            request.custom_allowed,
        )
        # Human approval of the full live dictionary takes precedence over
        # an Excel match which would otherwise narrow the snapshot to one value.
        full_snapshot = CandidateSnapshot(
            request.platform_id, request.category_leaf_id, request.field_id,
            request.field_label, requested_candidates, request.schema_version,
            request.custom_allowed,
        )
        full_confirmed = self.store.load_review_resolution(
            product_version=self.product_version,
            platform_id=canonical_platform_name(request.platform_id),
            snapshot_version=full_snapshot.snapshot_version,
        )
        if full_confirmed is not None:
            snapshot = full_snapshot
        self.store.save_candidate_snapshot(snapshot)
        confirmed_value = self.store.load_review_resolution(
            product_version=self.product_version,
            platform_id=canonical_platform_name(request.platform_id),
            snapshot_version=snapshot.snapshot_version,
        )
        if confirmed_value is not None:
            # 审核确认兼容多选：确认值可用逗号分隔多个 valueId/label
            # （如“101,102”或“男,女”）。全部唯一命中候选后按 Excel 同款
            # 逗号语法组合返回，消费端 selection_value_groups 会拆组全选。
            parts = split_multi_choice(confirmed_value)
            if not parts:
                raise ValueError("confirmed review value is empty")
            matched = []
            for part in parts:
                found = tuple(
                    candidate
                    for candidate in snapshot.values
                    if part in {candidate.value_id, candidate.label}
                )
                if len(found) > 1:
                    raise ValueError("confirmed review value is ambiguous")
                if not found:
                    matched = None
                    break
                matched.append(found[0])
            if matched is not None:
                unique = tuple(dict.fromkeys(matched))
                return self._remember_resolved(
                    request,
                    ResolvedAttribute(
                        ",".join(candidate.value_id for candidate in unique),
                        ",".join(candidate.label for candidate in unique),
                        "human_override",
                        snapshot.snapshot_version,
                    ),
                )
            if (
                snapshot.custom_allowed or request.field_id != "__category__"
            ):
                return self._remember_resolved(
                    request,
                    ResolvedAttribute(
                        confirmed_value,
                        confirmed_value,
                        "human_override",
                        snapshot.snapshot_version,
                    ),
                )
            raise ValueError("confirmed review value no longer matches snapshot")
        try:
            canonical_field = map_platform_field(
                request.platform_id, request.field_label
            )
        except FieldMappingError:
            canonical_field = None
        same_product_review = self._cross_product_choice(
            request, same_product_only=True
        )
        if same_product_review is not None:
            self._queue_candidate_snapshot(
                full_snapshot, canonical_field, request.control_type
            )
            self._schedule_flush()
            return self._remember_resolved(request, same_product_review)
        self._queue_candidate_snapshot(
            snapshot, canonical_field, request.control_type
        )
        if request.evidence.get("force_review"):
            await self._raise_review(request, snapshot, canonical_field,
                                     "platform_write_failed")
            return None
        if historical_candidate is not None or historical_group_valid:
            if historical_candidate is None:
                # 多选字段的保存回读是一组值（如 适用季节=秋季,冬季）：
                # 组内每个 label 都唯一命中当前候选且都在 Excel 意图内，
                # 按逗号语法组合复用，消费端 selection_value_groups 拆组全选。
                historical_candidate = CandidateValue(
                    ",".join(
                        candidate.value_id
                        for candidate in historical_candidates
                    ),
                    ",".join(
                        candidate.label for candidate in historical_candidates
                    ),
                )
            values = {}
            if canonical_field is not None:
                values[canonical_field] = {
                    "value_id": historical_candidate.value_id
                }
            self.store.enqueue(
                "text_facts.created:"
                + canonical_sha256(
                    {
                        "product_version": self.product_version,
                        "snapshot": snapshot.snapshot_version,
                        "history": historical_candidate.value_id,
                        "source": "verified_readback",
                    }
                ),
                "text_facts.created",
                {
                    "product_version": self.product_version,
                    "source": "verified_readback",
                    "payload_json": {
                        "values": values,
                        "platform_values": {
                            "|".join(
                                (
                                    request.platform_id,
                                    request.category_leaf_id,
                                    request.field_id,
                                )
                            ): {
                                "field_label": request.field_label,
                                "value_id": historical_candidate.value_id,
                                "value_label": historical_candidate.label,
                            }
                        },
                        "text_tokens": split_multi_choice(
                            historical_candidate.label
                        ),
                    },
                },
            )
            self._schedule_flush()
            return self._remember_resolved(
                request,
                ResolvedAttribute(
                    historical_candidate.value_id,
                    historical_candidate.label,
                    "verified_history",
                    snapshot.snapshot_version,
                ),
            )
        if exact_excel:
            exact_candidate = CandidateValue(
                ",".join(candidate.value_id for candidate in exact_excel),
                ",".join(candidate.label for candidate in exact_excel),
            )
            values = {}
            if canonical_field is not None:
                values[canonical_field] = {
                    "value_id": exact_candidate.value_id
                }
            text_payload = {
                "product_version": self.product_version,
                "source": "excel",
                "payload_json": {
                    "values": values,
                    "platform_values": {
                        "|".join(
                            (
                                request.platform_id,
                                request.category_leaf_id,
                                request.field_id,
                            )
                        ): {
                            "field_label": request.field_label,
                            "value_id": exact_candidate.value_id,
                            "value_label": exact_candidate.label,
                        }
                    },
                    "text_tokens": [request.excel_value.strip()],
                },
            }
            self.store.enqueue(
                "text_facts.created:"
                + canonical_sha256(
                    {
                        "product_version": self.product_version,
                        "snapshot": snapshot.snapshot_version,
                        "value": exact_candidate.value_id,
                        "source": "excel",
                    }
                ),
                "text_facts.created",
                text_payload,
            )
            # Excel remains authoritative while the rule library is being
            # accumulated. The candidate has already been cross-checked against
            # both API JSON and the live DOM before this point, so asking the
            # operator to confirm the same exact value adds no safety.
            self._schedule_flush()
            return self._remember_resolved(
                request,
                ResolvedAttribute(
                    exact_candidate.value_id,
                    exact_candidate.label,
                    "explicit_text",
                    snapshot.snapshot_version,
                ),
            )
        if canonical_field is None:
            # A platform pass owns an ordered local outbox.  While collecting a
            # batch, enqueue every unmapped field first and drain once at the
            # platform boundary; otherwise a size table with many empty cells
            # pays one network round trip per cell before the browser can move
            # on to the remaining fields.
            if not self._collecting_platform_id:
                await self._flush()
            await self._raise_review(
                request, snapshot, None, "field_mapping_required"
            )
            return None
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
            return None
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
            return None
        return self._remember_resolved(
            request,
            ResolvedAttribute(
                decision.value_id or "",
                decision.value_label or "",
                str(response.get("source") or ""),
                decision.snapshot_version,
            ),
        )


__all__ = [
    "AttributeRequest",
    "AttributeRuntime",
    "ResolvedAttribute",
    "ReviewBatchRequired",
    "ReviewRequired",
]
