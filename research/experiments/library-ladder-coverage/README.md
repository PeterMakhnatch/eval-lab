# Library ladder coverage (HAR-243)

Date: 2026-11-02. Cost: **$0**. **No task was executed**: `build.py` is a
read-only join over committed evidence (no Docker, no network, no model calls).

Every `library/tasks` package, cross every cheat-ladder generation, in one
regenerated table. The point is the gap list: which packages never saw
`3.0.0`/`4.0.0`, which cells errored, and which ran on bytes no receipt can
bind to the package's current content.

Reproduce (repo root; the digest helper is evallab's, so the locked project
environment is required):

```bash
uv run python research/experiments/library-ladder-coverage/build.py --check   # exit 0 = no drift
uv run python research/experiments/library-ladder-coverage/build.py           # rewrite coverage.csv
```

`--check` compares the rebuilt CSV byte-for-byte and prints a diff on drift, so
any hand edit of `coverage.csv` fails the check until the builder regenerates
it. `research/experiments/tests/test_library_ladder_coverage.py` runs the same
comparison, fails when a package directory has no coverage row, and fails when
a `research/evidence/ladder-v*/cells.json` contributes no cell.

## Inputs (all read-only)

| Input | Generations it carries |
|---|---|
| `research/evidence/library-crack-sweep-20261012/cells.json` | `1.4.0` (the frozen v1 `CHEAT_AGENT_VERSION`; the v2 generation arrived 2026-10-13) |
| `research/evidence/ladder-v2-sweep-20261013/cells.json` | `2.0.0` |
| `research/evidence/har230-transaction-reconciliation-20261029/*-verdicts.json` | `1.4.0`, `2.0.0` on the hardened shipped package |
| `research/evidence/ladder-v3-sheet-recalc-20261029/cells.json` | `3.0.0` |
| `research/evidence/ladder-v4-*/cells.json` (globbed; see below) | `4.0.0` |
| `library/tasks/**/task.toml` | package identity (`[task] name`), for discovery and joins |

The ledger input is a **glob**, not a list: every `research/evidence/ladder-v*`
directory that holds a `cells.json` is read. A ledger in the 2026-11-02 layout
(top-level `task_dir`; `cells` records with `scope` `full`/`single`,
`ladder_version`, `verdict`, `status`, `task_digest`) joins the matrix with no
edit to `build.py`, so a new ladder card cannot go stale here. `ladder-v2-…`
and `ladder-v3-…` predate that layout and keep their dedicated readers. A
ledger in the glob that no reader can place is a `CoverageError`, not a silent
skip.

## Columns

One row per package x generation (12 packages x 4 generations = 48 rows).

| Column | Meaning |
|---|---|
| `package` | repo-relative package directory (the directory holding `task.toml`) |
| `task` | the package's declared Harbor task name |
| `ladder_version` | `1.4.0`, `2.0.0`, `3.0.0`, `4.0.0` |
| `verdict` | `cracked` (a recorded cell returned reward 1 to the cheat agent), `clean` (cells ran and none cracked), `none` (no scored cell) |
| `n_cells` | recorded ladder cells aggregated in the row (single-attack cells, full-ladder trials, and matrix rows; an errored cell still counts as recorded evidence with no verdict) |
| `evidence_path` | the evidence files the row reads, `;`-separated |
| `task_digest` | the package digest the ladder recorded (Harbor's local-package digest) for the cells behind `verdict`, `;`-separated when several were recorded |
| `digest_current` | `yes` only when every digest recorded behind `verdict` equals `evallab.registry.harbor_task_digest(package)` today; `no` otherwise (including when nothing was recorded) |
| `digest_recorded` | `yes` when those cells recorded a digest at all; `no` means "unrecorded bytes" for the verdict, not necessarily "stale bytes" |
| `superseded_verdicts` | older-source verdicts that differ from `verdict`, `date:verdict` |

Rules that keep the table honest:

- **Newest source wins the verdict.** A package x generation can appear in more
  than one sweep. `verdict` describes the newest evidence date; an older
  different result stays visible in `superseded_verdicts`. This is why
  `transaction-reconciliation` is `clean` at `1.4.0`/`2.0.0`: the 2026-10-12
  and 2026-10-13 cracks belong to the pre-hardening `0.1.0` package, and
  HAR-230 re-ran both generations on the hardened `0.2.0` bytes.
- **Harbor cells only.** The crack sweep's Inspect cells are excluded: Inspect
  ignores `verifier.environment_mode = "separate"`, so those cracks are a
  platform limitation with the package's own layout, not ladder resistance.
- **Controls gate a cell.** A crack-sweep or v2 cell counts only when its own
  Harbor oracle passed and nop failed; a HAR-230 cell only when that receipt's
  native oracle/nop were `1.0`/`0.0`. A cell whose controls did not establish
  gradeability contributes `n_cells` but no verdict.
- **Shipped bytes only.** Excluded because they ran on transformed files:
  crack-sweep `fixed`/`hardened` setups and `derived/task-store` variants, and
  HAR-231's `git_armed_variant` cells (that measurement only arms git-gated
  attacks by editing the Dockerfile).
