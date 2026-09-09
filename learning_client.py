from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from typing import Any, Mapping, Optional, Tuple
from urllib import error, parse, request

from learning_models import canonical_json
from learning_store import LearningStore


DEVICE_EVENT_TYPES = frozenset(
    {
        "product.upsert",
        "snapshot.created",
        "checkpoint.updated",
        "stage.completed",
        "readback.recorded",
        "review.created",
        "text_facts.created",
    }
)


class LearningClientError(RuntimeError):
    pass


class LearningClientConfigurationError(LearningClientError):
    pass


class RetryableCloudError(LearningClientError):
    pass


class PermanentCloudError(LearningClientError):
    pass


class CloudLearningClient:
    """Small authenticated client whose exceptions never include request secrets."""

    def __init__(
        self,
        base_url: str,
        device_token: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        token = device_token or os.environ.get("KUAIMAI_LEARNING_DEVICE_TOKEN", "")
        if not base_url.strip():
            raise LearningClientConfigurationError("learning API URL is required")
        if not token:
            raise LearningClientConfigurationError("learning device authentication is required")
        self.base_url = base_url.rstrip("/")
        self.device_token = token
        self.timeout = timeout

    def post_event(
        self,
        key: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if event_type not in DEVICE_EVENT_TYPES:
            raise ValueError("unsupported device event type")
        status, body = self._request(
            "POST",
            "/api/device/events",
            {
                "idempotency_key": key,
                "event_type": event_type,
                "payload": payload,
            },
        )
        return self._successful_json(status, body, idempotent_conflict=True)

    def decide(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        status, body = self._request("POST", "/api/device/decide", payload)
        return self._successful_json(status, body, idempotent_conflict=True)

    def poll_resume(
        self,
        device_id: str,
        wait_seconds: int = 30,
    ) -> Mapping[str, Any]:
        bounded_wait = max(0, min(int(wait_seconds), 30))
        query = parse.urlencode(
            {"device_id": device_id, "wait_seconds": bounded_wait}
        )
        status, body = self._request(
            "GET",
            f"/api/device/resume?{query}",
            None,
            timeout=max(self.timeout, bounded_wait + 5.0),
        )
        return self._successful_json(status, body)

    def get_resume_events(
        self,
        device_id: str,
        wait_seconds: int = 30,
    ) -> Mapping[str, Any]:
        return self.poll_resume(device_id, wait_seconds=wait_seconds)

    def acknowledge_resume(
        self,
        event_id: str,
        *,
        checkpoint_id: str,
        device_id: str,
    ) -> Mapping[str, Any]:
        if not checkpoint_id or not device_id:
            raise ValueError("durable checkpoint_id and device_id are required")
        encoded_event_id = parse.quote(event_id, safe="")
        status, body = self._request(
            "POST",
            f"/api/device/resume/{encoded_event_id}/ack",
            {
                "checkpoint_persisted": True,
                "checkpoint_id": checkpoint_id,
                "device_id": device_id,
            },
        )
        return self._successful_json(status, body, idempotent_conflict=True)

    def upload_asset(
        self,
        product_version: str,
        sha256: str,
        kind: str,
        content_type: str,
        body: bytes,
    ) -> Mapping[str, Any]:
        encoded_sha = parse.quote(sha256, safe="")
        status, response = self._request(
            "PUT",
            f"/api/device/assets/{encoded_sha}",
            None,
            body=body,
            headers={
                "Content-Type": content_type,
                "X-Product-Version": product_version,
                "X-Asset-Kind": kind,
            },
        )
        return self._successful_json(status, response, idempotent_conflict=True)

    def _successful_json(
        self,
        status: int,
        body: Mapping[str, Any],
        *,
        idempotent_conflict: bool = False,
    ) -> Mapping[str, Any]:
        successful = {200, 201}
        if idempotent_conflict:
            successful.add(409)
        if status in successful:
            return body
        if status == 429 or status >= 500:
            raise RetryableCloudError(f"cloud status {status}")
        raise PermanentCloudError(f"cloud status {status}")

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]],
        *,
        body: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[int, Mapping[str, Any]]:
        request_headers = {
            "Authorization": f"Device {self.device_token}",
        }
        request_headers.update(headers or {})
        data = body
        if payload is not None:
            data = canonical_json(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        built_request = request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with request.urlopen(
                built_request,
                timeout=self.timeout if timeout is None else timeout,
            ) as response:
                return int(response.status), self._decode_response(response.read())
        except error.HTTPError as caught:
            return int(caught.code), self._decode_response(caught.read())
        except (error.URLError, TimeoutError, OSError):
            raise RetryableCloudError("cloud request failed") from None

    @staticmethod
    def _decode_response(raw: bytes) -> Mapping[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


def flush_outbox(
    store: LearningStore,
    client: CloudLearningClient,
    now: datetime,
) -> int:
    """Best-effort delivery; retryable cloud failures remain in the local outbox."""
    delivered = 0
    for event in store.pending_outbox(limit=50):
        try:
            client.post_event(
                event.idempotency_key,
                event.event_type,
                event.payload,
            )
        except RetryableCloudError as caught:
            delay = min(300, 2 ** min(event.attempts, 8))
            store.mark_retry(
                event.id,
                str(caught),
                (now + timedelta(seconds=delay)).isoformat(),
            )
            continue
        except PermanentCloudError:
            raise
        store.mark_delivered(event.id)
        delivered += 1
    return delivered
