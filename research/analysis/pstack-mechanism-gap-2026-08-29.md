# Pstack mechanism gap analysis — main `53a3af58`

Assignment F. Compares the current Pstack source mechanism-by-mechanism against
`.omp/skills`, `AGENTS.md`, `agents/*`, CI workflows, premerge, governance, generated
docs, and worktree discipline. Layers are separated: observed facts, inference,
forecast.

## 1. Observed facts

### Pstack source, as fetched

Fetched from the `cursor/plugins` GitHub API and raw endpoints on 2026-08-29, default
branch `main`, licence MIT (`pstack/LICENSE`).

| Measure | Value |
|---|---|
| Blobs under `pstack/` | 157 (tree not truncated) |
| Skills (`pstack/skills/*/SKILL.md`) | 45 |
| Groups | `skills` 122 files, `docs` 17, `automations` 12, `agents` 2, root 3, `.cursor-plugin` 1 |

Of the 45 skills, 21 are `principle-*` behavioural rules; the rest are workflows
(`poteto-mode`, `swarm`, `arena`, `architect`, `reflect`, `recall`, `tdd`, `why`,
`how`, `teach`, `interrogate`, `unslop`, `technical-writing`,
`create-verification-skill`, `maintain-verification-skill`, `show-me-your-work`,
`blast-radius`, `no-comments`, `figure-it-out`, `setup-pstack`, `automate-me`, `bro`,
`arena`, `make-bot-ui`, `typescript-best-practices`).

### Already adopted, verified in-tree

| Pstack mechanism | Where it landed | Evidence |
|---|---|---|
| `blast-radius` | `.omp/skills/change-impact/SKILL.md` | Merged in PR #299. Provenance line cites the Pstack source. |
| `principle-encode-lessons-in-structure` | `.omp/skills/repository-health/SKILL.md` §"Route recurring failures to structure" | 5-level escalation ladder, type → lint/CI → helper → boundary → skill |
| `principle-prove-it-works` | `agents/CHECKS.md` "Definition of Green" + `change-impact` proof levels 1–4 | Green is a property of the exact PR head; level 3/4 required for material risk |
| `principle-make-operations-idempotent` | `AGENTS.md:32`; pervasive test coverage | `sql/schema.sql` idempotency rule; `test_lance.py::test_build_analyses_idempotent`, `test_craft.py::test_cli_scan_is_idempotent_and_skips_rewrite`, `test_batched_scan_is_idempotent_on_rescan`; nightly registry carries a per-step `idempotent: bool` flag across 11 steps |
| `principle-migrate-callers-then-delete-legacy-apis` (code scope) | `change-impact/SKILL.md:46` | "Do not add compatibility layers in place of migrating known callers." |
| `principle-separate-before-serializing-shared-state` (instruction scope) | `agents/WORKFLOW.md:20` | "One writer per tree, disjoint paths per role" |
| `principle-fix-root-causes` (practice scope) | Not a stated rule; visible in handoffs | `agents/archive/2026-08-23-handoffs/explorer-truth.md:90,109`, `fixture-drift.md:39` trace symptoms to root causes |

### Not present anywhere in the governance surface

Searched `AGENTS.md`, `agents/`, `.omp/`, `scripts/premerge.sh` for each concept.

| Concept | Search result |
|---|---|
| decision log / decision trail | no match |
| PR size, PR shape, stacked commits | no match |
| context window budget | one incidental match (`craft.py` batch-size comment), no rule |
| build the lever / commit the script | no match (`lever` appears only as "elicitation lever", unrelated) |

### Measured repository state supporting the gaps

Pull-request queue, `gh pr list` on 2026-08-29:

| PR | Diff | CI | Merge state | Review decision |
|---|---|---|---|---|
| #280 research(goldset) frozen labeling package | **+56151 / −0, 8 files** | green | — | **null** |
| #282 policy-gated campaign runner | **+7687 / −341, 45 files** | green | **DIRTY** | **null** |
| #261 mcp-recovery-v1 | +3278 / −24, 22 files | **FAIL** | — | **null** |
| #230, #260, #272, #275 | small to medium | green | — | **null** |

Every open pull request has `reviewDecision: null`. #230 has been green and
unreviewed since 08-27.

Branch and worktree inventory, computed from `git for-each-ref` and
`git rev-list --left-right --count origin/main...<branch>`:

| Bucket | Branches | Median commits behind `origin/main` | Carrying unmerged commits |
|---|---:|---:|---:|
| Active (≤6 h) | 29 | 9 | 28 |
| Warm (6–48 h) | 36 | 37 | 33 |
| Stale (2–6 d) | 53 | 113 | 52 |
| Dead (>6 d) | 43 | 279 | 43 |
| **Total** | **161** | — | **156** |

