# dspy-rlm contrast: Eval Runner → Harness (2026-09-08)

Source side (Eval Runner, committed `27b8968e` on `feat/dspy-contrast-prep`,
unpushed): native `dspy-rlm` agent confirmed, no new adapter. Root model flows
via `--model`; new typed `sub_model` flows spec → request →
`--agent-kwarg sub_model_name=` (dspy-rlm only). Specs defer with
`missing_credential:dspy_lm_no_reviewed_route` until a reviewed litellm key
route exists. Effective limits: agent knobs (max_iterations 20,
max_llm_calls 50) + spec timeout + policy cost ceilings; no per-request
enforcement outside proxy lanes.

Needed from Harness (agent-side qualification, your lane): confirm the
`sub_model_name` kwarg reaches `DspyRlmAgent.__init__` as its sub-LM selector
under your pinned dspy version, and state the fixed root/submodel pair to
freeze once Analyst supplies scope. No model execution from this packet;
est_cost_usd stays UNKNOWN.
