# HAR-110: GEPA search for a general Python-code-task instruction addendum (MiMo-V2.6)

Question: can GEPA, searching ONLY on 6 development Python code tasks, find a
general agent-side instruction addendum that beats the seed addendum on 4
HELD-OUT Python code tasks under a fixed student route?

Status: staged + nop qualification proven on Daytona (~$0.01 sandbox, $0.00
model). No paid trial exists, no spec approved, no proposer authorization
written. Blocked at paid stages pending HAR-104 trial completion and Peter's
approvals (see §7).

## 1. Route (fixed student)

- Student: `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` + `terminus-2`,
  HAR-104 `harness/` tree (`research/experiments/har104-mimo-exploration/harness`,
  sha256:433d5d2946e317b0213438ea4aa1852f2aaffa5c3a81a3bbdeb42d4aa028ecf3).
  Single swappable parameter: `target.base_spec_path` →
  `base-specs/student-terminus2-selfhosted-python.json`.
- Limits (from the HAR-104 batch, identical on all 10 tasks): max_requests
  120, max_input_tokens 2_500_000, max_output_tokens 131_072
  (max_total 2_631_072), cost_limit_usd 0.01 (tokens are $0 on this route;
  cost is time-based via `mimo_selfhosted_trial_cost_usd`), agent timeout =
  the task's own 3600 s, verifier 2100 s, 2 vCPU / 8 GiB, no storage_mb so
  every spec carries `override_storage_mb: 10240` (Harbor `--override-storage-mb`;
  without it Daytona grants the 3 GiB default -- HAR-88).
- Base spec task: `format-code-task-002407` (first development task). It is a
  replay template only: model-backed evaluation overrides
  task/task_path/task_id/task_package_digest per example
  (`evaluator.py` replay path), and `validate_drift` checks digest-vs-disk,
  not split membership. So the base spec stays valid even if a future
  stratified split moves 002407 to held-out.
- Per-token price $0; `est_cost_usd` 1.85 is the worst-case spec estimate at
  campaign concurrency c=2 (see BUDGET.md). 1.85 <= 3.00 (the queue's per-job
  ceiling). At c=1 the same formula gives $3.25, so live waves MUST share the
  Modal server across >= 2 concurrent trials (BUDGET.md §Waves).
- Execution: cloud. The base spec sets `environment: daytona`, mapped to
  `evallab.harbor_daytona:BoundedDaytonaEnvironment` (named sandbox,
  provider-side TTL; `execution_contracts.build_command`). Task containers
  never run on the Mac. `make_paired_specs.py` copies route, environment,
  harness tree and limits from the same base spec, so search and held-out
  cannot diverge.
- Candidate artifact: agent-side instruction addendum ONLY (Harbor
  `extra_instruction_path`, `candidate_kind=instructions`). Task files, tests,
  and task instructions untouched.
- Graders are hidden tests (deterministic pytest, no judge).

## 2. Split (fixed before any live result)

- `split.json`, digest
  `sha256:4e9861fd34b58675929c6bcc36b85a95571864f7821411e9ec13be18ef29f2ed`,
  salt `har110-py-v1`: seeded hash over the 10 task ids from HAR-105 part 3
  (`../har105-exploration/python_selection.json`, entries with `in_set`).
- Development (6): 002407, 000226, 002259, 000383, 002256, 001896.
  Held-out (4): 001832, 000927, 002391, 002864.
- `make_split.py` reproduces it (`--check` verifies; fuzzed over 200 random
  reward maps for the stratified path). With `--rewards <task-id->reward.json>`
  (HAR-104 rewards, supplied later) it stratifies so passes spread over both
  sides, same salt for within-stratum order -- fully determined by its inputs.
  `fill_refs.py` and `make_paired_specs.py` both read split.json as the single
  source of truth (the latter pins the digest and refuses on drift).
- Rule: the split is fixed before the optimiser sees any live result. HAR-104
  baseline rewards are prior data, not optimiser results, so adopting the
  stratified split on their arrival is allowed -- but ONLY before the live
  search starts, and if the development set changes, the nop qualification
  (§6) must be re-run on the new set first (one command, ~$0.05). Held-out
  tasks never enter search.

