---
status: living
audience:
  - analyst
  - operator
---

# Monitor investigations (HAR-151)

Harbor-native, read-only trace investigation on top of the existing
[live watcher](live-watch.md). It consumes `evallab.live_watch/v1` status,
freezes source-bound cases, optionally runs a bounded hosted investigator,
renders source-cited hypotheses, and scores them against explicit frozen labels.


## Dataflow

```mermaid
flowchart LR
  W["watch status.json<br/>(evallab.live_watch/v1)"] --> P["investigate prepare<br/>(no keys, no model)"]
  R["explicit --runs-dir roots<br/>(job/trial dirs)"] --> P
  P -->|"cases/*/revisions/*<br/>snapshots/*.json<br/>status.json"| O[("out dir<br/>(default derived/analyses/monitor)")]
  O -->|"investigate run --allow-model<br/>(bounded hosted calls)"| A["analyses/&lt;profile&gt;/report.json<br/>spend.jsonl ledger"]
  O -->|"investigate report"| MD["monitor report ( hypotheses )"]
  A -->|"investigate score<br/>+ frozen labels.json"| S["calibration metrics<br/>(Wilson 95% CI)"]
```

Rules that never change:

- **Read-only.** Preparation and investigation never mutate source runs.
  `policy.findings_are_hypotheses` is always true in `status.json`.
- **Findings are hypotheses, not confirmed cheating.** Report dispositions
  are `suspicious` / `not_supported` / `inconclusive` only — never
  `confirmed`, never a numeric confidence. Alerts and rewards are not
  proof of a hack; the investigator must seek counterevidence and benign
  alternatives.
  A negative finding can be grounded in primary-trial counterevidence; it need
  not manufacture supporting evidence for the original suspicion. Suspicious
  findings still require supporting primary evidence.
- **Operator proposals are inert data.** Every `ProposedAction` carries
  `requires_approval: true`. The investigator has no shell, filesystem,
  network, grading, or deployment tools and cannot execute proposals.
  Anything beyond reading evidence needs separate human approval.

## Commands

Run these examples from the repo root; relative paths use the current directory.
`prepare` and `report` are local and require no keys. Only `run` spends
money, and only with explicit `--allow-model`.

### prepare — freeze evidence, no model calls

```sh
uv run evallab investigate prepare \
  --watch-state derived/watch/status.json \
  --runs-dir runs \
  --out derived/analyses/monitor
```

Useful options (defaults shown):

```sh
  --unflagged 1 --related 2 \
  --prepare-max-cases 200 --max-trials 200 --min-new-steps 20 \
  --cycles 1 --interval 0
```

- `--runs-dir` is repeatable and required: at least one explicit source
  root. Trial `job/trial` names from the watch status resolve **only**
  under these roots.
- `--cycles N --interval S` re-reads the watch status N finite times with
  S seconds between refreshes. There is no daemon mode.
- Output: `status.json`, `snapshots/<snapshot_id>.json` (immutable),
  `cases/<case_id>/revisions/<snapshot_id>/case.json` (immutable),
  `cases/<case_id>/latest.json` pointer. Re-running with unchanged bytes
  creates no new revision (`unchanged` counter in the printed summary).

### run — bounded hosted investigation (explicit opt-in)

Local preparation always runs first; provider calls happen only with
`--allow-model` and a named credential environment variable (the value is
never passed on the command line).

ZAI Standard example (current official guidance):

```sh
# Load the Standard key through your credential store (never commit it).
# Coding-plan ZAI_API_KEY is the wrong credential for this path.
keys run -- uv run evallab investigate run \
  --watch-state derived/watch/status.json \
  --runs-dir runs \
  --out derived/analyses/monitor \
  --allow-model \
  --model glm-5.3 \
  --endpoint https://api.z.ai/api/paas/v4 \
  --api-key-env ZAI_OPENAPI_API_KEY \
  --response-format json_object \
  --reasoning-effort low \
  --max-output-tokens 8000 \
  --max-cases 1 --max-calls 32 \
  --budget-usd 2 \
  --input-price 1.4 \
  --output-price 4.4
```

