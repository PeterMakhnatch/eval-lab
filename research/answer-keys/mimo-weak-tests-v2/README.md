# MiMo weak-test atlas v2

A source-derived, positive-only catalog for the loop **find a required behavior
that passing tests miss → strengthen the tests → regrade the retained witness**.
It is not an independent external answer key or a benchmark-prevalence estimate.

## Scope and files

The atlas preserves the fixed 567-task primary cohort, its original selection
positions, 338 archived primary reports, and the 229-task primary continuation.
It does not expand that cohort to all MiMo tasks. Existing supplementary line
probes are identified separately; missing line probes are not recorded as passes.

- `atlas.jsonl`: one evidence inventory row per primary task, including raw
  outcomes, semantic adjudications, unresolved claims, provenance, and witnesses.
- `items.jsonl`: one digest-bound `qa.answer_key/v1` item per **supported weak
  task**, not one item per candidate or per witness. There are no negative items.
- `findings-supported-v2.jsonl`: accepted `qa.finding/v1` records, one per retained
  executable witness. Subjects use the exact `task.toml` `[task].name` and package
  digest, rather than a bare directory name.
- `witnesses/`: the existing passing patches, copied without changing their
  executable contents and individually SHA-256 pinned in the atlas. A patch is
  applied to its task's benchmark base, not on top of the full reference patch.
- `summary.json`: deterministic task, witness, and split counts.
- `build.py`: validates the reviewed inventory and rebuilds the three derived
  outputs above. It rejects an incomplete or duplicate primary cohort before
  constructing answer-key outputs, and refuses mismatched witness digests,
  unaccepted witnesses, or unusable recorded controls.

The inventory and witness patches are committed; the original task packages,
Docker images, and raw run directories are not. Provenance paths identify the
archived evidence rather than promising those files exist on every checkout.
`package.root_hint` is a local materialization hint, not an assertion that a
fresh upstream download has the same bytes. A repaired or rematerialized package
must be checked against the recorded digest before transferring a label.

`primary.verdict` is the archived runner result, not a semantic defect label.
Only accepted witnesses establish `semantic_verdict = "confirmed_weak"`;
`"not_established"` is never a clean-task certificate. The inventory keeps
missing archived package bindings distinct from current materialization digests.
Its `unknowns` also identify untried generated mutations, line-stage baseline
mismatches, not-applicable probes, and historical survivors outside the reviewed
scope. A completed primary report does not imply exhaustive mutation coverage.

## What a supported label means

Every exported witness combines:

1. Recorded, scored, return-code-zero **full-reference = 1**, **no-op = 0**, and
   **witness = 1** controls on the bound package.
2. Instruction-level source adjudication identifying a required behavior the
   witness omits or breaks, including the consuming path when one is necessary.
3. A retained executable patch and provenance for the review and recorded grade.

A block witness is a passing kept subset of the reference patch. Search is
budget-bounded; “minimal.patch” is the runner's artifact name, not a proof of
smallest possible deletion. A line witness is the full reference patch with one
mutated edit, not a standalone one-line fragment. The reference itself need not
be globally correct for a separately established instruction violation to count.

A block review can identify a shared reference/specification conflict; the
review marks when that gap is not caused by its dropped hunks. Line reviews
still require an effect of the mutated edit: an inherited defect is not counted
again as a mutation-caused required gap.

A surviving edit is **not** a defect label by itself. Equivalent changes,
implementation choices, and behavior outside the task's requirements are not
exported. Missing consumer implementations, ambiguous contracts, infrastructure
failures, baseline mismatches, unscored checks, and unreviewed survivors remain
explicit in the inventory. A refuted omission does not establish that the whole
task is clean; neither does failure to find a passing omission.

Some reviews also record isolated probes of an actual source consumer. These
are not complete-task or grading-harness replays. A reconstructed source file
is marked as reconstructed and used only when its whole-file Git blob matches
the baseline prefix recorded by the reference patch; an unbound release or
similarly named helper is not a substitute for the actual consumer.

Declared-dependency findings may use the exact target-file and manifest blobs
plus one npm-range-valid production dependency closure as a source-level
counterexample. This is not a fresh installation or an observed module-loading
failure, and does not establish the benchmark image's ambient packages.

`refutation.status = "verified"` here denotes archived controls plus accepted
semantic adjudication. It does **not** denote a fresh replay, an independently
certified repair, or proof that a modified grader kills the witness. Reviewer
model/token accounting is unavailable; the zero cost field records no separately
charged local task execution, not a claim that reviewer inference was free.

## Repair admission is separate

Do not remove mutation evidence from a report to make it admissible. The current
block-repair consumer rejects full reports containing line-level survivors;
those findings remain useful targets but are not represented as admitted or
certified repairs. Regrading a repair requires the exact original package and
witness, a fresh isolated execution, and the repair consumer's own acceptance
checks. A schema-valid finding alone is not that certificate.

## Selection, splits, and scientific limits

These targets were selected by `ref-subset` and reviewed with its results in
view. Its own rediscovery score is circular. This positive-only catalog cannot
supply a false-alarm rate, estimate population prevalence, or establish detector
recall on an independent benchmark. No synthetic clean controls are introduced
to fill missing observations.

Existing task split assignments are preserved across `trace-paired-v1`,
`executed-outcomes-v1`, and `mimo-weak-tests-v1`. New task assignments use the
unchanged v1 hash seed and one-third development allocation. A `test` assignment
is bookkeeping, not evidence that these source-derived labels were unseen.
Selection exposure is registered in `../splits/exposure.jsonl`; v1 is retained
as a historical key rather than silently rewritten.

From the repository root:

```sh
uv run python research/answer-keys/mimo-weak-tests-v2/build.py
uv run python research/answer-keys/splits/guard.py
uv run python research/answer-keys/mimo-weak-tests-v2/build.py --check
uv run python research/answer-keys/splits/guard.py --check
```

These commands rebuild or validate data; they do not replay benchmark witnesses.
Run finding consumers from the repository root so repository-relative patch
paths resolve correctly.

## Source provenance and attribution

The source lineage is
[FineEnvs/MiMo-V2.6-RL-harbor-code](https://huggingface.co/datasets/FineEnvs/MiMo-V2.6-RL-harbor-code),
revision `5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785`, material digest
`sha256:892a58265a97f24ebbd9f9fcbc36b9983771544e45bbc6327f75c3a315fc8923`.
It derives from
[XiaomiMiMo/MiMo-V2.6-RL-oss](https://huggingface.co/datasets/XiaomiMiMo/MiMo-V2.6-RL-oss),
revision `639865fd3374018d6cb29b9fb82dd531406fcf5f`.
The conversion is by Hugging Face and FineEnvs and is not affiliated with or
endorsed by Xiaomi. The source dataset is Apache-2.0; its `LICENSE` and `NOTICE`
are retained here. Reference-derived witness patches are modified benchmark
submissions, not unchanged upstream fixes. Code and patch context remain subject
to their respective upstream project licenses; the dataset notice does not
relicense those projects.
