# Honest certification re-attempt

## Answer

**0/3 tasks receive a new certificate** (95% Wilson interval: 0.0–56.1%). This is a descriptive result for 3 selected DEV tasks.

Fresh, strict instruction-only alternatives expose Gold false rejections on 000682 and 002139. The 002391 generation requirement remains unmet: none of its 8 fresh alternatives passes the original grader.

| Task/cohort | Original grader | Independent public check | Existing Gold acceptance | Frozen evaluation: original / Gold |
|---|---:|---:|---:|---:|
| 000682, strict instruction-only | 4/4 | 4/4 side-effect/registration checks | 0/4 | 2/2 / 0/2 |
| 002391, initial strict cohort | 0/4 | Not run | 0/4 | 0/2 / 0/2 |
| 002391, prospective strict replication | 0/4 | Not run | 0/4 | 0/2 / 0/2 |
| 002139, initial filename-guided cohort | 3/4 | Not run | 0/4 | 1/2 / 0/2 |
| 002139, strict filename-unconstrained replication | 4/4 | 4/4 public Fetch API checks | 0/4 | 2/2 / 0/2 |

Wilson intervals: 4/4 = 51.0–100.0%; 0/4 = 0.0–49.0%; 3/4 = 30.1–95.4%; 2/2 = 34.2–100.0%; 0/2 = 0.0–65.8%; 1/2 = 9.5–90.5%. The pooled 002391 original result is 0/8, interval 0.0–32.4%.

All 20 generated candidates remain in the evidence. No failing candidate is excluded, repaired, or replaced. The preregistered corrected Gold versions also accept 0/8 on 002391 and 0/4 on the strict 002139 cohort. Their frozen evaluation acceptance is 0/2 per cohort. The initial 002139 cohort is not described as strict instruction-only: its allowlist supplies the reference-derived new filename `gcp.py`.

## Exact blockers

### 000682: schema and configuration-name requirements exceed the instruction

All 4 fresh alternatives pass the original grader. All 4 pass a separate frozen check of selected-key configuration, encryption removal, and shared KMS filter registration. These checks use fake SNS calls; they are not live AWS integration.

Existing Gold rejects all 4. It requires the key schema to equal `{'type': 'string'}` exactly, rejecting an added `minLength: 1`. It also requires the configuration field `enabled`. The alternatives expose `state`, which enables and disables encryption correctly. The instruction specifies neither field name nor exact schema dictionary.

Evidence: `contracts/000682/existing.py:53-54,111-118`; `/private/tmp/ov1104/wave2-grader/workers/cert/cells/000682-alt-2-existing/trial/verifier/test_output.log:60-82`; `evaluation-sns-public.json`.

The 4 generated patches differ, but they share substantially the same executable design. Their independent generation does not establish broad implementation diversity. No new SNS Gold correction follows these failures.

### 002139: Gold pins a reference-only module name

The strict replication chooses `gs.py` and registers `gs.GS` without a prescribed module name. All 4 alternatives pass the original grader. All 4 pass the independent public `bb.fetch2.Fetch` check, including filenames, object selection, output, missing-object conversion, missing/empty output, cleanup, existence checks, and network denial.

Both Gold versions import `bb.fetch2.gcp` directly. They reject all 4 strict alternatives at import time. The original grader reports 11/11 tests passed before this additional Gold failure. The earlier sentinel-only correction cannot remove this separate module-name requirement.

Evidence: `/private/tmp/ov1104/wave2-grader/workers/cert/cells/002139-alt-6-existing/result.json`; `002139-alt-6-corrected/result.json`; `002139-alt-6-public/result.json`; `PUBLIC-FREEZE.json`.

### 002391: no original-correct fresh alternatives

Both fresh cohorts merge duplicate primary IDs into the output alias set, including primary IDs absent from the supplied aliases. The original grader expects the union of the provided alias sets. The representative initial candidate fails 3 tests and passes 5. The second, deeper-reasoning cohort also fails original grading at 0/4.

The second cohort receives only the unchanged instruction and baseline source. Its generic reasoning request contains no alias-union hint, previous alternative, grading feedback, or reference patch. No generated candidate is repaired after grading.

Evidence: `/private/tmp/ov1104/wave2-grader/workers/cert/cells/002391-alt-2-original/trial/verifier/test_output.log:43-57,94-103,141-150`; `evaluation-advisory.json`. This is a generation blocker, not evidence that Gold rejects a correct fresh 002391 solution. Removing the preregistered `published` assertion does not address these original-grader failures.

## All 7 gates

`H` means immutable historical metadata with an independently recomputed exact cell key and unchanged full package digest. No historical injection control executes again. `U` means unexercised and not passed.

