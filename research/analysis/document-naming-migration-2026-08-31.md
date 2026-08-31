---
date: 2026-08-31
author: lane/naming-migration
summary: "Dated document naming migration dry-run inventory, classification breakdown, proposed renames, safety refusal audit, and inbound link rewrite plan across eval-lab and research-context."
status: raw
---

# Document Naming Migration Plan (2026-08-31)

## Executive Summary & Critical Preconditions

> **CRITICAL EXECUTION PRECONDITION**  
> **The PR queue on `eval-lab` (currently 9 open PRs) must be completely drained before executing `--apply`.**  
> Main merges frequently and multiple worktrees are active. Executing mass document renames while PRs are open will cause widespread merge conflicts. Execution of this migration is strictly gated on a quiet main branch with zero open PRs touching research directories.

This dry-run migration audits all dated markdown documents across:
- **Repo A (`eval-lab`)**: `research/inbox`, `research/analysis`, `research/explorations`
- **Repo B (`research-context`)**: `trajectory-analysis` (including subdirectories)

Living / navigational documents (`README.md`, `INDEX.md`, `INVENTORY.md`, `TAXONOMY.md`, `QUEUE.md`, etc.) and task package files (`instruction.md`, `steps/`, `tasks/`) are preserved as stable entry points and skipped from renaming.

## File Inventory and Classification Breakdown

| Repository | Already Conformant | Date-Suffixed | Undated | Refused (Unsafe) | Total Scanned |
|---|---|---|---|---|---|
| `eval-lab` | 0 | 23 | 28 | 7 | 58 |
| `research-context` | 6 | 34 | 68 | 10 | 118 |
| **Total** | **6** | **57** | **96** | **17** | **176** |

### Key Statistics
- **Total Audited Documents:** 176
- **Already Conformant:** 6 (no rename needed)
- **Proposed Renames (Ready):** 153
- **Refused Unsafe Cases (Collisions/PR Conflicts):** 17
- **Inbound Link Rewrites Planned:** 214

## Refused Files and Safety Audit

The following documents were refused from automated renaming to prevent global vault basename collisions, broken links, or merge conflicts:

| Repository | Original Path | Inferred Date | Date Source | Refusal Reason |
|---|---|---|---|---|
| `eval-lab` | `research/analysis/automated-trajectory-overnight-ledger.md` | 2026-08-26 | git-add | Referenced in open PR diff or active branch |
| `eval-lab` | `research/analysis/document-naming-migration-2026-08-31.md` | 2026-08-31 | filename-embedded | Referenced in open PR diff or active branch |
| `eval-lab` | `research/inbox/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md` | 2026-08-27 | filename-embedded | Target collision: duplicate global basename '2026-08-27-derivative-trajectory-feature-literature-map.md' across vault (eval-lab:research/inbox/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md, research-context:trajectory-analysis/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md) |
| `eval-lab` | `research/inbox/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md` | 2026-08-27 | front-matter (retrieved) | Target collision: duplicate global basename '2026-08-27-feature-to-synthetic-pipeline-contract.md' across vault (eval-lab:research/inbox/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md, research-context:trajectory-analysis/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md) |
| `eval-lab` | `research/inbox/NEXT-BENCHMARK-PROGRAM-TUTOR-REVIEW-2026-08-28.md` | 2026-08-28 | filename-embedded | Target collision: duplicate global basename '2026-08-28-next-benchmark-program-tutor-review.md' across vault (eval-lab:research/inbox/NEXT-BENCHMARK-PROGRAM-TUTOR-REVIEW-2026-08-28.md, research-context:trajectory-analysis/reviews/NEXT-BENCHMARK-PROGRAM-TUTOR-REVIEW-2026-08-28.md) |
| `eval-lab` | `research/inbox/TUTOR_CAPABILITY_CURVE_SPEC_ADVERSARIAL_REVIEW_2026-08-27.md` | 2026-08-27 | filename-embedded | Target collision: duplicate global basename '2026-08-27-tutor-capability-curve-spec-adversarial-review.md' across vault (eval-lab:research/inbox/TUTOR_CAPABILITY_CURVE_SPEC_ADVERSARIAL_REVIEW_2026-08-27.md, research-context:trajectory-analysis/reviews/TUTOR_CAPABILITY_CURVE_SPEC_ADVERSARIAL_REVIEW_2026-08-27.md) |
| `eval-lab` | `research/inbox/benchmark-themes-librarian-reply.md` | 2026-08-31 | front-matter (reviewed) | File has uncommitted changes in another active worktree |
| `research-context` | `trajectory-analysis/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md` | 2026-08-27 | filename-embedded | Target collision: duplicate global basename '2026-08-27-derivative-trajectory-feature-literature-map.md' across vault (eval-lab:research/inbox/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md, research-context:trajectory-analysis/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md) |
| `research-context` | `trajectory-analysis/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md` | 2026-08-27 | git-add | Target collision: duplicate global basename '2026-08-27-feature-to-synthetic-pipeline-contract.md' across vault (eval-lab:research/inbox/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md, research-context:trajectory-analysis/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md) |
| `research-context` | `trajectory-analysis/cards/agent-data-protocol.md` | 2026-08-25 | front-matter (reviewed) | Target collision: duplicate global basename '2026-08-25-agent-data-protocol.md' across vault (research-context:trajectory-analysis/cards/agent-data-protocol.md, research-context:trajectory-analysis/cards/storage/agent-data-protocol.md) |
| `research-context` | `trajectory-analysis/cards/bigquery-agent-analytics.md` | 2026-08-25 | front-matter (reviewed) | Target collision: duplicate global basename '2026-08-25-bigquery-agent-analytics.md' across vault (research-context:trajectory-analysis/cards/bigquery-agent-analytics.md, research-context:trajectory-analysis/cards/storage/bigquery-agent-analytics.md) |
| `research-context` | `trajectory-analysis/cards/formats/nvidia-atof.md` | 2026-08-25 | front-matter (reviewed) | Target collision: duplicate global basename '2026-08-25-nvidia-atof.md' across vault (research-context:trajectory-analysis/cards/formats/nvidia-atof.md, research-context:trajectory-analysis/cards/telemetry/nvidia-atof.md) |
| `research-context` | `trajectory-analysis/cards/storage/agent-data-protocol.md` | 2026-08-25 | front-matter (reviewed) | Target collision: duplicate global basename '2026-08-25-agent-data-protocol.md' across vault (research-context:trajectory-analysis/cards/agent-data-protocol.md, research-context:trajectory-analysis/cards/storage/agent-data-protocol.md) |
| `research-context` | `trajectory-analysis/cards/storage/bigquery-agent-analytics.md` | 2026-08-25 | front-matter (reviewed) | Target collision: duplicate global basename '2026-08-25-bigquery-agent-analytics.md' across vault (research-context:trajectory-analysis/cards/bigquery-agent-analytics.md, research-context:trajectory-analysis/cards/storage/bigquery-agent-analytics.md) |
| `research-context` | `trajectory-analysis/cards/telemetry/nvidia-atof.md` | 2026-08-25 | front-matter (reviewed) | Target collision: duplicate global basename '2026-08-25-nvidia-atof.md' across vault (research-context:trajectory-analysis/cards/formats/nvidia-atof.md, research-context:trajectory-analysis/cards/telemetry/nvidia-atof.md) |
| `research-context` | `trajectory-analysis/reviews/NEXT-BENCHMARK-PROGRAM-TUTOR-REVIEW-2026-08-28.md` | 2026-08-28 | filename-embedded | Target collision: duplicate global basename '2026-08-28-next-benchmark-program-tutor-review.md' across vault (eval-lab:research/inbox/NEXT-BENCHMARK-PROGRAM-TUTOR-REVIEW-2026-08-28.md, research-context:trajectory-analysis/reviews/NEXT-BENCHMARK-PROGRAM-TUTOR-REVIEW-2026-08-28.md) |
| `research-context` | `trajectory-analysis/reviews/TUTOR_CAPABILITY_CURVE_SPEC_ADVERSARIAL_REVIEW_2026-08-27.md` | 2026-08-27 | filename-embedded | Target collision: duplicate global basename '2026-08-27-tutor-capability-curve-spec-adversarial-review.md' across vault (eval-lab:research/inbox/TUTOR_CAPABILITY_CURVE_SPEC_ADVERSARIAL_REVIEW_2026-08-27.md, research-context:trajectory-analysis/reviews/TUTOR_CAPABILITY_CURVE_SPEC_ADVERSARIAL_REVIEW_2026-08-27.md) |

## Proposed Rename Plan

