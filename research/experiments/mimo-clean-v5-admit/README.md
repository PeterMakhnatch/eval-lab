# mimo-clean-v5 admit receipt (overnight 2026-10-13, local Docker $0)

Owner: AdmitOvernight. Runner pin: worktree detached at origin/main
`15f34e0a913000e496910f2cbdfc1e16dbdfb983` (branch `admit-overnight`, never
rebased). Ladder `CHEAT_AGENT_VERSION = "1.4.0"` (`src/evallab/cheat_ladder.py`),
gate `GATE_VERSION = "tasks.admit/v2"`, schema `evallab.task_admission/v1`.
No small fix PR was needed before the run (dry-run below succeeded); the run
itself exposed two blockers (open issues) and produced zero trust evidence.

Scope: all 564 manifest `reference_fix` rows minus 6 excluded-from-training
(`fail:open-leak`: 000928, 001478, 001671, 001945, 001971, 002414) = 558 tasks,
seeded stratified order (seed `20261013`: per-language shuffle, go/javascript/
typescript/python heads first, then proportional interleave). Full order +
per-task package/reference/digest in `sampling.json`. Canonical manifest:
`research/experiments/mimo-clean-v5/manifest.csv`.

## Dry-run (step-1 confirmation)

`evallab tasks admit --task derived/task-store/variants/mimo-v2.6-rl__format-code-task-000045/93af5cf735d3 --reference <manifest reference_fix patch> --dry-run --json`
returns 19 steps: `static-scan`, `oracle` (expect 1.0), `nop` (expect 0.0),
14 `cheat:<attack>` singles (full 1.4.0 ladder), `battery`, `mutation`
(skipped at runtime via `--skip-mutation`). `--reference <patch>` is consumed
verbatim as an explicit reference (record shows `fix=explicit`); grading runs
the v5 package's own bundled verifier (separate-verifier@8 chain for every
row incl. 001431 — no `@9` exists in this tree). `--no-variant-status` used
throughout, so lineage was never touched.

Runtime command per task:
`uv run --extra laminar evallab tasks admit --task <package> --reference
<manifest reference_fix patch> --skip-mutation --no-variant-status
--timeout-seconds 2400 --jobs-dir
~/Developer/eval-lab-results/2026-10-13/mimo-clean-v5-admit --by
admit-overnight --json`, 2 concurrent, `docker rmi` of newly pulled images
after each task.

## What ran (stopped early per parent order: fix over check)

88 of 558 tasks ran (first 88 of `sampling.json` order: 68 python,
9 javascript, 7 typescript, 4 go). Verdicts: **0 admit, 0 reject,
88 unproven** — every gate stopped at `oracle: infra`, `static-scan: pass`.
`trusted-tasks.csv` lists all 88 (copies of the 88 admission JSONs in
`records/`; raw job dirs under
`~/Developer/eval-lab-results/2026-10-13/mimo-clean-v5-admit/`).
Retries: one solo retry per task, all failed identically (staging collision,
see issue 3). Nothing here is trust evidence; all 558 still need valid admits.

## Open issues (no transform or manifest change made)

1. `oracle control broke: ValueError: Either datasets or tasks must be
   provided` (harbor 0.24.0 in the pinned venv). The gate reached the oracle
   step with a resolved explicit reference, then harbor ran with no usable
   task (`job_dir: null`, `rewards: []`, 1.2 s, no Docker). Suspected
   arg-forwarding skew between `run_stability_jobs` and the installed Harbor
   CLI, not a task verdict. Needs a gate-owner repro on one package.
2. The runner's worktree package copy was incomplete: `rsync --files-from`
   dropped the packages' symlinked members (`environment/Dockerfile`,
   `environment/setup`, `solution/solve.sh`, `tests/*`), so the worktree
   `task_directory_digest` (e.g. 000245: `39696ceb…`) mismatched the manifest
   `final_digest` (`e35ee80b…`; primary checkout recomputes `e35ee80b…`
   exactly). Fix: copy with symlink-preserving full-tree transfer and assert
   `task_directory_digest == final_digest` before any future run.
3. Runner retry artifact (mine, not the gate's): re-admitting the same task
   into the same jobs dir fails at oracle staging with `staging destination
   already exists`. Future retries need a fresh `--job-prefix`/jobs dir
   (or gate-side staging cleanup).

## Modal plan (brief; not run)

Assumptions: Modal per-trial rates from the v5 census receipt
(`research/experiments/mimo-clean-v5-census/README.md` + `spend.jsonl`):
ladder $0.006740/task (`v5-ladder250-limit` $1.6851/250 dispatched); controls
$0.0037–0.0066/trial (`v5-sample40-limit` $0.2944/80 cells;
`v5-nop35-limit` $0.2304/35). Per admit ≈ 6 control trials (repeat 3:
oracle 3 + nop 3) × ~$0.005 + 1 ladder × $0.00674 ≈ $0.037, +15% retry
margin ≈ **$0.043/admit → ~$24 for all 558** (battery runs local-Docker
per current code; if ported to Modal, add its trials). Ladder-only census of
the 1,679 `unverified:ladder-missing` tasks: 1679 × $0.00674 ≈ **$11.32**.
Commands (flags per `docs/mimo/verification.md` + v5 receipt; check `--help`
before launch): `evallab mimo-census run --checks ladder … --backend modal
--modal-resource-policy limit --timeout-seconds 900` (24-wide, billing app
`mimo-clean-census`), then `evallab mimo-census report` and
`evallab mimo-census record-spend` per batch; approval actor must be a fresh
Peter approval (`--actor` never `peter`, never assumed from the $25 programme
cap). Full admits on Modal have no established command — the gate is a
local-Docker path today; do not price it as census cells without a parity
trial first.

## Manifest `verify` column

Untouched. Admit evidence cannot fill it under the receipt's fill rules
(no passing cells exist), so all rows keep their census labels.

## Spend

$0 (local Docker only; no pulls landed — newest daemon image predates the
run; no ledger entry made).
