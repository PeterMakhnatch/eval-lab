# tidegauge

**Honest numbers for [Harbor](https://github.com/harbor-framework/harbor) runs.**

Harbor runs an agent on a set of tasks and writes a job directory. tidegauge reads that directory
and answers the questions that come next. It uses arithmetic, not a model.

| Question | Command |
|---|---|
| Are these trials complete, valid and comparable? | `tidegauge audit` |
| What is the score, and how far can it move? | `tidegauge score` |
| Is the difference between 2 runs real? | `tidegauge compare` |
| Which ranks in a leaderboard survive a rerun? | `tidegauge rank` |
| How many trials settle the question, and what do they cost? | `tidegauge plan` |
| May this candidate replace the baseline? | `tidegauge gate` (exit status for CI) |
| Where do the input tokens of a run go? | `tidegauge context` |
| How much of a team's gain comes from more agents, and how much from coordination? | `tidegauge decompose` |

- **Deterministic.** The same files give the same report. Tests are exact where that is possible.
- **No dependencies.** Python 3.12 and the standard library. It does not import Harbor.
- **Read only.** It never starts a trial, never opens the network, never spends money.

## Install

```sh
uv tool install git+https://github.com/PeterMakhnatch/tidegauge
# or: pipx install git+https://github.com/PeterMakhnatch/tidegauge
```

Every command takes Harbor job directories. An arm is 1 configuration: 1 directory, with 1 job or
with several jobs of the same agent and model.

```sh
tidegauge audit jobs/2026-11-18__10-00-00           # is this job sound?
tidegauge compare jobs/baseline jobs/candidate      # is the candidate better?
tidegauge rank --split jobs                         # each child directory of jobs/ is 1 arm
tidegauge compare old=runs/a new=runs/b --json      # name the arms; print JSON for a program
tidegauge decompose jobs/solo jobs/indep jobs/team  # 1 agent, a silent team, a team: 2 effects
```

## A real example: 1 model, 4 harnesses

The data is from the public Terminal-Bench 2.0 leaderboard submissions. All 4 arms use Claude Opus 4.6.
Each arm has 89 tasks with 5 attempts per task.

```
$ tidegauge score jobs/terminus-2 jobs/terminus-kira jobs/meta-harness jobs/judy
metric: reward · only trials with a valid reward count · 95% intervals

arm                             tasks  trials  score  rerun  tasks  noise floor  not valid
------------------------------  -----  ------  -----  -----  -----  -----------  ---------
Terminus2__Claude-Opus-4.6         89    441*  62.9%   ±2.8   ±8.7          3.9          4
Terminus-KIRA__Claude-Opus-4.6     89    442*  74.7%   ±2.7   ±7.8          3.8          3
Meta-Harness__Claude-Opus-4.6      89     445  76.4%   ±2.5   ±7.5          3.5
Judy__Claude-Opus-4.6              89    441*  72.1%   ±2.7   ±7.8          3.8          4

rerun        how far the score moves when the same tasks run again
tasks        how far the score moves on a new sample of similar tasks
noise floor  2 identical runs differ by less than this, 19 times in 20
*            tasks have different numbers of trials; each task has equal weight
```

The harness moves the score of the same model from 62.9% to 76.4%. Is Meta-Harness better than
Terminus-KIRA?

```
$ tidegauge compare jobs/meta-harness jobs/terminus-kira
       baseline  candidate  difference
-----  --------  ---------  ----------
score     76.4%      74.7%    -1.7 pts

question                  95% interval           p  sign-flip p  verdict
------------------------  ----------------  ------  -----------  ------------
rerun: same tasks again   -5.3 to +1.9 pts  0.3562               not resolved
tasks: new similar tasks  -6.9 to +3.5 pts  0.5220       0.5598  not resolved

per task: candidate higher on 14 (0 in every trial), baseline higher on 16 (2 in every trial), 59 equal
tasks verdict: the paired t-test (p) and the sign-flip test (sign-flip p) must both give p < 0.05
this design detects a true difference of about 5.1 pts (rerun) or 7.4 pts (tasks)

audit:
  warn  condition-mismatch: The arms ran under different conditions: environment is ['runloop'] in
        Meta-Harness__Claude-Opus-4.6 and ['daytona'] in Terminus-KIRA__Claude-Opus-4.6.
```

The data cannot tell. 445 trials per arm detect a difference of about 5.1 points, not 1.7. The 2 arms also
ran in different sandboxes. `tidegauge plan` gives the price of an answer:

```
$ tidegauge plan jobs/meta-harness jobs/terminus-kira --detect 0.02
To detect 2.0 pts on the same tasks: 31 attempts per task, 2,759 trials per arm
(about $5,068 and 568 trial-hours at the pilot's mean per trial).
For new similar tasks, no number of reruns detects less than 5.4 pts with 89 tasks. Below that, add tasks.
```

What does the better score cost? `tidegauge context` reads the trajectory of each trial:

```
$ tidegauge context jobs/terminus-2 jobs/terminus-kira jobs/meta-harness
arm                             trials  usable  calls   input   peak  re-sent  cached  drops  $/trial  $/pass  s/call
------------------------------  ------  ------  -----  ------  -----  -------  ------  -----  -------  ------  ------
Terminus2__Claude-Opus-4.6         445     445   10.0   78.4k  13.2k    93.1%   91.0%      0    $0.37   $1.04    20.7
Terminus-KIRA__Claude-Opus-4.6     445     442   28.0  618.6k  37.4k    96.8%   96.2%      0    $1.02   $2.15    14.8
Meta-Harness__Claude-Opus-4.6      445     445   38.0  755.0k  36.6k    97.8%   97.4%      0    $1.01   $2.69     7.4
```

The 2 stronger harnesses make 3 to 4 times more model calls and send 8 to 10 times more input tokens
for each trial. More than 93% of the input is context that the call before already sent, and the
prompt cache serves almost all of it. A passed trial costs $1.04, $2.15 and $2.69. No arm compacts
its context. The command also reports numbers that do not add up: for a 4th harness it finds that
the adapter counts cached tokens twice, so the token figures of that arm are about 2 times too high.

## What 32,803 public trials show

`scripts/fetch_tb2_leaderboard.sh` downloads the 75 public submissions (dataset commit `572b2614`).
[docs/findings/terminal-bench-2-leaderboard.md](docs/findings/terminal-bench-2-leaderboard.md) has
the commands and the full tables. In short:

- **Rerun noise is about ±2.6 points** for one submission (89 tasks, 5 attempts each, 95% interval).
  Two identical runs differ by up to 3.7 points.
- **Neighbors in the ranking are 0.45 points apart** (median). 3 of 74 neighbor pairs are separated
  from rerun noise. The 95% rank interval of a typical submission covers 9 positions.
- **A gap below 3 points is almost never separated (2 of 287 pairs). Gaps of 5 points or more are
  separated in more than 9 of 10 pairs.** Between 3 and 5 points the answer depends on the pair.
- **The counting rule moves 25 of 75 scores by 0.5 points or more.** Harbor's job mean leaves out
  a trial without a valid reward. The Terminal-Bench leaderboard counts it as a failure. The
  largest difference for 1 submission is 32 points (71.8% against 39.8%).
- **The audit finds 3 errors and 38 warnings.** Examples: 3 trials of Harbor's reference-solution
  agent inside 1 submission, a job with 130 of 445 planned results, 1 submission with an agent
  timeout multiplier of 9, 6 submissions that mix models, agents or sandboxes, and 6 different sets
  of task checksums.

These are observations about files. They do not say why a file is the way it is.

## The 2 error bars

Most tools print 1 error bar, and most readers use it for the wrong question.

- **rerun.** The tasks are fixed. Only the luck of each attempt changes. Use it for "did my change
  do anything on this benchmark?". It needs 2 or more attempts per task. Its formula is the one
  that the Terminal-Bench leaderboard uses for its standard error.
- **tasks.** The tasks are a sample of the work you care about. Use it for "is this agent better on
  work like this?". It is wider, and more attempts cannot shrink it below a floor. Only more tasks can.

`compare` answers both. For the rerun question on pass or fail rewards, with the same number of
trials per task in both arms, it uses an exact stratified permutation test: no approximation and no
random seed. Otherwise it uses a t-test with within-task variances. For the tasks question, a
difference is resolved only when the paired t-test and a sign-flip test both agree: with few tasks
the t-test alone claims too much. [docs/STATISTICS.md](docs/STATISTICS.md)
derives every formula, reports measured error rates, and records what an independent review found.

## Counting rules

`--trials valid` (default) counts the trials with a valid reward, as Harbor's job mean does.
tidegauge reads Harbor's `reward_valid` field, and rebuilds it for results from older Harbor versions.
`--trials all` counts every trial and scores a missing reward as 0, as the Terminal-Bench leaderboard does.
When the 2 rules disagree by 0.5 points or more, every command says so.

## A gate for CI and for agent loops

```sh
tidegauge gate jobs/baseline jobs/candidate --margin 0.02
echo $?   # 0 ok · 1 regressed · 2 undecided, run more trials · 3 the data is not usable
```

The gate decides from the interval, so "no proof of a regression" is not a pass. A decision that
claims a difference also needs the test to agree. The gate decides under both counting rules and
says `undecided` when they disagree. It refuses data with audit errors.
`--metric cost_usd --lower-is-better` gates on cost. `--json` gives the full report to a program.

An agent that changes a harness can propose. The gate decides, and it is not open to persuasion.

## Teams of agents: headcount and coordination

The `olamlabs/harbor-multiagent` fork of Harbor runs several agents in 1 trial, 1 agent per seat.
tidegauge reads these trials:

- The identity of an arm is its roster, for example `p1=terminus-2,p2=oracle`.
- Tokens and cost are the sums over the seats. If a seat that used a model reports no cost, the trial
  has no cost. tidegauge does not show the cost of a part of a team.
- `context` adds up the trajectories of the seats. `--reward-key seat.p1` scores the credit of 1 seat,
  when the verifier writes it.

A team can score higher because it has more agents, or because its agents coordinate. `decompose`
separates the 2 parts. It takes 3 arms that ran the same tasks: 1 agent (solo), the team without
communication (indep) and the team with communication (team). Then

```
team - solo = (indep - solo) + (team - indep)
total       = headcount      + coordination
```

Bespoke Labs' Multi Agent Coordination Bench measures teams with this identity. The numbers in the
example are synthetic: `tests/test_decompose.py` builds the 3 arms and checks this output.

```
$ tidegauge decompose jobs/solo jobs/indep jobs/team
metric: reward · 20 shared tasks · only trials with a valid reward count · 95% intervals

arm    seats  tasks  trials  score  rerun  tasks  $/trial  cost ratio
-----  -----  -----  ------  -----  -----  -----  -------  ----------
solo       1     20      60  40.0%   ±9.5  ±19.3  $0.0500        1.00
indep      2     20      60  70.0%  ±10.0  ±16.7  $0.1000        2.00
team       2     20      60  75.0%   ±8.9  ±16.7  $0.1300        2.60

effect        difference  rerun 95% (pts)        p  verdict       tasks 95% (pts)       p  sign-flip p  verdict
------------  ----------  ---------------  -------  ------------  ---------------  ------  -----------  ------------
headcount      +30.0 pts  +16.8 to +43.2   <0.0001  resolved      +15.8 to +44.2   0.0003       0.0005  resolved
coordination    +5.0 pts  -7.8 to +17.8     0.6226  not resolved  -13.4 to +23.4   0.5770       0.7070  not resolved
total          +35.0 pts  +22.6 to +47.4   <0.0001  resolved      +19.4 to +50.6   0.0002       0.0005  resolved

seats: agents in 1 trial · $/trial: mean cost of 1 trial · cost ratio: $/trial over the $/trial of solo
headcount = indep - solo · coordination = team - indep · total = team - solo = headcount + coordination
rerun: the same tasks run again · tasks: a new sample of similar tasks · resolved: p < 0.05 for that question
For tasks, the paired t-test (p) and the sign-flip test (sign-flip p) must both give p < 0.05.
Headcount and coordination share the arm indep, so their 2 estimates are negatively correlated.
Each interval is valid on its own. The table makes no joint claim.

More agents without communication add 30.0 pts (headcount, resolved for rerun and tasks).
Communication adds 5.0 pts to the same agents (coordination, not resolved).
The team with communication is 35.0 pts above 1 agent (total, resolved for rerun and tasks).
Coordination gives 14.3% of the total gain and headcount gives 85.7%. The coordination effect is not resolved, so its share can be 0%.
This design detects a true coordination effect of about 18.0 pts (rerun) or 25.9 pts (tasks).
```

In this example a second agent adds 30 points, and a trial costs 2 times more. Communication adds 5
points, and 60 trials per arm cannot tell 5 points from 0: this design detects about 18 points. The
names of the arms are free, and the position gives the role. Tasks pair by name. `decompose` uses only
the tasks that all 3 arms have, so the 2 parts add up exactly. It gives the share of coordination
only when the total is resolved. [docs/STATISTICS.md](docs/STATISTICS.md) section 13 derives the numbers.

Some experiments put the condition in the task name, as in `flotilla/schedule-009-solo`, `-silent` and
`-talk`. `--task-key '^(.*)-(?:solo|silent|talk)$'` gives the 3 names 1 key: the first group of the
pattern, here `schedule-009`. Every command that reads arms takes `--task-key`. A trial whose name does
not match keeps its key, and the note `unmatched-task-key` counts it. Across arms, task checksums are
compared only between trials with the same name. Inside 1 arm, 2 names under 1 key are still a finding.

## Why this is not inside Harbor

Harbor 0.26.0 reports a mean, pass@k and a comparison grid of averages. It has no interval, no test
between 2 runs and no sample-size answer. Statistics pull requests #517, #2361 and #2714 are open
since January, July and August 2026. Harbor's job is to run trials well. What a result means is a
separate question with its own opinions, and a small tool with no dependencies can hold them.

## Limits

- A pass rate says nothing about a broken task or a weak grader. tidegauge measures noise, not validity of tasks.
- The rerun interval is approximate (Student's t with Satterthwaite degrees of freedom). With 2
  attempts per task it is wider than needed. Without any trial noise in the data there is no interval.
- `rank` does not correct for the number of pairs. Read "separated" as 1 test per neighbor pair.
- `decompose` makes no joint claim about its 3 effects, and the share of coordination has no interval.
  The names of the effects say what the 3 arms are meant to isolate. tidegauge sees only the arms.
- No test here allows you to look after each batch and stop when you like the answer. A sequential
  gate is on the backlog.
- Harbor deletes the directory of a retried attempt. tidegauge reports the retry count and cannot
  see those attempts.

## Development

```sh
uv sync && uv run pytest -q && uv run ruff check .
```

The kernel (`model.py`, `load.py`, `stats.py`, `audit.py`) is the only shared code. Each command is
1 module. [AGENTS.md](AGENTS.md) has the rules, and [docs/BACKLOG.md](docs/BACKLOG.md) lists cards
that can be built in parallel, each with its own files.

## License

Apache-2.0. tidegauge is an independent project. It is not part of Harbor or Terminal-Bench.
