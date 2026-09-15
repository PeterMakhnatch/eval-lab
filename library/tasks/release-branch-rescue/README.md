# `peter/release-branch-rescue`

This hand-authored task exercises Git reflog recovery, integration of divergent
history, preservation of release provenance, and branch tracking configuration.
See [instruction.md](instruction.md) for the agent-visible contract.

## Provenance and placement

Originally imported, including `controls/`, from the recorded source
`PeterMakhnatch/rl-envs` commit `8f9b1fc12582f0d83190f0063cf7f3ee91924b3b`.
The preserved 2026-09-14 repair isolates pytest and adds a shadow-module control.
HAR-51 (2026-09-15) additionally removes submitted Git configuration from the
verifier execution path. This is not a verbatim copy of the original source.
The original `peter/` name and author are retained: the `local-lab/` example is
not a mandated namespace. `agents/STRUCTURE.md` assigns lab-authored tasks to
`library/tasks/` (lines 69–75); it does not require model calibration or an
`experimental/` subdirectory. This is library inclusion, not registry admission.

**Source provenance (2026-09-14): recorded upstream revision not independently verified.**
The historical control receipt below records the original local task path and
digest `2a554c16d9830630416d50a7cae23e1ecdc6308d8a8d0eaa1bf67a7b43b80cf3`,
but contains no license grant or binding to the claimed Git commit. The original
`~/Developer/rl-envs` checkout is absent. Both the public
[pinned source tree](https://api.github.com/repos/PeterMakhnatch/rl-envs/git/trees/8f9b1fc12582f0d83190f0063cf7f3ee91924b3b?recursive=1)
and [pinned LICENSE](https://raw.githubusercontent.com/PeterMakhnatch/rl-envs/8f9b1fc12582f0d83190f0063cf7f3ee91924b3b/LICENSE)
were unavailable (404); authenticated GitHub repository/tree lookup was also
unavailable. A 404 does not establish wrongdoing or a license violation.
The task metadata records Peter Makhnatch as author; that is a declaration,
not an independent upstream/license attestation. This repository copy retains
the source limitation. No third-party license grant, registry admission or
external dataset publication follows from a code repair.

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
Before any repository-aware Git command, the verifier constructs its own temporary
repository view. Only regular worktree files, Git objects, refs, HEAD, packed refs,
and index data are copied. Submitted configuration is parsed as data with
`git config --no-includes --file ...` from a trusted directory and clean environment.
Only `branch.main.remote`, `branch.main.merge`, `remote.origin.url`, and
`remote.origin.fetch` are written into newly generated configuration.
Submitted filters, hooks, includes, attributes overrides, repository redirects,
and other configuration never become verifier runtime configuration.
Git status/diff checks and content reads use this owned view, not the submission.
This is removal of execution authority, not a blacklist of filter spellings.
It independently reconstructs A, L2, R2, and the exact integrated tree in a fresh
temporary trusted repository. Unlike the nginx task, this task has no fresh
secret or randomized HTTP probes: its ground truth is reconstructed Git object
identity and tree state.

Pytest starts from trusted `/tests` with isolated Python (`-I`), a clean
environment, explicit empty configuration, no conftest discovery, importlib
test loading, and disabled plugin autoload. Only the installed CTRF plugin is
explicitly enabled. Submitted `pytest.py`, pytest configuration, and plugins
must not replace or alter the grader.

A preflight gate rejects missing/symlinked repository data, special filesystem
entries, replacement refs, grafts, shallow history, object alternates and corrupt
object databases. Inert submitted hook/filter configuration is not itself a
failing answer: it is never executed by the grader. Checks preserve upstream refs and
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
| `controls/game-shadow-pytest.sh` | 0.0 | Submitted `pytest.py` exits zero without recovery |
| `controls/game-clean-filter.sh` | 0.0 | Configured clean filter plus an uncommitted line |
| `controls/game-process-filter.sh` | 0.0 | Process-filter protocol plus the same uncommitted line |
| `controls/game-worktree-redirect.sh` | 0.0 | `core.worktree` redirect hides an untracked file behind a staged pristine copy |

The schema-2 matrix at
`research/experiments/release-branch-rescue-local-controls.json` binds the current
package/verifier and explicit oracle solution overrides. All controls are
container-only probes that print `HAR51_SETUP_COMPLETE <name>` once the bad
action is fully staged; a rejection counts only when that marker is present in
the agent log.

Measured before/after on 2026-09-15 (native Eval Lab matrices, receipts under
this worktree's `runs/.executor/`):

- **Old verifier (verifier `sha256:c415b3e1…`) false pass, demonstrated:**
  `game-worktree-redirect` with `HAR51_SETUP_COMPLETE` present earned reward
  **1.0** — submitted `core.worktree` redirected the verifier's status/diff
  checks to a staged pristine copy while the real worktree carried `junk.txt`.
  Receipt `01M2KK0FKZDB69NWRPK9667ERF`.
- **Filter masking did not reproduce:** the old verifier already rejected both
  filter controls (receipt `01M2KJQ3QT34F6829C68F1JRFC`); on this Git, the old
  command set (`status --porcelain -uall --ignored`, `diff-files --quiet`,
  `diff-index --quiet`) never invokes conversion, so neither masking nor the
  filter command executed. Filter execution is a *latent* class: a host probe
  showed `git diff HEAD` executes a configured clean filter, so any future
  verifier command that materializes content would have executed submitted
  code inside the verifier container.
- **Repaired verifier:** all eight final runs matched expectation — oracle 1,
  nop 0, both alternatives 1, clean/process-filter 0, worktree-redirect 0,
  pytest-shadow 0. Receipt `01M2KKAG84QCY4XGTQ98BYK0S7`, package
  `sha256:e61ad47b…`, verifier `sha256:f5bfa0ea…`; every rejected control
  printed `HAR51_SETUP_COMPLETE` before grading.

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
warning without affecting its Harbor reward. Those matrix package digests include
the original added documentation, not the subsequent verifier-isolation repair.
The historical registry audit refreshed the deterministic registration inventory
without creating or promoting registry records.

Historical source-task controls passed 10/10 (zero mismatches or infrastructure
errors) in the primary checkout's ignored receipt
`runs/batch-ctl-release-branch-rescu-20260910-180925-a76f/receipt.json`.
That is provenance for the source task, not a claim that all mutants were rerun
from this library package. No run directories are promoted to research evidence.

Current repair verification is recorded outside this task package in
`research/experiments/README.md` and the retained native matrix receipts, which
bind exact package, verifier and selected-solution digests. Historical oracle/nop
receipts do not qualify changed verifier bytes. An expected-zero control counts
only when setup succeeds and the real separate verifier rejects the submission.

**Limits:** These are validity controls, not model-capability evidence. No model
trials were run; difficulty is unknown. The inherited `medium` metadata is an
uncalibrated author estimate, not an empirical result. No registration, promotion,
external dataset publication, paid model call, or cloud execution is claimed.

HAR-51 addresses Git configuration callbacks only. Native Git object/index
parsers and resource-exhaustion attacks are not comprehensively qualified.
The separate **nginx submitted-module execution boundary remains unresolved**;
this change does not qualify or merge the whole held PR415.