| Task/version | G1 reference | G2 NOP | G3 witness | G4 exit control | G5 train mutants | G6 evaluation mutants | G7 alternatives | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 000682 existing | 1.0 H | 0.0 H | 0.0 H | 0.0 H | 1/1 killed H | 1/1 killed H | 0/4; eval 0/2 | Failed |
| 002391 existing | 1.0 H | 0.0 H | 0.0 H | 0.0 H | 4/4 killed H | 3/3 killed H | 0/8; eval 0/2 each cohort | Failed |
| 002391 corrected | 1.0 | 0.0 | U | U | 4/4 killed | 3/3 killed | 0/8; eval 0/2 each cohort | Failed; incomplete controls |
| 002139 existing | 1.0 H | 0.0 H | 0.0 H | 0.0 H | 4/4 killed H | 4/4 killed H | strict 0/4; eval 0/2 | Failed |
| 002139 corrected | 1.0 | 0.0 | U | U | 4/4 killed | 4/4 killed | strict 0/4; eval 0/2 | Failed; incomplete controls |

The corrected versions have fresh reference, NOP, and source-mutant executions. Changed package digests cannot inherit G3/G4. Existing G1–G6 reuse exact historical evidence. All 12 historical reference/NOP/witness/exit control keys match recomputation; see `historical-control-bindings.json`.

Mutant-kill Wilson intervals: 1/1 = 20.7–100.0%; 3/3 = 43.9–100.0%; 4/4 = 51.0–100.0%. These are reused mutant pools, not fresh adversarial searches. On 002391, the original grader already kills all 3 evaluation mutants. Its G6 does not establish additional hardening benefit.

Unmodified `certify-gold` does not execute. Its planner automatically includes prohibited witness/startup-injection controls (`src/evallab/gold_certifier.py:728-735`). The manual matrix executes only source patches, NOP, and safe source-mutant arms. No submission-side bypass, injection control, or grader/test modification executes. The operator changes only the preregistered synthesized assertions in separate package copies. No local inference or nested worker executes.

## Generation and provenance

- Primary preregistration SHA-256: `ac843c8075f0990c4c78944481093f7f48afbaad0d9d0b99b46363b9e8f619ec`.
- `FREEZE.json` binds source manifests and contracts before scoring. Separate replication/public preregistrations and frozen hashes remain in this folder.
- 20/20 gateway responses attest `openai-codex/gpt-6.1-terra`. All requests specify `tools: []` and `tool_choice: none`.
- 16 candidates use instruction and pristine baseline source without reference-derived filename guidance. The other 4 remain separately labeled.
- `input-manifest.json` records source hashes and pristine-source receipt hashes. `receipt-index.json` preserves all raw request/response paths and hashes. Raw files remain under `/private/tmp/ov1104/wave2-grader/workers/cert/generation/`.
- The controller converts model text to source-only diffs and applies them inside Modal task sandboxes. `source-review*.json` records review before each cohort's Gold grading. Source review is not substituted for executed correctness.
- The controller worker uses Sol, not terra. `worker-attribution.json` records its separate session evidence.

## Commands, receipts, and spend

`COMMANDS.txt` records exact runtime commands. Primary entry points are `generate.py`, `replicate139.py`, `replicate2391.py`, `evaluate.py`, `evaluate_replication.py`, `evaluate_advisory.py`, `evaluate_sns_public.py`, and `summarize.py`. The existing grader worktree's locked Harbor 0.24 environment supplies the runtime. No shared environment installation or repository check runs here.

`receipts.jsonl` preserves 86 compact cell receipts and hashes. There are 84 completed executions and 2 failed attempts: an unpinned CLI preflight and a wrong-workdir source probe. Both failures remain recorded. No local Docker container executes.

- Total approved Modal cap: **$2.00**.
- Campaign ledger runtime estimate: **$0.295943** across 86 attempts; outstanding reservations **$0**.
- Provider-reported cost to date: **$0.18728966**, observed at **3:19 AM ET**. This is separate from the runtime estimate. Provider billing can lag; the final invoice amount remains unknown.
- Subscription generation incremental cost: **$0**.
- Ledger: `~/.local/state/evallab-ov1104/wave2-grader-spend.json`.
- Provider evidence: `billing.json`. The owned application `ap-mypHhQj6Qhylr8IfWxBwUz` is stopped with 0 tasks at **3:21 AM ET**; see `closure.json`.

No task has a new seven-gate certificate. The exact unmet requirement is at least 2 original-correct fresh 002391 alternatives. The positive measured result is independent confirmation of Gold false rejections on 2 tasks, with every failed attempt retained.
