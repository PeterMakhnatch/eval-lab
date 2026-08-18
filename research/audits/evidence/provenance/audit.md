# Audit Evidence: Task Provenance Classifier & Report

Handoff: `agents/handoffs/provenance.md`
Subject: `src/evallab/provenance.py`

## 1. Test Suite Verification
Command: `uv run pytest tests/test_provenance.py -v`
Output:
```
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.4.2, pluggy-1.6.0
rootdir: /Users/petermakhnatch/Developer/eval-lab/.worktrees/m015-audit
configfile: pyproject.toml
plugins: hypothesis-6.165.10
collected 11 items

tests/test_provenance.py ...........                                     [100%]

============================== 11 passed in 0.88s ==============================
```

## 2. Report Generation
Command: `uv run python -m evallab.provenance report`
Output Summary:
- External corpus (`tb3_root`): found 74 tasks
- Local lab (`local-lab`): found 4 tasks (`event-summary`, `transaction-reconciliation`, `query-optimize`, `terminal-bench-html-js-filter`)
- Proposed (`proposed`): path does not exist (0 tasks)
- Discovered tasks classified with origins (`harbor-native`, `harbor-derived`, `local-lab`), family, and confidence.

## 3. CLI Subcommand Gap Finding
Command: `uv run evallab provenance`
Output: `argument command: invalid choice: 'provenance'`
Note: `provenance.py` is executable via `python -m evallab.provenance {classify,report}` but was not wired into the root `evallab` CLI.

## Verdict
CONFIRMED.
Provenance engine classifies tasks across multi-corpus roots with deterministic reports. CLI gap noted in board notes.
