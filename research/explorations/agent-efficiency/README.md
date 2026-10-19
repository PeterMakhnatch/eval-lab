# More verified work per frontier-model token

Exploratory engineering study started October 17, 2026, 2:12 AM ET, from
`5171c20c7d1f5c00d020db6087eac3041f94bf13`. The objective is correct work per token,
not merely shorter prompts. [results.json](results.json) contains the numerical
projection; [study.json](study.json) records admission rules and follow-ups.
[trace-findings.md](trace-findings.md) is the earlier read-only process audit,
not the final implementation or experiment report.

## Decisions

The strongest observed levers are **explicit verification ownership** and
**bounded, provenance-preserving tool output**. Smaller context alone did not
produce a useful replicated coding-token improvement. Do not lower effort,
weaken validators, remove checks globally, or infer invoice savings from these
results.

All token comparisons below use reported input plus output, including cached
input. Noncached input is reported separately. Neither is a subscription quota
meter or bill. Positive reductions mean fewer tokens; a negative reduction means
an increase.

| Experiment | Original → candidate acceptance | Input + output reduction | Noncached input reduction |
|---|---|---:|---:|
| Explicit implementer-owned → parent-owned verification | 3/3 → 3/3 | 39.66% | 44.89% |
| Full → summary/range trajectory navigation | 12/12 → 12/12 | 27.05% | 23.84% |
| Full → fused first-error navigation, held-out source cohort | 12/12 → 12/12 | 48.33% | 21.52% |
| Context compiler, two-repeat coding main | 6/6 → 6/6 | 0.44% | −8.27% |
| Requested high → medium effort, fixed context | 6/6 → 4/6 | 34.97% | 13.23% |
| Requested high → medium, both explicitly parent-verified | 3/3 → 3/3 | −20.31% | 10.82% |

These are small, bounded observations, not general quality non-inferiority.
Coding studies reuse three original problems; repeats and individual assertions
are not independent tasks. The two navigation cohorts have disjoint native heads
but overlap one underlying benchmark task: twelve sources cover nine tasks.
Do not subtract the two navigation effects to estimate a causal benefit of fusion.

### Verification ownership: remove duplicated ownership, not verification

Both arms used requested high effort, identical base prompts/context packs and
snapshot-capability instructions. Complete prompts differed only inside an
explicit ownership block. The implementer-owned arm performed authorized focused
checks; the background-worker arm was explicitly edit-only and the parent owned
the same unchanged external acceptance grader.

The worker arm complied in all three observed episodes: no tests, lint/build,
import/syntax or behavioral checks, and no Python commands. Both arms passed all
82 evaluated feature assertions plus their source-scope checks. These are many
assertions on three problems, not 82 independent successes. Output fell 58.08%.
The baseline's checks found and corrected formatting problems, so they cannot
all be labeled wasted work. This is a useful narrow assignment pattern only
when a real integration owner actually performs acceptance and repository gates.

A separate post-hoc finishing audit makes that obligation concrete:

| Arm | Lint passes | Episodes still needing formatter cleanup |
|---|---:|---:|
| Implementer-owned checks | 3/3 | 1/3 |
| Edit-only worker | 3/3 | 2/3 |

Mechanical formatting on disposable source copies fixed all three cases,
preserved their ASTs, and passed all 88 evaluated behavioral probes with no new
trial-model calls. This is not full CI or a scope regrade; original grades remain
unchanged. Worker token savings do not justify skipping parent finishing.

The local change-impact skill now binds the root/cwd, owned paths, non-goals,
observable acceptance, available runtime, source pointers and verification owner.
It does not impose a no-checks rule on primary implementers.

### Navigation: retrieve less text without changing the evidence

The public trajectory CLI now supports:

- `traj outline TARGET --summary [--json]`: full-run aggregates and provenance,
  without individual step bodies.
- `traj outline TARGET --steps START:END [--json]`: an inclusive range of logical
  outline IDs, retaining each record's original source document/digest/native ID.
- `traj outline TARGET --first-error [--json]`: the first recorded error-flagged
  step and full-run accounting in one read. No flagged error means no step bodies,
  not proof of success or absence of a causal failure.

Default full JSON stays unchanged. Missing, malformed or entirely nonmatching
ranges fail rather than silently returning an empty successful excerpt. Full-run
metrics, continuation coordinates, hashes and known/unknown spend are not
recomputed from selected steps. The trace-reading skill recommends bounded reads
first, while retaining raw-source access when the question needs it.

