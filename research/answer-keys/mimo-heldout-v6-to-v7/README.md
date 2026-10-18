# MiMo original33: frozen-v6 audit → historical-v7 reconciliation

A **retrospective, source-derived QA Task evidence bundle**, not an independent
answer key, detector-recall experiment, or complete grader certification. It
preserves the original 33 held-out task IDs and their order with **no swaps**.
The selected historical v7 candidate remains on scientific **HOLD**;
`scoring_allowed = false` and `final_manifest_scientific_pass = false`.

## Corrected scope and labels

The sealed sources do **not** support the previously advertised 12 weak labels.
The published key has:

| Label / measurement | Tasks |
|---|---:|
| Supported `expected_verdict=fail`, `expected_category=weak` | **11** |
| Of those, measured on the exact original frozen-v6 digest | **10** |
| Of those, measured on a setup-only v7 baseline retaining original test semantics | **1** (`001948`) |
| `expected_verdict=unknown`, not certified clean | **22** |
| Scoped measured task decisions | **33** |
| Certified negative items / pairs / invented root-cause labels | **0 / 0 / 0** |

The 11 supported IDs end in `000143`, `000149`, `000371`, `000657`, `000783`,
`000878`, `001425`, `001686`, `001948`, `002451`, and `002915`. Each fail decision
retains one concrete, hash-pinned representative witness, a prompt-grounded
reason, and recorded reward **1.0 on that decision's exact package**. One finding
per task is published; witness count is not task count.

Two corrections are essential:

1. **`002371` is unknown, not weak.** The original subset witness scored 1.0,
   but Verifier V7-016/V7-019 classify it as **unspecified** under public issue
   #2718. The sealed 32-candidate inventory has **21 wrong candidates on 11 task
   IDs**, six equivalent candidates, and five unspecified candidates. The extra
   handoff's `genuinely_weak_heldout_tasks_12` list still includes `002371` and is
   inconsistent with that later inventory. The historical 12 semantic-repair
   packages also include it; 12 repairs are not 12 established weaknesses.
2. **`001948` has no successful frozen-v6 replay.** Its original full digest is
   `sha256:90fcc765e6113eeadb806948f5cae2c3422d64c715d864e3e660a64b38694133`.
   S0 setup failed with Python 3.6 `future annotations` / `purge-installed-copies@2`.
   The authoritative `ed663141be` witness was accepted on the raw package and on
   the setup-only v7 package
   `sha256:ec0208624b8ae606c83d7ecefdd404bb16b2bf4560dc276ac5f13bb8a3cca1df`
   (native trial `0a88a240-cbb6-4064-9e46-3ba5e8272a11`). **Only the latter digest
   is the scored `001948` subject.** Original digest, failure, raw controls, and
   native result/lock hashes remain in the inventory. Byte-identical test scripts
   do not make a grade transferable across package digests; raw controls are not
   native triplet controls on the setup-fixed package.

Thus the 33 scoring baselines are **32 frozen-v6 + 1 setup-fixed**, not 33
successfully executed frozen-v6 packages. All 33 original frozen-v6 digests are
preserved separately in `original_subject` and `audit.json`. No original defect
label is scored against a **semantic-repair** v7 package.

## Files and custody

- `audit.json`: compact immutable reviewed input. It pins source handoffs, the
  canonical cohort, all original full digests, scoped original probe outcomes,
  later same-digest updates, semantic corrections, representative witnesses,
  historical-v7 diagnostics, execution limitations, and accounting caveats.
- `build.py`: deterministic publisher using the existing `qa.answer_key/v1` and
  `qa.finding/v1` models. It checks the two committed cohort manifests and the
  exact original membership/order/digests, permits only the documented `001948`
  baseline exception, and refuses promotion of the historical-v7 release gate.
- `items.jsonl`: generated 33 items, sorted by item ID, all `split=heldout`;
  the original cohort order remains explicit in `audit.json`.
