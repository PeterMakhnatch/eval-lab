# Statistics in tidegauge

This document derives the error bars, tests and plans that tidegauge reports. The code is in
`src/tidegauge/stats.py`. `tests/test_stats.py` checks each formula against an independent
calculation: a closed form, exact fractions, numerical integration, or a count of every possible
case. `tests/test_calibration.py` checks the claims by simulation. Section 11 gives the measured
numbers, and section 12 lists the defects that a review found and how they were fixed. Section 13
derives the 2 effects that `decompose` reports for a team of agents.

## 1. The data model and the 2 questions

A benchmark has T tasks. An arm is 1 configuration: agent, model and settings. The arm runs task t
k_t times. Each run is a trial, and each trial gives a reward x. A reward is usually 0 (fail) or
1 (pass). `plan` and `Score.attempts_min` call k_t the number of attempts.

Notation for task t:

```
x_t1 ... x_tk   the rewards of the k_t trials
m_t             = (x_t1 + ... + x_tk) / k_t, the task mean (the pass rate on the task)
s_t^2           = sum_i (x_ti - m_t)^2 / (k_t - 1), the sample variance of the trials
```

For 0/1 rewards, `s_t^2 = m_t (1 - m_t) k_t / (k_t - 1)`.

The model: task t has a true pass rate p_t. Trials are independent. A rerun runs the same tasks
again, and each trial passes with probability p_t again.

tidegauge answers 2 different questions. Each question has its own error bar.

- **rerun.** The task set is fixed. Would the number change if the same arm ran the same tasks
  again? Only the noise from trial to trial inside each task matters. The target is the mean of
  p_t over these T tasks.
- **tasks.** The tasks are a sample from a larger set of similar work. Would the number hold on
  new tasks of the same kind? The spread between tasks also counts. The target is the mean pass
  rate over the larger set.

Use rerun for "did my change do anything on this benchmark?". Use tasks for "is this agent better
on work like this?". Do not mix them: a rerun interval says nothing about new tasks.

## 2. The score and its 2 standard errors

The score is the task-macro mean. Every task has weight 1, whatever its number of trials.

```
score = (1/T) * sum_t m_t
```

`Score.micro` is the plain mean over all trials. It differs from the score only when tasks have
different numbers of trials. A task with more trials does not count more in the score.

### 2.1 Rerun standard error

On a rerun, m_t varies around p_t with variance `sigma_t^2 / k_t`. For 0/1 rewards,
`sigma_t^2 = p_t (1 - p_t)`. The tasks are independent, so the variances add:

```
Var(score) = (1/T^2) * sum_t sigma_t^2 / k_t
se_rerun   = sqrt( sum_t w_t ) / T,    with w_t = s_t^2 / k_t
```

s_t^2 is the unbiased estimate of sigma_t^2. For 0/1 rewards, `w_t = m_t (1 - m_t) / (k_t - 1)`.
So `se_rerun` is the Terminal-Bench leaderboard formula:

```
se_rerun^2 = (1/T^2) * sum_t m_t (1 - m_t) / (k_t - 1)
```

A task with 1 trial has no variance estimate of its own. It borrows the mean of s_t^2 over the
tasks with 2 or more trials. `Score.imputed_tasks` counts the tasks that borrow.

### 2.2 Degrees of freedom of the rerun error bar

`se_rerun` is itself an estimate, and with few trials it is a noisy one. Each term w_t has
`d_t = k_t - 1` degrees of freedom. The Satterthwaite formula gives the degrees of freedom of
their sum:

```
df_rerun = (sum_t w_t)^2 / sum_t (w_t^2 / d_t)
```

Terms with w_t = 0 add nothing to either sum. 2 examples show how it works:

- 3 tasks with equal terms and 1 degree of freedom each give df_rerun = 3.
- If 1 task holds almost all the noise, df_rerun is close to that task's d_t.

A task with 1 trial borrows the mean of s_j^2 over the tasks with 2 or more trials, the donors.
So all borrowed terms rest on the same donor estimates, and they are not independent. The formula
needs independent pieces. So each donor j carries its own term plus its share of the borrowed ones:

```
piece_j = s_j^2 * (1/k_j + lent),   with k_j - 1 degrees of freedom
lent    = (sum over the borrowing tasks b of 1/k_b) / (number of donors)
```

The pieces add up to the same total as the terms w_t. df_rerun is the formula above, applied to
the pieces. Without borrowing tasks, lent = 0 and the pieces are the terms. Example: 2 donors [1, 0] and [0, 1] lend to 20 tasks with 1 trial. Then df_rerun = 2,
because all 22 terms rest on 2 estimates with 1 degree of freedom each.

The 95% rerun interval uses Student's t distribution with these degrees of freedom:

```
score ± t_(0.975, df_rerun) * se_rerun
```

**None means "no noise is measured".** If no task has 2 or more trials, or every w_t is 0,
`se_rerun`, `df_rerun` and the interval are None. An interval of width 0 would claim a certainty
that the data does not have. For example, 3 tasks with 2 equal trials each show no noise. Their
true pass rates can still be anywhere between 0 and 1. Equal values have a variance of exactly 0,
also when their rounded mean is not exactly their value (D11).

### 2.3 Tasks standard error

If the tasks are a random sample, the task means m_t are independent draws. Their spread holds
both the true differences between tasks and the trial noise. So the usual standard error of a
mean covers both parts:

```
se_tasks = sqrt( sum_t (m_t - score)^2 / (T - 1) / T )
```

The 95% tasks interval is `score ± t_(0.975, T - 1) * se_tasks`. For T = 89 tasks, t = 1.987.

### 2.4 Why the 2 error bars differ

Let sigma_between^2 be the variance of p_t across tasks. Then, on average,

```
se_tasks^2 = ( sigma_between^2 + mean_t sigma_t^2 / k_t ) / T
se_rerun^2 =                    mean_t sigma_t^2 / k_t   / T
```

A benchmark with tasks from 0% to 100% has a large sigma_between^2. More trials shrink only the
second part. So more trials push `se_rerun` toward 0, but `se_tasks` stops at
`sqrt(sigma_between^2 / T)`. Only more tasks shrink it further.

### 2.5 Noise floor

2 identical runs differ by a random amount. Their difference has standard error
`sqrt(2) * se_rerun`, and it holds 2 equal noise terms, so it has `2 * df_rerun` degrees of
freedom. `Score.noise_floor()` is

```
t_(0.975, 2 * df_rerun) * sqrt(2) * se_rerun
```

