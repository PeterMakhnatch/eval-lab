# Reef: learning from agent experience

**Owner:** Reef <> EvalLab. **Work queue:** [HAR-125](https://linear.app/petermakhnatch/issue/HAR-125).
**Status:** active research and implementation; no live experiment authorized by this page.

Can a small, affordable loop produce reusable agent improvements, rather than
just more iterations or a lucky score? This is the current home for that work.
It is linked from Eval Lab's root README; `research-context/reef` is historical
reference material, not a second active plan.

## Work in both repositories

Peter's September 30 instruction, clarified in the next message: contribute to
**both Eval Lab and Reef regularly**, while keeping the work modular. Independence
means limited file overlap, not avoiding merges into the main repositories.

| Repository | This track contributes | Keep out of the critical path |
|---|---|---|
| Eval Lab | Reproducible study inputs, analysis, results, and narrow shared interfaces demanded by an experiment | Unrelated runner/queue/catalog refactors; another scheduler or dashboard |
| Reef | Learning recipes, feedback handling, harness changes, and reproducible fixes or examples exposed by the studies | A second evaluation platform or a new training stack before it is needed |

Use owned worktrees and small named-branch PRs; follow each repository's contribution
and CI rules through merge and verification. Do not switch or overwrite another
writer's checkout. Do not manufacture a code change in both repos for every study.
Reef fork development is in scope; public upstream submission/release remains a
separate human-reviewed decision under Reef's contribution policy.

## Next: one useful learning loop, deeply examined

### 1. Failure evidence into a reusable harness improvement

Pick one recurring failure family from existing, eligible development runs.
Freeze the target model and baseline harness. Compare a Reef-generated change
with the unchanged agent and one simple, frozen hand-written rule. The rule is
an important control: an elaborate loop that cannot beat a cheap static change
has not earned its complexity.

The first controlled contrast is **score-only feedback versus concrete failure
evidence**, with the same proposer, candidate allowance and evaluation budget.
Use only task-visible evidence; never feed hidden verifier answers to the proposer.
Measure earned task success, regressions, tokens, and total proposal-plus-evaluation
cost. First inspect the mechanism on a small pilot; then decide whether replication
and untouched tasks are worth funding. Do not call a few successful examples a
statistically established improvement.

### 2. Test whether a second cycle compounds or overfits

Only after one useful cycle: retain its accepted change and learn on a second
failure family. Compare with restarting from the original harness, and re-check
the first family. This tests retention and regression, not merely whether Reef can
publish a second version. Freeze the check set before either cycle; viewed tasks
are development data, not a fresh final holdout.

### 3. Let the experiments choose the code work

Fix a named bottleneck in the repository that owns it: proposer feedback and
learning behavior in Reef; evaluation inputs and result consumption in Eval Lab.
Weight training is a later option if a fixed-model harness cannot address the
observed limitation. No large sweep, general-purpose memory subsystem, or GPU
training campaign is the starting deliverable.

## Reuse rather than collide

- [Existing Reef pool and gate evidence](../reef-loop-pool-20260925/README.md): keep
  its historical inputs and results intact. HAR-73/PR #471 owns the pending live
  Lab evaluator; HAR-74/PR #462 owns the pending traffic bridge. Neither is implied
  merged or live by this page.
- [Gate rules](../../../library/adapters/reef_gate/src/evallab_reef_gate/rules.py)
  and [power calculations](../../../src/evallab/power.py) already exist. Reuse them;
  do not invent another gate. Reef itself stays out of Lab's core imports.
- [HAR-116 results](../har116-loopfix-leak/RESULTS.md) and HAR-120's task cohort remain
  Engineering-owned. Their results are reusable; their spend approvals are not.
- [HAR-124](https://linear.app/petermakhnatch/issue/HAR-124) exercised current Reef
  selection on 60 saved no-change comparisons: 3/30 and 11/30 selections, exactly
  reproducing the old decisions. It was an offline feasibility check, not new
  rollouts or an improvement. Three pairings cannot clear a one-sided 0.05 sign
  gate even with three wins (p = 0.125); tiny budgets constrain what can be claimed.

## Execution and receipts

Bounded CPU development, offline analysis and native-worker delegation proceed
independently. Peter still approves model/cloud/GPU experiments, downloads,
shared-runtime deployment, task admission, publication, and new persistent agents.
No local LLM inference. Before requesting spend, pin the models, tasks, harnesses,
splits, attempts including failures, expected cost, hard cap, abort rule, teardown,
and execution commands. Use existing Eval Lab authorization and execution paths;
missing integration is an explicit dependency, not permission to bypass them.

Each study here carries its runnable inputs and a short result: what actually ran,
what changed, what failed, and the next decision, with immutable run references.
Finished runs use Eval Lab's existing results home (`~/Developer/eval-lab-results/`),
not a new store. Scored ceiling stops remain real outcomes; missing scores and
unknown costs are not zero. Offline replay cannot predict how a changed agent acts.

The next deliverable is a frozen failure-feedback case and the smallest runnable
Reef recipe that consumes it, followed by a costed live pilot request. No spending
amount or model selection is implied yet.
