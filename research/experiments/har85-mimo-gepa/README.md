# HAR-85: GEPA search for a general terminal-task instruction addendum (MiMo-V2.6)

Question: can GEPA, searching ONLY on train tasks, find a general agent-side
instruction addendum that beats the seed addendum on HELD-OUT terminal tasks
under a fixed student route, with adoption gated by a preregistered rule?

Status: staged + dry-run proven, $0 spent. No paid trial exists, no spec
approved, no proposer authorization written. Blocked at paid stages pending
Peter's approvals (see §6).

## 1. Sealed labels (re-bound 2026-09-29; provisional split retired)

- Split: HAR-81's SEALED split `../har81-mimo-sft/split.json`, salt
  `har81-sealed-20260928`, terminal heldout_count 16 (train 48).
  Manifest_digest `sha256:c3df70a5…`. Read through `sealed_split.py`
  (never copied -- there is no second split); every consumer re-asserts the
  digest on load. The provisional manifest (`split.provisional.json`, salt
  `har85-provisional-20260928`) is deleted; its train pool leaked 10 tasks
  into HAR-81's held-out set (0109, 0390, 0534, 0674, 0847, 1305, 1682, 1824,
  2329, 2760, plus excluded 0260) and held back 11 sealed-train tasks.
- Train pool: the 48 sealed-split train tasks minus `train-exclusions.json` =
  **48** (`sha256:e8ef53a1…`, rule and digest in `train_pool.py`). No
  sealed-train task is grader-broken (HAR-88's Daytona nop run graded all 64
  terminal tasks; its 3 broken tasks are all sealed held-out), so the
  exclusions list is empty and records that fact. `search-round.sh
  --dispatch` and the DSPy arm refuse excluded ids.
- Held-out set: the 16 sealed-split held-out tasks minus
  `heldout-exclusions.json` = **13** (`sha256:237da8fa…`). Excluded:
  `candidate-0260-security-appsec` (stevedore), `candidate-0674-ml-evaluation`
  (torch) and `candidate-2376-security-cryptography` (cryptography) -- every
  attempt scores 0 at pytest collection whatever the agent does (HAR-88
  export-broken, `broken-on-daytona.json` sha256:35f66da1…, commit b56408b2;
  curated findings under `library/task-findings/terminal/`).
- Student: `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` + `terminus-2`,
  HAR-81 `harness/` tree (temperature 0.6, top_p 0.95, `max_tokens` 4096,
  `proactive_summarization_threshold` 16384, trajectory
  `raw_content,linear_history`; tree sha256:2a8fd70d…). Single swappable
  parameter: `target.base_spec_path` →
  `base-specs/student-terminus2-selfhosted.json`. Per-token price $0; cost is
  time-based (`mimo_selfhosted_trial_cost_usd`: server $2.8149/h x trial_h /
  concurrency + sandbox, plus ~$0.40 per warm period). Ceilings 2000 requests
  / 64M input / 1M output tokens (HAR-90 pair C, nothing tripped); the 900 s
  agent timeout bounds spend.
- Execution: cloud. The base spec sets `environment: daytona`, which the
  executor maps to `evallab.harbor_daytona:BoundedDaytonaEnvironment` for
  Terminus-2 (named sandbox, provider-side TTL = watchdog (agent timeout +
  600 s) = 25 min; `execution_contracts.build_command`). Task containers never
  run on the Mac; the Terminus-2 controller and its loopback proxy stay
  host-side and drive the sandbox remotely. `make_paired_specs.py` copies
  route, environment, harness tree and limits from the same base spec, so
  search and held-out cannot diverge.
- Candidate artifact: agent-side instruction addendum ONLY (Harbor
  `extra_instruction_path`, `candidate_kind=instructions`). Task files, tests,
  and task instructions untouched.
- MiMo facts: no task ships `solution/` (nop is the only control); setup runs
  in `[environment.healthcheck]`; verifier timeout 240 s; terminal graders are
  deterministic pytest (no judge).
