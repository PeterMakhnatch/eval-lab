# mimo-clean-v7 — dataset card

**A versioned metadata release, not a claim that the full MiMo fleet is clean.**
This catalog makes the qualified cohorts, exact task packages, historical
weaknesses and remaining prerequisites usable together without transferring
results or permissions between them. It contains no new benchmark measurements.

## Files and coverage

- [`manifest.json`](manifest.json): `mimo.clean.release/v1`; exact package rows,
  ordered cohort membership, compact controls, lineage, review scope and sources.
- [`provenance.json`](provenance.json): the `04-curated` provenance sidecar for
  those manifest bytes, validated by Eval Lab's `ProvenanceMetadata` contract.
- The underlying task packages, images, hidden tests, solutions, raw trajectories
  and private proof capsules are **not redistributed by this release**.

| Cohort | Selected packages | What is established | What is not established |
| --- | ---: | --- | --- |
| `heldout_v7` | 33 | Same ordered held-out IDs; 12 semantic repairs, one runtime repair, 20 unchanged v6 packages; exact-candidate independent release and gate-only adoption | Universal semantic cleanliness, training permission or new execution budget |
| `train_batch2_v7` | 20 | Strict assertion-scoped native reference `[1,1,1]` / nop `[0,0,0]` pairs | Blanket semantic, training, serving or dispatch admission |
| `train40_frozen` | 40 | Requested snapshot: **34/40 case-bound expected pairs**, preserving all failures and unchanged membership | Uniform current-reader strictness, a full-40 release or semantic/honesty certification |
| `train_repairs_v7` | 9 | Historical source-qualified repairs and their exact controls; six retain unchanged required-case sets | Current qualification of 21 added v2.1 cases on the other three repairs |
| `weak_test_atlas_v2_1` | 114 | Exact-package historical weak positives, supported by 242 whole-patch witnesses | Clean negatives or a prevalence/recall estimate |

The first four cohorts contain **102 different task/package selections across
91 task IDs**. Seven IDs occur in both batch2 and train40, but **none of those
selected packages matches**. The nine later train repairs also match neither
training cohort's selected packages. Counts must therefore distinguish IDs,
packages, trials and grader invocations. The atlas's 114 diagnostic package rows
are separate from those 102 selections; they are not additional clean tasks.

The catalog covers these cohorts, not all 567 reference-bearing tasks or the
larger 2,698-task source inventory. The atlas separately inventories 567 tasks:
114 supported positives and **453 not-established**, with **zero certified
negative items**. An absent item or `not_established` is not evidence of clean
behavior.

## Read the manifest without private runtime dependencies

From the repository root, this standard-library consumer selects the released
held-out targets by explicit cohort, never by a name-only join or a generic
"clean" filter:

```sh
python - <<'PY'
import json
from pathlib import Path

root = Path("research/experiments/mimo-clean-v7")
m = json.loads((root / "manifest.json").read_text())
by_id = {row["record_id"]: row for row in m["tasks"]}
heldout = [by_id[key] for key in m["cohorts"]["heldout_v7"]["members"]]
assert len(heldout) == 33
assert all(row["usage"]["inherited_scoring_allowed"] for row in heldout)
assert all(row["heldout_excluded_from_training"] for row in heldout)
assert not m["usage"]["training_authorized_by_this_release"]
print(json.dumps({
    "dataset": m["dataset_id"],
    "released_heldout_targets": len(heldout),
    "train40_requested_snapshot_pairs": m["cohorts"]["train40_frozen"]["case_bound_pairs"],
    "training_authorized": m["usage"]["training_authorized_by_this_release"],
}, sort_keys=True))
PY
```

`tasks` is keyed by **canonical task ID plus full package digest**, expressed in
`record_id`. A task may legitimately have multiple rows with different packages,
verifiers and statuses. Cohort `members` retain their source order. Six-digit
source IDs are normalized to `format-code-task-NNNNNN` without parsing away
leading zeros. Each projected fact has a source ID and JSON pointer or item ID;
`source_artifacts` records immutable revisions, byte digests and access scope.
A missing UUID, verifier digest, cost or license remains null, not a fabricated
value. Atlas rows do not supply a verifier digest.

