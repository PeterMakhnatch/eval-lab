# HAR-119 part 2 — frozen tool → hand-field mapping (BLIND)

Written **2026-09-30T23:42:02Z** (UTC), BEFORE any tool was run on the 12
trials and before any rater output was seen. No rule below is tuned on the
12 runs. Where a tool does not express a field, the row emits `null` for
that field and the NOTES.md records it as `not expressed` (scored as such,
never guessed).

Target hand fields (RATER_GUIDE.md):
- `stop_reason`: `model_finished` | `request_ceiling` | `token_ceiling` |
  `infra_error` | `agent_timeout` | `other`
- `first_failure_ref`: `head#<step_id>` / `cont-N#<step_id>` in
  `trajectory.json` / `trajectory.cont-N.json` numbering, or `null`
- `blame`: `model` | `harness` | `task` | `infra` | `none`
- `loop_kind`: `completion-claim` | `repetition` | `none` (+ `loop_span`
  `[start ref, end ref]` or `null`)
- `pass_copied`: `true` | `false` | `null` (null for fails)

Worktree HEAD at mapping time: `71f86346` (branch
`traces/har-119-trace-review`; assignment named merged main `57dd7091`,
which is the parent of the two census commits on top — tools run as merged,
no tool rule changed).

## 1. Eval Lab (as merged, `evallab report run --json` + `process-job`)

