# B4 receipt: Kimi re-judge of the CM-13 windows — blocked by quota, 0 judge calls

**Blocked, not measured.** The B4 experiment (the 418 frozen CM-13 prompts re-judged once by `kimi-code/k3.1-pro-highspeed`, thinking
low, `omp -p` subscription route) sent **0 of 418 prompts** to the judge. The preregistered Kimi gate stopped before the first call and
on every later probe, so this directory contains no recall, no false-alarm rate, no token figure and no paired comparison. Nothing
here is a negative result about the monitor: an unstarted or refused call is unknown, never a non-detection.

## Pinned freeze (re-checked, not re-created)

| Pin | Value |
|---|---|
| `plan_sha256` | `c35d2b6064b28405a9b7909a08f4d7afe9bc4918f19e64a68e90a124ba75cd1b` |
| `prompts_sha256` | `fcc18e44b4779f55b98a064fdf3f7a91c62dd87ae9c8aa2adfac9bee504e0a1a` |
| Unique prompts | 418 (armed A production windows, B facet windows; C is facet-first over the same keys) |
| Freeze file | `results/freeze.json`, copied from the CM-13 results directory |

The copied `plan.json` and `prompts.json` are byte-identical to the CM-13 freeze; the judge, windows and system prompt were not
touched.

## Admission (strict DEV cross-key, run before any prompt)

`tools/cm19_kimi.py smoke` calls the unchanged CM-13 `admit()` (strict `assignments.csv` cross-key through `CorpusRegistry`) and
refuses to proceed unless the frozen plan's runs equal the admitted cohort. Observed at 2026-10-28T23:30:35Z, 0 model calls:

- runs 150, strata `enacted_yes` 80 / `clean_no` 70, 200 assignment rows cross-keyed;
- plan windows 418 = 418 unique frozen prompts (plan prompts are a subset of the frozen prompt set);
- result: `results/smoke.json`.

## Quota stop (the reason there is no measurement)

`omp usage --json --provider kimi-code`, gate stop at 85% of any window. Exact observation from the same 0-call smoke run
(2026-10-28T23:30:35Z):

| Window | Used | Status | Resets (UTC) |
|---|---|---|---|
| `5h` | 100% | `exhausted` | 2026-10-29T03:23:00Z (11:23 PM ET) |
| `limit_month_total` | 84.93% | `ok` | 2026-11-24T00:00:00Z (Nov 23, 8:00 PM ET) |
| `limit_month_code` | 84.19% | `ok` | 2026-11-24T00:00:00Z |

Gate outcome at that probe: `quota_85_percent_stop`. The first attempted call at 17:21 ET was refused the same way, when the `5h`
window was already `exhausted` (100%) — no prompt left the machine and `judge-cache.jsonl` was never created.

Why this cannot be waited out this evening: the slice needs ~1.9M route-reported input tokens (418 prompts at roughly the CM-13 terra
/ CM-19 Opus per-call scale); the monthly window is ~56M tokens (1% ≈ 0.56M), so the remaining headroom of about 0.07 points is
~0.04M tokens (≈ 8 calls). The `5h` reset at 23:23 ET frees nothing on the monthly window, and the monthly window resets
2026-11-24T00:00Z. Kimi headroom was shared with slice B3, which had priority and consumed it.

## What was exercised, and what was not

| Command | Status |
|---|---|
| `prepare` | ran: copied the CM-13 plan/prompts to `comparisons/cm19-kimi/` and re-checked both sha256 pins |
| `smoke` | ran twice: admission guard passes, real quota probe stops the gate (`results/smoke.json`) |
| `run` | invoked once at 17:21 ET with `--limit 1`: the gate stopped it before any model call |
| `score` | never run (no Kimi judgments exist to score) |

## Deviation from the preregistration, stated plainly

No deviation in design: no prompt was edited, no model was substituted, no cap was raised, no partial pass was scored and no re-roll
happened. The slice did not get to make its single preregistered pass; that is a resource outcome, not a measurement. The only change
to the inherited CM-13/CM-19 code is that the Kimi gate records every window of the report (the Anthropic and Codex gates raised on
the first hot window and left no receipt) and adds a no-call `smoke` subcommand.
