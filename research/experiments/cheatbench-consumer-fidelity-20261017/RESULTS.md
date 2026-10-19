# CheatBench consumer fidelity — results

Run on 2026-10-17 from base `5171c20c7`, local CPU only, $0 provider spend.
Command:

```bash
.venv/bin/python \
  research/experiments/cheatbench-consumer-fidelity-20261017/run_audit.py \
  --verify-source-hash
```

Machine summary: `results/summary.json`. Full audit rows are in ignored
`derived/trace-consumer-audit-v1/{audit-atif,audit-kev}.jsonl` (4,472 and
885 rows). The Parquet SHA matched its PIN
(`b4d2a149…60e63`). Method and acceptance contract: [`README.md`](README.md).

## Answer

CheatBench traces are **not safe to consume as-is** for QA-trace judging or
evidence-grounded critic training. Two separate problems show up on the full
corpus:

1. **Answer leakage:** every measured materialized ATIF document (4,268 of
   4,268) carries evaluator metadata (`cheatbench_trace_id`,
   `cheatbench_label`, `cheatbench_mechanism` — 12,804 forbidden-key hits).
   A consumer that reads the whole document, as a trace investigator bundle
   does, can see the ground-truth label. This proves the input is not blind;
   it does not prove any model used the label.
2. **Exact-evidence loss:** of 626 source-verified measurable anchors, the
   labelled snippet survives verbatim (under the fixed variants) in only
   **89 (14.2%)** materialized ATIF documents and **180 (28.8%)** Kev window
   sets. Neither consumer preserves it for **428 (68.4%)**.

The reusable fix for problem 1 is in this change: `blind_atif` applied to all
4,268 materialized ATIF documents left **0 forbidden fields** and changed
flattened evidence text in **0** documents. Problem 2 has no metadata fix;
it defines which subset can honestly be used for exact-evidence supervision.

**Fixed at the source (2026-10-19).** `evallab.cheatbench.convert` now
returns the `blind_atif` projection and `materialize` writes `result.json` as
`{}`. Labels and mechanisms live only in the `detectors-v1` manifest. The
manifest's 4,268 CheatBench `trajectory_sha256` values were regenerated;
`cb_subset.json` is byte-identical. The shared root
`derived/detector-roots/cb` was re-materialized and checked:

| | before | after |
|---|---:|---:|
| forbidden-field hits in trajectories | 12,804 | 0 |
| label/mechanism/reward keys in `result.json` | 12,804 | 0 |
| documents whose evidence text changed | — | 0 |
| manifest hashes matching the files | 0 | 4,268 |

## Denominators

| stage | count |
|---|---:|
| CheatBench rows in `cb_subset.json` | 4,472 |
| labels | 844 cheating / 41 attempt / 3,587 benign |
| positive rows with a localization | 885 |
| measurable loc kinds (`localized_snippet` 618 + `line_span` 15) | 633 |
| source-verified in the resolved source field | **626** |
| not measurable / not source-verbatim (`unknown`) | 259 |

The 259 unknowns are 243 `latent_harness`, 8 `latent_inference`, 1
`unlocalized`, and 7 snippets not verbatim in their resolved source field.
They are never counted as consumer losses.

## Consumer headline

| consumer | coverage | blinding | exact anchor preserved / lost (n=626) |
|---|---|---|---|
| materialized ATIF | 4,268 measured; 204 `not_run` (`no_agent_content`) | **fail on 4,268/4,268 measured** | **89 / 537 (14.2% preserved)** |
| Kev windows (`state` only) | all 4,472 wanted traces seen; all 885 anchor traces have chunks | pass: 0 annotation-marker failures in `state` | **180 / 446 (28.8% preserved)** |

Kev `trace_label`/`chunk_label` columns are supervised targets beside
`state`; they are not counted as input leakage. A consumer that feeds whole
JSONL rows to a model would still leak them.

Joint exact-anchor survival on the same 626 traces:

|  | Kev preserved | Kev lost |
|---|---:|---:|
| ATIF preserved | 71 | 18 |
| ATIF lost | 109 | 428 |

At least one consumer preserves the exact snippet for 198/626 traces.

## By benchmark (source-verified anchors)

| benchmark | family | n | ATIF preserved | Kev preserved |
|---|---|---:|---:|---:|
| terminal-bench-2 | development | 481 | 59 (12.3%) | 47 (9.8%) |
| swe-bench-verified | heldout | 97 | 0 (0%) | 96 (99.0%) |
| swe-smith | development | 15 | 2 (13.3%) | 12 (80.0%) |
| cybench | heldout | 17 | 15 (88.2%) | 16 (94.1%) |
| swe-rebench | heldout | 8 | 8 | 8 |
| kernelbench | development | 3 | 1 | 0 |
| mle-bench | development | 3 | 3 | 0 |
| bountybench | development | 2 | 1 | 1 |

