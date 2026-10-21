# mimo-defect-map-v1 — historical source map

**Matcher readiness: unavailable.** Official hackbook match v0 code/catalog is absent from the frozen base 4b6f766674c18ab91486d95a270f419df5a4ac10. No substitute matcher or invented CLI/output schema is used.

This is a metadata-only retrospective source map. It is not a completed matcher evaluation when the official consumer is unavailable.

## Inventory and custody

- Frozen roster: **567 task IDs**, exact historical atlas packages; all33 heldout IDs remain training-excluded across every version.
- Package inventory: `{"verified_exact_package": 567}`.
- Mapped source findings: **315**, **138 named task IDs**; findings/programs/executions are not collapsed to tasks.
- Real-matcher measured task packages: **0 / 567**. Every other package has an explicit disposition in `predictions.json`.
- `sources.json` freezes repository/revision/path/byte digest/access; `manifest.json` binds generated artifacts. No raw hidden tests, solutions or trajectories are exported.

## Required source cohorts

| Category | Findings | Coverage disposition |
| --- | ---: | --- |
| `atlas114` | 242 | mapped_source_supported_historical_witnesses |
| `initial_v7` | 36 | pinned_initial_source_cases_and_control_scopes |
| `round2` | 10 | retained_complete_round |
| `round3` | 3 | retained_complete_round |
| `round4` | 0 | retained_complete_round_with_zero_programs |
| `round5` | 7 | retained_complete_round |
| `round6` | 8 | retained_complete_round |
| `training_wrong_pass8` | 8 | all_eight_current_source_observations_identity_bound_originals |
| `vals_answer_leak` | 1 | aggregate_claim_unjoinable_to_fleet |

## Hardening rounds and independent review

Rounds 2-6 retain **25** source programs: **15** supported, **10** refuted/unverified/withdrawn; **15** repair rows, **14** source-qualified, **1** not. Complete rounds with zero programs (round4) are real zero denominators, not missing rounds.

| Round | Programs | Supported | Repairs (qualified) | Independent review |
| --- | ---: | ---: | --- | --- |
| `round2` | 10 | 7 | 7 (7) | PASS |
| `round3` | 3 | 1 | 1 (1) | PASS |
| `round4` | 0 | 0 | 0 (0) | PASS |
| `round5` | 6 | 3 | 3 (3) | UNVERIFIED |
| `round6` | 6 | 4 | 4 (3) | NOT_CHECKED |

Independent review verdicts come from the pinned morning review's cited ranges only; an absent or unverified verdict is not a pass.

## Initial v7 release provenance, runtime repair and training exclusions

- The independent proof (`PASS_EXACT_CURRENT_HELDOUT_SCIENTIFIC_RELEASE`) hashes the **candidate** bytes (`7873515` B). The owner's gate-only adoption changed root keys `freeze_ready, independent_verifier, pending_gates, scoring_allowed, status` and produced the released bytes (`7873985` B). The independent verifier did **not** rehash the final released bytes, and nested per-task pending statuses in them are historical.
- `format-code-task-001246` runtime/collection repair: selected package controls reference x3 `[1.0, 1.0, 1.0]` and nop x3 `[0.0, 0.0, 0.0]` are native-custody controls, not semantic certification; roles staged on a different package: `prompt-valid-alternate`. Parent-package grades are not transferred.
- Training wrong-pass cohort: 8 programs / 8 original executions / 6 task IDs. Disputed (excluded from supported recall): `c5625dc0-ffd0-4204-88f0-2e79f587d528`; supported rows after exclusions: 7; independent review row status `{"independently_reproduced_fail_on_repaired_grader": 2, "located_in_repair_source_not_independently_replayed": 3, "not_independently_located": 3}`.
- `format-code-task-000657` parent and child reference controls both fail the same original-module timing assertion in one of three runs; the child remains source-unqualified and the failures are not closed here.


## Retrospective recall

