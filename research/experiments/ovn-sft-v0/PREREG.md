# ovn-sft-v0: pre-registration

Registered by HAR-133 (Cdx 3), 2026-10-01, **before G2**. The merge commit
and GitHub merge time, not this sentence, establish the registration time.
Authority: Peter's 03:55Z direction and the October 1 overnight plan at
`research-context/inbox/sft-overnight-20260918/OVERNIGHT-2026-10-01.md`;
execution cards HAR-126–129 and HAR-135. **Scope update:** Research-Harbor's
2026-10-01 04:21Z comments on HAR-133/HAR-126 (described as about 04:30Z)
add GEPA as the third arm, superseding the original two-arm draft **before
its merge and before G2**. This is SFT and prompt optimization, not RL or a
completion-fix experiment. The overnight allowance remains $30 from 04:00Z:
G2 $9, GEPA gate $2, G5 $10, Infra $6, Traces $0.50, Data $1, reserve $1.50.
HAR-133 itself spends $0; budget pressure prioritizes SFT over the GEPA arm.

After merge, preserve this document unchanged. Put any deviation, its time,
reason, authorizing card comment and data already seen in `RESULTS.md`;
do not silently amend the hypotheses, cohort or analysis after observing data.
The gates below bind artifacts that their owners have not yet produced.
An absent digest or failed audit is a closed gate, not a discretionary default.

## 1. Question and hypotheses

Do either (a) one LoRA SFT update on verified, clean solutions or (b) a
GEPA-selected prompt addendum improve the distill's counted pass rate on an
untouched 20-task Python cohort, under otherwise controlled conditions?

- **Stock:** `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`, pinned base revision,
  baseline lf2 prompt and no adapter.
- **Tuned:** that same base and prompt plus the single frozen G4 LoRA adapter.
- **GEPA:** stock weights, **stock model name**, baseline lf2 prompt plus the
  single HAR-135 candidate addendum, frozen by SHA-256; no adapter.
- **Two primary contrasts:** tuned minus stock and GEPA minus stock.
  Directional expectations are improvements, but each statistical null is
  equal probabilities of intervention-only and stock-only passes, tested
  **two-sided**. Control family-wise error at 0.05 by Holm (section 5).
- **Tuned minus GEPA is exploratory**, not a third primary test.

HAR-135 may search only training tasks: reuse G2 as seed evaluations, gate at
most two candidates on its 10-task training minibatch, and choose by counted
passes with tokens as tie-breaker. Bind the chosen addendum's exact bytes,
placement in the prompt, digest, selection evidence and freeze time before
G5. No eval prompts, traces, outcomes or derived feedback may enter search.

At the moment G5 is ready to launch, **before the first G5 trial/outcome**,
freeze whether HAR-135 has posted a selected, gated candidate. If not
(including no candidate beating the seed), run stock and tuned only: one
primary test at 0.05. Record that decision and time. A later candidate is not
added mid-evaluation. If a candidate was admitted, the primary family stays
size two even if GEPA later fails, is excluded or cannot finish within budget.
Never reduce multiplicity after seeing results or retrospectively choose an
arm subset. Use the same one stock trial per task for both primary contrasts.

An operationally correct loop is useful even with no improvement. This tests
one adapter/training seed and at most one selected prompt, with one sampled
rollout per arm: not a general claim about SFT/GEPA recipes, RL, pretraining
exposure or out-of-domain transfer.

## 2. Frozen cohort, training separation and chronology

G1/HAR-127 commits `eval_tasks.csv`: exactly 20 ledger-`usable` held-out tasks,
with task identity, digest to run, repository and image size. Follow the G1
light-image/repository-stratified selection rule. Exclude HAR-116's 10 tasks.
Variants inherit their parent's split, repository and project identity.

Before G2, record the eval file SHA-256, its commit and commit/merge times,
the PREREG merge and the G0 lf2 harness digest on HAR-126/HAR-127/HAR-133.
The eval list is committed before **tonight's** SFT sample selection, including
any dry-run sample selection. Previously existing candidate pools are not new
selection events; disclose their dates and any prior eval exposure. No task
replacement, cohort expansion or outcome-driven selection is allowed.

G1 audit compares eval against **all training-split tasks**, the HAR-120
proposal and the full HAR-104/110/116/120 candidate universe, not only eventual
passes. Require no shared canonical repository or `project_key`; resolve
missing identities rather than treating blanks as proof of disjointness.
Record instruction-normalization/shingling method, similarity threshold,
all flagged cross-split pairs and their dispositions before training. A known
solution-bearing near-duplicate is contamination even with different IDs.

