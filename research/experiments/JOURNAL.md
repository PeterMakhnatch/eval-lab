# Experiment journal

Running thread for the RUNNER role. Read top to bottom. Each study has
one section that is updated in place as status changes. Job directories
are under this worktree's `runs/` unless noted. Queue events are under
this worktree's `queue/events.jsonl`.

Worktree: `.worktrees/runner` on `role/runner`. Harbor 0.21.0. Policy
ceilings: $20/day, $3/job. Catalog spend at design time: $0.00 (oracle/nop
only).

## Gate status (2026-08-14)

| Check | Result |
|---|---|
| `evallab doctor` (human) | harbor, docker, uv, postgres, event-summary task all ok |
| `evallab doctor --headless` | **unhealthy** — `keychain_readable=false` (Claude OAuth item `harbor-practice-claude-oauth` absent) |
| Codex auth | `~/.codex/auth.json` present |
| Guarded tick | dispatches **nothing** while the doctor is unhealthy, including oracle/nop and Codex |
| Launchd tick | `cd ~/Developer/eval-lab && uv run evallab tick` every 30 min; main-checkout `queue/events.jsonl` is a sequence of `tick_quarantined` / `nightly_quarantined` with `headless_doctor_failed:keychain_readable` |
| This worktree queue | independent of the main checkout; launchd will not drain it |

Consequence: billable specs are submitted and then sit. Free baselines
run via `evallab matrix` (oracle/nop only; `execute_direct` refuses
Codex). That is not a policy bypass. It is the only path that actually
produces control evidence while tick is fail-closed.

## Study index

| ID | Variable | Admissible? | Status |
|---|---|---|---|
| 01 | canary task identity, Codex k=3 | yes — `canary` | 3/3 approved; tick quarantined |
| 02 | attempts {1,3,5} on txn-recon | k1 yes; k3 = 01 cell; k5 no | k1 approved; k5 `per_job_cost_ceiling` |
| 03 | extra-instruction preamble | design only | not submitted (harness cannot express the variable) |
| 04 | agent {codex, claude-code} | yes — `canary` | approved; tick quarantined |
| 05 | curated nominee identity | no | waiting `out_of_policy` |
| 06 | query-optimize registration | no | waiting `out_of_policy` |

Admitted billable estimate if tick were healthy: 3×$2.50 (01) + $0.83
(02 k1) + $2.50 (04) = **$10.83**.

---

## Study 01 — Codex pass@3 on the three pinned canaries

**What.** Three `ExperimentSpec` files under
`specs/01-canary-codex-pass-at-3/`. Variable is task identity.
`event-summary`, `transaction-reconciliation`,
`terminal-bench-html-js-filter`. Agent Codex, k=3, docker.

**Why.** Only billable slice standing policy will dispatch without
inventing `registered/*`. This is the lab's first model baseline, not a
leaderboard.

**Policy.** `canary`. Each job $2.50.

**n caveat.** k=3 is the canary maximum and is below the lab comparison
bar of ≥5. Do not rank the three tasks from these jobs.

**Status.** Three specs submitted and **approved** under `canary`.
Tick quarantined. No Codex trial exists.

**Results.** No Codex trials. Worktree oracle/nop k=5 for these
families is in "Control baselines" below (event-summary and
txn-recon 5/5; html-js-filter oracle 5/5, nop n=1). Prior catalog
oracle/nop on these families (n=1 unless noted), kept as
breadcrumbs only:

| Task | Agent | n | reward | seconds | Source |
|---|---|---:|---:|---:|---|
| event-summary | oracle | 1 | 1.0 | 8.88 | `evidence/runs/event-summary-oracle-evidence` |
| event-summary | nop | 1 | 0.0 | 8.04 | `evidence/runs/event-summary-nop-evidence` |
| transaction-reconciliation | oracle | 2 | 1.0, 1.0 | 8.56, 12.69 | catalog `…-oracle-20260813`, `brief07-transaction-oracle` |
| transaction-reconciliation | nop | 1 | 0.0 | 7.75 | catalog `…-nop-20260813` |
| html-js-filter | oracle | 1 | 1.0 | 189.32 | catalog `terminal-bench-html-js-filter-oracle-20260813` |
| html-js-filter | nop | 1 | 0.0 | 79.27 | catalog `terminal-bench-html-js-filter-nop-20260813` |