Source-supported known findings: **301**; evaluable **0**; unevaluable **301**; matched **0**.
Exact package/class labels: known **151**, evaluable **0**, matched **0**.
Task-package denominator: known **150**, evaluable **0**; any-known-class matches **0**.
Finding/class/task recall: `None` / `None` / `None`. Null means no evaluable denominator, **not zero measured recall**.
Excluded findings: `{"historical_or_other_package_not_scanned": 51, "matcher_unavailable": 242, "unjoinable_missing_package_identity": 8}`.
Per-category denominators (including real zero-denominator rounds) and every unmatched/error/other-package/unjoinable finding are retained in `manifest.json` / `dispositions.json`.

| Category | Known supported findings | Evaluable | State |
| --- | ---: | ---: | --- |
| `atlas114` | 242 | 0 | none_evaluable; coverage `mapped_source_supported_historical_witnesses` |
| `initial_v7` | 34 | 0 | none_evaluable; coverage `pinned_initial_source_cases_and_control_scopes` |
| `round2` | 7 | 0 | none_evaluable; coverage `retained_complete_round` |
| `round3` | 1 | 0 | none_evaluable; coverage `retained_complete_round` |
| `round4` | 0 | 0 | no_known_findings; coverage `retained_complete_round_with_zero_programs` |
| `round5` | 4 | 0 | none_evaluable; coverage `retained_complete_round` |
| `round6` | 6 | 0 | none_evaluable; coverage `retained_complete_round` |
| `training_wrong_pass8` | 7 | 0 | none_evaluable; coverage `all_eight_current_source_observations_identity_bound_originals` |
| `vals_answer_leak` | 0 | 0 | no_known_findings; coverage `aggregate_claim_unjoinable_to_fleet` |

| Primary class (inferred mapping) | All mapped | Supported | Evaluable | Matched | Finding recall |
| --- | ---: | ---: | ---: | ---: | --- |
| `answer_reachable` | 1 | 0 | 0 | 0 | null |
| `env_mismatch_or_flaky` | 3 | 2 | 0 | 0 | null |
| `unstated_requirement` | 1 | 1 | 0 | 0 | null |
| `weak_check` | 297 | 296 | 0 | 0 | null |
| `wrong_answer_key` | 2 | 2 | 0 | 0 | null |

Visible source exclusions (neither numerator nor denominator): `{"source_claim": 3, "source_optional_diagnostic": 1, "source_refuted_or_unverified": 10}`.
Claims, mapped-inference labels and differential confirmation are separate fields. No precision or specificity is estimated: the certified-negative denominator is zero.

## New predictions and bounded pool

Unconfirmed new task/class predictions: **0**, listed separately in `new-predictions.json`; absent known labels do not make predictions false positives or confirmed defects.
Frozen-atlas567 exact-package screen pool: verified materialized bytes, successful complete real-matcher scan, no predicted class, no unresolved source-supported finding on those exact bytes.

- Scan state: **unmeasured** (0 of 567 selections measured); a null count means the screen was never run, not an empty clean pool.
- Bounded screen pool: **not measured**.
- Control-qualified subset: **not measured**.
- Training-exclusion-filtered subset: **not measured**; newly training-authorized **0**.
- Certified semantic-clean items: **0**. Static silence never proves semantic cleanliness.

The pool concerns the frozen historical atlas selections, not the current repaired package fleet or a training admission. Selected-package controls and repaired lineage describe other exact packages; they would need their own exactly bound cohorts, and any supplemental scan of modern packages must be reported separately without changing this frozen roster. Historical labels do not transfer to repaired descendants; descendants and author/independent qualification remain separate in the repair history.

## Limits

Vals' 1,795/2,698 aggregate source claim concerns a different larger population and is not projected onto all567. Unjoinable task/package evidence remains explicit.
This producer makes no native executions, model calls, container launches, registry mutations, paid requests or new training/scoring/serving grants.
