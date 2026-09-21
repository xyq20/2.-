import asyncio
import dataclasses
import tempfile
import unittest
from pathlib import Path

from learning_models import RunCheckpoint, checkpoint_event_payload
from learning_store import LearningStore
from review_resume import (
    ResumeRejected,
    persist_resume_and_ack,
    validate_resume,
    wait_for_review,
)


CHECKPOINT = RunCheckpoint(
    "run-1",
    "product-1",
    "save_only",
    ("base", "tmall", "pdd"),
    1,
    "waiting_review",
    "review-1",
    2,
    "device-1",
    "images-1",
)
EVENT = {
    "event_id": "event-1",
    "payload": {
        "run_id": "run-1",
        "device_id": "device-1",
        "product_version": "product-1",
        "platform_id": "tmall",
        "review_id": "review-1",
        "final_value_id": "long",
        "snapshot_version": "snapshot-1",
    },
}


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.acks = []
        self.calls = []

    def post_event(self, *args):
        self.calls.append(("post", args))
        return {"ok": True}

    def poll_resume(self, device_id, wait_seconds):
        self.poll = (device_id, wait_seconds)
        return next(self.responses)

    def acknowledge_resume(self, event_id, *, checkpoint_id, device_id):
        self.calls.append(("ack", (event_id, checkpoint_id, device_id)))
        self.acks.append((event_id, checkpoint_id, device_id))
        return {"ok": True}


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_flushes_earlier_waiting_checkpoint_before_new_version(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / 'state.sqlite3')
            store.migrate()
            waiting = store.save_checkpoint(CHECKPOINT)
            store.enqueue('waiting', 'checkpoint.updated', checkpoint_event_payload(waiting))
            client = FakeClient(())
            decision = validate_resume(waiting, EVENT, 'product-1', 'images-1', 'save_only')
            await persist_resume_and_ack(store, client, waiting, decision)
            posted = [args[2] for kind, args in client.calls if kind == 'post']
            self.assertEqual([item['status'] for item in posted], ['waiting_review', 'resume_pending'])
            self.assertEqual([kind for kind, _ in client.calls], ['post', 'post', 'ack'])
            store.close()

    def test_matching_resume_retries_stopped_platform(self):
        decision = validate_resume(
            CHECKPOINT, EVENT, "product-1", "images-1", "save_only"
        )
        self.assertEqual(decision.platform_index, 1)
        self.assertEqual(decision.platform_id, "tmall")

    def test_platform_registry_alias_matches_runner_name(self):
        checkpoint = dataclasses.replace(
            CHECKPOINT,
            platform_order=("base", "taobao", "tmall"),
        )
        event = {
            **EVENT,
            "payload": {**EVENT["payload"], "platform_id": "tb"},
        }

        decision = validate_resume(
            checkpoint, event, "product-1", "images-1", "save_only"
        )

        self.assertEqual(decision.platform_id, "taobao")

    def test_identity_changes_and_consumed_events_are_rejected(self):
        cases = (
            ("product_version_changed", "changed", "images-1", "save_only", EVENT),
            ("image_version_changed", "product-1", "changed", "save_only", EVENT),
            ("execution_mode_changed", "product-1", "images-1", "preview", EVENT),
            ("event_already_consumed", "product-1", "images-1", "save_only", {**EVENT, "processed_at": "now"}),
        )
        for reason, product, images, mode, event in cases:
            with self.subTest(reason=reason), self.assertRaises(ResumeRejected) as caught:
                validate_resume(CHECKPOINT, event, product, images, mode)
            self.assertEqual(caught.exception.reason_code, reason)

    async def test_wait_ignores_other_runs_without_local_sleep(self):
        other = {"event_id": "other", "payload": {**EVENT["payload"], "run_id": "other"}}
        client = FakeClient(({"events": [other]}, {"events": [EVENT]}))
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / "state.sqlite3")
            store.migrate()
            try:
                decision = await wait_for_review(
                    store,
                    client,
                    CHECKPOINT,
                    current_product_version="product-1",
                    current_image_version="images-1",
                    execution_mode="save_only",
                )
            finally:
                store.close()
        self.assertEqual(decision.event_id, "event-1")
        self.assertEqual(client.poll, ("device-1", 30))

    async def test_ack_happens_only_after_resume_checkpoint_is_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / "state.sqlite3")
            store.migrate()
            store.save_checkpoint(CHECKPOINT)
            client = FakeClient(())
            decision = validate_resume(
                CHECKPOINT, EVENT, "product-1", "images-1", "save_only"
            )
            persisted = await persist_resume_and_ack(
                store, client, CHECKPOINT, decision
            )
            self.assertEqual(persisted.status, "resume_pending")
            self.assertEqual(store.load_checkpoint("run-1").status, "resume_pending")
            self.assertEqual(
                store.load_review_resolution(
                    product_version="product-1",
                    platform_id="tmall",
                    snapshot_version="snapshot-1",
                ),
                "long",
            )
            self.assertEqual(client.acks, [("event-1", "run-1", "device-1")])
            self.assertEqual([kind for kind, _args in client.calls], ["post", "ack"])
            posted = client.calls[0][1]
            self.assertEqual(
                posted[0], f"checkpoint.updated:run-1:{persisted.version}"
            )
            self.assertEqual(posted[1], "checkpoint.updated")
            self.assertEqual(posted[2]["status"], "resume_pending")
            self.assertEqual(posted[2]["version"], persisted.version)
            store.close()

    def test_completed_checkpoint_cannot_resume(self):
        with self.assertRaises(ResumeRejected) as caught:
            validate_resume(
                dataclasses.replace(CHECKPOINT, status="completed"),
                EVENT,
                "product-1",
                "images-1",
                "save_only",
            )
        self.assertEqual(caught.exception.reason_code, "checkpoint_not_waiting_review")


if __name__ == "__main__":
    unittest.main()
