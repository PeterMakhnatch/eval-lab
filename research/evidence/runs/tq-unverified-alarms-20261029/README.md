# TQ-UNVERIFIED: are the Task QA gate's alarms on "unchanged" tasks real defects?

All numbers below were measured by this item (2026-10-29). Machine-readable: `results.json`. Preregistration
(committed before any model call or label join): `PREREGISTRATION.md`. Driver: `research/experiments/tq-unverified-alarms/`.

## Question

The OQ-10 (Sonnet 5.5) and R29-OQ-1 (Codex terra) cross-tests alarmed on as many tasks with no injected defect as on
tasks with one (AutomationBench Verified: 22/30 vs 22/30, balanced accuracy 0.50), and on 6/30 to 21/30 PAIChecker
human-aligned tasks. The lane called these "unverified alarms" because the originals might hide real defects. Are
they real?

## What ran

- Population, from the stored frozen gate cells only (no new gate call): every alarm of the production gate
  (`new_verified_0_5`) on a task with no injected defect. That is **111 gate alarms on 73 distinct tasks**:
  AutomationBench unchanged 22 (Sonnet) + 26 (Terra), PAIChecker Multilingual human-aligned 6 + 24, SWE-Gym
  human-aligned 12 + 21. Their 202 eligible findings (identical findings across gates merged, 3 merges) were
  adjudicated in one call per task, gate attribution hidden.
- Calibration mixed in blind: 10 AutomationBench tasks with a verified defect where a terra matcher said an alarm
  names the documented defect (17 known-real findings) plus their other findings (off-target, status unknown), and
  6 defective tasks with no matched alarm. 89 units in all (73 alarm, 10 calibration, 6 off-target only).
- Two independent adjudicators, one call per unit: `openai-codex/gpt-6.1-sol` and `anthropic/claude-opus-5-5`, each
  shown the task package as the gate saw it plus the findings (no gate, confidence, benchmark, split or label).
  109 calls each (89 + 20 control), **0 retries, 0 refusals, 0 invalid, 0 fallbacks**, served identity equal to the
  requested route on every call, Codex and Claude 7-day quota 78/79% before and 80/81% after. $0 paid.
- A `real_defect` verdict needs at least one mechanically verified quote (none was downgraded).

## Result

Headline rule: both adjudicators agree -> that verdict, otherwise `uncertain`. Alarm label per (task, gate): real if
any of that gate's findings is real; not a defect if all are; else uncertain. Wilson 95%, ignoring task and domain
clustering.

| Corpus, gate | alarms on unchanged tasks | real (both agree) | not a defect (both) | uncertain (disagree or either uncertain) | Sol real | Opus real |
|---|---:|---|---|---:|---:|---:|
| AutomationBench, Sonnet | 22 of 30 | **20 = 91% (72-97%)** | 0 (0-15%) | 2 | 22 | 20 |
| AutomationBench, Terra | 26 of 30 | **22 = 85% (66-94%)** | 0 (0-13%) | 4 | 26 | 22 |
| Multilingual human-aligned, Sonnet | 6 of 30 | 5 = 83% (44-97%) | 0 | 1 | 6 | 5 |
| Multilingual human-aligned, Terra | 24 of 28 | 9 = 38% (21-57%) | 0 (0-14%) | 15 | 22 | 9 |
| SWE-Gym human-aligned, Sonnet | 12 of 30 | 9 = 75% (47-91%) | 0 | 3 | 9 | 10 |
| SWE-Gym human-aligned, Terra | 21 of 30 | 10 = 48% (28-68%) | 1 (1-23%) | 10 | 20 | 10 |
| **All six** | 111 | **75 = 68% (58-76%)** | 1 | 35 | 105 = 95% | 76 = 68% |
| Sonnet, all | 40 | 34 = 85% (71-93%) | 0 | 6 | 37 | 35 |
| Terra, all | 71 | 41 = 58% (46-69%) | 1 | 29 | 68 | 41 |

- **Of 111 alarms on unchanged tasks, 75 are confirmed real defects by both adjudicators, 1 is confirmed not a
  defect, 35 are uncertain.** The 35 are almost all one split: Sol calls the alarm real, Opus calls it not a defect
  (Opus not-a-defect on alarms: 34 of 111, Sol: 2 of 111). Per adjudicator, real alarms are 105/111 (Sol) and
  76/111 (Opus).
- **AutomationBench, the case that drove the "chance" reading:** 42 of 48 gate alarms on unchanged tasks (Sonnet 20/22,
  Terra 22/26) are real by both adjudicators, and 0 of 48 are not a defect. The corrected alarm precision on this set
  is 85-91% (lower bound, uncertain counted as not real). The unchanged tasks are not clean negatives: 20 of the 30
  carry a defect the Sonnet gate named and both adjudicators confirmed (22 of 30 for Terra).
- **Corrected balanced accuracy (AutomationBench)**, relabelling the unchanged tasks with a confirmed-real alarm as
  defective and treating every other unchanged task as clean (unverified): Sonnet 0.82-0.92, Terra 0.69-0.95 (lower
  bound: uncertain alarms count as false, upper bound: as real) against the reported 0.50 and 0.47.
- **PAIChecker:** Sonnet's alarms on human-aligned tasks are mostly real too (Multilingual 5/6, SWE-Gym 9/12 by both
  adjudicators), so its reported false-positive rates (20%, 40%) are mostly not false positives. Terra's are disputed
  and cannot be resolved here: Sol calls 22/24 and 20/21 real, Opus calls 15/24 and 11/21 not a defect.
