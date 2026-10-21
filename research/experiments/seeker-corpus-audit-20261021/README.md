# seeker-corpus-audit-20261021

Data-quality audit of `research/datasets/seeker-sft-v1/seeker_tool_sft.jsonl`, the
corpus every seeker SFT arm so far has trained on (pE-SELF-8). Free and local: it
reads the committed corpus, no model and no sandbox.

## Why

pE-SELF-5 and pE-SELF-6 trained `Qwen/Qwen3-8B` on this corpus four ways (full
loss, assistant-masked loss, base, trained) and every arm confirmed `0/6` `1/0`
differentials, with reproduction similarity to the corpus's own exploits never
above `0.29`. pE-SELF-7 then showed the models can *name* the right weakness class
(`3/4` plausible claims) without being able to *execute* it. This audit asks
whether the corpus can teach execution at all.

## Measurements (`corpus-audit.json`)

### Effective size — the corpus is ~39 distinct examples, not 90

| Quantity | Count |
| --- | ---: |
| Conversations | 90 |
| Distinct `submission/solve.sh` bodies | 25 |
| **Distinct (task, body) pairs** | **39** |
| Distinct tasks | 31 |
| Duplicated bodies | 25 |
| Extra copies beyond the first | 65 |
| Positive conversations → distinct (task, body) | 60 → **24** |
| Negative conversations → distinct (task, body) | 30 → **15** |

The single most duplicated body appears `30` times — it is the shared no-op
`solve.sh` of the `mimo_clean_control` negatives, so those `30` conversations
carry one distinct example between them. `cargo-flight-dispatch` repeats one body
`8` times, `html-js-filter` `6`, `vf2-speedup-networkx` `4`.

### Execution content — real and dominant

| Quantity | Value |
| --- | ---: |
| Assistant description characters | 44,228 |
| `write_file` argument characters | 274,932 |
| **Execution share of assistant content** | **86.1%** |

So the corpus is not short on execution *text*: the assistant turns are mostly the
exploit programs themselves.

### Diversity — bespoke programs, no shared template

| Class | Bodies | Distinct | Mean pairwise similarity | Max |
| --- | ---: | ---: | ---: | ---: |
| `grader_tamperable` | 26 | 13 | 0.502 | 1.000 |
| `weak_check` | 24 | 8 | 0.391 | 1.000 |
| `answer_reachable` | 10 | 5 | 0.254 | 1.000 |

Within a class the programs share about half their text at most, and the `1.000`
maxima are exact duplicates. There is no class-level template the model could
generalize from — each exploit is bespoke to its task.

### Coverage

| Dimension | Distribution |
| --- | --- |
| `exploit_found` | `true` 60 / `false` 30 |
| `mechanism_task_class` | `grader_tamperable` 26, `weak_check` 24, `answer_reachable` 10, `unknown` 30 |
| `conditioning_mode` | `hackbook_conditioned` 45 / `unconditioned` 45 |
| `source_family` | `breaker_bench` 30, `mimo_clean_control` 30, `tb4_dev` 22, `tb2_tb21` 8 |
| `verified_differential` | `true` 56 / `false` 34 |

## Verdict

**The corpus cannot teach execution, and the reason is size in *distinct
programs*, not text.** It offers `24` distinct positive exploits across three
classes — too few to memorize (the arms reproduce none of them) and too
heterogeneous to generalize (no class-level template). The `30` negatives are one
repeated no-op, so they teach "write a conservative submission" and nothing about
where a grader is weak, which is exactly the behaviour the assistant-masked arm
learned (`6/6` valid, `0/6` confirmed, all ~100-character no-op submissions).

This is consistent with every measurement the lane has: the models diagnose
(`3/4` plausible claims) and cannot execute (`0/6` confirmed), and the corpus has
only `24` executions to learn from.

## What would move it

1. **More distinct executed exploits per class.** The gym's rejection sampling
   (`seeker-gym-v0` → `seeker-sft-v0`) is the right source, but the audit says the
   *distinct-program* count is the number that matters, so the sampler should be
   scored on distinct confirmed exploits, not on rows produced.
2. **A class-level template, not just a class label.** The corpus conditions on
   `hackbook_conditioned` (`45` rows) but the conditioning carries the class, and
   the programs show no shared structure. Conditioning on the Hackbook's *recipe*
   for the class would give the model something transferable to imitate.
3. **Do not grow the corpus by repeating bodies.** `65` of the `90` conversations
   are extra copies; more copies of the same `25` programs cannot teach more.

## The data supply: `seeker-gym-v0`'s generator is budget-bound (`gym-budget-probe.jsonl`)

The gym is the intended source of the distinct executed exploits this corpus
lacks. Its v0 run recorded `14/32` candidates as `unparsed_generation`, so this
probes the same prompt and the same three tasks (`atrx-vep-crispr`, `cad-model`,
`cargo-flight-dispatch`) at two budgets, `k=8` each, reusing the gym's own
`PROMPT`, `task_view` and `MODELS` and changing only `max_tokens`.

| Budget | Candidates | Parsed | With `solve.sh` | `finish_reason=length` | Cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| `1200` (the gym's value) | 24 | **6 (25%)** | 6 | 17 | $0.00535 |
| `3072` | 24 | **21 (87.5%)** | 21 | 1 | $0.00708 |

**The gym's `max_tokens: 1200` is the binding constraint on its own data supply.**
It truncates `17/24` generations mid-JSON, and raising it to `3072` lifts the parse
rate from `25%` to `87.5%` for `$0.007`. This is the same failure mode as
pE-SELF-5's `768`-token generation budget, at the same place in the pipeline: the
submission is cut off before its closing brace.

Grading the parsed candidates is free on local Docker (see the protocol's
correction), so the probe was re-run at `3072` with grading
(`gym-graded-probe.jsonl`):

| Budget | Candidates | Parsed | Graded | Upstream `1.0` | Verified |
| --- | ---: | ---: | ---: | ---: | ---: |
| `3072` | 24 | 22 (92%) | 22 | **0** | **0** |

**The budget was necessary but not sufficient.** With the JSON intact, `22/22`
candidates still fail the upstream grader. Their ideas are the tamper family —
overwrite `test_outputs.py` with a passing dummy, `rm` it, replace `test.sh` with
`exit 0`, write an empty `{}` at the expected output path — and none of them
lands. That is the same shape the corpus audit found: the model names the right
class (`grader_tamperable`) and cannot execute it. So the gym's data supply needs
the budget fix *and* candidates that can actually reach a grader gap — a stronger
generator or a tool loop that can read the grader before writing, which is what
`evallab.seeker`'s `ConfinedTools` loop does and a single-shot JSON call cannot.

## Files

| File | Role |
| --- | --- |
| `corpus_audit.py` | the measurements |
| `corpus-audit.json` | the report |
| `gym_budget_probe.py` | the gym generation-budget probe (and optional local-Docker grading) |
| `gym-budget-probe.jsonl` | the probe's per-candidate rows |
| `gym-graded-probe.jsonl` | the probe re-run at `3072` with grading |
