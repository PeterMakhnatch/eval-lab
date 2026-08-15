Status: building
Last: Final code passed three consecutive premerge runs and fresh-clone default/non-default suites.
Next: Rebase once more, open the TRUTH PR, require all GitHub checks green, then merge and sunset.
Blockers: none

# TRUTH handoff

## Scope

- Honest task-level cohort comparisons and paired task inference.
- Power planning for detectable effects and n/k tradeoffs.
- Plain-language trajectory family reports from Parquet joined to canonical ATIF.
- Provenance-bearing eval-card templates and completed-spec drafts.

## Constraints observed

- Subscription credentials only. No API-key environment variables are introduced, read, or
  forwarded by this work.
- Raw Harbor jobs and registered task material remain read-only.
- Generated comparison/report artifacts stay rebuildable; durable eval-card drafts carry source
  digests and refuse overwrite.

## Evidence log

- `git fetch origin`: pass (2026-08-14).
- `uv sync`: pass with CPython 3.12.11 (sandboxed uv crashed in macOS system configuration;
  the same command succeeded outside that sandbox).
- Required repository guidance read before implementation.
- `uv run ruff check .`: pass.
- `uv run pytest`: pass, `90 passed in 7.03s`.
- `uv run pytest -q research/analysis/tests dashboard/tests`: pass after repairing stale
  post-REFRAME evidence paths, `33 passed`.
- `uvx ty@0.0.71 check src/ --output-format=concise`: expected nonzero under the ratchet,
  `28 diagnostics` (baseline/ceiling is 33; TRUTH adds zero and removes five local diagnostics).
- `uv run evallab compare research/analysis/control-oracle-vs-nop.json`: pass; the one-task
  control prints `not distinguishable / not comparable: only 1 paired task(s); at least 2 are
  required` instead of a ranking.
- Both `evallab power` modes render task-paired plans. Example fixed design: baseline 0.300,
  `n_tasks=100`, `k=3`, MDE `0.1428` per attempt (independent-attempt planning assumption is
  printed).

### Three consecutive acceptance runs (rebased commit `afab373`)

```text
$ scripts/premerge.sh  # pass 1
All checks passed!
90 passed in 6.49s
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 33

$ scripts/premerge.sh  # pass 2
All checks passed!
90 passed in 6.48s
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 33

$ scripts/premerge.sh  # pass 3
All checks passed!
90 passed in 6.46s
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 33
```

### Fresh-clone acceptance

Cloned remote `role/truth` at `afab37302fda1d10fef6606bc0bedad7f0891e55` into a temporary
repository-local directory, then removed it after verification.

```text
$ scripts/premerge.sh
Using CPython 3.12.11
Creating virtual environment at: .venv
Installed 41 packages
All checks passed!
90 passed in 11.85s
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 33

$ uv run pytest -q research/analysis/tests dashboard/tests
.................................                                        [100%]
```

### Real reviewed-evidence smoke

```text
$ rebuild_from_raw(research/evidence/runs)  # invoked through the installed package
{'trial_facts': 2, 'reward_facts': 8, 'artifact_facts': 6, 'tool_usage': 0}
{'trajectories': 0, 'steps': 0, 'tool_calls': 0, 'observations': 0}

$ uv run evallab report family event-summary ...
This family contains 2 trials across 2 jobs.
Recognizable verification ... was unknown for 2 trial(s) without readable ATIF.
```

The smoke exposed wording that called a no-ATIF control “0 steps.” The implementation now treats
step length as unavailable unless a trajectory exists, with a regression test.

### Final-code acceptance (remote commit `0b1a7b7`)

```text
$ scripts/premerge.sh  # pass 1
All checks passed!
91 passed in 6.42s
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 33

$ scripts/premerge.sh  # pass 2
All checks passed!
91 passed in 6.76s
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 33

$ scripts/premerge.sh  # pass 3
All checks passed!
91 passed in 6.58s
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 33
```

The final remote branch was cloned again at
`0b1a7b79b58b94aa91a60b2cbe97b8687c3777f1`; the temporary clone was removed after:

```text
$ scripts/premerge.sh
Using CPython 3.12.11
Creating virtual environment at: .venv
Installed 41 packages
All checks passed!
91 passed in 13.13s
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 33

$ uv run pytest -q research/analysis/tests dashboard/tests
.................................                                        [100%]
```

## Deferred coordination

- The type count fell from 33 to 28, so the shared ratchet should be lowered. Active SOLIDIFY work
  currently owns and changes `.github/workflows/`, `scripts/premerge.sh`, and related governance;
  TRUTH did not create an overlapping edit. Its PR/handoff should consume the observed 28 count.
