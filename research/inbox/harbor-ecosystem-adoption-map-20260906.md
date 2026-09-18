# Harbor ecosystem — what people build, and what we should adopt

Author: wK:p7 (Fable 5.1, Analyst). 2026-09-06. Sources: Peter's Exa agent runs
`aed186e6` (03:00) + `b763ef6e` (03:37, 36 resources / 6 categories) plus eight targeted
Exa `/search` queries of my own (adapters, task tooling, viewers, recovery, GEPA, jobs,
SFT docs, TB task authoring). Read-only. Goal frame: build useful things, learn Harbor /
evals / agentic AI, get a job. Not novel research.

## 1. Read of Peter's query

Good: the query was broad and the follow-up (sort by month, categorize, one-line TLDR)
turned 36 items into a usable table. It correctly surfaced the two clusters that matter
most for us: **synthetic terminal environments** (SETA, LiteCoder-Terminal, TerminalWorld,
Recursive Synthesis, FACET, Endless Terminals, Repo2RLEnv) and **data recipes**
(OpenThoughts-Agent, TMax, Harbor SFT/RL docs).

Missed (found by my queries): the **operational** layer people actually work in day to
day — Harbor's own `harbor traces` (SFT export), `harbor view` (trajectory UI),
`atif-lens`, Phoenix ATIF import, the Terminal-Bench **task review automation /
REVIEWING / CONTRIBUTING** process, Recovery-Bench's Harbor sandbox integration, the
GEPA Terminus example, and the "Continuous Validation and a Patch for Terminal-Bench 2.0"
paper (task-validity auditing). These are the adoptable pieces; the research papers are
mostly things to *read*, not run.

## 2. The kinds of things people build on Harbor

| Kind | What it is | Examples | Runs on a Mac? |
|---|---|---|---|
| A. Task corpora / benchmarks | Harbor-format task packs with verifiers | Terminal-Bench 2.1/3.0, terminal-bench-science, frontier-bench, MLS-Bench, cline-bench, CVE-Bench, PTXBench, ScienceAgentBench adapter, data_agent_harbor_eval | Yes (Docker) |
| B. Task generators | Turn something (recordings, skills, repos, DAGs) into executable verified tasks | TerminalWorld (recordings), FACET (skills), Repo2RLEnv (repos), SETA / LiteCoder / Recursive Synthesis / Endless Terminals (synthetic) | Generators need an LLM endpoint; outputs run locally |
| C. Task quality tooling | Validity checks: oracle passes, NOP fails, mutants fail, review automation | TB `TASK_REVIEW_AUTOMATION.md`, `REVIEWING.md`, "Continuous Validation and a Patch for TB 2.0" | Yes |
| D. Trajectory tooling | ATIF viewers, exporters, observability | `harbor view`, `harbor traces`, atif-lens, Phoenix ATIF import, dreadnode/agent-lens, mira::trajectory | Yes |
| E. Failure-derived evals | Reuse failed runs as new evals | **Recovery-Bench** (replay failed trajectory, fresh agent recovers, original verifier) | Yes (Docker, `--env` flag) |
| F. Agent/harness optimization | Improve the agent without training weights | GEPA on Terminus (cookbook + Cerebras example), HarnessBridge, "Do Agent Optimizers Compound?" | Yes (API model) |
| G. Data recipes → SFT | Filter/select/format trajectories | Harbor SFT docs (ShareGPT export + HF upload), OpenThoughts-Agent, TMax, ADP | Export yes; training needs external GPU |
| H. RL stacks | Rollouts with tokens/rewards → GRPO etc. | SkyRL Harbor integration, Polar, rLLM, NeMo Gym, ApexAgents-SkyRL | No (GPU) — read only |
| I. Sandbox providers | Remote execution backends | Daytona, Modal, E2B, TensorLake, Runloop | Optional |

## 3. What to adopt — ranked by (runs here) x (fills our gap) x (job signal)

