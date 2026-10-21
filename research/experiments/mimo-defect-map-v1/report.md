# mimo-defect-map-v1 — historical source map

**Matcher readiness: measured_hackbook_v0.** Real evallab hackbook match v0 run over all 567 frozen packages (private raw custody; only entry_id/class/side/score retained in the scan index). TS04/TS05 fire on every task, so they do not discriminate on this cohort; retrospective recall is source-overlapping (catalog seeded from atlas114), not heldout generalization.

This is a metadata-only retrospective source map. Matcher predictions are static surface screening, not semantic confirmation.

## Inventory and custody

- Frozen roster: **567 task IDs**, exact historical atlas packages; all33 heldout IDs remain training-excluded across every version.
- Package inventory: `{"verified_exact_package": 567}`.
- Mapped source findings: **315**, **138 named task IDs**; findings/programs/executions are not collapsed to tasks.
- Real-matcher measured task packages: **567 / 567**. Every other package has an explicit disposition in `predictions.json`.
- `sources.json` freezes repository/revision/path/byte digest/access; `manifest.json` binds generated artifacts. No raw hidden tests, solutions or trajectories are exported.

## Required source cohorts

| Category | Findings | Coverage disposition |
| --- | ---: | --- |
| `atlas114` | 242 | mapped_source_supported_historical_witnesses |
| `batch2_v7` | 0 | controls_only_reconciled_no_wrong_program_findings |
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

## v7 repair coverage

- Pinned `clean_v7` selects **39** repaired task/package rows (37 task IDs). Every one has a repair-history row keyed by exact task + child package: **19** reconciled to existing records and **20** added (`{"batch2_v7": 20}`); uncovered: **0**. Total repair rows **57** (`{"batch2_v7": 20, "initial_v7": 22, "round2": 7, "round3": 1, "round5": 3, "round6": 4}`).
- 18 earlier repair rows are not selected `clean_v7` packages (`{"initial_v7": 3, "round2": 7, "round3": 1, "round5": 3, "round6": 4}`: blocked initial children and later hardening-round children). They are retained, qualified or not, and are not counted among the 39 selected packages.
- Task IDs with more than one repaired package stay separate: `{"format-code-task-000089": 2, "format-code-task-000125": 2, "format-code-task-000371": 2, "format-code-task-000657": 2, "format-code-task-001248": 2, "format-code-task-001425": 4, "format-code-task-001809": 2, "format-code-task-001985": 2, "format-code-task-002451": 3}`.
- `batch2_v7`: **20/20** rows, scope _20 strict reference/nop pairs; no dispatch or semantic admission_. Controls only: reference-pass/nop-fail reported for **20**, but reference **and** nop were staged on the selected package for only **17** (reference staged elsewhere: `format-code-task-000792, format-code-task-001269, format-code-task-002486`). Observed wrong-program positive rows: **0**; findings exported: **0**. The inherited retained-case counts (total 39) are scope counts, not findings or recall inputs. No semantic or training admission.
- Repair classes are INFERENCE only where pinned metadata or a linked source finding supports one: selected packages `{"INFERENCE": 17, "unmapped_underdetermined": 22}`; all repair rows `{"INFERENCE": 21, "unmapped_underdetermined": 36}`. `unmapped_underdetermined` is retained, not guessed.
- Known-weak ancestry: 3 selected packages name a known-weak ancestor in a registered lineage edge; 21 share only a task ID with a known-weak package (not established, never transferred). No ancestor label transfers to a child or direct parent.
- Explicit corrective runtime lineage (not generic normalizations): `[{"child_package_digest": "sha256:228541ef14380263d4ba882a48d3ff2a7ecff540b3368806e36eb973743fc146", "task_id": "format-code-task-001948", "transforms": ["setup-python-path@1"]}, {"child_package_digest": "sha256:182be31d763ddd8607c64818e3660b0b6ca5e994079d4518e82ccde7bd4b1ddb", "task_id": "format-code-task-002451", "transforms": ["env-keep-build-outputs@1"]}, {"child_package_digest": "sha256:aa6cae30652ed7f270b3279a4ef1f6b9924efb799f8858d0344c827be9441d9d", "task_id": "format-code-task-001246", "transforms": ["mimo-001246-collection@1", "env-keep-files@1"]}, {"child_package_digest": "sha256:628a68c0f4cf0b6e1070d180332ce3adbbbc01986c7a1125a19079eb1aa146d9", "task_id": "format-code-task-002486", "transforms": ["mimo-002486-collection@1"]}]`.


## Retrospective recall

Source-supported known findings: **301**; evaluable **242**; unevaluable **59**; matched **119**.
Exact package/class labels: known **151**, evaluable **114**, matched **59**.
Task-package denominator: known **150**, evaluable **114**; any-known-class matches **59**.
Finding/class/task recall: `0.49173553719008267` / `0.5175438596491229` / `0.5175438596491229`. Null means no evaluable denominator, **not zero measured recall**.
Excluded findings: `{"historical_or_other_package_not_scanned": 51, "unjoinable_missing_package_identity": 8}`.
Per-category denominators (including real zero-denominator rounds) and every unmatched/error/other-package/unjoinable finding are retained in `manifest.json` / `dispositions.json`.

| Category | Known supported findings | Evaluable | State |
| --- | ---: | ---: | --- |
| `atlas114` | 242 | 242 | evaluable; coverage `mapped_source_supported_historical_witnesses` |
| `batch2_v7` | 0 | 0 | no_known_findings; coverage `controls_only_reconciled_no_wrong_program_findings` |
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
| `weak_check` | 297 | 296 | 240 | 119 | 0.49583333333333335 |
| `wrong_answer_key` | 2 | 2 | 2 | 0 | 0.0 |

Visible source exclusions (neither numerator nor denominator): `{"source_claim": 3, "source_optional_diagnostic": 1, "source_refuted_or_unverified": 10}`.
Claims, mapped-inference labels and differential confirmation are separate fields. No precision or specificity is estimated: the certified-negative denominator is zero.

## New predictions and bounded pool

Unconfirmed new task/class predictions: **1881**, listed separately in `new-predictions.json`; absent known labels do not make predictions false positives or confirmed defects.
Frozen-atlas567 exact-package screen pool: verified materialized bytes, successful complete real-matcher scan, no predicted class, no unresolved source-supported finding on those exact bytes.

- Scan state: **complete** (567 of 567 selections measured); a null count means the screen was never run, not an empty clean pool.
- Bounded screen pool: **0**.
- Control-qualified subset: **0**.
- Training-exclusion-filtered subset: **0**; newly training-authorized **0**.
- Certified semantic-clean items: **0**. Static silence never proves semantic cleanliness.

The pool concerns the frozen historical atlas selections, not the current repaired package fleet or a training admission. Selected-package controls and repaired lineage describe other exact packages; they would need their own exactly bound cohorts, and any supplemental scan of modern packages must be reported separately without changing this frozen roster. Historical labels do not transfer to repaired descendants; descendants and author/independent qualification remain separate in the repair history.

## Limits

Vals' 1,795/2,698 aggregate source claim concerns a different larger population and is not projected onto all567. Unjoinable task/package evidence remains explicit.
This producer makes no native executions, model calls, container launches, registry mutations, paid requests or new training/scoring/serving grants.
