# Eval Lab experiments, with Reef as an optional tool

**Owner:** Reef <> EvalLab. **Work queue:** [HAR-150](https://linear.app/petermakhnatch/issue/HAR-150).
**Status:** Lab-first evidence/comparison work; Reef contribution work is parked.

Peter's October 1 scope correction supersedes the earlier two-repository
contribution plan: improve Trace Lab/Eval Lab without making Reef maintenance
or upstream review a prerequisite. No further Reef/SDK commits or submissions
are planned, and the former publication approval request is withdrawn.

For October 2's overnight work, Peter authorized building and optional runs with
a **$10 maximum**, not a spending target. HAR-150 uses local CPU controls and
already-published evidence; no paid experiment launches are planned. Its two
targets are small-sample paired rankings and references lost from embedded ATIF
documents. Existing run, ownership, isolation and deployment gates still apply.

Local verification is recorded in [HAR-150's receipt](har150-receipt.json):

- Exact enumeration of the 16 equiprobable two-task Bernoulli(.5) null outcomes:
  erroneous rankings dropped from **2/16 to 0/16**. This is a small synthetic
  control, not evidence of model improvement or a general error-rate estimate.
- An embedded child's valid continuation now produces all **3 documents/steps**
  instead of silently losing the third; containment and cycle controls remain.
- **20 published HAR-116 trials** replayed with unchanged existing projections
  and source JSON hashes. **85 focused tests passed**, and the actual curve CLI
  refuses the previously ranked two-task comparison.
- External experiment spend: **$0**. No model calls, cloud launches or new agent
  trials were needed. Coding-session invoice totals are not exposed by the
  token-accounting helper and are not claimed as measured dollars.

## Retained protocol study (HAR-138, parked)

This earlier CPU-only replay proved a local SDK/service contract, not improved
agent behavior. It is retained evidence, not a prerequisite to Lab-native work:
[`replay_har138_flow.py`](replay_har138_flow.py).

It imports one saved Harbor trial as a labelled trajectory **projection**
(counts, hashes, saved reward; never the original provider request, logprobs,
or prompts) through the real SDK over local HTTP, drives
import -> report -> retry idempotency, record/report linkage, same-id conflict
(409) and foreign/missing reference (400) refusals, then exercises a
deterministic harness-update fixture: proposal -> selection -> publication ->
pinned pull with release/content identity and hashes, plus unknown-release
(404) refusal. Unscored trials refuse; reports stay unpinned
(`x-reef-release-id` binds the scenario base, never an evolved head).
Exact invocation with the Reef worktree's Python, from this worktree's root:

```bash
/Users/petermakhnatch/Developer/reef/.worktrees/lab-flow-release-contract/.venv/bin/python \
  research/experiments/reef/replay_har138_flow.py \
  --trial ~/Developer/eval-lab-results/2026-10-01/HAR-116-har116-a-002864-loopfix-r2 \
  --out <new-path>/har138-flow-receipt
```

`--trial` takes a trial directory or a published Harbor job holding one nested
trial; the aggregate job `result.json` is not mistaken for the trial result.
Ambiguous jobs and missing/invalid rewards refuse. `--out` must not exist.
Output is `receipt.json` (source hashes, protocol checks, fixture decisions,
implementation hashes and limits), `pull-head/`, and `pull-pinned/`.
No raw private prompts are copied into the receipt.

### Source and publication boundary

The replay requires the repaired SDK source at local commit
`fff27d59d75ebdf347ab83001f5bf9f3f438de2b`, editable-installed from the owned Reef
worktree's `third_party/reef-client`. Its package metadata still says `0.2.1`;
this is **not** a new published release, and installing PyPI `0.2.1` does not
install the repair. The replay checks the release-ID API and refuses an
incompatible SDK rather than substituting a raw-HTTP implementation.

The SDK is a separate upstream repository. A fixed publication is needed for
normal upstream package adoption, but **not** for using this existing patched
snapshot locally. The local Reef/SDK commits are not merged upstream; public
contribution work is parked rather than waiting on an approval from Peter.

### Observed repair and checks

The shipped SDK asked for `artifact_version`/`?version=` while Reef serves
`release_id`/`?release_id=`. An actual local HTTP pull failed with
`KeyError: 'artifact_version'` after already writing a file. The repaired SDK
pull returns the real release ID and writes the matching release/content receipt.
It also uses the current catalog, fixes skill release identity, and sends the
correct base-binding header. Reef's local test-only `_ReleaseClient` is removed;
the service tests now exercise the actual SDK.

- SDK tests: **20 passed**.
- Focused real Reef service regressions: **10 passed** (historical pull, catalog,
  byte fidelity, newer-file pruning, and base-binding refusal included).
- Full protocol replay: **12 checks passed**, using saved trial
  `har116-a-002864-loopfix-r2__CWRe7mJ` and preserving its reward `1.0`.

The fixture's second step uses a separate, explicitly synthetic inference/report;
it does not rewrite or double-count the saved trial reward. Fixture publications
are deliberately selected by a marker-count scorer, **not measured agent
improvements**. No model, Harbor trial, cloud sandbox or local LLM ran.

The [curated receipt](har138-receipt.json) records the executed source hashes and
limits. CLI refusal checks also passed for the incompatible stock SDK, an
unscored trial, an ambiguous multi-trial job, and an existing output directory
(the existing receipt stayed byte-identical).

Prepared local revisions: SDK `fff27d59d75ebdf347ab83001f5bf9f3f438de2b`;
Reef integration `b64254f55f76ed0c79fa06a2db61cbeb10131014`. The locked Reef
worktree preserves optional research material; it is not a deployed shared
runtime or an adopted dependency for the current Lab work.

## Superseded campaign (ideas retained, not current)

The failure-evidence learning loop below was the previous immediate program.
It is superseded by HAR-138 above; its ideas stay as later research options,
and none of it is authorized spend or a live experiment.

### 1. Failure evidence into a reusable harness improvement

Pick one recurring failure family from existing, eligible development runs.
Freeze the target model and baseline harness. Compare a Reef-generated change
with the unchanged agent and one simple, frozen hand-written rule. The first
controlled contrast is **score-only feedback versus concrete failure
evidence**, with the same proposer, candidate allowance and evaluation budget.
Measure earned task success, regressions, tokens, and total
proposal-plus-evaluation cost. Inspect the mechanism on a small pilot first.

### 2. Test whether a second cycle compounds or overfits

Only after one useful cycle: retain its accepted change and learn on a second
failure family. Compare with restarting from the original harness, and re-check
the first family. Freeze the check set before either cycle.

### 3. Let the experiments choose the code work

Fix a named bottleneck in the repository that owns it. Weight training is a
later option if a fixed-model harness cannot address the observed limitation.
No large sweep, general-purpose memory subsystem, or GPU training campaign is
the starting deliverable.

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
  rollouts or an improvement.

## Execution and receipts

Bounded CPU development, offline analysis and native-worker delegation proceed
independently. Peter still approves model/cloud/GPU experiments, downloads,
shared-runtime deployment, task admission, publication, and new persistent agents.
No local LLM inference. Finished runs use Eval Lab's existing results home
(`~/Developer/eval-lab-results/`), not a new store. Scored ceiling stops remain
real outcomes; missing scores and unknown costs are not zero.
