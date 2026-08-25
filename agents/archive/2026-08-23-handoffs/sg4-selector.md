Status: done
Last: merged as PR #109 (`731346c`)
Next: none
Blockers: none

## SG-4: Best-of-N Selection Lift and Verifier Calibration

Worktree: `.worktrees/sg4-selector` (branch `role/sg4-selector`)

### Key Deliverables

1. **`src/evallab/calibrate.py` (Generalization & Extensions)**:
   - **Selection Lift ($k \ge 3$)**: `evaluate_selection_lift` calculates $\text{pass@1}$ vs $\text{selected@k}$ vs oracle ceiling with TRUTH bootstrap 95% intervals from `evallab.cohort.bootstrap_mean_interval`. Underpowered cohorts ($n_{\text{tasks}} < 2$ or unavailable intervals) render `not distinguishable` (`NOT_COMPARABLE`).
   - **Chance-Corrected Verifier Agreement**: `evaluate_verifier_agreement` scores trials against execution ground truth ($y \in \{0, 1\}$). To handle unbalanced corpora (68 pass / 8 fail / 16 unmeasured in baseline corpus), computes Cohen's Kappa ($\kappa$), Balanced Accuracy ($\frac{\text{TPR}+\text{TNR}}{2}$), Matthews Correlation (MCC), and Macro-F1. A degenerate always-pass verifier produces $\kappa = 0.000$ and Balanced Accuracy = $50.0\%$.
   - **Separate Exception Accounting**: Never-measured trials (non-null `exception_class`) are strictly excluded from capability agreement calculations and reported separately in `never_measured_trials`.
   - **Isolated Extras Group & Degradation**: Optional dependency `llm-verifier` is isolated in `[project.optional-dependencies] verifier`. When absent, `load_llm_verifier()` degrades cleanly with `MissingVerifierDependencyError` providing installation remediation.
   - **Token Spend Protection**: `LlmVerifier` guards against paid token usage with `PaidModelAuthorizationError` unless `allow_paid_tokens=True` is explicitly passed.
   - **Stubs & Protocols**: `VerifierProtocol`, `StubVerifier`, `AlwaysPassStubVerifier`, and `AlwaysFailStubVerifier` enable deterministic local testing without model spend.
   - **Calibration Records & Eval Cards**: Emits schema-conforming `CalibrationRecord`s and drafts purpose-bound eval cards (`purpose="calibration"`) with explicit verifier provenance labeling via `build_verifier_calibration_card` / `draft_verifier_calibration_card`.

2. **`pyproject.toml` & `uv.lock`**:
   - Added `[project.optional-dependencies] verifier = ["llm-verifier>=0.2.0"]`. Isolated from `default-groups`, `dependencies`, and `dev`.

3. **`tests/test_calibrate.py`**:
   - Analytical validation of selection lift on known fixture cohort ($\text{pass@1}=5/12$, $\text{selected@3}=0.75$, oracle ceiling=$0.75$).
   - Underpowered cohort assertion rendering `not distinguishable`.
   - Exclusion and separate accounting of unmeasured exception trials.
   - Verification that degenerate always-pass verifier yields $\kappa = 0.0$ and Balanced Accuracy = $50\%$.
   - `CalibrationRecord` round-trip persistence and history loading.
   - Graceful degradation on missing `llm-verifier` dependency.
   - Paid model authorization gate enforcement.
   - Hard boundary verification (AST/import inspection confirming `task_workbench.py` and `registry.py` never import `llm_verifier` or `calibrate`).
   - Eval card generation with `purpose="calibration"` and stub provenance labeling.

4. **`sql/calibration.sql`**:
   - Views `v_judge_calibration_history`, `v_verifier_calibration_history`, and `v_selection_lift_candidates` for DuckDB and PostgreSQL.

5. **`docs/verifier-calibration.md`**:
   - Living documentation of selection lift methodology, chance-corrected metrics, exception handling, and platform boundaries.

6. **Indexes**:
   - `docs/INDEX.md` and `docs/repo-map.md` regenerated and verified.

### Verification

```bash
# Hard boundary verification
grep -rn 'calibrate' src/evallab/task_workbench.py src/evallab/registry.py || echo "boundary clean"
# Output: boundary clean

# Calibration test suite
uv run pytest tests/test_calibrate.py
# 9 passed in 0.29s

# Full test suite
uv run pytest
# 1214 passed, 2 skipped, 1 xfailed in 58.12s

# Linting
uv run ruff check .
# All checks passed!

# Type checking ratchet
uvx ty@0.0.71 check src/ --output-format=concise 2>&1 | tail -2
# Found 28 diagnostics (<= 28)

# Repo map & doc index checks
uv run python -m evallab.repomap check
# repomap check passed

uv run python -m evallab.docindex check
# docindex check passed
```