A rerun of the same design differs from this run by less than this 95% of the time.

## 3. The paired comparison

`compare(a, b)` compares a candidate B with a baseline A on the tasks that both arms ran. The task
is the unit of pairing:

```
d_t   = m_t(B) - m_t(A)
delta = (1/T) * sum_t d_t
```

tidegauge runs 4 tests. 2 tests answer the rerun question. 2 tests answer the tasks question.

### 3.1 Rerun: t-test with within-task variance

The 2 arms run independently. So the variance of d_t is the sum of the variances of the 2 task means:

```
se_rerun(delta) = sqrt( sum_t [ w_t(A) + w_t(B) ] ) / T
df_rerun        = Satterthwaite over all 2T terms, as in 2.2
t               = delta / se_rerun(delta)
p_rerun         = two-sided p of t, from Student's t with df_rerun degrees of freedom
```

Each arm keeps its own variance. The test does not assume that the arms have the same pass rates.
So it tests the null hypothesis "the mean of the true differences over these tasks is 0". The 95%
interval is `delta ± t_(0.975, df_rerun) * se_rerun(delta)`.

A task with 1 trial borrows the mean variance of its own arm. An arm can have no task with 2 or
more trials, or no trial noise can show in either arm. Then `se_rerun`, `df_rerun`, `p_rerun` and
the interval are None.

### 3.2 Rerun: exact stratified permutation test

The null hypothesis: on every task, A and B have the same distribution of rewards. If that is
true, the arm labels inside a task carry no information. Deal the `k_A + k_B` trials of a task back
to the arms at random, k_A trials to A. Every way to deal is equally likely. The p-value is the
share of ways, over all tasks together, that give `|sum_t d_t|` at least as large as observed.

For 0/1 rewards the count has a closed form. Take task t with S passes in `N = k_A + k_B` trials.
Let X be the number of passes that land in A. X has the hypergeometric distribution:

```
P(X = x) = C(S, x) * C(N - S, k_A - x) / C(N, k_A),   max(0, S - k_B) <= x <= min(k_A, S)
d_t(x)   = (S - x) / k_B - x / k_A
```

C(n, r) is the number of ways to choose r of n. The tasks are independent, so the distribution of
`sum_t d_t` is the convolution of the T task distributions. Each d_t is a fraction. Multiply it by
L, the least common multiple of all trial counts, and it becomes an integer. tidegauge convolves
on this integer grid with exact counts, so the p-value is exact. A task where all trials pass, or
all fail, always gives d_t = 0 and drops out.

tidegauge uses the exact method when the grid has at most 2,000,000 points. Otherwise, and when a
reward is not 0 or 1, it deals 20,000 times at random with a fixed seed. The p-value is then
`(hits + 1) / (20,000 + 1)`. A random deal that equals the observed one must count as a hit. So
both sides of the comparison add with `math.fsum`, and the comparison allows a relative error of
1e-9. `Comparison.permutation_exact` says which method ran.

**What the test does not test.** It tests "no change on any task". The t-test tests the weaker
claim "no change on average". A change can help some tasks and hurt others by the same total.
Then the weak claim is true, and the strong claim is false. With equal trial counts in both arms,
the permutation test then rejects less often than alpha. With unequal trial counts it can reject
much more often (section 11.2). Section 3.3 uses this fact.

### 3.3 Which test decides the rerun question

`Comparison.rerun_p()` gives the p-value for the rerun question. `Comparison.rerun_test` names the
test behind it. `compare`, `rank` and `gate` use `rerun_p()` for every verdict.

| Case | When | Test | `rerun_test` |
|---|---|---|---|
| 1 | 0/1 rewards, exact grid, and the same number of trials per task in both arms (`balanced`) | exact permutation | `exact permutation` |
| 2 | otherwise, when `p_rerun` exists | t-test of 3.1 | `t-test` |
| 3 | otherwise: no t-test exists, because an arm has no task with 2 or more trials, or no trial noise shows in either arm | permutation, exact or Monte Carlo | `permutation` with equal trial counts, `permutation, unequal trial counts` otherwise |

Case 1 is exact. With equal trial counts it is also safe for the weak null. Case 2 tests the weak
null directly. Case 3 tests only "no change on any task", so it can reject too often when effects
have both signs. Section 11.2 measures that risk with 1 trial against 5. If no test exists,
`rerun_test` is `none` and `rerun_p()` is None. Case 3 with equal trial counts happens, for
example, with rewards other than 0/1 and 1 trial per task.

### 3.4 Tasks: paired t-test

```
se_tasks(delta) = sqrt( sum_t (d_t - delta)^2 / (T - 1) / T )
t               = delta / se_tasks(delta),   with T - 1 degrees of freedom
```

This is the usual paired t-test with the tasks as the pairs. It treats the tasks as a sample. The
spread of d_t holds both the true effects per task and the trial noise. The 95% interval is
`delta ± t_(0.975, T - 1) * se_tasks(delta)`. If every d_t is the same, `se_tasks` is 0, and
tidegauge reports p = 1 when delta = 0 and p = 0 otherwise. This test alone does not decide the
tasks question (3.8).

### 3.5 Tasks: exact sign-flip test

The null hypothesis: the distribution of d_t is symmetric around 0. Then each d_t is as likely to
be positive as negative. Flip the signs of the d_t in all 2^T ways. The p-value is the share of
sign patterns with `|sum_t ±d_t| >= |sum_t d_t|`. A zero difference adds nothing to any sum, so it
drops out.

For 0/1 rewards, `d_t * lcm(k_t(A), k_t(B))` is an integer. tidegauge convolves the 2-point
distributions `{+d_t, -d_t}` on this integer grid, so the p-value is exact. When the d_t are not
on a grid, it draws 20,000 random sign patterns with a fixed seed. The test needs no normal
approximation. Section 3.8 uses it to check the t-test.

### 3.6 Counts per task

`b_better`, `a_better` and `ties` count the tasks with d_t > 0, d_t < 0 and d_t = 0.
`b_better_unanimous` counts the tasks where every trial of B beats every trial of A, and the
trials inside each arm all agree. For 0/1 rewards: B passed every trial, A failed every trial.
`a_better_unanimous` is the reverse.

### 3.7 Between-task variance of the effect

Write `d_t = delta_t + e_t`. Here delta_t is the true effect on task t, and e_t is trial noise with
variance `sigma_t^2(A) / k_t(A) + sigma_t^2(B) / k_t(B)`. The observed spread of d_t has the
expected value `Var(delta_t) + mean_t Var(e_t)`. Subtract the estimated noise:

