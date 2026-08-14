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
| `harbor-lab doctor` (human) | harbor, docker, uv, postgres, event-summary task all ok |
| `harbor-lab doctor --headless` | **unhealthy** — `keychain_readable=false` (Claude OAuth item `harbor-practice-claude-oauth` absent) |
| Codex auth | `~/.codex/auth.json` present |
| Guarded tick | dispatches **nothing** while the doctor is unhealthy, including oracle/nop and Codex |
| Launchd tick | `cd ~/Developer/harbor-experiment-lab && uv run harbor-lab tick` every 30 min; main-checkout `queue/events.jsonl` is a sequence of `tick_quarantined` / `nightly_quarantined` with `headless_doctor_failed:keychain_readable` |
| This worktree queue | independent of the main checkout; launchd will not drain it |

Consequence: billable specs are submitted and then sit. Free baselines
run via `harbor-lab matrix` (oracle/nop only; `execute_direct` refuses
Codex). That is not a policy bypass. It is the only path that actually
produces control evidence while tick is fail-closed.

## Study index

| ID | Variable | Admissible? | Status |
|---|---|---|---|
| 01 | canary task identity, Codex k=3 | yes — `canary` | submitted; waiting on tick |
| 02 | attempts {1,3,5} on txn-recon | k1 yes; k3 = 01 cell; k5 no | k1 submitted; k5 waiting (`per_job_cost_ceiling`) |
| 03 | extra-instruction preamble | design only | not submitted (harness cannot express the variable) |
| 04 | agent {codex, claude-code} | yes — `canary` | submitted; will not dispatch (keychain) |
| 05 | curated nominee identity | no | submitted representative; `out_of_policy` |
| 06 | query-optimize registration | no | submitted; `out_of_policy` |

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

**Status.** Specs written. Submission and queue state recorded below
under "Queue log".

**Results.** No Codex trials exist in the catalog. Prior oracle/nop on
these families (main catalog, n=1 unless noted):

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

**Status.** See Queue log.

**Results.** Free baseline in `baselines/query-optimize-controls.json`
(see Control baselines).

**Next spec.** Only if the oracle k=5 matrix passes and Peter registers
the name.

---

## Control baselines (oracle / nop)

Free. Run with `harbor-lab matrix` from this worktree, `jobs_dir=runs`,
concurrency 2. These test task and harness validity. They are not
model evidence.

| Family | Matrix | Oracle n | Nop n | Status |
|---|---|---:|---:|---|
| event-summary | `baselines/event-summary-controls.json` | 5 | 5 | pending |
| transaction-reconciliation | `baselines/transaction-reconciliation-controls.json` | 5 | 5 | pending |
| html-js-filter | `baselines/html-js-filter-controls.json` | 5 | 1 | pending |
| query-optimize | `baselines/query-optimize-controls.json` | 5 | 1 | pending |

Nop n=1 on the two slow families is intentional: nop is a deterministic
empty agent; repeating a ~80s+ fail five times buys harness-noise
information at a high wall-clock cost. The two cheap families run nop
n=5.

Wilson 95% interval for 5/5 successes is approximately (0.57, 1.00).
That is the right width to print if oracle is 5/5. It does not license
a capability claim.

---

## Queue log

Filled after `harbor-lab submit` and `harbor-lab tick`.

---

## Interpretation rules used in this journal

- No pass-rate claim from n=1.
- Report n and a Wilson 95% interval on any comparison.
- Attribute failures to task, agent, or harness with a path and a step,
  or write `unknown`.
- Oracle/nop success is not Codex success.
- A quarantined tick is not a deferred spec; a spec in `waiting/` with
  a reason file is.
