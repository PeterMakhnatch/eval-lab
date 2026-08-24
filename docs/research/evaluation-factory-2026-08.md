---
status: living
audience:
  - builder
  - analyst
---

# Evaluation-factory audit and build program (2026-08)

Role: orchestrator/builder session, at Peter's direction (brief:
`/private/tmp/eval-lab-orchestrator-brief.md`, 2026-08-23). This note records
the audit of eval-lab against the "evaluation factory" contract set, the
reuse/build/adapter decisions for seven upstream projects, the vertical-slice
designs, the backlog, and every source pin. Companion docs:
`docs/research/synthetic-tasks.md` (the Zone 03 blueprint this program starts
implementing), `docs/data-architecture.md` (binding zone rules),
`docs/research-questions.md` (what the lab studies).

Method note: upstream repositories were inspected read-only at pinned commits
in ephemeral scratch clones (deleted after reading); nothing upstream was
vendored. Eight bounded investigations (six upstream, Harbor 0.21.0, and the
in-repo contract audit) produced the evidence below; implementation facts were
verified against files in this repository and in the installed Harbor package.

## 1. Current-state architecture: the factory contracts already exist

The brief asked which of eight abstract contracts exist, under their actual
names. Verdict: **seven of eight are present**; the eighth (a deterministic,
sequence-first task generator) was designed (`docs/research/synthetic-tasks.md`)
but unimplemented, and is what this program builds first. Do not add parallel
abstractions for the other seven.

| Brief contract | Actual implementation | Evidence | Coverage |
|---|---|---|---|
| TaskGenerator | `authoring.py` (proposal pipeline, seed classes, battery), `task_workbench.py` (candidate inspection + local controls), `craft.py` (facet coverage), `ladder.py` (`GridSpec`), SG missions (`docs/prompts/synthesis-build.md`) | `schemas.py` `ProposalSpec`/`InversionSpec`/`AuthoringSeedClass` (~1461-1529) | Partial → **`seqgen.py` adds the sequence-first generator** |
| EnvironmentAdapter | Harbor `BaseEnvironment` (docker/modal/gke providers); the lab selects, never reimplements | `harbor/environments/base.py:84`; `runner.py` `RunRequest`/`build_command` | Covers |
| AgentAdapter | Harbor `BaseAgent` + `AgentFactory` (30+ adapters incl. `oracle`/`nop`); lab side: `profiles.py`, `modeladapter.py`, `runner.py` `LOCAL_TO_HARBOR_MODEL` (~423) | `harbor/agents/base.py:23`, `harbor/agents/factory.py:24` | Covers |
| Verifier | Harbor `Verifier` (`tests/test.sh` → `/logs/verifier/reward.json`); lab ingest: `results.py`, `ingest_verify.py`, `facts.py` | `harbor/verifier/verifier.py:44,64-92` | Covers |
| TraceNormalizer | `atif.py` (ATIF→facts/Parquet), `traj.py`, `parquet_compaction.py`, `lance.py`; ATIF-v1.7 is the canonical trace format | `harbor/models/trajectories/trajectory.py:13` | Covers |
| Analyzer | Deterministic: `facts.py`; model-assisted: `analyst.py` + `analysis_worker.py` behind the `modeladapter.py` seam, provenance in `TrialAnalysisSidecar`/`AnalysisProvenance` (`schemas.py` ~532-600); aggregates: `lessons.py`, `cohort.py`, `behavior.py`, `calibrate.py` | — | Covers (diagnosis taxonomy = slice B) |
| CampaignStrategy | `queue.py` (specs, leases, policy gates), `automation.py` (nightly loops), `researchers.py`, `ladder.py` | `ExperimentSpec`/`ExperimentMatrix`/`GridSpec` in `schemas.py` | Covers at current scale |
| Artifact/ProvenanceStore | Immutable Harbor job dirs (Zone 02), `ProvenanceMetadata` sidecars (`schemas.py:786`), `registry.py` admission records, `lineage.py`, PostgreSQL as rebuildable catalog (`database.py`) | `docs/data-architecture.md` | Covers |