```
between = max(0, sum_t (d_t - delta)^2 / (T - 1)  -  (1/T) * sum_t [ w_t(A) + w_t(B) ])
```

This is a method-of-moments estimate. tidegauge sets it to 0 when the estimated noise is larger
than the observed spread. When no trial noise shows, the noise part is 0 and all the spread counts.

### 3.8 Which tests decide the tasks question

**The rule.** A tasks difference is `resolved` only when the paired t-test of 3.4 and the
sign-flip test of 3.5 both give p below alpha. alpha is `--alpha` in `compare` (0.05 by default)
and `1 - level` in `decompose` and `gate`. The sign-flip p is exact on the grid of 3.5, and the
seeded Monte Carlo value otherwise. `Comparison.tasks_p()` is the larger of the 2 p-values, and
`compare`, `decompose` and `gate` use it for every tasks verdict. The text output shows both p-values.

**Why the t-test alone is not enough.** With few tasks, or with d_t that take few values, the
t-test rejects too often. Example: 12 tasks, 1 trial per task in each arm, 0/1 rewards. B passes 4
tasks that A fails, and the other 8 tasks are equal. So d_t is +1 on 4 tasks and 0 on 8:

```
delta    = 4/12 = 1/3
se_tasks = sqrt( (8/33) / 12 ) = sqrt(2/99) = 0.1421
t        = (1/3) / 0.1421 = 2.345, so t^2 = 5.5 with 11 degrees of freedom: p = 0.0388
```

The sign-flip test sees 4 differences of +1. 2 of the 16 sign patterns reach |4|, so p = 0.125.
The verdict is `not resolved`: 4 tasks that agree are not enough evidence. With 1 trial per task in
both arms and 0/1 rewards, the sign-flip test and the exact permutation test of 3.2 give the same p.
Then the tasks question resolves a difference only when the rerun question resolves it too.

**A floor.** With m nonzero d_t, the sign-flip p is at least 2 / 2^m: only the observed signs and
their mirror reach the largest sum. So at alpha = 0.05 a tasks verdict needs 6 or more tasks with a
nonzero difference (2/64 = 0.031). 5 such tasks give at least 2/32 = 0.0625.

**Why the rule keeps its level.** The rule rejects only when both tests reject. If 1 of the 2 tests
holds the level alpha, the rule holds it too: both tests reject at most as often as that 1 test. In
the A/A rows of section 11.1, the sign-flip test rejects 1.7% to 3.6%, so the rule rejects at most
that often there. The cost is power: the rule rejects less often than the t-test alone, also when
a true difference exists. The detectable effect of section 4 describes the t-test, so it is
optimistic for the tasks question.

**On real data.** For each of the 2,775 pairs of the 75 Terminal-Bench 2.0 leaderboard submissions
(dataset commit `572b2614`), run `tidegauge compare A B --json`. With `--trials all`, every pair
shares 89 tasks. The t-test alone gives p < 0.05 in 2,114 pairs, and in 17 of them the sign-flip p
is 0.05 or more, so the verdict is `not resolved`. With `--trials valid` (80 to 89 shared tasks),
the counts are 2,068 and 15. In these 32 pairs, the t-test p is 0.038 to 0.050 and the sign-flip
p is 0.050 to 0.064.

**An interval of width 0.** If every d_t is the same, `se_tasks` is 0 and the tasks interval has
width 0 (3.4). The t-test then gives p = 0, or p = 1 when every d_t is 0. The width 0 is not a
certainty: these tasks show no spread to measure. The sign-flip test still counts the tasks: 5
tasks that all give +1 give p = 2/32 = 0.0625, so the verdict is `not resolved`. `compare` and
`decompose` print 1 line that explains a tasks interval of width 0.

## 4. Minimum detectable effect and planning

A two-sided test at level alpha with power `1 - beta` detects a true difference Delta when
`Delta = (critical value + z_(1 - beta)) * se`. This Delta is the minimum detectable effect (MDE):

```
mde_rerun = (t_(1 - alpha/2, df_rerun) + z_power) * se_rerun(delta)
mde_tasks = (t_(1 - alpha/2, T - 1)    + z_power) * se_tasks(delta)
```

The formula ignores the small chance of a significant result in the wrong direction. It uses the
same standard error with and without a true difference. It describes the t-tests. The exact
permutation test is more careful, so its power at the MDE is 2.9 to 6.8 points lower (section 11.1).

### 4.1 Planning from a pilot

`plan` predicts the MDE for k trials per task in each arm, from pilot data. Let v be the mean
per-trial variance, summed over the 2 arms:

```
v = (1/T) * sum_t [ s_t^2(A) + s_t^2(B) ]      with 2 pilot arms
v = 2 * (1/T) * sum_t s_t^2(A)                 with 1 pilot arm
```

With 1 arm, tidegauge assumes that the candidate is as noisy as the pilot. For k trials per task
in each arm:

```
Var_rerun(delta) = v / (T k)
Var_tasks(delta) = (between + v / k) / T
mde_rerun(k)     = (z_(1 - alpha/2) + z_power) * sqrt(Var_rerun(delta))
```

The second line is the decomposition of the tasks variance. The between-task part stays fixed.
The trial noise shrinks with k. `plan` uses the normal quantile z, not t, because the degrees of
freedom of a future run are not known. So at k = 2 it promises a little more power than the t-test
delivers (section 11.1). `Plan.attempts_for(Delta)` solves
`(z_(1 - alpha/2) + z_power)^2 * v / (T k) = Delta^2` for k and rounds up:

```
k = ceil( v * (z_(1 - alpha/2) + z_power)^2 / (T * Delta^2) )
```

### 4.2 The floor

As k grows, `Var_tasks(delta)` falls to `between / T`, not to 0. So for the tasks question, no
number of reruns detects less than

```
floor_tasks = (t_(1 - alpha/2, T - 1) + z_power) * sqrt(between / T)
```

Below the floor, add tasks. With 2 pilot arms, `plan` takes `between` from
`compare(a, b).between_task_variance`. With 1 arm it cannot estimate `between`, and it reports no floor.

## 5. Rank intervals

`rank` sorts the arms by score. Then it asks: if every arm ran the same tasks again, where would
each arm land? It simulates 10,000 reruns with a fixed seed. Each simulated rerun adds normal noise
with standard deviation `se_rerun` to each score, independently, and sorts again. The 95% rank
interval holds the central 95% of the simulated ranks. `p_first` is the share of simulated reruns
in which the arm is first.

