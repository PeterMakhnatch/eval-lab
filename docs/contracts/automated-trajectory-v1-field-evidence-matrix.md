# Automated trajectory v1 field-to-evidence matrix

Validation basis: current origin/main after PR #187, `research/experiments/manifests/terminal-bench-v3-k1-gemini-low-{campaign-manifest,machine-analysis-inventory}.json`, central `derived/evidence-cas`, merged Quality Ledger, current raw-shape/storage audits, and research-context calibration freeze.

## CampaignAnalysisManifest mapping

| Contract field | Real source | Current evidence / normalization |
|---|---|---|
| campaign/source commit/actor | PR #185 campaign manifest | `terminal-bench-v3-k1-gemini-low-screen`, execution commit `a1897ee…`, Peter |
| source campaign manifest digest | bytes of merged PR #185 JSON | manifest builder computes SHA-256; not copied from prose |
| attempted entries | PR #187 inventory | 7 entries: 5 cohort trials, 1 quarantined auth attempt, 1 free control |
| source role | inventory `role` | preserve `infrastructure_retry_1`, `spec_2`…; never overload as cohort membership |
| cohort inclusion | inventory cohort array membership | five true; control/quarantine false |
| attempt role | normalized source role | successful bun retry=`retry`; spec_2…5=`primary`; free control=`control`; auth attempt=`quarantined_attempt` |
| task/verifier digest | inventory | nullable for free control where source says `n/a`; unknown reason recorded |
| CAS URI | inventory | all seven use `cas://sha256/<64hex>` |
| quality | inventory + Quality Ledger | cohort: four `warn`, one `pass`; quarantine/control preserve `quarantine|no_atif` |
| accounting | inventory `accounting` | planned 5, executed 7, cohort 5, retry 1, control 1, quarantine 1, unresolved 0 |

Schema correction from this mapping: trial role is split into `source_role`, `cohort_included`, and `attempt_role`; quality accepts the complete ledger enum; nullable control digests are explicit unknowns.

## TrajectoryIR mapping

| IR group | Authoritative source | Required handling |
|---|---|---|
| experiment/spec/job/trial | manifest + raw result/lab metadata | exact strings; no names parsed into IDs |
| document/session/trajectory/steps | raw ATIF restored from CAS | preserve ATIF `step_id`, `tool_call_id`, `source_call_id`; multiple calls per step retained |
| source bytes and CAS | CAS record/blob | every result/lock/ATIF/verifier/state source carries relative path, bytes, SHA-256, CAS URI |
| task/verifier/environment/scaffold/model | lock/result/config/lab metadata | null + unknown reason when absent; never infer from filename |
| outcome/reward/exception | trial `result.json` and verifier outputs | null reward is unknown/verifier unavailable, not zero |
| quality | merged Quality Ledger loader | missing report=`quality_not_evaluated`; fail/quarantine not model-callable |
| unpaired calls | quality findings + ATIF link audit | current four warning TB3 trials retain exact unlinked count; linkage-dependent labels unavailable |
| action/program/skeleton | deterministic parser with version/config digest | profile-dependent exit meaning is unknown without pinned semantics profile |
| state/verifier edges | explicit state journal/check refs only | no temporal-adjacency causality |
| opportunity | benchmark/profile contract citations | absent authority => unavailable; adjacency after error is not recovery opportunity |

Observed current TB3 cohort boundary: four trials contain repeated `ATIF_UNPAIRED_TOOL_CALL` warnings; `ico-path-patch` is quality pass with only two steps and reward zero. The IR must represent both without inventing observations, recovery, or capability conclusions.

## EvidencePack mapping

| Pack group | IR/raw source | Gate |
|---|---|---|
| header/contracts/outcome/coverage | deterministic IR + cited task/verifier bytes | mandatory budget reserve |
| selected evidence | exact CAS-hydrated CitationHandles | whole windows only; redacted content hash separate from raw source hash |
| episode index | deterministic IR boundaries | summaries `advisory_only=true` |
| omitted ranges | every unselected IR event | bounds/counts/type counts/ordered digest/reopen citations mandatory |
| pair evidence | valid AlignmentRecord | no pair window when counterpart confounded/unavailable |
| token accounting | frozen tokenizer + complete serialized pack | mandatory overflow => tier/reopen/abstain; pack cannot reach model |

Current campaign production smoke must start from the five PR #187 CAS URIs. `.worktrees/tbench3-screen` trial directories are stale development evidence and cannot satisfy campaign acceptance.

## Judgment / decision / calibration mapping

| Contract | Current source | Status |
|---|---|---|
| MachineJudgment v1 | no current main producer; Track B1 ontology/prompt freeze | new Platform artifact; output label closed to frozen ontology |
| AcceptanceDecision v1 | no current main producer | new pure Platform gate; default abstain-only |
| CalibrationReport v1 | research-context calibration schema/protocol; executable results pending | `experimental_hold`; no class enabled |
| human baseline | future multi-rater local held-out set | APB/TrajErr consensus rows do not provide production human baseline |
| citation entailment | deterministic resolution first; calibrated semantic blocker later | no current three-way corpus; semantic pass cannot open acceptance yet |

## Benchmark-specific evidence checks

| Family | Durable evidence observed | Required system disposition now |
|---|---|---|
| Gemini TB3 | five CAS-backed cohort trials + retry/control/quarantine accounting | build IR/pack; preserve warning coverage; judgments remain calibration-gated |
| AgentAbstain | free oracle control with no ATIF; superseded stubs are not Harbor trials | deterministic insufficient judgment; abstain `source_missing|pair_unavailable` |
| DeepPlanning | no reopenable local Harbor/CAS identity found | unavailable; do not fabricate six trials from an opinion note |
| LOCA | task/materializer pins but no durable Harbor trial/ATIF/quality/CAS in audited corpus | unavailable; task folders are not trajectories |

## Contract validation cases

1. The five Eval Lab runtime/input definitions compile under the combined JSON Schema; the sixth interface, CalibrationReport v1, is validated by its Synthetic Research-owned external schema and pinned SHA-256 rather than duplicated.
2. PR #187 records map without dropping any attempted entry or converting `n/a` to a digest.
3. Wrong/missing CAS, source, content, redaction, task, verifier, or quality digest fails closed.
4. A warning TB3 item with unpaired calls can build IR/pack but cannot auto-accept a linkage-dependent class.
5. Missing ATIF/reward/pair produces explicit unknowns and deterministic abstention.
6. Mandatory-window overflow produces no model-callable pack.
7. A class gate is copied from one exact frozen CalibrationReport row; v1 report/class enable flags are false and cannot be overridden by AcceptanceDecision.
8. Rebuilding PostgreSQL/Parquet from CAS sidecars preserves artifact digests and decisions.
