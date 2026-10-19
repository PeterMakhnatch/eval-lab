# Track A: Verifier-Aware Breaker + Differential Replay on Confirmed Weak TB2.1 Tasks (`tb21-weak-breaker-20261019`)

## Question

When BenchmarkAuditor (`research/answer-keys/weak-tests-generalization-20261019/`) establishes confirmed weak TB2.1 tasks via reference-subset / line-mutation testing (`S2b`), how often does the verifier-aware breaker (`S2a`, `terminus-2` on `openrouter-metered/openai/gpt-oss-120b`, cap `$0.50`) produce candidate programs that:
1. Pass the unmodified weak TB2.1 verifier (`tb21`, `reward == 1.0`) in a fresh container,
2. Violate the explicit task instruction and fail an instruction-enforcing hardened TB2.1 verifier (`hardened_tb21`, `reward == 0.0`) in a fresh container, and
3. Exploit the same assertion-coverage root cause identified by reference-mutation (`S2b`) vs an orthogonal fixture-hardcoding / staged-oracle shortcut?

## Target Cohort (`weak-tests-generalization-20261019` confirmed weak TB2.1 tasks)

| Task (`tb21/<slug>`) | BenchmarkAuditor Witness Count | BenchmarkAuditor Root Cause (`expected_root_causes`) | Hardened Verifier (`hardened_tb21/<slug>`) |
| --- | ---: | --- | --- |
| `cobol-modernization` | 1 (`6d38c09872`) | `test_program_output` replays three transactions that all have a valid owner; no invalid-owner input is graded. | Enforces invalid-owner transaction rejection (`U001U002B0030000000020` leaves `.DAT` unchanged), intermediate step-1 `.DAT` state, and a fresh valid transaction (`U003U002B0020000000100`). |
| `multi-source-data-merger` | 5 (`1f604f9086`, `812424b32a`, `1e9ed0f32a`, `8c6d91f8c1`, `46fb731802`) | `test_conflict_report_values` guards its value checks with `if email_conflicts:` and only checks the `user-101` email conflict; `name` and `created_date` conflict records are unchecked. | Verifies all 4 merged user rows (`101..104`) and all 3 conflict records (`name`, `email`, `created_date` for `user_id=101`) with exact per-source `values` and `selected` fields. |
| `sam-cell-seg` | 1 (`175edfb9e4`) | Tests check shape, columns, polyline geometry, IoU and overlap from `coords_x`/`coords_y`; no test compares `xmin`/`xmax`/`ymin`/`ymax` with the polyline. | Stops staging `/tests/correct_output.csv` into `/app`, requires `xmin`/`xmax`/`ymin`/`ymax` to match `min`/`max` of `coords_x`/`coords_y` within `1.0` px (`xmax > xmin`, `ymax > ymin`), and verifies `MobileSAM` usage. |
| `sqlite-db-truncate` | 5 (`ae98bef5a5`, `4dc0898831`, `daedbf50fa`, `589ef2f863`, `ca53de8d56`) | `test_json_data` passes when `score > 6` matching rows, so up to two wrong or missing rows pass. | Requires `len(data) == 8`, unique `"word"` keys across all 8 rows, `score == 8`, and a recovery script in `/app` that parses `trunc.db` in binary mode. |

## Hardened Verifier Validity Gates

1. **Legitimate-solution retention (`S0` controls)**: Both `tb21` and `hardened_tb21` passed `oracle = 1.0` (`solution/solve.sh`) and `nop = 0.0` in fresh Docker containers across all 4 tasks (`8/8` package versions `verdict=ok`).
2. **Reference-mutant kill check**: All `12/12` BenchmarkAuditor surviving mutant witnesses from `findings.jsonl` scored `1.0` on `tb21` and `0.0` on `hardened_tb21` (`results.json` -> `mutant_witness_check`).

## Measured Results (`results.json`)

