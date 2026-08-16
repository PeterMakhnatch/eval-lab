# Subscription quota accounting

Why the lab's cost model measures the wrong thing, what is actually observable
instead, and the model `src/evallab/quota.py` implements.

Status: the measurement exists and is proven over real history. Wiring it into
`PolicyGate` is a separate mission; nothing in this document changes what the
lab is authorised to run.

## The defect being closed

The lab denominates cost in dollars — `est_cost_usd` per experiment,
`daily_cost_ceiling_usd` and `per_job_cost_ceiling_usd` in
`policy/standing-approvals.yaml` — but its paid agents authenticate from a
personal subscription. Codex reads `~/.codex/auth.json`
(`src/evallab/runner.py`), and Claude goes through `scripts/with-claude-auth`
and the Keychain. No dollars move when a paid trial runs.

So every dollar figure in this repository is an API-list-price *equivalent*,
including Harbor's own per-trial `agent_result.cost_usd`. A ceiling denominated
in that unit cannot bind, because the quantity it limits is not the quantity
being spent. The binding constraint is a subscription rate-limit window.

## What is observable, established before designing anything

Four candidate sources were checked. The order matters: the answer changed
twice, and both earlier framings were wrong in the same direction — the
artifacts already contain far more than expected.

### 1. `~/.codex/auth.json` — no quota information

Field names only, values never read, printed, or committed:

```
OPENAI_API_KEY        (null on this workstation)
tokens.id_token
tokens.access_token
tokens.refresh_token
tokens.account_id
last_refresh
```

No key matches `quota|limit|usage|rate|remain|credit|balance|plan`. The
credential file is a credential file. **Not a quota source.**

### 2. The Codex CLI — a quota surface, but it is a network call

A static scan of the installed binary
(`@openai/codex@0.147.0`, `vendor/aarch64-apple-darwin/bin/codex`) shows a
first-class rate-limit surface: a `RateLimitSnapshot` type with
`limit_id / limit_name / primary / secondary / credits / plan_type`, a
`RateLimitWindow` with `used_percent / window_minutes`, and backend endpoints
`account/rateLimits/read` and `/api/codex/rate-limit-reset-credits`.

Reading it live means an authenticated HTTP request to OpenAI's account API
using the Sponsor's OAuth token. That is a network call with his credential, so
it is **out of scope for the lab's accounting** and this module does not do it.
It is, however, the reason the next source exists.

### 3. A completed trial's own artifacts — the real quota signal

The Codex CLI attaches that snapshot to the `token_count` event it writes into
its session rollout. Harbor copies the container's session tree out to
`<trial>/agent/sessions/YYYY/MM/DD/rollout-*.jsonl`. The provider's own
percentage is therefore **already on disk**, recorded at the moment each trial
ran, obtainable with no paid call, no network access and no API key:

```json
{"limit_id": "codex", "limit_name": null,
 "primary": {"used_percent": 92.0, "window_minutes": 10080, "resets_at": 1787250769},
 "secondary": null,
 "credits": {"has_credits": false, "unlimited": false, "balance": "0"},
 "individual_limit": null, "spend_control_reached": null,
 "plan_type": "prolite", "rate_limit_reached_type": null}
```

137 such snapshots exist across 17 of the 33 committed Codex trials.

### 4. A completed trial's own artifacts — token counts

Per trial, `<job>/<trial>/result.json`:

| Field | Meaning |
|---|---|
| `agent_result.n_input_tokens` | input tokens, **inclusive of cached** |
| `agent_result.n_cache_tokens` | the cached portion of the above |
| `agent_result.n_output_tokens` | output tokens |
| `agent_result.cost_usd` | API list-price equivalent, **not** spend |
| `agent_info.model_info.name` | the model actually used (`gpt-5.6-terra`) |
| `agent_setup` / `agent_execution` / `started_at` / `finished_at` | phase wall clock |