## 3. Seed

- `candidates/seed-addendum-v1.txt`, byte-identical to HAR-85's seed
  (`sha256:399ec1138ff0279833957b996439d4f119f37de2ba90ef915f9c4db2eeed0241`).
  Reused deliberately: every bullet is domain-general working guidance
  (inspect, reproduce a baseline, minimal reversible edits, re-run checks,
  read full errors, leave the workspace clean) with no task specifics and no
  answers; keeping the bytes identical preserves comparability with HAR-85.

## 4. Budget (BUDGET.md has the full arithmetic)

- Search: max_evals 10, max_iterations 2, max_candidates_per_iter 1,
  max_target_attempts 22 (= 6 baseline + 10 upstream + 6 selection re-evals,
  hard-capped by AggregateBudget). Expected ~16 new trials x ~$0.29 server
  (12.5 min wall at $2.8149/h shared c=2) = **~$4.7 Modal** + ~$0.9-1.3
  Daytona sandbox, both reported separately. Worst-case catalog 22 x $1.85 =
  $40.70 > $20/day: dispatch in waves, stop the Modal app between phases.
- Proposer: opencode Flash, <= 4 calls x $0.05 = **$0.20 cap** (<= $1.00).
- Held-out: 8 trials, ~$2.3 server + ~$0.5 sandbox expected.
- Qualification (§6): 6 nop trials, ~$0.06 expected sandbox; cap $0.20.

## 5. Proposer route finding (why Flash, not full GLM-5.3)

The card prefers GLM-5.3, but the loader admits only two proposer routes and
neither fits full GLM-5.3 today: direct-transport `zai/*` models are refused
by `direct_proposer_blocker` (the Coding Plan credential is not a general API
grant -- a separately approved API route would be needed), and the opencode
transport pins MODEL `zai-coding-plan/glm-5.3-flash` exactly (workflow.py
validates it). So the staged campaign uses opencode + Flash (option A,
`proposer-options.json`; every option sums to <= $1.00). Restaging on full
GLM-5.3 needs its own route qualification first -- no provider substitution.

## 6. Nop qualification evidence ($0.01 sandbox, $0.00 model, 2026-09-30)

`qualification-campaign.json` (engine gepa, target -> retained nop base spec
`base-specs/nop-daytona-python.json`, 6 development tasks, deterministic
QualificationProposer) RAN END TO END. The nop base carries
`environment: daytona` (plus timeout 5400 / attempts 1 / concurrency 1 from
the HAR-88 code-task precedent); control trials inherit it from the base spec
(`evaluator.py` control path), which is why this qualification runs on
Daytona with zero model calls.

- Report: `runs/gepa-har110-python-nop-qualification/attempt-4e2daf6d561048a3ad21bfd91a2f6471/result.json`
  status `candidate_review_required`, evidence_level
  `real_gepa_with_local_controls_and_deterministic_proposer`, proposer 1 call
  $0.00 (`deterministic_interface_fixture`, `fixture_no_model`),
  `model_improvement_claimed: false`.
- Seed (`sha256:399ec113…`) evaluated on all 6 tasks; the fixture proposal
  (`sha256:7c7138be…` = seed + `\nInspect the final task outputs against the
  stated requirements.\n`) stopped at the review gate, never submitted
  (`status` shows both candidates unreviewed, last attempt
  `CandidateReviewRequired`).
- Mechanism note (deviation from the card, verified in code): GEPA control
  trials run through `execute_direct` -- synchronous Harbor runs, no queue
  specs. Nothing parked in `queue/` (empty after the run), so there was
  nothing to approve and `tick` was inapplicable; DAYTONA_API_KEY came from
  the stored key (`keys run --`). The $0.20 cap therefore binds measured
  sandbox time, not approvals.

