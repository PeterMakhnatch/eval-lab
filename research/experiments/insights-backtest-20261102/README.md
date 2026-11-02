# Insight engine backtest: the 2026-11-02 overnight run

Question: if the insight engine had been running at 03:25 ET on 2 November,
how many of the corrections and follow-ups that verifiers and the coordinator
found later that night would it have raised, how early, and how does that
compare with one strong agent asked to review the same material?

## Design

- **Cutoff.** 03:25 ET, the end of the coordinator log's "Update 03:25 ET"
  section (line 62 of the overnight `FINDINGS.md`).
- **What the systems see.** A frozen snapshot of everything written before the
  cutoff: the 62-line log prefix, 42 author reports and the 1,621 JSON receipts
  next to them. Verifier and review artifacts (`VF-*`, `REVIEW-*`), files
  modified after the cutoff and scratch fixtures are excluded. The snapshot is
  copied, so the original folders (which kept changing) are never read.
- **Ground truth.** 35 items first written after the cutoff: 26 corrections of
  pre-cutoff claims and 9 follow-up experiments, each tied to the pre-cutoff
  claim it concerns and graded for whether it was anticipatable from the
  snapshot (25 "yes", 10 "partial"; partial items name what was already known,
  so repeating a known caveat earns nothing). Corrections already visible in
  the log prefix are not scored.
- **Engine arm.** `evallab insights watch` on the snapshot with shortened
  cooldowns (`config.yaml` in the state dir), the default routes (extractor on
  Sonnet 5.5, analysts and synthesizer on Opus 5.5, referee on Gemini 3.8 Flash),
  at most five passes at a time.
- **Baseline arm.** One Opus 5.5 session (thinking high, 45-minute limit) with
  the same snapshot and a plain brief: write the findings a research lead
  would want before the next decision, each with the cheapest check.
- **Judge.** Gemini 3.8 Flash, a different family from both arms, sees the
  ground-truth items and one arm's cards under shuffled aliases (it is never
  told which system wrote them) and marks each item full, partial or none.
  A second judge call rates every card specific or generic.
- **Leakage audit.** Every worker tool call in both arms is scanned for reads
  of the original folders or of verifier output outside the snapshot.

`backtest.py` runs the baseline, the judge, the audit and the metrics. The
snapshot, ledger, ground truth and judge outputs stay in local state
(`~/.local/state/evallab-insights/backtests/2026-11-02-overnight/`): they quote
research notes and agent transcripts, so they are not committed.

```bash
uv run python research/experiments/insights-backtest-20261102/backtest.py baseline
uv run python research/experiments/insights-backtest-20261102/backtest.py judge
uv run python research/experiments/insights-backtest-20261102/backtest.py report
```

`acceptance/` holds operator inputs for the single-experiment acceptance runs
(`evallab insights experiment --attribution ...`), described in
[docs/insights.md](../../../docs/insights.md).
