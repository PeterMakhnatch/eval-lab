# ovn-sft-v0: pre-registration

Registered by HAR-133 (Cdx 3), 2026-10-01, **before G2**. The merge commit
and GitHub merge time, not this sentence, establish the registration time.
Authority: Peter's 03:55Z direction and the October 1 overnight plan at
`research-context/inbox/sft-overnight-20260918/OVERNIGHT-2026-10-01.md`;
execution cards HAR-126–129. This is one small SFT experiment, not RL or a
completion-fix experiment. The total overnight run allowance is $30 from
04:00Z; HAR-133 itself spends $0.

After merge, preserve this document unchanged. Put any deviation, its time,
reason, authorizing card comment and data already seen in `RESULTS.md`;
do not silently amend the hypotheses, cohort or analysis after observing data.
The gates below bind artifacts that their owners have not yet produced.
An absent digest or failed audit is a closed gate, not a discretionary default.

## 1. Question and hypothesis

Does one LoRA SFT update on the distill's own verified, clean solutions to
training-split Python tasks change its counted pass rate on an untouched
20-task Python evaluation cohort, under an otherwise identical harness?

- **Stock:** `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`, pinned base revision.
- **Tuned:** that same base plus the single frozen LoRA adapter from G4.
- **Directional scientific expectation:** tuned has a higher counted pass rate.
- **Statistical null:** a task is equally likely to be a tuned-only pass as a
  stock-only pass. The alternative and the sole confirmatory test are
  **two-sided**; a negative effect is reportable, not a discarded experiment.

An operationally correct loop is useful even with no improvement. This tests
one adapter, training seed and sampled rollout per arm, not a general claim
about SFT recipes, RL, coding benchmarks or the model's pretraining exposure.

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

Run one intended attempt per arm per frozen task: **40 intended trials**.
Both model names use the **same LoRA-capable server in one session**; the base
name must actually disable the adapter. Hold constant the engine/version,
base weights, dtype/quantization, tokenizer/template, reasoning/tool parsers,
server arguments (including context length), sampling parameters, request
limits and cache policy. Do not migrate serving engines merely to satisfy a
name in a planning document; identify the actual implementation in the G4
parity record. Shared LoRA overhead is not an arm-specific treatment.

Use the same lf2 harness digest, proxy/runtime revision, task/environment/
verifier digests, sandbox policy and per-task timeout in both arms. HAR-126
specifies **120 calls and 2.5 million input tokens**. The inherited HAR-116
cap is **2,000 characters of terminal output per step**, not model-output
tokens; its model request limit is separately `max_tokens=4096`. Bind the
exact effective fields in the lf2 launch manifest. Pin one concurrency setting
and one scheduling policy for both arms before any G5 outcome. Record all
seeds supported by the serving path, with identical per-task settings across
arms; disclose absent or unenforced seeds.

Use the committed CSV's row order (excluding header), not completion order:
odd-numbered tasks run **stock then tuned**; even-numbered tasks run **tuned
then stock**. Start the second arm of a task after its first arm finishes;
task pairs may run concurrently under the fixed policy. Preserve actual
start/end timestamps, not merely the intended order. This counterbalances
order but is not randomization and does not eliminate shared-server effects.

No completion-fix arm, prompt search, changes prompted by held-out outcomes,
optional stopping on a p-value or additional attempts to rescue model failures.
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
and per-trial evidence. Apply the same deterministic rules to both arms;
freeze the adjudicated table before calculating the contrast. Evidence-backed
corrections must be logged and reprocessed symmetrically, not hand-overridden.

If either arm is missing/excluded, remove the **whole task pair** from the
primary test. Let `N` be the number of complete counted pairs (`0 <= N <= 20`).
Both primary arm rates have this **same denominator N**, not two separate
arm-specific denominators. With `N=0`, rates and inference are unavailable.
Report `N/20`, every omitted task and both arm verdicts prominently. If `N<20`,
call the result a complete-pair estimate, **not a measured full-20 pass rate**.
No replacement tasks and no inferential claim that omissions are random.

Only a trial excluded **solely for `infra`** may be replaced, after the
coordinator approves that specific use of the reserve on its card. Never
retry a counted failure or a copied/tainted pass. Preserve original and retry
IDs, timing, reasons and costs; take the first subsequent countable attempt,
not the most successful one. No retry changes the task, adapter, harness,
limits or sampling. Count all failed attempts toward spend. An unapproved or
unaffordable replacement remains missing; it does not relax a gate.

Report raw passes, counted passes/failures, exclusions and coverage for each
arm over all 20 planned tasks, separately from the paired comparison. Also
report a missing-outcome sensitivity bound: retain each known counted outcome
and let every missing arm outcome range over 0/1; give the smallest/largest
possible `(tuned passes - stock passes)/20`. These are hypothetical bounds,
not imputed model failures or extra confirmatory tests. Copied/tainted-pass
exclusion can be treatment-related: a significant complete-pair result alone
cannot establish an unconditional improvement across the full cohort.

