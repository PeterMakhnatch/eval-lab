# Harness → Eval Runner: consume the ready native wheel

Authority: Peter's immediate Harness / Eval Runner build assignments in `research-context/harbor/corpus/OMP-LANE-LEADS-2026-09-07.md:346-389`. This is the requested established-inbox handoff, not a page or new approval.

## Ready input

- Contract: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/harness/evidence/native-lab-20260908/adapter-contract.json`.
- Wheel: same directory, `dist/evallab_metaharness-0.2.1-py3-none-any.whl`.
- SHA256: `82f0fc557677068f2b60a4f590e9f0aafbf9a9a390e748b9a41445c5763beecb`.
- Fixed entrypoints: `evallab_metaharness.agents:Baseline`, `:Released`, `:BootstrapCwd`.
- Qualified native runtime: Harbor 0.21.0, LiteLLM 1.96.0, Tenacity 9.1.4, HTTPX 0.28.1. Installed-wheel factory/agent-loop proof is retained; package was not installed into shared runtime.
- Model: `anthropic/claude-opus-4-6`; temperature 0.7, max_turns=24, summarization off, 24 physical requests/trial and requested 4096 output tokens/request. Local timeouts do not prove remote cancellation. Input estimates are not exact token or dollar caps.
- Native ATIF plus `request-accounting.json`; distinguish actor/image_controller, partial known usage, missing/null cache fields, late/remote-unknown outcomes. Actual cost remains unknown, not zero.

## Exact execution-owner integration

Register reviewed fixed profiles and credential/limit/watchdog routing through existing Lab DTO/policy/queue/executor. Do not enable arbitrary `kwargs`, environment or PYTHONPATH passthrough, or weaken capability rejection. The current preparation's five typed requests were refused for provider request/cost/token enforcement; historical refusal is retained in `evidence/native-lab-20260908/prepared/native-comparison-request.json`.

The new optimizer first evolves supplementary instruction text. It will use the existing `extra_instruction_path` and `extra_instruction_sha256` / preamble provenance fields, holding the wheel/profile/model fixed. Generated Python is not executed on the host. Candidate-backed billable specs remain explicitly approval-gated; optimizer-side code neither approves nor calls Harbor directly. Confirm supported candidate instruction/profile binding in your owned handoff when integrated; no response/page is requested here.

## Independent implementation completed

Harness owns `src/evallab/gepa_optimizer/` and focused corresponding tests in `/Users/petermakhnatch/Developer/eval-lab/.worktrees/harness-gepa-optimizer`, branch created as `feat/harness-gepa-optimizer`, base `884dbc173404972ad78e17a03c6471cb3d663485`. It did not edit runner/queue/DTO/CLI/policy/ingestion. Released GEPA is pinned to `0632cdb5dcc052e690eab439e1b4a7e3e9cfe407`; optimize_anything now executes through the existing Lab evaluator boundary. Four actual native controls completed (oracle seed/proposal 1/1; no-op 0/0), with retained selection and existing cohort comparisons. Proposals were deterministic interface fixtures, not model-generated improvements.

- Final evidence/source hashes: `/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/harness/evidence/optimizer-gepa-20260908/verification.json`; 36 focused cases and Ruff checks pass. Original native jobs and the initial provenance-reader failure are retained.
- Included MetaHarnessEngine source pin, materialization, containment and native scoring are qualified. Live macOS launch is refused because pinned upstream sets `sandbox.failIfUnavailable=False`; live proposer/OS isolation is not qualified.
- The next real GEPA proposal requires its own campaign-bound authorization; billable target candidates require genuine estimates and existing Lab approvals. No shared profile integration/activation is claimed or rechecked here. The ready wheel and its three entrypoints remain unchanged.

No model/runtime/task-admission/spend/merge approval is implied by this note. Return path remains owned files only.
