"""Codex "ops request" trailer: parse, strip, and validate against policy.

Design: `agent-svc-notes/ops-requests-spec.md` section 2, work package WP-C.

Codex asks the owner to change a small, non-secret operational setting (an
env var on an allowlisted project) by ending its final message with one
line, the LAST line, of the exact form::

    AGENT_OPS_REQUESTS: [{"kind":"env_set","key":"ADMIN_TG_IDS", ...}]

Two, independent responsibilities live here:

* `split_trailer` finds that marker line (never trusting its *content* yet)
  and removes EVERY line that starts with the marker prefix from the
  message before that text is ever shown to a human or embedded in a PR
  body -- the raw JSON (env values, however harmless) must never reach a
  public PR, `failure_reason`, or a "Codex izohi" block. This is
  unconditional: it runs even when there is no marker (a no-op), one marker
  (the normal case), or more than one (ambiguous -- every candidate line is
  still stripped, and nothing after them is trusted).

* `validate` turns the one trusted JSON blob (if any) into a list of
  proposal dicts shaped for the `AgentRunCallback.ops_requests` field,
  checking each one against the *trusted* `agent_ops_policy` module (loaded
  by file path from `trusted_dir`, never imported directly here -- this
  module has no import on `scripts/agent_ops_policy.py` at all, exactly
  like `agent_svc.trusted` loads every other trusted script). A request the
  policy denies is still returned (so the owner's card can show *why* it
  was refused), but only after the same basic ASCII charset checks a policy
  *allowed* request must already pass: the backend's `AgentOpsProposal`
  schema enforces the identical `key`/`value` regex, and a single malformed
  row would 422 the *entire* callback, losing every other -- possibly
  perfectly valid -- proposal in the same run. Any structural problem with
  the batch as a whole (not valid JSON, over the byte cap, not a list, more
  than `policy_module.MAX_REQUESTS` objects, or any one object missing/
  adding a key) invalidates the *whole* batch (`validate` returns `[]`):
  only individual charset/policy outcomes are decided per-row.
"""

from __future__ import annotations

import json
from typing import Any

MARKER_PREFIX = "AGENT_OPS_REQUESTS:"
MAX_TRAILER_BYTES = 4096

_REQUEST_FIELDS = frozenset({"kind", "key", "op", "value", "reason"})
_MAX_OPS_NOTE_LEN = 200


def split_trailer(final_message: str | None) -> tuple[str, str | None, str | None]:
    """Strip every `AGENT_OPS_REQUESTS:` line from `final_message`.

    Returns `(summary, raw, note)`:
    * `summary` -- `final_message` with every marker line removed (always;
      even with zero or more than one marker) and surrounding whitespace
      trimmed. Safe to hand to `publish_implement(codex_summary=...)`, a
      `failure_reason`, or any other text a human or a public PR body will
      see.
    * `raw` -- the text after the ONE marker's colon, stripped, only when
      there was exactly one marker line; `None` otherwise (zero markers, or
      more than one -- an ambiguous message is never trusted).
    * `note` -- `"ambiguous"` when more than one marker line was found;
      `None` otherwise.
    """
    if not final_message:
        return "", None, None
    lines = final_message.split("\n")
    marker_indices = [i for i, line in enumerate(lines) if line.startswith(MARKER_PREFIX)]
    if not marker_indices:
        return final_message.strip(), None, None
    kept = [line for i, line in enumerate(lines) if i not in marker_indices]
    summary = "\n".join(kept).strip()
    if len(marker_indices) > 1:
        return summary, None, "ambiguous"
    raw = lines[marker_indices[0]][len(MARKER_PREFIX) :].strip()
    return summary, raw, None


def build_ops_note(trailer_note: str | None, policy_module: Any) -> str | None:
    """The `ops_note` callback field: currently just `split_trailer`'s own
    note (control-stripped, capped to the 200-char `AgentRunCallback`
    limit), sanitized with the same `sanitize_reason` used for the `reason`
    field on every proposal."""
    if not trailer_note:
        return None
    return policy_module.sanitize_reason(trailer_note)[:_MAX_OPS_NOTE_LEN]


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
) -> dict[str, Any] | None:
    key = item["key"]
    value = item["value"]
    # Basic ASCII charset checks that MUST hold before a row is ever sent to
    # the backend, regardless of the policy outcome: `AgentOpsProposal`
    # enforces the identical `KEY_RE`/`VALUE_RE` server-side, and a single
    # bad row would 422 the whole callback (see the module docstring).
    if not policy_module.KEY_RE.fullmatch(key):
        return None
    if not policy_module.VALUE_RE.fullmatch(value) or "://" in value:
        return None

    if allowlist is None:
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
) -> list[dict[str, Any]]:
    """Turn one trusted trailer JSON blob into `AgentRunCallback.ops_requests`
    proposal dicts.

    `allowlist` is the already-loaded `agent_ops_policy.Allowlist`, or `None`
    when the allowlist file itself was missing/invalid for this run (every
    proposal is then denied with `policy_reason="no_allowlist"`, never
    crashing). `policy_module` is the trusted `agent_ops_policy` module
    (`ctx.trusted.agent_ops_policy`), passed in rather than imported so this
    file never has its own import on `scripts/agent_ops_policy.py`.
    """
    if raw is None:
        return []
    items = _parse_batch(raw, policy_module)
    if items is None:
        return []
    proposals: list[dict[str, Any]] = []
    for item in items:
        proposal = _validate_one(
            item,
            project_key=project_key,
            repo_full_name=repo_full_name,
            allowlist=allowlist,
            policy_module=policy_module,
        )
        if proposal is not None:
            proposals.append(proposal)
    return proposals
