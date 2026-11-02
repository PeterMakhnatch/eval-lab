# A3 prereg: gold-certifier feedback rounds (written 2026-11-02 before any synthesis call and before any Modal cell)

State at writing: code built and unit-tested locally; `run_rounds.py prepare` run (local, no model, no cloud) and its `selection.json` fixed (sha256[:12] `28f6d292e213`). No label (no mutant reward) exists yet.

## Question

Does feeding a second (and third) synthesis round the FRESH mutants that survived the previous round's test raise the kill rate of **unseen held-out (EVAL) mutants that the original grader already accepts**, compared with the single-round certifier?

## Cohort (fixed)

The same 8 strictly-DEV tasks as the 2026-11-01 certifier run: `000079, 000232, 001267, 001850, 000682, 000931, 000484, 003005` (`format-code-task-*`; kinds, interpreter hooks and base packages as in `ov-gold-certifier-8-20261101/run_gold_certification.py`). No extra tasks: the A2 census had not produced weak tasks when this was written.

## Mutant sets (deterministic; seed `gold-rounds-v1`)

- **TRAIN** (round 1 input): last night's TRAIN, unchanged (first half by sha256 of `mutants_for_patch(limit=8)`).
- **EVAL_old**: last night's EVAL (18 mutants over 7 tasks; 000931 has none).
- **Fresh pool**: every valid mutant of the reference patch's added lines (standard operators plus `arith_flip`, `bool_op_flip`, `negate_condition`, `return_none`) whose hash is not in TRAIN or EVAL_old, ordered by sha256(seed|"pool"|hash).
- **EVAL_extra**: the first `min(2, pool // 2)` of the pool (12 mutants in total across the 8 tasks). **EVAL_A3 = EVAL_old ∪ EVAL_extra** (30 mutants). EVAL is never passed to the synthesizer, never graded as a probe, never in a prompt (a runtime check raises on any EVAL diff in a prompt).
- **PROBE pool**: the rest of the pool minus mutants on any line that an EVAL_A3 mutant touches. Round r grades the next 2 probes of the pool; probes that the round's test still accepts (reward 1.0) become extra training mutants for round r+1 (shown as diffs, plus the previous test file).
- Pool sizes (from `selection.json`): probe pool 141, 33, 1, 5, 1, 1, 2, 1 for the 8 tasks in the order above. Five tasks cannot supply a second round of fresh probes; their loop stops (`probe_pool_exhausted`) after one or two rounds. This limit exists before any data.

## Arms

One run per task with up to 3 rounds. Arm k = the test accepted at round min(k, last accepted round). Rounds = 1, 2, 3 arms are therefore nested (arm 2 shares arm 1's first synthesis), not independent syntheses. If the loop stops early (all probes killed, pool exhausted, probes unmeasured), later arms carry the last accepted test.

## Measurements

Per accepted round on its hardened package: reference (false-reject guard), the round's probes, every EVAL_A3 mutant. The original (base) grader is run once per EVAL_A3 mutant to define **base-accepted** (base reward 1.0). Gates nop / witness / exit0_pth are run on the round-1 package and the final package at lowest priority and only if the cap allows; TRAIN mutants are not regraded (cost). Synthesizer: `google-antigravity/gemini-3.8-flash`, `--thinking high`, `--no-tools --no-skills`, config copy of last night's worker file (the `{RUN}/worker.yml` named by the contract does not exist); served model read from every session log.

## Primary metric and test

Among EVAL_A3 mutants with base reward 1.0, **killed = hardened-grader reward 0.0** for arm 2 versus arm 1, paired by mutant, over all tasks (tasks whose loop stopped after round 1 contribute concordant pairs). Report `stats.paired(arm1_killed, arm2_killed)`: both discordant counts (`b` = killed only by arm 1, `c` = killed only by arm 2) and the exact McNemar p. Wilson 95% intervals for each arm's kill rate. Mutants with an unmeasured cell (error, marker missing, cap) are excluded from the pair and listed.

Secondary (same definitions): arm 3 vs arm 1 and arm 3 vs arm 2; the same comparison restricted to EVAL_old (last night's set) and to tasks where round 2 actually ran; all-EVAL (not only base-accepted) kill rate; probe kill rate per round; per-task table; cost per task.

## Gates

- **False-reject guard (hard):** every accepted round has reference reward 1.0 (the loop only accepts such a test; reverted rounds are listed). A round-1 failure after one repair leaves the task without an accepted test and it is listed, not dropped.
- **Verdict rule (fixed now):** "rounds=2 helps" requires c > b and exact McNemar p < 0.05. With about 10 to 20 base-accepted EVAL mutants the test is likely underpowered; a null here means "not detected", not "no effect". The result is reported either way, with b and c.

## Budget and order

Modal cap $1.50 on the measured estimate (SandboxMeter, rates x1.25), ledger `a3-rounds-20261102-ledger.json` at $0.006 per execution, approval string "Peter 2026-11-02 overnight A3: Modal cap $1.50". Order: base-grader EVAL cells, then round 1 for all tasks, round 2, round 3, then gates. If the cap stops a stage the remaining cells are reported NOT MEASURED, not imputed.

## Not decided here

How the PR is reviewed and merged (coordinator).
