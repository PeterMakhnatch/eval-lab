# CM-16: how early do whole-run facets see a reward hack?

DEV only. A fresh disjoint sample of 25 enacted and 25 clean Goodfire runs (outside the CM-6 cohort and every RA-6 sample;
strict `assignments.csv` cross-key check on every identity), each read by one reader on the blinded prefix at 25%, 50% and 100% of
its assistant steps. The design, measures, gate and budget were written into `causal-prefix-monitor.md` before the sample was drawn
(research-context commit `7a9d607`).

- Reader: `anthropic/claude-haiku-5-5` for every read (the CM-17 override of the original "terra if RA-10 has not reported"; RA-10
  was superseded). Served model attested per call; the extractor fails a read whose served model differs.
- Prefix: steps up to and including the ceil(fraction x N)-th assistant step, blinded exactly as the production extractor
  (`blind_atif`, widened redaction), the same prompt, chunking and validation. Prefix facets never enter `facets-v1.jsonl`.
- Sample rule: order by sha256(`20261028:` + trace id), one run per task, at least 12 assistant steps, blinded rendering at most
  150,000 characters (bounds the token cost and favours shorter runs), enacted runs must have a labelled passage.
- Labels are read by `sample` (to stratify) and `score` only; nothing label-derived reaches a prompt.

`tools/prefix_facets.py` has `sample`, `read` and `score`. The private store (prefix facet records with their prose, call receipts) stays
in `~/.local/state/evallab-prod-live/run-atlas/comparisons/cm16/`; this directory holds the frozen sample summary and the aggregates.
