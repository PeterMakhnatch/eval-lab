Status: done
Last: merged as PR #72 (`02f8d89`)
Next: none
Blockers: none

## Scope

Worktree `.worktrees/perf-test-sound`, branch `role/perf-test-sound`, granted by
the integrator. Branched from `8c9072f`, rebased onto `origin/main` `28d0cf0`
(clean, no conflicts) because main moved during the work and its new commits
touch `src/evallab/results.py`, which the harness imports — validating against a
stale base would have proved nothing about the merge result.

Wrote exactly the leased paths:

- `scripts/profile/harness.py`
- `tests/test_profile_harness.py`
- `agents/handoffs/perf-test-sound.md` (this file)

`scripts/profile/budgets.json` is **byte-identical** to `origin/main` (blob
`36c40e4`, verified by SHA, not by eyeballing the diff). No new top-level entry,
so `agents/STRUCTURE.md` needs no edit.

## The defect

`test_injected_slowdown_raises_named_path_median` failed on PR #68 head
`efa9aba` in the `test (3.12)` job:

```
assert 82.315 >= 94.207 + 40.0
```

`base` (no injection) measured **94.207 ms**; `slowed` (a deliberate 80 ms
injection) measured **82.315 ms**.

Why that is unsoundness rather than bad luck. `_inject_delay` sleeps
`80 / 1000` s and `measure()` times each rep, so **every injected rep has an
80 ms floor by construction**. `82.315 - 80 = 2.3 ms` of real digest work —
within 2.9% of the theoretical minimum. The injected side was therefore an
almost noise-free measurement; only `base` was noisy, and it absorbed ~92 ms.

Because `base` **exceeded** `slow` outright, every assertion comparing the two
fails on this data:

| formulation | required `slow` | observed | result |
|---|---:|---:|---|
| `slow >= base + 40` (shipped) | 134.2 | 82.3 | fail |
| `slow >= base * 1.5` | 141.3 | 82.3 | fail |
| `slow >= base * 1.2` | 113.0 | 82.3 | fail |
| `slow >= base` (zero margin) | 94.2 | 82.3 | fail |

So no margin — additive or relative — could have fixed it, and more reps only
helps if the load burst is short, whereas inflating a 5-rep *median* by 92 ms
requires the burst to span at least 3 reps. `agents/CHECKS.md` forbids tests
that depend on host wall clock, so this was a contract violation against a
named standard, not a tolerance to tune.

## The fix — principled, not the fallback

I took the injected-sleeper seam, not `slow >= 0.9 * injected_ms`. No fallback
was needed; the restructuring stayed inside the lease.

- `_inject_delay(inject_ms, name, sleeper)` takes the sleeper explicitly.
- `delay_fn(inject_ms, sleeper)` binds both into a `Callable[[str], None]`.
- `run_profile(..., sleeper=None)` defaults to `time.sleep`, matching the
  existing `fleet_fn=` seam convention in the same function.
- The six `_time_*` functions now take that callable instead of the injection
  dict, which also deletes the `inject_ms` plumbing from each of them.

Two deterministic tests replace the timing comparison:

1. `test_inject_delay_sleeps_only_the_named_path_for_its_configured_amount` —
   asserts the sleeper receives `0.080` for `"digest"`, nothing for another
   path, and nothing for a `0.0` or negative amount. This is literally
   "called with `("digest", 0.080)`".
2. `test_injection_reaches_each_named_path_in_measurement_order` — injects
   **two different amounts** (`facts=30`, `digest=80`) and asserts the recorded
   sleep sequence is `[0.030] * 6 + [0.080] * 6`. Differing amounts prove
   per-path *routing* in measurement order, not merely a call count; `6` is
   `warmup + reps`.

Neither reads a clock. Both are also faster than what they replace: the old
test ran `run_profile` twice and really slept ~480 ms; the new pair runs it once
with the sleeper stubbed out.

**No coverage was lost.** The end-to-end property that an injection inflates
the *reported median* and trips the gate is still covered by
`test_check_budgets_fails_when_ceiling_exceeded`, which injects `facts=120.0`
against a 1 ms budget — and that assertion is sound, because it is one-sided
and guaranteed by the sleep floor.

## Mutation proof (the new tests fail on real bugs)

Not assumed — each mutation applied to `harness.py`, suite run, then reverted:

| mutation | rc | which tests fail |
|---|---:|---|
| sleeper never called (injection dropped) | 1 | both new tests |
| wrong amount (`* 2`) | 1 | both new tests |
| `_time_digest` requests `"facts"` (misroute) | 1 | the measurement-order test only |
| none (baseline) | 0 | — 13 passed |

The third row is why both tests exist rather than one: the unit test cannot see
a wiring error, and the end-to-end test is what catches it.

## Audit: every wall-clock-dependent assertion in the file

Requested by the integrator. All six surviving sites, with a verdict on whether
each can fail spuriously.