G3 admits only training-split, usable `counted_pass` trials, at most two per
task, each explicitly marked clean with a cut step by HAR-128. Exclude copied
fixes, hidden-test access/tampering and unclean traces. Export from the actual
proxy requests/responses, respecting context/summarization boundaries and
reviewed trims; supervise assistant tokens only. Freeze the sample manifest,
export and clean-label hashes before training. HAR-133 reconstructs five
samples from source capture (all samples if fewer than five). A JSON message
round-trip alone is not proof of rendered prompt/token or loss-mask fidelity.

G4 fixes the trainer/configuration, tokenizer/chat template, base revision,
training seed and checkpoint-selection rule **before final training**, without
consulting eval results. Use the final checkpoint of that one planned fit;
no best-of-eval checkpoint, hyperparameter sweep or post-eval retraining.
Bind the adapter SHA-256 before G5. Dry-run adapters are not candidate arms.

## 3. Paired execution and controlled differences

Run one intended attempt per admitted arm per frozen task: **60 intended
trials with GEPA, otherwise 40**. All arms use the **same LoRA-capable server
in one session**. Stock and GEPA use the exact same base model name with the
adapter disabled; tuned differs only by the adapter selector. Hold constant
engine/version, base weights, dtype/quantization, tokenizer/template,
reasoning/tool parsers, server arguments (including context length),
sampling, request limits and cache policy. Do not migrate serving engines
merely to satisfy a name in a planning document; identify the actual engine
in the G4 parity record. Shared LoRA overhead is not an arm-specific treatment.

Stock/tuned use the identical lf2 harness digest. GEPA uses the same lf2
implementation/configuration plus **only the frozen prompt addendum**.
Bind the lf2 and addendum digests separately; if an aggregate harness-tree
digest includes the addendum, preserve it too and show the exact diff is
only that permitted prompt change. No hidden GEPA parser/loop/limit changes.
All arms share the proxy/runtime revision, task/environment/verifier digests,
sandbox policy and per-task timeout.

HAR-126 specifies **120 calls and 2.5 million input tokens**. The inherited
HAR-116 cap is **2,000 characters of terminal output per step**, not model
output tokens; its model request limit is separately `max_tokens=4096`.
Bind exact effective fields in the lf2 launch manifest. Pin one concurrency
setting and scheduling policy for all arms before any G5 outcome. Record
supported seeds with identical per-task settings across arms; disclose absent
or unenforced seeds. Additional GEPA prompt tokens are part of its treatment,
not permission for a larger context or token budget.

Use the committed CSV's row order, excluding header. For two arms, odd rows
run **stock then tuned**, even rows **tuned then stock**. For three arms,
repeat this fixed six-row cycle through all 20 tasks:

| Row modulo six (1-based) | Order |
|---:|---|
| 1 | stock → tuned → GEPA |
| 2 | tuned → GEPA → stock |
| 3 | GEPA → stock → tuned |
| 4 | GEPA → tuned → stock |
| 5 | stock → GEPA → tuned |
| 0 | tuned → stock → GEPA |

This retains alternating stock/tuned order and balances every arm across
positions (6 or 7 times per position over 20 tasks). Start each next arm of
a task only after its predecessor finishes; task groups may run concurrently
under the fixed policy. Preserve actual start/end times and concurrency
over time, per-call latency and queue wait; record GPU utilization only if
available, never infer a measurement. Counterbalancing is not randomization
and does not eliminate shared-server effects.

No completion-fix arm, eval-guided prompt search, changes prompted by held-out
outcomes, optional stopping on a p-value or attempts to rescue model failures.
Stop on safety/budget/invalidity gates, retaining every attempted trial. Only
infrastructure replacement under section 4 can add an attempt.

## 4. Outcome, exclusions and denominator

The **primary metric is counted pass rate, paired by task on the frozen
20-task set**. Use `process-job`'s existing `evallab.counts/v1` verdict from
`src/evallab/counts.py`; never replace it with raw reward or a model judgment.

| Final verdict | Binary analysis value | Handling |
|---|---|---|
| `counted_pass` | 1 | Verifier pass with no decisive exclusion |
| `counted_fail` | 0 | Valid scored failure, including scored limit/loop stops |
| `excluded` | missing | Preserve all reasons: `copied_fix`, `pass_tainted`, `task_not_usable`, `infra` |
| Missing verdict/capture | missing | Investigate; not a fabricated zero |

