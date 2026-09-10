---
status: living
audience:
  - builder
  - analyst
type: architecture-perspective
topic: eval-lab position in the Harbor ecosystem
date: 2026-09-06
base: origin/main b9fca536
standing: every claim cites either an inspected repo path:line or a corpus source in ~/Developer/research-context/harbor
---

# Where eval-lab sits in the Harbor ecosystem, and what it is missing

Source of the ecosystem side: `~/Developer/research-context/harbor`
(`NATIVE-CATALOG.md`, `PLUG-PLAY-PALETTE.md`, `TRAJECTORY-DATA-CONSUMERS.md`,
`OPERATIONS.md`, `PATHS-FORWARD.md`, `expert/{TRIED,NEXT,ECOSYSTEM-MAP}.md`,
`corpus/`). Source of the lab side: this repository at `b9fca536`.

## 1. What eval-lab actually is

Harbor owns one loop: **task package -> sandboxed trial -> verifier reward +
artifacts + ATIF trajectory**. Everything downstream of `result.json` is
deliberately left open, and that is the space eval-lab occupies.

Eval-lab is not an eval harness. It is an **admission-control and evidence
plane** wrapped around Harbor:

- **Execution control** — `runner.py`, `queue.py` (`DirectoryQueue` state
  machine), `campaigns.py` (`CampaignOrchestrator` + `PolicyGate` spend/quota
  gates), `execution_contracts.py:636` (`build_command`, the single canonical
  Harbor argv).
- **Evidence plane** — CAS (`evidence_store.py`), Postgres catalog, Parquet/
  DuckDB projections (`evidence/facts.py`, `evidence/event_mart.py`), a unified
  attach surface, and fail-closed admissibility ledgers.
- **Interpretation** — `trajectory_ir.py` (lossless ATIF IR),
  `interpretation/capability_deficits.py`, `interpretation/feature_registry.py`,
  `semantic_facts.py`.
- **Statistics and gating** — `cohort.py` (paired pass@k power), `analysis_statistics.py`
  (Wilson, Fisher exact), `sft_signal.py` (preregistered paired held-out gate).

That combination genuinely does not exist in the ecosystem. The public tooling
is either a viewer (`harbor view`, `vsc-atifviz`), a span exporter
(`harbor-atif2otel`, Phoenix), a converter (`atifact`, ADP), or a labeler
(AgentRx) — none of them carry provenance, admissibility, or a reward-integrity
contract. **Keep building here.** The corpus explicitly rejects rebuilding
viewers and SaaS trace boards as weightless duplication
(`OPERATIONS.md` OPS §4).

## 2. Where it is going

The frontier loop, per `PATHS-FORWARD.md`, is
**synthesise -> verify -> roll out -> filter -> train -> re-evaluate**, with Harbor as
the contract at every seam. Eval-lab owns *verify / filter / admit* and has real
depth there. Its declared direction (trajectory-to-training, `sft_signal.py`,
`training_export.py`, `trainer_bundle.py`) is the correct one.

The structural problem is that the plane is **over-built relative to its inputs**.
Three deficits, in order of how expensive they are to fix later.

## 3. Deficit 1 — capture fidelity is lossy, and the loss is irreversible

`trajectory_ir.py:51-55` declares `prompt_token_ids`, `completion_token_ids`,
and `logprobs`, and `trajectory_ir.py:329-333` stores logprobs into CAS when
present. The storage is built. The **capture is not requested**: nothing in
`execution_contracts.py:636-681` passes `--agent-kwarg collect_rollout_details=True`,
and no lane uses `terminus-2`, the only agent documented to emit rollout details
(`corpus/sources/harbor-docs-terminus-2.md`, `harbor-docs-training-rl.md`).

Confirmed empirically on a real paid trial:
`runs/tb21-codex-terra-slice/2026-09-06__18-48-38/regex-log__yzqDHH2/result.json`
has `agent_result.rollout_details = None`.

Consequence: **every trial run so far is permanently RL-unusable.** Logprobs and
token IDs cannot be backfilled from a finished container. The `logprobs` columns
are structurally null, so any GRPO/TRL path (`OPERATIONS.md` OPS §5,
`trl.experimental.harbor`) starts from zero data no matter how good the
admission machinery is.

What remains missing is only the *capture kwarg*. The neighbouring gap in this
same family has since closed: PR #383 added `--skill`, `--load-trajectory`, and
`--export-traces` passthrough to `build_command`
(`execution_contracts.py:216-217,701-705`), so Harbor's continuation surface is
now reachable and the with-context / without-context arm that makes
`analysis_capability.py`'s conditional-recovery and cascade metrics causal can
be dispatched. `collect_rollout_details` is still absent from the same builder,
so the irreversible loss above continues on every new run.

## 4. Deficit 2 — the zero-cost verifier loop is documented but unimplemented

Every lab generator already emits `environment_mode = "separate"`
(`synthetic_funcdag.py:845`, `synthetic_tool_memory.py:1868`, `seqgen.py:1074`,
`operational_restraint.py:441`, `synthetic_transform.py:128`), and
`task_workbench.py:3598` *rejects* any task that does not
(`verifier_not_isolated`). That is exactly the precondition for
`harbor trial regrade`, which re-scores a recorded trial's artifacts against a
new verifier in ~5s at zero model cost.

