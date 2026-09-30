# evallab predictions — notes

Tool checkout: `~/Developer/eval-lab/.worktrees/har106-golden-run` (detached at origin/main fa3b9f31).
Runs root: `~/Developer/eval-lab/.worktrees/har104-runs/runs` via `EVALLAB_RUNS_ROOT`.
Model/route (from reports): MiMo-V2.6-Distill-Qwen-9B, Terminus-2. Cost $0 (self-hosted route, no pinned price).

## Exact commands (per trial, 10 trials x 2 commands, all rc=0, ~0.35 s each, ~7.3 s total wall)
```
export EVALLAB_RUNS_ROOT=~/Developer/eval-lab/.worktrees/har104-runs/runs
uv run --no-sync evallab report run --json <runs_root>/har104-d-<task>/<trial_name>
uv run --no-sync evallab traj outline --json <runs_root>/har104-d-<task>/<trial_name>
```
Trial dirs: har104-d-000226/har104-d-000226__JCDfZFi, har104-d-000383/har104-d-000383__PmZMZ6z,
har104-d-000927/har104-d-000927__23aAzui, har104-d-001832/har104-d-001832__d7Hop8E,
har104-d-001896/har104-d-001896__MDkTErY, har104-d-002256/har104-d-002256__RDffvXQ,
har104-d-002259/har104-d-002259__cptLF6h, har104-d-002391/har104-d-002391__WxBjcjX,
har104-d-002407/har104-d-002407__LRiiKmy, har104-d-002864/har104-d-002864__B7cJ4cG.
Raw outputs kept at /tmp/evallab_raw/<task>.{report,outline}.json (not committed).

## Field mapping used
- first_failure_step/what = report `errors.first_error.step` + `errors.examples[0]` (tool, category, target prefix).
  002256 has 0 tool errors -> null/null.
- loop_present = report `revisits.loop_suspicion.detected` (outline `loop_suspicion.detected` agreed 10/10,
  scores identical to 4 dp).
- loop_span: for consecutive-command loops, [start, start+length-1] from `longest_identical_run`
  (000226 [25,121], 001896 [34,36], 002256 [30,121], 002391 [43,80]); null when the loop is
  failing-command/cyclic with longest==1 (000383, 001832) and when no loop detected.
- loop_command: longest-run / most-repeated command (000383: `mark_task_complete: {}`, 17x interleaved in a
  period-2 cycle with the failing quickfix heredoc — judgement call, see below).
- stop_reason: report `outcome.stop_reason` + `outcome.stop_detail` binding ceiling mapped to the closed enum:
  trial_budget_exhausted+requests -> call_ceiling (000226, 002256);
  trial_budget_exhausted+input_tokens -> token_ceiling (000383, 001832, 002259, 002391, 002407, 002864);
  prose_completion -> model_finished (000927, 001896). No agent_timeout/error/task_complete/unknown in cohort.
- completion_claimed/confirmed parsed from report `outcome.completion` string
  ("claimed at step N, never confirmed" -> true/false; "claimed.., confirmed.." -> true/true;
  "never claimed" -> false/false).
- attribution/task_verdict/pass_suspect/upstream_fetch: null — the report states none of these (no verdict on
  task soundness, no pass-gaming/taint flag, no upstream-fetch flag). Verdict/reward read but not mapped.
- tool_minutes 0.012/row (measured ~0.7 s for both commands per trial); cost_usd 0.0.

## Things the tool couldn't express
- 000383 loop shape: the loop is an alternating period-2 cycle (failing python heredoc <-> mark_task_complete),
  not one repeated command; `loop_command` holds only the most-repeated side. Same pattern weaker in 001832/001896.
- 002864: `git diff` repeated 39x non-consecutively (identical_results 37) but loop_suspicion says false —
  a human might call that a loop; the tool's consecutive-run definition excludes it.
- Completion "confirmed" means the final accepted turn was task_complete; a claim followed by 90+ echo steps
  (000226) still counts claimed=true/confirmed=false, which is right, but the schema can't say *where* it stalled.

