from datetime import UTC, datetime
from html import escape

import httpx
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from arq import cron
from arq.connections import RedisSettings

from app.cards import build_card
from app.config import settings
from app.handlers.agent_intake import notification_message
from app.handlers.agent_ops import combine_keyboards, ops_count_line, ops_keyboard, ops_lines
from app.handlers.agent_release import release_card, release_keyboard
from app.handlers.project_discussion import discussion_keyboard
from app.loader import create_bot
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


def _html_text(value: object, limit: int) -> str:
    output: list[str] = []
    used = 0
    for char in " ".join(str(value or "").split()):
        escaped = escape(char)
        if used + len(escaped) > limit:
            output.append("…")
            break
        output.append(escaped)
        used += len(escaped)
    return "".join(output)


def agent_result_card(notice: dict[str, object]) -> str:
    """A concise, factual status card. PR-ready requires exact-head CI success."""
    task_id = notice["task_id"]
    project = str(notice.get("repo_full_name") or "").split("/")[-1]
    title = notice.get("title") or "Vazifa"
    status = notice["status"]
    lines = [
        f"🤖 #{task_id} · {_html_text(project, 120)}",
        f"Vazifa: {_html_text(title, 500)}",
    ]
    if status == "pr_ready":
        if notice.get("mode") == "fast":
            lines.append("Holat: !fast himoyalangan o‘zgarish PRga o‘tdi; CI yashil.")
        else:
            lines.append("Holat: ✅ PR tayyor. Oxirgi commit CI’dan o‘tdi; review kutilmoqda.")
        lines.append(f"PR: {_html_text(notice.get('pr_url') or '—', 300)}")
        if notice.get("ci_url"):
            lines.append(f"CI: {_html_text(notice['ci_url'], 300)}")
    elif status == "pr_opened":
        lines.append(
            "Holat: CI xato; PR tuzatilmoqda."
            if notice.get("ci_status") == "failure"
            else "Holat: yangi commit uchun CI tekshirilmoqda."
        )
        lines.append(f"PR: {_html_text(notice.get('pr_url') or '—', 300)}")
        if notice.get("ci_url"):
            lines.append(f"CI: {_html_text(notice['ci_url'], 300)}")
    elif status == "merged":
        lines.append("Holat: PR birlashtirildi; production deploy tekshirilmoqda.")
        lines.append(f"PR: {_html_text(notice.get('pr_url') or '—', 300)}")
    elif status == "deployed":
        sha = str(notice.get("deployed_sha") or "")
        lines.append("Holat: ✅ Production’da, tekshiruv va image SHA mos.")
        if sha:
            lines.append(f"Commit: {sha[:12]}")
        lines.append(f"Deploy: {_html_text(notice.get('github_run_url') or '—', 300)}")
    elif status == "ops_pending":
        lines.append(
            "Holat: ⏳ Kod o‘zgarishi kerak emas; muhit sozlamalari egasi tasdig‘ini kutmoqda."
        )
    elif status == "ops_applied":
        lines.append("Holat: ✅ Muhit sozlamalari qo‘llandi.")
    else:
        lines.append("Holat: ⚠️ Agent ishi to‘xtadi.")
        lines.append(f"Sabab: {_html_text(notice.get('error') or 'noma’lum', 1200)}")
        if notice.get("github_run_url"):
            lines.append(f"Jarayon: {_html_text(notice['github_run_url'], 300)}")
    # The legacy/task-origin card is the one place ops details are NOT shown —
    # only ever a count, and only ever here (owner cards get the full block).
    count_line = ops_count_line(notice)
    if count_line:
        lines.append(count_line)
    return "\n".join(lines)


async def _upsert_agent_message(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> int:
    """Edit only an ID belonging to this chat; send a new message otherwise."""
    if message_id is not None:
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
            return message_id
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).lower():
                return message_id
    sent = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    return sent.message_id


async def ping(ctx: dict[str, object]) -> str:
    """Round-trip check for the queue itself.

    Enqueue it and wait for the result to prove the whole path — Redis, the
    worker process, the job serialiser — is alive, which a container healthcheck
    on the process alone cannot tell you.
    """
    return datetime.now(UTC).isoformat()


