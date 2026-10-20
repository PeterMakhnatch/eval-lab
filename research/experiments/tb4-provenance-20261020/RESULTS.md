# pC-SELF-1: measured provenance qualification

## Stage and interpretation

These results describe the completed **11,550-trial frozen native board census**
and all **1,400 existing labelled primary cells**. All native details were
acquired, with no missing details or API refusals. The available locks produce
**198 distinct package keys over 66 tasks**, all digest-byte bound using
**132 distinct retained public exports**. Five failed trials have genuine null
locks and remain unmeasured. The separate 132-package source-family controls
compare unique exports; they are not a substitute for the actual native-lock join.

All outcome labels come unchanged from the
[existing run census](../tb4-run-census-20261020/README.md). This experiment joins
those labels to provenance evidence; it does not relabel trials, regrade them,
or establish runtime integrity. Source mismatch alone is not bad data.

The retained primary join contains **478 `task_defect`, 29 `task_defect_gray`,
and 39 `known_exploit` labels**. Under the narrower canonical-source, strict
instruction/tests-content and measured-reported-outcome qualification,
**355 `task_defect`, 28 `task_defect_gray`, and 18 `known_exploit` labels remain
among 856 cells**. The subset also contains **9 original `unmeasured` judgments**,
so 856 is not a wholly adjudicated rate denominator. These are filtered existing
labels, not newly validated behavioral findings. Exclusion does not reverse a label.

## Frozen comparison identities

- Upstream Git v4.0.0: `452bf305c6daa62fc59061d22133a7cbc7c1572e`, using
  `/Users/petermakhnatch/Developer/eval-lab-data/tb4-20261020/upstream-git-v4.0.0/terminal-bench`.
- Canonical public board: `terminal-bench/terminal-bench/4-0-0`.
- Canonical dataset: `1922072f-a433-429a-8929-350d5e1bcf02`, pinned at
  `sha256:39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732`.
- Original label inputs and experiment limits are frozen in [spec.json](spec.json).
  Canonical Hub identity and equality to upstream Git are separate claims.

All artifact paths below are repository-relative unless absolute. `R` denotes
`runs/pc-self1-provenance/` in this retained worktree.

## Primary native coverage and source binding

All 1,400 label cells match 1,400 distinct native trial IDs and have
`inventory_and_native_detail_linked` original-archive bindings. There are no
unmatched, ambiguous or conflicting label joins.

Of these archives, 800 have `result.json.id` equal to the Hub trial UUID and
600 have different IDs. All 600 retain explicit inventory/archive/native
linkage; UUID inequality is not treated as a failed identity check.

Complete detail acquisition is not the same as a measured outcome: all 1,400
native statuses are
`completed`, but only **1,374/1,400** have a measured reported outcome; 26 carry
native errors and remain outside that outcome-qualified denominator.
Three of the excluded cells still report reward `1`: two have
`AgentSafetyRefusalError` and one has `NonZeroAgentExitCodeError`. Surviving
reward does not override the recorded error. The exact IDs, error-type counts
and unchanged label/side counts are retained in
`R/packages/primary-stage-identity-outcome-analysis.json`; this is outcome
measurement precedence, not a new integrity verdict.

Each of the eight primary tasks contributes 175 cells. The 24 package keys
are distinct recorded `(source, digest, task_name)` combinations:

| Recorded native lock stratum | Package keys | Label cells | Observed export binding | Canonical dataset eligible |
|---|---:|---:|---|---|
| Exact canonical source and canonical digest | 8 | 1,160 | Pinned canonical dataset edge | Yes |
| Alternate source `ryanmarten/terminal-bench-4-no-prebuild` | 8 | 40 | Alternate public export dataset edge | No |
| Canonical source string, alternate digest | 8 | 200 | Exact alternate export reproduces the digest; declared-source membership unproven | No |
| **Total** | **24** | **1,400** | **24/24 native Packager digest bindings measured** | **8 keys / 1,160 cells** |

For the eight canonical-source/alternate-digest keys, the actual export source
is `ryanmarten/terminal-bench-4-no-prebuild`, `binding_status` is `bound`,
`declared_source_binding` is `unproven`, and `canonical_dataset_eligible` is
`false`. Reproducing the recorded digest binds the native Packager-selected
bytes, not membership in the source named by the lock. Neither cross-source
recovery nor equal instruction/tests content repairs that uncertainty or
changes exact-source downloader admissibility.

### Existing labels by native and strict byte stratum

