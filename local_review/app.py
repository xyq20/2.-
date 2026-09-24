from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .config import Settings
from .database import backup_if_due, canonical_json, connect, migrate, remove_expired_originals, transaction, utc_now
from .security import hash_password, new_session, session_hash, verify_password
from .service import ApiError, analyze_product, decide_attribute, ingest_event, require
from .ui import SCRIPT, STYLES, page


SESSION_COOKIE = "km_session"
ASSET_ID = re.compile(r"^[0-9a-f]{64}$")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
MAX_ASSET_BYTES = 10 * 1024 * 1024
LEASE_SECONDS = 10 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCK_SECONDS = 10 * 60


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _same_origin(request: Request) -> None:
    forwarded = request.headers.get("x-forwarded-proto")
    scheme = forwarded.split(",", 1)[0].strip() if forwarded else request.url.scheme
    expected = f"{scheme}://{request.headers.get('host', request.url.netloc)}"
    require(request.headers.get("origin") == expected, 403, "same_origin_required")


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.split(",", 1)[0].strip() == "https"


def _login_scope(request: Request, username: str) -> str:
    address = request.headers.get("cf-connecting-ip")
    if not address:
        address = request.client.host if request.client else "unknown"
    return hashlib.sha256(f"{address}\0{username}".encode()).hexdigest()


def _check_login_rate(request: Request, scope: str) -> None:
    now = time.monotonic()
    failures = request.app.state.login_failures.get(scope, [])
    recent = [stamp for stamp in failures if now - stamp < LOGIN_LOCK_SECONDS]
    request.app.state.login_failures[scope] = recent
    require(len(recent) < LOGIN_FAILURE_LIMIT, 429, "login_temporarily_locked")


def _record_login_failure(request: Request, scope: str) -> None:
    request.app.state.login_failures.setdefault(scope, []).append(time.monotonic())


def _require_device(request: Request) -> None:
    token = _settings(request).device_token
    supplied = request.headers.get("authorization", "")
    require(bool(token) and hmac.compare_digest(supplied, f"Device {token}"), 401, "unauthorized")


def _session_user(request: Request) -> Optional[dict[str, Any]]:
    raw = request.cookies.get(SESSION_COOKIE, "")
    if not raw:
        return None
    with connect(_settings(request)) as connection:
        row = connection.execute(
            "SELECT u.id,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND s.revoked_at IS NULL AND u.active=1",
            (session_hash(raw), utc_now()),
        ).fetchone()
    return dict(row) if row else None


def _require_user(request: Request) -> dict[str, Any]:
    user = _session_user(request)
    require(user is not None, 401, "unauthorized")
    return user


def _require_admin(user: dict[str, Any]) -> None:
    require(user["role"] == "admin", 403, "admin_required")


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception:
        raise ApiError(400, "invalid_json") from None
    require(isinstance(value, dict), 400, "invalid_json")
    return value


def _response(data: object, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"cache-control": "no-store"})


def _review_tasks(settings: Settings, user_id: str) -> list[dict[str, Any]]:
    now = utc_now()
    with connect(settings) as connection:
        rows = connection.execute(
            "SELECT r.*,p.title,s.options_json,s.custom_allowed FROM review_tasks r "
            "JOIN products p USING(product_version) JOIN option_snapshots s USING(snapshot_version) "
            "WHERE p.deleting=0 AND r.status IN ('pending','claimed') "
            "AND NOT EXISTS(SELECT 1 FROM run_checkpoints c WHERE c.run_id=r.run_id "
            "AND c.status IN ('failed','completed')) "
            "AND (r.claimed_by IS NULL OR r.claimed_by=? OR r.lease_until<=?) "
            "ORDER BY r.created_at,r.id LIMIT 50",
            (user_id, now),
        ).fetchall()
        tasks: list[dict[str, Any]] = []
        for row in rows:
            assets = connection.execute(
                "SELECT id FROM assets WHERE product_version=? "
                "ORDER BY CASE WHEN kind='learning_thumbnail' THEN 0 ELSE 1 END,"
                "created_at,id LIMIT 8",
                (row["product_version"],),
            ).fetchall()
            task = dict(row)
            evidence = json.loads(task.pop("evidence_json"))
            options = json.loads(task.pop("options_json"))
            task.update(
                custom_allowed=not evidence.get("selection_only", False) and (
                    bool(task["custom_allowed"]) or task["field_id"] != "__category__"
                ),
                options=options,
                evidence=evidence,
                asset_ids=[asset["id"] for asset in assets],
            )
            tasks.append(task)
        return tasks


