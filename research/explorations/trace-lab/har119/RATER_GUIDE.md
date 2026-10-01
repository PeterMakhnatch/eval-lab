# HAR-119 rater guide: blind hand labels for 12 HAR-110 v2 runs

You label one agent run at a time: MiMo-V2.6-Distill-Qwen-9B on the Terminus-2
harness, solving a Python bug-fix task. Your labels are the ground truth that
the tools are scored against. Nobody tunes anything after your labels are frozen.

## What you may read

- The trial folder you are given, and nothing else from the run:
  - `agent/trajectory.json` plus any `agent/trajectory.cont-N.json`
    continuation files, read in order;
  - `result.json` (reward, exception, token counts, `n_episodes`);
  - `verifier/` (reward, test output, `agent.diff`);
  - `exception.txt` and `trial.log` if present.
- The task folder:
  `~/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/<task>/`
  (`instruction.md`, `tests/`, `solution/` if present).

## What you must not read

Nothing else: no `research/` folder, no `derived/trace-lab`, no Linear, no
`evallab` commands, no Docent or Scout output, no other rater. If you open
something off-limits by accident, say so in `notes`.

## Step references

Cite a step as `<file>#<step_id>`, where `<file>` is `head` for
`trajectory.json` and `cont-N` for `trajectory.cont-N.json`, and `<step_id>`
is the `step_id` field of that step. Example: `head#25` or `cont-1#7`.

## Fields

1. **stop_reason**: exactly one of:
   - `model_finished`: the harness accepted the model's completion and the run ended;
   - `request_ceiling`: 120 model calls were used (`n_episodes` = 121, budget exception);
   - `token_ceiling`: the input-token budget ran out (budget exception, fewer than 121 episodes);
   - `infra_error`: the proxy, sandbox or model server failed;
   - `agent_timeout`;
   - `other` (explain in notes).
2. **first_failure**: the first step that made failure likely, meaning the point after which the run was headed for a fail and never recovered.
   - Give a `ref`, a one-line `what`, and a short `quote` from that step.
   - For an earned pass, use `null`.
   - For a pass that was copied, use the step where the upstream code was fetched or pasted.
   - If the run failed only because of infrastructure, use the first infra error step.
3. **blame**: exactly one of:
   - `model`: the model had what it needed and made the mistake;
   - `harness`: the Terminus-2 harness blocked or misread a reasonable action, e.g. rejected valid output, or ignored a completion it should have accepted;
   - `task`: the instruction or hidden tests made a pass unreasonable. Check `instruction.md` against `tests/`;
   - `infra`: proxy, sandbox or server failure;
   - `none`: an earned pass.
4. **loop_kind**: exactly one of:
   - `completion-claim`: after the model said the task was done (a completion claim, `task_complete`, or `echo task_complete`-style turns), the run kept going and most of the remaining turns are more claims or re-checks;
   - `repetition`: at least 10 turns repeat the same or nearly the same action without progress, and are not driven by a completion claim;
   - `none`.

   Also give `loop_span` as `[start ref, end ref]`, or null. If both kinds occur, pick the one that consumed more turns and describe the other in notes.
5. **pass_copied**: `true` if the passing code came from an upstream release or repository fetched during the run (pip download, curl, git clone and so on) rather than being written by the model. Otherwise `false`. For failed runs, use `null`.

Per field, also give `confidence`: `high` or `low`.
Keep `evidence` to at most 8 quotes with refs.
