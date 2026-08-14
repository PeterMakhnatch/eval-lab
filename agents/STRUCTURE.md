# Repository structure

**Binding.** This file is the single source of truth for what may exist at the
repository root and where new things go. The root is **frozen**: creating a
new top-level entry requires editing this file in the same PR, with one
sentence saying which bucket rule made a new entry necessary. If you cannot
name the bucket something belongs to, you do not create it — you propose a
change here first. Iterate by PR; log changes at the bottom.

## The map

```
harbor-experiment-lab/
│
├── AGENTS.md                  repository rules — every agent reads this first
├── README.md                  human orientation
│
├── agents/                    HOW WE WORK — coordination and governance
│   ├── WORKFLOW.md            the work protocol (worktrees, merges, boundaries)
│   ├── ROLES.md               role registry: who exists, owns what, status
│   ├── STRUCTURE.md           this file
│   └── handoffs/<role>.md     live status, one file per role
│
├── docs/                      WHY IT'S BUILT THIS WAY — design and decisions
│   ├── architecture.md        system boundaries and planes
│   ├── analysis-loop.md       evidence → finding → proposal state machine
│   ├── design-additions.md    decisions, tool stack, unattended loop, briefs
│   ├── fleet-tracking.md      how the human tracks the fleet
│   ├── scaling.md             gates for object storage / k8s / ClickHouse
│   ├── operations.md          runbooks
│   └── prompts/               numbered implementation briefs (work orders)
│
├── library/                   WHAT WE EVALUATE — task supply, version-pinned
│   ├── curated/               verified third-party tasks with provenance cards
│   ├── tasks/                 lab-authored tasks
│   ├── benchmarks/            pinned frontier benchmark ingests (INGEST)
│   └── adapters/              benchmark → Harbor converters
│
├── research/                  WHAT WE LEARN — produced knowledge
│   ├── experiments/           experiment specs and matrices
│   ├── calibration/           judge ground truth: corpora, answer keys, labels
│   ├── explorations/          capability recon: demos + adoption notes
│   ├── analysis/              reusable analysis queries
│   └── evidence/              reviewed, immutable control bundles
│
├── policy/                    THE HUMAN'S STEERING WHEEL — standing approvals,
│                              canary suite. Peter-owned content; deliberately
│                              at root for visibility.
│
├── src/  tests/  sql/  scripts/     THE LAB SOFTWARE (BUILDER-owned)
├── pyproject.toml  uv.lock  Makefile  compose.yaml     build & services
│
├── digests/                   the daily one-pager the human reads (committed)
├── queue/  runs/              GENERATED STATE (gitignored, rebuildable)
└── .worktrees/                parallel working trees (gitignored, hidden)
```

## Placement guide

Ask which question the thing answers:

| The thing answers… | It goes in |
|---|---|
| "How do agents coordinate?" | `agents/` |
| "Why is the system designed this way?" / "What should be built?" | `docs/` (briefs → `docs/prompts/`) |
| "What can we run an agent against?" | `library/` |
| "What did we find out / what is our ground truth?" | `research/` |
| "What is the lab allowed to do unattended?" | `policy/` |
| "How does the lab software work?" | `src/` (+ `tests/`, `sql/`, `scripts/`) |
| "What happened?" (generated, rebuildable) | `runs/`, `queue/`, catalog — never committed |
| "What happened?" (curated for humans) | `digests/`, `research/evidence/` |

Rules that fall out of the buckets:

- `library/` content is version-pinned and immutable once registered; changing
  a task means a new version, never an edit in place.
- `research/` content states its provenance (which runs, which corpus digest).
- Nothing in `agents/`, `docs/`, or `policy/` is generated; nothing in
  `queue/` or `runs/` is hand-edited.
- Role ownership boundaries (`agents/ROLES.md`) follow these paths.

## Migration ledger

Done 2026-08-13 (this commit):

| Move | Refs patched |
|---|---|
| `curated/` → `library/curated/` | docs, ROLES, handoffs |
| `experiments/` → `research/experiments/` | `tests/test_runner.py`, README, docs |
| `calibration/` → `research/calibration/` | docs, ROLES, handoffs |
| `explorations/` → `research/explorations/` | docs, ROLES, handoffs |
| `analysis/` → `research/analysis/` | README, docs |
| `prompts/` → `docs/prompts/` | README, docs |

Completed 2026-08-14: `tasks/` and `adapters/` → `library/`, `evidence/` →
`research/`, `AGENTS.md` map refreshed. Patched: `policy/canary-suite.yaml`,
`src/harbor_lab/cli.py`, four test files, `research/experiments/
local-controls.json`, ruff excludes in `pyproject.toml`, README, docs.

Considered and kept at root: `policy/` (the human steering wheel — visibility
beats purity), `digests/` (the human's daily surface), `queue/`+`runs/`
(referenced throughout the fresh executor code; consolidating them under a
`var/` is a candidate future iteration, not worth breaking a working nightly
for today).

## Change log

- 2026-08-13 — created; buckets `library/` and `research/` introduced; six
  moves executed, four pending (Claude, at Peter's direction).
- 2026-08-14 — migration complete: all ledger moves executed; root is at its
  target state (Claude).
- 2026-08-14 — `library/benchmarks/` added for the INGEST role (bucket rule:
  evaluable task supply).
