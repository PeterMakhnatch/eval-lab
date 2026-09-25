# Trial diagnosis recovers Reef 04 gate flags — 2026-09-25

> Agent-reviewed analysis over the ReefIntake exp04 corpus
> (`.../reef-shift-20260925/derived/reef-intake/`, runs `04` seed and
> `04-check`). Reference flags: `episode_flags` in
> `research-context/reef/experiments/04_gate_aa.py:251-289`
> (reef `818997d7`). Detector: `trial_diagnosis/v1` + Reef-protocol
> follow-on (this PR). Episodes are scored by the recorded
> `extra.reef.episode_score`; unscored/infra trials never occur here.

## Head-to-head (failed episodes only)

| Reef flag | Diagnosis mode | 04-check (75 failed) | 04 seed (169 failed) |
|---|---|---|---|
| ran code that printed nothing (61 / —) | `silent_tool_output` | 55/61, P 1.00 | 57/71, P 1.00 |
| called a tool with wrong arguments (19 / —) | `wrong_tool_arguments` | 19/19, P 1.00 | 23/23, P 1.00 |
| assumed state persisted (16 / —) | `state_persistence_assumption` | 16/16, P 1.00 | 25/25, P 1.00 |
| turn ended on an empty reply (— / 68) | `empty_terminal_reply` | n/a (0) | 68/68, P 1.00 |
| never used a tool | `no_tool_use` | 1/1 | 46/46, P 1.00 |

Zero false positives on every flag in both runs. (04-check has no quoted
empty-reply/never-tool expectations beyond the observed 0/1, both recovered.)

## What the fix changed (small, protocol-scoped)

Reef-harness protocols (`execute`, `run_bash`, `read_file`) needed four
adjustments; shell-harness behavior is byte-identical on all 46 audited
shell trials (0/46 mode changes) and aggregate shell counts are unchanged:

1. Parse the `execute` tool's `code` argument (was: commands invisible,
   defined-names and excerpts empty).
2. Treat a bare `exit 0` output as silence, and ANY such output as marking
   its step (multi-output steps pair confirmations with the marker).
3. `empty_terminal_reply` = no non-empty agent reply in the episode
   (04_gate_aa verbatim), multi-labeled with `no_tool_use`.
4. `planning_no_edit` is shell-harness-scoped; `run_bash` counts as shell
   so redirect writes suppress it; REPL NameErrors fire without a prior
   definition while shell trials keep the defined-names gate; `exit N`
   text prefixes count as step errors.

## Known divergences (reported, not chased)

- 6/61 + 14/71 silent residue: single `exit 0` with zero error context, or
  `exit 0` from expected-silent writes (`echo … > file`). Flagging those
  would trade the current 1.00 precision for recall on weak evidence.
- 13 episodes carry `planning_no_edit` with no writes at all; the advice is
  file-repair-oriented and weak for compute tasks. Same trade.
- Reef's `had the right number in tool output, never reported it` and
  `right number, not alone on the last line` have no ATIF counterpart
  without hidden verifier inputs and stay unimplemented by design.
