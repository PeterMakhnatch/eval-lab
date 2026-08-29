---
name: delivery-sequencing
description: Shape multi-step work and its delivery so a reviewer can verify it cheaply: verify each unit before the next, size and order pull requests to prove themselves, keep a decision trail for unattended runs, and commit the script that does or proves bulk work. Use for sweeps, migrations, long or unattended runs, and any pull request that would land large or mixed.
---

# Delivery sequencing

`agents/CHECKS.md` defines when a pull request is *green*. This skill covers whether
it is *reviewable*. Both are required; green and unreviewable still blocks the merge
queue.

## Verify each unit before the next

In a sweep, migration, or run of similar edits, bracket every unit: known-good
state, one change, run the check, proceed. Never batch the edits and verify once at
the end — a break caught at the unit that caused it is cheap to localize, and a
break caught after a batch is already built on.

Rebase onto current `origin/main` before the first check so every result measures
against the real baseline, not a stale one.

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
subagents. When you do fan out, put the recipe, the verification contract, and the
do-not-touch fences in one artifact every delegate reads, outside their write scope.

## Size and order the pull request

Order commits so the sequence proves the work: the failing test before the fix, the
subtraction before the reshape, the baseline capture before the treatment, the
scaffold before the feature. Each commit stands alone and the stack reads as an
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
- Keep it uncommitted at `.audit/<slug>.tsv` by default. Commit it only when a
  reviewer needs the trail to trust the result, and prefer a durable report under
  `research/analysis/` over a single-commit branch that never merges.
- Before handing back, walk the log against what actually happened: every row maps
  to a real action, every evidence pointer resolves. Fix the log, not the story.

## Spend context deliberately

Route bulk output, long files, and large payloads to subagents and keep summaries
in the main thread. Do not read what the current step will not use. When a context
pack or bundle sheds most of its input to fit a budget, treat the shed volume as a
scoping defect in the request, not an acceptable cost.

## Provenance

Adapted for OMP and eval-lab from four MIT-licensed Pstack skills by Lauren Tan:
`principle-sequence-verifiable-units`, `principle-build-the-lever`,
`show-me-your-work`, and `principle-guard-the-context-window`
(`https://github.com/cursor/plugins/tree/main/pstack/skills`). The TSV helper
script, Cursor slash-command surface, cross-model review requirement, and
transcript-globbing steps are not imported.
