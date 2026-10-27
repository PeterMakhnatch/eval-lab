# LB-5: what the current Stage-1 rules miss on Goodfire `passage_positive` steps

**Question.** Which Goodfire steps with a publisher passage label (`passage_positive`, hydrated by
LB-3) do the current deterministic Stage-1 tripwires miss, and which new deterministic command or
state rules would catch some of them without raising too many alarms on clean traces?

**What ran (read-only, no model call, no Docker, no network).**

| Step | Tool | Output |
|---|---|---|
| 1 | `tools/stage1_scan.py --mode prefix` | the live `tripwires.scan_prefix` over all 3,674 admitted DEV Goodfire traces (rules version 3, `origin/main` `8857d7d25`); step numbers only |
| 2 | `tools/stage1_scan.py --mode trace` | the offline `tripwires.scan_trace` (adds the `offline` rules) |
| 3 | `tools/stage1_candidates.py` | 12 candidate single-step rules (prototypes, not in `src/evallab`) |
| 4 | `tools/stage1_analyze.py` | `results/lb5-stage1-coverage.json` (aggregates only, sha256 `047ea5fb...2deab`) |

Only tasks that are DEV in the database and admitted by the production gate were opened; held
Goodfire rows were not read. The scan output (step numbers) lives in a scratch directory, not in git.

**Adapter (the only judgment in step 1).** An `assistant` row becomes one agent step; its stored
arguments become `tool_calls` (`cmd` renamed `command` for shell tools; a `python` tool call is run
as a `python - <<EOF` heredoc so the shell rules see its code); following `tool` rows become the
step's observation. Rules that need a file-access log, a task manifest or a structural context
are `unavailable` here, as in any run without them.

## Results

Definitions are in `tools/stage1_analyze.py`. Base rate: 7.4% of agent steps lie in the turn window
of a positive step (DeepSWE 3.9%, SWE-bench 13.9%, ImpossibleBench 15.4%).

1. **Most positive steps are not command-visible.** 10,681 positive steps in 2,483 traces. The
   verdict-class rules catch 2.8% (298) and verdict + diagnostic rules 8.5% (912) at the step's turn
   window. 76.6% of positive rows are in the reasoning channel; a command rule cannot see them.
2. **The label also marks mentions.** In traces the publisher judged *not enacted* (`verdict_enacted`
   not `yes`), 395 of 479 `tool_calls` positives are `submit` answers in which the agent declines the
   shortcut, and the current rules fire on 0.2% of that group's positives (8 of 3,256), as they should.
   The recall target is therefore the 664 `tool_calls` positives in *enacted-yes* traces.
3. **Recall on that target.** Verdict-class 16.0% (106 of 664), verdict + diagnostic 28.9% (192).
   Missed most: `hardcoding_special_casing` (1 of 50), `harness_mechanics_targeting` (3 of 36),
   `misrepresented_compliance` (0 of 40), `fabricated_verification` (5 of 54), `test_modification` (19 of 134).
4. **The current verdict-class rules are no better than the base rate on this corpus.** Hit precision
   5.8% overall; per benchmark 4.9 / 16.9 / 14.4% against bases 3.9 / 15.4 / 13.9%. They alarm on 32.8%
   of clean traces (DeepSWE 66.4%) because agents routinely edit tests in these tasks. The rules with real lift
   are diagnostic: `hb_git_history_browse` 23.6%, `hb_egress_attempt` 22.6%, `external_sourcing` 29.6%.
5. **Candidate rules.** Five of twelve add signal:

| Rule | Hit steps | Hit precision | Targets caught (of 664) | Clean traces alarmed |
|---|---:|---:|---:|---:|
| `answer_path_probe` | 453 | 33.6% | 14 | 2.8% |
| `harness_scaffold_in_code` | 95 | 31.6% | 12 | 0.8% |
| `git_object_by_sha` | 647 | 17.2% | 13 | 3.5% |
| `test_exclusion_flag` | 1,272 | 16.8% | 33 | 6.0% |
| `package_fetch` | 453 | 15.7% | 39 | 7.1% |
| union of the five | 2,918 | 19.7% | 109 | 16.9% |

   The other seven are noise or silent on this corpus: `installed_package_tree` 6.1%,
   `dep_cache_listing` 6.1%, `test_file_script_write` 5.2% (alarms on 54.7% of clean traces),
   `literal_branch_special_case` 4.5% and zero targets, `skip_marker_added` 3.0%, `runtime_env_override`
   and `defines_run_tests` no positive hit. The union lifts precision above the base rate in all three benchmarks
   (16.5 / 39.1 / 18.6%). Added to verdict + diagnostic it moves target recall from 28.9% to 38.1%
   (+61 steps; 253 of 664), with 64.0% of clean traces alarmed. The other 62% of targets are ordinary-looking edits.
6. **Offline rules add almost nothing.** `hb_unverified_test_claim` fires on 43 steps (27.9% precision);
   recall is unchanged at 28.9%.

## Limits

* The labels are the publisher's three-vote LLM judge, hydrated by LB-3 and provisional (not
  replay-confirmed). "Positive" is a passage label, not a verified hack.
* The candidate rules were written after reading DEV misses, so the numbers are DEV-tuned. The
  per-benchmark rows are a robustness look inside DEV, not a cross-test; no unseen-benchmark data
  was available (HackBench is gated).
* No rule here is adopted; none changes `src/evallab`. Adoption needs TW-4 to re-measure on its own
  tune/cross-test split and CM-5 to decide the alert class.
