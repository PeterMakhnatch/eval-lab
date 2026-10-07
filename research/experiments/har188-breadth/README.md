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
  required. The source serving config now matches reference context 262,144
  and tool-call parser `qwen3_coder`; only HAR-164's `harness.additions`
  deviation remains. Sampling (1.0/0.95/20) and ceiling floor are unchanged.
  The `mimo` reasoning parser is retained: profile `server.reasoning_parser`
  is explicitly unsourced (Xiaomi's rollout configs pin no server parser).
  Source alignment is not deployment or authenticated live qualification.
- Images/packages: HAR-177 default `strip-future-history` variants.
  `variant-pins.json` records the per-task package digest (campaign pin),
  verifier digest (computed from the materialized variant with repo code),
  image digest, record path, status and transform chain, resolved at staging.
  Task packages, verifier/image digests and `variant-pins.json` are unchanged
  by this context/parser-only re-stage.
- Attempts: `attempts_per_task: 2` is the maximum per task (100 / 36
  trials); `concurrency: 19` preserves the shared warm-server waves. The
  HAR-193 policy pins confidence .95 / seed 20261007, stages one native draw
  per spec, and releases follow-ups only after a settled uncertain draw.
  First draws fill the priority wave; follow-ups precede unstarted tasks.
  Capacity, Daytona memory and HAR-189 reservations limit each wave; a
  budget stop leaves at most one wave partially informative. At x2/.95 this
  rule saves **zero attempts**: any gains are admission and ordering only.
  The prior `--no-smoke-gate` rationale used HAR-168's
  24 qualified cells with the old `mimo` parser and 65,536-token window.
  Those runs do not qualify this newly configured server: deployment and a
  fresh authenticated readiness/native-tools check require separate approval.
- Standing HAR-175 rules in force: one replacement per infra failure;
  context exhaustion is a counted non-pass (HAR-182, no replacement);
  budget exhaustion stops new launches via the STOP fence, running trials
  finish untouched.
- Oracle admission: **0/50 and 0/18 currently confirmed**. HAR-191's sweep
  is absent on the staging base; the committed pilots are only
  `000552=oracle:fail-network` and `001198=oracle:none`, neither in these
  cohorts. Nop-sound or model-history tags are not reference-solution
  confirmation. The explicit positive label needs its own package digest;
  no blank sweep digest is retargeted to a current ledger package. A later
  ledger-package label cannot validate the environment-changing strip
  variants through the existing scoring/metadata-only lineage gate.

Re-staged frozen content (unapproved; only `harness.additions` is declared):

| Campaign | Content digest |
|---|---|
| n50 | `sha256:6c2147d6cd71f17b189125c5e776495ee9be65d818972375c34a9bdf3b9a407f` |
| n18 | `sha256:9cc618ac7ecd6e1967af80fc502b0273a13ce5868e7e225ea2d83941ebd51662` |

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

The context-only change uses the same A100-80GB and hourly rates: no price
change. Longer trajectories may increase GPU time and sandbox time; this is
unmeasured, so the existing wave estimate is retained rather than presented
as a measured 262,144-token throughput estimate. SGLang's shared preallocated
KV pool is not a full-window reservation per sandbox. Nineteen simultaneous
full-window sequences require 152 GiB of KV and cannot fit; at realistic
lengths, pool pressure can queue or retract requests and lengthen the batch.
No memory-partition flags are added.

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

## HAR-178 legit predicate

The serving changes remove `server.context_length` and
`server.tool_call_parser` differences. However, HAR-178 uses **exact**
reference matching (`interpretation/trial_posture.py::_reference_match`),
not admission with declared deviations. HAR-164's retained harness adapters
still differ from reference `harness.additions={}` (profile cites
`example_configs/swe.yaml:3-24`). Consequently a new run with this staged
fingerprint is **not legit**, even with applied egress lock and no infra or
limit stop: `sql/trials.sql` requires `reference_profile_match=true`.
Neither that predicate nor the reference/lineage gates are weakened here.
This verdict is prospective, not evidence of a new trial.


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
Launch (after approval): stage one native attempt per spec (`attempts: 1`)
with `campaign_id` and `campaign_task_attempt: 1..2`, then run
`EVALLAB_FILE_ACCESS=1 evallab tick --parallel 19` for each priority wave.
The explicit executor parallel limit can narrow the campaign's 19-wide max;
do not batch two native attempts in one spec or pre-approve every draw.
Do not reuse the old `--no-smoke-gate` qualification for the changed parser/window.
