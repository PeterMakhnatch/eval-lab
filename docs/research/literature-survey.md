---
status: historical
audience:
  - builder
  - analyst
---

# Literature survey: scaffold effects, benchmark contracts, context degradation

Role: DATA-STRATEGY. Date: 2026-08-15. Sources are arXiv IDs, upstream repos
pinned in this workspace, and this lab's own audits. Every claim is cited;
judgment is marked as such.

## 0. Citation correction (read first)

The mission brief cited "The Scaffold Effect in Coding Agents (arXiv:2502.12151)".
**That ID resolves to "VoLUT: Efficient Volumetric streaming enhanced by
LUT-based super-resolution" — a video-compression paper.** No paper titled "The
Scaffold Effect in Coding Agents" exists on arXiv under that ID; the citation
appears to be a confabulated reference. The *phenomenon* it names is real and
well studied under other titles. This survey covers the actual literature:

| Paper | ID | What it contributes |
|---|---|---|
| On Randomness in Agentic Evals | arXiv:2602.07150 | Quantifies run-to-run variance; the strongest "scaffold effect" numbers |
| Building Effective AI Coding Agents for the Terminal (OpenDev) | arXiv:2603.05344 | Engineering catalog of harness variables |
| Inside the Scaffold: A Source-Code Taxonomy of Coding Agent Architectures | arXiv:2604.03515 | Taxonomy of scaffold architectures from source |
| Can Generalist Agents Automate Data Curation? | arXiv:2606.04261 | Light-vs-heavy scaffold effect on outcome variance |
| From QA to Task Completion: Survey on Agent System and Harness Design | arXiv:2606.20683 | Minimal-scaffold-with-good-primitives finding |
| Beyond the Leaderboard: Synthesis of Tool-Use, Planning, Reasoning Failures | arXiv:2607.05775 | Six-cluster failure taxonomy |
| Agentic Harness Engineering | arXiv:2604.25850 | Automated harness evolution from observability data |

## 1. The scaffold effect, quantified

**Headline numbers (arXiv:2602.07150; 60,000 trajectories, SWE-bench-Verified,
3 open models × 2 scaffolds × 10 runs, 25.58B tokens, 1.88M tool calls):**

- Single-run pass@1 ranges span **2.2–6.0 percentage points** across identical
  configurations; σ > **1.5pp even at temperature 0**.
- Detecting a 2pp improvement needs **~9 independent runs**; a 1pp improvement
  needs **~36 runs** (σ=1.5pp, p<0.05, 80% power).
- Gap between optimistic pass@k and pessimistic pass^k bounds reaches
  **24.9pp**.
- The same model shows *different variance profiles under different scaffolds*
  (nano-agent vs R2E-Gym) — scaffold choice changes not just the mean but the
  distribution.

Corroboration: arXiv:2606.04261 finds light scaffolds **reduce outcome
variance without improving the best outcome**, while heavy scaffolds shift
outcomes substantially in either direction. arXiv:2606.20683 concludes scaffold
effectiveness depends on **interface design more than feature count** — minimal
scaffolds with well-chosen primitives approach full-framework performance.

**Harness variables that move results** (arXiv:2603.05344, engineering report —
directional, no effect sizes published):

1. **Prompt composition** — modular, priority-ordered system-prompt sections;
   provider-conditional sections for cache efficiency.
2. **Error feedback timing** — event-driven reminders injected at decision
   points to counteract "instruction fade-out" in long sessions (vs
   front-loading everything in the system prompt).
3. **Edit/diff format** — a 9-pass fuzzy-match pipeline for file edits;
   robustness of the edit tool is itself a capability variable.
4. **Context management** — five-stage progressive compaction; episodic vs
   working memory separation.
5. **Tool surface** — lazy tool discovery (MCP) to keep tool descriptions from
   consuming the prompt budget.

**Takeaways for eval-lab:**

- **T1.** Single-attempt cohort comparisons cannot detect <3pp differences.
  The lab's experiment specs (`schemas.ExperimentSpec.attempts`) should default
  to ≥5 attempts for any claim of harness improvement, and cohort reports must
  carry σ, not just means. (Directly implementable in `cohort.py` reporting.)
- **T2.** "Agent" must be recorded as (adapter, version, model, prompt digest) —
  the scaffold is a first-class experimental variable, never a constant. The
  reproducibility contract in `docs/architecture.md` already lists this; the
  Parquet projection must carry it per-trajectory so cross-scaffold queries are
  possible (see P4 queries).
- **T3.** Temperature 0 does not buy determinism. Never treat two runs as
  A/A-identical; store per-run variance explicitly.

## 2. Benchmark contracts: Terminal-Bench 2/3 and SWE-bench

**Version correction:** there is **no Terminal-Bench 4.0** as of 2026-08-15.
The lineage is TB1 → TB2 (89 tasks) → TB2.1 → **TB3 = Frontier-Bench v0.1**
(74 tasks, 7 domains, semver, continuously evolving). The mission brief's
"2.0/3.0/4.0" reflects a version that does not exist yet.

### TB2 → TB3: what changed and why (verified against local clones)

