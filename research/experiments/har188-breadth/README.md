# HAR-188 breadth cohort: staged campaign (two budget variants, UNAPPROVED)

Staged for HAR-175 approval. Both variants are frozen below and **not
approved**: approve exactly one (or neither) with the commands at the end.

- Cohort: `breadth-cohort-tasks.txt` (copy of `/tmp/breadth-cohort-tasks.txt`,
  100 IDs, seed 20261007). Variant (a) `har188-breadth-n50.json` stages the
  first 50 (nested N=50 cohort); variant (b) `har188-breadth-n18.json` stages
  the first 18 (nested prefix, N=18: the largest N in 15-20 with expected <=
  $5 under the wave model, see cost table).
- Setup: frozen `xiaomi-mimo-rl` reference, agent `mimoagent`, model
  `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`, `daytona`, egress lock
  required. Deviations, sampling (1.0/0.95/20) and ceiling floor reuse the
  validated HAR-168 draft (`/private/tmp/har175/har168-campaign.json`).
- Images/packages: HAR-177 default `strip-future-history` variants.
  `variant-pins.json` records the per-task package digest (campaign pin),
  verifier digest (computed from the materialized variant with repo code),
  image digest, record path, status and transform chain, resolved at staging.
- Attempts: x2 per task (100 / 40 trials). One warm model server runs the
  batch concurrently. No serialised smoke run: tick with `--no-smoke-gate`
  (recorded as `smoke_gate_disabled`; 24 qualified HAR-168 cells already
  prove this setup).
- Standing HAR-175 rules in force: one replacement per infra failure;
  context exhaustion is a counted non-pass (HAR-182, no replacement);
  budget exhaustion stops new launches via the STOP fence, running trials
  finish untouched.

## Cost (HAR-168 actuals: GPU $2.90/h, Daytona $0.23148/h/sandbox, fresh 4-run batch $1.28)

One warm server runs the batch in waves of at most 19 concurrent sandboxes
(HAR-163 Daytona clamp `floor((limit*safety-used-pending-reserve)/per_sandbox)`
in `dispatch_guards.daytona_tick_allowance`: 19 on an idle account =
(200*0.8-0-0-8)/8, lower whenever the account is busy) while the server
stays warm for the whole wall time. With `T = 1.28/(2.90+4*0.23148) =
0.3346h` effective batch wall from the HAR-168 fresh 4-run batch and `w =
ceil(trials/19)` waves: `E = (4/60h + w*T)*$2.90/h + trials*T*$0.23148/h`.
Caveat: `T` was measured at 4 concurrent trials; whether 19 trials on one
server decode slower is unknown (HAR-168 records no per-trial wall times or
server throughput). Worst-case is every trial billing its $0.60 per-trial
ceiling; realized spend is fenced by the standing budget gate at the budget
plus at most one wave of in-flight ceilings (`fenced_spend_bound`). ($1.28
is a measured-time upper from the HAR-168 closure receipt, invoices UNKNOWN.)

| variant | tasks | trials | waves | expected | worst-case | realized <= | budget |
|---|---|---|---|---|---|---|---|
| (a) n50 | 50 | 100 | 6 | $13.76 | $60.00 | $41.40 | $30.00 |
| (b) n18 | 18 | 36 | 2 | $4.92 | $21.60 | $16.40 | $5.00 |

N scan for variant (b), expected with cold start: N=15 $4.46, N=16 $4.61,
N=17 $4.77, N=18 $4.92, N=19 $5.08, N=20 $6.20 (3 waves). N=18 is the
largest N in 15-20 with expected <= $5.

## Verification findings (cohort: 100 unique IDs, all ledger `usable` + verdict `keep`)

- 15 heldout-split tasks, staged as specified (not dropped): index 9
  (003053), 16 (002143), 39 (000087), 48 (000585), 49 (001199), 50
  (000145), 54 (002015), 58 (000211), 59 (001614), 62 (003035), 63
  (000939), 84 (000655), 88 (000771), 92 (000847), 93 (001369). Six sit
  in the N=50 cohort (two in N=18). Research-Harbor rules whether heldout
  tasks may run in a breadth (train-style) campaign before approval.
- 1 previously-run task: index 53 (000311) has one history trial
  (2026-10-02 HAR-157 base, infra only, 0 clean passes). Not never-run;
  staged as specified, flagged here.
- All 100 strips chain to their ledger `run_digest`, but the reference
  gate (`setup_fingerprint.lineage_ledger_binding`, allowlist
  `rewardkit-integrity@1` + `task-health-tags@1`) refuses them — see
  blocker below.

## Blocker: strip variants do not pass the reference gate (no gate weakened)

99/100 staged strips are record status `candidate`; 1 (000149) is
`validated`. Even the validated one is refused:

- candidate: `variant record library/... is 'candidate', not 'validated':
  validate its locked nop first`
- validated: `variant transform 'strip-future-history@1' is not
  verifier-only/metadata-only (allowlist: ['rewardkit-integrity@1',
  'task-health-tags@1'])`

What would legitimately admit them (HAR-177 owner call, not this card):
locked-nop validation promoting each strip record to `validated` with
evidence, **plus** an explicit admission for the environment-changing
transform — e.g. a ledger refresh promoting the strip digests to
`run_digest` (direct `ledger_match`, no lineage needed), or a
reference-gate-owner admission with grading-equivalence evidence.
Until then the campaign gate admits matching specs but dispatch refuses
at the reference gate.

## HAR-180 dependency

Per-trial file-access log (`agent/file-access.jsonl`) is on main but OFF
by default: env flag `EVALLAB_FILE_ACCESS=1` (`src/evallab/file_access.py`;
plugin `src/evallab/harbor_file_access.py`). No config setting exists;
Infra activates per approved campaign at tick
(`docs/live-watch.md:77-85`). Launch needs
`EVALLAB_FILE_ACCESS=1 ... evallab tick ...`, otherwise the log records
an explicit disabled marker.

## Approve (Research-Harbor only; never approve from this staging card)

Queue cwd is this worktree; it must still exist at approve/tick time:

```
cd /Users/petermakhnatch/Developer/eval-lab/.worktrees/har188
uv run evallab campaign validate research/experiments/har188-breadth/har188-breadth-n50.json
uv run evallab campaign approve research/experiments/har188-breadth/har188-breadth-n50.json --actor research-harbor
```

Variant (b): same two commands with `har188-breadth-n18.json`.
Launch (after approval): submit specs with `campaign_id` set, then
`EVALLAB_FILE_ACCESS=1 evallab tick --spec-id <approved...> --no-smoke-gate`
(reason: 24 qualified HAR-168 cells already prove this setup).
