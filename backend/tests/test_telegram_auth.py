import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import pytest

from app.core.config import settings
from app.core.security import (
    TelegramAuthError,
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_init_data,
    verify_login_widget,
)


def sign_widget(data: dict[str, Any]) -> dict[str, Any]:
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    key = hashlib.sha256(settings.bot_token.encode()).digest()
    return {**data, "hash": hmac.new(key, check.encode(), hashlib.sha256).hexdigest()}


def sign_init_data(user: dict[str, Any], auth_date: int | None = None) -> str:
    fields = {
        "user": json.dumps(user, separators=(",", ":")),
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAdummy",
    }
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": signature})


class TestLoginWidget:
    def test_accepts_a_correct_signature(self) -> None:
        payload = sign_widget(
            {"id": 42, "first_name": "Asad", "username": "asad", "auth_date": int(time.time())}
        )
        assert verify_login_widget(payload)["id"] == "42"

    def test_rejects_a_tampered_field(self) -> None:
        payload = sign_widget({"id": 42, "first_name": "Asad", "auth_date": int(time.time())})
        # The whole point of the hash: swapping the id after signing must not work.
        payload["id"] = 43
        with pytest.raises(TelegramAuthError):
            verify_login_widget(payload)

    def test_rejects_a_stale_auth_date(self) -> None:
        # A captured login URL must not still work tomorrow.
        payload = sign_widget(
            {"id": 42, "first_name": "Asad", "auth_date": int(time.time()) - 3600}
        )
        with pytest.raises(TelegramAuthError):
            verify_login_widget(payload)

    def test_rejects_a_missing_hash(self) -> None:
        with pytest.raises(TelegramAuthError):
            verify_login_widget({"id": 42, "auth_date": int(time.time())})


class TestMiniApp:
    def test_accepts_a_correct_signature(self) -> None:
        init_data = sign_init_data({"id": 42, "first_name": "Asad"})
        assert verify_init_data(init_data)["id"] == 42

    def test_widget_signature_is_not_accepted_as_miniapp(self) -> None:
        """The two use different keys; this is the mix-up that breaks every login.

        A widget payload signed with SHA256(token) must NOT verify under the Mini
        App's HMAC("WebAppData", token) key, or the distinction is decorative.
        """
        fields = {"user": json.dumps({"id": 42}), "auth_date": str(int(time.time()))}
        check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        wrong_key = hashlib.sha256(settings.bot_token.encode()).digest()
        signature = hmac.new(wrong_key, check.encode(), hashlib.sha256).hexdigest()
        with pytest.raises(TelegramAuthError):
            verify_init_data(urlencode({**fields, "hash": signature}))

    def test_rejects_a_stale_auth_date(self) -> None:
        with pytest.raises(TelegramAuthError):
            verify_init_data(sign_init_data({"id": 42}, auth_date=int(time.time()) - 3600))


class TestTokens:
    def test_access_token_round_trip(self) -> None:
        assert decode_token(create_access_token(7), "access") == 7

    def test_a_refresh_token_is_not_an_access_token(self) -> None:
        """Otherwise a 30-day credential would carry a 15-minute one's powers."""
        with pytest.raises(TokenError):
            decode_token(create_refresh_token(7), "access")

    def test_rejects_a_token_signed_with_another_secret(self) -> None:
        import jwt

        forged = jwt.encode(
            {"sub": "7", "type": "access", "exp": 9_999_999_999}, "not-our-secret"
        )
        with pytest.raises(TokenError):
            decode_token(forged, "access")
