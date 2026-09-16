---
status: living
audience:
  - builder
  - analyst
  - operator
updated: 2026-09-16
---

# Optimizer-friendly environments: selection criteria and the imarathon build

What makes a Harbor-native task useful for **GEPA**, **DSPy**, and the **RLM
harness** — and what we built against those criteria on 2026-09-16 (HAR-56).
Evidence-first: every criterion traces to a measured adapter contract in this
repository or a cited external result.

## 1. What the optimizers actually consume (measured in-repo)

| Tool | Editable surface | Consumes per call | Needs from a task |
|---|---|---|---|
| GEPA (`src/evallab/gepa_optimizer/`) | candidate instruction text injected as `extra_instruction_path` | `primary_reward` (float, finite) + `build_feedback` ASI from trajectory IR and verifier diagnostics (`checks.json`, `test-stdout.txt`) | fractional reward headroom; per-check failure text in verifier output |
| DSPy/RLM (`src/evallab/harbor_rlm.py`, `src/evallab/rlm/`) | task instructions + output-field descriptions (module compiles like any predictor) | terminal reward via Harbor verifier; episode = REPL turns over tools | large messy context where code exploration beats reading; many independent sub-questions |
| Lego-RL/verl (`create_task_index.py`) | policy weights | `Episode` (trajectory + reward from `/logs/verifier/reward.txt`) | float reward; task index rows; difficulty spread |
| Omni (`gepa_optimizer/composition.py`) | seed artifact (text or code) | same as GEPA ×3 engines | same as GEPA, plus stable task identity across stages |

Key measured fact: **fractional rewards are fully supported end-to-end**
(`results.py` float64 primary reward; `behavior.py` classifies `partial`), yet
before today **every task in `library/tasks` emitted strictly binary rewards**.
That is the single biggest gap between our library and what these tools need.

## 2. Criteria (each with source)

1. **Fractional, judge-free reward.** Binary rewards give GEPA/RL no gradient
   at small N; LLM judges add an unverified grader. LHTB (arXiv:2607.08964)
   gate checklists ($R = \text{gates}/\text{total}$) are the proven pattern;
   JetBrains' GEPA study shows why raw solve-rate deltas mislead.
2. **Many instances with frozen splits.** Bauplan (780 tasks, 70/15/15) and
   our own GEPA pilot refusal (1 dev task, 0 holdouts → no ranking) both say:
   a family, not a task. The evaluator binds examples via
   `task_id/task_path/task_package_digest/split`; instances must be separate
   digest-addressable task dirs.
3. **Expected facts derived from the shipped artifact.** Our env-quality audit
   found verifier/fixture drift and input-tampering exploits; the fix is to
   compute expectations from the exact fixture bytes (digests + file-derived
   answers), never from generator-side bookkeeping alone.
4. **Large, messy, decoy-laden context.** dspy.RLM exists for "context too
   large, too messy, too unevenly relevant"; the RLM agent's signature is
   `instruction, file_tree -> solution`. A ~1MB log corpus with red herrings
   makes programmatic exploration the winning strategy and punishes `cat`.
5. **Deterministic environment friction.** (Librarian direction, 2026-09-16.)
   Truncated dump-command output and one-shot transient failures test agent
   resilience with zero judges; the oracle must remain unaffected (work via
   grep/sed/python, retry-free paths).
6. **Anti-gaming gates.** Tampering (input digests), decoy traps (decoy job in
   `debug-notes.log`), vacuous credit (hygiene requires submitted
   deliverables; nop must score exactly 0.0).
7. **Cheap, serial-safe rollouts.** Optimizers run many evaluations; oracle
   must be fast (<2 min) and the package must respect the shared two-container
   cap (serial matrix).

## 3. Library gap analysis (2026-09-16)

| Existing family | Reward | Fit |
|---|---|---|
| event-summary | binary (3 binary components) | GEPA-proven surface, no partial credit |
| terminal-bench-html-js-filter, query-optimize, transaction-reconciliation, tau3-retail-1 | binary | no optimizer gradient |
| experimental/syn-funcdag-*, deepplanning-v1 | binary | generator exists; no fractional gates |
| **imarathon (new)** | **20-gate fractional** | first library family built for all three tools |

## 4. What shipped tonight

`scripts/gen_imarathon.py` + `library/tasks/experimental/imarathon/i0000..i0005`
+ family-shared controls + frozen splits manifest. See the family
[`README.md`](../../library/tasks/experimental/imarathon/README.md) for the
gate map and control ladder (nop 0.0 → router-only 0.15 → tamper 0.15 →
fake-report 0.20 → partial 0.85 → oracle 1.0).

## 5. Research pointers (verified or explicitly unverified)

- **Verified harbor-eco:** LHTB tasks (dense gates, separate verifiers);
  Bauplan skill optimization (Harbor + GEPA Optimize Anything, fractional
  checks, frozen splits, canary that the skill reached the container);
  gepa-terminus2/harbor-cookbook (prompt-template optimization loops);
  repo2rlenv (repo → Harbor tasks with `f2p_rate × p2p_rate` dense reward);
  lego-rl task index → verl.
- **External, read but not reproduced here:** RLVE (arXiv:2511.07317) — 400
  verifiable environments, environment-scaling and adaptive difficulty;
  CuES (arXiv:2512.01311) — task generation inside a given tool environment
  when no tasks exist; SCALER, CUA-Gym, GUI-GENESIS (environment synthesis);
  "Process vs. Outcome Reward for Agentic RAG" (NeurIPS 2025). These inform
  the *next* family decisions (difficulty banding, generated tasks), and none
  of their claims are treated as our results.

## 6. Open questions for the next session

- Does a real model land mid-ladder on imarathon (partial credit spread), or
  cliff at 0? Needs an authorized model trial — not run tonight.
- RLVE-style difficulty banding: parameterize noise/fault count per instance
  seed and check oracle stability at the edges.
- Fold a `--friction` flag experiment: same instance with/without the fault
  overlay measures harness resilience directly (paired design).
- Wire `imarathon` development instances into a GEPA campaign declaration
  (needs Peter's spend authorization; evaluator contract already matches).
