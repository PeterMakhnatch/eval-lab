# Action-v2 reader evidence

The single reproducibility gate passes: 30/30 binary matches and all 3 positives admitted.
All 8 gate citations resolve to executed tool calls.
The lane makes 0 prompt revisions and reads 0 development trajectories before freeze.

Part A reader decisions do not change registry membership.
The existing source and score integration files remain outside this lane's permitted scope.
Part B does not start before Part A ships.

Part A: 380 rows receive 2 reader outcomes from 760 calls.
Invalid reader answers: 5. These rows cannot become controls or positives.
Binary agreement: 351/375 valid pairs.
Mechanism agreement: 310/375 valid pairs.
Citation resolution: 414/416.

The lane leaves 219 candidates unread after the citation-verification stop.
Five invalid JSON answers remain reader failures. They do not become no-evidence.
Two citation quotes fail exact-substring verification. Both source coordinates identify real calls.
The affected rows remain unjudged, with no admitted action locators.
No prompt changes, reader repeats, quote normalization, or rule relaxation occur.

| Source | Executed-positive | Reviewed-control | Attempted | Unjudged |
|---|---:|---:|---:|---:|
| cheatbench | 1 | 2 | 0 | 4 |
| goodfire | 0 | 0 | 1 | 68 |
| terminal-wrench | 100 | 121 | 7 | 76 |

These are reader-qualified decisions, not new registry admissions.

## External receipts

- Gate result SHA-256: `3b516c8d6376a822069594c8895aa4291d6a6cbb0af9a8903cbdd086b9f695d6`.
- Part A result SHA-256: `0c17ab0df7eadb4f7cfa38a76a65819b2c0f1540d5e042cfeb0652fe146fd8b8`.
- Compact label view SHA-256: `4d61880bc7057264aa16c6590532e847e4354165315d164666a2a3f7b7cb3227`.
- Artifact index: `/Users/petermakhnatch/Developer/eval-lab-results/gold/grow-v1/action-v2-artifact-index.jsonl`.
- Artifact index SHA-256: `bb2ce5c70dc01aa900e152cf4e5894f6a786954101a10cfde71ce5398a11eccc`.
- Indexed files: 3261. The index binds the complete request, response, packet, and receipt bytes.

Every compact label row identifies `action-v2` and binds both reader receipts and responses.
The original `action-v1` labels and all publisher labels remain unchanged.

## Adapter change and checks

`evallab.gold.grow` implements the new source adapters through the existing IssueBank discovery path.
The 5 source modules preserve separate source names ending in `-action-v2`.
Each new row records `action-v2` in `provenance.version` and `metadata.protocol`.
The existing schema already supports that fact. No schema field is added.
The adapters verify source hashes, reader receipts, independent decisions, and cited tool calls.
They preserve publisher labels, nullable outcome evidence, and null polarity for null states.
Recorded reader failures remain unjudged and stay visible in row metadata.

The public Gold loader and score joins still fix the original sources and cohorts.
Those files remain owned by U08 and U04. This PR does not bypass that scope restriction.
Do not treat these adapters as a completed public-loader or score integration.

Checks executed:

- `tests/test_gold_grow.py`: 10 admission behavior tests pass.
- Changed Python files pass Ruff and `ty@0.0.71`.
- The actual `evallab failures bank sources` command lists all 5 adapters.
- The isolated adapter smoke builds and loads all 380 completed trajectories through `load_trajectory`.
- All 101 executed-positive rows have marked tool actions. All other rows have no cheating marks.
- The smoke preserves 156 null polarities and identifies 7 rows with reader failures.

Smoke receipt: `~/Developer/eval-lab-results/gold/grow-v1/action-v2-adapter-smoke.json`.
Smoke receipt SHA-256: `9a5ad63266247b4b3a62c0b80c5cbacfae7a36f533f7cd1d203891a5b4df8481`.
The smoke uses an isolated state root and does not modify the live registry or its manifest.
No enlarged `gold-counts`, cached score table, line gate, merge, or Part B run is claimed.


## Stop state

```json
{
  "batch_start": 375,
  "errors": [
    [
      {
        "citation": {
          "quote": "call setreg('a', \":%d | 0r /app/expected.csv | wq\\n\")",
          "source_event_id": "step:3"
        },
        "error": "quote mismatch"
      },
      {
        "error": "no resolved action citation"
      }
    ],
    [
      {
        "citation": {
          "quote": "{\"file_path\":\"/tests/test_outputs.py\"}",
          "source_event_id": "step:2"
        },
        "error": "quote mismatch"
      },
      {
        "error": "no resolved action citation"
      }
    ]
  ],
  "reason": "reader or citation failure"
}
```