Raw execution truth vs. derived diagnosis is already structural: Zone 02 job
directories are immutable; every model-assisted claim lives in a
`TrialAnalysisSidecar` with agent/model/prompt-digest provenance and is "a
hypothesis, not ground truth" (`docs/architecture.md` §5). Slice B must land
inside that contract, not beside it.

## 2. Reuse / build / adapter decisions

| Project | Pin (commit, date) | License | Decision | Reason |
|---|---|---|---|---|
| TASTE (`tomerkeren42/TASTE-task-synthesis-from-tool-sequence-evolution`, arXiv:2605.28556) | `d53da239`, 2026-05-31 | **Proprietary**: no redistribution/derivatives until paper release | **Re-implement the idea; zero code reuse** | License forbids adaptation; code is also hard-bound to tau2-bench (`sys.path` injection, tau2 DB models) and LLM validators. The durable ideas — sequence-first generation, validity gates before NL, coverage-driven selection, mutation lineage — are re-implemented deterministically in `src/evallab/seqgen.py`. Its Bayesian signed n-gram sampler and 7-operator adversarial catalog are candidates for a later version, from the paper, not the code. |
| Exgentic (`Exgentic/exgentic`, arXiv:2602.22953) | `ae8d10f7`, 2026-06-15 | MIT | **No adoption now; adapter later if ever** | Verified: zero Harbor/ATIF integration (repo-wide grep, no matches). It duplicates Harbor's execution role with a cloudpickle-over-HTTP RPC bus; per-benchmark adapters remain heavy (SWE-bench ≈1,057 LOC, BrowseComp+ ≈1,464 LOC — domain specialization displaced into adapters, now quantified). The one idea worth borrowing when needed: `PairableProxySession` (queue rendezvous inverting benchmark-driven loops). |
| CLEAR (`IBM/CLEAR`, arXiv:2605.22608) | `9a5367bd`, 2026-07-27 | Apache-2.0 | **Adapt: thin ATIF→IR converter + prompt/rubric assets; never a core dependency** | Its input IR is a flat CSV (`task_id`, `step_in_trace_general`, `model_input`, `response`, `api_spec`, `traj_score`) — ATIF maps onto it losslessly (~100 LOC). Verifier truth (`traj_score`) stays a separate column from judge outputs; upstream has no calibration machinery (temperature-0 only), so `calibrate.py`/`JudgeCalibrationRecord` remains the calibration layer. Heavy deps (watsonx, nicegui, streamlit, langchain) stay out. |
| ADO (`IBM/ado`) | `3ad092c6`, 2026-08-19 | MIT | **Reject as engine; keep as conceptual reference** | Monolithic platform: owns Ray lifecycle, global SIGTERM handlers, 6+ tables per store. Disqualifying for evaluation work: memoization identity is `(actuator, experiment@MAJOR, constitutive values)` — minor/patch versions, code SHAs, and environment digests are **excluded** (`ado/schema/reference.py` ~236-255, ~387-410), so a changed verifier silently replays stale results. eval-lab keys every trial by content digests and never memoizes across versions; that property is non-negotiable. The constitutive-vs-observed property vocabulary is worth borrowing for future `GridSpec` extensions. |
| Unitxt (`IBM/unitxt`) | `39897986`, 2026-05-27 | Apache-2.0 | **Ignore for agentic eval** | Static prompt/dataset DSL; its "multi-turn tool calling" scores one completion against recorded history — no environment, no execution. Everything inherits its `Artifact`/`Dataclass` metaclasses, so nothing is cleanly standalone. Its perturbation-operator catalog is worth mining conceptually. |
| KCIF (`IBM/KCIF`, arXiv:2410.12972) | `dfd7a872`, 2025-05-08 | Apache-2.0 | **Re-implement the composition idea for agents (slice C)** | Scripts, not a library. The durable idea: compose a base capability with a constraint operator and pre-compute *error candidate sets* so failures decompose into reasoning vs. instruction-following. Agentic translation: environment-state verifier for the base goal + trace-derived constraint checks, over paired isolated/composed task families. |
| Harbor 0.21.0 (installed uv tool) | `harbor[modal]==0.21.0` | upstream | **Reuse as substrate (unchanged)** | `TaskConfig`/`TaskPaths`, `BaseAgent`, `BaseEnvironment`, `Verifier`, ATIF-v1.7 models, regrade, `init`/`compile`/`check`. Harbor has **no task synthesis** — generation is legitimately the lab's job. |