- HAR-82/83 catalog (#491): terminal `split_group` = task id (matches this
  split). `train_eligible` needs a `learnable` model verdict plus stability
  evidence, and no MiMo terminal task has a model trial yet, so the eligible
  set is empty today; this search keeps the full 48-task sealed train pool and
  its trials become that evidence. Every terminal task carries the H1
  `mimo-terminal-hook-planting` finding; selection-rule criterion (4) screens
  held-out trajectories with the HAR-83 detector.

## 2. Dry-run evidence ($0 end-to-end through the real Lab path, 2026-09-28)

`qualification-campaign.json` (engine gepa, agent nop, 3 train tasks,
deterministic QualificationProposer, local Docker) RAN END TO END:

- Report: `runs/gepa-har85-mimo-nop-qualification/attempt-26a22eed26c64f08820cfe8982cf98a2/result.json`
  status `candidate_review_required`, evidence_level
  `real_gepa_with_local_controls_and_deterministic_proposer`, proposer 1 call
  $0.00, `model_improvement_claimed: false`.
- Seed (`candidates/seed-addendum-v1.txt`,
  `sha256:399ec113…`) evaluated on all 3 tasks; the fixture proposal
  (`sha256:7c7138be…` = seed + `\nInspect the final task outputs against the
  stated requirements.\n`) stopped at the review gate, never submitted.

| Job dir (`runs/…`) | Task | Reward | Wall | Verifier |
|---|---|---|---|---|
| `gepa-nop-candidate-0036-softw-6240505183e6d425d2ff4f33` | candidate-0036 (snakemake flags) | 0 | ~7 s (setup ~1 s, verify ~1 s) | real pytest: 5 failed (nop did nothing), reward 0 |
| `gepa-nop-candidate-0109-scien-8e8189d092062060ff23ed99` | candidate-0109 | 0 | ~8 s | completed, reward 0, no exception |
| `gepa-nop-candidate-0260-secur-8193bf44728c0c887a9e2de6` | candidate-0260 | 0 | ~7 s | grader collection error (`No module named 'stevedore'`): the 0 is not a nop result; task excluded since (`heldout-exclusions.json`) |

This dry run predates the sealed split (it ran on the provisional pool) and
is frozen evidence: `qualification-campaign.json` is untouched by the rebind.
Its 0109/0260 rows are controls, not search, and both tasks are out of the
sealed train pool (0109 sealed held-out, 0260 excluded).

Setup passed fast because the pinned MiMo images come pre-baked
(`/var/lib/mimo/ready` present; images pulled: `xiaomimimo/mimo-v2.6-rl-oss`,
~1.34 GB). Trial containers were removed by Harbor afterwards (no `gepa-*`
containers left). Each trial ran setup → nop agent → verifier with recorded
per-phase timestamps (see `result.json` `environment_setup/agent_execution/
verifier`). No infra failures; all rewards the expected nop 0.

## 3. Adapter (smallest change on existing interfaces)

- `src/evallab/gepa_optimizer/workflow.py`: `_apply_retained_target` no longer
  copies retained per-trial ceilings to the campaign level for agents that do
  not accept campaign-level `provider_ceilings` (only `mini-swe-agent` /
  `zai-opencode` do, via `_CAMPAIGN_CEILING_AGENTS`). Terminus-2 keeps its
  ceilings in the retained spec, where `replay_spec_for_candidate` preserves
  them exactly. Without this, the staged paid path (`target.base_spec_path` +
  Terminus-2) was refused at load. Nop-only qualification needed NO change
  (controls + `instructions` candidates were already supported).
- Tests: `tests/test_gepa_optimizer_workflow.py` (+2: terminus target loads
  without campaign ceilings + replay preserves base ceilings; broker-direct
  targets still propagate).

## 4. Files

