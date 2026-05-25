from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AppSetting

PASSWORD_SETTING_KEY = "auth.password_hash"
DEFAULT_SESSION_AGE_SECONDS = 60 * 60 * 24 * 14


@dataclass(frozen=True)
class SessionUser:
    username: str
    csrf_token: str


def hash_password(password: str, *, iterations: int = 390_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def ensure_password_hash(db: Session, settings: Settings) -> None:
    existing = db.get(AppSetting, PASSWORD_SETTING_KEY)
    if existing:
        return
    password_hash = settings.app_password_hash or hash_password(settings.app_password)
    db.add(AppSetting(key=PASSWORD_SETTING_KEY, value_json=json.dumps(password_hash)))
    db.commit()


def get_password_hash(db: Session) -> str | None:
    setting = db.get(AppSetting, PASSWORD_SETTING_KEY)
    if not setting:
        return None
    try:
        return json.loads(setting.value_json)
    except json.JSONDecodeError:
        return None


def _sign(data: bytes, secret_key: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret_key.encode("utf-8"), data, hashlib.sha256).digest()).decode("ascii")


def encode_session(payload: dict[str, Any], secret_key: str) -> str:
    data = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    signature = _sign(data.encode("ascii"), secret_key)
    return f"{data}.{signature}"


def decode_session(cookie_value: str | None, secret_key: str) -> dict[str, Any] | None:
    if not cookie_value or "." not in cookie_value:
        return None
    data, signature = cookie_value.rsplit(".", 1)
    expected = _sign(data.encode("ascii"), secret_key)
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None
    if int(payload.get("expires_at", 0)) < int(time.time()):
        return None
    return payload


def set_login_cookie(response: Response, settings: Settings, username: str) -> None:
    payload = {
        "username": username,
        "csrf_token": secrets.token_urlsafe(32),
        "expires_at": int(time.time()) + DEFAULT_SESSION_AGE_SECONDS,
    }
    response.set_cookie(
        settings.session_cookie_name,
        encode_session(payload, settings.app_secret_key),
        max_age=DEFAULT_SESSION_AGE_SECONDS,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )


def clear_login_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.session_cookie_name)


def current_user_from_request(request: Request, settings: Settings) -> SessionUser | None:
    payload = decode_session(request.cookies.get(settings.session_cookie_name), settings.app_secret_key)
    if not payload:
        return None
    username = str(payload.get("username", ""))
    csrf_token = str(payload.get("csrf_token", ""))
    if not username or not csrf_token:
        return None
    return SessionUser(username=username, csrf_token=csrf_token)


def require_user(request: Request, settings: Settings) -> SessionUser:
    user = current_user_from_request(request, settings)
    if not user:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


def validate_csrf(request: Request, settings: Settings, token: str) -> None:
    user = require_user(request, settings)
    if not hmac.compare_digest(user.csrf_token, token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