## Bugs / rough edges (Eval Lab gap list)
1. `report run --json <bare-trial-id>` fails with "not a directory" even with EVALLAB_RUNS_ROOT set —
   must pass the full trial dir path. (`src/evallab/tracing.py` trial resolution.)
2. `revisits.longest_identical_run.steps` is truncated to the first 20 steps for long runs (length 97 shows
   steps 25..44 only) — end must be derived as start+length-1 and cross-checked against the reason string.
   (`src/evallab/interpretation/run_report.py` revisits section.)
3. `errors.first_error` carries only step/offset/evidence; tool name, category and target require a join
   onto `errors.examples[0]`. Consider embedding them in first_error.
4. `loop_suspicion.reasons` truncates command text with a `:None` artefact, e.g.
   `bash_command:cd /workspace/repo && python3 - <<'PYEOF'\nimport quickfix as:None (6 failures)`.
5. `src/evallab/semantic_facts.py:59,185` UserWarning (`CapabilityOpportunity`/`EvidenceCoverage` field
   `construct` shadows parent) printed on every CLI invocation — noise on stderr.
6. No crash or oddity otherwise; report and outline agreed on loop flags/scores and first-error steps on all 10.

## No hand-label contact
Did not read `hand/*.json`, hand .md pages, or other tools' predictions; parent scores.

## PR #552 rerun (worktree HEAD bbe9cf50, branch traces/har-109-eval-gaps)
Reran both commands on all 10 trials from the PR worktree
(`EVALLAB_RUNS_ROOT=~/Developer/eval-lab/.worktrees/har104-runs/runs uv run --no-sync evallab report run
--json <trialdir>` + `evallab traj outline --json <trialdir>`, all rc=0, ~0.35 s each, ~7.2 s total wall).
Wrote `predictions/evallab_pr552.jsonl`; `evallab.jsonl` left untouched as the main baseline.
`tool` stays "evallab" (file name distinguishes the rerun); tool_minutes 0.012/row, cost $0.

New-field mapping: `loop_present`/`loop_span` also consider `revisits.longest_cycle` ([start_step, end_step]);
`upstream_fetch` = `outside_fetches.count > 0`; `pass_suspect` = `pass_may_be_copied is not null` on reward-1.0
runs, null on failures. attribution/task_verdict stay null (report states neither).

Deltas vs the baseline file:
- 000383 completion_confirmed true->false: PR report says "claimed at step 30, never confirmed; ended step 68
  (mark_task_complete)" — the two-turn-handshake confirmation rule no longer counts the step-68
  mark_task_complete as confirmation.
- 002864 loop_present false->true, span [29,102]: new `longest_cycle` {period 2, 37 repeats} over
  'cd /testbed && git diff --stat' / 'cd /testbed && git diff', with loop reason
  "repeating_command_cycle: period=2 (37x, steps 29-102)". Baseline's consecutive-run definition missed it.
- `longest_identical_run` now carries start_step/end_step (000226 [25,121], 001896 [34,36], 002256 [30,121],
  002391 [43,80]) — confirms the baseline's derived spans; steps array still truncated to 20 entries.
- `longest_cycle` null on the other 9 trials, so no other loop rows change.
- upstream_fetch true: 000226 (pip_download waitress==2.0.0, step 4, read back step 5), 000927 (curl of
  soupsieve util.py step 10 + pip_download soupsieve==1.9.1 steps 11/17, read back 12/18), 002407
  (pip_download control==0.9.3 steps 24/25, read back 26). All others count 0 -> false.
- pass_suspect: 000226 true, 000927 true (pass_may_be_copied with fetch/read-back evidence); 002391 false,
  002864 false (passed, flag null); null on all six failed trials.
- First-failure steps, stop reasons, and completion strings otherwise identical to baseline.
- Truncation artefact in loop reasons changed `:None` -> `:unknown` (e.g. "...import quickfix as:unknown
  (6 failures)") — still a truncation artefact, just relabelled.
- semantic_facts UserWarning noise on stderr still present at this commit.
