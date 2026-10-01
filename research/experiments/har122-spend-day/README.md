# HAR-122: Spend Day Ledger & Backfill (2026-09-29 and 2026-09-30)

## Overview

The daily spend ledger (`evallab spend day --date YYYY-MM-DD`) provides unified accounting of all Eval Lab infrastructure and model expenditure per UTC day, reported against the standing $20.00/day spending cap.

Every line item reports an explicit `basis` (`billed`, `estimate`, or `ledger`) so that provider-billed invoices are never mixed up with model rate estimates:

1. **Modal (`billed`)**: Retrieved from the `modal_billing_rows` catalog table populated by the read-only reconcile workflow (`evallab modal billing-reconcile`). Modal bills account-wide GPU/CPU/memory runtime. Because Modal does not tag individual trial containers with card IDs, account billing is attributed to `unattributed`.
2. **Daytona (`estimate`)**: The installed Daytona SDK (`daytona` 0.220.0) provides only instantaneous quota/usage models (`OrganizationUsageOverview` and `RegionUsageOverview`), without historical dollar billing endpoints. Daytona usage is estimated using the lab's standard list-price rate card (`DAYTONA_RATE_CARD` in `evallab.task_qualification`: $0.0504/vCPU-h, $0.0162/GiB-h RAM, $0.000108/GiB-h storage > 5 GiB) applied to the trial wall-time portion falling within the UTC day (midnight-crossing intervals are split).
3. **Model API calls (`ledger`)**: Metered proxy ledgers from `lab-metadata.json` (`provider_usage` totals `cost_micros`) for completed jobs, plus experiment `spend.jsonl` files across the repository tree and sibling worktrees (deduplicated by record content hash). Self-hosted MiMo proxy calls have 0/0 pricing at the proxy and are billed via Modal container runtime; they contribute $0 to the model ledger to prevent double counting.

## Pre-launch spend check (`evallab spend check`)

HAR-129 implements the HAR-122 proposal's pre-launch gate as a read-only
CLI check plus a library function (`evallab.spend_day.check_launch`, so a
later dispatch hook can call it). Before every launch:

```bash
evallab spend check --since 2026-10-01T04:00:00Z --cap-usd 30 --candidate-usd 6
```

This example covers the overnight operating window: spend counted from
2026-10-01T04:00Z (Modal, Daytona and model calls) against the $30
overnight envelope, with a $6 candidate launch. Omit `--since` to check
the current UTC day; omit `--cap-usd` to use the standing policy's
`daily_cost_ceiling_usd`.

Committed spend is `settled(window) + in-flight + candidate`, where the
window is the half-open UTC interval `[--since, now)` (a UTC day is the
special case) and in-flight reserves `max(cost_limit_usd, est_cost_usd)`
for every spec in `queue/running` and `queue/approved`. Source
granularity inside a window: Modal billing rows are hourly, so a row
counts whole when its hour starts in the window; Daytona trials
contribute the wall seconds overlapping the window; `spend.jsonl` rows
count by their own `ts`; per-job proxy ledgers count whole on the day
their job finished.

Exit codes: `0` allowed, `3` refused (committed strictly exceeds the cap;
exactly-at-cap allows), `2` unverified — the catalog is unreachable
(never treated as $0), a queued cloud (non-Docker) spec records no
positive `est_cost_usd`, or Modal billing rows remain stale (>4h vs now).

When stored Modal billing rows are stale and the window extends past them,
`check_launch` automatically refreshes them read-only via the reconcile
fetch path (`modal billing report --resolution h`). If refresh fails or
rows remain stale, dispatch is refused with `stale_modal_billing` (exit 2).
Pass `--allow-stale-modal` to downgrade this refusal to a visible warning.
Modal's reporting lag (the current hour is partial) is always noted.

---

## Daily Backfill Summary

### 2026-09-29

| Source | Basis | Amount (USD) | Notes |
|---|---|---:|---|
| **Modal** | `billed` | $13.6831 | 13 billing rows in `modal_billing_rows` |
| **Daytona** | `estimate` | $2.9422 | 192 trial slices across 192 jobs |
| **Model APIs** | `ledger` | $0.0000 | 0 jobs (see note below) |
| **Grand Total** | | **$16.6253** | **Under cap ($20.00): Headroom $3.3747** |