- Worktrees: **64**, of which 3 report `prunable`.
- Duplicate-intent branch stems: **11**, covering 22 branches — for example
  `atif-behavior-core` alongside `feature/atif-behavior-core-v1`; `role/solidify`
  alongside `role/solidify-close`; `pr-219-head` alongside `pr-219-review`.
- Branch prefixes in use: 14 (`role` 54, `feature` 24, bare 18, `feat` 14, `fix` 13,
  `lane` 11, `docs` 8, `research` 6, `hardening` 5, `preserve` 3, `architecture` 2,
  `hygiene`, `manifests`, `ops` 1 each).
- Eight single-commit `docs/*` branches are unmerged and 54–72 commits behind:
  `controller-final-closeout`, `instrumentation-ledger-track`, `model-budget-ledger`,
  `p0-focus-ledger`, `p0-stop-ledger`, `sidecar-retention-policy`,
  `stabilization-delta-442`, `trajectory-work-program-ledger`.

Context-pack pressure, from a generated analyst pack header
(`derived/context/repo-standards-pstack.md`):

| Measure | Value |
|---|---|
| Configured token budget | 12,000 |
| Estimated untruncated size | ~116,224 tokens |
| Tokens shed | **103,119 across 34 dropped documents** |
| Retained living docs | 8 |

`origin/main` advanced `7f4d1d40` → `b9597b4d` → `53a3af58` during this session.

## 2. Gap table with disposition

| # | Pstack mechanism | Current state | Disposition | Grounding evidence |
|---|---|---|---|---|
| 1 | `principle-sequence-verifiable-units` — **delivery altitude** (commit/PR shaping) | Absent. `CHECKS.md` defines green, not reviewable shape. | **ADOPT** | #280 at +56151 lines in one PR; #282 at 45 files and `DIRTY`; all PRs `reviewDecision: null` |
| 2 | `show-me-your-work` — TSV decision trail | Absent as a format. Ad-hoc equivalents exist and do not land. | **ADAPT** (format only; drop helper script, slash command, cross-model review requirement, transcript globbing) | 8 single-commit `docs/*` ledger branches, unmerged, 54–72 behind |
| 3 | `principle-build-the-lever` | Practised, never contracted. | **ADOPT** | `verify_roadmap_claims.py` in #300; citation-verification ledger in #269. Both ad hoc. |
| 4 | `principle-guard-the-context-window` | Absent. | **ADAPT** (state as scoping defect, not a token ceiling) | Context pack shed 103,119 of ~116,224 tokens against a 12,000 budget |
| 5 | `principle-separate-before-serializing-shared-state` | Instruction exists (`WORKFLOW.md:20`); structural ownership partial via `lane/*`. Pstack is explicit that "instructions and conventions are not concurrency control." | **DEFER to Custodian** — enforcement, not authoring | 161 branches, 64 worktrees, 11 duplicate stems |
| 6 | `principle-migrate-callers-then-delete-legacy-apis` — branch/worktree generations | Adopted for code only. | **DEFER to Custodian** | 43 dead branches at median 279 behind, all carrying unmerged commits; `role/*` is 54 of 161 |
| 7 | `principle-make-operations-idempotent` | Already adopted, with per-step metadata. | **REJECT as redundant** | `AGENTS.md:32`; nightly registry `idempotent` flag; 3 named idempotence tests |
| 8 | `principle-encode-lessons-in-structure` | Already adopted. | **REJECT as redundant** | `repository-health` escalation ladder |
| 9 | `principle-prove-it-works` | Already adopted at process level. | **REJECT as redundant** | `CHECKS.md` green definition; `change-impact` proof levels |
| 10 | `principle-fix-root-causes` | Practised; not stated. | **REJECT — not worth instruction weight** | Already the observed default in handoffs; adding prose duplicates behaviour without a gate |
| 11 | `principle-type-system-discipline` | Partially covered by the zero-diagnostic `ty` gate. | **REJECT for now** | `CHECKS.md` ty ratchet at zero already forces the outcome; the skill is TypeScript-centric |
| 12 | `poteto-mode`, `swarm`, `arena`, `architect`, `automate-me`, `bro`, `make-bot-ui`, `setup-pstack` | Absent. | **REJECT** | Cursor slash-command and model-router surfaces; no repo-native trigger. Ceremony without a demonstrated failure. |
| 13 | `no-comments` | Absent. | **REJECT** | Conflicts with formula, digest, and licence provenance comments this repository requires |
| 14 | `typescript-best-practices` | Absent. | **REJECT** | `AGENTS.md:11-15` forbids TypeScript without Peter's approval |
| 15 | `unslop`, `technical-writing` | Absent as skills. | **REJECT — already behavioural** | House prose style is already enforced by review, not a missing mechanism |
| 16 | `create-verification-skill` / `maintain-verification-skill` | Absent. Repository has `scripts/premerge.sh` plus 14 workflows. | **HOLD** | No demonstrated verification gap; premerge already reproduces CI. Revisit only if a task family lands without an oracle. |
| 17 | `reflect`, `recall` | Absent. | **HOLD** | Plausible value for overnight loops, but both spawn subagents and read transcripts; needs an authorization decision, not a skill file |
| 18 | `why`, `how`, `teach`, `interrogate` | `why` partly covered inside `change-impact:21` (`git blame`, history, linked PR). | **REJECT as separate skills** | The load-bearing part is already inline; four more files is catalogue growth |

