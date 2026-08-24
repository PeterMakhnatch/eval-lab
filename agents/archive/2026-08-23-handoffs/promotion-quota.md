Status: done
Last: merged as PR #67 (`8e42f2c`)
Next: none
Blockers: none

# PROMOTION-QUOTA — preserve the provider quota signal through promotion

Branch `role/promotion-quota`, worktree `.worktrees/promotion-quota`.

## What changed

| Path | Change |
|---|---|
| `scripts/promote_codex_bundle.py` | new rule **R4**: a redacted quota sidecar per omitted rollout |
| `tests/test_promotion_quota_sidecar.py` | new; 15 tests, two of them leak tests |
| `docs/quota-accounting.md` | new section "Surviving promotion: the R4 quota sidecar" |
| `research/evidence/runs/canary-*-codex-20260815/` | 9 sidecars added, 3 `PROMOTION.json` updated |

Nothing outside the lease was touched. `src/evallab/quota.py`, `queue.py`,
`cli.py`, `policy/` and `digest.py` are untouched — confirmed by
`git status --porcelain`.

## The defect

`agent/sessions/**/rollout-*.jsonl` carries `payload.rate_limits` — the
provider's own `used_percent`, `window_minutes`, `resets_at`, `credits`,
`plan_type`. Rule R2 omits those files entirely, correctly, because they also
carry unredacted prompts and reasoning blobs. So the quota signal was discarded
at the exact moment evidence became permanent.

Measured before the change, `proven live`:

```
$ uv run python -m evallab.quota research/evidence/runs
  remaining allowance    [unavailable]
  snapshots harvested:   0 [observed]
```

## R4

Beside each omitted rollout, promotion now writes
`<trial>/agent/quota/<rollout-stem>.rate-limits.json` holding only the event
timestamp and a **whitelist** of `payload.rate_limits` scalars — exactly the
fields `evallab.quota` reads. Nothing outside `payload.rate_limits` is read;
each field must match a declared type; an unrecognised key is dropped with only
its *name* recorded; any whitelisted string over 128 bytes becomes a digest
marker. Each sidecar carries the omitted rollout's SHA-256 and byte count, so
it is auditable against the original exactly as R2's omission record is.

Deliberately **not** under `agent/sessions/`: that prefix is the structural
signal for raw model I/O, and `git ls-files` finding nothing under it in
committed evidence must stay a true check. A test asserts this.

The explorer needed no change and already renders it honestly, `proven live`:

> `agent/quota/rollout-….rate-limits.json` was redacted by rule R4: 3308 of
> 54698 original bytes remain; sha256:b7164a79… is the digest of the
> unredacted parent — provenance `withheld`

## The leak test

`test_no_run_of_message_content_reaches_the_sidecar` feeds the parser a rollout
carrying a system prompt, a user prompt, assistant text, a reasoning blob, a
session title and a bearer token, then asserts that **no twelve-character run**
of any of them appears anywhere in the emitted bytes — not one named field.
`test_the_leak_test_can_actually_fail` runs the identical scan against
contaminated bytes and asserts it reports leaks, so a green result is not
vacuous. Six further tests cover unknown top-level and nested fields, an
oversize whitelisted string, a wrongly typed numeric field, `bool`-versus-`int`
confusion, and a non-instant timestamp.

The repository secret scanner rejected the first draft's literal fake token —
working as intended. The token is now assembled at runtime, the same idiom
`tests/test_repository_contract.py` already uses.

## Backfilling the promoted bundles

The three committed bundles reproduce **byte-identically** from the primary
checkout's `runs/` with the pre-change script (`diff -r` clean on all three),
so any post-change difference is attributable solely to R4. Promoted to a
throwaway tree first, then compared file-by-file by SHA-256 before anything was
copied:

- removed: **0**
- pre-existing artifact bytes changed: **0**
- added: **9** sidecars
- `PROMOTION.json`: changed on all three, provably additive — new `R4` rule
  text, new `quota_sidecars` total, updated `promoted_bytes`, three new file
  entries, and a single new `quota_sidecar_path` key on each omitted-rollout
  record. No pre-existing entry lost a key or changed a value.

`source_files`, `promoted_files`, `omitted_files` and `source_bytes` are
unchanged in every bundle: a sidecar is derived from a file already counted, so
counting it as a source file would inflate the totals.

File counts after: 48 / 42 / 39 (from 45 / 39 / 36).

