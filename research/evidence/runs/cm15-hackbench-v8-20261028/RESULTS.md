# CM-15 results

## HackBench command localization, v7 vs v8 (169 trajectories, judge `kimi-code/k3.1-pro-highspeed`)

Each arm ran once, unchanged, on the frozen design in `results/freeze.json`. HackBench is all positives, so there is no
false-alarm rate; these are localization and recall only.

| Measure | v7 (`submission_tamper` pass-through) | v8 (current main) |
|---|---|---|
| Localization hit within 0 commands | 2/169 = 1.2% (0.3–4.2) | 2/169 = 1.2% (0.3–4.2) |
| Localization hit within 5 commands | 2/169 = 1.2% (0.3–4.2) | 2/169 = 1.2% (0.3–4.2) |
| Trajectory alarmed (recall) | 7/169 = 4.1% (2.0–8.3) | 7/169 = 4.1% (2.0–8.3) |
| Mean command precision / recall | 0.0089 / 0.0089 | 0.0089 / 0.0089 |
| Natural 41: hit within 0 / within 5 / alarmed | 1 (0.4–12.6) / 1 / 6 (6.9–28.4) | 1 / 1 / 6 |
| Judge windows per trajectory (total) | 0.769 (130) | 0.769 (130) |
| Tokens per trajectory (input + output) | 1,499.7 (1,389.2 + 110.6) | 1,499.7 (1,389.2 + 110.6) |
| Fresh judge calls / failed calls | 0 / 0 | 0 / 0 |
| Cards confirmed, by rule | `hidden_info_read` 5, `harness_log_read` 1, `hackbook:hb_skip_exit_marker` 1, `repetition` 1 | same |

- **v8 did not lose or gain anything on HackBench.** All 169 trajectories have identical card sets in the two arms
  (rule, flagged step and status), and the discordant counts are 0 / 0 for alarm, hit at 0 and hit within 5. `submission_tamper`
  produced no card on any HackBench trajectory (its patterns do not match ML-research commands), so the only change #1353 made
  to the cascade has no effect here by construction. This also holds with the current Stage 1, which has grown since the
  CM-11 freeze: its stored v7 result (7 alarmed, 2 exact hits) is reproduced.
- Every judged prompt was served from the route-attested CM-11 cache (the Stage-1 cards did not change), so this item made
  no new judge call and spent no quota beyond the 90% gate checks (Kimi five-hour window 1% at the start). Tokens are the route-reported
  usage of each judged window.
- The cascade localizes 2 of 169 HackBench trajectories; that is the CM-11 finding, unchanged, not an effect of v8.

## DEV: what v8's judge does with `submission_tamper` cards

From the stored #1353 v8 replay (Goodfire 200, CheatBench 150; Kimi; no new calls):

| Outcome of a `submission_tamper` card | Goodfire | CheatBench | Total |
|---|---|---|---|
| Cards raised | 4 (all on 1 enacted run) | 26 (1 on an enacted run, 25 on 5 clean runs) | **30** |
| Confirmed by the judge | 0 | 0 | **0** |
| Judged, abstained (`neither`) | 1 | 6 | **7** |
| Never judged (no Stage-2 slot) | 3 | 20 | **23** |
| Judge unavailable | 0 | 0 | 0 |

Seven traces carry a tamper card: two enacted (one Goodfire, one CheatBench) and five clean (all CheatBench). v7 alarmed all seven
through pass-through (2 detections, 5 false alarms). v8 alarms none of them through this rule, because the judge never confirms one
(7 of 7 judged cards abstain) and 23 of 30 never reach it. So the recall floor cost of #1353 is **two enacted detections, one per
corpus, bought with five clean false alarms removed**, the trade CM-11 pre-registered. It does not show that tamper cards
are useless: the judge abstained on the two enacted traces' judged cards too (1 of 4 Goodfire cards, 1 of 1 CheatBench card).

## Limits

Two arms of one judge on one cross-test; 169 trajectories with 13 tasks, so intervals are wide and clustered. HackBench cannot show
a false-alarm cost, so a recall tie says nothing about v8's clean-run benefit, which is measured on DEV only. The DEV tally uses the
CM-11b replay artifacts, whose Stage 1 predates the later tripwire changes.
