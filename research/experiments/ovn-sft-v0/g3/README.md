# G3: frozen SFT set (HAR-127)

Frozen 2026-10-01 around 11:05Z under Research-Harbor's 10:23Z ruling.

**At this size no measurable effect is expected.** G4 and G5 exercise the stock-vs-LoRA process end to end; they don't test whether the method works.

## Dataset

| | |
|---|---|
| `conversations.jsonl` | `~/Developer/eval-lab-results/2026-10-01/ovn-g3/g3/conversations.jsonl` (3.2 MB, not committed), `sha256:71a9f7073a0ce4e2a12865fc2a0a74881986ec30067a18bf7e78a528a3875c94` |
| `manifest.json` | here, `sha256:567d64a4b78a3775dae89596787851e8e04578a111774932c6d3494af07a522b` |
| rows | 145, one model call each, `{"messages": …, "loss": "last"}`; the target carries `reasoning_content` |
| trials / tasks | 5 / 5, all G2 `captured` passes |
| sequence tokens | 1,569,955, of which 39,913 are target tokens; stride 1 (every eligible call) |
| optimizer steps | 1 epoch at accumulation 16: 10 (9 if the last partial step is dropped) |

## How it was built (`research/experiments/ovn-sft-v0/`)

1. **Immutable capture.** `capture-snapshot/calls.jsonl` is a read-only copy of `har126-capture/g2/calls.jsonl`: 292,044,252 bytes, seq 1–4319, prefix-identical to the live file when copied. All 4,319 calls are attributed by `route_token` across the 71 jobs that used the capture (64 HAR-120 and 7 HAR-135 C1); 0 are unassigned.
2. **`qualify_reconstruction.py` → `qualification.json`.**
   - 32 trials reproduce completely from ATIF.
   - Three trials do not (001269-a1, 001399-a1, 001897-a1). In each, the harness re-asks after an empty-content reply, and ATIF records neither that call nor the nudge.
   - So `admit_reconstructed` is false and the historical passes are out.
3. **`build_selection.py` → `selection.json`, `selection_exclusions.json`.** A trial is selected when it meets all of these:
   - `counted_pass`;
   - in the train split and `usable` in the ledger;
   - Traces-clean, with the kept window ending in a completion (`labels_g2_a1`, `labels_g2_r2`, `labels_g2_tail`; sha256s are in `selection.json`);
   - its own capture reproduces completely.
4. **`freeze_sft.py` → `freeze.json`, `data_card.md`, `fidelity.json`.**
   - On all 145 rows, prompt and completion tokens are exact and the captured bytes are identical, with one delivered call per row.
   - 0 targets stopped at `finish_reason=length`.
   - `freeze.json` records the producer digests and the capture bounds.

## Known limitations

- **Only 5 trials.** 000838-a1 is clean but its kept window has no completion (agent timeout). 002552-a1-r2 is degenerate.
- **`copied_fix` verdicts are as of the freeze.** Cdx 1's successful-fetch fix may later flip excluded raw passes, such as 001373-a2-r2 and 002356-a1-r2. Tonight there is no re-freeze.
- **Proposal:** export G2 trials directly from captured bodies, so the harness nudges the model saw are kept.