1. **Contribute one task to Terminal-Bench 3.0** (kind A+C). Public, reviewed by
   maintainers, on a repo with 600 stars and 400 forks. Uses exactly the oracle/NOP/mutant
   discipline we already enforce in `synthetic_cert.py`. Learning: the real bar for task
   quality. Job signal: the strongest single artifact on this list — a merged PR to the
   benchmark hiring managers name. Start: `CONTRIBUTING.md` + `REVIEWING.md`; seed from
   our BFCL four-gate finding or the tau credential-integrity finding.
2. **Run Recovery-Bench on our own failed trajectories** (kind E). It is literally our
   thesis — failures → environments — shipped as a tool with a Harbor sandbox flag. Feed
   it our action-memory / tau failures once a clean trial exists; until then run its own
   task set locally to learn the replay mechanics. Learning: replay drift, oracle validity
   after state corruption (the exact caveats our Track C worries about).
3. **Adopt Harbor's `traces` + `view` + atif-lens instead of building** (kind D+G).
   `harbor traces` exports trials to ShareGPT/HF — that is Track A's serializer, done. Keep
   our admission/authority layer (Track A's real value); delete any renderer duplication.
   `harbor view` / atif-lens replace any home-grown trajectory viewer. Cheap, immediate.
4. **GEPA on Terminus over our task families** (kind F). No weights, API model, runs
   overnight on a Mac; produces a measurable before/after on our own verifiers. It is the
   cheapest "improve the agent" experiment that yields a number, and it teaches the
   optimization loop without a trainer.
5. **Study, then borrow from, FACET and TerminalWorld** (kind B). Both are real Harbor
   task generators with released packages (6,020 and 1,530 tasks). Do not run the
   generators first; run the released tasks locally, read their verifiers, and compare
   against our `synthetic_transform.py` families. The borrowable pieces: environment
   construction + repair loop (FACET), recording→task extraction (TerminalWorld).
6. **OpenThoughts-Agent as the reference data recipe** (kind G). When a clean corpus
   exists, its curation ablations are the map for our arm design. Read now, use later.

Do NOT adopt now: SkyRL / Polar / rLLM / NeMo Gym / ApexAgents (GPU RL stacks — read the
READMEs, don't install); SPADE self-play (already ruled: designer-pattern only); any new
sandbox provider (Docker on Mac is fine until it isn't).

## 4. How these map onto eval-lab

| Ours | Ecosystem equivalent | Action |
|---|---|---|
| `synthetic_cert.py` 8-point certificate | TB task review automation; TerminalWorld's 3-trial validity | Align our certificate wording to TB's checklist; cite it |
| Track A export | `harbor traces` (ShareGPT/HF) | Keep admission/authority; use `harbor traces` as the serializer reference |
| Track B deficit miner | Recovery-Bench's "failed trajectory" input | Feed miner-certified failures to Recovery-Bench replay |
| Track C `synthetic_transform` | FACET / TerminalWorld generators | Borrow environment-repair loop; compare verifier strictness |
| `traj.py` outline / home viewer | `harbor view`, atif-lens, Phoenix | Stop building viewers |
| lessons / calibration | GEPA feedback loop on Terminus | Run GEPA as the first optimization experiment |

## 5. Pins (verify before acting)

- Harbor docs: tasks, task-difference, creating-tasks, publishing, SFT workflow, `traces`, `view`, ATIF RFC 0001.
- TB: `harbor-framework/terminal-bench` CONTRIBUTING.md, docs/REVIEWING.md, docs/TASK_REVIEW_AUTOMATION.md; `terminal-bench-2-1/tasks`.
- Recovery-Bench: `letta-ai/recovery-bench` (Harbor `--env` flag commit 36e767d); blog 2025-08-27.
- GEPA: `CerebrasResearch/gepa/src/gepa/examples/terminal-bench/train_terminus.py`; harbor-cookbook gepa.
- FACET: arXiv 2608.18580 + HF `FACET-Terminal/FACET-Terminal-Tasks-6k`; TerminalWorld: arXiv 2605.22535 + HF `EuniAI/TerminalWorld`.
- Task validity: "Continuous Validation and a Patch for Terminal-Bench 2.0" (OpenReview AhXMZPnOPS).
- Viewers: atif-lens (npm), Arize Phoenix ATIF import, dreadnode/agent-lens.

## 6. Play sequence (chosen path, 2026-09-06, after Exa agent run 4444cdcf + library cross-check)

Correction to section 3: **Recovery-Bench stays on the library's STRICT HOLD** (no root
license; upstream replay failed 11/20 audited instances; selection on reward==0 with no
clean twin). The native Harbor alternative for failure reuse is trajectory seeding
(`--load-trajectory`, harbor PR #2529) or `harbor traces export --filter failure` — no
license risk, no uncertified replay.

The intervention lever that matches "make a skill, see it used, measure it" is native:
**`harbor run --skill ./skills/my-skill`** (official docs, run-jobs/skills). Skills are
directories with SKILL.md copied into the sandbox; local path or org/repo@main; repeatable.
Academic backing exists for measuring exactly this: SkillsBench (2602.12670), "The
Regression Tax" (2607.22520), SWE-Skills-Bench (2603.15401), "Demystifying Agent Skills"
(2608.14036), SkillEval (2608.06891).

Ordered play sequence (each step produces data our stack already ingests):

1. **Oracle smoke (free):** `harbor run --dataset terminal-bench@2.0 --agent oracle
   --n-concurrent 4` — validates the pack and our Docker setup; zero API cost.
2. **First ATIF runs:** `harbor run --dataset terminal-bench@2.0 --agent terminus-2
   --model <cheap-api-model> --n-concurrent 2 --export-traces` on a 10-task slice.
   Output: jobs dir with trajectory.json per trial (steps, tool_calls, observations,
   metrics) — the exact shape `traj.py` ingests. Also `harbor view jobs` and
   `npx atif-lens <trajectory.json>` for eyeballing; Phoenix ATIF import for span trees.
3. **Native analysis pass:** `harbor traces export --path <job> --sharegpt` (and without
   --sharegpt for ATIF rows with reward/success/metadata); then our own pass@k / pass^k,
   cost-per-task, per-step tool-call counts, TER/CBV-style features from `traj.py` on the
   same bytes. Reference implementation to diff against: `langchain-ai/deepagents
   libs/harbor/scripts/analyze.py`; NeMo `eval_harbor_atif_interop.ipynb`.
4. **Skill intervention (the fun one):** write `./skills/debugging-discipline/SKILL.md`
   encoding one of our certified lessons (e.g. post-mutation inspection, no blind
   identical retry); re-run the SAME slice with `--skill`; diff success rate, cost,
   tool-call patterns, blind-retry counts per trial. That is a real measured intervention
   with zero training. Repeat with a second skill; keep a two-arm ledger.
5. **GEPA prompt intervention:** bespokelabsai/gepa-terminus2 (`train.py
   --max-metric-calls 50`) or cookbook GEPA on one small family; overlay best prompt;
   re-measure. Second intervention type, same measurement harness.
6. **Failure reuse, natively:** `harbor traces export --filter failure`; re-run those
   tasks with `--load-trajectory <trajectory.json>` (seeding) or let a fresh agent
   inherit state via `--no-delete` envs; measure recovery rate against the unchanged
   verifier. Methodology from Recovery-Bench, executed with zero license/replay risk.
7. **External packs as new task distributions:** TerminalWorld 20-task sample, then
   FACET 6k subset, through the same pipeline — new families for the ledger.
8. **Data recipe:** once >=200 trials accumulate, `harbor traces export --filter success`
   + OpenThoughts-Agent curation notebook = your first honest SFT corpus view (export
   only; training stays external per charter).

Suggested model: cheapest API model that clears the pack (flash-tier) for baselines;
one strong-model pass later for contrast. Keep slices small (10 tasks x 2 trials)
until the loop is smooth; a full loop costs single-digit dollars.
