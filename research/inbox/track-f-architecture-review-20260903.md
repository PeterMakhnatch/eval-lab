# Track F — architecture review and adoption gate (2026-09-03)

Basis: integration head `ccf5567e` — every spine symbol cited below was re-verified present at that exact commit (`ContractModel` schemas/__init__.py:21-24, `Digest` :160, `canonical_json`/`compute_sha256` benchmark_program_contracts.py:20-31, `load_trial_bundle` interpretation/benchmark_events.py, `TrialAdmissibilityV1`/`verify_trial_admissibility` trial_admissibility.py:501/551, `extra_instruction_path`/`extra_instruction_sha256` schemas/__init__.py:755/764, candidate quarantine path). Upstream facts come from the source-verified scorecard companion. Two operator rulings are binding below: the wK:p7 rejection of legacy candidate `d709cf6d` and the wK:p9 `allowed_use='training'` authority-axis ruling. TRACE naming follows wK:p3's disambiguation (`track-f-trace-disambiguation-20260903.md`): **TRACE-Capability (2604.05336)** vs **TRACE-Benchmark-Evolution (2510.00415)**.

## 1. Shared spine (all Tracks A–D)

Every derived artifact MUST:

1. Extend `ContractModel` (`schemas/__init__.py:21-24`, `extra="forbid"`, frozen) and use `Digest` (`schemas/__init__.py:160`). No second schema convention.
2. Digest via `canonical_json`/`compute_sha256` (`benchmark_program_contracts.py:20-31`). Identity is content, never path.
3. Persist through the existing CAS (`evidence_store.py`: `archive_evidence:380`, `read_record:768`, `restore_evidence:648`) with a NEW record kind per track (`training-export`, `capability-deficit`, `curriculum-candidate`, `trainer-bundle`, `paired-plan`). New record kinds are additive; no writer mutates existing kinds or archives.
4. Bind: source trial/job identity, source artifact digests (ATIF identities from `evidence/atif.py:78-79` NUL-joined SHA-256), extractor module+version, benchmark/task family, split, exclusion reasons.
5. Refuse rather than coerce: any missing binding is a typed refusal, not a default.

## 2. Authority axes (binding ruling — supersedes any track design)

Two independent axes; neither implies the other:

- **Registry axis:** `allowed_use` from the task registry. For any training export the required value is exactly `allowed_use='training'`.
- **Evidence axis:** the authoritative evidence/admissibility class (`TrialAdmissibilityV1` verify at `trial_admissibility.py:551-576`; `analysis_eligibility` literal `schemas/__init__.py:160-162`).

Rules: (a) `allowed_use='causal'` is rejected as a training-authorization claim — causal evidence class may gate analysis, never training-export admission; (b) no track may infer causal authority from path, mutable metadata, or cohort convenience; (c) exports carry BOTH values (registry `allowed_use` + evidence admissibility class) and refuse if either is absent or conflicting; (d) contradiction between the two axes is a typed refusal, not a precedence rule.

## 3. Gate Zero (provenance spine) — UNSATISFIED / BLOCKING

G1 owner: `interpretation/benchmark_events.py` (`load_trial_bundle:1291-1351`, `BenchmarkMissingArtifactError:53-55`). G7 owner: `trial_admissibility.py` (`verify_trial_admissibility:551-576`, `finalize_trial_admissibility:606-659`; authority absence is typed `artifact_present=False` at `:500-535`).

Decisions:
- Status: **not satisfied**. The replacement below is NOT implemented; G1 (`0/170` loadable) and G7 (128 unresolved backfills) remain open, and A–D stay fixture-only. This section is a blocking prerequisite for any real-corpus export, not a completed gate.
- The legacy candidate path `d709cf6d` is **REJECTED** (wK:p7 findings): partial 128/170 coverage, immutable-evidence mutation, authority inferred from path/mutable metadata, malformed digest acceptance. Required replacement, to be built by the integration owner as one PR: **additive, typed, content-bound sidecar records** (new record kind; references by digest only), a **future emitter** contract (sidecars may exist before history is complete), **contradiction refusal** (a sidecar contradicting its bound digests or another sidecar fails closed), and **downstream admissibility consumption** (A–D read only `TrialAdmissibilityV1`-verified state, never backfill descriptive records — `storage/data_backfill.py:749-807` forces `admissible=False` and must stay that way).
- Until that PR lands at an exact base where `0/170` loadability and the 128 unresolved-admissibility backfills are closed, Tracks A–D are **fixture-only**: no real-corpus export, deficit claim, synthesis input, or bundle against live evidence. Fixtures must be constructed to exercise the refusal paths, not to simulate success.