*Update 2026-10-01 (HAR-122 review)*: Corrected finish-time grouping to use the aware UTC finish time from `lab-metadata.json` rather than Harbor's naive local timestamp. Two jobs (`har104-canned-proof` $0.000619, `har104-canned-gptoss` $0.000116) finished at 22:49/22:50 EDT on 2026-09-29, which is 02:49/02:50 UTC on 2026-09-30. Under UTC grouping they move to 2026-09-30, reducing 2026-09-29 model spend from $0.0007 to $0.0000 and increasing 2026-09-30 model spend from $1.5238 to $1.5245.

#### Breakdown by Card (2026-09-29)

- `HAR-81`: $1.9291 (Daytona SFT trials)
- `HAR-90`: $0.4470 (Daytona trials)
- `HAR-95`: $0.0431 (Daytona nop runs)
- `HAR-105`: $0.0121 (Daytona fix/qual trials)
- `HAR-104`: $0.0007 (Model proxy ledger)
- `unattributed`: $14.1941 (Modal account billing $13.6831 + unattributed Daytona jobs)

---

### 2026-09-30

| Source | Basis | Amount (USD) | Notes |
|---|---|---:|---|
| **Modal** | `billed` | $7.2226 | 5 billing rows in `modal_billing_rows` |
| **Daytona** | `estimate` | $8.4638 | 1370 trial slices across 1370 jobs |
| **Model APIs** | `ledger` | $1.5245 | HAR-111 ($0.1865) + HAR-112 ($1.3373) + HAR-104 UTC-shifted ($0.0007) |
| **Grand Total** | | **$17.2114** | **Under cap ($20.00): Headroom $2.7886** |

#### Breakdown by Card (2026-09-30)

- `HAR-104`: $0.3822 (Daytona student runs)
- `HAR-105`: $0.0535 (Daytona qual runs)
- `HAR-108`: $2.8632 (Daytona python census nops)
- `HAR-110`: $0.6248 (Daytona GEPA trials)
- `HAR-111`: $0.1865 (Model API spend: task checker validation)
- `HAR-112`: $1.3373 (Model API spend: trace exploration)
- `HAR-113`: $2.2019 (Daytona census unknown nops)
- `HAR-115`: $1.8117 (Daytona census repeat nops)
- `unattributed`: $7.7490 (Modal account billing $7.2226 + unattributed Daytona jobs)

---

## Known Gaps & Inferences

1. `[INFERENCE]` **Daytona Hardware Specs for Cleaned-Up Staging Directories**:
   When queue jobs finish, Harbor cleans up `.exec-stage/<job>` staging directories. For trials where the staging directory is gone and no override exists in `config.json`, the ledger applies the verified family fallback (`mimo-v2.6-rl`: 2 vCPU / 8192 MiB RAM / 0 billable storage), which matches 100% (10/10) of surviving staged task definitions and the HAR-110 budget specs. Trials from unknown families without staged metadata cannot be rated and are surfaced in notes as unrated (0 on 2026-09-29 and 2026-09-30).
2. `[INFERENCE]` **Modal Account Attribution**:
   Modal billing reports report aggregate usage by app/object, but containers are not tagged with individual trial/card identifiers. Therefore Modal billed costs land under `unattributed` in the card breakdown.
3. `[INFERENCE]` **Job vs Trial Midnight Crossings**:
   Daytona sandboxes split their duration across UTC midnight to attribute exact seconds to the day they were running. Proxy ledgers record cost per job upon completion, so a job finishing after midnight attributes its token cost to the completion day.
4. **2026-09-30 is incomplete.** The backfill ran at about 23:50Z on 2026-09-30. Its Modal rows were last fetched at 08:58Z (`modal billing-reconcile`), so any Modal use after that time is missing. Re-run `evallab modal billing-reconcile --for 2026-09-30` and then `evallab spend day --date 2026-09-30` once the day has closed.
5. **Daytona counts catalog-ingested trials only.** A run that was never ingested is missing: for example HAR-122's own egress probes (about $0.04, run directly through `harbor run`), and any job still running.
6. **The rate card and the receipts differ.** HAR-113 reported $2.4389 using `qualify-collect`'s estimate, while this ledger puts HAR-113 at $2.2019 on 2026-09-30 and $0 on 2026-09-29. Both are list-price estimates, not Daytona bills; neither is authoritative until Daytona exposes billed usage.