def _mutate_review(
    settings: Settings,
    user: dict[str, Any],
    review_id: str,
    action: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    version = data.get("version")
    require(isinstance(version, int) and version > 0, 400, "invalid_version")
    now = utc_now()
    lease = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    with transaction(settings) as connection:
        task = connection.execute(
            "SELECT * FROM review_tasks WHERE id=?", (review_id,)
        ).fetchone()
        require(task is not None, 409, "task_locked_or_updated")
        if action in {"claim", "renew", "skip"}:
            if action == "claim":
                allowed = (
                    task["status"] in {"pending", "claimed"}
                    and (
                        task["claimed_by"] is None
                        or task["claimed_by"] == user["id"]
                        or (task["lease_until"] or "") <= now
                    )
                )
            else:
                allowed = (
                    task["status"] == "claimed"
                    and task["claimed_by"] == user["id"]
                    and (task["lease_until"] or "") > now
                )
            product = connection.execute(
                "SELECT deleting FROM products WHERE product_version=?",
                (task["product_version"],),
            ).fetchone()
            require(
                allowed and int(task["version"]) == version and product and not product["deleting"],
                409,
                "task_locked_or_updated",
            )
            status = "pending" if action == "skip" else "claimed"
            claimed_by = None if action == "skip" else user["id"]
            lease_until = None if action == "skip" else lease
            connection.execute(
                "UPDATE review_tasks SET status=?,claimed_by=?,lease_until=?,version=version+1,updated_at=? WHERE id=?",
                (status, claimed_by, lease_until, now, review_id),
            )
            return {
                "id": review_id,
                "version": version + 1,
                "status": status,
                "lease_until": lease_until,
            }
        require(action == "confirm", 404, "not_found")
        final = data.get("final_value_id")
        require(
            isinstance(final, str) and 0 < len(final.strip()) <= 256,
            400,
            "invalid_final_value_id",
        )
        # 审核确认兼容多选：final_value_id 支持逗号分隔多个候选
        # valueId（或 custom_allowed 时的自定义值），与 Excel 的
        # “逗号分组全选”语法一致；每个部分不得重复。
        parts = [
            part.strip()
            for part in re.split(r"[,，、;；]", final.strip())
            if part.strip()
        ]
        require(
            parts and len(parts) == len(set(parts)),
            400,
            "invalid_final_value_id",
        )
        final = ",".join(parts)
        reason = data.get("correction_reason")
        require(reason is None or isinstance(reason, str) and 0 < len(reason) <= 1000, 400, "invalid_correction_reason")
        require(
            task["status"] == "claimed"
            and task["claimed_by"] == user["id"]
            and int(task["version"]) == version
            and (task["lease_until"] or "") > now,
            409,
            "task_locked_or_updated",
        )
        snapshot = connection.execute(
            "SELECT options_json,custom_allowed FROM option_snapshots WHERE snapshot_version=?",
            (task["snapshot_version"],),
        ).fetchone()
        options = json.loads(snapshot["options_json"]) if snapshot else []
        selection_only = bool(json.loads(task["evidence_json"]).get("selection_only"))
        unmatched = [
            part
            for part in parts
            if sum(
                1
                for option in options
                if part in {option.get("value_id"), option.get("label")}
            ) != 1
        ]
        require(
            not unmatched
            or (not selection_only and (
                task["field_id"] != "__category__"
                or bool(snapshot and snapshot["custom_allowed"]))),
            422,
            "candidate_not_unique",
        )
        require(
            task["suggested_value_id"] is None
            or parts == [task["suggested_value_id"]]
            or bool(reason),
            422,
            "correction_reason_required",
        )
        action_id = uuid.uuid4().hex
        event_id = uuid.uuid4().hex
        next_version = version + 1
        resume_payload = {
            "review_id": review_id,
            "run_id": task["run_id"],
            "device_id": task["device_id"],
            "product_version": task["product_version"],
            "platform_id": task["platform_id"],
            "category_leaf_id": task["category_leaf_id"],
            "field_id": task["field_id"],
            "snapshot_version": task["snapshot_version"],
            "final_value_id": final,
            "version": next_version,
        }
        connection.execute(
            "INSERT INTO review_actions(id,idempotency_key,review_id,user_id,suggested_value_id,final_value_id,correction_reason,snapshot_version,task_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (action_id, f"confirm:{review_id}:{version}", review_id, user["id"], task["suggested_value_id"], final, reason, task["snapshot_version"], version, now),
        )
        connection.execute(
            "INSERT INTO attribute_decisions(id,product_version,platform_id,category_leaf_id,field_id,canonical_field,snapshot_version,proposed_value_id,final_value_id,source,status,reason_code,evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'human','confirmed',?,?,?,?)",
            (action_id, task["product_version"], task["platform_id"], task["category_leaf_id"], task["field_id"], task["canonical_field"], task["snapshot_version"], task["suggested_value_id"], final, task["reason_code"], task["evidence_json"], now, now),
        )
        connection.execute(
            "INSERT INTO device_events(id,idempotency_key,event_type,product_version,device_id,review_id,payload_json,created_at) VALUES(?,?,'resume_ready',?,?,?,?,?)",
            (event_id, f"resume_ready:{review_id}:{next_version}", task["product_version"], task["device_id"], review_id, canonical_json(resume_payload), now),
        )
        connection.execute(
            "UPDATE review_tasks SET status='resume_ready',version=?,updated_at=? WHERE id=?",
            (next_version, now, review_id),
        )
        return {"id": review_id, "version": next_version, "status": "resume_ready"}


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    configured = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migrate(configured)
        backup_if_due(configured)
        remove_expired_originals(configured)
        yield

    app = FastAPI(title="快麦审核中心", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = configured
    app.state.login_failures = {}

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, error: ApiError):
        return _response({"error": error.code}, error.status)

    @app.exception_handler(sqlite3.IntegrityError)
    async def sqlite_error(_request: Request, _error: sqlite3.IntegrityError):
        return _response({"error": "event_dependency_or_conflict"}, 422)

    @app.get("/health")
    async def health():
        return _response({"ok": True, "service": "kuaimai-local-review", "storage": "local"})

    @app.get("/")
    async def root(request: Request):
        return HTMLResponse(
            page(_session_user(request) is not None),
            headers={
                "cache-control": "no-store",
                "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
                "referrer-policy": "no-referrer",
                "x-content-type-options": "nosniff",
            },
        )

    @app.get("/app.css")
    async def css():
        return Response(STYLES, media_type="text/css", headers={"cache-control": "no-cache", "x-content-type-options": "nosniff"})

    @app.get("/app.js")
    async def javascript():
        return Response(SCRIPT, media_type="text/javascript", headers={"cache-control": "no-cache", "x-content-type-options": "nosniff"})

    @app.post("/api/login")
    async def login(request: Request):
        _same_origin(request)
        data = await _json_body(request)
        username = data.get("username")
        password = data.get("password")
        require(isinstance(username, str) and 0 < len(username) <= 128, 400, "invalid_username")
        require(
            isinstance(password, str)
            and len(password) == 4
            and password.isascii()
            and password.isdigit(),
            400,
            "invalid_password",
        )
        scope = _login_scope(request, username)
        _check_login_rate(request, scope)
        with transaction(configured) as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE username=? AND active=1", (username,)
            ).fetchone()
            if user is None:
                hashlib.pbkdf2_hmac(
                    "sha256", password.encode("utf-8"), b"\0" * 16, 310_000, 32
                )
                _record_login_failure(request, scope)
                raise ApiError(401, "invalid_credentials")
            if not verify_password(password, user["password_salt"], user["password_hash"]):
                _record_login_failure(request, scope)
                raise ApiError(401, "invalid_credentials")
            token, _expires = new_session(connection, user["id"])
        request.app.state.login_failures.pop(scope, None)
        response = _response({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=8 * 60 * 60,
            httponly=True,
            secure=_is_https(request),
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        _same_origin(request)
        _require_user(request)
        raw = request.cookies.get(SESSION_COOKIE, "")
        with transaction(configured) as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at=? WHERE token_hash=?",
                (utc_now(), session_hash(raw)),
            )
        response = _response({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.post("/api/device/events")
    async def device_events(request: Request):
        _require_device(request)
        status, result = ingest_event(configured, await _json_body(request))
        return _response(result, status)

    @app.put("/api/device/assets/{sha256}")
    async def upload_asset(sha256: str, request: Request):
        _require_device(request)
        require(bool(ASSET_ID.fullmatch(sha256)), 400, "invalid_sha256")
        product_version = request.headers.get("x-product-version", "")
        kind = request.headers.get("x-asset-kind", "")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        require(bool(SAFE_IDENTIFIER.fullmatch(product_version)), 400, "invalid_product_version")
        require(kind in {"original", "learning_thumbnail"}, 400, "invalid_asset_kind")
        require(content_type in {"image/jpeg", "image/png", "image/webp"}, 415, "invalid_content_type")
        body = await request.body()
        require(0 < len(body) <= MAX_ASSET_BYTES, 413, "asset_too_large")
        require(hashlib.sha256(body).hexdigest() == sha256, 422, "sha256_mismatch")
        relative = Path("products") / product_version / sha256 / kind
        target = configured.assets_dir / relative
        with transaction(configured) as connection:
            product = connection.execute(
                "SELECT deleting FROM products WHERE product_version=?", (product_version,)
            ).fetchone()
            require(product is not None and not product["deleting"], 404, "product_missing")
            existing = connection.execute(
                "SELECT id FROM assets WHERE product_version=? AND sha256=? AND kind=?",
                (product_version, sha256, kind),
            ).fetchone()
            if existing:
                return _response({"asset_id": existing["id"]})
            target.parent.mkdir(parents=True, exist_ok=True)
            file_descriptor, temporary_name = tempfile.mkstemp(prefix="asset-", dir=target.parent)
            try:
                with os.fdopen(file_descriptor, "wb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
            asset_id = uuid.uuid4().hex
            delete_after = (
                (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
                if kind == "original"
                else None
            )
            connection.execute(
                "INSERT INTO assets(id,product_version,r2_key,sha256,kind,content_type,byte_size,delete_after,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (asset_id, product_version, str(relative), sha256, kind, content_type, len(body), delete_after, utc_now()),
            )
        return _response({"asset_id": asset_id}, 201)

    @app.post("/api/device/analyze")
    async def analyze(request: Request):
        _require_device(request)
        data = await _json_body(request)
        product_version = data.get("product_version")
        require(isinstance(product_version, str) and product_version, 400, "invalid_product_version")
        return _response(
            await asyncio.to_thread(analyze_product, configured, product_version)
        )

    @app.post("/api/device/decide")
    async def decide(request: Request):
        _require_device(request)
        return _response(decide_attribute(configured, await _json_body(request)))

    @app.get("/api/device/resume")
    async def resume(request: Request, device_id: str, wait_seconds: int = 0):
        _require_device(request)
        require(bool(SAFE_IDENTIFIER.fullmatch(device_id)), 400, "invalid_device_id")
        deadline = asyncio.get_running_loop().time() + max(0, min(wait_seconds, 30))
        while True:
            with connect(configured) as connection:
                rows = connection.execute(
                    "SELECT e.id,e.payload_json FROM device_events e JOIN review_tasks r ON r.id=e.review_id "
                    "JOIN products p ON p.product_version=e.product_version WHERE e.device_id=? "
                    "AND e.event_type='resume_ready' AND e.processed_at IS NULL AND r.status='resume_ready' "
                    "AND p.deleting=0 ORDER BY e.created_at LIMIT 100",
                    (device_id,),
                ).fetchall()
            if rows or asyncio.get_running_loop().time() >= deadline:
                return _response({"events": [{"event_id": row["id"], "payload": json.loads(row["payload_json"])} for row in rows]})
            await asyncio.sleep(0.5)

    @app.post("/api/device/resume/{event_id}/ack")
    async def acknowledge(event_id: str, request: Request):
        _require_device(request)
        data = await _json_body(request)
        require(data.get("checkpoint_persisted") is True, 422, "persist_checkpoint_before_ack")
        checkpoint_id = data.get("checkpoint_id")
        device_id = data.get("device_id")
        require(isinstance(checkpoint_id, str) and checkpoint_id and isinstance(device_id, str) and device_id, 422, "persist_checkpoint_before_ack")
        with transaction(configured) as connection:
            event = connection.execute(
                "SELECT e.*,r.status task_status FROM device_events e JOIN review_tasks r ON r.id=e.review_id "
                "WHERE e.id=? AND e.device_id=? AND e.event_type='resume_ready'",
                (event_id, device_id),
            ).fetchone()
            require(event is not None and event["task_status"] in {"resume_ready", "consumed"}, 409, "resume_unavailable")
            if event["processed_at"]:
                require(event["checkpoint_id"] == checkpoint_id, 409, "checkpoint_conflict")
                return _response({"ok": True})
            connection.execute(
                "UPDATE device_events SET processed_at=?,checkpoint_id=? WHERE id=?",
                (utc_now(), checkpoint_id, event_id),
            )
            connection.execute(
                "UPDATE review_tasks SET status='consumed',version=version+1,updated_at=? WHERE id=? AND status='resume_ready'",
                (utc_now(), event["review_id"]),
            )
        return _response({"ok": True})

    @app.get("/api/reviews")
    async def reviews(request: Request):
        user = _require_user(request)
        return _response({"tasks": _review_tasks(configured, user["id"])})

    @app.post("/api/reviews/{review_id}/{action}")
    async def review_action(review_id: str, action: str, request: Request):
        _same_origin(request)
        user = _require_user(request)
        return _response(_mutate_review(configured, user, review_id, action, await _json_body(request)))

    @app.get("/api/assets/{asset_id}")
    async def read_asset(asset_id: str, request: Request):
        user = _require_user(request)
        with connect(configured) as connection:
            row = connection.execute(
                "SELECT * FROM assets WHERE id=?", (asset_id,)
            ).fetchone()
            require(row is not None, 404, "asset_missing")
            if user["role"] != "admin":
                allowed = connection.execute(
                    "SELECT 1 FROM review_tasks WHERE product_version=? AND claimed_by=? "
                    "AND status='claimed' AND lease_until>?",
                    (row["product_version"], user["id"], utc_now()),
                ).fetchone()
                require(allowed is not None, 403, "asset_forbidden")
            path = configured.assets_dir / row["r2_key"]
        require(path.is_file(), 404, "asset_missing")
        return FileResponse(path, media_type=row["content_type"], headers={"cache-control": "private, no-store", "x-content-type-options": "nosniff"})

    @app.get("/api/admin/status")
    async def admin_status(request: Request):
        user = _require_user(request)
        _require_admin(user)
        with connect(configured) as connection:
            counts = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("products", "review_tasks", "option_snapshots", "persisted_readbacks")
            }
        return _response({"ok": True, "storage": "local", "counts": counts})

    return app


app = create_app()
