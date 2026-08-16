Status: review-wanted
Last: pinned the profiled corpus to two named job directories; no budget or tolerance changed; PR opened
Next: reviewer confirms the pin and the corpus-shape assertion; merge unblocks PR #58
Blockers: none

# PERF-CORPUS-PIN

Mission: stop the perf gate from measuring the size of the committed evidence
corpus, so promoting evidence cannot turn it red. Discovered by PromoteEvidence
(PR #58), which correctly refused to re-scope a gate to make its own PR pass.

Branch `role/perf-corpus-pin`, worktree `.worktrees/perf-corpus-pin`, cut from
`origin/main` at `fdc22f8`.

## Lease

Written: `scripts/profile/harness.py`, `scripts/profile/check_budgets.py`,
`scripts/profile/budgets.json`, `scripts/profile/README.md`,
`tests/test_profile_harness.py`, `docs/engineering.md`, this file.

Not touched: `research/evidence/`, `src/`, `.github/workflows/`, `policy/`,
`library/`. `git status` on `research/evidence` is clean (the decoupling proof
below staged two throwaway directories there and removed them; nothing was
committed).

No new top-level entry, so `agents/STRUCTURE.md` needs no edit. Everything
written is inside `scripts/`, `tests/`, `docs/`, and `agents/handoffs/`, which
the map already covers.

## The shape chosen, and why

**Option (b): an explicit pinned set of job directories.** Not a new fixture
under `tests/fixtures/`.

```python
DEFAULT_CORPUS = (
    "research/evidence/runs/event-summary-nop-evidence",
    "research/evidence/runs/event-summary-oracle-evidence",
)
```

Reasons:

1. **It preserves the meaning of the committed budgets instead of resetting
   them.** `origin/main`'s `research/evidence/runs/` holds exactly these two
   control jobs, and they are the 2-job corpus every committed number was
   measured on. Pinning to them requires **zero** budget change. A new fixture
   would have forced a re-baseline of `projection`, `facts`, and `digest` —
   a loosening with nothing behind it, and one that would have discarded the
   14 CI samples behind `ingest`.
2. **It is structurally, not merely conventionally, decoupled.**
   `results.discover_job_dirs` short-circuits when a root is itself a job
   directory (`src/evallab/results.py:236-244`), returning it verbatim. So the
   profiled set cannot grow by discovery. Adding a sibling under
   `research/evidence/runs` is invisible to the default. Proven below.
3. **It keeps the profiler measuring real Harbor output** rather than a
   synthetic copy that would drift from the real parsers, and avoids
   duplicating reviewed evidence into `tests/` where it could be mistaken for
   evidence.

The pin lives in the harness default, not in `.github/workflows/perf.yml`.
Pinning in the workflow would recreate the split that PR #53 removed —
calibrated in one place, enforced in another. CI passes no `--corpus`
(`.github/workflows/perf.yml:51-55`), so changing the default is sufficient and
the workflow needs no edit.

`--corpus <repo-relative-path>` (repeatable) still points the harness anywhere
for ad-hoc profiling.

## Three of six paths were corpus-coupled, not two

Verified by timer signature and then by measurement. `_time_ingest`,
`_time_projection`, and `_time_facts` all take the loaded `list[JobRecord]`.
`_time_digest` takes only a repo root, `_time_queue_tick` a synthetic `tick_n`,
and `_time_fleet_status` is git/`gh`-bound. Run C below doubles the corpus and
moves exactly those three.

Pinning also makes the 2026-08-15 `ingest` re-baseline meaningful in retrospect:
its 14 CI samples were drawn on the 2-job corpus, which is now the corpus the
budget is enforced against.

## No re-baseline. `ingest` and `tolerance_pct` untouched.

`tolerance_pct` is still 50 and all six `paths` values are byte-identical to
`origin/main`:

```
$ python3 -c "... compare against git show origin/main:scripts/profile/budgets.json"
tolerance unchanged: True 50
paths unchanged: True
ingest: 115.0 115.0
old notes preserved as prefix: True
```

Local sample, three independent runs on 2026-08-16, each a median of 5 reps
after 1 warmup (macOS-26.5-arm64, Apple Silicon, Python 3.12.11, scratch
Postgres `evallab_speed_prof` on `127.0.0.1:54329`, Harbor stubbed):

| path | run 1 | run 2 | run 3 | budget | ceiling |
|---|---:|---:|---:|---:|---:|
| ingest | 30.390 | 28.638 | 31.198 | 115.0 | 172.5 |
| projection | 3.187 | 2.884 | 2.811 | 25.0 | 37.5 |
| facts | 4.048 | 3.905 | 3.861 | 25.0 | 37.5 |
| digest | 0.964 | 0.876 | 0.927 | 15.0 | 22.5 |
| queue-tick-100 | 78.202 | 74.679 | 74.837 | 150.0 | 225.0 |
| fleet-status | 2240.928 | 2250.273 | 2234.392 | 5000.0 | 7500.0 |

Every median is inside its ceiling, so nothing was re-baselined. **This is a
local sample and cannot support a budget change**; it supports only the claim
that no change is needed. There are no CI artifact samples for the pinned
corpus because it does not exist upstream yet — the first CI run on this PR is
the first CI datapoint for it. Per the PR #53 rule, any future re-baseline must
cite CI artifact samples.

`check_budgets.py` prints its usual `under 50% of budget — consider
re-baselining` notices for five paths. That is pre-existing and non-fatal
(already recorded in `agents/archive/2026-08-15-handoffs/perf-rebaseline.md:69`);
acting on them would tighten CI-enforced budgets against laptop medians, which
is the mistake PR #53 fixed.

Deliberately **not** in this PR: moving `initialize()` out of the timed `ingest`
region. Same file, same defect class, but it drops the number sharply and
forces a real re-baseline. Keeping it separate is why this PR moves no budget at
all. That work is a board candidate, and it must land *after* this pin —
otherwise its samples are drawn on an unpinned or 5-job corpus and pinning
afterwards forces a second re-baseline.

## Preventing re-coupling: two mechanisms, two failure modes

A **code** edit is caught by the suite; a **data** change is caught at the gate.

1. `tests/test_profile_harness.py::test_default_corpus_is_pinned_to_job_directories_not_the_evidence_directory`
   — fails if `"research/evidence/runs"` appears in `DEFAULT_CORPUS`, if any
   pinned entry is not itself a Harbor job directory, or if discovery over the
   default returns anything beyond the pinned entries.
2. `::test_promoting_a_job_directory_does_not_change_a_pinned_corpus` — builds a
   scratch corpus in `tmp_path` (never writes `research/evidence/`), adds a job
   directory, and shows the container root grows while the named roots do not.
3. `::test_committed_budgets_declare_the_pinned_corpus_shape` — `budgets.json`
   `corpus.roots` must equal `DEFAULT_CORPUS`, and its `jobs`/`result_json` must
   equal what `corpus_stats` actually measures. Stops the two from drifting.
4. `check_budgets.assert_corpus_shape` — `budgets.json` now carries a `corpus`
   block (`roots`, `jobs`, `result_json`); a report whose corpus differs fails
   before any budget is compared, with a message saying the report "neither
   passes nor fails them meaningfully, and it MUST NOT be cited as a
   re-baseline sample". Budgets files without a `corpus` block are rejected
   outright.
5. `harness.resolve_corpus_roots` — a pinned entry that no longer exists raises
   instead of silently profiling a smaller corpus.

Mutation-checked: temporarily restoring `DEFAULT_CORPUS = ("research/evidence/runs",)`
fails tests 1, 2, and 3. Restored afterwards.

## Decoupling proof

Run A — pinned default, clean tree:

```
- corpus: 2 jobs, 4 result.json, 31716 bytes
- corpus_roots: research/evidence/runs/event-summary-nop-evidence, research/evidence/runs/event-summary-oracle-evidence
| ingest         | 27.641 | 24.700 | 30.470 | 5 |
| projection     |  2.850 |  2.627 |  2.897 | 5 |
| facts          |  3.834 |  3.577 |  4.158 | 5 |
| digest         |  0.926 |  0.849 |  0.998 | 5 |
| queue-tick-100 | 73.262 | 71.859 | 74.197 | 5 |
```

Then two throwaway job directories were copied into
`research/evidence/runs/` (simulating a promotion).

Run B — same pinned default, corpus on disk now doubled:

```
- corpus: 2 jobs, 4 result.json, 31716 bytes
- corpus_roots: research/evidence/runs/event-summary-nop-evidence, research/evidence/runs/event-summary-oracle-evidence
| ingest         | 27.919 | 26.602 | 30.765 | 5 |
| projection     |  3.020 |  2.725 |  3.047 | 5 |
| facts          |  3.709 |  3.612 |  3.819 | 5 |
| digest         |  0.871 |  0.835 |  0.938 | 5 |
| queue-tick-100 | 75.026 | 73.541 | 93.848 | 5 |
```

Identical corpus, identical paths, numbers within run-to-run jitter.

Run C — same tree, old behaviour reproduced with
`--corpus research/evidence/runs`:

```
- corpus: 4 jobs, 8 result.json, 63432 bytes
- corpus_roots: research/evidence/runs
| ingest         | 35.767 | 34.776 | 38.519 | 5 |
| projection     |  5.158 |  4.992 |  6.035 | 5 |
| facts          |  6.665 |  6.435 |  7.270 | 5 |
| digest         |  0.812 |  0.788 |  0.856 | 5 |
| queue-tick-100 | 76.255 | 74.493 | 82.500 | 5 |
```

`ingest` +29%, `projection` +71%, `facts` +80% from doubling the corpus alone;
`digest` and `queue-tick-100` flat. That is the three-of-six coupling, measured.

Gate verdicts on the same two reports:

```
$ uv run python scripts/profile/check_budgets.py runs/_speed_after/profile-report.json
perf budgets ok                                                    # exit 0

$ uv run python scripts/profile/check_budgets.py runs/_speed_old/profile-report.json
perf corpus mismatch:
  profiled corpus does not match the shape the budgets were measured on:
  corpus_roots ['research/evidence/runs'] != declared [...nop..., ...oracle...];
  corpus_jobs 4 != declared 2; corpus_result_json 8 != declared 4. ...
  MUST NOT be cited as a re-baseline sample. ...                   # exit 1
```

Both throwaway directories were removed; `git status --porcelain
research/evidence` is empty.

## Verification

- `uv run python scripts/profile/harness.py` — runs against the pinned default.
- `uv run python scripts/profile/check_budgets.py runs/_speed/profile-report.json` — exit 0.
- `uv run pytest` — see PR body.
- `uv run ruff check .` — clean.

`scripts/premerge.sh` deliberately not run (integrator's step).

## Doc changes

`docs/engineering.md` gains two dated sections after the 2026-08-15 `ingest`
re-baseline:

- **Corpus pin (2026-08-16)** — what changed, why no budget moved, the local
  sample, and the extended rule: *a perf re-baseline may only cite samples from
  runs whose reported `corpus_roots` and `corpus_jobs` match the pinned shape
  declared in `budgets.json`.*
- **Anti-pattern: freezing the composition of a growing corpus (2026-08-16)** —
  names the pattern across its three instances today
  (`scripts/profile/budgets.json` via the harness default,
  `research/analysis/tests/test_facts.py:25` asserting the evidence set is
  exactly `{oracle, nop}`, `research/analysis/tests/test_analysis.py:331`
  asserting `n_labels == 25`) and the rule: **assert invariants and
  relationships, not census counts.**

Note: the brief located the third instance at
`research/calibration/tests/test_analysis.py`; the file is actually
`research/analysis/tests/test_analysis.py:331`. The doc cites the real path.
