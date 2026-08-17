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
- `harbor-derived`: local copy or adaptation of an upstream Harbor-native task (name collision across corpora detected). `family` is the upstream benchmark. Does not assert whether the local copy still matches upstream byte-for-byte; only that an upstream task of that name exists in the Harbor corpus. Evidence lists both locations and any instruction.md comparison performed.
- `unknown`: evidence insufficient to classify; `confidence=unknown` and `evidence` carries the reason. Never padded with a guess.

## Classification rules

Classification reuses `craft.discover_tasks` on the roots that exist at call time. 

- TB3 root taken from `EVALLAB_TB3_ROOT` env (documented default `~/Developer/agent-evals/terminal-bench/tasks`), then `--tb3-root`, then absent -> `unknown` path reported as absent rather than crash or empty.
- Local tasks: `library/tasks` relative to repo root.
- Proposed: `library/tasks/_proposed` when the directory exists.
- `task_ref` prefers `task.toml` `task.name` when present, else relative path under the corpus root.
When a `task_ref` resolves in more than one corpus root and one is harbor-native, it is classified `harbor-derived` (confidence=inferred) with evidence naming both paths (and instruction.md identity if compared). Single unambiguous corpus resolution yields confidence=certain. 

## Why unknown is honest

Matches preflight style: reports `UNKNOWN [unavailable]` with reason instead of inventing a value. A confidently wrong label would silently corrupt every cross-corpus comparison.

## Downstream use

When comparing results, filter or stratify by `origin` and `family`. A `harbor-native/terminal-bench-3` trial and a `local-lab` trial are not interchangeable evidence; the field makes the distinction queryable and auditable.


`report` output begins with a deterministic table of every configured corpus root (tb3_root, local-lab, proposed) and its status (found with task_count or unavailable with path and reason), followed by the task table. Output is byte-stable across runs with identical repo state.

```
python -m evallab.provenance classify <task_ref>
python -m evallab.provenance report [--tb3-root PATH]
```

