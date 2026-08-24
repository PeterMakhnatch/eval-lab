Status: done
Last: merged as PR #62 (`827bd1c`)
Next: none
Blockers: none

# OPERATOR — derived-root visibility and digest readability

Worktree `.worktrees/paths-digest`, branch `role/paths-digest`, from `origin/main`
`fa11f18`. Leased and touched: `src/evallab/paths.py`, `src/evallab/digest.py`,
`tests/test_paths.py`, `tests/test_unattended.py`, `docs/operations.md`, this
file. Nothing else, in particular not `cli.py`, `queue.py`, `explorer.py`,
`dashboard/`, or `policy/`.

## F-13 — a worktree's derived root now says whose it is

**Decision: keep the sharing, remove the silence.** The derived Parquet store is
a rebuildable projection of the single PostgreSQL catalog that every worktree
shares. A per-worktree derived root would not just waste disk — it would
disagree with the catalog on day one, and the two-store invariant
(`_assert_both_stores`, doctor's `catalog-parquet` line) would report every one
of the catalog's 72 jobs as unprojected in every fresh worktree. Sharing is
right; resolving into another checkout without saying so is not.

`src/evallab/paths.py` now separates the two concerns:

- `resolve_derived_root(...) -> DerivedRootResolution` is pure. It reports
  `path`, `origin`, `invoking_root`, the `base_root` the answer was resolved
  against, and `implicit` — whether the cross-checkout hop was named by anyone.
  `describe()` is the operator-facing line; `is_foreign` is the predicate.
- `derived_root_from_environment(...)` keeps its old signature and return type,
  so all fourteen callsites in `cli.py`, `queue.py`, `gc.py`, `status.py`,
  `automation.py`, `smoke.py`, and `dashboard/app.py` are untouched. It adds one
  stderr notice when the resolution was foreign *and* implicit, deduplicated per
  invoking tree and resolved root. A `notify` seam lets tests capture instead of
  print (CHECKS.md deterministic-test rule).

Quiet by design: `--derived-dir`/`explicit` and an absolute
`EVALLAB_DERIVED_ROOT` are deliberate operator choices, not surprises. Noisy by
design: the default, and a *relative* `EVALLAB_DERIVED_ROOT`, both of which
resolve against the primary checkout.

**Proven live** from this worktree:

```
$ uv run evallab status
evallab: derived root /Users/petermakhnatch/Developer/eval-lab/derived/parquet
belongs to /Users/petermakhnatch/Developer/eval-lab, not to this checkout
/Users/petermakhnatch/Developer/eval-lab/.worktrees/paths-digest; set
EVALLAB_DERIVED_ROOT to an absolute path to choose another.
  [observed] parquet — /Users/petermakhnatch/Developer/eval-lab/derived/parquet

$ EVALLAB_DERIVED_ROOT=/Users/petermakhnatch/Developer/eval-lab/derived/parquet \
    uv run evallab status
  [observed] parquet — /Users/petermakhnatch/Developer/eval-lab/derived/parquet
```

Same path both times; the first is inherited, the second is chosen, and now you
can tell which. That is exactly the reproduction in the mission brief, with the
surprise removed.

Test: `tests/test_paths.py::test_linked_worktree_never_resolves_a_foreign_derived_root_silently`
accepts either acceptable outcome — a worktree-local root, or a foreign root
that was announced with the owning checkout named — and fails on the third,
silent case. Mutation-checked: with `DerivedRootResolution.notice` stubbed to
return `None`, it fails.

## Digest — lab self-tests are summarised, never dropped

**The rule: the attribution the runs already carry.** `evallab smoke` names
every job it creates `smoke-<agent>-<token>` (`src/evallab/smoke.py:166`) and
writes it into the reserved scratch directory `runs/_smoke/`
(`src/evallab/smoke.py:167`). A job name beginning `smoke-` is a self-test and
nothing else in the catalog is — verified against the live catalog, where all 49
`runs/_smoke/*` jobs carry the prefix and none of the ten non-smoke
`oracle`/`nop` control jobs (`checkpoint-oracle-20260814`,
`event-summary-oracle-evidence`, `brief07-transaction-oracle`, …) come close to
matching it.

Four properties keep it honest:

1. Clean self-tests collapse into one bullet per (task, agent) with the trial
   count, the observed reward range, and the cohort's most recent job name —
   countable, and still traceable back to a run directory.
2. A self-test that raised an exception stays a **full row**. A broken harness
   is signal, and summarising it away would be the exact failure mode the brief
   warns against.
3. Every other run is always listed. `oracle` and `nop` controls used as
   evidence are structurally unreachable by this filter.
4. Spend and the exception taxonomy are still computed over *all* trials. The
   filter changes what is readable, not what is reported.

Rendered result:

```
- 49 self-test trials — local-lab/event-summary / oracle, reward 1, 0 exceptions (latest: smoke-oracle-ng0h6qg6d75e)
```

**Before/after over the committed `digests/2026-08-16.md` data.** The committed
file is 264 lines: 219 lines of renderer output plus a 45-line `<!-- fleet:start -->`
block appended afterwards by `append_fleet_section`, which this change does not
touch. Re-rendering the same catalog day and the same `queue/events.jsonl`
through `DigestRenderer`:

| render | lines | `smoke-oracle-*` rows |
|---|---:|---:|
| filter off (reproduces the committed body) | 219 | 49 |
| filter on | 174 | 0 |

45 lines removed net: 49 rows out, 4 lines of rule statement and summary in.
The filter-off render reproduces the committed digest byte-for-byte through the
`Cost and failures` and `Queue` sections; it differs only in the run-corpus byte
count (the measurement used a scratch `repo_root` with no `runs/`) and in two
`tick_deferred` events appended to the live queue after the digest was
committed. Measurement harness was throwaway, under gitignored
`runs/_measure/`, and has been removed.

Tests: `test_digest_summarises_smoke_noise_and_never_hides_a_real_control`
(a `smoke-oracle-*` trial and an `event-summary-oracle-evidence` trial, both
`oracle`, both reward 1 — exactly one leaves the table) and
`test_digest_keeps_a_failed_smoke_run_visible`. Both mutation-checked: with
`is_lab_self_test` stubbed to `False`, both fail.

One consequence worth knowing: `evallab smoke` proves itself end-to-end by
asserting its own job name appears in a digest it renders
(`src/evallab/smoke.py:273`). Naming each cohort's latest job in the summary
keeps that proof valid without an edit to `smoke.py`, which is not leased here.
It was the right design anyway — a summary that names none of its members
cannot be followed back to a run.

## Other digest signal problems — reported, not fixed

Measured on the committed `digests/2026-08-16.md`.

1. **`Queue events` is 48/85 rows of `tick_deferred | no_approved_specs`** — 48
   of the digest's 264 lines, more than the smoke rows this PR removed. It is a
   half-hourly heartbeat, not an event: it says the scheduler woke up and found
   nothing to do. Same defect class, same file, and it would collapse to one
   line ("scheduler ticked 48 times with no approved specs; longest gap N"). I
   left it alone because the brief scoped this mission to the trial tables.
   **Recommended as the next single-line change to `digest.py`.**
2. **`Canary drift` prints six rows for three canaries with no day column.**
   Rows 1–3 report `insufficient history, n=0`; rows 4–6 report `n=3` for the
   same three task/agent pairs. They are two different days concatenated
   (`self._drift_loader(period_date) + self._drift_loader(report_date)`,
   `digest.py`), but a reader sees three canaries contradicting themselves. A
   `day` column fixes it.
3. **`Completed trials` shows three byte-identical rows per canary job.** Those
   are the three attempts `policy/canary-suite.yaml` declares, but no attempt
   number is rendered, so they read as a duplication bug. Nine of the fifteen
   remaining table rows are these indistinguishable triples.
4. **`Fleet` is 45 lines describing a fleet that no longer exists.** Its Roles
   table is 24 rows of the superseded role registry — `ADAPTER`, `CURATOR`,
   `RECON` at `unknown`, `ANALYST` blocked on "PR #1", `PIPELINE` blocked on a
   `cli.py` conflict — all from the pre-mission era. It is the single largest
   block in the digest and the most confidently wrong. Owned by
   `append_fleet_section` in `src/evallab/researchers.py`, outside this lease.
5. **`Evidence and calibration` hardcodes `Judge calibration: not available
   until brief 09`** as a literal string. It will read the same after brief 09
   ships.
6. **`Cost and failures` is dishonest about what was consumed, and this mission
   deliberately did not touch it.** It reports `Recorded spend: $1.8378 / $20.00
   daily ceiling`, and the Fleet funnel adds `Combined observed/attributed:
   $8.8378 / $20.00`. No dollar figure in this repository corresponds to
   anything actually consumed: Codex authenticates from `~/.codex/auth.json`
   (`src/evallab/runner.py:410`), i.e. the Sponsor's ChatGPT subscription, so the
   binding constraint is subscription quota and the gate measures a currency
   nobody is billed in. Every green reading against that ceiling is green about
   the wrong quantity. **A sibling mission owns the accounting; this section must
   be corrected there, not here.** I confirmed only that the filter in this PR
   leaves the reported figure unchanged, because spend is still summed over all
   trials including self-tests.

## Requested follow-up outside this lease

`DerivedRootResolution.describe()` exists and is unused. The natural consumer is
`evallab status` / `evallab doctor`, which should print the derived root's owner
on its own line rather than relying on a stderr notice an operator may have
scrolled past. That is `src/evallab/status.py` and `src/evallab/cli.py` —
`cli.py` is leased exclusively to GateAuthorization — so the Integrator should
sequence it after both PRs land. One line, no new API needed.

## Verification

- `uv run pytest` — 570 passed.
- `uv run ruff check .` — All checks passed.
- Live: `uv run evallab status` from this worktree, with and without an absolute
  `EVALLAB_DERIVED_ROOT`, shown above.
- No paid agent executed; no `codex` or `claude-code` invocation; no scheduled
  job touched; no write to the primary checkout.
- Shared catalog read-only and unchanged: `72` jobs, `23` trajectory_documents
  before and after (`docker exec eval-lab-postgres-1 psql -U evallab -d evallab`).
