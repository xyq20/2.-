from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import re
import sqlite3
import time
import uuid
from typing import Any, Mapping, Optional

import httpx

from platform_registry import platforms_equivalent

from .config import Settings
from .database import canonical_json, connect, transaction, utc_now


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def require(condition: object, status: int, code: str) -> None:
    if not condition:
        raise ApiError(status, code)


EVENT_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "product.upsert": (
        {"product_version", "style_code", "title"},
        {"category_json"},
    ),
    "snapshot.created": (
        {
            "snapshot_version",
            "platform_id",
            "category_leaf_id",
            "field_id",
            "field_label",
            "schema_version",
            "custom_allowed",
            "options",
        },
        {"canonical_field", "control_type"},
    ),
    "review.created": (
        {
            "run_id",
            "device_id",
            "product_version",
            "platform_id",
            "category_leaf_id",
            "field_id",
            "field_label",
            "snapshot_version",
            "reason_code",
            "evidence_json",
        },
        {"id", "canonical_field", "suggested_value_id"},
    ),
    "checkpoint.updated": (
        {
            "run_id",
            "product_version",
            "device_id",
            "execution_mode",
            "platform_order",
            "current_index",
            "status",
            "pending_review_id",
            "version",
        },
        {"image_version", "checkpoint_id"},
    ),
    "stage.completed": (
        {
            "run_id",
            "platform_id",
            "status",
            "expected_json",
            "readback_json",
            "verified",
        },
        {"product_version"},
    ),
    "readback.recorded": (
        {
            "run_id",
            "product_version",
            "platform_id",
            "category_leaf_id",
            "field_id",
            "snapshot_version",
            "verified",
            "payload_json",
        },
        {"actual_value_id", "actual_label"},
    ),
    "text_facts.created": (
        {"product_version", "source", "payload_json"},
        {"id"},
    ),
}


SENSITIVE_KEY = re.compile(
    r"password|passwd|token|cookie|authorization|secret|api.?key|chrome.?profile",
    re.IGNORECASE,
)

VISUAL_FACT_KEYS = {
    "garment_type",
    "visible_colors",
    "length_landmark",
    "silhouette",
    "thickness_evidence",
    "image_consistency",
    "evidence_asset_ids",
}
VISUAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(VISUAL_FACT_KEYS),
    "properties": {
        "garment_type": {"type": ["string", "null"]},
        "visible_colors": {"type": "array", "items": {"type": "string"}},
        "length_landmark": {
            "type": "string",
            "enum": ["above_knee", "knee", "calf", "ankle", "unknown"],
        },
        "silhouette": {
            "type": "string",
            "enum": ["slim", "straight", "loose", "unknown"],
        },
        "thickness_evidence": {
            "type": "string",
            "enum": ["thin", "regular", "thick", "insufficient"],
        },
        "image_consistency": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "evidence_asset_ids": {"type": "array", "items": {"type": "string"}},
    },
}


def _safe_object(value: Any, depth: int = 0) -> None:
    require(depth < 16, 400, "payload_too_deep")
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key)
            require(
                normalized == "text_tokens" or not SENSITIVE_KEY.search(normalized),
                400,
                "sensitive_key_rejected",
            )
            _safe_object(nested, depth + 1)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _safe_object(nested, depth + 1)


