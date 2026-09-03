# 09 — ZAI metered baseline (3 cells)

Filed 2026-09-03 on `integrate/spine-batch1` by the overnight MeteredSpecs lane, after the
parent reconciled the zai lane with the spine (metered ceilings, single-trial binding,
auth.json key sourcing, models `glm-5.3-flash` + `glm-5.3`).

## What the reconciled lane requires

`src/evallab/execution_contracts.py` (`validate_request` + `build_command`, zai blocks):

- **Every metered ceiling is mandatory** for `zai-opencode`: `max_requests`,
  `max_input_tokens`, `max_output_tokens`, `max_total_tokens`, `cost_limit_usd`
  (any one implies all; positive; total ≤ input + output; cost > 0).
- **Single-trial binding**: `attempts == 1` and `concurrency == 1` — every ceiling
  binds exactly one trial (`build_command` adds `--n-concurrent-agents 1 --n-tasks 1
  --max-retries 0`).
- **Model pinning**: `zai-coding-plan/glm-5.3-flash` (used here, the lane default) or
  `zai-coding-plan/glm-5.3`; credentials sourced from the owner-only OpenCode
  `auth.json` store via the secret proxy (`read_zai_opencode_key` /
  `materialize_zai_secret_file`).

The pre-reconciliation shapes (`07` k3 spec, `08` k1 specs) carry **no** ceilings and are
the field-shape mirrors only; this lane is the first filed with the reconciled contract.

## Ceilings (generous, per lane instruction)

`max_requests=64`, `max_input_tokens=300_000`, `max_output_tokens=32_000`,
`max_total_tokens=332_000`, `cost_limit_usd=2.50`.

Sanity: worst-case metered spend at the token ceilings is
$0.30·(300k/1M)+$4.40·(32k/1M) ≈ **$0.56** < the $2.50 cap (proxy price constants
`ZAI_{INPUT,OUTPUT}_COST_MICROS_PER_MILLION`), so the cost ceiling can only trip on
unmodeled spend, never mid-run on normal traffic. Pilot usage fits: the heaviest cell
(neutral16k) needed 65 *MCP chunk reads* inside far fewer provider requests; 64 provider
requests is generous for a pass^1 cell.

## The 3 cells

All cells: agent `zai-opencode`, model `zai-coding-plan/glm-5.3-flash`, purpose
`baseline`, screening/calibration-only label in every `hypothesis`, attempts 1,
concurrency 1, docker, timeout 1800s, priority 80, est_cost 0.0, submitted_by
`autopilot-researcher`, ceilings as above.

| # | Spec | Task ref | Binding | Pilot provenance |
|---|---|---|---|---|
| 1 | `screening-metered-funcdag-easy-zai-opencode-k1` | `registered/syn-funcdag-easy` | **Fully bound** from `library/registry/syn-funcdag-easy.json` (task_path, version 1.0.0, verifier digest `sha256:c706…`; state=registered, allowed_uses=[measurement]) | lane's prior queued measurement (`07`, 2/3 pass^3) |
| 2 | `screening-metered-action-clean4k-zai-opencode-k1` | `registered/action-memory-clean4k` (planned ref) | unset — registry-bound at admission | wave-1 pilot 3/3 reward 1.0 (`research/evidence/runs/zai-flash-action-clean4k-r3-amd64-egress/`) |
| 3 | `screening-metered-action-neutral16k-zai-opencode-k1` | `registered/action-memory-neutral16k` (planned ref) | unset — registry-bound at admission | wave-1 pilot 3/3 reward 1.0, 65 valid chunk calls/trial (`research/evidence/runs/zai-flash-action-neutral16k-r3-amd64-egress/`) |

Per the `08` convention, the action-memory refs are **planned registered refs**: the
cells are not yet registered, so `task_path` / `task_version` / `verifier_digest` /
`generator_seed` are intentionally unset rather than guessed — a wrong pre-filled digest
raises `TaskDigestMismatchError` at admission (`src/evallab/registry.py:1938`), and no
seed is recoverable from the promoted pilot bundles. Digests must come from
`library/registry/action-memory-*.json` once records exist.

## Filing status: queued to proposed ONLY

- Three specs in `queue/proposed/` (`researcher_proposed` events in
  `queue/events.jsonl`, spec ids `01M1JV4X8QBC2SD6E534HHRBDZ`,
  `01M1JV4X8RMW5JBBB0W00287FN`, `01M1JV4X8SM5TYSG5C60XEZBPY`).
- No approval, no tick, no dispatch: the standing policy refuses every billable agent
  before any standing rule is consulted (`policy/standing-approvals.yaml`), so nothing
  here can spend without a recorded human `evallab approve`.
- Known and accepted: until registration lands, `audit_registry`
  (`src/evallab/registry.py:2212`) will report `false_registered_claim` errors for the
  two planned refs sitting in `proposed`. That is the documented cost of the
  `08`-style planned-ref convention; reconcile the refs at submission after
  registration rather than inventing digests now.
- Registration files are drafts only; human promotion required.

## Kill rules / reading rules

- Screening/calibration-only: n=1 per cell; not a ranking; not a comparison-bar result.
- Read each cell against its pilot wave-1 evidence (3/3 reward 1.0): the question is
  whether enforced metered ceilings distort a proven cell, so any cell < 1.0 is a
  ceiling-interaction finding first, a capability signal second.
- Proxy usage receipts (`ZAI_PROXY_USAGE_*` records) must show the ceilings actually
  enforced, not merely declared.

## Next-tasks

1. **Materialize + register the two action-memory cells** —
   `library/benchmarks/action-memory-v1/materializer.py` → candidate packages under
   `derived/harbor-tasks/action-memory/`; registration drafts to
   `library/registry/action-memory-clean4k.json` and
   `library/registry/action-memory-neutral16k.json` (pin generator seed + package
   digests; human promotion required). Then reconcile the planned refs in
   `queue/proposed/zai-opencode-01M1JV4X8RMW5JBBB0W00287FN.json` and
   `…/zai-opencode-01M1JV4X8SM5TYSG5C60XEZBPY.json`.
2. **Human approval + dispatch of the three proposed specs** —
   `queue/proposed/zai-opencode-01M1JV4X8QBC2SD6E534HHRBDZ.json` (+ the two above) via
   `uv run evallab approve <spec-id> --actor <you>`, then one guarded
   `uv run evallab tick`; confirm the metered proxy enforces the bound ceilings
   (`containers/zai_secret_proxy.py` usage records).
3. **Post-run read against pilot** — ingest the three jobs
   (`src/evallab/ingest_verify.py`), file per-cell pass^1 next to the wave-1 pilot rows
   in `research/evidence/README.md`, and attribute any <1.0 cell to a ceiling
   interaction (compare request/token usage receipts against the bound ceilings) before
   drawing capability conclusions.
4. **glm-5.3 mirror cells** — once the flash baseline reads clean, add a 3-cell
   `glm-5.3` mirror (`ZAI_OPENCODE_MODEL_SELECTORS` admits both models) in a follow-up
   spec dir (`research/experiments/specs/10-zai-metered-baseline-glm53/`) to separate
   model revision from lane mechanics.
