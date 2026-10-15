# mimo-clean-v6 (separate-verifier@10 fleet build)

Date: 2026-10-15. Spend: **$0** (local CPU and local Docker only;
no paid APIs or remote compute). Chain (Python):
`strip-future-history@1>purge-installed-copies@2>purge-build-caches@4>mtime-normalize@2>separate-verifier@10>agent-network-none@1`;
non-Python omits the purge slot; `NODE_FOLD_TASKS` keep
`purge-build-caches@5` per row. Manifest: `manifest.csv` (2,698 rows:
2,666 built, 32 skipped — the ledger discards). The build initialized
`verify` to `unverified` except 23 labels copied from older v5 evidence.
This receipt invalidates those unsupported current-digest labels to
`unverified`: none of the 23 has a matching final digest in the v5
census results or override receipts. Fresh controls update only their
exercised plain-v6 rows; no row is promoted to full clean-set `pass`.
Selection: `selection.json` (flagged traces + flagged envcheck + seeded
`sample_50`, seed `20261015`).

## Measured build wall-clock (local CPU, `--workers 8`, $0)

| build | tasks | wall |
|---|---|---|
| `mimo-clean-v6` selection subset (flagged union 49 + sample 50) | 99 built | 38.1 s |
| `mimo-clean-v6` Python ledger pool | 1,148 built / 32 skipped of 1,180 | 166.9 s |
| `mimo-clean-v6` full fleet | 2,666 built / 32 skipped of 2,698 | 269.9 s |
| (context) `mimo-clean-v5` #848 digest refresh, same host | 2,666 built of 2,698 | 652.8 s |

Builds are incremental and content-addressed: later builds reuse the
chain steps materialized by earlier ones in
`derived/task-store/variants/` (first-build numbers above are in run
order: subset, Python pool, full fleet).

## Cost/time to verify the full 1,148-task Python fleet on v6

Rates are the measured v5 census Modal LIMIT actuals: oracle
$0.0022–$0.0071/task, nop $0.0053/task, ladder $0.00674/task
(`research/experiments/mimo-clean-v5-census/`). Reference-fix tasks
(428) get oracle+nop+ladder; reference-less tasks (720) get
nop+ladder: 428 oracle + 1,148 nop + 1,148 ladder = 2,724 cells.
The 3x option runs the repeat verifier (`repeat_n=3`) for every cell,
not three independent agents: 2,724 trials / 8,172 verifier invocations.

**Historical full-protocol planning baseline, not measured v6 wall
time or approval to launch.** The local scenario uses the measured v5
ladder median of 15.5 min/task (n=50 local sample), plus an explicitly
assumed 5 min per oracle/nop cell. Thus local 1x trial-minutes =
`1,148 * 15.5 + (428 + 1,148) * 5 = 25,674` (427.9 h).
Modal time is only a planning scenario: an assumed 3 min/cell at the
census's 24-wide fan-out. The 3x columns conservatively multiply
whole-cell time and cost by three; real repeat-verifier overhead may be
lower because image/setup/agent work is shared. Cold pulls, retries,
flakiness, and the @10 integrity gate can change both estimates.

| option | verifier repeats | estimated wall | provider cost |
|---|---|---|---|
| Local Docker, 1 concurrent trial | 1x | 427.9 h (17.8 days) | $0 |
| Local Docker, 2 concurrent trials | 1x | 214.0 h (8.9 days) | $0 |
| Local Docker, 1 concurrent trial | 3x | 1,283.7 h (53.5 days) | $0 |
| Local Docker, 2 concurrent trials | 3x | 641.9 h (26.7 days) | $0 |
| Modal LIMIT, 24-wide | 1x | 5.7 h | $14.76–$16.86 |
| Modal LIMIT, 24-wide | 3x | 17.0 h | $44.29–$50.58 |

| Python group | checks | Modal 1x | Modal 3x | local 1x trial-hours | local 3x trial-hours |
|---|---|---|---|---|---|
| 428 reference-fix tasks | oracle+nop+ladder | $6.09–$8.19 | $18.28–$24.58 | 181.9 | 545.7 |
| 720 reference-less tasks | nop+ladder (no solvability claim) | $8.67 | $26.01 | 246.0 | 738.0 |
| all 1,148 | 2,724 cells | $14.76–$16.86 | $44.29–$50.58 | 427.9 | 1,283.7 |

