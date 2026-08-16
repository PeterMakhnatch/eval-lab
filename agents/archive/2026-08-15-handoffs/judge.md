Status: ready for PR; merge gate blocked by the current main-branch lint baseline
Last: Rebase was conflict-free; 36 tests and owned-path Ruff pass after the measured record.
Next: Push/open the JUDGE PR, then rebase when the upstream green-CI repair reaches main.
Blockers: Claude credential absent; Codex misses 0.90; origin/main has 9 non-JUDGE Ruff errors.

# JUDGE handoff

Worktree: `.worktrees/judge` on `role/judge`.

Implemented so far:

- `evallab calibrate <family>` modes for deterministic stub, queued judge
  staging, prediction collection, catalog persistence, and DSPy split audit.
- Raw Reward Kit pre-inversion verdict semantics and exact per-criterion agreement.
- Runtime-generated Harbor judge tasks keep all answer keys out of the agent and
  verifier environments.
- Stub results: checkout mean `0.5552`; retry mean `0.5682`; both correctly marked
  non-reportable.
- Checkout DSPy split: 12 train, 4 optimizer-validation, 6 sealed holdouts; overlap
  assertion is active.

One billable call was made, only from a submitted queue spec and under the `$3`
job cap. Runtime-only staged tasks/specs live under the ignored `queue/` tree in
this worktree. EVIDENCE's corpus and answer keys are unchanged.

The first Codex spec was approved under `researcher-followups`, capped at `$2.75`.
The narrow fallback dispatcher rechecks the policy plus Codex auth, Docker,
Postgres, and disk, and refuses to run if any other spec is approved. Its readiness
report was healthy; it does not treat the missing Claude credential as a Codex
prerequisite.

Dispatch `01KZZBQNGWMV3AZ1HWMC5GHM4E` failed before a model invocation with
`ValueError: Model name is required`. The immutable trial reports null input,
output, and cost fields. The local Codex config explicitly selects
`gpt-5.6-sol`; staging now refuses Codex specs without an explicit model.

The explicit-model replacement reached the Codex CLI but Harbor's default adapter
path generated an empty `OPENAI_API_KEY`, producing 401 before inference; cost and
token fields are again null. Installed Harbor 0.21 supports
`CODEX_FORCE_AUTH_JSON=1`; the narrow dispatcher now sets it only around `tick`,
causing Harbor itself to upload `~/.codex/auth.json` without exposing its contents.

The auth.json-backed replacement spec `01KZZC6XNT7KKJJEAKACWEY3HN` completed one
trial with no agent exception for `$1.111781`. Its artifact covers all 22 checkout
documents. The task verifier rejected the artifact only because the model sorted
JSON object keys; the verifier generator now compares criterion membership rather
than semantically irrelevant object order. The host validator accepted the same
unchanged artifact and opened the sealed keys only after prediction completion.

Measured record:

- id: `checkout-pool-exhaustion-20260814-gpt-5-6-sol-bbfcdacbd3`
- backend/model: `harbor-codex-agent` / `gpt-5.6-sol`
- mean exact agreement: `0.762987` across 308 criterion decisions
- gate: failed (`0.762987 < 0.90`), so this tuple is not calibrated for judging
- weakest criteria: `invents_evidence=0.1818`,
  `asserts_unsupported_cause=0.3182`, `proposes_unsupported_work=0.3636`
- persistence: append-only JSON plus a verified matching `judge_calibrations` row
- pending: `rewardkit-anthropic:credential-unavailable`

Verification checkpoint:

- `uv run pytest -q`: 36 passed.
- Owned paths Ruff clean.
- `uv run ruff check .`: blocked only by nine existing violations in
  `library/curated/_emit_card.py` and `research/explorations/harbor-021/demos/`,
  which JUDGE does not own.
- Ephemeral DSPy 3.2.1 `DummyLM`: metric 1.0; spy optimizer saw 16 examples and
  zero of six held-out controls.
- First nop smoke found a missing separate-verifier Dockerfile; generator fixed.
  Second smoke completed one trial with zero exceptions and reward 0 as expected
  for nop. Post-run ingestion still fails in BUILDER-owned catalog DDL with
  `psycopg.errors.InvalidTableDefinition: cannot drop columns from view`.
- The corrected generated verifier accepts the unchanged live prediction artifact
  as structurally complete.
- The DSPy MIPROv2 queue spec remains staged and unsubmitted. The measured baseline
  now exists, but the optimizer must not be treated as calibrated against a baseline
  judge that misses the policy floor; its six held-out controls remain sealed.
