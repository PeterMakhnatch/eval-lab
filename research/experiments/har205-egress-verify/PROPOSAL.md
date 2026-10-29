# HAR-205 proposal: live five-sandbox egress-verification battery

**Status: UNAPPROVED.** A proposal for Peter. Nothing here has been launched: no Daytona
sandbox was created and no provider API was called to produce it. It authorizes no spend;
`policy/standing-approvals.yaml` is unchanged and old run caps do not cover it.

## What is already done ($0, merged with this document)

`evallab.harbor_daytona.BoundedDaytonaEnvironment._lock_egress` no longer trusts the
provider's acknowledgement. After `update_network_settings(network_block_all=True)` it runs
the probe plan in `src/evallab/egress_verification.py` from inside the sandbox and starts
the agent phase only if the block is shown to hold. `egress-lock.json` (schema 3) keeps
`applied` (the provider acknowledged) apart from `verified`, with per-probe outcomes and the
runner identity the SDK reports. Done-when item 1 is met by code and fake-sandbox tests.

Items 2-4 of the card need live sandboxes and stay open:

| Done-when | State |
|---|---|
| 1. agent phase refuses to start unless the block is verified; durable result | code + tests |
| 2. at least five approved sandboxes check IPv4, IPv6, DNS; runner identity recorded | **open: this proposal** |
| 3. correct/no-op controls and an unlocked positive control separate a working block from a broken network or probe | **open: this proposal** |
| 4. the IPv6 explanation stays a hypothesis until the checks establish it | **open** (stated as a hypothesis everywhere in code and docs) |

## The probe plan (identical in code and in this battery)

Eight commands run from inside the sandbox, concurrently, each a separate record:

| probe | what it does | blocked looks like |
|---|---|---|
| `control_loopback_tcp` | `bash` `/dev/tcp/127.0.0.1/9` | refused (proves `timeout`, `bash`, `/dev/tcp` work) |
| `control_local_dns` | `getent hosts localhost` | resolves from local files (proves `getent`) |
| `dns_a`, `dns_aaaa` | `getent ahostsv4/ahostsv6 example.com` | no answer or timeout |
| `tcp4_cloudflare`, `tcp4_google` | TCP connect `1.1.1.1:443`, `8.8.8.8:443` | timeout, refused, or unreachable |
| `tcp6_cloudflare`, `tcp6_google` | TCP connect `[2606:4700:4700::1111]:443`, `[2001:4860:4860::8888]:443` | timeout, refused, or unreachable |

Verdict: verified only if both controls pass and all six network probes are blocked. A
reachable probe, a missing tool, an exec error, a hang, or an exit status the classifier does
not recognise leaves the lock unverified and the trial ends as `infra`. The canary names avoid
the 32-host `/etc/hosts` answer-leak blocklist so a hosts entry cannot mask a leak.

## Battery design

Task: `library/tasks/event-summary` (Debian bookworm image with `bash`, `timeout`, `getent`;
1 vCPU, 512 MB; no network dependence; not a MiMo task, so an unlocked arm is admissible, which
a MiMo Daytona run refuses by design). All arms use `--environment daytona` through
`evallab run`, so every arm records `egress-lock.json`.

| arm | n | agent | lock | what it shows |
|---|---|---|---|---|
| L-oracle | 3 | `oracle` | locked | correct run: probes run before the agent phase, then grading passes |
| L-nop | 2 | `nop` | locked | no-agent run: the lock + probes land on the verifier's first exec |
| U-probe | 2 | `oracle` with a probe `solve.sh` | **unlocked** | positive control: the same eight commands, network open |

That is 5 locked sandboxes (the card's floor) plus 2 unlocked positive controls: 7 trials.

How the controls separate failure modes:

* **Broken probe** (tool missing, `/dev/tcp` absent): the two loopback controls fail inside
  every locked sandbox, so the record says `probe tooling unusable`, not "blocked".
* **Broken network** versus **working block**: the U-probe arm must show IPv4 TCP and DNS
  reachable. If it does not, the probe or the runner network is broken and no locked result
  counts (stop rule 2).
* **IPv6 caveat.** The HAR-122 control reported IPv6 as *unreachable* even unlocked. If the
  U-probe arm shows the same, those runners have no routable IPv6 and a v6 leak cannot occur
  there; the battery then cannot test the IPv6 hypothesis, and says so rather than confirming it.
* **Runner identity**: `verification.runner` records the SDK's `runner_id`, `target`,
  `snapshot`, `sandbox_class`, `daemon_version` and whether an outbound proxy is configured.
  Launch in three waves at least 10 minutes apart to give the scheduler a chance to place
  sandboxes on different runners; the battery cannot choose runners.

Unlocked arm `solve.sh` (generated from the same plan, copied with the task under
`derived/har205-probe/`, never into `library/`):

```bash
mkdir -p derived/har205-probe && cp -R library/tasks/event-summary derived/har205-probe/event-summary
chmod -R u+w derived/har205-probe/event-summary
uv run python -c '
from evallab.egress_verification import PROBES
print("#!/bin/bash")
for p in PROBES:
    print(f"echo \"== {p.name} {p.target}\"; {p.command}; echo \"rc=$?\"")
' > derived/har205-probe/event-summary/solution/solve.sh
chmod 755 derived/har205-probe/event-summary/solution/solve.sh
```

## Commands that WOULD run it (not run)

Admissibility of the three shapes was checked offline with `validate_request`/`build_command`
(no launch). Wave 1 shown; waves 2 and 3 repeat with new names.

```bash
keys run -- uv run evallab run --task library/tasks/event-summary --agent oracle \
  --environment daytona --egress-lock --timeout-seconds 300 --name har205-lock-oracle-1
keys run -- uv run evallab run --task library/tasks/event-summary --agent nop \
  --environment daytona --egress-lock --timeout-seconds 300 --name har205-lock-nop-1
keys run -- uv run evallab run --task derived/har205-probe/event-summary --agent oracle \
  --environment daytona --no-egress-lock --timeout-seconds 300 --name har205-open-probe-1
```

Plan: wave 1 = lock-oracle-1, lock-nop-1, open-probe-1; wave 2 = lock-oracle-2, lock-nop-2,
open-probe-2; wave 3 = lock-oracle-3. One at a time (`--concurrency 1` default), so the
Daytona admission guard sees at most one of our sandboxes (two if the task's separate
verifier starts its own).

Read-out per trial: `runs/<name>/**/egress-lock.json` (`verified`, `verification.reason`,
`verification.probes`, `verification.runner`) and, for U-probe, the oracle agent's log under the
trial's `agent/` directory (each `== name` block with its `rc=`; classify with
`evallab.egress_verification.classify`).

