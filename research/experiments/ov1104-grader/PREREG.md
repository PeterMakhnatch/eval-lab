# PREREG: ov1104-grader (systematic grader trust)

Written 2026-11-04 00:20 ET, before any scoring or paid run of this lane. Hash committed before the first Modal cell.

## Questions
Q1. Across every DEV task with executed grader evidence, how is grader weakness distributed, and which tasks are closest to certifiable?
Q2. Can `evallab certify-gold` certify task `format-code-task-000682` (all 7 gates, incl. >=2 accepted alternative correct solutions), and up to 4 more of the closest tasks, inside the $6 lane cap?

## Data (DEV only; no `research/answer-keys/`, no `heldout-*`, no TEST tasks)
Receipts under `/private/tmp/eval-lab-study/research/experiments/` (read-only `origin/main`):
`ov-sting-mutants-20261031`, `on-weak-grader-census-20261102`, `mimo-clean-census` (and `-v4/-v5/-v6`),
`har169-separate-verifier`, `h2-separate-verifier-pth-20261030`, `h1-grader-hardening-20261030`, `a-harden-all-20261030`,
`t2-unified-harden-20261031`, `ov-gold-certifier-8-20261101`, `on-gold-v1-20261102`, `on-gold-v1b-20261102`,
`on-gold-false-reject-20261102`, `on-real-submission-replay-20261102`, `on-gold-attack-b-20261102`, `on-repair-vs-gold-20261102`,
`on-l0-aware-attacker-20261102`, `ov-k4-redteam-harden-20261031`, `ds-sk8-attack-triage-20261030`.
Task universe: the 50 Gold candidates of `on-gold-universe-20261102/summary.json` plus any other strictly-DEV task that has an executed cell in one of the receipts above.
Alternative solutions: `~/Developer/eval-lab-results/2026-11-02/A0/submissions-inventory.json` (`is_dev` and `original_reward == 1.0`), plus solutions generated tonight by headless agents on $0 routes.

## Scorecard columns (per task)
`nop_reward` (0 expected), `reference_reward` (1 expected), `mutants_killed_by_original k/n` with 95% Wilson CI (executed STING/census mutants only; static or unexecuted mutants are excluded), `attacker_vs_original k/n`, `attacker_vs_separate_verifier k/n`, `alt_solutions_accepted k/n` (real passing submissions or validated generated ones, graded by the strictest grader that has graded them), `gold_gates_passed` (of 7), `status`. Every cell carries its receipt path; a missing receipt is `null`, never a pass.

## Status rule (fixed before assembly; first matching rule wins)
1. `certified`: a `certify-gold` run with `certification.json` status `certified` (7/7 gates, >=2 alternatives accepted).
2. `near_certifiable`: gates 1-6 passed in some gold run (certify-gold, `ov-gold-certifier-8`, or A4B `gold_structural`/`gold_heldout`) and alternatives are <2 or unmeasured.
3. `hardened_but_leaky`: a reference-passing hardened test exists (A4B `accepted_test`) but some executed mutant survives the hardened grader (gates 5/6 fail) or a gate 2-4 fails.
4. `original_weak`: the original grader accepted >=1 executed broken mutant and no hardened package reached rule 2 or 3.
5. `original_no_weakness_found`: >=1 executed mutant, all killed by the original grader, and no rule above.
6. `no_executed_evidence`: everything else.

## Headline metrics and bars
- H1: share of tasks with executed mutant evidence whose original grader accepted >=1 broken mutant, k/n with Wilson CI. No pass/fail bar; it is descriptive. Expectation from prior receipts: 60-70%.
- H2: number of tasks reaching `near_certifiable` or better, and the list of exact missing items per task.
- H3 (Q2): `certify-gold` on 000682 reaches `certified`. PASS = `certification.json.status == "certified"` with >=2 alternative solutions accepted, reference 1, nop/witness/.pth 0, all train and held-out mutants killed. Anything else is a FAIL with the exact failed gate named. Alternatives count only if (a) they were produced without seeing the hidden tests (instruction + repository only, or a real agent run), or are marked `test_aware` and reported separately, and (b) the original grader scored them 1.0 on a fresh Modal cell tonight.
- H4: for every additional task attempted, the same rule as H3; report misses with the failed gate.

## Fixed parameters
- certify-gold: `--max-usd 0.95` per task, `--allow-degraded` only if `--expected-tests` cannot be derived, `--model google-antigravity/gemini-4-argon` for synthesis (fallback `anthropic/claude-fable-5-1`), one synthesis round.
- Validation of a generated alternative: 1 Modal cell on the original package, reward must be 1.0.
- Approval text: `Peter 2026-11-03 23:35 ET: $30 overnight infra budget (ov1104), lane cap per PROGRAM.md`. Lane cap $6. Ledger `~/.local/state/evallab-ov1104/grader-spend.json`. App names `ov1104-grader-<what>`.
- Order of tasks for H4 (decided from existing receipts, before any run): 002552 (3 real submissions already accepted by the A4B hardened grader), 002391 (1 accepted real submission; 3 rejected by over-strict tests), 002139 (1 accepted), 000838 (1 real submission, judged defective; needs 2 generated).
- Mutation-audit extension (item 3) only if >= $1.50 of the lane cap remains after items 1-2; population estimate = share of audited original graders accepting >=1 mutant, Wilson CI, audited tasks drawn in the fixed order of `on-gold-universe` `executable_audit` ids not yet audited.

## What counts as a miss
A certification that needs a `test_aware` alternative is reported as `provisional (test-aware alternatives)`, not `certified`.
Any generated alternative that fails the original grader is reported in the counts (k/n), not dropped.
