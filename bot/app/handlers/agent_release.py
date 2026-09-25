"""Owner-only Telegram controls for reviewing and merging coding-agent PRs."""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.api import ApiError
from app.callbacks import AgentReleaseAction
from app.handlers.helpers import api_for, editable
from app.states import AgentCorrection

router = Router(name="agent_release")
CARD_LIMIT = 3900
CORRECTION_LIMIT = 2000


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _html_text(value: Any, limit: int) -> str:
    """Escape text without allowing entities to exceed the rendered budget."""
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


def _link(label: str, value: Any) -> str | None:
    url = str(value or "")
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.netloc or len(url) > 300:
        return None
    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'


def _evidence_lines(run: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    ci = run.get("ci_evidence") or run.get("ci") or {}
    if isinstance(ci, dict):
        state = ci.get("state") or run.get("ci_status")
        if state:
            lines.append(f"CI: {_html_text(state, 30)}")
        verified_sha = ci.get("verified_sha") or ci.get("ci_verified_sha")
        if verified_sha:
            lines.append(f"CI SHA: <code>{_html_text(str(verified_sha)[:12], 12)}</code>")
        ci_url = ci.get("url") or ci.get("details_url")
        if ci_url:
            ci_link = _link("CI natijasini ochish", ci_url)
            if ci_link:
                lines.append(ci_link)
        checks = ci.get("checks") or []
        if isinstance(checks, list):
            for check in checks[:4]:
                if not isinstance(check, dict):
                    continue
                name = _html_text(check.get("name") or "Tekshiruv", 55)
                status = _html_text(check.get("state") or "noma’lum", 24)
                details = check.get("details_url")
                evidence_link = _link("dalil", details)
                suffix = f" — {evidence_link}" if evidence_link else ""
                lines.append(f"• {name}: {status}{suffix}")
    elif isinstance(ci, list):
        lines.extend(f"• {_html_text(item, 90)}" for item in ci[:4])
    elif ci:
        lines.append(f"CI: {_html_text(ci, 80)}")

    review = run.get("review") or {}
    if isinstance(review, dict):
        state = review.get("state")
        if state:
            lines.append(f"Review: {_html_text(state, 40)}")
        findings = review.get("findings") or []
        if isinstance(findings, list):
            for finding in findings[:3]:
                if not isinstance(finding, dict):
                    continue
                severity = _html_text(finding.get("severity") or "", 8)
                title = _html_text(
                    finding.get("title") or finding.get("evidence") or "Izoh", 100
                )
                location = ""
                if finding.get("file"):
                    location = f" ({_html_text(finding['file'], 50)}"
                    if finding.get("line"):
                        location += f":{_html_text(finding['line'], 12)}"
                    location += ")"
                lines.append(f"• {severity} {title}{location}")
    return lines


def release_card(run: dict[str, Any], *, detailed: bool = False) -> str:
    """Render trusted structure and HTML-escape every API-provided string."""
    run_id = _html_text(run.get("run_id") or "", 40)
    repo = _html_text(run.get("repo_full_name") or "Agent run", 90)
    sha = str(run.get("head_sha") or "")
    short_sha = _html_text(sha[:12] if sha else "SHA yo‘q", 12)
    actions = run.get("actions") or {}
    merge_available = bool((actions.get("merge") or {}).get("available"))
    review = run.get("review") or {}
    review_state = str(review.get("state") or "").lower() if isinstance(review, dict) else ""
    findings = review.get("findings") or [] if isinstance(review, dict) else []
    ci = run.get("ci_evidence") or run.get("ci") or {}
    ci_state = str(ci.get("state") or "").lower() if isinstance(ci, dict) else ""
    if merge_available:
        status_label = "PR tayyor"
    elif ci_state in {"failure", "failed", "error"}:
        status_label = "CI xato"
    elif findings or review_state in {
        "failed",
        "blocked",
        "changes_requested",
        "issues",
    }:
        status_label = "Tuzatish kerak"
    elif ci_state in {"pending", "queued", "running"} or review_state in {
        "pending",
        "running",
    }:
        status_label = "Tekshiruv kutilmoqda"
    elif str(run.get("status") or "").lower() == "pr_ready":
        status_label = "Tekshiruv shartlari bajarilmagan"
    elif str(run.get("status") or "").lower() in {"failed", "cancelled", "blocked"}:
        status_label = "Agent ishi to‘xtadi"
    else:
        status_label = _short(run.get("status") or "noma’lum", 40)
    summary = _html_text(
        run.get("summary") or "Qisqa xulosa mavjud emas.", 1600 if detailed else 520
    )
    impact = _html_text(
        run.get("impact") or "Ta’sir bo‘yicha ma’lumot yo‘q.", 900 if detailed else 260
    )
    lines = [
        f"<b>Codex PR · {repo}</b>",
        f"Holat: {_html_text(status_label, 40)} · <code>{short_sha}</code>",
    ]
    lines.extend(["", f"<b>Xulosa:</b> {summary}", f"<b>Ta’sir:</b> {impact}"])
    error = run.get("error")
    if error:
        lines.append(f"<b>Sabab:</b> {_html_text(error, 500)}")
    evidence = _evidence_lines(run)
    if evidence:
        lines.extend(["", "<b>CI va review dalillari:</b>", *evidence])
    pr_url = run.get("pr_url")
    if pr_url:
        pr_link = _link("PRni ochish", pr_url)
        if pr_link:
            lines.extend(["", pr_link])
    if detailed:
        review = run.get("review") or {}
        reviewed_sha = review.get("reviewed_head_sha") if isinstance(review, dict) else None
        if reviewed_sha:
            lines.append(
                f"Review qilingan SHA: <code>{_html_text(str(reviewed_sha)[:12], 12)}</code>"
            )
        if run.get("run_id"):
            lines.append(f"Run: <code>{run_id}</code>")
    text = "\n".join(lines)
    if len(text) <= CARD_LIMIT:
        return text
    compact = "\n".join(lines[:5])
    if len(compact) <= CARD_LIMIT:
        return compact
    return "<b>Codex PR</b>\nKarta juda uzun. Batafsil ma’lumotni qayta oching."


def release_keyboard(
    run_id: str, head_sha: str, *, actions: dict[str, Any] | None = None
) -> InlineKeyboardMarkup | None:
    builder = InlineKeyboardBuilder()
    sha12 = head_sha[:12]
    merge_available = actions is None or bool((actions.get("merge") or {}).get("available"))
    correction_available = actions is None or bool(
        (actions.get("correction") or {}).get("available")
    )
    if merge_available:
        builder.button(
            text="Merge va deploy",
            callback_data=AgentReleaseAction(action="merge", run_id=run_id, sha12=sha12),
        )
    if correction_available:
        builder.button(
            text="Tuzatish so‘rash",
            callback_data=AgentReleaseAction(action="correct", run_id=run_id, sha12=sha12),
        )
    builder.button(
        text="Batafsil",
        callback_data=AgentReleaseAction(action="detail", run_id=run_id, sha12=sha12),
    )
    builder.adjust(1)
    return builder.as_markup() if merge_available or correction_available else None


def _action_id(run_id: str, action: str, sha: str, instruction: str = "") -> str:
    # Telegram may deliver the same callback more than once. Stable IDs make
    # repeated clicks idempotent at the API boundary.
    return str(uuid5(UUID(run_id), f"{action}:{sha}:{instruction}"))


async def _load_owner_run(query: CallbackQuery, run_id: str) -> dict[str, Any] | None:
    message = editable(query)
    if message is None or message.chat.type != "private":
        await query.answer(
            "Bu amal faqat egasining shaxsiy chatida ishlaydi.", show_alert=True
        )
        return None
    api = api_for(query)
    try:
        actor = await api.me()
        if not actor.get("is_owner"):
            await query.answer("Bu amal faqat egasi uchun.", show_alert=True)
            return None
        return await api.agent_run(run_id)
    except ApiError as error:
        if error.status == 404:
            text = "Run topilmadi yoki endi mavjud emas."
        elif error.status == 403:
            text = "Bu amal faqat egasi uchun."
        else:
            text = "Run holatini olib bo‘lmadi. Birozdan so‘ng qayta urinib ko‘ring."
        await query.answer(text, show_alert=True)
        return None


async def _refresh(
    query: CallbackQuery, run: dict[str, Any], *, detailed: bool = False
) -> None:
    message = editable(query)
    if message is None:
        return
    run_id = str(run.get("run_id") or "")
    head_sha = str(run.get("head_sha") or "")
    markup = (
        release_keyboard(run_id, head_sha, actions=run.get("actions") or {})
        if len(head_sha) == 40
        else None
    )
    try:
        await message.edit_text(release_card(run, detailed=detailed), reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


@router.callback_query(AgentReleaseAction.filter())
async def release_action(
    query: CallbackQuery,
    callback_data: AgentReleaseAction,
    state: FSMContext,
) -> None:
    run = await _load_owner_run(query, callback_data.run_id)
    if run is None:
        return
    head_sha = str(run.get("head_sha") or "")
    if len(head_sha) != 40 or not head_sha.startswith(callback_data.sha12):
        await query.answer(
            "PR commiti yangilangan. Kartani qayta ochib tekshiring.", show_alert=True
        )
        await _refresh(query, run)
        return

    if callback_data.action == "detail":
        await query.answer()
        await _refresh(query, run, detailed=True)
        return

    actions = run.get("actions") or {}
    if callback_data.action == "merge":
        merge_action = actions.get("merge") or {}
        if not merge_action.get("available"):
            await query.answer(
                "Merge uchun CI va review shartlari bajarilmagan.", show_alert=True
            )
            await _refresh(query, run)
            return
        api = api_for(query)
        try:
            result = await api.merge_agent_run(
                callback_data.run_id,
                expected_head_sha=head_sha,
                action_id=_action_id(callback_data.run_id, "merge", head_sha),
            )
        except ApiError as error:
            await query.answer(
                f"Merge amalga oshmadi: {_short(error.detail, 180)}", show_alert=True
            )
            if error.code == "stale_head":
                await _refresh(query, await api.agent_run(callback_data.run_id))
            return
        await query.answer(
            _short(result.get("message") or "Merge amali qabul qilindi.", 180),
            show_alert=True,
        )
        updated = await api.agent_run(callback_data.run_id)
        await _refresh(query, updated)
        return

    if callback_data.action == "correct":
        correction_action = actions.get("correction") or {}
        if not correction_action.get("available"):
            await query.answer("Hozir tuzatish so‘rovi yuborib bo‘lmaydi.", show_alert=True)
            await _refresh(query, run)
            return
        message = editable(query)
        await state.set_state(AgentCorrection.instruction)
        await state.update_data(
            correction_run_id=callback_data.run_id,
            correction_head_sha=head_sha,
            correction_chat_id=message.chat.id if message else None,
            correction_message_id=message.message_id if message else None,
        )
        await query.answer("Tuzatish matnini yuboring.")
        if message:
            await message.edit_text(
                "Qanday tuzatish kerakligini bitta xabarda yozing. /cancel bekor qiladi.",
                reply_markup=None,
            )
        return

    await query.answer("Noma’lum amal.", show_alert=True)


@router.message(AgentCorrection.instruction, F.text)
async def submit_correction(message: Message, state: FSMContext, bot: Bot) -> None:
    instruction = (message.text or "").strip()
    if instruction.startswith("/"):
        await message.answer("Tuzatish matnini yuboring yoki /cancel bilan bekor qiling.")
        return
    if not instruction:
        await message.answer("Tuzatish matni bo‘sh bo‘lmasin. Qayta yuboring yoki /cancel.")
        return
    if len(instruction) > CORRECTION_LIMIT:
        await message.answer(
            f"Matn {CORRECTION_LIMIT} belgidan oshmasin. Qisqartirib qayta yuboring."
        )
        return
    if message.chat.type != "private" or message.from_user is None:
        await state.clear()
        await message.answer("Tuzatish faqat egasining shaxsiy chatida yuboriladi.")
        return

    data = await state.get_data()
    run_id = str(data.get("correction_run_id") or "")
    expected_sha = str(data.get("correction_head_sha") or "")
    api = api_for(message)
    try:
        actor = await api.me()
        if not actor.get("is_owner"):
            await state.clear()
            await message.answer("Bu amal faqat egasi uchun.")
            return
        run = await api.agent_run(run_id)
        current_sha = str(run.get("head_sha") or "")
        if current_sha != expected_sha:
            await state.clear()
            await message.answer(
                "PR commiti yangilangan. Yangi kartadagi tugmadan qayta boshlang."
            )
            return
        correction_action = (run.get("actions") or {}).get("correction") or {}
        if not correction_action.get("available"):
            await state.clear()
            await message.answer("Run holati o‘zgargan. Tuzatish so‘rovi yuborilmadi.")
            return
        result = await api.request_agent_correction(
            run_id,
            instruction=instruction,
            expected_head_sha=expected_sha,
            action_id=_action_id(run_id, "correction", expected_sha, instruction),
        )
    except ApiError as error:
        if error.code == "stale_head":
            await state.clear()
            try:
                latest = await api.agent_run(run_id)
                card_chat_id = data.get("correction_chat_id")
                card_message_id = data.get("correction_message_id")
                if card_chat_id and card_message_id:
                    markup = release_keyboard(
                        run_id,
                        str(latest.get("head_sha") or ""),
                        actions=latest.get("actions") or {},
                    )
                    await bot.edit_message_text(
                        release_card(latest),
                        chat_id=card_chat_id,
                        message_id=card_message_id,
                        reply_markup=markup,
                    )
            except (ApiError, TelegramBadRequest):
                pass
            await message.answer(
                "PR commiti yangilangan. Yangi kartadan tuzatishni qayta boshlang."
            )
            return
        await message.answer(
            f"Tuzatish so‘rovi yuborilmadi: {escape(_short(error.detail, 300))}"
        )
        return
    await state.clear()
    status = escape(_short(result.get("message") or "Tuzatish so‘rovi qabul qilindi.", 300))
    card_chat_id = data.get("correction_chat_id")
    card_message_id = data.get("correction_message_id")
    if card_chat_id and card_message_id:
        try:
            await bot.edit_message_text(
                f"🛠 {status}\nRun: <code>{escape(run_id)}</code>",
                chat_id=card_chat_id,
                message_id=card_message_id,
                reply_markup=None,
            )
            return
        except TelegramBadRequest:
            pass
    await message.answer(status)
