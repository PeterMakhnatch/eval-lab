# Repository structure

**Binding.** This file is the single source of truth for what may exist at the
repository root and where new things go. The root is **frozen**: creating a
new top-level entry requires editing this file in the same PR, with one
sentence saying which bucket rule made a new entry necessary. If you cannot
name the bucket something belongs to, you do not create it — you propose a
change here first.

## The map

```
eval-lab/
│
├── AGENTS.md                  repository rules — every agent reads this first
├── README.md                  human orientation
├── LICENSE                    Apache-2.0 license text
│
├── agents/                    HOW WE WORK — coordination and governance
│
├── docs/                      WHY IT'S BUILT THIS WAY — design and decisions
│                              Preserve the hand-authored HTML overviews and
│                              their stylesheet until Peter decides their disposition.
│
├── library/                   WHAT WE EVALUATE — task supply, version-pinned
│
├── research/                  WHAT WE LEARN — produced knowledge
│
├── policy/                    THE HUMAN'S STEERING WHEEL — standing approvals,
│                              canary suite. Peter-owned content; deliberately
│                              at root for visibility.
│
├── src/                       Platform implementation
├── tests/                     executable contracts for repository behavior
├── sql/                       schema and analysis views
├── scripts/                   operator and CI tooling
├── tools/                    isolated locked uv projects for external
│                              toolchains the lab shells out to by subprocess
│                              and never imports (own uv.lock; never root
│                              workspace members)
├── containers/                repo-owned container entrypoints and images used
│                              by Platform services (for example state-journal);
│                              committed runtime definitions, not generated state
├── pyproject.toml             Python package and tool configuration
├── uv.lock                    locked Python dependency graph
├── Makefile                   operator command shortcuts
├── compose.yaml               local service composition
├── .github/                   CI workflows and gates (Integration lane)
├── .omp/                      project-native OMP capabilities; scoped,
│                              on-demand skills rather than sticky policy
├── .claude/                   checked-in Claude command configuration
├── .githooks/                 checked-in repository hook implementations
├── .env.example               environment-variable template
├── .gitignore                 generated/local path exclusions
├── .ignore                    search exclusions (archive directories, sealed material)
├── .gitattributes             merge and path attributes
├── .python-version            development Python pin
│
├── authoring/                 versioned authoring templates and seed material
├── grids/                     declared experiment grid inputs
├── digests/                   the daily one-pager the human reads (committed)
├── queue/                     ignored leases, specs, and append-only event history
├── runs/                      ignored Harbor evidence and operational scratch
├── derived/                   mixed durable CAS evidence and derived projections
├── backups/                   ignored database and repository recovery material
└── .worktrees/                parallel working trees (gitignored, hidden)
```

## Placement guide

Ask which question the thing answers:

| The thing answers… | It goes in |
|---|---|
| "How do agents coordinate?" | `agents/` |
| "Why is the system designed this way?" / "What should be built?" | `docs/` |
| "What can we run an agent against?" | `library/` |
| "What did we find out / what is our ground truth?" | `research/` |
| "What is the lab allowed to do unattended?" | `policy/` |
| "How does the lab software work?" | `src/` (+ `tests/`, `sql/`, `scripts/`) |
| "What did the integrator verify by hand, on what date?" | `docs/checkpoints/` (append-only history — correct by dated note, never rewrite) |
| "What defines a repo-owned service container?" | `containers/` (Platform runtime definitions and entrypoints) |
| "What is local runtime or recovery state?" | `runs/`, `queue/`, `derived/`, `backups/`, catalog — determine authority before cleanup |
| "What happened?" (curated for humans) | `digests/`, `research/evidence/` |

Rules that fall out of the buckets:

- `library/` content is version-pinned and immutable once registered; changing
  a task means a new version, never an edit in place.
- `research/` content states its provenance (which runs, which corpus digest).
- Most documentation in `docs/` is hand-authored design and policy; the
  status snapshot `docs/STATUS.md` is generated via `evallab status --update`.
  Nothing in `queue/`, `runs/`, `derived/`, or `backups/` is hand-edited.
- The task defines write scopes. Ownership follows each path's subject.
- Folder-doc convention: declared top-level directories carry a bounded README.
  Source subpackages inherit `src/evallab/AGENTS.md`; add a local `AGENTS.md`
  only for distinct package-specific guidance. Existing local overrides remain
  capped at 60 lines. See Folder documents below for the exemptions.

## Folder documents

Every declared top-level directory in this file's map carries one `README.md`.
Source subpackages may inherit `src/evallab/AGENTS.md` instead of duplicating
the same instructions. A local `AGENTS.md` is appropriate when a package has
distinct invariants or procedures; its presence does not disable parent rules.

Top-level folder documents and explicit package overrides are capped at 60
lines and use these five sections in order:
1. Purpose (2–3 lines)
2. What lives here / entry points
3. Invariants or rules
4. Tests or checks
5. What not to add here

Exemptions from this requirement:
- Dot-directories (`.github`, `.githooks`, `.omp`, `.claude`, and gitignored dot roots)
- Runtime or ignored roots (`derived`, `runs`, `queue`, `backups`, `exports`, `logs`)
- `docs/`

Presence and line-length limits are enforced deterministically by
`python -m evallab.governance check`.

