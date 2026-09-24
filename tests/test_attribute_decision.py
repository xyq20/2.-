import dataclasses
import inspect
import unittest

from attribute_decision import (
    CandidateSnapshotProtocol,
    CandidateValueProtocol,
    DecisionInput,
    DecisionStatus,
    ValidatedDecision,
    validate_decision,
)


@dataclasses.dataclass(frozen=True)
class SnapshotValue:
    value_id: str
    label: str


@dataclasses.dataclass(frozen=True)
class Snapshot:
    platform_id: str
    category_leaf_id: str
    field_id: str
    field_label: str
    values: tuple
    schema_version: str
    custom_allowed: bool
    snapshot_version: str


def make_snapshot(**overrides):
    values = {
        "platform_id": "wxsph",
        "category_leaf_id": "pants",
        "field_id": "pants-length",
        "field_label": "裤长",
        "values": (
            SnapshotValue("short", "短裤"),
            SnapshotValue("long", "长裤"),
        ),
        "schema_version": "schema-1",
        "custom_allowed": False,
        "snapshot_version": "snapshot-1",
    }
    values.update(overrides)
    return Snapshot(**values)


def make_decision(**overrides):
    values = {
        "canonical_field": "pants_length",
        "proposed_value_id": "long",
        "evidence_kinds": ("visual",),
        "source": "mature_rule",
        "mature_rule": True,
        "support_count": 3,
        "calibrated_acceptance_rate": 0.95,
    }
    values.update(overrides)
    return DecisionInput(**values)


def make_material_percentage_snapshot(**overrides):
    return make_snapshot(
        field_id="material-percentage",
        field_label="面料材质成分含量",
        values=(
            SnapshotValue("cotton-100", "100%棉"),
            SnapshotValue("mixed", "混纺"),
        ),
        snapshot_version="snapshot-material-percentage-1",
        **overrides,
    )


def make_material_composition_snapshot(**overrides):
    return make_snapshot(
        field_id="material-composition",
        field_label="材质成分",
        values=(
            SnapshotValue("cotton", "棉"),
            SnapshotValue("polyester", "聚酯纤维"),
        ),
        snapshot_version="snapshot-material-composition-1",
        **overrides,
    )


class ComputedSnapshot:
    @property
    def platform_id(self):
        return "wxsph"

    @property
    def category_leaf_id(self):
        return "pants"

    @property
    def field_id(self):
        return "pants-length"

    @property
    def field_label(self):
        return "裤长"

    @property
    def values(self):
        return (SnapshotValue("long", "长裤"),)

    @property
    def schema_version(self):
        return "schema-1"

    @property
    def custom_allowed(self):
        return False

    @property
    def snapshot_version(self):
        return "computed-snapshot-1"