## Cost, basis and hard cap

Basis (measured, this repo): the Daytona rate card in `task_qualification.DAYTONA_RATE_CARD`
(list prices retrieved 2026-09-28: vCPU \$0.0504/h, memory \$0.0162/GiB-h, storage free under
5 GiB). `research/experiments/har122-egress-lock/README.md` measured about 154 s per probe
trial (HAR-140: 2 trials, \$0.0197 at 2 vCPU / 8 GiB) and 55 s per nop (563 nops, \$1.99).

* This task bills 1 vCPU + 1 GiB (memory rounds up to whole GiB): \$0.0666/h = **\$0.00111 per
  sandbox-minute**.
* Per trial, assuming 4 min for the agent sandbox (cold-ish start plus the lock, up to 8 s of
  probe wait, grading) and a second sandbox for the task's `separate` verifier at 2 min
  (assumption, not verified: whether Harbor 0.21/0.24 starts one on Daytona): **6
  sandbox-minutes**.
* 7 trials = 42 sandbox-minutes, plus 5 minutes for the first image build:
  **47 sandbox-minutes, about \$0.05 nominal.**
* Worst case, bounded by the per-trial watchdog (`--timeout-seconds 300` + 600 s allowance = 15
  min) on two sandboxes: 7 × 15 × 2 = 210 sandbox-minutes = \$0.23.
* **Hard cap proposed: \$0.25 of Daytona, no model calls (oracle/nop/probe only).** The operator
  sums trial lifetimes at the rate card after every trial and does not start the next trial if
  committed plus its 15-minute worst case would exceed the cap (no launcher exists; this is a
  manual check against the stop rule below).

The estimate rests on list prices and a measured per-trial time from a different task; actual
Daytona billing (credits, rounding) may differ. It is an estimate, not a billed figure.

## Stop rule

Stop launching (do not retry, do not add sandboxes) at the first of:

1. committed spend plus the next trial's worst case would exceed \$0.25;
2. a U-probe sandbox shows IPv4 TCP **and** DNS blocked (the probe or the network is broken;
   locked results would be meaningless);
3. two locked sandboxes report `probe tooling unusable` or any `error` outcome (fix the probe
   first; more sandboxes only repeat the defect);
4. any sandbox is left in the Daytona inventory after its trial (leaked resource);
5. all seven planned trials have completed (the battery's stated size).

A **leak** (`reachable`) in a locked sandbox is a finding, not a stop condition: the trial
already failed closed; finish the planned trials and record it.

## What five locked sandboxes can and cannot establish

* They can show the probe plan separates a working block from a broken network/probe (via the
  positive control and loopback controls) and record runner identity per sandbox.
* They cannot estimate the miss rate. At the observed 2 of 14 (about 14%), five sandboxes show
  no leak with probability 0.86^5 ≈ 47%; zero leaks in five only bounds the true rate below
  about 45% at 95% confidence.
* They cannot establish the IPv6 cause. A v6 leak with v4/DNS blocked in some sandbox would
  support the hypothesis; v6 unreachable even unlocked would show the hypothesis untestable on
  those runners; no leak at all is uninformative. Until a leak is seen, v6 and any other path
  stay unattributed.
* The cheaper route to the miss rate is the fix itself: every locked Daytona trial now records
  `verification` with runner identity, so ordinary approved runs accumulate per-runner evidence
  at about one extra probe wait (at most ~8 s of sandbox time) per trial. Run this battery only
  if Peter wants the controls and a first runner sample before relying on that.

## Decision requested

Approve or decline a \$0.25 Daytona cap for this seven-trial battery. Default if no answer:
**park** (no launch).
