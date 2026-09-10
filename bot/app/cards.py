"""Rendering a task as a Telegram card.

One card per task, edited in place. The bot stores the message id it sent, so a
status change updates that message instead of stacking a second card into the
chat — a chat with six cards for one task is how a task bot becomes noise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.callbacks import TaskAction
from app.config import settings
from app.texts import PRIORITY_EMOJI, PRIORITY_LABEL, STATUS_EMOJI, STATUS_LABEL

OPEN_STATUSES = {"backlog", "todo", "in_progress", "blocked", "review"}


def format_due(value: str | None, tz: str = settings.timezone) -> str | None:
    """Render a UTC timestamp in the reader's timezone.

    Everything is stored UTC; a deadline shown in UTC to someone in Tashkent is
    five hours wrong, which is exactly the kind of quiet error a deadline must
    not have.
    """
    if not value:
        return None
    moment = datetime.fromisoformat(value).astimezone(ZoneInfo(tz))
    now = datetime.now(UTC).astimezone(ZoneInfo(tz))
    stamp = moment.strftime("%H:%M")

    if moment.date() == now.date():
        return f"bugun {stamp}"
    if (moment.date() - now.date()).days == 1:
        return f"ertaga {stamp}"
    return moment.strftime("%d.%m %H:%M")


def is_overdue(task: dict[str, Any]) -> bool:
    if not task.get("due_at") or task["status"] not in OPEN_STATUSES:
        return False
    return datetime.fromisoformat(task["due_at"]) < datetime.now(UTC)


def render(task: dict[str, Any]) -> str:
    status = task["status"]
    priority = task["priority"]
    project = task["project"]

    head = [
        f"{STATUS_EMOJI.get(status, '•')} <b>{escape(project['name'])}</b>",
        f"{PRIORITY_EMOJI.get(priority, '')} {PRIORITY_LABEL.get(priority, priority)}",
    ]
    due = format_due(task.get("due_at"))
    if due:
        head.append(("⚠️ " if is_overdue(task) else "🕒 ") + due)

    lines = [" · ".join(head), "", f"<b>{escape(task['title'])}</b>"]

    if task.get("description"):
        lines.append(escape(task["description"]))

    footer = [f"#{task['id']}", STATUS_LABEL.get(status, status)]
    assignee = task.get("assignee")
    footer.append(escape(assignee["full_name"]) if assignee else "biriktirilmagan")
    if task.get("spent_minutes"):
        footer.append(f"{task['spent_minutes']} daq")
    lines += ["", "<i>" + " · ".join(footer) + "</i>"]

    return "\n".join(lines)


def keyboard(task: dict[str, Any], public_url: str | None = None) -> InlineKeyboardMarkup:
    """Buttons that reflect where the task actually is.

    Offering "Start" on a finished task, or "Done" on one in the backlog, invites
    a tap the API will only reject — the transition table is the authority and the
    keyboard should agree with it.
    """
    builder = InlineKeyboardBuilder()
    status = task["status"]
    task_id = task["id"]

    if status in {"todo", "blocked"}:
        builder.button(
            text="▶️ Boshlash", callback_data=TaskAction(action="start", task_id=task_id)
        )
    if status in {"in_progress", "review"}:
        builder.button(
            text="✅ Bajarildi", callback_data=TaskAction(action="done", task_id=task_id)
        )
    if status == "in_progress":
        builder.button(
            text="⏸ To‘xtatish", callback_data=TaskAction(action="block", task_id=task_id)
        )
    if status in OPEN_STATUSES:
        builder.button(
            text="💬 Izoh", callback_data=TaskAction(action="comment", task_id=task_id)
        )
        builder.button(
            text="⏰ Keyinga", callback_data=TaskAction(action="snooze", task_id=task_id)
        )
    if status in {"done", "cancelled"}:
        builder.button(
            text="↩️ Qayta ochish", callback_data=TaskAction(action="reopen", task_id=task_id)
        )

    base = public_url or settings.public_url
    if base:
        builder.button(text="🌐 Ochish", url=f"{base.rstrip('/')}/tasks/{task_id}")

    builder.adjust(2)
    return builder.as_markup()


def summary_line(task: dict[str, Any]) -> str:
    """One task on one line, for /my and /today."""
    marker = "⚠️" if is_overdue(task) else STATUS_EMOJI.get(task["status"], "•")
    due = format_due(task.get("due_at"))
    tail = f" — {due}" if due else ""
    return f"{marker} <b>#{task['id']}</b> {escape(task['title'])}{tail}"


def build_card(task: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    return render(task), keyboard(task)


__all__ = ["build_card", "format_due", "is_overdue", "keyboard", "render", "summary_line"]