async def notify_agent_runs(ctx: dict[str, object]) -> None:
    """Send durable PR/failure notices; an unacknowledged notice retries within 10s."""
    headers = {"X-Agent-Worker-Token": settings.service_token}
    base = f"{settings.api_base_url.rstrip('/')}/api/v1/agent-runs"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{base}/notifications", headers=headers)
        response.raise_for_status()
        notices = response.json()
        # Owner cards use escaped HTML for compact evidence links and untrusted
        # review text. Legacy status notices remain plain text.
        bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        try:
            for notice in notices:
                run_id = notice["run_id"]

                # Preserve the task-origin card and its message ID. This may
                # be a group chat and is never reused for the owner's private card.
                legacy_chat_id = notice.get("chat_id")
                legacy_message_id = notice.get("telegram_message_id")
                if legacy_chat_id:
                    try:
                        legacy_message_id = await _upsert_agent_message(
                            bot,
                            chat_id=legacy_chat_id,
                            message_id=legacy_message_id,
                            text=agent_result_card(notice),
                            reply_markup=None,
                        )
                    except (TelegramAPIError, OSError) as error:
                        log.error(
                            "agent_notice_failed",
                            run_id=run_id,
                            error=type(error).__name__,
                        )
                    else:
                        try:
                            ack = await client.post(
                                f"{base}/{run_id}/notified",
                                headers=headers,
                                json={"message_id": legacy_message_id},
                            )
                            ack.raise_for_status()
                        except httpx.HTTPError as error:
                            log.error(
                                "agent_notice_ack_failed",
                                run_id=run_id,
                                error=type(error).__name__,
                            )
                else:
                    try:
                        ack = await client.post(
                            f"{base}/{run_id}/notified",
                            headers=headers,
                            json={"message_id": None},
                        )
                        ack.raise_for_status()
                    except httpx.HTTPError as error:
                        log.error(
                            "agent_notice_ack_failed",
                            run_id=run_id,
                            error=type(error).__name__,
                        )

                # Owner controls have a distinct persisted message/chat pair.
                # Never apply the legacy task-origin message ID to this chat.
                owner_chat_id = notice.get("owner_chat_id")
                owner_notice_chat_id = notice.get("owner_notice_chat_id")
                owner_message_id = notice.get("owner_notice_message_id")
                if owner_chat_id:
                    owner_head_sha = str(notice.get("head_sha") or "")
                    owner_text = (
                        release_card(notice)
                        if len(owner_head_sha) == 40
                        else agent_result_card(notice)
                    )
                    # Full key/op/value detail is owner-only: never on the
                    # legacy task-origin card built above (that one only ever
                    # gets a count, via agent_result_card -> ops_count_line).
                    ops_block = ops_lines(notice)
                    if ops_block:
                        owner_text = "\n".join([owner_text, *ops_block])
                    owner_markup = None
                    if len(owner_head_sha) == 40:
                        actions = notice.get("actions") or {}
                        if not notice.get("owner_controls_available"):
                            actions = {}
                        owner_markup = release_keyboard(
                            str(run_id), owner_head_sha, actions=actions
                        )
                        if notice.get("ops_controls_available"):
                            owner_markup = combine_keyboards(
                                owner_markup,
                                ops_keyboard(notice.get("ops_requests")),
                            )
                    elif notice.get("ops_controls_available"):
                        owner_markup = ops_keyboard(notice.get("ops_requests"))
                    # An ID is reusable only alongside the same owner chat ID.
                    reusable_id = (
                        owner_message_id if owner_notice_chat_id == owner_chat_id else None
                    )
                    try:
                        owner_message_id = await _upsert_agent_message(
                            bot,
                            chat_id=owner_chat_id,
                            message_id=reusable_id,
                            text=owner_text,
                            reply_markup=owner_markup,
                        )
                    except (TelegramAPIError, OSError) as error:
                        log.error(
                            "owner_agent_notice_failed",
                            run_id=run_id,
                            error=type(error).__name__,
                        )
                    else:
                        try:
                            ack = await client.post(
                                f"{base}/{run_id}/owner-notified",
                                headers=headers,
                                json={"message_id": owner_message_id},
                            )
                            ack.raise_for_status()
                        except httpx.HTTPError as error:
                            log.error(
                                "owner_agent_notice_ack_failed",
                                run_id=run_id,
                                error=type(error).__name__,
                            )
        finally:
            await bot.session.close()


