Status: review-wanted
Last: recorded the exact 2026-08-15 integration state and replacement-orchestrator sequence
Next: independently review PRs #47 and #49 at the exact heads recorded below
Blockers: main checkout has user-owned local state; do not reconcile it without first preserving and classifying that state

# Replacement orchestrator handoff — 2026-08-15

This is the durable transition packet for the next main coordinating agent. The
function is **Integrator**; the agent or model filling it may change. Do not
invent a new organizational role name for each mission.

## Copy-paste startup prompt

```text
You are the Integrator for ~/Developer/eval-lab. Peter is the Sponsor and final
authority. Your job is to maintain an exact view of repository and experiment
state, turn Peter's goals into bounded missions, arrange independent reviews,
resolve integration decisions, and merge only exact-head green work. Stay
available to Peter; delegate implementation instead of disappearing into it.

Read completely: AGENTS.md, agents/WORKFLOW.md, agents/STRUCTURE.md,
agents/OWNERS.md, agents/CHECKS.md, agents/missions/ACTIVE.md, this handoff,
docs/prompts/next-functionalization-missions-2026-08-15.md, and the Grok System
Cartographer handoff/report when present. Trust git, GitHub, and run artifacts
over stale status prose.

Hard boundaries: subscription-only; never introduce/read/forward API-key env
vars; no paid model, cloud sandbox, large sweep, deploy, or publication without
Peter's explicit approval. Never loosen policy/. Workers stop at review; they do
not merge. Green means every GitHub check succeeds for the exact PR head plus an
independent semantic review. Use one writer per worktree and disjoint path
leases. Preserve the dirty main checkout until its local state is classified.

First: refresh refs and independently review PR #47 at
1f4cf6fe77eb7d0649b35cbdecd538f949c9bf08 and PR #49 at
c6c35a499ceaf1dcbedfc515c8ca1ff086b9b2fd. Verify the repaired acceptance
defects listed in this handoff rather than merely rerunning tests. Merge one at
a time only if the exact head remains green and the review is clean; after the
first merge, rebase/retest the second if GitHub says it is stale or conflicted.
Record every decision. If a defect remains, dispatch a narrowly scoped repair
agent on that PR branch and retain the reviewer as reviewer.

In parallel, let Grok finish System Cartographer. Review its evidence-backed
map before converting proposals into missions. After the two PR gates and
cartography review, run M009 as the integration proof. Then M010 and M011 may
run in parallel; M012 follows M009, and M013 follows successful M009+M010.
M014 is later hardening. Keep Peter's desired outcome visible: run a Harbor
task, watch ingestion and analysis happen, inspect evidence and findings in one
operator surface, and eventually operate that loop safely around the clock.
```

## Sponsor intent

Peter wants a functional evaluation R&D platform, not a growing pile of
blueprints. The near-term experience should be:

1. select or author a trustworthy Harbor task;
2. launch a policy-compliant experiment;
3. see current and recent execution state;
4. ingest the completed run into one experiment → job → trial → trajectory →
   analysis join path;
5. inspect evidence, traces, deterministic facts, and cited analysis;
6. compare experiments honestly; and
7. queue the next approved work without babysitting every process.

Peter favors substantial three-to-four-hour missions for Gemini and Grok, but
volume is subordinate to explicit acceptance criteria, path ownership, and
independent review. A 24/7 service is an objective after the synchronous path is
proven, not permission to automate an unproven path.

## Authority and responsibility

| Decision | Authority |
|---|---|
| Research direction, spending, policy changes, task registration, publication | Peter (Sponsor) |
| Repository truth, mission sequencing, path leases, semantic review, conflicts, merge/sunset | Integrator |
| Implementation inside a leased worktree; local validation; PR and handoff | Mission worker |
| Statistical or domain claims | Reviewer with evidence; Peter decides consequential tradeoffs |

The PR author owns ordinary rebases and repairs on its branch. The Integrator
owns cross-mission conflicts and decides which intent survives; it should not
ask Peter to click through a conflict without first explaining the competing
semantics. Peter is consulted only when preserving one side changes product,
research, policy, cost, or irreversible data decisions.

## Observed repository state

Snapshot time: 2026-08-15 after `git fetch origin`.

- `origin/main` is `903abe4` (`INTEGRATION: add system cartographer mission
  (#50)`). Immediately preceding are `aee9b81` (next functionalization missions)
  and `00f36ab` (release M006/M007).
- The primary checkout is **not** a safe integration tree. It is at `b5c29a8`,
  `ahead 1, behind 13`, with modified `digests/DISCOVERIES.md` and untracked
  `docs/prompts/Untitled` and `docs/repo_overview.html`. These are user-owned
  until classified. Do not pull, reset, clean, delete, or overwrite them.
- Only two GitHub PRs are open at this snapshot: #47 and #49. Both are reported
  mergeable, and all five reported checks are successful at the recorded heads.