n=1 (or n=2 on txn-recon oracle) is not a pass-rate. Those rows are
task-validity breadcrumbs. Wilson intervals are not computed for them.

**Trajectory (existing event-summary oracle, n=1).**
`evidence/runs/event-summary-oracle-evidence/event-summary__FZg7pvq/`:
Oracle writes `/app/output/summary.json`; verifier `checks.json` reports
correctness, input_preservation, output_hygiene all 1.0; no exception.
This is the reference solution running, not an agent solving the task.

**Next spec.** Wait for Codex jobs. If tick stays quarantined, the next
useful measurement is the k=5 oracle/nop matrices in `baselines/`, not a
second model.

---

## Study 02 — Attempt-count sensitivity (transaction-reconciliation)

**What.** k=1 and k=5 specs; k=3 is Study 01's txn-recon cell.

**Why.** The lab bar is ≥5 for comparisons. Canary policy max is 3. The
point of this study is to measure that collision, not to paper over it.

**Policy.** k1 admitted (`canary`, $0.83). k5 estimated at
5 × ($2.50 / 3) = $4.17 → `per_job_cost_ceiling`. Even at $2.99, canary
would still refuse attempts>3.

**Status.** See Queue log.

**Results.** None until k1 and the Study 01 k=3 cell both complete.
Interpreting k1 alone would be a claim from n=1.

**Next spec.** If a completed k=3 job's actual `cost_usd` is ≤ $1.80,
a re-estimated k=5 job would clear the $3 ceiling and still fail
`canary` max_attempts. That is then a `registered/*` question for Peter.

---

## Study 03 — Instruction-preamble A/B

**What.** Control = Study 01 event-summary. Treatment = same cell plus
`preambles/brief-discipline.md` via `--extra-instruction-path`.

**Why.** Event-summary is a tight contract (exact keys, percentile
definition, output hygiene). A short "satisfy the stated contract"
preamble is one variable.

**Status.** **Not submitted.** Harbor 0.21 has the flag;
`ExperimentSpec` and `build_command` do not. Submitting
`treatment.intended.json` would re-run the control. RUNNER does not
edit `src/`.

**Results.** None.

**Next spec.** BUILDER adds `extra_instruction_path` to the spec and
forwards the flag. Then submit the treatment arm only.

---

## Study 04 — claude-code vs Codex on event-summary

**What.** `specs/04-claude-code-canary/event-summary.json`. Variable is
agent. Codex cell is Study 01.

**Why.** Standing `canary` rule already lists `claude-code`. First
paired-agent study the moment the token exists.

**Policy.** Admitted. Dispatch blocked by the same headless-doctor
keychain check that blocks Codex.

**Status.** See Queue log.

**Results.** None. An auth exception, if tick is forced before the
token exists, is harness/credential, not a model failure.

**Next spec.** After one successful claude-code job, add the other two
canary members (still k=3, still not a ranking).

---

## Study 05 — Curated nominee pass@5

**What.** Five-task design; one representative submitted
(`library/curated/html-js-filter`, k=3, $2.50) so the reason code is
the namespace refusal, not the $3 ceiling.

**Why.** CURATOR nominated these five. They are cards, not runnable
tasks, in this checkout.

**Policy.** `out_of_policy`. Using `registered/*` would be a stretch.

**Status.** See Queue log.

**Results.** None. CURATOR oracle/nop cards are validity evidence in
another worktree.

**Next spec.** Peter registers a slice or promotes nominees into the
canary suite with digests. Do not copy `frontier-bench` into this repo
from RUNNER.

---

## Study 06 — query-optimize registration probe

**What.** `task=tasks/query-optimize`, Codex k=3, $2.50.

**Why.** Fourth lab-authored family; timing-sensitive SQL rewrite; not
in the canary suite. Submitted on the real path so the engine answers
the registration question.