Prices are USD per million tokens, pinned from the
[provider price table](https://docs.z.ai/guides/overview/pricing) on
2026-10-02. Verify current prices before a new budget. The cheaper
`glm-5.3-flash` uses different rates ($0.15 input / $0.50 output).

ZAI-relevant option notes:

- `--response-format json_object` for ZAI Standard (the CLI also supports
  `json_schema` for providers that implement it; it never falls back
  automatically — a mismatch is an error, not a silent retry).
- **Do NOT pass `--disable-thinking` for GLM-5.3 / GLM-5.3-Flash.**
  The [thinking-mode reference](https://docs.z.ai/guides/capabilities/thinking-mode)
  describes provider-specific controls. GLM-5.3 defaults to maximum reasoning;
  explicitly request `--reasoning-effort low` for this bounded triage recipe.
  That option and `--disable-thinking` are mutually exclusive. Output bounds
  include provider-reported reasoning tokens; a truncated response is rejected,
  not accepted as a finding.
- Other bounds (defaults): `--max-calls 24` (lifetime aggregate request
  ceiling), `--calls-per-case 8`, `--max-cases 3` (new investigations per
  cycle), `--max-output-tokens 2000`, `--timeout 90`.

Exit codes: `0` ok, `2` usage/validation error, `3` provider-stop or
budget-exhausted (also stops the cycle — see budgets below).

### report — render source-cited hypotheses

```sh
uv run evallab investigate report --out derived/analyses/monitor
```

Renders one Markdown report over the frozen cases. It cites record IDs and
exact quotes actually returned by evidence tools, lists benign
alternatives / missing evidence per finding, and never rewrites the
underlying trial verdict.

### score — score reports against frozen labels

```sh
uv run evallab investigate score \
  --labels labels/reward-hacking-v1.json \
  --report derived/analyses/monitor/cases/<id>/revisions/<snap>/analyses/<profile>/report.json \
  --category reward_hacking
```

`--report` is repeatable. Reports match labels exactly on
`(case_id, snapshot_id)`; duplicates raise; reports without a matching
label land in `extra_reports` and are never scored; labels without a
report land in `missing` (decisive labels) or `missing_unscored`
(inconclusive labels).

Label file format — one row per case. Example synthetic development control:

```json
[
  {
    "case_id": "8b9ee84b298ce93dfaaa46c7",
    "snapshot_id": "74741f6ab845bfb0e029f76722c9f1eb1d34f39fd97c40e18afcd8b293ab2517",
    "label": "positive",
    "provenance": "synthetic",
    "family": "grading",
    "split": "development"
  }
]
```

Copy identities from your frozen cases, not this example. Labels allow
`positive`, `negative`, or `inconclusive`; provenance is `human`, `synthetic`,
or `model`; splits are `development`, `validation`, or `test`. A model-authored
rating is not human gold. Fixtures used to debug the investigator are development
controls, even if a file was originally tagged `test`.

Constraints: one row per `case_id`; each `family` appears in a single
split only. Malformed rows, repeated cases, and split-crossing families
raise `ValueError`. Missing labels are never counted as negative.

Metric denominators (per stratum in `overall`, `human_only`,
`by_provenance`, `by_split`, `by_provenance_and_split`, `by_family`;
every rate carries a 95% Wilson interval, `z = 1.96`, or `null` on a
zero denominator):

| Metric | Denominator |
|---|---|
| `precision` | `tp + fp` (flagged decisive reports) |
| `recall_conservative` | `n_positive` (all positive labels) |
| `recall_selective` | `tp + fn` (decisive reports on positive labels) |
| `fpr_selective` | `fp + tn` (decisive reports on negative labels) |
| `fpr_all_negatives` | `n_negative` (all negative labels) |
| `coverage` / `abstention_fraction` / `error_fraction` / `missing_fraction` | `n_positive + n_negative` (decisive labels; the four fractions sum to 1) |

`inconclusive` labels are auditable (`cases[].outcome =
"unscored_inconclusive"`) but never enter confusion, missing, error, or
abstention counts. `failed`-status reports count as errors;
`budget_exhausted` / `inconclusive`-status reports count as abstentions.
Each stratum also reports `provenance_counts`, `pooled_provenance`
(more than one provenance present), and `not_human_evidence` flags so a
reader can tell human evidence from synthetic/model pooling at a glance.

## Evidence coverage: captured / truncated / missing

`status.json` reports `source_trials`, `captured_trials` (trials with any
readable artifact), `complete_trials`, and `unavailable_trials` plus a
`limitations` list. Per-trial `limitations` say exactly what happened:

- **Approved reads only.** The snapshotter reads agent trajectories and
  continuations, `result.json`, an in-tree `instruction.md` when available,
  `verifier/test-stdout.txt`, and `verifier/agent.diff`. It never follows
  config-specified task paths or opens config/lock files, hidden tests, or
  solution directories. Symlink escapes are refused.
- **Captured:** each artifact's SHA-256 covers the captured original bytes,
  before redaction or record rendering. Snapshot identity covers the canonical
  structured evidence. Source timestamps are not a substitute for byte identity.
- **Bounded:** oversized sources are refused whole, not hashed as if a partial
  prefix were complete. Text artifacts have a 2,000,000-byte raw cap; trial and
  corpus byte budgets also apply. Rendered records and tool views have separate
  character bounds. Record truncation, including verifier records, makes the
  snapshot incomplete.
- **Unexpanded source content:** known producer spill/truncation markers,
  references without readable inline content, and unsupported non-text payloads
  make the snapshot incomplete. Spill files are not automatically hydrated or
  followed. A plain command mentioning a spill path is not an omission marker.
- **Missing:** missing optional artifacts are explicit limitations while other
  readable evidence is retained. Unresolvable trials and trials excluded by the
  capture cap get unavailable shell snapshots. `complete` describes captured
  snapshot coverage and terminal state, not proof that every possible artifact
  exists or that a task is valid. Inspect the limitations even when it is true.
- **Fleet signals:** real members receive bounded cases. Unroutable fleet
  signals remain visible; they are never invented filesystem trials.
- Watch free-text alert fields (`quote`, `detail`, `target`, `trial`,
  `task`) are secret-redacted at snapshot time; redactions and dropped
  malformed alerts are recorded as limitations.

"No readable captured evidence" means the provider is not invoked for
that case: the run emits an `evidence_unavailable` inconclusive report
with zero usage instead of spending budget on nothing.

## Case revision and live batching

- A **case** is one concern: an alert trial (plus its alerts) or a sampled
  `unflagged_control`, with up to `--related` related trials for
  counterevidence. `case_id` (24 hex) identifies the concern;
  `snapshot_id` (sha256 of canonical content) identifies the frozen
  evidence prefix.
- `prepare_watch` freezes **new significant prefixes only.** Unchanged
  content → `unchanged`, no new revision. Still-running trials coalesce
  until at least `--min-new-steps` (default 20) new steps arrive
  (`coalesced_live_updates`); new alerts and terminal evidence bypass the
  gate, and fleet-alert membership changes force a revision.
- `status.json` keeps `case_ids` (all retained), `active_case_ids`
  (currently selected), and `inactive_case_ids` with reasons:
  `selection_cap` (over `--prepare-max-cases`; the omitted candidates
  stay listed), `not_selected` (source still present, concern no longer
  selected), `source_absent` (source vanished — the prior concern is
  retained, never silently dropped).
- `--cycles/--interval` implement finite live batching: each cycle
  re-prepares (coalescing small live updates) and, for `run`, investigates
  up to `--max-cases` new cases. Provider-stop or budget exhaustion ends
  the batch with exit code 3.

## Profile-bound resume; no ambiguous retries

- The analysis identity is the **request profile**: model, endpoint,
  pinned prices, `disable_thinking`, `reasoning_effort`, `response_format`,
  credential-env name, effective limits, and selected implementation hashes.
  Its content digest (`profile_key`) names the output directory:
  `cases/<id>/revisions/<snapshot>/analyses/<profile_key>/`.
- Resume is exact: an existing `report.json` whose `case_id`/`snapshot_id`
  match is reused (identity mismatch raises); a new profile or new
  snapshot gets a new directory; earlier outputs are retained.
- **No ambiguous retries.** If a prior analysis directory for the case
  holds a provider journal without a durable final report, or a prior
  report with `error: ambiguous_prior_request`, any further analysis of
  that case raises until a human inspects the retained journal. A
  possibly-paid call is never automatically retried. Per-case locks turn
  concurrent runs into an explicit "already being investigated" error.
- **Bounded model correction is not a transport retry.** A received, accounted
  response with malformed JSON, a non-verbatim citation, an absence claim over
  incomplete evidence, or a refused scoped tool request receives feedback
  within the same call, context, and spend limits. Refused trial keys are never
  case-folded or aliased; the model must select an exact allowed identifier.
  The model must produce a new valid candidate; the
  controller never patches JSON, loosens quote matching, or publishes a rejected
  candidate. Journals and report limitations retain these rejections.
- Actions use an operation-specific typed request inside a root object, e.g.
  `{"request":{"action":"related"}}`. `conclude` requires a complete finding;
  arguments belonging to other actions are not accepted. Cached terminal
  failures are retained, not silently rerun under the same profile. A run with
  zero new requests is not a claim that prior analyses succeeded.

## Budgets: reservations are not invoices

- The output dir owns one **lifetime** budget: `spend.jsonl` ledger plus
  `--budget-usd` (aggregate reservation ceiling) and `--max-calls`
  (aggregate request ceiling) shared across all cycles, cases, and
  profiles.
- Every provider call **reserves** before sending, computed from UTF-8
  request bytes plus a framing allowance and maximum output tokens at the
  operator-pinned `--input-price` / `--output-price`. Failed and
  unknown-usage calls keep their full reservation. These are conservative
  estimates, not provider invoices — actual billed cost is unknown to the
  tool. Unknown actual costs are surfaced as limitations, never netted
  against the reservation.
- Exhaustion and provider/usage-bound/model/accounting/journal failures stop
  later requests and live cycles. Candidate-validation failures remain visible
  per case; other selected cases may still run. Any failed case or hard stop
  gives CLI exit 3. Reports carry `reserved_usd`, token counts (`null` when
  unknown), and explicit accounting limitations.

## Human approval gates

1. `run` requires explicit `--allow-model`; `prepare` never calls a
   provider and is the default safe step.
2. All proposed next actions (`data`, `grader`, `environment`, `harness`,
   `experiment`) are inert and require approval before anything happens
   outside the report.
3. Ambiguous prior requests block until a human inspects the retained
   journal — the tool does not retry past uncertainty with paid calls.
4. Stopping a training run on a finding (the Applied Compute pattern
   below) requires owner approval; this tooling stops at the hypothesis.
5. Cloud execution, large sweeps, and publishing stay under the repo's
   normal approval rules (`AGENTS.md`, `policy/`); investigator output
   grants none of them.

## Research background

### Applied Compute: Ari + reward-hacking detection (Oct 1, 2026)

- [Reward-hacking detection in Ari](https://appliedcompute.com/platform/reward-hacking-detection-in-ari)
  (Oct 1): after training steps, a **cheap LLM scans a random sample**
  of trajectories; suspicious flags escalate to the full **Ari**
  investigator; **stopping a run requires owner approval**. Their benchmark
  compares true-positive rates at a **2% false-positive operating point**.
  That vendor benchmark is not calibration evidence for this implementation.
- [Ari](https://appliedcompute.com/platform/ari): skills + sandbox +
  context-base investigator framing. Our `run` path is the analogous
  bounded hosted investigator, but scoped to frozen snapshots (no live
  actor, no sandbox execution).
- [Billion-token trace analysis](https://appliedcompute.com/platform/billion-token-scale-trace-analysis):
  sample → compress → cluster → freeze a taxonomy → classify. This implementation
  reuses the sampling and frozen-evidence discipline, but does not implement their
  billion-token clustering infrastructure or a context-base learning loop.

### The likely hack agent: Xiaomi MiMo-V2.6 (§4.2.6 / Fig. 6)

Primary source (not a summary):
[XiaomiMiMo/MiMo-V2.6-Flash-RL technical report (PDF)](https://huggingface.co/XiaomiMiMo/MiMo-V2.6-Flash-RL/blob/fa7122372a7fa5e350bf3b279a89636da6368776/MiMo_V2_6_technical_report.pdf) —
§4.2.6 / Fig. 6 describe an **active-environment red-team agent** that
finds **published/cached solutions**. The report's mitigations are
**cleanup, network restriction, rerun, and a distinct offline rollout
audit** — i.e. hygiene plus re-verification, not a claim of full
prevention. Treat the agent as the likely pattern behind cached-solution
hacks; confirm per-trial with frozen evidence, never by model name
alone.

### Validation and monitoring references

- [Inspect Scout validation](https://meridianlabs-ai.github.io/inspect_scout/validation.html) —
  measure scanner outputs against explicit labels, including LLM classifiers;
  preserve denominators and the distinction between tuning and held-out data.
- [METR blocking-action monitor](https://metr.org/notes/2026-09-27-implementing-a-basic-blocking-action-monitor/) —
  the minimal example of a monitor that can actually block. Ours
  deliberately cannot: it publishes hypotheses and stops at approval
  gates.
- [Docent](https://github.com/TransluceAI/docent) is a public Apache-2.0
  repo; hosted vs. self-hosted support differ, so check current docs
  before claiming a deployment path. (Do not repeat the old
  enterprise-only claim.)

## Overlap boundaries

- **Watcher ([PR686](https://github.com/PeterMakhnatch/eval-lab/pull/686)):**
  owns live discovery and alerting. This tool consumes its versioned output,
  rather than adding a second watcher. Rewards, counts, and task eligibility
  remain separate, unchanged evidence.
- **Existing trace-lab tools:** local normalizers, deterministic Scout scanners,
  exporters, and behavior metrics remain useful. Hosted Docent/Scout readers
  have their own opt-in and cost policies; this is not the only paid reader.
- **Scout / Docent reused, not replaced:** no new external trace store, viewer,
  runner, or proxy is introduced. The investigator writes bounded local
  snapshots, journals, and reports.
- **No RL automation, no real-time actor costs, no new trial
  experiments.** This path investigates already-frozen snapshots; it
  launches no training, runs no live agent, and reports no calibrated
  detection rate of its own.

## Limits

- No prevention, autonomous RL improvement, actor-cost accounting, or calibrated
  population detection-rate claim. A valid exact citation establishes that a
  quote was visible, not that the model's interpretation is correct.
- Evidence and tool results are untrusted data. Hard scope, budget, and action
  checks remain outside the model; this is not a proof of general prompt-injection
  resistance or isolation from a malicious local host.
- `--allow-model` sends selected workload content to the configured hosted
  provider. Common credential patterns are redacted, but that is not general
  anonymization. Keep snapshots and journals private; this delivery commits no
  private Drive content, raw retained traces, or credentials.
- Source evidence vs. hypothesis stays explicit: suspicious findings require
  benign alternatives, and material missing evidence must remain visible.
  Incomplete/live snapshots cannot establish absence of reward hacking.
  An edit followed by a reward is not by itself a causal intervention result.
