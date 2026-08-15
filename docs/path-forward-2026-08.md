# The path forward — 2026-08

Answers to five questions Peter set on 2026-08-14: what is this really,
what's the 24/7 workload, is task supply a problem, how do evals get written
with agents, and what is the road to the GLM-style end goal. Grounded in the
lab as merged tonight (SOLIDIFY, TRUTH, OBSERVATORY producing; ROSTER,
REGISTER, NIGHTLY, FOUNDRY pending dispatch).

## 1. What you are actually doing

You are building and operating a **benchmark engineering and agent
evaluation lab** — the instrument-and-measurement layer of the exact
pipeline GLM-5.3 just demonstrated. Realistic *now*: 2–4 agents compared
honestly on a pinned ~12-task suite; elicitation studies (prompt/tool/k
variants); drift tracking; judge calibration; trajectory science at
OBSERVATORY volume; gated task mutation. Realistic in 3–6 months: a
qualified agent-authored task library, TB4 submissions carrying your name,
public eval cards. Not realistic on this machine and not needed yet:
training anything, million-run scale, cloud fleets.

## 2. The 24/7 workload — spend chases questions, not quotas

"Use up the daily limit" inverted: keep a **standing backlog of pre-approved
question classes** so the queue is never empty and every token lands as
evidence. Six streams, scheduled:

| Stream | Cadence | What it burns tokens on |
|---|---|---|
| A. Measure | nightly | registered suite × every authed agent × k=3 |
| B. Analyze | nightly, capped | Loop-B compare + digest + DISCOVERIES draft |
| C. Ladder | continuous backlog | elicitation grid: preamble × toolset × k × agent on registered tasks — pre-registered A/Bs, powered via `evallab power` |
| D. Foundry | daily batch | task mutations + qualification batteries (oracle/nop/adversarial runs are the biggest legitimate token sink — they *produce* tasks) |
| E. Observatory | continuous | per-trial observation records (already running) |
| F. Meta | weekly | judge recalibration; canary re-baseline; gc/backup |

The one missing piece is **LADDER**: a generator that enumerates the
elicitation grid into queue specs under policy (per-agent quotas as the real
budget). Build it after ROSTER + REGISTER land; then the factory saturates
subscriptions indefinitely without a single unattended judgment call.

## 3. Task supply — you are not task-poor, and porting is the wrong move

Counted tonight: ~70–100 runnable local tasks (19 curated verified + 4 lab
+ benchmark slices + 41 QuixBugs), the upstream Harbor registry lists ~80
datasets (aider-polyglot alone is hundreds of tasks), TB3's merged set is
adapted already, TB4 is coming. Your scarcity is **runs and analysis per
task, not tasks**: at k=5 across 4 agents, the 12-task registered suite is
already 240 trials — more than the lab has run in its life. Verdict:
- Do NOT port computer-use/ML suites — high effort, contaminated,
  off-thesis. The middle reviewer was right: analysis-poor, not bench-poor.
- When more supply is genuinely needed, it is one `evallab fetch` from the
  Hub, pinned. That door is already built.
- For *synthetic seed material*, benchmarks are the wrong seeds anyway —
  FOUNDRY seeds from **registered tasks (mutation)** and **real scenarios**
  (the calibration corpus method), which you own in unlimited supply.

## 4. Writing evals with agents — the position, adopted

Hand-authoring is not the path; **hand-adjudicating is non-negotiable**.
Agents draft (FOUNDRY), batteries qualify (oracle 1.0 / nop 0.0 /
fair-oracle / adversarial), you register — a judgment that takes minutes,
not authorship that takes days. Your eval-writing skill is the *gate*, not
the pen. External validation loop: submit the best qualified task to TB4;
their reviewers are the free frontier-grade quality signal.

## 5. The road to the GLM-style end goal

The realization that matters: **the GLM pipeline = this lab + a rented
training rig.** Synthetic environments + verifiable rewards + post-training
is exactly: FOUNDRY + your verifier discipline + a GPU you don't own yet.
"Synthetically generating actually-relevant data" is not magic — it is
mutation + qualification against verifiable rewards with contamination
hygiene, i.e. the thing this lab does. Stages, each gated by the last:

- **S0 (now):** measurement factory runs itself; comparisons honest.
- **S1 — Foundry at scale:** agent-authored tasks passing the gate at a
  known rate; TB4 submission as external audit. *Gate: 20+ registered
  qualified tasks, ≥1 externally reviewed.*
- **S2 — Difficulty targeting:** generate tasks landing in the 20–80%
  frontier-agent success band (the only band useful for training signal) —
  requires S0's calibrated measurement, which is why today's work is not a
  detour. *Gate: foundry can hit a requested difficulty band.*
- **S3 — Data products:** export verified-reward trajectories from YOUR
  tasks as SFT-ready sets (Harbor has trace→SFT utilities). No training
  yet; the product is a dataset you can defend. *Gate: a clean, documented,
  contamination-annotated trajectory set.*
- **S4 — Post-training pilot:** QLoRA a 7–8B open model on S3's set
  (rented GPU, tens-to-low-hundreds of dollars), evaluate on YOUR held-out
  suite + frontier tasks. This is GLM-in-miniature and the first step that
  needs money. *Gate: measured, honest delta on held-out tasks.*
- **S5 — RLVR at scale:** only if S4 shows signal; infra gates in
  scaling.md apply.

The fundamentals checklist (mentor-review Part IV) is precisely the
S0→S1 gate. Nothing about the end goal requires abandoning the current
path; every current mission is on it.

## Next things to build (after the pending wave lands)

1. **LADDER** — the standing experiment-backlog generator (§2). The last
   piece of true 24/7.
2. **REGISTER completion + first honest 4-agent suite comparison** — the
   lab's first citable result (eval card #1).
3. **FOUNDRY batch mode** — 5 mutations/night through the battery; measure
   the qualification pass-rate (that number IS S1 progress).
4. **TB4 submission track** — best qualified task, submitted; external
   review as ground truth for the gate.
5. **SFT exporter (dry)** — S3 plumbing on existing oracle trajectories;
   zero training, proves the data product path.
