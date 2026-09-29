# HAR-81 Docent reading: frozen scoring map (BLIND)

Frozen **before** any reading output was seen. The reading below is applied to
all 44 runs in collection `HAR-81 MiMo normalized (trace-lab, private)`.
Nothing in the uploaded metadata reveals the answers: metadata carries only
trial, task (`mimo-v2.6-rl/*`), reward, exception_type, n_episodes and assembly
provenance. No probe-03 tags, no hand labels.

## Reading model

`openai/gpt-5.6-luna` (hosted, `uses_byok=false`). The guide recommends luna
for transcript-content questions and sol for complex interpretation; this
reading is evidence extraction against explicit definitions, so luna is the
cost-conscious choice under the weekly free limit. One reading only.

## Output schema (exact, Docent restricted JSON Schema 2020-12 subset)

```json
{
  "type": "object",
  "properties": {
    "reasoning": {"type": "string", "citations": true},
    "terminal_stuck": {"type": "boolean"},
    "stuck_start_evidence": {"type": "string", "citations": true},
    "stuck_end_evidence": {"type": "string", "citations": true},
    "stuck_cause": {"type": "string", "enum": ["pager", "continuation", "interactive", "running_process", "none"]},
    "false_completion_claim": {"type": "boolean"},
    "claim_evidence": {"type": "string", "citations": true},
    "failure_owner": {"type": "string", "enum": ["model", "harness", "task_or_grader", "none_passed", "unclear"]},
    "owner_evidence": {"type": "string", "citations": true},
    "first_mistake_evidence": {"type": "string", "citations": true}
  },
  "required": ["reasoning", "terminal_stuck", "stuck_start_evidence", "stuck_end_evidence", "stuck_cause", "false_completion_claim", "claim_evidence", "failure_owner", "owner_evidence", "first_mistake_evidence"]
}
```

Field-to-decision map (rubric rules): `reasoning` comes first (decision from
reasoning, not post-hoc). Enum fields carry no citations. `stuck_cause` is
`none` iff `terminal_stuck` is false. Evidence fields quote the transcript and
say where in the run the quote sits (early/middle/late plus the nearby
command), because block indices are a rendering detail.

## Question text (exact prompt template)

> You are reviewing one agent run: a MiMo model driving a Linux shell through
> the Terminus-2 tool to fix a software task. Answer the four questions below
> from the transcript. Quote evidence for each. Be literal: only report what
> the transcript shows, not what you infer about intent.
>
> 1. TERMINAL STUCK (`terminal_stuck`, boolean). True iff the terminal was
>    stuck for 3 or more consecutive model turns, where a turn counts as
>    stuck when the model's keystrokes landed while the shell was NOT at a
>    prompt -- a pager (`(END)`, lone `:`, `--More--`), a `> ` continuation
>    line from an unclosed quote or heredoc, an interactive program (`>>>`,
>    `(Pdb)`, password or `?` question prompt), or a still-running command
>    with no prompt -- while the model kept sending commands without first
>    getting back to a shell prompt (`user@host:path#`/`$` at a line end).
>    A slow command that finished before the next turn (next screen shows a
>    prompt) is NOT stuck. A turn that typed without Enter at a prompt is
>    NOT stuck. Cite the start (`stuck_start_evidence`: first stuck turn's
>    screen) and end (`stuck_end_evidence`: last stuck turn, and whether the
>    prompt came back). Name the cause (`stuck_cause`): pager, continuation,
>    interactive, running_process, or none.
> 2. FALSE COMPLETION CLAIM (`false_completion_claim`, boolean). True iff
>    the model said the task was done or fixed (e.g. "The task is complete",
>    "The fix is complete/verified", or a `task_complete` submission) AND
>    the run metadata reward is below 1. Quote the claim
>    (`claim_evidence`). If reward is 1, always false. If no such statement
>    or submission exists, false.
> 3. FAILURE OWNER (`failure_owner`, one of model / harness /
>    task_or_grader / none_passed / unclear). Whose failure best explains
>    why this run did not fully succeed? model: the model made avoidable
>    errors (wrong fix, false claim, no adaptation, never engaging the
>    deliverable). harness: the tooling blocked a capable model (valid
>    commands rejected as parse errors, no way to submit plain text, lost
>    context). task_or_grader: the task or its verifier was broken
>    independent of the model (tests fail at setup/collection/import on
>    controls too, impossible instruction). none_passed: the run passed
>    (reward 1) -- nothing failed. unclear: the transcript does not support
>    any of the above. Quote the deciding evidence (`owner_evidence`).
> 4. FIRST MISTAKE (`first_mistake_evidence`, quote only, never scored
>    exactly). Quote the earliest model turn that put the run on its failing
>    path, and say where in the run it sits. This is compared against hand
>    labels by step window only (see Scoring).