## 5. Sole confirmatory test and effect estimate

For the complete pairs, report the full 2-by-2 table:

| | Stock pass | Stock fail |
|---|---:|---:|
| Tuned pass | `a` | `b` (gains) |
| Tuned fail | `c` (losses) | `d` |

`N = a+b+c+d`; tuned rate = `(a+b)/N`; stock rate = `(a+c)/N`;
paired difference **tuned minus stock** = `(b-c)/N`, in percentage points.
The unit is the task, not a turn, token, tool call or repeated training trace.

Use **exact McNemar on discordant pairs, two-sided, alpha = 0.05**:
`m=b+c`; if `m=0`, `p=1`; otherwise
`p = min(1, 2 * sum(comb(m,k) for k in range(min(b,c)+1)) / 2**m)`.
Reject at `p <= 0.05`. No asymptotic chi-square, continuity-corrected, mid-p,
one-sided or unpaired replacement. Report the exact p-value even if not
significant; non-rejection is not equivalence or evidence of no effect.

Reuse `evallab.analysis_statistics.exact_paired_binary_contrast`, with
`arm_a_outcome=tuned`, `arm_b_outcome=stock`, and one `PairedBinaryInput` per
complete task. Its positive `risk_difference` then means tuned improvement.
Report its 95% paired score interval as an **approximate descriptive interval**,
not an exact inversion of McNemar; the exact p-value decides the test.
Publish the analysis input table and command so the numbers are reconstructable.

This calculation treats task pairs as independent. Report tasks per repository/
project; shared repositories, near-duplicate residuals and shared-server
failures can undermine that assumption. The light-image nonrandom cohort
supports a narrow held-out demonstration, not population-wide generalization.

## 6. Secondary metrics: descriptive, not additional success gates

On the same complete pairs, give per-arm totals and median/IQR plus paired
per-task differences for **input tokens, output tokens and model-call count**.
Separate observed usage from reserved/estimated tokens; missing usage stays
missing, with its own coverage denominator. Include all-attempt resource cost
and latency separately so infrastructure replacements are not free.

Report **loop incidence/kind and stop-reason counts** by arm, with denominators
and the fixed lf2/HAR-128 RUBRIC v2.1 definitions. G6 blind-to-arm trace review
adds completion claims, format errors and time to first real edit; runs with
no edit are reported separately rather than assigned zero time. Link evidence
for each discordant task. These are descriptive/exploratory, with no multiple
metric fishing, significance-based success labels or claims that a shorter
failed run is necessarily better. Do not change counts from these judgments.

## 7. Power and interpretation at n = 20

With all 20 pairs observed, one net gained task is **5 percentage points**.
Even five gains and no losses give `p=0.0625`; six gains and no losses give
`p=0.03125`. Seven gains/one loss give `p=0.0703125`; eight/one give
`p=0.0390625`. Fewer than six discordant pairs cannot reject at this alpha.
The theoretical floor with all 20 discordant in one direction is `2**-19`,
but attaining that floor is not a plausible precision guarantee.

Power depends on discordance, not just the two marginal pass rates. Exact
multinomial enumeration for 20 independent pairs gives these **illustrative
assumptions**, not measured or promised power:

| Probability tuned-only | Probability stock-only | True difference | Power |
|---:|---:|---:|---:|
| 0.20 | 0.00 | +20 pp | 19.58% |
| 0.30 | 0.10 | +20 pp | 17.37% |
| 0.40 | 0.10 | +30 pp | 36.71% |
| 0.50 | 0.10 | +40 pp | 57.25% |

Thus this is an operational and effect-size pilot, poorly powered for modest
gains. It cannot establish equivalence, robust superiority, run-to-run training
stability or out-of-domain transfer. Missing pairs reduce information further.

## 8. Required RESULTS and final audit

`RESULTS.md` must include: (1) chronology and all frozen digests; (2) training
config, sample counts and adapter identity; (3) actual serving/harness/limit/
concurrency/order parity; (4) all 20 tasks with trial IDs, raw rewards, counts,
exclusion evidence and approved retry lineage; (5) paired table, N/20, both
rates, difference/interval, exact p and missing-outcome bounds; (6) descriptive
secondary metrics and trace links; (7) total reconciled spend including failed
attempts/startup/idle time and any remaining estimation limits; (8) deviations
and limitations. Label any other analysis **exploratory**.

HAR-133's one-page final verdict is **valid**, **valid with caveats**, or
**invalid**, with primary evidence and unresolved gates. Contamination,
unreviewed training samples, outcome-driven cohort/adapter selection or
arm-specific harness/limit changes invalidate the claimed controlled contrast.
Missingness, nonrandom selection, stochastic runs and small n constrain an
otherwise faithful comparison; they are not concealed by a green software CI.
A blocked or unexecuted G5 yields **no observed stock-versus-tuned result**.
