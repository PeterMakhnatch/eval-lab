---
status: current
date: 2026-09-08
producer: Data Engineer lane (wK:pJ), via read-only scout of Quality/Factory/Integration evidence
method: existing typed readers mapped per record class; no Harbor provenance manufactured; direct-Docker records stay external
supersedes: nothing (first inventory)
---

# Evidence inventory — Quality / Factory / Integration (2026-09-08)

Authoritative executions below are direct-Docker runs under the OwnershipRunner transport unless noted; they are linked as immutable external diagnostics, never projected into Harbor `runs/<job>/<trial>` and never given fabricated ATIF.

## Authoritative Quality executions (30 baseline + 19 paired = 49)

| # | task | arm | type | outcome | path |
|---|---|---|---|---|---|
| 1 | task_000002 | oracle | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000002/oracle` |
| 2 | task_000002 | nested_reorder | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000002/nested_reorder` |
| 3 | task_000002 | nop | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000002/nop` |
| 4 | task_000002 | corrupt_agent_health | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000002/corrupt_agent_health` |
| 5 | task_000002 | drop_wallet_intent | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000002/drop_wallet_intent` |
| 6 | task_000003 | oracle | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000003/oracle` |
| 7 | task_000003 | valid_alternative | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000003/valid_alternative` |
| 8 | task_000003 | nop | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000003/nop` |
| 9 | task_000003 | invalid_value | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000003/invalid_value` |
| 10 | task_000003 | invalid_security | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000003/invalid_security` |
| 11 | task_000003 | markdown_heading | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000003/markdown_heading` |
| 12 | task_000003 | fabricated_markdown | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000003/fabricated_markdown` |
| 13 | task_000003 | truncated_markdown | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000003/truncated_markdown` |
| 14 | task_000003 | header_only_audio | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000003/header_only_audio` |
| 15 | task_000003 | tamper_source_and_output | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000003/tamper_source_and_output` |
| 16 | task_000003 | tamper_source_format_only | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000003/tamper_source_format_only` |
| 17 | task_000005 | oracle | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000005/oracle` |
| 18 | task_000005 | markdown_format_variation | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000005/markdown_format_variation` |
| 19 | task_000005 | nop | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000005/nop` |
| 20 | task_000005 | corrupt_seed_proof | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000005/corrupt_seed_proof` |
| 21 | task_000005 | corrupt_manifest_version | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000005/corrupt_manifest_version` |
| 22 | task_000005 | fabricated_market_analysis | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000005/fabricated_market_analysis` |
| 23 | task_000011 | oracle | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000011/oracle` |
| 24 | task_000011 | reverse_email_context_keys | baseline_control | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000011/reverse_email_context_keys` |
| 25 | task_000011 | nop | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000011/nop` |
| 26 | task_000011 | wrong_report_text | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000011/wrong_report_text` |
| 27 | task_000011 | swap_summaries | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000011/swap_summaries` |
| 28 | task_000011 | reverse_root_keys | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000011/reverse_root_keys` |
| 29 | task_000011 | missing_required_key | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000011/missing_required_key` |
| 30 | task_000011 | extra_email_key | baseline_control | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before-owned/000011/extra_email_key` |
| 31 | task_000003 | oracle | paired_after_repair | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/oracle` |
| 32 | task_000003 | valid_alternative | paired_after_repair | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/valid_alternative` |
| 33 | task_000003 | nop | paired_after_repair | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/nop` |
| 34 | task_000003 | invalid_value | paired_after_repair | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/invalid_value` |
| 35 | task_000003 | invalid_security | paired_after_repair | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/invalid_security` |
| 36 | task_000003 | markdown_heading | paired_after_repair | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/markdown_heading` |
| 37 | task_000003 | fabricated_markdown | paired_after_repair | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/fabricated_markdown` |
| 38 | task_000003 | truncated_markdown | paired_after_repair | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/truncated_markdown` |
| 39 | task_000003 | header_only_audio | paired_after_repair | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/header_only_audio` |
| 40 | task_000003 | tamper_source_and_output | paired_after_repair | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/tamper_source_and_output` |
| 41 | task_000003 | tamper_source_format_only | paired_after_repair | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/tamper_source_format_only` |
| 42 | task_000011 | oracle | paired_after_repair | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000011/oracle` |
| 43 | task_000011 | reverse_email_context_keys | paired_after_repair | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000011/reverse_email_context_keys` |
| 44 | task_000011 | nop | paired_after_repair | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000011/nop` |
| 45 | task_000011 | wrong_report_text | paired_after_repair | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000011/wrong_report_text` |
| 46 | task_000011 | swap_summaries | paired_after_repair | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000011/swap_summaries` |
| 47 | task_000011 | reverse_root_keys | paired_after_repair | reward 1 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000011/reverse_root_keys` |
| 48 | task_000011 | missing_required_key | paired_after_repair | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000011/missing_required_key` |
| 49 | task_000011 | extra_email_key | paired_after_repair | reward 0 | `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000011/extra_email_key` |

