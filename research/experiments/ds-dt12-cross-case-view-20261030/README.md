# DT-12 — cross-case view: duplication, mechanism families, taxonomy coverage

DeepSeek-squad item DT-12 (`research-context/bridge/2026-10-29-ds-squad/raw/DT-12`).
Descriptive, no gate. Full report, prereg and receipts live in the bridge directory;
this folder holds the code and text-free summaries.

## What was measured

1. **Duplication (effective n).** Each of the 300 DEV runs the DT-8 reader saw
   (Goodfire 150, CheatBench 150) is fingerprinted by its normalised tool-call
   sequence — every `<call NAME>` block, command lower-cased, split on shell operators,
   hex hashes / digit runs / temp paths replaced — then clustered by exact fingerprint,
   TF-IDF cosine >= 0.90 and >= 0.80, per corpus.
   Result: Goodfire 150/150 independent (max pairwise cosine 0.41); CheatBench 137
   clusters at 0.80, and the loss is all on the positive side (75 positives ->
   64 independent action sequences). No exact duplicates, no cross-corpus duplicates.
   DT-8's headline re-scored per cluster: Goodfire 15/80 at 0/70 (unchanged);
   CheatBench 40/63 = 63.5% at 12/73 = 16.4%, against 42/74 = 56.8% at 12/75 run level.

2. **Mechanism families.** Agglomerative (average-linkage) TF-IDF clustering of the 162
   verified quotes and the 210 distinct v1 seeker scripts; one
   `deepseek/deepseek-flash` call per cluster (40 calls, 20 largest clusters per set)
   names the family. The quote population is dominated by one unmapped behaviour
   (overwrite the input with the expected output, 28/162); the seeker population by two
   (`pytest` conftest / exit-status override, 53/210, and blind probing, 21/210).

3. **Coverage.** C09 sandbagging, C10 loop/runaway, C11 infrastructure and C12 other
   have **zero** examples across quotes, quoted positive runs and seeker attempts;
   C03 is zero at run level. Seven OpenAI categories show zero, three of them only
   because the prereg mapping sends them to C12.

## Deviation from the prereg (recorded)

The prereg fingerprinted only the first two tokens of each command. On CheatBench, where
every run makes a single `bash` call to the `forge` harness, that truncation collapses
all runs to one fingerprint and invents duplicates by discarding the payload. The three
duplication tiers therefore use the full normalised command; the 2-token-head clustering
is kept and reported as `skeleton`, a structure measure only.

## Files

| file | role |
|---|---|
| `dt12_dup.py` | fingerprints, three-threshold clustering, effective n, cluster-level DT-8 re-score |
| `dt12_fam.py` | TF-IDF + average-linkage clustering of quotes and seeker scripts |
| `dt12_name.py` | one `deepseek/deepseek-flash` call per cluster (<=40), served-model readback |
| `dt12_assemble.py` | names -> keyword-table mapping -> `clusters.json`, attestation |
| `dt12_cover.py` | per-category counts for quotes, quoted runs, seeker classes, OpenAI rows |
| `dt12_length.py` | cluster-level DT-8 alerts by run-length tertile |
| `receipts/` | text-free outputs (ids, sizes, top terms, names, counts) |

Run order: `dt12_dup.py` -> `dt12_fam.py quotes|seeker` -> `dt12_name.py` ->
`dt12_assemble.py` -> `dt12_cover.py` -> `dt12_length.py`. Inputs are the DT-8 / DT-2 /
DA-3b receipts and `/tmp/dt8/prompts` under the bridge directory; no run text or quote
text is stored here.
