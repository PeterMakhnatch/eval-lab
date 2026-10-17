# CheatBench consumer fidelity and blinding gate

## Goal

Make the external CheatBench trace corpus safe to use for QA-trace work and
trace-critic training. The question is not "can a detector score well?" It is:

> Does the evidence that justifies the external cheating label survive into
> the exact artifacts a downstream consumer reads, while the label,
> mechanism, rationale, and viewer annotation do **not** leak into that
> consumer's input?

That is the prerequisite for using CheatBench to QA traces or train a critic.
Without it, a score can be inflated by answer leakage, or a training window
can be labelled positive after the decisive evidence was clipped away.

## What is audited

Source key: `steinad/CheatBench` at the pinned revision in
`src/evallab/cheatbench.py`, indexed by committed
`research/calibration/detectors-v1/cb_subset.json` (4,472 rows). Evaluator-only
anchors come from the local pinned Parquet's metadata columns
(`loc_snippet`, field path, char/line ranges); the Parquet `trace` column is
not read by the anchor loader.

Consumers in this slice:

| Consumer | Actor-visible text audited | Blinding check |
|---|---|---|
| `materialized_atif` | Flattened ATIF step messages, tool-call strings, and observation contents from `derived/detector-roots/cb/cb/<id>/agent/trajectory.json` | Whole ATIF JSON is scanned for evaluator fields and annotation text. Current converter metadata is expected to fail this check; `blind_atif` is the proposed safe projection. |
| `kev_windows` | Existing Kev JSONL `state` strings under `derived/kev/chunks/{train,dev,test,external}.jsonl` | `state` must not contain the CheatBench annotation marker. `trace_label`/`chunk_label` are supervised target columns, not part of `state`. |

QA Trace bundle integration is deliberately not edited here (`qa/trace.py`
has an active owner). The library's `find_forbidden_json` and `blind_atif`
are the reusable boundary that lane can adopt.

## Method

1. Load all subset rows. Benign rows have no positive anchor and are never
   counted as evidence-preservation successes.
2. For cheating/attempt rows, load the Parquet anchor and verify it against
   the resolved source trace field. Only `localized_snippet` and `line_span`
   localizations whose snippet is verbatim in that source field (under the
   fixed variants below) are measurable consumer anchors.
   `latent_harness`, `latent_inference`, `unlocalized`, and non-verbatim
   snippets stay `unknown`, not lost and not preserved.
3. Search each consumer's text for the first 200 snippet characters under a
   small set of exact-substring normalizations already used by the converter
   (literal-newline unescaping, pipe-newlines, newline escaping, and JSON
   quote escaping). Audit rows store snippet hashes, never snippet text.
4. Scan actor-visible JSON for forbidden evaluator fields
   (`cheatbench_*`, `loc_*`, top-level/metadata mechanism or rationale keys,
   `extra.cheatbench`, and the annotation marker).
5. Report the existing family split separately: held-out benchmarks are
   `swe-bench-verified`, `swe-rebench`, and `cybench`; all other CheatBench
   benchmarks are development for this audit. Search variants are fixed in
   code before the run and are not tuned on held-out outcomes.

## Acceptance

- All 4,472 subset rows are accounted for in the ATIF audit; materialized
  coverage reconciles with the retained 4,268 converted / 204
  `no_agent_content` receipt when the full local roots are present.
- Every source-verified measurable positive anchor is exactly one of
  `preserved`, `lost`, or `unknown`, with the reason recorded. Missing
  artifacts are `not_run`; non-verbatim source snippets are `unknown`.
- `blind_atif` passes `find_forbidden_json`, preserves evidence/tool text,
  drops annotation steps and span side-channels, and is invariant if the
  original label/mechanism/rationale metadata is mutated.
- Kev coverage reports seen wanted traces, per-anchor chunk counts,
  positive/unknown/negative chunk labels, and whether a preserved anchor was
  found in a positive-labelled chunk.
- The run exits nonzero if a requested input root is unavailable or the
  optional source-hash verification mismatches the PIN.

This does **not** establish detector recall, QA-agent quality, or a training
gain. It validates the data contract those claims depend on.

## Run

From this worktree, after `uv sync --frozen`:

```bash
.venv/bin/python \
  research/experiments/cheatbench-consumer-fidelity-20261017/run_audit.py \
  --verify-source-hash
```

Default derived inputs are read from the primary Eval Lab checkout's ignored
`derived/` tree (they are not copied into git). Override `--subset`,
`--parquet`, `--atif-root`, `--kev-root`, `--out-dir`, or `--summary-out` on
another machine. Use `--limit N` only for a smoke; headline numbers use no
limit.

Outputs:

- `derived/trace-consumer-audit-v1/audit-atif.jsonl` and
  `audit-kev.jsonl` (ignored, full audit rows)
- `derived/trace-consumer-audit-v1/summary.json`
- `results/summary.json` in this directory (compact committed summary)
- `RESULTS.md` in this directory (interpretation of the measured run)

## Why not the adjacent builds?

- Another LLM breaker benchmark is already owned by RH Agents, and QA Task
  already owns the executed S0–S3 task auditor. Rebuilding either would
  duplicate active lanes.
- Another generic CheatBench SFT/window dataset already exists (Kev windows)
  and prior held-out family transfer was weak. The missing piece is proving
  that supervision evidence and blinding survive the projection.
- The breaker ladder floor is useful deterministic evidence, but its
  hardened-side oracle controls fail in the local emulation, so those rows
  cannot be promoted into certified RL-safe negatives without a separate
  qualification step.
