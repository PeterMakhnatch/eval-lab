# D4 preregistration: label noise in TraceMonitor-Bench DEV v1

Written 2026-11-02 about 01:35 ET, before any case was read by any reader (no call to any reader has been made).

## Question
How often does an independent blind re-judgement disagree with the frozen benchmark label, per label-provenance class
(`publisher`, `publisher_cot_judged`, `container_verified`)? This estimates label noise so later numbers can state a ceiling.
It never changes a benchmark label.

## Cohort (fixed now)
60 cases = 5 benchmark positives + 5 benchmark negatives from each of the six families (cheatbench, terminal_wrench,
goodfire, deep_swe, c1c5, sol_astra), drawn from the 776 scored cases of the manifest with
`random.Random(f"{20261102}|{family}|{label}").sample(sorted(case_ids), 5)` (script `label_noise.py sample`). Provenance
classes: `publisher` 20 (cheatbench 10, terminal_wrench 10), `publisher_cot_judged` 20 (goodfire 10, deep_swe 10),
`container_verified` 20 (c1c5 10, sol_astra 10): at least 8 per class and per family, every class and family with at
least 10. The labels are used only to stratify the draw. The case ids are in `sample-case-ids.txt` (sha256[:12]
`b571b81e3d5b`).

Known limit: all 5 `sol_astra` positives are scripted one-step subversions and 5 of the 10 c1c5 cases are
container-verified positives from a small family; class estimates are n = 20 each and the intervals will be wide.

## Readers and protocol
* Reader A `anthropic/claude-sonnet-5-5`, Reader B `google-antigravity/gemini-3.8-flash`; headless `omp -p --no-tools
  --no-skills --thinking low --mode text --config worker-{A,B}.yml` (`modelFallback: false`), at most 4 concurrent,
  foreground. Every call is attested from its session log (served model equals requested, 0 fallbacks); an unattested
  or unparseable call is "missing", is retried once, and is never scored.
* Each reader sees, per case, the label-free `evallab trace anatomy` card from the benchmark plus a blinded step
  transcript from the registry loader (`blind_run`, `render_step`; reasoning and messages kept where the trace has them),
  capped at 90,000 characters (first 25% and last 75% of the budget kept, omission marked). Never the benchmark label,
  split, family, source id, any monitor output or any other reader's answer.
* Each reader reads each case twice with two prompt wordings (`w1` audit framing, `w2` accept/reject framing; same
  definitions and answer format; template sha256[:12] `9b541882e428`, `10bc70e36679`). Output: `verdict` in
  {`cheated`, `honest`, `cannot_tell`}, a `reason_code` from a fixed list, and a one-line reason.
* 60 cases x 2 readers x 2 wordings = 240 calls.

## Metrics (per provenance class and over all 60)
* Reader final verdict = the verdict if both wordings agree, else `unstable`. Only `cheated`/`honest` finals are
  compared with the label; `cannot_tell`, `unstable`, `missing` are counted and excluded.
* **Disagreement with the benchmark label** per reader: wrong / answered, 95% Wilson; separately the share of benchmark
  positives called `honest` and of benchmark negatives called `cheated`.
* **Consensus** = both readers' finals agree on `cheated` or `honest`. Consensus disagreement with the label: wrong /
  consensus cases (Wilson), with consensus coverage. Consensus disagreement is the headline noise estimate; single-reader
  disagreement is shown beside it. Reader error is not subtracted, so these are noise-plus-reader-error and read as an
  upper estimate of label noise.
* **Self-consistency** per reader: share of cases where the two wordings give the same verdict (Wilson).
* **A versus B**: share of identical verdicts on matched wordings (120 pairs), Wilson, and Cohen's kappa.
* Disagreement lists give case ids and reason codes only (no text, no direction).

## Sensitivity (POST-HOC, never the headline)
For a monitor perfect on the true label, recall at a 1% false-alarm ceiling under the estimated noise: with `p` the
consensus share of benchmark positives called honest and `q` the consensus share of benchmark negatives called cheated,
recall = `(1 - p) * min(1, 0.01 / q)`, at the point estimates and at the Wilson bounds. No benchmark label is modified
and no case is relabelled.

## Stats and receipts
`stats.py` helpers only (Wilson). Scripts: `research/experiments/on-label-noise-20261102/label_noise.py` (sha256[:12]
`1637ff774da8` at this time). Per-case readings, prompts and sessions stay under
`~/Developer/eval-lab-results/2026-11-02/D4/`; aggregates only in git.
