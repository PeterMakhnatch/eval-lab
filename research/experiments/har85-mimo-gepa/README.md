# HAR-85: GEPA search for a general terminal-task instruction addendum (MiMo-V2.6)

Question: can GEPA, searching ONLY on train tasks, find a general agent-side
instruction addendum that beats the seed addendum on HELD-OUT terminal tasks
under a fixed student route, with adoption gated by a preregistered rule?

Status: staged + dry-run proven, $0 spent. No paid trial exists, no spec
approved, no proposer authorization written. Blocked at paid stages pending
Peter's approvals (see §6).

## 1. Provisional labels (stand-ins until HAR-81 seals)

- Split: PROVISIONAL salt `har85-provisional-20260928`, terminal heldout_count
  16 (train 48). Manifest `split.provisional.json`
  (manifest_digest `sha256:fb645f…52dab`, byte-identical to the parent-frozen
  copy). Rule: per-domain rank by `sha256(salt + "\0" + task_id)`, first 16
  held out; terminal split_group == task_id.
- Student: PROVISIONAL `SecretSafeTerminus2` + `ZAI_OPENAPI_MODEL_SELECTOR`
  (`zai/glm-5.3-flash`), stock settings, profile
  `terminus-2-glm-5.3-flash`. Single swappable parameter:
  `target.base_spec_path` →
  `base-specs/student-terminus2-provisional.json`.
- Candidate artifact: agent-side instruction addendum ONLY (Harbor
  `extra_instruction_path`, `candidate_kind=instructions`). Task files, tests,
  and task instructions untouched.
- MiMo facts: no task ships `solution/` (nop is the only control); setup runs
  in `[environment.healthcheck]`; verifier timeout 240 s; terminal graders are
  deterministic pytest (no judge).

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
| `gepa-nop-candidate-0260-secur-8193bf44728c0c887a9e2de6` | candidate-0260 | 0 | ~7 s | completed, reward 0, no exception |

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

- `split.provisional.json` (frozen copy), `selection-rule.json` (preregistered
  critical-W sign-test table + no-infra-loss + generality rule; criterion (1)
  amended same-day before any paid trial), `BUDGET.md` (formula with measured
  counts, $ as placeholders).
- `candidates/seed-addendum-v1.txt` (general guidance, no task specifics).
- `qualification-campaign.json` (dry-run source; outputs under
  `runs/gepa-har85-mimo-nop-qualification/`, runtime state, uncommitted).
- `base-specs/student-terminus2-provisional.json` (retained student route;
  committed source, NEVER submitted/approved here).
- `campaign-train.json` (STAGED search: 8 train examples, `target` → base
  spec, opencode Flash proposer option A; validated by `load_campaign`,
  never run).
- `proposer-options.json` (A/B/C: 4/8/2 reflection calls; stamp fields +
  fresh approval ref to switch).
- `proposer-approval.template.json` (STAGED binding `becdc632…`, signer/date
  blank; signed materialization is runtime state, gitignored).
- `search-round.sh` (per-round helper: no-args list + exact approve line,
  `--dispatch --ref` gated tick + campaign rerun; refusals exercised).
- `make_paired_specs.py` (final 32 held-out specs generator; refuses on
  drift/missing bytes; refusal + schema validation exercised).
- `paired-specs/` (empty until the winner exists; `ids.txt` recorded at submit).
- `run-after-approval.sh` (32/32 approval gate + key-scoped tick; bash-3.2
  safe; refusals exercised: missing `ids.txt` and 0/32 approved, both exit 2).
- `tasks/` (worktree-local, gitignored train materialization; held-out NEVER
  materialized here until the final eval).

## 5. Reproduce the task bytes (worktree-local, gitignored)

```bash
EXP=research/experiments/har85-mimo-gepa
SRC=/Users/petermakhnatch/Developer/.sources/mimo/terminal@fe1c2b66/tasks
# train split (48 tasks; asserts digests against split.provisional.json):
uv run --no-sync python -c "
import json, shutil
from pathlib import Path
from evallab.registry import task_directory_digest
m = json.load(open('$EXP/split.provisional.json'))
for row in m['tasks']:
    if row['assignment'] != 'train': continue
    d = Path('$EXP/tasks') / row['task_id']
    if not d.exists(): shutil.copytree(Path('$SRC') / row['task_id'], d)
    assert task_directory_digest(d) == row['task_package_digest'], row['task_id']
print('train materialized, digests match')
"
# held-out (16 tasks) ONLY at final-eval time, same pattern over heldout_task_ids.
```

## 6. Launch sequence (Peter's commands, from a clean eval-lab worktree at main)

The scripts resolve the repository root from their own location, so run them
from any clean worktree containing this revision (never the dirty primary
checkout). `.worktrees/har85-gepa-mimo` is left prepared for this.

PREREQUISITES (once per worktree, $0):
```bash
uv sync --locked
uv pip install -r research/experiments/harness-gepa/requirements.txt   # pinned GEPA runtime (verify_release)
# then run the §5 recipe to materialize the 48 train tasks
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
output_dir); `--dispatch` refuses unless every listed spec is approved, ticks
only those IDs with the key loaded non-printing, then reruns the campaign.
The signed ref is runtime state (gitignored), never committed.
The `--proposer-approval-ref` JSON must carry `binding_sha256` of the exact
frozen campaign binding + `approved_by` + `approved_at` (operator: Peter).
The staged binding (`proposer-approval.template.json`) was precomputed with
the real loader/pin/hash (`load_campaign`, `verify_release`,
`hashlib.sha256(json.dumps(binding, sort_keys=True))` per workflow.py:592-606):
`becdc632c2ba575b195069c8976cadd252a723e1b23e3f63f65716a8df2a2990`.
Recompute after ANY byte change to `campaign-train.json`, the seed, or the
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
uv run python research/experiments/har85-mimo-gepa/make_paired_specs.py \
  --winner <reviewed-winner-path> --winner-sha256 sha256:<64hex>
# submit paired-specs/ (parks 32 specs), record the 32 queue IDs in paired-specs/ids.txt
uv run evallab approve <SPEC_ID> --actor peter   # x 32
./research/experiments/har85-mimo-gepa/run-after-approval.sh
```
Then apply `selection-rule.json` verbatim (sign test via `src/evallab/power.py`,
paired analysis via `src/evallab/gepa_optimizer/paired_analysis.py`,
trajectory generality check); retain the seed unless ALL conditions hold.

## 7. Swap to HAR-81's sealed split/route

- Split: replace `split.provisional.json` with HAR-81's sealed manifest,
  re-run the §5 recipe (train ids), regenerate; `make_paired_specs.py` refuses
  on digest mismatch (pinned manifest digest) until updated.
- Student: add the Qwen-on-Tinker retained spec under `base-specs/`, point
  `campaign-train.json` `target.base_spec_path` at it; the route stays one
  parameter. Cost placeholders in BUDGET.md move with the new route.

## Limits

- No paid call of any kind was made; all trial evidence is nop controls plus
  staged (unapproved) artifacts. The staged per-trial ceilings ($2.00) and
  estimates ($0.25) are genuine caps/estimates, not measurements.
- 3-task dry run: proves the loop mechanics on real MiMo packages, not search
  quality. The 8-task search config is staged, not run.
- Proposer route is OpenCode-Flash only; any other reflection model needs its
  own qualification + fresh approval ref.
