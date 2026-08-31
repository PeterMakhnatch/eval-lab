---
status: proposed
reviewed: 2026-08-31
audience:
  - operator
  - platform
  - analyst
---

# Eval Lab next buildout report

## Executive decision

The next **large buildout** should be an **Analysis Control Plane**, not another feature batch and not a broad benchmark expansion.

Before that large build starts, land one bounded runtime-readiness slice so every paid model lane fails or passes before a benchmark trial exists.

Recommended sequence:

1. finish the low-risk correctness fixes in this report;
2. build `evallab agents list/doctor/smoke/qualify`;
3. build outcome authority, regrade ingestion and composite validity;
4. materialize the operator analysis views;
5. run one cheap corpus per research theme;
6. refine/prune features from repeated usefulness evidence.

## Current baseline

Eval Lab now has:

- Harbor and revision-safe Inspect source ingestion;
- exact multi-runner parity bindings;
- 74 autonomous-research features with source, artifact and scale provenance;
- two RSI calibration runs with sealed verifier evidence;
- a corrected Antigravity/Gemini 3.7 Flash High Harbor smoke with ATIF capture;
- a three-theme benchmark programme;
- 240 registered trajectory features, of which 109 are currently predictor-eligible.

The principal debt is no longer raw capture. It is deciding which facts are authoritative, which analyses are admissible and which operator conclusions are safe.

## Small fixes: immediate queue

These are bounded changes with local behavioral acceptance criteria.

| Order | Fix | Why now | Acceptance |
|---:|---|---|---|
| 1 | Require at least two selection decisions before emitting `optimal_selection_flag` or `final_selection_regret` | BBO's single-candidate regret was `0` by construction | One selected candidate retains `final_visible_score` but emits null regret/optimal flag |
| 2 | Clarify `--allow-billable` UX | The flag records spend consent but cannot bypass the standing-policy queue | CLI help and refusal explicitly state the distinction |
| 3 | Backfill BBO findings and validation coverage | BBO and Game2048 used the same schema but had asymmetric findings depth | BBO carries composite-outcome, log-conformance, validation and anytime/final findings |
| 4 | Reconcile the 74-feature inventory | `environment_setup_seconds` was undocumented | Declared and actual inventory counts both equal 74 |
| 5 | Surface the existing predictor audit | The registry already computes 109 eligible / 131 refused, but the operator cannot see it | CLI/view prints the refusal mix without a custom script |
| 6 | Mark container credential transport separately from host login | Cursor currently appears credential-ready while Harbor requires an API key | Readiness output cannot label host-only Cursor as Harbor-ready |

Items 1–4 are applied in the report PR. Items 5–6 are the next bounded implementation slice.

Items 1–4 are correctness/documentation fixes. Items 5–6 are the first slice of the runtime and analysis control planes.

## Large buildout 0: Agent Readiness Control Plane

This is the prerequisite for reliable model swapping. It is not a provider-specific adapter rewrite.

### Operator surface

```text
evallab agents list
evallab agents doctor PROFILE
evallab agents smoke PROFILE --task canary/event-summary
evallab agents qualify PROFILE --repeats 3
```

### Data contract

One row per exact profile/model pin:

- declared adapter and model;
- host credential state;
- credential-transport state;
- environment/network compatibility;
- trajectory-capture capability;
- last smoke and canary evidence;
- blocking reason;
- qualification state and evidence digest.

### Acceptance

- Gemini 3.7 Flash High reports ready and reproduces the passing smoke.
- Cursor reports host-ready but Harbor-blocked until a supported credential transport exists.
- Claude and DeepSeek name their exact missing credential prerequisite.
- A blocked profile stops before a Harbor trial and cannot produce reward `0`.
- Full paid campaigns still use the standing-policy queue; `agents smoke` is the only bounded direct paid path.

## Large buildout 1: Analysis Control Plane

This is the primary buildout.

### 1. Outcome authority and regrade lineage

Create source-native facts for:

- original trial outcome;
- verifier result or absence;
- preserved artifact;
- verifier-only regrade;
- supersession relationship;
- authoritative outcome pointer;
- authority reason and source digests.

A standalone `harbor trial regrade` directory must become ingestible evidence rather than a manually maintained JSON exception.

**Acceptance:** Game2048 resolves to reward `0.37800819`; the original synthetic `0.0` remains visible but cannot enter a reward aggregation.

### 2. Composite outcome validity

Do not create a universal status. Resolve a vector:

```text
(agent_axis, verifier_axis, artifact_axis, authority_axis, admissibility)
```

