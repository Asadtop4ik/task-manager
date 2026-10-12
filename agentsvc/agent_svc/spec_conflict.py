"""Codex "spec conflict" trailer: parse and strip.

When the locked acceptance criteria (or an explicit literal in the request) are
wrong or impossible, the builder/fix-up must NOT "improve" them. Instead it ends
its final message with exactly one line::

    AGENT_SPEC_CONFLICT: {"criterion": 3, "reason": "short explanation"}

`split_spec_conflict` is modelled on `ops_requests.split_trailer` and runs
BEFORE it, on the raw final message:

* a STRICT marker line (exact literal prefix at column 0, a JSON object with
  exactly the keys `criterion` (int or null) and `reason` (non-empty str, at
  most 800 chars, longer is truncated), located in the last 5 non-empty lines
  of the message) is the only thing ever trusted as a conflict. More than one strict marker line is
  ambiguous and never trusted;
* every line that merely LOOKS like the marker (Unicode-normalised, casefolded
  substring match, plus an immediately following JSON-looking line) is removed
  from the returned text either way, so the marker JSON can never leak into a
  PR body, a failure reason or a Telegram message.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from .ops_requests import _fuzzy_marker_key

MARKER_PREFIX = "AGENT_SPEC_CONFLICT:"
MAX_REASON_CHARS = 800
MAX_CRITERION = 1000
_TAIL_NON_EMPTY_LINES = 5
_FUZZY_NEEDLE = "agent_spec_conflict"
_FIELDS = frozenset({"criterion", "reason"})


@dataclass(frozen=True)
class SpecConflict:
    criterion: int | None
    reason: str


def _parse(raw: str) -> SpecConflict | None:
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != _FIELDS:
        return None
    criterion = data["criterion"]
    reason = data["reason"]
    if criterion is not None and (
        isinstance(criterion, bool)
        or not isinstance(criterion, int)
        or not 0 < criterion <= MAX_CRITERION
    ):
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    # An over-long reason is truncated, not rejected: the conflict is real.
    return SpecConflict(criterion=criterion, reason=" ".join(reason.split())[:MAX_REASON_CHARS])


def split_spec_conflict(final_message: str | None) -> tuple[str, SpecConflict | None]:
    """Return `(text_without_marker_lines, conflict_or_None)`.

    When any marker-shaped line exists the returned text has every one of them
    removed (even when no conflict is trusted) and is whitespace-trimmed; a
    message without any is returned unchanged."""
    if not final_message:
        return "", None
    lines = final_message.splitlines()

    fuzzy = {i for i, line in enumerate(lines) if _FUZZY_NEEDLE in _fuzzy_marker_key(line)}
    if not fuzzy:
        # Nothing marker-shaped: hand the message back untouched.
        return final_message, None
    remove = set(fuzzy)
    for index in fuzzy:
        following = index + 1
        if following < len(lines) and lines[following].lstrip().startswith("{"):
            remove.add(following)
    text = "\n".join(line for i, line in enumerate(lines) if i not in remove).strip()

    non_empty = [i for i, line in enumerate(lines) if line.strip()]
    tail = set(non_empty[-_TAIL_NON_EMPTY_LINES:])
    strict = [i for i, line in enumerate(lines) if line.startswith(MARKER_PREFIX)]
    if len(strict) != 1 or strict[0] not in tail:
        return text, None
    raw = lines[strict[0]][len(MARKER_PREFIX) :].strip()
    return text, _parse(raw) if raw else None


def conflict_error(
    conflict: SpecConflict, redact: Callable[[str], str] | None = None
) -> str:
    """The failure text for the Task Manager card: `SPEC_CONFLICT (mezon N): <reason>`
    (`mezon` = Uzbek for "criterion"); at most 1000 characters in total."""
    reason = conflict.reason
    if redact is not None:
        reason = redact(reason)
    where = f"mezon {conflict.criterion}" if conflict.criterion is not None else "mezon ?"
    return f"SPEC_CONFLICT ({where}): {reason}"[:1000]
