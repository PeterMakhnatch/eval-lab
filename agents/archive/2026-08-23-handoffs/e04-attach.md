Status: done
Last: merged as PR #92 (`3e8cdea`)
Next: none
Blockers: none

## Summary
Implemented `src/evallab/attach.py` exporting `attach()` returning DuckDB connection with Z2 (postgres_scanner), Z3 (Parquet views over hot+cold with union_by_name), Z4 (front_matter via parse_doc). CLI `evallab db attach` supports --print-sql, --query, --zones with honest degradation.

## Verification
- `uv run pytest tests/test_attach.py -q` passes
- Full suite + ruff + ty + docindex check green
- PR opened
