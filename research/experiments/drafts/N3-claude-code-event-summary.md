# Draft N3 — claude-code vs Codex on event-summary

**Status:** designed (unsubmitted; Study 04 still the spec).
**PROGRAM id:** `EXP-N3-claude-code-event-summary`

**One variable.** Agent ∈ {codex, claude-code}.

**Fixed elicitation.** `task=canary/event-summary`, k=3, docker, no
extra instruction. Codex cell already exists:
`runs/canary-event-summary-codex-20260815` (3/3 reward 1.0,
codex 0.147.0 / gpt-5.6-terra).

**n / k.** n_tasks=1, k=3. **not distinguishable / not comparable** as
a ranking. First value of the pair is “does claude-code complete a
scored canary trial.” Auth exceptions stay outside the capability
denominator.

**What would change the decision.**

- Auth/harness exception → store the Claude keychain item; do not
  write a capability card.
- Scored 3/3 or 0/3 with no exception → enough to decide whether to
  spend a second night on the other two canary tasks (still not a
  ranking).

**Dependencies.** Auth: `harbor-practice-claude-oauth` keychain.
Policy: `canary` already lists `claude-code`. Spec already written:
`specs/04-claude-code-canary/event-summary.json`. Not resubmitted here.

**Avoided.** Do not expand to three tasks until one claude-code trial
scores.
