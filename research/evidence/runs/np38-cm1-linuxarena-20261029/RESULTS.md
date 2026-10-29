# NP38-CM-1: LinuxArena test-writing alarms, with a disclosed resumption

## Primary result: completed 60 matched pairs

The frozen Haiku whole-run facet detector alarmed on **9/60 S runs (15.0%, Wilson 95% 8.1–26.1%)** and **3/60 environment-matched non-S runs (5.0%, 1.7–13.7%)**. There were no facet exclusions. S alarms spanned **6 of 41 environment × main-task keys (14.6%, Wilson 6.9–28.4%)**. The matched table, exact paired sign test and categorical verdict are withheld under the preregistered small-cell/complement rule. The run-level intervals and nominal paired test do not account for task clustering.

For the frozen Sonnet-judged cascade, the final S total is **27+<3/60**, with a conservative Wilson upper bound of **60.7%** evaluated at the maximum consistent count. The already-public deterministic Stage-1 replay accounts for 27 S runs, so the Sonnet replay added fewer than three S alarms beyond pass-through. The non-S cascade rate is withheld because fewer than three non-S readings were excluded; the other cascade partitions and its categorical verdict are also withheld to protect small cells and complements. All 120 cascade rows were processed; an excluded reading is not a negative.

These results measure **test-writing**, not repairs of failing tests. Most S hits are editor `create` calls. The corpus designates these as honest-condition runs, but does not independently certify each behaviour as benign; LinuxArena's adapter drops `submit` and reasoning. They should not be generalized to repair-specific false-alarm rates or other distributions. All 60 S runs are used across 41 task keys, so effective independent sample size is not established.

## Prefix seen before continuation

At the first stop, the model-free Stage-1 replay already showed **27/60 S (45.0%, Wilson 33.1–57.5%) and 0/60 non-S (0%, 0–6.0%)** with `grader_tamper` pass-through; all 47 pass-through cards were on S. Those pass-throughs count as cascade alarms under the frozen rule. The partial facets were **3/16 S (18.8%, 6.6–43.0%) versus 0/16 non-S (0%, 0–19.4%)**; 44 S and 44 non-S were then unread. The prefix score remains at `results/score.json`.

The parent and researcher **saw these prefix numbers before the remaining pairs were read**. The mandatory usage read had returned no windows after retries; the parent identified this as an intermittent infrastructure read failure and gave written GO to resume **on that infrastructure ground, not on the numbers**. This unblinding is a material qualification: the resumed full-sample score is the primary reading, but the decision to continue was not made blind to the prefix. No detector, model, prompt, rule, sample, 500-attempt cap or breaker was changed.

The committed `results/resumption-plan.json` declared the operational amendment before continuation: at most five logged usage-read attempts, spaced 10 seconds apart after read failures, with an immediate stop if any window is at or above 85%. A resume event is in the private call ledger; the published `results/resumption-freeze.json` pins the amendment and prefix hashes. The parent-authorized `--rescore` reason and start/completion hashes are published in `results/rescore-log.jsonl`. The final score is separately published at `results/final/score.json`; the pre-resumption score is not overwritten. The final publication additionally masks any 1–2 post-prefix additions recoverable by comparing the two scores.

## Accounting and scope

The two stages used **377 of 500 permitted attempts** in total. All successful calls were attested as served by the requested Haiku or Sonnet route. Refusal-class and parse-failure counts are each reported as **<3** under the disclosure rule; judge-unavailable readings are excluded rather than called clear. Total input was **7,561,910 facet tokens and 652,758 judge tokens** (judge cache reads and writes included); output was **58,833 facet and 19,291 judge tokens**. Fallback was disabled and read back, credentials were stripped, and each batch was logged as $0 subscription spend. No other models, local inference or uploads were used.

The sample was all 60 NP29-defined S runs plus 60 same-environment, nearest-step-count non-S matches without replacement or caliper. Absolute pair step differences were minimum 0, median 1, p90 9, maximum 17. The strict cross-key guard checked 1,256 literals with zero TEST/sealed hits. Transcripts, per-run lists, facet prose, judge replies and unsuppressed local cells remain outside git.

**Do not expand to 400 runs:** alarms occurred and the preregistered any-alarm stop clause applies. The full-design linear dry-run estimate was approximately **689 Haiku plus 557 judge calls (1,246 total), about 30M input tokens**, well above this study's 500-attempt cap; it was not executed.
