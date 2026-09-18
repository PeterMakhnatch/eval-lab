# Eval Runner — standing mission handoff (wK:p8)

Assignment: standing mission "Eval Runner — common execution path and unattended
approved jobs" per harbor corpus charter `OMP-LANE-LEADS-2026-09-07.md`
(2026-09-07, standing missions 2026-09-08). Prior receipt: M4 held-out freeze
(`trajectory-training-m4-results-20260904.md`, merged PR #371) — immutable,
superseded here, not edited.

As-of: 2026-09-08. Checkout: branch `feat/dispatch-queue-compiler`,
HEAD `884dbc17`. Working tree is dirty with others' uncommitted work
(AGENTS.md, cli.py, evidence/, sql/, tau scripts, …) — not mine, not touched.
Base note: merge `42b9e4c` (PR #383: --skill/--load-trajectory/--export-traces)
lives on `feat/lab-integration-*`, NOT in this ancestry. Mapped read-only
2026-09-08: 5 files (+229/-4); DTO fields `skill`/`skills`/`load_trajectory`/
`export_traces` + `resolved_skills` at `execution_contracts.py:214-243`,
flag emission at `:700-701`, 6 unit tests in `tests/test_runner.py:998+`;
plus committed `derived/harbor-packs/MANIFEST.md` pack pins. No follow-up
fixes on the lab-integration side. Rebase target is the integrator's call;
recorded below as waiting, not acted on.
## Ready (authorized, inputs present)

- R1: qualify syn-funcdag-easy oracle/nop + one pinned custom-harness config
  (codex/gpt-5.6-terra, validate-only, no execution) against the Lab path;
  bring the first failing requirement into the typed contract/adapter.
  DONE 2026-09-08: oracle 1 / nop 0 via `evallab run`
  (`runs/r1-oracle-funcdag-easy-20260908`, `runs/r1-nop-funcdag-easy-20260908`);
  PinnedCodex harness config validated with `--print-config` exit 0, no
  execution; no typed-contract repair needed.
- R2: native job → data capture proof: dispatch oracle spec for
  syn-funcdag-easy, tick, verify ingestion/catalog/projection. Free controls
  only; billable waiting specs untouched.
  DONE 2026-09-08: spec `01M2199Y5ZE41QNPVCS7TSKA3Y` went
  pending→approved→running→done in 1 tick;
  job `runs/r2-oracle-funcdag-easy-20260908`, reward 1;
  postgres trial record + 7 parquet tables incl. reward_facts row present.
  Capture status: full_success.
- R3 (read-only): map --skill/--load-trajectory/--export-traces surface on
  `feat/lab-integration-scan-text` via `git show` (no checkout switch) so the
  PR #383 reuse is exact, not from stale notes. DONE 2026-09-08: 5 files,
  DTO at `execution_contracts.py:214-243`, emission at `:700-701`, tests at
  `tests/test_runner.py:998+`; absent here; no follow-ups upstream.

## Waiting (needs another lane or Peter)

- W1: integration base decision (integrator): rebase target for
  `feat/dispatch-queue-compiler` to pick up `42b9e4c`. No reset of the
  primary checkout; no action until integrator answers in their handoff.
- W2: four codex specs in `queue/waiting/` (01M1W4WEEF…, 01M1W4WSTB…,
  01M1W4WSZD…, 01M1W4WSZY…) need Peter's per-spec approval. Untouched.
- W3: provider-route execution (Gemini/Meta/ZAI) needs real approvals;
  until then routes stay qualified-but-unexecuted with rejected states kept.

## Next (ordered, pull as capacity frees)

- N1: after R2 proof, exercise tick recovery (interrupted settlement,
  duplicate prevention) with control jobs only.
  DONE 2026-09-08: lifecycle works (~12s); SIGTERM reconciles via
  trial_wall_clock_timeout; duplicate resubmission accepted at submit, fails
  at execution. Gaps found: (a) submit-time dedup, (b) lease never released
  on reconcile-fail, (c) no signal trapping, (d) .lease.lock invisible to
  reconcile. Follow-on repair (a) DONE: `duplicate_terminal_spec` refusal in
  `submit()` with `policy_rejected` event + reason file; done/failed
  regression tests green; full `test_queue.py` green; committed `13ae0e39`
  on isolated `feat/queue-submit-dedup` (unpushed, unmerged — integrator owns
  merge order).
- N2: after W1 resolves, rebase dispatch branch and re-prove R2 on the new base.
- N3: agent-picked overnight sets via researcher pass (deferred; user picks work
  until then).

- B1: Harness wheel profiles → typed `AgentProfile` registration (three fixed
  entrypoints, model/limits bound, no arbitrary kwargs/env forwarding).
  No wheel install, no model execution (both need review/approval).
  Worker-owned slice; lead integrates serially.
  DONE 2026-09-08 (lead-reviewed): three profiles registered with contract
  wheel sha/model/limits, `verified_facts=()`, preflight fails closed
  pending credential review; 44 focused tests green, ruff clean; committed
  `241ddbad` on isolated `feat/harness-fixed-profiles` (unpushed, unmerged).
  Minor: unused `credential-route` literal + alias properties kept, noted.
- B2: Factory retained no-network package → oracle/nop through the Lab path,
  contrasted with Quality's working Compose route (`routes-proposal.json`).
  Correct refusal stands; shared-boundary defect gets fixed in an isolated
  worktree, other lanes' defects reported with file:line.
  DONE 2026-09-08 (lead-verified from raw result.json): retained package
  reproduces the correct Darwin no-network refusal pre-task (no reward);
  same content with task-authored Compose isolation runs oracle 1.0
  (job `12245d54`, 1 trial, 0 errors) and nop 0.0 (job `3063bfdc`) via
  `evallab run` in ~6s each. Verdict: Factory packaging defect
  (transformation.json:43-59, task.toml:28) — no task-authored Compose
  evidence lives in `/private/tmp/factory-runs/` (ephemeral, un-ingested).

## Contrast prep (dspy-rlm vs stock, 2026-09-08)

- DONE (lead-implemented, no execution): dspy-rlm route qualified through
  policy/queue without bypass. Native `dspy-rlm` agent confirmed (no new
  adapter); root model via `--model`, sub-model via typed `sub_model`
  (spec → request → `--agent-kwarg sub_model_name=`, dspy-rlm only); golden
  ExperimentSpec schema updated (+13 lines). Credential gate is grounded,
  not invented: specs defer with `missing_credential:dspy_lm_no_reviewed_route`
  (names the missing route, not a findable key); provider table
  (openai/anthropic) verified live against `litellm.utils.get_api_key`;
  unrelated credentials cannot open the route (tested). dspy 3.3.1 installs
  ephemerally with zero shared mutation; Deno absent (runtime-only need).
  Effective limit path: agent knobs (max_iterations 20, max_llm_calls 50) +
  spec timeout + policy cost ceilings; no per-request enforcement exists
  outside proxy lanes (verified in `runner.py`) — documented, not invented.
  Full affected files green; committed `707b6d2c` on isolated
  `feat/dspy-contrast-prep` (unpushed, unmerged).
- Lifecycle verdicts (lead-adjudicated, control-proven): orphan `.lease`
  files self-heal via mtime reclaim (existing test) and never match specs;
  `.lock` files stay by design (unlinking under concurrency is racy) —
  locked in by new convention tests. Signal trapping deferred with reason:
  recovery-by-reconcile is proven working and the child already tears down
  its process group on lease loss; handler threads risk deadlocks for
  marginal gain. No code change; no litter deleted.
- Missing (not invented): dspy package + Deno absent from the Harbor tool
  env; Analyst scope (task IDs + root/submodel selection) not received;
  est_cost_usd stays UNKNOWN.
- Integration pointer: `queue/approved/` is EMPTY — no eligible approved
  spec-bound native job exists. Not fabricated; Integration already consumed
  the DONE R2 job/spec in
  `.worktrees/lab-integration-experiment-visibility/derived/corrected-review-intake-20260908/native-intake-proof.json`
  (reuse, don't rerun).

## Route states (read-only qualification, 2026-09-08, no execution)

- ZAI (zai-opencode): allowlisted glm-5.3 / glm-5.3-flash (`craft.py:134`,
  `zai_campaign.py:91`, `run_preflight.py:65`); highspeed refused at compile;
  HTTP 429 = provider-access failure, never reward 0. Live credential state
  unprobed (needs approval); route stays qualified-but-unexecuted.
- DeepSeek (mini-swe-agent): `deepseek-v4-flash` via proxy lane
  (`execution_contracts.py:116`). Same unprobed-credential status as ZAI.
- Gemini (antigravity-cli): adapter exists (`harbor_antigravity.py`);
  profiles verified 2026-08-19 (`profiles.py:576`); credential lives in OS
  keyring via `agy` CLI. Unprobed today; no spend, no inference.
- Meta: no adapter and no profile found in source — recorded as missing
  capability, not a rejection. Any Meta route needs a new adapter lane.
- Codex quota reading 2026-09-08 (`evallab preflight`): 99% used is a STALE
  window (reset 2026-09-07 passed); refuses nothing, reassures nothing.
- `evallab preflight` also confirms the 4 waiting codex specs (our dispatch
  proof set) parked correctly; tick/launchd owns the drain once approved.

## Rules in force

Lab path only (`evallab run` for controls, dispatch→approve→tick otherwise);
never raw `harbor run` for evaluation. Free oracle/nop need no approval;
billable needs Peter per spec. Max two local experimental containers. No
edits to another lane's files or uncommitted work. Monitored return: this
file plus the registered completion monitor is the channel; no manual pages
or acknowledgement callbacks.