- Grok is active in `.worktrees/system-cartographer` on
  `role/system-cartographer`, based at `903abe4`. Its untracked
  `agents/handoffs/system-cartographer.md` says `Status: building`, no blocker,
  with intended writes limited to its handoff,
  `docs/checkpoints/2026-08-15-system-cartography.md`, and
  `docs/system-cartography.html`. It has not committed or opened a PR yet.
- The M006 and M007 repair agents are finished. Their test claims are evidence
  from the authors, not substitutes for independent review.

## Immediate integration gates

### Gate A — PR #47, M006 analysis worker

- URL: https://github.com/PeterMakhnatch/eval-lab/pull/47
- Exact head: `1f4cf6fe77eb7d0649b35cbdecd538f949c9bf08`
- Snapshot: mergeable; `profile`, `lint`, `ty`, `test (3.12)`, and `test (3.14)`
  all succeeded.
- Author validation: 46 focused tests, 418 full tests, Ruff, premerge, and ty
  ratchet green.
- Earlier independent review, at an older head, found three acceptance defects:
  a crash after provider return could cause a duplicate live call; lease
  deletion had an ownership race; and staging failure lacked durable nightly
  evidence.
- The repair claims an fsynced invocation journal with explicit ambiguity
  resolution, descriptor/token-owned locking, and a durable staging event.
- Review those exact state transitions at the current head. Also verify that
  the default adapter remains intentionally unavailable and its calibration
  gate remains closed. Do not imply that merging M006 enables unattended live
  model analysis by default.

### Gate B — PR #49, M007 task-quality workbench

- URL: https://github.com/PeterMakhnatch/eval-lab/pull/49
- Exact head: `c6c35a499ceaf1dcbedfc515c8ca1ff086b9b2fd`
- Snapshot: mergeable; the same five GitHub checks all succeeded.
- Author validation: 35 focused tests, 407 full tests, Ruff, and premerge green.
- Earlier independent review, at an older head, found execution was not bound
  to the inspected task/digest, symlinks could leak hidden inputs, Docker builds
  could fetch from the network, and determinism/reporting claims were too weak.
- The repair claims path/digest binding before execution, symlink containment,
  build-time network denial, stronger verifier determinism semantics, and a
  truthful packet check vector.
- Review those properties at the current head, including adversarial path and
  mutation cases. This task is a certification tool; false green is worse than
  a refusal.

### Merge procedure

1. Assign different independent reviewers when possible. Reviewers inspect the
   exact heads and report findings with file/line evidence.
2. If findings remain, dispatch a bounded repair on the existing PR branch.
   Re-review the new exact head; previous green checks do not carry forward.
3. If clean, confirm `gh pr checks <number>` is fully successful and confirm the
   head SHA again immediately before squash merge.
4. Merge one PR at a time. Fetch `origin/main`; then determine whether the other
   branch requires a rebase and fresh checks.
5. Update the mission board and archive/sunset state from a fresh Integration
   worktree. Do not reuse a squash-merged branch.

## Grok System Cartographer

Canonical mission prompt:
`docs/prompts/system-cartographer-2026-08-15.md`.

Grok's job is to produce an evidence-backed system map and executable HTML, not
to implement product features. Treat its component and mission proposals as
recommendations until the Integrator verifies them against current main. Let it
finish without assigning overlapping writes. Review especially:

- what is operational versus scaffolded;
- the exact human journey from experiment intent to visible analysis;
- boundaries among Harbor, Eval Lab, Phoenix, and future post-training exports;
- which missing capability most directly unlocks an observable end-to-end run;
- whether proposed long missions have measurable demonstrations and disjoint
  ownership.

## Next work sequence

The canonical detailed prompts are in
`docs/prompts/next-functionalization-missions-2026-08-15.md`. This sequence is
the current plan; alter it only with recorded evidence from cartography or a
failed gate.

1. **Review and merge M006/M007.** This is the blocking integration work now.
2. **Review System Cartographer.** Reconcile its observed map with current main
   and turn only the highest-value gaps into missions.
3. **Safely reconcile the primary checkout.** Inventory the unique local commit
   and three local paths. Preserve them on a branch or backup before making the
   main checkout match `origin/main`. Ask Peter if ownership or intended value
   is ambiguous; never discard them merely because they look stale.
4. **M009 — live integration flight.** From merged main, demonstrate the
   approved local control path end to end: task/run → immutable evidence →
   ingest/facts/trajectory → analysis sidecar → explorer/dashboard. Use
   oracle/nop or existing evidence only; no paid call. Record every command,
   identifier, expected UI state, and failure. This is the first proof that
   separate components form a usable system.
5. **M010 and M011 in parallel after M009.** M010 qualifies one real stage-5
   analysis runtime through the queue and keeps the calibration gate closed
   until criteria pass. M011 creates the first certifiable task pack and runs it
   through M007 without registering it automatically.