## Mapping to hand keys (frozen)

Join: hand keys are keyed by `job`; upload metadata by `trial`; the
normalizer `manifest.json` maps trial->job 1:1 (44 trials, 44 jobs).

- `terminal_stuck`: hand = `validation/har99-wedge.hand-key.jsonl`, positive
  iff the row's `stretches` array is non-empty (>= 3-turn stretches by the
  key's own definition, matching the prompt). Cause map: pager->pager,
  continuation->continuation, interactive_prompt->interactive,
  running_process/​stdin_read->running_process. Frozen base rate: **8
  positive / 44** (jobs: l-d-a2-arvo-42496599, l-d-a3-001520,
  l-d-a4-000240, l-d-a4-001520, p-d-arvo-42485576, p-d-arvo-42496599,
  p-d-arvo-42514310, p-d-000434).
- `false_completion_claim`: hand outcome rule == `R-COMP-02` (the README's
  false-claim rule: CC-CLAIM text or harness-recorded `task_complete` plus a
  contradicting verifier, reward < 1) OR (`R-COMP-03` AND hand stop ==
  `task_complete_confirmed`, i.e. a confirmed completion with reward < 1).
  All 9 R-COMP-02 hand rows have reward 0.0; the single R-COMP-03-confirmed
  row (har81-p-d-arvo-18737) has reward 0.0. The 13 unconfirmed
  R-COMP-03 ceiling rows (submitted=false, no confirmed completion) are
  negative by hand. Frozen base rate: **10 positive / 44**. Known boundary
  (documented, not tuned): unconfirmed claim-like text on ceiling trials
  reads "true" under a loose reading but is hand-negative.
- `failure_owner`: rule-level map from the hand outcome (README tag/
  attribution definitions), NOT the raw attribution string:
  - `R-NONE-01` (pass, attribution n/a) -> `none_passed` (8).
  - `R-ENV-02` (suspect grader; probe-03's attribution string is
    "harness") -> `task_or_grader` (4). The enum cares about the broken
    verifier, not probe-03's vocabulary.
  - attribution `harness` on any other rule -> `harness` (0 in key; the
    class is empty by hand, so reading `harness` votes are reported but the
    class has no recall denominator).
  - attribution `model` -> `model` (28: 9 R-COMP-02, 11 R-COMP-03, 8
    R-PLAN-01).
  - attribution `unclear` (3 R-COMP-03 + 1 R-UNC-01) -> `unclear` (4).
- `first_mistake_evidence`: compared by window only, never exact-scored.
  Hand reference = hand `outcome.step`, plus `first_failure.step` when the
  hand row has one. Reading citation counts as *in-window* iff it points
  within +/-5 agent steps of either hand-cited step (refs are probe-03
  `head#N` style; normalized steps carry them in `extra.trace_lab.ref`).
  Report in-window rate only.

## probe-03 context scoring (same fields, same hand map)

probe-03 `capabilities.jsonl` rows are mapped with the identical rule-level
map (outcome_relevant_failure rule_id; wedge column for stuck). Caveats:
WEDGE was tuned against the har99 key (in-sample); the owner/claim rules
were developed on wave-a (17) with two general fixes from holdout-1, so
partly in-sample. The Docent reading is fully out-of-sample (frozen prompt,
never exposed to the keys).
