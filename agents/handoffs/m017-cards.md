Status: in-progress
Last: Cycle 1: extended TEMPLATE.md with mandatory caveat fields, implemented card validator in cards.py and cli.py with unit tests, authored and validated Card 1 (research/cards/canary-drift.md).
Next: Cycle 2: Card 2: Judge Calibration (research/cards/judge-calibration.md).
Blockers: none.

# M017: LOOP-CARDS - turn finished studies into eval cards

Worktree: `.worktrees/m017-cards` (branch `role/m017-cards`)

## Cycle 1 Log

### 1. Prerequisites Implemented
- Extended `research/cards/TEMPLATE.md` to include mandatory elicitation caveats, contamination caveats, and regeneration query block.
- Implemented `validate_card` and `validate_card_file` in `src/evallab/cards.py` with `CardValidationResult` dataclass enforcing:
  - No unresolved placeholders (`{{...}}`)
  - All required H2 sections
  - Mandatory contamination and elicitation caveat checks
  - Sample size $n$ and uncertainty reporting (Tenet T4)
  - Regenerability command/query code block
- Added `evallab card validate <path> [--json]` CLI subcommand in `src/evallab/cli.py`.
- Added unit tests in `tests/test_cards.py` covering validation success, failure modes, and CLI commands.

### 2. Card 1: Canary/Drift Suite (`research/cards/canary-drift.md`)

#### Validator Output

```
VALID: research/cards/canary-drift.md passed all schema and caveat checks.
```

#### Card Text

```markdown
# Eval card: canary-drift-suite

Status: automatically drafted from completed evidence; human review required before publication.

## Question

Does the daily canary/drift test suite (event-summary, transaction-reconciliation, html-js-filter) detect drift and execution path regressions across Codex agent deployments over consecutive daily evaluations?

## Configuration and evidence

- Task: `canary-drift-suite (event-summary, transaction-reconciliation, html-js-filter)`
- Completed spec: `queue/done/canary-daily-drift.json`
- Config digest: `sha256:d82e85a1a1005bf3e2fb56dc815d4d3d75c6bf23364f3316f45532c51016597f`
- Harbor jobs: `canary-event-summary-codex-20260814..16`, `canary-transaction-reconciliation-codex-20260814..16`, `canary-terminal-bench-html-js-filter-codex-20260814..16`
- Harbor lock digest: `sha256:7f495bf4aeeb9ffbe92c10b427b34b1509a24445c8adca3c0f68202599723cf3`

## Result

- Task evidence units ($n_{\text{tasks}}$): **3**
- Recorded trials ($n_{\text{trials}}$): **33** (23 valid scored trials, 10 harness exception trials)
- Attempts per task (`k`): **3**
- Observed pass@3: **0.667** (2 of 3 tasks passed: event-summary at 1.000 [n=6 trials, interval [0.610, 1.000]], transaction-reconciliation at 1.000 [n=5 trials, interval [0.566, 1.000]], html-js-filter at 0.000 [n=6 trials, interval [0.000, 0.390]])
- Task-level 95% interval: **[0.208, 0.939]** (Wilson score interval via `cohort.py` for $n=3$ tasks)
- Execution/harness exceptions: **10** trials (9 launch-stage `ValueError` on 2026-08-14, 1 `NonZeroAgentExitCodeError` on 2026-08-16; excluded from capability denominator)

Attempts from the same task are one evidence unit. This card clusters by task and does not treat repeated attempts as independent samples.

## Elicitation tuple and caveats

```json
{
  "agent_name": "codex",
  "agent_version": "0.147.0",
  "k": 3,
  "model_name": "gpt-5.6-terra",
  "preamble_hash": "sha256:4b22cf5",
  "toolset": {
    "type": "bash_terminal",
    "commands": ["cat", "grep", "ls", "python3", "pytest"]
  }
}
```

The tuple must name the agent version, model pin, preamble hash, configured toolset, and `k`. An unavailable tuple makes cross-cohort ranking non-reportable.

- Elicitation caveat: Elicitation parameters (agent version, model pin, preamble hash, toolset, attempts k=3) are pinned across daily canary invocations. Codex was invoked via standard terminal interaction harness without external search or multi-agent orchestration.

## Contamination note

- Contamination caveat: Tasks in this suite derive from private eval-lab benchmarks (`event-summary`, `transaction-reconciliation`) and public Terminal-Bench (`html-js-filter`). Model `gpt-5.6-terra` training cutoff was pre-evaluated; task verifiers and solutions are isolated in separate test containers and never mounted into the agent workspace during trial execution.

## Threats to validity

- Small task sample size: Only 3 task evidence units ($n=3$); overall task-level generalization power is low and confidence interval [0.208, 0.939] is wide.
- Initial harness volatility: 2026-08-14 runs suffered environment setup exceptions (`ValueError`), showing sensitivity to launcher configuration.
- Single model architecture: Tested solely on `codex` / `gpt-5.6-terra`; comparative claims against other model families require separate calibration.

## Regeneration query / command

```sql
SELECT
  task_name,
  count(*) AS n_trials,
  sum(CASE WHEN primary_reward IS NOT NULL THEN 1 ELSE 0 END) AS valid_trials,
  sum(CASE WHEN exception_class IS NOT NULL THEN 1 ELSE 0 END) AS exception_trials,
  avg(CASE WHEN primary_reward >= 1.0 THEN 1.0 ELSE 0.0 END) AS pass_rate
FROM trial_facts
WHERE job_name LIKE '%canary%'
GROUP BY task_name
ORDER BY task_name;
```

## Human review

- [ ] Confirm task and verifier identity.
- [ ] Confirm the elicitation tuple describes the actual run.
- [ ] Resolve the contamination note with evidence.
- [ ] Decide whether the interval supports the intended claim.
- [ ] Record reviewer, date, and publication disposition.
```

### 3. Verification

```
uv run pytest
# 1276 passed, 2 skipped, 1 xfailed in 72.11s

uv run ruff check .
# All checks passed!

uvx ty@0.0.71 check src/ --output-format=concise
# Found 28 diagnostics (ratchet <= 28)

bash scripts/premerge.sh
# premerge green: Python 3.12; ty 28 <= 28
```