6. **M012 — unified operator cockpit.** After M009 has exposed the real journey,
   connect status, experiment submission, run evidence, trajectories, and cited
   analysis into one operator flow. Avoid a second dashboard architecture.
7. **M013 — restart-safe analysis service and soak.** Only after M009 and M010
   prove the synchronous and qualified-provider paths. Demonstrate recovery,
   leases, ambiguity handling, backpressure, STOP, and a meaningful local soak.
8. **M014 — CI determinism.** Useful hardening, but it should not displace the
   operational path unless current CI becomes unreliable.

## Long-horizon mission candidates

Do not dispatch all of these immediately. Ask the cartographer to rank them and
require a zero-to-one demonstration plus a bounded path lease before launch.

- **HARBOR-CONTRACT:** a version-aware, executable Harbor integration contract:
  capability manifest, supported task/result schemas, drift tests, and safe
  oracle/nop recipes. Do not build a copied Harbor wiki.
- **TRACEGRAPH:** correlate Phoenix traces with experiment, job, trial,
  trajectory, and analysis identifiers, then link from the operator surface.
  Phoenix remains derived and disposable; Harbor evidence remains canonical.
- **EXPERIMENT-STUDIO:** turn a validated experiment spec into a reviewable
  queue submission and show progress/results without hiding policy decisions.
- **FOUNDRY-BATCH:** author several candidate Harbor tasks, certify them with
  M007, and produce admission packets; never self-register agent-authored tasks.
- **TRAINING-EXPORT:** produce versioned, provenance-rich supervised/preference
  examples from immutable trajectories and cited analyses. This is the bridge
  toward post-training, not a training system itself.

Avoid a model leaderboard mission until there are enough registered tasks,
frozen elicitation profiles, and approved trials for the statistics to mean
something.

## Current architecture judgment

Observed capabilities already present include Harbor execution wrappers,
guarded queue/policy/STOP/cost/auth controls, immutable evidence promotion,
PostgreSQL and Parquet/ATIF projections, deterministic trial facts, cohort
statistics and reports, a nightly/researcher loop, CLI/dashboard/explorer
surfaces, and Phoenix export.

Observed or strongly evidenced gaps are: no human-registered task in
`library/registry/`; M006 and M007 are not yet merged; no merged-main M009 proof;
the operator UI is split; Phoenix is not joined back into the research graph;
and there is no post-training data product. The cartographer should confirm or
correct these claims.

The honest product description today is **a real-environment evaluation R&D
control plane and evidence/data factory**. Calling it a post-training platform
would currently be an aspiration, because dataset curation/export, training,
checkpoint evaluation, and promotion loops are not complete products.

## Coordination rules that prevent another sprawl

- Keep four stable lanes from `agents/OWNERS.md`; missions are temporary IDs,
  not permanent personas.
- Keep at most one Integration mission and one writing mission per leased path.
- Every mission states dependency gates, owned paths, forbidden paths, a demo,
  stop conditions, and who reviews it.
- The Integrator maintains one live board. Chat is not the source of truth.
- A worker that finishes opens a PR and stops at `review-wanted`; it does not
  invent its successor or merge itself.
- Prefer two or three deep missions with non-overlapping demonstrations over a
  dozen speculative roles.
- The Integrator reports to Peter in four lines: what merged, what is running,
  what is blocked and why, and the next decision Peter actually owns.

## Do not do

- Do not touch or clean the dirty primary checkout until its state is preserved.
- Do not merge #47/#49 solely because CI is green.
- Do not launch another agent on Grok's three owned cartography paths.
- Do not enable a live analysis provider before exact runtime qualification.
- Do not let an agent register its own authored task or weaken hidden-input
  isolation.
- Do not introduce API-key environment variables; subscription auth is via
  approved keychain/auth-file probes.
- Do not run paid models, cloud sandboxes, large sweeps, deploys, or publication
  without Peter's explicit approval.
- Do not treat Phoenix, PostgreSQL, or generated dashboards as canonical
  evidence. The immutable Harbor/evidence files remain the source of truth.

## Required orientation files

- `AGENTS.md`
- `agents/WORKFLOW.md`
- `agents/STRUCTURE.md`
- `agents/OWNERS.md`
- `agents/CHECKS.md`
- `agents/missions/ACTIVE.md`
- `docs/architecture.md`
- `docs/analysis-loop.md`
- `docs/prompts/next-functionalization-missions-2026-08-15.md`
- `docs/prompts/system-cartographer-2026-08-15.md`
- `agents/handoffs/system-cartographer.md` and its eventual cartography report

## Transition definition of done

The new Integrator has taken over when it has refreshed this snapshot, assigned
independent exact-head reviews for #47 and #49, acknowledged the dirty-main
preservation requirement, inspected Grok's current handoff, and told Peter the
next single decision that requires Sponsor authority. Everything else should be
handled by the Integrator or bounded mission workers.
