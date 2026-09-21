import unittest

from platform_candidate_source import (
    CandidateSourceError,
    DomCandidate,
    reconcile_candidates,
    validate_observed_selection,
)
from platform_schema import FieldOption


class CandidateSourceTests(unittest.TestCase):
    def test_api_ids_are_retained_in_current_dom_order(self):
        api = (
            FieldOption("long", "长裤", 0),
            FieldOption("short", "短裤", 1),
        )
        dom = (
            DomCandidate("short", "短裤"),
            DomCandidate("long", "长裤"),
            DomCandidate("", "请选择", enabled=False),
        )

        self.assertEqual(
            reconcile_candidates(api, dom),
            (
                FieldOption("short", "短裤", 0),
                FieldOption("long", "长裤", 1),
            ),
        )

    def test_dom_without_ids_must_match_each_api_label_exactly_once(self):
        api = (FieldOption("long", "长裤", 0),)

        self.assertEqual(
            reconcile_candidates(api, (DomCandidate("", " 长裤 "),)),
            api,
        )

    def test_missing_api_falls_back_to_unique_live_dom_candidate(self):
        self.assertEqual(
            reconcile_candidates((), (DomCandidate("long", "长裤"),)),
            (FieldOption("long", "长裤", 0),),
        )

    def test_duplicate_api_id_does_not_block_unique_live_dom_labels(self):
        api = (
            FieldOption("long", "长裤", 0),
            FieldOption("long", "长裤冲突", 1),
        )
        self.assertEqual(
            reconcile_candidates(api, (DomCandidate("long", "长裤"),)),
            (FieldOption("long", "长裤", 0),),
        )

    def test_api_candidate_missing_id_uses_live_dom_identity(self):
        self.assertEqual(
            reconcile_candidates(
                (FieldOption("", "长裤", 0),),
                (DomCandidate("dom-long", "长裤"),),
            ),
            (FieldOption("dom-long", "长裤", 0),),
        )

    def test_api_candidate_missing_label_uses_live_dom_candidate(self):
        self.assertEqual(
            reconcile_candidates(
                (FieldOption("long", " ", 0),),
                (DomCandidate("long", "长裤"),),
            ),
            (FieldOption("long", "长裤", 0),),
        )

    def test_whitespace_api_candidate_id_uses_live_dom_candidate(self):
        self.assertEqual(
            reconcile_candidates(
                (FieldOption("  ", "长裤", 0),),
                (DomCandidate("", "长裤"),),
            ),
            (FieldOption("长裤", "长裤", 0),),
        )

    def test_wrong_dom_id_can_correlate_by_unique_exact_label(self):
        self.assertEqual(
            reconcile_candidates(
                (FieldOption("api-long", "长裤", 0),),
                (DomCandidate("dom-long", "长裤"),),
            ),
            (FieldOption("api-long", "长裤", 0),),
        )

    def test_extra_enabled_dom_candidate_is_included_as_actionable(self):
        self.assertEqual(
            reconcile_candidates(
                (FieldOption("long", "长裤", 0),),
                (DomCandidate("long", "长裤"), DomCandidate("short", "短裤")),
            ),
            (FieldOption("long", "长裤", 0), FieldOption("short", "短裤", 1)),
        )

    def test_duplicate_label_only_dom_candidate_is_ambiguous(self):
        self.assertEqual(reconcile_candidates(
                (FieldOption("long", "长裤", 0),),
                (DomCandidate("", "长裤"), DomCandidate("", "长裤")),
            ), (FieldOption('long', '长裤', 0),))

    def test_saved_dom_selection_validates_full_api_candidates_without_dropdown(self):
        api = (
            FieldOption("long", "长裤", 0),
            FieldOption("short", "短裤", 1),
        )

        self.assertEqual(validate_observed_selection(api, ("长裤",)), api)

    def test_saved_dom_selection_rejects_stale_or_ambiguous_api_soft_path(self):
        self.assertEqual(validate_observed_selection(
                (
                    FieldOption("long-a", "长裤", 0),
                    FieldOption("long-b", "长裤", 1),
                ),
                ("长裤",),
            ), (FieldOption('long-a', '长裤', 0),))


if __name__ == "__main__":
    unittest.main()
