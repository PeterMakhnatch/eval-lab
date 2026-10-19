# multi-source-data-merger: conflict report is mostly unchecked

Task: terminal-bench-2-1 `tasks/multi-source-data-merger` (package digest
`sha256:bf447b1768ecf5e0ee55c29f135cdb85f61b92bf8d22705e1fa8ffdfde700bb0`).
Patch: `../patches/multi-source-data-merger.patch` (append-only; no original
assertion changed).

## Gap

The instruction fixes the format of `/app/conflicts.json`: for each conflict it
gives `user_id`, `field` (a unified name), per-source `values`, and `selected`
(the value from the highest-priority source). `test_conflict_report_values` only
checks the email conflict of user 101, and it skips even that check when no
record has `field == "email"`. Five one-line edits of the reference
`conflicts.json` passed the original tests:

| mutant | edit |
|---|---|
| 1f604f9086 | name conflict: `source_a` value becomes `""` |
| 812424b32a | name conflict reported for user 102 instead of 101 |
| 1e9ed0f32a | created_date conflict loses its `selected` key |
| 8c6d91f8c1 | email conflict's `field` becomes `""` (the email check is then skipped) |
| 46fb731802 | created_date conflict reported for user 102 instead of 101 |

## Repair

`test_conflict_report_records_match_sources` was added. Only user 101 appears in
more than one source, so the test checks:

- every conflict belongs to user 101;
- every `field` is a unified name, with `status` allowed because it is optional;
- every conflict has `selected`, and it equals the highest-priority value present;
- user 101's name, email and created_date conflicts are each present once, with
  the per-source values from the three input files.

## Qualification (native Harbor, the task's own limits)

- solve.sh oracle: reward 1. Full reference reconstruction on a fresh base: reward 1. nop: reward 0.
- The 5 recorded survivors now get reward 0.
- In the oracle trial's verifier output, all original tests and the new test passed. Locally, the original test functions also pass on the oracle `conflicts.json`.
- Round 2: the same seeded search ran the same 8 of 43 generated line mutants, and all 8 were killed (0 survivors).
