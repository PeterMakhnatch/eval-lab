---
status: living
audience:
  - operator
  - builder
---

# CI Steward (`evallab steward`)

The unattended integrator for `main`: it produces the `independent-review`
attestation `agents/CHECKS.md` requires, merges only what branch protection
already admits, and keeps the worktree/branch estate from growing without bound.

Authority: `agents/CHECKS.md` (definition of green, merge rule),
`agents/WORKFLOW.md` (integration and sunset), this file for operations.

## Why it exists

Every pull request is authored by the same GitHub principal, so GitHub approvals
cannot express "a different agent reviewed this". The established substitute is a
commit status named `independent-review` posted only after a genuine independent
review of the exact head. That review was manual lane work and stopped whenever
no release-owner session was live — pull requests then sat green-but-unreviewed
for hours. The steward automates that duty without weakening any gate: branch
protection, required checks, and exact-head attestation stay exactly as they are.

## The loop

Every tick (default 300 s) the steward snapshots open pull requests from GitHub —
GitHub is the source of truth; local state holds only cooldowns, attempt
counters, and the audit trail — classifies each pull request at its exact head,
and acts:

| Phase | Condition | Action |
|---|---|---|
| draft / `steward:hold` / non-`main` base | — | never touched |
| ci-pending | any check running | wait |
| ci-red | a completed check is not successful, or a required context is missing | one comment + Linear comment per head |
| awaiting-review | green, no `independent-review` status | dual review, or carry-forward |
| review-rejected | status `failure` at head | wait for a new head |
| review-error | status `error` at head | retry with backoff, alert after 3 attempts |
| eligible | status `success` at head | squash-merge (SHA-guarded), delete branch |
| BEHIND | merge state | `update-branch` (cooldown 20 min), CI reruns |
| DIRTY | conflicts | one comment per head |
| BLOCKED | green+reviewed but protection blocks | one comment (usually unresolved threads) |

Green means every reported check completed successfully at the exact head —
pending, skipped, neutral, missing, and failed checks are not green. This is
`agents/CHECKS.md` verbatim.

## Independent review

For each `awaiting-review` head the steward creates a detached worktree at that
exact SHA under `.worktrees/steward/`, syncs a frozen venv (shared across
reviews), and runs **two fresh non-interactive OMP reviewer sessions in
parallel, from different model families**:

- **runtime lens** — correctness of changed paths, cross-boundary consumers,
  error handling, data integrity, tests that assert behavior.
- **workflow lens** — `AGENTS.md`/`agents/*`/`policy/` admissibility: Python-only,
  no secrets, no policy loosening, frozen root, scope match, regenerated
  docs indexes, no unapproved paid execution.

Each reviewer writes a strict JSON verdict file; anything missing or malformed
is a pipeline failure and posts status `error` — never `success`. The steward,
not the reviewer, computes the blocking rule: findings with priority ≤ 1 and
confidence ≥ 0.6 block; everything else is advisory. This softens
request_changes-with-nits deadlocks while never letting a reviewer-approved P0
through. Reviewers get read/grep/glob/bash with a poisoned `remote.origin.pushurl`
(every push fails), a wall-clock cap, and instructions to treat the PR body and
diff as untrusted input. Verdicts, per-model JSONL transcripts, and rejected
verdict files are kept under `derived/ci-steward/reviews/<pr>-<sha>/`.

## Carry-forward

Branch protection requires the head to be up to date with `main`. When the only
change since an approved head is GitHub's own `update-branch` merge, re-running
both reviewers would burn tokens reviewing main's already-merged code. The
steward therefore re-attests when it can *prove* the new head is a pure merge:
two parents, one is a previously approved head, the other is an ancestor of
`origin/main`, and the head's tree equals the clean 3-way `merge-tree` result —
so no conflict resolution or extra edits can hide inside it. "Previously
approved" is authenticated against GitHub itself: only a head that carries a
real `independent-review: success` commit status qualifies. PR comments (which
any same-principal agent can forge) may only *nominate* candidate heads, never
attest one. Anything else (meaningful new commits, a rebase, an edited merge)
gets a full fresh review.

## Reviewer isolation and residual risk