The two strict byte strata contain 1,110 equal and 290 unequal cells. Here
“equal” means all compared instruction/tests paths and file content, not only
common selected verifier files; modes/link metadata are separate.

| Native lock / strict instruction-tests stratum | Cells | `task_defect` | `task_defect_gray` | `known_exploit` | `neither` | Original `unmeasured` |
|---|---:|---:|---:|---:|---:|---:|
| Canonical exact / equal | 870 | 355 | 28 | 18 | 459 | 10 |
| Canonical exact / unequal because of omitted test-support paths | 290 | 31 | 0 | 17 | 229 | 13 |
| Canonical source, alternate digest / equal, source unproven | 200 | 83 | 0 | 4 | 113 | 0 |
| Alternate source / equal | 40 | 9 | 1 | 0 | 28 | 2 |
| **All unchanged labels** | **1,400** | **478** | **29** | **39** | **829** | **25** |

The original side counts remain 556 pass, 833 fail and 11 unscored. Original
`unmeasured` labels describe the prior census's evidence limits; they are not
the same field as the 26 native outcomes excluded here.

### Qualification is a subset, not a new adjudication

`qualified_canonical_static` requires original-archive linkage, an exact
canonical native lock, strict exported instruction/tests content equality and
a measured reported outcome. `qualified_canonical_hash_covered` additionally
requires all compared instruction/tests content paths to be covered by the
recorded native digest. Both predicates select the same **856 cells** in this
primary-stage join: 870 canonical/equal cells minus 14 with unmeasured native
outcomes. The other 12 unmeasured native outcomes are in the 290 strict-unequal
canonical cells.

| Retained scope | Cells | `task_defect` | `task_defect_gray` | `known_exploit` | `neither` | Original `unmeasured` |
|---|---:|---:|---:|---:|---:|---:|
| All original labels | 1,400 | 478 | 29 | 39 | 829 | 25 |
| Measured reported native outcome, any source/byte stratum | 1,374 | 478 | 29 | 39 | 814 | 14 |
| Qualified canonical static **and** hash-covered subset | 856 | 355 | 28 | 18 | 446 | 9 |

The 856 qualified cells retain **287 original pass-side and 569 original
fail-side** classifications. Excluding nine original fail-side `unmeasured`
labels leaves 560 decidable fail labels. The measured-reported-outcome-only
subset has 553 original pass-side and 821 original fail-side cells, with 14
original fail-side `unmeasured` labels and hence 807 decidable fails.

| Selected scope of unchanged labels | `known_exploit` / original pass-side cells | `task_defect` / decidable original fail-side cells | Including `task_defect_gray` / same fail denominator |
|---|---:|---:|---:|
| Original published scope | 39/556 | 478/819 | 507/819 |
| Measured reported native outcome only | 39/553 | 478/807 | 507/807 |
| Qualified canonical static and hash-covered subset | 18/287 | 355/560 | 383/560 |

These are conditional frozen-label fractions, not new adjudication or
estimates of runtime bad-data rates. Original unscored cells and original
fail-side `unmeasured` labels are outside these fraction denominators, but
remain in the conservation tables where applicable. Qualification selects
against two tasks' canonical test-support omissions, noncanonical locks and
unmeasured native outcomes; the fractions do not support a causal rate
comparison.

Per-task conservation of the existing definite-defect labels is:

| Task | Original cells | Measured reported outcomes | Qualified canonical cells | Original `task_defect` | `task_defect` in qualified subset |
|---|---:|---:|---:|---:|---:|
| `atrx-vep-crispr` | 175 | 172 | 142 | 66 | 37 |
| `batched-eval-parity` | 175 | 173 | 143 | 53 | 35 |
| `biped-contact-dynamics` | 175 | 170 | 140 | 50 | 40 |
| `cumulative-layout-shift` | 175 | 169 | 0 | 0 | 0 |
| `data-anonymization` | 175 | 173 | 143 | 162 | 134 |
| `distributed-dedup` | 175 | 169 | 0 | 35 | 0 |
| `vf2-speedup-networkx` | 175 | 173 | 143 | 0 | 0 |
| `wal-recovery-ordering` | 175 | 175 | 145 | 112 | 109 |
| **Total** | **1,400** | **1,374** | **856** | **478** | **355** |

The two zero-qualified task rows result from the strict omitted-path rule for
canonical packages, not a finding that either task's prior labels were wrong.
These tables do not recompute a behavioral pass/fail rate, corrected
leaderboard or full-board bad-data estimate.

