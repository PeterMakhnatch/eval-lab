# Reef loop pool: task pool, split proposal, and gate design (2026-09-25)

Status: proposal for Peter and HAR-73. Nothing here is a registration, a committed
split, or an executed model run. Produced on branch `feat/reef-loop-pool`
(worktree `.worktrees/reef-loop-pool-20260925`) by the LoopPool worker.

Question: which tasks, how many repeats, and what time and money does a
trustworthy Reef harness decision cost when **Eval Lab runs the evaluation**?

## Files

| File | Content |
|---|---|
| `inventory.json` | per-candidate inventory (registry state, digests, controls, resources, platform, Terminus 2 fit, catalog history); regenerate with `inventory.py --history <export>` |
| `controls.json` | tonight's oracle/nop control runs (job, task, package digest, reward, wall seconds, verdict) |
| `split-proposal.json` | dev / held-out proposal with task ids, package digests, rationale |
| `gate-design.json` | exact gate operating characteristics and planning grids; regenerate with `gate_tables.py` |
| `harness-tree-baseline/` | byte-identical copy of HAR-71's baseline Terminus harness tree (digest `sha256:058bb47…e286d`), used by the parked noise-floor specs |
| `morning-packet.md` | the 20 parked noise-floor specs, their ids, and Peter's exact approve/tick commands |

Code: the calculator is `src/evallab/power.py`
(`paired_gate_publish_probability`, `paired_gate_plan_grid`, `PairedGateRule`),
covered by `tests/test_power_gate.py`.

## 1. Inventory (summary)

Fifteen candidates were inventoried (7 registry tasks, 3 MCP benchmark families,
8 exp05 trap tasks); full detail is in `inventory.json`.

- **Plain-terminal, arm64-native, fresh or current controls** (pool-ready):
  exp05's 8 tasks (python:3.12-slim-bookworm, tmux/asciinema preinstalled —
  built for Terminus 2), `event-summary`, `syn-funcdag-easy`,
  `travel-lisbon-002` (registered, controls current at digest), and
  `transaction-reconciliation` (registry candidate; tonight's control ran at the
  current registry package digest `sha256:f2bb698d…`).
- **Plain-terminal but heavy** (reserve): `query-optimize` (900 s agent deadline,
  2 GB, internet allowed), `terminal-bench-html-js-filter` (3600 s agent
  deadline, separate browser verifier).
- **Not Terminus-2 plain-terminal** (excluded from the first pool):
  `tau3-retail-1` (streamable-HTTP MCP runtime; allowed_uses `canary` only) and
  the `action-memory-v1` / `mcp-funcdag-v1` / `mcp-recovery-v1` MCP benchmark
  families (FastMCP sidecars).
- No image in the pool pins `--platform=linux/amd64`; nothing runs emulated.
- Catalog history (read-only SELECT on the shared catalog, 2026-09-25) gives
  per-task outcome rows by agent/model in `inventory.json`; the two registry
  tasks with the richest model history are `event-summary` (12 agent/model
  combinations, e.g. opencode glm-5.3-flash 14 trials mean reward 0.857) and
  `syn-funcdag-easy` (opencode glm-5.3-flash 20 trials mean reward 0.474 — a
  naturally mid-range task).

Explicitly missing: per-task Terminus-2 pass rates for any model on the exp05
pool (only exp05 round 1's single episodes for qwen3-coder:30b, and HAR-71's
0/4 for qwen2.5:7b on two lab tasks); benchmark-family task counts
(materialization-time facts).

## 2. Free controls (tonight, local Docker, ≤2 concurrent Harbor trials)

Standing rule `local-controls` (`policy/standing-approvals.yaml`), executed
through `evallab run` in this worktree with an **owned ephemeral catalog**
(`evallab_loopool_20260925`, dropped after proof) and an owned derived root; the
shared catalog received zero rows (verified: `SELECT count(*) FROM jobs WHERE
job_name LIKE 'loopool%'` → 0). Verdict rule: valid iff oracle reward = 1 and
nop reward = 0 with no exception; infra-failed otherwise. Full table:
`controls.json`.

- All 8 exp05 tasks: oracle 1.0, nop 0.0, ~7.2–7.7 s per trial. **Valid.**
- `transaction-reconciliation`: oracle 1.0 (17.9 s), nop 0.0 (15.0 s), at the
  current registry package digest. **Valid.**
- `query-optimize`: oracle 1.0 (580.1 s), nop 0.0 (561.5 s), at the current
  registry package digest. **Valid.**
- `terminal-bench-html-js-filter`: oracle 1.0 (136.1 s), nop 0.0 (16.7 s), at
  the current registry package digest (browser verifier built and ran). **Valid.**

All three registry candidates that lacked current-digest evidence this morning
now have it from tonight's runs.

## 3. Split proposal (for Peter and HAR-73)

`split-proposal.json` proposes exactly exp05's structure, which is the only
candidate set with construct-paired dev/held-out tasks already exercised by a
Reef gate round:

| Construct | Dev | Held-out |
|---|---|---|
| bugfix (hidden tests) | search/fix-median | holdout/fix-slugify |
| data (csv, exact output) | search/sales-total | holdout/inventory-value |
| logs (json) | search/error-count | holdout/status-codes |
| text (exact output, ties) | search/top-words | holdout/top-tags |

