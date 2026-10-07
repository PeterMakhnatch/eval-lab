---
name: review
description: Perform a requested read-only Eval Lab PR review against current-head CI, changed contracts and acceptance evidence; this is not an extra mandatory author merge gate.
---

# Review

Requested read-only review only. Do not merge. Do not start Harbor or paid models.

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

Read the PR body, then the changed paths. Shared files and money paths
need the actual hunks. Confirm ownership in `agents/OWNERS.md` and the
current assignment cover every written path. Follow current
`agents/WORKFLOW.md` ownership and `agents/CHECKS.md`, not a frozen board
lease. Verify that explicit `make docs` was run before review if
documentation or code symbols were touched, so `docs/INDEX.md` and
`docs/repo-map.md` are fresh.

## 3. Handoff

Read the PR's acceptance and verification record and, when linked by its claim,
the live handoff. `agents/WORKFLOW.md` defines that optional handoff's format.
Missing or stale evidence is unavailable, not an inferred completed review.

## 4. Claimed behaviour is real

Trust verification, not prose:

- named acceptance criteria met with a command, test, or artifact path
- focused tests covering changed behavior recorded for code changes;
  run only explicitly assigned focused verification at the final checkpoint
- explicit `make docs` run, and `python -m evallab.docindex check` /
  `python -m evallab.repomap check` pass cleanly when doc/code topology moved
- a claim without a run, log, or test that would fail on the bug is a draft

The author remains responsible for CI fixes and protected merge when that is
the assignment. Oracle and nop prove the task and harness, not model capability.
Refuse to treat them as evidence of skill.
