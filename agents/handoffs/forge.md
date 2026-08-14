Status: review-wanted
Last: Baselines measured + docs/engineering.md + non-blocking ty type-check workflow committed
Next: Peter/integrator to resolve the ty-vs-mypy duplication before either merges
Blockers: ANOTHER WRITER IS EDITING .github/workflows/ci.yml INSIDE .worktrees/forge (see below)

# FORGE handoff

Role: engineering quality and performance. Owns `.github/`,
`docs/engineering.md`, this file; `pyproject.toml` only via a PR that FORGE
does not self-merge.

## Session log — 2026-08-14

**Setup.** Worktree `.worktrees/forge` already existed at `origin/main`
(0 ahead / 0 behind), so no rebase was needed. `uv sync` clean, 17 packages.

**Baseline verification.** At session start `origin/main` was **red**:
`ruff` reported 9 errors (4x E501 in `library/curated/_emit_card.py` (CURATOR)
and 4x E501 + UP017 + F401 in `research/explorations/harbor-021/demos/*`
(RECON)). Per `agents/WORKFLOW.md` line 27 I did not touch another role's
paths. `codex/restore-green-ci` has since merged as `65ef29c` and fixed all
nine.

**Final verification, rebased onto `d0d6760`:**

- `uv run ruff check .` — **All checks passed.**
- `uv run pytest -q` — **49 passed, 0.67 s.**
- `uvx ty@0.0.71 check src/` — 33 diagnostics, none in FORGE-owned files.

## ⚠ Live collision — a second writer is in the FORGE worktree

At 09:32:40 on 2026-08-14, while this session was running,
`.github/workflows/ci.yml` **inside `.worktrees/forge`** was modified by
something that is not this session. This session has never written that file;
its only writes were `typecheck.yml`, `docs/engineering.md`, this handoff,
`agents/ROLES.md`, and the gitignored `runs/_forge/` scratch tree.

This breaks the one-writer-per-tree invariant in `agents/WORKFLOW.md`.

**What I did about it:** nothing destructive. Per the protocol ("on any
conflict: stop, record it, continue with other mission work") I left the edit
in place, **unstaged and uncommitted**, and staged only FORGE-owned paths by
explicit path. Their work is intact in the working tree. Do not `git checkout`
that file without finding out whose it is first.

**Why it matters beyond the invariant:** their edit adds a **mypy** typecheck
step to `ci.yml`, covering six `src/harbor_lab/*.py` modules. FORGE
independently shipped a **ty** typecheck as `typecheck.yml`. The repository
now has two competing type checkers proposed at once. They should not both
merge.

Rough comparison, so the decision is informed rather than arbitrary:

| | ty (`typecheck.yml`, FORGE) | mypy (`ci.yml`, other writer) |
|---|---|---|
| Runtime | 0.18 s warm, whole `src/` | not measured — mypy is typically seconds |
| Version risk | pre-1.0 (0.0.71), **pinned** | mature, but invoked unpinned via `uv run --with mypy` |
| Blocking | no — reports 4 known diagnostics | yes — would turn PRs red immediately |
| Scope | all of `src/` | 6 explicitly listed modules |

My recommendation: pick one. If the goal is a gate that passes today, theirs
needs the same 4-diagnostic problem solved (and `--check-untyped-defs` on
`queue.py`/`database.py` will surface more, in files nobody may edit tonight).
If the goal is visibility now and a gate later, mine is already that. I am not
deleting anyone's work to force the answer.

## Collision watch (resolved)

`codex/restore-green-ci` also rewrote `.github/workflows/ci.yml` — a
FORGE-owned path. It merged mid-session as `65ef29c`. Its version is good work
(SHA-pinned actions, `concurrency` + `cancel-in-progress`, split `lint`/`test`
jobs, a 3.11/3.14 matrix, `uv sync --locked`, `timeout-minutes`), and FORGE
did not touch it — which is exactly why the merge was clean and this branch
rebased with zero conflicts in its own commit.

## Measured findings

Full numbers and the reproduction recipe are in `docs/engineering.md`.

The headline: **`Executor.tick()` issues 2 PostgreSQL round-trips per approved
spec**, from `_spent_today()` and `_consecutive_harness_failures()` called
inside the dispatch loop (`src/harbor_lab/queue.py:438-439`), each opening its
own connection. Measured with production seams wired in:

| N approved specs | queue scan only | with production seams | ratio |
|---|---|---|---|
| 10 | 17.57 ms | 149.53 ms | 8.5x |
| 50 | 40.61 ms | 772.18 ms | 19x |
| 100 | 70.26 ms | 1594.27 ms | 23x |

~15.2 ms of DB overhead per spec; at N=100 roughly 96% of tick wall-clock is
catalog round-trips rather than queue work.

**Not fixed, deliberately.** `queue.py` is hot tonight (mission: "queue and
automation are hot — leave them; note findings instead"). There is also a real
semantic question a fix must answer first: spend *changes* as jobs dispatch
within a tick, so the per-iteration re-read may be intentional for the
$20/day ceiling. The safe optimization is connection reuse, **not** hoisting
the query out of the loop. Recorded here for daytime pickup rather than
guessed at.

## Other observations (no action taken)

- `agents/WORKFLOW.md:45` references `harbor-lab fleet`; that subcommand does
  not exist. Only `scripts/fleet-status.sh` does. Doc/reality drift.
- `fleet-status.sh` takes ~1.2-1.5 s, dominated by subprocess `git`/`gh`
  calls. Fine for human use; would need batching if it ever runs per-tick.
- The worktree venv resolves to Python 3.13 while `pyproject.toml` declares
  `requires-python = ">=3.11"` and the in-flight CI matrix tests 3.11 and
  3.14 — 3.13 itself is never exercised in CI.
