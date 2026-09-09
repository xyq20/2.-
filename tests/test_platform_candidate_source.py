import unittest

from platform_candidate_source import (
    CandidateSourceError,
    DomCandidate,
    reconcile_candidates,
)
from platform_schema import FieldOption


class CandidateSourceTests(unittest.TestCase):
    def test_api_candidates_are_authoritative_and_keep_order(self):
        api = (
            FieldOption("long", "长裤", 0),
            FieldOption("short", "短裤", 1),
        )
        dom = (
            DomCandidate("short", "短裤"),
            DomCandidate("long", "长裤"),
            DomCandidate("", "请选择", enabled=False),
        )

        self.assertEqual(reconcile_candidates(api, dom), api)

    def test_dom_without_ids_must_match_each_api_label_exactly_once(self):
        api = (FieldOption("long", "长裤", 0),)

        self.assertEqual(
            reconcile_candidates(api, (DomCandidate("", " 长裤 "),)),
            api,
        )

    def test_missing_api_never_falls_back_to_dom(self):
        with self.assertRaises(CandidateSourceError) as caught:
            reconcile_candidates((), (DomCandidate("long", "长裤"),))
        self.assertEqual(caught.exception.reason_code, "api_candidates_unavailable")

    def test_duplicate_api_id_is_rejected(self):
        api = (
            FieldOption("long", "长裤", 0),
            FieldOption("long", "长裤冲突", 1),
        )
        with self.assertRaises(CandidateSourceError) as caught:
            reconcile_candidates(api, (DomCandidate("long", "长裤"),))
        self.assertEqual(caught.exception.reason_code, "api_candidate_duplicate_id")

    def test_wrong_dom_id_does_not_fall_back_to_equal_label(self):
        with self.assertRaises(CandidateSourceError) as caught:
            reconcile_candidates(
                (FieldOption("api-long", "长裤", 0),),
                (DomCandidate("dom-long", "长裤"),),
            )
        self.assertEqual(caught.exception.reason_code, "candidate_source_mismatch")

    def test_extra_enabled_dom_candidate_is_rejected(self):
        with self.assertRaises(CandidateSourceError) as caught:
            reconcile_candidates(
                (FieldOption("long", "长裤", 0),),
                (DomCandidate("long", "长裤"), DomCandidate("short", "短裤")),
            )
        self.assertEqual(caught.exception.reason_code, "candidate_source_mismatch")

    def test_duplicate_label_only_dom_candidate_is_ambiguous(self):
        with self.assertRaises(CandidateSourceError) as caught:
            reconcile_candidates(
                (FieldOption("long", "长裤", 0),),
                (DomCandidate("", "长裤"), DomCandidate("", "长裤")),
            )
        self.assertEqual(caught.exception.reason_code, "dom_candidate_ambiguous")


if __name__ == "__main__":
    unittest.main()
