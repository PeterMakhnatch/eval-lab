# MiMo stratified sample100: historical source reconciliation

This key publishes **100 actual raw-baseline task subjects**, not 100 independently
executed v6 cloud audits. It reconciles the frozen QA Task sample handoff against
its original atlas classifications. It is source-derived supported-positive
rediscovery, **not external, blinded, independent detector recall**.

## Counts and interpretation

| Quantity | Frozen source result |
| --- | --- |
| Answer-key expectations | **23 fail / 77 unknown / 0 pass** |
| Strict QA finding decisions | **24 fail / 76 scoped nonflag pass** |
| Supported-positive reconciliation | **23/23** original atlas positives flagged |
| Strict flags without an original atlas positive label | **1**, task `001244` |
| Original reviewed atlas overlap | **99** tasks: 23 positive, 76 nonpositive |
| Strict binary atlas agreement | **99/99** on that overlap |
| Raw executed S2b candidates | **69/100**, Wilson 95% CI **[0.5937, 0.7722]** |
| Strict prompt-adjudicated weak proportion | **24/100**, Wilson 95% CI **[0.1669, 0.3323]** |
| Sensitivity including resolved uncertain cases | **26/100**, Wilson 95% CI **[0.1840, 0.3537]** |
| Historical separate-verifier v6 S3 replays | **9**, comprising 6 Modal and 3 Docker |
| Preserved key split metadata | **40 dev / 60 test**; score this reconciliation with `--split all` |

The schema scorer's `recall=1` is the **23/23 supported-positive reconciliation**
only. The 24/100 proportion is a descriptive sample weak-test rate, not “24%
recall.” The 99/99 agreement is a separate original-source binary crosscheck,
not independent benchmark accuracy. **FPR is n/a**: this key has no pass-labelled
clean negatives. Every source nonpositive, uncertain, or unreviewed expectation
is `unknown`.

Strict findings use exactly `qa_task_verdict == "confirmed_weak"`. Tasks
`000116` and `001950`, labelled `confirmed_weak_from_uncertain`, contribute to the
26-task sensitivity result but **not** to the 24 strict flags. Their finding
`pass` means only “not flagged under this strict rule,” with measured probe
references; it is not a claim that their graders or tasks are clean. The source
strings `clean_pass` and `refuted_neutral_or_equivalent` are retained as historical
classifications, not promoted to clean answer-key labels. Source QA verdicts are
31 `clean_pass`, 43 `refuted_neutral_or_equivalent`, 2 sensitivity-only resolutions,
and 24 strict positives.

## Sampling denominator and selection limitations

The sample seed is `qa-task:mimo-phase2-stratified-100:20261017`. The stated frame
is **534 non-held-out tasks** in the 567-task primary MiMo cohort, excluding the
original 33-task heldout. Actual sampling eligibility was the **389 previously
measured S0+S2b primary-report tasks** in four languages, not all 534 tasks.
There is **zero task-name overlap** with the original heldout33; the inventory
pins the 33-name exclusion source and retains the names used for that check.

| Language | Sample n | Raw candidates | Strict weak |
| --- | ---: | ---: | ---: |
| Python | 74 | 47 | 15 |
| JavaScript | 13 | 10 | 3 |
| TypeScript | 8 | 7 | 3 |
| Go | 5 | 5 | 3 |

Wilson intervals are computed deterministically from these selected observations.
They do not correct availability selection, language allocation, reused probes,
or adjudication with candidate evidence already visible. This is not an unbiased
fleet-wide prevalence claim for all 534 non-held-out tasks or all MiMo packages.

All 24 tasks having current atlas items retain those existing item splits,
including `001244` despite its frozen unknown expectation. Remaining tasks retain
pre-existing locked splits where available, otherwise the unchanged atlas hash
seed `mimo-weak-tests-v1:split:20261015` supplies the assignment. `audit.json` records
which source supplied every split. Here “non-heldout” refers to exclusion of the
original 33-task cohort; an inherited `test` split is bookkeeping, **not evidence
that the selecting or adjudicating system had never seen the task**.

