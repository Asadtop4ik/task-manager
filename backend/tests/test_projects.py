from httpx import AsyncClient

from app.db.models import Project, User
from tests.conftest import auth


async def test_executor_only_sees_their_own_projects(
    client: AsyncClient, executor: User, outsider: User, project: Project
) -> None:
    mine = (await client.get("/api/v1/projects", headers=auth(executor))).json()
    assert [p["key"] for p in mine] == [project.key]

    theirs = (await client.get("/api/v1/projects", headers=auth(outsider))).json()
    assert theirs == []


async def test_only_a_manager_creates_projects(
    client: AsyncClient, executor: User, manager: User
) -> None:
    body = {"key": "new-bot", "name": "New Bot"}
    assert (
        await client.post("/api/v1/projects", json=body, headers=auth(executor))
    ).status_code == 403

    created = await client.post("/api/v1/projects", json=body, headers=auth(manager))
    assert created.status_code == 201
    assert created.json()["key"] == "new-bot"


async def test_a_duplicate_key_is_a_409(
    client: AsyncClient, manager: User, project: Project
) -> None:
    response = await client.post(
        "/api/v1/projects", json={"key": project.key, "name": "Clash"}, headers=auth(manager)
    )
    assert response.status_code == 409


async def test_membership_grants_visibility(
    client: AsyncClient, manager: User, outsider: User, project: Project
) -> None:
    assert (await client.get("/api/v1/projects", headers=auth(outsider))).json() == []

    added = await client.put(
        f"/api/v1/projects/{project.id}/members/{outsider.id}", headers=auth(manager)
    )
    assert added.status_code == 200

    after = (await client.get("/api/v1/projects", headers=auth(outsider))).json()
    assert [p["key"] for p in after] == [project.key]


async def test_archived_projects_are_hidden_by_default(
    client: AsyncClient, manager: User, project: Project
) -> None:
    await client.patch(
        f"/api/v1/projects/{project.id}", json={"is_archived": True}, headers=auth(manager)
    )
    assert (await client.get("/api/v1/projects", headers=auth(manager))).json() == []
    with_archived = await client.get(
        "/api/v1/projects?include_archived=true", headers=auth(manager)
    )
    assert len(with_archived.json()) == 1