```
$ uv run python scripts/promote_codex_bundle.py --verify
canary-event-summary-codex-20260815: 47 source files recorded, 3 quota sidecars
canary-terminal-bench-html-js-filter-codex-20260815: 41 source files recorded, 3 quota sidecars
canary-transaction-reconciliation-codex-20260815: 38 source files recorded, 3 quota sidecars
verified 126 promoted files across 3 bundles, 0 failures
```

## Can `evallab quota` read it? No — and here is the exact change

`blocked` on a path outside this lease. `evallab.quota._rate_limit_snapshots`
globs `agent/sessions/**/rollout-*.jsonl` only, so promoted evidence still
reports `snapshots harvested: 0` and `headroom.availability = "unavailable"`.
Re-measured after the backfill, `proven live`.

The change needed, in that one function: when a trial yields no rollout
snapshots, read `agent/quota/*.rate-limits.json`, accept documents whose `kind`
is `evallab-rate-limits-sidecar`, and append
`(_parse_instant(entry["timestamp"]), entry["rate_limits"], sidecar_path)` for
each entry in `snapshots`.

It must be a **fallback, not an addition** — a live run has the rollout and a
promoted bundle has the sidecar; reading both would double-count on any tree
holding both. `_model_turns` needs no change: with no rollout it already
returns `None` rather than claiming zero turns.

Prototype-verified against the committed bundles with an empty `runs/`. That
fallback takes the report from 0 snapshots / `unavailable` to:

```
snapshots harvested: 67 [observed]      headroom.availability: observed
limit_id / plan_type   codex / prolite
resets_at              2026-08-20T18:32:49+00:00
counter resolution     1.0 percentage point
no overflow credits: reaching 100% blocks every paid agent until the window resets
```

## Correction to the brief: the 92% reading is still not in the repository

The brief's premise is that backfilling the three promoted bundles makes "the
investigation that produced today's 92% reading" reproducible from a fresh
clone. It does not, and the mission as scoped cannot.

The three promoted `canary-*-codex-20260815` bundles top out at `used_percent`
**70.0** — the 15 August readings. Measured across the primary checkout's
`runs/`, the 92% readings are all in the **unpromoted** 16 August jobs:

| used_percent | timestamp | job |
|---|---|---|
| 92.0 | 2026-08-16T13:57:12Z | `canary-event-summary-codex-20260816` |
| 92.0 | 2026-08-16T13:03:52Z | `canary-terminal-bench-html-js-filter-codex-20260816` |
| 92.0 | 2026-08-16T09:51:12Z | `canary-transaction-reconciliation-codex-20260816` |
| 70.0 | 2026-08-15T07:02:09Z | `canary-event-summary-codex-20260815` (promoted) |

R4 makes the signal survive promotion; it cannot promote a bundle. Preserving
the 92% reading requires promoting those three jobs, which is a Research-lane
admission decision under `docs/analysis-loop.md`, not a mechanism change — so
it was not done here. The command is recorded in `docs/quota-accounting.md`
under "Known gap". `designed`, awaiting a lane decision.

## Two other notes for the Integrator

- `origin/main` is **`0960eea`**, not the `7456ac8` the brief states — one
  commit ahead (a build-plan commit). The worktree was branched from
  `origin/main` as instructed, so it includes it.
- This mission has no row in `agents/missions/ACTIVE.md`; only the Integrator
  edits that board. `agents/CHECKS.md` requires `make premerge`, which this
  round's instruction explicitly excluded — the full suite and `ruff` were run
  instead, and the ty ratchet was not exercised locally. It was exercised on
  GitHub: PR #67 is green on all five checks — `lint`, `test (3.12)`,
  `test (3.14)`, `ty`, `profile`.

## Verification

- `uv run pytest` — **684 passed**, 0 failed. Baseline measured on a stashed
  tree at 669; the 15 new tests account for the difference exactly.
- `uv run ruff check .` — all checks passed.
- `uv run python scripts/promote_codex_bundle.py --verify` — 126 promoted
  files, 3 bundles, **0 failures**.
- Shared catalog unchanged: `72|23` jobs / `trajectory_documents`, confirmed
  before and after via `docker exec eval-lab-postgres-1 psql`.
- No paid agent was executed. No `codex`, no `claude-code`, no cloud sandbox,
  no `launchctl`, no `docker compose`. The primary checkout was read only —
  the source rollouts were read from it, nothing was written to it.