- `findings/decisions.jsonl`: generated 33 scoped measured decisions: 11 fail and
  22 nonflag pass findings. A nonflag pass means only that the retained, measured
  probe and semantic reconciliation do not establish a wrong witness. Its key
  label remains **unknown**, never `expected_verdict=pass`.

The builder needs only `audit.json` and the committed original `heldout.json` and
`split.json`. Original packages, witness bytes, and run archives are **not**
substituted with the existing MiMo atlas or downloaded packages. External paths
and archive-member hashes are custody references: they do not promise fresh
checkout availability, constitute newly replayed trials, or authorize execution.
Replaying a witness requires separately recovering the exact recorded package
and witness bytes and observing the execution-policy boundary.

Primary source hashes (full paths and byte sizes are in `audit.json`):

| Source | SHA-256 |
|---|---|
| Original `qa-task-mimo-v6-reproducers-20261017.json` | `2f085e317114c9d13dd75613c7f69224d59a2d4b99aef0c63ede59a4bc863910` |
| `qa-task-v7-verification.json` | `3347cb819a2ee46c419ea4b2c9a78d74fe92b6eacf43361b20fc471edd3c272e` |
| `reproducers-v7-extra.json` | `dfbacfcb9c44b3a014d97dfb152d3007434cdfa7cc56aa699d35c25ce9c71bf7` |
| `qa-task-v7-replacement-candidates-000783-000371.json` | `1e2d063d9afd0be271b62f19c67fb99d3937b5d91105d666c2ee629c38ef817a` |
| Original committed `mimo-training-20261016/heldout.json` | `d6b0336b4d475f9e9631f295cbde554630e5087c630f3093cc56b13a6e73f9de` |
| Original committed `mimo-training-20261016/split.json` | `3f6a82e2fa8abf86f3d67d63edc0ecd1c823e466171f1f1a6a85100cb27b37da` |

The original audit is under `research-context/bridge/overnight-2026-10-17/`;
other primary handoffs are under `research-context/bridge/2026-10-17-v7/`.
The two QA Task narrative files are separately hash-pinned as snapshot context,
not substitutes for the sealed JSON. Raw custody includes the original M3,
phase-one, `001948`, selected-v7, and replacement-sidecar archives under
`lab-extras/data/qa-task-runs/`; member hashes are retained where a representative
original witness or report is used. The large archives are not committed here.

## Historical-v7 diagnostics are not original labels

The selected **historical** manifest is
`sha256:c5de206438d3e103aec4d8266d3cbe881ff5e03a96eb8dcac29047f80105d200`.
The sealed QA source reports 33/33 QA-complete, 33/33 selected digest matches,
12 repaired packages plus 21 unchanged-v6 packages, **21/21 genuinely wrong
reproducer grades 0**, and **6/6 equivalent QA replay grades 1**. These outcomes
remain bound to their own selected package digests in diagnostics only.

They do not resolve the remaining scientific HOLDs:

- **V7-014, `000783/df676d2dcc42`:** supported-tokenizer correct alternate scores
  v6=1, selected v7=0 (correct-submission preservation).
- **V7-018, `000371/cc1349da3c95`:** constructor-preserving correct alternate
  scores v6=1, selected v7=0 (correct-submission preservation).
- **V7-021, `000657/da34eb301f58`:** one-second deadline timing sensitivity in
  added retry tests (SOURCE HOLD).
- **V7-016/V7-019, `002371/aaffb9ae35c8`:** recorded subset witness is unspecified,
  not a certified wrong reproducer.

The separate replacement-sidecar audit reports three QA-complete candidates,
both preservation alternates accepted at 1.0, and all 12 recorded wrong
reproducers rejected at 0.0. Its packages are `000783/bb1da511153d`,
`000371/eaefb3cc164b`, and `000657/302936515e85`; their **full digests** are in the
inventory. This local audit does not modify the c5de-selected manifest, claim
native RepeatVerifier ×3 qualification, or promote `scoring_allowed`.

## What the measured controls establish