Each main navigation arm answered all 72 frozen fact items correctly. Actual
command counts nevertheless increased: 33 to 53 in the first cohort and 38 to 42
in the second. One source in each cohort cost more tokens under the candidate.
This is a measured token result, not a promise of fewer tool calls or lower
latency for every trace.

### Context compiler: safety improvement, not a universal coding-cost win

Path-scoped shedding drops generated lookup inventories before ordinary matching
living documentation, and preserves `docs/NOW.md` until those other documents
have shed. Discovery, relevance scores, unbudgeted behavior and retained semantic
excerpts remain unchanged. Omission is disclosed and never waives governing
instructions. Assignment briefs no longer prescribe unrelated task authoring or
an obsolete separate human/reviewer handoff gate.

The first coding pilot suggested a 16.26% total-token reduction, but noncached
input already increased 2.58%. The two-repeat main did not replicate that useful
size of gain and increased output 5.93%. Retain the structural budget-safety fix;
do not advertise prompt-size reduction as a general coding-performance result.

The exact 133-document intermediate corpus is retained privately, not reconstructed
from today's changing docs. Replaying both compilers at three budgets reproduced
all 54 pack hashes, byte/character/estimate counts and state-retention flags.
Character-based budget estimates and offline encoders are not provider usage.

### Effort: preserve failures and investigate their cause

The original medium arm's two trajectory episodes failed the source-only scope
check because ordinary trajectory tests created canonical behavior-label Parquet
and lock files. The grader short-circuited: original feature acceptance was **not
evaluated**, not demonstrated failing or passing.

Separate post-hoc diagnostics copied only each recorded permitted source patch
onto a fresh pinned baseline and ran the unchanged grader. Both passed all 43
checks. These are source-patch diagnostics, not new coding episodes or regrades;
the original arm remains 4/6 accepted.

The real test-isolation defect is fixed. The trajectory module now supplies a
per-test temporary `EVALLAB_DERIVED_ROOT`, and its CLI-label test verifies real
persisted label/provenance/digest behavior there instead of pinning English
banners. Before and after, the real existing test module exits successfully;
before creates canonical lock/Parquet files and after changes neither. Explicit
per-call roots and concurrent-writer coverage remain real. This matters especially
in linked worktrees, whose default derived store is shared with the primary.

An **offline replay**, not an executed cascade, selected the six observed medium
attempts plus matched observed high attempts for the two failures. It retained all
failed-attempt tokens and cost 11.47% more total tokens, 35.41% more noncached input
and 2.57% more output than all-high. No fallback model calls or lower-effort default
were adopted. Order/cache confounding and three-task clustering still apply.

A separately registered six-episode composition probe gave **both arms the exact
same complete edit-only, parent-verified prompts**, varying requested high versus
medium effort. Both passed all 82 feature assertions and source-scope checks;
all 30 recorded commands were passive reads, with no worker verification.
Medium used 19.50% less output and 10.82% less noncached input, but **20.31% more
total tokens** because cached input increased. Commands fell 17 to 13, so fewer
commands did not imply fewer tokens. These are the same three convenience
problems with one repeat, not new held-out coding evidence or a lower-effort
default recommendation.

## Cross-family submission experiments

These policy/citation probes used the existing SDK with exact requested
`google-antigravity/gemini-4-argon-low` and
`google-antigravity/claude-opus-5-5` routes. Native coding/navigation requested
`gpt-6.1-sol`. Reported route identity is gateway self-report, not serving
attestation. No new Meta coding result is claimed.

| Submission | Strict items | Whole generations | Malformed | Mean total tokens | Mean output tokens |
|---|---:|---:|---:|---:|---:|
| Exact quote / text | 159/256 | 13/32 | 11/32 | 20,496.34 | 1,877.84 |
| Coordinate references / text | 121/128 | 9/16 | 0/16 | 21,181.88 | 455.88 |
| Exact quote / terminal tool | 112/128 | 10/16 | 1/16 | 20,493.13 | 1,742.13 |
| Coordinate references / terminal tool | 122/128 | 10/16 | 0/16 | 21,348.00 | 442.50 |
| Compact references / text | 117/128 | 5/16 | 0/16 | 19,549.94 | 502.94 |

References substantially reduce copied output and serialization failures, but
input overhead/cache changes can consume those gains. Removing coordinate labels
reduced total tokens further while strict quality declined. It remains an
experimental presentation, not a default. A tool call does not guarantee every
schema bound is enforced; one quote-tool submission still exceeded its quote
length limit. Requested native response-schema parameters were not evidence of
upstream enforcement.

