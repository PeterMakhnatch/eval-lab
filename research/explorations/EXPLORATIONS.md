# Explorations

RECON owns this directory. Notes are one-pagers with a runnable demo and an
adopt / skip verdict against `docs/design-additions.md` briefs 05–11.

Source of Harbor APIs: Harbor **0.21.0** on PATH and
`~/Developer/agent-evals/harbor` (`packages/harbor-langsmith`,
`packages/harbor-atif2otel`). All demos are free/local (`oracle` / `nop` /
host Python / `uvx`). No Hub publish, no compose, no paid model.

## Ranking — adopt, in this order

| Order | Capability | Note | Brief | Why this order |
|---|---|---|---|---|
| 1 | `harbor-atif2otel` validate+convert | [09](harbor-021/09-harbor-atif2otel.md) | **08** | Works today on a real ATIF; unblocks Phoenix with no new runtime. |
| 2 | Job plugin API (`--plugin`) | [03](harbor-021/03-job-plugin-api.md) | **05**, then **08** | Local hook fired on an oracle job. Executor should subscribe to Harbor's own lifecycle; later attach `atif2otel` plugin. |
| 3 | Separate verifier (already used) | [08](harbor-021/08-separate-verifier.md) | **07**, **11** | Already correct on `event-summary`. Make it the default for every new/migrated task. |
| 4 | Local `dataset.toml` packaging | [05](harbor-021/05-harbor-hub-dataset.md) | **07**, **11** | `dataset init` + `add` is the pin/inventory format 07/11 already describe. |
| 5 | `harbor check` rubric + verifier | [01](harbor-021/01-harbor-check.md) | **07**, **11** | Quality gate before a task is pinned. Live evaluator only via the 05 queue. |
| 6 | `harbor analyze` default rubric | [02](harbor-021/02-harbor-analyze.md) | **09** (via **05**) | Cheap reward-hacking screen on canary/billable trials. Does not replace prompts 01–03. |
| 7 | Multi-step tasks | [06](harbor-021/06-multi-step-tasks.md) | **11** | Oracle two-step demo works. Adopt only when a migrated task is sequential. |

## Ignore

| Capability | Note | Why |
|---|---|---|
| `harbor exec` | [04](harbor-021/04-harbor-exec.md) | **Skip because** the lab's unit of work is a versioned task + experiment spec. Exec bypasses the registry/canary pin. (Demo itself succeeded: compiled task, oracle reward 1.0.) |
| Hub remote publish / upload | [05](harbor-021/05-harbor-hub-dataset.md) | **Skip because** `AGENTS.md` forbids publishing without approval. Local manifests only. |
| `network_mode = "allowlist"` on this host | [07](harbor-021/07-network-allowlist.md) | **Skip because** Docker Desktop rejects allowlist (`network_mode='allowlist' is not supported by EnvironmentType.DOCKER`). Revisit on OrbStack/Linux. |
| LangSmith plugin | [03](harbor-021/03-job-plugin-api.md) | **Skip because** it is a paid cloud backend. Copy the hook shape, not the destination. Phoenix is the chosen store (08). |
| Live `harbor check` / `harbor analyze` from this worktree | [01](harbor-021/01-harbor-check.md), [02](harbor-021/02-harbor-analyze.md) | Defaults to `claude-code`. Billable. Queue them after 05. |

## How to re-run

From `~/Developer/helab-recon`:

```bash
bash explorations/harbor-021/demos/run-check.sh
bash explorations/harbor-021/demos/run-analyze.sh
bash explorations/harbor-021/demos/run-atif2otel.sh
bash explorations/harbor-021/demos/run-hub.sh          # no publish
# Docker, -n 1, oracle only:
bash explorations/harbor-021/demos/run-plugin.sh
bash explorations/harbor-021/demos/run-exec.sh
bash explorations/harbor-021/demos/run-multistep.sh
bash explorations/harbor-021/demos/run-separate-verifier.sh
bash explorations/harbor-021/demos/run-allowlist.sh    # expected fail on Desktop
```

Captures: `explorations/harbor-021/captures/`.
