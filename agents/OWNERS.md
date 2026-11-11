# Owners

These areas route technical responsibility, not persistent-chat identities or contact permissions.
The task defines each agent's write scope.

| Lane | Owns (paths) | Decides |
|---|---|---|
| **Integration** | `agents/`, `.github/`, merge queue | Cross-product interfaces, integration conflicts, repository delivery |
| **Research** | `research/`, `digests/`, `docs/research/`, analysis sections of `sql/` | What counts as evidence, analysis semantics, findings, experiment agenda |
| **Tasks** | `library/` (benchmarks, curated, adapters, registry, synthetic staging) | Task admission, verification standards, benchmark pins, certification gates |
| **Platform** | `src/`, `tests/`, `scripts/`, `sql/` schema, `compose.yaml`, `pyproject.toml`, `uv.lock`, `Makefile`, `dashboard/` | Code architecture, CI/premerge contract, storage topology, tooling |

Docs follow their subject: `docs/research/` is Research; engineering and
operations docs are Platform; governance docs are Integration.

## Peter's reserved authority

Peter alone decides, and is asked *only* about:

1. **Policy and spend** — `policy/standing-approvals.yaml` content, cost
   ceilings, anything billable or cloud (`escalate_to_human` classes).
2. **Publication** — anything leaving the repository: pushes to public repos,
   external PRs, publishing tasks or results. Normal pushes/PRs in Peter's
   authorized repository follow the task and repo contract without a second approval.
3. **Research direction** — which hypotheses the lab pursues; accepting or
   rejecting DISCOVERIES entries.
4. **Registration of evaluation tasks** — promotion into `registered/*`.

Choose informed local decisions within the task. Escalate only unresolved scope or authority conflicts.
