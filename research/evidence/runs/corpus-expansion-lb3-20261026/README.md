# LB-3: Goodfire passage labels on the exact steps (DEV only)

Peter asked whether Goodfire points at specific reward hacks and how trusted data can
tune live monitoring. It does: the publisher's three-vote LLM judge labels individual
passages as `positive`, `ambiguous` or `negative`, 606,332 passage records in all.

## What was already stored, and what this adds

LB-1/LB-2 ingestion already put every passage record (quoted text removed) on the step it
addresses, as `trace_steps.flags_json.source_passages`. The passage count in the database
(578,705 DEV + 27,627 held) equals the publisher's 606,332. What was missing was a flat,
queryable mark and the exact step lists. `tools/hydrate_goodfire_passage_labels.py` derives
them from the publisher's own `consensus` field and nothing else:

| Where | Field | Meaning |
|---|---|---|
| step `flags_json` | `passage_positive`, `passage_ambiguous` (bool) | the step has at least one passage with that consensus |
| step `flags_json` | `passage_labels` | counts of positive / ambiguous / negative passages, positive channels, ambiguous reasons, strongest positive vote (null where the protocol has no votes) |
| trace receipt `verified_receipt_json` | `passage_step_labels` | exact `positive_step_indices`, `ambiguous_step_indices`, `positive_turn_runs` and `ambiguous_turn_runs` as `[first, last, turns]`, with counts |

Marks exist on every step that has a passage record (236,771 steps), never elsewhere. The
receipt block exists on every judged trace (3,665 of 3,674; 9 traces are unjudged by the
publisher and stay untouched). Labels stay provisional publisher source labels: `status`,
`is_exploit` and every other column are unchanged.

Turn runs, not consecutive indices: transcripts alternate assistant turns and tool results,
and every passage sits on an assistant turn, so all 10,681 positive steps are isolated by
step index. A "span" of consecutive indices is always length 1, so adjacency is taken over
assistant turns (`positive_turn_run_length_histogram` in the receipt).

## Result (production, 2026-10-26)

| Quantity | Number |
|---|---|
| DEV Goodfire tasks targeted (production admission gate) / refused / held and not touched | 1,048 / 0 / 95 |
| DEV traces / judged / unjudged (untouched) | 3,674 / 3,665 / 9 |
| Passage records on DEV steps / steps carrying one | 578,705 / 236,771 |
| Positive passage records / positive steps / assistant-turn runs | 11,889 / 10,681 / 8,766 |
| Ambiguous passage records / ambiguous steps / runs | 10,621 / 9,737 / 8,198 |
| Traces with at least one positive step / one ambiguous step | 2,483 / 2,461 |
| Positive steps per positive trace (min / median / mean / max) | 1 / 3 / 4.3 / 33 |
| Marked steps that are not assistant turns | 0 |

Per-run numbers are in `hydration-receipt.json`; the cross-tabs against the publisher's
verdicts are in `base-rates.json` (for example, 1,609 of the 1,636 enacted-exploit traces
carry a positive step, 639 traces whose enacted verdict is `no` (and 235 `split`) do too,
and no trace whose publisher verdict is `no` or `split` has one).

## Checks that had to pass

- Before any write, for every judged trace the derived passage, positive and ambiguous
  counts equal the publisher's own `n_passages`, `n_passages_positive` and
  `n_passages_ambiguous` (0 mismatches over 3,665 traces; any mismatch aborts).
- Only tasks that are DEV in the database **and** admitted by `admitted_cloud_task_ids` over
  `CorpusRegistry` (the function retrieval uses) are touched; the plan reads step flags and
  receipts only, no trajectory text.
- A before/after fingerprint of every row of all four tables, with this script's additions
  removed, is identical (so nothing but the additions changed); row counts are identical
  (3,707 / 252 / 4,971 / 595,255); SQLite and all four FTS indexes pass their integrity checks.
- Every written trace was read back through `CloudTraceDatabase` and compared; a second pass
  recomputed counts, step lists and runs from the stored rows and compared them to the plan;
  no marks exist on held Goodfire rows or on any other dataset.
- The same pipeline ran first on an APFS clone (`clone-rehearsal-receipt.json`).
- A consistent pre-write backup was taken (integrity `ok`); it stays in private state.

## Consumers

`CloudTraceRetriever` currently decides "flagged step" with `step["flags_json"]` being
truthy. Every one of the 550,955 DEV Goodfire steps has a non-empty `flags_json`, so that
test marks 100% of steps; `passage_positive` marks 10,681 (1.9%). Retrieval and the live
monitor must read `passage_positive` (steps) or `passage_step_labels` (traces) to return a
cheat window. This change does not alter retrieval code; `monitor_worker.py` does not read
step flags at all.

## Files

`tools/hydrate_goodfire_passage_labels.py` (plan by default, `--apply` writes),
`hydration-receipt.json` (production), `clone-rehearsal-receipt.json`, `hydration-log.jsonl`,
`base-rates.json`, `archive-manifest.json` (SHA-256 of each file). No model was called, no
Docker container ran, no network request was made, and no trajectory text was printed or
stored.

## Limits

The marks are as good as the publisher's three-vote LLM judge; they are not replay-confirmed
and `positive` does not mean the cheat was enacted (639 traces with a positive step have no
enacted exploit). Passages sit on assistant turns, so a window around the cheat's effect on
the next tool result is not marked. Meta's HackBench is a separate, gated dataset and is not
part of this change.