async def notify_agent_intakes(ctx: dict[str, object]) -> None:
    """Deliver intake questions, summaries and failures, then ack their revision."""
    if not settings.agent_intake_enabled:
        return
    headers = {"X-Agent-Worker-Token": settings.service_token}
    base = f"{settings.api_base_url.rstrip('/')}/api/v1/agent-intakes"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{base}/notifications", headers=headers)
        response.raise_for_status()
        bot = create_bot()
        try:
            for notice in response.json():
                chat_id = notice.get("chat_id")
                if not chat_id:
                    log.warning("agent_intake_notice_without_chat", intake_id=notice["id"])
                    continue
                text, markup = notification_message(notice)
                try:
                    sent = await bot.send_message(chat_id, text, reply_markup=markup)
                except (TelegramAPIError, OSError) as exc:
                    log.error(
                        "agent_intake_notice_failed",
                        intake_id=notice["id"],
                        error=type(exc).__name__,
                    )
                    continue
                ack = await client.post(
                    f"{base}/{notice['id']}/notified",
                    headers=headers,
                    json={"message_id": sent.message_id, "revision": notice["revision"]},
                )
                ack.raise_for_status()
        finally:
            await bot.session.close()


async def notify_project_discussions(ctx: dict[str, object]) -> None:
    """Deliver one answer per conversation revision, then acknowledge it."""
    if not settings.agent_intake_enabled:
        return
    headers = {"X-Agent-Worker-Token": settings.service_token}
    base = f"{settings.api_base_url.rstrip('/')}/api/v1/project-discussions"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{base}/notifications", headers=headers)
        response.raise_for_status()
        bot = create_bot()
        try:
            for notice in response.json():
                if notice["status"] == "idle":
                    plain = (
                        str(notice.get("response") or "").replace("**", "").replace("`", "")
                    )
                    text = "💬 " + escape(plain[:3500])
                    markup = discussion_keyboard(notice["id"])
                else:
                    text = "⚠️ " + escape(str(notice.get("error") or "Suhbat xatosi")[:500])
                    markup = None
                try:
                    await bot.send_message(notice["chat_id"], text, reply_markup=markup)
                except (TelegramAPIError, OSError) as exc:
                    log.error(
                        "discussion_notice_failed",
                        discussion_id=notice["id"],
                        error=type(exc).__name__,
                    )
                    continue
                ack = await client.post(
                    f"{base}/{notice['id']}/notified",
                    headers=headers,
                    params={"revision": notice["revision"]},
                )
                ack.raise_for_status()
        finally:
            await bot.session.close()


async def sync_deleted_task_cards(ctx: dict[str, object]) -> None:
    """Reflect delete/restore on old Telegram cards without blocking the web API."""
    headers = {"X-Agent-Worker-Token": settings.service_token}
    base = f"{settings.api_base_url.rstrip('/')}/api/v1/tasks/card-sync"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{base}/pending", headers=headers)
        response.raise_for_status()
        bot = create_bot()
        try:
            for notice in response.json():
                task = notice["task"]
                if notice["kind"] == "deleted":
                    text = f"🗑 Vazifa #{task['id']} o‘chirildi."
                    markup = None
                else:
                    text, markup = build_card(task)
                try:
                    await bot.edit_message_text(
                        chat_id=notice["chat_id"],
                        message_id=notice["message_id"],
                        text=text,
                        reply_markup=markup,
                    )
                except (TelegramBadRequest, TelegramForbiddenError) as exc:
                    # Missing/old cards cannot be repaired; keep the API's audit
                    # record and stop retrying this one permanently.
                    log.warning(
                        "task_card_sync_unavailable",
                        event_id=notice["event_id"],
                        error=str(exc),
                    )
                except (TelegramAPIError, OSError) as exc:
                    log.error(
                        "task_card_sync_failed", event_id=notice["event_id"], error=str(exc)
                    )
                    continue
                ack = await client.post(
                    f"{base}/{notice['event_id']}/notified", headers=headers
                )
                ack.raise_for_status()
        finally:
            await bot.session.close()


async def startup(ctx: dict[str, object]) -> None:
    configure_logging()
    log.info("worker_starting")


async def shutdown(ctx: dict[str, object]) -> None:
    log.info("worker_stopped")


class WorkerSettings:
    """arq entrypoint.

    Only `ping` for now — reminder and digest jobs land in milestone 5. arq
    refuses to start with an empty function list, and the process exists from the
    first deploy so the stack file and the deploy path are exercised early rather
    than bolted on later.
    """

    functions = [  # noqa: RUF012
        ping,
        notify_agent_runs,
        notify_agent_intakes,
        notify_project_discussions,
        sync_deleted_task_cards,
    ]
    cron_jobs = [  # noqa: RUF012
        cron(notify_agent_runs, minute=set(range(60)), second=set(range(0, 60, 10))),
        cron(notify_agent_intakes, minute=set(range(60)), second=set(range(0, 60, 10))),
        cron(notify_project_discussions, minute=set(range(60)), second=set(range(0, 60, 10))),
        cron(sync_deleted_task_cards, minute=set(range(60))),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
