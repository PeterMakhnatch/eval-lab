# What the Harbor corpus says to implement — the delta (2026-09-06)

Analyst read of `research-context/harbor/` (INDEX, FIND, PATHS-FORWARD, PALETTE,
CONSUMERS, DATA-MECHANICS, OPERATIONS, expert/NEXT+TRIED, four scout passes over
sources/swarm) cross-checked against eval-lab's working tree and today's
`harbor-buildout-plan-20260906.md` Wave-0 results. Rule: list only what is (a) not
already HAVE in `src/evallab`, (b) not already done in Wave 0, (c) not on the corpus
cut list, (d) free by default. Metered spend = approve-per-run.

## 0. State the corpus does not know yet (observed today)

- eval-lab already HAS: ATIF ingest/validate (`evidence/atif.py`), traces export with
  the custom-agent crash workaround (`scripts/export_harbor_traces.py`), atif2otel →
  Phoenix (`tracing.py`), Wilson/pass@k/pass^k/MDE (`cohort.py`), feature registry
  (n-grams, loops, CBV, blind retries), state-diff/hook plugin
  (`harbor_state_journal.py`), 8-point task cert with oracle/nop/cheat
  (`authoring.py`), external-pack contamination policy.
- Wave 0 DONE on three lane branches, **none merged**: `feat/hn-data-20260906`
  (store read-rule view, $2.01 proven; 52k corpus table, 37% loop rate),
  `feat/hn-synth-20260906` (Repo2RLEnv REJECTED with cheat evidence; harden-v0
  baselined; loader shims), `feat/hn-run-20260906` (`--skill` passthrough proven,
  tblite/data-agent probes). Integration is the gate on everything below.
- Two corpus-verified facts that shape the plan: oracle/nop trials emit no ATIF
  (`traces export` → NotImplementedError); fresh `harbor task init` scaffolds
  FAIL OPEN (nop scores 1.0) until real tests exist.

## 1. The delta, dependency-ordered (free unless marked)

| # | Item | Bucket | Why (corpus) | Status in lab | Effort | Depends on |
|---|---|---|---|---|---|---|
| 1 | **Merge the three Wave-0 lane branches** | integration | nothing in Wave 0 is usable from main until this lands | 3 branches unmerged | M (review) | — |
| 2 | Runner passthrough `--load-trajectory` + `--export-traces` (`--skill` done) | collection | continuation evals + export through the queue | MISSING (plan item 9) | S | 1 |
| 3 | Per-trial fault-isolated `traces export` + **one-line-guard upstream PR** for the AgentName crash | collection | corpus + START-HERE both name this the first upstream contribution; lab has only a shadow-dir workaround | PARTIAL | S | — |
| 4 | `atifact` over the 24 quarantined non-ATIF lanes; certify unconvertible per lane | parsing | corpus: prerequisite for export/analyze/OTel on those lanes | MISSING (plan item 11) | M | 1 |
| 5 | `harbor-rewardkit` path-graded criteria (`trajectory_tool_used`, `turn_count`) in our own verifiers | collection | PALETTE P0; grades the *path*, our tool-use axis, $0 | NOT FOUND | S | — |
| 6 | `benchmark-template` 22 `ci_checks/*.sh` as a static admission gate on our families | task QA | the public quality bar; lab cert is behavioral only | NOT FOUND | S (<1h) | — |
| 7 | Regrade ingestion: `harbor trial/job regrade` outputs → Parquet with verifier digest | analysis | regrade verified $0/5.4s but results are not ingested; makes hardening measurable retroactively | NOT FOUND | S | 1 |
| 8 | Lego-RL Rule 5 (termination filter) + Rule 6 (deterministic archive) as curation columns | analysis | my own labels: 18/25 failures are harness — these must leave capability denominators mechanically | MISSING | S | 1 |
| 9 | Variance-by-task×scaffold + temperature-0 drift over the 52k corpus | analysis | population priors before claiming our N is special; A1 gave loop rate only | PARTIAL | S (DuckDB) | 1 |
| 10 | AgentRx judge stage on the 7 model-side failures — **subscription lane** (Gemini Flash primary), not Azure | LLM analysis | plan item 8 assumed Azure; corpus notes any OpenAI-compatible lane works | designed, not run | S | approve lane |
| 11 | `harbor analyze -r trial-analysis.toml` on the 25 labeled trials → κ vs hand labels | LLM analysis | the corpus's judge-calibration recipe; we have the gold labels already | NOT RUN | S | approve lane |
| 12 | ADP over our ShareGPT export + OpenThoughts ≥5-turn filter + item-8 termination filter = first curation view (export only) | synthetic prep | Path B step 3; first honest SFT-corpus view | MISSING (plan item 10) | M | 1, 8 |
| 13 | FACET staging-mirror loader (org/name rewrite) → 15-task admission trio | synthetic prep | FACET-LOADER-DECISION; unblocks 6,020 released tasks | shim on synth branch — verify | S | 1 |
| 14 | Continuation-harness design: premise-manipulation arms (Failure-as-Process onset→propagation→collapse) | synthetic prep | plan item 12; analyst-owned; blind protocol like the skill spec | design only | S | — |
| 15 | SETA-Evol on `syn-funcdag` (difficulty/context mutation, oracle=1 ∧ nop=0 gate) | synthetic | PATHS-FORWARD Path A step 2 — "where you stop reproducing and start differentiating" | backlog | M | 6, 13; **API cost** |

Items 1–9, 12–14 are $0. Items 10–11 need a model on a subscription lane (approve
lane once). Item 15 is the only API-cost synthesis step and sits last.

## 2. Cut (corpus + lab policy, do not revisit)

vestige-as-tool (keep the estimators, already in `cohort.py`); atif-lens; SaaS
observability (LangSmith/Opik/Braintrust/elluminate — Phoenix is local); RL stacks
(SkyRL/NeMo Gym/Lego-RL trainers/Tinker/rLLM — borrow the six integrity rules only);
Recovery-Bench (STRICT HOLD); Harbor Index submission; FACET full 71k-skill pipeline;
Repo2RLEnv (REJECTED with cheat evidence, Wave 0); heavy/gated packs (ERP-Bench,
Lego-RL-2699, MLS/PTX, ScienceAgentBench); any new viewer/exporter.

## 3. Gaps the corpus admits (so nobody re-discovers them)

No turnkey ATIF→SFT curation tool (item 12 is ours to write); `--load-trajectory`
only for claude-code/codex (terminus-2/opencode fail fast); task-level
`trajectory.json` (PR #2529) absent in 0.21.0; multi-step regrade unsupported;
macOS Docker egress filtering falls back to `network_mode: none`; structural
metrics screen but never prove cause (paired continuation/perturbation trials do).

## 4. Analyst-owned from this list

14 (continuation-harness design) now; 9 (variance priors) and 8 (curation columns
spec) once item 1 lands; calibration read-outs for 10–11 when a lane is approved.
Everything else is data-engineer / builder / runner lane work and should go on the
board, not be paged.