This lab holds first-hand evidence: `~/Developer/agent-evals/frontier-bench@3d694e91`
(audited in the fin-saccr-rwa study) and the curated library's 19 re-verified
tasks (`library/curated/README.md`).

| Contract element | TB2 | TB3 / Frontier-Bench |
|---|---|---|
| Verifier location | agent's container (shared) | **separate container** built from `tests/`, artifacts explicitly declared (`environment_mode = "separate"`) |
| Verifier network | runtime `curl`/`uv` downloads | **forbidden at trial time**; deps baked into tests/Dockerfile (`checks/check-trial-network-fetch.sh`) |
| Anti-gaming | none systematic | cheating-agent CI runs, canary GUIDs in every text file, 23 static checks |
| Timeouts | shorter | agent 1.5–8h (`timeout_sec` 5400–28800 across 74 tasks), verifier ≤600s typical |
| Resources | modest | 45/74 tasks ≤8GB; 24 heavier; 4 GPU-only (this lab's `docs/execution-tiers.md`) |
| Reward | binary `reward.txt` | binary, plus per-test CTRF sidecar |

The forcing event: Berkeley RDI showed a fake `curl` wrapper scored **89/89 on
TB2 with zero tasks solved** (82/89 tasks fetched `uv` at verification time).
Terminal Wrench (arXiv:2604.17596) generalized this: 331 demonstrably hackable
environments, 3,632 exploit trajectories; >15% of surveyed verifiers bypassable.

### SWE-bench family contract

Task = repo snapshot + issue text; verification = FAIL_TO_PASS + PASS_TO_PASS
test sets executed post-patch; reward binary. Weaknesses (motivating Pro/Live
variants): training-set contamination of popular repos, saturation (~75% on
Verified), and solution leakage in issue threads. SWE-bench Pro answers with
copyleft/commercial held-out repos; SWE-bench-Live/LiveCodeBench answer with
post-cutoff continuous refresh — the only *structural* contamination defense.

**Takeaways for eval-lab:**

- **T4.** The lab's synthetic-task gate (P5) must adopt TB3's full contract:
  separate verifier, no trial-time network in tests, declared artifacts,
  cheating-agent pass, canary strings. This is now the documented floor, not
  best practice.
- **T5.** Timeout is a *task-schema field with analytical value*: TB3 timeouts
  span 1.5–8h and timeout-vs-solve interacts with model strength (Harbor-Index
  found open-weight models time out 3–4× more often than frontier models).
  Timeouts must be first-class in the Parquet projection.
- **T6.** Binary rewards hide diagnosis. TB3's CTRF per-test sidecar is the
  model: keep the headline binary, store the full check vector (the lab's
  Postgres `rewards` table + verifier stdout already do this — preserve it in
  the Parquet layer too).

## 3. Context degradation and long-horizon failure

**Established results:**

- **Context rot** (Chroma, 18 frontier models): every model tested degrades as
  input length grows, on tasks whose difficulty is otherwise constant.
- **Long-horizon cliff** (METR time-horizon work): near-100% success on tasks
  taking skilled humans <4 minutes; <10% on tasks taking >4 hours.
- **Embedding effect**: web-agent tasks solvable at 40–50% in short-horizon
  form drop **below 10%** when embedded in a longer interaction history — even
  with all relevant information still in context.
- **Coding agents maximize context rot** by construction: accumulative tool
  output, high distractor density from code search, 15–60min+ horizons.
- **Mitigation magnitudes** (Anthropic evals): context editing alone +29%;
  editing + memory tool +39%.
- **Failure synthesis** (arXiv:2607.05775, six clusters): tool
  invocation/parameter errors; planning & constraint-satisfaction failures;
  long-horizon degradation from context accumulation; multi-agent coordination
  failures; safety/security under adversarial or underspecified conditions;
  measurement-validity problems. Failures **compound nonlinearly with task
  length**; sub-task competence does not compose into end-to-end success.

**Takeaways for eval-lab:**

- **T7.** Context growth is a measurable trajectory property, not an anecdote.
  The Parquet projection must support computing **tokens-in-context per step**
  so Context Bloat Velocity (P4) is a queryable fact.
- **T8.** The six-cluster taxonomy from arXiv:2607.05775 is the peer-reviewed
  anchor for this lab's failure buckets; P4's operational buckets (Flaky
  Verifier, Tool Hallucination, Timeout, Surrender) map into clusters 1, 3, and
  6 and should be documented as such rather than invented ad hoc.
- **T9.** Because failures compound nonlinearly, *where* in a trajectory a
  failure occurs matters as much as whether one occurs. Step index and
  cumulative-token position belong on every extracted failure fact.

## 4. What this survey changes about lab priorities (judgment)

1. Variance-aware reporting (T1/T3) is cheap and immediately raises the
   evidential standard of every cohort comparison the lab produces.
2. The scaffold is the lab's most promising *independent variable*: the
   literature says harness deltas of several pp are common, and the lab is
   uniquely instrumented (one harness, many adapters, ATIF everywhere) to
   measure them honestly.
3. Long-horizon/context metrics are underserved publicly (no benchmark measures
   multi-session work; TB3 tasks are single-goal). The lab's trajectory
   intelligence layer (P4) can produce novel, publishable measurements from
   data it already generates.
