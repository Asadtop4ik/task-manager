from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.parsing import parse

TZ = "Asia/Tashkent"
KEYS = {"ketoshop", "qurbot", "kans-shop"}
# A Wednesday, so weekday arithmetic is checkable by hand.
NOW = datetime(2026, 9, 9, 10, 0, tzinfo=ZoneInfo(TZ))


def p(text: str, *, keys: set[str] | None = None, now: datetime = NOW):
    return parse(text, known_keys=keys if keys is not None else KEYS, tz=TZ, now=now)


class TestProject:
    def test_prefix_resolves_by_unique_abbreviation(self) -> None:
        result = p("keto: fix the mini app url")
        assert result.project_key == "ketoshop"
        assert result.title == "fix the mini app url"

    def test_hash_form_anywhere_in_the_line(self) -> None:
        result = p("rotate the webhook secret #qurbot")
        assert result.project_key == "qurbot"
        assert result.title == "rotate the webhook secret"

    def test_an_ambiguous_abbreviation_is_left_alone(self) -> None:
        """Two keys share the prefix, so guessing one would be a coin flip."""
        result = p("k: something", keys={"kans-shop", "ketoshop"})
        assert result.project_key is None

    def test_an_unknown_prefix_stays_in_the_title(self) -> None:
        result = p("note: buy milk")
        assert result.project_key is None
        assert result.title == "note: buy milk"

    def test_exact_key_with_a_hyphen(self) -> None:
        assert p("kans-shop: restock").project_key == "kans-shop"


class TestPriorityAndAssignee:
    @pytest.mark.parametrize(
        ("word", "expected"),
        [
            ("!urgent", "urgent"),
            ("!shoshilinch", "urgent"),
            ("!срочно", "urgent"),
            ("!high", "high"),
            ("!muhim", "high"),
            ("!low", "low"),
        ],
    )
    def test_priority_words(self, word: str, expected: str) -> None:
        result = p(f"fix it {word}")
        assert result.priority == expected
        assert result.title == "fix it"

    def test_unknown_priority_word_is_kept_as_text(self) -> None:
        result = p("fix it !soon")
        assert result.priority is None
        assert "!soon" in result.title

    def test_assignee(self) -> None:
        result = p("@asad fix the url")
        assert result.assignee_username == "asad"
        assert result.title == "fix the url"


class TestDates:
    def test_tomorrow_in_three_languages(self) -> None:
        for word in ("ertaga", "tomorrow", "завтра"):
            due = p(f"{word} deploy").due_at
            assert due is not None
            assert due.date() == NOW.date().replace(day=10)
            assert (due.hour, due.minute) == (18, 0), "a bare date means end of day"

    def test_day_after_tomorrow(self) -> None:
        due = p("indinga deploy").due_at
        assert due is not None and due.day == 11

    def test_explicit_time_today(self) -> None:
        due = p("deploy 15:30").due_at
        assert due is not None
        assert (due.date(), due.hour, due.minute) == (NOW.date(), 15, 30)

    def test_a_time_already_past_means_tomorrow(self) -> None:
        """Said at 10:00, "08:00" cannot mean two hours ago."""
        due = p("standup 08:00").due_at
        assert due is not None and due.day == NOW.day + 1

    def test_date_and_time_together(self) -> None:
        due = p("25.12 09:00 yearly report").due_at
        assert due is not None
        assert (due.month, due.day, due.hour) == (12, 25, 9)
        assert p("25.12 09:00 yearly report").title == "yearly report"

    def test_a_past_bare_date_rolls_to_next_year(self) -> None:
        due = p("01.03 plan").due_at
        assert due is not None
        assert (due.year, due.month, due.day) == (2027, 3, 1)

    def test_explicit_year_is_respected(self) -> None:
        due = p("01.03.2026 plan").due_at
        assert due is not None and due.year == 2026

    def test_weekday_never_resolves_to_today(self) -> None:
        # NOW is a Wednesday; "chorshanba" must mean the next one.
        due = p("chorshanba review").due_at
        assert due is not None and due.date() == NOW.date().replace(day=16)

    def test_relative_days(self) -> None:
        due = p("+3 audit").due_at
        assert due is not None and due.day == 12

    def test_an_impossible_date_is_ignored(self) -> None:
        result = p("32.13 nonsense")
        assert result.due_at is None
        assert "32.13" in result.title


class TestWholeLines:
    def test_the_readme_example(self) -> None:
        result = p("keto: fix mini app url !urgent @asad ertaga 18:00")
        assert result.project_key == "ketoshop"
        assert result.priority == "urgent"
        assert result.assignee_username == "asad"
        assert result.due_at is not None
        assert (result.due_at.day, result.due_at.hour) == (10, 18)
        assert result.title == "fix mini app url"

    def test_a_plain_sentence_is_all_title(self) -> None:
        result = p("just write something down")
        assert result.title == "just write something down"
        assert (result.project_key, result.priority, result.due_at) == (None, None, None)

    def test_an_empty_line_is_not_usable(self) -> None:
        assert not p("   ").is_usable

    def test_a_line_of_only_metadata_is_not_usable(self) -> None:
        """Nothing left to call the task, so the bot must ask rather than create."""
        assert not p("keto: !urgent @asad ertaga").is_usable


class TestDotDisambiguation:
    """`.` separates both dates and times, and one regex cannot tell them apart.

    The rule is date-first: a pair that is a real day/month reads as a date, and
    one that is not falls through to being a time.
    """

    def test_a_valid_day_month_is_a_date(self) -> None:
        due = p("25.12 report").due_at
        assert due is not None and (due.month, due.day) == (12, 25)

    def test_a_pair_that_is_no_date_is_a_time(self) -> None:
        # Month 00 does not exist, so 18.00 is six in the evening.
        due = p("deploy 18.00").due_at
        assert due is not None
        assert (due.date(), due.hour, due.minute) == (NOW.date(), 18, 0)

    def test_colon_is_always_a_time(self) -> None:
        due = p("standup 11:30").due_at
        assert due is not None and (due.hour, due.minute) == (11, 30)
