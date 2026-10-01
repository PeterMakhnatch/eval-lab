# Reef: learning from agent experience

**Owner:** Reef <> EvalLab. **Work queue:** [HAR-138](https://linear.app/petermakhnatch/issue/HAR-138).
**Status:** SDK repair and CPU protocol replay verified locally; public SDK/upstream delivery needs approval. No live experiment is authorized by this page.

This remains regular, scoped work in **both repositories**: repairs and reusable
client/recipe behavior in Reef; experiment inputs, replay tools and evidence in
Eval Lab. Use owned branches and PRs, not changes in another writer's checkout.
Source delivery is separate from permission to run paid infrastructure.

## Current task (HAR-138): prove the Lab-evidence -> Reef -> served-harness flow

Before any new learning loop, the release contract between Lab evidence and a
served Reef harness must work end to end. The deliverable is a runnable
CPU-only replay, not a fake live experiment:
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

### Source and approval gate

The replay requires the repaired SDK source at local commit
`fff27d59d75ebdf347ab83001f5bf9f3f438de2b`, editable-installed from the owned Reef
worktree's `third_party/reef-client`. Its package metadata still says `0.2.1`;
this is **not** a new published release, and installing PyPI `0.2.1` does not
install the repair. The replay checks the release-ID API and refuses an
incompatible SDK rather than substituting a raw-HTTP implementation.

The SDK is a separate upstream repository. Its publication, maintainer review
and PyPI release are prerequisites to normal Reef dependency adoption. Local
Reef/SDK commits are prepared and tested, not claimed merged upstream.

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
Reef integration `b64254f55f76ed0c79fa06a2db61cbeb10131014`. The Reef worktree is
locked while the unpublished SDK dependency awaits review; it is not a deployed
shared runtime.

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