## Reconciliation of the 290 strict-unequal cells

`R/packages/verifier-difference-reconciliation.json` resolves all 290 cells to
two canonical packages. Both have `instruction_equal=true`,
`tests_equal=false`, and **zero common instruction/test content changes**:

| Canonical package | Native primary cells | Missing Git test-support paths |
|---|---:|---|
| `cumulative-layout-shift` | 145 | Two nested `SKILL.md` files and their six parent directories |
| `distributed-dedup` | 145 | Five hidden CRC files |
| **Total** | **290** | **Seven files; six directories** |

The two omitted files are
`tests/barber-shop-site/.agents/skills/agent-browser/SKILL.md` and
`tests/barber-shop-site/.claude/skills/agent-browser/SKILL.md`. The six omitted
directories are `.agents`, `.agents/skills`, `.agents/skills/agent-browser`,
`.claude`, `.claude/skills` and `.claude/skills/agent-browser`, all beneath
`tests/barber-shop-site/`.

The five omitted CRC files are under `tests/harness/data/docs_10k/`:
`._SUCCESS.crc` and
`.part-0000N-259edbd7-43de-4d94-90a0-8190c5552027-c000.snappy.parquet.crc`
for `N` = `0`, `1`, `2`, `3`. The reconciliation artifact retains every exact
path, file hash, frozen IN-1 record, package key and affected native trial ID.

These are package omissions, not formatting differences or edits to shared
verifier/instruction files. Frozen IN-1 already recorded the omitted file
paths and explicitly scoped its equality statement to **common files**:

> Every common file under tests/ or solution/ is byte-identical; this does not
> claim complete folder equality because some Git-only test support files are
> omitted by Hub.

The retained byte diffs remain at these paths under `R/packages/`:

- `comparisons/9c08201b678b59fc13180c6219566bfe352b2acd53342f97d653554380bb423e/985ef98c9d36a7e539437c6ecf8253ba41c3ab0d76887bbfe1d850d9cb3af522/bytes.diff`,
  SHA-256 `67d83a6e7e663f36c515de2c5a3e56864d246adacda79b434f09d5bf6a643a3c`.
- `comparisons/9b51fc55267f42de4fe2910dedbd5a5b7cd9a3b5aff999dd0037ce6670cb1717/0f7b19a9a1105f6af5115fa0634d8b3a4e6df55bd27e509101aca4bf240f9efe/bytes.diff`,
  SHA-256 `bc79c82b09d942f1d41fcff5673afe20f95f7e396fb13972393c0c3bf26c4dde`.

The reconciliation records matching current and frozen full file-content
inventories for **all 66 Git tasks**, with aggregate SHA-256
`f77f8dd3cfb6064f6711056361345e57fd3787cc417b4fb7e24605781212b28d`.
The 66 canonical export file inventories also match frozen IN-1: zero added,
missing or content-changed paths relative to that frozen export. Thus the
measured file-content difference is not drift since IN-1. IN-1's drift baseline
covers file/symlink content paths, not empty directories; current mode/link
metadata is recorded separately. No behavioral impact of the omissions was
executed, inferred or adjudicated, and the strict unequal flags remain false
for tests equality.

## Completed full-board denominator coverage

The frozen board contains **35 rows × 66 tasks × 5 attempts = 11,550 distinct
associated trials and 11,550 row–trial memberships**. All 35 job summaries and
all 11,550 detail responses are retained. The **422 ambient job entries not
associated with the frozen board** are excluded rather than counted as extra
attempts. There are no missing details or API refusals.

Native status is completed for 11,545 trials and failed for 5. Those five
`medical-claims-processing` failures have original `lock:null`; no package
identity or canonical eligibility was inferred for them. The local null-shape
handling fix rerendered cached evidence without retrying public reads.

| Recorded lock stratum | Trials | Strict instruction/tests equal | Strict unequal | Static unmeasured | Reported outcome unmeasured |
|---|---:|---:|---:|---:|---:|
| Canonical exact lock | 9,565 | 9,275 | 290 | 0 | 278 |
| Canonical source, alternate digest; source membership unproven | 1,650 | 1,650 | 0 | 0 | 2 |
| Different source | 330 | 330 | 0 | 0 | 10 |
| Null lock | 5 | 0 | 0 | 5 | 5 |
| **Total** | **11,550** | **11,255** | **290** | **5** | **295** |

