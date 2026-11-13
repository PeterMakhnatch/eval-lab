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

## Publish without extra CI cycles

Create the PR once, with its complete title and body in UTF-8 files:

```bash
uv run evallab ship open --title-file "$TITLE_FILE" --body-file "$BODY_FILE"
```

The helper uses the current origin topic branch and targets `main`.
Push the intended head first. The helper checks the live remote head.
It refuses an empty title, an empty body, a placeholder body, or an existing open PR.
Headings, HTML comments, and unchecked template items do not count as a complete body.
The helper never edits an existing PR title or body.

Add later evidence with a comment, including after merge:

```bash
uv run evallab ship comment 123 --body-file "$EVIDENCE_FILE"
```

Before each later push, check whether the patch changes:

```bash
uv run evallab ship check-push 123
```

Omit the PR number to find the open PR for the current origin branch.
No open PR passes. Ambiguous PRs and GitHub errors stop the check.
The helper fetches current `main` and the exact PR head.
It compares each aggregate merge-base-to-head diff with `git patch-id --stable`.
An unchanged patch stops the push check, including an unnecessary rebase onto newer `main`.
A changed patch passes. The helper does not push.

Only a real conflict or a needed commit justifies an explicit exception:

```bash
uv run evallab ship check-push 123 --needs-base "$BASE_SHA" --reason "The change needs the new base API."
```

The SHA must name a commit reachable from local HEAD and fetched `main`.
The reason must contain text. An identical local and PR head still fails.
Stable patch IDs ignore whitespace, including Python indentation on changed lines.
Use the explicit exception for a needed indentation change, with its relevant base SHA and reason.
This behavior follows Git's stable patch-ID contract; it is not a semantic code comparison.

Direct `gh` and `git push` commands can bypass these helpers.
The helpers do not change workflow triggers or branch protection.
Run `check-push` explicitly until the local prepush checkpoint includes the coordinated hook.


## Run

Run from a stable checkout and runtime outside the worktree you want to remove.
A running process in that worktree blocks cleanup, including the ship process itself.
The ship entry point avoids the analysis stack and works from an installed wheel.
Ship does not require the current `main` commit in the PR head's ancestry.
New commits on `main` do not require a rebase or another PR CI cycle.
Rebase only for a real conflict or a required dependency.
Ship still requires GitHub to report the exact head as mergeable.

```bash
uv run evallab ship 123 --smoke 'uv run --locked evallab --version'
```

Run preflight immediately after creating a PR:

```bash
uv run evallab ship 123 --preflight-only
```

Preflight exits `0` when delivery inputs and cleanup prerequisites pass.
Preflight writes a separate `<pr>-preflight.json` receipt.
It does not bind a later delivery to the current PR head or replace a delivery receipt.
Repair commands retain `--preflight-only` when the original command uses it.
It exits `1` with a repair command when a prerequisite fails.
It does not wait for CI, run gates, merge, or remove worktrees.
Normal ship runs the same preflight before its CI wait.
The final cleanup checks remain in place.

Preflight checks active topic processes and every unknown ignored path.
It preserves unknown ignored bytes with explicit move commands, not deletion.
The generated `$TOPIC/tools/mimoagent-harbor/.venv` environment is not automatically disposable.
Here, `$TOPIC` is the topic worktree path.
A lockfile cannot prove that its local source or installed files contain no unique bytes.
Move that environment outside the worktree when preflight names it.

The delivery checkout must match fetched `origin/main` for ship code and gate controls:
`src/evallab/ship`, `src/evallab/entry.py`, `agents/ship-gates.toml`, and `agents/CHECKS.md`.
Ship also checks runtime source bytes, including an installed package from another checkout.
Unrelated commits do not make the delivery checkout stale.
A stale checkout refusal gives a fresh detached-checkout command.
That command preserves local changes instead of resetting them.

`--timeout` bounds the CI wait and each gate or smoke command separately.
Its default is `1800` seconds. Ordinary Git and GitHub commands retain a `300`-second limit.
Use a larger verification budget for a full DEV replay. Every gate must still pass.

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
Ship binds each Actions run to the exact PR, head branch, and target base branch.
A retarget, including a retarget away and back, requires a new PR event run.
Rerunning an older workflow does not refresh its event evidence.
Run creation must follow the latest retarget timestamp. Ambiguous same-second evidence blocks delivery.
Ship records the tested merge commit from checkout logs and verifies its parents.
The tested base must remain an ancestor of current `main`; it need not equal current `main`.
Missing run association or tested merge evidence blocks delivery.
Failed-jobs reruns can reuse checkout evidence from earlier attempts of the same exact-head PR event.
Ship checks every recorded merge checkout, not only the latest attempt.
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
Gate receipts bind the exact head and fetched current `main`, not only the head.
Main advancement invalidates local gate receipts, but does not invalidate same-base PR CI.
Main movement during the final gate attempt blocks merge. Repeat ship without rebasing.

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
Ship always refreshes remote CI evidence for an open PR.
A passed local gate can resume only when its exact head and current base remain unchanged.
Older head-only gate receipts do not qualify for reuse.
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
It covers main advancement without rebasing, retargets, stale merge evidence, head movement, checks, gates, recovery, and unsafe cleanup.
A process-kill scenario proves smoke recovery removes the resumed checkout without merging twice.
No test requires network access or host authentication.
The earlier F4 Temporal prototype remains research evidence under `research/experiments/on-mergetrain-workflow-20261102/`.
Ship reuses its remote-state reconciliation pattern without a server or automatic source edits.