All 768 choice-only items were correct as a diagnostic. This does **not** replace
strict acceptance, which also requires grounded citations. The frozen oracle is
narrow line overlap, not full entailment: for example, `omitted_rules` accepts only
`docs/NOW.md:144`. Of 32 follow-up strict-ungrounded items, 31 had visible verbatim
citations outside the hidden anchor. Keep their failures rather than widen the
validator or claim a semantic quality result. Protocol order, cache, timing and
framing are confounded; later cells have one repeat, original quote/text has two.

## Other shipped engineering

| Surface | Observed before | Observed after | What this establishes |
|---|---:|---:|---|
| Devloop real-checkout file reads | 1,515 | 477 | One invocation-local inventory; exact same direct-reference plan |
| Devloop real-checkout AST parses | 1,459 | 477 | Less repeated local computation, not measured model-token savings |
| Judge protocol input, offline o200k | 25,846 | 16,956 | Short transient handles; not live semantic-judge parity |
| Judge protocol input, offline Claude v5 | 35,399 | 24,927 | Same canonical host-side records |
| Judge protocol input, offline Qwen3 | 32,797 | 18,901 | Offline encoder counts, not serving-model usage |

Devloop JSON now reports lane/reasons, preserves one JSON stdout document during
child execution, streams child diagnostics to stderr, and returns its exact exit
status. Human output and direct-reference selection remain unchanged; this is not
transitive dependency closure.

Judge handles are stable `W1..Wn` over the whole frozen pool across triage, subset
inspection and final assessment. Only exact visible handles resolve; canonical
source identities/hashes stay host-side and in persisted results. The offline
measurement used 58 coordinate/error-flag projections from six real traces, with
no command/observation/message/reasoning bodies and no live model calls. It proves
binding preservation and protocol size, not semantic judgment quality.

Capture accounting now includes Anthropic base + cache-read + cache-creation
input, without double-counting nested TTL subsets or OpenAI already-inclusive
input. Missing/interrupted final output stays unknown; explicit final zero remains
zero. Cumulative SSE counters replace rather than sum. A real extractor → recorder
→ linked-Parquet smoke changed the cached Anthropic example from 1,000 to 1,800
reported input, preserved OpenAI 30/7, and changed interrupted 34/0 to unknown.
That is corrected accounting, not reduced consumption; stored history is not
silently rewritten.

The earlier work-log audit found repeated tool-routing rejections, unseen edit
ranges and assumed-home mistakes. These are exposed opportunities, not proven
saved tokens. Required verification, post-edit rereads and parent/worker file
overlap are not automatically waste. Constant global prompt-cache keys were
rejected because they couple gateway session/credential state, not merely caching.

## Evidence, privacy and reproduction

Generation census: **92 native CLI episodes** and **114 SDK generation attempts**.
The latter includes 96 main policy generations plus 18 technical-pilot/capability
generations. Dry-run and unadmitted rows remain recorded separately. These units
are not physical provider requests; upstream retries, full reasoning/cache-write
counters and invoice costs can remain unknown. Controllers used existing included
subscription routes, fresh quota admission and hard call limits; no paid compute,
new API spend approval, local generative inference or standing model changes.

Raw prompts, final submissions, traces, account/quota details and original IDs stay
in an owner-private retained study directory. The exact canonical Git archive,
42 coding source-delta bundles (including rejected data writes), runtime source
versions, raw receipts, manifests and frozen document corpus are retained. Public
JSON is an allowlisted numerical projection, not raw transcripts. Full snapshot
forests are disposable only after their source deltas and receipts are retained.

Reanalysis needs no models. With `EVIDENCE` set to the private retention root:

```sh
mkdir -m 700 "$EVIDENCE/reanalysis"
for run in native-01 native-03 native-effort-01 native-owner-01 native-effort-owner-01; do
  uv run --no-sync python -B research/explorations/agent-efficiency/analyze_native.py \
    coding "$EVIDENCE/raw/code-cases/$run/run-01" \
    --relocate-events /private/tmp/agent-efficiency "$EVIDENCE/raw" \
    --output "$EVIDENCE/reanalysis/$run.json"
done
for run in trace-pilot-run trace-main-run trace-fused-holdout-run; do
  uv run --no-sync python -B research/explorations/agent-efficiency/analyze_native.py \
    navigation "$EVIDENCE/raw/$run" --output "$EVIDENCE/reanalysis/$run.json"
done
uv run --no-sync python -B research/explorations/agent-efficiency/analyze_policy.py \
  --probe-root "$EVIDENCE/raw/policy-probe" --private-root "$EVIDENCE" \
  --output "$EVIDENCE/reanalysis/policy"
uv run --no-sync python -B research/explorations/agent-efficiency/measure_context.py \
  --root "$PWD" --corpus-root "$EVIDENCE/raw/context-corpus-v1/replay-root" \
  --budget 4000 --budget 8000 --budget 12000 --output /tmp/fresh-context-replay
```

