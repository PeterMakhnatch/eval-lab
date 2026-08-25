Status: done
Last: merged as PR #96 (`303e7e3`)
Next: none
Blockers: none

## E11: eval-card generator with purpose-bound shape and mandatory uncertainty

Worktree: `.worktrees/e11-cards` (branch `role/e11-cards`)

### Key Deliverables

1. **`src/evallab/cards.py`**:
   - `build_eval_card` / `draft_eval_card` / `generate_card`: card generator querying the unified attach surface (`evallab.attach`) and stats API (`evallab.cohort`).
   - Purpose binding:
     - `baseline`: per-agent pass@k card clustered by task.
     - `comparison`: requires a preregistration block (`expected` + `decision_rule`), quoting it verbatim; refuses without it.
     - `practice`: explicitly excluded from cards and lessons with `CardRefusalError`.
   - Task as evidence unit: pass@k evaluated over tasks with bootstrap 95% interval.
   - Mandatory uncertainty (Tenet T4): underpowered cohorts ($n_{\text{tasks}} < 2$ or unavailable interval) render `not distinguishable` rather than bare rates.
   - Separate exception accounting: trials raising harness exceptions are never scored as capability zeros ($0.0$), excluded from the capability denominator, and reported under exceptions and validity threats.
   - Deterministic: same inputs produce byte-identical markdown; carries `inputs: [{path, digest}]` list for E14 lineage tracing.
2. **`src/evallab/cli.py`**:
   - Wired `evallab card generate <target> [-o path] [--json]` following the `db attach` pattern.
3. **`tests/test_cards.py`**:
   - Synthetic fixture cohort under `tmp_path` verifying pass@k, sample size $n$, bootstrap interval, and separation of exceptions from scored zeros.
   - Refusal assertions for `purpose="comparison"` without prereg and `purpose="practice"`.
   - Underpowered cohort assertion verifying `not distinguishable` rendering.
   - Deterministic byte-identity test and lineage inputs assertion.
   - CLI execution tests covering stdout rendering, `--json` summary, and `-o` file output.
4. **`docs/eval-cards.md`**:
   - Living documentation with required front-matter, purpose-to-card matrix, refusal conditions, statistical mapping, and human review guidelines.
5. **Surfaces**:
   - `docs/INDEX.md` and `docs/repo-map.md` regenerated and verified.

### Verification

```bash
uv run pytest tests/test_cards.py
# 9 passed, 1 skipped in 0.69s

uv run pytest
# 1076 passed, 2 skipped, 1 xfailed in 34.58s

uv run ruff check .
# All checks passed!

uvx ty@0.0.71 check src/ --output-format=concise 2>&1 | tail -2
# Found 28 diagnostics (at ratchet <= 28)

uv run python -m evallab.repomap check
# repomap check passed

uv run python -m evallab.docindex check
# docindex check passed
```
