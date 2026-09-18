# Harbor build-out plan: trajectories → feature-rich data → synth evals (2026-09-06)

Direction: Harbor-native only. No porting, no new viewers/exporters, no paid runs
without Peter, Recovery-Bench on STRICT HOLD, RL training stays external (no CUDA
on Mac). Governing contracts: `FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md` (C0
features barred from synth seeding; 7 certification gates), hn-track
`contamination-claim-boundaries.md` (public packs = behavior-study only).

## Where things stand (observed, today)

- Analyst track Tasks 1–4 DONE in `research/analysis/harbor-native-track/`:
  deepagents-vs-traj diff (store over-counts cost $3.77 vs true $2.01 — reprojection
  duplicates + stale rows), Vestige curves (only transaction-reconciliation
  non-degenerate, cohort-confounded), 25 AgentRx failure labels (7 model / 18
  harness), frozen `--skill` intervention spec (needs ~$1.50 approve). Remaining:
  Task 5 contamination note → now written (`contamination-claim-boundaries.md`).
- Staged packs: `derived/harbor-packs/terminalworld/tasks/` (20; sweep below),
  `derived/harbor-packs/facet/` (zip + 15-task sample; BLOCKED — every task.toml
  names `[task] name="FACET-Terminal"`, rejected by harbor 0.21.0 `org/name` rule),
  TB2.0 oracle smoke 3/3 in `runs/harbor-tb21-oracle-smoke`.
- TW 20-task oracle sweep DONE (`runs/harbor-tw20-oracle`): 5/8 loaded tasks
  oracle 1.0 (tw_100459, tw_10865, tw_117921, tw_118507, tw_129015); 3/8
  RuntimeError = Docker environment bitrot (e.g. tw_126385 pins matplotlib 1.4.0
  / numpy 1.12.0, unbuildable today — infra class, not capability); 12/20 never
  loaded (task.toml sets both `memory='2G'` and `memory_mb=4096` with conflicting
  values; 0.21.0 correctly refuses — upstream data bug, fix at our load boundary
  with recorded policy or file upstream, never by rewriting the pack).
- Vendored: `vendor/vestige` (d03e8a7), `vendor/deepagents-harbor` (stats+failure,
  07d2952), `vendor/agent-data-protocol` (040a279), `vendor/atifact` (db0bf0f).
- Corpus: `derived/harbor-packs/tb-trajectories/` — 52,104 TB trajectories, 89
  tasks, 26 scaffolds, Apache-2.0, DuckDB-readable in 0.26s.
- Runner: `extra_instruction_path` passthrough exists; `--skill` /
  `--load-trajectory` / `--export-traces` passthrough do NOT exist yet.

## Wave 0 — assign first (all free, all runnable AFK)

1. **A4 store hygiene** (data engineer). `derived/parquet/traj_features` double/triple-counts
   trials (reprojection history, stale `trials*/` names) and misses 3 on-disk trials.
   Define the read rule (status filter + `source_sha256`-to-bytes dedupe + known-absent
   list) as a versioned view, add `projected_at`, prove naive `sum(cost)` returns
   $2.01. Acceptance: committed view + focused test on fixture rows.
2. **A1 offline distributions** (data engineer + analyst). Over tb-trajectories parquet:
   tool n-gram histogram, loop/retry rates, token/cost distributions by task and
   scaffold, oracle-vs-agent step-shape contrast. Output: one committed table +
   the top-5 tool-sequence skeletons as TASTE-style *proposals only* (no generation;
   contract gates apply). Acceptance: reproducible DuckDB query + MD table with n's.
3. **C3 Repo2RLEnv emitter trial** (builder). Emitter on one known repo →
   `harbor run -p out -a oracle` + nop + one cheat probe (first-use triple, free).
   Acceptance: oracle 1.0 / nop 0.0 / cheat 0.0 recorded, or a written verdict why
   the family is unsuitable. This is the synthetic supply line test.
