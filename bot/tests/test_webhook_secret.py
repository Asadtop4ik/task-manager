from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def _client() -> TestClient:
    # No lifespan on purpose: entering it would register a webhook with Telegram,
    # which needs a real token and a public URL.
    return TestClient(app)


def test_webhook_rejects_missing_secret_header() -> None:
    response = _client().post("/webhook/telegram", json={"update_id": 1})
    assert response.status_code == 403


def test_webhook_rejects_wrong_secret_header() -> None:
    response = _client().post(
        "/webhook/telegram",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret + "x"},
    )
    assert response.status_code == 403


def test_webhook_rejects_a_prefix_of_the_secret() -> None:
    # The comparison is constant-time; this pins that a truncated value is not
    # accepted, which a naive startswith-style check would allow.
    response = _client().post(
        "/webhook/telegram",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret[:-1]},
    )
    assert response.status_code == 403
