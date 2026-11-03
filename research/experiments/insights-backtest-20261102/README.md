# Insight engine backtest: two overnight runs

Question: if the insight engine had been running at the cutoff of an overnight
run, how many of the corrections and follow-ups that verifiers and the
coordinator found later that night would it have raised, how early, how many
of its cards are true, and how does that compare with one strong agent asked
to review the same material?

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
  of the original folders or of verifier output outside the snapshot. A pass
  with such a read is contaminated: its cards cannot score a match. Writing a
  card that quotes an original path is counted separately and is not a read.

### Second night: 2026-10-31

Same method on the 2026-10-31 overnight, to guard against tuning to one night.
The cutoff is 03:09 ET on 1 November, the launch of the two blind verifiers;
there is no time-stamped coordinator log. The snapshot holds 747 pre-cutoff
sources (37 author reports, 705 JSON/JSONL receipts, the mission brief and 4
status files). Ground truth is 31 later items from the verifiers, a lane audit
and two continuation reports, written 2 to 66 minutes after the cutoff: 20
"yes", 10 "partial", 1 "no" (not scored).

### Arms

| Arm | Code | Nights | What changed |
|---|---|---|---|
| engine v1 | `a92e5513f` | 11-02 | first version; skeptic ran 3 passes in total |
| engine v2 | `0d72116f7` | both | skeptic reviews every experiment (3 per pass, up to 3 passes at once); extractor writes receipt recipes; unit-aware checks; precision-first signals; sealed per-pass ledger snapshots |
| baseline | — | both | one Opus 5.5 session per night |

Both engine commits are earlier commits of PR #1826's branch that a later
rebase replaced, so `main` does not contain them. Each run used a copy of its
commit's tree, kept with the local backtest state under `runtime/<commit>`.

The v2 runs used at most 3 to 6 concurrent passes because the Mac was under
memory pressure, and every lens was disabled once the skeptic had covered every
experiment. The judge ran twice: the first run cut each card to 2,400
characters, which hid the proposed check of long engine cards, so the second
run (`--judge-dir judge2`) puts the check first and shows 4,000 characters.
Results below use the second run; the first gives the same picture.

`backtest.py` runs the baseline, the judge, the audit and the metrics. The
snapshot, ledger, ground truth and judge outputs stay in local state
(`~/.local/state/evallab-insights/backtests/2026-11-02-overnight/`): they quote
research notes and agent transcripts, so they are not committed.

```bash
uv run python research/experiments/insights-backtest-20261102/backtest.py baseline
uv run python research/experiments/insights-backtest-20261102/backtest.py judge
uv run python research/experiments/insights-backtest-20261102/backtest.py report
```

```bash
# second night, a named engine arm, and the second judge run
uv run python research/experiments/insights-backtest-20261102/backtest.py \
  --dir ~/.local/state/evallab-insights/backtests/2026-10-31-overnight \
  judge --judge-dir judge2 --arm engine-v2=state-v2
```

`acceptance/` holds operator inputs for the single-experiment acceptance runs
(`evallab insights experiment --attribution ...`), described in
[docs/insights.md](../../../docs/insights.md).

## Results

### Recall of what verifiers found later

Matches are full or partial, from the second judge run; intervals are Wilson 95%.

| Night | Arm | Cards | Full | Partial | Matched | Recall |
|---|---|---|---|---|---|---|
| 11-02 (35 items) | engine v1 | 21 | 6 | 2 | 8 | 23% (12–39%) |
| 11-02 | engine v2 | 58 | 10 | 8 | 18 | 51% (36–67%) |
| 11-02 | baseline | 31 | 13 | 5 | 18 | 51% (36–67%) |
| 10-31 (30 items) | engine v2 | 61 | 8 | 4 | 12 | 40% (25–58%) |
| 10-31 | baseline | 33 | 13 | 1 | 14 | 47% (30–64%) |

- **v2 against v1** (11-02, same items): v2 matched 11 items v1 missed, v1
  matched 1 that v2 missed; exact McNemar p = 0.006.
- **v2 against the baseline** (both nights, 65 items): 30 against 32 matched;
  9 items only the engine found, 11 only the baseline found; p = 0.82. No
  detectable difference in recall.
- **Together** they matched 41 of 65 items (63%, 51–74%): the two find
  different things.
- **Judge stability.** Between the two judge runs, the hit/no-hit verdict per
  item agreed on 32 to 34 of 35 items (11-02) and 26 of 30 (10-31), so a
  difference of one or two items between arms is within judge noise.
- **Leakage.** Six passes on 11-02 and four on 10-31 named an original folder
  outside the snapshot; two were real directory listings (`ls`), the rest were
  paths inside scripts or card text. Their cards were excluded from matching;
  no scored match depended on them in the second judge run.

### Are the cards true?

A blind audit (Claude sub-auditors, recomputing numbers from the snapshot
receipts, system labels hidden) rated every card. v1 and the 11-02 baseline
were audited in one batch; v2 and the 10-31 baseline in a second batch.

| Night | Arm | Cards | Correct | Partly right | Wrong | High value | False assertions |
|---|---|---|---|---|---|---|---|
| 11-02 | engine v1 | 20 | 20 | 0 | 0 | 8 | 0 of 74 |
| 11-02 | engine v2 | 57 | 55 | 2 | 0 | 23 | 0 of 188 |
| 11-02 | baseline | 31 | 21 | 10 | 0 | 14 | 3 of 113 |
| 10-31 | engine v2 | 61 | 48 | 13 | 0 | 22 | 4 of 220 |
| 10-31 | baseline | 33 | 21 | 12 | 0 | 8 | 3 of 131 |

