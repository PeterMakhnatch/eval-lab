Status: review-wanted
Last: premerge green; audit 5/5 with .ruff_cache present
Next: PR FETCH: ignore ruff cache in audit; merge when checks green
Blockers: none

Follow-up after #13 on fresh `role/fetch-audit`.

`uv run pytest tests/test_fetch.py` — 9 passed (includes cache-dir regression).
`scripts/premerge.sh` — 69 passed; `premerge green: Python 3.12; ty 33 <= 33`.
After premerge, `library/benchmarks/.ruff_cache` exists; `uv run evallab fetch --audit` still:
```
5 benches, 0 fail
```
No `.ruff_cache` / `MANIFEST.md missing` line.