The family contrast is a benchmark/envelope effect, not a generalization
claim: held-out Kev preservation is high because SWE/CyBench snippets survive
raw rendering, while held-out ATIF preservation is dragged to zero on
SWE-bench-Verified by the converter's lossy projection. Development is
dominated by Terminal-Bench-2, where both consumers lose most exact snippets.

By localization kind, `line_span` survives better than `localized_snippet`
in both consumers (8/13 vs ATIF 81/613 and Kev 172/613), on a very small
`line_span` denominator.

## The Kev positive-window nuance

Exact-snippet loss does **not** mean a Kev trace has no labelled positive
window. Of the 446 Kev-lost anchors, **444 still have at least one
positive-labelled chunk**. Kev's positive label means a window overlaps the
localization at field/window level after rendering and clipping (single tool
outputs are truncated at 4,000 chars; windows are ~5,000 tokens). It does
not guarantee that the first 200 snippet characters are present verbatim.

Of the 180 Kev-preserved anchors, 146 were found in a positive-labelled
chunk; the rest were found only in unknown-label chunks (attempt traces keep
null chunk labels by design, and non-overlapping windows of cheating traces
are unknown rather than negative).

Practical consequence: Kev positive windows may still be usable for coarse
window classification, with that caveat stated. They must not be sold as
exact-evidence supervision. For a critic that has to cite the evidence it
used, the safe CheatBench subset from this run is the source-verified +
consumer-preserved set (ATIF 89, Kev 180, union 198), projected blind.

## Blinding validation

- Existing materialized ATIF: 4,268/4,268 measured documents fail
  `find_forbidden_json`, exactly the three converter metadata keys per
  document. The 204 excluded rows have no ATIF and stay `unknown`.
- `blind_atif` over the same 4,268 documents: **0** forbidden hits after
  projection; flattened evidence text unchanged in **4,268/4,268**.
  (Materialized detector ATIF has no viewer annotation step; dropping
  annotation steps and label-mutation invariance are covered by unit tests.)
- Kev `state` text: 0 traces with the CheatBench annotation marker.

Adopting `blind_atif` at the QA-bundle boundary changes investigator-visible
bytes, so any cached CheatBench QA-trace verdicts produced from whole-ATIF
bundles are stale for the blind input and need an owner-run blind repeat
before being quoted as blind scores. That integration is QA Trace's active
lane and was not edited here.

## What this makes the benchmark useful for

1. **A gate, not just a corpus.** Any new trace exporter, bundle builder, or
   critic-training set can be required to pass two checks before use:
   no evaluator fields in actor-visible artifacts, and a reported
   source-verified anchor-survival table by benchmark/envelope. Unknowns
   stay visible in the denominator.
2. **A small exact-evidence subset.** The 198 traces preserved by at least
   one consumer (with snippet hashes and consumer offsets in the audit
   JSONL) are the defensible starting point for evidence-cited critic
   supervision or evaluation. The other 428 source-verified traces are
   `unknown` for exact-quote purposes in these two consumers, however their
   coarse labels are used.
3. **A concrete QA-trace fix.** `blind_atif` + `find_forbidden_json` give the
   bundle boundary a tested projection and regression check, instead of
   relying on a renderer happening not to print metadata today.

It does not make CheatBench a plug-and-play training set, and it does not
measure any detector or agent. No breaker agent, SFT run, or QA score is
claimed from this audit.

## Limits

- Preservation means the first 200 snippet characters appear verbatim under
  five fixed exact-substring variants. Semantic evidence can remain after
  restructuring or clipping even when this audit says `lost`; conversely a
  short snippet (minimum 7 chars) can match by coincidence within its own
  trace. Read `lost` as "exact quote not available to this consumer".
- ATIF text is the structured string content of steps/tool calls/
  observations (metadata and span side-channels excluded), not a rendered
  transcript. Kev text is the `state` field only.
- The QA Trace bundle itself was not re-audited as a third full consumer;
  the observed bundle leak motivated the library boundary, and bundle
  coverage/resolution remains that lane's work.
- Derived inputs (Parquet, materialized ATIFs, Kev chunks) live outside git
  in the primary checkout's `derived/` tree. The runner records their paths
  and the Parquet hash; on a machine without them it reports unavailable and
  exits nonzero.
- Search variants were fixed before the full run. Development diagnostics
  were used only to add source verification (7 anchors moved from possible
  loss to `unknown`); held-out families were not used to choose variants.

## Checks run for this deliverable

- `uv run --no-sync pytest tests/test_cheatbench_fidelity.py -q` — 12 passed.
- `uv run --no-sync ruff check` on the new module, tests, and runner — clean.
- `uvx ty@0.0.71 check src/evallab/cheatbench_fidelity.py` — clean.
- Full audit above (35 s wall after source verification was integrated),
  plus the full-corpus `blind_atif` validation reported here.