Engine v2 cards were fully correct in 103 of 118 cases (87%, 80–92%) against
42 of 64 (66%, 53–76%) for the baseline. "Partly right" cards mostly state an
inferred cause or a magnitude more strongly than the receipts support. The
most valuable cards found report headlines contradicted by their own receipts
(a McNemar p of 0.031 that is 0.109 when recomputed; "L0 caught all" flips
when L0 fired on 3 of 18 cheats; a 9/10 headline whose own table gives 7/10).

### Timing and cost

- **Timing.** The baseline finishes in about 15 minutes. The engine starts
  cold at the cutoff: extraction takes about an hour and the skeptic covers
  every experiment after about two. On 10-31, where verifiers wrote within 2
  to 66 minutes of the cutoff, most engine matches came after the verifier; on
  11-02 about half came before. A live engine extracts reports as they land,
  so cold-start timing understates it [inference, not measured].
- **Cost per night.** Engine v2: 93 to 106 passes, about 2.1 M output tokens
  and 155 to 160 M cache-read tokens. Baseline: one session, 60 to 77 k output
  tokens and 9 to 13 M cache-read tokens, about 30 times less. All on
  subscription routes.

### Quality-control measurements

| Check | Result |
|---|---|
| Claim extraction (50 sampled claims, 11-02) | 44 of 50 fully faithful (88%, 76–94%); v1 43 of 50; quotes verbatim 50 of 50 |
| Receipt recompute (11-02, v2) | 605 claims carry a recipe (38%; v1 5%); 600 match a receipt field; with the stricter rule that every stated number must match, 416 reproduced and 184 partial |
| Recipes read the intended field | 20 of 20 sampled |
| Independent re-derivation | 54 of 60 (11-02) and 35 of 46 (10-31) agreed as recorded; all 17 disagreements compared a count with a rate that had equal values, a comparison bug fixed afterwards |
| Signals | 9 of 26 real (35%, 19–54%), v1 9 of 50; 11-02 volume 136 to 15 |
| Referee on planted flawed twins | 8 of 8 rejected, 8 of 8 originals accepted; easy twins, blinding imperfect |
| Referee on real cards (old brief) | all 6 engine cards it rejected or sent back were rated correct by the audit; it accepted 13 of the 15 partly-right cards (2 were never graded) |
| Referee on real cards (current brief, all 118 re-graded) | accepted 96 of 103 correct cards (93%, 87–97%) and all 15 partly-right ones (0 of 15 flagged, 0–20%); its 7 non-accepts were all on correct cards |

### What this says

1. Systematic per-experiment review was the main fix: recall doubled from v1
   to v2 on the same night.
2. The engine matches one strong reviewing session on recall and writes more
   cards that are more often fully correct, at about 30 times the tokens.
3. The two approaches are complementary (63% together against about 50% each).
4. The referee does not add measurable precision on real cards, old or new
   brief: it checks cards against the ledger claims they cite, while the
   audit's "partly" flaws only show when the numbers are recomputed from the
   receipts. The cheap checks (quotes, numbers, recipes) and human review
   carry the quality bar; the board is a ranked review queue, not a list of
   findings.

### Single-experiment reviews

`evallab insights experiment` was run on two committed experiments against
criteria set in advance by the research agent: the C1–C5 live rollouts (with
the Sol/Astra receipts it compares against and a layer-attribution spec) and
the C2 hack-drop dry run.

| Criterion | First version | After fixes |
|---|---|---|
| Headline numbers extracted (0/42, 6/20, C1 4/4, AutoReview 5/6) | met | met |
| Those numbers recomputed from receipts | 0 of 48 | 29 reproduced, 0 differ; 0/42 still has no recipe |
| Prevention kept apart from detection; AutoReview alone equals the union | met | met |
| Stage-4 paired test with 6 cheats and the sample size it would need | met | met |
| C2 per-corpus numbers recomputed | 0 of 58 | 127 reproduced; the 2 differences were rounding |
| Label provenance surfaced (390 labels outside the shared loader) | missed | found by a deterministic check |
| Independent re-derivation on a sample | empty | C2: 28 of 28 agree |
| Different cohorts not compared as one rate | partly | after a later fix, flags 0/42 = 0/24 + 0/18 and 0/42 against 6/20 |

Defects found in the second run (scripted-positive denominators, a referee pass
lost to a database lock, count-against-rate comparisons, rounding reported as a
difference, spliced report sentences) were fixed with regression tests; the
fixed version was replayed on the stored outputs, not rerun end to end.

### Limits

- Two nights, one ground-truth annotator per night, and ground truth limited
  to what the verifiers chose to check; a card can be right and still match
  nothing.
- The judge (Gemini 3.8 Flash) is the referee's model family; the auditors are
  the analysts' family (Claude).
- The snapshots lack some per-case files, so 21 audited assertions were
  unverifiable.
- v2 is not the merged code. Later fixes (honest `partial` recompute status,
  rederiver comparison, signal deduplication, launch stagger) were not
  backtested end to end; only the current referee brief was re-run on the
  backtest cards.
