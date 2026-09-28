"""Codex "ops request" trailer: parse, strip, and validate against policy.

Design: `agent-svc-notes/ops-requests-spec.md` section 2, work package WP-C.

Codex asks the owner to change a small, non-secret operational setting (an
env var on an allowlisted project) by ending its final message with one
line, the LAST line, of the exact form::

    AGENT_OPS_REQUESTS: [{"kind":"env_set","key":"ADMIN_TG_IDS", ...}]

Two, independent responsibilities live here:

* `split_trailer` finds that marker line and removes it -- and every line
  that merely LOOKS like an attempt at it -- from the message before that
  text is ever shown to a human or embedded in a PR body: the raw JSON (env
  values, however harmless) must never reach a public PR, `failure_reason`,
  or a "Codex izohi" block. Two separate notions of "marker line" are used
  on purpose: a STRICT one (the exact literal prefix at column 0) decides
  what is ever trusted enough to parse as real JSON, and a much looser FUZZY
  one (Unicode-normalized, casefolded, substring match) decides what gets
  scrubbed from the summary -- a near-miss (leading whitespace, a bullet, a
  lowercase variant, a zero-width character smuggled in front of it, ...)
  is never parsed as a real request, but it is still swept out of the
  summary rather than left to leak.

* `validate` turns the one trusted JSON blob (if any) into a list of
  proposal dicts shaped for the `AgentRunCallback.ops_requests` field,
  checking each one against the *trusted* `agent_ops_policy` module (loaded
  by file path from `trusted_dir`, never imported directly here -- this
  module has no import on `scripts/agent_ops_policy.py` at all, exactly
  like `agent_svc.trusted` loads every other trusted script). A request the
  policy denies is still returned (so the owner's card can show *why* it
  was refused), but only after the same basic checks a policy *allowed*
  request must already pass -- `kind == "env_set"`, `op` one of the three
  known operations, and the ASCII key/value charset: the backend's
  `AgentOpsProposal` schema enforces all of these as Literal/regex fields
  server-side, and a single malformed row would 422 the *entire* callback,
  losing every other -- possibly perfectly valid -- proposal in the same
  run. Any structural problem with the batch as a whole (not valid JSON,
  over the byte cap, not a list, more than `policy_module.MAX_REQUESTS`
  objects, or any one object missing/adding a key) invalidates the *whole*
  batch; only individual charset/policy outcomes are decided per-row.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

MARKER_PREFIX = "AGENT_OPS_REQUESTS:"
MAX_TRAILER_BYTES = 4096

# The one kind this feature supports in v1 (spec section 1: `kind String(16)
# CHECK ='env_set'`). Not sourced from `policy_module` -- the trusted policy
# module has no single exported constant for it (it is inlined in
# `validate_request`'s own `bad_kind` check) -- so it is named here instead.
_ALLOWED_KIND = "env_set"

_REQUEST_FIELDS = frozenset({"kind", "key", "op", "value", "reason"})
_MAX_OPS_NOTE_LEN = 200

# Category "Cf" (Unicode "Format"): zero-width joiners/non-joiners, the
# byte-order mark used as a leading zero-width no-break space, soft hyphen,
# bidi control characters, ... -- invisible characters a hostile or merely
# mangled message could smuggle in front of the marker text to dodge a
# literal substring check. Stripped (after NFKC normalization, which also
# folds width/compatibility variants of the same letters) before the fuzzy
# "does this line even look like the marker" test below.
_STRIP_CATEGORIES = frozenset({"Cf"})
_FUZZY_NEEDLE = "agent_ops_requests"


def _fuzzy_marker_key(line: str) -> str:
    """Normalize `line` for the loose "looks like the marker" test only --
    never used to decide what gets PARSED as JSON, only what gets SCRUBBED
    from the summary. NFKC normalization, then drop every Unicode format
    character, then casefold."""
    normalized = unicodedata.normalize("NFKC", line)
    visible = "".join(
        ch for ch in normalized if unicodedata.category(ch) not in _STRIP_CATEGORIES
    )
    return visible.casefold()


def split_trailer(final_message: str | None) -> tuple[str, str | None, str | None]:
    """Strip the `AGENT_OPS_REQUESTS:` trailer -- and every line that merely
    resembles it -- from `final_message`.

    Returns `(summary, raw, note)`:
    * `summary` -- `final_message` with every marker-shaped line removed
      (always; even with zero or more than one strict marker) and
      surrounding whitespace trimmed. Safe to hand to
      `publish_implement(codex_summary=...)`, a `failure_reason`, or any
      other text a human or a public PR body will see. Uses
      `str.splitlines()` (not a plain `"\\n"` split), so a message using
      `\\r`, `\\r\\n`, or a Unicode line separator (U+2028/U+2029) still has
      its marker line correctly isolated rather than silently fused into a
      longer line that no longer starts with the marker at all.
    * `raw` -- the text after the ONE strict marker's colon (exact literal
      `AGENT_OPS_REQUESTS:` at column 0), stripped, only when there was
      exactly one such line; `None` otherwise (zero, more than one -- an
      ambiguous message is never trusted -- or the text after the colon was
      empty, e.g. the JSON was written on the following line instead: that
      continuation line is still scrubbed from the summary below, just
      never treated as the trusted raw text).
    * `note` -- `"ambiguous"` when more than one strict marker line was
      found; `None` otherwise.
    """
    if not final_message:
        return "", None, None
    lines = final_message.splitlines()

    strict_indices = [i for i, line in enumerate(lines) if line.startswith(MARKER_PREFIX)]
    fuzzy_indices = {
        i for i, line in enumerate(lines) if _FUZZY_NEEDLE in _fuzzy_marker_key(line)
    }

    # A marker line with nothing (or only whitespace) after the colon is
    # still marker-shaped -- catches "JSON on the next line instead"
    # without ever trusting that continuation as the raw text to parse.
    remove_indices = set(fuzzy_indices)
    for index in fuzzy_indices:
        following = index + 1
        if following < len(lines) and lines[following].lstrip().startswith("["):
            remove_indices.add(following)

    kept = [line for i, line in enumerate(lines) if i not in remove_indices]
    summary = "\n".join(kept).strip()

    if not strict_indices:
        return summary, None, None
    if len(strict_indices) > 1:
        return summary, None, "ambiguous"
    raw = lines[strict_indices[0]][len(MARKER_PREFIX) :].strip()
    return summary, raw or None, None


def build_ops_note(
    *,
    trailer_note: str | None = None,
    drop_note: str | None = None,
    policy_module: Any,
) -> str | None:
    """The `ops_note` callback field: `trailer_note` (currently only
    `split_trailer`'s own `"ambiguous"`) takes priority when set -- it means
    `raw` was never even handed to `validate`, so `drop_note` (which
    describes what happened while parsing `raw`) is never set at the same
    time as `trailer_note`. Either way, sanitized with the same
    `sanitize_reason` used for the `reason` field on every proposal and
    capped to the 200-char `AgentRunCallback` limit."""
    note = trailer_note or drop_note
    if not note:
        return None
    return policy_module.sanitize_reason(note)[:_MAX_OPS_NOTE_LEN]


def _parse_batch(raw: str, policy_module: Any) -> list[dict[str, str]] | None:
    """Structural validation of the whole batch. `None` means "invalid as a
    whole" (bad JSON, oversize, not a list, too many objects, or any one
    object with the wrong shape) -- the caller must treat this exactly like
    "no trailer at all", never guess at a partial reading of it."""
    if len(raw.encode("utf-8")) > MAX_TRAILER_BYTES:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list) or len(parsed) > policy_module.MAX_REQUESTS:
        return None
    items: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict) or set(item) != _REQUEST_FIELDS:
            return None
        if not all(isinstance(item[field], str) for field in _REQUEST_FIELDS):
            return None
        items.append(item)
    return items


def _validate_one(
    item: dict[str, str],
    *,
    project_key: str | None,
    repo_full_name: str,
    allowlist: Any | None,
    policy_module: Any,
    enabled: bool,
) -> dict[str, Any] | None:
    # `kind`/`op` MUST be one of the exact values the backend's
    # `AgentOpsProposal` schema accepts (Literal fields) before a row is
    # ever sent, for the identical reason the charset checks below exist: a
    # single row with, say, `op: "delete_everything"` would 422 the whole
    # callback, silently losing every other proposal (and the PR/failure
    # report itself) in the same run. `policy_module.validate_request`
    # checks `kind`/`op` too, but only ever for an ALLOWED row -- several of
    # its own denial paths (project not allowlisted, hard-denied project,
    # repo mismatch) return before ever looking at `kind`/`op`, so a denied
    # row's `kind`/`op` must be validated here independently, unconditionally.
    if item["kind"] != _ALLOWED_KIND or item["op"] not in policy_module.OPS:
        return None

    key = item["key"]
    value = item["value"]
    # Basic ASCII charset checks that MUST hold before a row is ever sent to
    # the backend, regardless of the policy outcome: `AgentOpsProposal`
    # enforces the identical `KEY_RE`/`VALUE_RE` server-side.
    if not policy_module.KEY_RE.fullmatch(key):
        return None
    if not policy_module.VALUE_RE.fullmatch(value) or "://" in value:
        return None

    if not enabled:
        # The ops lane itself is off (`settings.ops_lane_enabled=False`):
        # treated exactly like "no allowlist" for policy purposes, but with
        # its own code so the owner card (and any operator reading the
        # logs) can tell "feature is off" apart from "allowlist file is
        # missing/invalid" -- see agentsvc-notes and P3-5.
        allowed, reason_code = False, "ops_disabled"
    elif allowlist is None:
        allowed, reason_code = False, "no_allowlist"
    else:
        allowed, reason_code = policy_module.validate_request(
            item,
            allowlist,
            project_key=project_key or "",
            repo_full_name=repo_full_name,
        )

    restart_services: tuple[str, ...] = ()
    if allowed:
        assert allowlist is not None and project_key is not None
        restart_services = allowlist.projects[project_key].services

    return {
        "kind": item["kind"],
        "key": key,
        "op": item["op"],
        "value": value,
        "reason": policy_module.sanitize_reason(item["reason"]),
        "policy": "allowed" if allowed else "denied",
        "policy_reason": reason_code,
        "restart_services": list(restart_services),
    }


def validate(
    raw: str | None,
    *,
    project_key: str | None,
    repo_full_name: str,
    allowlist: Any | None,
    policy_module: Any,
    enabled: bool = True,
) -> tuple[list[dict[str, Any]], str | None]:
    """Turn one trusted trailer JSON blob into `AgentRunCallback.ops_requests`
    proposal dicts.

    `allowlist` is the already-loaded `agent_ops_policy.Allowlist`, or `None`
    when the allowlist file itself was missing/invalid for this run (every
    proposal is then denied with `policy_reason="no_allowlist"`, never
    crashing). `enabled=False` (`settings.ops_lane_enabled` is off) is
    treated the same way regardless of `allowlist` -- no proposal is ever
    `allowed` -- but with its own `policy_reason="ops_disabled"`, so a
    denied row's reason distinguishes "feature is off" from "allowlist is
    missing/invalid". `policy_module` is the trusted `agent_ops_policy`
    module (`ctx.trusted.agent_ops_policy`), passed in rather than imported
    so this file never has its own import on `scripts/agent_ops_policy.py`.

    Returns `(proposals, drop_note)`. `drop_note` is `"invalid_trailer"` when
    `raw` itself failed the whole-batch structural check (so `proposals` is
    always `[]`), `f"dropped_{n}"` when `n` >= 1 individual rows failed a
    per-row check (kind/op/charset) and were silently omitted, or `None`
    when nothing was dropped (including when `raw` is `None`, i.e. no
    trailer was present at all) -- see `build_ops_note`, which combines this
    with `split_trailer`'s own note into the one `ops_note` callback field.
    """
    if raw is None:
        return [], None
    items = _parse_batch(raw, policy_module)
    if items is None:
        return [], "invalid_trailer"
    proposals: list[dict[str, Any]] = []
    dropped = 0
    for item in items:
        proposal = _validate_one(
            item,
            project_key=project_key,
            repo_full_name=repo_full_name,
            allowlist=allowlist,
            policy_module=policy_module,
            enabled=enabled,
        )
        if proposal is not None:
            proposals.append(proposal)
        else:
            dropped += 1
    return proposals, (f"dropped_{dropped}" if dropped else None)