| Job dir (`runs/…`) | Task | Reward | Wall | Verifier |
|---|---|---|---|---|
| `gepa-nop-format-code-task-002-70de49ff856e2c7fde29cc1c` | 002407 (python-control) | 0 | ~10 s | `_DaytonaDirect`, prebuilt image, healthcheck passed, real pytest graded 0 (nop did nothing), no exception |
| `gepa-nop-format-code-task-000-50fb375f93ef3c5d86172d0e` | 000383 (quickfix) | 0 | ~29 s | same Daytona shape, reward 0, no exception |
| `gepa-nop-format-code-task-002-89bd67733a7271e2dd34df11` | 002259 (PHARE) | 0 | ~40 s | same Daytona shape, reward 0, no exception |
| `gepa-nop-format-code-task-000-e2c573e839768e76f230c53a` | 000226 (waitress) | 0 | ~7 s | same Daytona shape, reward 0, no exception |
| `gepa-nop-format-code-task-002-b66f936286c2b38ad40e08df` | 002256 (persist-queue) | 0 | ~7 s | same Daytona shape, reward 0, no exception |
| `gepa-nop-format-code-task-001-8020ecff9375e5dc0926cc84` | 001896 (linkpreview) | 0 | ~10 s | same Daytona shape, reward 0, no exception |

Campaign wall ~121 s total. Spend: model $0.00 (null usage ledgers on every
trial); sandbox by rate card ~$0.01 (100 trial-seconds x $0.23094/h) --
exact sandbox figure lives on the Daytona org billing, two orders of
magnitude inside the $0.20 cap. No infra failures; all rewards the expected
nop 0. Trial configs record
`environment.import_path: evallab.harbor_daytona:BoundedDaytonaEnvironment`
and the seed addendum under `extra_instruction_paths`.

## 7. Launch sequence (parent's commands, from a clean eval-lab worktree at main)

The scripts resolve the repository root from their own location, so run them
from any clean worktree containing this revision. Prerequisites ($0):
`uv sync --locked`, `uv pip install -r
research/experiments/harness-gepa/requirements.txt`, DAYTONA_API_KEY +
Modal upstream (`EVALLAB_MIMO_SELFHOSTED_UPSTREAM` + `MIMO_SELFHOSTED_API_KEY`)
in the key store, and the Modal server redeployed (HAR-90 leaves the app
stopped).

```bash
EXP=research/experiments/har110-python-gepa
SRC=/Users/petermakhnatch/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks
# 0. materialize the 10 task bytes (gitignored; pins in split.json via fill_refs digests,
#    cross-checked one-for-one against HAR-104 derived/prepared/har104-d-*.json digests):
for t in $(uv run --no-sync python -c "import json;s=json.load(open('$EXP/split.json'));print(' '.join(s['development']+s['heldout']))"); do cp -r $SRC/$t $EXP/tasks/$t; done
# 1. split with HAR-104 rewards (BEFORE any live GEPA result; preview first):
uv run --no-sync python $EXP/make_split.py --rewards <task-id-to-reward.json> --check
#    if the sets match the committed split: proceed. If they differ: adopting the
#    stratified split is allowed ONLY now -- overwrite, then re-run fill (§2) and
#    the §6 qualification on the new development set before touching the live campaign:
uv run --no-sync python $EXP/make_split.py --rewards <task-id-to-rewards.json>
# 2. fill campaign examples + prior_run_reference slots from finished HAR-104 trials
#    (refuses unless every development task has a finished result.json):
uv run --no-sync python $EXP/fill_refs.py --trials <har104-runs-worktree>/runs
# 3. recompute the proposer binding (must equal proposer-approval.template.json
#    ONLY if nothing changed since staging -- the --trials fill changes examples,
#    so expect a NEW binding and record it in a fresh template):
uv run --no-sync python -c "
import json, hashlib
from pathlib import Path
from evallab.gepa_optimizer.workflow import load_campaign
from evallab.gepa_optimizer.release import verify_release
root = Path('.').resolve()
config = load_campaign(Path('$EXP/campaign-train.json'), root)
seed = (root / config['seed_candidate_path']).read_text(encoding='utf-8')
binding = {'config': config, 'seed_sha256': 'sha256:' + hashlib.sha256(seed.encode()).hexdigest(), 'release': verify_release(), 'qualification': False}
print(hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest())
"
#    then materialize the signed ref (runtime state, never committed):
uv run --no-sync python - research/experiments/har110-python-gepa/proposer-approval.template.json <<'EOF'
import json, sys
from datetime import UTC, datetime
t = json.load(open(sys.argv[1]))
t['approved_by'] = 'peter'
t['approved_at'] = datetime.now(UTC).isoformat(timespec='seconds')
json.dump(t, open('research/experiments/har110-python-gepa/proposer-approval.signed.json', 'w'), indent=2)
print('wrote signed ref')
EOF
# 4. live search (parks baseline specs first; approve each, TICK THE 6 TOGETHER
#    so the server is shared c>=2 per BUDGET.md Waves; rerun until completed):
uv run --no-sync python -m evallab.gepa_optimizer run $EXP/campaign-train.json \
  --proposer-approval-ref $EXP/proposer-approval.signed.json
uv run --no-sync python -m evallab.gepa_optimizer status $EXP/campaign-train.json
# for id in <parked spec ids>; do uv run evallab approve "$id" --actor peter; done
# uv run evallab tick --spec-id <id1> --spec-id <id2> ...   (one tick, whole batch)
# 5. review gate:
uv run --no-sync python -m evallab.gepa_optimizer approve-candidate \
  $EXP/campaign-train.json --candidate <FULL_SHA256>
# 6. held-out comparison (one-shot, after a reviewed winner exists):
uv run --no-sync python $EXP/make_paired_specs.py \
  --winner <reviewed-winner-path> --winner-sha256 sha256:<64hex>
# submit paired-specs/ (parks 8 specs), record the 8 queue IDs in paired-specs/ids.txt,
# approve x8, tick together, then compare seed-vs-gepa arms on the 4 held-out tasks.
```

