"""Telegram identity verification and JWT issuance.

Two Telegram entry points, one identity. Both hand us a signed blob; the whole
job here is proving the signature came from our own bot token before anyone is
let in.
"""

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl

import jwt

from app.core.config import settings

# Telegram's payloads carry auth_date. Anything older than this is refused so a
# captured login URL cannot be replayed tomorrow.
MAX_AUTH_AGE_SECONDS = 300


class TelegramAuthError(Exception):
    """The payload did not come from our bot, or is too old to trust."""


def _data_check_string(data: dict[str, str]) -> str:
    return "\n".join(f"{key}={data[key]}" for key in sorted(data))


def _check_auth_date(raw: str | None) -> int:
    if raw is None:
        raise TelegramAuthError("auth_date is missing")
    try:
        auth_date = int(raw)
    except ValueError as exc:
        raise TelegramAuthError("auth_date is not an integer") from exc
    age = time.time() - auth_date
    if age > MAX_AUTH_AGE_SECONDS:
        raise TelegramAuthError("auth_date is too old")
    # A clock-skewed future date is equally suspect, but allow a minute of drift.
    if age < -60:
        raise TelegramAuthError("auth_date is in the future")
    return auth_date


def verify_login_widget(payload: dict[str, Any]) -> dict[str, Any]:
    """Verify a Telegram Login Widget callback.

    Key is SHA256(bot_token) — note this differs from the Mini App below. Getting
    the two mixed up produces an identical-looking "invalid hash" for every user.
    """
    data = {k: str(v) for k, v in payload.items() if k != "hash" and v is not None}
    provided = payload.get("hash")
    if not provided:
        raise TelegramAuthError("hash is missing")

    secret_key = hashlib.sha256(settings.bot_token.encode()).digest()
    expected = hmac.new(
        secret_key, _data_check_string(data).encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, str(provided)):
        raise TelegramAuthError("hash mismatch")

    _check_auth_date(data.get("auth_date"))
    if "id" not in data:
        raise TelegramAuthError("id is missing")
    return data


def verify_init_data(init_data: str) -> dict[str, Any]:
    """Verify a Mini App `initData` query string.

    Same construction as the widget except the key is HMAC-SHA256("WebAppData",
    bot_token) rather than a plain SHA256 of the token.
    """
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    provided = pairs.pop("hash", None)
    if not provided:
        raise TelegramAuthError("hash is missing")

    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(
        secret_key, _data_check_string(pairs).encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, provided):
        raise TelegramAuthError("hash mismatch")

    _check_auth_date(pairs.get("auth_date"))

    raw_user = pairs.get("user")
    if not raw_user:
        raise TelegramAuthError("user is missing")
    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise TelegramAuthError("user is not valid JSON") from exc
    if "id" not in user:
        raise TelegramAuthError("user.id is missing")
    return user


# --------------------------------------------------------------------- JWT


def _encode(subject: int, token_type: str, ttl_seconds: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(subject), "type": token_type, "iat": now, "exp": now + ttl_seconds},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def create_access_token(user_id: int) -> str:
    return _encode(user_id, "access", settings.jwt_access_ttl_minutes * 60)


def create_refresh_token(user_id: int) -> str:
    return _encode(user_id, "refresh", settings.jwt_refresh_ttl_days * 86400)


class TokenError(Exception):
    pass


def decode_token(token: str, expected_type: str) -> int:
    """Return the user id, or raise. Never returns for the wrong token type.

    Without the type check a refresh token would be accepted as an access token,
    which would hand a 30-day credential the powers of a 15-minute one.
    """
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if claims.get("type") != expected_type:
        raise TokenError("wrong token type")
    try:
        return int(claims["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("bad subject") from exc
