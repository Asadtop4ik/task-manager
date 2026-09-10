from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Project, User
from tests.conftest import auth


async def _create(client: AsyncClient, user: User, project: Project, **overrides):
    body = {"project_id": project.id, "title": "Fix the Mini App URL"} | overrides
    return await client.post("/api/v1/tasks", json=body, headers=auth(user))


class TestCreateAndRead:
    async def test_manager_creates_and_assigns_in_one_call(
        self, client: AsyncClient, manager: User, executor: User, project: Project
    ) -> None:
        response = await _create(
            client, manager, project, assignee_id=executor.id, priority="urgent"
        )
        assert response.status_code == 201
        body = response.json()
        assert body["assignee"]["id"] == executor.id
        assert body["created_by"]["id"] == manager.id
        assert body["status"] == "todo"
        assert body["project"]["key"] == project.key

    async def test_executor_cannot_assign_to_someone_else(
        self, client: AsyncClient, executor: User, manager: User, project: Project
    ) -> None:
        response = await _create(client, executor, project, assignee_id=manager.id)
        assert response.status_code == 403

    async def test_a_task_cannot_be_created_already_done(
        self, client: AsyncClient, manager: User, project: Project
    ) -> None:
        assert (await _create(client, manager, project, status="done")).status_code == 422

    async def test_creating_records_activity(
        self, client: AsyncClient, manager: User, project: Project
    ) -> None:
        task_id = (await _create(client, manager, project)).json()["id"]
        entries = (
            await client.get(f"/api/v1/tasks/{task_id}/activity", headers=auth(manager))
        ).json()
        assert [e["kind"] for e in entries] == ["created"]
        assert entries[0]["actor"]["id"] == manager.id


class TestVisibility:
    async def test_a_non_member_cannot_see_the_task(
        self, client: AsyncClient, manager: User, outsider: User, project: Project
    ) -> None:
        """404 rather than 403: 'exists but not yours' leaks the id space."""
        task_id = (await _create(client, manager, project)).json()["id"]
        response = await client.get(f"/api/v1/tasks/{task_id}", headers=auth(outsider))
        assert response.status_code == 404

    async def test_a_non_member_sees_an_empty_list(
        self, client: AsyncClient, manager: User, outsider: User, project: Project
    ) -> None:
        await _create(client, manager, project)
        body = (await client.get("/api/v1/tasks", headers=auth(outsider))).json()
        assert body["items"] == []
        assert body["total"] == 0

    async def test_a_member_sees_the_project_board(
        self, client: AsyncClient, manager: User, executor: User, project: Project
    ) -> None:
        await _create(client, manager, project)
        body = (await client.get("/api/v1/tasks", headers=auth(executor))).json()
        assert body["total"] == 1


class TestTransitions:
    async def test_assignee_walks_a_task_to_done(
        self, client: AsyncClient, manager: User, executor: User, project: Project
    ) -> None:
        task_id = (await _create(client, manager, project, assignee_id=executor.id)).json()[
            "id"
        ]

        started = await client.post(
            f"/api/v1/tasks/{task_id}/transition",
            json={"status": "in_progress"},
            headers=auth(executor),
        )
        assert started.status_code == 200
        assert started.json()["started_at"] is not None

        done = await client.post(
            f"/api/v1/tasks/{task_id}/transition",
            json={"status": "done"},
            headers=auth(executor),
        )
        assert done.status_code == 200
        assert done.json()["done_at"] is not None

    async def test_an_illegal_jump_is_a_409(
        self, client: AsyncClient, manager: User, project: Project
    ) -> None:
        """The case the transition table exists for: a stale card jumping to done."""
        task_id = (await _create(client, manager, project, status="backlog")).json()["id"]
        response = await client.post(
            f"/api/v1/tasks/{task_id}/transition",
            json={"status": "done"},
            headers=auth(manager),
        )
        assert response.status_code == 409

    async def test_reopening_clears_the_completion_time(
        self, client: AsyncClient, manager: User, project: Project
    ) -> None:
        task_id = (await _create(client, manager, project)).json()["id"]
        for target in ("in_progress", "done"):
            await client.post(
                f"/api/v1/tasks/{task_id}/transition",
                json={"status": target},
                headers=auth(manager),
            )
        reopened = await client.post(
            f"/api/v1/tasks/{task_id}/transition",
            json={"status": "todo"},
            headers=auth(manager),
        )
        assert reopened.status_code == 200
        assert reopened.json()["done_at"] is None

    async def test_a_bystander_cannot_move_someone_elses_task(
        self, client: AsyncClient, manager: User, executor: User, project: Project
    ) -> None:
        task_id = (await _create(client, manager, project)).json()["id"]
        response = await client.post(
            f"/api/v1/tasks/{task_id}/transition",
            json={"status": "in_progress"},
            headers=auth(executor),
        )
        assert response.status_code == 403