## 8. Files

- `split.json` (fixed 6/4 split, digest-pinned) + `make_split.py` (seeded
  hash; `--rewards` stratification; `--check` verify-only).
- `fill_refs.py` (regenerates examples in both campaigns from split.json;
  attaches `prior_run_reference` {trial_path, result_sha256 of the HAR-104
  trial result.json, current task package digest} with `--trials`).
- `base-specs/student-terminus2-selfhosted-python.json` (retained student
  route; committed source, NEVER submitted/approved here) and
  `base-specs/nop-daytona-python.json` (retained nop template that puts the
  qualification on Daytona).
- `candidates/seed-addendum-v1.txt` (byte-identical HAR-85 seed,
  sha256:399ec113…).
- `campaign-train.json` (STAGED search: 6 development examples, target ->
  student base, opencode Flash proposer option A; validated by
  `load_campaign`, never run; prior refs attached by the parent via
  fill_refs before live).
- `qualification-campaign.json` (RAN §6; outputs under
  `runs/gepa-har110-python-nop-qualification/`, runtime state, uncommitted).
- `proposer-options.json` (A/B/C: 4/8/2 reflection calls; all <= $1.00) and
  `proposer-approval.template.json` (STAGED binding `5f2b37f4…` for the
  as-committed campaign; the `--trials` fill changes examples, so the parent
  recomputes per §7 step 3).
- `make_paired_specs.py` (final 8 held-out specs generator: route,
  environment, harness tree and limits copied from the student base spec
  incl. override_storage_mb; refuses on split drift/missing bytes) +
  `paired-specs/` (empty until the winner exists).
- `BUDGET.md` (time-based formula with per-trial estimates and the c>=2
  sharing discipline).
- `tasks/` (worktree-local, gitignored task materialization).

## Limits

- No paid call of any kind was made; all trial evidence is nop controls plus
  staged (unapproved, unsigned) artifacts. The staged per-trial nominal model
  ceiling ($0.01) and worst-case estimate ($1.85 at c=2) are genuine
  caps/estimates, not measurements.
- No GEPA-search trial has run on this route yet. The first live wave (the 6
  baseline specs ticked together) is the smoke: inspect it with
  `uv run evallab report run <run>` before ticking search rounds.
- The as-committed campaign-train.json carries no `prior_run_reference`
  (HAR-104 trials unfinished); it loads but is not live-ready until the §7
  step 2 fill. Its staged binding (`5f2b37f4…`) covers the unfilled form;
  the parent recomputes after filling.
