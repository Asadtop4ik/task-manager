"""Quick-capture parser: one line of free text -> a task.

The manager types `keto: fix the mini app url !urgent @asad ertaga 18:00` on a
phone. Everything this module extracts is shown back on a confirmation card
before anything is created — quick capture is where a bot usually gets a date
subtly wrong, and a silently mis-parsed deadline is worse than no parsing at all.

Uzbek, Russian and English are all in scope because that is how this team
actually writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

PRIORITY_WORDS: dict[str, str] = {
    "urgent": "urgent",
    "shoshilinch": "urgent",
    "срочно": "urgent",
    "high": "high",
    "muhim": "high",
    "важно": "high",
    "normal": "normal",
    "oddiy": "normal",
    "low": "low",
    "past": "low",
    "низкий": "low",
}

TODAY_WORDS = frozenset({"bugun", "today", "сегодня"})
TOMORROW_WORDS = frozenset({"ertaga", "tomorrow", "завтра"})
# Uzbek has a single word for "the day after tomorrow"; it comes up constantly.
DAY_AFTER_WORDS = frozenset({"indinga", "послезавтра"})

WEEKDAYS: dict[str, int] = {
    "dushanba": 0,
    "monday": 0,
    "понедельник": 0,
    "seshanba": 1,
    "tuesday": 1,
    "вторник": 1,
    "chorshanba": 2,
    "wednesday": 2,
    "среда": 2,
    "payshanba": 3,
    "thursday": 3,
    "четверг": 3,
    "juma": 4,
    "friday": 4,
    "пятница": 4,
    "shanba": 5,
    "saturday": 5,
    "суббота": 5,
    "yakshanba": 6,
    "sunday": 6,
    "воскресенье": 6,
}

# Default time of day for a date given without one. End of the working day, not
# midnight: "ertaga" means "by tomorrow", not "by 00:00 tomorrow".
DEFAULT_DUE_TIME = time(18, 0)

_PROJECT_RE = re.compile(r"^\s*([a-z0-9][a-z0-9-]{1,31})\s*:\s*", re.IGNORECASE)
_HASH_PROJECT_RE = re.compile(r"(?:^|\s)#([a-z0-9][a-z0-9-]{1,31})\b", re.IGNORECASE)
_PRIORITY_RE = re.compile(r"(?:^|\s)!([a-zA-Zа-яА-Я]+)")
_ASSIGNEE_RE = re.compile(r"(?:^|\s)@([a-zA-Z0-9_]{3,32})\b")
_TIME_RE = re.compile(r"(?:^|\s)(\d{1,2})[:.](\d{2})(?=\s|$)")
_DATE_RE = re.compile(r"(?:^|\s)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?=\s|$)")
_IN_DAYS_RE = re.compile(
    r"(?:^|\s)\+(\d{1,3})\s*(?:d|kun|дн|day|days)?(?=\s|$)", re.IGNORECASE
)


@dataclass
class ParsedTask:
    title: str
    project_key: str | None = None
    priority: str | None = None
    assignee_username: str | None = None
    due_at: datetime | None = None
    # What the parser thinks it recognised, for the confirmation card. The user
    # sees this before anything is written.
    notes: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return bool(self.title.strip())


def _strip(text: str, match: re.Match[str]) -> str:
    return (text[: match.start()] + " " + text[match.end() :]).strip()


def _resolve_project(token: str, known_keys: set[str]) -> str | None:
    """Match `keto` to `ketoshop` by prefix, but only when it is unambiguous.

    Two projects starting with the same letters must not silently resolve to
    whichever one sorts first.
    """
    token = token.lower()
    if token in known_keys:
        return token
    candidates = [key for key in known_keys if key.startswith(token)]
    return candidates[0] if len(candidates) == 1 else None


def _next_weekday(today: date, weekday: int) -> date:
    ahead = (weekday - today.weekday()) % 7
    # "juma" said on a Friday means next Friday, not today.
    return today + timedelta(days=ahead or 7)


def parse(
    text: str, *, known_keys: set[str], tz: str = "Asia/Tashkent", now: datetime | None = None
) -> ParsedTask:
    """Pull structure out of one line. Never raises — worst case it is all title."""
    zone = ZoneInfo(tz)
    now = now.astimezone(zone) if now else datetime.now(zone)

    rest = text.strip()
    result = ParsedTask(title="")

    # --- project: "keto: ..." or "#keto" anywhere ---
    match = _PROJECT_RE.search(rest)
    if match:
        key = _resolve_project(match.group(1), known_keys)
        if key:
            result.project_key = key
            rest = _strip(rest, match)
    if result.project_key is None:
        match = _HASH_PROJECT_RE.search(rest)
        if match:
            key = _resolve_project(match.group(1), known_keys)
            if key:
                result.project_key = key
                rest = _strip(rest, match)

    # --- priority: !urgent ---
    match = _PRIORITY_RE.search(rest)
    if match:
        priority = PRIORITY_WORDS.get(match.group(1).lower())
        if priority:
            result.priority = priority
            rest = _strip(rest, match)

    # --- assignee: @username ---
    match = _ASSIGNEE_RE.search(rest)
    if match:
        result.assignee_username = match.group(1)
        rest = _strip(rest, match)

    # --- date before time, because "." means both ---
    # "25.12" is a date and "18.00" is a time, and one regex cannot tell them
    # apart. Trying the date first settles it: a pair that is a real day/month
    # is read as a date, and one that is not (month 00) falls through to time.
    due_date: date | None = None

    match = _DATE_RE.search(rest)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        raw_year = match.group(3)
        year = now.year if raw_year is None else int(raw_year)
        if year < 100:
            year += 2000
        try:
            due_date = date(year, month, day)
        except ValueError:
            # Not a real date (32.13, or 18.00 meaning six o'clock). Leave it in
            # place for the time parser or the title.
            due_date = None
        else:
            rest = _strip(rest, match)
            # A bare day/month that has already passed means next year — nobody
            # files a task due nine months ago.
            if raw_year is None and due_date < now.date():
                due_date = due_date.replace(year=year + 1)

    if due_date is None:
        match = _IN_DAYS_RE.search(rest)
        if match:
            due_date = now.date() + timedelta(days=int(match.group(1)))
            rest = _strip(rest, match)

    if due_date is None:
        words = rest.lower().split()
        for word in words:
            bare = word.strip(".,!?")
            if bare in TODAY_WORDS:
                due_date = now.date()
            elif bare in TOMORROW_WORDS:
                due_date = now.date() + timedelta(days=1)
            elif bare in DAY_AFTER_WORDS:
                due_date = now.date() + timedelta(days=2)
            elif bare in WEEKDAYS:
                due_date = _next_weekday(now.date(), WEEKDAYS[bare])
            else:
                continue
            rest = re.sub(rf"(?:^|\s){re.escape(word)}(?=\s|$)", " ", rest, count=1).strip()
            break

    due_time: time | None = None
    match = _TIME_RE.search(rest)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour < 24 and minute < 60:
            due_time = time(hour, minute)
            rest = _strip(rest, match)

    if due_date is not None or due_time is not None:
        the_date = due_date or now.date()
        the_time = due_time or DEFAULT_DUE_TIME
        due = datetime.combine(the_date, the_time, tzinfo=zone)
        # A bare "18:00" said at 19:00 means tomorrow, not an hour ago.
        if due_date is None and due < now:
            due += timedelta(days=1)
        result.due_at = due

    result.title = re.sub(r"\s{2,}", " ", rest).strip(" -–—:")
    return result
