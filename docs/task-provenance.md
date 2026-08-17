---
status: living
audience:
  - analyst
  - builder
  - operator
---

# Task Provenance

`src/evallab/provenance.py` records the corpus origin of every task so that downstream analysis can distinguish evidence types.

## Taxonomy

- `harbor-native`: task shipped by Harbor. `family` names the benchmark (`terminal-bench-3` for the TB3 corpus at the external root).
- `local-lab`: authored under `library/tasks/`.
- `proposed`: sitting in `library/tasks/_proposed/` (quarantine, unregistered).
- `unknown`: evidence insufficient to classify; `confidence=unknown` and `evidence` carries the reason. Never padded with a guess.

## Classification rules

Classification reuses `craft.discover_tasks` on the roots that exist at call time. 

- TB3 root taken from `EVALLAB_TB3_ROOT` env (documented default `~/Developer/agent-evals/terminal-bench/tasks`), then `--tb3-root`, then absent -> `unknown` path reported as absent rather than crash or empty.
- Local tasks: `library/tasks` relative to repo root.
- Proposed: `library/tasks/_proposed` when the directory exists.
- `task_ref` prefers `task.toml` `task.name` when present, else relative path under the corpus root.

A task present in multiple roots is classified by first-match order (harbor, local, proposed).

## Why unknown is honest

Matches preflight style: reports `UNKNOWN [unavailable]` with reason instead of inventing a value. A confidently wrong label would silently corrupt every cross-corpus comparison.

## Downstream use

When comparing results, filter or stratify by `origin` and `family`. A `harbor-native/terminal-bench-3` trial and a `local-lab` trial are not interchangeable evidence; the field makes the distinction queryable and auditable.

## CLI

```
python -m evallab.provenance classify <task_ref>
python -m evallab.provenance report [--tb3-root PATH]
```

`report` output is deterministic (sorted, no timestamps) so identical repo state yields byte-identical stdout.
