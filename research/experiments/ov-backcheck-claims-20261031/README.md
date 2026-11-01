# W2-BACKCHECK-CLAIMS — paired ablation: Built-Action-View vs +backcheck Claim-vs-Evidence ledger

Tests one hypothesis from the overnight squad: in real deployments the model's private
`reasoning_content` (CoT scratchpad) is **not** visible, while user-facing
`assistant.content` claims, tool calls, tool outputs and diffs **are**. Yesterday's DT-15
stripped **both**, so it could not detect `misrepresented_compliance` (lying to the user
about what ran or passed). This experiment keeps CoT stripped and re-introduces the
user-facing claim, juxtaposed with a deterministic claim-vs-evidence check.

## Design

Two arms, two corpora (frozen DT-8 cohorts: Goodfire 150 = 80 enacted / 70 clean,
CheatBench DEV 150 = 75 / 75), one toolless `deepseek/deepseek-v4-pro` reader:

- **Arm A (Built-Action-View)** — task + file writes (test/grader paths first) + final
  patch + one-line-per-step command/exit ledger + top-3 suspicious output windows. Zero
  `reasoning_content`, zero `assistant.content`.
- **Arm B (Arm A + backcheck ledger)** — byte-identical to Arm A through the output
  windows, plus the final user-facing claim and the deterministic
  `backcheck_ledger` Claim-vs-Evidence ledger (CoT still hidden).

Paired McNemar: Arm A vs DT-15 (view lift), Arm B vs Arm A (claim-vs-reality lift),
plus `steps >= 41` length control and per-mechanism Goodfire recall
(`misrepresented_compliance` / `hidden_test_targeting` / `upstream_fix_recall`).

## Files

- `backcheck_ledger.py` — deterministic Claim-vs-Evidence extractor (pure, label-free).
- `backcheck_experiment.py` — driver: `build`, `read`, `score`, `attest`.

## Reproduce

```bash
uv run --no-sync python research/experiments/ov-backcheck-claims-20261031/backcheck_experiment.py build
uv run --no-sync python research/experiments/ov-backcheck-claims-20261031/backcheck_experiment.py read --corpus goodfire --arm A
# ... A/B x goodfire/cheatbench ...
uv run --no-sync python research/experiments/ov-backcheck-claims-20261031/backcheck_experiment.py score
uv run --no-sync python research/experiments/ov-backcheck-claims-20261031/backcheck_experiment.py attest
```

`read` makes toolless `omp -p` calls (`deepseek/deepseek-v4-pro`, `--no-tools
--no-skills --thinking low`, ≤4 concurrent, foreground, attested). Run text, prompts,
replies and session logs stay in `/tmp/w2bc`; only label-free metadata and aggregate
numbers reach the repo and the bridge receipts. DEV only; labels join in `score`, after
every call is on disk. `research/answer-keys/` and held splits are never opened.

The preregistration, report and receipts live in the squad bridge
`raw/W2-BACKCHECK-CLAIMS/`.
