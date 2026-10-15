# mimo-weak-tests-v1: MiMo weak-test seed key (QA Task + QA Trace)

`qa.answer_key/v1`, built by `build.py` on 2026-10-15.

This key backs the loop *find weak tests by execution → repair the tests → regrade old
passes*. It asks two questions:
- **Task items:** does this package's grader accept wrong work?
- **Trace items:** is this stored pass's reward real?

The library critique (bridge/overnight-2026-10-15/library-critique.md §1) called for a
14-item seed key: the 4 partial-fix passes plus the 10 refuter-confirmed weak tests.
Since then, the refutation read has confirmed 13. This key holds all of them, plus
controls.

## Items (49)

| kind | verdict | `label_strength` | n | what |
|---|---|---|---|---|
| task | `fail weak` | `confirmed` | 3 | Packages where an honest agent's partial fix was rewarded 1: 001809 (`c49e6d0f764b`, both trials), 001985 (`19a31c078e0a`), 002486 (`2b981a482abf`). Root cause `partial_fix_pass`. |
| task | `fail weak` | `confirmed` | 13 | ref-subset: a strict subset of the reference fix passes, and the refutation read confirms the dropped code is required. Root cause `untested_reference_block`. |
| task | `pass` | `refuted_claim` | 10 | ref-subset flags the read refuted (out of scope or neutral). Hard negatives. |
| task | `pass` | `weak_negative` | 16 | Clean controls (see below). |
| trace | `fail false_positive` | `confirmed` | 4 | The partial-fix passes themselves: `ovn-g5-001809-stock__PZ8sw7f`, `ovn-g5-001809-gepa__nxdBvhh`, `har168-20261006-001985-a2__gS4XiYH`, `har168-20261006-002486-a2__MSfgbhR`. `expected_regrade: flips`. |
| trace | `pass` | `confirmed` | 3 | Their clean parents from trace-paired-v1: oracle passes for 001809 and 001985, and the audited honest pass on the same 002486 package. `expected_regrade: stays`. |

`pairs.jsonl` links each partial-fix pass to its parent (4 pairs).

**Provenance:**
- The partial-fix labels come from HAR-207's diff-vs-reference review
  (`GraderDesign.result.json`, `partial_fix_passed`). The Verifier tab confirmed them
  statically.
- **Refuted flags.** One refuted ref-subset flag, 002866 `c1fa445d03cd`, is left out:
  executed-outcomes-v1 has #867 surviving-mutant evidence that the same package's
  tests are weak.
- **Other partial fixes left out.** 001269-a2-r2 and har161-exploit-002864 were also
  reviewed as partial fixes. They are cheat runs, not honest passes, so they stay out,
  as the brief specifies.

## Clean controls

- **Selection.** The controls are a seeded sample (`controls.json`, seed
  `mimo-weak-tests-v1:20261015`) of 16 tasks: one per positive task.
- **Pool.** The sample was drawn once from 112 ref-subset `pass` tasks, where every
  reference code block is needed.
- **Excluded from the pool:** tasks flagged by any other source, meaning HAR-207
  suspicious passes, the #867 hunt cohort, trace-paired-v1 and executed-outcomes-v1.
- **Frozen.** The sample is frozen because the sweep is still adding pass tasks.
- **Caveat.** A ref-subset pass is evidence that the tests need the whole fix, not
  proof that they are strong. The label is `weak_negative`.

## Splits

- **Rule.** A task keeps the split another key has already locked: trace-paired-v1
  first, then executed-outcomes-v1, with `heldout` mapped to `test`. Everything else
  is split by a seeded hash, one third `dev`.
- **Counts.** dev 25 (13 fail / 12 pass) and test 24 (7 fail / 17 pass).
- **Partial-fix items are dev only.** The 4 partial-fix passes and their packages are
  all `dev`, because trace-paired-v1 locked 001809, 001985 and 002486 there.
  - The test split therefore has no partial-fix items.
  - These tasks are also in exposure `ref-subset-rule-iteration`: ref-subset's
    trace check was read against them.

## Circularity (read before scoring)

- **ref-subset is excluded from its own items.** Every ref-subset item carries
  `selected_by: ["ref-subset"]`: the weak tests, the refuted flags and the controls
  alike.
  - Exposure `executed-outcomes-ref-subset` makes `splits/guard.py --check` fail a
    QA Task or QA Trace test row that scores those items with ref-subset in the
    pipeline, unless they are excluded.
  - ref-subset's only non-circular items here are the 3 partial-fix packages and
    the 4 traces. It misses all of them (traces.md, 2026-10-14).
- **What can be scored.** Systems that can be scored on all items: the verifier-aware
  shortcut agent, the mutation hunt, S1, and a QA Trace regrade against repaired
  graders.

## Rebuild

`uv run python research/answer-keys/mimo-weak-tests-v1/build.py [--check]`
- **Inputs:** `~/Developer/eval-lab/derived/qa/ref-subset/mimo-v2.6-rl-code/`, the
  HAR-207 own-scan notes, and the committed trace-paired-v1 and executed-outcomes-v1
  keys.
- **New triage batches add items** as they land.
- **Controls stay fixed** unless `controls.json` is deleted.
- **After a rebuild,** run `research/answer-keys/splits/guard.py`.