Duplicated in `<trial>/agent/trajectory.json` under `final_metrics`
(`total_prompt_tokens`, `total_completion_tokens`, `total_cached_tokens`,
`total_cost_usd`, `total_steps`, `extra.reasoning_output_tokens`). Aggregated
per job in `<job>/result.json` under `stats`, and the launch command plus the
authorising `experiment.policy_rule` sit in `<job>/lab-metadata.json`.

`n_input_tokens` being inclusive of `n_cache_tokens` was verified against
Harbor's own `harbor view runs --jobs` renderer, whose input column equals
`n_input_tokens - n_cache_tokens`. This module reproduces that renderer's
figures from the artifacts alone.

### What remains unobservable

- **The lab's share of the percentage.** The provider reports one integer for
  the whole account. It cannot be decomposed, so `lab_attributable` is
  permanently `[unavailable]`.
- **Any conversion between tokens and percent.** The window's size in tokens is
  not published, so "how many more trials fit" is unanswerable.
- **Whether cached input draws on the allowance at the same rate as uncached
  input.** UNVERIFIED — not in the artifacts, not in the published
  documentation. Cached and uncached input are therefore reported separately and
  never combined into one consumption number. On real history the cache share is
  90.7%, so a wrong assumption here would move the ledger by an order of
  magnitude.
- **Claude consumption, entirely.** Zero `claude-code` trials exist, and the
  Keychain path exposes no comparable signal. `PAID_AGENTS` includes
  `claude-code` so the ledger is ready, but it has nothing to report.
- **Quota after promotion.** `research/evidence/runs/` excludes
  `agent/sessions/`, correctly, because rollouts contain unredacted prompt text
  that `AGENTS.md` forbids committing. The consequence is that promotion
  discards the quota signal at the exact moment evidence becomes permanent, and
  this investigation is not reproducible from a fresh clone. A redacted
  `rate_limits` sidecar written at promotion time would fix both.

## The model

`src/evallab/quota.py` keeps two questions apart, because conflating them is the
original defect one layer up.

```
Headroom            what remains          scope: account       provider-reported
ConsumptionLedger   what the lab used     scope: this lab      derived from our trials
```

`Headroom` leads, because it is the constraint that binds. It carries
`used_percent`, `remaining_percent`, `window_minutes`, `resets_at`,
`observed_at`, `staleness_seconds`, `plan_type`, `limit_id`, the credits block,
and `hard_stop`. **`hard_stop` is the field that changes the risk profile**: with
`has_credits` false, `unlimited` false and `balance` `"0"`, reaching 100% is not
an extra charge, it is a lockout for every paid agent until the window resets.
When no snapshot exists, every field is `None` and `availability` is
`unavailable` with a reason — never a zero, never an estimate.

`ConsumptionLedger` holds one `TrialConsumption` per paid trial and groups by
day, task, agent, policy rule and job. Two counters are deliberately distinct:
`paid_trials` (dispatched) and `trials_with_observed_usage` (left a usage
record). A trial with no record is `[unavailable]`, never zero; its
`exception_type` is reported so a reader can judge why, and the module does not
guess on their behalf.

`counter_resolution_percent()` reports the floor of the provider's counter — 1.0
percentage point on all 137 observations. Consumption below one point of the
window registers as no movement at all, which is why a zero delta is evidence of
"not detectable", never of "consumed nothing".

Provenance follows the existing operator convention exactly: `[observed]` /
`[unavailable]`, the same vocabulary as `evallab.status.Availability`.

### Boundaries the module keeps

- Reads Harbor job directories only. Never `~/.codex`, the Keychain, the
  catalog, the network, or the wall clock — `now` is injected, which is what
  makes the tests deterministic per `agents/CHECKS.md`.
- Parses only `payload.rate_limits` and the token counters out of a rollout,
  never message text, so its output is safe to commit even though the file it
  parsed is not. `tests/test_quota.py` asserts that prompt-shaped payloads
  cannot reach the report and that an observation's `source` is trial-relative.