A timeout, parse error, loop or exception **does not itself exclude** a valid
scored result. Counts' deterministic evidence decides; first-failure/blame/
loop judgments are display-only. A fetch on a failed trial remains a counted
failure if no other exclusion applies. Preserve the raw verifier reward.
Record the counts implementation revision, census/ledger/hand-label versions
and per-trial evidence. Apply the same deterministic rules to every arm;
freeze the adjudicated table before calculating contrasts. Evidence-backed
corrections must be logged and reprocessed symmetrically, not hand-overridden.

For **each contrast separately**, if either of its arms is missing/excluded,
remove the whole task pair from that test. Let `N` be its number of complete
counted pairs (`0 <= N <= 20`); its two rates share that denominator. Report
`N_tuned-stock` and `N_GEPA-stock` separately: missing GEPA must not discard a
complete tuned/stock pair. Stock rates can differ between contrast tables
because their eligible task sets differ; do not mix those denominators.
With `N=0`, rates/inference are unavailable. Report each `N/20`, omitted
task and arm verdict prominently. With `N<20`, call it a complete-pair
estimate, **not a measured full-20 pass rate**. No replacement tasks or claim
that omissions are random. A common-complete-three-arm summary is exploratory.

Only a trial excluded **solely for `infra`** may be replaced, after the
coordinator approves that specific use of the reserve on its card. Never
retry a counted failure or a copied/tainted pass. Preserve original and retry
IDs, timing, reasons and costs; take the first subsequent countable attempt,
not the most successful one. No retry changes the task, adapter, addendum,
harness, limits or sampling. Count every attempt toward spend. An unapproved
or unaffordable replacement remains missing; it does not relax a gate.

Report raw passes, counted passes/failures, exclusions and coverage for each
arm over all 20 planned tasks, separately from paired comparisons. For each
primary contrast, report a missing-outcome sensitivity bound: retain known
counted outcomes and let missing arm outcomes range over 0/1; give the
smallest/largest possible `(intervention passes - stock passes)/20`.
These are hypothetical bounds, not imputed failures or extra confirmatory
tests. Copied/tainted-pass exclusion can be treatment-related: significant
complete-pair results alone cannot establish unconditional improvement across
the full cohort.

## 5. Primary tests, Holm correction and effect estimates

For each primary contrast, let `X` mean tuned or GEPA and report:

| | Stock pass | Stock fail |
|---|---:|---:|
| X pass | `a` | `b` (gains) |
| X fail | `c` (losses) | `d` |

`N=a+b+c+d`; X rate = `(a+b)/N`; stock rate = `(a+c)/N`;
paired difference **X minus stock** = `(b-c)/N`, in percentage points.
The unit is the task, not a turn, token, call or repeated training trace.

Each raw p-value is **exact McNemar on discordant pairs, two-sided**:
`m=b+c`; if `m=0`, `p=1`; otherwise
`p=min(1, 2*sum(comb(m,k) for k in range(min(b,c)+1))/2**m)`.
No asymptotic chi-square, continuity-corrected, mid-p, one-sided or unpaired
replacement. Report raw and adjusted p-values even if not significant;
non-rejection is not equivalence or evidence of no effect.

**If GEPA was admitted, apply Holm across the two primary tests.** Sort raw
p-values `p_(1) <= p_(2)` (tie order is immaterial). Reject the first only if
`p_(1) <= 0.025`; only after that rejection, reject the second if
`p_(2) <= 0.05`. Equivalently, report adjusted values
`p_adj_(1)=min(1,2*p_(1))` and
`p_adj_(2)=min(1,max(2*p_(1),p_(2)))`, mapped back to named contrasts,
and compare with 0.05. Shared stock trials make tests dependent; Holm does
not require independence between tests. If one contrast is not estimable,
label it unavailable, never rejected; keep the family of two and use
`min(1,2*p)` for the other contrast. Do not fabricate a raw p-value for the
missing test or revert to a single-test threshold.

**If no GEPA candidate was admitted before G5**, the sole primary contrast
is tuned versus stock, rejected at its raw exact `p <= 0.05`; adjusted p
equals raw p. There is no correction for a test that was never in the frozen
family. Tuned versus GEPA, when available, gets its own complete-pair table
and is explicitly exploratory, not promoted by its p-value.

