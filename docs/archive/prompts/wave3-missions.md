---
status: historical
audience:
  - builder
  - operator
---

> **Archived work order**: Completed historical mission set (retired by M001). Living contracts: agents/missions/ACTIVE.md, agents/OWNERS.md, agents/WORKFLOW.md. Board: agents/missions/ACTIVE.md.

# Wave 3 mission prompts — 2026-08-14

Copy-paste source so prompts never live only in chat scrollback.
Dispatched so far: SOLIDIFY (running). Launch-now-safe: OBSERVATORY, TRUTH.
After SOLIDIFY merges: ROSTER, REGISTER. Later: NIGHTLY, FOUNDRY.

## STANDING ORDERS (paste above every mission)

Lab: ~/Developer/eval-lab. Setup per agents/WORKFLOW.md: worktree
.worktrees/<mission>, branch role/<mission>. Read AGENTS.md,
agents/WORKFLOW.md, agents/CHECKS.md, docs/checkpoints/2026-08-14.md first.
This lab runs on SUBSCRIPTIONS ONLY: never introduce, forward, or read
API-key env vars; credentials are keychain/auth-file probes only.
"Done" means SOLID, not green-once: every acceptance criterion passes 3
consecutive runs AND from a fresh clone; paste command output in your
handoff as evidence. Do not stop at the first passing state — after
acceptance, execute your continuation list until no item under ~1h remains.
When blocked on one thread, record it and advance another; update
agents/handoffs/<mission>.md every 30 min; PR "<MISSION>: …" merges only
with gh pr checks fully green and scripts/premerge.sh passed. Never merge
agent-authored tasks into registered/*; never loosen policy/.

## OBSERVATORY (Gemini drone — 24/7 volume role, zero collisions)

Your current mission: OBSERVATORY — a standing, long-running production
role. [Standing Orders apply, with one change: your PRs are small and
frequent — one per completed batch, never larger than 15 units.]
You own ONLY research/observations/ and agents/handoffs/observatory.md.
You never modify src/, tasks, policy, or anything another role owns. Your
outputs are DRAFTS — structured observations, not conclusions.

PHASE 1 — build your own workshop (before any production):
research/observations/TEMPLATE.md — fixed per-trial observation record:
trial id/job/agent/model/task/reward; steps taken; first-failure step (or
"none"); loop detected (same command >=3x: yes/no + step); verified-
before-done (yes/no); tool errors count; one-sentence plain summary; NO
speculation fields. Plus CHECKLIST.md: the 8 mechanical steps per trial,
and a SELF-AUDIT procedure: every 10 records, re-derive 2 random earlier
records from scratch and diff — mismatch means stop, fix checklist, redo
the batch. Run pytest+ruff to confirm you break nothing (you touch no
code; any failure means you strayed).
PHASE 2 — calibrate: produce records for the 5 trials that have reference
labels in research/calibration/trajectory-labels/; factual fields must
match >=90% before production; record the score in your handoff; below
90%: fix checklist, repeat.
PHASE 3 — produce, forever: every completed trial in runs/ oldest-first
(skip ones already recorded); one record per trial under
research/observations/<job>/<trial>.md; batch of 10 -> self-audit -> PR ->
next batch. Backlog empty? Poll for new completed runs (canary nights feed
you daily); between batches, upgrade records made under older template
versions (version-stamp every record). Never idle: backlog -> audit ->
template improvements -> backlog. Handoff always states: records produced,
current agreement score, backlog remaining.

## TRUTH (statistics + trajectory reports; safe to launch now)

Your current mission: TRUTH. [Standing Orders apply]
Make comparisons honest or impossible. In cohort.py: bootstrap-over-TASKS
intervals (attempts within a task are one evidence unit), paired-by-task
default for any two-cohort question, `evallab power` (min detectable
difference for given n,k; required n,k for a target). compare REFUSES to
print a ranking unless it can name: n_tasks, k, interval, and the
elicitation tuple (agent version, model pin, preamble hash, toolset, k) —
otherwise it prints the literal sentence "not distinguishable / not
comparable" with the reason. Validation: simulation tests with known
truth — null difference (~5% false-finding), large difference (detected),
clustered-wider-than-naive regression test.
Then the reading layer: `evallab report family <task>` — one trajectory-
level report per job family from Parquet + ATIF: first-failure step
distribution, loop detection, does-it-verify-before-done, cost/steps.
Plain-language output a non-statistician reads. Continuation: eval-card
template in research/cards/ auto-drafted from a completed spec (config
digest, numbers with intervals, elicitation, contamination note, threats).

## ROSTER / REGISTER / NIGHTLY / FOUNDRY

Full texts in the 2026-08-14 chat (aggregated-plan message). Append here
before dispatching each — prompts must land in this file at dispatch time
so the repo, not scrollback, is the record.
