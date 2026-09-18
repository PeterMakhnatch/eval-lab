---
status: living
audience:
  - builder
  - analyst
  - operator
updated: 2026-09-16
---

# Existing agentic benchmarks on Harbor: shortlist and dev cohort (HAR-61)

Question: which **existing** agentic benchmarks actually run on Harbor today,
so DSPy/GEPA optimization and later SFT trajectory collection have real tasks
to work on? Scope per Peter (2026-09-16): basic setup, exclude Terminal-Bench
3 and 4 entirely, Terminal-Bench 2 is fair game as development data. Evidence
below is source-verified (repo README/LICENSE/task trees read directly; HF
cards; live `harbor download`).

## Shortlist

| Family | Source / version | Harbor route | Tasks | License | Verdict |
|---|---|---|---|---|---|
| **Terminal-Bench 2** | Harbor registry pin `terminal-bench@2.0` (89 tasks; GitHub `harbor-framework/terminal-bench-2` mirror shows 51 + Apr-2026 metadata commits) | native: `harbor download terminal-bench@2.0`; packages are Harbor task.toml with pinned prebuilt images | 89 | Apache-2.0 | **runnable-now (primary)** |
| **FACET-Terminal** | `github.com/StoKou/FACET-Terminal` + HF `FACET-Terminal/FACET-Tasks-6k` | native task.toml template; local 10-task subset already on disk | 6,020 pool (2 imported) | Apache-2.0 | **runnable-now (complementary)** |
| SWE-bench Verified | `SWE-bench/SWE-bench_Verified` | Harbor Hub dataset `swe-bench/swe-bench-verified` (official adapter, parity-verified) | 500 | MIT | qualified, deferred (heavy per-repo images; patch-producing agents) |
| MLE-bench | `openai/mle-bench` | none needed to exclude | 75 | MIT code | **resource-blocked** (Kaggle creds + 158 GB–3.3 TB + ~24 h GPU/task) |
| Terminal-Bench 3/4 | — | — | — | — | **excluded by Peter directive** |

## Why TB2 is the primary

- Native Harbor packages, no adapter: every one of the 89 downloaded tasks
  carries `environment/` (Dockerfile or a pinned `docker_image` tag),
  `tests/test.sh`, and `solution/solve.sh` (89/89 each, counted on disk).
- Oracle-complete: 100% solution coverage makes free oracle/nop controls and
  SFT-grade reference trajectories possible today.
- Capability spread: 16 categories counted from manifests (26
  software-engineering, 9 system-administration, 8 security, 8 data-science,
  8 scientific-computing, 5 debugging, 5 file-operations, …1 video-processing).
- Deterministic pytest verifiers, binary terminal reward — sufficient for
  DSPy/GEPA (a terminal reward per task across a cohort is a valid training
  signal; fractional rewards are not required).

Known limitation kept on the record: TB2's stock verifier runs in the agent's
container (shared mode). Fine as a terminal benchmark signal; not
tamper-proof against adversarial agents. If a hardened reading is needed,
wrap the specific task like `library/tasks/terminal-bench-html-js-filter`
already does (separate verifier image).

## Why FACET is the complementary family

Artifact/data-synthesis tasks (consolidate captured pipeline outputs into a
unified record) — a different capability slice from TB2's build/repair bent,
Harbor-native by construction, and the upstream 6k pool plus 1.2k curated
rollout trajectories give headroom for scaling optimization and SFT later.
Two tasks imported for the dev cohort (pipeline-record consolidation and a
three-deliverable cross-validation variant).

## Cohort materialized

`research/experiments/bench-cohort-2026-09-16.json` pins the immutable dev
cohort (digests computed by the Lab registry):

- **development**: 6 TB2 tasks stratified by category + 2 FACET tasks
- **validation**: 3 TB2 tasks from disjoint categories, scored but never
  optimized on
- sealed exclusions: TB3, TB4 (directive), MLE-bench (resources),
  SWE-bench Verified (deferred with route documented)

Imported through the existing batch interface
(`evallab tasks import`, resumable ledger, digest-suffixed destinations).

## Consumer wiring (the point of all this)

GEPA/DSPy get one manifest and one command per task:

```
uv run evallab run --task library/tasks/experimental/bench-tb2-dev/<task-dir> \
  --agent zai-opencode --model zai-coding-plan/glm-5.3-flash \
  --name <unique> --jobs-dir runs
```

- DSPy/GEPA: optimize against the **development** group; report on
  **validation**. The evaluator contract (task feedback = reward + ctrf
  checks) already matches what `evallab run` emits.
- SFT: collect actual model traces on development tasks (oracle traces are
  reference material, not training data by themselves).
- Difficulty baselining: the same command with `--agent zai-opencode` is the
  first billable measurement; oracle/nop controls are free and already proven
  (canary below).

## Canary evidence

`db-wal-recovery` (TB2 file-operations task) through normal Lab startup:
oracle and nop trial receipts recorded in `runs/har61-canary-*` (see
`lin review` on HAR-61 for exact rewards). This establishes operability of
the route, not difficulty or training value.

## What this is not

Not a new benchmark, not admission, not a model claim. TB2's own upstream
verifiers and fixtures are trusted as shipped for development use; nothing
here weakens the Lab's admission gates for curated library tasks.
