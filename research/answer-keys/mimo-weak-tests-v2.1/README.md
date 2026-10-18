# MiMo weak-test atlas v2.1

A complete, source-derived, positive-only weak-test catalog for the fixed
**567-task** primary cohort. This version adjudicates the **112 previously
unreviewed archived line survivors across 22 tasks**; it neither reruns the
primary sweep nor invents observations for unprobed mutations.

The publication contains **114 supported weak tasks**, **242 complete-patch
witnesses** (79 block subsets and 163 line mutants), and **453 not-established
tasks**. The completed 112-survivor batch has **35 required gaps, 62 non-positive
edits and 15 unresolved claims**. All 207 v2 witnesses remain byte-identical;
the 35 supported additions are retained separately in this version.

## Version and files

- `atlas.jsonl`: all 567 inventory rows, preserving the original selection,
  primary reports, package identities, archived line edits and grade observations.
  The 112 new reviews carry batch `archived-112-20261018`, exact source citations,
  instruction-bound reasoning and explicit parent acceptance. No archived line
  survivor remains `unreviewed`.
- `items.jsonl`: one package-digest-bound `qa.answer_key/v1` item per supported
  weak task, **not** per survivor. There are no invented clean negatives.
- `findings-supported-v2.1.jsonl`: one typed `qa.finding/v1` per accepted witness.
- `witnesses/`: individually SHA-256-pinned, complete passing patches, including
  unchanged retained v2 witnesses. A line witness is applied to the benchmark
  base, **not** on top of the reference fix; it is not just the one-line edit.
- `summary.json`: deterministic cohort, review, unknown, witness and split counts,
  including the exact 112-survivor review-batch disposition.
- `build.py`: checks the frozen v2 inventory's accepted SHA-256, exact review-batch
  coverage and unchanged archived edit/grade bindings; reuses the v2 typed exporter
  without changing its default outputs. It rejects unaccepted witnesses, changed
  patch digests, unusable recorded controls and any remaining unreviewed survivor.

[`mimo-weak-tests-v2`](../mimo-weak-tests-v2/README.md) remains a separate historical
publication. Its inventory, items, findings, summary and witness bytes are not
rewritten. Existing first-wins splits are retained, including all v2 assignments;
new task assignments use the unchanged v1 seed. A `test` split is bookkeeping,
not proof these source-derived targets were unseen. Selection is recorded in
[`splits/exposure.jsonl`](../splits/exposure.jsonl).

## Adjudication and evidence limits

`required_gap` means the mutated edit causes a concrete violation of a literal
task requirement, with the actual consuming path checked when needed. An
inherited reference defect is not relabeled as a mutation-caused defect.
`non_positive` records equivalence, a permissible implementation choice or
behavior outside the requested contract; it does **not** certify a clean task.
`uncertain` records an adjudicated but unresolved source/contract claim and names
the missing evidence. Only `required_gap` reviews with usable archived
**full-reference = 1, no-op = 0, witness = 1** controls become findings.

These are historical scored, return-code-zero controls on the exact package,
combined with source review; no fresh benchmark/container replay is claimed.
`refutation.status = "verified"` denotes that combination, not a fresh replay,
a grader repair, proof a strengthened test kills the witness, or certification
of a rematerialized package. The zero reviewer cost field records no separately
charged execution; native-review model/token accounting is unavailable and is
not claimed to be free inference.

Bounded local probes exercised actual archived helper/expression bodies and the
touched-function consumer on explicit, syntactically valid C++ fixtures. That
consumer proof uses inclusive function spans supplied by the fixture, **not**
recovered original parser output. The NamedTuple probes exercise the actual
classifier and added argument-access prefix, not a complete Hypothesis run.
SQS and alert-aggregation probes likewise do not import/replay the original
services or UI. These probes establish their stated local premises only.

All 112 whole reference/mutant pairs were checked for the stated single final
source-line edit using agreeing literal old contexts; this is not recovery of
an omitted original source tree. The napari pair retains an explicit differing
old-Git-object limitation. Unresolved claims retain their actual missing
dependency/default/consumer bindings, including the rack-check source/archived
test contradiction; they are not counted as positive or clean.

The inventory's other unresolved claims, untried mutations, missing scans and
baseline mismatches remain explicit. Completing this review batch does not mean
exhaustive mutation coverage. `not_established` is not a clean negative. Labels
are bound to the exact package digest; check it again before transferring a
label to another materialization or repair.

Actual instructions, raw reports, test sources, images and original run trees
are archival inputs, not committed runtime dependencies. Their provenance paths
identify the reviewed evidence; they need not exist on another checkout. The
committed witness paths resolve from the repository root and are checked by the
producer. Package `root_hint` is a local materialization hint only.

This is a target catalog for **find a passing wrong patch → strengthen the tests
→ regrade the retained witness**. Repair admission and certification remain
separate. Source-visible selection makes the selecting tool's rediscovery score
circular. This key supplies neither population prevalence nor an independent
recall/false-alarm estimate; no synthetic clean controls fill the gaps.

## Rebuild and check

From the repository root:

```sh
uv run python research/answer-keys/mimo-weak-tests-v2.1/build.py
uv run python research/answer-keys/splits/guard.py
uv run python research/answer-keys/mimo-weak-tests-v2.1/build.py --check
uv run python research/answer-keys/mimo-weak-tests-v2/build.py --check
uv run python research/answer-keys/splits/guard.py --check
```

These commands validate/rebuild committed data, not benchmark grades. Run finding
consumers from the repository root so repository-relative witness paths resolve.

Use the shared scorer for this provenance-rich key, with one `qa.finding/v1`
JSON document per file in the findings directory:

```sh
uv run evallab qa score --key mimo-weak-tests-v2.1 \
  --findings RUN/findings --scoreboard LOCAL_SCOREBOARD.csv
```

The separate `qa task-score` command currently uses a stricter local answer-key
model that rejects these provenance fields; it is not this key's consumer.
Scoring the exported supported findings against their own key is an integration
self-consistency check, **not** a detector result. There are no negative items
from which to measure a false-alarm rate.

## Source attribution

Source lineage:
[FineEnvs/MiMo-V2.6-RL-harbor-code](https://huggingface.co/datasets/FineEnvs/MiMo-V2.6-RL-harbor-code),
revision `5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785`, material digest
`sha256:892a58265a97f24ebbd9f9fcbc36b9983771544e45bbc6327f75c3a315fc8923`,
derived from
[XiaomiMiMo/MiMo-V2.6-RL-oss](https://huggingface.co/datasets/XiaomiMiMo/MiMo-V2.6-RL-oss),
revision `639865fd3374018d6cb29b9fb82dd531406fcf5f`.

The Hugging Face/FineEnvs conversion is not affiliated with or endorsed by
Xiaomi. The Apache-2.0 dataset `LICENSE`, `NOTICE` and original MiMo license notice
are retained here. Witnesses are modified benchmark submissions, not unchanged
upstream fixes. Patch context and code retain their respective upstream project
licenses; the dataset notice does not relicense those projects.
