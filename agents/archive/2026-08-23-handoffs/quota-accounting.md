Status: done
Last: merged as PR #64 (`8cda33b`)
Next: none
Blockers: none

# QUOTA — measure subscription consumption instead of imaginary dollars

Lease honoured: `src/evallab/quota.py`, `tests/test_quota.py`,
`docs/quota-accounting.md`, this file. **Nothing else touched.** No edit to
`src/evallab/queue.py`, `src/evallab/cli.py` or `policy/` — confirmed with
GateAuthorization over IRC before writing; they are sole writer there.

No paid agent executed. No `codex` or `claude-code` invocation, no cloud
sandbox, no API-key environment variable, no network call. No
`docker compose up/down`. Catalog untouched and confirmed unchanged: 72 jobs,
23 `trajectory_documents`, before and after.

## The finding, leading with the correction

Both framings this mission started from were wrong in the same direction: the
artifacts already contain far more than expected.

1. **Consumption is observable in detail.** Real token counts per trial in
   `result.json` → `agent_result.{n_input_tokens, n_cache_tokens,
   n_output_tokens, cost_usd}`, plus `agent_info.model_info.name`
   (`gpt-5.6-terra`) and phase wall clock. `n_input_tokens` is **inclusive of**
   `n_cache_tokens` — verified by reproducing `harbor view runs --jobs` column
   arithmetic from artifacts alone.
2. **Remaining allowance is observable too, where a rollout survives.** Codex
   attaches a `rate_limits` snapshot to its `token_count` event, and Harbor
   copies the rollout out of the container to
   `<trial>/agent/sessions/YYYY/MM/DD/rollout-*.jsonl`. 137 snapshots across 17
   of the 33 trials. Latest: `used_percent` 92.0, weekly window (10080 min),
   `resets_at` 2026-08-20T18:32:49Z, `plan_type` `prolite`, `credits.balance`
   `"0"` with `has_credits`/`unlimited` false.
3. **`credits.balance` `"0"` means 100% is a lockout, not a bill.** Exhausting
   the window blocks every paid agent until Thursday. That changes the risk
   profile of unattended runs from "costs money" to "locks him out", and it is
   surfaced as `Headroom.hard_stop`.
4. **The nightly did not cause the 92%.** Full timeline table with sources is in
   `docs/quota-accounting.md`. Summary: the counter read 70 both immediately
   before and throughout all nine 2026-08-15 trials, and 71 at the next reading
   9h26m later — a window containing the whole nightly and no other reading. It
   then went 71 → 91 during interactive use that evening, hours after the
   nightly. Stated precisely, because the counter's resolution is 1 percentage
   point and the window is 7-day rolling: **the nightly's contribution was not
   detectable at this counter's resolution and cannot account for more than 1 of
   the 22 points.** Not "consumed nothing"; not "caused the 92%".

The authorization defect is unaffected — unattended paid execution against a
subscription with a hard stop and no quota-aware ceiling is still wrong. Only
the magnitude changes.

`~/.codex/auth.json` holds no quota information. Field names only, values never
read or printed: `OPENAI_API_KEY`, `tokens.{id_token, access_token,
refresh_token, account_id}`, `last_refresh`. No key matches
`quota|limit|usage|rate|remain|credit|balance|plan`. Verified before pushing
that no fragment of that file appears in any committed file, log, or report.

The Codex CLI does expose a live quota surface (`account/rateLimits/read`,
`/api/codex/rate-limit-reset-credits`), found by scanning the installed binary's
printable strings — the binary was never executed. Reading it live is an
authenticated request with the Sponsor's OAuth token, so the module does not.

## What was built

`src/evallab/quota.py` — `Headroom` (what remains, scope account) and
`ConsumptionLedger` (what the lab used, scope lab) as separate types, because
conflating them is the original defect one layer up. Every quantity carries
`[observed]` / `[unavailable]`, matching `evallab.status.Availability`. Nothing
is estimated. `python -m evallab.quota [--json] [ROOT ...]` reads it from the
command line without any `cli.py` edit.

Boundaries, each asserted by a test: reads job directories only, never
`~/.codex`, the Keychain, the catalog, the network, or the clock (`now` is
injected, per the deterministic-test rule in `agents/CHECKS.md`); parses only
`payload.rate_limits` and token counters, never message text; imports nothing
from `queue.py` or `cli.py`.

`uv run pytest` full suite and `uv run ruff check .` both clean, run once at the
end as instructed. `scripts/premerge.sh` deliberately not run.

## The ledger over the 33 real trials