**Policy.** `out_of_policy`. Poor canary (900s agent timeout, 1800s
verifier, prebuilt image, 5-iteration timing gate). Reasonable first
`registered/*` candidate if oracle/nop pass.

**Status.** Submitted; waiting `out_of_policy`. Free baseline complete.

**Results.** Oracle 5/5 (Wilson 0.566–1.000), nop 0/1. ~10 min/trial
on this host. See Control baselines.

**Next spec.** Peter can register it if he wants a hard/slow family.
Do not add it to the nightly canary suite.

---

## Control baselines (oracle / nop)

Free. Run with `evallab matrix` from this worktree, `jobs_dir=runs`,
concurrency 2. These test task and harness validity. They are not
model evidence.

Wilson 95% for 5/5 is (0.566, 1.000); for 0/5 is (0.000, 0.434). For
n=1, (0.207, 1.000) / (0.000, 0.793). `evallab matrix` exits 1 on
every k>1 job because `expected_primary_reward` only compares when
`len(trials)==1` and otherwise treats the actual as `None`. That is a
lab-harness quirk, not a task failure. Rewards below are from each
job's `result.json`.

### event-summary — done

Jobs: `runs/runner-es-oracle-k5` (job `7dbcaa2f-…`),
`runs/runner-es-nop-k5` (job `18a8be56-…`). Harbor 0.21.0, Docker
29.4.1, commit `014d6f0`, 2026-08-14T05:14Z.

| Agent | n | rewards | Wilson 95% | wall (job) |
|---|---:|---|---|---|
| oracle | 5 | 5×1.0 | (0.566, 1.000) | 27s |
| nop | 5 | 5×0.0 | (0.000, 0.434) | 24s |

Oracle trials `event-summary__{WSfk8Ms,3Uyw27i,NRMbpf5,uEWJTH9,8DQiAxF}`:
`agent/oracle.txt` is empty (reference `solve.py` is copied, not
logged). Verifier `checks.json` on `WSfk8Ms`: schema, correctness,
input_preservation, output_hygiene all passed. Artifact
`summary.json` is
`{"schema_version":1,"total_events":8,"counts":{"cache_hit":2,"error":1,"request":4,"retry":1},"total_duration_ms":1617,"p95_duration_ms":900}`.

Nop trials `event-summary__{6UzRped,BshLfBf,HaKG6sR,9poYPB8,3nog5mF}`:
`/app/output` is empty. `6UzRped` checks: schema "summary.json is
missing", correctness "wrong summary", output_hygiene
"extra/missing output", input_preservation passed. Attribution:
**task** (unsolved start state does not pass) + **agent** (nop writes
nothing). Not harness: verifier completed, no exception.

What this does **not** say: nothing about Codex. The task is valid.

### transaction-reconciliation — done

Jobs: `runs/runner-tr-oracle-k5` (job `5d96f93e-…`),
`runs/runner-tr-nop-k5`. Same host/tools.

| Agent | n | rewards | Wilson 95% | wall (job) |
|---|---:|---:|---|---|
| oracle | 5 | 5×1.0 | (0.566, 1.000) | 38s |
| nop | 5 | 5×0.0 | (0.000, 0.434) | 31s |

Oracle `transaction-reconciliation__dC5d9s5` verifier: pytest
`test_ledger_entries_are_reconciled_without_collateral_changes`,
`test_settlement_feed_is_unchanged`,
`test_database_schema_is_preserved` all passed. `reward.txt` = `1`.
`agent/oracle.txt` empty; solution is a single SQLite `UPDATE`
joining `settlement_feed`.

Nop `transaction-reconciliation__5WTPKTF`: feed and schema tests
pass; ledger test fails at `txn_1004` (`7500, 7050, 'pending'` vs
expected `7500, 7500, 'reconciled'`). Attribution: **task** (seeded
discrepancy) + **agent** (nop does not update the row). Not a
harness exception.

Harness note, not a failure: every trial's verifier `apt-get install`s
curl and downloads uv before pytest (~10s of the ~12s wall, public
network). That is task-image waste, not flakiness in this n=5.

### html-js-filter — done

