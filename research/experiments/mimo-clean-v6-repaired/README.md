# mimo-clean-v6: retained EnvCheck repairs

Date: 2026-10-15. Spend: **$0**. Local CPU derivation and local Docker
controls only; no model inference, Modal, or other paid compute.

**Port milestone, not full verification acceptance:** 16/16 ports are
lineage-valid; 0 are non-portable. Only **2/16** (000561 and 001478) have
completed oracle `[1,1,1]`, nop `[0,0,0]`, and all their retained killed
mutants. The other **14 tasks are explicitly `not-run`**, with
`kill-replay:not-run`; 28 control trials and 24 mutant replays remain.
The parent authorized shipping this construction/completed-control
milestone rather than waiting indefinitely in the shared Docker queue.
No missing measurement is passed or inferred from an old/v5 digest.

## Measured outcomes

| task | source disposition | oracle x3 | nop x3 | killed replay | active trial seconds | elapsed seconds, including FIFO |
|---|---|---|---|---|---|---|
| 000561 | full | `[1,1,1]` | `[0,0,0]` | 2/2 killed | 4557.617 | 13858.708 |
| 001478 | full; training hold retained | `[1,1,1]` | `[0,0,0]` | 2/2 killed | 1124.091 | 3193.112 |
| remaining 14 | 1 full, 13 partial | not-run | not-run | 24 not-run | 0 | see `results.csv` |

Eight native Harbor trials completed: four repeat-3 controls and four
ordinary task-correctness mutant replays, totaling 16 verifier
invocations. All executed checks passed. Active trial/pull time was
5681.708 s; the whole campaign command occupied 17178.630 s, including
FIFO waits and cleanup dispatch. The next task's queued oracle command
(000511) was cancelled **before acquiring a slot or starting a trial**
after 81.755 s when the parent selected the milestone boundary.
`campaign-stop.json` retains that exact queue snapshot and disposition;
this is not a failed task or a failed verifier.

## Scope and immutable lineage

This receipt ports the 16 final variants in
[`envcheck-overnight-2026-10-13/final-results.json`](../envcheck-overnight-2026-10-13/final-results.json)
to the corresponding `final_digest` in
[`mimo-clean-v6/manifest.csv`](../mimo-clean-v6/manifest.csv), as read at
commit `751a7e462aa98622e49b6ac6b265c5992bbb0937`. The input manifest's SHA-256
is retained in `port-plan.json` below. The three source `repaired` tasks are
000561, 001478, and 000511; the other 13 source variants are `partial`.
The four source tasks with no final derived variant (001891, 002742,
000390, and 001219) are not part of this 16-task port set.

**All 16 variants were portable; none required a layout rewrite or new
assertion.** The retained old lineage records use `verifier-repair@1` and
change exactly `tests/test.patch` and `tests/test_command.sh`. For every
task, both files in the v6 parent are byte-identical to the old repair's
preimages: 32/32 exact preimage matches. Although v6's separate-verifier
harness changed, it still consumes those two files. No verifier-bundle
unpacking, harness change, or base-manifest edit was necessary.

The port procedure verifies the retained old record and repair-receipt
SHA-256 values, validates the original `RepairProposal`, and calls
`evallab.verifier_repair.derive_repair_variant` with the v6 package as its
parent. Its injected derivation function delegates to
`evallab.task_variants.derive_task`, retaining the original transform and
adding the old variant, old record/receipt hashes, and v6 parent digest to
`inputs`. Each new record's parent is **the v6 final package digest**, not
the old v5 digest. The generated contents of both changed files are
asserted byte-identical to the old repair (32/32 matches), and
`evallab.task_variants.verify` returned no lineage errors for all 16.
Existing package directories and lineage records are never overwritten.

[`ports.csv`](ports.csv) is the repaired-variant pointer table. The
`repaired_variant` column pins the ported digest only within this receipt; it
neither replaces nor reclassifies the original v6 manifest row.
Content-addressed packages remain in the shared store:

```text
~/Developer/eval-lab/derived/task-store/variants/
  mimo-v2.6-rl__format-code-task-<task>/<ported-digest12>/
```

Tasks **001478 and 001671 retain their prior training-admission holds**
for an answer-visible installed dependency. These are carried exclusion
reasons, not newly measured v6 findings. Both are built and gradable:
001478 was verified here; 001671 remains not-run because of the shared
queue/milestone stop, **not** because exclusion permits skipping it.

## Verification protocol and interpretation

Each oracle and nop control uses one local Docker Harbor trial with the
following native repeat-verifier arguments:

```text
--verifier evallab.harbor_repeat_verifier:RepeatVerifier
--verifier-kwarg repeat_n=3
```

These are three verifier invocations on one submitted snapshot, **not**
three independent agent attempts. The existing model-free
`Executor.execute_direct` path receives
`RunRequest(verifier_repeat_n=3, concurrency=1, attempts=1,
environment="docker")`. A control is accepted only when Harbor completes
without an exception, ordinary control evidence is present, and all three
`verifier/stability.json` runs contain finite rewards without errors:
oracle must be `[1,1,1]`, nop `[0,0,0]`. `verifier/repeat/{0,1,2}/`
preserves each invocation's artifacts. A zero reward with a harness or
oracle-script failure is infrastructure failure, not a killed mutant.

The source receipts identify **28 previously killed mutants** across
these tasks. Replays reuse their exact retained `scripts/survivor-*.sh`
bytes, pinned by SHA-256, rather than generating mutations or invoking a
model. The existing `staged_matrix_request` solution-override mechanism
stages each script as the oracle solution for the new repaired package;
`Executor.execute_direct` and `matrix_run_outcome` execute and classify it
with expected reward zero. Replay is one verifier invocation per mutant;
it is not an additional repeat-stability claim. An omitted replay is
explicitly `kill-replay:not-run`, never a pass.

