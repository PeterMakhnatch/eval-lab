---
source_type: internal
type: lead-sync-reply
pane: wK:p4 (Tutor)
date: 2026-09-03
to: OMP lead pane (wS:pA)
---

# Lead sync reply — wK:p4 Tutor

## 1. Current focus
Tutor lane: Track F methodology/claim gates. No code branch tonight — deliverables are audit artifacts: `research/inbox/tutor-trackf-trace-capability-claim-audit-20260903.md` (sha `377b1214…d45897`) + gates doc `/private/tmp/eval-lab-briefs/tutor-trackf-claim-gates-20260903.md` (sha `c65b8cf9…b6e4dc`). Open PR: **#295** (state report) @ `b19016f6`, mergeable; owes a one-line anchor fix (`src/evallab/authoring.py` L115/L124).

## 2. Top blocker
Every gate that matters (G1 base-rate window, G3 SFT signal, G12 RL) needs **runs**, and tonight's policy bars them — so the real blocker is on Peter: (a) a Linux/cloud host with enforced no-network (Darwin cannot: `src/evallab/harbor_network.py:70`); (b) the ICC pilot + trajectory campaign for gold labeling ($K_{\text{eff}}=13.33<20$).
On other panes: Track C `f63172b3` is BLOCKED by p7 (authority reverify deleted; owner via wH:p9) — my G6/G7 gates can't be exercised on candidates until restored. And the SFT-signal brief (wH:p9) has **no control arm** (random-trace SFT) — waiting on wH:p9 to accept it; without it "signal" is indistinguishable from format learning.

## 3. One unowned thing for you
**Evidence-visibility reconciliation — the lab's own dashboard says 0 while the ledger says 235.** `research/lessons.md` aggregation views report **0 eligible trials across all views**; the Evidence Quality Ledger counts **235 evaluated** (24 pass / 178 warn / 24 fail / 9 quarantine; top reason `missing_trajectory_file`: 24). Nobody owns the join.
- **Files/symbols:** generator `src/evallab/lessons.py` ↔ ledger `src/evallab/interpretation/trajectory_quality.py` (`trajectory_quality_reports` parquet; `QualityStatus` L39; `is_analysis_ready` L143). Likely a filter/join-key defect, reproducible now.
- **Contract:** eligibility must reconcile per disposition, or exclusions itemized with typed reasons — *missing/ambiguous yields refusal, never zero* (gate G0 applied to the dashboard).
- **Acceptance:** fixture with N ledger rows across all four dispositions → aggregation reports `eligible + Σ per-reason exclusions == N`; live counts reconcile against the ledger; **negative control:** a row with `is_analysis_ready=False` never enters an eligible count; **positive twin:** a `pass` row does. Both directions, or a filter that excludes everything passes.

(De-dup note: I already handed Fable/wS:p9 the *other* gap — EXP-S03 `extra_instruction_path` on `ExperimentSpec` + `build_command`. Take this one; don't collide with that.)
