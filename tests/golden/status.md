---
status: living
audience:
  - operator
  - builder
  - runner
---

# Research status — 2026-08-16

Projection of live catalog, queue state, and `PROGRAM.json`.
Answers what happened yesterday and what is running now deterministically.

## RECENT (Yesterday: 2026-08-15)

- **canary/event-summary** — 0/2 `reward==1.0` via codex (gpt-5.6-terra) [exceptions: EnvironmentError=1]
- **library/tasks/event-summary** — 2/2 `reward==1.0` via oracle

## RUNNING NOW

- `[APPROVED]` **base-a** (`BASEA`) — task=`canary/event-summary`, agent=`codex` [purpose: baseline]
- `[APPROVED]` **cmp-alpha** (`CMPALPHA`) — task=`t/alpha`, agent=`codex` [purpose: comparison]
- `[APPROVED]` **cmp-bravo** (`CMPBRAVO`) — task=`t/bravo`, agent=`codex` [purpose: comparison]
- `[APPROVED]` **cmp-charlie** (`CMPCHARLIE`) — task=`t/charlie`, agent=`codex` [purpose: comparison]

## NEXT

- `[proposed]` **oracle-control** (`ORACLECONTROL`): task=`library/tasks/event-summary`, agent=`oracle` [purpose: practice]
- `[waiting]` **canary-run** (`01GOLDENWAIT00000000000000`): task=`canary/event-summary`, agent=`codex` [purpose: drift] — *Reason/Blocker:* daily_cost_ceiling: recorded by the gate

### Program Ledger Next Actions

1. **EXP-S01-canary-codex-k3** (`status: designed`): Does Codex pass@3 on event-summary remain above baseline?
   - *Next Action:* Submit treatment spec

## TASK DECISIONS

Human-owned, unresolved decisions from active proposals and policy review.

- **EXP-S01-canary-codex-k3**: Decision pending human review

## SYSTEM HEALTH & OPERATIONAL SMOKE

- Catalog accessible: yes
- Operational smoke/control specs count: 1
- Active storm alarms: 0 (quiet: no alarms in window)
