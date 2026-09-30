# Scout predictions (HAR-109, HAR-104 runs) — notes

Tool: Trace Lab Scout rule scanners (`scout/scanners.py` rule family over
`scout/rules.py` → probe-03 `capabilities.py`). Rule-based only, $0, no
model calls, no launches. All commands from the `har109-analysis-stack`
worktree root with
`UV="uv run --with inspect-scout==0.5.3 --with harbor==0.21.0"`
(project env so `evallab` imports resolve). Wall time start
`/tmp/scout_har104_start.txt`; `cost_usd: 0.0` on all rows.

## Exact commands

```sh
# 1. Import 10 HAR-104 trials into a NEW db (HAR-81 db untouched)
uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 -- python research/explorations/trace-lab/scout/import_evallab.py \
  ~/Developer/eval-lab/.worktrees/har104-runs/runs/har104-d-000226 \
  .../har104-d-000383 .../har104-d-000927 .../har104-d-001832 \
  .../har104-d-001896 .../har104-d-002256 .../har104-d-002259 \
  .../har104-d-002391 .../har104-d-002407 .../har104-d-002864 \
  --db ~/Developer/eval-lab/derived/trace-lab/scout/har104/transcripts \
  --staging ~/Developer/eval-lab/derived/trace-lab/scout/har104/staged
# resolved 10 trial dirs; inserted 10 transcripts (10 staged files carry tool_calls)

# 2. Rule scan (4 text + 6 rule scanners)
uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 -- scout scan \
  research/explorations/trace-lab/scout/scanners.py \
  -T ~/Developer/eval-lab/derived/trace-lab/scout/har104/transcripts \
  --scans ~/Developer/eval-lab/derived/trace-lab/scout/har104/scans --display plain
# -> scans/scan_id=bkQuujxvA3MYYgz2n8BtZt (10 transcripts x 10 scanners, errors: [])

# 3. Predictions built by /tmp/scout_har104_predict.py via
# rules.analyze_trial_rules(trial_dir, evallab_src=<har109-analysis-stack>/src)
# -> predictions/scout.jsonl (10 rows)

# 4. Throwaway view check on port 7583 (stopped afterwards; port-7576 untouched)
uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 -- scout view \
  -T .../har104/transcripts --scans .../har104/scans \
  --host 127.0.0.1 --port 7583 --no-browser --display none
curl root -> 200; POST /api/v2/transcripts/<base64url-dir> -> 10/10 items with
task_id (mimo-v2.6-rl/format-code-task-<id>), score, error/limit;
GET /api/v2/scans/<dir>/<scan> -> complete:true, 10 transcript ids.
Server killed; port 7583 closed.
```

## evallab_src

Predictions pass the current-stack `src` explicitly as `evallab_src`
(stock-recorded runs). Separately verified
(`/tmp/scout_har104_dump.py`): `analyze_trial_rules` with current src vs
default HAR-81 src gives byte-identical `first_failure/outcome/handshake/
loops/wedge/stop/verifier` on all 10 trials — the stock shape passes the
normalizer through — so the `scout scan` rule values (default src) and
`scout.jsonl` (current src) agree. Cross-checked: scan `rule_*` values via
`scan_results_df` match the direct computation on all 10 trials.

## Schema mapping used

- `first_failure_step/what`: all 10 trials have `first_failure: none`, so
  per the mapping this falls back to the outcome rule's evidence step:
  `outcome.step_ref` if set else `evidence_step_refs[0]` (numeric `#` part),
  `what` = outcome `rule_id`.
- `attribution`: outcome attribution; probe-03 `n/a` (R-NONE-01) mapped to
  schema `none`; anything outside the schema enum would be `null` (none seen).
- `loop_present/span`: `rule_loops` spans; span = the longest span
  (matches the scanner's numeric value). `loop_command`: null (see below).
- `stop_reason`: `ceiling:input_tokens/output_tokens/total_tokens` →
  `token_ceiling`; `ceiling:requests` → `call_ceiling`;
  `task_complete_confirmed` → `model_finished`; `agent_timeout` → `timeout`;
  `unknown` + exception → `error`; else `other`.
- `completion_claimed/confirmed`: handshake present / `handshake.confirmed`.
- `task_verdict`, `pass_suspect`, `upstream_fetch`: null — the tool has no
  notion of these (capabilities computes `pass_caveats`, but
  `rules.analyze_trial_rules` intentionally omits them).
- `source`: scan id + per-dimension rule ids/reasons (exact scan/field
  provenance per row).

## What the tool couldn't express / judgment calls

- `loop_command` is null: loop spans carry only kind/length/step-ref pairs,
  no command text. `scout.jsonl` does not backfill from trajectory reading.
- `har104-d-002864`: the longest span (78) is kind `confirmation`
  (`head#25→head#102`), not the identical run (75, `head#28→head#102`).
  `loop_span` reports the confirmation span to stay consistent with the
  `rule_loops` value; the kind is recorded in `source`.
- When `first_failure` is none, `first_failure_what` carries the *outcome*
  rule id (e.g. `R-NONE-01`) — the null-vs-outcome distinction is visible
  only via `source`, not via a dedicated field.
- `upstream_fetch` is null even though probe-03 has guard/nop machinery;
  the shared `rules.py` output simply doesn't surface a fetch flag.

## Bugs / rough edges (Eval Lab gap list candidates)

- `scout view` serves data only via `POST /api/v2/transcripts/<base64url-dir>`
  and `/api/v2/scans/...` (`server.py:148` mounts `/api/v2`); the CLI help
  documents only `-T/--scans`, and `openapi.json` is an empty FastAPI
  placeholder (`{"paths":{}}`). Found the routes by reading
  `inspect_scout/_view/server.py` + `_api_v2_*.py` in the uv cache.
- `rule_loops` value mixes span kinds: `scanners.py:543`
  (`longest = max(...)` over identical + confirmation spans from
  `capabilities.py` LOOP-COST). By design, but a consumer reading "loop"
  as "identical retry loop" will misread `har104-d-002864` (78).
- `scout scan --display plain` wraps the scan path across lines; scan id had
  to be re-read from the scans dir listing.
- Bare `python` is not on PATH in this environment; everything must go
  through `uv run` (even for stdlib-only probe scripts).