- `sealed_split.py` (read-through view of HAR-81's sealed split: terminal rows
  in pool shape; every consumer re-asserts manifest_digest
  `sha256:c3df70a5…`), `selection-rule.json` (preregistered critical-W
  sign-test table + no-infra-loss + generality rule + HAR-83 exploit screen;
  counts re-bound 16→13 on 2026-09-29, still before any paid trial),
  `BUDGET.md` (time-based formula with measured counts and per-trial estimates).
- `candidates/seed-addendum-v1.txt` (general guidance, no task specifics).
- `qualification-campaign.json` (dry-run source; outputs under
  `runs/gepa-har85-mimo-nop-qualification/`, runtime state, uncommitted).
- `base-specs/student-terminus2-selfhosted.json` (retained student route;
  committed source, NEVER submitted/approved here).
- `campaign-train.json` (STAGED search: 8 sealed-split train examples,
  `target` → base spec, opencode Flash proposer option A; validated by
  `load_campaign`, never run).
- `proposer-options.json` (A/B/C: 4/8/2 reflection calls; stamp fields +
  fresh approval ref to switch).
- `proposer-approval.template.json` (STAGED binding `16102672…`, signer/date
  blank; signed materialization is runtime state, gitignored).
- `train-exclusions.json` (empty under the sealed split: no sealed-train task
  is grader-broken) and `heldout-exclusions.json` (0260/0674/2376, each with
  its exact package digest, reason and HAR-88 evidence, recorded against the
  sealed manifest digest) and `train_pool.py` (`digest`, `check-campaign`,
  `materialize`; refuses on any drift).
- `search-round.sh` (per-round helper: no-args list + exact approve line,
  `--dispatch --ref [--max-specs N]` gated tick + campaign rerun; refusals
  exercised).
- `make_paired_specs.py` (final 26 held-out specs generator: route,
  environment, harness tree and limits copied from the base spec; also writes
  `paired-specs/cohort.json` for the exploit screen; refuses on drift/missing
  bytes).
- `paired-specs/` (empty until the winner exists; `ids.txt` recorded at submit).
- `run-after-approval.sh` (26/26 approval gate + key-scoped tick; bash-3.2
  safe; refusals exercised: missing `ids.txt` and 0/26 approved, both exit 2).
- `tasks/` (worktree-local, gitignored train materialization; held-out NEVER
  materialized here until the final eval).

## 5. Reproduce the task bytes (worktree-local, gitignored)

```bash
EXP=research/experiments/har85-mimo-gepa
SRC=/Users/petermakhnatch/Developer/.sources/mimo/terminal@fe1c2b66/tasks
# train pool (48 tasks = sealed split terminal train minus train-exclusions.json;
# asserts each package digest against the sealed manifest and removes excluded copies):
uv run --no-sync python $EXP/train_pool.py materialize --src $SRC
# held-out (13 scorable tasks) ONLY at final-eval time: copy the held-out ids
# (sealed_split.heldout_ids) from $SRC and assert task_directory_digest
# against the split rows the same way.
```

## 6. Launch sequence (Peter's commands, from a clean eval-lab worktree at main)

The scripts resolve the repository root from their own location, so run them
from any clean worktree containing this revision (never the dirty primary
checkout). `.worktrees/har85-gepa-mimo` is left prepared for this.

PREREQUISITES (once per worktree, $0):
```bash
uv sync --locked
uv pip install -r research/experiments/harness-gepa/requirements.txt   # pinned GEPA runtime (verify_release)
# then run the §5 recipe to materialize the 48-task train pool
# Redeploy the Modal server first (HAR-90 leaves the app stopped); the tick
# process needs EVALLAB_MIMO_SELFHOSTED_UPSTREAM + MIMO_SELFHOSTED_API_KEY
# (Modal upstream) and DAYTONA_API_KEY (sandboxes) in ~/.omp/agent/.env --
# the scripts read them into the tick process only, never print them.
# The proposer (opencode Flash) uses the coding-plan credential from the
# owner's OpenCode auth store, as before.
```

