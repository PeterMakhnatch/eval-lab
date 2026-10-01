# HAR-110 results: GEPA on the MiMo-V2.6-Distill-Qwen-9B addendum, split v2 (first look)

Everything here is n=1 per task and arm, so read it as a first look, not evidence of an effect.
Per-run rows, including tokens and stop reasons, are in `results-v2-trials.jsonl`.
Those token columns are native Harbor `agent_result` totals, not settled
proxy-ledger usage; the two can differ. HAR-132 replayed all 36 declared
comparison cells from raw records with no reward, score, fetch, native-token,
or stop-reason differences.

**Score column.** The GEPA score is the verifier reward, except that a run which fetched upstream code (`upstream_fetch_zero`) scores 0.
The "fetch" column is reported separately from reward, as Research-Harbor asked.

## Arms

- **plain**: the Terminus-2 prompt with no addendum. Dev rows are HAR-104's retained runs on the same route and harness tree, re-scored at $0. Held-out rows are fresh (HAR-110 round v2-r2).
- **seed**: the HAR-85 addendum, `sha256:399ec113…`.
- **candidate**: `sha256:cfe31418…`, committed byte-for-byte as `candidates/gepa-cfe31418.txt`. It is GEPA's first reflection on the v2 dev seed runs (GLM-5.3 proposer, $0.074). Research-Harbor approved it at 2026-09-30T08:20Z.
  - Dev 001896 ran through GEPA's own parked spec.
  - The other dev tasks used identical-route specs built outside GEPA, because GEPA parks one evaluation per Modal deploy.

## Development (6)

| task | plain | seed | candidate |
|---|---|---|---|
| 000383 | 0 | 0 | 0 |
| 001832 | 0 | 0 (verifier 1.0, pip-download) | 0 |
| 001896 | 0 | 0 | 0 |
| 002256 | 0 | 0 | None: infra, proxy 502 "unsupported upstream encoding" after 251k tokens |
| 002391 | 1 | 0 | 0 (verifier scored; lab marked the spec `proxy_usage_unreconciled`) |
| 002864 | 1 | 0 | **1**, agent finished at 74k input tokens |
| **total** | **2/6** | **0/6** | **1/5** (+1 unscored) |

## Held-out (4)

| task | plain | seed | candidate |
|---|---|---|---|
| 000495 | 0 | 0 | 0 |
| 000587 | 0 | 0 | 0 |
| 001161 | 0 (pip-download) | 0 | 0 |
| 001181 | 0 (pip-download) | 0 | 0 |
| **total** | **0/4** | **0/4** | **0/4** |

## Reading

- **Held-out has no signal.** All 12 runs scored 0, and all 12 ended `TrialBudgetExhaustedError`, the per-trial ceiling of 120 requests / 2.5M input tokens. The seed-vs-candidate held-out comparison is 0/4 vs 0/4, so it says nothing either way.
- **Upstream fetches.** The candidate's "stay offline" rule held in its runs: 0 of 9 scored candidate runs fetched upstream code. In the other arms:
  - plain: 2/4 held-out runs fetched;
  - seed: 1/6 v2 dev runs fetched (001832, which would otherwise have scored 1.0).
- **The candidate's one pass was also its only early finish.** On 002864 the candidate stopped on its own at 74k input tokens with reward 1. The seed also stopped on its own (at 268k) but scored 0, and plain passed only after running to the ceiling. A single run cannot tell a behaviour change from luck.
- **The plain prompt remains the strongest dev arm (2/6).** The addendum seed beats it nowhere. No arm solves a held-out task within the ceiling.

## Spend

- **Modal:** 2026-09-30 billed $7.22 (`evallab modal billing-reconcile --for 2026-09-30`). HAR-104 used $2.121 of that, so **HAR-110 ≈ $5.10 of the $6 cap**. The split by round is approximate; the bill is per app:
  - aborted v1 round $0.66;
  - v1 seed baseline $1.52;
  - v2-r1 ≈ $1.2;
  - v2-r2 ≈ $1.7.
- **Reflection (Z.ai standard API, LiteLLM estimates):** ≈ $0.21 of $1.
  - $0.059 route precheck;
  - $0.072 for a proposal cut off at the old 8,192-token ceiling (retained, never proposed);
  - $0.074 for candidate cfe31418;
  - ≈ $0.001 of one-line probes.
  - Also, 4 requests went to the expired Coding Plan and were rejected with 429, so $0 is expected.
- **Daytona:** Not measured in the original receipt because the SDK was absent (`modal-teardown.json`: `daytona-sdk-not-installed`). The later [HAR-122 backfill](../har122-spend-day/README.md) attributes **$0.6248** to HAR-110 on September 30 using trial wall times and a list-price rate card. That is an offline estimate, not a provider bill.

## What DSPy would add

`dspy.GEPA` is built on the same GEPA engine. Beyond what `python -m evallab.gepa_optimizer` already runs (pinned gepa 0.1.4), it adds:

- **Multi-predictor programs.** Each predictor gets its own trace-level feedback (`pred_name` / `pred_trace`), with a component selector to choose which predictor to mutate and optional merge of Pareto-frontier programs.
- **Budget presets and resume.** Auto light/medium/heavy budgets, run statistics, and `log_dir` resume.

None of that applies here. The candidate is a single instruction addendum for one Terminus-2 agent, so there is only one "predictor". Using DSPy would mean wrapping the Harbor trial as a single-predictor `dspy.Module` and losing the lab's own machinery:

- per-spec approval, `AggregateBudget` ceilings and the review gate;
- journalled replay-safe proposer receipts;
- the upstream-fetch score rule.

DSPy also offers no reflection-cost cap. The limits in this run were the per-trial ceiling and the Modal budget, and a different optimiser would relax neither [inference]. The source for the DSPy API is https://dspy.ai/current/api/optimizers/GEPA/overview/.