- Imports nothing from `queue.py` or `cli.py`. Measurement does not authorise.

## The ledger over real history

`uv run python -m evallab.quota <runs-root>` over the 33 committed Codex trials.
Per-job figures match `harbor view runs --jobs` exactly, read from artifacts
rather than from the viewer.

Remaining, latest observation `2026-08-16T14:00:31Z`:

| Field | Value | Provenance |
|---|---|---|
| `used_percent` | 92.0 | `[observed]` |
| `remaining_percent` | 8.0 | `[observed]` |
| window | 10080 minutes (7 days) | `[observed]` |
| `resets_at` | 2026-08-20T18:32:49Z | `[observed]` |
| `plan_type` / `limit_id` | `prolite` / `codex` | `[observed]` |
| `credits.balance` | `"0"`, no credits, not unlimited | `[observed]` |
| `hard_stop` | **true** — 100% locks out, it does not bill | `[observed]` |
| counter resolution | 1.0 percentage point | `[observed]` |
| lab's share of the percentage | — | `[unavailable]` |

Consumed by the lab, all 11 paid jobs and 33 paid trials:

| Quantity | Value |
|---|---|
| paid jobs / trials dispatched | 11 / 33 |
| trials with observed usage | 17 |
| trials without usage evidence | 16 |
| model turns | 137 |
| uncached input tokens | 230,241 |
| cached input tokens | 2,251,776 (90.7% of input) |
| output tokens | 77,247 |
| job wall clock | 7h41m |
| longest single trial | 2h28m |
| `reported_cost_usd` | 1.8378 — list-price equivalent, not spend |
| exceptions | `ValueError` 9, `NonZeroAgentExitCodeError` 7 |

Per day:

| Day | Trials | With usage | Turns | Uncached in | Cached in | Out | Wall |
|---|---|---|---|---|---|---|---|
| 2026-08-14 | 15 | 0 | 0 | `[unavailable]` | `[unavailable]` | `[unavailable]` | 16m19s |
| 2026-08-15 | 9 | 9 | 67 | 113,176 | 1,176,832 | 40,375 | 32m15s |
| 2026-08-16 | 9 | 8 | 70 | 117,065 | 1,074,944 | 36,872 | 6h52m |

Per task:

| Task | Trials | With usage | Turns | Uncached in | Cached in | Out | Wall |
|---|---|---|---|---|---|---|---|
| `local-lab/event-summary` | 9 | 6 | 32 | 44,126 | 416,768 | 4,660 | 13m12s |
| `petermakhnatch/transaction-reconciliation` | 12 | 5 | 27 | 21,234 | 262,144 | 2,716 | 5h53m |
| `terminal-bench/html-js-filter` | 12 | 6 | 78 | 164,881 | 1,572,864 | 69,871 | 1h34m |

Per agent: `codex` accounts for all 33 trials. Per policy rule: `canary`
authorised all 33.

### Three things the ledger makes visible that nothing else did

**Half the paid dispatches consumed nothing.** All 15 trials on 2026-08-14 died
with `ValueError: Model name is required` before reaching the model, and
`transaction-reconciliation__XB3Bbr8` on 2026-08-16 failed installing the CLI.
A ceiling that counts dispatches would have counted 33; a ledger that counts
consumption observes 17 — and reports the other 16 as `[unavailable]`, not zero.

**Wall clock and token consumption are decoupled.**
`canary-transaction-reconciliation-codex-20260816` occupied the workstation for
**5h44m** while consuming 6,931 uncached input tokens, the smallest of any
completed job. Its three trials spent 3,720s, 3,464s and 4,098s in
`agent_setup` — installing the CLI from npm inside each container — and the
third then failed there. A sibling job doing the same task the day before
finished in 3m36s. Nothing in the lab surfaces this today.

**The cache dominates.** 2,251,776 of 2,482,017 input tokens were served from
cache. Whether that matters to the allowance is UNVERIFIED, and it is the single
assumption most capable of invalidating the whole ledger.