STEP 1 — train search (iterative, approval-gated; one parked spec per round):
```bash
# once: materialize the signed proposer authorization from the staged template
uv run --no-sync python - research/experiments/har85-mimo-gepa/proposer-approval.template.json <<'EOF'
import json, sys
from datetime import UTC, datetime
t = json.load(open(sys.argv[1]))
t['approved_by'] = 'peter'
t['approved_at'] = datetime.now(UTC).isoformat(timespec='seconds')
json.dump(t, open('research/experiments/har85-mimo-gepa/proposer-approval.signed.json', 'w'), indent=2)
print('wrote signed ref')
EOF
# first round: park the 8 baseline specs ($0; no tick while nothing is approved)
./research/experiments/har85-mimo-gepa/search-round.sh --dispatch \
  --ref research/experiments/har85-mimo-gepa/proposer-approval.signed.json
# approve the 8 printed by search-round.sh, then run ONE as the smoke: this is
# the first GEPA-search trial on the distill route (worst-case est $0.77,
# expected ≈ $0.44 + warm share). Inspect it with
# `uv run evallab report run <run>` before ticking the other 7.
./research/experiments/har85-mimo-gepa/search-round.sh --dispatch \
  --ref research/experiments/har85-mimo-gepa/proposer-approval.signed.json --max-specs 1
# each round after that:
./research/experiments/har85-mimo-gepa/search-round.sh   # lists parked specs + exact approve line; changes nothing
# for id in <...>; do uv run evallab approve "$id" --actor peter; done   (printed above)
./research/experiments/har85-mimo-gepa/search-round.sh --dispatch \
  --ref research/experiments/har85-mimo-gepa/proposer-approval.signed.json
# repeat until status completed; review gate:
uv run --no-sync python -m evallab.gepa_optimizer approve-candidate \
  research/experiments/har85-mimo-gepa/campaign-train.json --candidate <FULL_SHA256>
```
`search-round.sh` lists only this campaign's parked specs (name prefix
`gepa-runs-gepa-har85-mimo-train-search-`, derived from the evaluator
output_dir); `--dispatch` refuses unless every campaign example is in the
train pool (`train_pool.py check-campaign`) and every listed spec is approved,
ticks only those IDs with the key loaded non-printing, then reruns the campaign.
The signed ref is runtime state (gitignored), never committed.
The `--proposer-approval-ref` JSON must carry `binding_sha256` of the exact
frozen campaign binding + `approved_by` + `approved_at` (operator: Peter).
The staged binding (`proposer-approval.template.json`) was precomputed with
the real loader/pin/hash (`load_campaign`, `verify_release`,
`hashlib.sha256(json.dumps(binding, sort_keys=True))` per workflow.py:592-606):
`161026722648190bc6b15fa7af1218fa95a2df25fe3c9a637e52abd5f99c4ad1`.
Recompute after ANY byte change to `campaign-train.json`, the retained base
spec, the seed, or the
pinned GEPA release (reinstall first: `uv pip install -r
research/experiments/harness-gepa/requirements.txt`, then):
```bash
uv run --no-sync python -c "
import json, hashlib
from pathlib import Path
from evallab.gepa_optimizer.workflow import load_campaign
from evallab.gepa_optimizer.release import verify_release
root = Path('.').resolve()
config = load_campaign(Path('research/experiments/har85-mimo-gepa/campaign-train.json'), root)
seed = (root / config['seed_candidate_path']).read_text(encoding='utf-8')
binding = {'config': config, 'seed_sha256': 'sha256:' + hashlib.sha256(seed.encode()).hexdigest(), 'release': verify_release(), 'qualification': False}
print(hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest())
"
```

