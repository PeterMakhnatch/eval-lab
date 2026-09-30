# HAR-110: GEPA search for a general Python-code-task instruction addendum (MiMo-V2.6)

Question: can GEPA, searching ONLY on 6 development Python code tasks, find a
general agent-side instruction addendum that beats the seed addendum on 4
HELD-OUT Python code tasks under a fixed student route?

Status: staged + nop qualification proven on Daytona on the stratified split
(~$0.01 sandbox, $0.00 model). No paid trial exists, no spec approved, no
proposer authorization written. Blocked at paid stages pending Peter's
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
  `sha256:a7cf5d581ca7f43257daca9eabad7ae1eed50f99558d12a9f363510983c32719`,
  salt `har110-py-v1`: seeded hash over the 10 task ids from HAR-105 part 3
  (`../har105-exploration/python_selection.json`, entries with `in_set`),
  stratified by the leak-adjusted HAR-104 rewards in `har104-rewards.json`
  (rewards file digest
  `sha256:a0b7ce61bf62e80164f38b7a78d8dc6b58311eb65148eea32812b7c108321878`).
- Development (6): 002407, 000226, 002259, 000383, 002256, 002391.
  Held-out (4): 001896, 001832, 000927, 002864. Each side holds one clean
  HAR-104 pass (002391 / 002864); the two tainted passes (000226, 000927)
  score 0 under the leak rule (§2a) wherever they run.
- `make_split.py` reproduces it (`--check` verifies; fuzzed over 200 random
  reward maps for the stratified path). With `--rewards <task-id->reward.json>`
  it stratifies so passes spread over both sides, same salt for within-stratum
  order -- fully determined by its inputs. `fill_refs.py` and
  `make_paired_specs.py` both read split.json as the single source of truth
  (the latter pins the digest and refuses on drift).
- Rule: the split is fixed before the optimiser sees any live result. HAR-104
  baseline rewards are prior data, not optimiser results, so adopting the
  stratified split on their arrival was allowed -- but ONLY before the live
  search starts, and because the development set changed (001896 out, 002391
  in), the nop qualification (§6) was re-run on the new set first. Held-out
  tasks never enter search.

## 2a. Leak rule (upstream-fetch guard)

- Raw HAR-104 verifier rewards: 000226 1.0, 000927 1.0, 002391 1.0, 002864
  1.0, the other six 0.0. The detector (`src/evallab/upstream_fetch.py`,
  `detect_upstream_fetch` + `commands_from_trial` over the trial ATIF
  trajectories) flags 000226 (step 4: `pip download waitress==2.0.0`,
  names the task repo), 000927 (step 10: curl of the upstream soupsieve file;
  steps 11/17: `pip download soupsieve==1.9.1`, all naming the task repo),
  and 002407 (steps 24/25: `pip download control==0.9.3`, already reward 0).
  The 002391/002864 passes and the other five trials are clean (a bare
  `urllib` mention inside a quoted URL with no fetch call is NOT a finding --
  the python rule requires a fetch call site).
- `har104-rewards.json` is therefore the raw rewards with the two tainted
  passes forced to 0 (000226: 0, 000927: 0; 002391: 1, 002864: 1; rest 0).
  The committed `campaign-train.json` carries
  `score_rules: ["upstream_fetch_zero"]`: wherever
  `evallab.gepa_optimizer` turns a trial into a GEPA score, a trial with any
  detector finding scores 0 (the verifier reward stands; the rule, findings,
  and excerpts land in the evaluation evidence and feedback). Clean-trial
  scoring is untouched. Behaviour tests: `tests/test_upstream_fetch.py`.

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
- Proposer: GLM-5.3 on the Z.ai standard API, <= 4 calls, **$0.20 cap** (<= $1.00); one real reflection prompt measured $0.059.
- Held-out: 8 trials, ~$2.3 server + ~$0.5 sandbox expected.
- Qualification (§6): 6 nop trials, ~$0.06 expected sandbox; cap $0.20.

