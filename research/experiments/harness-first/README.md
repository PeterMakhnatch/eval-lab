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

## Compile identical per-task specs

```bash
uv run python research/experiments/harness-first/compile.py \
  --baseline-agent <mini-swe Harbor agent> \
  --candidate-agent <authors-RLM Harbor agent> \
  --model <exact shared root checkpoint> \
  --stage canary \
  --out research/experiments/harness-first/compiled
```

`--stage matrix` is the remaining two tasks after the canary pair completes without an infrastructure exception and remaining approval exists. `--stage all` emits both; it is not permission to launch the matrix first.

Each pair shares `task`, `task_path`, `task_version`, verifier/package digests, timeout, attempts=1, environment, and model. Only `agent` / arm label change.

Submit through existing `evallab submit`. Unapproved model jobs must remain held. Factory does not approve or tick the queue.

## After jobs exist

`compile.build_comparison_spec` takes completed job paths for both arms. Pairing key is Harbor `task_digest` from trials, not the package digest. Mode is `exploratory`. A one-attempt canary is not a ranking.

## Out of scope

No FACET/SETA/PR-generation, no sealed-final inspection, no Tau3 mixing, no spend authorization, no merge.
