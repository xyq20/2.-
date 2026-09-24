from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import secrets
import sqlite3
import uuid
from typing import Optional

from .database import utc_now


PBKDF2_ITERATIONS = 310_000


def hash_password(password: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    if len(password) != 4 or not password.isascii() or not password.isdigit():
        raise ValueError("审核密码必须恰好为 4 位数字")
    raw_salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), raw_salt, PBKDF2_ITERATIONS, 32
    )
    return base64.b64encode(raw_salt).decode(), base64.b64encode(digest).decode()


def verify_password(password: str, salt: str, expected: str) -> bool:
    try:
        raw_salt = base64.b64decode(salt, validate=True)
    except Exception:
        raw_salt = b"\0" * 16
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), raw_salt, PBKDF2_ITERATIONS, 32
    )
    actual = base64.b64encode(digest).decode()
    return hmac.compare_digest(actual, expected)


def create_user(
    connection: sqlite3.Connection,
    username: str,
    password: str,
    *,
    role: str = "admin",
) -> str:
    if role not in {"admin", "operator"}:
        raise ValueError("角色必须是 admin 或 operator")
    if not username or len(username) > 128:
        raise ValueError("用户名不能为空")
    salt, digest = hash_password(password)
    user_id = uuid.uuid4().hex
    connection.execute(
        "INSERT INTO users(id,username,password_salt,password_hash,role,active,created_at) "
        "VALUES(?,?,?,?,?,1,?)",
        (user_id, username, salt, digest, role, utc_now()),
    )
    return user_id


def new_session(connection: sqlite3.Connection, user_id: str) -> tuple[str, str]:
    token = secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(hours=8)
    connection.execute(
        "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
        (token_hash, user_id, expires.isoformat(), utc_now()),
    )
    return token, expires.isoformat()


def session_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
