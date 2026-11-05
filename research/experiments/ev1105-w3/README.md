# W3 dataset admission: real acceptance run

The workflow reproduces the HackBench #1888 intake counts from a fresh download.
The gated Impossible-SWE data download refuses admission with HTTP 403 and exit 2.
The workflow also refuses scoring before the registration reaches a remote branch.

## Frozen custody

- Source: `facebook/hackbench`, revision `b3d6f678e6359dcd35e20f3f2c5ee085b5af4934`.
- Fresh data: 408,528,473 bytes; SHA-256 `42c0cfa4a3dc822eb6505fbcc58989c5df5dcf509cc4a97338abbb38c7e9fd87`.
- Registration: `freeze.json`, committed and pushed at `584e8753d644fff00c7c60f6178516ced3038572` before scoring.
- Existing canonical cache remains unchanged. The fresh download stays under the external output directory.
- The workflow reuses the functional #1888 adapter, positive-only label map, and DetBench scoring functions.
- The registration freezes current implementation hashes and historical DEV thresholds. It does not claim historical code identity.
- License: CC-BY-NC-4.0, non-commercial evaluation only, with additional no-training and no-rehosting terms.
- Only aggregate registration and results enter Git. Source bytes, publisher terms, views, and per-record outputs stay local.

## Exact admission census

| Quantity | Observed |
|---|---:|
| Released positives | 169 |
| Released negatives | 0 |
| Command blocks | 60,541 |
| Natural / synthetic | 41 / 128 |
| Distinct raw transcripts | 166 |
| Prior detector exposure, exact CM-11 ID join | 169 / 169 |
| Unmatched prior records | 0 |
| Conversion failures / exclusions | 0 / 0 |

These are complete-release census counts, not sampled estimates. No sampling interval applies.
All records have prior detector exposure. This is not a detector-unread source.

## Frozen local scoring

The full receipt contains every frozen local signal, Wilson intervals, task-cluster intervals, and matched length baselines.
`aggregate-summary.json` preserves the admission census and selected measurements with a digest of that receipt.

| Measurement | Result | Task-cluster 95% interval |
|---|---:|---:|
| L0 hard union, native threshold | 33 / 169 = 19.5% | 5.1–42.2% |
| L0 any rule, native threshold | 60 / 169 = 35.5% | 18.3–63.1% |
| Character length, frozen DEV 5% threshold | 155 / 169 = 91.7% | 82.4–96.8% |
| Step length, frozen DEV 5% threshold | 153 / 169 = 90.5% | 79.3–96.4% |

L0 scores 150 records and retains 19 watchdog timeouts as abstentions in the denominator.
Both length baselines score every record. No retry changes the observed timeout results.
Intervals use 1000 task-cluster bootstrap resamples, seed 1104.
No labelled negatives exist, so AUROC, FPR, and superiority over length remain unavailable.
The full line abstains as incompatible. No model calls, fallback routes, or paid compute run.

## Receipts and commands

External receipt root: `~/.local/state/evallab-ev1105/w3/`.

- `controls.json`: observed commands, the real gated 403, and the unpushed scoring refusal.
- `admission/access.json`: successful real-data access probe.
- `admission/source-download/`: complete fresh pinned release, verified against the canonical cache.
- `admission/manifest.json`: SHA-256 digests, byte sizes, row counts, license, and publisher checks.
- `admission/publisher-terms.md`: pinned publisher license and additional terms.
- `admission/exposure.json`: exact prior-source exposure census and receipt digest.
- `admission/converted/conversion-report.json`: conversion census.
- `admission/SCORING.json`: registration commit and freeze identity before detector execution.
- `admission/aggregate-results.json`: complete local scoring and length comparisons.

Run commands appear in [the command guide](../../../docs/dataset-admission.md).
The focused boundary suite is `uv run pytest tests/test_admission.py -q`.
