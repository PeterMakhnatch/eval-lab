# agent-network-none: task-level agent egress lock (V6)

Owner: NetworkPolicy. $0 (local Docker; Harbor 0.24.0 from the locked
`laminar` extra). No model calls, no paid compute.

## Why

V6 (fetching the upstream fix / newer release over the network) is closed
on Eval Lab's own runs only by the run-time egress lock. Harbor's Docker
egress sidecar is opt-in per task: when every declared policy is `public`
— the MiMo default; clean packages carry `[environment] network_mode =
"public"` and no phase override — no sidecar is attached at all
(`docker.py:_requires_egress_control`), so a clean package run by anyone
else (Docker/Modal) leaves V6 open. FineEnvs 1.3.0 only adds an
`/etc/hosts` blocklist that a root agent can rewrite (see the FineEnvs
audit page; not re-derived here).

## What

New transform `agent-network-none@1`
(`src/evallab/agent_network_policy.py`, registered in `hardening.py` as
`AGENT_NETWORK_NONE_ID`) adds exactly one declaration to a clean package:

```toml
[agent]
network_mode = "no-network"
```

An explicit *agent-phase* override (Harbor 0.24.0
`models/task/config.py:PhaseNetworkPolicyConfig`). Harbor resolves the
agent phase to it (`trial/network_policy.py:resolve_agent_phase_policy`)
and applies it around `agent.run()` only
(`trial.py:_phase_network_policy` → `set_network_policy`, baseline
restored after). The verifier is untouched — no `[verifier]` override, no
`[verifier.environment]` table — so under separate-verifier@3 the
verifier environment stays a fresh copy of the still-public
`[environment]` (`verifier_mode.resolve_effective_verifier_env_config`)
and the verifier phase falls back to that public baseline
(`resolve_verifier_phase_policy`). Environment setup/build keeps network;
only the agent window is locked.

`[environment] network_mode = "no-network"` was deliberately NOT used: the
separate-verifier baseline would inherit it (fresh copy of
`[environment]`), taking the verifier image build and setup offline and
breaking grade-time fetchers. The transform refuses second application,
`[agent] allowed_hosts` without a mode (would be an invalid Harbor
config), Windows targets (Harbor's Docker backend rejects non-public
policies there), and unparsable TOML — and proves the derived file parses
with only `[agent].network_mode` changed.

A Harbor-level resolution test (`test_harbor_plan_locks_agent_and_keeps_verifier_public`)
runs the derived task.toml through Harbor 0.24's own
`resolve_trial_network_plan` under separate mode: agent phase `no-network`,
verifier phase `public`.

## Backend enforcement (Harbor 0.24.0, all fail closed)

| Backend | `no-network` mechanism | Caveat |
|---|---|---|
| Docker (Linux) | egress sidecar (`network_mode: service:sidecar`); `deny-all` = nftables TCP redirect to a transparent proxy with an empty allowlist (`bin/network-policy`) | nftables `output` (nat) redirects every TCP packet except marked/local/DNS-to-resolver to gost `:12345`; the `egress` (filter) chain accepts marked, local, DNS (UDP+TCP 53 to `/etc/resolv.conf` nameservers only), and ICMP/ICMPv6, and rejects all other non-TCP. So names resolve and pings pass; no file can be fetched over TCP. A UDP-53 DNS-tunneling channel remains by design (residual 3 below) |
| Daytona | `network_block_all=True` at create (`_create_network_kwargs`/`_network_kwargs`: `NO_NETWORK` → `{"network_block_all": True}`) + phase switch via `_apply_network_policy` → `update_network_settings` | phase switch needs `dynamic_network_policy`, which is `False` in compose (DinD) mode — the switch raises instead of running open |
| Modal | `NO_NETWORK` → empty `outbound_domain_allowlist` + `outbound_cidr_allowlist` (`modal.py:_network_kwargs`) | compose mode sets `disable_internet=False` and `dynamic_network_policy=False` (`_compose_mode` = `environment/docker-compose.yaml` exists or extra compose paths), so `base.py:validate_network_policy_support` raises `ValueError` at startup — fail closed. MiMo packages ship only `environment/Dockerfile`, so `compose_mode` is False and the lock applies. Non-compose phase switching works via `dynamic_network_policy` |

