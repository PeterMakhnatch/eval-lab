# Pre-Registration: MiMo Gold v1 Batch Certification (A4)

**Document Status:** FROZEN BEFORE LAUNCH  
**Target Item:** A4-GOLD-BATCH  
**Date:** 2026-11-02  
**Cohort:** 32 strictly-DEV executable packages (`gold-universe.json`, `gold_ok: true`)  
**Modal Spend Cap:** $3.00 (Approval: "Peter 2026-11-02 overnight A4: Modal cap $3.00")  
**Synthesizer:** `google-antigravity/gemini-3.8-flash` (`--thinking high --no-tools --no-skills`)  
**Cell Runner:** `evallab.modal_cells.ModalCellRunner` with `timeout_seconds=300`, `ReserveSettleSpendGate`

---

## 1. Objective and Research Questions

The primary objective of `A4-GOLD-BATCH` is to evaluate and certify the 32 executable candidate tasks in the MiMo Gold universe using the hardened test synthesis feedback loop (`run_rounds` with fresh mutants), producing an immutable catalog: **"MiMo Gold v1"**.

### Primary Questions
1. **Structural Integrity:** How many candidate packages achieve `gold_structural` status (reference passing 1.0, nop 0.0, witness 0.0, exit0/.pth hook 0.0, and 100% of assigned TRAIN and measured PROBE mutants killed)?
2. **Held-Out Generalization:** Among structurally certified packages with >= 3 base-accepted unseen EVAL mutants, how many achieve `gold_heldout` status (kill rate >= 0.75 on unseen base-accepted mutants)?
3. **Failure Taxonomy:** For packages failing certification, what are the primary bottleneck failure modes (reference failure, repair refusal, survivor leakage, lack of base-accepted mutants, spend exhaustion)?
4. **Demonstrated Non-Equivalence (Addendum 04:00 ET):** For each surviving mutant from the A2 weak-grader census exercised in this pipeline, does a validated synthesized test (which passes the reference solution) kill the mutant, thereby rigorously *proving* non-equivalence without reliance on LLM judges? What is the corpus-wide demonstrated weak-grader rate?

---

## 2. Cohort and Priority Order

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
  28. `format-code-task-002552`
  29. `format-code-task-002609`
  30. `format-code-task-002680`
  31. `format-code-task-002976`
  32. `format-code-task-003007`

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

## 4. Demonstrated Non-Equivalence Criteria (Addendum 04:00 ET)

For any mutant that survived the baseline grader in study A2 (`reward == 1.0` in `census.json`):
1. **Demonstrated Non-Equivalent:** A synthesized test package that passes the reference solution (`reward == 1.0`) scores `reward != 1.0` (killed) on that mutant. This provides absolute proof that the mutant alters observable execution behavior on legal inputs.
2. **Demonstrated Weak Grader:** A task whose baseline grader accepted at least one mutant that is demonstrated non-equivalent by a validated synthesized test.
3. **Unresolved Survivor:** A mutant that was not tested or survived the synthesized test suite as well, remaining unproven either way.

---

## 5. Execution Protocol and Operational Limits (Addendum 04:50 ET)

- **Rounds Loop:** `rounds = 2` (maximum of 2 feedback rounds per task).
- **Probes per Round:** `probes_per_round = 2`.
- **EVAL Extra Limit:** `eval_extra = 2` (additional held-out mutants generated from extra operators).
- **Synthesizer Model:** `google-antigravity/gemini-3.8-flash` (subscription, attested).
- **Model Invocation:** `omp -p --model google-antigravity/gemini-3.8-flash --thinking high --no-tools --no-skills`.
- **Model Concurrency:** At most 4 concurrent nested `omp -p` processes.
- **Task Concurrency:** At most 3 packages running in parallel.
- **Cell Concurrency:** At most 6 parallel container cells per package, bounded by global pool semaphore (`GLOBAL_MODAL_SLOTS = 6`).
- **Cell Execution Backend:** `evallab.modal_cells.ModalCellRunner` over Modal sandbox with reserve/settle gate.
- **Cell Timeout:** EXPLICIT `timeout_seconds = 300` on every `CellSpec`.
  - Worst-case overrun per in-flight cell: $300 \times \$0.00016525 - \$0.03966 = \$0.0099$.
  - With 6 concurrent slots, maximum potential overrun beyond cap: $6 \times \$0.0099 = \$0.059$.
- **Budget and Spend:**
  - Global Modal Cap: **$3.00**.
  - Approval: `"Peter 2026-11-02 overnight A4: Modal cap $3.00"`.
  - Default Cell Reservation: sized by `min(240, 300)` = $0.03966 (settling to actual measured cost $\approx \$0.003 - \$0.008$).
  - Metering: Spent cost tracked through `SandboxMeter` and `ReserveSettleSpendGate` ledger (`a4-gold-batch-ledger.json`).
  - Zero double-payment invariant: finished cells cached by content hash (`result.json`) are never re-executed.

---

## 6. Statistical Reporting Plan

1. **Point Estimates and Confidence Intervals:**
   - Structural Gold Rate: $k_{\text{structural}} / N_{\text{eligible}}$ with 95% Wilson score interval.
   - Held-Out Gold Rate: $k_{\text{heldout}} / N_{\text{eligible}}$ with 95% Wilson score interval.
   - Mean Unseen EVAL Kill Rate across evaluated tasks (with 95% CI).
   - Demonstrated Weak Grader Rate: $k_{\text{demonstrated\_weak}} / N_{\text{audited}}$ with 95% Wilson score interval.
2. **Paired Comparisons:**
   - Use `stats.paired(arm1_bools, arm2_bools)` reporting discordant counts $b$ and $c$, exact two-sided McNemar $p$.
3. **Data Integrity:**
   - Manifest contains strictly no mutant diffs or source code.
   - All numbers backed by durable per-cell receipts.

---

## 7. Stopping Rules

1. **Global Cap:** When cumulative measured spend on the meter hits $3.00 (or pending reservations exhaust available budget), remaining tasks are cleanly marked `not_run (spend_cap_exhausted)`.
2. **Per-Task Early Stop:**
   - If reference solution measures 0.0 and self-repair fails or reference measures 0.0 again, loop reverts with `guard_reverted`.
   - If all probes are killed in round 1, round 2 is skipped by design (`all_probes_killed`).
   - If a reference is unmeasured (`None`), task halts without second synthesis (`reference_unmeasured`).
3. **Coverage Principle:** If time or budget limits preclude finishing all 32 tasks, report coverage as $k/32$. Never claim the universe was completed unless every package has a definitive certificate.
