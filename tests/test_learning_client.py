from datetime import datetime, timezone
import io
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from learning_client import (
    DEVICE_EVENT_TYPES,
    CloudLearningClient,
    LearningClientConfigurationError,
    PermanentCloudError,
    RetryableCloudError,
    flush_outbox,
)
from learning_models import OutboxEvent


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class LearningClientTests(unittest.TestCase):
    def test_device_token_can_come_from_environment_but_is_required(self):
        with patch.dict(os.environ, {"KUAIMAI_LEARNING_DEVICE_TOKEN": "env-secret"}):
            client = CloudLearningClient("https://review.example")
        self.assertEqual(client.device_token, "env-secret")

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(LearningClientConfigurationError) as caught:
                CloudLearningClient("https://review.example")
        self.assertNotIn("env-secret", str(caught.exception))

    def test_all_stable_device_event_types_use_the_fixed_envelope(self):
        expected = {
            "product.upsert",
            "snapshot.created",
            "checkpoint.updated",
            "stage.completed",
            "readback.recorded",
            "review.created",
            "text_facts.created",
        }
        self.assertEqual(DEVICE_EVENT_TYPES, frozenset(expected))
        client = CloudLearningClient("https://review.example", "device-secret")
        with patch.object(client, "_request", return_value=(201, {"ok": True})) as request:
            for event_type in sorted(expected):
                client.post_event("key-1", event_type, {"x": 1})
                self.assertEqual(
                    request.call_args.args,
                    (
                        "POST",
                        "/api/device/events",
                        {
                            "idempotency_key": "key-1",
                            "event_type": event_type,
                            "payload": {"x": 1},
                        },
                    ),
                )

    def test_unknown_event_type_is_rejected_before_request_construction(self):
        client = CloudLearningClient("https://review.example", "device-secret")
        with patch.object(client, "_request") as request:
            with self.assertRaises(ValueError):
                client.post_event("key-1", "visual_facts.created", {})
        request.assert_not_called()

    def test_only_marker_only_409_is_an_existing_idempotent_event(self):
        client = CloudLearningClient("https://review.example", "device-secret")
        with patch.object(client, "_request", return_value=(409, {"event_id": "e-1"})):
            result = client.post_event("key-1", "review.created", {"x": 1})
        self.assertEqual(result["event_id"], "e-1")

        for body in ({"error": "product_deleting"}, {"task_id": "r-1"}):
            with self.subTest(body=body), patch.object(
                client, "_request", return_value=(409, body)
            ):
                with self.assertRaises(PermanentCloudError):
                    client.post_event("key-1", "review.created", {"x": 1})

    def test_retryable_and_permanent_statuses_do_not_leak_secrets(self):
        client = CloudLearningClient("https://review.example", "device-secret")
        for status in (429, 500, 503):
            with self.subTest(status=status), patch.object(
                client, "_request", return_value=(status, {"password": "payload-secret"})
            ):
                with self.assertRaises(RetryableCloudError) as caught:
                    client.post_event("key", "review.created", {"token": "payload-secret"})
                self.assertEqual(str(caught.exception), f"cloud status {status}")

        with patch.object(
            client, "_request", return_value=(422, {"authorization": "payload-secret"})
        ):
            with self.assertRaises(PermanentCloudError) as caught:
                client.post_event("key", "review.created", {"token": "payload-secret"})
        self.assertEqual(str(caught.exception), "cloud status 422")

        with patch.object(
            client,
            "_request",
            return_value=(400, {"error": "invalid_payload_keys"}),
        ):
            with self.assertRaises(PermanentCloudError) as caught:
                client.post_event("key", "review.created", {})
        self.assertEqual(
            str(caught.exception),
            "cloud status 400 (invalid_payload_keys)",
        )

    def test_network_error_is_retryable_without_leaking_url_error_reason(self):
        client = CloudLearningClient("https://review.example", "device-secret")
        with patch(
            "learning_client.request.urlopen",
            side_effect=URLError("device-secret payload-secret"),
        ):
            with self.assertRaises(RetryableCloudError) as caught:
                client.post_event("key", "review.created", {"token": "payload-secret"})
        self.assertEqual(str(caught.exception), "cloud request failed")

    def test_http_error_body_is_classified_by_post_event(self):
        client = CloudLearningClient("https://review.example", "device-secret")
        error = HTTPError(
            "https://review.example/api/device/events",
            409,
            "conflict",
            {},
            io.BytesIO(b'{"event_id":"existing"}'),
        )
        with patch("learning_client.request.urlopen", side_effect=error):
            result = client.post_event("key", "review.created", {})
        self.assertEqual(result, {"event_id": "existing"})

    def test_request_sets_device_auth_but_exception_text_never_contains_it(self):
        client = CloudLearningClient("https://review.example", "device-secret")
        with patch(
            "learning_client.request.urlopen", return_value=_Response(200, {"ok": True})
        ) as urlopen:
            client.post_event("key", "review.created", {})

        built_request = urlopen.call_args.args[0]
        self.assertEqual(built_request.get_header("Authorization"), "Device device-secret")
        self.assertEqual(built_request.get_header("Content-type"), "application/json")

    def test_decide_and_resume_methods_have_stable_paths(self):
        client = CloudLearningClient("https://review.example", "device-secret")
        with patch.object(client, "_request", return_value=(200, {"status": "ready"})) as request:
            self.assertEqual(client.decide({"field": "fabric"}), {"status": "ready"})
            self.assertEqual(
                request.call_args.args,
                ("POST", "/api/device/decide", {"field": "fabric"}),
            )

        with patch.object(client, "_request", return_value=(200, {"status": "ready"})) as request:
            self.assertEqual(client.analyze("product-1"), {"status": "ready"})
            self.assertEqual(
                request.call_args.args,
                ("POST", "/api/device/analyze", {"product_version": "product-1"}),
            )

        with patch.object(client, "_request", return_value=(200, {"events": []})) as request:
            self.assertEqual(client.poll_resume("device 1", wait_seconds=30), {"events": []})
            self.assertEqual(
                request.call_args.args,
                ("GET", "/api/device/resume?device_id=device+1&wait_seconds=30", None),
            )
            self.assertEqual(request.call_args.kwargs, {"timeout": 35.0})

        with patch.object(client, "_request", return_value=(200, {"ok": True})) as request:
            self.assertEqual(
                client.acknowledge_resume(
                    "event/1", checkpoint_id="run-1", device_id="device-1"
                ),
                {"ok": True},
            )
            self.assertEqual(
                request.call_args.args,
                (
                    "POST",
                    "/api/device/resume/event%2F1/ack",
                    {
                        "checkpoint_persisted": True,
                        "checkpoint_id": "run-1",
                        "device_id": "device-1",
                    },
                ),
            )

    def test_resume_ack_requires_durable_checkpoint_identity(self):
        client = CloudLearningClient("https://review.example", "device-secret")
        with patch.object(client, "_request") as request:
            with self.assertRaises(ValueError):
                client.acknowledge_resume(
                    "event-1", checkpoint_id="", device_id="device-1"
                )
            with self.assertRaises(ValueError):
                client.acknowledge_resume(
                    "event-1", checkpoint_id="run-1", device_id=""
                )
        request.assert_not_called()

    def test_upload_asset_uses_hash_path_and_metadata_headers(self):
        client = CloudLearningClient("https://review.example", "device-secret")
        body = b"jpeg bytes"
        with patch.object(client, "_request", return_value=(201, {"asset_id": "a-1"})) as request:
            result = client.upload_asset(
                "product-v1", "abc123", "learning_thumbnail", "image/jpeg", body
            )

        self.assertEqual(result, {"asset_id": "a-1"})
        self.assertEqual(
            request.call_args.args,
            ("PUT", "/api/device/assets/abc123", None),
        )
        self.assertEqual(
            request.call_args.kwargs,
            {
                "body": body,
                "headers": {
                    "Content-Type": "image/jpeg",
                    "X-Product-Version": "product-v1",
                    "X-Asset-Kind": "learning_thumbnail",
                },
            },
        )

    def test_upload_asset_accepts_success_rejects_409_and_retries_5xx(self):
        client = CloudLearningClient("https://review.example", "device-secret")
        for status in (200, 201):
            with self.subTest(status=status), patch.object(
                client, "_request", return_value=(status, {"ok": True})
            ):
                self.assertEqual(
                    client.upload_asset("p", "sha", "original", "image/jpeg", b"x"),
                    {"ok": True},
                )
        with patch.object(client, "_request", return_value=(409, {"error": "product_deleting"})):
            with self.assertRaises(PermanentCloudError):
                client.upload_asset("p", "sha", "original", "image/jpeg", b"x")
        with patch.object(client, "_request", return_value=(503, {})):
            with self.assertRaises(RetryableCloudError):
                client.upload_asset("p", "sha", "original", "image/jpeg", b"x")

    def test_flush_outbox_delivers_success_and_schedules_exponential_retries(self):
        now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        events = tuple(
            OutboxEvent(
                index + 1,
                f"key-{index}",
                "review.created",
                {},
                attempts,
                now.isoformat(),
                None,
            )
            for index, attempts in enumerate((0, 1, 8))
        )
        store = Mock()
        store.pending_outbox.return_value = events
        client = Mock()
        client.post_event.side_effect = (
            RetryableCloudError("cloud status 503"),
            RetryableCloudError("cloud status 503"),
            RetryableCloudError("cloud status 503"),
        )

        self.assertEqual(flush_outbox(store, client, now), 0)
        self.assertEqual(
            [call.args[2] for call in store.mark_retry.call_args_list],
            [
                "2026-09-09T00:00:01+00:00",
                "2026-09-09T00:00:02+00:00",
                "2026-09-09T00:04:16+00:00",
            ],
        )
        store.mark_delivered.assert_not_called()

        store.reset_mock()
        client.post_event.side_effect = None
        store.pending_outbox.return_value = events[:1]
        self.assertEqual(flush_outbox(store, client, now), 1)
        store.mark_delivered.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
