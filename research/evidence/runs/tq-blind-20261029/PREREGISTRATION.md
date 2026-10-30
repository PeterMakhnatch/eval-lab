# TQ-BLIND preregistration (written before any model call of this item)

Written 2026-10-29 by the TQ-BLIND worker (timestamp: the commit time in `git log`), committed before the first adjudicator call.

## What I had seen before writing this

- The TQ-UNVERIFIED receipt, README, preregistration, results and the VERIFY audit (published aggregates).
- The stored TQ-UNVERIFIED prompts, only to enumerate which identity strings occur (token counts and string forms;
  no verdict was looked at per finding for this item). I ran the free, model-free `prepare` stage of the driver once
  (redaction and its proof, below) to confirm the unit count. No TQ-BLIND model call has been made and no
  TQ-BLIND verdict exists.
- The stored original verdicts are aggregates already published in PR #1625; the comparison reads them from the
  frozen `adjudication-{sol,opus}.json` of that item.

## Question

Does removing the benchmark identity from the adjudicator prompts change the TQ-UNVERIFIED verdicts, in particular
the AutomationBench result "most Task QA gate alarms on unchanged tasks are real defects" (both adjudicators agree:
Sonnet gate 20/22, Terra gate 22/26)?

## Units (fixed by rule)

Every one of the 89 main units of TQ-UNVERIFIED (73 alarm units, 10 calibration units with matched findings, 6
calibration units with none; the 20 post-hoc swap-control units are not re-run) whose rendered adjudicator prompt
contains at least one identity token. The stored prompt must be reproduced byte-for-byte by re-rendering
(`prompts.render_adjudicator_prompt(source, candidates)` from frozen `units.json` and the staged `source.txt`);
otherwise the driver stops.

Identity token rules (case-insensitive match, one left-to-right pass, first alternative wins; code and tests in
`research/experiments/tq-blind/redact.py`):

| rule | matches | replaced by |
|---|---|---|
| url | `https://github.com/zapier/AutomationBench` | `https://example.invalid/org/TaskPkg` |
| holder | `Zapier, Inc.` (the copyright line of every source file) | `Example Org, Inc.` |
| name | `automationbench` anywhere (import path, directory component of `tests/original-grader/automationbench/...`, env-var prefix `AUTOMATIONBENCH_`, prose) | `taskpkg` / `TaskPkg` / `TASKPKG` by the matched casing (any other casing aborts) |

Not redacted, by design: `zapier_tools` and other task-domain vocabulary (integration names, rubric and world-schema
structure), and the directory component `original-grader`. These are task content; the redaction removes explicit
names, not structural recognizability (a limit, stated in the README).

Observed at preparation, before any call: **42 of 89 units** contain tokens (26 alarm units, 10 `cal_known`, 6
`cal_off_only`), all AutomationBench; 47 contain none (all PAIChecker). Total 4,587 tokens in the 42 prompts (url 42,
holder 652, name 3,893). Those 47 prompts are not re-run and are not touched.

## Redaction (proof required before the call is accepted)

The redaction is applied to the whole rendered prompt and to the displayed source used for the mechanical evidence
check, and nothing else changes: same system prompt, same instructions, same findings with the same opaque ids and
order, same routes, settings and no-tool protocol. For each of the 42 prompts the driver must show (1) the text
outside the matched tokens is identical to the original (segment-by-segment), (2) the number of placeholder strings
equals the number of replaced tokens, (3) zero identity tokens remain. Any failure stops the run. Result of the
preparation run: 42/42 pass, residual tokens 0, residual `zapier` words 42 (exactly one `zapier_tools` key per unit).

## Arms and calls

- **redacted** (primary): both `openai-codex/gpt-6.1-sol` and `anthropic/claude-opus-5-5`, one call per unit on the
  redacted prompt, `omp -p --thinking high --no-tools` with the controlled no-retry/no-fallback config of
  TQ-UNVERIFIED (sha256 `0df78b7a...b7fb9`), 42 calls each plus at most one retry per unparseable answer.