Successful original S0 reports for 32 tasks use three `oracle-1/2/3` slots and
three `nop-1/2/3` slots, but **one physical session per role**: 64 successful
role sessions, not 192 independent runs. `001948` has three failed path locators
per role; they do not establish successful grader executions. The selected QA
re-audit similarly reports one session per role on all 33 tasks.

Separately, the sealed handoff records **24 native Harbor RepeatVerifier trials
on the 12 repaired packages, comprising 72 physical grader executions** (three
per trial). Those are historical native evidence, not the QA slot aliases and
not executions made by this publisher. V7-030 also notes that original
`.qa-task-controls` sibling artifacts were not included in the QA tarballs;
those QA S0 paths remain unbound custody, not archive-verified native triplets.

The original report had 23 QA-complete tasks and ten incomplete tasks. Later
same-digest work resolved `001199` and `001371` S2 baseline failures and completed
the unchanged-v6 audit. Both original failures and later observations remain
visible. Completed QA scope is still not exhaustive coverage. Lower-level
ref-subset hashes are retained as such, not equated with full task digests;
accepted original witness bindings use the full task-report digest and recorded
S3 evidence. Equivalent edits, unspecified behavior, absent mutable lines, and
unreviewed survivors are never promoted to weak labels or certified negatives.

## Regeneration and scoring

From the repository root:

```sh
uv run python research/answer-keys/mimo-heldout-v6-to-v7/build.py
uv run python research/answer-keys/mimo-heldout-v6-to-v7/build.py --check
uv run evallab qa score \
  --key mimo-heldout-v6-to-v7 \
  --findings research/answer-keys/mimo-heldout-v6-to-v7/findings \
  --split heldout
```

These commands publish/compare data and score retained decisions; they do not
run models, containers, controls, or witnesses. `--check` compares both generated
outputs byte-for-byte and does not write them. Use `audit.json.suggested_score`
for the `QA Task` agent, `qa-task/mimo-v7-audit` system, explicit retrospective
status, and row notes. The integration owner publishes scoreboard and exposure
registry changes separately.

The scorer's supported-positive rediscovery ratio can be **11/11 = 1**, but this
is **source-derived reconciliation**, not independent detector recall. All 33
scoped decisions are measured; the 22 unknown labels supply no clean-negative
population, so false-alarm rate is **n/a**. Root-cause match and paired
discrimination are also **n/a**. `heldout` preserves the original cohort split;
it is not a claim that these reviewed source labels were unseen.

Reviewer `cost_usd=0` records **no newly incurred publishing charge only**. The
extra source's $0.30 field includes the historical `001948` native reservation
($0.60 per-replay ledger cap); it is not independently reconciled provider
billing. Other native-control spend and historical subscription/model costs
remain unreconciled. Replacement-sidecar recorded Modal spend of zero does not
make the overall work free. **Do not report zero total cost.**

## Attribution and licensing

This bundle contains laboratory evidence metadata and short requirement/review
excerpts, not a redistribution of full task packages or archives. Its recorded
source lineage is
[FineEnvs/MiMo-V2.6-RL-harbor-code](https://huggingface.co/datasets/FineEnvs/MiMo-V2.6-RL-harbor-code),
revision `5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785`, derived from
[XiaomiMiMo/MiMo-V2.6-RL-oss](https://huggingface.co/datasets/XiaomiMiMo/MiMo-V2.6-RL-oss),
revision `639865fd3374018d6cb29b9fb82dd531406fcf5f`.
The dataset is Apache-2.0; retained notices and source licensing caveats are in
[`../mimo-weak-tests-v2/README.md`](../mimo-weak-tests-v2/README.md#source-provenance-and-attribution),
[`LICENSE`](../mimo-weak-tests-v2/LICENSE),
[`NOTICE`](../mimo-weak-tests-v2/NOTICE), and
[`LICENSE-mimoagent.md`](../mimo-weak-tests-v2/LICENSE-mimoagent.md).
Referenced code/patches remain subject to the respective upstream project
licenses; the dataset notice does not relicense those projects. Witnesses are
modified benchmark submissions, not unchanged upstream fixes. No new license or
independent certification is asserted for the externally retained archives.
