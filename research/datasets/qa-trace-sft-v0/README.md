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

## Entry point: `evallab qa trace-cheap <trial|dir>`

Single cheap-trace-QA command (code: `src/evallab/qa/trace_cheap.py`):
tripwires prefilter ($0, deterministic) → cheap judge in **confirmation
mode** when the caller supplies a candidate or the hackbook matcher ranks
one, opt-in **proposed mode** (`--propose`: tripwire hackbook hits,
hackbook matches and `--retrieval-db` DEV analogues as a lead list), or
**discovery mode** (whole hackbook short list) otherwise → optional
`--escalate-model` second stage for flagged or
ambiguous answers → one `qa.finding/v1` finding with step citations and
the mode stated in its summary. `--offline` runs only the $0 stages;
otherwise the judge is an OpenRouter model (`--model`, default
`openai/gpt-oss-120b`) using structured JSON-schema output + one repair
retry, with cite-the-step-or-abstain enforced. The proposed mode and the
cascade are measured in `research/experiments/oq3-discovery-cascade/`.

Measured on this dataset (same stratified samples; test run once per frozen
version; zero-shot → harness v2 confirmation → v3 discovery/defect):

| model | exploit recall: zero-shot → confirmation → discovery | false alarms (neither) | defect recall |
|---|---|---|---|
| GPT-OSS-120b | 0.05 → **0.75** → 0.20 | 0.25 → 0.05 → 0.30 | 0 → 0 → **1.00** (defect variant, n=7) |
| GLM 5.3 Flash | 0.25 → 0.30 → 0.20 | 0.45 → 0.00 → 0.00 | 0 → 0 → 0.29 |
| Qwen 3.6 35B-A3B | 0.00 → 0.15 → 0.00 | 0.85 → 0.05 → 0.10 | 0 → 0 → 0.00 |

Caveats — read before trusting a finding:

- **Confirmation ≠ discovery.** Most of the confirmation gain is the
  candidate: without it, GPT-OSS exploit recall falls 0.70→0.20 (same
  subset) and false alarms triple; mechanism-id match in discovery was
  0/4 even when the label was right. A discovery-mode flag is a lead for
  review, not a verdict of the same strength.
- Defect recall was 0 for every model until the v3 defect variant added
  verifier output + wrong-fail hackbook entries; GPT-OSS then hit 7/7 on a
  small TB4-only test sample (n=7 defect + 7 neither). GLM/Qwen defect
  gains remain capped by output-envelope (parse) failures.
- `neither` means the checked mechanism was not witnessed in the actions
  shown — it is never evidence that a run is clean, and TB4 labels behind
  this dataset are mechanism-supported, not replay-confirmed.
- Samples are small (20/label dev, 50 test for v2; 10/label and 7/label
  subsets for v3 under budget caps). Treat rates as directional.
