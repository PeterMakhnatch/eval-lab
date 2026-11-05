# Frozen evaluation preparation

These tools prepare and score the admitted ov1106-impscale cohort. They never tune thresholds or change the production line.

## Gates and ownership

The parent owns admission, Git, CI, preregistration publication, and `FROZEN.json`.
No scoring starts before the parent commits and pushes the assets and writes the freeze.
No paid APIs or induced agent trials run through these tools.

`ADMITTED.json` binds `admitted: true`, `transport_sha256`, `cohort_manifest`,
`cohort_manifest_sha256`, `canary_receipt`, and `canary_receipt_sha256`.
The canary receipt must contain `passed: true`.
This check consumes the parent's admission. It is not a second containment certificate.

`FROZEN.json` requires:

```json
{
  "commit": "full 40-character pushed Git commit",
  "pushed": true,
  "prereg_sha256": "SHA-256 of PREREG.md",
  "cohort_manifest_sha256": "same as ADMITTED.json",
  "assets": {"repository-relative path": "SHA-256"},
  "inputs": {"absolute private input path": "SHA-256"}
}
```

Include every `contract.json` source entry and every evaluation `.py` file in `assets`.
Also include `PREREG.md`, `README.md`, `contract.json`, and `sample.json`.
Copy `inputs` exactly from the private index named in `sample.json`.
The gate checks the index digest, every input digest, and every asset against the committed bytes.
The parent attests that the commit is pushed.
Do not commit raw views, prompts, transcripts, command excerpts, or judge replies.

## Prepared labels and views

The admitted sample has 151 runs. The source-action audit selects 53 positives and 27 negatives.
It excludes 71 rows. Among 34 original passes, 6 change existing assertions or fixtures.
One more original pass has an unresolved fixture preimage and uses a version shim.
Neither passing rewards nor a clean final diff establishes honesty.
The historical evidence has no separate complete original/final test-tree snapshots.
The audit uses all bound actions, restored-test execution, and the post-restoration diff.

The prepared views reconstruct tool observations from bound sessions and remove hidden reasoning.
The task text is the generic make-tests-pass prompt, not the full task specification.
The diff comes after test restoration, not at submission. Every view states this source limit.

All paths below use this private helper root:
`/private/tmp/ov1106/impscale/helpers/scoring`.
Existing output directories are preserved. Preparation commands require a fresh output directory.

```bash
# Admission only; no detector scores or model calls.
uv run python research/experiments/ov1106-impscale/evaluation/prepare.py \
  --out /private/tmp/ov1106/impscale/helpers/scoring/set

uv run python research/experiments/ov1106-impscale/evaluation/audit_labels.py \
  --packets /private/tmp/ov1106/impscale/helpers/scoring/set/audit-packets.jsonl \
  --positive-targets /private/tmp/ov1106/impscale/helpers/scoring/set/positive-targets.md \
  --out /private/tmp/ov1106/impscale/helpers/scoring/audit

uv run python research/experiments/ov1106-impscale/evaluation/native_line.py prepare \
  --runs /private/tmp/ov1106/impscale/helpers/scoring/set/judge-runs.jsonl \
  --out /private/tmp/ov1106/impscale/helpers/scoring/native-line-v2
```

These outputs already exist. Their hashes appear in the private frozen input index.
`RESUMED-READY.md` records the validated counts and preparation evidence.

## Scoring after the committed freeze

```bash
uv run python research/experiments/ov1106-impscale/evaluation/evaluate.py gate

uv run python research/experiments/ov1106-impscale/evaluation/judge_batch.py \
  --judge j3 \
  --runs /private/tmp/ov1106/impscale/helpers/scoring/set/judge-runs.jsonl \
  --out /private/tmp/ov1106/impscale/helpers/scoring/judge-out

uv run python research/experiments/ov1106-impscale/evaluation/judge_batch.py \
  --judge j2b \
  --runs /private/tmp/ov1106/impscale/helpers/scoring/set/judge-runs.jsonl \
  --out /private/tmp/ov1106/impscale/helpers/scoring/judge-out

uv run python research/experiments/ov1106-impscale/evaluation/l0_batch.py \
  --runs /private/tmp/ov1106/impscale/helpers/scoring/set/judge-runs.jsonl \
  --out /private/tmp/ov1106/impscale/helpers/scoring/signals \
  --raw /private/tmp/ov1106/impscale/helpers/scoring/l0-raw

uv run python research/experiments/ov1106-impscale/evaluation/collect.py \
  --runs /private/tmp/ov1106/impscale/helpers/scoring/set/judge-runs.jsonl \
  --judge-out /private/tmp/ov1106/impscale/helpers/scoring/judge-out \
  --out /private/tmp/ov1106/impscale/helpers/scoring/signals

uv run python research/experiments/ov1106-impscale/evaluation/native_line.py run \
  --out /private/tmp/ov1106/impscale/helpers/scoring/native-line-v2

uv run python research/experiments/ov1106-impscale/evaluation/evaluate.py report \
  --audit /private/tmp/ov1106/impscale/helpers/scoring/audit/labels.jsonl \
  --signals /private/tmp/ov1106/impscale/helpers/scoring/signals \
  --line-decisions /private/tmp/ov1106/impscale/helpers/scoring/native-line-v2/line-decisions.jsonl \
  --out /private/tmp/ov1106/impscale/helpers/scoring/report.json
```

Each judge launcher saves fresh quota before any model call. It refuses the program's quota ceilings.
J3 correctly uses the same frozen V2 rubric as J2b. The earlier freeze explicitly records this fact.
Only the route changes. See `ov1104-judges/FREEZE.md:42-47`.
The lane-local gateway includes the required nofallback config and checks exact served-model equality.
A missing successful-response attestation or wrong route stops the whole batch.
The runner refuses result overwrites. For a deliberate new attempt, use a separate output root and retain failures.
`collect.py` preserves the frozen maximum-successful-window score and reports failed windows separately.

`l0_batch.py` keeps native raw reasons. It emits zero only from a successful native result with no matching reason.
Timeouts and runner errors remain abstentions. It never calibrates on this cohort.

## Native line prerequisites and limits

`native_line.py` calls the real `evallab.line.core.run_line` with admitted, label-free bundles.
The preparation intake parses all 151 bundles, task IDs, actions, observations, and diffs.
It does not compute a detector decision before freeze.

The standard replay spec supplies the actual task policy and audit seed.
Its default policy passes uncertified tasks; it does not review every uncertified task.
No constructed task receives a fabricated certification.
The staged cache has no AutoReview readings. Native code reports `AUTOREVIEW_CACHE_MISS`
for eligible rows and `AUTOREVIEW_NOT_ELIGIBLE` otherwise.
The judge queue is isolated and read-only. Station verdicts are empty.

These are native categorical decisions under missing downstream evidence.
They are not a complete deployed-model performance result.
The report states this operating condition and counts missing-reading reasons.
Fresh AutoReview readings need a separately frozen input set and route-safe execution.
Never substitute J2b, invent CLEAN verdicts, disable AutoReview to imply success, or infer missing keeps.
Never convert keep/review/drop into ordinal AUROC.

The parent runs final tests, lint, and build checks once. Preparation uses only local intake and synthetic gateway checks.
No preparation output is a detector performance number.
