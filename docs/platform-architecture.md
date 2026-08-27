---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Eval R&D Platform — System Architecture (v2, detailed)

The capital design document. v2 elaborates every plane against the system
as it exists (44k lines across 52 modules in `src/evallab/`, 38 CLI commands, `sql/` views),
in the format: **Current → Contracts → Deltas → Edge cases**. Harbor is the
execution engine; this is the laboratory around it. Sections marked ∆ are
unbuilt or partial; everything else is grounded in shipped code.

## 0. Design tenets (binding, unchanged from v1)

T1 evidence immutable / stores rebuildable · T2 contract-first parallelism ·
T3 agents propose, one executor disposes · T4 provenance + uncertainty on
every claim · T5 generated>hand-written for state surfaces · T6 extension
by seams · T7 janitorial duty inside every contract.

## 1. System context

Planes and their primary code homes:

| Plane | Primary modules (today) |
|---|---|
| Execution | `queue.py` (1670), `runner.py` (809), `automation.py` (1003), `credentials.py`, `canary.py`, `quota.py` (966) |
| Data | `database.py`, `results.py`, `atif.py` (959), `facts.py` (1311), `attach.py`, `sql/{schema,craft_views,lessons,behavior}.sql` |
| Experimentation | `schemas.py` (1520), `cli.py` (2117) preflight/power/submit/matrix, `registry.py` (930), `ladder.py` (914) |
| Analysis | `cohort.py` (1089), `analysis_worker.py` (1035), `calibrate.py` (1719), `explorer.py` (1209), `behavior.py` (736), `cards.py` (457) |
| Knowledge | `craft.py` (1319), `contextpack.py` (795), `lance.py` (493), lessons views, `research/**` |
| Authoring | `authoring.py` (3383), `task_workbench.py` (3395), `library/tasks/_proposed/` |
| Surfaces | `digest.py` (754), `status_generator.py` (484), `dashboard/`, `tracing.py` (769), `lineage.py` (572), status/preflight CLI |
| Coordination | board files (Z5), `agents/{WORKFLOW,ROLES,CHECKS}.md`, Integrator role |
| Quality/Identity | CI (`.github/`), premerge, GATE (paid-run authorization), AgentProfile probes, `tidy.py` (1026) |

## 2. Data plane (normative, heaviest)

### 2.1 Entity spine — contracts

All pydantic v2 models live in `schemas.py` (single module by design; other
modules import, never redefine). IDs: ULID strings. Digests: `sha256:hex`.
The **join spine invariant**:

```
spec_id → job_id → trial_id → {trajectory, analysis_record, observation}
task_ref@version → {craft_record, suite_member, registry entry}
agent_name → agent_profile
```

CI guard: `tests/test_join_spine.py` builds one synthetic spec through a
stubbed run and asserts every hop resolves in both Z2 and Z3. No component
may ship a write path that breaks a hop.

Field-level contracts (delta fields marked ∆):

- **ExperimentSpec** (exists): name, hypothesis, task|task_path, agent,
  model?, attempts, concurrency, est_cost_usd (legacy alarm units),
  policy_rule, submitted_by, priority, task_version, verifier_digest,
  `purpose` (enum, required at dispatch), `question_ref` (free str),
  `elicitation {preamble_hash, toolset[], env_overrides{}}` — defaulted and
  hashed at submit so *every* trial is joinable to an elicitation tuple;
  `prereg {expected, decision_rule}` optional but required for
  purpose=comparison.
- **AgentProfile** (partial in `credentials.py` as
  AGENT_CREDENTIAL_REQUIREMENTS + DEFAULT_AGENT_MODELS): ∆ unify into one
  table `{agent_name, provider, probe: callable-name, default_model,
  daily_run_quota, enabled}` consumed by doctor, executor, preflight,
  policy. Probe contract: `() -> ProbeResult{present: bool, detail: str}`
  — detail must never contain a secret (CI greps probe outputs in tests).
