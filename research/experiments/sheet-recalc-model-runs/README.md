# `sheet-recalc` model runs (2026-10-28)

**Question.** Do Claude Code runs on the lab's subscription gateway solve a
hand-authored incremental-recomputation task, and does adding an early-cutoff
rule separate models that implement the contract from models that approximate it?

**What ran.** Two packages, `library/tasks/sheet-recalc` (exactly-once
recalculation) and `library/tasks/sheet-recalc-cutoff` (same engine, plus early
cutoff), through `evallab tasks prepare → submit → approve → tick` on local
Docker, agent `claude-code` over the host-loopback gateway lane
(`claude-gateway/claude-{haiku,sonnet,opus}-5-5`). Verifier is
`environment_mode = "separate"`; reward is 1 only if all hidden tests pass.
Every trial's gateway ledger records `model_requested == model_served`, so the
attested model matches the requested one.

**Cost.** $0 billed. All trials drew on Peter's Claude subscription.

## `sheet-recalc` (exactly-once)

| Model | Trials | Reward 1 | Wall time | Input tokens | Output tokens |
|---|---|---|---|---|---|
| claude-haiku-5-5 | 2 | 2 | 338, 372 s | 149.6K, 150.8K | 19.3K, 19.6K |
| claude-sonnet-5-5 | 1 | 1 | 291 s | 57.6K | 3.9K |
| claude-opus-5-5 | 1 | 1 | 431 s | 168.5K | 8.4K |

**Meaning.** This contract is not discriminating on its own: every trial passed,
including Haiku. The task is a fair, well-controlled environment (see the
control matrix in the package README), but as a difficulty probe it is too easy
for these models.

## `sheet-recalc-cutoff` (exactly-once + early cutoff)

| Model | Trials | Reward 1 | Wall time | Input tokens | Output tokens |
|---|---|---|---|---|---|
| claude-haiku-5-5 | 2 | 2 | 264, 325 s | 295.0K, 259.9K | 33.6K, 37.3K |
| claude-sonnet-5-5 | 2 | 0 | 221, 257 s | 82.4K, 59.0K | 4.6K, 4.6K |
| claude-opus-5-5 | 2 | 2 | 204, 186 s | 118.3K, 119.6K | 9.3K, 9.7K |

**Meaning.** Adding one rule separated the models where the first contract did
not: both Sonnet trials failed the same way while both Haiku and both Opus
trials passed. Both Sonnet failures passed every built-in cutoff case and failed only the
randomized sequences, and every failure is an **extra** evaluation, never a
wrong value or a missing evaluation:

- `seed=37`: setting `C8` from 6 to 7 made Sonnet evaluate `B3` and `A7`, which
  do not read `C8` at all (`B3` reads `A4:D5` and `E1`; `A7` reads `A4`, `B3`,
  `B4:B5`, `C4:C5`, `D4:D5`, `E4:E5`). The oracle expects zero evaluations,
  because nothing reads `C8`.
- `seed=23`: rewriting `A3` as `=MIN(C7, 0)` changed its value from 0 to -2. Its
  only formula readers, `A4` and `C2`, are on cycles (self-referential ranges),
  so they stay `#CYCLE` and are not evaluated. The oracle expects `A3` alone;
  Sonnet also evaluated `B4` and `B6`, which read `C4:F8`, `E2` and `B4`.

So the failures are over-evaluation: cells outside the affected set, and cells
whose inputs did not change. That is the property the contract states and two
other models implemented, so this is a contract-level miss, not ambiguity.

## Limits

- n = 1 or 2 per model per task. These are directional, not a ranking.
- The randomized sequences are six fixed hidden seeds (300 operations each). A
  passing engine passed those sequences, not every possible edit stream.
- No Codex trials: the ChatGPT weekly quota was exhausted at 100% when these
  runs started. No non-Anthropic comparison is claimed.
- All trials ran on a busy host (load average > 200 from the agent fleet); wall
  times are inflated and are not a cost or latency measurement.

## Evidence

- Runs: `runs/sheet-recalc-cc-*` and `runs/sheet-recalc-cutoff-cc-*` in the
  eval-lab worktree that produced this record.
- Control matrices (oracle 1, nop 0, alternative-correct 1, seven exploit and
  partial controls 0) are in each package README.