11 paid jobs, 33 trials dispatched, **17 with observed usage, 16 with none**.
230,241 uncached input, 2,251,776 cached input (90.7%), 77,247 output, 137 model
turns, 7h41m job wall clock, `reported_cost_usd` 1.8378 (list-price equivalent,
not spend). Per day / task / agent / policy rule / job tables in
`docs/quota-accounting.md`. Per-job figures match `harbor view runs --jobs`
exactly, read from artifacts rather than the viewer.

Two operational facts nothing else in the lab surfaces:

- **16 of 33 paid dispatches consumed nothing.** All 15 trials on 2026-08-14
  died with `ValueError: Model name is required` before reaching the model;
  `transaction-reconciliation__XB3Bbr8` failed installing the CLI. Reported as
  `[unavailable]`, never as zero.
- **`canary-transaction-reconciliation-codex-20260816` ran 5h44m** while
  consuming the fewest uncached tokens of any completed job. Its three trials
  spent 3,720s / 3,464s / 4,098s in `agent_setup` installing the CLI from npm
  inside each container, and the third failed there. The same task the day
  before took 3m36s.

## What the Sponsor still cannot see

- **The lab's share of the percentage.** One account-wide integer, not
  decomposable. Permanently `[unavailable]`.
- **Any tokens-to-percent conversion**, so "how many more trials fit" is
  unanswerable.
- **Whether cached input draws on the allowance at the same rate as uncached.**
  UNVERIFIED — absent from the artifacts and from the published documentation.
  At a 90.7% cache share this is the single assumption most capable of
  invalidating the ledger, so cached and uncached are reported separately and
  never combined.
- **Claude consumption**, entirely. Zero `claude-code` trials; the Keychain path
  exposes nothing comparable. `PAID_AGENTS` is ready and has nothing to report.
- **Quota headroom between nightlies.** A snapshot only exists when a paid trial
  ran, so the figure is as stale as the last paid trial. `staleness_seconds` is
  reported so nobody mistakes it for live.

## For the integrator and follow-up missions — not done here

1. **Wire `Headroom` into `PolicyGate`.** Mechanical; the intended call and its
   two traps are written out in `docs/quota-accounting.md`. Check
   `headroom.availability` before reading `remaining_percent`, because an
   unavailable headroom is `None` and treating `None` as "plenty left" would
   reproduce the defect in a new unit. `since()` drops trials with no recorded
   start, so a window count is a deliberate lower bound. The honest gate this
   supports is a paid-trial/attempt count ceiling plus refusal when `hard_stop`
   is true and `remaining_percent` is low and fresh — **not** a percentage
   budget for the lab, which is unavailable by construction.
2. **Promotion drops the quota signal.** `research/evidence/runs/` excludes
   `agent/sessions/`, correctly, since rollouts hold unredacted prompts that
   `AGENTS.md` forbids committing. The consequence is that the signal is
   discarded at the exact moment evidence becomes permanent, and **this
   investigation is not reproducible from a fresh clone**. A redacted
   `rate_limits` sidecar written at promotion time fixes both; this module's
   parser is already the right shape for it, touching only `payload.rate_limits`.
   Not in this lease.
3. **`agents/STRUCTURE.md` needs a one-line addition** for
   `docs/quota-accounting.md`. That file is not in my lease and another mission
   may hold it this round, so it is recorded here rather than edited. The
   `docs/` submap enumerates its entries since COORD-GC, so leaving it out makes
   the binding map untrue for a `docs/` entry.
4. **The catalog drops it too.** `trials` has `input_tokens`, `cache_tokens`,
   `output_tokens`, `cost_usd` but no quota column. Out of lease; noted because
   a future indexing mission should carry `used_percent` and `resets_at`.
5. **`agent_setup` spends about an hour per trial installing the Codex CLI from
   npm inside each container.** A real operational defect, unowned, not
   partially addressed here.
6. **`agents/CHECKS.md` states the ty ratchet is 33; `.github/workflows/
   typecheck.yml` sets `TY_BASELINE: 28`.** The verification contract is stale
   for a gate it declares. Found because the first push went red at 29: this
   module added exactly one diagnostic, now fixed, and `src/` is back at 28
   which is the baseline. Neither file is in this lease, so this is recorded
   rather than corrected — but CHECKS.md is the document agents are told to
   trust, and it currently understates the gate by five diagnostics.

## Boundary respected on the host session files

The causal correction required reading the Sponsor's own interactive session
rollouts under `~/.codex/sessions`, read-only, outside this repository. Per the
integrator's instruction, only `payload.rate_limits` fields and their timestamps
left that directory. The committed table carries timestamps and `used_percent`
values only. No message text, no prompt fragment, no session title, no session
identifier and no path from that directory appears in any commit, the PR body,
this handoff, or any log. The module itself never reads that directory; it scans
only the job roots it is given, and a test asserts an observation's `source` is
trial-relative so no host path can appear.