## 4. Minimal interfaces

**I1 — A→D (training export):** `TrainingExampleManifest` + JSONL records. Record envelope: `source_job_id`, `source_trial_id`, `source_digests` (CAS locators), `extractor` (module+version digest), `benchmark_family`, `split`, `registry_allowed_use` (must equal `training`), `evidence_class` (authoritative admissibility literal), `representation` (`sft_prompt_completion | sft_messages | episode_step`). Dedup key: canonical content digest. Exclusions are typed rows in the manifest (`exclusions[]` with reason codes: `capture_loss`, `environment_integrity_failed`, `missing_evaluator`, `prohibited_corpus`, `reward_only_no_semantic_evidence`, `authority_conflict`). `syn-funcdag-easy` refusal is an explicit export-admission condition keyed on the identifier (`synthetic_funcdag.py:674-725` has no calibration marker, so key on the name/family, not on inferred status). D consumes I1 **read-only**.

**I2 — B→C (deficit artifact):** `CapabilityDeficitArtifact` versioned `capability-deficit/v1`. Fields: family, failure mechanism (closed set: `complete-but-reordered`, `wrong-binding-addressing`, `wrong-graph-traversal`, `blind-retry`, `malformed-output`, `unclassified`), evidence IDs (ATIF identity digests), observed support, counterevidence, capture status, classification boundary, candidate intervention dimensions. Unknown stays `unclassified`; model prose lives outside the mechanical core (existing precedent: `BehaviorLabel` provenance split, `schemas/__init__.py:1682`). C consumes only artifacts whose every evidence ID resolves to a digest-pinned ATIF identity; a dangling ID is a refusal.

**I3 — C→E (candidate):** `CurriculumCandidate` quarantined under `research/registration/candidates` (`task_workbench.py:6155-6162`; registry rejects packets outside it, `registry.py:451-460`). Binds parent deficit digest(s), transform id/version, cluster key, expected capability, hidden-verifier plan, leak scan result, solvability/control requirements, twin-pair identity. C emits **plans only** — no task packages, no registration.

**I4 — E→queue (paired plan):** planner emits ordinary `ExperimentSpec` records for `DirectoryQueue.submit`/`approve` (`queue.py:780,966-985`); no parallel queue protocol. Correction to the brief: the typed intervention surface already exists — `ExperimentSpec.extra_instruction_path` + digest-bound `extra_instruction_sha256` (`schemas/__init__.py:754-768`, queue resolution `queue.py:2437-2467`, command build `execution_contracts.py:892-894`). E therefore adds only pair/block identity, assignment unit, one-variable-delta validation (reuse `ElicitationSpec.diff_fields`, `schemas/__init__.py:647-677`), capture expectations, and replacement policy; it must NOT edit shared schema or runner files in wave one (its own module may import them).

**I5 — D→external (trainer bundle):** bundle describes model/checkpoint identity, dataset manifest digest (I1), objective, rendering contract, seed, expected output manifest, backend requirements, and typed incompatibilities. Plan renderers only. Per scorecard: TRL SFT consumes `messages` or `prompt`+`completion` columns (scorecard §TRL-b); a verl Parquet renderer (`prompt`, `ground_truth`, `data_source`) is future/external-only; SPADE-shaped and Agent-Lightning-shaped consumers are external descriptors, not imports.

## 5. Adoption decisions (source-verified)

