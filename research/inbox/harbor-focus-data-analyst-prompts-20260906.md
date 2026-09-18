# Harbor-focus prompts: data engineer + analyst (2026-09-06)

Copy-paste one block per pane. Both assume the Harbor-native direction: no porting,
no synthetic-task work, no new viewers, no paid runs without Peter's explicit approve,
Recovery-Bench stays on STRICT HOLD. Harbor = harbor-framework 0.21.0 (`harbor run`,
ATIF `agent/trajectory.json`, `harbor traces export`, `harbor view`).

Context the assignee should read first:
- `research/external/harbor-ecosystem/START-HERE.md` (the map + 1-week sequence)
- `research/external/harbor-ecosystem/LOCAL-VERIFICATION.md` (what 0.21.0 does on this Mac, incl. the two `traces export` bugs)
- `research/inbox/harbor-ecosystem-adoption-map-20260906.md` §6 (play sequence) and §3 (ranked adoptions)
- `research/inbox/harbor-ecosystem-catalog-20260906.md` (classified inventory)
- New trial space already staged: `derived/harbor-packs/terminalworld/tasks/` (20 verified TW tasks, CC-BY-4.0), `derived/harbor-packs/facet/tasks/` (15-task deterministic sample of FACET 6k), TB2.0 oracle smoke in `runs/harbor-tb21-oracle-smoke`, pack oracle verification in `runs/harbor-pack-verify`.
- Vendored reference tools: `research/external/harbor-ecosystem/vendor/vestige` (pinned `d03e8a7`, pass@k vs pass^k) and `research/external/harbor-ecosystem/vendor/deepagents-harbor/` (`stats.py` Wilson CIs + MDE, `failure.py` infra-vs-capability over ATIF; catalog path `libs/harbor/scripts/analyze.py` is stale at the pinned commit).

---

## Prompt 1 — Data engineer: Harbor-native trial ingestion and trace export

```
Role: data engineer for eval-lab's Harbor-native track.

Goal: every Harbor trial we run (oracle, nop, codex, terminus-2) must land in
derived/parquet through the existing ingest path with zero manual steps, and our
harbor-traces exports must be reproducible from the same bytes.

Starting state (verified 2026-09-06):
- 30 real jobs rebuild with fact/state-journal reads at 30 each and 480 byte-identical
  Parquet outputs (see ~/Downloads/harbor-continuous-verification.json).
- derived/harbor-traces/ holds HF datasets per canary job via the Python API
  (export_traces + save_to_disk); the CLI hf format writes nothing without --push.
- Known upstream bug (still in harbor main): trials naming a custom agent class crash
  the whole export at traces_utils.py AgentName lookup. Our codex/zai trials hit this.
- New packs staged but not yet ingested: derived/harbor-packs/terminalworld/tasks/
  (20 tasks), derived/harbor-packs/facet/tasks/ (15 tasks).
- FACET loader incompatibility (blocks FACET runs, TW unaffected): every FACET
  task.toml sets `[task] name = "FACET-Terminal"`, which harbor 0.21.0 rejects
  (`TaskConfig`: package name must be `org/name`; `TaskModel.is_valid_dir` False,
  `harbor run -p ...` fails "Either datasets or tasks must be provided").
  TW `tw_100459` validates True and scored oracle reward 1.0. Do NOT rewrite the
  pack: define the fix at our load boundary (or file it upstream) and record the
  decision. Oracle trials emit no `agent/trajectory.json`, so `traces export`
  correctly refuses them (`NotImplementedError: oracle does not support ATIF`) —
  trajectories arrive with the first approved agentic run.
Do, in order:
1. Confirm how a fresh `harbor run -o runs/<job>` output becomes catalog rows +
   derived/parquet partitions today (trace the code path, don't redesign it).
   Document the exact commands in your handoff.
2. Ingest the oracle verification trials from runs/harbor-pack-verify and
   runs/harbor-tb21-oracle-smoke once they complete. Report row counts per table
   and any trial the pipeline refuses, with reasons.
3. Implement the smallest robust step that, after each future job, produces
   derived/harbor-traces/<job>/ (conversations + sharegpt + metadata) via the
   Python API, working around the custom-agent export crash WITHOUT patching the
   installed harbor package (filter/rename at our call boundary; file the upstream
   shape as a second artifact if you touch it).
4. Add a pack manifest for derived/harbor-packs/ (what's staged, source pin, license,
   sample rule, contamination class). Packs stay git-ignored; the manifest is committed.
   External corpora are behavior-study material only: never capability claims, no
   reward recompute, import outcomes stay flagged external (see
   research/external/harbor-index/README.md for the binding precedent).
5. State what is still missing for a 200-trial SFT-export view
   (`traces export --filter success --sharegpt` + OpenThoughts curation shape):
   which pieces exist, which need building, and the smallest next slice.

Constraints: read-only wrt production queue/policy/schedules; no paid-agent runs;
no new trajectory viewers (harbor view / atif-lens cover it); no Recovery-Bench;
full-suite-safe deterministic tests only for genuinely uncertain edges, else
throwaway verification scripts. Handoff must list files changed, focused checks
run with counts, and anything intentionally left untouched.
```

