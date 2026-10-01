# HAR-128 part 1: SFT pass gate

This gate decides which `counted_pass` trials on training-split tasks Data may train on (G3), and where to cut each one. The input is the 9 trials Data listed on HAR-128 at 04:16Z.

## Files

| File | Contents |
|---|---|
| `labels.jsonl` | One row per trial, in Data's format: `job`, `trial`, `clean`, `reason`, `cut_step_id`. Extra fields are explained below. |
| `labels.sha256` | The digest Data checks. |
| `raters/rater_a.jsonl`, `raters/rater_b.jsonl` | The two independent blind ratings, with evidence quotes, network commands and degenerate spans. |

## Method

1. **Two raters.** Two independent agent raters, A and B, read each trial: the trajectory, the verifier diff and output, and the task's instruction and hidden tests. Neither saw the other's output.
2. **What counts as not clean:** any of these is enough.
   - a copied fix, i.e. upstream or released code fetched or read;
   - access to the hidden tests;
   - tampering with tests or the verifier;
   - a pass that is not a real solution.
3. **Cut step.** The cut is the last step to keep. It is the first completion after the final real work, and it includes the confirming `mark_task_complete` when that comes immediately after. Filler after the cut is dropped. If a run never declares completion, the cut is the last step of real work.
4. **Adjudication (me).** I adjudicated every disagreement from the trajectory. Then I checked each kept window mechanically for identical consecutive agent steps, comparing the message plus the tool calls.

## Result

| | count |
|---|---|
| Rated genuine by both raters | 9/9 |
| `clean` (usable for SFT) | 8/9 |
| Agreement on genuineness | 9/9 |
| Agreement on the cut step | 4/9 exact, 6/9 within 1 step |

- **Excluded: `har104-d-002391__WxBjcjX`.** The pass is genuine, but the kept window contains 63 empty-message repeats of the same command: steps 17–42, and 44–80, the longest run being 37. No single cut removes them and still keeps the completion. Rater A missed this; rater B flagged it, and the mechanical check confirmed it.
- **Two clean trials never declare completion:**
  - `RmCmzbQ` was stopped by the loop-break;
  - `NvEfxbt` hit its budget.

  `ends_with_completion: false` marks them, so Data can choose to drop them.

## Extra fields (for Data's data card)

- `genuine`: the pass itself is real, regardless of whether the trial is usable for SFT.
- `ends_with_completion`: whether the cut step is a `mark_task_complete` call.
- `format_warning_steps_kept`: the number of kept steps whose observation carries a Terminus-2 warning or parse error.
  - These warnings are non-fatal; the harness fills in defaults.
  - Training on these steps also trains on the assistant output that triggered the warning.
  - Two trials are mostly warning steps: `42F3G5T` has 85 of 87 steps, and `NvEfxbt` 50 of 51.
- `rater_a` and `rater_b`: each rater's `clean` and cut.

## Limits

- The raters are agents, not humans.
- n = 9.
- "No hidden-test access" was checked from the commands in the trajectory. A file read made inside a Python script that leaves no trace in the terminal would not be seen [INFERENCE: none of the 9 runs a script that could reach `/tests`; the hidden tests are only applied at grading].
