# Pre-Registration (DRAFT): MiMo Gold v1 Batch Certification (A4)

> **========================================================================**
> **FREEZE AT LAUNCH**
> **This document is a pre-flight draft prepared by A4P-PREP.**
> **Before running the first paid cell, A4 must:**
> 1. Review this draft against the merged code on `origin/main` (PR #1738 and PR #1744).
> 2. Adjust ONLY for facts that changed in the merged code (e.g. CLI flags or signature updates).
> 3. Freeze this file as `prereg.md` in the A4 experiment directory.
> 4. Record its SHA-256 hash (`sha256sum prereg.md` -> `sha256[:12]`) in the launch report.
> 5. NEVER change thresholds, definitions, or cohort priority post-hoc after seeing data.
> **========================================================================**

## 1. Objective and Research Questions

The primary objective of `A4-GOLD-BATCH` is to evaluate and certify the 32 executable candidate tasks in the MiMo Gold universe using the hardened test synthesis feedback loop (`run_rounds` with fresh mutants), producing an immutable catalog: **"MiMo Gold v1"**.

### Primary Questions
1. **Structural Integrity:** How many candidate packages achieve `gold_structural` status (reference passing, all adversarial hooks rejected, all training and probe mutants killed)?
2. **Held-Out Generalization:** Among structurally certified packages with >= 3 base-accepted unseen EVAL mutants, how many achieve `gold_heldout` status (kill rate >= 75% on unseen mutants)?
3. **Failure Taxonomy:** For packages failing certification, what are the primary bottleneck failure modes (reference failure, repair refusal, survivor leakage, lack of base-accepted mutants, spend exhaustion)?

---

## 2. Cohort and Universe Definition

The evaluation cohort consists of the **32 executable packages** designated as `gold_ok: true` in `gates/gold-universe.json` (A1 inventory). All 32 are strictly-DEV Python packages with single-container support, extractable hooks, and valid reference patches.

### Execution Priority Order
Packages are scheduled strictly in priority order:
- **Priority Tier 1 (18 weak tasks from A2 census):** Tasks where original test suites permitted mutants to survive in baseline audit (`gates/weak-tasks.json`).
  1. `format-code-task-000682` (survivors: 2)
  2. `format-code-task-001267` (survivors: 2)
  3. `format-code-task-001804` (survivors: 2)
  4. `format-code-task-002545` (survivors: 2)
  5. `format-code-task-000079` (survivors: 1)
  6. `format-code-task-000081` (survivors: 1)
  7. `format-code-task-000232` (survivors: 1)
  8. `format-code-task-000256` (survivors: 1)
  9. `format-code-task-000838` (survivors: 1)
  10. `format-code-task-000847` (survivors: 1)
  11. `format-code-task-000931` (survivors: 1)
  12. `format-code-task-001338` (survivors: 1)
  13. `format-code-task-001426` (survivors: 1)
  14. `format-code-task-001431` (survivors: 1)
  15. `format-code-task-001809` (survivors: 1)
  16. `format-code-task-001850` (survivors: 1)
  17. `format-code-task-001985` (survivors: 1)
  18. `format-code-task-002535` (survivors: 1)
- **Priority Tier 2 (Remaining 14 gold_ok tasks, sorted lexicographically by task_id):**
  19. `format-code-task-000085`
  20. `format-code-task-000212`
  21. `format-code-task-000322`
  22. `format-code-task-000812`
  23. `format-code-task-000945`
  24. `format-code-task-001205`
  25. `format-code-task-002139`
  26. `format-code-task-002160`
  27. `format-code-task-002391`
  28. `format-code-task-002573`
  29. `format-code-task-002621`
  30. `format-code-task-002693`
  31. `format-code-task-002760`
  32. `format-code-task-003005`

---

## 3. Certification Criteria and Definitions

Each task in the universe receives exactly one classification:

### A. `gold_structural`
A task is certified as `gold_structural` if and only if all of the following hold on the final accepted test package:
1. **Gate 1 (Reference Validity):** Reference solution achieves reward = 1.0 (passes the test).
2. **Gate 2 (Nop Control):** Empty submission achieves reward = 0.0 (does not pass without solution).
3. **Gate 3 (Witness Control):** Pre-recorded witness/conftest submissions achieve reward = 0.0 (does not pass via false positives).
4. **Gate 4 (Execution Sandbox/Exit0 Hook):** Exit0 `.pth` bypass hook achieves reward = 0.0.
5. **Gate 5 (Training Mutant Coverage):** 100% of all assigned TRAIN mutants are killed (reward != 1.0).
6. **Gate 6 (Probe Mutant Coverage):** 100% of measured PROBE mutants evaluated in the accepted round are killed.

### B. `gold_heldout`
A task is certified as `gold_heldout` if and only if:
1. It satisfies all criteria for `gold_structural`; AND
2. The original base grader accepted at least 3 unseen EVAL mutants (`n_eval_base_accepted >= 3`); AND
3. The final upgraded test package achieves an unseen EVAL kill rate >= 0.75:
   $$\text{Kill Rate}_{\text{heldout}} = \frac{\text{killed unseen base-accepted mutants}}{\text{total unseen base-accepted mutants}} \ge 0.75$$

### C. `not_gold`
The package was evaluated, but failed one or more certification criteria. A specific reason code is assigned:
- `ref_failed_no_repair`: Reference solution failed and was not repairable.
- `ref_unmeasured`: Reference cell was unmeasured (timeout, infra error, or cap reached).
- `gate_failed_nop`: Nop control passed (reward > 0.0).
- `gate_failed_witness`: Witness control passed (reward > 0.0).
- `gate_failed_exit0`: Exit0 `.pth` bypass succeeded (reward > 0.0).
- `train_mutants_survived`: One or more TRAIN mutants survived.
- `probe_mutants_survived`: One or more fresh PROBE mutants survived.
- `heldout_insufficient_eval`: Structural gold achieved, but fewer than 3 base-accepted EVAL mutants exist.
- `heldout_kill_rate_low`: Structural gold achieved, but unseen EVAL kill rate < 0.75.

### D. `not_run`
The package could not be executed or was skipped due to execution boundary constraints:
- `spend_cap_exhausted`: Global cap reached before dispatch.
- `time_limit_reached`: Batch window expired.
- `infra_unavailable`: Modal infrastructure failure prevented launch.

---

## 4. Execution Protocol and Hyperparameters

- **Rounds Loop:** `rounds = 2` (maximum of 2 feedback rounds).
- **Probes per Round:** `probes_per_round = 2`.
- **EVAL Extra Limit:** `eval_extra = 2` (additional held-out mutants generated from extra operators).
- **Synthesizer Model:** `google-antigravity/gemini-3.8-flash`.
- **Model Invocation:** `omp -p --model google-antigravity/gemini-3.8-flash --thinking high --no-tools --no-skills`.
- **Model Concurrency:** At most 4 concurrent nested `omp -p` processes.
- **Task Concurrency:** At most 3 packages running in parallel.
- **Cell Concurrency:** At most 6 parallel container cells per package, bounded by global pool semaphore.
- **Cell Execution Backend:** `evallab.modal_cells.ModalCellRunner` over Modal sandbox with reserve/settle gate.
- **Budget and Spend:**
  - Global Modal Cap: **$3.00**.
  - Approval: `"Peter 2026-11-02 overnight A4: Modal cap $3.00"`.
  - Default Cell Reservation: **$0.010** (settling to actual measured cost $\approx \$0.003 - \$0.008$).
  - Metering: Spent cost tracked through `SandboxMeter` and `ReserveSettleSpendGate` ledger.
  - Zero double-payment invariant: finished cells cached by content hash (`result.json`) are never re-executed.

---

## 5. Statistical Reporting Plan

1. **Point Estimates and Confidence Intervals:**
   - Structural Gold Rate: $k_{\text{structural}} / N_{\text{eligible}}$ with 95% Wilson score interval.
   - Held-Out Gold Rate: $k_{\text{heldout}} / N_{\text{eligible}}$ with 95% Wilson score interval.
   - Mean Unseen EVAL Kill Rate across evaluated tasks (with 95% CI).
2. **Paired McNemar Comparisons (if evaluating round 1 vs round 2):**
   - Use `stats.paired(arm1_bools, arm2_bools)` reporting both discordant cells $b$ and $c$, exact two-sided $p$.
3. **Data Integrity and Blindness:**
   - Synthesizer prompts are verified disjoint from EVAL mutants (`assert_prompt_blind`).
   - Manifest does NOT include per-mutant diffs or source code.
   - All results backed by per-cell receipts in `results/cells.csv` and `summary.json`.

---

## 6. Stopping Rules

1. **Global Cap:** When cumulative measured spend on the meter hits $3.00 (or pending reservations exhaust available budget), remaining tasks are cleanly marked `not_run (spend_cap_exhausted)`.
2. **Per-Task Early Stop:**
   - If reference solution measures 0.0 and self-repair fails or reference measures 0.0 again, loop reverts with `guard_reverted`.
   - If all probes are killed in round 1, round 2 is skipped by design (`all_probes_killed`).
   - If a reference is unmeasured (`None`), task halts without second synthesis (`reference_unmeasured`).
3. **Coverage Principle:** If time or budget limits preclude finishing all 32 tasks, report coverage as $k/32$. Never claim the universe was completed unless every package has a definitive certificate.
