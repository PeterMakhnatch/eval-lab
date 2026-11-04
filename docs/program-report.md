---
status: living
audience:
  - analyst
  - operator
---

# Receipt-bound program reports

`evallab report program` builds reports from `<program>/*/RESULT.md`.
It makes no model calls and does not change the input files.
The implementation is `program_report.py`; `report.py` continues to own trajectory reports.

## Index before drafting

```bash
uv run evallab report program /path/to/program --output-dir /path/to/index --index-only
```

The complete `facts.json` records each number-bearing physical line verbatim.
Each fact has a stable content-bound ID, lane, source path, line, and receipt paths.
Headings, code, dates, and identifiers remain indexed. They are not silently discarded.
Numeric checks reuse the insights quantity checker, including its identifier and date exemptions.
A receipt must be an existing file, not the RESULT itself or a directory.
Inline backtick paths and Markdown links resolve against the lane root.
Explicit `research/`, `src/`, `docs/`, and `tests/` paths also resolve against the repository root.
The index does not infer that every lane receipt supports every claim.

`checks.json` lists missing receipts and numbers absent from cited receipt text.
The checker recomputes printed `numerator/denominator = percentage` expressions using their printed rounding precision.
Arithmetic mismatches block the draft, even when a receipt repeats the wrong percentage.
The check records receipt hashes. Receipts above 256 KiB remain unchecked; use a bounded receipt extract.
Numeric presence is not semantic verification. Its status is `receipt_bound_unchecked`, not verified.
The workflow does not certify model routes, statistical validity, or scientific conclusions.
`verification.json` records each `verify-lane` invocation when that command exists on `origin/main`.
Until then, lane verification remains unchecked. The report preserves verifier failures rather than claiming a clean audit.

## Select and draft

The default draft includes every indexed fact. Missing receipts or unsupported numbers block it.
The failed build retains its index, checks, and `rejected.json`, but creates no `REPORT.md`.
Use a new output directory for each build. Existing output directories are never overwritten.

For an editorial report, select explicit facts with repeated `--select ID` options.
The complete index still contains every unselected statement and its check status.
Selection is not a correction: do not select a refuted number merely because a receipt repeats it.
Every selected statement must have receipt support. A selection cannot bypass that check.

Some older RESULT files cite receipts elsewhere rather than beside the statement.
Use `--citations bindings.json` to bind those receipts explicitly:

```json
[
  {
    "id": "F-content-bound-id",
    "quote": "The exact physical line from RESULT.md",
    "receipts": ["/absolute/path/to/receipt.json"]
  }
]
```

The quote guard rejects changed statements. Fact IDs also change when the source path or line changes.
Review the source and receipt before creating a binding. Numeric presence alone does not prove the claim.

```bash
uv run evallab report program /path/to/program \
  --output-dir /path/to/draft \
  --citations bindings.json --select F-content-bound-id
```

The draft starts with the answer, then gives a table for each lane, then lists failures.
Each selected numeric statement links to its facts-index ID.
Lanes without selected facts remain visible. The complete index preserves their unchecked claims.

## Publish only when authorized

Add `--bridge /path/to/research-context/bridge/new-report` to publish the successful build.
The destination must not exist. The repository must be `research-context`, on `main`.
The workflow copies the artifacts, stages only that new directory, commits only that path, and pushes `origin main`.
Other staged work remains uncommitted. A failed push returns failure; the local commit remains recoverable.
This option publishes to others. Use it only with current authorization.
Google Docs publication is not part of this command.
