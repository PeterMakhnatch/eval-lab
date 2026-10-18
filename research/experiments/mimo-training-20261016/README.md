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

Fresh controls for an unchanged v6 selection are explicit, optional inputs:
add `--native-control-metadata /absolute/job/lab-metadata.json` once for each
reference and nop job. Each supplied task must have one distinct, model-free
oracle execution and one nop execution, with actual repeat3 case evidence
and rewards `[1,1,1]` / `[0,0,0]`. Partial pairs, duplicate roles or UUIDs,
uncompleted or nonzero-exit oracle submissions, conflicting primary rewards,
and observations for another package or a repaired selection are rejected.
The native job/config/lock, agent, verifier and stability artifacts are
hash-bound; three stability rows without case execution do not qualify.
These are three grader invocations on each final state, not three independent
agent trials. `reported_baseline_controls` records the new native environment
type/import path separately from the retained historical controls. The frozen
v6 files, packages and x1 evidence are not rewritten; every semantic and independent gate
below remains closed.
Selected metrics identify that measurement source instead of reusing the
historical observation indices. The enclosing `backend` continues to describe
the frozen source, not the fresh native environment.

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

### Source-bound 001246 collection repair

`format-code-task-001246` has a separate, bounded runtime-repair producer:
`python -m evallab.mimo_runtime_repair`. It is **not** an EnvCheck semantic
repair, and does not relax the append-only `verifier-repair@1` selector.
The optional `--runtime-repair-handoff /absolute/runtime-plan.json` argument
is forwarded by assembly and exact-source validation. Without it, selection
is unchanged. Supplying both selectors for 001246 is rejected.

The retained nop UUID `bf0d13f9-ff9a-446f-87a1-7cd1231edd6e` failed before
collection: `import burnman` eagerly imported `elasticsolutionmodel`, which
imports the absent `_ideal_activities_fct`. Setup already reported that
failure before neutralizing **zero** installed copies. The retained
`solution.py` and `solutionmodel.py` Git blobs are respectively
`b370f4399b309d14f385aac3b21bff7c78f4debf` and
`e08ec127c682616c721195ec9490e96c974d2044`: the exact recovered reference
patch preimages. The reference patch introduces the helper alongside the
requested public activity implementations. Installing the helper in nop
setup would therefore install part of the answer, not repair an unrelated
environment defect.

`mimo-001246-collection@1` changes only `tests/test.patch` on the fixed
`c4d94db236a5` parent. It moves the thirteen BurnMan imports into a loader
called at the beginning of every original test, and realizes the four
existing parameter model factories there, before the temperature exception
assertion. Imports are never caught. All eleven test functions, fourteen
cases, model constructors, numeric inputs, assertions and exception checks
remain; an inverse AST check must reproduce the entire original test AST.
The command diff and every other package member remain byte-identical.
A missing helper must now fail a named test body, not be counted as a
successful zero-case rejection. Native controls must still establish this;
derivation alone makes no reference/nop outcome claim.

Use this worktree's already-installed interpreter and editable source, with
explicit artifact destinations (no native execution occurs):

```sh
.venv/bin/python -B -m evallab.mimo_runtime_repair \
  --parent "$FROZEN_001246_PARENT" \
  --inventory "$BRIDGE/input-inventory-v7-v3.json" \
  --causal-evidence /absolute/retained-causal-evidence.json \
  --repo-root "$PWD" \
  --output-dir "$PWD/derived/001246-control-plan" \
  --variants-root "$PWD/derived/001246-variants" \
  --handoff /absolute/new-runtime-plan.json \
  --created-by "$ACTOR"
```

The producer never overwrites a plan, handoff or canonical record. It uses
`derive_task` for a new variant and `materialize` to rebuild an existing exact
record in a fresh checkout. Reuse requires the same full parent, transform,
inputs and only permitted patch bytes; conflicting records are rejected.
The full canonical parent lineage and original frozen patch hash stay pinned.
Its closed
`mimo-v7-runtime-repair/v1` handoff binds the parent/selected package,
Harbor and verifier identities, native record, snapshot/reference/log
evidence, complete effective inventory and its actual authoritative sources,
every wrong/correct patch and generated script, and a native control matrix.
The manifest independently rebuilds the only permitted patch and replays
the record. Added inventory entries, changed bytes, altered adjudications,
unbound scripts, local-parent substitutions and unrelated deltas are rejected.
Zero wrong rows is permitted only because the complete bound inventory says
so, never as a hardcoded qualification count.

The initial handoff has `qualification.status="pending"` and is a
`development_candidate`: `qualified_for_v7`, `scoring_allowed` and
`training_allowed` must be literal `false`. Parent controls stay historical,
never transfer to the child, and selected measurement arrays remain empty.
The parent executes the planned reference and nop with verifier repeat3,
each wrong control once, and each adjudicated-correct alternative with
repeat3. Every run is a distinct, completed, model-free physical execution.
The current effective inventory's sole correct alternative is the exact
`ref-mutants-323865e88f` patch (SHA-256
`eb228e2508808878720ff654fe8822d5b53a32d8a54f827750b135edc8ca6b6d`);
the authoritative correction says `.prod(-1)` and `.prod(1)` are equivalent
on the actual two-dimensional occupancy array. It must pass, not be killed.

Completed measurements are optional input, not producer assertions. Publish
a separately bound `mimo-v7-runtime-controls/v1` receipt (validated by
`evallab.training_freeze.RuntimeQualificationReceipt`) and replace the
qualification field in a **new** handoff with
`{"status":"recorded","receipt":{"path":"/absolute/controls.json",
"sha256":"sha256:<64 hex>","bytes":1234}}`. The receipt binds `task_id`,
`package_digest`, `harbor_digest`, `verifier_digest`, `matrix_sha256` and
`inventory_sha256`; its `checks` contain `run`, `role`, `status="ok"`,
`expected_reward`, `matrix_sha256`, absolute `job_dir`, `repeats`
(`null` for wrong, three expected rewards otherwise), plus native matrix
staging's `solution_sha256` (raw hex) and `staged_task_digest` for submission
overrides. Omit absent staging fields. No `qualified=true` flag is accepted.

The consumer reuses the existing native fact reader, repeat proof,
`staged_matrix_request` and `matrix_run_outcome` guards: reference must be
`[1,1,1]`, nop `[0,0,0]`, every actual wrong submission `0`, and every
correct alternative `[1,1,1]`, on the exact selected grader and staged
payload. Case execution is required even for zeros; aliased repeats, parent
package outcomes, errored or incomplete submissions and false negatives are
rejected. Successful intake records native custody, **not** v7 acceptance:
scoring and training stay closed and an independently source-bound Verifier
PASS remains mandatory.

Exclusion compatibility is not grading admission. Historical byte-pinned
partition inputs and frozen-v6 scoring analyzers remain historical consumers;
pointing them at a new filename does not authorize a changed grader.
