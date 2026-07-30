"""Session + bearer token auth for browser-agent.

Better Auth is a TypeScript library; this module provides the equivalent
for FastAPI: username/password login, signed HttpOnly cookie sessions,
and APP_AUTH_TOKEN bearer for API/automation.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "ba_session"
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(60 * 60 * 24 * 14)))  # 14d


def _secret() -> str:
    return (
        os.getenv("AUTH_SECRET")
        or os.getenv("APP_AUTH_TOKEN")
        or "dev-insecure-change-me"
    )


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt="browser-agent-session")


def auth_enabled() -> bool:
    return bool(os.getenv("APP_USERNAME") and os.getenv("APP_PASSWORD"))


def bearer_token() -> str | None:
    return os.getenv("APP_AUTH_TOKEN") or None


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000
    )
    return f"{salt}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    # Support plain env password comparison via runtime hash
    # stored format salt$hex OR plain (env APP_PASSWORD compared directly)
    if "$" not in stored:
        return hmac.compare_digest(password, stored)
    salt, _hex = stored.split("$", 1)
    return hmac.compare_digest(_hash_password(password, salt), stored)


def verify_credentials(username: str, password: str) -> bool:
    expected_user = os.getenv("APP_USERNAME", "")
    expected_pass = os.getenv("APP_PASSWORD", "")
    if not expected_user or not expected_pass:
        return False
    return hmac.compare_digest(username, expected_user) and _verify_password(
        password, expected_pass
    )


def create_session_token(username: str) -> str:
    return _serializer().dumps({"u": username, "iat": int(time.time())})


def read_session_token(token: str) -> dict[str, Any] | None:
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
        if not isinstance(data, dict) or "u" not in data:
            return None
        return data
    except (BadSignature, SignatureExpired, Exception):
        return None


def set_session_cookie(response: Response, username: str) -> None:
    token = create_session_token(username)
    secure = os.getenv("AUTH_COOKIE_SECURE", "true").lower() in ("1", "true", "yes")
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


@dataclass
class AuthUser:
    username: str
    via: str  # session | bearer


def _headers_get(headers: Any, key: str) -> str | None:
    try:
        return headers.get(key)
    except Exception:
        return None


def authenticate_headers_cookies(
    headers: Any,
    cookies: dict[str, str] | None = None,
    query_token: str | None = None,
) -> AuthUser | None:
    expected = bearer_token()

    auth = (
        _headers_get(headers, "authorization")
        or _headers_get(headers, "Authorization")
        or ""
    )
    if expected and isinstance(auth, str) and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if hmac.compare_digest(token, expected):
            return AuthUser(username="token", via="bearer")

    api_key = _headers_get(headers, "x-api-key") or _headers_get(headers, "X-API-Key")
    if api_key and expected and hmac.compare_digest(str(api_key), expected):
        return AuthUser(username="token", via="bearer")

    if query_token and expected and hmac.compare_digest(query_token, expected):
        return AuthUser(username="token", via="bearer")

    cookies = cookies or {}
    cookie = cookies.get(COOKIE_NAME)
    if cookie:
        data = read_session_token(cookie)
        if data:
            return AuthUser(username=str(data["u"]), via="session")

    return None


def authenticate_request(request: Request) -> AuthUser | None:
    return authenticate_headers_cookies(request.headers, dict(request.cookies))


PUBLIC_PATHS = {
    "/health",
    "/api/auth/login",
    "/api/auth/status",
    "/login",
    "/favicon.ico",
}


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/static/"):
        return True
    return False


def require_user(request: Request) -> AuthUser:
    if not auth_enabled() and not bearer_token():
        # Auth not configured — open mode (dev only)
        return AuthUser(username="anonymous", via="open")
    user = authenticate_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user