Phase policies are validated at startup, not just at switch time
(`base.py:_validate_network_policy_support` checks every phase policy),
so an unsupported agent-phase lock rejects the task before the agent
ever runs.

Note: Eval Lab's host staging adapter (`harbor_network.adapt_task_toml_for_host`)
tracks only `[environment]` / `[verifier.environment]` / `[verifier]`, so the
`[agent]` override passes staging byte-identical on every host (verified on
Darwin: `changed: False, adaptation: None`) — the declaration always
reaches Harbor intact.

## Validation ($0, local Docker, Harbor 0.24.0)

Packages: clean v2 chain of `format-code-task-002552`
(`strip>purge-build-caches@3>mtime-normalize@2>separate-verifier@3`) plus
`agent-network-none@1`, vs the same package without the transform
(control); clean v2 chain of `format-code-task-000199` plus the transform
vs its unlocked parent (network-grader pair); the same pair shape for
`format-code-task-000114`; and a throwaway copy of the locked 002552
package with a verifier-side `curl` appended to `tests/test.sh` (scratch
only, never committed). Model-free probe agent (`/tmp` scratch, not
shipped) ran `pip download`, `curl https://pypi.org`, `git ls-remote`
from inside the agent phase. Raw trial logs under
`~/Developer/eval-lab-results/2026-10-10/agent-network-none/`.

| Cell | Locked (`+agent-network-none@1`) | Open (control) |
|---|---|---|
| `pip download six` (agent phase) | rc=1, TLS closed (EOF) | rc=0, wheel saved |
| `curl https://pypi.org` (agent phase) | rc=35, http=000 | rc=0, http=200 |
| `git ls-remote https://github.com/git/git.git` (agent phase) | rc=128, handshake terminated | rc=0, refs listed |
| oracle 002552 | reward 1.0 | — |
| nop 002552 | reward 0.0 | — |
| cheat `upstream_fetch` 002552 (Eval Lab staged path) | clean, reward 0.0 (no remote configured; recon only) | — |
| oracle 000199 (grade-time `pip install`, network grader) | reward 1.0 | reward 1.0 (control) |
| oracle 000114 (grade-time `pip install`, network grader) | reward 0.0 | reward 0.0 (control) — identical `.venv/_pytest` tamper hit both legs (see below) |
| verifier-side `curl -s -o /dev/null -w http=%{http_code} https://pypi.org` (throwaway 002552 copy, nop agent) | `verifier_egress http=200` in the collected `test_output.log` | — (agent lock is the only delta; verifier config untouched) |

## The 000199 cell, diagnosed

The earlier reward-0 on 000199 was not a verdict: that trial never ran to
completion (the launching worker crashed mid-flight; `trial.log` holds a
single line, no `result.json`). Relaunched 2026-10-10: oracle on the
locked package rewards **1.0**, oracle on the unlocked parent rewards
**1.0**. The reference fix exists (`mimo-reference-fixes/index.csv`:
`oracle:pass+nop:fail`) and applies; the verifier is untouched by the
transform, so grade-time `pip install` (`numpy<2`,
`semantic_version<2.7`, `scipy`, `autograd`, `toml`, `appdirs`, `pytest`,
`pip install -e .` in the resolved test command) runs exactly as in the
control. 000199 is therefore the positive network-grader proof: the
agent lock changes nothing grade-side.

000114 is the counterpoint that proves the same thing by symmetry: locked
**and** open both reward 0.0 with byte-identical
`tamper \b_pytest\b in .venv/lib/python3.6/site-packages/_pytest/_code/code.py`
hits, so grading never reached the network — the 0 is the known
network-grader `.venv` false-tamper bug (routed to VerifierIdFix, not
this slice), not the egress lock. No network-grader task with a
reference fix is blocked by the lock; the throwaway verifier-side curl
above shows `http=200` from inside the separate verifier under the lock
directly.

## Residuals

1. Chain adoption is the clean-set owner's call (`AGENT_NETWORK_NONE_ID`
   shipped in `hardening.TRANSFORMS`; `mimo_clean.py` untouched).
2. Run-level `extra_allowed_hosts` (trial config) can widen any policy at
   launch; Eval Lab controls its own runs, but third-party runners should
   be audited for this.
3. Docker-sidecar DNS/ICMP passthrough (by Harbor design) leaves a
   theoretical DNS-tunneling channel; it cannot fetch files over TCP and
   is out of V6's scope (fetching the fix).
