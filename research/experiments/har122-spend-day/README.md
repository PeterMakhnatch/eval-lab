# HAR-122: Spend Day Ledger & Backfill (2026-09-29 and 2026-09-30)

## Overview

The daily spend ledger (`evallab spend day --date YYYY-MM-DD`) combines available billing-cache rows, infrastructure estimates and model-usage ledgers per UTC day. These historical backfills compare that recorded total with the then-standing $20.00/day cap; they are not complete provider invoices or a new spending approval.

Every line item reports an explicit `basis` (`billed`, `estimate`, or `ledger`) so that provider-billed invoices are never mixed up with model rate estimates:

1. **Modal (`billed`)**: Retrieved from the `modal_billing_rows` catalog table populated by the read-only reconcile workflow (`evallab modal billing-reconcile`). Modal bills account-wide GPU/CPU/memory runtime. Because Modal does not tag individual trial containers with card IDs, account billing is attributed to `unattributed`.
2. **Daytona (`estimate`)**: The installed Daytona SDK (`daytona` 0.220.0) provides only instantaneous quota/usage models (`OrganizationUsageOverview` and `RegionUsageOverview`), without historical dollar billing endpoints. Daytona usage is estimated using the lab's standard list-price rate card (`DAYTONA_RATE_CARD` in `evallab.task_qualification`: $0.0504/vCPU-h, $0.0162/GiB-h RAM, $0.000108/GiB-h storage > 5 GiB) applied to the trial wall-time portion falling within the UTC day (midnight-crossing intervals are split).
3. **Model API calls (`ledger`)**: Metered proxy ledgers from `lab-metadata.json` (`provider_usage` totals `cost_micros`) for completed jobs, plus experiment `spend.jsonl` files across the repository tree and sibling worktrees (deduplicated by record content hash). Self-hosted MiMo proxy calls have 0/0 pricing at the proxy and are billed via Modal container runtime; they contribute $0 to the model ledger to prevent double counting.

---

## Daily Backfill Summary

### 2026-09-29

| Source | Basis | Amount (USD) | Notes |
|---|---|---:|---|
| **Modal** | `billed` | $13.6831 | 13 billing rows in `modal_billing_rows` |
| **Daytona** | `estimate` | $2.9422 | 192 trial slices across 192 jobs |
| **Model APIs** | `ledger` | $0.0007 | Ledger-priced local canned-response proofs (`har104-canned-proof`, `har104-canned-gptoss`), not actual paid model requests |
| **Grand Total** | | **$16.6261** | **Under cap ($20.00): Headroom $3.3739** |

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
| **Model APIs** | `ledger` | $1.5238 | HAR-111 checker ($0.1865) + HAR-112 trace exploration ($1.3373) |
| **Grand Total** | | **$17.2101** | **Under cap ($20.00): Headroom $2.7899** |

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

## HAR-132 reproduction (2026-10-01)

Both dates were recomputed with the actual read-only `evallab spend day` CLI.
September 29 matches the snapshot exactly: **$16.626055719385402**.
September 30 returned **$17.210682529581202** in this replay, versus the saved
**$17.21014313347965**: the **$0.0005393961015514** increase is one later
catalog-ingested 8.408357-second Daytona trial,
`har115-rnop-000238-a3485fb06ded`. Both still round to **$17.21**. The
historical JSON snapshots are preserved rather than silently overwritten.

The **18** retained Modal cache rows (13 + 5) and both experiment spend
ledgers reproduce the recorded arithmetic. All 18 use daily resolution;
the October 1 daily/hourly overlap defect does not affect these two dates.
The original provider billing response was not found in the inspected
retained sources, so this is cache/ledger reproduction, not independent
invoice verification. Gap 4 still applies.
Also, the audited `sibling_worktree_roots()` implementation discovers no
siblings from a linked checkout. The 47 retained spend-file copies collapse
to two distinct ledgers, so that defect did not change these totals, but
unique unmerged sibling records could be missed. The runtime fix is owned
on HAR-122, not hidden by rewriting these receipts.