Adjacent-stack conclusion (web survey; author claims unless verified in code):
none of Inspect AI / BrowserGym / tau2 / BFCL / AppWorld / Terminal-Bench
synthesize tasks; APIGen and ToolBench generate but prompt-first (query before
validated execution), and APIGen is CC-BY-NC. Sequence-first generation with
environment-grounded verification was not available to reuse under a
compatible license — building it was the right call. Inspect AI remains the
"only if leaving Harbor" alternative (`docs/research-questions.md`).

The brief's caution held up in code: the IBM projects (CLEAR, ADO, Unitxt,
KCIF) share no schemas, no imports, and no execution substrate with each other
or with TASTE/Exgentic. The "survey gaps → synthesis → cross-protocol
execution → grounded verification → trace diagnosis → next campaign" loop is
our architectural synthesis; no upstream runs it as one stack.

## 3. What was built: SEQGEN v0 (slice A)

`src/evallab/seqgen.py` + `tests/test_seqgen.py` + `library/synthetic/seqgen-v0/`
(first Zone 03 batch; `library/synthetic/` is the storage boundary
`docs/data-architecture.md:37` already reserves).

Sequence-first pipeline, all deterministic, no model calls:

1. **Typed tool schema.** Seven record-pipeline ops (filter_eq, filter_ge,
   select, sort_by, dedupe_by, head, group_sum) with preconditions over the
   live dataset state, plus a terminal `write_output`. Op semantics live once,
   in the embedded `RP_SOURCE` program that ships into each task image as
   `/app/bin/rp`; the generator simulates by executing the same source — no
   dual implementation to drift.
2. **Valid-sequence generation.** Seeded random walk over precondition-valid
   (op, args) instantiations; 3–6 ops; **every step must change state**
   (filters/dedupe/group_sum strictly shrink, sorts must reorder, heads must
   truncate) — the guard that keeps generated difficulty from degenerating
   into vacuous padding.
3. **Coverage selection.** Greedy maximization of op-bigram coverage against
   the statically enumerated reachable-transition set (54 bigrams);
   `BATCH.json` reports covered/reachable and the uncovered remainder, so
   coverage is measured beyond task count.
4. **Instantiation.** Each selected sequence becomes a full Harbor task
   package mirroring `library/tasks/event-summary` (separate verifier image,
   trusted fixtures, reward vector with primary `reward` key), plus three
   adversarial probes (`workbench/adversarial/`), `generation.json` (seed,
   generator version, sequence, digests, coverage contribution, verifier
   network choice) and `provenance.json` (`ProvenanceMetadata`, zone
   `03-synthetic`, `transform="seqgen@0.1.0"`, parent digests of the domain
   spec and `RP_SOURCE`).
5. **Certification is the existing gate, not new code.** Static acceptance is
   proven in-tree by calling `task_workbench.inspect_candidate` inside the
   unit tests; control runs use the existing runner/workbench machinery.
   Zone 03 rule honoured: synthetic tasks never enter `registered/` without
   the human gate.

Verified (2026-08-24, this workstation): 10 focused tests green in <2 s
(determinism, per-step validity, simulator↔`rp` subprocess equivalence,
workbench static acceptance, leakage, adversarial wrongness, greedy-coverage
correctness, provenance round-trip, immutability, verifier-network variants);
`ruff` clean including generated task code; live local controls on the
uncommitted `inherit` scratch batch: **oracle reward 1.0** on `seqgen-s7-000`
and `seqgen-s7-003`, **nop reward 0.0** on `seqgen-s7-003`
(`runs/seqgen-smoke-*` in the mission worktree; reward vector
`{"reward","correctness","input_preservation","output_hygiene"}`).

Known, recorded limitations (v0): linear pipelines (no joins/DAGs yet); the
instruction is a deterministic declarative rendering of the goal — filters are
folded and sort+head collapse into "top-N", but instruction surface order
still correlates with op order, so v0 measures *goal execution and tool
sequencing*, not inference of underspecified intent (LLM intent rewriting is
SG/SEAM-gated follow-up); the verifier golden is simulator-derived —
independence comes from the oracle executing the same semantics in the
container plus the adversarial probes, and any certification record must say
so (`docs/research/synthetic-tasks.md` §3.C).