Dev-extension candidates (transaction-reconciliation, event-summary,
syn-funcdag-easy, travel-lisbon-002, then the two heavy reserves) have no
same-kind held-out partners; adding them grows dev only. **Held-out gap:** no
held-out stock exists beyond exp05's four; enlarging it means authoring new
tasks of each kind — flagged for Peter, not done tonight.

## 4. Gate design

The calculator is **exact** (full probability convolution over decisive-pair
configurations; no simulation, no seed). Two rules are modeled, with Reef
citations: Reef's default `score_comparison` publishes iff wins − losses > 0
(`reef/train/cordis_backend/backend.py`, `ScoreComparisonMixin.decide`), and the
HAR-72 gate is a one-sided exact sign test at α on valid pairs, ties dropped,
plus a per-task regression veto (gate adapter `rules.py`, `decide_pairs`;
default `regression_failure_threshold=1`).

Validation:

- **exp04-check (12/12 rows exact).** At the recorded pass rates
  32/60, 34/60, 39/60, the calculator reproduces every `gate_table` row of
  `work/04-check/summary.json` to the recorded 3 decimals: null sign-test FPR
  0.019/0.030 (5/10 episodes per task), +0.2 power 0.169/0.404, +0.2 majority
  power 0.543/0.845, +0.4 row 0.761/0.996/0.681/0.977, null majority FPR
  0.34/0.426. See `gate-design.json: exp04_check_validation` (`all_match: true`).
- **HAR-72 ceiling A/A.** At pooled rates [1.0, 0.9799, 1.0], 5 repeats × 3
  tasks, the computed false-publish rate is 2.9e-9, matching RE's independent
  prediction (2.94e-9) and the observed 0/30 publishes. **Caveat (per RE and
  confirmed by the calculator):** at ceiling pass rates almost every pair ties,
  so that A/A says little about noise at mid-range rates — the pool needs
  mid-range tasks for the target model.

Headline findings:

1. **Reef's default gate cannot be made trustworthy by scale.** At mid-range
   pass rates its false-publish rate under identical trees is 0.36 at 4 tasks
   and rises toward 0.5 as tasks/repeats grow (04-check's observed A/A
   false-publish rate was 36.7% = 11/30; its table predicts 0.34–0.43). More
   episodes make the majority rule *more* confident in noise, not less.
2. **The sign-test gate controls the false-publish rate** (2.2% at the 4-task ×
   5-repeat cell under mid-range rates) but needs pool size to gain power:
   FPR ≤ 5% and power ≥ 0.8 at **+0.2/task** requires ≈11–12 tasks × 8 repeats
   (176–192 trials per decision, both sides). At **+0.4/task** the same
   requirements are met by **4 tasks × 5 repeats** (40 trials), exactly the
   proposed first-run cell — under mid-range rates its FPR is 2.2% and power
   0.877.
3. **Thin empirical rates** (exp05 round 1: two tasks at 1.0, two at 0.0 for
   qwen3-coder:30b) distort planning: pairs at p=1 never split, so the
   requirement table improves deceptively. The noise-floor run exists to
   replace these with measured mid-range rates.

Cost of one decision (exact tables in `gate-design.json: decision_cost`):

| Design | Trials | Local qwen3-coder:30b (exp05-observed 76.1 s/episode, 2 concurrent) | Metered glm-5.3-flash (HAR-71 token envelope, $0.15/M in, $0.50/M out) |
|---|---:|---:|---:|
| 4 tasks × 5 reps | 40 | 0.85 h, $0 | ≈$0.15–0.30 [estimate] |
| 11–12 tasks × 8 reps | 176–192 | 3.7–4.1 h, $0 | ≈$1.3–1.5 [estimate] |

DeepSeek V4.1 flash is wired for mini-swe-agent in main, not Terminus 2; RE's
HAR-72 A/A drove it through Reef's own serve (31.3 s median per 30-episode
evaluation on tutorial tasks). A Terminus-2 DeepSeek/GLM-Flash host-side route
requires runner work owned by ReefTraffic (see `morning-packet.md`).

## 5. Morning packet

Twenty noise-floor specs are prepared, submitted, and **parked in
`queue/waiting/` of this worktree** — one pinned Terminus 2 harness tree
(HAR-71 baseline `sha256:058bb47…e286d`; proven to load and execute with
verified skill locks in HAR-71's merged-revision proof) × 4 dev tasks × 5
repeats, on the metered `zai/glm-5.3-flash` route, $0.40/trial ceiling,
estimated ≈$0.08 total. **Nothing was approved or executed.** Exact spec ids
and Peter's commands: `morning-packet.md`.

## Limits

- No model call of any kind was made tonight; all numbers are either exact
  computation, recorded evidence, or marked estimates/[INFERENCE].
- The Bernoulli-independence model ignores within-task correlation across
  repeats (a task that is "hard for this model" may fail every repeat); the
  veto model partially covers the protective direction. Treat grid outputs as
  planning figures, not guarantees.
- The calculator assumes fully valid episodes; infra failures shrink evidence
  and are not modelled.
- exp05's tasks are external to Eval Lab and unregistered; the proposal does
  not register them.
