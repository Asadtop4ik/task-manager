from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.cards import (
    MAX_CARD_CHARS,
    _telegram_length,
    format_due,
    is_overdue,
    keyboard,
    render,
    summary_line,
)

TZ = "Asia/Tashkent"


def make_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": 7,
        "project": {"id": 1, "key": "ketoshop", "name": "Ketoshop", "color": "#10b981"},
        "title": "Fix the Mini App URL",
        "description": None,
        "status": "todo",
        "priority": "urgent",
        "assignee": {"id": 2, "telegram_id": 99, "full_name": "Asadbek"},
        "due_at": None,
        "spent_minutes": 0,
    }
    return task | overrides


def button_texts(task: dict[str, Any]) -> list[str]:
    markup = keyboard(task, public_url="https://tasks.example.uz")
    return [button.text for row in markup.inline_keyboard for button in row]


class TestKeyboard:
    def test_a_todo_task_can_be_started_but_not_finished(self) -> None:
        """Offering a button the transition table will reject invites a dead tap."""
        texts = button_texts(make_task(status="todo"))
        assert any("Boshlash" in t for t in texts)
        assert not any("Bajarildi" in t for t in texts)

    def test_an_in_progress_task_can_be_finished_or_blocked(self) -> None:
        texts = button_texts(make_task(status="in_progress"))
        assert any("Bajarildi" in t for t in texts)
        assert any("To‘xtatish" in t for t in texts)

    def test_a_finished_task_only_offers_reopen(self) -> None:
        texts = button_texts(make_task(status="done"))
        assert any("Qayta ochish" in t for t in texts)
        assert not any("Boshlash" in t or "Izoh" in t for t in texts)

    def test_the_open_button_links_to_the_task(self) -> None:
        markup = keyboard(make_task(), public_url="https://tasks.example.uz")
        urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
        assert urls == ["https://tasks.example.uz/tasks/7"]


class TestRender:
    def test_long_description_stays_within_telegram_limit_and_keeps_html_valid(self) -> None:
        body = render(make_task(description="🧪<&>" * 1500))
        assert _telegram_length(body) <= MAX_CARD_CHARS
        assert "To‘liq tavsif: 🌐 Ochish" in body
        assert "&lt;" in body
        assert "&am" not in body.replace("&amp;", "")

    def test_escapes_html_in_user_text(self) -> None:
        """Titles are user input and the parse mode is HTML."""
        body = render(make_task(title="fix <b>bold</b> & co"))
        assert "&lt;b&gt;" in body and "<b>bold</b>" not in body
        assert "&amp;" in body

    def test_shows_the_project_status_and_assignee(self) -> None:
        body = render(make_task())
        assert "Ketoshop" in body
        assert "Asadbek" in body
        assert "#7" in body

    def test_unassigned_is_stated_not_blank(self) -> None:
        assert "biriktirilmagan" in render(make_task(assignee=None))


class TestDue:
    def test_today_and_tomorrow_are_named(self) -> None:
        soon = datetime.now(UTC) + timedelta(hours=2)
        assert format_due(soon.isoformat(), TZ) is not None

    def test_none_stays_none(self) -> None:
        assert format_due(None) is None

    def test_overdue_only_applies_to_open_tasks(self) -> None:
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        assert is_overdue(make_task(due_at=past, status="todo"))
        # However late it was, a finished task is not overdue.
        assert not is_overdue(make_task(due_at=past, status="done"))

    def test_a_future_deadline_is_not_overdue(self) -> None:
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        assert not is_overdue(make_task(due_at=future))

    @pytest.mark.parametrize("status", ["todo", "in_progress", "blocked"])
    def test_summary_line_marks_overdue(self, status: str) -> None:
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        assert summary_line(make_task(due_at=past, status=status)).startswith("⚠️")
