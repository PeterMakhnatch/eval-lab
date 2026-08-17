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
need the actual hunks. Confirm the lease in `agents/missions/ACTIVE.md`
covers every written path.

## 3. Handoff

`agents/handoffs/<role>.md`. First four lines must be `Status:`,
`Last:`, `Next:`, `Blockers:`. `Status: review-wanted` is the only
state that asks for this skill. A stale header is unknown — investigate.

## 4. Claimed behaviour is real

Trust verification, not prose:

- named acceptance criteria met with a command, test, or artifact path
- `uv run pytest` / `uv run ruff check .` recorded for code changes
- `python -m evallab.docindex check` / `python -m evallab.repomap check`
  when those artifacts moved
- a claim without a run, log, or test that would fail on the bug is a
  draft

Oracle and nop prove the task and harness, not model capability. Refuse
to treat them as evidence of skill.