class TestEditing:
    async def test_executor_cannot_retitle_a_managers_task(
        self, client: AsyncClient, manager: User, executor: User, project: Project
    ) -> None:
        task_id = (await _create(client, manager, project)).json()["id"]
        response = await client.patch(
            f"/api/v1/tasks/{task_id}",
            json={"title": "something else"},
            headers=auth(executor),
        )
        assert response.status_code == 403

    async def test_priority_and_due_changes_are_recorded(
        self, client: AsyncClient, manager: User, project: Project
    ) -> None:
        task_id = (await _create(client, manager, project)).json()["id"]
        due = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        await client.patch(
            f"/api/v1/tasks/{task_id}",
            json={"priority": "high", "due_at": due},
            headers=auth(manager),
        )
        kinds = {
            entry["kind"]
            for entry in (
                await client.get(f"/api/v1/tasks/{task_id}/activity", headers=auth(manager))
            ).json()
        }
        assert {"priority_changed", "due_changed"} <= kinds

    async def test_time_logging_accumulates(
        self, client: AsyncClient, manager: User, executor: User, project: Project
    ) -> None:
        task_id = (await _create(client, manager, project, assignee_id=executor.id)).json()[
            "id"
        ]
        for _ in range(2):
            response = await client.post(
                f"/api/v1/tasks/{task_id}/time", json={"minutes": 30}, headers=auth(executor)
            )
            assert response.status_code == 200
        assert response.json()["spent_minutes"] == 60


class TestFilters:
    async def test_overdue_only_returns_open_past_due_tasks(
        self, client: AsyncClient, manager: User, project: Project, session: AsyncSession
    ) -> None:
        past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        late = (await _create(client, manager, project, title="late", due_at=past)).json()
        await _create(client, manager, project, title="soon", due_at=future)

        # A finished task is not overdue, however late it was.
        closed = (await _create(client, manager, project, title="closed", due_at=past)).json()
        for target in ("in_progress", "done"):
            await client.post(
                f"/api/v1/tasks/{closed['id']}/transition",
                json={"status": target},
                headers=auth(manager),
            )

        body = (await client.get("/api/v1/tasks?overdue=true", headers=auth(manager))).json()
        assert [item["id"] for item in body["items"]] == [late["id"]]

    async def test_search_matches_title_and_description(
        self, client: AsyncClient, manager: User, project: Project
    ) -> None:
        await _create(client, manager, project, title="rotate the webhook secret")
        await _create(client, manager, project, title="unrelated", description="rotate later")
        await _create(client, manager, project, title="nothing to see")

        body = (await client.get("/api/v1/tasks?q=rotate", headers=auth(manager))).json()
        assert body["total"] == 2

    async def test_status_filter_and_paging(
        self, client: AsyncClient, manager: User, project: Project
    ) -> None:
        for index in range(3):
            await _create(client, manager, project, title=f"task {index}")
        body = (
            await client.get("/api/v1/tasks?status=todo&limit=2", headers=auth(manager))
        ).json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert body["limit"] == 2


class TestComments:
    async def test_commenting_is_visible_and_recorded(
        self, client: AsyncClient, manager: User, executor: User, project: Project
    ) -> None:
        task_id = (await _create(client, manager, project, assignee_id=executor.id)).json()[
            "id"
        ]
        posted = await client.post(
            f"/api/v1/tasks/{task_id}/comments",
            json={"body": "blocked on BotFather"},
            headers=auth(executor),
        )
        assert posted.status_code == 201
        assert posted.json()["author"]["id"] == executor.id

        listed = (
            await client.get(f"/api/v1/tasks/{task_id}/comments", headers=auth(manager))
        ).json()
        assert [c["body"] for c in listed] == ["blocked on BotFather"]

    async def test_an_outsider_cannot_comment(
        self, client: AsyncClient, manager: User, outsider: User, project: Project
    ) -> None:
        task_id = (await _create(client, manager, project)).json()["id"]
        response = await client.post(
            f"/api/v1/tasks/{task_id}/comments", json={"body": "hello"}, headers=auth(outsider)
        )
        assert response.status_code == 404