4. **C5 harden-v0 on html-js-filter** (builder). Hacker→fixer loop against the one
   family with 6 known parser-differential misses (hn-track failure-labels Task 3
   names the vectors). Free oracle/nop/mutant runs only. Acceptance: exploit
   journal + verifier diff, or a written no-exploit finding.
5. **D1 new-distribution probes** (runner). `harbor run -d openthoughts-tblite -a oracle`
   and fetch `HuggingEnvs/data-agent-harbor-eval` (144 tasks) + oracle/nop/cheat
   trio on 3 tasks. Acceptance: pack-verification rows per contamination policy.

## Lane results 20:05 UTC (all verified by Main, isolated worktrees)

- Data (`feat/hn-data-20260906` b3242f20): read-rule tests 63 passed; store sums
  $2.01; corpus table committed (52,104 trials, 1.43M tool calls, 37% loop rate).
- Builder (`feat/hn-synth-20260906` aa16f1b): shim tests 4/4; Repo2RLEnv family
  REJECTED with cheat evidence; harden baselined; loader shims + upstream artifacts.
- Runner (`feat/hn-run-20260906` 1f48dc5): passthrough tests 8/8; `--skill`
  reproduced on Main checkout (event-summary oracle 1.0, skill digest
  sha256:cfc3a6… recorded in trial config, no-op semantics confirmed).
  D1/D2 trio tables are worker-reported (artifacts ran outside this checkout):
  tblite 2/3 (one dummy solve.sh), data-agent-harbor-eval 144 tasks MIT but no
  solve.sh (oracle N/A), data-eng-bench needs DB_TYPE=duckdb, WildClaw needs a
  13.4GB base image (defer). Pins recorded in derived/harbor-packs/MANIFEST.md.
## Wave 1 — needs Peter's approve (specs ready, do not run yet)

6. **Skill intervention execution** (~$1.50): spec frozen at
   `harbor-native-track/skill-intervention-spec.md`, skill frozen at
   `harbor-native-track/skills/debugging-discipline/SKILL.md`. Blind protocol incl.
7. **Continuation-eval pilot**: one failed html-js-filter trial ± `--load-trajectory`,
   k≥3, original verifier (needs `--load-trajectory` passthrough = item 8 first, or
   raw `harbor run` with Peter's OK to bypass the queue runner).
8. **AgentRx judge stage** on the 7 capability-failure trials (needs Azure OpenAI).

## Wave 2 — build (free, mechanical)

9. **C1 runner passthrough**: `--skill`, `--load-trajectory`, `--export-traces`
   from RunRequest through `execution_contracts.py` to `harbor run`. Unit-test with
   oracle (flag-forwarding only, no agent semantics). Unblocks 6–7 through the queue.
10. **A2 ADP on our export**: run vendored agent-data-protocol over a
    `derived/harbor-traces` ShareGPT export → first SFT-format view (export only,
    no training). Acceptance: schema-valid output + yield report.
11. **A3 atifact on quarantined lanes**: convert the 24 non-ATIF trials or certify
    unconvertible per lane. Acceptance: converted count + reasons table.
12. **C2 continuation-harness design**: analyst spec for premise-manipulation arms
    (Failure-as-a-Process: onset→propagation→collapse), following the skill-spec
    blind-protocol pattern. Design only.
13. **D2 WildClaw + data-eng-bench fetch**: 60-task MIT pack + 103-task DuckDB pack
    (30-task fast subset first), first-use triple each. New non-terminal family.

## Explicitly not now

RL training of any kind (TRL HarborSpec read only; cheapest credible = rented 4090
~$0.44/hr — Peter decision), Recovery-Bench (HOLD), Harbor Index trials (not
publicly fetchable), FACET runs before the loader decision, any viewer/exporter
build, any capability claim on public packs.

## Suggested AFK assignment (4 panes)

- Pane 1 (data eng): A4 → A1 → A2.
- Pane 2 (builder): C3 → C5 → D1.
- Pane 3 (runner): D1-assist → 9 (C1) → 13 (D2 fetch).
- Pane 4 (analyst): B Task 5 follow-ups → 12 (C2 design) → review Pane 1 tables.