An arm without a noise estimate of its own borrows the mean trial variance of the other arms. This
happens with 1 trial per task, or when no task of the arm shows trial noise. For each arm j with
an estimate, `v_j = (T_j * se_j)^2 / sum_t (1 / k_t)`. This is the per-trial variance that gives
se_j. The borrowing arm gets the noise `sqrt(mean_j v_j * sum_t (1 / k_t)) / T`. With 1 trial per
task, that is `sqrt(mean_j v_j / T)`. The text output marks such an arm with `~`.

If no arm has a noise estimate, the ranks cannot be simulated. Then every interval is the full
range 1 to N, and `p_first` is None.

The simulation starts from the observed scores, not the true ones. So a rank interval is a
parametric bootstrap. It is not an exact confidence interval for the true rank. `rank` tests each
neighbor pair with `rerun_p()`.

## 6. The gate

`gate` turns a comparison into a decision for CI: `ok`, `regressed` or `undecided`. It uses the
95% interval of the chosen question (rerun by default) and these rules:

1. No interval (no noise is measured): `undecided`.
2. With `--require-gain`: `ok` if the whole interval is above 0, `regressed` if the whole interval
   is below 0.
3. Without it: `ok` if the whole interval is above `-margin`. Else `regressed` if the whole
   interval is below 0.
4. A decision that claims a difference needs the test to agree. `regressed`, and `ok` with
   `--require-gain`, also need `rerun_p()` below 1 - level (0.05 at level 0.95). For the tasks
   question they need `tasks_p()` below it: the paired t-test and the sign-flip test must both
   agree (3.8). If the test does not agree, the decision is `undecided`, and the reason says so.
5. Anything else is `undecided`.

Rule 4 protects the case where few tasks show any noise and the interval is too narrow. The exact
test does not depend on a variance estimate. For the tasks question, rule 4 also protects the case
where few tasks differ.

## 7. Counting rules

Some trials end without a valid reward. For example, the sandbox did not start, the model API
failed, or the verifier timed out. Harbor's job mean counts only the trials with a valid reward
(`--trials valid`, the default). The Terminal-Bench leaderboard counts every trial and scores a
missing reward as 0 (`--trials all`). The 2 rules can move a score by tens of points.

The rule is a choice about the input. The statistics take its result as the data:

- `valid` drops the broken trials. A task with broken trials then has a smaller k_t, and its
  variance term grows. A task with no valid trial leaves the arm.
- `all` keeps every trial and counts an infrastructure failure as a failure of the agent.

Neither rule is wrong. They answer different questions. `score` and `compare` show the result
under the other rule when it differs. `gate` says undecided when the 2 rules give different decisions.

## 8. Known limits

- **Few trials.** With k = 2 trials per task, the t intervals are conservative. The 95% interval
  of 1 arm holds the true score 96.6% to 98.8% of the time (section 11.1). Use k >= 3 for an
  interval that is not wider than it needs to be.
- **Power below the target.** At the MDE, the t-test rejects 74.5% to 80.0% of the time, and the
  exact test 71.3% to 76.8%, not 80%. `plan` uses normal quantiles: at 2 trials per task it
  predicts 59.8% power where the t-test reaches 56.5% (section 11.1).
- **The strong null of the permutation test.** Case 3 of 3.3 tests only "no change on any task".
  With 1 trial against 5 and effects of both signs, it rejects 2.0% and 4.3% in 2 cases built from
  mid89. In a constructed case it rejects 18.5% (section 11.2). Give both arms the same number of
  trials per task, and at least 2.
- **Borrowed noise.** A task with 1 trial borrows the mean variance of the donors. This assumes
  that it is as noisy as the donors on average. Under `valid`, a task with 1 trial is often a task
  whose other trials broke, so it may not be typical. In the simulation the assumption holds, and
  the interval of 1 arm holds the truth 95.0% to 96.5% of the time (section 11.3).
- **Few tasks.** The paired t-test assumes that the d_t are roughly normal. With few tasks, or few
  distinct d_t, it rejects too often. So a tasks verdict also needs the sign-flip test (3.8). At
  alpha = 0.05 that needs 6 or more tasks with a nonzero difference.
- **No correction for many comparisons.** `rank` tests each neighbor pair at alpha, with no
  correction for the number of pairs. Sorted neighbors are close, so false separations are rare:
  0.2% of runs with 10 identical arms (section 11.2). The rank intervals hold for each arm
  alone. They are not joint intervals for all arms at once.
- **No correction for optional stopping.** Every p-value assumes that k was fixed before the run.
  If you add trials until the result is significant, false alarms become frequent:
  14.0% instead of at most 5% in section 11.2. Fix k with `plan` before the run.
- **The binary assumption of the exact tests.** The exact permutation and sign-flip tests need
  0/1 rewards. Other rewards use 20,000 random draws, and the p-value then has a Monte Carlo
  standard error of up to 0.0035.
- **Independent trials.** Every formula assumes independent trials. Trials that share a
  rate-limited API, a cache or a time window can be correlated. Then every error bar is too small.
- **No joint claim in `decompose`.** The 2 effects share 1 arm, so their estimates are correlated.
  Each interval holds on its own. The share of coordination has no interval (section 13).

## 9. Worked example

3 tasks, 2 trials per task in each arm:

| Task | Baseline A | Candidate B | m_t(A) | m_t(B) | d_t |
|---|---|---|---|---|---|
| t1 | 1, 0 | 1, 1 | 0.5 | 1 | 0.5 |
| t2 | 0, 0 | 1, 0 | 0 | 0.5 | 0.5 |
| t3 | 1, 1 | 1, 1 | 1 | 1 | 0 |

**Scores.** A = (0.5 + 0 + 1) / 3 = 0.500. B = (1 + 0.5 + 1) / 3 = 0.833.

**Rerun standard error of A.** s^2 is 0.5 for t1 and 0 for t2 and t3. So w = 0.5 / 2 = 0.25 for
t1 and 0 for the others. `se_rerun = sqrt(0.25) / 3 = 0.167`. The leaderboard formula gives the
same: 0.5 * 0.5 / (2 - 1) = 0.25 for t1.

**Degrees of freedom of A.** Only t1 shows noise, with 2 - 1 = 1 degree of freedom. So
df_rerun = 0.25^2 / (0.25^2 / 1) = 1. The 95% interval is 0.5 ± 12.706 * 0.167, so -1.618 to
2.618. 1 noisy task with 2 trials tells almost nothing about the noise. The noise floor is
4.303 * sqrt(2) * 0.167 = 1.014, from t with 2 * 1 = 2 degrees of freedom.

