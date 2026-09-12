# `peter/release-branch-rescue`

This hand-authored task exercises Git reflog recovery, integration of divergent
history, preservation of release provenance, and branch tracking configuration.
See [instruction.md](instruction.md) for the unchanged agent-visible contract.

## Provenance and placement

Imported verbatim, including `controls/`, from `PeterMakhnatch/rl-envs` commit
`8f9b1fc12582f0d83190f0063cf7f3ee91924b3b`; this README is the only added task file.
The original `peter/` name and author are retained: the `local-lab/` example is
not a mandated namespace. `agents/STRUCTURE.md` assigns lab-authored tasks to
`library/tasks/` (lines 69–75); it does not require model calibration or an
`experimental/` subdirectory. This is library inclusion, not registry admission.

## Environment and fixture

Ubuntu 24.04 provides Git and standard tools, with one CPU, 1024 MB RAM, and a
600-second agent timeout. The local checkout is `/workspace/release`; its offline
bare upstream is `/srv/origin.git`. At baseline A, `v1` marks the release. Local
commits L1/L2 raised retries to 3 and added `eu-west`, but a hard reset followed
by detached HEAD left them reachable through reflog rather than a branch.
Tracking was removed. Meanwhile upstream commits R1/R2 increased timeout to 60
seconds and added a runbook latency-metrics check. Recover the original local
tip under `rescue/pre-reset`, integrate both sets of changes, and restore a clean
attached `main` tracking `origin/main`, without modifying upstream or `v1`.

The fixture disables automatic pruning to preserve the lost objects. The task
uses the supported `public` network baseline for local Docker Desktop, not an
enforced offline sandbox; image builds fetch packages. Tests, reference solution,
and controls are outside the environment build context. The build script is
removed from the final agent image after constructing the fixture.

## Verifier

`environment_mode = "separate"` builds a fresh verifier image from `tests/`.
Harbor transfers `/workspace/release` (including `.git`) and `/srv/origin.git`.
The verifier invokes its own `/usr/bin/git` with a restricted environment,
disabled hooks/fsmonitor/replacement objects and isolated global/system config.
It independently reconstructs A, L2, R2, and the exact integrated tree in a fresh
temporary trusted repository. Unlike the nginx task, this task has no fresh
secret or randomized HTTP probes: its ground truth is reconstructed Git object
identity and tree state.

A preflight gate rejects missing/symlinked repository roots, replacement refs,
grafts, shallow history, object alternates, config includes/fsmonitor, executable
workspace hooks, and corrupt object databases. Checks preserve upstream refs and
`v1`, bind `rescue/pre-reset` to the original L2, require attached `main` and
correct tracking/remote, require upstream ancestry, and inspect normal index
flags and a clean worktree/index with the exact expected tree. Both merge orders
and rebasing the recovered changes are accepted when those invariants hold.
A clean-looking tree alone, an ours-strategy merge, pushing upstream, or tagging
the new integration commit instead of the lost tip is insufficient.

Every pytest check must pass for reward 1; otherwise reward is 0. Outputs include
`/logs/verifier/reward.txt` and `ctrf.json`. These checks and controls are bounded
evidence, not a proof against all hostile repositories or reward gaming.

## Control matrix

| Control | Expected reward | Purpose |
|---|---:|---|
| Oracle (`solution/solve.sh`) | 1.0 | Complete reference recovery |
| Nop | 0.0 | Detached, reset starting state |
| `controls/alt-rebase.sh` | 1.0 | Valid rebase alternative |
| `controls/alt-reverse-merge.sh` | 1.0 | Valid opposite merge order |
| `controls/partial-no-tracking.sh` | 0.0 | Missing upstream tracking |
| `controls/partial-tag-merge-commit.sh` | 0.0 | Rescue tag names wrong commit |
| `controls/game-fake-git.sh` | 0.0 | Agent-side Git replacement |
| `controls/game-merge-s-ours.sh` | 0.0 | History without integrated content |
| `controls/game-push-upstream.sh` | 0.0 | Forbidden upstream mutation |
| `controls/game-reclone.sh` | 0.0 | Reclone loses unpublished work |

The committed schema-2 matrix at
`research/experiments/release-branch-rescue-local-controls.json` contains only
oracle/nop and validates against current main. Follow-up: after matrix solution
overrides merge, the `solution` field can run the included alternate and negative
control scripts; that field is deliberately absent from this matrix.

## Verification

On 2026-09-11, local Docker with Harbor **0.21.0** produced:

| Run name (under this worktree's `runs/`) | Actual reward |
|---|---:|
| `release-branch-rescue-lib-oracle-20260911` | 1.0 |
| `release-branch-rescue-lib-nop-20260911` | 0.0 |
| `release-branch-rescue-lib-matrix-oracle-20260911` | 1.0 |
| `release-branch-rescue-lib-matrix-nop-20260911` | 0.0 |

```bash
uv run evallab run --task library/tasks/release-branch-rescue --agent oracle --name release-branch-rescue-lib-oracle-20260911 --jobs-dir runs
uv run evallab run --task library/tasks/release-branch-rescue --agent nop --name release-branch-rescue-lib-nop-20260911 --jobs-dir runs
uv run evallab matrix research/experiments/release-branch-rescue-local-controls.json
```

The matrix invocation passed both expectations without extra CLI flags.
Subsequent runs used `EVALLAB_DERIVED_ROOT` set to this worktree's absolute
`derived/parquet` path; the first direct oracle reported a derived-root ownership
warning without affecting its Harbor reward. Final matrix package digests include
this added documentation; executable task and verifier bytes are unchanged.
Registry audit passed after refreshing the deterministic registration inventory,
without creating or promoting registry records.

Historical source-task controls passed 10/10 (zero mismatches or infrastructure
errors) in the primary checkout's ignored receipt
`runs/batch-ctl-release-branch-rescu-20260910-180925-a76f/receipt.json`.
That is provenance for the source task, not a claim that all mutants were rerun
from this library package. No run directories are promoted to research evidence.

**Limits:** These are validity controls, not model-capability evidence. No model
trials were run; difficulty is unknown. The inherited `medium` metadata is an
uncalibrated author estimate, not an empirical result. No registration, promotion,
publication, paid model call, or cloud execution is claimed.
