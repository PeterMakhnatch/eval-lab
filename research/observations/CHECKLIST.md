# Observatory checklist (observatory-1)

Mechanical only. If a file is missing, write `none` / `0` — do not infer.

## Eight steps per trial

1. **Locate.** Trial directory contains `result.json` and `finished_at` is set
   (trial file or parent job `result.json`). Skip incomplete dirs.
2. **Identity.** From trial `result.json`: `id` → trial_id; `trial_name`;
   parent job directory name → job; `agent_info.name` → agent;
   `agent_info.model_info.name` or `config.agent.model_name` or `none` → model;
   `task_name` → task; `verifier_result.rewards.reward` → reward.
3. **Steps.** If `agent/trajectory.json` exists, `steps_taken` = number of
   entries in `steps`. Else `steps_taken` = 0.
4. **First failure.** Walk ATIF steps in order. First step whose tool result
   is an error, or whose message records an exception, is `first_failure_step`
   (use `step_id` if present, else 1-based index). If no such step: `none`.
5. **Loop.** Among ATIF tool calls, if the same command string (or tool
   name + arguments) appears **≥ 3 times**, `loop_detected` = yes and
   `loop_step` = `step_id` of the third occurrence. Else both `no` / `none`.
6. **Verified-before-done.** `yes` only if some ATIF step after the last
   write/edit tool is a verify/test/check command. No trajectory → `no`.
7. **Tool errors.** Count ATIF tool results that are errors. No trajectory → 0.
8. **Summary.** One sentence of facts only: agent, reward, exception type if
   any, and what the verifier file states. Cite `evidence_files` as relative
   paths actually opened (`result.json`, `verifier/reward.json`, …).

## SELF-AUDIT (every 10 records)

1. After a batch of 10 (or at end of a smaller first batch), pick **2**
   earlier records at random (or the two calibration records if fewer exist).
2. Re-derive each from the trial directory into a scratch copy using this
   checklist. Do not look at the committed record while filling.
3. Diff scratch vs committed. Any mismatch on a factual field: **stop**,
   fix this checklist, redo the whole batch.
4. Write the two trial names and pass/fail into `agents/handoffs/observatory.md`.