| line | assertion | depends on clock | can fail spuriously? |
|---|---|---|---|
| 101 | `item.median_ms >= 0` | yes | **No.** `time.perf_counter()` is monotonic, so elapsed is non-negative by construction. |
| 102 | `min_ms <= median_ms <= max_ms` | yes | **No.** Algebraic invariant of min/median/max over one sample list; holds at any noise level. |
| 200 | `main(tight) == 1` — `facts` ceiling 1.1 ms vs a 120 ms injection | yes | **No.** One-sided; the sleep floor guarantees ≥120 ms and noise can only push it further past the ceiling. |
| 205 | `main(loose) == 0` — every path under a generous ceiling | yes | **Was the thinnest margin in the file; fixed.** See below. |
| 303 | `main(grown) == 1` | yes | **No.** Driven by corpus mismatch, which short-circuits in `check_budgets.main` *before* `evaluate`, and the preceding `"re-baseline sample"` assertion pins the cause so it cannot pass for the wrong reason. |
| 210 | `_time_fleet_status(lambda _name: None)` | no | Not a timing assertion. Host-dependent (git, subprocess, `gh` stub) but asserts only that the script exits 0. |

**Line 205, fixed.** The fixture used a 10 000 ms per-path budget, i.e. a 15 s
ceiling. I measured the actual medians in that fixture rather than guessing:

| path | median | headroom to the 15 s ceiling |
|---|---:|---:|
| ingest | 0.218 ms | 68 807x |
| projection | 2.754 ms | 5 447x |
| digest | 1.640 ms | 9 146x |
| queue-tick-100 | 7.023 ms | 2 136x |
| facts (120 ms injected) | 177.971 ms | 84x |
| **fleet-status** | **2 993.955 ms** | **5.0x** |

`fleet-status` spawns git and `gh` subprocesses, so 5.0x is a real residual —
thinner than my initial estimate of ~8x, which is why I measured instead of
inferring. Raised the fixture to `UNREACHABLE_BUDGET_MS = 1_000_000` so those
assertions test the checker and not the host's speed. The *fail*-path fixtures
still set one path low, so their behaviour is unchanged.

**Outside my lease — listed, not touched:**

- `tests/test_runner.py:90` — `assert time.monotonic() - started < 2` in
  `test_executor_process_enforces_wall_clock_timeout` (0.05 s timeout, a
  subprocess that sleeps 5 s). One-sided, ~1.7 s of headroom over a CPython
  spawn, and the primary contract is already asserted deterministically on
  line 89 (`result.timed_out is True`). Line 90 still earns its place: it
  proves the call did not block for the full 5 s. **Sound; no change
  recommended.**
- `tests/test_runner.py:105,128` — `time.sleep` appears only inside generated
  fixture scripts that exist in order to be timed out. Not assertions.

No other file under `tests/` references `perf_counter`, `time.sleep`,
`median_ms`, or `monotonic`.

## Pin invariants still hold (confirmed, since I edited the file that tests them)

1. `corpus_jobs == len(DEFAULT_CORPUS)` — asserted and passing.
2. `corpus_roots == list(DEFAULT_CORPUS)` — asserted and passing.
3. `budgets.json` byte-unchanged — blob SHA `36c40e4` on both `HEAD` and
   `origin/main`.
4. `check_budgets.assert_corpus_shape` still fires **before** any budget
   comparison — verified by reading `main()`: it returns 1 on a corpus mismatch
   ahead of the `evaluate` call.

The six pin/corpus tests pass by name (`-k "pinned or corpus or promoting or
vanished"`).

## Verification

- `uv run pytest` — **full suite, 696 tests across 35 files, rc=0**, on the
  rebased head that includes `origin/main` `28d0cf0`.
- `uv run ruff check .` — all checks passed.
- `uv run pytest tests/test_profile_harness.py` — 13 passed (12 before; one
  unsound test became two sound ones).
- Mutation matrix above.
- Measured fixture medians above.

Capability labels: **proven live** for the suite, lint, mutation matrix, and the
measured medians, all on this machine. **pending in PR** for GitHub's
`test (3.12)` / `test (3.14)` matrix, which is where the original failure
appeared and which only CI can exercise.

## Note carried for the #68 review

`82.315 - 80 = 2.3 ms` of real digest work means the digest path was *fast* in
the very run that "failed", so no `src/evallab/results.py` change can produce
that signature. The failure was this test's unsoundness, not JobsDirContract's
diff. Recorded here so it cannot be misattributed if it recurs before this
lands.

## Follow-up candidate — NOT part of this change

Unchanged from the earlier `perf-rebaseline` mission: `_time_ingest` still calls
`initialize()` — a full `sql/schema.sql` DDL replay on its own connection —
inside the timed region, so `ingest` measures connection setup and catalog work
rather than ingest logic. That remains a board candidate and needs its own
re-baseline, whose CI samples must be drawn from runs matching the pinned corpus
shape. This PR does not change any measured quantity: the sleeper seam is inert
when `sleeper=None`, which is the only way `main()` calls it.