Count check (verified): rows 1–30 baseline + rows 31–49 paired = 49.
Why authoritative: every baseline arm ran under the OwnershipRunner archive transport (numeric UID/GID + modes preserved, digests.json + task-file manifests verified); every paired-after arm is an exact filesystem match of its baseline executed against the repaired verifier digest in its rationale. Full per-arm rationales live in the scout transcript (agent session EvidenceInventory).

## Contaminated earlier attempts (retained as observations, superseded)

- `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before/000003` (task_000003, 9 arms, 7 infra-contaminated): Per transport-adjudication.json and campaign-summary.json: 'All chmod600 task000003 reference-derived outputs encounter PermissionError reading credentials in verifier... Docker host round trip changed root-owned mode-0600 credentials to UID 501, GID 20. With all capabilities dropped, the verifier could not read them.' Contaminated arms: oracle, valid_alternative, markdown_heading, fabricated_markdown, truncated_markdown, header_only_audio, invalid_value. Only chmod644 invalid_security and nop avoided PermissionError.
- `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before/000002` (task_000002, 5 arms, 0 infra-contaminated): Initial 5-arm pass lacked archive-transport numeric ownership verification and byte/UID/GID mode manifests before verifier execution. Results retained as observations but superseded by authoritative before-owned/000002.
- `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before/000005` (task_000005, 6 arms, 0 infra-contaminated): Initial 6-arm pass lacked archive-transport numeric ownership verification and byte/UID/GID mode manifests. Results retained as observations but superseded by authoritative before-owned/000005.
- `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/before/000011` (task_000011, 8 arms, 0 infra-contaminated): Initial 8-arm pass lacked archive-transport numeric ownership verification and byte/UID/GID mode manifests. Results retained as observations but superseded by authoritative before-owned/000011.

## Factory candidates, controls, and rejection/usage gaps (0 admissions)

- candidate `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/repo-diff/generated/huggingface__Repo2RLEnv-73`
  - controls: reference: reward 1.0; noop: reward 0.0; perturbed: reward 0.993548 (narrow probe confirms helper drops valid nonempty SHA under perturbation, yet diff similarity scores ~0.99).
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/pr73-reference/receipt.json`
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/pr73-noop/receipt.json`
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/pr73-perturbed/receipt.json`
  - gap: Rejected from admission (0 admissions): diff similarity reward fails to reject inverted conditional logic; oracle patch exposed plaintext at /verifier/oracle.patch to default container user; no live model calls or token usage recorded.
- candidate `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/repo-diff/generated/huggingface__Repo2RLEnv-49`
  - controls: reference: reward 1.0; noop: reward 0.0; perturbed: reward 0.981818 (similarity 0.909, size/file/region 1.0).
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/pr49-reference/receipt.json`
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/pr49-noop/receipt.json`
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/pr49-perturbed/receipt.json`
  - gap: Rejected from admission: diff similarity accepts invalid warning-condition mutation with >0.98 reward; oracle patch exposed plaintext at /verifier/oracle.patch; zero live model spend/usage captured.