Reuse `evallab.analysis_statistics.exact_paired_binary_contrast`, with
`arm_a_outcome=X`, `arm_b_outcome=stock`, one `PairedBinaryInput` per complete
task. Positive `risk_difference` means improvement. Report its 95% paired
score interval as an **approximate, marginal descriptive interval**: neither
an exact inversion of McNemar nor a simultaneous family-wise interval.
The Holm-adjusted test decides the primary rejection. Publish the input
table and analysis command so all numbers are reconstructable.

Each McNemar calculation treats task pairs as independent. Report tasks per
repository/project; shared repositories, near-duplicate residuals and shared
server failures can undermine that assumption. The light-image nonrandom
cohort supports a narrow held-out demonstration, not population generalization.

## 6. Secondary metrics: descriptive, not additional success gates

For each primary contrast's complete pairs, give per-arm totals and median/
IQR plus paired per-task differences for **input tokens, output tokens and
model-call count**. Separate observed usage from reserved/estimated tokens;
missing usage stays missing, with its own coverage denominator. Include
all-attempt cost and latency separately so infrastructure replacements are
not free; link G2/G5 scheduling/latency telemetry for the throughput analysis.

Report **loop incidence/kind and stop-reason counts** by arm, with denominators
and the fixed lf2/HAR-128 RUBRIC v2.1 definitions. G6 blind-to-arm trace review
adds completion claims, format errors and time to first real edit; runs with
no edit are reported separately rather than assigned zero time. Link evidence
for each discordant task. These are descriptive/exploratory, with no multiple
metric fishing, significance-based success labels or claims that a shorter
failed run is necessarily better. Do not change counts from these judgments.

## 7. Power and interpretation at n = 20

With 20 complete pairs, one net gained task is **5 percentage points**.
Five gains/no losses give `p=0.0625`; six/no losses give `p=0.03125`.
The latter can pass a single test at 0.05, but **cannot clear Holm's first
0.025 threshold**. Seven/no losses give `p=0.015625`. Eight gains/one loss
give `p=0.0390625`; nine/one give `p=0.021484375`. Fewer than six discordant
pairs cannot reject even at 0.05; fewer than seven cannot clear 0.025.
The all-20-one-direction floor is `2**-19`, not a precision guarantee.

Power depends on discordance and, for Holm, the other contrast. Exact
multinomial enumeration for 20 independent pairs gives these **illustrative
single-contrast powers at fixed thresholds**, not measured power or a joint
three-arm power claim:

| Pr(X-only pass) | Pr(stock-only pass) | True difference | Power at 0.025 | Power at 0.05 |
|---:|---:|---:|---:|---:|
| 0.20 | 0.00 | +20 pp | 8.67% | 19.58% |
| 0.30 | 0.10 | +20 pp | 10.70% | 17.37% |
| 0.40 | 0.10 | +30 pp | 27.10% | 36.71% |
| 0.50 | 0.10 | +40 pp | 47.26% | 57.25% |

A contrast's Holm rejection probability lies between its fixed-0.025 and
fixed-0.05 powers; the joint task-outcome distribution determines where.
This is an operational/effect-size pilot, poorly powered for modest gains.
It cannot establish equivalence, robust superiority, training/prompt-search
stability or out-of-domain transfer. Missing pairs reduce information.

## 8. Required RESULTS and final audit

`RESULTS.md` must include: (1) chronology, all frozen digests and the
pre-G5 arm-admission/family-size decision; (2) training config, sample counts,
adapter identity and GEPA selection/addendum identity; (3) actual serving,
harness, permitted prompt difference, limits, concurrency and order parity;
(4) all 20 tasks with every arm's trial IDs, raw rewards, counts, exclusion
evidence and approved retry lineage; (5) each primary paired table, N/20,
rates, difference/marginal interval, raw exact p, Holm-adjusted p, rejection
decision and missing-outcome bounds; (6) descriptive secondary metrics and
trace links; (7) reconciled spend including failures/startup/idle time and
estimation limits; (8) deviations/limitations. Tuned-versus-GEPA and any
other non-primary analysis must be labeled **exploratory**.

HAR-133's one-page final verdict is **valid**, **valid with caveats**, or
**invalid**, with primary evidence and unresolved gates. Contamination,
unreviewed SFT samples, eval-driven cohort/adapter/addendum selection,
post-outcome multiplicity changes or unpermitted arm-specific harness/limit
changes invalidate the claimed contrast. Missingness, nonrandom selection,
stochastic runs and small n constrain an otherwise faithful comparison;
green software CI cannot conceal them. An unexecuted G5 yields **no observed
improvement-method comparison**, not an inferred gain.

