import tempfile
import unittest
from pathlib import Path

from attribute_runtime import AttributeRequest, AttributeRuntime, ReviewRequired
from learning_models import CandidateValue
from learning_store import LearningStore


class FakeClient:
    def __init__(self, response=None):
        self.response = response or {
            "status": "auto_fill_ready",
            "value_id": "long",
            "source": "mature_rule",
            "evidence_kinds": ["visual"],
            "mature_rule": True,
            "support_count": 3,
            "calibrated_acceptance_rate": 1.0,
        }
        self.events = []

    def post_event(self, key, event_type, payload):
        self.events.append((key, event_type, payload))
        return {"event_id": key}

    def decide(self, request):
        return {**self.response, "snapshot_version": request["snapshot_version"]}


def make_length_request(**overrides):
    values = {
        "platform_id": "wxsph",
        "category_leaf_id": "pants",
        "field_id": "length",
        "field_label": "裤长",
        "candidates": (
            CandidateValue("short", "短裤"),
            CandidateValue("long", "长裤"),
        ),
        "excel_value": "",
        "evidence": {"visual": ["asset-1"]},
        "custom_allowed": False,
        "schema_version": "schema-1",
    }
    values.update(overrides)
    return AttributeRequest(**values)


class AttributeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "learning.sqlite3")
        self.store.migrate()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_resolves_only_a_live_candidate_after_snapshot_delivery(self):
        client = FakeClient()
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        result = await runtime.resolve(make_length_request())

        self.assertEqual((result.value_id, result.label), ("long", "长裤"))
        self.assertEqual(client.events[0][1], "snapshot.created")
        self.assertEqual(client.events[0][2]["options"][1]["value_id"], "long")
        self.assertEqual(self.store.pending_outbox(), ())

    async def test_review_response_is_persisted_and_raises_before_write(self):
        client = FakeClient(
            {"status": "review_required", "reason_code": "insufficient_evidence"}
        )
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        with self.assertRaises(ReviewRequired) as caught:
            await runtime.resolve(make_length_request())

        self.assertEqual(caught.exception.reason_code, "insufficient_evidence")
        self.assertEqual(client.events[-1][1], "review.created")
        self.assertEqual(client.events[-1][2]["id"], caught.exception.review_id)

    async def test_changed_or_invented_cloud_candidate_requires_review(self):
        client = FakeClient(
            {
                "status": "auto_fill_ready",
                "value_id": "invented",
                "source": "mature_rule",
                "evidence_kinds": ["visual"],
                "mature_rule": True,
                "support_count": 3,
                "calibrated_acceptance_rate": 1.0,
            }
        )
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        with self.assertRaises(ReviewRequired) as caught:
            await runtime.resolve(make_length_request())
        self.assertEqual(caught.exception.reason_code, "candidate_missing")

    async def test_excel_exact_match_is_sent_as_text_evidence(self):
        client = FakeClient(
            {
                "status": "auto_fill_ready",
                "value_id": "long",
                "source": "explicit_text",
                "evidence_kinds": ["text", "visual"],
                "mature_rule": False,
                "support_count": 0,
                "calibrated_acceptance_rate": 0,
            }
        )
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        await runtime.resolve(make_length_request(excel_value="长裤"))
        text_event = next(event for event in client.events if event[1] == "text_facts.created")
        self.assertEqual(
            text_event[2]["payload_json"]["values"]["pants_length"]["value_id"],
            "long",
        )

    async def test_operational_selector_is_rejected(self):
        runtime = AttributeRuntime(
            self.store, FakeClient(), "run-1", "product-1", device_id="device-1"
        )
        with self.assertRaisesRegex(ValueError, "operational selector"):
            await runtime.resolve(make_length_request(control_type="freight"))


if __name__ == "__main__":
    unittest.main()
