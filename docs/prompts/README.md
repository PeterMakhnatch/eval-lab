# Implementation briefs and mission prompts

This directory holds two kinds of work order, and this index covers both. It
is checked against the directory: every committed file below is listed, and
nothing is listed that does not exist.

## Numbered component briefs

Scoped handoffs for coding agents. They are numbered because later components
depend on evidence contracts established earlier.

Attribution below is only what `agents/archive/2026-08-15-role-registry.md`
states explicitly; `—` means the frozen registry does not name the brief, not
that the work is unbuilt.

| Brief | Outcome | Named owner in the frozen registry |
|---|---|---|
| [01-atif-index.md](01-atif-index.md) | validated ATIF facts and Parquet projection | ANALYST (briefs 01–03) |
| [02-cohort-compare.md](02-cohort-compare.md) | deterministic cohort comparison | ANALYST (briefs 01–03) |
| [03-analysis-pipeline.md](03-analysis-pipeline.md) | provenance-bearing model analysis wrapper | ANALYST (briefs 01–03) |
| [04-proposal-gate.md](04-proposal-gate.md) | follow-up proposals with an execution approval gate | — |
| [05-queue-executor-policy.md](05-queue-executor-policy.md) | the queue, the executor, and the policy admission gate | — |
| [06-headless-doctor-launchd-digest.md](06-headless-doctor-launchd-digest.md) | headless `doctor`, launchd schedule, nightly digest | — |
| [07-canary-suite-drift.md](07-canary-suite-drift.md) | canary suite and drift detection | — |
| [08-phoenix-trace-shipping.md](08-phoenix-trace-shipping.md) | Phoenix + ATIF trace shipping with OpenInference | OBSERVER (brief 08) |
| [09-judge-calibration-dspy.md](09-judge-calibration-dspy.md) | judge calibration, then DSPy experiment 1 | JUDGE (brief 09) |
| [12-bounded-researcher-loop.md](12-bounded-researcher-loop.md) | bounded 24/7 researcher loop and fleet digest | — (AUTOPILOT's mission matches it) |

**Briefs 10 and 11 were never written.** The numbers are reserved, not lost.
Brief 10 was the deferred LanceDB memory layer (`docs/design-additions.md`
still marks `lancedb` as "deferred until brief 10"; no `memory` dependency
group exists in `pyproject.toml`, so it was never executed). Brief 11 was the
Streamlit surface and asset migration (`docs/fleet-tracking.md` refers to "the
Streamlit app (brief 11)"). No brief file was written for it; the Streamlit
surface that exists is `dashboard/`, merged by the DASHBOARD role in PRs #11
and #15, so the capability landed without a brief behind it. Do not renumber to
close the gap — the references above point at these numbers.

## Mission prompt sets

Dated, multi-mission dispatch documents rather than single-component briefs.
Newest first.

| File | Contents | Standing |
|---|---|---|
| [next-functionalization-missions-2026-08-15.md](next-functionalization-missions-2026-08-15.md) | M006-R repair, M009 integrator live flight, M010–M014 | Newest generation |
| [functionalization-missions-2026-08-15.md](functionalization-missions-2026-08-15.md) | M005, M006, M007 | Generation that produced the missions now in flight |
| [system-cartographer-2026-08-15.md](system-cartographer-2026-08-15.md) | the system cartography mission | Spent — merged as PR #52 |
| [wave3-missions.md](wave3-missions.md) | OBSERVATORY, TRUTH, ROSTER/REGISTER/NIGHTLY/FOUNDRY | Historical — codename roles, retired by M001 |
| [overnight-missions.md](overnight-missions.md) | INGEST, OBSERVER, ANALYST, RUNNER, AUTOPILOT | Historical — codename roles, retired by M001 |

### Which generation is authoritative

**`agents/missions/ACTIVE.md` is authoritative, not any file here.** A prompt
set states what was dispatched on its date; the board states what is true now.
Where the two disagree, the board wins and the prompt set is history.

As of 2026-08-15 the board points at
`functionalization-missions-2026-08-15.md` for the M-numbered prompts, because
that is where M006 and M007 — the two missions actually at review — are
specified. `next-functionalization-missions-2026-08-15.md` is the later
document (merged as PR #48) and holds the M006-R repair prompt plus the
unstarted M009–M014 forward plan. So neither file supersedes the other
wholesale: use the older one for the missions in flight and the newer one for
the repair and for what comes after. Read the board first either way.

## Rules for anything in this directory

Use one writing agent per Git worktree and one work order per branch. Before
starting, the agent must read `AGENTS.md`, `agents/WORKFLOW.md`,
`agents/STRUCTURE.md`, `docs/architecture.md`, `docs/analysis-loop.md`, and the
selected brief or mission. Do not run a paid model, cloud sandbox, large sweep,
deployment, or publication as part of implementation or validation without
Peter's explicit approval.

The briefs intentionally require runnable increments rather than an all-at-once
platform build. Each one ends with fixture-based tests and a documentation
update. If current Harbor behavior contradicts a brief, inspect the installed
version and adapt the implementation while documenting the discrepancy; do not
invent compatibility behavior from memory.
