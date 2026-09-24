# First 20 real agent tasks

The owner-only board link **Agent tezligi** reads
`GET /api/v1/agent-runs/metrics`. The default pilot starts at
`2026-09-24T18:00:00Z`, after the three reusable starters were accepted.
An owner may pass an ISO 8601 `since` query parameter to inspect another period.
The report stays marked incomplete until the first 20 distinct task IDs have
terminal agent runs. This does not generate artificial tasks to fill the sample.

The API records four new milestones on each run: runner started, PR ready,
external PR merged, and verified deploy. From these it reports queue time,
agent-to-PR time, human PR wait and task-to-production time. A retry counts as
one task in the 20-task sample, but every attempt contributes to error and token
totals. The report shows p50 and p90 using nearest-rank percentiles, with a
sample count beside each number.

Task Manager PR merges currently do not send a separate merge callback, so
**human-review time is measured only for public-project PRs**. The metric shows
its sample count instead of inventing a value. GitHub-hosted CI minutes and
actual rollback events are checked from Actions runs at the 20-task audit;
subscription tokens stored on runs are usage counts, not a dollar invoice.

At 20 completed tasks, compare this report with
[the pre-rollout baseline](AGENT_PLATFORM_BASELINE.md), inspect the associated
GitHub Actions jobs and owner review time, then decide one improvement. If the
p90 runner queue is repeatedly above five minutes, evaluate a second isolated
runner. Do not place another coding agent directly on public-repo PR runners.
