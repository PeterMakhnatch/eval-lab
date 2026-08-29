---
name: delivery-sequencing
description: Shape multi-step work and its delivery so a reviewer can verify it cheaply: verify each unit before the next when one serial writer owns the sweep, keep every commit green while still proving the bug was real, size and split pull requests that would land large or mixed, keep a decision trail for unattended runs, and commit the script that does or proves bulk work. Use for sweeps, migrations, long or unattended runs, and pull-request shaping. Concurrent fan-out is covered only to say delegates skip validation and the integration owner validates at a barrier.
---

# Delivery sequencing

`agents/CHECKS.md` defines when a pull request is *green*. This skill covers whether
it is *reviewable*. Both are required; green and unreviewable still blocks the merge
queue.

## Verify each unit before the next — one serial writer only

This section governs a **single serial writer** applying a run of similar edits: one
codemod, one migration, one sweep. It does not govern concurrent work.

In that serial case, bracket every unit: known-good state, one change, run the check,
proceed. Never batch the edits and verify once at the end — a break caught at the
unit that caused it is cheap to localize, and a break caught after a batch is already
built on.

Rebase onto current `origin/main` before the first check so every result measures
against the real baseline, not a stale one.

**Concurrent fan-out inverts this.** Parallel workers do not validate. Each delegate
skips formatters, linters, and test suites, because validating mid-flight blocks
workers on each other's in-progress edits and produces failures that belong to no
one. Validation is the integration owner's job, at a defined barrier or after
integration. Name one integration owner per batch, decide the shared contracts before
dispatch, and give every delegate a non-overlapping write scope.

## Build the script that does or proves the work

When the work is not trivial, write the codemod, generator, query, or rerunnable
check instead of doing it by hand. The script is the artifact a reviewer can rerun;
hand-done work can only be re-verified by redoing it.

- Do the first unit by hand to learn the recipe, then build the script and diff its
  output against the hand-done unit.
- Make it safe to rerun, because a reviewer will rerun it.
- Commit it when the work outlives the session, so the next run reruns it instead of
  redoing it.
- If you claim this and the diff contains no script, generator, or check, you did
  not do it.

Prefer one deterministic pass over fanning the same mechanical edit out to
subagents. When you do fan out, put the recipe, the write-scope fences, and the
do-not-touch list in one artifact every delegate reads, kept outside their write
scope. The verification contract in that artifact states what the **integration
owner** will check at the barrier, not work the delegates perform.

## Size and order the pull request

**Every commit must be green.** `agents/CHECKS.md` makes green a property of the
exact head, and any commit can become a head under bisect, revert, or a partial
merge. A deliberately red commit is therefore prohibited, which rules out the
failing-test-first shape: a red commit cannot stand alone.

Demonstrate that the bug was real without ever going red:

- put the regression test and the fix in the **same** commit; or
- land a green test first that pins the old behaviour through a fixture or
  parametrised case, so the pre-fix path is asserted rather than merely broken, then
  flip the expectation with the fix.

Beyond that, order commits so the sequence proves the work: the subtraction before
the reshape, the baseline capture before the treatment, the scaffold before the
feature. Each commit stands alone, is green on its own, and the stack reads as an
argument.

Split before opening when any of these holds:

- generated, promoted, or bulk data would land in the same pull request as code or
  contract changes — land the payload as evidence with a manifest, and review the
  code separately;
- the change spans unrelated contracts that reviewers would have to evaluate
  independently;
- part of the work is already green and the rest is not — ship the green part.

A pull request nobody can finish reviewing is not faster than two that merge.

## Keep a decision trail for unattended work

For long-running, overnight, or multi-phase work a human reviews after stepping
away, append one row per decision to a tab-separated log: `ts`, `phase`,
`decision`, `why`, `evidence`, `result`.

- Evidence is a pointer — commit SHA, pull-request number, `file:line`, artifact
  path — never a paragraph.
- Append-only. A wrong call gets a superseding row; never edit history.
- Log forks, completed units with their verification result, pivots and reverts
  with the trigger, blockers, and gate fixes. Skip the self-evident.
- Ephemeral trails live at `runs/decision-trails/<slug>.tsv`. `runs/` is already
  gitignored and already declared in `agents/STRUCTURE.md`; do not invent a new
  top-level directory, because the repository root is frozen.
- When the trail itself is the deliverable, write a durable report under
  `research/analysis/` instead of committing the raw log. Never a single-commit
  branch that never merges.
- Before handing back, walk the log against what actually happened: every row maps
  to a real action, every evidence pointer resolves. Fix the log, not the story.

## Spend context deliberately

Do not read what the current step will not use. When a context pack or bundle sheds
most of its input to fit a budget, treat the shed volume as a scoping defect in the
request, not an acceptable cost.

Routing bulk output, long files, and large payloads to subagents is permitted **only
when delegation is already authorized** for the work, and only with non-overlapping
write scopes per delegate. Keep summaries in the main thread rather than raw
payloads. Delegation is not a way to obtain approval the work does not have, and two
delegates writing the same path is a design defect, not a merge problem.

## Provenance

Adapted for OMP and eval-lab from four MIT-licensed Pstack skills by Lauren Tan:
`principle-sequence-verifiable-units`, `principle-build-the-lever`,
`show-me-your-work`, and `principle-guard-the-context-window`
(`https://github.com/cursor/plugins/tree/main/pstack/skills`). The TSV helper
script, Cursor slash-command surface, cross-model review requirement, and
transcript-globbing steps are not imported.

Two deliberate divergences from the upstream skills, both forced by contracts this
repository already holds:

- Pstack's canonical delivery shape is a failing test committed before its fix.
  `agents/CHECKS.md` makes green a property of the exact head, so a deliberately red
  commit is prohibited here. This skill requires every commit green and demonstrates
  the prior failure through a fixture or a same-commit regression test instead.
- Pstack applies per-unit verification uniformly. Here it is scoped to a single
  serial writer, because the repository's parallel-worker policy has delegates skip
  validation and the integration owner validate at a barrier.
