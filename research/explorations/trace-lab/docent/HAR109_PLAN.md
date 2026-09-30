# HAR-109 item-5 Docent analysis plan (PREPARED, not run)

One-command analysis of the 10 HAR-104 Python runs once they are uploaded
and Peter's hand labels are frozen.

## Exact command

```sh
keys run -- uv run --no-project --python 3.12 --with docent==0.1.87 \
  python research/explorations/trace-lab/docent/har109_docent_analysis.py \
  --collection <HAR-104-COLLECTION-UUID>
```

Prerequisites: (1) `export_har104_upload.py --no-dry-run` has created
`HAR-104 Python exploration (private)` and uploaded the 10 runs with blind
metadata (`stop_reason`, `verdict`, `domain`); substitute its collection UUID
above. (2) Hand labels frozen. The script blocks on `per_run.results`, so
with auto-approve ON it runs to completion; with manual approval, approve
each phase in the dashboard when prompted.

## What it does (upstream style: `plan_markdown` first)

1. DQL lists the 10 runs (trial, task, verdict, stop_reason).
2. Template reading, `openai/gpt-5.6-luna`, one prompt per run: first-failure
   step window + quoted evidence, what the model tried, task fairness with
   evidence, harness/model/task_or_grader/unclear attribution with evidence.
   Blind context only: `trace_lab.{trial,task,reward,stop_reason,verdict,domain}`.
3. DQL aggregates reading outputs over `reading_results` /
   `reading_result_links` (attribution x verdict x stop, mean window width).
4. Synthesis reading over the 10 results: 5-8 snake_case failure modes.

Analysis-plan URL is the evidence link; scoring follows `har109_scoring.md`
(step windows, never exact steps; deterministic rules outrank readings).

## Token and cost estimate

Measured proxy: the 12 HAR-81 code-domain trials (same task family shape as
HAR-104 Python tasks) have normalized transcripts of mean 494 KB / max 889 KB
(~125K / ~222K tokens at 4 chars/token). For 10 runs:

- Per-run reading input: ~10 x ~125K ≈ 1.25M tokens (+ ~1K prompt each).
- Synthesis input: ~10 x ~800 output tokens ≈ 8K.
- Output: ~10 x ~800 + ~2.5K synthesis ≈ 11K tokens (caps: 1500/run, 2500 synthesis).
- DQL steps: zero model tokens.

Pricing/quota check 2026-09-30: `get_reading_models()` (1,219 entries,
`openai/gpt-5.6-luna` hosted, 1.05M context, `uses_byok=false`) exposes no
price, free/paid flag, or quota endpoint, so no list-price cost can be quoted
from the SDK. Hosted reading models were free within weekly limits on
2026-09-29 (44-run reading, 2.35M input tokens, no quota message). Expected
spend: **$0** under the same free hosted quota; re-check the dashboard quota
before running, and record the actual token counts from the plan results.

## Freeze

- Scoring map: `docent/har109_scoring.md`
  `sha256:07612c79a6c6739e15b1e00226eedc959b734f9e05ae2ae652fa6f45948cb3d2`
- Plan script syntax-checked (`py_compile`): not executed, $0 spent.
- Any prompt/schema change invalidates the freeze: re-hash and re-record here.
