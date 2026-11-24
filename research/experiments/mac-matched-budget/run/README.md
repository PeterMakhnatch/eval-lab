# Holdout matched-budget pilot

**NOT APPROVED.** Do not start the pilot. Scripted seats show that a background process can issue orders after its seat ends.
Use Bash from the Eval Lab worktree. These paths refer to local files on Peter's Mac.

```bash
export PYTHONDONTWRITEBYTECODE=1
B="$HOME/Developer/bespoke-macbench-lab"
RUN=research/experiments/mac-matched-budget/run
PLAN=campaign/plans/mac-matched-pilot.toml
OUT="$HOME/Developer/muster-runs/day-2026-11-24/mac-run"
MATCHED="$HOME/Developer/eval-lab/.worktrees/fable-mac-matched/research/experiments/mac-matched-budget"
READS="$HOME/Developer/eval-lab/.worktrees/fable-mac-reads/research/experiments/mac-matched-budget"
STUDY4="$HOME/Developer/eval-lab-results/multiagent-campaign-2026-11-23/bespoke/study4-holdout"
mkdir -p /tmp/fable-day/macrun
```

## Commands in order

1. **Prepare.** Check the pinned inputs and write the two task copies and their manifest outside the repository.

   ```bash
   "$B/.venv/bin/python" "$RUN/matched_trial.py" prepare
   ```

2. **Run the $0 proofs.** Do not use `keys run`. The command runs Docker proofs in sequence.

   ```bash
   HOLDOUT_PROOF_MEMORY_MB=1024 "$B/.venv/bin/python" "$RUN/proofs.py"
   ```

3. **Check the plan.** Use a fake home and fake probes for a check without a provider call.

   ```bash
   "$B/.venv/bin/python" "$RUN/pilot_support.py" fake-check \
     --home "$(mktemp -d /tmp/fable-day/macrun/check.XXXXXX)/home" \
     --report "$OUT/proofs/pilot-plan-check.json"
   ```

4. **Start the paid pilot — NOT APPROVED.** Do not run this command today. The plan schedules 16 paired waves with at most 32 trial starts.
   After approval, set `command.env.MAC_MATCHED_APPROVAL` to `approved` and commit that change.
   Use the final plan hash for the whole campaign.
   The pilot requires funded OpenRouter and Daytona accounts. The balance check reads OpenRouter only.

   ```bash
   MAC_MATCHED_PAID=1 .venv/bin/python -m campaign run "$PLAN"
   ```

   The plan reserves $0.475 per trial. The total reserve is $15.20. Its separate recorded model cap is $4.80.
   The wave gate stops new allocations if its estimates exceed the total reserve. These estimates do not cap the invoices.
   Do not use `campaign fill`. The 16 waves already include the spare starts.

5. **Analyze.** Select only the matched pilot trial directories for the first command. Set `--arm` for every trial because both arms use the same board policy. The second command reads the historical Study 4 cohort. It does not accept the matched pilot's 2-arm layout.

   ```bash
   python3 "$MATCHED/analysis/matched_budget.py" \
     "$TEAM_TRIAL" "$SILENT_TRIAL" --arm "$TEAM_TRIAL=team" --arm "$SILENT_TRIAL=silent" --json &&
   python3 "$READS/analysis/holdout_reads.py" --root "$STUDY4" --json
   ```

   For the full pilot, pass all measured trial directories and their labels. Keep the wave ID to pair results. A missing score is unmeasured, not zero. The `game_call` rows in each trial's `board-events.jsonl` separate accepted world operations from reads and messages.
   Sum `world_ops` and count each true `replace_cleared` as one queue operation. An accepted order does not prove completion.

6. **Check recorded spend.** Keep proxy list-price spend separate from the actual provider charge and the sandbox estimate.

   ```bash
   .venv/bin/python -m campaign spend "$RUN/spend-roots.toml"
   ```

## Proof receipts

Read the verdict files in `$OUT/proofs/`. Each file points to the evidence from that attempt. A `pass` does not authorize a paid run. A failed or missing proof cannot certify the comparison.

| Check | OBSERVED verdict | Evidence in `$OUT/proofs/` |
|---|---|---|
| O1 | Pass. The CLI exited 1 on 429 with no retry or hang. The keepalive simulation still waits for the fixed cut. | `O1.json` |
| O2 | Pass. All 12 Proxy cases matched the reference. The largest scripted overshoot was $0.026 at 4 seats. | `O2.json` |
| O3 | Fail. 2 workers received 50 replies and placed 8 accepted orders after the harness `stop()` returned. | `O3.json` |
| O4 | Pass. Both 150 s trials froze at 121.1 s, after the seats ended at 50.9 s. Queued moves completed. | `O4.json` |
| O5 | Pass. All 5 seats failed to read or write the team remote. Each seat could use its private control remote. | `O5.json` |
| O6 | Pass. The CLI explicitly refused both canned web tool calls. The pinned control offered both. | `O6.json` |
| Game call log | Pass. REST and MCP recorded one row per scripted call, without message text. Silent refused all 6 message operations. Team accepted all 6. | `game-call-log.json` |

**INFERRED:** Before the paid pilot, isolate seat processes in a dedicated kill boundary. Revoke the game token when the seat ends or stops.
Repeat O3 with that control. The `time_up` freeze refuses mutations but still allows reads.

## Paid questions only

| Check | What to measure |
|---|---|
| P1 | Compare OpenRouter charges with proxy USD for the same trial. Concurrent trials on one key cannot separate the provider charges by trial. |
| P2 | Measure time to the $0.20 cap at 4 seats. The 2-seat pilot cannot settle this. |
| P3 | Measure seat behavior at a binding cap, post-cap game operations, and actual overshoot. |
| P4 | Measure the spread and ties of paired scores under equal assigned caps. Use 12 complete pairs for calibration, not an efficacy test. |
| P5 | Measure the cost and duration of a short Power Outage run. Holdout cannot settle this. |
