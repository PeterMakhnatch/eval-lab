# TT-7: immutable post-merge continuation attempt

## Question and answer

Could the two predeclared Claude readers finish the remaining frozen sample once the parent observed new subscription headroom?

**Not in this campaign. No new model calls were dispatched.** The fresh parent observation at **2:44:30 AM ET, October 28** showed five-hour 19% and weekly 53%, but the actual runner's later bounded refresh could not obtain usable telemetry. Both fixed queues stopped before inference. The resulting full-200 report reuses the original measurements unchanged; it is not new evidence of reader capability, model negatives, or exhausted allowance.

This is a separate campaign from [the merged original experiment](../trace-tools-tt7-20261027/README.md), commit `25f63a3c28a3e02474eeb3014715158ba2b20493` (PR #1307). That directory's Git tree remains `c1cd22eef0da5da24486ead7c72e6f3266a74bb2`. Neither its source nor its finalized reports/private raw receipts was overwritten.

## What ran

- New topic branch/worktree: `trace-tools/bakeoff-continuation-20261028` / `.worktrees/trace-tools-bakeoff-continuation-20261028`.
- New public/private namespaces: this directory and `~/.local/state/evallab-prod-live/trace-tools/tt7-continuation-20261028/`.
- The original fixed 200 runs, file order, form, prompts, decision rules, contexts and chunk policy were retained. The private baseline terminal roster admits **only the 125 previously unattempted positions per route**. All earlier 75 terminal cells per route—including refusals and policy abstentions—are ineligible for another call.
- `common.py` and `canonical_secret_gate.py` are byte-identical to their pinned original sources. `run_omp.py` changes only its private namespace, conservative CLI bounds, and frozen eligible-pending intersection. Its actual model-call, rendering/chunking and form-reading functions are unchanged.
- Admission requires authentic telemetry no older than **120 seconds**, both five-hour and weekly windows **below 65%**, concurrency/batch size at most two, and no fallback. The hard ceiling remains 70%. No Luna, Grok, paid route, OpenRouter/BYOK or Laminar call was attempted.
- Uniform pre-call privacy audit: **200 inputs, 45,564,204 rendered bytes, 13,957 steps; the same four policy flags; 196 safe-eligible only as a secondary descriptive view**. All newly eligible positions passed the unchanged canonical gate. No new hosted upload or remote mutation occurred.

### Observed execution

| Operation | Result | Elapsed |
|---|---|---:|
| Isolated locked `uv sync --frozen --python 3.12` | 120 packages installed; no cloned virtualenv | 6.62 s |
| `campaign.py prepare` | Original 75 + eligible 125 per route; all200 privacy audit; zero calls | 19.77 s CLI / 19.527 s audit |
| Guarded `run_omp.py run` | Both queues fail closed before dispatch | 6.26 s |
| One bounded read-only quota diagnostic | Exit0, zero reports, one Anthropic/OAuth account without usage, no disabled credentials, stderr0 bytes | 1.596 s |
| `campaign.py close` | Immutable combined snapshot and terminal partial receipt | Recorded in command receipt |
| `finalize.py && score.py --readings-closed` | Terminal gate, then local-only label join and all aggregate views | 1.87 s |

Runner dispatch was attempted at **3:02:26 AM ET**, with route stops at **3:02:29** and **3:02:32 AM ET**. The retained raw guard reason is `usage snapshot older than 120 seconds; fail closed`. The diagnostic at **3:05:07 AM ET** clarifies that the CLI returned **no usage reports**. It exposes no HTTP/auth/provider failure cause. A refresh-rate-limit or authentication explanation would be inference, not an observation. The campaign closed at **3:07:34 AM ET**.

There were four quota-helper CLI reads and two local cache invalidations in the original bounded guards, followed by the one read-only diagnostic. There was no unbounded retry, TTL change, global configuration change, inferred headroom, or model call while usage was unknown.

## Numbers

| Arm | Original terminal | New terminal | Still unattempted | Reused valid forms |
|---|---:|---:|---:|---:|
| Claude Sonnet 5.5 | 75 | **0** | **125** | 51 |
| Claude Opus 5.5 | 75 | **0** | **125** | 48 |

The combined private snapshot is **1,256,402 bytes**, SHA256 `12865827a22c777ee32ce76c864dffdf1129c7e683adf2ab53b40b1ba693fbfc`, byte-identical to the matching baseline snapshot. Thus zero earlier result rows changed, zero new canonical TT7 rows were appended, and the old 49 provider refusals remain abstentions.

See [score.md](score.md), [score.json](score.json) and its 12 hash-bound replay shards: **19 arm views; 82 scored cheap→frontier replays plus two unavailable same-measurement policy records, accounting for all 84 Cartesian pairs**. Full200, identical paired prefix60 and descriptive safe196 views remain separate. Each arm reports conditional recall/FAR with Wilson intervals, coverage, full-positive detection yield, actual gold-passage intersection, tokens/cost/latency and arm-only positives versus the cascade. Missing measurements remain null, not zero.

Because there are no new readings, substantive primary Goodfire results remain:

| Reused arm | Reading coverage | TP / all80 positives | Full-positive yield | Conditional recall | Conditional FAR |
|---|---:|---:|---:|---:|---:|
| Docent Sonnet | 69 / 150 | 3 / 80 | 3.75% | 3 / 42 = 7.1% | 0 / 27 |
| Subscription Sonnet | 26 / 150 | 0 / 80 | 0% | 0 / 10 | 0 / 16 |
| Subscription Opus | 24 / 150 | 0 / 80 | 0% | 0 / 9 | 0 / 15 |
| Existing Stage1 | 150 / 150 | 60 / 80 | 75% | 75% | 32 / 70 = 45.7% |
| CM9 final alarms | 150 / 150 | 8 / 80 | 10% | 10% | 2 / 70 = 2.9% |

Goodfire DEV drives conclusions; CheatBench remains unchanged cross-test only. Evidence hit means a predicted cited step actually intersects the locally joined gold passage steps—not merely a valid cited ID. See the complete score tables for intervals, evidence and resource denominators. Neither the paired prefix nor the secondary privacy-eligible view selects a model.

## Recommendation and financial basis

**No supported new every-run reader or escalation winner.** The telemetry failure adds no capability evidence. Preserve existing broad Stage1/current routing operationally; that is not an endorsement of CM9 final's 10% recall. Selective coverage, refusals and unpaired conditional rates do not establish a winner.

This attempt used **zero new model calls, zero model input/output tokens and zero incremental inference spend**. The appended spend-ledger batch `tt7-postmerge-guard-20261028` records that narrow fact. It does not recertify original billing or include coding-worker usage.

The reused study retains known 18,498,882 input / 17,121 output tokens. **Whole-study provider-billed/marginal cost remains UNKNOWN**, specifically because the historical credential/auth class and free/included entitlement for `openai-codex/gpt-6-luna` were not retained. Its attested API was `openai-codex-responses`; client-reported $0 and $0.0136769 list equivalent are not a confirmed charge or free entitlement. The original append-only billing annotation remains authoritative. No claim of a verified-$0 whole study or guaranteed free 1,000-run capacity is made.

## Deferred arms and exact unblock conditions

- **Claude continuation:** all125 remaining IDs/model stay unattempted until a genuinely fresh authentic snapshot proves both applicable windows below65%, with unchanged TTL120/concurrency2/hard70 and a separate authorized continuation namespace. No reset/exhaustion claim is inferred from absent telemetry.
- **Docent remaining hosted arms:** the original free weekly window is604800 seconds/cap2500 cents; API/errors did not expose a reset date. Resume only when free allowance is demonstrably restored or funded BYOK is explicitly authorized. The retained uncached OpenRouter list-price scenario,100k input+1k output/run, costs **$72.842625** for the four submitted arms' remaining readings, **$75.152125 including GLM**; the approved-but-unfunded $50 is insufficient for that whole scenario. No OpenRouter calls occurred.
- **Laminar flow1:** unfunded paid Signals; hypothetical200-run uncached scenario **$1.06**, distinct from Starter $30/month. Only allowed Goodfire storage could be used:150=$0.795, privacy-eligible149=$0.7897. CheatBench storage remains prohibited. No paid call occurred.
- **Luna/Codex:** no new call; original weekly reset was November2,6:42:43 AM ET. Any continuation needs verified allowance, financial entitlement and current authorization—not a catalog/client zero.
- **Grok form:** no new call; original deferred reset October30,2:10 PM ET.
- **METR:** only retained matching records are reused. The Kimi preflight originally showed shared five-hour100% and zero model attempts; its reported next reset was October28,3:23 AM ET. This campaign did not wait for it or launch a scanner.

## Reproduction and provenance

Run from the new worktree; no labels are read by preparation, the runner or closure:

```sh
uv run --locked --python 3.12 python research/evidence/runs/trace-tools-tt7-continuation-20261028/campaign.py prepare
uv run --locked --python 3.12 python research/evidence/runs/trace-tools-tt7-continuation-20261028/run_omp.py run --routes anthropic/claude-sonnet-5-5 anthropic/claude-opus-5-5 --max-runs 200 --stop-threshold 0.65 --concurrency 2 --batch-size 2
uv run --locked --python 3.12 python research/evidence/runs/trace-tools-tt7-continuation-20261028/campaign.py close
uv run --locked --python 3.12 python research/evidence/runs/trace-tools-tt7-continuation-20261028/finalize.py
uv run --locked --python 3.12 python research/evidence/runs/trace-tools-tt7-continuation-20261028/score.py --readings-closed
```

These finalized files are immutable evidence, not an instruction to re-execute inference. The finalizer refuses an existing manifest; the scorer requires explicit closure and a fresh output destination within this new public/private namespace. A bounded merged-consumer proof may use `--out ~/.local/state/evallab-prod-live/trace-tools/tt7-continuation-20261028/delivery/merged-main-proof/score.json` without touching committed reports. Only local labels/gold passages are read at scoring.

`campaign.json` pins the original sources; `postmerge-continuation-receipts.json` accounts for every fixed200 cell; `privacy-audit.json` and `quota-diagnostic.json` expose aggregates only. `audit_continuation.py` checks protocol, old-row preservation, pending-order lineage, publication safety and shard hashes offline. No trace text, original IDs, raw labels or gold step lists belong in this public evidence directory.

## Local verification

The offline lineage audit and required focused prepush chain passed in **77.80 seconds**: static gates passed and the existing evidence-reviewability test passed (**1 test, 2.08 seconds**). Exact commands and source hashes are in [checks.json](checks.json); [command-receipts.json](command-receipts.json) retains execution timings without inventing an elapsed value for the interrupted closure command. This checkpoint is not full local-suite/runtime-smoke coverage or GitHub CI green. The final publication audit includes these receipts and all replay hashes.

## Forward-only scoped-cache correction

A subsequent forward-runner correction uses the documented command `omp usage invalidate --provider anthropic`, never positional `invalidate anthropic`. The latter does not bind the documented provider flag; upstream18.12.6 ignores that positional target for invalidation, but the installed handler's exact build was not independently attested. The wrong command was not replayed. Only this forward guard and its offline audit allowlist change; original25f source and the sealed7274 experiment/results remain reproducible through Git, with no historical receipt rewrite.

The one allowed real verification ran **October28,4:43:04 AM ET** through `scoped_attempt.py --resume`: the first read already returned authentic usage fetched **2.49 seconds** earlier—**five-hour73%, weekly60%**. Consequently the unchanged **below65% admission / hard70% / TTL120s** guard stopped before inference. No invalidation was necessary or executed, and valid shared telemetry was not forcibly cleared merely to exercise a branch. There were **zero new model calls**, **125 still unattempted per Claude**, and no reset/account/configuration change. This is a measured threshold stop, not exhausted allowance or unknown telemetry.

The bounded attempt took **0.926364 seconds** internally (**1.24 seconds CLI**). Its exact sanitized argv, fetched timestamp, windows, source/baseline hashes and zero-call outcome are in [the small attempt receipt](scoped-forward-20261028/attempt.json). There is **no copied metric campaign or rescoring**: all19 original arm views/82 scored replays+2 policy records retain their existing report hashes and financial caveats, including unknown historical Luna billing.

`scoped_attempt.py` isolates any separately authorized new measurements into a new public/private namespace, refuses a repeated attempt, verifies unchanged canonical privacy/all200 input hashes before dispatch, and still excludes every old terminal/refusal/privacy case. The scoped-invalidation stale-cache branch is checked offline with controlled CLI responses; it was not claimed as exercised by the live fresh-read stop. A CLI return time is never substituted for the report's `fetchedAt`.

The scoped-fix offline audit and focused prepush/static checkpoint passed in **88.59 seconds**, with **4 tests passed in1.18 seconds**. The three new boundary controls cover provider-scoped stale-cache invalidation, rejection of fast-but-stale CLI results, and refusal of a fresh report missing the weekly window. See [scoped checks](scoped-forward-20261028/checks.json) for exact commands, source hashes, live-versus-offline coverage and the unchanged baseline index hash. No full local-suite/runtime-smoke or pre-CI-green claim is made.