**Tasks standard error of A.** The task means 0.5, 0 and 1 have variance 0.25.
`se_tasks = sqrt(0.25 / 3) = 0.289`.

**Difference.** delta = (0.5 + 0.5 + 0) / 3 = 0.333.

**Rerun t-test.** The terms are 0.25 (A, t1) and 0.25 (B, t2), so their sum is 0.5.
`se_rerun = sqrt(0.5) / 3 = 0.236`. df_rerun = 0.5^2 / (0.25^2 + 0.25^2) = 2.
t = 0.333 / 0.236 = 1.414. For 2 degrees of freedom, `p = 1 - |t| / sqrt(2 + t^2)`, so
p = 1 - 1.414 / 2 = 0.293. The 95% interval is 0.333 ± 4.303 * 0.236, so -0.681 to 1.347.

**Exact permutation test.** t3 has 4 passes in 4 trials, so it drops out. t1 has 3 passes in 4
trials. A gets 1 or 2 of them, each with probability 3/6, so d_1 = +0.5 or -0.5. t2 has 1 pass.
It lands in B with probability 3/6, so d_2 = +0.5 or -0.5. The sum d_1 + d_2 is 1, 0 or -1, with
probabilities 1/4, 1/2 and 1/4. The observed sum is 1. So p = P(|sum| >= 1) = 1/2.

**Which test decides.** Both arms have 2 trials on every task, and the rewards are 0/1. So case 1
of 3.3 applies: `rerun_p()` = 0.5, from the exact permutation test.

**Paired t-test.** The d_t are 0.5, 0.5 and 0. Their mean is 1/3 and their variance is 1/12.
`se_tasks = sqrt((1/12) / 3) = 1/6`. t = (1/3) / (1/6) = 2, with 2 degrees of freedom. So
p = 1 - 2 / sqrt(6) = 0.184.

**Sign-flip test.** The nonzero differences are 0.5 and 0.5. The 4 sign patterns give the sums 1,
0, 0 and -1. 2 of the 4 reach |1|, so p = 0.5.

**Between-task variance.** 1/12 - 0.5/3 is below 0, so `between = 0`.

**MDE.** (4.303 + 0.842) * 0.236 = 1.213. This design cannot detect any difference that fits in
the range of a score.

**Plan.** v = (0.5 + 0.5 + 0) / 3 = 1/3. To detect 0.10:
k = ceil((1/3) * 2.802^2 / (3 * 0.10^2)) = ceil(87.2) = 88 trials per task in each arm.

`tests/test_stats.py::test_docs_worked_example` checks every number in this section.

## 10. How the numbers in section 11 were produced

Run `uv run python tests/test_calibration.py`. It takes about 15 minutes and prints every
number in section 11. The simulation uses fixed seeds, so every run prints the same numbers.

Each simulated trial passes with a fixed probability p_t. The 3 profiles of p_t are:

| Profile | Tasks | Never solved | Always solved | Spread evenly between | Mean |
|---|---|---|---|---|---|
| mid30 | 30 | 8 | 7 | 15 tasks over (0, 1) | 0.483 |
| mid89 | 89 | 25 | 20 | 44 tasks over (0, 1) | 0.472 |
| low89 | 89 | 50 | 5 | 34 tasks over (0, 0.5) | 0.152 |

A p-value or an interval that is None counts as "no rejection" and "no interval". `gate` treats
both as undecided.

## 11. Calibration

### 11.1 Balanced arms

Both arms have k trials on every task. The columns:

- **A/A**: both arms come from the same profile. The share of 20,000 comparisons with p <= 0.05.
  The target is 5% or less. `rerun_p()` is the exact permutation test here (case 1).
- **score 95%**: the share of 20,000 runs of 1 arm whose 95% rerun interval holds the true mean.
- **noise floor 95%**: the share of 20,000 pairs of runs whose gap stays below the noise floor of
  the first run.
- **delta 95%**: B gains 0.15 on every task that is sometimes solved, capped at 1. The share of
  10,000 comparisons whose 95% rerun interval holds the true difference.
- **power**: B gains the same amount on every task that is sometimes solved, capped at 1. The gain
  makes the true difference equal to the MDE of section 4, computed from the true pass rates and
  their degrees of freedom. The share of 10,000 comparisons with p <= 0.05. The target is 80%.

Monte Carlo standard errors: 0.15 points for an A/A rate, 0.15 points for score 95% and noise
floor 95%, 0.22 points for delta 95%, and 0.40 points for power.

| Profile | k | A/A `rerun_p()` | A/A t-test | A/A paired t | A/A sign-flip | score 95% | noise floor 95% | delta 95% | power t-test | power `rerun_p()` |
|---|---|---|---|---|---|---|---|---|---|---|
| mid30 | 2 | 2.5% | 3.5% | 4.2% | 1.7% | 98.8% | 96.8% | 96.9% | 74.5% | 71.3% |
| mid30 | 3 | 2.6% | 4.4% | 4.4% | 2.1% | 96.0% | 95.2% | 95.5% | 78.4% | 71.6% |
| mid30 | 5 | 3.4% | 5.1% | 4.9% | 2.9% | 95.2% | 94.7% | 95.2% | 79.4% | 73.9% |
| mid89 | 2 | 3.1% | 4.2% | 5.1% | 3.1% | 96.6% | 95.4% | 95.8% | 77.6% | 74.7% |
| mid89 | 3 | 3.3% | 4.6% | 4.7% | 3.3% | 95.3% | 94.7% | 95.0% | 78.8% | 75.0% |
| mid89 | 5 | 3.6% | 4.9% | 4.8% | 3.6% | 94.9% | 94.9% | 94.7% | 80.0% | 76.8% |
| low89 | 2 | 2.8% | 3.8% | 4.9% | 2.7% | 96.9% | 95.3% | 95.6% | 76.6% | 73.7% |
| low89 | 3 | 3.6% | 5.2% | 5.4% | 3.5% | 95.4% | 94.8% | 95.1% | 78.4% | 74.7% |
| low89 | 5 | 3.7% | 5.0% | 4.9% | 3.6% | 95.3% | 94.8% | 95.1% | 79.7% | 76.3% |

What the table shows:

- The exact test rejects 2.5% to 3.7% of A/A comparisons, and the t-test 3.5% to 5.2%. Both stay
  at or near the 5% target.