**Acceptance:** BBO is agent-timed-out + verifier-valid + artifact-valid. Game2048 is agent-timed-out + original-verifier-invalid + regrade-valid. Both remain distinguishable.

### 3. Headline and scale bindings

Every analysis declares:

- selected visible scalar;
- selected artifact;
- metric direction;
- task/verifier/metric/outcome binding digests;
- whether arithmetic across axes is permitted.

**Acceptance:** BBO explicitly binds transfer to `selected`, not implicitly to `final`; Game2048 normalized transfer remains null.

### 4. Selection reconstructibility

Reconcile:

- experiment log;
- artifact versions;
- selected version;
- submitted bytes;
- visible validation coverage;
- sealed replay coverage.

**Acceptance:** selected-unlogged BBO v1 and Game2048 v5 refuse method-improvement claims until their histories are repaired or explicitly waived with evidence.

## Operator views

Build these as the stable analysis API:

| View | Purpose |
|---|---|
| `v_agent_readiness` | Which profiles can run now, and why not? |
| `v_composite_outcome_validity` | Independent agent/verifier/artifact/authority axes |
| `v_reward_authority` | Authoritative and superseded outcomes |
| `v_headline_binding` | Declared scalar and alternatives |
| `v_scale_binding_status` | Permitted/refused arithmetic across outcome axes |
| `v_selection_reconstructibility` | Log/artifact/selection conformance |
| `v_feature_activation_map` | Populated, zero, null-with-denominator and dormant features |
| `v_predictor_eligibility` | Eligible predictors and refusal codes |

The first two views to materialize are `v_agent_readiness` and `v_composite_outcome_validity`.

## Feature-governance buildout

Current registry audit:

| Condition | Count |
|---|---:|
| Registered features | 240 |
| Eligible predictors | 109 |
| Predictor refusals | 131 |
| Missing temporal availability | 72 |
| Missing denominator applicability | 73 |
| Undeclared coupling reaching refusal | 3 |
| Reward-definition leakage refusals | 19 |
| Post-verdict refusals | 10 |

Work order:

1. declare temporal availability on the 72 unresolved features;
2. declare denominator applicability on 73 legacy features;
3. resolve the three coupling refusals;
4. namespace/reconcile overlapping refusal enums;
5. expose the audits through `v_predictor_eligibility`;
6. add a usefulness ledger: retain, retain-dormant, refine or prune.

Do not bulk-label fields from names alone. Each declaration must name the producer and evidence timing.

## Thematic experiment programme

After the analysis views work, run one cheap slice per theme.

### Theme 1 — Autonomous research and improvement

- Continue RSI calibration with one clean-completion task.
- Add RE-Bench import/limited execution for score-time curves.
- Use CORE-Bench or MLE-bench only to activate a specific dormant construct.

### Theme 2 — Stateful tool use and recovery

- Start with existing MCP recovery, tool composition and FuncDAG tasks.
- Compute exposure-conditioned recovery and failed-prefix cost.
- Integrate ToolSandbox only after milestone views exist.

### Theme 3 — Memory, context and continuity

- Run LoCoMo for trajectory volume.
- Build a T3 producer for write/read/use, context position and boundary events before making comparative claims.
- Add MemoryAgentBench after the producer is stable.

## PR sequence

| PR | Scope | Dependency |
|---:|---|---|
| A | Small correctness fixes and this report | — |
| B | `agents list/doctor/smoke/qualify` and readiness facts | A |
| C | Harbor regrade source facts, supersession and reward authority | A |
| D | Composite outcome resolver and first two views | B + C |
| E | Headline, scale and selection-conformance views | D |
| F | Temporal/denominator governance declarations and predictor view | D |
| G | One clean Theme-1 run plus cheap Theme-2/Theme-3 slices | E + F |
| H | Feature usefulness report and first retain/refine/prune decisions | G |

PRs B and C may run in parallel after A. D is the integration boundary.

## What not to build next

- no universal capability score;
- no generic cross-benchmark leaderboard;
- no new large feature family before the activation and eligibility views;
- no broad GAIA/OSWorld/SWE-bench expansion outside the three themes;
- no provider-specific model selector that bypasses the profile/readiness contract;
- no conversion of unavailable or invalid outcomes to zero.

## Reporting contract

Each buildout PR should ship a short evidence report containing:

- contracts added or changed;
- exact source/denominator/authority semantics;
- one positive and one refusal-path runtime example;
- affected operator views;
- feature activation changes;
- unresolved risks and the next dependency.

The next report after this one should accompany PR B/C and answer two questions with runtime evidence:

1. Can every declared model profile be classified before trial creation?
2. Can every original/regraded outcome be assigned one unambiguous authority state?