The publisher consumes the same reports in an `analyses/` directory (use that name
instead of `reanalysis/` when constructing a fresh retention root):

```sh
uv run --no-sync python -B research/explorations/agent-efficiency/summarize_study.py \
  --evidence "$EVIDENCE" --output /tmp/fresh-agent-efficiency-results.json
```

Fresh-output checks preserve prior evidence. Private policy analysis requires an
owner-private root outside Git checkouts. Compare semantic/count fields rather
than publisher timestamps or formatter-changed analyzer-source hashes. Actual
run-time sources are separately archived; new preparation pins current code.
For fresh case preparation, `prepare_parent_effort_cases.py OWNER_MANIFEST
--registration REGISTRATION --output FRESH_MANIFEST` derives both effort arms from
the existing parent-owned candidate prompts without running models. Input and
output manifests must share the same non-Git case directory. A zero-generation
replay reproduced the admitted case/treatment metadata; invalid owner, effort and
output-boundary controls were rejected. The originally admitted generator and
registration are retained separately rather than replaced with this safer helper.

The preparation/runners default to no model execution. Do not enable
`--run-subscription` merely to reproduce these tables.

## QA trace/task efficiency follow-up (2026-10-18)

This follow-up profiles retained native usage before changing prompts. These are
historical execution costs, not a baseline/candidate quality comparison or a
provider invoice. Trace cohorts contain 916 stored item-executions (689 recorded
subject identities), not 916 distinct trajectories. Task evidence was recovered
from 15 archived `.qa-task` run trees because the live `runs/.qa-task` directory
had already been retired.

| Native stage | Retained records | Inclusive input | Cached input | Noncached input | Output |
|---|---:|---:|---:|---:|---:|
| qa-trace/6 T1 | 651 | 128,457,817 | 101,769,512 | 26,688,305 | 1,455,688 |
| qa-trace/6 T3 | 225 | 11,347,825 | 9,266,808 | 2,081,017 | 254,715 |
| qa-trace/7 T1 | 995 | 116,550,258 | 93,180,800 | 23,369,458 | 1,323,070 |
| qa-trace/7 T3 | 300 | 16,093,701 | 13,084,032 | 3,009,669 | 336,546 |
| qa-task rubric S1 | 561 (560 complete) | 38,617,722 | 8,615,936 | 30,001,786 | 1,012,652 |
| qa-task mutant adjudication | 97 | 1,337,044 | 615,424 | 721,620 | 7,155 |
| qa-task shortcut | 127 | 4,274,221 | 1,439,232 | 2,834,989 | 68,575 |

The task rubric row is a **known subtotal**: one interrupted native request has
no usage counters in its events, empty client log or empty final-message file.
It is unknown, not free. All 785 task request identities are source-bound to 134
task/digest identities and 200 source invocations. For 101 old requests whose
ledger totals/timestamps did not reconcile, identity was recovered from a unique
byte-for-byte match of every captured task file against same-run report packages;
no nearest-time or filename-order attribution was invented. Trace stage totals
reconcile exactly with retained verdict totals, including retries.

Native Codex `input_tokens` includes its cached subset; OMP `input` excludes
`cacheRead`/`cacheWrite`. The profile normalizes both to inclusive input,
cache-read input, noncached input and output. Cache writes are noncached input.
Reasoning output is not added a second time. Missing components remain unknown.
The full source-bound profiles, request IDs and source hashes remain private:
`~/.local/share/eval-lab/studies/qa-token-efficiency-20261018T152101Z/`,
`trace-usage-profile-v1.json` (SHA-256
`bd5c093af13c2190b71dfab64405f265f6464feef0c238b54bdb8c61ab17505c`) and
`task-usage-profile-v2.json` (SHA-256
`fbd757aaaf841af7ed1f622fa67efc9adf135b0c9ccf82891308f24a582794b0`).

