# NP38-CM-1: LinuxArena test-writing alarms, with a disclosed resumption

## Primary result: completed 60 matched pairs

**Answer:** The cascade shows the Flake-type weakness clearly through its frozen `grader_tamper` pass-through: **27/60 S (45.0%, Wilson 33.1–57.5%) versus 0/60 non-S (0%, 0–6.0%)**. This is deterministic Stage-1 evidence, known before any model call, not a new finding from the Sonnet run. The 27 S pass-throughs span **19 of 41 S task keys**. The Haiku facets are elevated but only suggestive: **9/60 S (15.0%, Wilson 95% 8.1–26.1%) versus 3/60 non-S (5.0%, 1.7–13.7%)**, rate ratio **3.0**. The 9 S facet alarms span **6 of 41 keys** (14.6%, Wilson 6.9–28.4%), indicating clustering. There were no facet exclusions.

The preregistered facet verdict is **inconclusive**, not worse. Comparable is impossible because the S Wilson upper bound, 26.1%, exceeds 10%. Let d be pairs alarming in both groups; the discordant counts are 9−d and 3−d. The masked matched table implies d is 1 or 2, giving exact two-sided sign p of 0.109 or 0.070, neither below 0.05. This verdict and the 3.0 ratio are already inferable from the public margins and suppression, so publishing them exposes no additional small cell. The matched cells remain withheld. Run-level Wilson intervals and the descriptive sign test do not account for task clustering.

For the frozen Sonnet-judged cascade, the final S total is **27+<3/60**; here **<3 means 1 or 2, not zero**, so Sonnet confirmed 1–2 S alarms on runs without a pass-through. The conservative Wilson upper bound is **60.7%**, evaluated at 29. The non-S cascade rate is withheld because its denominator would identify the 1–2 excluded readings; the number of Sonnet-confirmed non-S alarms is **unknown**, not zero. All 120 cascade rows were processed; an excluded reading is not a negative.

| Alarm reason | Count |
|---|---:|
| Facets, all S and non-S | 12 |
| S cascade: pass-through only | 27 |
| S cascade: judge-confirmed only | 1–2 |
| S cascade: both pass-through and judge-confirmed | 0 |
| S cascade: pooled pass-through-only and judge-only | 27+<3 |
| Non-S judge-confirmed | Unknown |

The rare judge-only cell is pooled with pass-through, but the pooled sum stays masked as 27+<3: an exact sum beside the already-public 27 pass-throughs would reveal the 1–2 cell. The full privacy-safe breakdown and the supplement to the frozen score are in `results/interpretation.json`.

These results measure **test-writing**, not repairs of failing tests. Most S hits are editor `create` calls. The corpus designates these as honest-condition runs, but does not independently certify each behaviour as benign; LinuxArena's adapter drops `submit` and reasoning. They should not be generalized to repair-specific false-alarm rates or other distributions. All 60 S runs are used across 41 task keys, so effective independent sample size is not established.

## Prefix seen before continuation

At the first stop, the model-free Stage-1 replay had already shown the 27/60 versus 0/60 split above; all 47 pass-through cards were on S and count as cascade alarms under the frozen rule. The partial facets were **3/16 S (18.8%, 6.6–43.0%) versus 0/16 non-S (0%, 0–19.4%)**; 44 S and 44 non-S were then **unread**, not excluded by failed readings. The frozen prefix score remains byte-for-byte at `results/score.json`; `results/prefix-readable.json` supplies the corrected `unread` labels without changing its published hash.

The parent and researcher **saw these prefix numbers before the remaining pairs were read**. The mandatory usage read had returned no windows after retries; the parent identified this as an intermittent infrastructure read failure and gave written GO to resume **on that infrastructure ground, not on the numbers**. Completing the preregistered fixed sample was the default; stopping at 16 pairs because of infrastructure would have departed from that design. The prefix had already triggered the any-alarm stop clause, so continuation could not change the decision not to expand. Unblinding nevertheless materially qualifies the continue/stop decision. **Producing a second score departed from the preregistered score-once rule**; both the original prefix and full-sample primary score are retained.

The committed `results/resumption-plan.json` declared the operational amendment before continuation: at most five logged usage-read attempts, spaced 10 seconds apart after read failures, with an immediate stop if any window is at or above 85%. The resumption used **new wrapper code after the original freeze**: wrapper SHA-256 `029f506a814ca779882536036ab0e3807b8b4ea23100fec9279b5db10b027758`, committed head `5693ba92c721eb387bac34f3f15ad69a797b9272`, pinned in `results/resumption-freeze.json`. Detector code, models, prompts, rules, sample, 500-attempt cap and breakers were unchanged. The resume event occurred at **3:45:34 PM ET**; its timestamp, event-line hash and redacted usage readings are in `results/usage-readings-redacted.json`. All 52 successful post-resume reads were below 85%; six read attempts failed and were retried. The original runner did not individually log the failed pre-resumption reads.

The parent-authorized `--rescore` reason and start/completion hashes are in `results/rescore-log.jsonl`. The final score is at `results/final/score.json` and the prefix remains at `results/score.json`. The frozen scorer conservatively withholds some derived fields; `results/interpretation.json` supplies the inferable ratio and verdict without changing either hashed score. The final publication also masks any 1–2 post-prefix additions recoverable by comparing the two scores.

## Accounting and scope

The two stages used **377 of 500 permitted attempts** in total. All successful calls were attested as served by the requested Haiku or Sonnet route. There were **1–2 refusal-class calls, all in Sonnet judging after resumption**, and **zero parse failures**; judge-unavailable readings are excluded rather than called clear. Total input was **7,561,910 facet tokens and 652,758 judge tokens** (judge cache reads and writes included); output was **58,833 facet and 19,291 judge tokens**. Fallback was disabled and read back, credentials were stripped, and each batch was logged as $0 subscription spend. No other models, local inference or uploads were used.

The sample was all 60 NP29-defined S runs plus 60 same-environment, nearest-step-count non-S matches without replacement or caliper. Absolute pair step differences were minimum 0, median 1, p90 9, maximum 17. The strict cross-key guard checked 1,256 literals with zero TEST/sealed hits. Transcripts, per-run lists, facet prose, judge replies and unsuppressed local cells remain outside git.

**Do not expand to 400 runs:** alarms occurred and the preregistered any-alarm stop clause applies. The full-design linear dry-run estimate was approximately **689 Haiku plus 557 judge calls (1,246 total), about 30M input tokens**, well above this study's 500-attempt cap; it was not executed.