This consumer reads the public metadata only. Executing a target additionally
requires authorized access to its exact materialized package, applicable image
and native evidence. Locate packages by full digest using the existing
[task-variant custody contract](../../../docs/task-variants.md), verify package,
Harbor and verifier identities, and use the source's approved control/runtime
protocol. This catalog is not a package download service and does not silently
rebuild, register, install dependencies, relax assertions or launch work.

### Reproduce the metadata

[`build_manifest.py`](build_manifest.py) is a standard-library, allowlisted
projection from the immutable Git revisions recorded in the script and manifest.
With an authorized evidence checkout in `$RESEARCH_CONTEXT_ROOT`, run:

```sh
python research/experiments/mimo-clean-v7/build_manifest.py \
  --evidence-root "$RESEARCH_CONTEXT_ROOT"
```

Both checkouts must contain the pinned commits. The producer reads committed
blobs, not mutable working-tree evidence, and reproduces both JSON files byte for
byte. It makes no network, native execution or registry calls. Its recorded
creation time is fixed to the actual catalog-assembly start; rerunning it does
not represent a new measurement.

## Status and usage rules

| Per-package `status` | Meaning | Safe interpretation |
| --- | --- | --- |
| `repaired` | Selected bytes have a documented repair and scoped passing controls | Read `repair_scope` and current case coverage; repair is not universal certification |
| `unchanged-clean-controls` | Unchanged selected bytes have the stated expected, case-bound controls | The word `clean` describes those controls only, not semantic correctness or honesty |
| `known-weak` | The atlas has a supported historical positive on this exact package digest | Diagnostic/review target, not a clean training or benchmark target |
| `blocked` | The requested snapshot lacks a qualifying pair, or the current expanded required-case set is unmeasured | Preserve the measured failures/history; do not fill gaps with another package's grades |

Weakness review and admission are separate fields. A historical weak label on
another digest remains historical context; it is neither automatically inherited
by a repaired child nor erased by a passing reference/nop pair. Conversely,
zero exact atlas matches does not certify cleanliness.

1. **Keep all 33 held-out task IDs out of training under every package version.**
   The exclusion follows task identity, not only the selected evaluation digest.
   Retain all 42 historical training exposures separately from the canonical
   40-task training split; this release does not rewrite past exposure.
2. **Scientific scoring is inherited only for the exact released held-out
   selection.** V7-053 independently accepted candidate bytes, then explicitly
   authorized gate-only adoption. The final released file was not separately
   re-read by the Verifier. No task selection, control or evidence changed in
   that adoption. Source-local training declarations on the nine repair rows
   are recorded as historical authority, not enlarged into catalog-wide grants.
3. **No new training, serving, dispatch, paid execution or publication of raw
   evidence is authorized here.** Existing campaign admission, budget, exclusion,
   leakage and runtime checks still apply. Do not select examples from status
   alone or treat reference controls as independent model solves.
4. **Repeat3 is three verifier invocations on one final submission**, not three
   independent agent trials. Preserve solution-staged submission identities
   separately from selected canonical packages. A known-honest reference override
   is not the original packaged solve script. Nulls and failed auxiliary workflows
   remain visible in their source histories.
5. **Never weaken an existing assertion to clear a control.** Retain original
   commands and tests, typed wrong/correct dispositions and scope restrictions.
   Reference success does not establish that every incorrect patch is rejected
   or that every correct implementation is accepted.

## Snapshot, repair and weakness limitations

### Canonical train40: requested 34 versus later measured 37