## Correction: the nightly did not cause the 92%

The brief attributed the Sponsor's 92% Codex allowance to the lab's nine nightly
Codex sessions. The recorded evidence does not support that.

`used_percent` is account-wide, so the same weekly window
(`resets_at` 1787250769) can be read both from the lab's trial rollouts and from
the Sponsor's own interactive sessions. Aligning them:

| # | Time (UTC) | Source | `used_percent` |
|---|---|---|---|
| 1 | 2026-08-15T05:57:02Z | host interactive session | 70 |
| 2 | 2026-08-15T06:30:58Z → 07:02:25Z | in-repo trial rollouts — **all 9 trials, 67 snapshots** | 70, every one |
| 3 | 2026-08-15T15:23:30Z | host interactive session (first host reading after the nightly; **none in between**) | 71 |
| 4 | 2026-08-15T20:33:37Z | host interactive session | 91 |
| 5 | 2026-08-16T09:51:12Z → 14:00:31Z | in-repo trial rollouts — **all 8 trials, 70 snapshots** | 92, every one |

Rows 1 and 3 bracket an interval of 9h26m that contains all nine 2026-08-15
trials and no other reading. Across it the counter moved 70 → 71: **+1
percentage point, shared with whatever else used the account.** The climb the
Sponsor noticed, 71 → 91, happened between rows 3 and 4, hours after the nightly
finished, during interactive use. The 2026-08-16 nightly's own first snapshot
already read 92, so it did not produce that either.

State the bound precisely:

- The counter's resolution is **1 percentage point** (every one of the 137
  in-repo and all host observations is a whole number). Nine agent sessions
  could therefore consume anything up to just under 1% of the weekly window and
  register as no movement.
- The window is a **7-day rolling** one, so it decays. A net movement of +1 point
  is a bound on *net* change, not a gross measurement of what the nightly drew.
- The defensible claim is therefore: **the nightly's contribution to the weekly
  window was not detectable at this counter's resolution**, and it cannot
  account for more than 1 of the 22 points between 70% and 92%. Not "the nightly
  consumed nothing", and not "the nightly caused the 92%".

The authorization defect is unaffected: unattended paid execution against a
subscription with a hard stop and no quota-aware ceiling is still wrong, and
still worth fixing tonight. What changes is the magnitude, and therefore which
fix is urgent.

## Intended integration, not performed here

`PolicyGate` is leased to another mission this round; nothing below is wired.

```python
from datetime import UTC, datetime
from evallab.quota import load_quota_report, default_roots

report = load_quota_report(default_roots(repo_root), now=datetime.now(UTC))

report.headroom.availability      # "observed" | "unavailable"
report.headroom.remaining_percent # None unless observed
report.headroom.hard_stop         # True -> exhaustion is a lockout
report.consumed.since(cutoff).totals().paid_trials  # trials in a window
```

Two properties matter for a gate. `since()` drops trials with no recorded start
rather than assuming they are recent, so a window count is a **lower bound** —
the safe direction. And `headroom.availability` must be checked before
`remaining_percent` is read, because an unavailable headroom is `None`, and
treating `None` as "plenty left" would reproduce the defect in a new unit.

The honest gate this measurement supports is a **paid-trial and attempt count
ceiling**, plus refusal when `hard_stop` is true and `remaining_percent` is low
and fresh. It does not support a percentage budget for the lab, because the
lab's share of the percentage is unavailable and always will be.

## Reproducing

```bash
uv run python -m evallab.quota ../../runs          # text report
uv run python -m evallab.quota ../../runs --json   # same figures as JSON
uv run pytest tests/test_quota.py
```

Field names in `~/.codex/auth.json` were listed with a script that printed keys
and types only. The CLI's rate-limit surface was found by scanning the installed
binary's printable strings; the binary was never executed. The host-session
readings in the correction table above are timestamps and `used_percent` values
only — no message text, no session identifier, and no path from that directory
appears in this repository.
