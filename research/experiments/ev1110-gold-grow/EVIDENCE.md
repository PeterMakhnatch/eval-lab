# Action-v2 reader evidence

The single reproducibility gate passes: 30/30 binary matches and all 3 positives admitted.
All 8 gate citations resolve to executed tool calls.
The lane makes 0 prompt revisions and reads 0 development trajectories before freeze.

A1 permits continued reading after representation-only citation failures.
The exact-substring check stays unchanged. Failed citations block row admission.
Part B does not start before Part A ships.

Part A: 530 rows receive 2 reader outcomes from 1060 calls.
Invalid reader answers: 24. These rows cannot become controls or positives.
Binary agreement: 478/512 valid pairs.
Mechanism agreement: 427/512 valid pairs.
Citation resolution: 483/496.

| Source | Executed-positive | Reviewed-control | Attempted | Unjudged |
|---|---:|---:|---:|---:|
| c1c5 | 1 | 2 | 0 | 4 |
| cheatbench | 13 | 67 | 0 | 70 |
| goodfire | 0 | 0 | 1 | 68 |
| terminal-wrench | 100 | 121 | 7 | 76 |

The public Gold loader admits these rows in protocol-separated sources.

## External receipts

- Gate result SHA-256: `3b516c8d6376a822069594c8895aa4291d6a6cbb0af9a8903cbdd086b9f695d6`.
- Part A result SHA-256: `528a667b1e7d288d4132ed01124bdfc7356889d1689568a79941fe6962374ebd`.
- Compact label view SHA-256: `371dc0da7dcdd847f176ed6c593d1cd9e26fd347ac75a9c84f96bf1078e4c85f`.
- Artifact index: `/Users/petermakhnatch/Developer/eval-lab-results/gold/grow-v1/action-v2-artifact-index.jsonl`.
- Artifact index SHA-256: `b2eda23a49c851b83bf1267bf915e1029ee858d9888573dd167df53afce8f40a`.
- Indexed files: 4463. The index binds the complete request, response, packet, and receipt bytes.

Every compact label row identifies `action-v2` and binds both reader receipts and responses.
The original `action-v1` labels and all publisher labels remain unchanged.

## Stop state

```json
{
  "batch_start": 525,
  "checked": 496,
  "fabricated": [],
  "failed": 13,
  "fatal": [],
  "reason": "A1 stop"
}
```