Jobs: `runs/runner-hjf-oracle-k5`, `runs/runner-hjf-nop-k1`. Separate
`evallab run` calls (the matrix path died on catalog ingest after
query-optimize oracle). Harbor still wrote complete job directories.

| Agent | n | rewards | Wilson 95% | seconds / trial |
|---|---:|---|---|---|
| oracle | 5 | 5×1.0 | (0.566, 1.000) | 129.5–194.6 |
| nop | 1 | 0.0 | (0.000, 0.793) | 8.8 |

Oracle `terminal-bench-html-js-filter__8VG3Bbm` verifier:
`test_filter_blocks_xss` ("Filter successfully blocked all 444 XSS
attack vectors") and `test_clean_html_unchanged` ("preserved all 12
clean HTML files"), 2 passed in 120.90s, `reward.txt=1`. Artifact
`/app/filter.py` present. The ~2 minute pytest is the XSS corpus, not
agent work (oracle.txt is the copied reference).

Nop `terminal-bench-html-js-filter__CG7CCNn`: both tests fail in 0.06s
with `filter.py does not exist`. `reward.txt=0`. Attribution: **task**
(contract requires `/app/filter.py`) + **agent** (nop writes nothing).
Not harness: verifier completed, no exception. n=1 is enough for a
deterministic missing-file fail; it is not a rate.

The two slower oracle trials (~195s) vs three faster (~130s) look like
image/cache warmup, not reward noise. All five rewards are 1.0.

### query-optimize — done

Jobs: `runs/runner-qo-oracle-k5` (job `94652a72-…`),
`runs/runner-qo-nop-k1` (`query-optimize__msnBDS8`).

| Agent | n | rewards | Wilson 95% | seconds / trial |
|---|---:|---|---|---|
| oracle | 5 | 5×1.0 | (0.566, 1.000) | 581–587 |
| nop | 1 | 0.0 | (0.000, 0.793) | 563 |

Oracle `8NDQkjL` verifier: 6 passed in 569.74s. Timing test captured
golden median 0.997s vs solution 0.990s (speedup 1.008). Almost all
of the 9.5 minutes is verifier setup: apt, uv, **cpython-3.13.9
linux-x86_64** download, then six tests. Image
`alexgshaw/query-optimize:20251031` is amd64 on this arm64 Mac.

Nop `msnBDS8`: 4 failed, 2 passed in 547.34s. Failures are all
`Solution SQL not found at /app/sol.sql`
(`test_compare_golden_vs_solution_runtime`,
`test_outputs_match_exactly`,
`test_solution_contains_single_sql_query`, `test_solution_is_small`).
Passes are `test_compare_golden_vs_my_sql_query_correctness` (the
stock unoptimized query) and `test_check_for_db_modifications`.
Attribution: **task** (requires `/app/sol.sql`) + **agent** (nop).
The 9-minute nop is **harness/verifier**: it does not fail fast; it
still pays the x86_64 setup tax. Not a capability result.

After query-optimize oracle, `evallab matrix` crashed in
`database.initialize` with `psycopg.errors.InvalidTableDefinition:
cannot drop columns from view`. The shared Postgres catalog has a
schema this worktree's `sql/schema.sql` cannot re-apply. Subsequent
`evallab run` calls still produced jobs; each then failed at
ingest with the same error. RUNNER does not edit `sql/`. Evidence is
the job directories, not the catalog.

**Next spec this implies.** Do not promote query-optimize to the
nightly canary suite. If Peter registers it, budget ~10 minutes per
trial on this host and treat timing-test failures as
environment-suspect until the image is arm64 or the verifier stops
reinstalling an x86_64 CPython every trial.

---

## What the controls imply for the six studies

All four lab-authored families are **valid tasks** on this host
tonight: oracle 5/5, nop 0. The Wilson interval on a 5/5 oracle is
(0.566, 1.000). That is harness stability, not a model score.

Study 01 (Codex pass@3 on the three canaries) is the first study that
would measure an agent. It is **approved and not run**. Reading
oracle trajectories does not substitute. The honest status of
"Codex on event-summary / txn-recon / html-js-filter" is unknown.

Study 02 cannot be interpreted without the k=1 and k=3 Codex cells.

Study 03 is still a harness request (forward
`--extra-instruction-path`).

Study 04 is the same doctor block as Study 01, plus the missing
Claude token.

Study 05 remains a registration + materialization question.

Study 06: the free baseline says the task is valid and expensive.
The standing policy correctly refused the Codex spec
(`out_of_policy`). Registering it is a cost/time decision, not a
validity decision.

### Failure-attribution cheat sheet (controls only)

| Observation | Task | Agent | Harness |
|---|---|---|---|
| event-summary nop: `summary.json` missing | contract | nop writes nothing | verifier ran |
| txn-recon nop: `txn_1004` still 7050/pending | seeded discrepancy | nop writes nothing | verifier ran; apt+uv every trial is waste |
| html-js-filter nop: `filter.py` missing | contract | nop writes nothing | verifier fail-fast (good) |
| query-optimize nop: `/app/sol.sql` missing | contract | nop writes nothing | verifier does **not** fail-fast (~9 min) |
| query-optimize 9 min / trial | amd64 image + uvx CPython | n/a | environment + verifier setup |
| `evallab matrix` / `run` exit 1 after success | n/a | n/a | catalog `initialize` vs live views |
| `evallab matrix` "expected 1, got None" on k=5 | n/a | n/a | compare only when `len(trials)==1` |
| Codex / claude-code not dispatched | n/a | n/a | headless doctor requires Claude keychain |

---

## Queue log

Submitted 2026-08-14T05:14:40Z from this worktree. `evallab tick`
immediately after: `dispatched 0`, `quarantined: yes`,
`reason_code=headless_doctor_failed:keychain_readable`.

| Spec | spec_id | Gate | State | Reason |
|---|---|---|---|---|
| runner-canary-event-summary-codex-k3 | `01KZZB36PPBKM863RB5R2MQDZG` | `canary` | approved | — |
| runner-canary-txn-recon-codex-k3 | `01KZZB36VPXEQ6E8D0QZ13SRNZ` | `canary` | approved | — |
| runner-canary-html-js-filter-codex-k3 | `01KZZB370NKG312T8ZB17Y371H` | `canary` | approved | — |
| runner-txn-recon-codex-k1 | `01KZZB375FWNZERK8V09RJYNGN` | `canary` | approved | — |
| runner-txn-recon-codex-k5 | `01KZZB37AF6HKA21A57NP5D0N8` | refused | waiting | `per_job_cost_ceiling` ($4.17 > $3.00) |
| runner-canary-event-summary-claude-k3 | `01KZZB37F6S9NHAYHR7W5KAR5S` | `canary` | approved | — |
| runner-curated-html-js-filter-codex-k3 | `01KZZB37M99GPF788SXF5CJ1EF` | refused | waiting | `out_of_policy` |
| runner-query-optimize-codex-k3 | `01KZZB37SFMQJ9JEAHC41HVXDG` | refused | waiting | `out_of_policy` |
| (not submitted) runner-event-summary-codex-preamble-k3 | — | — | — | harness cannot express `--extra-instruction-path` |

Events: this worktree `queue/events.jsonl` (submitted → policy_admitted
or policy_waiting, then one `tick_quarantined`). Reason files:
`queue/reasons/<spec_id>-<ulid>.json`. Launchd continues to tick the
**main checkout**, not this worktree.

PR: https://github.com/PeterMakhnatch/eval-lab/pull/5
(left open; CI ruff fails on files this role does not own).

Peter: to run the five approved jobs, store the Claude keychain item
(unguards tick for everyone) **or** split the headless doctor so a
missing Claude token does not quarantine Codex/oracle dispatch. The
five approved jobs live in `.worktrees/runner/queue/approved/`. They
are not visible to the main-checkout LaunchAgent.

---

## Interpretation rules used in this journal

- No pass-rate claim from n=1.
- Report n and a Wilson 95% interval on any comparison.
- Attribute failures to task, agent, or harness with a path and a step,
  or write `unknown`.
- Oracle/nop success is not Codex success.
- A quarantined tick is not a deferred spec; a spec in `waiting/` with
  a reason file is.