Measured reported outcomes are **4,646 pass and 6,609 fail**. Their total
happens to equal the strict byte-equal total, but the sets are different:
**278 byte-equal trials have unmeasured outcomes**, and **278 measured-outcome
trials have unequal test trees**. Intersecting exact canonical locks, bound
instruction/tests equality and measured reported outcomes leaves **9,009**
board trials (3,497 reported pass, 5,512 reported fail). This is provenance and
reported-outcome coverage, not 9,009 integrity-validated or previously labelled
trials. No bad-data labels are inferred for the additional 10,150 board members.

All **198 available native lock keys** are measured: 66 pinned-canonical,
66 alternate-source and 66 canonical-source/alternate-digest keys. The latter
66 remain declared-source-unproven and canonically ineligible even though the
exact alternate exports reproduce their digests. No additional downloads
were needed for this final key join. Instruction/reference content is equal
for all 198 keys; tests are equal for 196 and strictly unequal for 2. No full
package byte-and-path equality or historical runtime identity is established.

## Separate source-family controls: bytes and configuration

The control fingerprint input is explicitly scoped
`public_native_dataset_source_family_controls_not_native_trial_qualification`.
It contains **132/132 measured packages: 66 canonical and 66 alternate**, with
no unmeasured packages or declared-source-unproven control keys. This control
coverage must not be substituted for full-board native lock coverage or for
the eight source-unproven primary keys.

Strict equality to the pinned Git task is:

| Dimension | Primary native packages equal / measured | Source-family control packages equal / measured |
|---|---:|---:|
| Instruction | 24/24 | 132/132 |
| Tests, including support paths | 22/24 | 130/132 |
| Environment | 23/24 | 119/132 |
| Configuration (`task.toml`) | 0/24 | 0/132 |
| Reference solution | 24/24 | 132/132 |
| Other support | 3/24 | 70/132 |
| Full byte-and-path equality | 0/24 | 0/132 |

These flags compare exported content/path inventories. Filesystem metadata is
separate: metadata equality is false for 24/24 primary packages and 113/132
controls. Full historical inventory identity is `unmeasured` for all 24
primary keys and all 132 controls. The native digest does not attest omitted
upstream support paths or mode/link metadata; a complete current static
inventory is not a historical full-inventory or executed-runtime attestation.

Across both source-family inventories, the only changed **common file
contents** are the 132 `task.toml` files and 11 canonical
`environment/docker-compose.yaml` files. Each source family omits the same 59
other-support files and 21 directories across 31 tasks. Canonical exports
additionally omit seven test files/six directories across two tasks and eight
environment files/six directories across five tasks. There are no export-only
inventory paths. These are path/content observations, not claims that omitted
support files are unused or that any difference is behaviorally harmless.

### Parsed configuration declarations, not applied runtime values

`R/packages/configuration-comparison.json` records **132 SDK TaskConfig pairs
and 22 Compose file pairs parsed**, with **0 errors**, **0 unmeasured SDK
pairs**, and **0 packages with unknown raw fields**. All **132 exported
`task.toml` files** are covered by their recorded native digest. The 132 pairs
compare those exports to 66 distinct upstream `task.toml` files. The pinned
parser is Harbor **0.24.0**
(`harbor.models.task.config.TaskConfig.model_validate_toml`); Compose uses
`yaml.safe_load`, PyYAML **6.0.3**.

| Control export | Packages / SDK pairs | SDK interpretation of configuration difference | Compose pairs / byte-changed pairs | Equal instruction/tests content with changed projected declarations |
|---|---:|---|---:|---:|
| Alternate `ryanmarten/terminal-bench-4-no-prebuild` | 66 | Package identity only: `/task/name` | 11 / 0 | 0 |
| Canonical `terminal-bench/terminal-bench` | 66 | Agent and verifier pinned image declarations | 11 / 11 | 64 |
| **Total** | **132** | **66 identity-only; 66 image-declaration changes** | **22 / 11** | **64** |

All 66 alternate pairs have no projected declaration differences. All 66
canonical pairs change `/agent/environment/docker_image` and
`/verifier/environment/docker_image`; all 132 resulting image declarations
are digest-pinned (`@sha256:`). Canonical Compose differences are in the
image/prebuild dimension. Under the documented SDK inheritance projection,
there are **zero CPU, RAM, GPU/TPU, storage, network or timeout differences
across 132 packages**. Explicitly copying inherited resource declarations is
not a projected resource change.

