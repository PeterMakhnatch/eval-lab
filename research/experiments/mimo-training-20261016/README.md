# MiMo experiment freeze — 2026-10-16

This metadata release fixes task membership and package custody. It does not certify task quality or authorize model scoring.

## Consumer entry points

- `heldout.json`: canonical bare task IDs in `heldout`; 33 tasks, disjoint from all 42 historical training exposures.
- `split.json`: immutable selection snapshot; 40 currently eligible training IDs, 42 historical exposures, original 30 held-out IDs plus Research's 000657/000371/001686 additions.
- `manifest.json`: `mimo.training-manifest/v1`; original/v6/test-repair bindings, source provenance, exclusions and grading targets.
- `package-pairs.csv`: 428 Python recovered-reference rows; 425 original v5 counterparts and 428 v6 counterparts materialized. Original pins for 000471, 001804 and 002205 are missing, not substituted; none is held out.
- `repair-inventory.json`: distinct repair lineage and control limitations; hardening alone is not a test repair.
- `validity-release.json`: Research's gate authorization and the written QA Trace/Trainer acknowledgements.
- `quota-pause.json`: observed Codex weekly utilization of 61%, triggering the prescribed 60% pause.
- `cloud-control-table.csv`, `cloud-controls.json`, `native-controls.json`: current control evidence supplement. Infra's native Modal controls qualify all 33 held-out packages and training task 001647 on their exact v6 digests. The original freeze manifest's unproven control fields remain historical; these later receipts do not rewrite identity or grading-package bindings.
- `consumer-check.json`: the actual QA Trace exclusion parser accepts all 33 IDs with zero historical-training overlap.
- `evidence-relocations.json`: durable locations of the archived isolated PostgreSQL smoke and full static intake evidence.

The live custody path is `/Users/petermakhnatch/Developer/eval-lab/.worktrees/mimo-shared-image-restoration/research/experiments/mimo-training-20261016/heldout.json`. That worktree is locked against retirement while consumers use these absolute bindings. Do not move it or silently replace the membership.

## Independent gates

`freeze_ready=true` and `validity_check_allowed=true` permit exclusion-aware data construction and RH's validity check. The exact JSON bool **`scoring_allowed=false`** remains closed until Research dispositions the validity findings. Neither acknowledgement nor static package hashing is a control result.

All 33 held-out grading targets currently bind plain v6@10, labelled **v6-hardened**. In particular, 001425's partial, unverified test repair is not selected. Known-defect candidates still require Research's repair-or-drop disposition before scoring.

000838 and 001373 are excluded from active training and held-out membership. Their historical training exposure is retained: this manifest does not rewrite already-trained arm A. The newly reported broken-reference task 000081 is outside all three sets. The coordinator's queried tasks 000089, 002160, 002216, 002709 and 002736 are also outside all three.

## Verification and reproduction

Run from an installed eval-lab checkout:

```sh
python research/experiments/mimo-training-20261016/freeze.py --check
```

`--release-validity <receipt.json>` validates the split, written-ACK metadata, current A/B/C exclusion coverage, unchanged per-held-out evidence, and every held-out original/v6 package digest. It publishes the package manifest first and atomically publishes the held-out readiness gate last. It never enables scoring.

After release, nonsemantic publication edits and A/B/C rebuilds within the frozen training-exposure set do not reshuffle membership. New training exposure, held-out evidence changes, frozen source drift and package-binding drift are refused. Historical source hashes remain observations of the original freeze, not assertions that mutable upstream publications never change.

The actual release and read-only check passed; all 33 original/v6 package pairs were hashed. A consumer smoke verified the boolean gate types, all-plain-v6 target selection and zero training overlap. The earlier focused suite passed 239 cases. No fresh paid cloud control was launched by Dataset; Research accepted Infra's controls as tonight's substitute.

The supplement preserves all 70 native observations: the initial 66 cells, the corrected 001948 pair, and the 001647 pair. Every selected oracle=1/nop=0 cell binds the exact frozen `source_package_digest` from native executor metadata. Initial 001948 setup failures remain unscored history, not silently erased failures. Infra corrected Modal's interpreter PATH without changing any package; no package rebuild occurred. Per-task billed cost remains unmeasured here, Dataset spend remains $0, and the RH wrong-acceptance validity gate remains distinct from these successful controls. Scoring is still disabled.

## Reference custody outside the packaged index

The recovered 002486 implementation patch is at `/Users/petermakhnatch/Developer/eval-lab-results/2026-10-15/envcheck-repair/milestone-3/runs/envcheck-repair-1015/native-002486/reference.patch`, SHA256 `71de99ac34848cb170be5350f158323be4bee32863b1a39fe323e5f5895d0e85`. The sibling `002486-reference/provenance.json` records its source and `install_plan.py`-only selection. This supplies custody, not fresh exact-v6 qualification.
