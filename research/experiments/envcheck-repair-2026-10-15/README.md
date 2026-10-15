---
status: living
audience: [builder, analyst, runner]
---

# EnvCheck repair stage (2026-10-15): ref-subset and QA Task flags

## Authority and acceptance rule

Research assignment, 2026-10-15 11:00 ET (Peter away; relayed by
research-harbor wK:pH, `FOCUS.md` "2026-10-15 assignments"): first land or
supersede draft PR #867, then repair tests for tasks flagged by Traces
ref-subset and QA Task. Start with 002402, 002391 and the ref-subset
high/medium list.

PR #867 was already merged (squash `5e2bb943e`, 2026-10-14), so there was
nothing to land or supersede; this branch builds on it from `origin/main`.

A repair is accepted only if, on the derived test-only variant:

- the correct (full reference) fix passes **3 of 3** grades,
- the do-nothing (base) submission fails **3 of 3** grades, and
- **every** shortcut submission that passed before now fails.

"Shortcut" for a ref-subset flag is every proper subset of the reference
fix that scored reward 1 in the probe report: the minimised reproducer
plus each passing drop-file / drop-block grade. For the two seeds, the
shortcuts are the Oct 13 mutation survivors (002402 is ref-subset
`not_applicable` — one code run, no proper subset; 002391 is ref-subset
`pass` — every block is needed).

Spend: $0 paid. Subscription Codex only, gated at 60% weekly consumption
before every model call; local Docker only, serial (one sandbox container
at a time from this tab).

## Flag inventory (Traces ref-subset sweep, derived/qa/ref-subset)

High/medium at intake: 23 tasks (3 high, 20 medium). Independent triage
(`_triage/batch{1,2}.jsonl`) labels 10 **confirmed weak** (000371, 000471,
000657, 000847, 000931, 001686, 001750, 001804, 002205, 002976), 8
out-of-scope, 3 neutral, and 3 untriaged (000081 high, 000560 medium,
002742 high). Repair attempts start with the confirmed-weak list; an
out-of-scope or neutral flag is not repaired by inventing assertions.

## Tooling

`evallab hack ref-repair <ref-subset report dir> [--execute]
[--feedback <prior result>]` (`src/evallab/ref_repair.py`): preview writes
the proposer prompt with no model call; `--execute` runs one quota-gated
subscription proposal, derives a test-only variant via the existing
`tasks derive` path, and grades it in the ref-subset sandbox under the
x3/x3/all-shortcuts rule. Verdicts: `repaired`, `rejected`,
`unrepairable`, `blocked`.

## Outcomes

Recorded per task in `results.json` in this directory as attempts finish;
raw receipts stay under `runs/envcheck-repair-1015/` in the worktree and
are retained outside Git before the worktree is retired.
