---
status: living
audience:
  - operator
  - analyst
---

# Verify a lane from its receipts

`evallab verify-lane` checks a lane without changing its source files or calling a model.
It writes `VERIFY.json`, `VERIFY.md`, GitHub responses, and the Git fetch receipt outside the lane.
The command uses the existing insights recipe engine, Wilson interval, and exact McNemar test.
The implementation stays in `src/evallab/laneverify.py`, beside the existing core CLI modules.

```bash
uv run evallab verify-lane /absolute/path/to/lane \
  --output-dir /absolute/path/to/new-audit \
  --repo /absolute/path/to/eval-lab \
  --allowed-route 'openai-codex/*' \
  --claims /absolute/path/to/claim-recipes.json
```

The output directory must be outside the checked lane. Existing verification outputs cannot be overwritten.
The command reads `RESULT.md` and `EARLY.md`. Missing reports fail the audit.
It inventories number-bearing prose with verbatim quotes and character offsets.
Code identifiers, URLs, hashes, section headings, and list ordinals are not measurements.
Unmapped numbers remain `not_recomputable`. They never count as verified numbers.

## Bind numbers to receipts

Use a JSON array of recipes. `number` selects the numeric token within the exact quoted line, starting at 0.
A changed quote invalidates its recipes. Relative receipt paths resolve inside `root`, or inside the lane by default.
The insights engine rejects receipt paths that escape that root.

For this report line:

```text
Passed 3 of 10 cases (30.0%).
```

And this `counts.json` receipt:

```json
{"passed": 3, "total": 10}
```

Use these mappings:

```json
[
  {"quote":"Passed 3 of 10 cases (30.0%).", "number":0,
   "recipe":{"op":"value", "file":"counts.json", "pointer":"/passed"}},
  {"quote":"Passed 3 of 10 cases (30.0%).", "number":1,
   "recipe":{"op":"value", "file":"counts.json", "pointer":"/total"}},
  {"quote":"Passed 3 of 10 cases (30.0%).", "number":2, "scale":100,
   "recipe":{"op":"ratio",
             "num":{"op":"value", "file":"counts.json", "pointer":"/passed"},
             "den":{"op":"value", "file":"counts.json", "pointer":"/total"}}}
]
```

Each recipe checks one number. Provide mappings for every measurement, including confidence bounds.
`report` selects `RESULT.md` by default; use `EARLY.md` for an early report.
Recipes support counts, filtered counts, distinct counts, sums, means, scalar pointers, and ratios.
See [the insights engine](insights.md) for recipe semantics and provenance limits.

A statistical mapping replaces `recipe` with `stat`, `inputs`, and `field`:

```json
{"quote":"Wilson lower bound: 10.8%.", "number":0, "scale":100,
 "stat":"wilson", "field":"low",
 "inputs":{"k":{"op":"value", "file":"counts.json", "pointer":"/passed"},
           "n":{"op":"value", "file":"counts.json", "pointer":"/total"}}}
```

Use `mcnemar_exact`, `inputs.b`, `inputs.c`, and `field: p_value` for paired discordant counts.
Receipt recipes do not execute code from the lane. Printed precision controls rounding tolerance.
Arithmetic checks are additional evidence; they do not replace receipt recomputation.

## Artifact and delivery checks

The audit checks inline code paths and Markdown links. It expands brace lists and filesystem globs.
Relative paths resolve against the lane, repository, and each explicit `--receipt-root`.
Missing paths fail. Unresolved placeholders remain warnings.
The audit records file hashes and modification times.
It uses a dated claim or a matching STATUS citation before the report modification time.
Times without dates use the report date. The output labels that assumption.
A later modification time means the artifact changed after that claim, not necessarily that it was absent.
An earlier modification time does not prove historical existence.

The audit queries each cited PR through `gh pr view` and saves the response and command.
It compares explicit state and head claims. CI claims require successful checks and both required gate names.
A merged claim requires the merge commit to be an ancestor of freshly fetched `origin/main`.
Current GitHub state cannot prove a past state. Missing or inaccessible PR evidence fails closed.
GitHub calls are read-only; the command updates the repository's remote-tracking ref with `git fetch origin main`.

## Recursive route checks

The audit streams every JSONL under the lane, including nested helper directories.
Only assistant messages count as served turns. A requested `model_change` does not prove the served model.
The output lists each session hash and its provider/model counts.
Unknown served routes, malformed session records, paid routes, and off-policy routes remain visible discrepancies.
DeepSeek, OpenRouter, Meta, and unknown providers count as paid-route risks.
Anthropic and Google subscription routes still require an explicit allowed-route policy.
Omitting `--allowed-route` leaves the route policy unverified.
A reported zero provider count receives a scope warning when whole-lane totals contain that provider.
The audit does not compare a time-scoped or worker-scoped zero with whole-lane totals.
Session records attest recorded turns, not provider billing or authorization.

## Exit status and limits

- `0`: all supported checks verify, with no unresolved evidence.
- `1`: discrepancies or incomplete evidence; inspect both output files.
- `2`: invalid input or command failure.

Free-form prose cannot always identify its receipt cohort without a mapping.
The audit reports that gap instead of selecting a matching number from an unrelated receipt.
It does not infer past file existence, scientific label validity, or authorization from current files.
