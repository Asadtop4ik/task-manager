# Agent platform baseline — 2026-09-24

This is a snapshot taken before the six-stage platform rollout. Times below are
GitHub Actions wall-clock times (`createdAt` to `updatedAt`), not Codex execution
time or a promise of future performance. All values came from read-only GitHub
and production checks.

| Project | Last successful deploy run | Workflow time | Production image SHA |
| --- | --- | ---: | --- |
| Task Manager | [36011784842](https://github.com/Asadtop4ik/task-manager/actions/runs/36011784842) | 3m 14s | `b07f7d36ba7ad82b52b65d0aeadbe1b71e4270a9` |
| Qurbot | [36015074215](https://github.com/muradjanov-dev/qurbot/actions/runs/36015074215) | 4m 19s | `01ed148909b5732a1c3d17aaefd06a52f2a04010` |
| Kans Shop | [36015110988](https://github.com/muradjanov-dev/kans-shop/actions/runs/36015110988) | 3m 16s | `48c9d13a739bc9b80b7bb364bbefbcf52f43f2c3` |
| Ketoshop | [36015611083](https://github.com/muradjanov-dev/ketoshop/actions/runs/36015611083) | 1m 04s | `9fc0423f27298918e3277088125f98952e3d9c7f` |

The Task Manager deploy above spent approximately 1m 39s reaching the CI gate,
55s in parallel image builds, and 34s in the deploy job; the rest was job
startup and scheduling. The four production image sets matched these exact
commit tags and their configured container healthchecks were healthy. Task
Manager `/ready` reported PostgreSQL and Redis healthy.

The production database had 10 Task Manager agent runs: 7 deployed, 2 failed,
1 cancelled. Two deployed runs used `fast`, five used PRs. There were no agent
runs for Qurbot, Kans Shop or Ketoshop. The intake table contained one confirmed
draft. Agent-run `created_at` to `finished_at` includes waiting for human review
and merge, so it is not a valid measure of Codex execution time. Future
measurement must record dispatch, runner start, PR ready, merge and deploy as
separate events.

The (since decommissioned) private GitHub Actions runner was online and idle. The server had 4 CPUs and
8 GiB RAM, with about 3.7 GiB available at the time of inspection; the coding
runner is capped at 1 CPU / 2 GiB and the intake service at 0.5 CPU / 768 MiB.
The intake service and external deploy monitor timer were active. Public-agent
and intake feature flags were enabled.

Three `Public project agent task` workflow runs on 2026-09-24 ended in failure
because they used intentionally nonexistent task callbacks for checkout smoke
tests. They did not prove the complete Telegram task → PR → CI → merge → deploy
path. Stage 1 therefore needs one real, low-impact README task in each public
repository before the integration is called verified.

## Stage 1 pilot record

Record each real pilot's Task Manager task ID, GitHub agent run, PR, CI run,
merge commit, deploy run, production image SHA and bot notification here. Use
GitHub timestamps for automated stages and note owner review time separately.

| Project | Task | Agent run | PR | CI | Merge SHA | Deploy | Bot notice | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Qurbot | #18 | [36023913121](https://github.com/Asadtop4ik/task-manager/actions/runs/36023913121) | [#4](https://github.com/muradjanov-dev/qurbot/pull/4) | [36024062515](https://github.com/muradjanov-dev/qurbot/actions/runs/36024062515) passed | `26e82ddeda5381469488698999b5f7028e2e4687` | [36024834743](https://github.com/muradjanov-dev/qurbot/actions/runs/36024834743) passed | Delivered | Task done; both images healthy |
| Kans Shop | #19 | [36024276399](https://github.com/Asadtop4ik/task-manager/actions/runs/36024276399) | [#2](https://github.com/muradjanov-dev/kans-shop/pull/2) | [36024405216](https://github.com/muradjanov-dev/kans-shop/actions/runs/36024405216) passed | `16718babb4dba0b83d925f8b9bb09005e332f53f` | [36024841736](https://github.com/muradjanov-dev/kans-shop/actions/runs/36024841736) passed | Delivered | Task done; both images healthy |
| Ketoshop | #20 | [36024516034](https://github.com/Asadtop4ik/task-manager/actions/runs/36024516034) | [#3](https://github.com/muradjanov-dev/ketoshop/pull/3) | [36024684023](https://github.com/muradjanov-dev/ketoshop/actions/runs/36024684023) passed | `571a10e00025911dd81320a67ce210b3d73d1ff3` | [36024849622](https://github.com/muradjanov-dev/ketoshop/actions/runs/36024849622) passed | Delivered | Task done; image healthy |

For the three real public pilot tasks, the private implementation-and-publish
workflows took 82s (Qurbot), 63s (Kans Shop) and 88s (Ketoshop). Their PR CI
workflows took 233s, 68s and 22s respectively. These are workflow wall times,
not isolated model times. Each PR changed only its requested README file.
The merge-triggered deploy workflows took 293s, 116s and 73s respectively.
Task creation to verified deploy took 13m 11s, 7m 04s and 4m 29s; these
durations include human PR review/merge. All three completion notices were
acknowledged by the bot worker and the task statuses became `done`.

Qurbot spent 233s in PR CI and ran the same CI workflow again inside its deploy
workflow. This repeated full test run is a concrete candidate for Stage 4's
change-sensitive check optimization; no quality gate was removed in Stage 1.
