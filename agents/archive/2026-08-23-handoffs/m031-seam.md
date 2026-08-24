Status: done
Last: merged as PR #134 (`64e1e96`)
Next: none
Blockers: none

# M031 — LOOP-SEAM: Model adapter transport and injection points

Status: complete — ready for review
Last: implemented `src/evallab/modeladapter.py` supporting `cursor-agent` and `agy` headless CLI transports behind an injectable `SubprocessRunner` seam with explicit model pinning, timeout, verbatim stdout capture, typed errors, and `AnalyzerCallable` compatibility; wired injection points in `analyst.py` (`ModelAnalyzer`, `run_analysis`) and verified `analysis_worker.py` (`default_worker`); added comprehensive unit test suite in `tests/test_modeladapter.py`.
Next: authoring stub designer in `authoring.py` (SG cycle) and persisting the analyst's complete multi-turn reasoning transcript to closed storage.
Blockers: none for this PR. Closed study loop requires the subsequent authoring designer and reasoning transcript storage missions.

## Overview

Prior to M031, all model seams in the lab were refusing stubs (`ModelAnalyzer.analyze()` unconditionally raised `ModelProviderRefusedError`, `analysis_worker.py` defaulted to `_no_adapter`, and no provider SDK was installed).

M031 introduces a dependency-free model adapter subsystem that shells out to local subscription CLIs (`cursor-agent` and `agy`), preserving fail-closed spend gates while enabling real model invocation when explicitly configured.

## What landed

| Component | Detail |
|---|---|
| `src/evallab/modeladapter.py` | `ModelAdapter`, `ModelAdapterResult`, `validate_pinned_model`, `cursor_adapter`, `agy_adapter`, typed exceptions (`ModelAdapterRefusalError`, `ModelAdapterExecutionError`, `ModelAdapterTimeoutError`), injectable `SubprocessRunner` seam |
| `src/evallab/analyst.py` | Updated `ModelAnalyzer` to accept an optional `adapter` parameter and delegate analysis to it, while continuing to refuse with `ModelProviderRefusedError` when no adapter is supplied; updated `run_analysis` to accept and pass `adapter` |
| `src/evallab/analysis_worker.py` | Maintained `_no_adapter` fail-closed default in `default_worker` while supporting injected `AnalyzerCallable` |
| `tests/test_modeladapter.py` | 22 unit tests exercising CLI argv generation, stdout capture, timeout handling, non-zero exit handling, model pinning validation, analyst refusal/execution, and worker deferral/execution with a fake subprocess runner |
| `docs/repo-map.md`, `docs/INDEX.md` | Regenerated documentation index and repository map |

## Key Invariants Enforced

1. **Explicit Pinned Models Only**: `validate_pinned_model` refuses `None`, empty strings, whitespace, `auto`, `default`, `latest`, `none`, `unpinned`, and generic selectors before any subprocess starts.
2. **Fail-Closed Default**: `ModelAnalyzer.analyze()` without an injected adapter raises `ModelProviderRefusedError`. `analysis_worker.default_worker()` without an adapter uses `_no_adapter` and defers requests under `adapter_not_wired`.
3. **No Secret Leakage**: Subprocess calls execute with `subscription_environment()`, ensuring no API keys or secret environment variables are forwarded.
4. **Full Provenance Recorded**: `ModelAdapterResult` captures `model`, `argv`, `transport`, and `raw_output`.

## Live Verification

Exercised live calls on this workstation through both transports:

```
=== Testing cursor_adapter live call ===
Transport: cursor-agent
Model: cursor-grok-4.6-high
Argv: ['cursor-agent', '-f', '--model', 'cursor-grok-4.6-high', '-p', 'Reply with EXACTLY: cursor-adapter live test ok']
Output: cursor-adapter live test ok

=== Testing agy_adapter live call ===
Transport: agy
Model: gemini-3.7-flash-high
Argv: ['/Users/petermakhnatch/.local/bin/agy', '--model', 'gemini-3.7-flash-high', '-p', 'Reply with EXACTLY: agy-adapter live test ok']
Output: agy-adapter live test ok
```

## Mutation Evidence

1. **Mutation 1 (Allow unpinned models)**: Bypassing `validate_pinned_model` caused 13 tests in `test_unpinned_or_empty_model_refused_before_process_starts` and `test_none_model_refused_before_process_starts` to fail (`DID NOT RAISE ModelAdapterRefusalError`). Restored -> 22 passed.
2. **Mutation 2 (Default adapter fallback in ModelAnalyzer)**: Making `ModelAnalyzer.analyze()` fall back to a default adapter when none was injected caused `test_analyst_refuses_without_adapter` and `test_no_model_invoked_without_model_flag` to fail (`DID NOT RAISE ModelProviderRefusedError`). Restored -> 30 passed.

## What remains before the study loop closes end to end

- The `authoring.py` stub designer (remains a deterministic stub, scheduled for a separate authoring cycle).
- Storing the analyst's full multi-turn reasoning transcript to durable storage.
- Neither of these are part of this PR's lease and neither are claimed as complete.