The repository already knows this. `docs/research/evaluation-factory-2026-08.md:60`
lists regrade as reuse-as-substrate, `:232` names "Harbor regrade for verifier
re-runs" as the plan, and `docs/architecture.md:144-146` already handles
Harbor's `.sources` regrade cache in job discovery. There is **no
implementation** — `grep -rn regrade src/` at `b9fca536` returns only that
discovery comment (`results.py:284-286`).

Cost of the gap: verifier hardening currently requires re-running billable
trials. 102 separate-mode trials are sitting in `runs/` right now, including
paid `codex` and `antigravity-cli` runs with multi-dimensional rewards
(`correctness`, `input_preservation`, `output_hygiene`). Each is a free
re-scoring opportunity that the lab cannot take.

A second, subtler capability falls out of the same mechanism: regrading a trial
with an **unchanged** verifier is a free **grader-determinism probe**. Verifier
nondeterminism is among the most damaging task defects (it silently poisons
every downstream contrast), and nobody detects it because re-running is
expensive. At ~5s per probe it becomes routine. This is the RIVER
verifier-defect programme (`papers/generalizable-terminal-behaviors.pdf` Sec.3.2)
made cheap.

## 5. Deficit 3 — reward is outcome-shaped while analysis is process-shaped

Eval-lab mines process facts richly *after* the fact (tool calls, loops, step
timing, semantic actions). But the reward its tasks emit is end-state only:
`harbor-rewardkit` is not a dependency (`pyproject.toml`), so
`trajectory_tool_used` / `trajectory_tool_not_used` / `trajectory_turn_count`
(`corpus/sources/harbor-docs-FULL-llms-full.md:7785-7801`) are unused. Process
signal can therefore never become *reward*, only commentary — which blocks both
path-graded gates and any RL objective that should care about tool discipline.

On the temporal side the lab is further ahead than the literature: it already
has `t_err` and `t_lock` **with right-censoring** (`analysis_capability.py:240-258`,
`cascade_distance = lock_step - first_error_step` at `:722`;
`trajectory_compliance.py:67-74` carries lock predicates and censor steps).
Failure-as-a-Process (`papers/failure-as-process.pdf` Sec.II-E) adds one thing
the lab does not have: **`t_obs`, first *observable* failure**, hence the
observability lag `t_obs - t_lock` and the early-prefix monitor. That is the
missing lever for early-stopping hopeless trajectories, and it is pure CPU over
data already in hand.

## 6. Deficit 4 — supply

The reported Gate Zero state is 0/164 strictly eligible rows, zero craft x facts
digest overlap, degenerate Arm C/D. The admission plane has almost nothing to
admit because task supply is self-generated only (`synthetic_funcdag`,
`seqgen`, `synthetic_transform`).

The ecosystem has ready supply that runs on this Mac today
(`PLUG-PLAY-PALETTE.md` P0/P1, `NATIVE-CATALOG.md`):
`openthoughts-tblite` (100 calibrated tasks, r=0.911 against TB 2.0 at 2.6-8x
speed — a *free operational upgrade*: stop iterating on the expensive suite),
`data_agent_harbor_eval` (144 tasks, deterministic graders, no judge variance),
FACET-Terminal 6,020, RST 37,484, plus Repo2RLEnv as a repo->task emitter.
Note the external-pack caveat this repo will hit immediately: terminal-bench 2.1
tasks record `verifier_environment_mode = 'shared'`
(`runs/tb21-codex-terra-slice/.../regex-log__yzqDHH2/result.json`), so imported
packs are **not** regradable and not interchangeable with lab-generated ones.

## 7. What to build, ordered

| # | Capability | Cost | Why now |
|---|---|---|---|
| 1 | **`evallab regrade`** — typed regrade receipts binding source-trajectory digest x verifier digest -> reward, with a grader-determinism verdict | $0, ~5s/trial | Precondition already universally satisfied; 102 local trials waiting; documented intent with no implementation. **Landed: `src/evallab/regrade.py`, PR #384** |
| 2 | **Rollout-detail capture** — `collect_rollout_details` + a `terminus-2` lane in `build_command` | one config change | Stops permanent, unrecoverable data loss on every future run |
| 3 | ~~**`--load-trajectory` continuation arm**~~ | — | **Landed in PR #383** (`execution_contracts.py:701-705`); the causal with/without-context arm is now dispatchable |
| 4 | **`t_obs` + prefix monitor** | CPU only | Completes the temporal triple; enables early-stop token savings |
| 5 | **Path-graded verifiers** (`harbor-rewardkit` in generated tasks) | one dependency | Lets process facts become reward instead of commentary |
| 6 | **Ecosystem supply intake** through the existing admission gates | downloads | Unblocks Gate Zero; `openthoughts-tblite` also replaces the expensive dev loop |

Explicitly **not** to build (corpus-rejected as duplication or infeasible):
trajectory viewers and exporters, SaaS trace boards, distributed GPU RL stacks,
local GPU training. Borrow their reward-integrity rules
(`OPERATIONS.md` OPS §5) and skip the clusters.

## 8. The one-sentence read

Eval-lab has built the most rigorous *admission and evidence* layer in the
Harbor ecosystem and then starved it: it captured less than Harbor offers
(no rollout details, and until #383 no continuation), it graded less than Harbor
offers (until #384 no regrade, still no path-graded criteria), and it admits from
a supply of one generator. The cheapest repairs are all config-sized; the first
of them — regrade — was free and immediately exercisable on trials already on
disk, and the standing one — `collect_rollout_details` — is losing data on every
run it is missing from.
