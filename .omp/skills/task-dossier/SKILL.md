---
name: task-dossier
description: Look up one Eval Lab task's health, verdict, leak and repair, static flags, failing tests, exploit probes, trials, copy judgments, and evidence links without running models or changing evidence.
---

# Read-only task dossier

Use `task_dossier(task_id)` before answering a task-level evidence question. From an updated Eval Lab checkout, invoke:

```bash
uv run evallab task format-code-task-000792 --json
```

For an agent already using the checkout's Python environment, the same callable is:

```python
from evallab.task_dossier import task_dossier

dossier = task_dossier("format-code-task-000792")
```

The command and callable read existing evidence. Missing Parquet fields may be projected in memory; neither path writes backfills, starts a server, runs a probe/reader/model, or changes the task library. No MCP daemon or global OMP configuration is required. See `evallab task --help` for explicit source-root and optional static-CSV inputs.

Interpret the result without changing its evidence:

- Cite `sources`, the repair record/digest, and native trial IDs. A task-page address is not proof that its server or page is available.
- Missing evidence stays null; an empty selected trial list is not proof that the task never ran elsewhere. `solve:*` describes the recorded model history, while `trials` also includes controls and exploit probes.
- A repair with `status: candidate` exists but is **not validated**. Do not call it an admitted or certified fix.
- Static-audit health/solve labels can be stale; the dossier retains them only as ignored source metadata, not as current task status.
- Keep copy-check, RewardKit, Laminar `copied`, Harbor `reward_hacking`, and the HAR-159 counts verdict separate. Preserve disagreements, abstentions, and `binding_conflicts`; do not invent a consensus.
- Laminar links come from identity-bound stored reader evidence. Do not collect missing Signals or run `harbor analyze` to fill a gap: those can spend money and are outside this read-only tool.