Rounds (from the code): the baseline gate and the final selection loop submit
EVERY example before halting (`_evaluate_allowing_pending`: pending halts are
collected per example and the first is re-raised, so the run still reports
`pending_evaluation`; any other halt stops immediately). In-engine evaluation
still halts on the first novel pair (`evaluate()` wraps every evaluator
exception — including the `EvaluationPending` raised when the first spec of a
batch parks — into `_EvaluationHalt`: workflow.py:685-703, :854-866).
So: baseline = 1 round (8 specs); search = one round per novel batch (worst
case 24, typical a handful — reflection minibatches are 1 example, each
proposal round parks its novel evals then halts); selection re-eval of the
best on the 8 train examples = 1 round (mostly retained hits). Worst case ≈
26 approve/dispatch rounds (was ~33 before batching); typical ≈ 8-12. Hard
cap unchanged: `AggregateBudget` `max_target_attempts: 32` distinct target
submissions. Each round is: `search-round.sh` → approve line →
`search-round.sh --dispatch --ref …`.
In-engine minibatches were deliberately NOT parallelized: the pinned upstream
fans batch evaluations over threads (which would submit every started spec
before propagation), but our candidate store (`open("x")` after an
`exists()` check) spuriously fails concurrent same-candidate stores, so a
concurrency field stays out until that race is fixed. See BUDGET.md.

STEP 2 — final held-out eval (one-shot, after a reviewed winner exists):
```bash
# materialize the 13 scorable held-out tasks (§5 pattern over the sealed_split
# held-out ids), then:
uv run python research/experiments/har85-mimo-gepa/make_paired_specs.py \
  --winner <reviewed-winner-path> --winner-sha256 sha256:<64hex>
# submit paired-specs/ (parks 26 specs), record the 26 queue IDs in paired-specs/ids.txt
uv run evallab approve <SPEC_ID> --actor peter   # x 26
./research/experiments/har85-mimo-gepa/run-after-approval.sh
# criterion (4): HAR-83 exploit screen over the 26 held-out job dirs ($0, local)
uv run evallab tasks exploit-collect \
  --cohort research/experiments/har85-mimo-gepa/paired-specs/cohort.json \
  <26 held-out job dirs> --output runs/har85-heldout-exploits/task_exploits.parquet
```
Then apply `selection-rule.json` verbatim (sign test via `src/evallab/power.py`,
paired analysis via `src/evallab/gepa_optimizer/paired_analysis.py`,
trajectory generality check, exploit screen); retain the seed unless ALL
conditions hold.

## 7. Sealed-split rebind (done 2026-09-29, $0; paid runs still gated)

- Split: `split.provisional.json` deleted; everything derives from HAR-81's
  sealed manifest via `sealed_split.py` (digest re-asserted on every load).
  Train 48, scorable held-out 13; `train_pool.py check-campaign`,
  `make_paired_specs.py` and the DSPy arm refuse anything else.
- Student: `base-specs/student-terminus2-selfhosted.json` (distill route +
  HAR-81 harness/ tree, ceilings 2000/64M/1M, timeout 900 s); the route stays
  one parameter (`target.base_spec_path`). BUDGET.md recomputed time-based.
- Sequencing (Research-Harbor 2026-09-29): paid GEPA/DSPy runs wait for
  HAR-81's baseline and Peter's approval. This rebind spent $0.

## Limits

- No paid call of any kind was made; all trial evidence is nop controls plus
  staged (unapproved) artifacts. The staged per-trial nominal model ceiling
  ($0.01 -- tokens are $0) and worst-case estimate ($0.77) are genuine
  caps/estimates, not measurements.
- No GEPA-search trial has run on the distill route yet (HAR-90 ran the same
  route + harness on 0036/0758 outside this campaign). Replay specs validate
  and build the bounded-Daytona Harbor command at $0; the first real execution
  is the `--max-specs 1` smoke.
- 3-task dry run: proves the loop mechanics on real MiMo packages, not search
  quality. The 8-task search config is staged, not run.
- Proposer route is OpenCode-Flash only; any other reflection model needs its
  own qualification + fresh approval ref.