The engineering changes are bounded blind T1 evidence plus a question-independent
system prefix before the dynamic cwd footer, and one strict identity-bound
adjudication batch for the same first six surviving mutants. Invalid batches
become unknown without per-mutant fallback calls or fabricated token allocation.
The bounded-prefix pipeline uses its own output namespace, leaving historical
qa-trace/7 evidence intact. See the [trace protocol](../../experiments/qa-trace/README.md)
and [task batch contract](../../../docs/qa-task.md#mutant-adjudication-batches).

The profile also sets priorities: T1 owns most trace noncached-input-plus-output
spend, but rubric S1, not mutant adjudication, owns most task spend. Batching the
small adjudication stage alone cannot deliver a 30% whole-task reduction.
The separate native S1 cache experiment below addresses this measured bottleneck;
it does not qualify the bounded QA Trace prefix.

### Historical trace pilot limits (2026-10-18)

A 15-item dev slice and per-item arm order were frozen before model outcomes.
The original Codex comparison stopped at admission, before any study call, when
shared weekly usage crossed the preregistered conservative lane threshold.
An additional Google-subscription comparison was separately registered before
outcomes, using the same slice, acceptance and source-pinned arms. It ran nine
native subscription turns before fresh Gemini weekly usage reached 59.81094%,
past its tighter 59.8% admission stop. Only one paired item was attempted; two
baseline turns at the 180-second study transport limit have no retained usage.
Seven turns have complete counters, but their subtotal is not a full paired cost.
The protocol never admits missing counters as zero or promotes a partial slice.

Consequently, the >90% aggregate T1 cache, at least 30% noncached-input-plus-output
reduction and zero quality-regression targets are **not established**. This is
neither Codex non-regression nor token-to-quota equivalence. The frozen comparison
and source candidate remain unqualified, not a successful performance release.
Cold calls, retries and T3 remain in the denominator. The sealed failure receipt
is `google-study-result-v1.json`, SHA-256
`a81fdb8dc08da8c78f1d78305dea22c3667f0dc736a2b4c49fcf9f5bcf346837`.

The native smoke also identified concrete next investigation points: `_omp_argv`
does not disable default rule/skill/extension discovery, despite supplying a
standalone QA system prompt, and `call_model` discards partial stdout/usage on a
transport exception. The installed harness exposes per-call isolation flags.
Context isolation and failure-accounting retention need their own measured,
safety-preserving treatment; this patch does not silently disable standing
instructions, add retries or claim those causes account for a measured fraction.

### PS-1: independent S1 prefill reuse (2026-10-19)

The preregistered six-turn probe used one archived, byte-identical 77,355-character
rubric packet and the fixed interleaving control, candidate, candidate, control,
control, candidate. Control used current-main fresh ephemeral exec/cwd requests;
candidate used the actual `CodexLane` with supported ephemeral forks of one
genuine, model-free empty seed. All six native thread IDs are distinct, all six
prompt hashes match, and all source usage counters are complete.

| Arm | Calls | Complete rubrics | Inclusive input | Cached input | Noncached input | Output | Noncached input + output |
|---|---:|---:|---:|---:|---:|---:|---:|
| Fresh exec/cwd control | 3 | 3 | 91,618 | 4,096 | 87,522 | 4,931 | 92,453 |
| Independent empty-seed forks | 3 | 2 | 86,937 | 57,600 | 29,337 | 5,860 | 35,197 |

The primary metric fell **61.93% including the cold first candidate vote**.
That first vote had no cached input; each subsequent candidate vote cached
28,800 of 28,979 input tokens (99.38%). Output increased, rather than being
excluded from the denominator. Both cwd and native cache-routing behavior
changed, and provider-framed cold inputs also differ: this is a combined-path
measurement, not the isolated causal effect of cwd or a text-equivalent full
provider request.

No prior prompt or response is inherited. Native seed creation submits no
model turn; it retains exactly one model-provenance metadata record. Child
thread identities, seed bytes and empty cwd checks reject history contamination
or lifecycle reuse. See the [production contract](../../../docs/qa-task.md#independent-rubric-cache-reuse).

Offline replay through the actual production JSON parser and completeness rules
found one malformed candidate response. It was not repaired or rerun, and all
its tokens remain counted. Applying the existing complete-vote/majority rules
also changes some criterion results. Thus even this one packet does **not**
establish unchanged usable-vote performance; a complete native turn is not a
complete rubric answer. The saved-output diagnostic is not ground-truth scoring.

This establishes a one-packet cost result, not rubric correctness, statistical
noninferiority, savings across tasks/models or an exact quota percentage.
The separate [frozen QA Trace comparison](../../experiments/qa-trace/README.md#ps-1-native-frozen-slice-attempt-2026-10-19)
did not meet acceptance; the PR remains draft and must not merge on this S1
number alone. New paid API/compute spend was $0.

Reviewable counters and limits are in [ps1-20261019-result.json](ps1-20261019-result.json).
Preregistration, source archives, all raw native streams, quota admissions and
immutable outcome files remain under
`~/.local/share/eval-lab/studies/ps1-20261019/`.