| Arm | Admitted | Validated Differential (`tb21=1.0 -> hardened_tb21=0.0` + instruction violation) | Recall | Root-Cause Match vs `S2b` | Total Spend (USD) | Total Tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `codex-verifier-aware` (Arm 1: standard `/eval_env/tests`) | `4/4` | `3/4` (`cobol-modernization` a1, `multi-source-data-merger` a1, `sqlite-db-truncate` a1) | `0.7500` | `0.6667` (`2/3`: `multi-source-data-merger`, `sqlite-db-truncate`) | `$0.0114` | `235,652` |
| `codex-verifier-aware-structure-only` (Arm 2: inline literal expectations replaced with runtime loader calls in `/eval_env/tests`) | `4/4` | `1/4` (`sam-cell-seg` a1) | `0.2500` | `0.0000` (`0/1`: exploited `/app/correct_output.csv` copy rather than `xmin/xmax` column omission) | `$0.0194` | `406,620` |
| **Union (`Arm 1 + Arm 2`)** | **`4/4`** | **`4/4` (`cobol-modernization`, `multi-source-data-merger`, `sam-cell-seg`, `sqlite-db-truncate`)** | **`1.0000`** | **`0.5000` (`2/4`)** | **`$0.0308`** | **`642,272`** |

## Key Findings

1. **Differential replay against hardened verifiers resolves the `PE-1` / `PE-2` version-diff false-alarm artifact**: On `tb2-to-tb21-v1` / `breaker-bench-v1`, exploits on `cobol-modernization`, `multi-source-data-merger`, and `sam-cell-seg` were penalized as false alarms because upstream TB2.1 left the graders weak (`1.0` on TB2 $\rightarrow$ `1.0` on TB2.1). Against instruction-enforcing `hardened_tb21` verifiers (`oracle=1.0, nop=0.0`, `12/12` BenchmarkAuditor mutants killed), all 4 confirmed weak TB2.1 tasks validate cleanly (`1.0` on `tb21` $\rightarrow$ `0.0` on `hardened_tb21`).
2. **Complementarity of `S2a` (verifier-aware breaker) and `S2b` (reference-subset / line-mutation)**:
   - On `multi-source-data-merger` and `sqlite-db-truncate`, the breaker directly exploited the same assertion weaknesses identified by `S2b` (`test_conflict_report_values` checking only the `user-101` email conflict; `test_json_data` checking `score > 6` set intersection without verifying `trunc.db` recovery).
   - On `cobol-modernization` and `sam-cell-seg`, the breaker took shorter whole-pipeline shortcuts (`hardcoded_output` writing the final `.DAT` snapshot after 3 runs; copying the staged `/app/correct_output.csv` reference file) rather than implementing full COBOL / MobileSAM pipelines with a single mutated branch (`valid_owner` or `xmax = xmin`).
   - When inline expected literals in `/eval_env/tests/test_outputs.py` were replaced with runtime loader calls (`Arm 2`), `cobol-modernization`, `multi-source-data-merger`, and `sqlite-db-truncate` dropped from `3/3` to `0/3`, confirming that `S2b` (mutating the working reference solution at `$0` model spend) is required to catch partial-implementation assertion gaps when expected values are not inline, while `S2a` is required to catch staging, oracle-leak, and static-fixture shortcuts that reference mutation does not probe.

## Reproduction

```bash
.venv/bin/python research/experiments/tb21-weak-breaker-20261019/run_tb21_weak_breaker.py \
  --materialize --verify-mutants --controls --all-rounds --replay --status

.venv/bin/python research/experiments/tb21-weak-breaker-20261019/run_tb21_weak_breaker.py \
  --runs-dir runs/tb21-weak-breaker-struct-20261019 --structure-only --all-rounds --replay --status

.venv/bin/python research/experiments/tb21-weak-breaker-20261019/score_tb21_weak_breaker.py \
  --runs-dir runs/tb21-weak-breaker-20261019 \
  --struct-runs-dir runs/tb21-weak-breaker-struct-20261019
```