Native outputs used (frozen code, not redefined here):
- `outcome.stop_reason` ∈ {`trial_budget_exhausted`, `task_complete`,
  `prose_completion`, `agent_timeout`, `error`, `unknown`} plus
  `outcome.stop_detail` binding ceiling (`binding ceiling: requests` /
  `input_tokens` / `output_tokens` / `total_tokens` / unknown), from
  `classify_stop_reason` + `_binding_ceiling` (mirrors probe-03
  `ceiling_which`). `outcome.first_failure` (#560) =
  `{step, kind, evidence, confidence}`, kinds: `upstream_fetch`,
  `harness_rejection`, `stuck_cycle`, `tool_error`, `bad_edit`, or null
  (`none found`). NOTE: `step` is a stitched-trajectory ordinal
  (`_Step.step`), with `native_step_id` kept separately — it is NOT a
  `head#`/`cont-N#` ref.
- `revisits.longest_identical_run` (`start_step`/`end_step`) /
  `revisits.longest_cycle` (`start_step`/`end_step`) + `token_flow`
  `loop_onset` (normalized-command run ≥ 4, `src/evallab/token_flow.py`).
- `outside_fetches.items[]` + `pass_may_be_copied` (fetch/read-back evidence).
- `outcome.completion` string (first claim / confirmation / final turn).
- No attribution, no task-soundness verdict, no claim-vs-repetition loop
  distinction in native output.

Mapping:
- `stop_reason`: `task_complete`/`prose_completion` → `model_finished`;
  `trial_budget_exhausted` + binding `requests` → `request_ceiling`;
  `trial_budget_exhausted` + binding `input_tokens`/`output_tokens`/
  `total_tokens` → `token_ceiling`; `trial_budget_exhausted` + binding
  unknown → `other` (raw kept); `agent_timeout` → `agent_timeout`;
  `error` → `infra_error`; `unknown` → `other`. Raw
  `stop_reason`+`stop_detail` preserved in raw source fields.
- `first_failure_ref`: **not expressed**. Native `outcome.first_failure.step`
  is a stitched ordinal, not a `head#`/`cont-N#` ref, and the report carries
  no segment→file map. Rows emit `null`; raw `step`/`kind`/`evidence`/
  `confidence` preserved in raw source fields. (Scout/probe-03 below is the
  ref-carrying path.)
- `blame`: **not expressed** (report states no attribution and no task
  verdict). Rows emit `null`.
- `loop_kind`: **not expressed** (report has loop spans but no
  completion-claim (≥10 turns after first confirm prompt, ≥50% claim-bearing)
  vs repetition (≥10 near-identical turns, HAR-114 onset) distinction).
  Rows emit `null` for kind. `loop_span`: expressed — `[start_step, end_step]`
  from `longest_cycle` when present else `longest_identical_run` (as numeric
  steps; refs not expressed, so span endpoints are numeric steps, recorded
  as such in raw fields and surfaced as `loop_span` numerics, not refs).
  No loop → `none`/`null` span only when both are null AND token_flow onset
  is null; otherwise span from whichever fired.
- `pass_copied`: expressed via `pass_may_be_copied`: non-null on reward-1.0
  runs → `true` iff flagged (fetch/read-back evidence), `false` iff
  explicitly clean; `null` on failed runs (no passing reward to suspect).
  `outside_fetches.count` kept raw.

## 2. Scout (Inspect Scout, deterministic, no model)

Scanners used exactly as in `research/explorations/trace-lab/scout/scanners.py`
(10 total — the card says "4 text + 6 rule"; actual file holds 10):
text: `repeated_assistant_message`, `harness_parse_errors`,
`native_tool_call_markup`, `restored_tool_calls`;
rule (via `rules.analyze_trial_rules` → probe-03 `capabilities.py`, no label
lookup): `rule_outcome`, `rule_first_failure`, `rule_handshake`,
`rule_loops`, `rule_stop`, `rule_wedge`.
Normalization first with `normalize/harbor_normalize.py`; probe-03 rows via
`probe-03-capabilities/capabilities.py` (same functions the scanners call).
Reuse of the HAR-109 mapping (`har109/predictions/scout.notes.md`).

Native values: `rule_stop` ∈ {`ceiling:input_tokens`, `ceiling:output_tokens`,
`ceiling:total_tokens`, `ceiling:requests`, `ceiling:trial_budget`,
`task_complete_confirmed`, `agent_timeout`, `model_auth_error`, `unknown`, …};
`rule_first_failure` ∈ rule id or `none` (+ `step_ref` `head#…`);
`rule_outcome` = `tag/attribution/rule_id` (attributions: `n/a`, `model`,
`harness`, `unclear`); `rule_handshake` ∈ {`confirmed`, `unconfirmed`,
`none`}; `rule_loops` value = longest identical-run length (0 when no
≥10 byte-identical run), metadata `spans[]` (`kind`/`span`/`length`);
`rule_wedge` bool (stuck-terminal, not a loop_kind).

Mapping:
- `stop_reason`: `ceiling:input_tokens`/`output_tokens`/`total_tokens` →
  `token_ceiling`; `ceiling:requests` → `request_ceiling`;
  `task_complete_confirmed` → `model_finished`; `agent_timeout` →
  `agent_timeout`; `model_auth_error`/`unknown` + exception → `infra_error`
  when an exception is present else `other`; `ceiling:trial_budget` →
  `other` (raw kept). Raw `rule_stop` value + ceiling kept.
- `first_failure_ref`: `rule_first_failure.step_ref` (`head#…` /
  `trajectory.cont-N.json#…`, normalized to `cont-N#…`) when value ≠ `none`;
  when `none`, fallback to the outcome rule's evidence step
  (`outcome.evidence_step_refs[0]`, numeric `#` part) with `what` = outcome
  `rule_id` (per HAR-109 mapping). Null only when both are absent. Raw
  `rule_id`/`tag`/`attribution` kept.
- `blame`: literal outcome attribution: `n/a` (R-NONE-01 pass) → `none`;
  `model` → `model`; `harness` → `harness`; `unclear` → `null` (**not
  expressed** — rater has no `unclear`). `task` and `infra` are **never
  emitted** by this tool (notably R-ENV-02 suspect-grader votes stay
  `harness`, not `task`, per the frozen literal map — known limitation,
  not retuned).
- `loop_kind`: **not expressed**. `rule_loops` measures byte-identical
  consecutive runs (≥10); it has no completion-claim (claim-phase share)
  vs repetition (HAR-114 onset) distinction, and `rule_wedge` is
  stuck-terminal, not a loop. Rows emit `null` for kind. `loop_span`:
  expressed — longest `rule_loops` span `[start ref, end ref]` (refs as
  emitted); no span → `null` (kind then `none` only when no span AND no
  confirmation-loop span; otherwise kind stays `null`/not expressed with
  span present).
- `pass_copied`: **not expressed** (`rules.analyze_trial_rules` intentionally
  omits taint/fetch; capabilities computes `pass_caveats` but the shared
  rule output surfaces no fetch flag — per HAR-109 notes). Rows emit `null`.

## 3. Docent (hosted, Transluce; reading model `openai/gpt-5.6-luna`)

Frozen prompt/schema exactly as
`research/explorations/trace-lab/docent/har81_reading_scoring.md` (same
reading model, one reading only, blind metadata only). Native output per run:
`reasoning`, `terminal_stuck` bool, `stuck_start_evidence`,
`stuck_end_evidence`, `stuck_cause` ∈ {`pager`, `continuation`,
`interactive`, `running_process`, `none`}, `false_completion_claim` bool,
`claim_evidence`, `failure_owner` ∈ {`model`, `harness`, `task_or_grader`,
`none_passed`, `unclear}`, `owner_evidence`, `first_mistake_evidence`
(quote only). Mapping reuses `har109/predictions/docent.notes.md` adapted to
the five fields (HAR-109 schema fields `task_verdict`/`pass_suspect`/
`upstream_fetch`/windows have no counterpart here except as raw).

Mapping:
- `stop_reason`: **not expressed** (no stop notion in the schema; stop input
  was blind Eval Lab metadata, not a finding). Rows emit `null`.
- `first_failure_ref`: **not expressed** (quote only, window-scored ±5, never
  an exact step per the frozen scoring map). Rows emit `null`; raw quote kept.
- `blame`: `model` → `model`; `harness` → `harness`; `task_or_grader` →
  `task`; `none_passed` → `none`; `unclear` → `null` (**not expressed** —
  rater has no `unclear`). `infra` is **never emitted** (schema has no infra
  notion). Prior: judges blame the model on every failure (HAR-81) — recorded
  here, not tuned away.
- `loop_kind` + `loop_span`: **not expressed** (no loop notion;
  `terminal_stuck`/`stuck_cause` is wedge/stuck-terminal, not the rater's
  completion-claim/repetition loop). Rows emit `null`/`null`; raw stuck
  fields kept.
- `pass_copied`: **not expressed** (HAR-81 schema has no fetch/taint notion).
  Rows emit `null`.
