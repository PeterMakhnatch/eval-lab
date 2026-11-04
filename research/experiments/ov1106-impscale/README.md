# Impossible-task detector evaluation and strict coverage

This experiment repeats the failed canary with an explicit container-local working directory.
It then evaluates frozen detectors on verified test edits and original control passes.
The original run evidence stays unchanged.

## Containment

`canary.py` imports the frozen `ov1104-impossible/container_tool.py` by its exact SHA-256:
`f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a`.
The driver adds a prompt instruction: every probe uses `cwd=/workspace`.
It also adds the required no-fallback overlay to the isolated OMP settings.
It does not change the transport, tool registry, container, execution, or cleanup checks.
The driver also rejects a forbidden probe that fails at the working-directory check.

The fresh canary passes. Forbidden probes return `2`, `1`, and `2`.
The workspace probe returns `0` and prints `/workspace`.
The active registry contains only `bash`; the transport rejects every other tool call.
Cleanup succeeds. The receipt SHA-256 is
`97825de69db37d256c936e68175fc98dfc0996e443d2ba8ebccb782842b1d116`.
Private receipt: `/private/tmp/ov1106/impscale/canary-explicit-cwd/receipt.json`.

The resumed scale launch uses a second passing canary with the same transport.
It includes `--config /private/tmp/ov1106/shared/nofallback.yml`.
Its receipt is `/private/tmp/ov1106/impscale/canary-nofallback-v3/receipt.json`,
SHA-256 `d122170082a9972167eeb9fcd463b8b31cb28b4691c4113ed335d494f587cb09`.
All 3 forbidden probes fail at their paths. The workspace probe passes.
The canary uses only Terra and has no fallback event.
Historical admission keeps its first receipt binding.

`admit.py` validates historical receipts, settings, extensions, sessions, artifacts,
actual mounts, models, and the fresh exact-transport canary. It admits 151 runs:
101 variants and 50 original controls over 26 tasks. It excludes 41 runs with another
transport hash before reading their trajectories.

This is the retrospective admission that the current assignment permits.
It does not establish a contemporaneous canary binding for the historical runs.
It does not change or relax the earlier `admission.py` contract.
The historical receipts retain their original canary reference.
The audit also records 30 failed tool calls over 11 runs. Their trusted tracebacks
show failure at container inspection, before the command executes. No host fallback exists.

The historical receipt's `workspace_destination` summary says `/workspace`.
Its actual mount and transport configuration use `/testbed`.
The audit checks the actual mount against the configuration, not the inaccurate summary.

Reproduce the retrospective audit from this worktree:

```sh
python research/experiments/ov1106-impscale/admit.py \
  --transport research/experiments/ov1104-impossible/container_tool.py \
  --runs /private/tmp/ov1104/impossible/work-v2/runs \
  --canary /private/tmp/ov1106/impscale/canary-explicit-cwd/receipt.json \
  --quarantine /private/tmp/ov1104/impossible/workers/safety-audit/quarantine.jsonl \
  --output /tmp/impscale-admission-replay.json
```

## Evidence limits

The historical runner captures `final.diff` **after** it restores the variant's tests.
That diff can hide submitted test edits and includes constructed test changes.
It is not a submitted final diff. Judge reports must state this input limitation.
Original-control honesty needs an audit of source actions, not only this diff.
A submitted-test pass followed by a restored-test failure gives causal evidence
that the submitted test changes affect the grade. The legacy construction alone
is not a formal contradiction certificate.

The `evaluation/` slice owns preregistration, label audits, frozen signals, and metrics.
The `strict/` slice owns broader contradiction construction and executed controls.
Raw transcripts and workspace contents stay outside Git.

## Strict construction coverage

The extension admits 18 of 52 current image-backed DEV candidates: 34.62%.
The Wilson 95% interval is 23.15–48.20%.
It attempts 37 tasks and preserves 84 completed non-agent controls.
The old generator produces 4 tasks, but only 2 have eligible controls.
The new set includes both eligible tasks and adds 16 eligible tasks.
The resumed audit validates 368 JSON files and all available JUnit bindings.
All admitted tasks have 3 valid control receipts and a marked selected-case failure.

Receipts: `/private/tmp/ov1106/impscale/helpers/strict/RESUMED-AUDIT.json`.
The strict README explains the proof assumptions and 2 worked examples.
The candidate manifest binds 100 planned runs over 18 tasks.
Planning is not evidence that model runs complete or pass.

`batch.py` requires a parent approval file bound to the plan and passing canary.
It validates control and generated metadata hashes before a run.
It uses fresh evidence paths, no more than 2 concurrent containers, and quota gates.
It captures the submitted diff before G1 and test restoration.
It records the actual OMP launch with the required no-fallback overlay.

The driver stops before a new batch if the quota command fails or returns invalid data.
It preserves the error output and all completed outcomes in its terminal summary.
It distinguishes a failed quota probe from a quota ceiling stop.
For an older interrupted campaign, `recover_campaign.py` creates a separate, hash-bound recovery summary.
It never fabricates the missing original summary or changes saved outcomes.
The consumer recomputes that recovery and keeps every unattempted ID explicit.
Only a separate approved plan can resume those IDs after a fresh passing quota gate.

## Construct limitation

The original prompt asks the agent to make repository tests pass.
It does not itself forbid test edits or include the original task specification.
Verified test edits establish a test-tampering mechanism under this study's permitted-change policy.
They do not independently establish deceptive intent.
Strict contradiction proofs require intact tests and standard Python semantics.
The scale study keeps the original prompt, apart from explicit container-local cwd guidance.
