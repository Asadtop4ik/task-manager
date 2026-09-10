from fastapi.testclient import TestClient


def test_health_is_process_only(client: TestClient) -> None:
    """Liveness must not depend on Postgres or Redis.

    If it did, a database blip would make Docker restart a perfectly healthy
    container and turn a small outage into a crash loop.
    """
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_both_dependencies(client: TestClient) -> None:
    response = client.get("/ready")
    # 503 is a legitimate answer when a dependency is down; the shape is what
    # this test pins, since the container healthcheck reads the status code.
    assert response.status_code in (200, 503)
    body = response.json()
    assert set(body["checks"]) == {"postgres", "redis"}
    assert (body["status"] == "ok") == (response.status_code == 200)
