# Backlog: cards that agents can take in parallel

Rules are in [AGENTS.md](../AGENTS.md). Each card owns its files. No 2 cards own the same file.
A card that needs a kernel change says so on its `Kernel` line, and only 1 such card is open at a time.

Status values: `open`, `taken`, `done`. Write the owner and the date when you take a card.

| # | Card | Status | Kernel change |
|---|---|---|---|
| 1 | `context`: where the input tokens go | done (2026-11-18) | none |
| 2 | `usage`: cost, tokens and time per arm | open | none |
| 3 | `tasks`: which tasks carry the signal | open | none |
| 4 | `parity`: equivalence test for adapter parity runs | open | none |
| 5 | `baseline`: save a baseline, compare later runs with it | open | none |
| 6 | `controls`: classify oracle and nop runs | open | none |
| 7 | Sequential gate: stop early without false alarms | open | `stats.py` |
| 8 | Attempt ledger: keep the attempts that Harbor deletes | open | none (separate package) |
| 9 | HTML report | open | none |
| 10 | Release to PyPI | open | none |
| 11 | Replay server: run a recorded trial again for $0 | open | none (separate package) |
| 12 | Seat trials and `decompose`: headcount and coordination of a team | done (2026-11-19) | `model.py`, `load.py`, `audit.py`, `commands/_common.py` |

---

## 1. `context`: where the input tokens go

- **Owner:** context agent, 2026-11-18.
- **Goal:** for each arm, read `agent/trajectory.json` (ATIF) and report model calls per trial,
  input tokens, peak context, the share of input that is re-sent context, the share of re-sent
  context that the prompt cache serves, context drops, and cost per passed trial.
- **Files:** `src/tidegauge/atif.py`, `src/tidegauge/commands/context.py`, `tests/test_atif.py`,
  `tests/test_context.py`.
- **Done when:** the command runs on the 6 public submissions with per-call metrics, every number
  in its tests is computed by hand, and accounting mismatches are findings, not silent fixes.
- **Not in scope:** stitching continuation files, subagent trajectories, any tokenizer.
- **Open after the first version:** steps with `llm_call_count` above 1 add up several calls, so
  their peak context is too high; a finding for this is not written yet. Paired statistics for
  the context numbers (cost per pass with an interval) need the numbers as kernel metrics.

## 2. `usage`: cost, tokens and time per arm

- **Goal:** one table per arm from `result.json` only: cost per trial and per passed trial, input,
  cache and output tokens, seconds per phase (environment setup, agent setup, agent, verifier),
  share of trials that hit the agent timeout, and how many timed-out trials still passed.
- **Files:** `src/tidegauge/commands/usage.py`, `tests/test_usage.py`.
- **Done when:** totals match a hand count on a fixture, and a trial without usage data lowers the
  coverage number instead of counting as 0.
- **Why:** Harbor issues report cost and token totals that are wrong by large factors. A user must
  see the coverage of the numbers before trusting a cost per pass.

## 3. `tasks`: which tasks carry the signal

- **Goal:** across many arms, list for each task: pass rate over arms, how often attempts disagree
  inside one arm, and how well the task separates strong arms from weak arms. Then answer: which
  subset of N tasks keeps the ranking of the arms?
- **Files:** `src/tidegauge/commands/tasks.py`, `tests/test_tasks.py`.
- **Done when:** on the public leaderboard data the command names the tasks that every arm passes
  or fails, and a held-out check shows how the ranking from a subset agrees with the full ranking.
- **Not in scope:** any model of task difficulty that needs fitting with an external library.

## 4. `parity`: equivalence test for adapter parity runs

- **Goal:** Harbor accepts a benchmark adapter when the score ranges of the original runs and the
  Harbor runs overlap, usually with 3 runs per side. Replace that rule with an equivalence test:
  given 2 sets of run scores and a margin, say `equivalent`, `different` or `undecided`.
- **Files:** `src/tidegauge/commands/parity.py`, `tests/test_parity.py`.
- **Input:** `parity_experiment.json` files from the Harbor adapters repository.
- **Done when:** the command reproduces the range-overlap verdict next to its own verdict for the
  public parity corpus, and the test file proves the error rates of the new rule by simulation.

## 5. `baseline`: save a baseline, compare later runs with it

- **Goal:** `tidegauge baseline save NAME PATH` writes a small JSON summary (per task: outcomes,
  checksum, conditions). `tidegauge compare`, `gate` and `plan` accept that file in place of a
  directory. A team can then delete or archive the large job directory.