- At k = 2, the t interval of 1 arm is careful: it holds the truth 96.6% to 98.8% of the time. On
  the same runs, the normal interval `score ± 1.96 * se_rerun` held it 91.1% (mid30), 94.5%
  (mid89) and 93.3% (low89) of the time. With k = 3 or more, the t interval holds the truth 94.9%
  to 96.0% of the time.
- The noise floor holds 94.7% to 96.8% of rerun gaps. The interval of the difference holds the
  truth 94.7% to 96.9% of the time.
- At the MDE, the t-test has 74.5% to 80.0% power, and the exact test 71.3% to 76.8%. The t-test
  falls short most at k = 2.
- At most 0.14% of runs of 1 arm had no interval (mid30 at k = 2). Every comparison had one.

`plan` gets a pilot of 40 trials per task in each arm, with pass counts equal to the truth. The
truth is the mid89 profile, with each p_t rounded to a multiple of 1/40. B gains 6 passes in 40 on
every task that is sometimes solved, capped at 40. 10,000 simulated comparisons then test the
prediction:

| Trials per task | Power that `plan` predicts | t-test | `rerun_p()` (exact test) |
|---|---|---|---|
| 2 | 59.8% | 56.5% | 52.8% |
| 5 | 93.7% | 94.0% | 92.7% |

### 11.2 Effects of both signs, optional stopping and ranking

In these 7 setups, the mean difference is 0, but single tasks change. Each row has 5,000
comparisons, so a rate has a Monte Carlo standard error of 0.31 points at 5%. The last column says
which case of 3.3 decided. It was the same case in every comparison of a row.

| A | B | Trials A vs B | `rerun_p()` | exact permutation | t-test | Case of 3.3 |
|---|---|---|---|---|---|---|
| mid89 | mid89, +-0.3 in turn | 3 vs 3 | 2.8% | 2.8% | 5.2% | 1 |
| mid89 | mid89, +-0.3 in turn | 2 vs 8 | 4.9% | 5.2% | 4.9% | 2 |
| always or never | 50% | 5 vs 5 | 1.2% | 1.2% | 5.1% | 1 |
| always or never | 50% | 8 vs 2 | 4.3% | 16.2% | 4.3% | 2 |
| mid89, +-0.3 in turn | mid89 | 1 vs 5 | 2.0% | 2.0% | none | 3 |
| mid89 | mid89, +-0.3 in turn | 1 vs 5 | 4.3% | 4.3% | none | 3 |
| 50% | always or never | 1 vs 5 | 18.5% | 18.5% | none | 3 |

"+-0.3 in turn" means: the sometimes-solved tasks of mid89 move up and down by 0.3 in turn. "Always
or never" is 60 tasks, half never solved and half always solved. "50%" is 60 tasks solved half of
the time.

- With equal trial counts, case 1 decides and rejects 1.2% to 2.8%.
- With 8 vs 2 or 2 vs 8 trials, the t-test decides and rejects 4.3% to 4.9%. The exact permutation
  test alone rejects 16.2% in the 8 vs 2 row: that was D4.
- With 1 trial against 5, case 3 decides. It rejects 2.0% and 4.3% in the 2 rows built from mid89.
  In the last of the 7 rows it rejects 18.5%. There, the 1 trial of A is a coin flip, and the 5
  trials of B always agree. The test assumes that all trials of a task are alike, so it reads the
  coin flips as effects.

2 more checks use `rerun_p()` with equal trial counts, so the exact test decides (case 1):

| Check | Runs | Result |
|---|---|---|
| A/A on mid89: start at 2 trials per task, add 1 trial per task to each arm, test after each step up to 10, stop at the first `rerun_p() <= 0.05` | 2,000 | 14.0% of runs stop with a false alarm |
| 10 identical arms on mid89 with 5 trials per task: rank them, then test each neighbor pair | 1,000 | 0.2% of runs show a separated pair |

Optional stopping turns at most 5% false alarms into 14.0%. Sorted neighbors are close, so a false
separation between neighbors is rare.

### 11.3 Tasks with 1 trial

Both arms have 5 trials on some tasks and 1 trial on the others. The tasks with 1 trial borrow
noise from the donors (2.1 and 2.2). Each row has 10,000 runs or comparisons.

| Trials per task | A/A t-test | A/A `rerun_p()` | score 95% | delta 95% |
|---|---|---|---|---|
| 45 tasks with 5 trials, 44 with 1 | 4.8% | 4.1% | 95.0% | 94.5% |
| 9 tasks with 5 trials, 80 with 1 | 4.3% | 3.9% | 96.5% | 95.3% |

- With 45 tasks that lend noise, the t-test rejects 4.8%, and the interval of 1 arm holds the truth
  95.0% of the time.
- With 9 tasks that lend noise to 80, the t-test rejects 4.3%, and the interval of 1 arm holds the
  truth 96.5% of the time. 9 donors give few degrees of freedom, so the interval is wide.
- In 0.19% of the runs with 9 donors, no donor showed noise, so the arm had no interval.
- Both arms have the same trial counts, so `rerun_p()` is the exact test (case 1). It rejects 4.1%
  and 3.9%.

`tests/test_calibration.py` runs smaller versions of these checks in every test run. Set
`TIDEGAUGE_SLOW=1` to also run the A/A check with 20,000 comparisons per row.

## 12. Review record

An independent review of `stats.py` found the defects D1 to D9 and t. D10 came from a real run with
12 tasks, and D11 came up during its fix. Each fix has a test in `tests/test_stats.py`.