| Repository | Original Path | Classification | Inferred Date | Date Source | Proposed Path | Status |
|---|---|---|---|---|---|---|
| `eval-lab` | `research/analysis/agent-runtime-readiness-2026-08-31.md` | date-suffixed | 2026-08-31 | filename-embedded | `research/analysis/2026-08-31-agent-runtime-readiness.md` | READY |
| `eval-lab` | `research/analysis/automated-trajectory-overnight-ledger.md` | undated | 2026-08-26 | git-add | `research/analysis/2026-08-26-automated-trajectory-overnight-ledger.md` | REFUSED |
| `eval-lab` | `research/analysis/completed-trial-data-layer-backfill-contract.md` | undated | 2026-08-27 | git-add | `research/analysis/2026-08-27-completed-trial-data-layer-backfill-contract.md` | READY |
| `eval-lab` | `research/analysis/content-inventory-evidence-2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `research/analysis/2026-08-27-content-inventory-evidence.md` | READY |
| `eval-lab` | `research/analysis/document-naming-migration-2026-08-31.md` | date-suffixed | 2026-08-31 | filename-embedded | `research/analysis/2026-08-31-document-naming-migration.md` | REFUSED |
| `eval-lab` | `research/analysis/eval-lab-next-buildout-report-2026-08-31.md` | date-suffixed | 2026-08-31 | filename-embedded | `research/analysis/2026-08-31-eval-lab-next-buildout-report.md` | READY |
| `eval-lab` | `research/analysis/features-and-derived-analysis-meta.md` | undated | 2026-08-31 | front-matter (reviewed) | `research/analysis/2026-08-31-features-and-derived-analysis-meta.md` | READY |
| `eval-lab` | `research/analysis/git-estate-handoffs-2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `research/analysis/2026-08-27-git-estate-handoffs.md` | READY |
| `eval-lab` | `research/analysis/incremental-package-migration-plan.md` | undated | 2026-08-27 | front-matter (date) | `research/analysis/2026-08-27-incremental-package-migration-plan.md` | READY |
| `eval-lab` | `research/analysis/pr-186-architect-integration-review.md` | undated | 2026-08-26 | git-add | `research/analysis/2026-08-26-pr-186-architect-integration-review.md` | READY |
| `eval-lab` | `research/analysis/preserved-primary-evidence-AGENTS-2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `research/analysis/2026-08-27-preserved-primary-evidence-agents.md` | READY |
| `eval-lab` | `research/analysis/pstack-mechanism-gap-2026-08-29.md` | date-suffixed | 2026-08-29 | filename-embedded | `research/analysis/2026-08-29-pstack-mechanism-gap.md` | READY |
| `eval-lab` | `research/analysis/repo-stabilization-audit.md` | undated | 2026-08-26 | git-add | `research/analysis/2026-08-26-repo-stabilization-audit.md` | READY |
| `eval-lab` | `research/analysis/stage5-prompt.md` | undated | 2026-08-14 | git-add | `research/analysis/2026-08-14-stage5-prompt.md` | READY |
| `eval-lab` | `research/analysis/thematic-benchmark-portfolio.md` | undated | 2026-08-31 | front-matter (reviewed) | `research/analysis/2026-08-31-thematic-benchmark-portfolio.md` | READY |
| `eval-lab` | `research/analysis/trajectory-data-quality-audit-2026-08-19.md` | date-suffixed | 2026-08-19 | filename-embedded | `research/analysis/2026-08-19-trajectory-data-quality-audit.md` | READY |
| `eval-lab` | `research/analysis/trajectory-data-trust-audit-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `research/analysis/2026-08-26-trajectory-data-trust-audit.md` | READY |
| `eval-lab` | `research/analysis/zai-opencode-mcp-wave1-wave2-analysis-2026-08-29.md` | date-suffixed | 2026-08-29 | filename-embedded | `research/analysis/2026-08-29-zai-opencode-mcp-wave1-wave2-analysis.md` | READY |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | undated | 2026-08-13 | git-add | `research/explorations/2026-08-13-explorations.md` | READY |
| `eval-lab` | `research/explorations/harbor-021/01-harbor-check.md` | undated | 2026-08-13 | git-add | `research/explorations/harbor-021/2026-08-13-01-harbor-check.md` | READY |
| `eval-lab` | `research/explorations/harbor-021/02-harbor-analyze.md` | undated | 2026-08-13 | git-add | `research/explorations/harbor-021/2026-08-13-02-harbor-analyze.md` | READY |
| `eval-lab` | `research/explorations/harbor-021/03-job-plugin-api.md` | undated | 2026-08-13 | git-add | `research/explorations/harbor-021/2026-08-13-03-job-plugin-api.md` | READY |
| `eval-lab` | `research/explorations/harbor-021/04-harbor-exec.md` | undated | 2026-08-13 | git-add | `research/explorations/harbor-021/2026-08-13-04-harbor-exec.md` | READY |
| `eval-lab` | `research/explorations/harbor-021/05-harbor-hub-dataset.md` | undated | 2026-08-13 | git-add | `research/explorations/harbor-021/2026-08-13-05-harbor-hub-dataset.md` | READY |
| `eval-lab` | `research/explorations/harbor-021/06-multi-step-tasks.md` | undated | 2026-08-13 | git-add | `research/explorations/harbor-021/2026-08-13-06-multi-step-tasks.md` | READY |
| `eval-lab` | `research/explorations/harbor-021/07-network-allowlist.md` | undated | 2026-08-13 | git-add | `research/explorations/harbor-021/2026-08-13-07-network-allowlist.md` | READY |
| `eval-lab` | `research/explorations/harbor-021/08-separate-verifier.md` | undated | 2026-08-13 | git-add | `research/explorations/harbor-021/2026-08-13-08-separate-verifier.md` | READY |
| `eval-lab` | `research/explorations/harbor-021/09-harbor-atif2otel.md` | undated | 2026-08-13 | git-add | `research/explorations/harbor-021/2026-08-13-09-harbor-atif2otel.md` | READY |
| `eval-lab` | `research/explorations/pstack-agent-standards-2026-08.md` | undated | 2026-08-27 | front-matter (reviewed) | `research/explorations/2026-08-27-pstack-agent-standards.md` | READY |
| `eval-lab` | `research/inbox/C1-MATCHED-CAUSAL-AUDIT-AND-DECISION-SPEC-2026-08-29.md` | date-suffixed | 2026-08-29 | filename-embedded | `research/inbox/2026-08-29-c1-matched-causal-audit-and-decision-spec.md` | READY |
| `eval-lab` | `research/inbox/C2-INTERVENTION-GRADE-PROMOTION-GATE-2026-08-29.md` | date-suffixed | 2026-08-29 | filename-embedded | `research/inbox/2026-08-29-c2-intervention-grade-promotion-gate.md` | READY |
| `eval-lab` | `research/inbox/CAPABILITY-CURVE-ENGINE-SPEC-2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `research/inbox/2026-08-27-capability-curve-engine-spec.md` | READY |
| `eval-lab` | `research/inbox/CITATION-INTEGRITY-CORRECTION-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `research/inbox/2026-08-28-citation-integrity-correction.md` | READY |
| `eval-lab` | `research/inbox/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `research/inbox/2026-08-27-derivative-trajectory-feature-literature-map.md` | REFUSED |
| `eval-lab` | `research/inbox/DERIVATIVE-TRAJECTORY-FEATURE-RESEARCH-PROGRAM-2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `research/inbox/2026-08-27-derivative-trajectory-feature-research-program.md` | READY |
| `eval-lab` | `research/inbox/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md` | undated | 2026-08-27 | front-matter (retrieved) | `research/inbox/2026-08-27-feature-to-synthetic-pipeline-contract.md` | REFUSED |
| `eval-lab` | `research/inbox/NEXT-BENCHMARK-PROGRAM-ANALYST-REPLY-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `research/inbox/2026-08-28-next-benchmark-program-analyst-reply.md` | READY |
| `eval-lab` | `research/inbox/NEXT-BENCHMARK-PROGRAM-ARCHITECTURE-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `research/inbox/2026-08-28-next-benchmark-program-architecture.md` | READY |
| `eval-lab` | `research/inbox/NEXT-BENCHMARK-PROGRAM-CONTINUOUS-LOOP-ADDENDUM-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `research/inbox/2026-08-28-next-benchmark-program-continuous-loop-addendum.md` | READY |
| `eval-lab` | `research/inbox/NEXT-BENCHMARK-PROGRAM-DECISION-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `research/inbox/2026-08-28-next-benchmark-program-decision.md` | READY |
| `eval-lab` | `research/inbox/NEXT-BENCHMARK-PROGRAM-TUTOR-REVIEW-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `research/inbox/2026-08-28-next-benchmark-program-tutor-review.md` | REFUSED |
| `eval-lab` | `research/inbox/NEXT-BENCHMARK-TRAJECTORY-PROGRAM-BRIEF-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `research/inbox/2026-08-28-next-benchmark-trajectory-program-brief.md` | READY |
| `eval-lab` | `research/inbox/TRAJECTORY-ANALYSIS-COMPENDIUM-2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `research/inbox/2026-08-27-trajectory-analysis-compendium.md` | READY |
| `eval-lab` | `research/inbox/TUTOR_CAPABILITY_CURVE_SPEC_ADVERSARIAL_REVIEW_2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `research/inbox/2026-08-27-tutor-capability-curve-spec-adversarial-review.md` | REFUSED |
| `eval-lab` | `research/inbox/benchmark-themes-brief.md` | undated | 2026-08-31 | front-matter (retrieved) | `research/inbox/2026-08-31-benchmark-themes-brief.md` | READY |
| `eval-lab` | `research/inbox/benchmark-themes-librarian-reply.md` | undated | 2026-08-31 | front-matter (reviewed) | `research/inbox/2026-08-31-benchmark-themes-librarian-reply.md` | REFUSED |
| `eval-lab` | `research/inbox/drive-evals-benchmarks.md` | undated | 2026-08-19 | front-matter (retrieved) | `research/inbox/2026-08-19-drive-evals-benchmarks.md` | READY |
| `eval-lab` | `research/inbox/drive-salvage-2026-08-18.md` | date-suffixed | 2026-08-18 | filename-embedded | `research/inbox/2026-08-18-drive-salvage.md` | READY |
| `eval-lab` | `research/inbox/egs-best-practices.md` | undated | 2026-08-19 | front-matter (retrieved) | `research/inbox/2026-08-19-egs-best-practices.md` | READY |
| `eval-lab` | `research/inbox/feature-analysis-meta-analyst-reply.md` | undated | 2026-08-31 | front-matter (retrieved) | `research/inbox/2026-08-31-feature-analysis-meta-analyst-reply.md` | READY |
| `eval-lab` | `research/inbox/feature-analysis-meta-brief.md` | undated | 2026-08-31 | front-matter (retrieved) | `research/inbox/2026-08-31-feature-analysis-meta-brief.md` | READY |
| `eval-lab` | `research/inbox/meta-task-B-dimensions.md` | undated | 2026-08-19 | front-matter (retrieved) | `research/inbox/2026-08-19-meta-task-b-dimensions.md` | READY |
| `eval-lab` | `research/inbox/meta-task-D-review-rubric.md` | undated | 2026-08-19 | front-matter (retrieved) | `research/inbox/2026-08-19-meta-task-d-review-rubric.md` | READY |
| `eval-lab` | `research/inbox/meta-task-F1-instruction-template.md` | undated | 2026-08-19 | front-matter (retrieved) | `research/inbox/2026-08-19-meta-task-f1-instruction-template.md` | READY |
| `eval-lab` | `research/inbox/meta-task-F2-spec-design-prompt.md` | undated | 2026-08-19 | front-matter (retrieved) | `research/inbox/2026-08-19-meta-task-f2-spec-design-prompt.md` | READY |
| `eval-lab` | `research/inbox/meta-task-F3-trajectory-judge.md` | undated | 2026-08-19 | front-matter (retrieved) | `research/inbox/2026-08-19-meta-task-f3-trajectory-judge.md` | READY |
| `eval-lab` | `research/inbox/repo-custodian-state-report-2026-08-29-reply.md` | date-suffixed | 2026-08-29 | filename-embedded | `research/inbox/2026-08-29-repo-custodian-state-report-reply.md` | READY |
| `eval-lab` | `research/inbox/tutor-state-report-2026-08-29-reply.md` | date-suffixed | 2026-08-29 | filename-embedded | `research/inbox/2026-08-29-tutor-state-report-reply.md` | READY |
| `research-context` | `trajectory-analysis/2026-08-26-analyst-final-findings.md` | already-conformant | 2026-08-26 | filename-prefix | `trajectory-analysis/2026-08-26-analyst-final-findings.md` | ALREADY CONFORMANT |
| `research-context` | `trajectory-analysis/2026-08-26-analyst-handoff-false-completion-seed.md` | already-conformant | 2026-08-26 | filename-prefix | `trajectory-analysis/2026-08-26-analyst-handoff-false-completion-seed.md` | ALREADY CONFORMANT |
| `research-context` | `trajectory-analysis/2026-08-26-analyst-improvement-requests.md` | already-conformant | 2026-08-26 | filename-prefix | `trajectory-analysis/2026-08-26-analyst-improvement-requests.md` | ALREADY CONFORMANT |
| `research-context` | `trajectory-analysis/2026-08-26-analyst-termination-claims-deep-dive.md` | already-conformant | 2026-08-26 | filename-prefix | `trajectory-analysis/2026-08-26-analyst-termination-claims-deep-dive.md` | ALREADY CONFORMANT |
| `research-context` | `trajectory-analysis/2026-08-27-a2-claim-classifier-hold-redesign-brief.md` | already-conformant | 2026-08-27 | filename-prefix | `trajectory-analysis/2026-08-27-a2-claim-classifier-hold-redesign-brief.md` | ALREADY CONFORMANT |
| `research-context` | `trajectory-analysis/AUTOMATED-INTERPRETATION-EXA-PROVENANCE.md` | undated | 2026-08-26 | git-add | `trajectory-analysis/2026-08-26-automated-interpretation-exa-provenance.md` | READY |
| `research-context` | `trajectory-analysis/AUTOMATED-INTERPRETATION-SOURCE-CATALOG.md` | undated | 2026-08-26 | front-matter (reviewed) | `trajectory-analysis/2026-08-26-automated-interpretation-source-catalog.md` | READY |
| `research-context` | `trajectory-analysis/AUTOMATED-TRAJECTORY-INTERPRETATION-PROGRAM-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-automated-trajectory-interpretation-program.md` | READY |
| `research-context` | `trajectory-analysis/AUTOMATED-TRAJECTORY-LONG-HORIZON-ASSIGNMENTS-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-automated-trajectory-long-horizon-assignments.md` | READY |
| `research-context` | `trajectory-analysis/CALIBRATION-AND-FALSIFICATION-PROTOCOL-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-calibration-and-falsification-protocol.md` | READY |
| `research-context` | `trajectory-analysis/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `trajectory-analysis/2026-08-27-derivative-trajectory-feature-literature-map.md` | REFUSED |
| `research-context` | `trajectory-analysis/EVAL-LAB-SOURCE-TO-SOFTWARE-DELTA-MATRIX-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-eval-lab-source-to-software-delta-matrix.md` | READY |
| `research-context` | `trajectory-analysis/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md` | undated | 2026-08-27 | git-add | `trajectory-analysis/2026-08-27-feature-to-synthetic-pipeline-contract.md` | REFUSED |
| `research-context` | `trajectory-analysis/HARBOR-INSTRUMENTATION-CAPABILITY-MATRIX-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-harbor-instrumentation-capability-matrix.md` | READY |
| `research-context` | `trajectory-analysis/HUMAN-MULTIRATER-SOURCE-AUDIT-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-human-multirater-source-audit.md` | READY |
| `research-context` | `trajectory-analysis/INDEPENDENT-TRAJECTORY-METHOD-MATRIX-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-independent-trajectory-method-matrix.md` | READY |
| `research-context` | `trajectory-analysis/IR-EVIDENCEPACK-SCHEMA-COMPARISON.md` | undated | 2026-08-26 | front-matter (reviewed) | `trajectory-analysis/2026-08-26-ir-evidencepack-schema-comparison.md` | READY |
| `research-context` | `trajectory-analysis/MACHINE-JUDGMENT-CALIBRATION-HANDOFF.md` | undated | 2026-08-26 | front-matter (reviewed) | `trajectory-analysis/2026-08-26-machine-judgment-calibration-handoff.md` | READY |
| `research-context` | `trajectory-analysis/MASTER_ATIF_AND_TRAJECTORY_LANDSCAPE_2026.md` | undated | 2026-08-25 | git-add | `trajectory-analysis/2026-08-25-master-atif-and-trajectory-landscape-2026.md` | READY |
| `research-context` | `trajectory-analysis/OVERNIGHT-TRAJECTORY-AUTOMATION-LOOP-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-overnight-trajectory-automation-loop.md` | READY |
| `research-context` | `trajectory-analysis/PSTACK-REPO-STANDARDS-REVIEW-2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `trajectory-analysis/2026-08-27-pstack-repo-standards-review.md` | READY |
| `research-context` | `trajectory-analysis/REPLICATION-REPORT-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-replication-report.md` | READY |
| `research-context` | `trajectory-analysis/TRAJECTORY-ANALYSIS-RECIPES-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-trajectory-analysis-recipes.md` | READY |
| `research-context` | `trajectory-analysis/TRAJECTORY-METHODS-EXA-PROVENANCE-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-trajectory-methods-exa-provenance.md` | READY |
| `research-context` | `trajectory-analysis/TRAJECTORY-STATISTICAL-METHOD-CATALOG-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-trajectory-statistical-method-catalog.md` | READY |
| `research-context` | `trajectory-analysis/TRAJECTORY-STATISTICAL-METHOD-MATRIX-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-trajectory-statistical-method-matrix.md` | READY |
| `research-context` | `trajectory-analysis/TRAJECTORY-TO-SYNTHETIC-FUNNEL-HANDOFF-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-trajectory-to-synthetic-funnel-handoff.md` | READY |
| `research-context` | `trajectory-analysis/TRAJECTORY-WORK-ORDERS-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-trajectory-work-orders.md` | READY |
| `research-context` | `trajectory-analysis/TUTOR_ADVERSARIAL_REVIEW_2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-tutor-adversarial-review.md` | READY |
| `research-context` | `trajectory-analysis/TUTOR_ADVERSARIAL_VERIFICATION_P0_DEFECTS.md` | undated | 2026-08-26 | mtime | `trajectory-analysis/2026-08-26-tutor-adversarial-verification-p0-defects.md` | READY |
| `research-context` | `trajectory-analysis/calibration/CALIBRATION-REPORT-V1.md` | undated | 2026-08-26 | front-matter (reviewed) | `trajectory-analysis/calibration/2026-08-26-calibration-report-v1.md` | READY |
| `research-context` | `trajectory-analysis/calibration/FROZEN-CONTRACT-V1.md` | undated | 2026-08-26 | git-add | `trajectory-analysis/calibration/2026-08-26-frozen-contract-v1.md` | READY |
| `research-context` | `trajectory-analysis/calibration/GEMINI-CAPACITY-RERUN-V1.md` | undated | 2026-08-26 | git-add | `trajectory-analysis/calibration/2026-08-26-gemini-capacity-rerun-v1.md` | READY |
| `research-context` | `trajectory-analysis/calibration/SIGNED-HANDOFF-V1.md` | undated | 2026-08-26 | git-add | `trajectory-analysis/calibration/2026-08-26-signed-handoff-v1.md` | READY |
| `research-context` | `trajectory-analysis/calibration/human-baseline-protocol.md` | undated | 2026-08-26 | git-add | `trajectory-analysis/calibration/2026-08-26-human-baseline-protocol.md` | READY |
| `research-context` | `trajectory-analysis/calibration/judge-prompt-v1.md` | undated | 2026-08-26 | git-add | `trajectory-analysis/calibration/2026-08-26-judge-prompt-v1.md` | READY |
| `research-context` | `trajectory-analysis/calibration/runs/judge-prompt-leaky-v0.md` | undated | 2026-08-26 | git-add | `trajectory-analysis/calibration/runs/2026-08-26-judge-prompt-leaky-v0.md` | READY |
| `research-context` | `trajectory-analysis/cards/agent-data-protocol.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/2026-08-25-agent-data-protocol.md` | REFUSED |
| `research-context` | `trajectory-analysis/cards/bigquery-agent-analytics.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/2026-08-25-bigquery-agent-analytics.md` | REFUSED |
| `research-context` | `trajectory-analysis/cards/daydream.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/2026-08-25-daydream.md` | READY |
| `research-context` | `trajectory-analysis/cards/diagnostics/agentcheck.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/diagnostics/2026-08-25-agentcheck.md` | READY |
| `research-context` | `trajectory-analysis/cards/diagnostics/agentdiagnose.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/diagnostics/2026-08-25-agentdiagnose.md` | READY |
| `research-context` | `trajectory-analysis/cards/diagnostics/agentprocessbench.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/diagnostics/2026-08-25-agentprocessbench.md` | READY |
| `research-context` | `trajectory-analysis/cards/diagnostics/critictool-recovery.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/diagnostics/2026-08-25-critictool-recovery.md` | READY |
| `research-context` | `trajectory-analysis/cards/diagnostics/safari-attribution.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/diagnostics/2026-08-25-safari-attribution.md` | READY |
| `research-context` | `trajectory-analysis/cards/diagnostics/science-agent-reliability.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/diagnostics/2026-08-25-science-agent-reliability.md` | READY |
| `research-context` | `trajectory-analysis/cards/diagnostics/toolbench-x.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/diagnostics/2026-08-25-toolbench-x.md` | READY |
| `research-context` | `trajectory-analysis/cards/diagnostics/toolmaze-recovery.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/diagnostics/2026-08-25-toolmaze-recovery.md` | READY |
| `research-context` | `trajectory-analysis/cards/diagnostics/trail-error-taxonomy.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/diagnostics/2026-08-25-trail-error-taxonomy.md` | READY |
| `research-context` | `trajectory-analysis/cards/diagnostics/trajdebug-error-lifecycle.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/diagnostics/2026-08-25-trajdebug-error-lifecycle.md` | READY |
| `research-context` | `trajectory-analysis/cards/formats/agent-trail.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-agent-trail.md` | READY |
| `research-context` | `trajectory-analysis/cards/formats/agentastra.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-agentastra.md` | READY |
| `research-context` | `trajectory-analysis/cards/formats/agentsight-agentvis.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-agentsight-agentvis.md` | READY |
| `research-context` | `trajectory-analysis/cards/formats/atif-preview.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-atif-preview.md` | READY |
| `research-context` | `trajectory-analysis/cards/formats/atifact.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-atifact.md` | READY |
| `research-context` | `trajectory-analysis/cards/formats/graphectory.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-graphectory.md` | READY |
| `research-context` | `trajectory-analysis/cards/formats/harbor-atif-rfc.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-harbor-atif-rfc.md` | READY |
| `research-context` | `trajectory-analysis/cards/formats/nvidia-atof.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-nvidia-atof.md` | REFUSED |
| `research-context` | `trajectory-analysis/cards/formats/openhands-trajectory-visualizer.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-openhands-trajectory-visualizer.md` | READY |
| `research-context` | `trajectory-analysis/cards/formats/pair-agent-trace-vis.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-pair-agent-trace-vis.md` | READY |
| `research-context` | `trajectory-analysis/cards/formats/python-atif.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-python-atif.md` | READY |
| `research-context` | `trajectory-analysis/cards/formats/theanecdote-trajectory.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/formats/2026-08-25-theanecdote-trajectory.md` | READY |
| `research-context` | `trajectory-analysis/cards/nemo-agent-toolkit.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/2026-08-25-nemo-agent-toolkit.md` | READY |
| `research-context` | `trajectory-analysis/cards/scoring/deterministic-metrics.md` | undated | 2026-08-25 | git-add | `trajectory-analysis/cards/scoring/2026-08-25-deterministic-metrics.md` | READY |
| `research-context` | `trajectory-analysis/cards/scoring/human-labels.md` | undated | 2026-08-25 | git-add | `trajectory-analysis/cards/scoring/2026-08-25-human-labels.md` | READY |
| `research-context` | `trajectory-analysis/cards/scoring/langchain-agentevals.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/scoring/2026-08-25-langchain-agentevals.md` | READY |
| `research-context` | `trajectory-analysis/cards/scoring/meta-task-d-package-criteria.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/scoring/2026-08-25-meta-task-d-package-criteria.md` | READY |
| `research-context` | `trajectory-analysis/cards/scoring/meta-task-f3-trajectory-judge.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/scoring/2026-08-25-meta-task-f3-trajectory-judge.md` | READY |
| `research-context` | `trajectory-analysis/cards/scoring/model-judges.md` | undated | 2026-08-25 | git-add | `trajectory-analysis/cards/scoring/2026-08-25-model-judges.md` | READY |
| `research-context` | `trajectory-analysis/cards/scoring/recovery-bench-state-replay.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/scoring/2026-08-25-recovery-bench-state-replay.md` | READY |
| `research-context` | `trajectory-analysis/cards/scoring/toolprmbench.md` | undated | 2026-08-26 | front-matter (reviewed) | `trajectory-analysis/cards/scoring/2026-08-26-toolprmbench.md` | READY |
| `research-context` | `trajectory-analysis/cards/scoring/vertex-trajectory-match.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/scoring/2026-08-25-vertex-trajectory-match.md` | READY |
| `research-context` | `trajectory-analysis/cards/scoring/vestige-repeated-trial.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/scoring/2026-08-25-vestige-repeated-trial.md` | READY |
| `research-context` | `trajectory-analysis/cards/scoring/webclipper.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/scoring/2026-08-25-webclipper.md` | READY |
| `research-context` | `trajectory-analysis/cards/storage/agent-data-protocol.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/storage/2026-08-25-agent-data-protocol.md` | REFUSED |
| `research-context` | `trajectory-analysis/cards/storage/atif-subagent-trajectories.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/storage/2026-08-25-atif-subagent-trajectories.md` | READY |
| `research-context` | `trajectory-analysis/cards/storage/bigquery-agent-analytics.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/storage/2026-08-25-bigquery-agent-analytics.md` | REFUSED |
| `research-context` | `trajectory-analysis/cards/storage/daydream-runs.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/storage/2026-08-25-daydream-runs.md` | READY |
| `research-context` | `trajectory-analysis/cards/storage/harbor-trial-artifacts.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/storage/2026-08-25-harbor-trial-artifacts.md` | READY |
| `research-context` | `trajectory-analysis/cards/storage/nemo-nat-atif-samples.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/storage/2026-08-25-nemo-nat-atif-samples.md` | READY |
| `research-context` | `trajectory-analysis/cards/storage/otel-atif-correlation.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/storage/2026-08-25-otel-atif-correlation.md` | READY |
| `research-context` | `trajectory-analysis/cards/storage/vestige-run-jsonl.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/storage/2026-08-25-vestige-run-jsonl.md` | READY |
| `research-context` | `trajectory-analysis/cards/telemetry/agentops.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/telemetry/2026-08-25-agentops.md` | READY |
| `research-context` | `trajectory-analysis/cards/telemetry/atof-to-atif.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/telemetry/2026-08-25-atof-to-atif.md` | READY |
| `research-context` | `trajectory-analysis/cards/telemetry/harbor-atif-metrics.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/telemetry/2026-08-25-harbor-atif-metrics.md` | READY |
| `research-context` | `trajectory-analysis/cards/telemetry/langfuse.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/telemetry/2026-08-25-langfuse.md` | READY |
| `research-context` | `trajectory-analysis/cards/telemetry/langsmith-agentevals.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/telemetry/2026-08-25-langsmith-agentevals.md` | READY |
| `research-context` | `trajectory-analysis/cards/telemetry/nemo-relay-atif-export.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/telemetry/2026-08-25-nemo-relay-atif-export.md` | READY |
| `research-context` | `trajectory-analysis/cards/telemetry/nvidia-atof.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/telemetry/2026-08-25-nvidia-atof.md` | REFUSED |
| `research-context` | `trajectory-analysis/cards/telemetry/openinference.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/telemetry/2026-08-25-openinference.md` | READY |
| `research-context` | `trajectory-analysis/cards/telemetry/phoenix.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/telemetry/2026-08-25-phoenix.md` | READY |
| `research-context` | `trajectory-analysis/cards/telemetry/weave.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/telemetry/2026-08-25-weave.md` | READY |
| `research-context` | `trajectory-analysis/cards/trajdebug.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/2026-08-25-trajdebug.md` | READY |
| `research-context` | `trajectory-analysis/cards/vestige.md` | undated | 2026-08-25 | front-matter (reviewed) | `trajectory-analysis/cards/2026-08-25-vestige.md` | READY |
| `research-context` | `trajectory-analysis/curriculum/BENCHMARK-SUBSTRATE-LICENSE-AUDIT-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `trajectory-analysis/curriculum/2026-08-28-benchmark-substrate-license-audit.md` | READY |
| `research-context` | `trajectory-analysis/curriculum/RUNTIME-MECHANICS-READING-SPINE-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `trajectory-analysis/curriculum/2026-08-28-runtime-mechanics-reading-spine.md` | READY |
| `research-context` | `trajectory-analysis/curriculum/TRACE-TO-SYNTHETIC-EXEMPLAR-SPINE-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `trajectory-analysis/curriculum/2026-08-28-trace-to-synthetic-exemplar-spine.md` | READY |
| `research-context` | `trajectory-analysis/curriculum/TUTOR-RUNTIME-SIDE-PRIMER-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `trajectory-analysis/curriculum/2026-08-28-tutor-runtime-side-primer.md` | READY |
| `research-context` | `trajectory-analysis/curriculum/TUTOR_ANALYST_SPLIT_CONTRACT_2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `trajectory-analysis/curriculum/2026-08-28-tutor-analyst-split-contract.md` | READY |
| `research-context` | `trajectory-analysis/curriculum/TUTOR_REVIEW_OF_ANALYST_PRIMER_2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `trajectory-analysis/curriculum/2026-08-28-tutor-review-of-analyst-primer.md` | READY |
| `research-context` | `trajectory-analysis/opinions/2026-08-25-analyst-trajectory-analysis-program.md` | already-conformant | 2026-08-25 | filename-prefix | `trajectory-analysis/opinions/2026-08-25-analyst-trajectory-analysis-program.md` | ALREADY CONFORMANT |
| `research-context` | `trajectory-analysis/platform-engineer-handoff-2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/2026-08-26-platform-engineer-handoff.md` | READY |
| `research-context` | `trajectory-analysis/recipes/analysis-recipe-contracts-v1.md` | undated | 2026-08-26 | front-matter (date) | `trajectory-analysis/recipes/2026-08-26-analysis-recipe-contracts-v1.md` | READY |
| `research-context` | `trajectory-analysis/reviews/NEXT-BENCHMARK-PROGRAM-TUTOR-REVIEW-2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `trajectory-analysis/reviews/2026-08-28-next-benchmark-program-tutor-review.md` | REFUSED |
| `research-context` | `trajectory-analysis/reviews/TUTOR_AUTOMATED_INTERPRETATION_AUDIT_2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/reviews/2026-08-26-tutor-automated-interpretation-audit.md` | READY |
| `research-context` | `trajectory-analysis/reviews/TUTOR_CAPABILITY_CURVE_SPEC_ADVERSARIAL_REVIEW_2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `trajectory-analysis/reviews/2026-08-27-tutor-capability-curve-spec-adversarial-review.md` | REFUSED |
| `research-context` | `trajectory-analysis/reviews/TUTOR_COMPLETED_TRIAL_DATA_LAYER_AUDIT_2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/reviews/2026-08-26-tutor-completed-trial-data-layer-audit.md` | READY |
| `research-context` | `trajectory-analysis/reviews/TUTOR_COMPLETED_TRIAL_DATA_LAYER_BACKFILL_CONTRACT_AUDIT_2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/reviews/2026-08-26-tutor-completed-trial-data-layer-backfill-contract-audit.md` | READY |
| `research-context` | `trajectory-analysis/reviews/TUTOR_GOLDSET_DIVISION_AND_FINDINGS_2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `trajectory-analysis/reviews/2026-08-28-tutor-goldset-division-and-findings.md` | READY |
| `research-context` | `trajectory-analysis/reviews/TUTOR_MERGED_ANALYSIS_RECIPES_AUDIT_2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/reviews/2026-08-26-tutor-merged-analysis-recipes-audit.md` | READY |
| `research-context` | `trajectory-analysis/reviews/TUTOR_PR253_KSTAR_VALIDATION_ADVERSARIAL_REVIEW_2026-08-27.md` | date-suffixed | 2026-08-27 | filename-embedded | `trajectory-analysis/reviews/2026-08-27-tutor-pr253-kstar-validation-adversarial-review.md` | READY |
| `research-context` | `trajectory-analysis/reviews/TUTOR_PR254_KSTAR_ARM2_ADVERSARIAL_REVIEW_2026-08-28.md` | date-suffixed | 2026-08-28 | filename-embedded | `trajectory-analysis/reviews/2026-08-28-tutor-pr254-kstar-arm2-adversarial-review.md` | READY |
| `research-context` | `trajectory-analysis/reviews/TUTOR_RECOVERY_MEMORY_AUDIT_2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/reviews/2026-08-26-tutor-recovery-memory-audit.md` | READY |
| `research-context` | `trajectory-analysis/reviews/TUTOR_SOURCE_TO_SOFTWARE_DELTA_REVIEW_2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/reviews/2026-08-26-tutor-source-to-software-delta-review.md` | READY |
| `research-context` | `trajectory-analysis/reviews/TUTOR_TRAJECTORY_CLAIM_REVIEW_2026-08-26.md` | date-suffixed | 2026-08-26 | filename-embedded | `trajectory-analysis/reviews/2026-08-26-tutor-trajectory-claim-review.md` | READY |
| `research-context` | `trajectory-analysis/synthetic-funnel/ENGINEER-BRIEF-FAMILY-A.md` | undated | 2026-08-26 | git-add | `trajectory-analysis/synthetic-funnel/2026-08-26-engineer-brief-family-a.md` | READY |
| `research-context` | `trajectory-analysis/synthetic-funnel/FUNNEL-DECISION-V1.md` | undated | 2026-08-26 | git-add | `trajectory-analysis/synthetic-funnel/2026-08-26-funnel-decision-v1.md` | READY |
| `research-context` | `trajectory-analysis/synthetic-funnel/ONTOLOGY-GAP-REVIEW-V1.md` | undated | 2026-08-26 | git-add | `trajectory-analysis/synthetic-funnel/2026-08-26-ontology-gap-review-v1.md` | READY |

## Inbound Link Rewrite Plan

A total of **214** inbound Markdown links and wikilinks across both repositories reference files scheduled for renaming.

| Source Repo | Source Document | Line | Link Type | Original Target | Proposed Target |
|---|---|---|---|---|---|
| `eval-lab` | `docs/GIANT-FILE-SPLIT-PROPOSALS.md` | 12 | markdown | `research/analysis/repo-stabilization-audit.md` | `research/analysis/2026-08-26-repo-stabilization-audit.md` |
| `eval-lab` | `docs/git-estate-inventory.md` | 65 | markdown | `../research/analysis/git-estate-handoffs-2026-08-27.md` | `../research/analysis/2026-08-27-git-estate-handoffs.md` |
| `eval-lab` | `docs/git-estate-inventory.md` | 66 | markdown | `../research/analysis/preserved-primary-evidence-AGENTS-2026-08-27.md` | `../research/analysis/2026-08-27-preserved-primary-evidence-agents.md` |
| `eval-lab` | `research/analysis/git-estate-handoffs-2026-08-27.md` | 58 | markdown | `preserved-primary-evidence-AGENTS-2026-08-27.md` | `2026-08-27-preserved-primary-evidence-agents.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 15 | markdown | `harbor-021/09-harbor-atif2otel.md` | `harbor-021/2026-08-13-09-harbor-atif2otel.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 16 | markdown | `harbor-021/03-job-plugin-api.md` | `harbor-021/2026-08-13-03-job-plugin-api.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 17 | markdown | `harbor-021/08-separate-verifier.md` | `harbor-021/2026-08-13-08-separate-verifier.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 18 | markdown | `harbor-021/05-harbor-hub-dataset.md` | `harbor-021/2026-08-13-05-harbor-hub-dataset.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 19 | markdown | `harbor-021/01-harbor-check.md` | `harbor-021/2026-08-13-01-harbor-check.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 20 | markdown | `harbor-021/02-harbor-analyze.md` | `harbor-021/2026-08-13-02-harbor-analyze.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 21 | markdown | `harbor-021/06-multi-step-tasks.md` | `harbor-021/2026-08-13-06-multi-step-tasks.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 27 | markdown | `harbor-021/04-harbor-exec.md` | `harbor-021/2026-08-13-04-harbor-exec.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 28 | markdown | `harbor-021/05-harbor-hub-dataset.md` | `harbor-021/2026-08-13-05-harbor-hub-dataset.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 29 | markdown | `harbor-021/07-network-allowlist.md` | `harbor-021/2026-08-13-07-network-allowlist.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 30 | markdown | `harbor-021/03-job-plugin-api.md` | `harbor-021/2026-08-13-03-job-plugin-api.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 31 | markdown | `harbor-021/01-harbor-check.md` | `harbor-021/2026-08-13-01-harbor-check.md` |
| `eval-lab` | `research/explorations/EXPLORATIONS.md` | 31 | markdown | `harbor-021/02-harbor-analyze.md` | `harbor-021/2026-08-13-02-harbor-analyze.md` |
| `eval-lab` | `research/inbox/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md` | 27 | markdown | `CITATION-INTEGRITY-CORRECTION-2026-08-28.md` | `2026-08-28-citation-integrity-correction.md` |
| `research-context` | `trajectory-analysis/AUTOMATED-INTERPRETATION-SOURCE-CATALOG.md` | 88 | markdown | `AUTOMATED-INTERPRETATION-EXA-PROVENANCE.md` | `2026-08-26-automated-interpretation-exa-provenance.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 22 | markdown | `TRAJECTORY-STATISTICAL-METHOD-MATRIX-2026-08-26.md` | `2026-08-26-trajectory-statistical-method-matrix.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 22 | markdown | `TRAJECTORY-STATISTICAL-METHOD-CATALOG-2026-08-26.md` | `2026-08-26-trajectory-statistical-method-catalog.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 23 | markdown | `TRAJECTORY-ANALYSIS-RECIPES-2026-08-26.md` | `2026-08-26-trajectory-analysis-recipes.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 23 | markdown | `TRAJECTORY-METHODS-EXA-PROVENANCE-2026-08-26.md` | `2026-08-26-trajectory-methods-exa-provenance.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 24 | markdown | `TRAJECTORY-TO-SYNTHETIC-FUNNEL-HANDOFF-2026-08-26.md` | `2026-08-26-trajectory-to-synthetic-funnel-handoff.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 25 | markdown | `PSTACK-REPO-STANDARDS-REVIEW-2026-08-27.md` | `2026-08-27-pstack-repo-standards-review.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 27 | markdown | `EVAL-LAB-SOURCE-TO-SOFTWARE-DELTA-MATRIX-2026-08-26.md` | `2026-08-26-eval-lab-source-to-software-delta-matrix.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 27 | markdown | `TRAJECTORY-STATISTICAL-METHOD-MATRIX-2026-08-26.md` | `2026-08-26-trajectory-statistical-method-matrix.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 28 | markdown | `HARBOR-INSTRUMENTATION-CAPABILITY-MATRIX-2026-08-26.md` | `2026-08-26-harbor-instrumentation-capability-matrix.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 28 | markdown | `cards/telemetry/harbor-atif-metrics.md` | `cards/telemetry/2026-08-25-harbor-atif-metrics.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 29 | markdown | `IR-EVIDENCEPACK-SCHEMA-COMPARISON.md` | `2026-08-26-ir-evidencepack-schema-comparison.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 29 | markdown | `AUTOMATED-INTERPRETATION-SOURCE-CATALOG.md` | `2026-08-26-automated-interpretation-source-catalog.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 30 | markdown | `MACHINE-JUDGMENT-CALIBRATION-HANDOFF.md` | `2026-08-26-machine-judgment-calibration-handoff.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 30 | markdown | `AUTOMATED-TRAJECTORY-INTERPRETATION-PROGRAM-2026-08-26.md` | `2026-08-26-automated-trajectory-interpretation-program.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 31 | markdown | `HUMAN-MULTIRATER-SOURCE-AUDIT-2026-08-26.md` | `2026-08-26-human-multirater-source-audit.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 31 | markdown | `calibration/human-baseline-protocol.md` | `calibration/2026-08-26-human-baseline-protocol.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 32 | markdown | `AUTOMATED-INTERPRETATION-SOURCE-CATALOG.md` | `2026-08-26-automated-interpretation-source-catalog.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 32 | markdown | `AUTOMATED-INTERPRETATION-EXA-PROVENANCE.md` | `2026-08-26-automated-interpretation-exa-provenance.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 33 | markdown | `cards/formats/theanecdote-trajectory.md` | `cards/formats/2026-08-25-theanecdote-trajectory.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 33 | markdown | `cards/formats/graphectory.md` | `cards/formats/2026-08-25-graphectory.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 34 | markdown | `cards/formats/atif-preview.md` | `cards/formats/2026-08-25-atif-preview.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 34 | markdown | `cards/formats/openhands-trajectory-visualizer.md` | `cards/formats/2026-08-25-openhands-trajectory-visualizer.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 34 | markdown | `cards/formats/agent-trail.md` | `cards/formats/2026-08-25-agent-trail.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 34 | markdown | `cards/formats/pair-agent-trace-vis.md` | `cards/formats/2026-08-25-pair-agent-trace-vis.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 35 | markdown | `cards/nemo-agent-toolkit.md` | `cards/2026-08-25-nemo-agent-toolkit.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 35 | markdown | `cards/formats/agentsight-agentvis.md` | `cards/formats/2026-08-25-agentsight-agentvis.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 36 | markdown | `cards/daydream.md` | `cards/2026-08-25-daydream.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 36 | markdown | `cards/scoring/langchain-agentevals.md` | `cards/scoring/2026-08-25-langchain-agentevals.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 37 | markdown | `cards/vestige.md` | `cards/2026-08-25-vestige.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 37 | markdown | `cards/scoring/vestige-repeated-trial.md` | `cards/scoring/2026-08-25-vestige-repeated-trial.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 38 | markdown | `cards/trajdebug.md` | `cards/2026-08-25-trajdebug.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 41 | markdown | `cards/formats/harbor-atif-rfc.md` | `cards/formats/2026-08-25-harbor-atif-rfc.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 41 | markdown | `cards/formats/python-atif.md` | `cards/formats/2026-08-25-python-atif.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 41 | markdown | `cards/formats/atifact.md` | `cards/formats/2026-08-25-atifact.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 48 | markdown | `AUTOMATED-INTERPRETATION-SOURCE-CATALOG.md` | `2026-08-26-automated-interpretation-source-catalog.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 49 | markdown | `IR-EVIDENCEPACK-SCHEMA-COMPARISON.md` | `2026-08-26-ir-evidencepack-schema-comparison.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 50 | markdown | `MACHINE-JUDGMENT-CALIBRATION-HANDOFF.md` | `2026-08-26-machine-judgment-calibration-handoff.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 51 | markdown | `HUMAN-MULTIRATER-SOURCE-AUDIT-2026-08-26.md` | `2026-08-26-human-multirater-source-audit.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 52 | markdown | `TRAJECTORY-STATISTICAL-METHOD-CATALOG-2026-08-26.md` | `2026-08-26-trajectory-statistical-method-catalog.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 53 | markdown | `TRAJECTORY-STATISTICAL-METHOD-MATRIX-2026-08-26.md` | `2026-08-26-trajectory-statistical-method-matrix.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 54 | markdown | `TRAJECTORY-ANALYSIS-RECIPES-2026-08-26.md` | `2026-08-26-trajectory-analysis-recipes.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 55 | markdown | `TRAJECTORY-TO-SYNTHETIC-FUNNEL-HANDOFF-2026-08-26.md` | `2026-08-26-trajectory-to-synthetic-funnel-handoff.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 56 | markdown | `TRAJECTORY-METHODS-EXA-PROVENANCE-2026-08-26.md` | `2026-08-26-trajectory-methods-exa-provenance.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 57 | markdown | `HARBOR-INSTRUMENTATION-CAPABILITY-MATRIX-2026-08-26.md` | `2026-08-26-harbor-instrumentation-capability-matrix.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 58 | markdown | `EVAL-LAB-SOURCE-TO-SOFTWARE-DELTA-MATRIX-2026-08-26.md` | `2026-08-26-eval-lab-source-to-software-delta-matrix.md` |
| `research-context` | `trajectory-analysis/INDEX.md` | 60 | markdown | `PSTACK-REPO-STANDARDS-REVIEW-2026-08-27.md` | `2026-08-27-pstack-repo-standards-review.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 12 | markdown | `AUTOMATED-INTERPRETATION-EXA-PROVENANCE.md` | `2026-08-26-automated-interpretation-exa-provenance.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 16 | markdown | `cards/formats/openhands-trajectory-visualizer.md` | `cards/formats/2026-08-25-openhands-trajectory-visualizer.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 17 | markdown | `cards/daydream.md` | `cards/2026-08-25-daydream.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 18 | markdown | `cards/vestige.md` | `cards/2026-08-25-vestige.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 19 | markdown | `cards/formats/atif-preview.md` | `cards/formats/2026-08-25-atif-preview.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 20 | markdown | `cards/formats/atifact.md` | `cards/formats/2026-08-25-atifact.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 21 | markdown | `cards/formats/python-atif.md` | `cards/formats/2026-08-25-python-atif.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 22 | markdown | `cards/formats/theanecdote-trajectory.md` | `cards/formats/2026-08-25-theanecdote-trajectory.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 23 | markdown | `cards/formats/harbor-atif-rfc.md` | `cards/formats/2026-08-25-harbor-atif-rfc.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 26 | markdown | `cards/formats/pair-agent-trace-vis.md` | `cards/formats/2026-08-25-pair-agent-trace-vis.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 27 | markdown | `cards/formats/agent-trail.md` | `cards/formats/2026-08-25-agent-trail.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 28 | markdown | `cards/formats/agentsight-agentvis.md` | `cards/formats/2026-08-25-agentsight-agentvis.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 30 | markdown | `cards/formats/graphectory.md` | `cards/formats/2026-08-25-graphectory.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 31 | markdown | `cards/formats/agentastra.md` | `cards/formats/2026-08-25-agentastra.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 33 | markdown | `cards/nemo-agent-toolkit.md` | `cards/2026-08-25-nemo-agent-toolkit.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 36 | markdown | `cards/trajdebug.md` | `cards/2026-08-25-trajdebug.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 37 | markdown | `cards/diagnostics/agentprocessbench.md` | `cards/diagnostics/2026-08-25-agentprocessbench.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 39 | markdown | `cards/diagnostics/critictool-recovery.md` | `cards/diagnostics/2026-08-25-critictool-recovery.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 41 | markdown | `cards/diagnostics/safari-attribution.md` | `cards/diagnostics/2026-08-25-safari-attribution.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 52 | markdown | `cards/diagnostics/agentdiagnose.md` | `cards/diagnostics/2026-08-25-agentdiagnose.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 55 | markdown | `cards/diagnostics/agentcheck.md` | `cards/diagnostics/2026-08-25-agentcheck.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 57 | markdown | `cards/scoring/langchain-agentevals.md` | `cards/scoring/2026-08-25-langchain-agentevals.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 58 | markdown | `cards/scoring/vertex-trajectory-match.md` | `cards/scoring/2026-08-25-vertex-trajectory-match.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 65 | markdown | `cards/scoring/meta-task-f3-trajectory-judge.md` | `cards/scoring/2026-08-25-meta-task-f3-trajectory-judge.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 66 | markdown | `cards/scoring/meta-task-d-package-criteria.md` | `cards/scoring/2026-08-25-meta-task-d-package-criteria.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 74 | markdown | `cards/trajdebug.md` | `cards/2026-08-25-trajdebug.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 75 | markdown | `cards/diagnostics/agentprocessbench.md` | `cards/diagnostics/2026-08-25-agentprocessbench.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 76 | markdown | `cards/diagnostics/critictool-recovery.md` | `cards/diagnostics/2026-08-25-critictool-recovery.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 81 | markdown | `cards/diagnostics/trail-error-taxonomy.md` | `cards/diagnostics/2026-08-25-trail-error-taxonomy.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 83 | markdown | `cards/scoring/toolprmbench.md` | `cards/scoring/2026-08-26-toolprmbench.md` |
| `research-context` | `trajectory-analysis/INVENTORY.md` | 84 | markdown | `cards/scoring/webclipper.md` | `cards/scoring/2026-08-25-webclipper.md` |
| `research-context` | `trajectory-analysis/PSTACK-REPO-STANDARDS-REVIEW-2026-08-27.md` | 196 | markdown | `/Users/petermakhnatch/Developer/eval-lab/research/inbox/PSTACK-REPO-STANDARDS-REVIEW-2026-08-27.md` | `/Users/petermakhnatch/Developer/eval-lab/research/inbox/2026-08-27-pstack-repo-standards-review.md` |
| `research-context` | `trajectory-analysis/TRAJECTORY-STATISTICAL-METHOD-CATALOG-2026-08-26.md` | 60 | markdown | `AUTOMATED-INTERPRETATION-SOURCE-CATALOG.md` | `2026-08-26-automated-interpretation-source-catalog.md` |
| `research-context` | `trajectory-analysis/TRAJECTORY-STATISTICAL-METHOD-CATALOG-2026-08-26.md` | 60 | markdown | `MACHINE-JUDGMENT-CALIBRATION-HANDOFF.md` | `2026-08-26-machine-judgment-calibration-handoff.md` |
| `research-context` | `trajectory-analysis/cards/INDEX.md` | 12 | markdown | `nemo-agent-toolkit.md` | `2026-08-25-nemo-agent-toolkit.md` |
| `research-context` | `trajectory-analysis/cards/INDEX.md` | 13 | markdown | `daydream.md` | `2026-08-25-daydream.md` |
| `research-context` | `trajectory-analysis/cards/INDEX.md` | 14 | markdown | `trajdebug.md` | `2026-08-25-trajdebug.md` |
| `research-context` | `trajectory-analysis/cards/INDEX.md` | 17 | markdown | `vestige.md` | `2026-08-25-vestige.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 15 | markdown | `./trajdebug-error-lifecycle.md` | `./2026-08-25-trajdebug-error-lifecycle.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 16 | markdown | `./agentprocessbench.md` | `./2026-08-25-agentprocessbench.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 17 | markdown | `./agentdiagnose.md` | `./2026-08-25-agentdiagnose.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 18 | markdown | `./agentcheck.md` | `./2026-08-25-agentcheck.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 19 | markdown | `./toolmaze-recovery.md` | `./2026-08-25-toolmaze-recovery.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 20 | markdown | `./toolbench-x.md` | `./2026-08-25-toolbench-x.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 21 | markdown | `./critictool-recovery.md` | `./2026-08-25-critictool-recovery.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 22 | markdown | `./safari-attribution.md` | `./2026-08-25-safari-attribution.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 23 | markdown | `./trail-error-taxonomy.md` | `./2026-08-25-trail-error-taxonomy.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 24 | markdown | `./science-agent-reliability.md` | `./2026-08-25-science-agent-reliability.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 28 | markdown | `../trajdebug.md` | `../2026-08-25-trajdebug.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 29 | markdown | `../vestige.md` | `../2026-08-25-vestige.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 30 | markdown | `../daydream.md` | `../2026-08-25-daydream.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/INDEX.md` | 37 | markdown | `../../MASTER_ATIF_AND_TRAJECTORY_LANDSCAPE_2026.md` | `../../2026-08-25-master-atif-and-trajectory-landscape-2026.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/critictool-recovery.md` | 19 | markdown | `../../MASTER_ATIF_AND_TRAJECTORY_LANDSCAPE_2026.md` | `../../2026-08-25-master-atif-and-trajectory-landscape-2026.md` |
| `research-context` | `trajectory-analysis/cards/diagnostics/trajdebug-error-lifecycle.md` | 19 | markdown | `../trajdebug.md` | `../2026-08-25-trajdebug.md` |
| `research-context` | `trajectory-analysis/cards/formats/INDEX.md` | 12 | markdown | `openhands-trajectory-visualizer.md` | `2026-08-25-openhands-trajectory-visualizer.md` |
| `research-context` | `trajectory-analysis/cards/formats/INDEX.md` | 13 | markdown | `atif-preview.md` | `2026-08-25-atif-preview.md` |
| `research-context` | `trajectory-analysis/cards/formats/INDEX.md` | 14 | markdown | `atifact.md` | `2026-08-25-atifact.md` |
| `research-context` | `trajectory-analysis/cards/formats/INDEX.md` | 15 | markdown | `python-atif.md` | `2026-08-25-python-atif.md` |
| `research-context` | `trajectory-analysis/cards/formats/INDEX.md` | 16 | markdown | `theanecdote-trajectory.md` | `2026-08-25-theanecdote-trajectory.md` |
| `research-context` | `trajectory-analysis/cards/formats/INDEX.md` | 17 | markdown | `harbor-atif-rfc.md` | `2026-08-25-harbor-atif-rfc.md` |
| `research-context` | `trajectory-analysis/cards/formats/INDEX.md` | 18 | markdown | `pair-agent-trace-vis.md` | `2026-08-25-pair-agent-trace-vis.md` |
| `research-context` | `trajectory-analysis/cards/formats/INDEX.md` | 19 | markdown | `agent-trail.md` | `2026-08-25-agent-trail.md` |
| `research-context` | `trajectory-analysis/cards/formats/INDEX.md` | 20 | markdown | `agentsight-agentvis.md` | `2026-08-25-agentsight-agentvis.md` |
| `research-context` | `trajectory-analysis/cards/formats/INDEX.md` | 22 | markdown | `graphectory.md` | `2026-08-25-graphectory.md` |
| `research-context` | `trajectory-analysis/cards/formats/INDEX.md` | 23 | markdown | `agentastra.md` | `2026-08-25-agentastra.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 13 | markdown | `./deterministic-metrics.md` | `./2026-08-25-deterministic-metrics.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 14 | markdown | `./model-judges.md` | `./2026-08-25-model-judges.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 15 | markdown | `./human-labels.md` | `./2026-08-25-human-labels.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 21 | markdown | `./toolprmbench.md` | `./2026-08-26-toolprmbench.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 22 | markdown | `./webclipper.md` | `./2026-08-25-webclipper.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 23 | markdown | `./meta-task-f3-trajectory-judge.md` | `./2026-08-25-meta-task-f3-trajectory-judge.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 24 | markdown | `./meta-task-d-package-criteria.md` | `./2026-08-25-meta-task-d-package-criteria.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 25 | markdown | `./langchain-agentevals.md` | `./2026-08-25-langchain-agentevals.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 26 | markdown | `./vertex-trajectory-match.md` | `./2026-08-25-vertex-trajectory-match.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 27 | markdown | `./vestige-repeated-trial.md` | `./2026-08-25-vestige-repeated-trial.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 28 | markdown | `./recovery-bench-state-replay.md` | `./2026-08-25-recovery-bench-state-replay.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 32 | markdown | `../vestige.md` | `../2026-08-25-vestige.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 33 | markdown | `../trajdebug.md` | `../2026-08-25-trajdebug.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 34 | markdown | `../daydream.md` | `../2026-08-25-daydream.md` |
| `research-context` | `trajectory-analysis/cards/scoring/INDEX.md` | 40 | markdown | `../../MASTER_ATIF_AND_TRAJECTORY_LANDSCAPE_2026.md` | `../../2026-08-25-master-atif-and-trajectory-landscape-2026.md` |
| `research-context` | `trajectory-analysis/cards/scoring/deterministic-metrics.md` | 15 | markdown | `./langchain-agentevals.md` | `./2026-08-25-langchain-agentevals.md` |
| `research-context` | `trajectory-analysis/cards/scoring/deterministic-metrics.md` | 16 | markdown | `./vertex-trajectory-match.md` | `./2026-08-25-vertex-trajectory-match.md` |
| `research-context` | `trajectory-analysis/cards/scoring/deterministic-metrics.md` | 17 | markdown | `./vestige-repeated-trial.md` | `./2026-08-25-vestige-repeated-trial.md` |
| `research-context` | `trajectory-analysis/cards/scoring/deterministic-metrics.md` | 18 | markdown | `../diagnostics/science-agent-reliability.md` | `../diagnostics/2026-08-25-science-agent-reliability.md` |
| `research-context` | `trajectory-analysis/cards/scoring/deterministic-metrics.md` | 19 | markdown | `./webclipper.md` | `./2026-08-25-webclipper.md` |
| `research-context` | `trajectory-analysis/cards/scoring/deterministic-metrics.md` | 20 | markdown | `./recovery-bench-state-replay.md` | `./2026-08-25-recovery-bench-state-replay.md` |
| `research-context` | `trajectory-analysis/cards/scoring/deterministic-metrics.md` | 21 | markdown | `../diagnostics/agentcheck.md` | `../diagnostics/2026-08-25-agentcheck.md` |
| `research-context` | `trajectory-analysis/cards/scoring/deterministic-metrics.md` | 25 | markdown | `../vestige.md` | `../2026-08-25-vestige.md` |
| `research-context` | `trajectory-analysis/cards/scoring/deterministic-metrics.md` | 26 | markdown | `../daydream.md` | `../2026-08-25-daydream.md` |
| `research-context` | `trajectory-analysis/cards/scoring/deterministic-metrics.md` | 32 | markdown | `../../MASTER_ATIF_AND_TRAJECTORY_LANDSCAPE_2026.md` | `../../2026-08-25-master-atif-and-trajectory-landscape-2026.md` |
| `research-context` | `trajectory-analysis/cards/scoring/human-labels.md` | 15 | markdown | `../diagnostics/agentprocessbench.md` | `../diagnostics/2026-08-25-agentprocessbench.md` |
| `research-context` | `trajectory-analysis/cards/scoring/human-labels.md` | 16 | markdown | `../diagnostics/trajdebug-error-lifecycle.md` | `../diagnostics/2026-08-25-trajdebug-error-lifecycle.md` |
| `research-context` | `trajectory-analysis/cards/scoring/human-labels.md` | 17 | markdown | `../diagnostics/trail-error-taxonomy.md` | `../diagnostics/2026-08-25-trail-error-taxonomy.md` |
| `research-context` | `trajectory-analysis/cards/scoring/human-labels.md` | 18 | markdown | `../diagnostics/agentdiagnose.md` | `../diagnostics/2026-08-25-agentdiagnose.md` |
| `research-context` | `trajectory-analysis/cards/scoring/human-labels.md` | 19 | markdown | `./toolprmbench.md` | `./2026-08-26-toolprmbench.md` |
| `research-context` | `trajectory-analysis/cards/scoring/human-labels.md` | 31 | markdown | `../../MASTER_ATIF_AND_TRAJECTORY_LANDSCAPE_2026.md` | `../../2026-08-25-master-atif-and-trajectory-landscape-2026.md` |
| `research-context` | `trajectory-analysis/cards/scoring/langchain-agentevals.md` | 19 | markdown | `../telemetry/langsmith-agentevals.md` | `../telemetry/2026-08-25-langsmith-agentevals.md` |
| `research-context` | `trajectory-analysis/cards/scoring/langchain-agentevals.md` | 19 | markdown | `../../MASTER_ATIF_AND_TRAJECTORY_LANDSCAPE_2026.md` | `../../2026-08-25-master-atif-and-trajectory-landscape-2026.md` |
| `research-context` | `trajectory-analysis/cards/scoring/langchain-agentevals.md` | 56 | markdown | `../telemetry/langsmith-agentevals.md` | `../telemetry/2026-08-25-langsmith-agentevals.md` |
| `research-context` | `trajectory-analysis/cards/scoring/model-judges.md` | 15 | markdown | `./toolprmbench.md` | `./2026-08-26-toolprmbench.md` |
| `research-context` | `trajectory-analysis/cards/scoring/model-judges.md` | 16 | markdown | `./meta-task-f3-trajectory-judge.md` | `./2026-08-25-meta-task-f3-trajectory-judge.md` |
| `research-context` | `trajectory-analysis/cards/scoring/model-judges.md` | 17 | markdown | `../diagnostics/agentdiagnose.md` | `../diagnostics/2026-08-25-agentdiagnose.md` |
| `research-context` | `trajectory-analysis/cards/scoring/model-judges.md` | 18 | markdown | `../diagnostics/trajdebug-error-lifecycle.md` | `../diagnostics/2026-08-25-trajdebug-error-lifecycle.md` |
| `research-context` | `trajectory-analysis/cards/scoring/model-judges.md` | 19 | markdown | `../diagnostics/agentcheck.md` | `../diagnostics/2026-08-25-agentcheck.md` |
| `research-context` | `trajectory-analysis/cards/scoring/model-judges.md` | 20 | markdown | `../diagnostics/critictool-recovery.md` | `../diagnostics/2026-08-25-critictool-recovery.md` |
| `research-context` | `trajectory-analysis/cards/scoring/model-judges.md` | 21 | markdown | `../diagnostics/safari-attribution.md` | `../diagnostics/2026-08-25-safari-attribution.md` |
| `research-context` | `trajectory-analysis/cards/scoring/model-judges.md` | 22 | markdown | `./langchain-agentevals.md` | `./2026-08-25-langchain-agentevals.md` |
| `research-context` | `trajectory-analysis/cards/scoring/model-judges.md` | 26 | markdown | `./meta-task-d-package-criteria.md` | `./2026-08-25-meta-task-d-package-criteria.md` |
| `research-context` | `trajectory-analysis/cards/scoring/model-judges.md` | 31 | markdown | `../../MASTER_ATIF_AND_TRAJECTORY_LANDSCAPE_2026.md` | `../../2026-08-25-master-atif-and-trajectory-landscape-2026.md` |
| `research-context` | `trajectory-analysis/cards/scoring/vestige-repeated-trial.md` | 19 | markdown | `../vestige.md` | `../2026-08-25-vestige.md` |
| `research-context` | `trajectory-analysis/cards/scoring/vestige-repeated-trial.md` | 46 | markdown | `../vestige.md` | `../2026-08-25-vestige.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 16 | markdown | `harbor-trial-artifacts.md` | `2026-08-25-harbor-trial-artifacts.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 17 | markdown | `atif-subagent-trajectories.md` | `2026-08-25-atif-subagent-trajectories.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 18 | markdown | `daydream-runs.md` | `2026-08-25-daydream-runs.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 19 | markdown | `vestige-run-jsonl.md` | `2026-08-25-vestige-run-jsonl.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 20 | markdown | `nemo-nat-atif-samples.md` | `2026-08-25-nemo-nat-atif-samples.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 21 | markdown | `otel-atif-correlation.md` | `2026-08-25-otel-atif-correlation.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 29 | markdown | `harbor-trial-artifacts.md` | `2026-08-25-harbor-trial-artifacts.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 30 | markdown | `atif-subagent-trajectories.md` | `2026-08-25-atif-subagent-trajectories.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 31 | markdown | `daydream-runs.md` | `2026-08-25-daydream-runs.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 32 | markdown | `vestige-run-jsonl.md` | `2026-08-25-vestige-run-jsonl.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 33 | markdown | `nemo-nat-atif-samples.md` | `2026-08-25-nemo-nat-atif-samples.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 34 | markdown | `otel-atif-correlation.md` | `2026-08-25-otel-atif-correlation.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 40 | markdown | `../daydream.md` | `../2026-08-25-daydream.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 41 | markdown | `../nemo-agent-toolkit.md` | `../2026-08-25-nemo-agent-toolkit.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 44 | markdown | `../vestige.md` | `../2026-08-25-vestige.md` |
| `research-context` | `trajectory-analysis/cards/storage/INDEX.md` | 45 | markdown | `../trajdebug.md` | `../2026-08-25-trajdebug.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 14 | markdown | `harbor-atif-metrics.md` | `2026-08-25-harbor-atif-metrics.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 16 | markdown | `atof-to-atif.md` | `2026-08-25-atof-to-atif.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 17 | markdown | `nemo-relay-atif-export.md` | `2026-08-25-nemo-relay-atif-export.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 18 | markdown | `phoenix.md` | `2026-08-25-phoenix.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 19 | markdown | `openinference.md` | `2026-08-25-openinference.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 20 | markdown | `langsmith-agentevals.md` | `2026-08-25-langsmith-agentevals.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 21 | markdown | `weave.md` | `2026-08-25-weave.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 22 | markdown | `agentops.md` | `2026-08-25-agentops.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 23 | markdown | `langfuse.md` | `2026-08-25-langfuse.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 29 | markdown | `harbor-atif-metrics.md` | `2026-08-25-harbor-atif-metrics.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 31 | markdown | `atof-to-atif.md` | `2026-08-25-atof-to-atif.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 32 | markdown | `nemo-relay-atif-export.md` | `2026-08-25-nemo-relay-atif-export.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 33 | markdown | `phoenix.md` | `2026-08-25-phoenix.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 34 | markdown | `openinference.md` | `2026-08-25-openinference.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 35 | markdown | `langsmith-agentevals.md` | `2026-08-25-langsmith-agentevals.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 36 | markdown | `weave.md` | `2026-08-25-weave.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 37 | markdown | `agentops.md` | `2026-08-25-agentops.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 38 | markdown | `langfuse.md` | `2026-08-25-langfuse.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 44 | markdown | `../daydream.md` | `../2026-08-25-daydream.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 45 | markdown | `../nemo-agent-toolkit.md` | `../2026-08-25-nemo-agent-toolkit.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 48 | markdown | `../vestige.md` | `../2026-08-25-vestige.md` |
| `research-context` | `trajectory-analysis/cards/telemetry/INDEX.md` | 49 | markdown | `../trajdebug.md` | `../2026-08-25-trajdebug.md` |