### Finding F-SEQGEN-1: the certification gate and local execution contradict on this machine

Discovered by smoke-running the batch, then confirmed from both sides:

- `task_workbench` v1.1 requires a separate verifier to declare
  `[verifier.environment] network_mode = "no-network"`
  (`verifier_network_not_isolated`, `task_workbench.py`
  `_validate_network_and_isolation`), because Harbor drops compose overlays
  for the verifier container.
- Harbor 0.21.0 refuses to *start* such a verifier on this workstation:
  Docker egress control is kernel-gated
  (`harbor/environments/docker/docker.py:188-195`), macOS Docker Desktop
  fails the gate, so `capabilities.disable_internet` is false and
  `validate_network_policy_support` raises
  (`harbor/environments/base.py:777`). Observed as a trial `ValueError` with
  reward absent.
- The lab's own exemplar sits on the other horn: `library/tasks/event-summary`
  executes locally and **fails today's static gate** (5 errors:
  `verifier_network_not_isolated`, unpinned agent/verifier base images,
  unpinned `source_ref`, missing adversarial probes) — measured directly with
  `inspect_candidate` at `origin/main` `8ea9f8b`.

Consequence: on this machine no task.toml spelling passes both the gate and a
local control run; certification-by-local-controls is currently impossible
for gate-clean tasks lab-wide, and the registered in-repo tasks predate the
gate's current strictness. SEQGEN makes the tension explicit instead of
hiding it: `--verifier-network no-network` (default, committed batch,
gate-clean) vs `--verifier-network inherit` (event-summary parity, locally
runnable), recorded per task and per batch. Resolving the contradiction is an
integrator/Peter decision — candidates: run control batteries on a Linux
executor; or teach the gate a machine-capability qualifier that records
enforcement provenance instead of demanding an unenforceable declaration.

## 4. The other two slices (designed, not built here)

**Slice B — trace diagnosis over ATIF (CLEAR-shaped, inside existing
contracts).** A converter `ATIF → CLEAR-IR rows` (pure function beside
`atif.py`, fixture-tested; ~100 LOC) and a diagnosis pass that runs through
`analyst.py`/`analysis_worker.py` and writes `TrialAnalysisSidecar` records —
which already carry judge agent/model/prompt digest, confidence, and evidence
citations. Node segmentation is deterministic (ATIF steps/tool_calls);
diagnosis labels are model-assisted and never touch verifier truth. Explicit
verifier: for a fixture trial with a planted defect, deterministic
segmentation must locate the step span; judge-label agreement is measured
against `research/calibration/` answer keys before any use. Blocked today by
the model seam being a refusing stub and by billable gating — correctly so.

**Slice C — compositional perturbations (KCIF for agents).** Operators over a
certified seqgen base family: distractor files in `/app/data`, instruction
degradation levels, tool constraint (remove `/app/bin/rp`), pre-broken partial
state in `/app/output`. Every variant is a new task version re-certified
oracle==1.0 / nop==0.0 (`docs/research/synthetic-tasks.md` §4 rule verbatim);
sidecar `transform` records `perturb-<op>@<ver>` with `parent_digests`
linking the base. Analysis compares isolated vs. composed scores as paired
families via `cohort.py`. Falsification: if composed scores equal the min of
isolated scores across the family, composition adds no measured signal and
the axis is dropped.

## 5. Design answers the brief required

- **Unit of an eval:** the trial `(task@version, environment, agent, model,
  verifier@digest)` (`docs/research-questions.md`); campaigns are spec'd trial
  sets; capability distributions are derived cohort statistics — never
  primary evidence.
- **Solvability independent of the generating model:** seqgen has no
  generating model — solvability is by construction (simulation) and
  independently by containerized oracle execution; LLM-authored tasks (SG-1)
  keep the battery + human gate.
- **Ground truth vs. heuristic:** environment-grounded verifier checks (exact
  artifact content vs. trusted fixtures, input preservation, output hygiene)
  are ground truth; every LLM judgment is a provenance-bearing sidecar under
  review.