class AttributeDecisionTests(unittest.TestCase):
    def assert_review(self, result, reason_code):
        self.assertEqual(result.status, DecisionStatus.REVIEW_REQUIRED)
        self.assertEqual(result.reason_code, reason_code)
        self.assertIsNone(result.value_id)
        self.assertIsNone(result.value_label)

    def test_validated_decision_is_immutable(self):
        decision = ValidatedDecision(
            DecisionStatus.REVIEW_REQUIRED,
            None,
            None,
            "candidate_missing",
            "snapshot-1",
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.reason_code = "changed"

    def test_empty_candidates_require_review(self):
        result = validate_decision(make_decision(), make_snapshot(values=()))

        self.assert_review(result, "candidate_empty")

    def test_value_id_must_exist(self):
        result = validate_decision(
            make_decision(proposed_value_id="missing"), make_snapshot()
        )

        self.assert_review(result, "candidate_missing")

    def test_empty_or_whitespace_candidate_ids_are_invalid_placeholders(self):
        for invalid_id in ("", "   "):
            with self.subTest(invalid_id=repr(invalid_id)):
                snapshot = make_snapshot(
                    values=(
                        SnapshotValue(invalid_id, "请选择"),
                        SnapshotValue("long", "长裤"),
                    )
                )

                self.assert_review(
                    validate_decision(make_decision(), snapshot),
                    "candidate_invalid",
                )

    def test_empty_or_whitespace_proposed_value_id_is_invalid(self):
        for invalid_id in ("", "   "):
            with self.subTest(invalid_id=repr(invalid_id)):
                result = validate_decision(
                    make_decision(proposed_value_id=invalid_id), make_snapshot()
                )

                self.assert_review(result, "proposed_value_invalid")

    def test_duplicate_candidate_id_requires_review(self):
        duplicate = make_snapshot(
            values=(
                SnapshotValue("long", "长裤"),
                SnapshotValue("long", "九分裤"),
            )
        )

        self.assert_review(
            validate_decision(make_decision(), duplicate), "candidate_ambiguous"
        )

    def test_changed_snapshot_requires_review(self):
        result = validate_decision(
            make_decision(expected_snapshot_version="snapshot-old"), make_snapshot()
        )

        self.assert_review(result, "snapshot_changed")

    def test_label_submitted_as_free_text_is_rejected_when_custom_is_disabled(self):
        result = validate_decision(
            make_decision(proposed_value_id="长裤"), make_snapshot(custom_allowed=False)
        )

        self.assert_review(result, "free_text_not_allowed")

    def test_confirmed_custom_value_is_accepted_when_snapshot_allows_input(self):
        result = validate_decision(
            make_decision(
                proposed_value_id="加长裤",
                source="human_override",
                mature_rule=False,
                evidence_kinds=(),
            ),
            make_snapshot(custom_allowed=True),
        )

        self.assertEqual(result.status, DecisionStatus.AUTO_FILL_READY)
        self.assertEqual((result.value_id, result.value_label), ("加长裤", "加长裤"))
        self.assertEqual(result.reason_code, "validated")

    def test_conflict_requires_review(self):
        result = validate_decision(make_decision(has_conflict=True), make_snapshot())

        self.assert_review(result, "evidence_conflict")

    def test_unknown_snapshot_field_requires_mapping_review(self):
        result = validate_decision(
            make_decision(), make_snapshot(field_label="平台新增字段")
        )

        self.assert_review(result, "field_mapping_required")

    def test_decision_canonical_field_cannot_bypass_snapshot_field_policy(self):
        result = validate_decision(
            make_decision(
                canonical_field="pants_length",
                proposed_value_id="cotton-100",
                evidence_kinds=("visual",),
            ),
            make_material_percentage_snapshot(),
        )

        self.assert_review(result, "canonical_field_mismatch")

    def test_material_percentage_requires_explicit_text_evidence(self):
        visual_only = make_decision(
            canonical_field="material_percentage",
            proposed_value_id="cotton-100",
            evidence_kinds=("visual",),
        )

        self.assert_review(
            validate_decision(visual_only, make_material_percentage_snapshot()),
            "required_text_evidence_missing",
        )

    def test_material_composition_requires_explicit_text_evidence(self):
        visual_only = make_decision(
            canonical_field="material_composition",
            proposed_value_id="cotton",
            evidence_kinds=("visual",),
        )

        self.assert_review(
            validate_decision(visual_only, make_material_composition_snapshot()),
            "required_text_evidence_missing",
        )

    def test_material_percentage_cannot_be_inferred_by_rule_even_with_text(self):
        result = validate_decision(
            make_decision(
                canonical_field="material_percentage",
                proposed_value_id="cotton-100",
                evidence_kinds=("text",),
            ),
            make_material_percentage_snapshot(),
        )

        self.assert_review(result, "rule_not_allowed")

    def test_mature_rule_passes_only_when_current_candidate_exists(self):
        ready = validate_decision(make_decision(), make_snapshot())
        missing = validate_decision(
            make_decision(proposed_value_id="old-long"), make_snapshot()
        )

        self.assertEqual(ready.status, DecisionStatus.AUTO_FILL_READY)
        self.assertEqual(ready.value_id, "long")
        self.assertEqual(ready.value_label, "长裤")
        self.assertEqual(ready.reason_code, "validated")
        self.assertEqual(ready.snapshot_version, "snapshot-1")
        self.assert_review(missing, "candidate_missing")

    def test_immature_rule_requires_review(self):
        result = validate_decision(
            make_decision(mature_rule=False),
            make_snapshot(),
        )

        self.assert_review(result, "rule_not_mature")

    def test_mature_rule_still_requires_calibrated_confidence(self):
        too_little_support = validate_decision(
            make_decision(support_count=2, calibrated_acceptance_rate=1.0),
            make_snapshot(),
        )
        low_acceptance = validate_decision(
            make_decision(support_count=3, calibrated_acceptance_rate=0.949),
            make_snapshot(),
        )

        self.assert_review(too_little_support, "confidence_gate_not_met")
        self.assert_review(low_acceptance, "confidence_gate_not_met")

    def test_two_verified_model_outcomes_at_one_hundred_percent_are_rejected(self):
        result = validate_decision(
            make_decision(
                source="constrained_model",
                mature_rule=False,
                support_count=2,
                calibrated_acceptance_rate=1.0,
            ),
            make_snapshot(),
        )

        self.assert_review(result, "confidence_gate_not_met")

    def test_three_verified_model_outcomes_at_ninety_five_percent_pass(self):
        result = validate_decision(
            make_decision(
                source="constrained_model",
                mature_rule=False,
                support_count=3,
                calibrated_acceptance_rate=0.95,
            ),
            make_snapshot(),
        )

        self.assertEqual(result.status, DecisionStatus.AUTO_FILL_READY)
        self.assertEqual((result.value_id, result.value_label), ("long", "长裤"))

    def test_three_verified_model_outcomes_below_ninety_five_percent_are_rejected(self):
        result = validate_decision(
            make_decision(
                source="constrained_model",
                mature_rule=False,
                support_count=3,
                calibrated_acceptance_rate=0.949,
            ),
            make_snapshot(),
        )

        self.assert_review(result, "confidence_gate_not_met")

    def test_model_self_reported_confidence_does_not_affect_gate(self):
        weak = validate_decision(
            make_decision(
                source="constrained_model",
                mature_rule=False,
                support_count=0,
                calibrated_acceptance_rate=0.0,
                model_confidence=1.0,
            ),
            make_snapshot(),
        )
        strong = validate_decision(
            make_decision(
                source="constrained_model",
                mature_rule=False,
                support_count=3,
                calibrated_acceptance_rate=0.95,
                model_confidence=0.0,
            ),
            make_snapshot(),
        )

        self.assert_review(weak, "confidence_gate_not_met")
        self.assertEqual(strong.status, DecisionStatus.AUTO_FILL_READY)

    def test_human_override_and_explicit_text_require_corresponding_evidence(self):
        human = validate_decision(
            make_decision(source="human_override", mature_rule=False), make_snapshot()
        )
        human_without_original_visual_evidence = validate_decision(
            make_decision(
                canonical_field="thickness",
                proposed_value_id="regular",
                source="human_override",
                mature_rule=False,
                evidence_kinds=(),
            ),
            make_snapshot(
                platform_id="douyin",
                field_id="thickness",
                field_label="厚度",
                values=(SnapshotValue("regular", "常规"),),
            ),
        )
        explicit_missing = validate_decision(
            make_decision(source="explicit_text", mature_rule=False), make_snapshot()
        )
        explicit_present = validate_decision(
            make_decision(
                canonical_field="material_composition",
                proposed_value_id="cotton",
                source="explicit_text",
                mature_rule=False,
                evidence_kinds=("text",),
            ),
            make_material_composition_snapshot(),
        )

        self.assertEqual(human.status, DecisionStatus.AUTO_FILL_READY)
        self.assertEqual(
            human_without_original_visual_evidence.status,
            DecisionStatus.AUTO_FILL_READY,
        )
        self.assert_review(explicit_missing, "explicit_text_evidence_missing")
        self.assertEqual(explicit_present.status, DecisionStatus.AUTO_FILL_READY)

    def test_unknown_source_requires_review(self):
        result = validate_decision(
            make_decision(source="model_confidence", mature_rule=False), make_snapshot()
        )

        self.assert_review(result, "unknown_source")

    def test_snapshot_protocol_members_are_read_only_properties(self):
        for protocol, names in (
            (CandidateValueProtocol, ("value_id", "label")),
            (
                CandidateSnapshotProtocol,
                (
                    "platform_id",
                    "category_leaf_id",
                    "field_id",
                    "field_label",
                    "values",
                    "schema_version",
                    "custom_allowed",
                    "snapshot_version",
                ),
            ),
        ):
            for name in names:
                with self.subTest(protocol=protocol.__name__, name=name):
                    self.assertIsInstance(inspect.getattr_static(protocol, name), property)

    def test_computed_snapshot_properties_are_supported(self):
        result = validate_decision(make_decision(), ComputedSnapshot())

        self.assertEqual(result.status, DecisionStatus.AUTO_FILL_READY)
        self.assertEqual((result.value_id, result.value_label), ("long", "长裤"))
        self.assertEqual(result.snapshot_version, "computed-snapshot-1")


if __name__ == "__main__":
    unittest.main()
