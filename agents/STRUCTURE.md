# Repository structure

**Binding.** This file is the single source of truth for what may exist at the
repository root and where new things go. The root is **frozen**: creating a
new top-level entry requires editing this file in the same PR, with one
sentence saying which bucket rule made a new entry necessary. If you cannot
name the bucket something belongs to, you do not create it — you propose a
change here first. Iterate by PR; log changes at the bottom.

## The map

```
eval-lab/
│
├── AGENTS.md                  repository rules — every agent reads this first
├── README.md                  human orientation
│
├── agents/                    HOW WE WORK — coordination and governance
│   ├── WORKFLOW.md            the work protocol (worktrees, merges, boundaries)
│   ├── OWNERS.md              four stable lanes + Peter's reserved authority
│   ├── ROLES.md               superseded — compatibility pointer (M001)
│   ├── missions/              ACTIVE.md live board + TEMPLATE.md
│   ├── archive/               dated closed-mission and registry records, plus
│   │                          dated handoff subdirectories. Normalize the
│   │                          four-line header before the 1:1 `git mv`; after
│   │                          archival, handoffs are never edited or deleted.
│   │                          Dated INDEX.md files remain mutable indexes.
│   ├── CHECKS.md              the verification contract
│   ├── STRUCTURE.md           this file
│   └── handoffs/<role>.md     live status, one file per **live** mission only;
│                              finished ones move to archive/ (ACTIVE.md policy)
│
├── docs/                      WHY IT'S BUILT THIS WAY — design and decisions
│   ├── architecture.md        system boundaries and planes
│   ├── analysis-loop.md       evidence → finding → proposal state machine
│   ├── data-architecture.md   the four provenance zones
│   ├── design-additions.md    decisions, tool stack, unattended loop, briefs
│   ├── execution-tiers.md     what runs where, and what it costs
│   ├── scaling.md             gates for object storage / k8s / ClickHouse
│   ├── engineering.md         standards + measured performance baselines
│   ├── observability.md       tracing and telemetry surfaces
│   ├── operations.md          runbooks
│   ├── operating-manual.md    Peter's manual for running an agent-built lab
│   ├── operator-demo.md       one truthful analysis loop, end to end
│   ├── fleet-tracking.md      how the human tracks the fleet
│   ├── agent-profiles.md      subscription-only agent identity/qualification
│   ├── canaries.md            canary suite and drift interpretation
│   ├── task-registry.md       task admission trust boundary
│   ├── run-explorer.md        run & analysis explorer
│   ├── research-questions.md  what this lab studies
│   ├── path-forward-2026-08.md    dated direction note
│   ├── mentor-review-2026-08.md   dated external review
│   ├── parallel-work.md       superseded — pointer to agents/WORKFLOW.md
│   ├── prompts/               numbered implementation briefs (work orders)
│   │                          + dated mission-prompt sets; README indexes both
│   ├── checkpoints/           dated hands-on integrator verification records
│   ├── research/              Research-lane docs (survey, external datasets,
│   │                          synthetic tasks, trajectory intelligence)
│   ├── agent-workflow.html        \
│   ├── eval-rd-roadmap.html       |  four hand-authored overview renders
│   ├── repository-state.html      |  sharing one stylesheet. Nothing in the
│   ├── system-cartography.html    |  repository generates them, so they
│   └── repository-overview.css    /  cannot be rebuilt; keep-or-archive is
│                              the one open `Needs Peter` item in
│                              agents/missions/ACTIVE.md. Do not move or delete
│                              them without that decision.
│
├── library/                   WHAT WE EVALUATE — task supply, version-pinned
│   ├── curated/               verified third-party tasks with provenance cards
│   ├── tasks/                 lab-authored tasks
│   ├── benchmarks/            pinned frontier benchmark ingests (INGEST)
│   ├── adapters/              benchmark → Harbor converters
│   └── registry/              task admission and execution trust records (REGISTER)
│
├── research/                  WHAT WE LEARN — produced knowledge
│   ├── experiments/           experiment specs and matrices
│   ├── calibration/           judge ground truth: corpora, answer keys, labels
│   ├── explorations/          capability recon: demos + adoption notes
│   ├── analysis/              reusable analysis queries
│   ├── evidence/              reviewed, immutable control bundles
│   └── registration/          task review packets and admission audits (REGISTER)
│
├── policy/                    THE HUMAN'S STEERING WHEEL — standing approvals,
│                              canary suite. Peter-owned content; deliberately
│                              at root for visibility.
│
├── src/                       Platform implementation
├── tests/                     executable contracts for repository behavior
├── sql/                       schema and analysis views
├── scripts/                   operator and CI tooling
├── dashboard/                 read-only research overview (Streamlit app +
│                              explorer, projection, queries, own tests/).
│                              Platform lane per agents/OWNERS.md. Separate
│                              from src/ because it is a presentation surface
│                              over committed evidence, never an execution path.
├── containers/                repo-owned container entrypoints and images used
│                              by Platform services (for example state-journal);
│                              committed runtime definitions, not generated state
├── pyproject.toml             Python package and tool configuration
├── uv.lock                    locked Python dependency graph
├── Makefile                   operator command shortcuts
├── compose.yaml               local service composition
├── .github/                   CI workflows and gates (Integration lane)
├── .claude/                   checked-in Claude command configuration
├── .githooks/                 checked-in repository hook implementations
├── .env.example               environment-variable template
├── .gitignore                 generated/local path exclusions
├── .gitattributes             merge and path attributes
├── .python-version            development Python pin
│
├── authoring/                 versioned authoring templates and seed material
├── grids/                     declared experiment grid inputs
├── digests/                   the daily one-pager the human reads (committed)
├── queue/                     generated queue state (gitignored, rebuildable)
├── runs/                      generated run state (gitignored, rebuildable)
├── derived/                   generated projections (gitignored, rebuildable)
├── backups/                   nightly local PostgreSQL recovery snapshots
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
| "How do I *look* at what the lab produced?" | `dashboard/` (read-only presentation over committed evidence) |
| "What did the integrator verify by hand, on what date?" | `docs/checkpoints/` (append-only history — correct by dated note, never rewrite) |
| "What defines a repo-owned service container?" | `containers/` (Platform runtime definitions and entrypoints) |
| "What happened?" (generated, rebuildable) | `runs/`, `queue/`, `derived/`, `backups/`, catalog — never committed |
| "What happened?" (curated for humans) | `digests/`, `research/evidence/` |

Rules that fall out of the buckets:

- `library/` content is version-pinned and immutable once registered; changing
  a task means a new version, never an edit in place.
- `research/` content states its provenance (which runs, which corpus digest).
- Nothing in `agents/`, `docs/`, or `policy/` is generated; nothing in
  `queue/`, `runs/`, `derived/`, or `backups/` is hand-edited.
- Ownership boundaries (`agents/OWNERS.md` lanes; `agents/missions/ACTIVE.md`
  leases) follow these paths.

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
`src/evallab/cli.py`, four test files, `research/experiments/
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
- 2026-08-14 — ignored `backups/` declared for nightly PostgreSQL recovery
  snapshots (bucket rule: generated local operational state); existing ignored
  `derived/` made explicit alongside it.