Adopt or adapt: 4. Defer to Custodian: 2. Reject or hold: 12.

## 3. What this pull request changes

One file: `.omp/skills/delivery-sequencing/SKILL.md`, covering gaps 1–4 in a single
skill because they share one theme — how work is delivered so a reviewer can verify
it cheaply. Four separate files would be the catalogue growth that
`repository-health:35` prohibits.

Deliberately excluded from the skill: the TSV helper script, `/slash` surfaces, the
mandatory cross-model review subagent, and transcript globbing.

Not touched: `docs/INDEX.md` and `docs/repo-map.md` (owned by PR #301); any branch,
worktree, or evidence directory.

## 4. Inference

Labelled as inference, not observation.

- Integration capacity, not production capacity, is the binding constraint. Seven or
  more pull requests sit green and unreviewed while 29 branches were touched within
  six hours. Adding lanes deepens the queue.
- Staleness is self-reinforcing past roughly 100 commits behind. At 279 median
  commits behind, rebasing a dead branch costs more than rewriting its intent, so the
  43 dead branches holding unmerged commits are effectively unrecoverable regardless
  of intent.
- The 11 duplicate-intent stems and 14 prefixes are the branch-level form of the
  compatibility-layer smell that `principle-migrate-callers-then-delete-legacy-apis`
  targets: a second name kept alive because the first was never retired.
- A 103,119-token shed against a 12,000-token budget indicates the pack request is
  mis-scoped rather than the budget being too small. Nine tenths of the assembled
  input was discarded before the consumer saw it.

## 5. Forecast

Labelled as forecast. No claim that these are measured.

- Splitting bulk payloads out of code pull requests should convert #280-class
  reviews from unreviewable to reviewable without changing what lands. Falsifiable:
  if #280 still sits unreviewed after being split, the constraint is reviewer time
  and not pull-request shape.
- A committed decision trail for overnight runs should replace single-commit ledger
  branches. Falsifiable: if new `docs/*-ledger` branches keep appearing after this
  skill lands, the format was not the obstacle.
- Retiring the `role/*` generation should remove roughly a third of branch noise at
  zero risk if tips are archive-tagged first. This is Custodian's call, not mine.

## 6. Implementation contracts for Custodian

Exact contracts for the two deferred gaps. Not implemented here.

**Contract A — structural single-writer enforcement (gap 5).**
Precondition: `git worktree list --porcelain` is the inventory source; naming is never
inferred. Deliverable: a check that fails when two live worktrees have the same
branch checked out, or when a tracked path is dirty in more than one worktree. Wire
into `governance check`, not a new gate. Acceptance: passes on current main; fails on
a synthetic two-worktree same-branch fixture. Never deletes a dirty worktree.

**Contract B — branch generation retirement (gap 6).**
Precondition: every candidate is `>6 d` old, `>150` commits behind `origin/main`, has
no open pull request, and is not checked out by any live worktree. Deliverable: for
each candidate, create `archive/<original-name>-<short-sha>` at the tip, then delete
the branch; prune only worktrees git already reports `prunable`. Acceptance: tag count
equals deleted-branch count; `git rev-list` of every archived tip resolves after
deletion; no dirty worktree removed. Reversible by construction.

## 7. Checks run

| Check | Result |
|---|---|
| `uv run ruff check .` | pass |
| `uv run python -m evallab.governance check` | pass |
| `uv run pytest tests/test_inbox_conformance.py` | pass |
| Test coverage of touched paths | none — no test references `.omp/` or `research/analysis/`; additions are test-neutral |
| `docs/INDEX.md` / `docs/repo-map.md` regeneration | not required; neither `docindex` nor `repomap` scans `.omp/` |

## 8. Residual design items

- Whether `reflect` and `recall` justify a subagent-spawning skill needs an
  authorization decision, not an authoring decision.
- `create-verification-skill` stays on hold until a task family lands without an
  oracle; premerge currently reproduces CI adequately.
- The 14 branch prefixes deserve a single naming contract, but that is Custodian's
  structural call and would conflict with active lane branches if imposed now.