def _model_output_text(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for item in payload.get("output", []) if isinstance(payload.get("output"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("content"), list):
            continue
        for part in item["content"]:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    return None


def _valid_visual_facts(value: Any, asset_ids: set[str]) -> bool:
    if not isinstance(value, dict) or set(value) != VISUAL_FACT_KEYS:
        return False
    if value["garment_type"] is not None and not isinstance(value["garment_type"], str):
        return False
    if not isinstance(value["visible_colors"], list) or not all(
        isinstance(item, str) for item in value["visible_colors"]
    ):
        return False
    if value["length_landmark"] not in {"above_knee", "knee", "calf", "ankle", "unknown"}:
        return False
    if value["silhouette"] not in {"slim", "straight", "loose", "unknown"}:
        return False
    if value["thickness_evidence"] not in {"thin", "regular", "thick", "insufficient"}:
        return False
    consistency = value["image_consistency"]
    if not isinstance(consistency, dict) or not all(
        key in asset_ids and isinstance(note, str) for key, note in consistency.items()
    ):
        return False
    evidence = value["evidence_asset_ids"]
    if (
        not isinstance(evidence, list)
        or not all(isinstance(item, str) and item in asset_ids for item in evidence)
        or len(evidence) != len(set(evidence))
    ):
        return False
    has_visible_claim = bool(
        value["garment_type"] is not None
        or value["visible_colors"]
        or value["length_landmark"] != "unknown"
        or value["silhouette"] != "unknown"
        or value["thickness_evidence"] != "insufficient"
    )
    return not has_visible_claim or bool(evidence)


def _text(value: Any, name: str, maximum: int = 256) -> str:
    require(isinstance(value, str) and 0 < len(value) <= maximum, 400, f"invalid_{name}")
    return value


def validate_device_event(data: Any) -> tuple[str, str, dict[str, Any]]:
    require(isinstance(data, dict), 400, "invalid_payload")
    require(set(data) <= {"idempotency_key", "event_type", "payload"}, 400, "unknown_envelope_key")
    key = _text(data.get("idempotency_key"), "idempotency_key")
    event_type = _text(data.get("event_type"), "event_type")
    require(event_type in EVENT_FIELDS, 400, "unknown_event_type")
    payload = data.get("payload")
    require(isinstance(payload, dict), 400, "invalid_payload")
    _safe_object(payload)
    required, optional = EVENT_FIELDS[event_type]
    for name in required:
        require(name in payload, 400, f"missing_{name}")
    require(set(payload) <= required | optional, 400, "invalid_payload_keys")
    for name in required - {
        "category_json",
        "evidence_json",
        "expected_json",
        "readback_json",
        "payload_json",
        "options",
        "platform_order",
        "custom_allowed",
        "verified",
        "current_index",
        "version",
        "pending_review_id",
    }:
        _text(payload.get(name), name, 1000 if name == "title" else 256)
    if event_type == "snapshot.created":
        options = payload.get("options")
        custom_allowed = payload.get("custom_allowed")
        require(isinstance(custom_allowed, bool), 400, "invalid_custom_allowed")
        require(
            isinstance(options, list)
            and len(options) <= 5000
            and (bool(options) or custom_allowed),
            400,
            "invalid_options",
        )
        for option in options:
            require(isinstance(option, dict) and set(option) <= {"value_id", "label", "position"}, 400, "invalid_option")
            _text(option.get("value_id"), "value_id")
            _text(option.get("label"), "label")
            require(isinstance(option.get("position"), int) and option["position"] >= 0, 400, "invalid_position")
    if event_type in {"stage.completed", "readback.recorded"}:
        require(isinstance(payload.get("verified"), bool), 400, "invalid_verified")
    if event_type == "checkpoint.updated":
        legacy_checkpoint_id = payload.get("checkpoint_id")
        require(
            legacy_checkpoint_id is None
            or legacy_checkpoint_id == payload.get("run_id"),
            400,
            "checkpoint_id_mismatch",
        )
        order = payload.get("platform_order")
        require(isinstance(order, list) and order and all(isinstance(x, str) and x for x in order), 400, "invalid_platform_order")
        require(isinstance(payload.get("current_index"), int) and 0 <= payload["current_index"] <= len(order), 400, "invalid_index")
        require(isinstance(payload.get("version"), int) and payload["version"] > 0, 400, "invalid_version")
    normalized = dict(payload)
    if event_type == "checkpoint.updated":
        normalized.pop("checkpoint_id", None)
    return key, event_type, normalized


def _snapshot(connection: sqlite3.Connection, payload: dict[str, Any]) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM option_snapshots WHERE snapshot_version=? AND platform_id=? "
        "AND category_leaf_id=? AND field_id=?",
        (
            payload.get("snapshot_version"),
            payload.get("platform_id"),
            payload.get("category_leaf_id"),
            payload.get("field_id"),
        ),
    ).fetchone()
    require(row is not None, 422, "snapshot_mismatch")
    return row


CHECKPOINT_TRANSITIONS = {
    "running": {"running", "waiting_review", "failed", "completed", "cancelled"},
    "waiting_review": {"waiting_review", "resume_pending", "failed", "cancelled"},
    "resume_pending": {"resume_pending", "running", "failed", "cancelled"},
    "failed": {"failed", "running", "waiting_review", "cancelled"},
    "completed": {"completed"},
    "cancelled": {"cancelled"},
}


def _validate_checkpoint(connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
    status = str(payload.get("status"))
    require(status in CHECKPOINT_TRANSITIONS, 409, "checkpoint_state_conflict")
    payload.setdefault("image_version", "")
    prior = connection.execute(
        "SELECT * FROM run_checkpoints WHERE run_id=?", (payload["run_id"],)
    ).fetchone()
    if prior is None:
        require(status in {"running", "waiting_review"}, 409, "checkpoint_initial_state_conflict")
    else:
        identity_matches = (
            payload["product_version"] == prior["product_version"]
            and payload["device_id"] == prior["device_id"]
            and payload["execution_mode"] == prior["execution_mode"]
            and payload["image_version"] == prior["image_version"]
            and canonical_json(payload["platform_order"])
            == canonical_json(json.loads(prior["platform_order_json"]))
        )
        require(identity_matches, 409, "checkpoint_identity_conflict")
        version = int(payload["version"])
        require(version >= int(prior["version"]), 409, "checkpoint_version_conflict")
        same = (
            payload["current_index"] == prior["current_index"]
            and status == prior["status"]
            and payload.get("pending_review_id") == prior["pending_review_id"]
        )
        require(version != int(prior["version"]) or same, 409, "checkpoint_version_conflict")
        # A batch consumes one decision at a time. After acknowledging one,
        # wait for the next review on the SAME platform, not a new stage.
        batch_next_review = (
            prior["status"] == "resume_pending"
            and status == "waiting_review"
            and payload["current_index"] == prior["current_index"]
            and bool(payload.get("pending_review_id"))
            and payload["pending_review_id"] != prior["pending_review_id"]
        )
        require(
            (status in CHECKPOINT_TRANSITIONS[prior["status"]] or batch_next_review)
            and payload["current_index"] >= prior["current_index"],
            409, "checkpoint_state_conflict",
        )
        require(prior["status"] not in {"completed", "cancelled"} or same, 409, "checkpoint_terminal_conflict")
    if status in {"waiting_review", "resume_pending"}:
        review_id = payload.get("pending_review_id")
        require(isinstance(review_id, str) and review_id, 409, "checkpoint_review_required")
        task = connection.execute(
            "SELECT status,platform_id FROM review_tasks WHERE id=? AND run_id=? "
            "AND product_version=? AND device_id=?",
            (review_id, payload["run_id"], payload["product_version"], payload["device_id"]),
        ).fetchone()
        index = int(payload["current_index"])
        require(
            task is not None
            and index < len(payload["platform_order"])
            and platforms_equivalent(
                payload["platform_order"][index], task["platform_id"]
            ),
            409,
            "checkpoint_review_mismatch",
        )
        if status == "resume_pending":
            require(task["status"] in {"resume_ready", "consumed"}, 409, "checkpoint_review_not_confirmed")


STAGE_TRANSITIONS = {
    "started": {"filled", "saved", "failed", "readback_verified"},
    "filled": {"saved", "failed", "readback_verified"},
    "saved": {"failed", "readback_verified"},
    "failed": {"readback_verified"},
    "readback_verified": set(),
}


def _validate_stage(connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
    status = str(payload.get("status"))
    require(status in STAGE_TRANSITIONS and bool(payload.get("verified")) == (status == "readback_verified"), 422, "invalid_stage_state")
    prior = connection.execute(
        "SELECT * FROM stage_results WHERE run_id=? AND platform_id=?",
        (payload["run_id"], payload["platform_id"]),
    ).fetchone()
    if prior is None:
        return
    same = (
        prior["status"] == status
        and int(prior["verified"]) == int(bool(payload["verified"]))
        and canonical_json(json.loads(prior["expected_json"])) == canonical_json(payload["expected_json"])
        and canonical_json(json.loads(prior["readback_json"])) == canonical_json(payload["readback_json"])
    )
    require(same or status in STAGE_TRANSITIONS[prior["status"]], 409, "stage_state_or_evidence_conflict")


def ingest_event(settings: Settings, data: Any) -> tuple[int, dict[str, Any]]:
    key, event_type, payload = validate_device_event(data)
    with transaction(settings) as connection:
        existing = connection.execute(
            "SELECT id,event_type,payload_json FROM device_events WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if existing is not None:
            require(
                existing["event_type"] == event_type
                and canonical_json(json.loads(existing["payload_json"])) == canonical_json(payload),
                409,
                "idempotency_payload_conflict",
            )
            return 409, {"event_id": existing["id"]}
        if payload.get("product_version"):
            product = connection.execute(
                "SELECT deleting FROM products WHERE product_version=?",
                (payload["product_version"],),
            ).fetchone()
            require(product is None or not product["deleting"], 409, "product_deleting")
        if event_type in {"review.created", "readback.recorded"}:
            snapshot = _snapshot(connection, payload)
            options = json.loads(snapshot["options_json"])
            if event_type == "review.created":
                require(payload["field_label"] == snapshot["field_label"], 422, "snapshot_label_mismatch")
                mapping = connection.execute(
                    "SELECT canonical_field FROM platform_fields WHERE platform_id=? "
                    "AND category_leaf_id=? AND source_field_id=? AND schema_version=?",
                    (payload["platform_id"], payload["category_leaf_id"], payload["field_id"], snapshot["schema_version"]),
                ).fetchone()
                canonical = mapping["canonical_field"] if mapping else None
                require(payload.get("canonical_field") in {None, canonical}, 422, "canonical_field_mismatch")
                payload["canonical_field"] = canonical
                if canonical is None:
                    payload["reason_code"] = "field_mapping_required"
                payload["suggested_value_id"] = payload.get("suggested_value_id") or None
                require(
                    payload["suggested_value_id"] is None
                    or sum(1 for option in options if option["value_id"] == payload["suggested_value_id"]) == 1,
                    422,
                    "suggested_candidate_not_unique",
                )
            else:
                actual = payload.get("actual_value_id")
                require(isinstance(actual, str) and actual, 422, "actual_value_required")
                human_value = connection.execute(
                    "SELECT 1 FROM attribute_decisions WHERE snapshot_version=? "
                    "AND product_version=? AND final_value_id=? "
                    "AND source='human' AND status='confirmed' LIMIT 1",
                    (payload["snapshot_version"], payload.get("product_version"), actual),
                ).fetchone()
                require(
                    bool(snapshot["custom_allowed"])
                    or bool(human_value)
                    or sum(1 for option in options if option["value_id"] == actual) == 1,
                    422,
                    "actual_candidate_not_unique",
                )
        event_product = payload.get("product_version")
        if event_type in {"readback.recorded", "stage.completed"}:
            checkpoint = connection.execute(
                "SELECT product_version,platform_order_json FROM run_checkpoints WHERE run_id=?",
                (payload["run_id"],),
            ).fetchone()
            require(
                checkpoint is not None
                and (not payload.get("product_version") or payload["product_version"] == checkpoint["product_version"])
                and payload["platform_id"] in json.loads(checkpoint["platform_order_json"]),
                422,
                "checkpoint_mismatch",
            )
            event_product = checkpoint["product_version"]
        if event_type == "checkpoint.updated":
            _validate_checkpoint(connection, payload)
        if event_type == "stage.completed":
            _validate_stage(connection, payload)

        event_id = uuid.uuid4().hex
        now = utc_now()
        if event_type == "product.upsert":
            connection.execute(
                "INSERT INTO products(product_version,style_code,title,category_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(product_version) DO UPDATE SET "
                "style_code=excluded.style_code,title=excluded.title,category_json=excluded.category_json,"
                "updated_at=excluded.updated_at WHERE products.deleting=0",
                (payload["product_version"], payload["style_code"], payload["title"], canonical_json(payload.get("category_json", {})), now, now),
            )
        connection.execute(
            "INSERT INTO device_events(id,idempotency_key,event_type,product_version,device_id,payload_json,created_at,processed_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (event_id, key, event_type, event_product, payload.get("device_id"), canonical_json(payload), now, now),
        )
        if event_type == "snapshot.created":
            serialized = canonical_json(payload["options"])
            prior = connection.execute(
                "SELECT * FROM option_snapshots WHERE snapshot_version=?",
                (payload["snapshot_version"],),
            ).fetchone()
            if prior is not None:
                require(
                    prior["options_json"] == serialized
                    and prior["platform_id"] == payload["platform_id"]
                    and prior["category_leaf_id"] == payload["category_leaf_id"]
                    and prior["field_id"] == payload["field_id"]
                    and prior["schema_version"] == payload["schema_version"],
                    409,
                    "snapshot_version_conflict",
                )
            else:
                connection.execute(
                    "INSERT INTO option_snapshots(snapshot_version,platform_id,category_leaf_id,field_id,field_label,schema_version,custom_allowed,options_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (payload["snapshot_version"], payload["platform_id"], payload["category_leaf_id"], payload["field_id"], payload["field_label"], payload["schema_version"], int(payload["custom_allowed"]), serialized, now),
                )
            connection.execute(
                "INSERT OR IGNORE INTO platform_fields(id,platform_id,category_leaf_id,source_field_id,label,canonical_field,control_type,custom_allowed,schema_version,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f'{payload["platform_id"]}:{payload["category_leaf_id"]}:{payload["field_id"]}:{payload["schema_version"]}', payload["platform_id"], payload["category_leaf_id"], payload["field_id"], payload["field_label"], payload.get("canonical_field"), payload.get("control_type", "select"), int(payload["custom_allowed"]), payload["schema_version"], now),
            )
        elif event_type == "review.created":
            review_id = payload.get("id") or event_id
            connection.execute(
                "INSERT INTO review_tasks(id,idempotency_key,run_id,device_id,product_version,platform_id,category_leaf_id,field_id,field_label,canonical_field,snapshot_version,suggested_value_id,status,reason_code,evidence_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, key, payload["run_id"], payload["device_id"], payload["product_version"], payload["platform_id"], payload["category_leaf_id"], payload["field_id"], payload["field_label"], payload.get("canonical_field"), payload["snapshot_version"], payload.get("suggested_value_id"), "pending", payload["reason_code"], canonical_json(payload["evidence_json"]), now, now),
            )
            connection.execute(
                "UPDATE device_events SET review_id=? WHERE id=?", (review_id, event_id)
            )
        elif event_type == "checkpoint.updated":
            connection.execute(
                "INSERT INTO run_checkpoints(run_id,product_version,device_id,execution_mode,platform_order_json,current_index,status,pending_review_id,version,image_version,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET current_index=excluded.current_index,status=excluded.status,pending_review_id=excluded.pending_review_id,version=excluded.version,updated_at=excluded.updated_at WHERE excluded.version>run_checkpoints.version",
                (payload["run_id"], payload["product_version"], payload["device_id"], payload["execution_mode"], canonical_json(payload["platform_order"]), payload["current_index"], payload["status"], payload.get("pending_review_id"), payload["version"], payload.get("image_version", ""), now),
            )
        elif event_type == "stage.completed":
            connection.execute(
                "INSERT INTO stage_results(idempotency_key,run_id,platform_id,status,expected_json,readback_json,verified,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,platform_id) DO UPDATE SET idempotency_key=excluded.idempotency_key,status=excluded.status,expected_json=excluded.expected_json,readback_json=excluded.readback_json,verified=excluded.verified,updated_at=excluded.updated_at WHERE excluded.status<>stage_results.status",
                (key, payload["run_id"], payload["platform_id"], payload["status"], canonical_json(payload["expected_json"]), canonical_json(payload["readback_json"]), int(payload["verified"]), now),
            )
        elif event_type == "readback.recorded":
            connection.execute(
                "INSERT INTO persisted_readbacks(idempotency_key,run_id,product_version,platform_id,field_id,snapshot_version,actual_value_id,actual_label,verified,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (key, payload["run_id"], payload["product_version"], payload["platform_id"], payload["field_id"], payload["snapshot_version"], payload.get("actual_value_id"), payload.get("actual_label"), int(payload["verified"]), canonical_json(payload["payload_json"]), now),
            )
            if payload["verified"]:
                _record_verified_rule_outcome(connection, key)
        elif event_type == "text_facts.created":
            connection.execute(
                "INSERT INTO text_facts(id,product_version,source,payload_json,created_at) VALUES(?,?,?,?,?)",
                (payload.get("id") or event_id, payload["product_version"], payload["source"], canonical_json(payload["payload_json"]), now),
            )
    return 201, {"event_id": event_id}


def _condition_signature(connection: sqlite3.Connection, product_version: str) -> str:
    visual = connection.execute(
        "SELECT payload_json FROM visual_facts WHERE product_version=?", (product_version,)
    ).fetchone()
    text = connection.execute(
        "SELECT payload_json FROM text_facts WHERE product_version=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (product_version,),
    ).fetchone()
    try:
        visual_facts = json.loads(visual["payload_json"]) if visual else {}
    except Exception:
        visual_facts = {}
    try:
        text_facts = json.loads(text["payload_json"]) if text else {}
    except Exception:
        text_facts = {}
    conditions = {
        "garment_type": visual_facts.get("garment_type"),
        "length_landmark": visual_facts.get("length_landmark"),
        "silhouette": visual_facts.get("silhouette"),
        "text_tokens": sorted(set(x for x in text_facts.get("text_tokens", []) if isinstance(x, str) and x)),
    }
    stable = json.dumps(conditions, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(stable.encode()).hexdigest()


def _record_verified_rule_outcome(
    connection: sqlite3.Connection, readback_key: str
) -> bool:
    if connection.execute(
        "SELECT 1 FROM rule_outcomes WHERE idempotency_key=?", (readback_key,)
    ).fetchone():
        return False
    outcome = connection.execute(
        "SELECT pr.idempotency_key,pr.product_version,pr.platform_id,"
        "s.category_leaf_id,d.canonical_field,pr.snapshot_version,"
        "pr.actual_value_id,d.proposed_value_id,d.final_value_id,"
        "s.schema_version,s.options_json FROM persisted_readbacks pr "
        "JOIN option_snapshots s ON s.snapshot_version=pr.snapshot_version "
        "JOIN attribute_decisions d ON d.product_version=pr.product_version "
        "AND d.platform_id=pr.platform_id AND d.field_id=pr.field_id "
        "AND d.snapshot_version=pr.snapshot_version "
        "AND d.final_value_id=pr.actual_value_id AND d.source='human' "
        "AND d.status='confirmed' WHERE pr.idempotency_key=? AND pr.verified=1 "
        "AND d.canonical_field IS NOT NULL ORDER BY d.created_at DESC LIMIT 1",
        (readback_key,),
    ).fetchone()
    if outcome is None:
        return False
    candidates = json.loads(outcome["options_json"])
    candidate_valid = (
        sum(
            1
            for candidate in candidates
            if candidate.get("value_id") == outcome["final_value_id"]
        )
        == 1
    )
    accepted = bool(
        candidate_valid
        and outcome["proposed_value_id"] is not None
        and outcome["proposed_value_id"] == outcome["final_value_id"]
    )
    signature = _condition_signature(connection, outcome["product_version"])
    existing = connection.execute(
        "SELECT * FROM conditional_rules WHERE platform_id=? "
        "AND category_leaf_id=? AND canonical_field=? AND condition_signature=?",
        (
            outcome["platform_id"],
            outcome["category_leaf_id"],
            outcome["canonical_field"],
            signature,
        ),
    ).fetchone()
    same_target = bool(
        existing and existing["target_value_id"] == outcome["final_value_id"]
    )
    same_schema = bool(
        existing and existing["schema_version"] == outcome["schema_version"]
    )
    consecutive = (
        (int(existing["consecutive_confirmations"]) if existing else 0) + 1
        if accepted
        and candidate_valid
        and (existing is None or (same_target and same_schema))
        else 0
    )
    accepted_count = (int(existing["accepted_count"]) if existing else 0) + int(
        accepted
    )
    total_count = (int(existing["total_count"]) if existing else 0) + 1
    status = (
        "active"
        if candidate_valid
        and (existing is None or same_schema)
        and consecutive >= 3
        and accepted_count / total_count >= 0.95
        else "observing"
    )
    now = utc_now()
    rule_id = existing["id"] if existing else uuid.uuid4().hex
    connection.execute(
        "INSERT INTO rule_outcomes(idempotency_key,product_version,platform_id,"
        "category_leaf_id,canonical_field,condition_signature,snapshot_version,"
        "target_value_id,proposed_value_id,accepted,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            outcome["idempotency_key"],
            outcome["product_version"],
            outcome["platform_id"],
            outcome["category_leaf_id"],
            outcome["canonical_field"],
            signature,
            outcome["snapshot_version"],
            outcome["final_value_id"],
            outcome["proposed_value_id"],
            int(accepted),
            now,
        ),
    )
    connection.execute(
        "INSERT INTO conditional_rules(id,platform_id,category_leaf_id,"
        "canonical_field,condition_signature,target_value_id,schema_version,"
        "consecutive_confirmations,accepted_count,total_count,status,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform_id,category_leaf_id,"
        "canonical_field,condition_signature) DO UPDATE SET "
        "target_value_id=excluded.target_value_id,schema_version=excluded.schema_version,"
        "consecutive_confirmations=excluded.consecutive_confirmations,"
        "accepted_count=excluded.accepted_count,total_count=excluded.total_count,"
        "status=excluded.status,updated_at=excluded.updated_at",
        (
            rule_id,
            outcome["platform_id"],
            outcome["category_leaf_id"],
            outcome["canonical_field"],
            signature,
            outcome["final_value_id"],
            outcome["schema_version"],
            consecutive,
            accepted_count,
            total_count,
            status,
            now,
        ),
    )
    return True


def _decision_response(
    request: dict[str, Any],
    *,
    reason: str,
    value_id: Optional[str] = None,
    label: Optional[str] = None,
    source: Optional[str] = None,
    mature: bool = False,
    support: int = 0,
    rate: float = 0.0,
) -> dict[str, Any]:
    return {
        "status": "auto_fill_ready" if value_id is not None else "review_required",
        "value_id": value_id,
        "value_label": label,
        "source": source,
        "reason_code": "validated" if value_id is not None else reason,
        "snapshot_version": request["snapshot_version"],
        "evidence_kinds": ["text"] if source == "explicit_text" else (["human"] if source == "human_override" else (["visual"] if source == "mature_rule" else [])),
        "mature_rule": mature,
        "support_count": support,
        "calibrated_acceptance_rate": rate,
    }


def decide_attribute(settings: Settings, data: Any) -> dict[str, Any]:
    keys = {"product_version", "platform_id", "category_leaf_id", "field_id", "canonical_field", "snapshot_version"}
    require(isinstance(data, dict) and set(data) == keys, 400, "invalid_decision_request")
    for key in keys:
        _text(data[key], key)
    with transaction(settings) as connection:
        product = connection.execute(
            "SELECT 1 FROM products WHERE product_version=? AND deleting=0",
            (data["product_version"],),
        ).fetchone()
        require(product is not None, 404, "product_missing")
        snapshot = _snapshot(connection, data)
        options = json.loads(snapshot["options_json"])
        ids = [option.get("value_id") for option in options]
        require(options and len(ids) == len(set(ids)), 422, "snapshot_candidates_invalid")
        mapping = connection.execute(
            "SELECT canonical_field FROM platform_fields WHERE platform_id=? AND category_leaf_id=? AND source_field_id=? AND schema_version=?",
            (data["platform_id"], data["category_leaf_id"], data["field_id"], snapshot["schema_version"]),
        ).fetchone()
        if mapping is None or mapping["canonical_field"] != data["canonical_field"]:
            response = _decision_response(data, reason="field_mapping_required")
        else:
            human = connection.execute(
                "SELECT final_value_id FROM attribute_decisions WHERE product_version=? AND platform_id=? AND category_leaf_id=? AND field_id=? AND canonical_field=? AND snapshot_version=? AND source='human' AND status='confirmed' AND final_value_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                (data["product_version"], data["platform_id"], data["category_leaf_id"], data["field_id"], data["canonical_field"], data["snapshot_version"]),
            ).fetchone()
            response = None
            if human and (
                ids.count(human["final_value_id"]) == 1
                or bool(snapshot["custom_allowed"])
                or data["field_id"] != "__category__"
            ):
                option = next(
                    (
                        option
                        for option in options
                        if option["value_id"] == human["final_value_id"]
                    ),
                    None,
                )
                final_value = human["final_value_id"]
                response = _decision_response(
                    data,
                    reason="",
                    value_id=final_value,
                    label=option["label"] if option else final_value,
                    source="human_override",
                )
            if response is None:
                text_row = connection.execute(
                    "SELECT payload_json FROM text_facts WHERE product_version=? ORDER BY created_at DESC,id DESC LIMIT 1",
                    (data["product_version"],),
                ).fetchone()
                text_facts = json.loads(text_row["payload_json"]) if text_row else {}
                conflicts = text_facts.get("conflicts", []) if isinstance(text_facts, dict) else []
                if isinstance(conflicts, list) and data["canonical_field"] in conflicts:
                    response = _decision_response(data, reason="evidence_conflict")
                values = text_facts.get("values", {}) if isinstance(text_facts, dict) else {}
                fact = values.get(data["canonical_field"]) if isinstance(values, dict) else None
                explicit = fact if isinstance(fact, str) else (fact.get("value_id") if isinstance(fact, dict) else None)
                if response is None and isinstance(explicit, str) and ids.count(explicit) == 1:
                    option = next(option for option in options if option["value_id"] == explicit)
                    response = _decision_response(data, reason="", value_id=explicit, label=option["label"], source="explicit_text")
            if response is None:
                signature = _condition_signature(connection, data["product_version"])
                rule = connection.execute(
                    "SELECT target_value_id,accepted_count,total_count FROM conditional_rules WHERE platform_id=? AND category_leaf_id=? AND canonical_field=? AND condition_signature=? AND schema_version=? AND status='active' AND consecutive_confirmations>=3 AND total_count>0 AND (accepted_count*1.0/total_count)>=0.95",
                    (data["platform_id"], data["category_leaf_id"], data["canonical_field"], signature, snapshot["schema_version"]),
                ).fetchone()
                if rule and ids.count(rule["target_value_id"]) == 1:
                    option = next(option for option in options if option["value_id"] == rule["target_value_id"])
                    response = _decision_response(data, reason="", value_id=option["value_id"], label=option["label"], source="mature_rule", mature=True, support=int(rule["total_count"]), rate=float(rule["accepted_count"]) / int(rule["total_count"]))
                else:
                    # Category IDs and option IDs are vendor-scoped.  A mature
                    # rule for the same platform/field can therefore be reused
                    # by another category when its live candidate *name* is
                    # present exactly once.  Resolve the historical target ID
                    # back to its saved snapshot label before mapping it onto
                    # the current option ID.
                    cross_category = connection.execute(
                        "SELECT cr.target_value_id,cr.accepted_count,cr.total_count,"
                        "cr.updated_at,os.options_json "
                        "FROM conditional_rules cr "
                        "JOIN rule_outcomes ro ON ro.platform_id=cr.platform_id "
                        "AND ro.category_leaf_id=cr.category_leaf_id "
                        "AND ro.canonical_field=cr.canonical_field "
                        "AND ro.condition_signature=cr.condition_signature "
                        "AND ro.target_value_id=cr.target_value_id "
                        "JOIN option_snapshots os ON os.snapshot_version=ro.snapshot_version "
                        "WHERE cr.platform_id=? AND cr.canonical_field=? "
                        "AND cr.condition_signature=? AND cr.status='active' "
                        "AND cr.consecutive_confirmations>=3 AND cr.total_count>0 "
                        "AND (cr.accepted_count*1.0/cr.total_count)>=0.95 "
                        "ORDER BY cr.updated_at DESC,ro.created_at DESC",
                        (data["platform_id"], data["canonical_field"], signature),
                    ).fetchall()
                    matched_cross_category = None
                    for candidate_rule in cross_category:
                        old_options = json.loads(candidate_rule["options_json"])
                        old_labels = [
                            option.get("label")
                            for option in old_options
                            if option.get("value_id") == candidate_rule["target_value_id"]
                        ]
                        if len(old_labels) != 1:
                            continue
                        live_matches = [
                            option for option in options
                            if option.get("label") == old_labels[0]
                        ]
                        if len(live_matches) != 1:
                            continue
                        matched_cross_category = (candidate_rule, live_matches[0])
                        break
                    if matched_cross_category is not None:
                        candidate_rule, option = matched_cross_category
                        support = int(candidate_rule["total_count"])
                        response = _decision_response(
                            data,
                            reason="",
                            value_id=option["value_id"],
                            label=option["label"],
                            source="mature_rule",
                            mature=True,
                            support=support,
                            rate=float(candidate_rule["accepted_count"]) / support,
                        )
                    else:
                        support = int(rule["total_count"]) if rule else 0
                        rate = float(rule["accepted_count"]) / support if rule and support else 0.0
                        response = _decision_response(data, reason="insufficient_evidence", support=support, rate=rate)
        now = utc_now()
        connection.execute(
            "INSERT INTO attribute_decisions(id,product_version,platform_id,category_leaf_id,field_id,canonical_field,snapshot_version,proposed_value_id,final_value_id,source,status,reason_code,evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, data["product_version"], data["platform_id"], data["category_leaf_id"], data["field_id"], data["canonical_field"], data["snapshot_version"], response["value_id"], response["value_id"] if response["status"] == "auto_fill_ready" else None, response["source"] or "review", response["status"], response["reason_code"], canonical_json({}), now, now),
        )
        return response


def analyze_product(settings: Settings, product_version: str) -> dict[str, Any]:
    with connect(settings) as connection:
        cached = connection.execute(
            "SELECT payload_json FROM visual_facts WHERE product_version=?", (product_version,)
        ).fetchone()
        if cached:
            return {"status": "ready", "facts": json.loads(cached["payload_json"]), "cached": True}
        product = connection.execute(
            "SELECT 1 FROM products WHERE product_version=? AND deleting=0", (product_version,)
        ).fetchone()
        require(product is not None, 404, "product_missing")
        if not settings.model_api_key or not settings.model_api_url:
            return {"status": "review_required", "reason_code": "model_not_configured"}
        rows = connection.execute(
            "SELECT id,r2_key,content_type FROM assets WHERE product_version=? "
            "AND (kind='learning_thumbnail' OR (kind='original' AND NOT EXISTS("
            "SELECT 1 FROM assets thumbnails WHERE thumbnails.product_version=assets.product_version "
            "AND thumbnails.kind='learning_thumbnail'))) ORDER BY created_at,id",
            (product_version,),
        ).fetchall()
        if not rows:
            return {"status": "review_required", "reason_code": "image_evidence_missing"}
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                "Only report facts visibly supported by the supplied product images. "
                "Never infer material percentages, brand claims, or functionality. "
                "Use unknown or insufficient when evidence is absent. Cite only the "
                "supplied asset IDs in evidence_asset_ids."
            ),
        }
    ]
    asset_ids: set[str] = set()
    for row in rows:
        asset_path = settings.assets_dir / row["r2_key"]
        if not asset_path.is_file():
            continue
        asset_ids.add(row["id"])
        content.append({"type": "input_text", "text": f"asset_id={row['id']}"})
        encoded = base64.b64encode(asset_path.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{row['content_type']};base64,{encoded}",
            }
        )
    if not asset_ids:
        return {"status": "review_required", "reason_code": "image_evidence_missing"}
    try:
        upstream = httpx.post(
            settings.model_api_url,
            headers={
                "authorization": f"Bearer {settings.model_api_key}",
                "content-type": "application/json",
            },
            json={
                "model": settings.ai_model,
                "input": [{"role": "user", "content": content}],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "kuaimai_visual_facts",
                        "strict": True,
                        "schema": VISUAL_SCHEMA,
                    }
                },
            },
            timeout=90,
        )
    except httpx.HTTPError:
        raise ApiError(503, "model_request_failed") from None
    if not upstream.is_success:
        raise ApiError(503 if upstream.status_code >= 500 else 422, "model_request_failed")
    try:
        output = _model_output_text(upstream.json())
        facts = json.loads(output) if output is not None else None
    except (TypeError, ValueError):
        facts = None
    if not _valid_visual_facts(facts, asset_ids):
        return {"status": "review_required", "reason_code": "model_output_invalid"}
    with transaction(settings) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO visual_facts(product_version,payload_json,model,created_at) "
            "VALUES(?,?,?,?)",
            (product_version, canonical_json(facts), settings.ai_model, utc_now()),
        )
        winner = connection.execute(
            "SELECT payload_json FROM visual_facts WHERE product_version=?",
            (product_version,),
        ).fetchone()
    require(winner is not None, 409, "analysis_cache_conflict")
    return {"status": "ready", "facts": json.loads(winner["payload_json"]), "cached": False}
