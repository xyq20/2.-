from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from learning_client import CloudLearningClient, flush_outbox_async
from learning_models import RunCheckpoint
from learning_store import LearningStore


class ResumeRejected(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ResumeDecision:
    run_id: str
    platform_index: int
    platform_id: str
    review_id: str
    final_value_id: str
    snapshot_version: str
    event_id: str
    device_id: str


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("payload")
    return value if isinstance(value, Mapping) else event


def validate_resume(
    checkpoint: RunCheckpoint,
    event: Mapping[str, Any],
    current_product_version: str,
    current_image_version: str,
    execution_mode: str,
) -> ResumeDecision:
    if checkpoint.status != "waiting_review":
        raise ResumeRejected("checkpoint_not_waiting_review")
    if checkpoint.product_version != current_product_version:
        raise ResumeRejected("product_version_changed")
    if checkpoint.image_version != current_image_version:
        raise ResumeRejected("image_version_changed")
    if checkpoint.execution_mode != execution_mode:
        raise ResumeRejected("execution_mode_changed")
    if event.get("processed_at") or event.get("consumed") is True:
        raise ResumeRejected("event_already_consumed")
    payload = _payload(event)
    if payload.get("run_id") != checkpoint.run_id:
        raise ResumeRejected("run_id_mismatch")
    if payload.get("device_id") != checkpoint.device_id:
        raise ResumeRejected("device_id_mismatch")
    if payload.get("product_version") != checkpoint.product_version:
        raise ResumeRejected("product_version_mismatch")
    if not (0 <= checkpoint.current_index < len(checkpoint.platform_order)):
        raise ResumeRejected("platform_index_invalid")
    platform = checkpoint.platform_order[checkpoint.current_index]
    if payload.get("platform_id") != platform:
        raise ResumeRejected("platform_mismatch")
    review_id = str(payload.get("review_id") or "")
    if not review_id or checkpoint.pending_review_id != review_id:
        raise ResumeRejected("review_id_mismatch")
    final_value_id = str(payload.get("final_value_id") or "")
    snapshot_version = str(payload.get("snapshot_version") or "")
    event_id = str(event.get("event_id") or "")
    if not final_value_id or not snapshot_version or not event_id:
        raise ResumeRejected("resume_payload_incomplete")
    return ResumeDecision(
        checkpoint.run_id,
        checkpoint.current_index,
        platform,
        review_id,
        final_value_id,
        snapshot_version,
        event_id,
        checkpoint.device_id,
    )


async def wait_for_review(
    store: LearningStore,
    client: CloudLearningClient,
    checkpoint: RunCheckpoint,
    *,
    current_product_version: str,
    current_image_version: str,
    execution_mode: str,
    cancelled: Optional[asyncio.Event] = None,
) -> ResumeDecision:
    while not (cancelled and cancelled.is_set()):
        await flush_outbox_async(store, client, datetime.now(timezone.utc))
        response = await asyncio.to_thread(
            client.poll_resume, checkpoint.device_id, 30
        )
        events = response.get("events", ())
        if not isinstance(events, (list, tuple)):
            raise ResumeRejected("resume_response_invalid")
        for event in events:
            if not isinstance(event, Mapping):
                continue
            try:
                return validate_resume(
                    checkpoint,
                    event,
                    current_product_version,
                    current_image_version,
                    execution_mode,
                )
            except ResumeRejected as caught:
                if caught.reason_code == "run_id_mismatch":
                    continue
                raise
    raise asyncio.CancelledError


async def persist_resume_and_ack(
    store: LearningStore,
    client: CloudLearningClient,
    checkpoint: RunCheckpoint,
    decision: ResumeDecision,
) -> RunCheckpoint:
    if checkpoint.run_id != decision.run_id:
        raise ResumeRejected("run_id_mismatch")
    persisted = store.save_checkpoint(
        replace(checkpoint, status="resume_pending", version=checkpoint.version)
    )
    await asyncio.to_thread(
        client.acknowledge_resume,
        decision.event_id,
        checkpoint_id=persisted.run_id,
        device_id=persisted.device_id,
    )
    return persisted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="恢复快麦待审核任务")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-id")
    target.add_argument("--latest", action="store_true")
    parser.add_argument("--db", default=".local-state/learning.sqlite3")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    store = LearningStore(Path(args.db))
    try:
        store.migrate()
        checkpoint = (
            store.latest_checkpoint()
            if args.latest
            else store.load_checkpoint(args.run_id)
        )
        if checkpoint is None:
            raise SystemExit("未找到可恢复的审核任务")
        if checkpoint.status != "waiting_review":
            raise SystemExit("任务当前不在等待审核状态")
        print(
            f"run_id={checkpoint.run_id} platform="
            f"{checkpoint.platform_order[checkpoint.current_index]}"
        )
        print(
            "请通过 kuaimai_erp.py --resume-run-id "
            f"{checkpoint.run_id} 恢复；执行模式将使用原检查点。"
        )
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
