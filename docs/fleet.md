---
status: living
audience:
  - builder
  - operator
---

# Fleet lane supervision

`evallab fleet` starts explicit-route OMP lanes and records their observed state.
It does not grant execution, spend, publication, or merge approval.

## Start the supervisor

Run these commands from the owned Eval Lab worktree:

```bash
uv run evallab fleet watch
```

Run the supervisor in a separate background terminal process for unattended work.
The default interval is 10 seconds. `--interval` accepts values through 15 seconds.
A machine-wide file lock refuses a second supervisor, including one with another state directory.
No LaunchAgent is installed automatically.

The default state directory is `~/.local/state/evallab-fleet/`.
`XDG_STATE_HOME` changes its parent directory.
Place `--state-dir DIR` before the fleet subcommand to select another database.
The supervisor and launch commands must use the same directory.

## Start a lane

```bash
uv run evallab fleet lane start /absolute/program lane-a \
  --route openai-codex/gpt-6.1-sol \
  --brief /absolute/brief.md \
  --worktree /absolute/owned-worktree
uv run evallab fleet status
uv run evallab fleet status --json
uv run evallab fleet stop lane-a
```

The lane name is unique within the state database.
Use a fresh name for another attempt. Fleet preserves previous evidence.
The brief must include the lane's acceptance conditions and repository delivery rules.
The operator must start the supervisor before launching work.

Fleet reuses the insights OMP launcher. It pins every configured model role,
disables fallback, removes the native `task` tool, and disconnects standard input.
Each worker starts a new process group. The database records its PID, PGID, and command.
A registered worktree sets the worker's current directory.
Without one, the worker starts in the lane directory.

Fleet refuses every non-`openai-codex` route without a nonempty `--approval` string.
The string must quote a current applicable approval. Fleet records it; it does not create approval.
A subscription route still consumes quota. An old approval does not authorize a new specification.

## Quota configuration

Create `config.json` in the state directory when another threshold is required:

```json
{
  "thresholds": {
    "openai-codex": 75,
    "anthropic": 80
  }
}
```

The default threshold is 75% for `openai-codex`.
Fleet reuses the insights usage parser and requests `omp usage --json` every 180 seconds.
It uses the highest reported usage for the provider and the selected model family.
Missing usage for a configured provider blocks launches and pauses its running lanes.
A threshold pauses each affected process group with `SIGSTOP`.
Usage below the threshold permits `SIGCONT` on a later tick.
An unconfigured provider has no quota threshold; paid-route approval remains required.

## Route evidence and recovery

Fleet scans every `*.jsonl` below the lane directory, not only the lead session.
This includes helper session directories and native child histories.
OMP stores native child histories below the lead's artifact directory:

```text
lane/session/<timestamp>_<session-id>.jsonl
lane/session/<timestamp>_<session-id>/<child-id>.jsonl
lane/helpers/<helper>/session/<timestamp>_<session-id>.jsonl
```

Fleet reuses the insights attestation parser, including error turns for safety checks.
It stores journal offsets with per-model cumulative turn counts.
It waits for the newline before it processes a partial final record.
It resumes those offsets when the supervisor restarts.
`spend.jsonl` contains cumulative snapshots, not additive charges or cost estimates.
Do not sum snapshots to calculate turn counts.

An off-route assistant turn or fallback event stops the lane with `SIGKILL`.
A malformed complete record or an assistant turn without route metadata also stops the lane.
Fleet checks saved process ownership before signalling.
It includes child process groups and helper processes with session paths below the lane.
The reason appears in the database, the event history, and the lane's `STATUS.md`.
The observation delay is normally one tick. Sleep or a stopped supervisor prevents enforcement.

A dead worker without `RESULT.md` enters recovery.
Fleet uses the insights staggered retry delay, then resumes the latest lead JSONL with `--resume PATH`.
The recovery prompt names the original brief and `STATUS.md`.
If startup fails before a lead journal exists, Fleet starts the original brief again.
The default limit is 2 resumes. `--max-resumes N` changes it for a new lane.
Fleet records each launch and resume in the SQLite event history.

## Power and alerts

The supervisor holds `caffeinate -is` while any lane needs supervision.
The assertion follows the supervisor PID. It releases when the supervisor exits.
Check the actual assertion with `pmset -g assertions`.
A clock gap over 45 seconds writes an `ALERT` to each active lane's `STATUS.md`
and its program's `ALERTS.md`. A backwards clock change also raises an alert.
Fleet saves `pmset -g log` output as supporting evidence.
Collection stops after 2 seconds. The receipt marks incomplete output.
An incomplete power log does not disable route supervision.
A clock gap proves an interruption; it does not alone prove laptop sleep.

`status.json` contains the last heartbeat and lane snapshots.
`fleet status` reports the heartbeat age and each lane's observed state.
A stale heartbeat is not healthy supervision.

## Completion verification

Worker exit does not mean success. Fleet requires `RESULT.md`, served-route evidence,
a clean registered worktree, and a pushed branch or a head bound to a merged PR.
For each PR citation, it checks GitHub's state, exact head, and reported checks.
Both `quality-required` and `typecheck-required` must succeed. Every reported check must succeed.
The exact PR head must appear in the citation's `RESULT.md` paragraph.
Cite PRs with `#NUMBER`, `PR NUMBER`, or GitHub URLs.
Fleet currently requires delivery PRs to be merged before it marks the lane verified.
The retired branch can be absent after a guarded merge when its head matches the merged PR.

Fleet checks completion in a separate thread so GitHub requests do not block route scans.
It records `done-verified` or `done-unverified`, with reasons and GitHub responses.
This verifies delivery metadata, not scientific arithmetic or the truth of every prose claim.

Use this optional hook after the lane verifier becomes available:

```json
{
  "thresholds": {"openai-codex": 75},
  "verify_command": ["evallab", "verify-lane"]
}
```

Fleet appends the absolute lane directory. A nonzero exit prevents verified completion.
Set the hook's fixed arguments to match the installed verifier's CLI contract.

## LaunchAgent deployment

Use `scripts/ops/launchd/com.petermakhnatch.evallab.fleet.plist` as a template.
Replace its worktree, local binary directory, and log directory placeholders.
The binary directory must contain `omp`; the configured PATH must also resolve `gh`.
Keep the existing repository LaunchAgent installation pattern.
Installing or loading a shared LaunchAgent requires separate operator authorization.
The template uses `KeepAlive` and starts disabled; this change does not load it.

## Verification

`tests/test_fleet.py` covers nested route violations, quota transitions, recovery limits,
clock-gap alerts, process ownership, detached helpers, and false completion claims.
The process acceptance run must also launch the actual CLI with a fake OMP executable.
It must measure route-stop latency, kill and resume a worker, and inspect macOS assertions.
A real authorized lane must reach its guarded merged PR before an end-to-end delivery claim.