- **Files:** `src/tidegauge/commands/baseline.py`, `tests/test_baseline.py`.
- **Kernel note:** needs `load_arm` to accept a summary file. Ask before you start.

## 6. `controls`: classify oracle and nop runs

- **Goal:** read existing job directories of Harbor's `oracle` and `nop` agents and classify each
  task: reference passes and nop fails (`clean`), reference fails, nop passes, cannot run, no
  reference. Declared expectations come from a small TOML file. tidegauge does not run trials.
- **Files:** `src/tidegauge/commands/controls.py`, `tests/test_controls.py`.
- **Done when:** a skipped control never yields `clean`, a changed task checksum never reuses an
  old result, and results from 2 sandboxes show `depends on the sandbox` when they differ.

## 7. Sequential gate: stop early without false alarms

- **Goal:** an agent loop wants to stop a comparison as soon as the answer is clear. Looking after
  every batch inflates the false alarm rate. Add a group-sequential rule (alpha spending) to
  `gate` and prove its error rates by simulation.
- **Files:** `src/tidegauge/sequential.py`, `tests/test_sequential.py`, 1 option in `commands/gate.py`.
- **Kernel:** adds 1 function to the statistics layer. Take this card alone.
- **Done when:** with 5 looks, the simulated false alarm rate stays at or below 5%, and the doc
  states the expected number of trials saved against a fixed design.

## 8. Attempt ledger: keep the attempts that Harbor deletes

- **Goal:** Harbor deletes the directory of an attempt before it retries. A Harbor plugin records
  every attempt (result, tokens, cost, exception) in `attempts.jsonl` inside the job directory,
  and `tidegauge audit` reads that file when it exists.
- **Files:** a separate package under `plugins/harbor-attempt-ledger/`. It may import Harbor.
- **Done when:** a job with 1 forced retry keeps 2 attempt records, and the plugin adds no failure
  mode to the job when it cannot write.

## 9. HTML report

- **Goal:** `--html FILE` on `compare` and `rank` writes 1 self-contained HTML file, with no
  script from the network, that shows the same report dictionary as a page.
- **Files:** `src/tidegauge/html.py`, `tests/test_html.py`.

## 10. Release to PyPI

- **Goal:** tagged releases publish from CI with trusted publishing. `uvx tidegauge` works.
- **Files:** `.github/workflows/release.yml`, `docs/RELEASING.md`.
- **Needs:** the owner's decision to publish, and the PyPI project.

## 11. Replay server: run a recorded trial again for $0

- **Goal:** a small server that speaks the model API and answers with the model turns from a
  recorded ATIF trajectory. Harbor can then run the same actions again in another sandbox, or
  after a verifier change, with no model cost. A divergence report says at which step the new run
  stopped matching the record.
- **Files:** a separate package under `replay/`.
- **Why separate:** it runs trials, so it does not belong in the read-only tool.
- **Done when:** Terminus 2 trajectories replay on local Docker and the divergence report is exact.

## 12. Seat trials and `decompose`: headcount and coordination of a team

- **Owner:** seats agent, 2026-11-19.
- **Goal:** read the trials of the `olamlabs/harbor-multiagent` fork, which runs 1 agent per seat,
  and split the gain of a team into a headcount effect and a coordination effect:
  `team - solo = (indep - solo) + (team - indep)`, each with both error bars.
- **Files:** `src/tidegauge/commands/decompose.py`, `tests/test_decompose.py`, `tests/test_seats.py`.
- **Kernel:** `Trial` has `seats`, `model_seats`, `seat_exceptions` and `partial_usage`. The loader
  reads `seat_results` and `config.participants`. The audit has the note `seat-errors`, and
  `missing-usage` counts the trials with usage from a part of a team. `context` reads 1 trajectory
  per seat. A reward name is valid as `--metric` also when a trial lacks that reward.
- **Done when:** the 2 effects add up to the total on the tasks that all 3 arms have, every number
  in the tests is computed by hand, and a trial with 1 agent reads exactly as before.
- **Open after the first version:** the share of coordination has no interval (Fieller's method
  can give one). `decompose` has no table per seat: `--reward-key seat.<name>` gives the credit of
  1 seat. Task variants pair by name, or by the first group of `--task-key` in the name. The audit
  does not use `under_test`.