## Actual package bindings: raw probes versus v6 diagnostics

All **100** hash-bound primary reports identify `raw_package_digest`, and none
identify the different `v6_package_digest` listed beside them in the handoff.
Consequently both `items.jsonl` and `findings/decisions.jsonl` use the **actual raw
report package digest and exact original task name** for every task. The inventory
preserves each original v6 mapping separately as provenance; it does not transfer
raw-package control outcomes to v6. The measured raw reports all retain scored,
return-code-zero full-reference=1 and nop=0 controls. Four report searches reached
their budget bound; this is not exhaustive mutation coverage.

Each strict fail finding reuses one concrete, same-raw-digest supported witness
from the committed atlas, with its patch hash, source path, accepted
instruction-level reason, full/nop/witness controls, and evidence references.
The builder checks the retained patch hash and digest binding. `verified` denotes
these archived controls plus reused semantic adjudication, **not a new execution
or independent reviewer**. No task code is imported or executed by this builder.

The **nine historical v6 S3 replays** retain their separate exact package digests,
witness hashes, archive-member hashes, and job paths in the audit inventory. They
are diagnostics, **not nine extra score findings** and not evidence of 100 fresh
cloud audits or 24 fresh replayed witnesses. Their source-reported rewards are
9/9 at 1.0. No new model call, container, cloud replay, or additional control is
part of publishing this bundle. Archive paths are evidence custody, not a claim
that those trials were replayed during publication.

## Frozen labels and later source revisions

The original handoff has 23 atlas strict positives, 76 atlas-reviewed nonpositives,
and **one atlas-unreviewed task, `001244`**. QA Task marked that last task strict
weak and retained its patch plus exact-v6 Modal replay provenance. Its raw primary
report also has measured full/nop and passing retained-subset evidence.

The current committed atlas has since promoted `001244` to `confirmed_weak` and
retains the same patch hash
`359677686abe442549b60db9910a95b32e54501f29e5a19e3ae0f04c4816ec72`.
This later supported witness supplies concrete source custody, **not a retroactive
24th positive key label**: the frozen expectation remains `unknown`. Current atlas
reasons/witnesses are reused supporting evidence, not represented as the exact
original handoff's adjudication bytes. Original and current semantic statuses are
both retained.

Two mutable atlas upstreams also changed after the handoff. The inventory keeps
original custody records and separately observed current records:

- `portable-witnesses-current.json`: original
  `sha256:204afbfe4866b830e372a4adc8d71290c754176d7079ea34425a45cde77a225e`;
  observed current
  `sha256:02148374730f59bf79d1200f673dce714724157b5b27892dc958a19b8cbe0707`.
- `atlas-adjudicated-lines.json`: original
  `sha256:aac0ca60009c78daf6b8016e82691a89a2a106382d9a2dfa7dcf3a161e394c40`;
  observed current
  `sha256:e39c3a91739f48519c13a21080310d10963966e40a919027993c9989a1846917`.

Current bytes are not called original bytes, and no silently revised atlas label
changes this key's historical denominators. The current committed atlas and items
used for witness/split custody are themselves separately hash-pinned.

## Files, source custody, and regeneration

- `audit.json`: compact immutable inventory, including all 100 original report
  paths/byte counts/hashes, actual report package bindings, both raw/v6 mappings,
  frozen classifications, 24 supported witness references, split provenance, and
  nine replay member inventories. It contains hashes/metadata, not massive raw
  archive or task copies.
- `build.py`: deterministic stdlib + existing `AnswerKeyItem` / `Finding` builder.
- Generated `items.jsonl`: 100 `qa.answer_key/v1` task rows, sorted by item ID.
- Generated `findings/decisions.jsonl`: 100 `qa.finding/v1` strict decisions,
  sorted by finding ID.
- Generated `summary.json`: computed counts, Wilson intervals, original atlas
  agreement, cost limitations, and suggested scoreboard status/notes.

Primary source custody:

