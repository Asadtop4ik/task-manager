# Software Factory

The agent pipeline that turns an owner's Telegram task into a merged, deployed and verified change: the Task Manager plus agent-svc, seen as one conveyor of stations.

## Language

**Software Factory**:
agent-svc plus the Task Manager, seen as one conveyor of Stations from Task to deployed change.
_Avoid_: dark factory, pipeline (when meaning the whole thing)

**Station**:
One step of the factory with a fixed input, output and gate (intake, test-author, builder, Check Gate, reviewer).
_Avoid_: stage, phase, agent (an agent may run a station; it is not the station)

**Worker**:
The Codex CLI process that does a station's model work inside the sandbox; it never holds secrets.
_Avoid_: bot, runner

**Brief**:
The intake station's structured restatement of a Task: title, goal and up to five Acceptance Criteria, with exact values in backticks.
_Avoid_: spec, ticket

**Acceptance Criterion**:
One owner-facing sentence in the Brief that must be true when the Task is done.
_Avoid_: requirement, AC (in prose)

**Acceptance Check**:
An executable test written by the test-author station for an Acceptance Criterion and locked before the builder runs; it needs no owner approval. It lives in the project repo under `tests/acceptance/`, visible to the builder but never editable by it, and stays there after merge as regression.
_Avoid_: acceptance test (ambiguous with owner testing), golden test

**Holdout Check**:
An Acceptance Check kept only by the factory and never shown to the builder, used to catch work that passes the visible checks without meeting the criterion.
_Avoid_: hidden test, secret test

**Check Gate**:
The factory's own run of the locked Acceptance Checks after the builder finishes and before the PR is opened. A PR is opened either way; a failed gate is visible on the card and CI blocks the merge.
_Avoid_: CI (CI re-runs the checks as regression, it is not the gate)

**Fix-up**:
A bounded follow-up builder run in the same workspace after a failed Check Gate, before any PR exists; it is not a Correction.
_Avoid_: retry, correction

**Correction**:
A new builder run on an existing PR, requested by the owner.
_Avoid_: fix-up, revision

**Spec Conflict**:
The builder's declared outcome that a Brief or Acceptance Check cannot or should not be satisfied as written. The run fails with a `SPEC_CONFLICT` reason, and the owner either re-runs it with "follow the spec as written" or edits the Task. It is the builder's only way to dispute a check or a literal.
_Avoid_: blocker, improvement