| Item | What was wrong | How it is fixed |
|---|---|---|
| D1 | The Monte Carlo permutation test missed exact ties when the values are large. With 4 tasks, 1 trial per arm and values near 50,000, it gave p = 1/20,001. The exact p is 0.125. | Both sides add with `math.fsum`, and the comparison allows a relative error of 1e-9. The sign-flip test does the same. |
| D2 | When no task showed trial noise, the rerun interval had width 0 and the z-test gave p = 0. 1 flipped task could make `gate` say `regressed`. | `se_rerun`, `df_rerun`, `p_rerun` and the interval are None, and `gate` says `undecided` (2.2, 6). |
| D3 | The MDE described the z-test, but the verdicts used the exact test, which has less power. | Wording: `mde_rerun` names the t-test, and section 11.1 gives the power of both tests. |
| D4 | The exact permutation test decided also with unequal trial counts. With 8 vs 2 trials and a change of mean 0, it rejects 16.2% at alpha = 5% (section 11.2). | The exact test decides only for balanced arms. The t-test decides otherwise (3.3). |
| D5 | A NaN value gave a "significant" Monte Carlo p-value of 1/20,001. | `score`, `compare` and both exact tests raise `ValueError` on a value that is not finite. |
| D6 | `normal_two_sided_p` returned 0 from z = 8.3 on. At z = 10 the true value is 1.5e-23. | It uses `math.erfc`. |
| D7 | With no noise estimate in any arm, `rank` gave intervals 1 rank wide and `p_first = 1`: a false certainty. | Every interval is 1 to N, and `p_first` is None (5). |
| t | The normal reference was too optimistic at k = 2. The 95% interval of 1 arm, `score ± 1.96 * se_rerun`, held the truth 91.1% of the time for mid30 (section 11.1). | Student's t with Satterthwaite degrees of freedom (2.2, 3.1). |
| D8 | Each borrowed term counted as an independent estimate with the pooled degrees of freedom. But all borrowed terms rest on the same donors. 2 tasks [1, 0] and [0, 1] that lend noise to 20 tasks with 1 trial gave `df_rerun = 42.0`, where the data hold 2 degrees of freedom. | Each donor carries its own term plus its share of the borrowed terms, and the formula applies to these pieces (2.2). The example now gives `df_rerun = 2.0`. |
| D9 | `rerun_test` said `permutation, unequal trial counts` also for balanced arms. This happens with rewards other than 0/1 and no t-test. The p-value was right. | Case 3 is named `permutation` with equal trial counts (3.3). |
| D10 | A tasks verdict came from the paired t-test alone. 12 tasks with 1 trial per task, where 4 tasks give +1 and 8 give 0, gave p = 0.0388 and `resolved`. The exact sign-flip test gives p = 0.125. Tasks that all give the same difference gave p = 0, also for 2 tasks. | A tasks verdict needs both tests below alpha (3.8). `compare`, `decompose` and `gate` use `tasks_p()`. The text shows the sign-flip p, and it explains a tasks interval of width 0. |
| D11 | Equal values could give a sample variance of about 1e-33, not 0, because their mean is rounded. 10 tasks with the rewards 0.1, 0.1, 0.1 then showed trial noise: `se_rerun = 3.1e-18` with 20 degrees of freedom, and a rerun interval of width 0 (D2). | `sample_variance` gives exactly 0 when all values are equal. That arm now has no rerun interval. |

## 13. Decomposition: headcount and coordination

A team of agents can score higher than 1 agent for 2 reasons: it has more agents, or its agents
coordinate. `decompose` separates the 2 reasons. It takes 3 arms that ran the same tasks:

| Role | What runs | Score |
|---|---|---|
| solo | 1 agent | S |
| indep | a team whose agents cannot communicate | I |
| team | the same team, with communication | C |

It reports 3 effects:

```
headcount     H = I - S    more agents, no communication
coordination  K = C - I    the same agents, with communication
total         D = C - S    = H + K
```

Bespoke Labs' Multi Agent Coordination Bench uses this identity for teams of 8, with the names
S1, S∅8 and S8. tidegauge adds the error bars of section 3 to each effect.

### 13.1 Why the 2 parts add up exactly

Each score is a task-macro mean (section 2). Let T be the set of tasks that all 3 arms have. On T,

```
D = (1/T) * sum_t [ m_t(C) - m_t(S) ]
  = (1/T) * sum_t [ m_t(I) - m_t(S) ]  +  (1/T) * sum_t [ m_t(C) - m_t(I) ]
  = H + K
```

The identity holds for each task, so it holds for the mean. It holds for any numbers of trials per
task. It needs 1 set of tasks. `compare(a, b)` uses the tasks that a and b share. With 3 arms, each
pair can share different tasks. Then the score of indep in H is not the score of indep in K, and
the 3 differences do not add up. So `decompose` first cuts all 3 arms to T. The finding
`task-set-mismatch` lists the tasks that it leaves out. Under `--trials valid`, T holds the tasks
with 1 or more valid trials in each arm.

`decompose` checks the identity on every run: `|D - H - K| <= 1e-12 * max(1, largest value)`.
Only the rounding of floating-point numbers is left.

Tasks pair by name: the last part of the task path. The 3 arms run 3 variants of each task: for 1
agent, for a team without a channel, and for a team with a channel. The variants need the same
key. Their files and their rosters differ by design, so `decompose` does not compare task
checksums or rosters between the arms. It does report an arm that mixes rosters, and arms that
differ in sandbox, Harbor version or limits.

`--task-key REGEX` is for variants whose names hold the condition, as in `schedule-009-solo`,
`schedule-009-silent` and `schedule-009-talk`. The key of a trial is then the first group of REGEX
in its task name, with the usual normalization: `^(.*)-(?:solo|silent|talk)$` gives all 3 the key
`schedule-009`. A trial whose name does not match keeps the key of its name, and the note
`unmatched-task-key` counts those trials. The checks across arms compare task checksums only
between trials with the same task name. Inside 1 arm, 2 names under 1 key still give
`merged-task-names` and `checksum-drift`: that arm mixes 2 variants.

### 13.2 The 2 error bars of each effect

Each effect is the paired comparison of section 3 for 2 of the arms, on T. So each effect has both
error bars, the rerun test of section 3.3, the paired t-test of section 3.4, and the detectable
effect of section 4. Let V(X) be the rerun variance of the score of arm X:
`V(X) = (1/T^2) * sum_t w_t(X)` (section 2.1).

| Effect | Baseline | Candidate | Rerun variance |
|---|---|---|---|
| headcount | solo | indep | V(S) + V(I) |
| coordination | indep | team | V(I) + V(C) |
| total | solo | team | V(S) + V(C) |

What the 2 error bars say, for the coordination effect:

- **rerun.** If the 2 teams ran these tasks again, would communication add this much again? Only
  the trial noise of indep and team counts. Use it for "did communication help on this benchmark?".
- **tasks.** Would communication add this much on new similar tasks? The spread of the effect
  between tasks also counts. Use it for "does communication help on work like this?".

The same 2 questions apply to headcount and to the total. A team effect often differs from task
to task: some tasks split into parallel work, and others do not. Then the between-task variance of
the effect (section 3.7) is large, and the tasks interval is much wider than the rerun interval.

A verdict is `resolved` when the p-value is below `1 - level`: below 0.05 at the level 0.95. For
rerun, the p-value is `rerun_p()` (3.3). For tasks, the paired t-test and the sign-flip test must
both give p below it (3.8), and the table shows both p-values. This is the rule of `compare`
without the direction. `cannot tell` means that no test exists.

### 13.3 The 2 parts share an arm

