---
name: trace-review
description: Run a blind, frozen, scored trace review of an experiment (arms such as stock/tuned/GEPA, or checkpoints) with evallab review plus parallel trace-rater subagents, and produce the experiment report. Use when an experiment's runs have landed and someone asks what differed, why, or which passes are genuine.
---

# Blind trace review of an experiment

This is the G6 workflow (HAR-128), made repeatable. Raters never see which arm produced a run. Labels and metrics are frozen before the arms are joined. Every tool or model reader is scored against the frozen labels.

## 0. Before you start
- Read `research/explorations/trace-lab/QUIRKS.md` and the `trace-reading` skill.
- Make sure the jobs were processed by a current `evallab process-job`. If they predate a fix listed in QUIRKS, reprocess copies first; never write to `~/Developer/eval-lab-results/`.
- Pick the review directory under `~/Developer/eval-lab/derived/trace-lab/<experiment>/`. It holds packs and the sealed map, and is not committed.

## 1. Prepare (no model calls)
```bash
uv run evallab review prepare --jobs-glob '<results>/<date>/<job-prefix>-*' \
  --arm-regex '-(?P<arm>stock|tuned|gepa)(-r\d+)?__' \
  --mask-text-file <arm-specific prompt text, e.g. the GEPA addendum> \
  --out <review-dir>
```
- It fails if any arm token, model-id suffix, mask text or trial name survives in a pack. It also fails if a task's first prompts still differ across arms after masking hostname ids; add a mask for the span it prints.
- Do not open `SEALED_arm_map.json`. Do not look at per-arm results until step 4.

## 2. Label with blind raters
- Spawn `trace-rater` subagents in one `task` batch from `<review-dir>/rater_batches.json`. Use two independent raters (A and B) per pack and about 6 packs per agent.
- Give each rater the guide `research/explorations/trace-lab/review/RATER_GUIDE.md`, the pack format note `<review-dir>/PACK_FORMAT.md`, the harness facts (limits, loop-break settings), and whether `sft_cut` applies (null for eval runs).
- Write each returned label to `<review-dir>/labels/rater_{a,b}/<id>.json` verbatim, after checking the required fields.
  - If a rater returns an incomplete report, ask **that** rater to resend its own judgements.
  - Never fill in or fix a label yourself.
- Tool predictions are optional and blind: Eval Lab and Scout rules keyed by pack id, written to `<review-dir>/predictions/<tool>.jsonl`. A model reader such as Docent is allowed only within its quota and spend rules (QUIRKS Q7, Q8).

## 3. Freeze
```bash
uv run evallab review freeze <review-dir> --labels <review-dir>/labels
```
- Commit the frozen labels, `MANIFEST.sha256`, `FROZEN_AT`, `metrics_blind.jsonl` and the predictions to `research/explorations/trace-lab/<experiment>/` **in their own commit before the join**. The commit order is the proof that nothing was tuned after the arms were known.

## 4. Join and report
```bash
uv run evallab review join <review-dir> --predictions <review-dir>/predictions
```
Copy the sealed map in as `arm_map.json` and commit it with the outputs: `TABLES.md`, `scores.md` and `REPORT.md`. Then write `RESULTS.md` by hand from REPORT.md:
- **Passes:** give the counts verdict and the raters' genuine-pass judgement side by side, each named as what it is.
- **Every task where pass/fail differs between arms:** the decisive steps from each arm, quoted.
- **Behaviour:** descriptive only, with n and the number of attempts stated. Nothing is "significant" unless the experiment's pre-registration says so.
- **Metrics:** drop or mark any metric whose detector QUIRKS lists as broken. Don't invent a replacement metric after the freeze.
- **Tool scores** against the agreed labels, with Wilson intervals.

## 5. Deliver
- Open a PR with auto-merge. Post the result path and manifest sha on the experiment's Linear card.
- Add any new quirk you found to QUIRKS.md in the same PR.
- A corrected claim stays visible as a correction; never silently rewrite it.

## Spend and authority
Rater subagents use the configured route and have no API spend line. Paid model readers, new provider keys and Docent BYOK are Peter's decision.