- candidate `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/repo-diff/generated/huggingface__Repo2RLEnv-75`
  - controls: reference: reward 1.0; noop: reward 0.0; perturbed: reward 0.931021 (standalone writer restores competing reward.json, probe fails, yet diff reward is ~0.93).
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/pr75-reference/receipt.json`
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/pr75-noop/receipt.json`
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/pr75-perturbed/receipt.json`
  - gap: Rejected from admission: diff similarity reward cannot discriminate broken runtime behavior; oracle patch readable at /verifier/oracle.patch; zero live model calls or token usage captured.
- candidate `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/facet-repair/task`
  - controls: original: reward 0.0, exit 1; reference: reward 0.0, exit 0 (1/4 tests pass); noop: reward 0.0, exit 0 (0/4 tests pass); checksum-corruption: reward 0.0, exit 0 (1/4 tests pass, fails before checksum).
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/facet-original/receipt.json`
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/facet-reference/receipt.json`
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/facet-noop/receipt.json`
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/lead-evidence/facet-checksum-corruption/receipt.json`
  - gap: Quarantined, not admitted: manual oracle crash fix (exit 1->0) leaves unchanged verifier failing on dict uniqueness, ID content-hash convention, and report parsing (all arms score reward 0.0).
- candidate `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/accounting-qualification/canary_002_qualification.json`
  - controls: Canary-002 not approval-ready: AttemptLedger drops token usage returned on parse/error attempts and labels partial sums exact; writer overwrites and marks synthetic.
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/accounting-qualification/counterexample_result.json`
  - receipt: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/campaign-20260908/accounting-qualification/synthetic_counterexample.jsonl`
  - gap: Past run paid_glm45flash_001 has attempts: 8, accepted: 0, failed: 1, usage_tokens: null, spend_usd: null, coverage unknown; native agent prep cost is unknown (no token/cost metadata returned).

## Integration evidence and its existing typed reader

- **PR #388 Delivery: Read-Only Audit Comparison (task_000008 content repair)** — `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/task000008-content-repair-20260907` (Directory containing before/ and after/ subdirectories, each with summary.json, run_meta.json, and per-arm CTRF results)
  - reader: `evallab.task_workbench:compare_quality_audits(before_dir, after_dir, declaration_path=..., repo_root=...) and CLI 'python -m evallab.task_workbench audit-compare'` (consumed today: True); Exercised in PR #388 tests (tests/test_task_workbench_audit_compare.py) in text and JSON formats: empty_values and nested_corruption observed 1->0; oracle/reordered_objects 1->1; nop/drop_event 0->0.
- **PR #388 Delivery: Audit Repeat Comparison (task_000010 probe)** — `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/facet-key-order-probe-run-audited-2` (Two directory roots (facet-key-order-probe-run-audited vs facet-key-order-probe-run-audited-2) with summary.json, run_meta.json, ctrf.json)
  - reader: `evallab.task_workbench:compare_quality_audits` (consumed today: True); Exercised in PR #388: 4 arms compared with neutral zero deltas; correctly marked unqualified because retained identity/runtime coverage was missing. Repeated historical audit, not repair.
- **Quality Task 000003 Before/After Repair Pair (11 paired arms)** — `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000003/comparison.json` (after-owned/000003/comparison.json and per-arm before-owned/000003/* vs after-owned/000003/* directories with observed-summary.json, metadata.json, and CTRF ctrf.json)
  - reader: `evallab.task_workbench:compare_quality_audits (requires minimal extension to accept observed-summary.json)` (consumed today: False); Demonstrates bounded source-integrity verifier repair: tamper_source_and_output and tamper_source_format_only flip 1->0; reference/rephrasing/heading 1->1; nop/wrong-metric/permissions 0->0. Fabricated/truncated markdown and 3-byte ID3 remain 1->1. Requires filename tolerance in reader (observed-summary.json).
- **Quality Task 000011 Before/After Repair Pair (8 paired arms)** — `/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/after-owned/000011/comparison.json` (after-owned/000011/comparison.json and per-arm before-owned/000011/* vs after-owned/000011/* directories with observed-summary.json, metadata.json, and CTRF ctrf.json)
  - reader: `evallab.task_workbench:compare_quality_audits (requires minimal extension to accept observed-summary.json)` (consumed today: False); Demonstrates bounded key-ordering verifier repair: reverse_root_keys flips 0->1; reference and reverse_email_context_keys 1->1; all five negative arms remain 0->0. Requires filename tolerance in reader (observed-summary.json).

## Intake mapping (existing paths only; extensions are function-level, no new store)

- **Quality Comparative Development Cohort (2026-09-08) (artifacts/quality-cohort-20260908/before-owned and after-owned)** — FAILS_INTAKE_DUE_TO_NAMING_AND_LAYOUT
  - path: `evallab.task_workbench:load_quality_audit_evidence (PR #388)`; extension: Extend load_quality_audit_evidence to fall back to 'observed-summary.json' when 'summary.json' is absent, and 'metadata.json' when 'run_meta.json' is absent. Support reading per-arm task-file-manifest.json / verifier-task-file.manifest.json in addition to directory-level task-manifest.json.
- **Quality Historical Probes and Single-Task Repairs (artifacts/pipeline-record-quality-audit, artifacts/facet-key-order-probe-run-audited-2, artifacts/task000008-content-repair-20260907)** — COMPATIBLE_NOW
  - path: `evallab.task_workbench:load_quality_audit_evidence and compare_quality_audits (PR #388)`; extension: None: directly consumes summary.json, run_meta.json, audit-rollup.json, and ctrf.json present in these directories.
- **Contaminated Quality Attempts (artifacts/quality-cohort-20260908/before/000003 and related v1 runs)** — INTAKE_AS_IMMUTABLE_EXTERNAL_DIAGNOSTIC_ONLY
  - path: `evallab.schemas.ProvenanceMetadata(zone='01-external') via fetch.py / external audit sidecars`; extension: Add external-audit status flag distinguishing infrastructure failure (PermissionError on host UID/GID mismatch) from genuine task-level reward rejections. Never project into Harbor runs/<job>/<trial> or fabricate ATIF.
- **Environment Factory PR-Diff Candidates and Controls (lanes/generation/campaign-20260908/repo-diff/)** — NO_EXECUTION_INTAKE_PATH
  - path: `evallab.task_workbench:validate_task_directory (for generated package structure); NO intake for direct-Docker candidate receipts or diff-reward JSONs`; extension: Add function 'load_factory_candidate_evidence(candidate_dir: Path)' in task_workbench.py to read generation_receipt.json, source lineage, and direct-Docker control receipts without manufacturing native Harbor trial provenance. Attach ProvenanceMetadata(zone='03-synthetic').
- **Environment Factory Accounting & Ledger Records (lanes/generation/campaign-20260908/accounting-qualification/)** — NO_INTAKE_PATH
  - path: `NO intake path in eval-lab currently parses or stores generator attempt ledgers or Canary qualifications`; extension: Define a typed Pydantic ContractModel 'AttemptLedgerRecord' and validation function in evallab/schemas/ to parse attempt-level prompt/completion token usage and cost bounds before authorizing Canary model dispatch.

## Open questions (not resolved from disk)

1. What was the exact token consumption and dollar spend of the 8 attempts in generation's legacy run paid_glm45flash_001, which are currently recorded as usage_tokens: null, spend_usd: null?
   Looked in: /Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/generation/evidence/accounting-20260907/ and lanes/generation/campaign-20260908/accounting-qualification/canary_002_qualification.json
2. What were the resolved model identifiers, token totals, and costs for the native task workers (FacetManualRepair, RepoDiffRoute, Quality preparation workers), which returned null in worker calibration receipts?
   Looked in: /Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908/worker-calibration.json and lanes/generation/campaign-20260908/checkpoint.json
3. Is PR #388 (branch feat/lab-integration-audit-compare) intended to be merged into main before adding observed-summary.json compatibility for cohort-20260908, or should the reader extension be submitted as an incremental commit on PR #388?
   Looked in: pr://PeterMakhnatch/eval-lab/388, /Users/petermakhnatch/Developer/eval-lab/.worktrees/lab-integration-audit-compare/, and OMP-LANE-LEADS-2026-09-07.md section 280-285
4. What is the scientific disposition regarding the invalid reference MP3 audio (427 bytes, fails ffprobe with 'Invalid frame size') in task_000003 and the conflicting HTTPS instruction in task_000011—will new fixtures be authored or are these tasks disqualified from training candidate consideration?
   Looked in: /Users/petermakhnatch/Developer/research-context/harbor/corpus/ENVIRONMENT-QUALITY-AUDIT-SET-2026-09-07.md and artifacts/quality-cohort-20260908/campaign-summary.json
