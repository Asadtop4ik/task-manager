from datetime import UTC, datetime

import httpx
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from arq import cron
from arq.connections import RedisSettings

from app.cards import build_card
from app.config import settings
from app.loader import create_bot
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


async def ping(ctx: dict[str, object]) -> str:
    """Round-trip check for the queue itself.

    Enqueue it and wait for the result to prove the whole path — Redis, the
    worker process, the job serialiser — is alive, which a container healthcheck
    on the process alone cannot tell you.
    """
    return datetime.now(UTC).isoformat()


async def notify_agent_runs(ctx: dict[str, object]) -> None:
    """Send durable PR/failure notices; an unacknowledged notice retries next minute."""
    headers = {"X-Agent-Worker-Token": settings.service_token}
    base = f"{settings.api_base_url.rstrip('/')}/api/v1/agent-runs"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{base}/notifications", headers=headers)
        response.raise_for_status()
        notices = response.json()
        bot = create_bot()
        try:
            for notice in notices:
                chat_id = notice["chat_id"]
                if chat_id:
                    if notice["status"] == "pr_ready":
                        text = f"🤖 #{notice['task_id']} uchun PR tayyor: {notice['pr_url']}"
                    elif notice["status"] == "deployed":
                        text = (
                            f"✅ #{notice['task_id']} serverga chiqdi. "
                            f"Deploy: {notice['github_run_url']}"
                        )
                    else:
                        text = (
                            f"⚠️ #{notice['task_id']} Codex ishi to‘xtadi. "
                            f"Sabab: {(notice['error'] or 'nomaʼlum')[:500]}. "
                            f"Jarayon: {notice['github_run_url']}"
                        )
                    try:
                        await bot.send_message(chat_id, text)
                    except (TelegramAPIError, OSError) as exc:
                        log.error(
                            "agent_notice_failed",
                            run_id=notice["run_id"],
                            error=type(exc).__name__,
                        )
                        continue
                ack = await client.post(f"{base}/{notice['run_id']}/notified", headers=headers)
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
                except TelegramBadRequest as exc:
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

    functions = [ping, notify_agent_runs, sync_deleted_task_cards]  # noqa: RUF012
    cron_jobs = [  # noqa: RUF012
        cron(notify_agent_runs, minute=set(range(60))),
        cron(sync_deleted_task_cards, minute=set(range(60))),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