H and K both use the score of indep, with opposite signs. The 3 arms run independently, so

```
Cov(H, K)  = Cov(I - S, C - I) = -V(I)
Corr(H, K) = -V(I) / sqrt( [V(S) + V(I)] * [V(I) + V(C)] )
```

With equal noise in the 3 arms, the correlation is -1/2. What follows from this:

- Luck in indep moves H and K by the same amount in opposite directions. It does not move D.
  1 more passed trial in indep, on a task with k trials, adds `1 / (k T)` to H and takes
  `1 / (k T)` from K.
- "More agents help, and communication hurts" with a total near 0 is the pattern that noise in
  indep alone produces. The reverse pattern also comes from noise in indep. Check both intervals
  before you read such a pattern as a result.
- Each interval and each p-value is valid on its own. The 2 are not a joint statement. With 2
  tests at the level alpha, the chance of 1 or more false `resolved` is at most 2 alpha. For a
  joint statement at 95%, run `decompose --level 0.975`.
- D = H + K, so the 3 rows hold 2 independent pieces of information, not 3.

`tests/test_decompose.py` checks the first item on files, and the correlation of -1/2 with 4,000
simulated runs of 3 arms.

### 13.4 The share of coordination

The share of the total gain that comes from coordination is `K / D`. A ratio has a meaning only
when its denominator is not noise: when D is near 0, a small change of D moves the share from a
large positive number to a large negative number. So `decompose` gives the share only when the
total is resolved for the rerun question. Otherwise it says that the share is not defined.

Even then, the share is a ratio of 2 estimates. tidegauge gives it no interval. K and D share the
arm team, so `Cov(K, D) = V(C)`: the 2 estimates are positively correlated. When K is not resolved,
the output says that the share of coordination can be 0%. The share is below 0% or above 100% when
H and K have opposite signs.

### 13.5 What the design can detect

The detectable effect of section 4 applies to each effect. `decompose` prints it for the
coordination effect, because that effect is often small. If the design detects 18 points and a
realistic coordination effect is 5 points, `not resolved` says nothing about coordination. It says
that the run is too small.

The coordination effect does not use solo. To detect a smaller coordination effect, add trials to
indep and to team.

indep is in both effects. For a fixed total number of trials and equal noise per trial, the sum
`Var(H) + Var(K) = V(S) + 2 V(I) + V(C)` is smallest when indep has sqrt(2) = 1.41 times the
trials of each other arm. That sum is then `(2 + sqrt(2))^2 / 12 = 97.1%` of the sum for equal
arms: a gain of 2.9%. Equal numbers of trials per task keep the exact test of section 3.2, so
equal arms are the better design.

### 13.6 Cost

For a seat trial, tokens and cost are the sums over the seats. The sum exists only when every seat
that used a model reports the value. A seat used a model when its agent names a model, or when it
reports any usage. A trial without a complete sum has no cost: the cost of a part of a team is
too low.

The cost per trial of an arm is a task-macro mean over T, like the score: the mean over the tasks
of the mean cost of the trials of each task. A trial without a valid reward still has a cost, and
it counts here. Trials without a cost are left out, and the table marks an arm where that happens.
The cost ratio is the cost per trial of the arm divided by that of solo. It uses only the tasks
with a cost in both arms, so both means cover the same tasks.

### 13.7 What the names assume

The names of the effects say what the 3 arms are meant to isolate. tidegauge sees only the arms.

- indep can be the same team with its channels closed. It can also be N separate runs of 1 agent
  that count as 1 result. These are 2 different controls. The first keeps the shared environment,
  and the second removes it.
- The headcount effect holds everything that differs between solo and indep: more agent-hours,
  more tokens, and shared resources. The cost ratio shows a part of that.
- A seat can stop early while its trial still has a reward. The note `seat-errors` counts those
  trials. In them, fewer agents than the roster names worked to the end.
- The 3 arms must be separate runs. If indep is built from the runs of solo, the 2 scores are
  correlated, and the interval of the headcount effect is wrong.

### 13.8 Worked example

`tests/test_decompose.py` builds 3 arms with 20 tasks and 3 trials per task. A task with 1 or 2
passes in 3 trials has `w_t = 1/9` (section 2.1). solo has 7 such tasks, indep 8 and team 6. The
scores are S = 0.40, I = 0.70 and C = 0.75.

**Effects.** H = 0.30, K = 0.05 and D = 0.35 = H + K.

**Rerun error bars.** `V(S) = (7/9) / 400`, `V(I) = (8/9) / 400` and `V(C) = (6/9) / 400`. All
terms are equal and have 2 degrees of freedom each, so df_rerun is 2 times the number of terms.

| Effect | se_rerun | df_rerun | 95% interval |
|---|---|---|---|
| H | sqrt(15/9) / 20 = 0.0645 | 30 | 0.30 ± 2.042 * 0.0645: 0.168 to 0.432 |
| K | sqrt(14/9) / 20 = 0.0624 | 28 | 0.05 ± 2.048 * 0.0624: -0.078 to 0.178 |
| D | sqrt(13/9) / 20 = 0.0601 | 26 | 0.35 ± 2.056 * 0.0601: 0.226 to 0.474 |

**Tasks error bar of K.** The 20 differences `m_t(C) - m_t(I)` have the sum 1 and the sum of
squares 3. Their variance is `(3 - 1/20) / 19 = 0.1553`. `se_tasks = sqrt(0.1553 / 20) = 0.0881`,
and the 95% interval is 0.05 ± 2.093 * 0.0881: -0.134 to 0.234. t = 0.05 / 0.0881 = 0.567 with 19
degrees of freedom, so p = 0.577.

**Sign-flip test.** K has 12 nonzero differences, and their sum is 1. 2,896 of the 4,096 sign
patterns give `|sum| >= 1`, so p = 0.707. H and D each have 12 nonzero differences of 1 sign, so
p = 2/4,096 = 0.0005. The tasks verdicts of H and D are `resolved`, because both tests agree. K is
`not resolved` (3.8).

**Correlation.** `Corr(H, K) = -8 / sqrt(15 * 14) = -0.552`.

**Detectable coordination effect.** (2.048 + 0.842) * 0.0624 = 0.180 for rerun, and
(2.093 + 0.842) * 0.0881 = 0.259 for tasks. The observed 0.05 is far below both.

**Share.** The exact permutation test gives p < 0.0001 for D, so the total is resolved and the
share exists: 0.05 / 0.35 = 14.3%. K is not resolved, so this share can be 0%.

`tests/test_decompose.py` checks every number in this section.