- **control** (secondary, descriptive; added to give the flips a noise baseline): the same two adjudicators on the
  *unredacted* prompts again, one call per unit. The same model on the same prompt does not reproduce its verdicts
  exactly; without this arm a flip count cannot be told apart from sampling noise. It is run only after the redacted
  arm is complete and only if quota allows, and it enters no decision rule.
- Call cap 100 per adjudicator over both arms (retries included). Quota gate: read `omp usage --json` before every
  call, stop if that provider's 7-day window is at or above 85% (Codex and Claude both 81% at preparation). Served
  `provider/model` and fallback flag are recorded per call from the session log; a call served by another identity,
  or that used a tool, is invalid (no verdict). Refusals are counted per call; the batch stops after 3
  consecutive failures to answer. A stop is reported as partial coverage, never filled. Processing order is the
  frozen unit order of `blind-units.json`.

## Comparison (all computed from stored verdicts; original arm = frozen TQ-UNVERIFIED verdicts)

1. Per-finding verdict flips, original -> redacted, for Sol, for Opus and for the final label (both agree, else
   uncertain): full 3x3(+missing) transition counts, flip rate, and counts of final-label movements into and out of
   `real_defect`. Same for original -> control and redacted vs control (noise reference).
2. Per-gate both-real counts on AutomationBench unchanged alarms (Sonnet gate: 22 alarms; Terra gate: 26), with the
   original headline alarm label rule (real if any of the gate's findings is final `real_defect`; not a defect if
   all are; else uncertain), Wilson 95%, and the same table for Sol alone and Opus alone.
3. Calibration recall on the 17 known-real findings (final, Sol, Opus), the validity gate (>= 0.70), and the share of
   off-target calibration findings labelled real.
4. Cohen's kappa between the adjudicators and finding-verdict distributions per arm.
5. A reproduction check: the original arm recomputed by this driver from the frozen verdicts must give 20/22,
   22/26 and 12/17, the published numbers.

## Decision rule (fixed now)

Let `lo_g` be the lower bound of the original Wilson 95% interval of the both-real share for gate `g` (Sonnet 0.7219,
Terra 0.6647) and `p_g` the redacted both-real share `k/n` over the same alarms.

- **The TQ-UNVERIFIED AutomationBench conclusion stands** if `p_g >= lo_g` for both gates (the redacted share is
  inside or above the original interval).
- **Weakened** if either gate has `p_g < lo_g` but both are at least 0.60 (the original "mostly real" threshold).
- **Changed** (the leak was doing the work) if either gate has `p_g < 0.60`.
- The calibration validity gate (recall >= 0.70) is reported separately. The original passed it by one finding
  (12/17 = 70.6%); a drop to 11/17 fails it and is reported as "adjudicators not validated under blinding", which
  is a statement about the instrument, not by itself a change of the alarm conclusion.
- The control arm is evaluated with the same rule as a noise reference only. If the control itself falls outside
  the interval, the rule's verdict for the redacted arm is reported as not separable from re-run noise.
- Not tested and not claimed: the PAIChecker results (their prompts contain no identity token), and any statement
  that the adjudicators' judgments are executed-defect truth.

## Pins

- `research/experiments/tq-blind/redact.py` sha256: f0ef3247ff6d704ebaea2835c6f2b450eba57eefe7315bbcae6d996f49e59e07
- `research/experiments/tq-blind/run.py` sha256: 2755e000fddb83ea1add4058cf65057d76af95e83a4b954720c305b0e71f71ed
- `research/experiments/tq-unverified-alarms/prompts.py` sha256: 62438dad08bc7aeb96b3b5c6ceae5e8f0bd942aa76ad1aee4c8bd637f4f0c693
  (the TQ-UNVERIFIED preregistration pinned b8b940b2...; the later file differs only by line wrapping of one
  expression in `parse_verdicts`; the rendered prompt text is unchanged, which `prepare` proves by byte-comparing
  against the 89 stored prompts)