- 2026-08-15 — `library/registry/` (task admission records) and `research/registration/`
  (task review packets and admission audits) added for REGISTER role.
- 2026-08-15 — M001: `agents/OWNERS.md`, `agents/missions/`, `agents/archive/`
  added; ROLES.md reduced to a pointer (historical table archived).
- 2026-08-15 — COORD-GC: map corrected against `git ls-tree origin/main`, not
  restructured. Nothing moved in the repository; only this file changed.
  - `dashboard/` added. It has existed at the root since DASHBOARD (PRs #11 and
    #15) and appeared **nowhere** in this file — an already-merged root-freeze
    violation, meaning the binding map has been silently untrue for a top-level
    entry. Bucket: Platform lane per `agents/OWNERS.md`; it is a read-only
    presentation surface over committed evidence, not an execution path, which
    is why it is its own entry rather than part of `src/`.
  - The `docs/` submap listed 6 of the 28 committed entries. All 28 are now
    named, including `docs/checkpoints/` (dated integrator verification
    records), `docs/research/`, and the four hand-authored `.html` renders with
    `repository-overview.css` — whose keep-or-archive question is now the one
    open item under `Needs Peter` in `agents/missions/ACTIVE.md`.
  - `agents/CHECKS.md` added to the `agents/` submap (also previously absent).
    `agents/archive/` now documents dated handoff subdirectories, and
    `agents/handoffs/` is stated to hold live missions only, which is the
    `ACTIVE.md` policy this mission executed.
  - `.github/` and the root config dotfiles added: they are frozen-root entries
    that the map did not name.
  - Ownership labels refreshed from `agents/OWNERS.md`: `src/ tests/ sql/
    scripts/` is Platform lane, not "BUILDER-owned" (a codename retired by
    M001). Placement-guide rows added for `dashboard/` and `docs/checkpoints/`.
- 2026-08-23 — `containers/` declared after PR #146 merged the
  `containers/state-journal/` runtime. Bucket: Platform-owned service container
  definitions and entrypoints; unlike `runs/` or `derived/`, these sources are
  committed and reviewed.
