# HAR-14 frozen harness-first cohort

Factory-owned inputs for a same-root **mini-swe-agent** versus **authors-RLM-derived** comparison. Integration is the only experimental dispatcher. This directory does not run models.

## What is frozen

[cohort.json](cohort.json) pins three already-eligible registered measurement tasks at commit `000750a897b612948067e3d00651f56da501bcbd`:

| Stage | Task | Role | Why |
|---|---|---|---|
| Canary | `registered/event-summary` | Conventional control | Short JSONL aggregation. First operability pair. |
| Matrix | `registered/travel-lisbon-002` | Cross-artifact planning | Flight/hotel/pass sources or explicit refusal. Closest eligible information-gathering case. |
| Matrix | `registered/syn-funcdag-easy` | Conventional multi-file | Tiny deterministic DAG. RLM should not uniquely help. |

The registry has only these three `state=registered` measurement tasks with oracle=1 / nop=0 evidence. The fourth slot is an explicit gap, not a silent registration.

Do not use `dspy-rlm` as a stand-in for the authors-RLM arm. Agent names and the shared root/worker model are required execution parameters from Harness/Post-Training via Integration.

## Compile offline per-task specs

```bash
uv run --no-sync python research/experiments/harness-first/compile.py \
  --baseline-agent mini-swe-agent \
  --candidate-agent <authors-RLM Harbor agent> \
  --model <exact shared root checkpoint> \
  --max-requests <explicit per-trial request ceiling> \
  --max-input-tokens <explicit per-trial input-token ceiling> \
  --max-output-tokens <explicit per-trial output-token ceiling> \
  --max-total-tokens <explicit per-trial total-token ceiling> \
  --cost-limit-usd <explicit per-trial USD ceiling> \
  --stage canary \
  --out research/experiments/harness-first/compiled
```

All five ceiling values must be supplied explicitly for `mini-swe-agent`. Missing
or partial ceilings are refused before specs are written. Request/token ceilings
must be positive integers, the cost ceiling must be positive, and the total-token
ceiling cannot exceed input plus output ceilings. These become the actual
`ExperimentSpec.max_requests`, `max_input_tokens`, `max_output_tokens`,
`max_total_tokens`, and `cost_limit_usd` fields, not grid metadata or a cost
estimate. `est_cost_usd=0.0` is neither a provider cap nor an approval.

The Python `compile_spec`, `compile_pair`, and `compile_stage` APIs accept the
same five keyword arguments. Pair/stage calls apply them only to the Mini-SWE
baseline; unsupported agents refuse supplied ceilings rather than silently
discarding them. No budgets or candidate-arm limits are inferred.

`--stage matrix` is the remaining two tasks after the canary pair completes without an infrastructure exception and remaining approval exists. `--stage all` emits both; it is not permission to launch the matrix first.

Each pair shares `task`, `task_path`, `task_version`, verifier/package digests,
timeout, attempts=1, environment, and model. Agent/arm labels differ, and these
provider ceilings belong only to the Mini-SWE baseline. Compilation does not
establish enforceable candidate limits or qualify a runtime comparison.

Authors-RLM and HAR-10 remain held. Emitted candidate specs are offline
definitions, not proof of operability, capability binding, or permission to
execute. Any future authorized dispatch must use the existing `evallab submit`
route and satisfy its independent gates. Factory does not approve or tick the
queue; this compiler repair changes no receipt or authorization.

## After jobs exist

`compile.build_comparison_spec` takes completed job paths for both arms. Pairing key is Harbor `task_digest` from trials, not the package digest. Mode is `exploratory`. A one-attempt canary is not a ranking.

## Out of scope

No FACET/SETA/PR-generation, no sealed-final inspection, no Tau3 mixing, no spend authorization, no merge.
