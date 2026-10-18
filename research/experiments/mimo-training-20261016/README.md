# MiMo experiment freeze — 2026-10-16

The freeze fixes task membership and package custody; it does not certify task quality. Scoring requires Research's separate, hash-bound authorization in `scoring-release.json`.

## Consumer entry points

- `heldout.json`: canonical bare task IDs in `heldout`; 33 tasks, disjoint from all 42 historical training exposures.
- `split.json`: immutable selection snapshot; 40 currently eligible training IDs, 42 historical exposures, original 30 held-out IDs plus Research's 000657/000371/001686 additions.
- `manifest.json`: `mimo.training-manifest/v1`; original/v6/test-repair bindings, source provenance, exclusions and grading targets.
- `package-pairs.csv`: 428 Python recovered-reference rows; 425 original v5 counterparts and 428 v6 counterparts materialized. Original pins for 000471, 001804 and 002205 are missing, not substituted; none is held out.
- `repair-inventory.json`: distinct repair lineage and control limitations; hardening alone is not a test repair.
- `validity-release.json`: Research's gate authorization and the written QA Trace/Trainer acknowledgements.
- `scoring-release.json`: actor, exact Research approval, and predecessor/released hashes for the two scoring gates.
- `quota-pause.json`: observed Codex weekly utilization of 61%, triggering the prescribed 60% pause.
- `cloud-control-table.csv`, `cloud-controls.json`, `native-controls.json`: current control evidence supplement. Infra's native Modal controls qualify all 33 held-out packages and training task 001647 on their exact v6 digests. The original freeze manifest's unproven control fields remain historical; these later receipts do not rewrite identity or grading-package bindings.
- `consumer-check.json`: the actual QA Trace exclusion parser accepts all 33 IDs with zero historical-training overlap.
- `evidence-relocations.json`: durable locations of the archived isolated PostgreSQL smoke and full static intake evidence.
- `lineage-registration.json`: exact-byte reconstruction and retained-control evidence for the 33 headline packages and three separately selected v6-ported pin diagnostics; canonical variant-record paths and hashes.

The live custody path is `/Users/petermakhnatch/Developer/eval-lab/.worktrees/mimo-shared-image-restoration/research/experiments/mimo-training-20261016/heldout.json`. That worktree is locked against retirement while consumers use these absolute bindings. Do not move it or silently replace the membership.

## Independent gates

`freeze_ready=true` and `validity_check_allowed=true` permit exclusion-aware data construction and RH's validity check. Research subsequently authorized the exact top-level JSON bool **`scoring_allowed=true`** in both manifests after the validity gate closed and the Verifier confirmed it. The separate scoring receipt binds that instruction and both before/after file hashes. Historical nested policy notes and original evidence bindings remain unchanged; they are not independent live scoring gates.

All 33 held-out grading targets retain plain v6@10, labelled **v6-hardened**. In particular, 001425's partial, unverified test repair is not selected. Research's validity dispositions govern interpretation; opening scoring does not replace a package, erase a validity flag, or certify all grader behavior.

000838 and 001373 are excluded from active training and held-out membership. Their historical training exposure is retained: this manifest does not rewrite already-trained arm A. The newly reported broken-reference task 000081 is outside all three sets. The coordinator's queried tasks 000089, 002160, 002216, 002709 and 002736 are also outside all three.

## Verification and reproduction

Run from an installed eval-lab checkout:

```sh
python research/experiments/mimo-training-20261016/freeze.py --check
```

`--release-validity <receipt.json>` validates the split, written-ACK metadata, current A/B/C exclusion coverage, unchanged per-held-out evidence, and every held-out original/v6 package digest. It publishes the package manifest first and atomically publishes the held-out readiness gate last. It never enables scoring.

The authorized scoring transition is:

```sh
python research/experiments/mimo-training-20261016/freeze.py \
  --release-scoring --actor research-harbor \
  --approval 'Research scoring disposition 2026-10-16: validity gate closed and Verifier-confirmed, flags ruled'
```

It requires both scoring gates to be closed and the validity release to be ready. It creates `scoring-release.json`, changes only the top-level scoring booleans and the manifest's dependent held-out file fingerprint, then atomically publishes the held-out gate last. `--check` validates the released state against the custody record; a bare flag flip, missing authorization, or changed package binding is refused. The command is not an authorization to run new jobs or spend.

After release, nonsemantic publication edits and A/B/C rebuilds within the frozen training-exposure set do not reshuffle membership. New training exposure, held-out evidence changes, frozen source drift and package-binding drift are refused. Historical source hashes remain observations of the original freeze, not assertions that mutable upstream publications never change.

The actual release and read-only check passed; all 33 original/v6 package pairs were hashed. A consumer smoke verified the boolean gate types, all-plain-v6 target selection and zero training overlap. The earlier focused suite passed 239 cases. No fresh paid cloud control was launched by Dataset; Research accepted Infra's controls as tonight's substitute.

The supplement preserves all 70 native observations: the initial 66 cells, the corrected 001948 pair, and the 001647 pair. Every selected oracle=1/nop=0 cell binds the exact frozen `source_package_digest` from native executor metadata. Initial 001948 setup failures remain unscored history, not silently erased failures. Infra corrected Modal's interpreter PATH without changing any package; no package rebuild occurred. Per-task billed cost remains unmeasured here, Dataset spend remains $0, and the RH wrong-acceptance validity gate remains distinct from these successful controls. The later scoring authorization does not rewrite this control evidence.

## Reference custody outside the packaged index