- **Adjudicator quality:** Cohen kappa between Sol and Opus over 275 findings is 0.12 (raw agreement 64%). Calibration
  recall on the 17 known-real findings is 12/17 = 71% (47-87%) under the headline rule (Sol 15/17, Opus 13/17); the
  preregistered validity gate (at least 70%) passed by one finding. On the other findings of those defective tasks Sol
  labels 49/56 real and Opus 40/56. Sol is lenient; Opus is the stricter and also finds most AutomationBench alarms real.
- **Post-hoc negative control (not preregistered):** 47 findings that belong to a different task of the same corpus,
  shown against this task's source: both Sol and Opus labelled 47/47 `not_a_defect`. Neither adjudicator accepts
  findings indiscriminately; the control is easy and says nothing about hard negatives.
- The benchmark itself records no review note for the unchanged AutomationBench tasks (`tasks.csv`: all 30 are
  `unchanged`, with no finding or issue group), so there is no independent label to compare with.

## Meaning

The earlier "chance" reading is **wrong about the gate's findings and right about the labels**. On AutomationBench the
alarm rate on unchanged tasks equals the rate on verified-defective tasks because the unchanged tasks have the same
kind of defect: graders that never check a requirement the instruction states (a CC recipient, a QuickBooks update,
the amounts in a Slack post, a time range), or that require an action the context says is redundant. By the
preregistered rule (real share at least 0.60 of alarms, validity gate passed), the alarms on unchanged AutomationBench
tasks are mostly real findings, not false positives, and the 0.50 balanced accuracy measures label noise, not a gate
at chance. It does not show the gate separates good from defective tasks: no detection-quality claim follows about the
unflagged tasks or about how severe the confirmed defects are compared with the verified ones.

For PAIChecker, Sonnet's "false positives" are mostly real defects of the aligned tasks; Terra's flag-everything
pattern is not settled by two adjudicators that are split on 29 of its 71 alarms.

## Limits

- Adjudication is two LLMs reading the displayed source, not an executed grader differential; "real defect" includes
  coverage omissions of a stated requirement, which almost any grader has. Severity and executed impact are not
  measured. The verified AutomationBench defects were established by execution; these are not.
- Only 17 known-real calibration findings on 10 tasks (the matcher matched 10 of 24 flagged defective tasks; target
  was 20 tasks). Calibration recall 71% passed the 70% gate narrowly, and the interval is 47-87%. Calibration is
  AutomationBench only; no calibration exists for PAIChecker.
- No hard known-false items; the only negative control is the easy swap control, added after seeing the main verdict
  pattern.
- Sol and Opus disagree heavily (kappa 0.12); the headline treats disagreement as uncertain, which makes "real" a
  conservative count and "uncertain" a large bucket on Terra/PAIChecker.
- The gate saw a bounded source view; the adjudicators saw the same view. A defect outside the view is invisible to both.
- Intervals ignore domain/repository clustering and the overlap between gates (Sonnet's AutomationBench alarmed tasks
  are a subset of Terra's). Per-finding rows are in `results.json` keyed by opaque unit ids; class, gate and task
  identity stay in the private state directory.
- Deviations: see `results.json` `deviations` (calibration at the floor, post-hoc control, second score run, driver code
  committed after the preregistration with prompts pinned).

## Next decision

If the lane wants a gate that is scored on AutomationBench, the unchanged tasks need a certification pass (execute a
control against the confirmed alarms, or re-verify the 42 confirmed ones) before any false-positive rate is
quoted from them. Raw prompts, responses and sessions: `~/.local/state/evallab-fleet/2026-10-29/TQ-UNVERIFIED/`.

## Corrections after independent verification (added by TQ-BLIND, 2026-10-29; the numbers above are unchanged)

The independent audit (`VERIFY`, TQ section) reproduced every count in this README and made three corrections:

1. **Source blinding did not hold.** 42 of the 89 main prompts to each adjudicator (all AutomationBench: 26 alarm, 10
   calibration with matched findings, 6 calibration with none) contained the benchmark identity: `AutomationBench`
   in import paths, directories and env-var names, the source URL `https://github.com/zapier/AutomationBench`, the
   `tests/original-grader/automationbench/` paths and the `Zapier, Inc.` copyright line. The preregistration and the
   statement above that no benchmark name was shown are wrong for those 42 units. The 47 PAIChecker prompts contain none
   of these tokens. The result of re-running the 42 with the identity redacted is in
   `research/evidence/runs/tq-blind-20261029/`: the AutomationBench conclusion stands (Sonnet gate 20/22, Terra gate
   23/26 both-real; calibration recall 12/17; final-label flips 12/188 against 11/188 for an unredacted re-run).
2. **The "35 uncertain" breakdown.** The 35 uncertain alarms are not all Sol-real / Opus-not-a-defect: 30 are Sol real
   and Opus not a defect, 3 are Sol uncertain and Opus not a defect, 1 is uncertain for both, and 1 is Sol not a defect
   and Opus real. ("Almost all one split" in the text above holds; the receipt's "all" did not.)
3. **"Corrected balanced accuracy" is a conditional scenario, not a bound.** The 0.82-0.92 (Sonnet) and 0.69-0.95
   (Terra) figures relabel agreed-real alarms as positives, assume every unflagged unchanged task is clean, treat the
   known-defective labels as correct and toggle only the uncertain alarms. Unseen defects among unflagged tasks,
   adjudication errors and imperfect original positives can move the true value outside them. "Confirmed" in this
   README means two LLMs agree after reading source (kappa 0.12, 17 calibration findings, no executed differential).
