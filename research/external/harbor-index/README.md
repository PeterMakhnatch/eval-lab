---
status: living
audience:
  - runner
  - analyst
  - operator
---

# Harbor-Index corpus — acquisition investigated, corpus PENDING

**State: `pending`. No trial files were acquired.** This directory holds the
acquisition-path investigation, not data. It exists so the next attempt starts from
evidence instead of repeating tonight's probes, and so nobody plans a feature on a
corpus the lab does not have.

`docs/prompts/gym-campaign.md` GYM-DATA cycle 1 expected: *"harbor-index publishes
its 82 tasks and all 1,476 leaderboard trials … Fetch via the pinned-acquisition
path (fetch.py), verify digests, land under `research/external/harbor-index/`. This
multiplies the lab's trajectory holdings ~15× for free."* The 82 tasks and the
1,476 trials are two different artifacts, and only the tasks are publicly
distributable today.

## What the corpus is (from the published release note, 2026-07-07)

82 tasks selected from 6,627 candidates across 54 benchmarks, spanning 29
benchmarks and seven domains. 1,476 rollouts = one per agent-model pair per task,
across 9 models × 2 harnesses. Their own audit of those rollouts: 134 solved (9%),
1,259 honest failures (85%), 9 gamed the verifier (0.6%), 74 infra false negatives
(5%). No agent-model pair exceeds 30%.

That audit is exactly why the corpus is attractive to TRAJ: it is a large,
frontier-diverse, **outcome-labelled** trajectory set. It remains attractive. It is
just not downloadable by us right now.

## Every probe, with its result

`fetch.py`'s pin list does not contain it:

```
$ uv run python -m evallab.cli fetch --list | grep -ci harbor-index
0
# 83 pins present, including terminal-bench@2.0, terminal-bench-pro@1.0,
# terminal-bench-sample@2.0 — no harbor-index entry.
```

No bulk export on the site:

```
404  https://harbor-index.org/data/v1/trials
404  https://harbor-index.org/data/v1/trials/index.json
404  https://harbor-index.org/data/v1/manifest.json
200  https://harbor-index.org/data/v1/trials/algotune-optimize-lti-sim__3EUgiDP/   (HTML page, 54 KB)
```

Only per-trial pages resolve. Scraping 1,476 HTML pages was **not** attempted: it is
not a pinned acquisition, produces no verifiable digests, and is not what the
`fetch ≠ register` discipline means.

Harbor's own Hub CLI (0.21.0) reaches the leaderboards but not the rows:

```
$ harbor hub leaderboard list --json          # works unauthenticated
harbor-index leaderboards: 5
  2026-07-28  f05852ba-…  harbor-index/harbor-index      harbor-index-1-4
  2026-07-18  18fc94cf-…  harbor-index/harbor-index      harbor-index-1-3
  2026-07-13  d7dec3cd-…  harbor-index/harbor-index      harbor-index-1-2
  2026-07-11  6fb9b39f-…  harbor-index/harbor-index      harbor-index-1-1
  2026-07-07  a74d914e-…  harbor-index/harbor-index-1.0  harbor-index-1-0

$ harbor hub leaderboard show a74d914e-6653-45a5-b08f-3c8606307afd --json
rows: 0        # same for 1.3 and 1.4 — every harbor-index leaderboard returns zero rows
               # leaderboard metadata is present: "Leaderboard for the Harbor Index 1.0
               # benchmark (82 agentic tasks)", dataset_version_ids: [e106c59e-…]

$ harbor hub job list
ID  Name  Status  Started  Trials  Errors  Reward  Cost      # empty: no publicly visible jobs

$ harbor hub trial show algotune-optimize-lti-sim__3EUgiDP
Error: trial_id must be a UUID.
$ harbor hub trial download algotune-optimize-lti-sim__3EUgiDP
Error: 'algotune-optimize-lti-sim__3EUgiDP' is not a valid UUID.
```

`harbor hub trial download` is the right transport — it just needs trial **UUIDs**,
and no public endpoint enumerates them. The website's slugs are not UUIDs, and the
trial page embeds neither a UUID nor an API path (checked: zero UUID matches, zero
`/api/` or `_next/data` references in the 54 KB response), so it is a client-rendered
app calling an endpoint we have not been given.

## What would unblock it

1. **Hub credentials.** `harbor hub job list` returning empty suggests jobs are
   visibility-scoped; an authenticated account that can see the harbor-index org's
   jobs would make `harbor hub job trials <job>` → `harbor hub trial download <uuid>`
   the complete pinned path. This is a Peter question (does the lab have or want a
   Hub account?), not a build task.
2. **An official export.** If the harbor-index team publishes the rollouts as a
   dataset (HF or Hub dataset package), that is a one-line `fetch.py` pin and the
   original plan works unchanged.
3. **The tasks, separately.** The 82 **tasks** are a Hub dataset
   (`hub.harborframework.com/datasets/harbor-index/harbor-index-1.0`,
   `dataset_version_ids: [e106c59e-6d25-410a-9c7e-1cbee4a89703]`). Acquiring tasks is
   a different mission from acquiring trials, and it interacts with the registry
   decision — tasks would be *candidates*, never auto-registered.

Deliberately **not** done: no `fetch.py` pin was added for a target that cannot
fetch, and no `sql/external_views.sql` view was written for rows that do not exist.
Both would be scaffolding that reads as capability.

## Contamination note — binding on any future use of this corpus

These are **public models' rollouts on public tasks**, published as a leaderboard.

- **Behaviour-study material only. Never capability claims.** Nothing derived from
  this corpus may enter a lab capability number, a card's Result section, or any
  comparison against lab-run trials.
- Exposure is unknown and unknowable per task: the tasks are drawn from 29 public
  benchmarks, several of which predate the models that ran them.
- The rewards are **their** verifier's verdicts under **their** harness and timeout
  policy (1.2× fastest-model runtime, or 3h for tasks all models fail). Those are
  elicitation and instrument choices, not neutral facts. **No reward recompute** —
  imported outcomes stay theirs, flagged external.
- `fetch ≠ register`: acquiring this corpus would never register a task. Registration
  is human-only and separate.

## Next

Queue item 2 in the GYM-DATA backlog is the **llm-as-a-verifier TB 2.1 trajectory
corpus** (`data/terminal_bench_2.1_trajs/`), and unlike harbor-index its repository
is publicly reachable (`https://github.com/llm-as-a-verifier/llm-as-a-verifier` →
`200`, GitHub API → `200`). That is the better next target: same discipline, same
contamination class, but an actual public artifact behind it.
