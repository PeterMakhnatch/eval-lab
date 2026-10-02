# HAR-128 rater guide v2: blind hand labels for overnight runs

This guide changes HAR-119's `RATER_GUIDE.md` in three places, all from HAR-119's findings:
- `stop_reason` gains `loop_break`, because lf2 can stop a run when it detects a loop.
- `blame` is now defined for a copied pass (it was ambiguous in HAR-119 run CFCbfps).
- Every run also gets `sft_cut`, so passes go through the HAR-128 SFT gate in the same reading.

Everything else is unchanged so that scores stay comparable.

You label one agent run at a time: MiMo-V2.6-Distill-Qwen-9B on the Terminus-2 harness (lf2), solving a Python bug-fix task. Your labels are the ground truth the tools are scored against. Nobody tunes anything after your labels are frozen. You do not know which arm (stock, tuned or GEPA) produced the run, and you must not try to find out.

## What you may read

- **The trial folder you are given**, and nothing else from the run:
  - `agent/trajectory.json`, plus any `agent/trajectory.cont-N.json` files, read in order;
  - `result.json`: reward, exception, `n_episodes`;
  - `verifier/`: reward, test output, `agent.diff`;
  - `exception.txt` and `trial.log`, if present.
- **The task folder:** `~/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/<task>/`. That is `instruction.md` and `tests/`. Variant packages have the same instruction and tests.

## What you must not read

Nothing else:
- no `config.json`, `lab-metadata.json` or job-level files (they name the arm);
- no `research/` folder or `derived/trace-lab`;
- no Linear or `evallab` commands;
- no Docent or Scout output;
- no other rater.

If you open something off-limits by accident, say so in `notes` and set `off_limits_opened: true`.

## Step references

Cite a step as `<file>#<step_id>`:
- `<file>` is `head` for `trajectory.json` and `cont-N` for `trajectory.cont-N.json`;
- `<step_id>` is that step's `step_id` field.

Examples: `head#25`, `cont-1#7`.

## Fields

1. **stop_reason**: exactly one of the following.
   - `model_finished`: the harness accepted the model's completion.
   - `loop_break`: the harness stopped the run because it detected a loop. Look for a loop-break/stop message in the last steps, or in `exception.txt`.
   - `request_ceiling`: the model-call budget ran out (budget exception, with `n_episodes` at the request limit).
   - `token_ceiling`: the token budget ran out (budget exception, with fewer episodes than the request limit).
   - `infra_error`: the proxy, sandbox or model server failed.
   - `agent_timeout`.
   - `other`: explain in notes.
2. **first_failure**: the first step that made failure likely, meaning the run was headed for a fail from there and never recovered.
   - Give `ref`, a one-line `what`, and a short `quote`.
   - Earned pass: `null`.
   - Copied pass: the step where upstream code was fetched or pasted.
   - Failure caused only by infrastructure: the first infra error step.
3. **blame**: exactly one of the following.
   - `model`: the model had what it needed and made the mistake. **A copied pass is `model`.**
   - `harness`: Terminus-2/lf2 blocked or misread a reasonable action. Examples: it rejected valid output, ignored a completion it should have accepted, or loop-broke a run that was still making progress.
   - `task`: the instruction or hidden tests made a pass unreasonable. Check `instruction.md` against `tests/`.
   - `infra`.
   - `none`: an earned pass.
4. **loop_kind**: exactly one of the following.
   - `completion-claim`: after the model said it was done, the run kept going, and most of the remaining turns are more claims or re-checks.
   - `repetition`: at least 10 turns repeat the same or nearly the same action without progress, and are not driven by a completion claim.
   - `none`.

   Also give `loop_span` as `[start ref, end ref]`, or null. If both kinds occur, pick the one that used more turns.
5. **pass_copied**: `true` if the passing code came from an upstream release or repository fetched during the run. Otherwise `false`. Failed runs: `null`.
6. **sft_cut** (passes only; `null` for failed runs). This is the HAR-128 SFT gate.
   - **`clean`**: true only if the pass is genuine (none of: copied fix, access to the hidden tests, tampering with tests or the verifier, accidental pass) **and** the kept steps contain no run of 5 or more identical or empty-message repeated actions.
   - **`cut_step_id`**: the last step to keep. Keep up to the first completion after the last real work, including an immediately following confirming `mark_task_complete`. If the run never declares completion, keep up to the last step of real work.
   - **`reason`**: one line.

For each field, give a `confidence` of `high` or `low`. Keep `evidence` to at most 8 quotes with refs.

## Output (one JSON object per run)

```json
{"trial": "...", "stop_reason": "...", "stop_confidence": "high",
 "first_failure": {"ref": "head#N", "what": "...", "quote": "..."} | null, "first_failure_confidence": "high",
 "blame": "...", "blame_confidence": "high",
 "loop_kind": "...", "loop_span": ["head#A", "head#B"] | null, "loop_confidence": "high",
 "pass_copied": true|false|null, "pass_copied_confidence": "high",
 "sft_cut": {"clean": true, "cut_step_id": N, "reason": "..."} | null,
 "evidence": [{"ref": "head#N", "quote": "..."}], "notes": "", "off_limits_opened": false}
```
