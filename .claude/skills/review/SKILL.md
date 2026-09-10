---
name: review
description: >
  Review an eval-lab pull request in one pass: GitHub checks, the diff,
  the mission handoff, and whether claimed behaviour was actually run.
  Use when reviewing a PR, deciding merge readiness, or checking that
  acceptance criteria are evidenced rather than asserted.
---

# Review

Do not merge. Do not start Harbor or paid models.

## 1. Checks

```bash
gh pr checks
gh pr checks <number>
```

Every GitHub check on the current head must be complete and successful.
Local green is not a substitute. Pending is not pass (`gh pr checks`
exits 8). External providers are out of scope; report only the details
URL.

## 2. Diff

```bash
gh pr diff <number>
gh pr view <number>
```
Read the PR body, then the leased paths. Shared files and money paths
need the actual hunks. Confirm the lease in `research/inbox/board.md`
(referenced via `agents/missions/ACTIVE.md`) covers every written path.
Verify that explicit `make docs` was run before review if documentation or
code symbols were touched, so `docs/INDEX.md` and `docs/repo-map.md` are fresh.
## 3. Handoff

Read the PR's acceptance and verification record and, when linked by its claim,
the live handoff. `agents/WORKFLOW.md` defines that optional handoff's format.
Missing or stale evidence is unavailable, not an inferred completed review.

## 4. Claimed behaviour is real

Trust verification, not prose:

- named acceptance criteria met with a command, test, or artifact path
- focused tests covering changed behavior recorded for code changes;
  `uv run pytest` / `uv run ruff check .` run at final premerge checkpoint
- explicit `make docs` run, and `python -m evallab.docindex check` /
  `python -m evallab.repomap check` pass cleanly when doc/code topology moved
- a claim without a run, log, or test that would fail on the bug is a draft

Oracle and nop prove the task and harness, not model capability. Refuse
to treat them as evidence of skill.