## 5. Proposer route: GLM-5.3 on the Z.ai standard API (changed 2026-09-30)

Staging chose GLM-5.3-Flash through opencode on the Z.ai Coding Plan, because
`direct_proposer_blocker` refused every `zai/*` model on the assumption that
the lab's Z.ai credential was the tool-restricted Coding Plan. Live, the Coding
Plan returned HTTP 429 code 1309 ("package has expired"). The first opencode
proposal also stalled before any request: an 85 KB reflection prompt plus
opencode's ~10k-token system prompt exceeds the 22k input ceiling.

The lab also holds the pay-as-you-go standard-API key `ZAI_OPENAPI_API_KEY`, the
`zai/` route that Terminus and mini-swe-agent targets already use. The blocker
now refuses only `zai-coding-plan/*` and unlisted `zai/*` models. A direct `zai/`
proposer binds `ZAI_OPENAPI_API_KEY` explicitly (never LiteLLM's default
`ZAI_API_KEY`), with 32,768 output tokens and a 900 s timeout because GLM-5.3
reasons first. A reply cut at that ceiling is recorded as `truncated` (billed,
retained for inspection) and stops the campaign instead of becoming a
candidate: the first live call, under an 8,192 ceiling, returned a proposal
cut off mid-sentence. The campaign pins `proposer_model: zai/glm-5.3` and
`proposer_transport: direct`, which is the card's GLM-5.3. A precheck on the
real 85 KB prompt took 113 s: 25,428 in / 5,406 out (4,922 reasoning), $0.059.
Run the campaign under `keys run --` so the key is present.

## 6. Nop qualification evidence (~$0.005 sandbox, $0.00 model, 2026-09-30)

`qualification-campaign.json` (engine gepa, target -> retained nop base spec
`base-specs/nop-daytona-python.json`, the 6 stratified-split development
tasks, deterministic QualificationProposer) RAN END TO END after the §2a
re-split (an earlier qualification ran on the pre-stratification set; the
development change 001896 -> 002391 required this re-run). The nop base
carries `environment: daytona` (plus timeout 5400 / attempts 1 / concurrency
1 from the HAR-88 code-task precedent); control trials inherit it from the
base spec (`evaluator.py` control path), which is why this qualification runs
on Daytona with zero model calls.

- Report: `runs/gepa-har110-python-nop-qualification/attempt-8be86ea7462f44b793e3e56e195cd6a1/result.json`
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
| `gepa-nop-format-code-task-002-70de49ff856e2c7fde29cc1c` | 002407 (python-control) | 0 | ~9 s | `_DaytonaDirect`, prebuilt image, healthcheck passed, real pytest graded 0 (nop did nothing), no exception |
| `gepa-nop-format-code-task-000-e2c573e839768e76f230c53a` | 000226 (waitress) | 0 | ~31 s | same Daytona shape, reward 0, no exception |
| `gepa-nop-format-code-task-002-89bd67733a7271e2dd34df11` | 002259 (PHARE) | 0 | ~7 s | same Daytona shape, reward 0, no exception |
| `gepa-nop-format-code-task-000-50fb375f93ef3c5d86172d0e` | 000383 (quickfix) | 0 | ~10 s | same Daytona shape, reward 0, no exception |
| `gepa-nop-format-code-task-002-b66f936286c2b38ad40e08df` | 002256 (persist-queue) | 0 | ~9 s | same Daytona shape, reward 0, no exception |
| `gepa-nop-format-code-task-002-447291d10963b07d1d8ae3a9` | 002391 (pip-audit) | 0 | ~9 s | same Daytona shape, reward 0, no exception |

