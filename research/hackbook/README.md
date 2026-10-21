# Hackbook v0 (`research/hackbook/`)

**Hackbook v0** (`evallab.hackbook.entry/v0`) is Eval Lab's versioned catalog of known task-side benchmark defects (`TS01`–`TS11`) and run-side reward-hacking / evaluation failure modes (`RS01`–`RS07`). Each entry records:

- **`mechanism`**: How the task defect or run-side behavior produces an unearned pass (`false_pass`) or undeserved failure (`false_fail`).
- **`signatures`**: File globs, file regexes, trajectory command regexes, diff regexes, and canonical `structural_signals` used by `evallab hackbook match`.
- **`detection_recipe`**: Concrete `static_check`, `tripwire_rule`, or `probe_test` recipe aligned with the 11 canonical `detection_method_ids` (`reference_mutation`, `differential_tests`, `reward_seeker`, `static_review`, `failure_clustering`, `trace_judge`, `tripwires`, `fixed_task_rerun`, `controls`, `equivalent_rewrite`, `grader_fuzz`).
- **`exploit_template`**: Preconditions, probe steps, illustrative snippet, and differential verdict (`original_grader` vs `fixed_grader`) for reward-seeker / control verification.
- **`fix_pattern`**: Target fix location (`grader`, `environment`, `instruction`, `answer key / reference`, `task`), summary, verification checklist (`reference passes 3/3`, `nop fails 3/3`, `witness fails 3/3`), and example diff or rule.
- **`examples`**: Grounded examples across the 9 seed sources with `evidence_status` (`"executed"` or `"claimed"`).

## Files

| File | Description |
| --- | --- |
| `entries.jsonl` | Canonical catalog entries (`evallab.hackbook.entry/v0`, one sorted-key JSON object per line). |
| `taxonomy.json` | 11 task-side (`TS01`–`TS11`) and 7 run-side (`RS01`–`RS07`) classes, decision order, detection methods, and crosswalks. |
| `schema.json` | Generated JSON Schema for `evallab.hackbook.entry/v0` (`build.py`). |
| `summary.json` | Generated coverage and source census summary (`build.py`). |
| `build.py` | Validator and generator (`python research/hackbook/build.py [--check]`). |

## Seed Sources (9 Canonical Sources)

1. **`preference_model_tb4`**: Preference Model's 241 Terminal-Bench 4 findings (aggregate 241-finding census in `summary.json`; entry examples strictly restricted to the 25 `tb4-audit-v1` `dev`-split tasks and 3 `benchmark-wide` items, including all 10 executed `dev`-split witnesses from `research/answer-keys/tb4-audit-v1/witnesses.json`).
2. **`benchjack`**: BenchJack vulnerability classes `V1`–`V8` ([arXiv:2605.12673](https://arxiv.org/abs/2605.12673)).
3. **`cheatbench`**: CheatBench (`steinad/CheatBench` / `cheatbench-actions-v1`) human-verified cheating mechanisms (`secret_discovery_in_environment`, `secret_discovery_via_web_search`, `verifier_exploitation`, `harness_leaks_answer`, `harness_leaks_verifier`).
4. **`terminal_wrench`**: Terminal Wrench ([arXiv:2604.17596](https://arxiv.org/abs/2604.17596)) and Benchflow `reward-hack-matrix` (`breaker-bench-v1`) mechanisms (`hollow-implementation`, `conftest_hook`, `output-spoofing`, `constraint-loophole`, `path_trojan`, `binary-hijacking`, `algorithmic-simplification`, `mutable-input-tampering`, `keyword-gaming`, `structural-extraction`, `metric-spoofing`, `pth_injection`, `security-downgrading`, `deceptive-rationalization`).
5. **`hacktrace`**: HackTrace (`JH012/HackTrace-Data` / `hacktrace-act-v1`) shortcut mechanisms (`test_tamper`, `hardcode_output`, `special_casing`, `eval_bypass`).
6. **`envcheck`**: Tokenless EnvCheck findings (`tokenless-ai/envcheck-findings` / `envcheck-tb4-39` `dev`-split tasks + DeepSWE `mutable-test-infrastructure` + BFCL `agentic-removes-numeric-separators`).
7. **`mimo_atlas`**: Eval Lab MiMo weak-test atlas (`mimo-weak-tests-v2.1`), `mimo-heldout-v6-to-v7` package repairs, and the Vals MiMo git-history leakage audit (`1,795 / 2,698` tasks).
8. **`eval3`**: All 18 Tokenless Eval3 sample issues ([usetokenless.com/eval3](https://usetokenless.com/eval3/)) across `ScienceWorld boil-water` (8 issues: `liquid`, `optional`, `overlength`, `ticks`, `refocus`, `shadow`, `replay`, `config`), `Terminal-Bench 2 large-scale-text-editing` (5 issues: `setreg`, `filter`, `generator`, `vim`, `symlink`), and `Spider2 local017` (5 issues: `constant`, `columns`, `comment`, `first`, `gold`).
9. **`tb2_to_tb21`**: Terminal-Bench 2 to 2.1 maintainer fix cohort (`tb2-to-tb21-v1`) and executed TB2.1 weak-test witnesses (`tb21-weak-tests-v1`).

## Answer-Key Leak Guard (`tb4-audit-v1` `test` Split)

To prevent data leakage into held-out evaluation, **zero** of the 41 `tb4-audit-v1` `test`-split task names or 152 `test`-split item IDs may appear anywhere in `research/hackbook/`, `src/evallab/hackbook/`, `.omp/skills/hackbook/SKILL.md`, or `tests/test_hackbook.py`. Both `python research/hackbook/build.py --check` and `pytest tests/test_hackbook.py` enforce this boundary.

## CLI and Python Usage

```bash
# List entries (filter by class ID/key, side, source, or evidence status)
evallab hackbook list
evallab hackbook list --class TS04 --evidence executed --json

# Show full details for one entry by ID or slug
evallab hackbook show HB-TS01-001
evallab hackbook show hardcoded-fixture-outputs --json

# Relevance search across the catalog
evallab hackbook search "conftest sitecustomize" --limit 5

# Match a task package, exported view, or trajectory against the catalog
evallab hackbook match path/to/task_dir --kind task --top 10
evallab hackbook match path/to/trajectory.json --kind trace
evallab hackbook match path/to/view_dir --kind view --emit-findings /tmp/findings
```

```python
from pathlib import Path
from evallab.hackbook import load_hackbook, match_target, match_task, match_trace

entries = load_hackbook()
task_hits = match_task(Path("path/to/task"), entries)
trace_hits = match_trace(Path("path/to/trajectory.json"), entries)
```

## Rebuilding and Validating

```bash
.venv/bin/python research/hackbook/build.py
.venv/bin/python research/hackbook/build.py --check
```