- **TaskVersion** (exists in `registry.py`): ref, version, task_digest,
  verifier_digest, source{repo, commit}, oracle/nop evidence paths.
  ∆ add: `contamination {public_since?: date, in_pretrain: y|n|unknown,
  basis: str}`, `human_minutes?: int`. Registered versions are immutable —
  a change is a new version; `registry` remains automation-refusing
  (human-only) by its existing TTY/confirmation mechanism.
- **Trial** (exists via ingest): reward, reward_dims{name→float},
  exception_type?, tokens{input, cache, output}, cost_usd (retained as
  API-key alarm only), duration_s. Quota truth lives in
  `quota_consumption`, not in cost_usd.
- **CraftRecord** (exists in `craft.py`): keep current facet set; contract
  freeze via `facets_schema_version`; additive evolution only.
- **AnalysisRecord / ObservationRecord / CalibrationRecord / Proposal /
  Lesson / Verdict** — as implemented; Verdict: Z2 table
  `verdicts(discovery_id, status, by, at, note)` written by an authorized CLI
  (`evallab verdict`) so researcher prompts can query rather than parse.

### 2.2 Zone Z1 — evidence filesystem

Current: `runs/<job>/` written by Harbor via `runner.py`; promotion to
`research/evidence/` with manifests; `gc` implements
compress(14d)/prune(60d)/tombstones for unpromoted jobs. Contracts:
executor is the only writer; `gc --apply` is the only deleter; tombstone =
`{job_id, spec_id, digests, reward_summary, removed_at, reason}`.
Edge cases: (a) partially-written job dir on executor crash → reconcile
marks `dispatch_failed(execution_interrupted)` and the dir is retained as
evidence, never half-deleted (SOLIDIFY landed this); (b) promotion of a
job referenced by a digest/DISCOVERIES pins it against gc (exclusion list
is derived, not hand-kept — ∆ implement as a gc pre-scan of Z4 links).

### 2.3 Zone Z2 — PostgreSQL catalog