Campaign wall ~94 s total. Spend: model $0.00 (null usage ledgers on every
trial); sandbox by rate card ~$0.005 (77 trial-seconds x $0.23094/h) --
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
# 1. split with HAR-104 rewards (BEFORE any live GEPA result; preview first).
#    The committed split is already stratified on har104-rewards.json -- this
#    should print matching sets (split_digest a7cf5d581…); proceed only then:
uv run --no-sync python $EXP/make_split.py --rewards $EXP/har104-rewards.json --check
#    If the sets ever differ: adopting a new stratified split is allowed ONLY
#    now -- overwrite, then re-run fill (§2) and the §6 qualification on the
#    new development set before touching the live campaign.
# 2. fill campaign examples + prior_run_reference slots from finished HAR-104 trials.
#    Trial refs are jailed to the repo, so stage the 6 development job dirs'
#    finished trial subdirs under $EXP/prior-trials first (gitignored; layout
#    mirrors the HAR-104 runs root; _aborted-* entries are ignored by the fill):
for s in 002407 000226 002259 000383 002256 002391; do cp -r <har104-runs-worktree>/runs/har104-d-$s $EXP/prior-trials/har104-d-$s; done
#    (refuses unless every development task has exactly one finished trial):
uv run --no-sync python $EXP/fill_refs.py --trials $EXP/prior-trials
# 3. recompute the proposer binding (must equal proposer-approval.template.json
#    `aab2639a…` when run on the as-committed campaign + staged prior-trials;
#    any other value means something changed -- stop and diff before signing):
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
#    so the server is shared c>=2 per BUDGET.md Waves; rerun until completed).
#    `keys run --` supplies ZAI_OPENAPI_API_KEY to the direct GLM-5.3 proposer:
keys run -- uv run --no-sync python -m evallab.gepa_optimizer run $EXP/campaign-train.json \
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

- `split.json` (fixed 6/4 split, digest-pinned, stratified on
  `har104-rewards.json`) + `make_split.py` (seeded hash; `--rewards`
  stratification; `--check` verify-only).
- `har104-rewards.json` (leak-adjusted HAR-104 rewards: the two tainted
  passes 000226/000927 forced to 0; see §2a).
- `fill_refs.py` (regenerates examples in both campaigns from split.json;
  attaches `prior_run_reference` {trial_path, result_sha256 of the HAR-104
  *trial-level* result.json, current task package digest} with `--trials`,
  discovering the single finished `har104-d-<suffix>__<trial>` subdir per
  job and ignoring `_aborted-*`).
- `base-specs/student-terminus2-selfhosted-python.json` (retained student
  route; committed source, NEVER submitted/approved here) and
  `base-specs/nop-daytona-python.json` (retained nop template that puts the
  qualification on Daytona).
- `candidates/seed-addendum-v1.txt` (byte-identical HAR-85 seed,
  sha256:399ec113…).
- `campaign-train.json` (live search: 6 development examples WITH prior
  refs, target -> student base, `score_rules: [upstream_fetch_zero]`,
  direct GLM-5.3 proposer on the Z.ai standard API, §5).
- `qualification-campaign.json` (RAN §6 on the stratified development set;
  outputs under `runs/gepa-har110-python-nop-qualification/`, runtime state,
  uncommitted).
- `proposer-options.json` (staging-time A/B/C opencode Flash options,
  superseded by §5) and `proposer-approval.template.json` (binding
  `aab2639a…` for the as-committed campaign with priors + score rule;
  recompute per §7 step 3 and compare before signing).
- `make_paired_specs.py` (final 8 held-out specs generator: route,
  environment, harness tree and limits copied from the student base spec
  incl. override_storage_mb; refuses on split drift/missing bytes) +
  `paired-specs/` (empty until the winner exists).
- `BUDGET.md` (time-based formula with per-trial estimates and the c>=2
  sharing discipline).
- `tasks/` + `prior-trials/` (worktree-local, gitignored materializations:
  task bytes and staged HAR-104 trial copies; see §7 steps 0/2).

## Limits

- Live spend and per-run results are recorded on the HAR-110 Linear card, not
  here. The per-trial nominal model ceiling ($0.01) and worst-case estimate
  ($1.85 at c=2) are caps/estimates, not measurements.
- The as-committed campaign-train.json is live-ready (priors attached,
  binding `aab2639a…`) once the gitignored `tasks/` + `prior-trials/` are
  staged per §7 steps 0/2 and the binding recomputation matches.
