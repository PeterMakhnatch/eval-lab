# LS-CHEAT-VIS: what reaches the supervisor reader?

## Question and method

Does the installed Live Supervisor reader see the evidence on publisher-labelled cheating steps? This is a **model-free visibility census**, not a detector-recall study. The preregistration was frozen before the label join at `~/.local/state/evallab-fleet/2026-10-29/LS-CHEAT-VIS/PREREG.md` (2026-10-29T23:18:48Z, SHA-256 `8d396102dcf069bdcbb197a1c818b4027b822857bbaa9a790be3f72262e6c7d7`). No model, network, Docker or paid call was made.

The population is all Goodfire traces returned by `run_health.store_runs` under `CorpusRegistry` / `admitted_cloud_task_ids`, using the cross-key ledger SHA-256 `7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f`. This is the admission used by earlier supervisor studies: require the database row to be DEV, reject sealed identities/digests and identities assigned TEST under any key, and enforce an affirmative DEV assignment for datasets which have a ledger key or are registered datasets. Goodfire has **no ledger key** and is not one of those registered datasets, so its database DEV status plus the vetoes is used; this is not an affirmative Goodfire ledger assignment. No held-out or HackBench content was opened.

After admission, the local CAS `passages.cas_uri` in each receipt supplies publisher passage rows and `carrier_quotes`; group positive-consensus rows by original node index, retaining all nonempty quotes. The receipt's LB-3 `passage_positive` marks agree in aggregate: 10,681 steps, of which 8,749 lie on the final parent path, and 1,932 are resampled off-path nodes excluded by `cloud_doc`. There are 3,674 admitted traces, 2,483 with a positive step, nine without a passage CAS URI, and zero missing CAS objects. No on-path positive step lacked a nonempty carrier quote. The earlier supervisor's CheatBench DEV cases have run-level labels but no labelled onset/step in the replay manifest; they cannot be added to a step-quote denominator. Goodfire replay onsets are derived from these same LB-3 marks, not independent labels.

For each on-path step, cut the stored `HealthRun` immediately after that step using `health_replayable`, call the actual Harbor `trial_observation` projection and `live_supervisor_triage.build_prompt`, then match only the labelled event's exact rendered line, not an accidental occurrence elsewhere. No watch or trace alerts are injected, and the rule proposal is `continue`: these are **counterfactual immediate-step windows**, not the actual 0/5 decision windows or a claim that the reader is invoked then. The installed release `b37b3a80b106ccbd0e3b71de1b0f91641cac8951` has identical triage, replay, store conversion and token-flow files; its Harbor file differs in alert-trigger handling, not the event projection used here. The stored adapter attaches tool results to their originating assistant step, which may make an immediate-step prefix more informative than a real event before the result arrives.

All quotes fully visible means every whitespace-normalized quote for that step appears in the labelled event line. Partial means not full but at least 20 contiguous normalized characters from a quote occur there; absent means neither. This mechanical proxy does **not** search separately for shorter key paths, commands or values and is not a semantic judgment; it may overstate meaningful loss or count a generic fragment as partial. No-quote and unrenderable steps have separate buckets; both are zero among on-path positive steps. Labels and quotes stayed local.

## Measured results

| Labelled step tool type | On-path steps | Default full | Default partial | Default absent | Expanded full | Expanded partial | Expanded absent |
|---|---:|---:|---:|---:|---:|---:|---:|
| Bash command | 8,264 | 591 | 787 | 6,886 | 641 | 765 | 6,858 |
| File edit/write | 20 | 1 | 2 | 17 | 1 | 2 | 17 |
| Message / think / submit | 439 | 8 | 44 | 387 | 8 | 44 | 387 |
| Other tool | 26 | 3 | 1 | 22 | 3 | 1 | 22 |
| **Total** | **8,749** | **603 (6.9%)** | **834 (9.5%)** | **7,312 (83.6%)** | **653 (7.5%)** | **812 (9.3%)** | **7,284 (83.3%)** |

Type is derived from the projected tool name; shell writes remain bash, not file-edit calls. The 9,639 positive passage rows on these scored steps include 7,378 `reasoning`, 1,457 `content`, and 804 `tool_calls` channels; steps can have multiple passages/channels. The event projection does not include `reasoning_content`, so much missing evidence may be outside its intended actor-facing view rather than merely cut after 200 characters. These provisional three-vote publisher labels are not proof that a hack was enacted.

The default-off `expanded_tool_text=True` option lets tool arguments compete for the existing 600-character event budget instead of clipping each at 200; message, event, reader and window limits remain unchanged. It may displace later tool output or later calls. On the same 8,749 steps, the number of full-quote cases increases by a net 50 and absent cases decrease by a net 28; individual transitions were not retained. It does **not** address the missing-reasoning issue. Do not enable it on these numbers alone.

| User prompt size, same step windows | Default | Expanded | Change |
|---|---:|---:|---:|
| Median characters | 8,433 | 8,979 | +546 |
| p90 characters | 10,213 | 10,893 | +680 |
| Median estimated tokens | 4,849 | 5,163 | +314 |
| p90 estimated tokens | 5,872 | 6,263 | +391 |

Estimates use 0.575 tokens/character, the previously measured Goodfire B3 ratio, not this reader's tokenizer or observed usage. Sizes are the rendered user prompt only, without the system prompt or real trigger details. The per-window character cap still applies.

## Interpretation and limitations

The measured view frequently lacks the publisher quote even at an optimistically chosen immediate-step window. This is evidence of a visibility limitation, **not** proof that clipping caused the five earlier Opus-versus-reader disagreements, nor a detection-performance estimate. The optional change buys little quote visibility for a larger prompt and does not include reasoning; it is an experiment, not a deployment recommendation. The full corpus is step-weighted (long trajectories contribute more), quote-based and restricted to this Goodfire DEV admission and the cloud adapter; no claim is made about live Harbor traffic or unseen tasks. The five historical adjudicated windows have not been individually joined here.

Aggregates and denominators are in `results.json`; raw run-level material remains local. The local driver and both measurements live under `~/.local/state/evallab-fleet/2026-10-29/LS-CHEAT-VIS/`; the driver was switched from default to the option between runs. Next: inspect whether missing **action-bearing** evidence can be exposed within a fixed budget without exposing private reasoning, then assess actual decision-window detection separately.