The original baseline measured 33 case-bound pairs. The unchanged-package
001647 runtime repair in [PR #1066](https://github.com/PeterMakhnatch/eval-lab/pull/1066)
added one, yielding the requested **34-pair snapshot**. Its selected package and
tests did not change; the native interpreter/setup path did.

Six snapshot blockers remain explicit: `arvo_18737`, `arvo_42485576`,
`candidate-2684-security-appsec`, and format tasks `000495`, `001109`, `001832`.
The first two have benign no-submission negative measurements only; this release
includes no positive exploit recovery or execution instructions. The scanner's
reference failed on a missing runtime dependency, not a fabricated zero-case pass.

A later, separately bound supplement completed production-only references and
public-behavior checks for the three format tasks, bringing **measured** coverage
to 37/40. `supplements.train40_later_references` retains these new vectors, source
custody and the failed earlier auxiliary workflow. It does **not** silently
replace the requested 34-pair snapshot, add training permission, rerun old nops
or turn either snapshot into semantic admission. The three latest remaining
prerequisites are the two positive arms and the scanner dependency.

Historical train40 observations include separate case-execution proofs and older
reader limitations. They are not relabelled uniformly native-strict. Full
six-edge lineage reconstruction was not proved by that baseline; retain its
actual package/grader custody rather than importing another release's ancestry.

### Batch2 and later train repairs

Batch2's 20 packages have 84 retained physical executions and 250 grade
observations, including failures and one single-observation vector outside the
selected reference/nop pairs. Task 001269 remains synchronous-scope-only; its
wrong-lenient `[0]` is one observation, not `[0,0,0]`. Task 000792 uses a
known-honest solution-only reference override. Task 002486 has a newly derived
collection repair; its unadjudicated old HAR161 submission is neither an admitted
correct alternate nor a known-wrong control. Its repaired package must not
replace the different frozen train40 package by ID.

The nine later EnvCheck repairs retain 13 historically killed wrong cases and
one supplied correct alternate, with original assertions preserved. Under atlas
v2.1, **000232 needs one additional wrong case, 001809 three and 001985 seventeen**.
Those 21 added cases remain unmeasured on their selected children; the manifest
marks these three rows `blocked` for the expanded case set while preserving their
historical qualifications. The six other repairs retain unchanged recorded case
sets. None of the nine silently expands batch2 or train40.

### Atlas and labelled trajectories

Use the published [weak-test atlas v2.1](../../answer-keys/mimo-weak-tests-v2.1/README.md),
its [summary](../../answer-keys/mimo-weak-tests-v2.1/summary.json) and
[exact-digest items](../../answer-keys/mimo-weak-tests-v2.1/items.jsonl), published in
[PR #1063](https://github.com/PeterMakhnatch/eval-lab/pull/1063).
The atlas supports source-adjudicated, historical positives; it does not establish
fresh replay on repaired packages, exhaustive coverage, detector recall, fleet
prevalence or independent held-out discovery. Its dev/test bookkeeping is
source-visible. Uncertain and non-positive reviews are not clean negatives.

For labelled trajectories, use the existing public
[canonical comparison summary](../qa-trace/README.md#complete-canonical-mimo-comparison-7070)
and [qa6](../qa-trace/results/qa-trace-6-mimo-v7-canonical-complete.json) /
[qa7](../qa-trace/results/qa-trace-7-mimo-v7-canonical-complete.json) reports:
70 retained original passes, 64 accepted and six rejected by the v7 graders.
These are **grader-acceptance labels**, not independent semantic, cheating,
sourcing or intent labels. No raw ATIF, prompt, hidden test or submission payload
is exported here.

## Provenance, access, licensing and maintenance

The machine manifest pins committed Eval Lab sources and the exact private
`research-context` evidence bytes. Public metadata is usable without those
private files; independent inspection of the underlying native/adjudication
proof requires repository access. Absolute workstation paths, credentials,
unredacted transcripts and proof archives are excluded. Source projections are
allowlisted; existing upstream raw evidence is neither rewritten nor republished.

The Code source lineage is FineEnvs/MiMo-V2.6-RL-harbor-code at
`5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785`, derived from
XiaomiMiMo/MiMo-V2.6-RL-oss at `639865fd3374018d6cb29b9fb82dd531406fcf5f`.
Mixed-domain train40 records retain their own source pins; the Code pin must
not be assigned to a cyber or general-domain package by default.

The card and metadata follow this repository's Apache-2.0 license. The retained
[dataset license](../../answer-keys/mimo-weak-tests-v2.1/LICENSE),
[NOTICE](../../answer-keys/mimo-weak-tests-v2.1/NOTICE) and
[MiMo agent MIT notice](../../answer-keys/mimo-weak-tests-v2.1/LICENSE-mimoagent.md)
identify upstream attribution: Xiaomi dataset material and verl portions are
Apache-2.0; mimoagent portions are MIT. Individual benchmark projects, patch
context, images and provider outputs retain their own rights and conditions.
No complete project-license audit or blanket redistribution grant for private
trajectories is claimed. This metadata release does not relicense those assets.

Maintain this release as an immutable, source-pinned snapshot. A newly selected
package, changed grader, added exposure, new required case or authorization needs
an explicit successor/version and fresh applicable admission. An old result,
matching task name or non-empty reward vector is not a substitute.