Every executed trial, and the original per-task image cleanup, used the
host-wide FIFO:

```text
uv run --extra laminar evallab docker-lock run <label> -- \
  uv run --extra laminar python <retained-run-procedure> <task> <role>
```

The original driver did not bound FIFO waiting; it was stopped at the
parent's milestone boundary. The retained resumable procedure now uses
`docker-lock run --slots 1 --timeout 120`, records queue timeout as
`not-run:queue-timeout`, and never classifies an unstarted trial as a task
failure. It skips immutable completed trial receipts. This corrected
procedure has **not** been used to run additional controls in this
milestone.

The run environment sets `EVALLAB_STATE_JOURNAL=off`. There is at most one
active trial in this lane, with no more than the trial and its egress
sidecar. The two explicitly pulled task images were removed using
non-forced `docker rmi`; all 243 initial image IDs were protected by the
cleanup guard. See the cleanup incident below; no broad cleanup is safe
on a shared daemon. No daemon restart or forced image removal occurred.

[`results.csv`](results.csv) reports observed control arrays, kill-replay
result, and per-task timings. `wall_seconds` sums active trial durations,
including image pulls inside the slot. `elapsed_wall_seconds` spans the
first queue entry through cleanup for completed tasks. The recorded
`fifo_queue_wait_seconds` covers trial slots (and the cancelled next
slot); it does not include the uninstrumented original cleanup-slot
waits. Whole-command elapsed time includes those waits. Empty arrays
with `not-run` status are not zero-valued results or passes.

## Shared-image cleanup incident

The temporary runner's first cleanup implementation incorrectly treated
a global before/after image difference as ownership evidence. On this
shared daemon it also selected unrelated images. In addition to removing
our two explicitly pulled task images, non-forced `docker rmi` removed
these **two non-baseline, unowned images**:

```text
xiaomimimo/mimo-v2.6-rl-oss@sha256:1d1b433a00baa9203ea86bf4697e820e0e2367fa0de8d9186f80fb6d3c2fbc11
xiaomimimo/mimo-v2.6-rl-oss@sha256:73c51c6bcc159c75a12110d77e2a379dcbb68af16798259578598546132f5ca2
```

Other unrelated candidates were already absent. The initial 243 image
IDs were protected; no baseline image, container, package, or user data
was removed by this cleanup. The raw cleanup receipts are preserved.
The corrected retained procedure removes **only explicit owned pull
IDs**, never a daemon-wide difference, and does not remove these two
unowned images after restoration.

A narrowly scoped restoration command attempted to acquire the shared
FIFO with `--slots 1 --timeout 120`; it timed out with exit 124 before
either `docker pull <exact-repo@sha256>` command ran. **Restoration is
pending, not successful.** `pending-restoration-attempt-1.json` records
both complete refs, the exact wrapper/pull commands, timeout, and error.
`owned-image-cleanup-confirmation.json` separately confirms that our two
explicitly pulled task images are absent.

## Evidence custody and reproduction

Raw evidence is retained outside the disposable worktree:

```text
~/Developer/eval-lab-results/2026-10-15/mimo-clean-v6-repaired/
  port-plan.json                 # source hashes, parents, packages, killed IDs
  custody.json                   # input/procedure hashes, derivation assertions
  lineage/<task-slug>/*.json      # complete new derive_task records
  procedures/                    # exact local derivation/execution procedures
  matrices/                      # pinned mutant replay matrices
  dispatch/                      # per-command FIFO acquisition/execution logs
  trials/                        # one structured outcome per actual trial
  jobs/                          # complete copied native Harbor jobs
  worktree-runtime/runs/         # all 674 owned runtime files, 559728893 bytes
  runtime-sha256.json            # verified surviving copy of each runtime file
  image-pulls/                   # explicit pulls and immutable image IDs
  cleanup/                       # per-task docker rmi outcomes
  docker-images-before.txt       # pre-campaign Docker inventory
  summary.json                   # measured/unrun counts and exact per-task data
  campaign-stop.json             # cancelled, never-started FIFO entry
  receipt-consistency.json       # CSV/digest/native outcome binding checks
```

`ports.csv` pins each retained new lineage record by SHA-256; its inline
changes can materialize the package against the manifest-pinned v6
parent using `evallab.task_variants.materialize`. `port-plan.json` also
pins old records and repair receipts. The retained procedures use only
repository APIs, local files, and Docker; no paid replay is necessary.
The original derivation procedure refuses existing record/package paths;
do not rerun it over this evidence. For the remaining measurements, use
`procedures/mimo-v6-repaired-resume-bounded.py` from a fresh owned
eval-lab worktree with `uv run --extra laminar python <procedure>`. It
uses that worktree's `cwd`, skips completed immutable trial receipts,
uses bounded FIFO acquisition, and leaves missing measurements unrun on
a timeout. It was retained but not exercised in this milestone. A later
measurement receipt must be a new snapshot rather than rewriting this
tracked result table.

## Honest limits

- `full` and `partial` preserve **the source repair disposition**. A
  passing control or killed-mutant replay does not promote a partial
  repair to a full repair.
- The source study had 83 measured survivors before these final repairs;
  28 were killed and **55 remain** across the 13 partial tasks. Equivalent
  and out-of-scope mutations remain; no new mutation search or equivalence
  adjudication is claimed by this port.
- Even the three source full repairs mean only that their previously
  measured survivor set was eliminated. They are not proof that all
  possible wrong implementations are rejected.
- Repeat-verifier stability, oracle solvability, mutation discrimination,
  and training eligibility are distinct. This receipt does not erase
  the two training exclusions, certify the entire v6 fleet, or alter the
  independent base-v6 verification lane's manifest/results.