None of the 132 raw TOML differences is formatting-only. The 66 alternate
pairs change exactly `/task/name`. Every canonical pair adds
`/environment/docker_image`; 60 also add an explicit `/verifier/environment`
table where it was absent, while 6 change the image inside an existing verifier
environment table. The SDK-parsed resources in those explicit tables agree
with the documented inherited projection; their raw presence is not evidence
of different applied resources. All 11 changed canonical
`environment/docker-compose.yaml` files replace build declarations with image
declarations; `medical-claims-processing` also changes `pull_policy`.

In particular, **64 canonical control packages have equal instruction/tests
content while their image declarations differ**. This is an observed
separation between static verifier content and runtime declarations, not
proof that the executed environments or grader behavior were equal or
different. No images were pulled or executed for this comparison. Job/provider
overrides, prebuild execution, template/environment-variable resolution,
hardware allocation and historical runtime values are outside its projection.
Identity-only or declaration-normalization classifications do not change any
strict package byte flags: `config_equal=false` and full byte/path inequality
remain for every measured package.

## Retained evidence and scope limits

Final source artifacts are pinned below by SHA-256. Native and join snapshots
are content-addressed under their `exports/` directories; package reports use
`fingerprint-stages/`, `configuration-stages/` and `reconciliation-stages/`.
The small reviewed [results.json](results.json) records aggregate counters and
exact immutable paths. These are measured observations, not substituted CI claims.

| Final artifact beneath `R` | SHA-256 |
|---|---|
| Native input manifest | `3083c03284de00d4c4025ee28fac9635da738a4d2fca07c8163fdd0665e89f98` |
| Native package locks | `80e9985e8bacf706c673d58c5e34376b504ca17c14647e3e9423a5fdef4f2548` |
| Full native-lock fingerprints | `c0b7c047bbaf2a1d1d321cfca01f21138ff63898654a7415f7c7cb360af0ca65` |
| Joined summary | `9976adb39749d310874dbd1b5093d4e08e0ff67375f7c54014248721f8f0a136` |
| Joined primary cells | `32d65fe9478a30bd8b147f8226099e67aca5acf3289ac034e43e09c82eb89453` |
| Joined full board | `8fe689230a3a9cf3c2bdd6943c3056e2859525f027991077dc604908f1226444` |
| Source-family control fingerprints | `310ec35c8302226bf52c478648b2eef8bbcd368412c756bfb6d273772923d67f` |
| Configuration comparison | `e2eefd29146d6134b171cf8d5d3e1c0013a43f545a24d40d870b68def04da574` |
| Verifier-difference reconciliation | `65e3e3eeacd4ade2878ae1a3db1b882bb3b53017d8d8958e071ffdf88ac4df2f` |

The earlier primary-only snapshots remain retained: native input
`ee7c08a7c0ce996139a2b20c71df247129443887384fe1736b601a317c7ecb0d`,
primary join `430d30a1cdcc5202a03e21fb06759804af79df8e4d17040d3d78b646abe1e3b1`,
summary `ebebd672264e86868afed74ab84d71813701041098db121e030652c743fd3b82`,
and `packages/primary-native-fingerprints.json`
`e3b32a6b83203bfe3c76ba663f51ccdaee6174dec412a94b9184e6541a7383b7`.
They describe that stage, not the scope currently named by mutable output views.

The reconciliation retains the frozen IN-1 baseline identities:
`file-manifest.jsonl` SHA-256
`10b767e80f9a632e62b62dafbfc2e8481eec3d3a14e2e9d947cc62a51275f638`
and `source-manifest.json` SHA-256
`f41ff70fbfbb62b2c1c7998aa0dd814693e5bbdf54187d80ad3deab09f7a78cc`,
from the retained `infra-tb4-upstream-grader-20261020/runs/in1-tb4-inputs/`
worktree. Original compact label hashes remain
`adcde15bdecba3854a8e6a150282d071ef3acd1ee591a17671748b105de35bf6`
(pass) and
`d1992ae09d577e4617fdd18ecd7b66416596c5919af11686b29b11fb5ada0289`
(fail), as recorded in the frozen spec and primary join inputs.

In every scope, reported reward is not an integrity adjudication, equal
instruction/tests bytes are not runtime equivalence, a current public export
edge is not historical membership attestation, and source mismatch is not a
task defect. The retained primary qualification changes which existing labels
support a narrowly stated provenance-qualified subset, not the original labels
or their historical published denominator.
