# MiMo defect map v1

Metadata-only historical defect map and exact-package fleet inventory. This experiment does not execute tasks, launch containers, call models, mutate a registry, or grant training/scoring permission.

**Release state: source map complete, matcher evaluation blocked.** The official `hackbook match v0` implementation and catalog were absent from the frozen base (`4b6f766674c18ab91486d95a270f419df5a4ac10`) and from `origin/main` when this producer last ran. No substitute matcher, guessed CLI or output schema is used, so every roster selection carries an explicit `matcher_unavailable` disposition, recall is `null`, and the bounded-pool counts are `null` (never run), not zero.

## Reproduce

From the repository root, using its installed benchmark runtime. `--evidence-root` is the authorized private evidence repository and `--results-root` the retained results store (original training executions):

```bash
.venv/bin/python research/experiments/mimo-defect-map-v1/build_map.py inventory \
  --evidence-root /path/to/research-context --results-root /path/to/eval-lab-results
.venv/bin/python research/experiments/mimo-defect-map-v1/build_map.py build \
  --evidence-root /path/to/research-context --results-root /path/to/eval-lab-results
.venv/bin/python research/experiments/mimo-defect-map-v1/build_map.py report
```

`inventory` writes only `inventory.json`; `build` rewrites every artifact (including `inventory.json`, `manifest.json` and `report.md`); `report` re-renders `report.md` from `manifest.json`. `--package-root /path/to/materialized/packages` optionally changes only the package locator; expected identities remain frozen. `--output-dir /tmp/mimo-defect-map` keeps a local reproduction outside the checkout. Source bytes are validated against `sources.json`; a missing source or custody mismatch fails the build rather than becoming an empty evidence cohort. Missing or changed packages are explicit inventory dispositions.

The 567 selections are **the exact historical packages in the v2.1 atlas**, not an unversioned task-name list and not an implicit latest repaired fleet. The producer reuses `evallab.registry` package, Harbor and verifier digests over actual read-only materializations. It reads committed source bytes with `git show <full-revision>:<path>`, validates lengths/SHA-256, and separately validates retained archive members. Authorized-local retained files (`revision: null`) are validated by exact bytes and SHA-256 and are never presented as Git-immutable. `sources.json` is the frozen input contract; `manifest.json` binds generated outputs and the producer bytes.

## Outputs and scientific boundaries

- `inventory.json`: all 567 roster rows, expected and observed identities, resolved locators, materialization/error states, and all 33 heldout exclusions.
- `findings.json`: historical finding/program/execution identities, class-mapping inference, source adjudication, direction, uncertainty and citations. Refuted or unverified proposed wrong programs, optional diagnostics and disputed or aggregate claims remain visible and are not supported defect labels.
- `repairs.json`: parent/child version boundaries and historical qualification/control scopes. A parent's label is never silently transferred to a repaired child. The 001246 runtime repair separates the selected package, the packages actually staged for each control, and the parent's retained grades.
- `predictions.json`: one real prediction or explicit unavailable/error disposition per exact fleet selection. Known labels are never matcher inputs.
- `dispositions.json`: every historical finding's exact-package comparison disposition, including other-package, unjoinable, unknown-class and matcher failures.
- `new-predictions.json`: separately listed unconfirmed task/class review candidates, never automatically new defects or false positives.
- `manifest.json` and `report.md`: cohort counts; finding, exact-package/class and task-package recall denominators; per-category denominators (including the real zero-program round 4); visible exclusions by evidence status; hardening-round totals; initial release provenance; pool definitions and limits.

A source's claim, a source-supported archival observation, an inferred taxonomy mapping, and independently custody-bound differential confirmation are different fields. A source's `confirmed` label is not upgraded into fresh confirmation. A reported reward flip is not enough: confirmation requires original reward 1 / fixed reward 0 for the same identified program, exact packages and bound controls. Native UUIDs absent from an archive stay null; the retained report/grade line or trial locator remains the execution identity rather than inventing one.

### Independent review and release provenance

- Rounds 2–4 carry the morning review's independent PASS (`morning_review`, cited lines 12–40 and 98–121). Round 5 was in progress and **UNVERIFIED** at 07:29 ET; round 6 is **not checked**. Author-side qualification is not independent acceptance.
- The independent V7-053 proof hashes the **candidate** heldout bytes; the owner's gate-only adoption changed five root keys and produced the released bytes. The independent verifier did not rehash the final released bytes, and nested per-task pending statuses there are historical.
- Round 6 `000657`: parent and child reference controls both fail one of three runs on the same original-module elapsed-time assertion; they are separate TS09 findings, the child stays source-unqualified, and the failures are not closed as semantic qualification.
- Training wrong-pass cohort (8 programs / 8 original executions / 6 task IDs): the `c5625dc0…` row is a disputed source claim excluded from supported recall; the `a6bee1be…` original and its stripped replay are different artifacts; the recovered `002486` run6 receipt does not confirm `772a78ad…`.

Retrospective recall uses only supported, exactly joined, successfully measured labels. Excluded and unknown rows do not become negatives. Multiple findings or executions on one task are not collapsed into a fabricated task count. Precision/specificity remain null because this evidence has no certified-negative cohort. Vals' 1,795/2,698 aggregate concerns a different population and cannot be projected onto these 567 packages.

## Matcher dependency and bounded pool

The interim taxonomy is used **only for known-class mapping**, never as a substitute matching implementation. Missing from the repository: the merged hackbook match v0 code, its catalog, and a published crosswalk from its class IDs to the taxonomy keys used in `findings.json`. Integration must consume that real merged interface, record the raw output, command, matcher/catalog revision and digests and options, and exact package identities, run all 567 without supplying known labels and without tuning on this evaluation, and map predicted class IDs through the published crosswalk. A successful source-map command is not a completed v0 release.

The **bounded screen pool** is limited to frozen historical selections with verified bytes, a complete successful real-matcher scan, no predicted class and no unresolved supported finding on those exact bytes. Its **control-qualified subset** additionally requires source-bound scored reference-all1/nop-all0 controls on that same package; that is not semantic cleanliness or independent release acceptance. Its **training-exclusion-filtered subset** removes every one of the frozen 33 heldout task IDs across **all versions**, and still grants no training permission or answer-exposure clearance. The scan cohort is the historical atlas roster, not the current repaired package fleet; current selected-package controls or lineage would need their own exactly bound cohorts, and supplemental scans of modern packages must be reported separately without changing the frozen 567 roster. With no real scan the counts are `null`, and certified semantic-clean and newly training-authorized counts stay zero.

Only allowlisted metadata is exported. No private raw tests, solutions, patches, prompts or trajectories are copied here; the timing logs contribute only a failed pytest node ID. Source paths and hashes are custody references; obtaining private bytes requires independent authorization. The experiment reuses the atlas's existing source license/NOTICE metadata through its frozen source pointers and creates no extra distribution permission.

Focused regression boundaries live in `tests/test_mimo_defect_map.py`. Run them only as part of the parent-owned integrated gate after edit handoff; workers do not run tests, builds, linters or formatters mid-flight.
