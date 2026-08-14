# 09 — Judge calibration, then DSPy experiment 1

No judged dimension is reportable until the exact judge/rubric/corpus tuple has a
measured calibration record. The policy floor is mean exact agreement `>= 0.9`.
A stub record proves plumbing but is never reportable and never satisfies the gate.

## Calibration contract

`harbor-lab calibrate <family>` consumes the manifest-ordered documents in
`research/calibration/<family>/corpus.json`. The judge receives the documents,
the named rubric criteria, and family reference facts. It never receives manifest
variant labels, `answer-keys/`, document sources, or trajectory labels.

The prediction artifact preserves Reward Kit's raw, pre-inversion binary answer:
`yes` means the named behavior is present. For a negated criterion such as
`invents_evidence`, the answer is not inverted before comparison. After all model
predictions exist, the host process opens the sealed keys and computes exact-match
agreement independently for every criterion.

The pydantic `JudgeCalibrationRecord` contains:

- judge backend, model, and engine version;
- SHA-256 digests of the canonical rubric and manifest-ordered corpus bytes;
- exact `agreements`, `total`, and `rate` for every criterion;
- weighted mean agreement, the `0.9` floor, and whether it is met;
- date, document count, prediction-artifact path, and pending backends;
- `status=measured|stub` and `reportable`, where only `measured` is reportable.

Records are append-only JSON under `research/calibration/records/<family>/`.
Measured records are also upserted into the rebuildable `judge_calibrations`
catalog table. The gate query is deliberately simple:

```sql
SELECT reportable AND meets_floor
FROM judge_calibrations
WHERE family = $1
ORDER BY evaluated_on DESC, created_at DESC
LIMIT 1;
```

The table is created idempotently by the calibration writer because JUDGE does
not own `sql/schema.sql`. A BUILDER integration can later move the identical DDL
into the canonical schema without changing the record contract.

## Safe CLI sequence

First prove the complete corpus/keys/agreement path without a model or catalog
write:

```bash
uv run harbor-lab calibrate checkout-pool-exhaustion \
  --stub --skip-catalog --date 2026-08-14
```

Stage the Codex judge task and a policy-valid queue spec. Staging does not invoke
a model:

```bash
uv run harbor-lab calibrate checkout-pool-exhaustion \
  --stage codex --date 2026-08-14 --est-cost-usd 2.75
uv run harbor-lab submit \
  queue/calibration-specs/judge-checkout-codex-20260814.json
uv run harbor-lab calibrate checkout-pool-exhaustion \
  --dispatch-approved <spec-id>
```

The dispatch command is a narrow credential fallback for this bootstrap only. It
requires exactly one approved spec, verifies that it is the requested Codex
calibration, reruns standing-policy admission, and checks Codex auth, Docker,
Postgres, and disk headroom. It deliberately does not require the unrelated Claude
credential. The actual model call still originates from the previously submitted
queue record.

After the queued Harbor job completes, point calibration at the immutable
`judgments.json` artifact. Supply the concrete model reported by the Harbor trial
when the staged task used the agent's default model:

```bash
uv run harbor-lab calibrate checkout-pool-exhaustion \
  --predictions runs/<job>/<trial>/artifacts/output/judgments.json \
  --judge-model <resolved-model> \
  --pending-backend rewardkit-anthropic:credential-unavailable
```

Every billable call is started only after the corresponding spec has passed
`harbor-lab submit`; each staged estimate is `$2.75`, below the `$3` job ceiling.
The generated task verifier checks only prediction completeness and shape. It
contains no answer key and therefore cannot turn calibration labels into agent
context.

## Credential fallback

Reward Kit 0.1.7 supports both provider judges and CLI agent judges. Its binary
criteria normalize yes/true/1 to `1.0`, retain raw answers in
`reward-details.json`, and invert `negate=true` only after judging. Agent mode
shells out to `codex` or `claude-code`.

For Anthropic subscription auth, Reward Kit reads `CLAUDE_CODE_OAUTH_TOKEN` and
`REWARDKIT_FORCE_OAUTH=1` forces the token ahead of an API key. The lab reads the
token from the `harbor-practice-claude-oauth` Keychain item only into the child
process environment; neither the value nor command output is logged. When that
item is absent or locked, the Anthropic spec stays staged and the record lists it
as pending.

Codex fallback uses the existing `~/.codex/auth.json` through Harbor's `codex`
agent. This live path is recorded honestly as `harbor-codex-agent`, not as a
Reward Kit execution. It uses the same raw-verdict contract, so its measured
agreement is comparable to the future Reward Kit Anthropic record.

The staged specifications are versioned at:

- `research/calibration/records/queue-specs/checkout-codex-20260814.json`
- `research/calibration/records/queue-specs/checkout-anthropic-20260814.json`
- `research/calibration/records/queue-specs/checkout-dspy-miprov2-20260814.json`

## DSPy experiment 1

`build_dspy_program()` represents the complete checkout rubric as a DSPy
signature with inputs `family`, `rubric_json`, and `document`, and structured JSON
verdicts as output. `dspy_metric()` is exact per-criterion agreement.

The 22 documents split deterministically into:

- 12 optimizer training examples;
- 4 optimizer validation examples;
- 6 sealed held-out controls covering empty, correct, subtly wrong cause,
  useless actions, fabricated evidence, and fluent-only output.

Only the first two sets are passed to `MIPROv2.compile(trainset=..., valset=...)`.
The held-out tuple is returned only after compilation and an overlap assertion
guards the boundary. The optimizer run is successful only if its held-out mean is
higher than the handwritten baseline at equal-or-lower judge cost. The committed
DSPy queue spec remains unsubmitted until a measured baseline record exists and
the optional `dspy[optuna]` runtime is supplied by the queue worker.

Dry-run the sealed split without a model:

```bash
uv run harbor-lab calibrate checkout-pool-exhaustion --dspy-dry-run
```

Because JUDGE cannot edit `pyproject.toml`, production DSPy remains an optional
import. Development verification uses an ephemeral `uv run --with dspy` runtime;
normal `harbor-lab` commands do not acquire or import DSPy.

## Acceptance

- Both corpus families complete end to end with the deterministic stub and emit
  non-reportable records outside the tracked records tree.
- A real Codex queue run produces at least one measured calibration record.
- The absent/locked Claude token produces no Anthropic call and is named as
  pending in the record and handoff.
- The DSPy program instantiates with a stub LM, and a spy optimizer proves no
  held-out document id reaches `compile()`.
- `uv run pytest -q` and `uv run ruff check .` pass before the PR.