- **Sequence coverage beyond task count:** op unigram/bigram coverage against
  the reachable transition set, per batch; the uncovered remainder is the
  next batch's target.
- **Difficulty without ambiguity:** perturbed variants re-certify (oracle
  exactly 1.0, nop exactly 0.0, probes 0.0); a variant that fails is rejected
  lineage, not a "hard task".
- **Generality vs. adapter engineering:** measure it — per-benchmark adapter
  LOC and injected domain knowledge are first-class numbers (Exgentic's own
  tree: ≈700–1,500 LOC per benchmark).
- **Retries/loops/recovery:** raw ATIF steps are Zone 02 evidence; loop and
  recovery features are derived (`traj.py`, `behavior.py`); harness retries
  live in queue events and `.transient-attempts`, excluded from discovery.
- **Exact replay:** immutable job dir (config, lock with task checksum,
  result, ATIF, artifact digests) + task package digest + `generation.json`
  (seed, generator version) + Harbor regrade for verifier re-runs.
- **Versions in cache identity:** the lab memoizes nothing across versions;
  identity is content digests everywhere (`TaskDigests`,
  `AnalysisSourceDigests`, provenance sidecars). ADO's major-version cache is
  the documented counterexample.
- **What would falsify "measures the named capability":** any adversarial
  probe scoring >0; nop scoring >0; a trivial baseline matching the treatment
  agent; isolated-capability scores fully predicting composed scores (slice C
  makes this testable).

## 6. Prioritized backlog

| # | Item | Depends on | Acceptance |
|---|---|---|---|
| 1 | SEQGEN v0 (this PR) | — | focused tests green; workbench static pass in-tree; oracle 1.0 / nop 0.0 local smoke |
| 2 | Decide F-SEQGEN-1 (gate vs. local Docker) | Peter/integrator | recorded decision; either a Linux control executor or a capability-aware gate |
| 3 | Certify `seqgen-v0` through the workbench battery (oracle ×3, nop, probes) and store packets | 2 | `check_candidate` pass for all 4 tasks; packets under `research/registration/` |
| 4 | SEQGEN v1: multi-dataset joins (DAG sequences), arg-shape coverage, richer datasets | 1 | new op tests; certified batch; coverage report extends |
| 5 | Slice C perturbation operators over a certified base family | 3 | every variant re-certified; paired isolated-vs-composed report via `cohort.py` |
| 6 | Slice B: ATIF→CLEAR-IR converter + diagnosis sidecars | SEAM (M031), billable approval | fixture round-trip test; one diagnosed trial with full judge provenance; calibration ≥ floor before use |
| 7 | LLM intent rewriting for seqgen instructions | SEAM, SG-lane handshake | leakage scan; oracle still 1.0; human review of N rewrites |
| 8 | Exgentic-style bridge | only if a non-Harbor benchmark must run unchanged agents | decision first; adapter-LOC budget declared up front |

## 7. Sources

- Survey: arXiv:2503.16416 (Findings ACL 2026). TASTE: arXiv:2605.28556.
  Exgentic: arXiv:2602.22953. Agentic CLEAR: arXiv:2605.22608. KCIF:
  arXiv:2410.12972. ADO: `ibm.github.io/ado/latest` (JOSS 2026).
- Pinned upstream commits inspected (read-only, ephemeral clones):
  `taste@d53da23956d63e2e6d9f6f5ba77fc5d0eca6b173`,
  `exgentic@ae8d10f7f1e29d2b08d8a5d41bafa16836004998`,
  `clear@9a5367bd048ed656b62093cc09d77b872fca4ff8`,
  `ado@3ad092c68835b9894af83e62b13d1aa425775f07`,
  `unitxt@39897986970b91ee3e2001c7a665d0e9918838cc`,
  `kcif@dfd7a872383f265cb17f4bb572cee3ec3457f210`.
- Harbor: `harbor[modal]==0.21.0`, installed uv tool (see
  `docs/execution-tiers.md`).
- Uncertainties: TASTE's license may change at paper release (recheck before
  borrowing anything beyond the idea); adjacent-stack survey claims were not
  all verified in code and are marked as author claims; the mission-board
  snapshot used for lease checks predated `origin/main` `8ea9f8b` — leases
  were re-verified against files, not the board, at build time.
