---
status: living
audience:
  - builder
  - operator
---

# Ship a pull request

`evallab ship <pr>` waits for exact-head CI, merges the PR, proves main ancestry, and retires the topic branch.
It does not rebase, resolve conflicts, enable auto-merge, bypass protection, or execute model calls.
It uses Git, GitHub CLI authentication, `lsof`, and macOS `launchctl`.
Fork PRs and bases other than `main` stop without a merge.

## Run

Run from a stable checkout and runtime outside the worktree you want to remove.
A running process in that worktree blocks cleanup, including the ship process itself.
The ship entry point avoids the analysis stack and works from an installed wheel.
Rebase the topic onto current `main` before delivery. Ship requires the current base commit in the PR's ancestry.

```bash
uv run evallab ship 123 --smoke 'uv run --locked evallab --version'
```

Use `--repo-root /path/to/stable/checkout` when the command starts outside that checkout.
The stable checkout must share the topic worktree's Git repository.
The smoke runs in a new detached checkout of the merge commit.
Use a command that leaves no untracked files. Ignored dependency caches can remain.
Retirement also accepts tool caches (`.ruff_cache`, `.pytest_cache`, `__pycache__`
and similar) in any directory; any other ignored file still blocks it.
A failed smoke stops retirement. Fix its cause and repeat the same command.

The command prints pending, successful, and failed check names.
Right after a push GitHub reports mergeability as `UNKNOWN`; ship asks again a few
times before treating the PR as unmergeable.
It reads required names from branch protection through `gh api`.
If protection is unavailable, it reads the required-check table in `agents/CHECKS.md` on the base.
Every reported check must succeed, not just the required checks.
Skipped, neutral, cancelled, and unknown results block delivery.
Failed checks include failing workflow logs and test names when GitHub supplies them.
The command reruns failed workflow jobs once per head by default.
Use `--no-rerun-failed-once` to stop on the first failure.
Use `--timeout 1800` and `--poll 15` to set the wait limit and poll interval in seconds.

## Path gates

`agents/ship-gates.toml` maps case-sensitive path globs to command argument arrays.
Ship reads existing gates from the fetched base.
A PR cannot remove or weaken its own existing gate by editing the configuration.
New gate names from the candidate also run before their registration merges.
The base definition wins whenever both policies contain the same name.

```toml
[[gate]]
name = "line"
paths = ["src/evallab/line/**", "src/evallab/autoreview_monitor.py"]
command = ["make", "line-gate"]
```

Ship runs each selected gate in a detached checkout of the exact PR head.
It sets `EVALLAB_SHIP_HEAD`, `EVALLAB_SHIP_BASE`, and `EVALLAB_SHIP_PR`.
Exit `0` passes. Exit `1` blocks. Other exit codes report an error and block.
A head or base change invalidates the delivery attempt.
The author must refresh combined evidence before a new attempt.

The registered `line` gate runs `make line-gate` for decision-sensitive paths.
Its scope includes L0 scanners, normalization, task policy, replay inputs, and gate controls.
Documentation-only and unrelated research PRs do not select this gate.
A replay regression or missing private data blocks merge.
See [the line contract](line.md#sensitive-decision-paths) for coverage and receipts.

## Receipts and recovery

`~/.local/state/evallab-wf/ship/<pr>.json` stores the PR snapshot, completed steps, commands, exit codes, and log paths.
Command logs live beside the receipt. A per-PR file lock prevents concurrent writers.
The receipt uses atomic replacement and a flushed file before each side effect.
Launchd output can contain credentials, so receipts retain its reference verdict without raw environment values.
Use `--state-dir` for an isolated receipt directory, including tests.

Repeat the command after an interruption. Ship checks remote PR state before any merge.
A merged PR never receives another merge request.
If GitHub still reports the same PR open, ship refreshes CI evidence before another guarded merge request.
GitHub permits only one merge for a PR. A late response cannot create a second merge.
A moved head requires a fresh receipt directory after the author checks the new diff.
A second failed CI attempt stops. Repeating the command does not grant another rerun.
Gate and smoke checkouts use stable paths beside the receipt.
After interruption, ship reuses a clean, inactive checkout with the recorded commit.
It preserves dirty, changed, unknown, or active checkouts instead of deleting them.
An interrupted gate or smoke can execute again. Make those commands safe to repeat.

## Retirement safety

Before the merge and before cleanup, ship checks every matching registered worktree.
It refuses dirty files, unique ignored evidence, changed or unpushed commits, Git operations, locks, running processes, and loaded launchd references.
It never removes the primary checkout or an integration branch.
Only known dependency and test caches count as disposable ignored files.
Unknown ignored paths remain intact under the [retention contract](GENERATED-CACHE-POLICY.md).
All targets must pass before the first deletion.
Native worktree removal uses no force option.
Local ref deletion matches the recorded PR head, including squash-merged branches.
Remote ref deletion uses an exact-head lease if GitHub has not already removed it.
A concurrent change can still stop cleanup after an earlier target completes. Repeat the command after resolving the cause.

GitHub CLI can return an error after its remote merge succeeds.
Ship judges the merge from the PR's `MERGED` state and merge commit, not that exit code.
It fetches `origin/main` and requires the merge commit to be its ancestor.
Completion also requires the local branch, remote branch, and topic worktree to be absent.

## Verification

`uv run pytest tests/test_ship.py` uses a fake GitHub CLI and real disposable Git repositories.
It covers head movement, failing test names, one rerun, pending checks, gate exits, remote merge reconciliation, resume, and unsafe cleanup.
A process-kill scenario proves smoke recovery removes the resumed checkout without merging twice.
No test requires network access or host authentication.
The earlier F4 Temporal prototype remains research evidence under `research/experiments/on-mergetrain-workflow-20261102/`.
Ship reuses its remote-state reconciliation pattern without a server or automatic source edits.