The recovered 002486 implementation patch is at `/Users/petermakhnatch/Developer/eval-lab-results/2026-10-15/envcheck-repair/milestone-3/runs/envcheck-repair-1015/native-002486/reference.patch`, SHA256 `71de99ac34848cb170be5350f158323be4bee32863b1a39fe323e5f5895d0e85`. The sibling `002486-reference/provenance.json` records its source and `install_plan.py`-only selection. This supplies custody, not fresh exact-v6 qualification.

## Frozen lineage registration — 2026-10-16

Research authorized reconstructing the missing lineage in isolated scratch storage,
without modifying a shared package, ledger root, frozen membership or scoring gate.
The existing producer reproduced **33/33 package digests and 33/33 Harbor digests**
from the pinned ledger parents, with **198 actual derivation edges**:

`strip-future-history@1 → purge-installed-copies@2 → purge-build-caches@4 → mtime-normalize@2 → separate-verifier@10 → agent-network-none@1`.

The 33 existing first-edge records remain unchanged; 165 missing headline records
and three diagnostic child records are registered. Fifteen ledger roots retain
their older, truthful ancestry: `leak-close-pypi@1` (11),
`env-keep-build-outputs@1` (3), and `env-keep-files@1` (1). These are not relabelled
or omitted to satisfy a transform policy.

Only the 36 endpoints receive `validated` status. Their append-only evidence binds
the reproduced package/Harbor identities to retained exact-digest controls:
headline oracle x1 = 1 / nop x1 = 0, and diagnostic oracle x3 = 1 / nop x3 = 0.
Intermediate records are not promoted. This records existing control evidence;
it is neither a new grading run nor a claim that the tests enforce every prompt
requirement. The frozen validity flags and caveats remain in force.

The diagnostic packages are `000371/cc1349da3c95`, `000657/da34eb301f58`, and
`001686/852e5afe1b8f`. Each `verifier-repair@1` edge was reproduced from its exact
frozen headline parent with a real variant-record link and unchanged endpoint
digests. Their `task.toml` files are byte-identical to those parents and inherit
`[agent] network_mode = "no-network"`. No fresh runtime network-lock probe was
performed here; runtime admission remains a separate gate. The raw repaired
variants are not the selected diagnostics, and these extra diagnostic rows do
not replace any headline grading package.

Full replay evidence is retained at
`/Users/petermakhnatch/Developer/eval-lab-results/2026-10-16/mimo-lineage-registration/headline-replay.json`
(SHA256 `16801d5ab23b1c53d85a29b153e035cd4028923e696b7fcdc6f87a75dd1e0d14`).
The executed procedures are archived in that directory's `procedures/` folder;
their hashes and the original execution paths are recorded in the registration
receipt. The canonical record files retain every parent package/Harbor digest
and every before/after file change. No new model, container or paid execution
was used for this registration.

## Held-out v7 successor

The evening 2026-10-17 campaign creates a **new** `heldout-v7.json`; it does
not rewrite this freeze, rerank membership, replace a historical v6 column,
or inherit the old scoring authorization. Its `mimo.heldout/v1` envelope
retains exactly the same 33 bare IDs in the same order, so the existing
held-out exclusion consumers continue to exclude the whole cohort.

From an installed repository root, assemble the successor explicitly:

```sh
BRIDGE="$HOME/Developer/research-context/bridge/2026-10-17-v7"
uv run --no-sync python research/experiments/mimo-training-20261016/freeze.py \
  --assemble-successor \
  --frozen-source-dir research/experiments/mimo-training-20261016 \
  --repairs-handoff "$BRIDGE/repairs-v7.json" \
  --setup-handoff "$BRIDGE/setup-001948-v7.json" \
  --successor-destination "$BRIDGE/heldout-v7.json" \
  --repo-root .
```

Each rich selected-grader row binds its exact frozen parent, selected package
and Harbor identities, canonical lineage records, and control evidence.
Unchanged selections keep their exact v6 package and historical controls;
an old x1 pair is not three observations. EnvCheck's actual hash-bound
`repairs-v7.json` supplies semantic repairs. The separately owned 001948
`setup-python-path@1` child changes setup and its embedded healthcheck only,
not tests or assertions, and must not be labelled a test repair.

Only complete EnvCheck rows are selectable. The assembler binds their
candidate, qualification, matrix, native-record and source-inventory hashes,
replays the variant independently, checks that the original test patch is
retained as a byte prefix, and requires the exact original-command wrapper:
either the current task-interpreter preference or its immutable historical
form without that preference. Both retain the complete original command
and propagate failures from both suites; skipping or masking either is
rejected. Partial rows leave the exact v6 parent selected; changed source
hashes are rejected rather than silently reinterpreted. This compatibility
check does not waive semantic acceptance or qualify a known false negative.

Native control labels and aggregate qualification flags are not execution
evidence. Each selected observation is re-read from its actual Harbor UUID,
config, lock, results and positive case-execution artifacts. Repeated controls
also require the three requested runs and distinct indices in `stability.json`.
Wrong and correct-alternative oracle scripts are re-staged through the native
matrix API, so their private solution-package digests are checked without
mistaking them for the unchanged source package or grader digest.

The assembler always emits `scoring_allowed=false`. Pending repair entries,
missing native repeats, wrong-reproducer replays, correct-alternative
controls, or unresolved adjudications stay explicit; publishing a candidate
is not acceptance. V7 acceptance requires reference `[1,1,1]`, do-nothing
`[0,0,0]`, rejection of every recorded adjudicated-wrong submission, no
rejected adjudicated-correct submission, and an independent Verifier PASS
bound to the final manifest and grader bytes.

Exclusion compatibility is not grading admission. Historical byte-pinned
partition inputs and frozen-v6 scoring analyzers remain historical consumers;
pointing them at a new filename does not authorize a changed grader.