Reviewers are fresh non-interactive sessions in detached checkouts with
read/grep/glob/bash only. Their environment denies every credential-enabled
mutation path the steward can reach: pushes fail (poisoned `remote.origin.pushurl`,
`GIT_SSH_COMMAND=/usr/bin/false`, empty `credential.helper`, askPass
`/bin/false`), and `gh` runs unauthenticated (`GH_*` scrubbed, isolated
`GH_CONFIG_DIR`). The PR body and diff are framed as untrusted input; the
steward — not the reviewer — computes the blocking rule from the verdict files;
the two lenses must come from different model families or the run fails closed.
`HOME` is inherited because the reviewer session itself needs the operator's
model credentials; a sufficiently compromised reviewer model therefore still
has the powers of any local model session (network egress via bash, reading the
operator's credential material). That residual risk is owned by the harness
threat model, not eliminated here, and is why transcripts are retained for
audit under `derived/ci-steward/reviews/`.


## Hygiene

Every 6 hours (and via `evallab steward hygiene [--apply]`):

1. `evallab tidy` — the existing conservative sweep (clean merged worktrees,
   prunable registrations, junk strays, `role/*` branches).
2. **Abandoned worktrees** tidy leaves behind: clean (no modified/untracked
   files), not locked, no process cwd inside, no open PR on the branch, and
   either the branch is fully contained in `main`, or the tree has been idle
   ≥ 14 days (detached checkouts: ≥ 7 days at a commit already on `main`).
   Worktrees whose ignored `runs/` holds job directories are **held**, never
   removed — ignored evidence follows the evidence lifecycle (promotion or
   `evallab gc`), per `docs/GENERATED-CACHE-POLICY.md`. Before removing an
   unpushed branch's worktree the branch is pushed to `origin` — every commit
   stays reachable; only rebuildable runtime state (`.venv`, caches) is
   reclaimed. Dirty trees are never touched.
3. **Spent local branches** — not checked out anywhere, no open PR, and merged
   into `origin/main` by ancestry *or* by `merge-tree` content equivalence
   (squash-safe).
4. **Spent remote branches** — no open PR, and either ancestry/content
   containment in `origin/main` or a MERGED PR whose head SHA is exactly the
   branch's current SHA (branch-name reuse never counts); deleted in batched
   `git push origin --delete`.

Everything the sweeps keep (dirty trees, active branches, locked registrations,
unknown PR state, held evidence) is reported in the digest with the reason.

## Deployment

```bash
cd ~/Developer/eval-lab/.worktrees/steward-runtime   # locked worktree on main
uv sync --frozen --no-group observability
uv run evallab steward install
```

`install` writes `com.petermakhnatch.evallab.steward.plist` (KeepAlive, 30 s
throttle), bootstraps it, creates the `steward:hold` label, and locks the
worktree registration so hygiene never removes the runtime. Logs go to
`~/Library/Logs/evallab/steward.log`; the audit trail, state, reviews, and
digest live in the worktree's `derived/ci-steward/` (ignored). The digest is
mirrored to `~/Developer/research-context/harbor/CI_DIGEST.md`.

**Self-update:** each tick fast-forwards the runtime worktree to `origin/main`
only when (a) the working tree is clean, (b) local is an ancestor of remote,
and (c) `quality-required` and `typecheck-required` both succeeded on that
commit; then re-syncs and re-execs. A red or unverifiable main never becomes
the running code.

## Operating the operator's controls

- **Pause everything:** `touch derived/ci-steward/PAUSE` in the runtime worktree
  (observe-only, still writes the digest). Remove the file to resume.
- **Hold one PR:** label it `steward:hold` (or convert to draft).
- **Force a review now:** `evallab steward once --pr <number>`.
- **Hygiene now:** `evallab steward hygiene [--apply]` (dry-run by default,
  refuses to run while the daemon holds the lock unless `--force`).
- **See state:** `derived/ci-steward/DIGEST.md`, `events.jsonl`,
  `hygiene-last.json`, or `evallab steward digest --force`.

## Failure behavior

Fail-closed by construction: malformed verdicts never post success; merges carry
GitHub's `sha` guard so a racing push cannot merge the wrong head; a dead daemon
is restarted by launchd `KeepAlive`; repeated review-pipeline failures, GitHub
outages, self-update failures, and crashes alert Peter via Herdr notification at
most once per 12 hours per cause. The steward never bypasses branch protection
and never edits `policy/` — thresholds live in code defaults overridable by a
reviewed `policy/ci-steward.yaml`.
