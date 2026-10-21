# qa-trace-sft-v0 — leak-free trace SFT/eval dataset

Task: given a trajectory (**actions-only view**, plus a candidate hackbook
entry when a mechanism is to be checked), output
`{label, mechanism, evidence:[{step, quote}]}` with
label in `exploit-pass | defect-fail | neither`. Mechanism-targeted judging
follows the Library notes in `ideas-feed.md` (Trace QA section): index/flag
first, judge the candidate mechanism, cite steps, abstain via `neither` only
when the checked mechanism is not witnessed in actions.

## Sources and licences

| source | rows used | licence / provenance |
|---|---|---|
| TB4 DEV census (PR #1137, `labels-dev.jsonl`) | sampled from 2,269 labelled DEV rows | public Terminal-Bench 4 Hub trajectories, **22 DEV tasks only**; labels are mechanism-supported, **not replay-confirmed** |
| `cheatbench-actions-v1` (dev+test) | 300 | MIT — steinad/CheatBench @461360b; cheating/attempt → `exploit-pass`, benign → `neither` (benign ≠ correct) |
| `hacktrace-act-v1` (dev+test) | 300 | CC BY 4.0 — JH012/HackTrace-Data @291cf50 (Jiang et al. 2026; CodeContests Li et al. 2022); ACT → `exploit-pass`, negative → `neither` |
| `mimo-v7-canonical-pass70`, `mimo-fresh-window3` | **0 — excluded** | both keys are all-`test` (70 + 52 items); **no dev split exists**, so including them would be test-only training contamination |

## Hard rules (leak discipline)

- No `tb4-audit-v1` TEST task, finding, or trajectory is read or included.
  TB4 rows come only from the DEV-only census export.
- Split is **by task/source group** (sha256 bucket 70/15/15). Source `test`
  items (cheatbench/hacktrace) go only to this dataset's `test` split and
  are never in train. No group straddles splits.
- Inputs contain only actions (assistant prose/reasoning stripped for
  cheatbench/hacktrace; agent tool calls for TB4) and the candidate
  hackbook entry. Labels/mechanisms live only in `target`.
- `neither` means *no checked mechanism was witnessed*, not "clean".
  TB4 `unmeasured` / `no_documented_mechanism` rows (1,581) are excluded,
  never relabelled clean.
- This release grants **no training authorization**; see the training
  decision in the Trace QA lane bridge file.

## Files

- `build_dataset.py` — stdlib producer ($0, reads published keys + results home)
- `train.jsonl` / `dev.jsonl` / `test.jsonl` — one example per line (see `manifest.json` for counts)
- `baseline.py`, `baseline-results.json` — zero-shot cheap-model baseline (dev, then test once)

Rebuild: `python3 research/datasets/qa-trace-sft-v0/build_dataset.py`
