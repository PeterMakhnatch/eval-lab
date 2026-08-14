# Network policy — `allowlist`

## What it is

Harbor 0.21 has three `network_mode` values: `public`, `no-network`,
`allowlist`. `allowlist` plus `allowed_hosts` restricts egress. Docker
enforcement needs Linux nftables `CONFIG_NFT_FIB_INET`. Docker Desktop on
macOS is documented to reject `no-network`/`allowlist` at environment
validation (`docs/tasks/network-policy.mdx`; already noted on
`tasks/event-summary`).

The lab currently forces `network_mode = "public"` on event-summary.

## Demo

```bash
bash explorations/harbor-021/demos/run-allowlist.sh
```

Tiny task `demos/tasks/allowlist-probe` declares:

```toml
[environment]
network_mode = "allowlist"
allowed_hosts = ["pypi.org", "example.com"]
```

Oracle does not use the network; the run still has to start the env.

Observed (2026-08-13, Docker Desktop, Harbor 0.21.0): Harbor raised before
the trial started:

```
ValueError: network_mode='allowlist' is not supported by
EnvironmentType.DOCKER environment. Environment providers must enforce the
requested network policy or reject the task.
harbor_exit=1
```

That is the honest result, not a harness bug. Full traceback:
`captures/allowlist/demo.log`.

## Verdict

**Skip because Docker Desktop on this Mac cannot enforce allowlist (or
no-network).** Revisit when the lab has OrbStack or a Linux Docker host.
Until then, keep `public` on local canaries (07) and treat cloud/remote
network policy as an `escalate_to_human` item in brief 05's policy file.
Do not pretend Desktop `public` is an isolation boundary.
