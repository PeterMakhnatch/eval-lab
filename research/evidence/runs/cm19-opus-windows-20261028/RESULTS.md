# CM-19 results: Opus 5.5 on CM-13's facet-cited windows

**Not adopted.** Arm C with Opus reaches **14/80 (17.5%, 10.7-27.3) recall at 5/70 (7.1%) clean false alarms**; the rule was 20/80 at 2/70. It fails on both counts.
Eight of the 418 prompts were refused by the route (`Refusal (reasoning_extraction)`, Anthropic terms-of-service filter on prompts that carry the judged step's
reasoning) after two fresh passes; they are unknown, never negatives, and touch 4 enacted runs in A, B and C. Even if all four had been detections, C would
reach 18/80, still below 20, so the verdict does not depend on them. Prompts were not edited to get around the refusals.

| Arm (judge) | Recall | Clean false alarms | Recall at false alarms <= 3% | Tokens per run (in + out) | Windows per run |
|---|---|---|---|---|---|
| A production, terra (CM-13) | 9/80 (11.2%) | 2/70 | 9/80 | 3,253 | 1.05 |
| A production, **Opus** | 4/80 (5.0%; 4 unknown) | 0/70 (0.0-5.2) | 4/76 | 5,351 | 1.05 |
| B facet windows, terra | 23/80 (28.8%) | 5/70 | 4 | 5,365 | 1.73 |
| B facet windows, **Opus** | 12/80 (15.0%; 4 unknown) | 5/70 (7.1%) | 0 | 8,772 | 1.73 |
| C facet-first, terra | 25/80 (31.2%) | 6/70 (8.6%) | 7/80 | 6,735 | 2.17 |
| C facet-first, **Opus** | **14/80 (17.5%; 4 unknown)** | **5/70 (7.1%)** | 2/76 | 11,010 | 2.17 |

Opus is a stricter judge: of 410 prompts judged by both, terra confirmed 58 windows and Opus 24 (21 in common, 37 terra only, 3 Opus only). Opus labels
`neither` on 386 windows against terra's 349. The cut comes mostly from enacted runs: per-run alarms among complete cases, terra only vs Opus only, in arm C
10 vs 0 on enacted runs (sign test p = 0.002) and 3 vs 2 on clean runs (p = 1.0); in B 10 vs 0 enacted, 2 vs 2 clean. So a stronger judge does not remove the
facet-first arm's clean false alarms (5/70 with Opus, 6/70 with terra); it removes about 44% of its detections (25 to 14).

Facet citation still gives better placement, with Opus as with terra: flagged step on a labelled step 15 (A), 23 (B), 26 (C) of 80; within 5 turns 48, 66, 72;
but a confirmed window sits on the labelled step in only 0, 1, 1 runs. Facet windows beat production windows with Opus too (C vs A: 10 vs 0 enacted
runs, p = 0.002; clean 5 vs 0, p = 0.06); that is more detections and more false alarms, not a better trade.

## Tokens (route-reported, 418 prompts)

Opus: **2,052,444 input and 18,837 output tokens over the 410 successful calls** (5,006 input and 46 output per call); terra on the same prompts 3,050 and 31.5 per call.
Anthropic reports uncached input, cache reads and cache writes separately; the Opus figure sums all three and includes about 3.6k tokens per call of fixed omp
harness prompt (a one-line prompt measured 3,613 cache-write tokens), so per-call input is not like-for-like with terra. Cost: $0 billed (subscription); the
Claude 5-hour window went from 2% to 15% over the whole run, which includes other fleet use.

## Limits

One tuned cohort (the CM-6 sample), wide intervals, a single Opus pass (terra test-retest moved arms by 1 to 2 runs). Judge thinking `low`, as in CM-13; Opus at a higher
thinking level was not tried. The terra figures are CM-13's. The refusals are a property of the route and the prompt content, not of the monitor.