Current: `sql/schema.sql` idempotent DDL — jobs, trials, rewards,
artifacts, run_files, analysis, calibration, registry, quota_consumption
(#64), plus `sql/craft_views.sql`, `sql/lessons.sql`. Nightly `pg_dump`
exists. Contracts: every table is reconstructible from Z1+Z3 (`db init` +
`ingest` from scratch is a tested path); FKs follow the spine; JSONB only
for small original documents (raw_config/raw_lock), never logs.
∆ Deltas: `verdicts` table; `suites(name, version, frozen_at)` +
`suite_members(suite, task_ref, version)`; view `v_spine` materializing
the canonical join; `v_quota_today(provider, runs, tokens, quota,
remaining)`; index review after compaction lands (trials(task_ref, agent),
trials(job_id) exist — verify with EXPLAIN on the leaderboard query).
Edge cases: Postgres down ⇒ doctor fails closed (tick quarantines — by
design; the catalog is a hard dependency of *dispatch*, deliberately, so
the lab never runs unrecorded); concurrent ingest of same job ⇒ upsert on
job_id (idempotent, exists).

### 2.4 Zone Z3 — Parquet analytics

Current layout: `derived/parquet/<table>/job_id=…/trial_id=…/*.parquet`
for trials/steps/tool_calls (+ craft, ledger); single ingest path lands
catalog+parquet together (MENDER/SOLIDIFY); derived root configurable
(worktree topology decision — shared root via env, documented).
Schemas (contract, `schema_version` column in every file):
- `trials`: trial_id, job_id, spec_id, task_ref, task_version, agent,
  model, attempt_n, reward, dims (map), exception_type, tokens_in/out,
  duration_s, started_at.
- `steps`: trial_id, step_index, role, kind, tool_name?, duration_ms,
  tokens_in/out, error: bool, content_digest.
- `tool_calls`: trial_id, step_index, tool_name, args_digest, exit_code?,
  duration_ms.
∆ Compaction (the small-files clock): nightly step —
`for dt < today-7: COPY (SELECT * FROM read_parquet(hot glob dt) ORDER BY
task_ref, agent, trial_id, step_index) TO
compact/<table>/dt=<dt>/part-0.parquet` → verify row counts vs a
`_MANIFEST.json{source_files[], rows, digest}` → only then delete hot
partitions for that dt. Idempotent per (table, dt); ORDER BY chosen for
zone-map pruning on the two hottest predicates (task, agent). Target file
64–256 MB; below that, one file per table-day is fine for years.
Edge cases: compaction crash between COPY and delete ⇒ manifest present
+ hot present → next night detects manifest, re-verifies, deletes (never
re-copies); mixed schema_versions in one dt ⇒ union_by_name + explicit
casts in the compact SELECT; clock: at ~100–240 trials/night the hot zone
holds <10k files by construction (7-day window), so scans stay flat.

### 2.5 Unified attach surface (the one query door)

`evallab db attach [--print-sql]`: emits a DuckDB init script —
`INSTALL postgres_scanner; LOAD postgres_scanner; ATTACH '<dsn>' AS z2 (TYPE postgres);`
+ `CREATE OR REPLACE VIEW <t> AS SELECT * FROM read_parquet(['hot glob','compact
glob'], union_by_name=true)` per Z3 table + in-memory table `z4.front_matter`
built directly from markdown front-matter in `docs/` (`path`, `title`, `status`,
`audience[]`, `generated_by`). Contract: dashboards, analysis workers, researcher
agents, and ad-hoc sessions consume ONLY this surface; new code globbing
paths directly is a review reject. Versioned via a `pragma_attach_version`
table so consumers can assert compatibility.
### 2.6 Zones Z4/Z5 + lineage + retention

Z4 knowledge: front-matter contract `{status: living|historical, audience[],
generated_by?, schema_version?}` — already being applied across docs/;
generated files carry `generated_by` and CI ∆ blocks human diffs to them
(check: git author ≠ generator marker on changed generated files).
Z5 coordination: board/briefs/handoffs move-only; COORD-GC archives.
Lineage: generated artifacts embed `inputs: [{path|id, digest}]`;
`evallab lineage <path|id>` resolves recursively (stop at Z1).
Retention matrix (contract):

| Class | Hot | Cold | Delete | Tombstone |
|---|---|---|---|---|
| Z1 unpromoted job | 14d | compressed to 60d | gc --apply | yes |
| Z1 promoted evidence | ∞ | — | never | — |
| Z3 hot partitions | 7d | compacted | after manifest verify | manifest |
| Z2 tables | live | pg_dump nightly ×14 | rotate dumps | — |
| queue/events.jsonl | 30d rolling | gzip monthly | ∆ rotation | — |
| Z5 briefs/handoffs | until done | archive/ | never | — |

## 3. Execution plane (detailed)

### 3.1 Queue + executor (`queue.py`)

Current: DirectoryQueue with state dirs
`pending→{approved|waiting|rejected}→running→{done|failed}` + `reasons/` +
append-only `events.jsonl`; atomic `mv` transitions; PolicyGate.decide
(standing-approvals rules, per-job/day ceilings as API-key alarms, quiet
failure rule, human-approval bypass); Executor.tick: reconcile_running →
STOP check → per-spec: credential deferral (`missing_credential:<x>`) →
gate → dispatch → run via `runner.py` → ingest → done/failed with reason
files; claim-races between concurrent ticks tolerated (vanished-file
skip); transient provider errors classified with capped backoff and
excluded from quiet-failure counting; per-trial wall-clock watchdog;
GATE: paid agents additionally require an explicit authorization artifact.

∆ Deltas, specified:
- **Bounded parallel dispatch:** `tick --parallel N` (default 1). Design:
  a dispatch pool where each running spec holds a lease file
  `running/<spec>.lease` (heartbeat = mtime touched by the runner wrapper
  every 30 s). Per-provider concurrency: semaphore map from AgentProfile
  (`{codex: 2, claude-code: 2, oracle: 4}`); global cap = min(N, Docker
  capacity from execution-tiers doc). Reconcile treats a lease with
  mtime > 5 min stale as orphaned: verify no live container for the task
  (via Harbor Docker Compose labels); if none, `failed(execution_interrupted)`
  with the partial dir preserved.
- **Quota enforcement point:** gate consults `v_quota_today`; a spec whose
  provider quota is exhausted is **deferred in approved/** (event
  `dispatch_deferred(quota_exhausted:<provider>)`), not moved to waiting —
  quota renews at UTC midnight; waiting/ remains human-only territory.
- **Container hygiene:** runner uses Harbor's Docker Compose labels
  (`com.docker.compose.project`, `com.docker.compose.project.config_files`);
  post-run cleanup and orphan sweeps filter by label only (never global prune
  — Postgres/Phoenix run in the same daemon).

Crash matrix (contract, mostly landed via SOLIDIFY): executor dies
pre-dispatch ⇒ spec intact in approved/; mid-run ⇒ lease+label recovery as
above; post-run pre-ingest ⇒ nightly reconcile re-ingests from Z1 (ingest
is idempotent); mid-transition ⇒ impossible-by-construction (single `mv`).

### 3.2 Scheduling + health (`automation.py`)

Current: launchd tick (30 min) + nightly (02:30); HeadlessDoctor with
independent probes (docker, postgres, disk, per-credential); healthy =
infra ∧ ≥1 credential; quarantine reason lists blocking failures only;
NightlyCycle: canary enqueue (pinned suite yaml with digests) → dispatch →
digest render → commit; postgres backup steps serialized, timeout-bounded,
quarantining on timeout. ∆ Deltas: nightly becomes an ordered **pipeline
registry** (list of named steps, each `(name, fn, timeout, on_fail:
continue|quarantine)`) so compaction, lessons materialization, STATUS
generation, storm-alarm evaluation, and gc plan slot in as steps with
per-step events rather than an ever-growing function body.

### 3.3 Identity, quota, GATE

Current: `credentials.py` probes (keychain via /usr/bin/security, codex
auth file) — subscription-only, no API-key env vars anywhere (CI-greppable
rule); `quota.py` meters real consumption per provider per UTC day; GATE
requires explicit authorization before any paid dispatch. ∆ AgentProfile
unification (§2.1) + `evallab profiles` CLI (list, probe, enable) + Grok /
Gemini rows behind the ProviderAdapter seam: adding an agent =
{profile row, probe fn, default model pin, policy allowlist entry, 3-canary
acceptance battery} with zero executor edits.

## 4. Experimentation plane (detailed)

Current: `submit`/`matrix` CLIs validate ExperimentSpec; `preflight` prints
provider quota + queue; `power` computes detectable effects; `registry`
audits registered tasks; canary suite pinned by digest. Contracts and ∆:

- **Spec lifecycle:** draft → (purpose=comparison ⇒ prereg block required:
  expected result + decision rule; stored verbatim, quoted by the eval
  card) → submitted → gated → dispatched → analyzed → carded.
- **Purpose semantics (dispatch + analysis defaults):**
  baseline→per-agent pass@k card; comparison→paired analysis, refuses
  without prereg; elicitation→one-variable check (spec diff vs its
  `ref_spec` must touch exactly one elicitation field — enforced);
  drift→canary compare vs trailing 7d; calibration→judge agreement path;
  craft→cheap classify batches; practice→excluded from lessons/cards.
- **Preflight output contract (sections, in order):** provider quota table
  (from v_quota_today) · queue grouped by purpose with est units · power
  warnings (specs whose planned n,k cannot detect their prereg'd effect) ·
  storm/alarm banners · blocked-on-human list (waiting/ + pending
  verdicts). Runs at tick start; `--json` for surfaces.
- **LADDER:** `grids/*.yaml` = {axes: {task_refs[], agents[],
  preamble: [hash…], k: [1,3,5]}, constraints, purpose, daily_budget_units}.
  `evallab ladder generate` expands the cross-product minus constraints,
  round-robins across providers under quota, submits in priority order,
  and records grid_id on each spec so partially-run grids resume instead
  of duplicating (dedupe key: grid_id + point coordinates).
- **Suites:** `evallab registry freeze <suite>@<v>` snapshots member
  TaskVersions into Z2; comparisons across dates must cite a frozen suite
  (compare warns otherwise). Frozen = immutable forever.

## 5. Analysis plane (detailed)

Current: `facts.py` deterministic extraction; `cohort.py` (TRUTH):
bootstrap-over-tasks intervals, paired-by-task default, refuse-to-rank
without n/interval/elicitation; `power` both directions;
`analysis_worker.py` guarded model-assisted sidecars; `calibrate.py`
judge agreement vs sealed corpus; `explorer.py` run/analysis browsing;
`report family` trajectory-level reports; 27+ observation records.

Contracts:
- **Statistics API (stable):** record-oriented functions in `cohort.py`:
  `compare(spec, repo_root=...)` (paired comparison producing `{distinguishable: bool,
  paired_difference_interval, ...}`), `bootstrap_mean_interval(values,
  confidence=0.95)`, `minimum_detectable_effect(n_tasks=..., k=...,
  baseline=..., correlation=...)`, `required_tasks_for_effect(...)`,
  `pass_at_k_probability(...)` (model-based independent-attempt planning
  transform, not realized first-k and not Chen/Yao). Render rule (binding): any surface printing a
  ranking calls comparison and prints its explanation verbatim when
  `distinguishable=false` — "not distinguishable at this sample size" is a
  first-class result.
- **Analysis worker bounds:** input = read-only bundle manifest (paths +
  digests); output = AnalysisRecord validated with one retry; runs via
  queue purpose=analysis under GATE; per-day call caps from policy;
  evidence citations must resolve (validator checks paths/step ids).
- **Calibration calendar ∆:** weekly rotation `family = families[week %
  len]`; every judged number rendered anywhere carries
  `(judge agreement: 0.xx @ <date>)` or renders as draft; floor 0.90
  gates `calibrated_judges_only` policy scopes.
- **Lessons ∆ (materializer):** nightly step runs `sql/lessons.sql` views
  through the statistics API, writes `lessons.parquet` + generated
  `research/lessons.md`; rows below power render "insufficient n (needs
  ~N more trials)" — the gap itself becomes a LADDER hint.

## 6. Knowledge plane (detailed)

Current: `craft.py` (scan heuristics + facet records + cookbooks),
`contextpack.py` (tag-selected deterministic packs), lessons views,
DISCOVERIES journal, doc front-matter adoption in progress.

Contracts and nuance:
- **Craft heuristics (deterministic pass):** verifier_type from tests/
  inventory (pytest imports ⇒ pytest; golden files dir ⇒ golden; judge
  yaml/rubric ⇒ judge; >1 signal ⇒ hybrid); anti_cheat detection: tests
  uploaded post-agent (Harbor default) ⇒ hidden_tests; answer material
  absent from environment/ image ⇒ answer_outside_image; digest checks in
  verifier source ⇒ digest_check. LLM classify pass fills only
  {instruction_style, difficulty_mechanism, answer_hiding} — batched 10
  tasks/call, cheap model, purpose=craft, GATE-authorized; classify never
  overwrites deterministic fields.
- **Cookbook generation:** group CraftRecords by facet; exemplar selection
  = source priority (tb3 > registered > curated) then human_minutes desc;
  each entry = ≤30-line excerpt + file path + facet row; regeneration is
  idempotent (stable ordering).
- **Context packs:** assembly order = mission brief → role contract →
  task facet card → top-k patterns by facet overlap (k=5, deterministic
  tie-break by task_ref) → matching lessons rows → accepted-verdict
  discoveries touching same facets → hard size budget (12k tokens,
  truncation drops lowest-rank patterns first, never the brief). Output
  header records selector inputs + content hash; same repo state ⇒ same
  hash (tested).
- **Verdict loop:** `evallab verdict <discovery_id> accepted|rejected|
  needs-evidence --note "…"` writes Z2 + patches the journal block;
  researcher prompts receive the verdict table, not raw markdown.

## 7. Authoring plane (detailed)

Current: `authoring.py` + `task_workbench.py` (3.4k lines: certification
surface over the real task library) + `registry.py` human gate + gc'd
quarantine dir.

Contracts:
- **Proposal layout:** `library/tasks/_proposed/<name>@<v>/` = full task
  dir + `PROPOSAL.yaml {proposal_id, seed_class: mutation|scenario|
  craft_gap, ref_task?, generation {model, pack_hash, prompt_digest},
  created_at}`. Mutations always target a new version; in-place edits are
  rejected by the workbench.
- **Battery (order matters, cheap-first):** structure lint → oracle=1.0
  (n≤2) → nop=0.0 → fair-oracle (a solver agent given ONLY
  instruction+environment; must succeed without solution/ access —
  catches hidden-knowledge tasks) → adversarial (cheat-instructed agent
  must score 0 — catches gameable verifiers). Each stage writes evidence
  paths into the qualification ledger; first failure stops the battery.
- **Craft-review scorer:** rubric items compiled from cookbooks (pinning
  present; answer-hiding ∈ known-good; instruction length within band;
  difficulty_mechanism ≠ clerical…) → score 0–100 + reasons[]; threshold
  configurable in policy, not code.
- **Ledger:** `derived/parquet/qualification/ledger.parquet`
  (proposal_id, seed_class, stage results, score, outcome, ts) — the
  pass-rate per seed_class is the S1 metric and a dashboard pane.
- **Promotion:** only `evallab registry add` (human, automation-refusing).
  The pipeline can *stage* a TB4 export bundle (`evallab author export
  <proposal>`) but never submits externally.

## 8. Coordination plane (multi-agent OS, detailed)

Current: Integrator role with succession handoff; board files; worktree
isolation under `.worktrees/`; handoff schema (Status/Last/Next/Blockers);
COORD-GC archival; token-cheap dispatch by mission briefs.

Contracts:
- **Board schema ∆ (formalize what exists):** `board/{open,active,done}/
  <mission>.yaml {mission, owns: [globs], interfaces_touched: [entity or
  CLI names], acceptance: [runnable claims], pack_ref, claimed_by?,
  claimed_at?}`. Claim = atomic mv open→active. Done requires: PR merged
  green, worktree removed, branch deleted, brief mv→done (T7 checklist is
  part of acceptance, not advice).
- **Role contracts:** `agents/roles/*.yaml {role, allowed_paths,
  allowed_clis, pack_type, handoff_schema: v1}` — the executor of
  coordination is convention + review, not runtime enforcement (single
  operator trust model); CI enforces the parts it can (ownership of
  changed paths vs board entry).
- **Concurrency:** disjoint `owns` globs across active missions is a
  board-level validation (`evallab board check` ∆); shared files
  (cli.py/schemas.py/pyproject) additive-only remains the standing rule
  with union-merge resolution by the Integrator.
- **Token economy:** orchestrator writes briefs + reads handoffs; context
  packs replace doc crawls; transcripts are never coordination state.

## 9. Surfaces plane (detailed)

Current: digest (nightly, committed), `status` CLI, dashboard (app,
explorer, projection panes), Phoenix traces via `tracing.py` + atif2otel,
fleet-status script. Contracts and ∆:
- **STATUS.md generator:** sections = merged-today (git log) · suite
  health (canary deltas) · quota (v_quota_today) · queue by purpose ·
  alarms · decisions-pending (waiting/ + verdicts pending + registry
  stages). Written by CI post-merge and nightly; front-matter
  `generated_by: status vN`.
- **Digest sections (ordered):** health/doctor · dispatches with reasons ·
  canary drift · lessons delta · discoveries + verdict status · spend =
  runs/tokens per provider vs quota · gc plan · storm banners.
- **Dashboard pane→query map (binding):** every pane declares its attach-
  surface view in code (`PANES = {name: view}`); a pane without a view
  entry fails a golden test. Read-only enforced by connecting via the
  READ_ONLY attach.
- **Alarms:** rule table in policy
  (`{condition: same reason_code ≥N/hour | quota ≥90% | canary drift >σ·k,
  channel: digest+STATUS banner}`) evaluated in the nightly pipeline and
  at tick end. No paging infrastructure — banners on the surfaces Peter
  already reads.

## 10. Quality & identity plane (detailed)

Current: CI (lint 3.12, tests 3.12/3.14, ty ratchet 33≤33), premerge parity
script, make smoke (composed free-control path), golden CI evidence for
perf budgets, GATE authorization, secret-scrubbed probes; hypothesis fuzz
`tests/test_queue_properties.py` (operation alphabet {submit, approve, reject,
tick(n), stop, resume, crash-restart, quota-exhaust, credential-flip};
invariants: no spec lost, no double dispatch, quota never exceeded, STOP
honored within one dispatch, events strictly append-only, every terminal
state has a reason); golden files for digest/STATUS/preflight rendering;
join-spine CI test (`tests/test_join_spine.py`, §2.1); repo-local skills in
`.claude/skills/` (lab-status, mission-launch, review — read-only over
surfaces).

## 11. Extension seams (stability promises)

| Seam | Interface (stable) | Test double | First consumer |
|---|---|---|---|
| ProviderAdapter | AgentProfile row + probe fn | stub probe | Grok, Gemini profiles |
| StorageBackend | Z1/Z3 root resolvers behind env/config | tmpdir roots | S3 at scaling gate 1 |
| AnalysisPlugin | module registering views + metrics over attach surface | fixture parquet | new taxonomies/stats |
| SurfaceRenderer | reads attach surface, writes Z4/UI | golden files | dashboard v3, reports |
| TrajectoryExport | `evallab export sft --suite S --filter …` → messages-format + provenance manifest | fixture trials | slime/verl-class stack |
| RewardInterface | TaskVersion verifier contract (engine-agnostic already) | oracle/nop | RL env wrappers |
| EnvironmentRegistry | registry + suites ≡ environment catalog | frozen test suite | RL curriculum |

RL/post-training consumes the last three seams; nothing upstream changes.

## 12. Migration map (existing → target) and epic binding

| Module / Component | Keep / Change / Built | Epic | Status |
|---|---|---|---|
| `schemas.py` | + purpose/elicitation/prereg, AgentProfile, Verdict | E00 | shipped |
| `ladder.py` | LADDER evaluation grid generator CLI | E02 | shipped |
| `attach.py` | unified DuckDB attach surface (Z2+Z3+Z4) | E04 | shipped |
| `database.py`/`sql`/`spine.py` | + suites, verdicts, v_spine, v_quota_today | E05 | shipped |
| `cohort.py`/`cards.py` | record-oriented stats API + eval cards | E11 | shipped |
| `automation.py`/`status_generator.py` | nightly step registry + STATUS.md generator | E12 | shipped |
| `dashboard/` | rebind all panes to attach surface | E13 | shipped |
| `lineage.py` | artifact lineage walker CLI | E14 | shipped |
| `tidy.py` | working tree tidy sweep CLI | E16 | shipped |
| `.claude/skills/` | repo-local skills: lab-status, mission-launch, review | E18 | shipped |
| `behavior.py`/`sql` | behavioral analysis views + metrics | — | shipped |
| `authoring.py`/`calibrate.py` | meta-task loop, spec sampler, inversion, selector | SG-1..4 | shipped |
| `lance.py` | LanceDB vector store beside DuckDB | — | shipped |
| `verdicts.py` / CLI | verdict recording, history, and queries | E-verdict | shipped |
| `queue.py` | leases + heartbeat + `tick --parallel N` landed (M020); provider semaphores and orphan reconcile still open | E01/E-par | partial |
| `credentials.py`+`quota.py` | unify AgentProfile; profiles CLI | E01 | unbuilt |
| `atif.py`/`facts.py` | keep; + compaction step + manifests | E03 | partial |
| `craft.py` | classify batching + tested idempotence contract (M023) | E07 | shipped |
| `contextpack.py` | keep; size budget + hash tests | E08 | unbuilt |
| `researchers.py` | consume verdict table + packs | E09 | unbuilt |
| `authoring.py`/`workbench` | keep; ledger + fair-oracle/adversarial stages | E10 | partial |
| `export.py` / CLI | sft / trajectory export | E17 | unbuilt |
| board check CLI | board consistency validation | E-board | unbuilt |

Build protocol: E00, E02, E04, E05, E07, E11, E12, E13, E14, E16, E18, and SG-1..4
have shipped; E01 and E03 and E10 are partial; remaining epics (E08, E09, E17 SFT
export, E-board) remain unbuilt.

**The model-call seams are unimplemented, which no epic row states.** Three injection
points exist and every default is a refusing stub, so the study loop cannot close on a
real model today regardless of spend authorisation:

| Seam | Default | Behaviour |
|---|---|---|
| `analyst.ModelAnalyzer.analyze()` | — | raises `ModelProviderRefusedError` **even when `--model` is supplied** (`analyst.py:150`) |
| `analysis_worker.AnalyzerCallable` | `_no_adapter` | raises `no analysis adapter is wired` (`analysis_worker.py:657`) |
| `authoring.design_novel_spec(designer=…)` | `local_test_designer` | deterministic test-only fallback; production `model-propose` requires explicit pinned model and transport (`authoring.py`) |

Trial *execution* against real agents does work — the catalog holds 33 `codex` trials
beside 57 `oracle` and 2 `nop` controls — so the gap is confined to the analysis and
generation halves, and it is code, not policy.

## 13. Non-goals and risks (unchanged from v1)

Non-goals: distributed execution, multi-tenancy, RL training itself,
alternative harnesses. Vector memory was originally a pre-trigger non-goal,
but the trigger was met on Peter's explicit ruling and shipped via
`src/evallab/lance.py` (LanceDB vector store beside DuckDB with deterministic
lexical embedding). Top risks: schema churn (E00 freeze + schema_version),
small-files regression (§2.4 + budgets), judge drift (calendar), coordination
sprawl (board check + T7), single machine (backups now, StorageBackend at gate).

## Changelog
- 2026-08-17 — v3: doc-drift corrections approved by Peter; updated code
  measurements (~44k lines across 52 modules, 38 CLI commands), §2.5 unified
  attach flags (--print-sql) and in-memory Z4 front-matter table, §3.1 Harbor
  Docker Compose container labels, §5 record-oriented cohort statistics API,
  §7 qualification ledger path in derived/, §13 lance vector store trigger
  status, §10/§12 epic completion states (E00, E02, E04, E05, E11, E12, E13,
  E14, E16, E18, SG-1..4 shipped), and restored E18 mapping to repo-local skills
  under `.claude/skills/` (with `behavior.py` placed outside the epic list).
- 2026-08-17 — v2: full per-plane elaboration in Current→Contracts→Deltas→
  Edge-cases form, grounded in the shipped 33k-line codebase; migration
  map added (Claude, at Peter's direction).
- 2026-08-16 — v1: initial planes/zones/epics version.
