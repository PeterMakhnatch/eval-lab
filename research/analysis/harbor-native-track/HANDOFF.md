# Analyst lane handoff — Harbor-native track (standing mission)

```yaml
role: analyst
mission: continuous experiment analysis and follow-on proposals
charter: research-context/harbor/corpus/OMP-LANE-LEADS-2026-09-07.md (Analyst section)
origin: Peter via wK:pH, standing direction 2026-09-08 (replaces finite campaigns)
as_of: 2026-09-08
repo_head: 884dbc17 (feat/dispatch-queue-compiler; dirty tree is user's work, untouched)
```

## Current assignment

Continuous analysis over ecosystem experiments + lead-ready follow-on packets.
Prior finite deliverables (evidence, not claims): measurement pack `50ca365f`
(27 ATIF trials: stats diff with DuckDB +87% overcount finding, reliability
curves with confounded transaction fan, 25 AgentRx labels 7-model/18-harness,
frozen skill spec); corpus delta `3b3f1b3f`.

## Observed results (facts only)

- New runs since 2026-09-06 (surveyed 2026-09-08, R1): `runs/harbor-tw20-oracle`,
  `runs/tb21-codex-terra-slice`, `runs/harbor-pack-verify`,
  `runs/harbor-skill-verify`, `runs/harbor-tb21-oracle-smoke`,
  `runs/funcdag-codex-canary` updates.
- Lab digest 2026-09-08: queue running 0, approved 0; 4 codex baselines waiting on
  paid authorization; 3 zai-opencode specs unreadable (schema validation errors);
  codex window 99% used (stale 33h); judge calibration: none calibrated, admission
  gate closed.
## Landed 2026-09-08 p2 — command/failure-pattern consumer (branch
analyst/command-pattern-consumer @752a7c00, worktree
.worktrees/analyst-command-patterns; lead-validated, NOT merged)

- Module `src/evallab/interpretation/command_patterns.py`: Jaccard command-only
  repetition (≥0.8) distinct from River-penalty equivalence (cmd&obs >0.8);
  changed-observation rate; recovery events; last-step inspection; availability
  denominators. Reuses traj.py parsing; no new IR, no schema change.
- Corpus: 34 eligible ATIF trial dirs (23 canary + 4 tb21 + 4 funcdag + 3
  minimal-luna); oracle-only and crash-missing excluded with reasons; Quality
  30+19 direct-Docker controls cited as context only (not ATIF input).
- Output `command-pattern-output/`: 34/34 records; 27/27 ATIF-coverage match;
  34/34 rewards match; 7-model/18-harness split reconciles; cited examples
  reproduce (1e40baab steps [12,15,17], river turn 17). Lead re-ran 3 trials
  independently: exact match.
- Selection brief: 2 Harness candidates (coverage-expansion continuation,
  verify-before-done stop gate; both model-backed, NOT approved) + 2 Quality
  candidates (gaia2 wait-loop adjudication; kziNARo regrade decision — both
  deterministic, need Quality owner sign-off; regrade verdict-affecting).

- Successor landed 2026-09-08 (wK:pH training program; branch
  analyst/command-pattern-consumer @bd20761f, NOT merged): FIRST fixed-model
  (gpt-5.6-terra) baseline vs native dspy-rlm development contrast (C3 spec,
  NOT run) + ONE curation ablation. Selector `select_contrast.py` reproduces
  lead-frozen S=16/Q=16/empty-diff and 6 seeds; 72d21d01 resolved as PmTen2E
  (dir-vs-task-name confusion), S stands. Ablation finding: process gate
  VACUOUS (S−Q empty) — do not pay for process-gated curation; falsifier is a
  future success with river-penalty/non-inspected turns. dspy-rlm qualified
  structurally (registered, -m/--ak/budgets) BUT SUPPORTS_ATIF=False (reward-
  only arm until conversion) + missing harbor[dspy]/deno block execution.
  Split/exclusion/eval-boundary/API-denominator bindings frozen; all model
  arms NOT approved. Next: R4 chess-best-move read; open questions (submodel
Canonical output handoff (monitor-registered): command-pattern-output/HANDOFF.md
on branch analyst/command-pattern-consumer @a172dfc0 (C4 + R4 landed, validated).

- R1 evidence-survey: 22 trials across 6 run groups + Quality 30+19 cohort with
  paired before/after rewards + Factory readiness (blocked: accounting/spend
  envelopes) + ATIF coverage. Key new analyzable: tb21-codex-terra-slice 4/4
  scored with full ATIF (rewards 3×1.0, 1×0.0 chess-best-move); TW oracle 5/8
  (3 ARM64 Docker build failures, infra class). All denominators in report.
- R2 query pack v1.0.0: `queries/01-04*.sql` + README. Lead reproduced 01
  (12 rows; crash jobs rate NULL). 02 ALL_JOBS prompt sum 2,795,536 reproduces
  s1_out exactly. 03 exposes 18 hidden duplicate featured rows. 04: 41 keys,
  24 complete, 0 orphans. State change vs trial-statistics-diff.md: the store
  NOW projects the 3 `trials*/` funcdag trials (re-ran since 2026-09-06); their
  live gap is coverage-export membership only.
- R3 continuation-harness design: `packets/continuation-harness-design.md`
  (frozen, NOT run, NOT approved). Falsification structure over premise arms
  P1/P2/X + controls; seeds = 6 AgentRx-1 html trials by trajectory ID;
  M1–M7 frozen (M6 self-verification count new; M7 replay_suspected flag new).
  Verified by lead: `--extra-instruction-path` IS routed RunRequest→command on
  HEAD (`execution_contracts.py:213,679-680`); `--load-trajectory`/`--skill`/
  `--export-traces` are ABSENT there — dependency correctly recorded.

## Ready (replenished)

- R4 chess-best-move read: AgentRx-style read of
  tb21-codex-terra-slice chess-best-move__JhBKoLn + contrast vs the 3 passing
  rollouts in the same job (same agent/model/date). Free, existing ATIF.
- R5 variance-by-scaffold priors over the 52k corpus (DuckDB direct).

## Waiting (needs approval/resource/owner outside analyst scope)

- W1 skill A/B execution (~$1.50): spec frozen; needs Peter approve + lane.
- W2 AgentRx judge on 7 model-side failures: needs lane approval (subscription).
- W3 GEPA overnight: needs approve.
- W4 four codex baselines in queue: `paid_run_unauthorized`, needs Peter.
- W5 zai-opencode spec schema errors: Eval Runner/schema owner; recorded, not owned.
- W6 continuation packet Stages 0–2: all codex arms NOT approved; codex window
  99% used (digest 2026-09-08, reading 33h stale); `--load-trajectory`
  passthrough unmerged (origin/feat/hn-run-20260906).

## Next (queued behind ready)

- N1 = R4/R5 above (dispatched next activation).
- N2 regrade→Parquet ingestion proposal (Data Engineer boundary; propose only).
- N3 analysis read-out for Quality five-arm audit when their evidence lands.

## Boundaries (do not cross)

No edits to `src/evallab/cli.py`, execution DTOs (Eval Runner), projections/
curation code (Data Engineer), workbench views (Harbor Integration),
`missions/ACTIVE.md`, or leases. No sealed-task inspection (incl. FACET
task_000009). No paid/model-backed runs. Interpretations stay labeled as
hypotheses until grounded; raw measurements, inferred categories, and semantic
adjudication remain distinct fields.
