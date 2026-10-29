# TQ-BLIND: does the benchmark-name leak explain TQ-UNVERIFIED's result?

All numbers below were measured by this item (2026-10-29). Machine-readable: `results.json`. Preregistration
(committed before any model call of this item): `PREREGISTRATION.md`. Driver: `research/experiments/tq-blind/`.

## Question

The independent audit of TQ-UNVERIFIED (PR #1625) found that 42 of its 89 adjudicator prompts contained the benchmark
identity (`AutomationBench`, its source URL, `tests/original-grader/automationbench/` paths), although the
preregistration and README said no benchmark name was shown. If the identity is removed, do the adjudicators'
verdicts change, and does the finding "most Task QA gate alarms on unchanged AutomationBench tasks are real defects"
survive?

## What ran

- **Units:** the 42 of 89 main units whose rendered prompt contains an identity token: 26 alarm units on unchanged
  AutomationBench tasks, 10 calibration units with matched findings, 6 calibration units with none (188 findings).
  The other 47 units (PAIChecker) contain no token and were not re-run. The 42 prompts were re-rendered from the
  frozen units and compared byte for byte with the stored prompts first.
- **Redaction** (rules in `PREREGISTRATION.md`, code `redact.py`): the benchmark name in any position, the source URL
  and the copyright holder line replaced by neutral placeholders (`taskpkg`/`TaskPkg`/`TASKPKG`,
  `https://example.invalid/org/TaskPkg`, `Example Org, Inc.`), in the prompt and in the displayed source used for the
  mechanical evidence check. Proof, per prompt: text outside the tokens identical to the original, placeholder count
  equal to token count, zero tokens left. **4,587 tokens replaced** (name 3,893, holder 652, URL 42) in 42/42
  prompts; every proof passed. Same system prompt, instructions, findings, opaque ids and order, routes and
  settings as TQ-UNVERIFIED.
- **Redacted arm** (primary): `openai-codex/gpt-6.1-sol` and `anthropic/claude-opus-5-5`, one call per unit.
  42 calls each, 0 retries, 0 refusals, 0 fallbacks, served identity equal to the requested route on every call.
- **Control arm** (preregistered as secondary, a noise baseline): the same adjudicators on the *unredacted* prompts
  once more, so a flip can be compared with ordinary re-run noise. Sol 42 calls; Opus 44 (2 retries after
  unparseable answers), 0 refusals, 0 fallbacks, correct served identity. Calls per adjudicator over both arms: 84
  (Sol) and 86 (Opus), under the cap of 100. Codex and Claude 7-day quota 81% before, 83% after. $0 paid.
- Reproduction check passed: this driver recomputes the original arm from the frozen verdicts as 20/22, 22/26 and
  12/17, the published TQ-UNVERIFIED numbers.

## Result

| Both adjudicators agree real, alarms on unchanged AutomationBench tasks | original | redacted | unredacted re-run (control) |
|---|---|---|---|
| Sonnet gate (22 alarms) | 20 = 91% (72-97%) | **20 = 91% (72-97%)** | 21 = 95% |
| Terra gate (26 alarms) | 22 = 85% (66-94%) | **23 = 88% (71-96%)** | 22 = 85% |
| Not a defect (both), either gate | 0 | 0 | 0 |
| Known-real calibration recall, 17 findings (gate: 70%) | 12 = 71% | **12 = 71%** | 12 = 71% |

- **Decision rule (preregistered): the conclusion stands.** Sonnet 20/22 = 0.909 is at or above the original lower
  bound 0.722; Terra 23/26 = 0.885 is at or above 0.665. Both are far above the 0.60 "mostly real" threshold, and the
  validity gate is passed with the same 12/17 as before (still passed by a single finding).
- **Per-finding flips (188 findings), original -> redacted, vs original -> re-run with the unredacted prompt:**

  | adjudicator | redacted flips | unredacted re-run flips | Fisher p |
  |---|---|---|---|
  | Sol | 12 (6.4%) | 4 (2.1%) | 0.07 |
  | Opus | 14 (7.4%) | 14 (7.4%) | 1.00 |
  | both-agree final label | 12 (6.4%) | 11 (5.9%) | 1.00 |

  The final-label verdict totals are unchanged: real 123 / 123 (original / redacted; 120 on the re-run). Movements
  into and out of `real` are balanced (6 gained, 6 lost) and the redacted flip rate of the final label equals the
  re-run noise.
- **Direction by adjudicator.** Sol marks fewer findings real once the name is gone (170 -> 160 of 188; uncertain
  16 -> 25), and Opus marks more (133 -> 141; not a defect 49 -> 42). This is the only difference of any size, it is
  not significant for Sol (p = 0.07, 12 vs 4 flips, at the margin of 188 findings) and it points in opposite
  directions for the two adjudicators, so it is not a consistent "named benchmark gets more charity" effect. The
  headline needs both to agree, and its alarm counts did not move.
- Agreement and the other calibration reads: kappa between the adjudicators on these 188 findings 0.13 (original),
  0.12 (redacted), 0.04 (re-run), i.e. low throughout. Off-target calibration findings labelled real (final):
  34/56 original, 31/56 redacted, 34/56 re-run.
- Single-adjudicator alarm counts (real, Sonnet / Terra gate): Sol 22/22, 26/26 original; 21/22, 25/26 redacted.
  Opus 20/22, 22/26 original; 21/22, 24/26 redacted.

## Meaning

Plain answer: **the benchmark-name leak does not explain TQ-UNVERIFIED's result.** With the benchmark name, source
URL and copyright holder removed from the prompts, the AutomationBench gate alarms are still real by both
adjudicators at 91% (Sonnet gate) and 88% (Terra gate), the final labels are statistically the same as an unredacted
re-run, and the calibration recall is unchanged. The reading from #1625 (the unchanged AutomationBench tasks hold the
kind of defect the gate flags, so the "chance" balanced accuracy comes from label noise, not a gate at chance) is
supported unchanged. The audit's separate corrections still apply (see the correction section in the TQ-UNVERIFIED
README): the "corrected balanced accuracy" figures are conditional scenarios, not bounds, and "confirmed" means two
LLMs agree after reading source.

## Limits

- The redaction removes explicit names, not recognizability. The task content (integration names, the world-schema and
  rubric structure, the `zapier_tools` key, which appears once in each prompt, and the `original-grader` path
  component) remains; a model that knows the benchmark can still identify it. This tests the leak the audit found, not
  a fully anonymized corpus.
- The `Zapier, Inc.` copyright holder was redacted as an identity token though the brief named only the benchmark
  name, the URL and the paths; the primary run cannot separate the effect of the three rules.
- One call per unit per arm and a single re-run as noise reference: 188 findings on 42 tasks, domain clustering
  ignored. The Sol shift (p = 0.07) would need more repeats to settle; it does not affect the alarm decision.
- Only the 42 prompts with tokens were re-run. PAIChecker prompts have none of these tokens (the audit's finding was
  AutomationBench-only); other cues in them (for example repository URLs) were not examined or changed, and the
  post-hoc swap control was not re-run.
- The calibration validity gate is knife-edge (12/17 against a 70% bar): the same 12/17 holds in all three arms,
  which is stability, not extra power. Adjudicators are still two LLMs reading source, not an executed grader
  differential.

## Next decision

None needed for the leak. The next step stays the one from TQ-UNVERIFIED: certify the unchanged AutomationBench
tasks by execution before any false-positive rate is quoted from them. Raw prompts, responses and sessions:
`~/.local/state/evallab-fleet/2026-10-29/TQ-BLIND/`.