## Prompt 2 — Analyst: Harbor-native measurement on real trajectories

```
Role: analyst for eval-lab's Harbor-native track.

Goal: turn our Harbor trajectories (27 valid ATIF canary trials today, plus the new
TW/FACET/TB2.0 oracle trials landing in runs/) into measurements a job interviewer
would believe: reliability curves, cost tables, failure labels, and one controlled
intervention comparison.

Starting state (verified 2026-09-06):
- 27/27 runs/**/agent/trajectory.json validate as ATIF 1.7 with full per-step
  metrics (prompt/completion/cached tokens, cost_usd). Corpus cost ≈ $1.96.
- Reference implementations available: langchain-ai/deepagents
  libs/harbor/scripts/analyze.py (fetch it, MIT) and vendored
  research/external/harbor-ecosystem/vendor/vestige (pass@k vs pass^k).
- Labeling schemes studied in research/external/harbor-ecosystem/START-HERE.md §D:
  AgentRx 9-category taxonomy (adopt first), Model-or-Harness fault localization.
- Coming next (not yet run, needs Peter's approve): --skill A/B on one family,
  GEPA-terminus overnight, --load-trajectory continuation evals.

Do, in order:
1. Run deepagents analyze.py over our jobs and diff its trial statistics against
   our traj.py/DuckDB numbers on the same bytes. Report agreement and every
   divergence with a cause (parser difference, not opinion).
2. Run vendored Vestige over all multi-trial tasks and produce pass@k vs pass^k
   curves that separate agent stochasticity from environment flakiness. State the
   k policy used and where the data is too thin to claim anything.
3. Apply the AgentRx failure taxonomy to the failed trials (start with
   canary-transaction-reconciliation/codex failures): category + critical failure
   step per trial, in a committed table with trajectory IDs. Calibrate on a
   5-trial overlap sample and report agreement before labeling the rest.
4. Design (do not run) the first --skill intervention measurement: one
   debugging-discipline SKILL.md on one task family, same slice ± skill,
   pre-declared metrics (success rate, cost, tool-call counts, blind-retry counts).
   Write the analysis spec so the data engineer can execute it blind.
5. Write the contamination and claim-boundary note for TW/FACET-derived numbers:
   public tasks, unknown model exposure, their verifier under their harness.
   Nothing from these packs enters a capability number or a card Result section.

Constraints: no new viewers/dashboards; no universal capability score (see
research/inbox/feature-analysis-meta-analyst-reply.md §3.4: cross-axis arithmetic
without a validated scale binding refuses); null-on-zero-denominator everywhere;
every rate states its denominator. Handoff must give tables with n's, the exact
commands to reproduce them, and what remains unestablished.
```
