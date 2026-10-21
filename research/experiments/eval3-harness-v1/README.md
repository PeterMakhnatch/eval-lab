# Eval3 policy on retained QA outputs

This adapter uses Tokenless's public judge policy and **unmodified official
ranking implementation**, pinned to
[`e15a35a1da4237bf5dd2e664725aae66c207d65c`](https://github.com/tokenless-ai/eval3-grader/tree/e15a35a1da4237bf5dd2e664725aae66c207d65c).
The dependency is MIT-licensed, copyright 2026 Tokenless. No upstream implementation
is vendored here.

It is **not the official 65-environment Eval3 benchmark**. That repository ships
policy/scoring code, not the task corpus, canonical findings, baseline reports,
or model-execution pipeline. This adapter supplies a bounded execution pipeline
for our retained reports. Its scores describe a local DEV comparison, not a
Tokenless leaderboard position or a second independent dataset.

## Input: any harness, one explicit identity contract

The auditor is a configuration label, not necessarily a model name. v1, v2 and
cheap-model runs use the same input contract: original authored output files,
retained trace files if available, a real execution state, and the original
package digest. Do not convert a missing output into an empty report or reuse a
model name across changed configurations.

`prepare` consumes a configuration with:

- `study_id`, the declared `cases` and `auditors`;
- `source_view`, an existing sealed `tb4.audit.agent-view/v1` DEV export;
- `trials`, each declaring `run_id`, `trial_id`, `auditor`, `case`, `state`,
  `task_digest`, `authored_files`, `trace_files`, `source_only`, and optional
  `reported_model`.

Valid states are `completed`, `limit_reached`, `failed`, `pending`, and `ungraded`.
A completed trial requires retained authored output, even a report declaring no
findings. A missing auditor/task observation is explicitly planned/pending.
Several real repeats may be supplied; repeat averaging remains upstream policy.
`reported_model` is provenance, not independent provider verification.

Paths in the configuration identify existing local files. The preparation step
checks the view's identity inventory and every selected source-file hash, refuses
TEST views and mismatched trial digests, and copies evidence without changing the
originals. Binary source files retain their hashes and an explicit unreadable
status; they are not silently decoded into invented text. Never put the answer
key, PM labels, witness programs, or scorer output in authored/trace inputs.

For a tool-using harness, supply the original session summary plus its recorded
finding files as authored output, and the original tool journal as trace evidence.
For a static reader, supply its original report and an empty trace list with
`source_only=true`. Where a legacy report has only an opaque task ID, preserve the
identity-map provenance; do not claim that the report itself recorded a digest.

## Run

Install into an isolated Eval Lab worktree's environment:

```sh
uv sync --locked
uv pip install --python .venv/bin/python \
  -r research/experiments/eval3-harness-v1/requirements.txt

uv run --no-sync python research/experiments/eval3-harness-v1/grade.py prepare \
  /path/to/study-config.json --out runs/eval3-study

keys run -- uv run --no-sync python research/experiments/eval3-harness-v1/grade.py grade \
  runs/eval3-study --out runs/eval3-grade \
  --model openrouter-metered/z-ai/glm-5.3-flash \
  --budget-dir runs/eval3-budget --max-usd 1 \
  --approve 'Current, explicitly approved study and dollar cap'
```

`prepare` makes no model calls. `grade` is paid and requires explicit approval.
This lane refuses a cap above $1; that ceiling is not standing spending permission.
Use the **same budget directory across invocations**. The existing lifetime ledger
reserves full-request UTF-8 bytes plus framing and maximum output before each
request, never refunds reservations, and durably stops after usage-bound breaches.
Known token usage is a separate cost estimate, not a reconciled invoice. Missing
usage is never reported as free. No provider fallback or automatic retry occurs.
Existing output directories are refused rather than overwritten.
The judge fingerprint includes the adapter's source digest. A deliberately changed
implementation or judge configuration therefore cannot reuse an old approval.
Preserve every earlier ledger; a replacement configuration needs a new budget
whose ceiling is at most the original authorized cap minus **all earlier reserved
charges**, not merely their smaller observed token-cost estimates.

## Judgment and scoring

1. Extract final active allegations from authored output only. Source browsing is
   unavailable during this stage; withdrawals and non-findings remain distinct.
2. Validate the frozen allegations against pinned source and retained traces.
   The model has only the existing sealed `WorkspaceTools` read/grep/diff
   capabilities: no shell, network, execution, or arbitrary host-file access.
   A support citation must refer to actual complete lines independently read by
   the judge. A source-only report cannot acquire an executed-replay label.
3. Link supported findings by the same causal assessment defect. The result must
   partition every supplied finding exactly once. Uncertain equivalences remain
   separate and the affected trials remain unresolved under strict scoring.
4. Give each issue five fresh, blinded importance judgments and take their ordinal
   median. Discovery count and auditor identity are not supplied to this stage.
5. Call `eval3_grader.rankings.rank_catalog` with the real task/trial matrix,
   400 environment-bootstrap draws, fixed seed 20261021, and the optional
   unregularized fits. Pending, failed, incomplete and unresolved observations
   stay missing; the adapter never manufactures a clean empty observation.

The output contains the prepared study, source/report identities, all request and
response records, stage judgments, judge configuration fingerprint, canonical
`catalog.json`, official `rankings.json`, and a receipt. Exit status is nonzero
when grading remains provisional or any stage failed; retained partial evidence
is still available for inspection.

The headline relative method is `fractional_bradley_terry_importance`, using
1/3/6 importance and shared-issue cancellation. Default regularization is the
upstream 0.25; the unregularized variants report separation or missing finite
estimates instead of inventing extreme Elo. Read exclusions, informative-match
counts and graph identification alongside any ranking. A small DEV comparison
cannot support a general model-leaderboard claim.

## Comparability

The public default judge is GPT-6 Luna with high reasoning and a 64,000-token
output allowance. This adapter defaults to the existing pinned OpenRouter
GLM-5.3-Flash route, medium reasoning and 6,000 output tokens, with a read-only JSON
action transport. Those are disclosed configuration differences. The published
policy explicitly permits another judge, but only auditor outputs graded with
the same frozen judge configuration and compatible task universe are comparable.
The public `tb4-audit-v1` key is not used to manufacture judgments or issue credit.
A source-supported claim is not a newly executed exploit or task certification.

GPT-OSS-120b failed the retained-cohort transport qualification, including
non-text provider responses and invalid citations; it did not produce usable
auditor rankings. GLM is an explicitly selected new judge configuration, not an
automatic fallback or a mixture of partially successful GPT-OSS judgments.
