# Acceptance Checks are written by a separate station, locked, and kept in the project repo

In a 20-feature end-to-end run, 19 of 20 agent changes deployed with green CI and a clean LLM review, but only 13 met their acceptance criteria. The builder "improved" literals the spec stated exactly, and nothing in the pipeline checked behaviour against the criteria.

We decided:
- A separate test-author station writes executable Acceptance Checks before the builder runs, and they need no owner approval. Checks hidden from the builder lowered legitimate success in the research we reviewed, and requiring owner approval would make every task wait on a person.
- The checks are committed under `tests/acceptance/` in the project repo. The builder can read them but cannot change them: agent-svc splices the pinned copy back over any edit, and the Check Gate runs that pinned copy. Builder-editable tests let agents game the gate.
- The checks stay in the repo after merge as regression tests, even in client repos.

## Consequences

- A builder that believes a check or literal is wrong cannot fix it itself; it must report a Spec Conflict to the owner.
- Running repo code in the gate is a new execution surface. It runs only under `codex sandbox` with networking off and a fail-closed canary, and in milestone 1 only for allowlisted stdlib-only repos.
