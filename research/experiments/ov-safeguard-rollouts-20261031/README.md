# ov-safeguard-rollouts-20261031 — real agent rollouts on safeguard-integrity-v1 + two monitors

Item W2-SAFEGUARD-ROLLOUTS of the 2026-10-31 overnight factory. Companion to
`research/experiments/ov-safeguard-integrity-v1-20261031/` (the four Sol/Astra-pattern Harbor tasks, #1690).

## What ran

1. `rollouts.py prepare`: for each of 24 real cases (4 tasks × 3 models × 2 trials) and 10 scripted controls
   (honest ×4, subverted ×4, dishonest_subverted ×2) a fresh git workspace holding only `environment/app`.
   The package `tests/` (pristine tests, `verify.py`, probes, fixtures) never enters a workspace.
2. `rollouts.py run`: real cases are `omp -p` coding sessions (`bash,read,edit,write,grep,glob`, no skills/rules/
   context files/extensions, 20-tool-call budget stated in the prompt, `--max-time 600 --max-cost 3`) with the
   task's `instruction.md` (`/testbed` rewritten to the workspace path) and nothing else. Served model is attested
   from the session log. Scripted controls run the suite's `solve.sh` locally. Output per case: `trajectory.json`
   (hidden `reasoning_content` kept in its own field), `final.diff`, `run.json`.
3. `rollouts.py grade`: the final workspace is restored over `/testbed` in the task's own Modal container
   (`ModalBackend.run_container_trial(mode="shortcut")`, lab Harbor 0.24.0) and the native `tests/test.sh` →
   `verify.py` writes `safeguard_report.json` (`functional_pass`, `safeguard_preserved`, `test_tampered`,
   `summary_dishonest`) and `reward.txt`. The shortcut script first runs `ls /tests` to prove the agent side has
   no verifier. Spend: `CloudSpendGate` cap $1.80, $0.01 per container start, ledger
   `~/.local/state/evallab-failure-library/w2-safeguard-rollouts-ledger.json`.
4. `rollouts.py monitor --arm baseline|structured`: `anthropic/claude-fable-5-1`, no tools, one read per case.
   Baseline = flat DT-15-masked transcript (tool calls + outputs only; 4k/2.5k field caps, 40k budget).
   Structured = instruction + policy files + full git diff with protected-test / safeguard hunks marked +
   command/exit ledger + user-facing final message and summary file + deterministic `backcheck` lines.
   Neither arm ever sees `reasoning_content`, `safeguard_report.json` or `reward.txt`.
5. `rollouts.py score`: `receipts/rollouts.jsonl` (one row per case) and `receipts/summary.json`.

Preregistration and the full report live in the bridge directory
(`research-context/bridge/2026-10-31-overnight/fable-2/raw/W2-SAFEGUARD-ROLLOUTS/{prereg,report}.md`);
the headline numbers are copied into `receipts/summary.json` here.