1. Handoff:
   `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-17-v7/qa-task-phase2-sample100.json`,
   **286,989 bytes**,
   `sha256:e0146b9068b7ef5146976efbdca1647034850e631ba4cfcdcf7eb570c1ba40ce`.
2. Committed source snapshot at
   `aa9df22796b225125c4fa98489872addbb4bd26d`:
   `../mimo-weak-tests-v2/atlas.jsonl`,
   `sha256:67c2ab7a0b6ba2849b14daa5fa69c9b322f1f509c28ff1facc087cdb19dc8c4a`;
   `../mimo-weak-tests-v2/items.jsonl`,
   `sha256:bad32d4b83a8ce606fce10b361908e49c7ab81852a971892fd001d7c9edeffb2`.
3. Replay archive:
   `/Users/petermakhnatch/Developer/lab-extras/data/qa-task-runs/mimo-phase2-sample100-s3-replays-20261017.tgz`,
   **96,874,701 bytes**,
   `sha256:c9b1c58df14755f1a955874caa2e52f8ff7de0b7bddddb3738e8a1ac8444885e`.
   The original 274 trial member hashes plus the summary member hash were bound
   and read during source ingestion. One additional 158-byte `cloud-spend.json`
   reservation receipt is separately hash-pinned in the inventory. The original
   `cloud_spend_gate_member: null` is preserved rather than rewritten as a gate.
4. Original heldout33 exclusion source:
   `bridge/2026-10-17-v7/qa-task-v7-verification.json`,
   `sha256:3347cb819a2ee46c419ea4b2c9a78d74fe92b6eacf43361b20fc471edd3c272e`.

The audit also retains the handoff's exact source-package bindings and remaining
primary cohort custody, plus original/current upstream byte counts and all
source-report hashes. All 100 source report hashes and byte counts matched the
original handoff during inventory preparation; no source report was replayed.
Regeneration requires only committed inventory and referenced committed witness
patches, not access to those external raw paths.

From the repository root:

```sh
uv run python research/answer-keys/mimo-stratified-sample100/build.py
uv run python research/answer-keys/mimo-stratified-sample100/build.py --check
uv run evallab qa score --key mimo-stratified-sample100 \
  --findings research/answer-keys/mimo-stratified-sample100/findings --split all
```

Use scoreboard agent `QA Task`, system `qa-task/mimo-sample100-strict`, and an
explicit source-derived reconciliation status; `summary.json` supplies suggested
notes. Schema-valid publishing is not proof of scientific independence. Split
registry/exposure integration belongs to the parent repository integration step.

## Cost and release boundaries

`reviewer.cost_usd = 0` records **no newly incurred publishing charge**. Historical
phase-2 Modal authorization had a $2.00 cap and **$1.80 reserved for 36 recorded
executions** at $0.05 reserved per execution; this is not a settled invoice or full
cost total. Historical model/provider inference cost is unreconciled. Neither the
zero publishing field nor a source-reported local Docker charge of zero establishes
zero total historical cost.

The v7 scientific release gate stays **false** (`scoring_allowed=false`,
`final_manifest_scientific_pass=false`). Historical selected candidate
`sha256:c5de206438d3e103aec4d8266d3cbe881ff5e03a96eb8dcac29047f80105d200`
is not a final scientific release. This raw-baseline reconciliation certifies
neither repaired-package correctness nor new outcomes on any other digest.

## Licensing and attribution

The source lineage and retained legal notices are documented in
`../mimo-weak-tests-v2/README.md`, `LICENSE`, `NOTICE`, and `LICENSE-mimoagent.md`.
The MiMo Harbor conversion derives from FineEnvs/MiMo-V2.6-RL-harbor-code revision
`5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785` (material digest
`sha256:892a58265a97f24ebbd9f9fcbc36b9983771544e45bbc6327f75c3a315fc8923`),
itself derived from XiaomiMiMo/MiMo-V2.6-RL-oss revision
`639865fd3374018d6cb29b9fb82dd531406fcf5f`. The dataset is Apache-2.0; benchmark
patch context remains subject to its upstream project licenses. This bundle
references existing retained witness patches without copying or relicensing them.
