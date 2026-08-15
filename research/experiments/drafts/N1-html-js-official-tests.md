# Draft N1 — html-js-filter official-test instruction

**Status:** designed (unsubmitted).
**PROGRAM id:** `EXP-N1-html-js-official-tests`

**One variable.** Presence of a short extra instruction: run official
`tests/test_outputs.py` before declaring done.

**Fixed elicitation.** `agent=codex`, `model=gpt-5.6-terra`,
`task=canary/terminal-bench-html-js-filter`, k=3, docker, no other
preamble. Control cell = 2026-08-15 html-js job
(`runs/canary-terminal-bench-html-js-filter-codex-20260815`, 0/3
reward 1.0, 0 exceptions).

**n / k.** n_tasks=1, k=3 (canary max). TRUTH comparability: this is
**not distinguishable / not comparable** as a ranking across tasks
(n_tasks=1). It is a within-task A/B against the 2026-08-15 control
**if and only if** agent version, model pin, k, and toolset match and
the only changed field is the extra instruction.

**What would change the decision.**

- XSS still 0/3 with same `srcdoc` first vectors → implementation-limit
  hypothesis; stop instruction tweaks; consider a harder *task version*
  or accept this canary as currently unsolved by this elicitation.
- Any trial reward 1.0 → process hypothesis; consider making official
  tests visible in the default instruction (separate human decision).

**Dependencies.** Harness: `ExperimentSpec` has no
`extra_instruction_path`; `build_command` does not forward Harbor
`--extra-instruction-path`. Policy: `canary` would admit a $2.50 k=3
job. Auth: Codex. Registry: already a canary member.

**Avoided.** No 3×3 grid. Does not resubmit the control.
