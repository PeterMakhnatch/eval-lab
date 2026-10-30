# Full-store exporter audit

## Question and answer

Do run exports and cluster-scope exports preserve admission, determinism and
identity boundaries, and how do batched CLI exports scale? On the October 28
read-only store copy, all 4,544 admitted runs exported with zero missing or extra
runs. Twenty facet filters selected 8,312 overlapping run records with zero
missing or extra exports. All 68 clusters preserved their 3,435 admitted member
rows. No application change is proposed: no parity defect or undeclared content
nondeterminism was found. This is evidence, not a new admission policy.

Source revision: `4fbccb6320c318b0691ae13d27c4c7a76da5ae77`.
Store: 4,207,423,488 bytes; SHA-256
`06ec014faf70953abe63145c80f9f3ac3f68590989d958ec2517d149bd04ab3e`.
The directory date is the assignment's evidence identifier; the completion time
is recorded in `results.json`. Only counts, public field names, timings and
source/provenance digests are retained. No run identities, task names, trace
content, matching seal values or export bytes are included.

## Determinism and parity

- A 500-run selection, batched 200/200/100, was exported twice through the real
  `uv run --no-sync evallab qa-data export` CLI using ids files. All 500 blinded
  files and three sealed manifests were byte-identical (503 comparisons).
- Including the three stdout evidence replies produced three raw differences.
  Repeating the full pair while normalizing only the explicitly measured
  `meta.latency_ms` yielded zero differences in 506 comparisons. This is the
  declared latency field emitted by `qa_data._reply`, not changing export
  content. Transcript/manifest comparisons remain raw SHA-256 comparisons.
  Normalized reply comparisons use parsed, sorted JSON. No timestamp was
  removed from any export file. Explicit selection preserves request order;
  canonical reversed-order behavior is not promised and was not required.
- Whole-population parity used read-only admission plus identity-column selection,
  then explicit batches of at most 200. Facet parity used `search_runs` for the
  expected set, the real facet exporter for the first batch, and explicit IDs
  for subsequent batches (the CLI has no facet offset). All 20 filters completed;
  the store served 1,234 distinct facet records. A separate real-CLI facet call
  selected/exported 50 runs with zero missing/extra rows.
- The admitted run whose facet record is withheld exported with zero evidence
  citations. An admitted task whose identifier contains a sealed-name substring
  also exported with zero evidence citations. These are admitted bodies, not a
  promise that every admitted run has a served facet record.
- One nonadmitted ID and one missing ID each refused the entire request even
  beyond `limit=1`, publishing neither output nor manifest. Counts only were
  retained. Cluster-scope parity found zero refused members in this snapshot.

## Exposure and policy boundary

The scanner reuses the retriever's `_sealed_pattern` over its current admission
seals. It scans serialized text and JSON decoded/re-serialized with Unicode
unescaped, without printing matching content. Counts are file observations,
including repeated exports, not distinct run counts:

| File kind | Scanned | Matching files |
|---|---:|---:|
| Blinded transcript/format files | 27,259 | 0 |
| Sealed manifests | 160 | 0 |
| Evidence replies | 156 | 0 |
| Cluster-scope files (two per cluster) | 136 | 6 |

The six cluster-scope hits are **class (b)**: an admitted identity contains a
sealed name as a substring. Removing only the identity fields from each document
leaves zero matching files: no withheld facet text or sealed identity itself was
observed (**class (c): 0**). Cluster-scope files are intentionally metadata-only,
not blinded exports. Both runs of every cluster were byte-identical.

An earlier, rejected policy experiment withheld those admitted substring members:
3 of 3,435 rows would be refused and the six matching files would disappear.
The parent explicitly declined this admission-policy change. It was reverted
before completing this audit; the final cluster pass remeasured unchanged code.
Research may evaluate that option separately. This report does not claim literal
substring-freedom for identity-bearing scope files.

ATIF was audited over the whole admitted population. Additional Harbor and Scout
samples exported 50 runs each with zero parity gaps and no seal hits; these are
format smoke checks, not whole-population coverage of those two renderers.
No original/unblinded body export was requested or inspected.

## Throughput and hotspots

Real CLI, ids-file selection, three repeats per size, serial batches capped at
200. CPU is `os.wait4` child user+system time summed across batches; peak RSS is
the maximum child high-water mark (Darwin bytes), **not** summed batch memory.
The parent scanner and hashing work are outside the timed CLI interval. Median
CPU/wall seconds and maximum RSS across repeats:

| Runs | CPU s | Wall s | CPU s / 100 | Peak MiB | Runs / wall s |
|---:|---:|---:|---:|---:|---:|
| 50 | 2.775 | 2.883 | 5.550 | 97.89 | 17.34 |
| 200 | 10.605 | 11.072 | 5.302 | 108.53 | 18.06 |
| 500 | 20.083 | 20.922 | 4.017 | 103.94 | 23.90 |
| 1,000 | 38.623 | 41.042 | 3.862 | 104.48 | 24.37 |
| 2,000 | 73.464 | 78.395 | 3.673 | 109.08 | 25.51 |

`results.json` also retains every repeat, normalized RSS/100 runs, and per-run CPU
growth relative to 50 runs (0.662 at 2,000). RSS/100 is a normalization only, not
an allocation estimate. No super-linear growth appears in this series; prefix
samples differ in body size and batching amortizes startup, so this is not an
asymptotic complexity proof or a controlled optimization speedup claim.

A separate cProfile pass covered 2,000 runs in twenty 100-run in-process batches.
Top three self-time hotspots were `builtins.next` (33.333 s),
`admitted_cloud_task_ids` (14.509 s), and `CorpusRegistry.task_name` (10.733 s).
Top cumulative functions were the export entry point (157.552 s),
`_admitted_ids` (79.206 s), and `task_of_run` (78.713 s); cumulative times overlap.
Repeated per-ID admission is a visible cost, not demonstrated super-linearity.
No performance code change or >25% identical-byte speedup is claimed.

## Reproduction and checks

Use the locked Python 3.12 worktree environment, no network/model/Docker calls,
and the same read-only snapshot. Scripts expect repository-root cwd, write raw
exports only under `/private/tmp/ps7j2`, and delete completed export batches.
Use a fresh scratch directory/checkpoint for a fresh audit; never overwrite
retained evidence. Copy these helpers there, then run:

1. `python /private/tmp/ps7j2/audit.py init` (baseline scopes and job schedule).
2. Repeat `python /private/tmp/ps7j2/audit.py work --seconds 120` until all jobs
   complete (counts-only JSON progress/checkpoints; each call is bounded).
3. Repeat `python /private/tmp/ps7j2/profile_chunk.py` until profile runs = 2,000.
4. `python /private/tmp/ps7j2/supplement.py` (unchanged cluster policy, format
   samples, real-CLI facet call).
5. `python /private/tmp/ps7j2/summarize.py` checks complete parity, throughput
   coverage and normalized determinism and writes the summary. Its provenance
   constants identify this retained measurement, not arbitrary future stores.

The retained checkpoint includes additional raw and latency-normalized 500-run
pairs scheduled with `repeat_determinism.py`; the final `audit.py` directly
normalizes latency for subsequent pairs. The historical rejected-policy option
is recorded separately, not applied by these scripts.

Existing exporter contract suites: **203 passed, 5 skipped**. No new regression
test fails on main because no product defect/fix is proposed. Full-store exports
and the CLI series are the runtime smoke evidence; tests alone are not the audit.
Static gates are recorded in the PR delivery, not represented as hosted CI green.
Raw scratch exports and measurement checkpoints are removed after delivery;
only this counts-only evidence is committed.