Concurrency 2 is a hypothetical throughput comparison, **not today's
run configuration**: with the egress sidecar, this tab's two-container
cap requires at most one active Harbor trial (`--n-concurrent 1`).
The CPU build slice (#909) ran no Docker trials; the following independent
local repeat-verifier receipt measures the plain v6 packages at their
exact post-#898 `separate-verifier@10` digests. Controls and four
task-specific regressions are not the full ladder/fix-content census.

## Local repeat-verifier protocol and scope

Every Harbor invocation sets `EVALLAB_STATE_JOURNAL=off`,
`--n-concurrent 1`, and `--n-attempts 1`. Oracle and nop use
`--verifier evallab.harbor_repeat_verifier:RepeatVerifier
--verifier-kwarg repeat_n=3`; these are three verifier invocations in
one Harbor trial, not three independent agent runs. The original four
task-specific probe scripts are copied unchanged from
`derived/detector-roots/own-scan/hardened-probe/solve/` into scratch
packages' `solution/solve.sh`, then run through the oracle agent once.
They are not a four-by-two cross-product: the three pytest probes target
`002139`, and the unittest-output probe targets `002866`.

After the host-wide FIFO became available, each subsequent trial holds
its own `evallab.docker_lock.docker_slot` context, releasing between
oracle and nop so other tabs can interleave fairly. Queue wait and active
CLI wall time are recorded separately. The final batch considers cached
tail tasks, then flagged tasks in ascending ledger `image_mib`, then the
seeded sample; it admits no new task after its two-hour wall window
(including FIFO wait), finishing an in-flight oracle/nop pair.

`results.csv` binds every row to the complete v6 final digest. Raw
repeat rewards, recognized test-execution evidence for every repeat,
and controls-only verdicts are separate from the manifest's clean-set
verdict. An exercised reference pair does **not** become `verify=pass`:
the full cheat ladder and positive-control fix-content census were not
run here. Successful pairs remain
`unverified:ladder-probe-pending`; nop-only reference-less rows remain
`unverified:ladder-missing`. Prior-generation exclusions remain blocking
context, not current-digest evidence; their unsupported labels are
invalidated to `unverified`, never relabeled as a clean-set pass.
Repaired EnvCheck variants have separate digests and receipts under
`../mimo-clean-v6-repaired/`; these rows describe only the plain packages.

## Existing exclusions and absent reference fixes

- `000081` already had oracle `fail:0` across multiple v5 census attempts
  (and nop `0`); no v6 solvability claim is inferred from that history.
- `000803` and `000838` were excluded in #891 as
  `fail:unfair-task:prompt-misaligned`; that remains prior-generation
  context, not proof for the new v6 digests.
- `001478` and `001671` had
  `fail:open-leak:env-dependency`; fresh controls cannot establish that
  those leaks are absent. Their new digests remain unverified.
- The cached packages `000236`, `000792`, `001269`, `002308`, and `002486`
  have `reference_fix=none`, no indexed patch, and no
  `solution/solve.sh`. They can receive nop x3 but not an honest oracle
  run. The flagged `000495` likewise has no reference fix. They are not
  relabeled as reference-fix or solvable tasks.
- #898 (`d24fd89d7`) already ran oracle x3 `[1,1,1]` and a **single** nop
  `0` on `000245` and `000048` at the identical full digests in this
  manifest. Historical single-nop evidence is not relabeled nop x3.

The complete set of invalidated inherited labels is below. These are
**historical context only**, not fresh negative or positive v6 verdicts.
Unverified packages remain outside clean-set admission until the
same-digest census contract is satisfied.

| unbound prior-generation label | tasks |
|---|---|
| `env-broken` | `000080`, `000323`, `000703`, `000752`, `000830`, `001024`, `001419`, `001420`, `001422`, `001505`, `001508` |
| `fail:unfair-task:prompt-misaligned` | `000803`, `000838`, `000865`, `002661` |
| `fail:unfair-task:non-idempotent-test` | `001833` |
| `fail:open-leak:node-build-pnpm` | `000928` |
| `fail:open-leak:env-dependency` | `001478`, `001671`, `001945`, `001971`, `002414` |
| `fail:vacuous-grader:compile-only` | `001694` |

## Post-#898 @10 grader-hole regression replay

This closes the prior #897-only probe caveat: both targets are the v6
packages built after #898, not the earlier packages with the same `@10`
version label. Final digests:

- `002139`: `sha256:6d156eb8a8ce50b1bf11de06b8fbebf6de3205ee82451cd85e96d0fee39deca0`
- `002866`: `sha256:faf79fbb247cabed25d83ece356d763cb223e9bed828b2f85798ff2bc6c2a139`

| task | unchanged probe | reward | active Docker CLI wall | verifier decision |
|---|---|---:|---:|---|
| `002139` | `noop_runtest` | 0 | 49.9 s | pytest runner integrity changed |
| `002139` | `noop_runtest_obfuscated` | 0 | 50.2 s | pytest runner integrity changed |
| `002139` | `pyc_shadow` | 0 | 81.7 s | 22 failed cases; pristine outcomes not preserved |
| `002866` | `fake_unittest_output` | 0 | 853.5 s | unittest JUnit cases=0, markers=False |

Source `.solve.sh` SHA-256 values, in table order:

```text
04d7c4bbfc4ecf4bfb273e3ab4ddf8cd7cc937455f40c14df6d99f0e081a357b
e55a8c555dd2a171cef696e0edb90726530e2d266f9e91cd4737d24e416e72b1
d74316beba8883b8eca414a0be9fb63dbcd749f892f1aca7684e953a1ebb7cfa
52fba804b7d3ba38ccf0502d7c0d8e796056d9a1e459bcf90e018b11ef47c1ca
```

These four successful regression rejections are not a complete cheat
ladder. The first probe also had one unscored 70.4 s harness attempt
(`FileNotFoundError` for a missing trial `result.json`) during the
initial tool-deadline/relaunch handoff; the table uses the subsequent
completed run. No retained peak-container log exists for that initial
handoff, so serial configuration is not presented as a measured
whole-run container peak.

## Measured control results (partial milestone, not selection acceptance)

Nine fresh reference-fix pairs returned oracle `[1,1,1]` and nop
`[0,0,0]`, with recognized case-execution evidence in every repeat.
Four reference-less packages have an evidenced nop x3; a fifth
(`000792`) returned `[0,0,0]` but needs manual case-evidence binding.
Its recorded runner is `custom`, rc=8; the immutable output contains
Django unittest summaries, which the census intentionally does not
recognize as runner-bound evidence for `custom`. It is **not** silently
called no-tests or a verified nop control, and an identical Docker
rerun is not queued merely to recover counting.

The census JUnit parser now accepts the optional `skipped=` field
emitted after #898, while preserving the older format and return
schema. Re-collecting existing immutable logs then recognizes the
15-case `002402` repeats and `002486` nop repeats; no Docker reruns
were needed for this counting correction.

The wall columns below are **active control time**, including image
pull/setup/agent/repeated verification, not just verifier CPU time.
FIFO wait is separate. `000236` uses native Harbor trial timestamps
after the FIFO adoption-boundary recovery, excluding CLI startup.
`000048` is historical #898 evidence, not a fresh repeat-3 nop.
Full digests, repeat arrays, case-proof arrays, errors, source jobs,
image sizes, cache state, task wall including queue, and timing basis
are in [results.csv](results.csv).

| task | final digest prefix | oracle rewards | nop rewards | oracle wall (s) | nop wall (s) | FIFO wait (s) | controls verdict |
|---|---|---|---|---:|---:|---:|---|
| `000048` | `673816e51dbc` | `[1.0,1.0,1.0]` | `[0.0]` | 104.9 | 96.9 | — | `historical:oracle-x3+nop-x1` |
| `000236` | `5e5aaac879c3` | `n/a` | `[0.0,0.0,0.0]` | — | 168.1 | 0.0 | `nop-only:no-reference-fix` |
| `000245` | `35a598db08e6` | `[1.0,1.0,1.0]` | `[0.0,0.0,0.0]` | 2160.8 | 458.6 | 0.0 | `pass` |
| `000792` | `687f2e9a65b1` | `n/a` | `[0.0,0.0,0.0]` | — | 123.8 | 0.0 | `unverified:nop-control` |
| `000941` | `baa10a49a813` | `[1.0,1.0,1.0]` | `[0.0,0.0,0.0]` | 273.6 | 75.6 | 3807.3 | `pass` |
| `001269` | `9f4c6ffcd9ec` | `n/a` | `[0.0,0.0,0.0]` | — | 124.5 | 0.0 | `nop-only:no-reference-fix` |
| `001425` | `be536e940963` | `[1.0,1.0,1.0]` | `[0.0,0.0,0.0]` | 257.8 | 91.5 | 1754.5 | `pass` |
| `001891` | `75a6a40a98fd` | `[1.0,1.0,1.0]` | `[0.0,0.0,0.0]` | 154.0 | 89.0 | 7848.6 | `pass` |
| `002139` | `6d156eb8a8ce` | `[1.0,1.0,1.0]` | `[0.0,0.0,0.0]` | 71.2 | 67.1 | 0.0 | `pass` |
| `002308` | `c8f7d177bfeb` | `n/a` | `[0.0,0.0,0.0]` | — | 106.6 | 0.0 | `nop-only:no-reference-fix` |
| `002391` | `8a1868e975b8` | `[1.0,1.0,1.0]` | `[0.0,0.0,0.0]` | 106.6 | 73.6 | 0.0 | `pass` |
| `002402` | `0374b461751c` | `[1.0,1.0,1.0]` | `[0.0,0.0,0.0]` | 186.4 | 194.5 | 0.0 | `pass` |
| `002486` | `2992d36b1aba` | `n/a` | `[0.0,0.0,0.0]` | — | 90.1 | 0.0 | `nop-only:no-reference-fix` |
| `002552` | `fe9c389dabf0` | `[1.0,1.0,1.0]` | `[0.0,0.0,0.0]` | 93.3 | 67.5 | 579.9 | `pass` |
| `002866` | `faf79fbb247c` | `[1.0,1.0,1.0]` | `[0.0,0.0,0.0]` | 869.7 | 922.1 | 0.0 | `pass` |

Here `pass` means only the repeat-control pair, **not** clean-set
admission. Manifest totals after this receipt: 2,684 `unverified`,
10 `unverified:ladder-probe-pending` (nine fresh pairs plus the
historical partial `000048` controls), and four
`unverified:ladder-missing`. No full-census `pass` is asserted.

### Actual batch wall and local-versus-Modal projection

The final batch took **15,186.5 s (4 h 13 min)**, including **1,192.4 s
active trials** and **13,990.3 s FIFO queue wait**. The original driver
stopped admitting *tasks* after 7,200 s but allowed an in-flight pair
to finish through unbounded FIFO acquisition. That overshot the intended
two-hour timebox. This is not reported as a two-hour finish; no further
sample trials were launched.

Fresh oracle+nop pairs average **11.51 min/task** (n=9, all languages);
the Python subset averages **7.49 min/task** (n=8). Python oracle x3
averages **4.19 min/cell** (n=8); Python nop x3 averages **2.81 min/cell**
(n=12, including the conservatively classified custom-runner cell).
Historical `000048`, probes, FIFO wait, and unscored attempts are excluded
from these cell means. The interrupted fresh `000048` pull never produced
a completed trial or per-cell wall receipt; it is not hidden as a pass
or included in completed-task means.

Applying these measured cell means to 428 oracle + 1,148 nop repeat-3
controls gives **83.75 serial active hours (3.49 days)** for the full
Python fleet. This is a biased cached/small-image sample, not a fleet
benchmark or queue-inclusive ETA. Docker reports `linux/aarch64`;
the inspected cached MiMo image is `linux/amd64`, so compatibility
execution overhead also limits comparison with native remote hardware.

For that **same controls-only scope**, historical Modal LIMIT rates
imply $7.03–$9.12 for 1x, or a conservative **$21.08–$27.37** whole-cell
3x projection. Actual repeat-verifier cost may be lower because setup
is shared. The **full** oracle+nop+ladder protocol instead projects
**$44.29–$50.58** at conservative 3x rates, as in the historical planning
table above; the control-only local measurement cannot estimate full
ladder runtime. No Modal job was launched; these projections are not spending approval.

### Frozen selection, pending inventory, and safe resumption

[pending-controls.json](pending-controls.json) retains every original
99 selected task (49 frozen flags + 50 seeded sample tasks), full digest,
group, per-agent state, and all missing control cells. The original
`selection.json` is unchanged and SHA-256-bound in that inventory.
**Ninety original tasks still lack complete available repeat-3 controls;
the original flagged-plus-50 acceptance remains incomplete.** Forty
original flags have no fresh control trial; no sample task was newly
completed (only `000048` has historical partial coverage).

The inventory also includes the five explicitly requested out-of-selection
cached/historical tasks and **`000354` as a post-selection addition**.
Its provenance is the 13-verified-weak handoff in
`~/Developer/research-context/bridge/overnight-2026-10-15/traces.md`
lines 11–26. The frozen 49 flags are not relabeled 49 verified weaknesses.
Unverified low findings and the named unverified line-level findings stay
separate advisory metadata, not silently added to or claimed handled by
the original selection.

Across the 105-task inventory, 92 tasks have 181 outstanding control
cells. Of those, **176 are runnable**: the four cells for the historically
excluded `000803`/`000838` and the custom-runner `000792` cell are held
for manual triage. Absent reference fixes are explicit not-applicable
oracle cells, never fabricated oracle passes. These are control coverage
counts, not a full-ladder acceptance result.

[resume_controls.py](resume_controls.py) is the retained **controls-only**
driver. It cannot select custom agents or execute attack/probe scripts.
It reuses scored positive **or negative** cells only at the exact
`task_id + full final_digest + agent`, requiring three reward values,
three case-proof flags, no repeat errors, and no native trial exception.
An old single nop is not reused as x3, and changed pending digests stop
the run rather than inheriting verification.

Resumption validates the materialized package's complete digest before
requesting a Docker slot. It dispatches through Eval Lab's
`Executor.execute_direct(RunRequest(..., verifier_repeat_n=3), ingest=False)`,
not raw `harbor run`, so the standard host-isolated Docker boundary and
execution provenance apply. Only `oracle` and `nop` can be selected here.
The optional state-journal observer is disabled before CLI execution.

Each individual FIFO acquisition is bounded by the smaller of
`--lock-wait-seconds` and the remaining admission window. A FIFO timeout
records queue time and stops without launching Docker. Admission is checked
again after image inspection, immediately before launching Harbor, so a slow
inspection cannot admit a new trial at or after the deadline. An already-running
native Harbor trial may finish beyond it. Native task timeouts remain authoritative,
rather than killing a CLI and risking orphan containers. Oracle and nop
release FIFO separately; rewards and active/queue times are journaled
after each cell, making partial pairs resumable.

From an isolated current-main worktree, with the exact packages already
derived in the shared read-only task store:

```bash
export EVALLAB_STATE_JOURNAL=off
uv run --extra laminar python research/experiments/mimo-clean-v6/resume_controls.py \
  --package-root "$HOME/Developer/eval-lab" \
  --jobs-dir "$HOME/Developer/eval-lab-results/2026-10-15/mimo-clean-v6-resume/jobs" \
  --journal "$HOME/Developer/eval-lab-results/2026-10-15/mimo-clean-v6-resume/controls.jsonl" \
  --seconds 7200 --lock-wait-seconds 60
```

### Evidence retention and Docker cleanup

Native jobs, repeat snapshots, raw result/timing/cleanup JSONL, plans,
and the unbound-label audit are retained under
`~/Developer/eval-lab-results/2026-10-15/mimo-clean-v6-verify/`
(`raw-evidence.tar.gz`, `summary.json`, `docker-cleanup.json`).
The historical `000048` jobs remain under
`~/Developer/eval-lab-results/2026-10-14/repeat-oracle-fix/`.

Each of the four newly pulled complete images (`000245`, `000941`,
`001425`, `001891`) was removed after its task. The post-run audit confirms
all four absent, the interrupted `000048` image absent, all 12 protected
pre-existing MiMo images still present, and no remaining containers owned
by these jobs. Other writers' containers/images were not removed and the
Docker daemon was not restarted. The missing initial peak log caveat
above remains; a post-run cleanup audit does not prove a historical peak.
Operational spend for this milestone: **$0**.