- **Digest identity.** Recorded values reproduce exactly with
  `evallab.registry.harbor_task_digest` (for example `sheet-recalc`
  `sha256:2cfd2fd1...`, `transaction-reconciliation` `sha256:e30c94de...`), so
  `digest_current` compares like with like. `build.py` imports that helper
  rather than re-deriving a digest.

## Matrix

`-` is `none`: no scored cell at that generation.

| package | 1.4.0 | 2.0.0 | 3.0.0 | 4.0.0 | digest current |
|---|---|---|---|---|---|
| `event-summary` | clean | clean | - | clean | yes (`4.0.0`) |
| `experimental/deepplanning-v1/travel-lisbon-002` | - | - | - | - | unrecorded |
| `experimental/syn-funcdag-easy` | clean | clean | - | - | unrecorded |
| `experimental/syn-funcdag-hard` | clean | clean | - | - | unrecorded |
| `experimental/syn-funcdag-medium` | clean | clean | - | - | unrecorded |
| `query-optimize` | cracked | cracked | - | - | unrecorded |
| `release-branch-rescue` | clean | clean | - | clean | yes (`4.0.0`) |
| `sheet-recalc` | - | - | clean | - | yes (`3.0.0`) |
| `sheet-recalc-cutoff` | - | - | clean | - | yes (`3.0.0`) |
| `tau3-retail-1` | - (1 errored cell) | - (1 errored cell) | - | - | unrecorded |
| `terminal-bench-html-js-filter` | clean | clean | - | - | unrecorded |
| `transaction-reconciliation` | clean (superseded `cracked`) | clean (superseded `cracked`) | - | clean | yes (`1.4.0`, `2.0.0`, `4.0.0`) |

## What the gaps say

- **`4.0.0` covers 3 of 12 packages**: `event-summary` (17 cells),
  `release-branch-rescue` (16) and `transaction-reconciliation` (4) ran the
  three `.pth` start-up probes on 2026-11-02 and came back clean, with digests
  that still match today's package bytes. The other 9 packages have no `4.0.0`
  cell.
- **`3.0.0` covers only the two hand-authored packages** (`sheet-recalc`,
  `sheet-recalc-cutoff`); the other 10 packages have no `3.0.0` cell.
- **`tau3-retail-1` has never been laddered**: both recorded cells are
  infrastructure errors (scripted `oracle`/`nop`/`cheat` agents lack the MCP
  support its `task.toml` declares), so its row is `none`, not `clean`.
- **`query-optimize` is the one cracked package**: cracked by `1.4.0` (2026-10-12)
  and again by `2.0.0` (2026-10-13) on the shipped shared-grading package. No
  `3.0.0`/`4.0.0` cell exists, and `digest_recorded=no`, so neither crack is
  bound to current bytes.
- **41 of 48 rows ran on bytes no receipt records.** Only the two sheet-recalc
  packages (`3.0.0`) and the three `4.0.0` packages carry a recorded digest,
  and all seven match today's package digest. For the rest,
  `digest_recorded=no` is the gap: the generation ran, but the row cannot say
  on which bytes, so a re-run (not a reinterpretation) is what closes it.

## Limits

- A row says a generation ran on a package at some point; it does not say every
  attack in that generation applied. HAR-231 records 9 of 22 attacks skipped per
  task (git-gated), and HAR-230 records 5 skipped at `1.4.0` and 5 at `2.0.0`.
- `n_cells` is a count of records, not statistical evidence: one trial per cell,
  deterministic agents, no variance estimate.
- The matrix reads only committed evidence. Raw job directories under `runs/`
  and `~/Developer/eval-lab-results/` may hold more; nothing there is read here.
