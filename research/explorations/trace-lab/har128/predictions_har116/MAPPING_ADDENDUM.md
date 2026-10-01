# HAR-128 part 2 — mapping addendum for the 40 HAR-116 trials (BLIND)

Written **2026-10-01T08:00:15Z** (UTC), BEFORE any tool was run on the 40
HAR-116 trials and before any rater output was seen. Reuses
`research/explorations/trace-lab/har119/predictions/MAPPING.md` exactly, with
the two additions below. No rule is tuned on the 40 runs. Where a tool does
not express a field, the row emits `null` for that field (`not expressed`,
never guessed).

Worktree HEAD at mapping time: `ba8d8358` (branch `traces/har-128-tools`,
tracking `origin/main`; includes #610 at `dc74d9e9`). Tools run as merged; no
tool rule changed.

## Addition 1: `stop_reason` = `loop_break`

The HAR-116 loopfix-r2 arm (internally `lf2`) ends looped runs with a
harness loop-break: `evallab.harbor_terminus.LoopBreakStop`
(`loop break: the repetition was still going ...`), agent metadata
`stop_reason: loop_break` (`result.json` `agent_result.metadata.stop_reason`),
and a trajectory `final_metrics.extra.loop_break` record. Neither frozen
mapping target knows this value (MAPPING.md `stop_reason` targets are the
rater set `model_finished | request_ceiling | token_ceiling | infra_error |
agent_timeout | other`), and neither shipped tool expresses it today:

- Eval Lab `classify_stop_reason` maps the `LoopBreakStop` exception to
  `error` (only `trial_budget_exhausted` metadata is special-cased); the
  `loop_break` metadata value has no branch.
- Scout/probe-03 `EXCEPTION_STOP` has no `LoopBreakStop` entry, so the probe
  stop falls back to `unknown`.

Mapping (Eval Lab and Scout only):

- `LoopBreakStop` in the exception type/message (`stop_detail` /
  `exception_type` containing `LoopBreakStop`), OR a `loop_break` stop
  (`agent metadata stop_reason == loop_break`, probe metadata stop, or a
  native `loop_break` stop string) → prediction `stop_reason` =
  `loop_break`.
- Raw fields keep the native values verbatim (`raw_stop_reason`,
  `raw_stop_detail`, `raw_exception_type`, `raw_probe03_*` as applicable), so
  the mapping stays auditable.
- This is a new prediction value outside the MAPPING.md rater set; scoring
  decides how to treat it. Nothing else in MAPPING.md changes.
- Docent (both the HAR-119 reading and the `model-readers` strong reading)
  is unchanged: its frozen schema enum has no `loop_break`, so Docent rows
  keep the schema values (`other` when none fits). For the model-readers
  output, reuse `model-readers` `build_predictions.py` / `reading.py` mapping
  as-is.

## Addition 2: `first_failure_step` (int or null)

Output rows carry `first_failure_step` alongside `first_failure_ref`
(`head#N` or null):

- Eval Lab: `null` (not expressed, same as `first_failure_ref`; the native
  `outcome.first_failure.step` is a stitched ordinal, kept only in
  `raw_first_failure_step`).
- Scout: integer `N` parsed from `first_failure_ref` (`head#N` trailing
  `#N`; `cont-N#M` → `M`), else `null`.
- Docent strong: mapped step of the first citation block
  (`raw_first_failure_step`), `null` on passes (`first_failure_is_pass`) or
  unmapped blocks.

All other fields (`stop_reason` otherwise, `first_failure_ref`, `blame`,
`loop_kind`, `loop_span`, `pass_copied`, `raw_*`) follow MAPPING.md literally.
