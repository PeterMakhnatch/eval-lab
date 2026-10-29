# NP23: a defined, rendered-text leak check and a corrected retrieval audit

Read-only, zero model calls, zero spend. Store: the read-only copy of the cloud trace
store (4,207,423,488 bytes), assignments digest `sha256:7f4703f6…880f` (equals SB-5's).
Counts only; ids appear as short hashes, no retrieved content.

| file | what |
|---|---|
| `src/evallab/production/retrieval_leak_check.py` | the definition: fixed fields, tokens and per-example values, searched in rendered text |
| `tools/audit.py` | the corrected audit (below) |
| `results/audit-{pre-np22,main,after}-summary.json` | the same audit on three source trees: `98aeca5c9` (NP22 base), `25f92764b` (main with NP22) and this change; full per-cell results live in the bridge `raw/np23/` |

## What was wrong with SB-5's design

One query (`weak_check`) was used for all 44 targets, so the 132 Atlas examples were the
same top results repeated 44 times and none of the targets was among them. A target that
was never a candidate cannot show that exclusion works. The corrected totals: 132
primary-mode retrieval calls (44 × 3 modes) returned **484 examples** (M1 352, M2 132, M3 0);
the preregistered G2 gate counts **132 violating examples**, not 264 fields.

## Corrected audit

Two panels of 44 targets (the SB-1 cohort; 44 store-resident DEV tasks with traces),
each target queried by its own task name, four modes (lexical; `facets` filter;
`facets={}`; cluster + `like_run_id` anchor), each with and without excluding the target,
top-k 8: 704 calls per tree. Positive control: the same calls without the exclusion.

| measure (excluded, 352 calls) | pre-NP22 `98aeca5c9` | main `25f92764b` | this change |
|---|---|---|---|
| examples / distinct traces / distinct tasks | 1,561 / 170 / 138 | same | same |
| same-task examples | 0 | 0 | 0 |
| examples failing the **shared** profile | 1,561 | 1,561 (`dev split` header only) | **0** |
| examples failing the **blind** profile after the blind adapter | 1,561 | 1,561 | **0** |
| shared block failing the **blind** profile (by design) | 1,561 | 1,561 | 1,561 |

| positive control (no exclusion, 352 calls) | pre-NP22 | main | this change |
|---|---|---|---|
| calls answered / refused (no named exclusion) | 352 / 0 | 88 / 264 | 88 / 264 |
| same-task examples returned | 123 | 76 | 76 |

## Not covered

- The cluster + anchor mode returns no example for any of the 88 targets (0 of 88 calls
  yield an example), so cluster exclusion is exercised only by synthetic tests.
- The `facets` filter mode reaches only 9 distinct examples per panel; the store holds few
  traces matching the SB-5 filter, so its same-task check stays weak.
- The driver's own retrieval (`CloudTraceRetriever.retrieve`, M1 in SB-5) was not re-run:
  its DEV projection is the slow step (see the NP22 receipt) and it returns the driver's
  lifecycle JSON, not retrieval examples.
- Step bodies are evidence and are not gated; the check does not test whether a model
  reading the block can infer the source (no model was run).
- Identifiers stay readable in the shared block, so a QA worker still sees the dataset name
  inside them and the word `reward`; the shared block fails the blind profile on purpose.