| Component | Decision | Conditions |
|---|---|---|
| TRL | **ADOPT** (SFT only) | Terminal consumer of digest-pinned rendered export; RL algorithms excluded until separately approved; weight updates stay external. |
| ADP | **ADAPT** | Its `ATIFTrajectory` is compatible with ours at the export boundary, but Eval Lab's envelope (lineage, redaction, split, authority axes) stays authoritative; never import `browsergym`/`openhands` deps. |
| Agent Lightning | **REJECT for v1 / deferred** | Consolidated librarian decision + integration lock: no v1 use. Future only, after the SFT-signal gate, at a plan/result boundary; its rollout/event schemas and in-memory server (`server/store.py`, never provenance) are noted for that future boundary only. |
| SPADE | **ADAPT** (no control plane) | Borrow: Gym text-env boundary, generation/validation paths (`game_generator.py`, `generate_and_validate_games.py`, `env_validator.py`), hint-regret curriculum ideas (`hint_generator.py`). Reject: `EnvironmentMemory` as evidence (mutable JSON — Harbor/CAS stays authority), native trainer (requires current-policy logprobs; fails kill gate on stored-trajectory-only training). Generated code executes only in quarantine sandbox, tonight not at all. |
| TRACE-Capability (2604.05336) | **ADAPT (methodology only)** | Primary Tracks B/C methodology source per wK:p3: applicability-aware `NA`/`PRESENT`/`LACKING` labels and deterministic `Cov`/`ER-`/`ER+`/`Delta` metrics (`pipeline/aggregate_capabilities.py::compute_metrics` @ `d2db23085409555b3f13ea426f42d62cf0bbc43d`). Borrow label distinctions + metrics only; reject its LLM labeling as authority, `GameSpec` registry, environment generator, GRPO/LoRA pipeline, MoE gate. |
| TRACE-Benchmark-Evolution (2510.00415) | **ADAPT (optional synthesis reference only)** | Separate work (Guo et al.). May inform an optional later task-synthesis validation plan. Must NOT be cited for `Cov`/`ER-`/`ER+`/`Delta` or Track B deficit extraction; not a substitute for TRACE-Capability. Scorecard facts pinned to paper v3 only (repo empty, license unavailable). |
| verl | **REJECT for v1 / deferred** | No v1 work, including adapter scaffolding: on-policy + Linux/CUDA/Ray + live rollouts all fail first-wave constraints. Future only, after the SFT-signal gate, as an external backend ingesting an explicit result manifest. |

**Kill gates (binding):** (1) SFT signal gate precedes any RL: no RL adapter work until an SFT run on exported data shows a measurable, paired, capture-accounted improvement frozen in a held-out Harbor evaluation. (2) SPADE spike admissibility: stored Eval Lab trajectories + environment contracts must enter WITHOUT replacing Harbor evidence/provenance; any design that makes `EnvironmentMemory`-style state authoritative fails. (3) TRACE invariant: Tracks B/C follow **TRACE-Capability (2604.05336)**; **TRACE-Benchmark-Evolution (2510.00415)** is an optional later synthesis-validation reference only; neither becomes evidence authority, executable admission, trainer, or Harbor replacement. (4) ZAI (API-only) is analyzer/generator/evaluated solver — never a weight-updated training target. (5) No Linux/GPU execution tonight; bundles may declare requirements, never launch.

## 6. Conflict and resolution instructions (first-wave PRs)

- **A/D overlap (bundle manifest fields):** D defines the bundle; A defines the export. Resolution: I1 is A's contract; D imports and validates it read-only. If both define a field name differently, A wins for export rows, D wins for bundle envelope; neither redefines the other's module.
- **B/C overlap (deficit "certification"):** C requires "certified deficits"; certification is B's artifact-level property (all bindings resolve + capture accounted), not a new authority module. No new certification service in wave one.
- **E/shared schema:** E does not edit `schemas/__init__.py` or runner files; if it finds the typed surface insufficient it reports the exact gap for the integration owner instead.
- **Everyone:** no edits to CLI routing, `pyproject.toml`, generated docs, `docs/STATUS.md`, policy.

## 7. Acceptance self-check for reviewers of A–E heads

Each PR passes only if: (1) spine rules §1 hold; (2) authority axes §2 hold — grep the head for `allowed_use` and confirm no `'causal'` training claim; (3) Gate Zero respected — fixture-only, refusals exercised; (4) interface fields match §4 names; (5) negative controls present (leak, malformed digest, contradiction, missing lineage); (6) byte-identical rerun demonstrated; (7) no subprocess/network/GPU/model invocation (import scan: no `torch`, `trl`, `verl`, `requests`, `subprocess` in the new modules).