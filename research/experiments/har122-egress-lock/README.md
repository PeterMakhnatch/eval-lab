# HAR-122 part 1: a leak block the agent cannot undo

**Question.** HAR-113 closed the PyPI answer leak by adding the PyPI hosts to each task's
blocklist, which the harness appends to `/etc/hosts` after setup. The agent runs as root, so
it can edit `/etc/hosts` back. Can the block be enforced outside the agent's reach, after
setup, without breaking grading?

**Answer.** Yes, on Daytona. `evallab.harbor_daytona:BoundedDaytonaEnvironment` takes a new
opt-in `egress_lock` option (off by default). When it is on, the environment blocks all
outbound traffic from the sandbox through Daytona's runner-side firewall once agent setup has
finished. On both tasks, a root probe that rewrote `/etc/hosts` could no longer reach PyPI,
two mirrors, raw IPs, GitHub, public DNS or the Daytona API. The verifier failed exactly the
same tests as HAR-113's nop.

## Mechanism

- **Daytona network limits**
  ([docs](https://www.daytona.io/docs/en/network-limits/), "Update network settings while a
  sandbox is running").
  - `update_network_settings(network_block_all=True)` changes the firewall of a *running*
    sandbox. The rule lives on the runner, not inside the sandbox.
  - Under block-all, essential services (PyPI, GitHub, npm, …) are not exempt.
  - Only Tier 3 and 4 organizations can override at sandbox level; Tier 1 and 2 get an API
    error. Our organization can: a bare sandbox reached example.com before the call and timed
    out resolving pypi.org after it, while Daytona's own exec channel kept working
    (2026-09-30, about 15 s of sandbox time).
- **When the lock is taken.** Task setup (the healthcheck) and the agent's install keep the
  network. The Terminus install runs `apt-get install tmux asciinema`.
  - Harbor 0.21 runs `agent.setup()` inside the first top-level `scoped_exec_env`
    (`trial.py` `_setup_agent`).
  - When that scope exits, the environment marks the lock as due.
  - The next `exec` takes the lock before it runs. That is the agent's first command,
    whichever agent runs; with an agent that never executes, it is the verifier's first
    command.
  - If Daytona refuses or does not confirm, that `exec` raises, so the agent never runs
    unlocked.
  - The lock is recorded in `<trial>/egress-lock.json` (time, sandbox id, confirmed
    `network_block_all`).
- **It stays locked through verification.** The environment refuses later network-policy
  changes. It rejects:
  - tasks whose `task.toml` declares phase network policies (Harbor would restore the
    baseline after the phase);
  - compose tasks;
  - `SecretSafeDaytonaEnvironment`, whose in-sandbox model proxy needs egress.
- **Why not Harbor's native phase policy?** Harbor 0.21 can switch Daytona's network per
  phase from `task.toml` (`[agent] network_mode`). Using it would mean rewriting every task
  package or the runner's staging, and Harbor restores the baseline before verification.
  The environment option changes neither and stays on through grading.

Enable it with `--environment-kwarg egress_lock=true` on `harbor run`. The lab's queue does
not emit it yet: the runner is being changed for HAR-116, and HAR-116 and HAR-120 decide
whether to adopt this. Their environment is unchanged.

## Proof (no model, 2026-09-30)

`run_probe.py` copies each task's HAR-113 leak-closed variant, adds
`solution/solve.sh` = `probe_solve.sh`, and runs Harbor's oracle agent on Daytona twice:
once with `egress_lock=true` and once without (the control). Nothing else differs.

The probe runs as root after setup. It:
1. applies the blocklist as the harness does;
2. deletes every blocklist line and pins pypi.org's real address in `/etc/hosts`;
3. tries pip against PyPI and two mirror indexes, HTTPS to a pinned IP and to bare IPs,
   `git ls-remote` to GitHub, raw TCP, IPv6, DNS and the Daytona API;
4. puts `/etc/hosts` back.

It never touches `/testbed`.

| | 000226 lock | 000226 open | 000927 lock | 000927 open |
|---|---|---|---|---|
| `/etc/hosts` after edit | 0 blocked, PyPI pinned | same | same | same |
| `pip download` (PyPI, pinned) | **exit 1** | exit 0, `waitress-1.3.1` | **exit 1** | exit 0, `soupsieve-1.9` |
| pip, mirror tuna / aliyun | **exit 1 / exit 1** | exit 0 / exit 0 | **exit 1 / exit 1** | exit 0 / exit 0 |
| HTTPS pypi.org via pinned IP | **no connection** | 200 | **no connection** | 200 |
| TCP to that IP:443, 1.1.1.1:443, 8.8.8.8:53 | **refused** | connected | **refused** | connected |
| `git ls-remote` GitHub | **fails** | HEAD returned | **fails** | HEAD returned |
| DNS (example.com) | **fails** | resolves | **fails** | resolves |
| `https://app.daytona.io` | **no connection** | 200 | **no connection** | 200 |
| IPv6 | unreachable | unreachable | unreachable | unreachable |
| verifier | 13 run, 6 failures | same | 22 failed | same |
| equals HAR-113 nop (`har113-vnop-*`), failing test ids | identical | identical | identical | identical |
| `egress-lock.json` | `network_block_all: true` | none | `network_block_all: true` | none |

**Can the agent lift the lock?**
- The sandbox exposes only these Daytona variables: `ORGANIZATION_ID`, `REGION_ID`,
  `SANDBOX_ID`, `SANDBOX_SNAPSHOT`, `SANDBOX_USER`, `OTEL_ENDPOINT`,
  `AMD_KFD_TOPOLOGY_WAIT`, `USER_HOME_AS_WORKDIR`. None is a credential.
- The Daytona API itself is unreachable under the lock.
- Without the lock, a root agent undoes the blocklist with a two-line `/etc/hosts` edit
  (control column).

**Jobs.** `runs/har122-probe-{000226,000927}-{lock,open}`, plus `runs/har122-probe-report.json`
(the `report` output), in the locked worktree `.worktrees/har122-egress-evidence` (gitignored
run data).
- These jobs are a re-run from merged `main` (#585). They reproduced the table above exactly:
  every probe line, the rewards, and the failing test ids.
- Two earlier attempts on the PR branch were lost when that worktree was removed. In the
  first, the probe's `/etc/hosts` edit used `sed -i`, which cannot rename over the
  bind-mounted file, so the blocklist lines stayed in place. Its unlocked arm downloaded
  anyway, because Debian's `multi on` returns both the `0.0.0.0` line and the pinned
  address.

**Spend.** About $0.05 of Daytona over the three attempts: 800 s of trial time at 2 vCPU /
8 GiB on the list-price rate card, plus about 15 s for the tier check. Cap $0.50. No model
calls.

## What the block cuts off

Everything outbound, for the agent and the verifier. What an agent might legitimately need:

- **Installing a dependency the image lacks.** Measured over the 56 HAR-81/HAR-104
  Terminus-2 MiMo trials:
  - 7 trials ran network commands.
  - Only one pass depended on the network: `candidate-2684` (bandit) ran
    `pip3 install stevedore`, which bandit imports at runtime.
  - Three `candidate-1789` trials also tried to install stevedore and failed the task anyway.
  - Under the lock, 2684's pass would most likely have been a fail. That is an image defect
    (the dependency is missing), not a reason to keep the network: such tasks should be
    repaired, as HAR-113 repaired 51.
- **Reading upstream source or docs** (`git clone`, `git ls-remote`). Already blocked in
  these waves: GitHub is on the task blocklist. 2 trials tried; neither depended on it.
- **Fetching fixtures, datasets or models.** None observed.
- **Grading.** HAR-113 found no PyPI need at test time across the 247 leak variants. Here,
  both tasks grade identically under the lock.

**Solvability.** For the MiMo Python code tasks whose image already carries every dependency
the tests import (HAR-108 `sound`), the lock should not change solvability [INFERENCE: two
tasks probed, 56 trials surveyed]. Tasks that need an install at solve time become unsolvable
under the lock; this is the price of a block that cannot be undone.

## Limits

- **Agents exercised.** The lock was run live only under Harbor's oracle agent. The order it
  relies on (setup inside the first top-level `scoped_exec_env`, then the first `exec` of
  the run) is Harbor 0.21's for every agent. Terminus-2 itself was not run locked here,
  since that needs a model.
- **Daytona only, single-container tasks only.** Docker and other backends are untouched.
- **Not in the setup key.** Data's `trial_treatment` key reads agent kwargs, not environment
  kwargs, so a locked trial would pool with unlocked ones. An adopting card should add
  `egress_lock` to the key, or keep locked runs under their own names.

## Reproduce

```bash
keys run -- uv run python research/experiments/har122-egress-lock/run_probe.py run
uv run python research/experiments/har122-egress-lock/run_probe.py report
```
