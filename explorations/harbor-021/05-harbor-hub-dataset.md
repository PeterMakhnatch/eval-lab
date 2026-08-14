# harbor hub + dataset publishing (local only)

## What it is

Harbor Hub is the hosted registry/UI: `harbor hub job|trial|leaderboard`,
`harbor publish`, `harbor upload`, `harbor download`. Datasets are
`dataset.toml` manifests (`harbor dataset init`, `harbor add`, `harbor sync`).
Publishing uploads the packaged tasks and is irreversible without org access.
`AGENTS.md` forbids publishing a task without approval.

## Demo

```bash
bash explorations/harbor-021/demos/run-hub.sh
```

Local only: `harbor auth status` (redacted), `harbor hub --help`,
`harbor publish --help` (not invoked), `harbor dataset init lab/recon-demo`
into `demos/dataset-local/`, `harbor add tasks/event-summary`.

Observed (2026-08-13):

```
Logged in as ubhulbuibiulbbl (API key sk-harbor-REDACTED…)
✓ Dataset initialized at .../demos/dataset-local/dataset.toml
  Added local-lab/event-summary
Added 1, updated 0, skipped 0 task(s)
```

`dataset.toml`:

```toml
[dataset]
name = "lab/recon-demo"
version = "1.0.0"
description = "Local-only recon demo dataset; never published."

[[tasks]]
name = "local-lab/event-summary"
digest = "sha256:2c16dfb286d74d3ba9069ea19f436d3754887df8cac9613947a20e1b60cebdb3"
```

No `harbor publish` / `harbor upload` was run. Transcript:
`captures/hub/demo.log`.

## Verdict

**Adopt local `dataset.toml` into brief 07 (canary pin set) and 11
(migration inventory).** `harbor download terminal-bench/...@<version>` is
the specified pin mechanism for 07. **Skip remote publish / Hub upload
because it is an externally consequential write and is forbidden without
Peter's approval.** Hub browse is optional for CURATOR later; it is not a
lab source of truth.
