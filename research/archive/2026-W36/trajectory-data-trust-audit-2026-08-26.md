# Trajectory data-trust audit — 2026-08-26

Platform-owned durable record over 17 analysis-ready trials / five campaigns.
Distinguishes **observed** vs **[INFERENCE]**. Does not paper over missing source fields.

## Heads and artifacts

| Layer | SHA / locator | Role |
|---|---|---|
| Code baseline (this worktree) | `2c8b732ff5870fb1a28b46d1cc1382a3cac85f37` | PR #199 merged (`feat(traj): intermediary quality…`) |
| Audit-time main | `8c996cbb1f1f8b34b18e89cb3b05141e400e0176` | PR #196; FieldParity / StorageJoin / CorruptionFailClosed **did not consume PR #199** |
| Cross-campaign inventory | `research/experiments/manifests/cross-campaign-analysis-inventory.json` | `generated_at` 2026-08-26T16:30:00Z |
| Auto-accept | `src/evallab/trajectory_acceptance.py` `AUTO_ACCEPTANCE_ENABLED=False` | Observed; 17/17 abstained, 0 accepted, 0 rejected |

Audit JSON inputs (pre-#199): FieldParityAudit, StorageJoinAudit, CorruptionFailClosedAudit, plus FieldParityAudit follow-up yield (CurrentMainCampaignParity; PR #199 unused).

PR #199 changed Data IR/pack/hydration/readiness. This Platform slice does **not** edit those files. It records remaining Data field loss and repairs Platform fail-closed / observability.

## 1. Exact campaign accounting (observed manifests)

| Campaign | planned | executed | analysis-ready | excluded | unique source CAS | unique ir/pack | HOLD | accepted/rejected/abstained |
|---|---|---|---|---|---|---|---|---|
| TB3 `terminal-bench-v3-k1-gemini-low-screen` | 5 | 7 | 5 | 1 quarantined_auth `BKZ7rHT` + 1 oracle `no_atif` | 5 analysis + 2 excluded | 5/5 | HOLD | 0/0/5 |
| `canary-event-summary-codex-20260815` | 1 | 3 | 3 | 0 | 1 shared `cas://sha256/310777bd…` | 3/3 | HOLD | 0/0/3 |
| `canary-terminal-bench-html-js-filter-codex-20260815` | 1 | 3 | 3 | 0 | 1 shared `cas://sha256/d48693ec…` | 3/3 | HOLD | 0/0/3 |
| `canary-transaction-reconciliation-codex-20260815` | 1 | 3 | 3 | 0 | 1 shared `cas://sha256/ab56fd18…` | 3/3 | HOLD | 0/0/3 |
| `canary-syn-funcdag-suite` | 3 | 3 | 3 | 0 | 3 unique | 3/3 | HOLD | 0/0/3 |

Cross inventory: 21 indexed / 17 analysis-ready / 17 batch-interpreted / 4 quarantined HOLD / 5 campaigns.

TB3 machine inventory: planned 5 / executed 7 / analysis-ready 5 / quarantine 1 / control 1 / unresolved 0.

Claim limits (inventory `statistical_notes.tb3_k1_screen`, observed): screening-only; 0/5 is not precise zero; Wilson 95% `[0, 0.4345]`; bootstrap CI suppressed; Eval Lab pass@k is first-k indicator, not Chen/Yao; no k=3 pooling; no ranking/capability/reliability/causality.

## 2. Field parity (Harbor/ATIF/CAS/verifier/state to IR/pack)

Observed by FieldParityAudit at `8c996cb` (PR #199 unused). Compilers: `build_trajectory_ir` / `build_evidence_pack`. Live TB3 Harbor dirs `runs/tb3-k1-*` **absent**; TB3 identity is CAS+manifest. Canary 2026-08-15 trees exist under `research/evidence/runs/`.

### TB3 analysis-ready (IR events = manifest `atif_steps_count`)

| trial | events | unpaired | linkage | tokens/cost | timestamps |
|---|---|---|---|---|---|
| `bun-sourcemap-leak__vaurWUd` | 104 | 51 | degraded | 0/0 | null |
| `cargo-flight-dispatch__z5vUTct` | 74 | 36 | degraded | 0/0 | null |
| `embedding-drift-monitor__JcUjDcj` | 103 | 59 | degraded | 0/0 | null |
| `foodstuff-beta-activity__GfEgM6V` | 56 | 27 | degraded | 0/0 | null |
| `ico-path-patch__5dkQZr5` | 2 | 0 | complete | 0/0 | null |

TB3 pack: selected 3 (ico 2), omitted 101/71/100/53/0. quality_status warn except ico pass. No `observation` event_type on TB3 IR.

### Canary analysis-ready

| suite | ATIF steps | IR events | verifier_digest in manifest |
|---|---|---|---|
| event-summary x3 | 11/11/11 | 11/11/11 | null |
| html-js-filter x3 | 21/18/15 | 21/18/15 | null |
| txn-recon x3 | 10/10/9 | 10/10/9 | null |
| syn-funcdag x3 | IR 6/9/8 | 6/9/8 | present |

Harbor verifier files exist on promoted canaries. No IR `event_type=verifier_check`. `exit_code` null on 100% of IR events (`exit_semantics=unobserved`).

### Pack budget (all 17, observed)

`budget_tokens=16000`; `overflow_reason=null`; `is_model_callable=true`; `abstain_required=false`; selected_windows=1; selected_events_count=3 except ico=2. Omission is routine-event compression, not budget overflow.

### PR #199 vs this Platform slice

Observed in merged Data files at `2c8b732` (read-only): pack `source_digests` copies IR sources then adds `ir_digest=ir.ir_digest` and `redaction_profile_digest`. PR #199 emits distinct tool-call IREvents and call-bound citations; observation-event extraction, verifier projection, and remaining raw-field parity are tracked separately in PR #202. Platform runtime at this HEAD **before this slice** required `pack.source_digests == ir.source_digests`, so every rebuilt pack failed `schema_valid` (`schema_invalid`). That is a Platform validator defect, not a missing Data field.

**[INFERENCE]** TB3 zero tokens is likely an AntigravityCli capture/accounting gap, not true zero usage. Job-level CAS reuse is likely by-design job archive and breaks trial-unique CAS identity if consumers key only on `source_cas_uri`.

## 3. Citation / digest / CAS / Parquet / DuckDB / PG

StorageJoinAudit at `8c996cb` against shared `/Users/petermakhnatch/Developer/eval-lab/derived`. Concurrent store growth during CorruptionFailClosed (60 to 66 blobs; 7 to 8 campaign records) is **observed**, not inferred.

| Store | Observed | Unknown-vs-zero |
|---|---|---|
| CAS blobs | 60 unique content/archive digests at StorageJoin; later 66 at CorruptionFailClosed | 0 missing blobs; hex join 60/60 |
| CAS records | job=16, interpretation=37, interpretation_campaign=7 (then 42/8) | `Path.stem` on `*.tar.gz` is a **false-orphan**; join on hex without `.tar.gz` |
| Sidecars | 17 complete IR/pack/judgment/decision quads; 5 campaign reports | `_recipes` is not a trial |
| Postgres | **unavailable**, not zero | Catalog unread. `row_count` must stay `null` |
| jobs parquet hive | `job_id=*/trial_id=*/jobs.parquet` **absent** | Two stray `job_id=*/jobs.parquet` without `trial_id` are out of attach globs — not the hive, not zero jobs |
| trial_facts | 146 trial dirs; sidecar.trial_id = trial_facts.trial_id 17/17 | present for interpreted trials |
| interpretation_artifacts parquet | 85 rows; trial_id populated; 17 each of ir/pack/judgment/decision/interpretation | present |
| machine_judgments / acceptance_decisions parquet | 17 rows each; **no trial_id/job_id** | join via pack_digest / judgment_id / decision_id |
| DuckDB attach | Z3 readable; Z2 not attached | empty view is not “zero jobs exist” |
| CitationHandle.trial_id | null x17 on live window citations | unique citation cas_uris=11 all resolve at audit time |

Job CAS `record_id` is `job_name`, not Harbor UUID `job_id`. TB3 names have a second unanalyzed Harbor `job_id`. Recorded, not rewritten.

## 4. Fail-closed before vs after this Platform change

CorruptionFailClosedAudit (pre-#199 source of `_analyze_trial_core`):

| Injection | Expected | Observed at audit | After this slice |
|---|---|---|---|
| missing blob | `missing_cas` | MATCH | preserved |
| mapping without `cas_uri` | `missing_cas` | MATCH | preserved |
| invalid `trial_name` (`../outside`) | `cas_integrity_error` | MATCH (`CASTrialResolutionError`) | preserved |
| XOR-mutated gzip | integrity failure | `BadGzipFile`; analyze_trial wrap **unwrapped ValueError/gzip** | wrapped as `cas_integrity_error` (not `missing_cas`) |
| restore digest mismatch / path escape | `cas_integrity_error` | source-only wrap for `FileNotFoundError` | `ValueError` to `cas_integrity_error` |
| `quality_status=quarantine` | `quarantined_input` **before** restore | **NOT REACHED**: restore mkdir first | mapping quarantine/fail/quarantined raises **before** `restore_evidence` |
| auto-accept | never accepted | MATCH | `AUTO_ACCEPTANCE_ENABLED is False` unchanged |

P0 after PR #199 (this worktree, observed by source + focused tests):

| Check | Before | After |
|---|---|---|
| `_validate_artifact_digests` source map | `pack.source_digests == ir.source_digests` | exact expected map = IR sources + `ir_digest=ir.ir_digest` + `redaction_profile_digest=pack.redaction_profile_digest` |
| missing / extra / mismatch keys | would fail only if IR map unequal | fail `schema_invalid`; **not** a subset match |
| rebuilt valid IR/pack | 17/17 observed `schema_invalid` in the hard-stopped post-merge forensic batch | `schema_valid` pass in focused contract test; no campaign rerun before reviewed merge |

`FileNotFoundError` is classified before generic `OSError`. `gzip.BadGzipFile` is an `OSError` on 3.12 and becomes `cas_integrity_error`.

## 5. Operator surface (this slice)

`evallab analyze quality <inventory>` calls `campaign_data_quality_report`.

No judge, no IR/pack rebuild, no CAS restore of quarantined rows. Missing PG: `status=unavailable`, `row_count=None`. Missing jobs hive: `status=missing`, `row_count=None`. Canary `verifier_digest=null` stays unknown, not 0.

Readiness is always `HOLD` while auto-accept is disabled or any coverage/source/quarantine/projection gap exists.

## 6. Remaining Data blockers (record, do not fix here)

From FieldParityAudit yield and pre-#199 field audit. Locators are Data-owned; this slice does not edit them.

1. Job-level CAS reused for k=3 canaries (`310777bd…`, `d48693ec…`, `ab56fd18…`). Distinct `trial_id`/`trial_name` but identical `cas_uri`.
2. `verifier_digest` / `task_digest` null on all 9 2026-08-15 Codex canary manifest rows. Harbor `result.json` has `task_checksum` (event-summary `2a7d47fa…`).
3. Inventory overwrites Harbor identity: event-summary manifest `gpt-5.6-luna` / PinnedCodex `0.148.0` vs `result.json` `gpt-5.6-terra` / `0.147.0`.
4. Cost/token loss: canary manifest `cost_usd=0` vs `result.json` `agent_result.cost_usd=0.033356`, `n_input_tokens=71542` on `event-summary__5E3btLv`.
5. Pre-PR #199 baseline collapsed tool calls into one step event. PR #199 now emits distinct call events/citations; observation-event extraction and count parity remain in PR #202.
6. Verifier artifacts were not IR events in the pre-PR #199 baseline; PR #202 owns verifier digest/event follow-up.
7. State journals remain unobserved when no journal artifact or state digest exists; missing evidence stays unknown.
8. `tool_schema_digest` / `matched_result_digest` remain unset when raw ATIF does not carry the required source contract.
9. `exit_code` stays null / `exit_semantics=unobserved` when no matched observation records an outcome.
10. TB3 inventory/Quality Ledger reports unpaired counts 51/36/59/27/0, while a later raw-CAS scan found source_call_id pairs. PR #202 owns explicit raw-vs-ledger discrepancy recording; do not silently relabel.
11. TB3 timestamps/tokens remain absent in durable source evidence.
12. CitationHandle trial identity and projection-key parity are PR #202/Platform follow-ups; MJ/AD parquet currently join through pack/judgment/decision digests.
13. Pack selection behavior changed in PR #199; pre/post comparison requires a corrected runtime rerun.
14. Screening adjacency labeled `error_recovery_candidate` must not be treated as recovery.
15. Tokenizer: pack `_est_tokens` is 4 chars/token, not a frozen tokenizer.

## 7. Remaining Platform blockers (record, not deleted)

- job_name vs job_id authority: CAS job `record_id=job_name`; parquet uses Harbor UUID `job_id`; TB3 names have a second unanalyzed Harbor job_id.
- Orphan CAS: interpretation records 37 vs sidecar quads 17; campaign records 7 vs 5 reports (audit-time). Do not delete.
- Missing jobs parquet hive; two stray `job_id=*/jobs.parquet`.
- Absent PG (unavailable, not zero).
- DuckDB empty views for missing tables must not be counted as zero jobs in operator reports.
- Quality warn unpaired: IR/pack may build; linkage-dependent classes must abstain. Auto-accept remains disabled.
- Pack mandatory-window overflow must stay uncallable (`is_model_callable` false).

## 8. Files in this Platform slice

| Path | Change |
|---|---|
| `src/evallab/trajectory_runtime.py` | P0 exact expected pack source map; quality-before-restore; restore error classification |
| `src/evallab/trajectory_data_quality.py` | new per-campaign HOLD/coverage/CAS/projection operator |
| `src/evallab/cli.py` | `analyze quality` |
| `src/evallab/database.py` | `catalog_availability` (unavailable implies `row_count=None`) |
| `tests/test_trajectory_runtime.py` | schema_valid exact-map contracts; quality-before-restore; corrupt restore classification |
| `tests/test_trajectory_data_quality.py` | unknown-vs-zero; current manifest accounting; canary verifier_digest stays unknown |
| `research/analysis/trajectory-data-trust-audit-2026-08-26.md` | this report |

No Data file edits. No commit/PR from this agent. Devin builder quota was exhausted; implementation is local in `.worktrees/platform-data-trust-v1`.

## 9. What this checkout could not re-observe (unknown, not zero)

- Live PostgreSQL catalog row counts.
- Current shared-derived CAS blob/record counts after concurrent growth (audits reported 60 then 66).
- Exact per-trial `consumed_tokens_est` / omitted-range reopen against live CAS restore in this sandbox.
- Whether job-level CAS restore + `trial_name` collides for the three canary siblings (code path exists; restore of live job CAS not re-run here).
- TB3 ATIF bytes: Harbor dirs absent; only CAS URIs are durable.
