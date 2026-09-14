# `peter/nginx-proxy-repair`

This hand-authored task exercises diagnosis and persistent repair of an nginx
reverse proxy, using service configuration, HTTP behavior, and filesystem routing.
See [instruction.md](instruction.md) for the unchanged agent-visible contract.

## Provenance and placement

Originally imported, including `controls/`, from the recorded source
`PeterMakhnatch/rl-envs` commit `8f9b1fc12582f0d83190f0063cf7f3ee91924b3b`.
The 2026-09-14 repair changes HTTP probes and adds adversarial controls;
the current package is no longer a verbatim copy of that source revision.
The original `peter/` name and author are retained: the `local-lab/` example is
not a mandated namespace. `agents/STRUCTURE.md` assigns lab-authored tasks to
`library/tasks/` (lines 69–75); it does not require model calibration or an
`experimental/` subdirectory. This is library inclusion, not registry admission.

**Source provenance (2026-09-14): recorded upstream revision not independently verified.**
The historical control receipt below records the original local task path and
digest `0b35be184ae78507a1a94587a604852aa796c533a35a0f4c6bd1aafb798de310`,
but contains no license grant or binding to the claimed Git commit. The original
`~/Developer/rl-envs` checkout is absent. Both the public
[pinned source tree](https://api.github.com/repos/PeterMakhnatch/rl-envs/git/trees/8f9b1fc12582f0d83190f0063cf7f3ee91924b3b?recursive=1)
and [pinned LICENSE](https://raw.githubusercontent.com/PeterMakhnatch/rl-envs/8f9b1fc12582f0d83190f0063cf7f3ee91924b3b/LICENSE)
were unavailable (404); authenticated GitHub repository/tree lookup was also
unavailable. A 404 does not establish wrongdoing or a license violation.
The task metadata records Peter Makhnatch as author; that is a declaration,
not an independent upstream/license attestation. This repository copy retains
the source limitation. No third-party license grant, registry admission or
external dataset publication follows from a code repair.

## Environment and planted faults

Ubuntu 24.04 provides nginx, Python, curl, and standard diagnostic tools, with one
CPU, 1024 MB RAM, and a 600-second agent timeout. A JSON backend belongs on
`127.0.0.1:8081`; an HTML page and CSS live under `/var/www/app`.
The planted configuration has a missing semicolon after `client_max_body_size`,
the wrong upstream port (8080), and a doubled static-directory mapping.
The backend itself serves `/api/...`, so the proxy must preserve that prefix.
Repair must persist on disk; a process that only works in the agent container
is insufficient.

The task uses the supported `public` network baseline for local Docker Desktop,
not an enforced offline sandbox. Image builds fetch packages. The environment
build context excludes the hidden tests, reference solution, and controls.

## Verifier

`environment_mode = "separate"` builds a fresh verifier image from `tests/`.
Harbor transfers only `/etc/nginx`, `/var/www/app`, and `/opt/backend/app.py` to
that image. Trusted Python, pytest, nginx, and the backend implementation do not
come from the agent's installed tools. The verifier rejects a modified, missing,
or symlinked backend, starts its own trusted backend with a fresh random nonce,
and starts nginx from the submitted on-disk configuration.

HTTP checks cover configuration validity, port 80, HTML/CSS bytes and content
types, fresh randomly named static files and nested paths, absent-file 404s,
nonce-bearing health, query forwarding, and unchanged backend JSON 404s. All
probes inspect the first HTTP response without following redirects. Items
responses, including IDs and JSON content type, are compared with the live
trusted backend across absent, zero, partial, oversized, invalid, and encoded
limit queries. The fresh nonce and filesystem probes reject fixed health
replies and inlined answers for known static files. Equivalent routing
configurations are accepted; removing the unnecessary body-size directive or
inheriting the server root is valid. Every pytest check must pass for reward 1;
otherwise reward is 0.
Outputs include `/logs/verifier/reward.txt`, `ctrf.json`, and `backend.log`.
These defenses and controls are bounded evidence, not proof against all gaming.

## Control matrix

| Control | Expected reward | Purpose |
|---|---:|---|
| Oracle (`solution/solve.sh`) | 1.0 | Complete reference repair |
| Nop | 0.0 | Broken starting state |
| `controls/alt-delete-body-size.sh` | 1.0 | Valid directive removal |
| `controls/alt-inherit-root.sh` | 1.0 | Valid inherited static root |
| `controls/partial-syntax-only.sh` | 0.0 | Syntax repair alone is insufficient |
| `controls/game-static-health.sh` | 0.0 | Fixed health response |
| `controls/game-inline-static.sh` | 0.0 | Inlined known static answers |
| `controls/game-move-backend.sh` | 0.0 | Backend modification |
| `controls/game-tamper-pytest.sh` | 0.0 | Agent-side pytest tampering |
| `controls/game-redirect-api.sh` | 0.0 | Client-side redirect to loopback instead of proxying |
| `controls/game-fabricate-items.sh` | 0.0 | Names-only items and fabricated query response |

The committed schema-2 matrix at
`research/experiments/nginx-proxy-repair-local-controls.json` includes the
reference, nop, both valid alternatives, and the redirect/items regression controls.
It binds the current package/verifier and uses explicit oracle solution overrides;
running it does not admit the task or authorize model/training execution.

## Verification

On 2026-09-11, local Docker with Harbor **0.21.0** produced:

| Run name (under this worktree's `runs/`) | Actual reward |
|---|---:|
| `nginx-proxy-repair-lib-oracle-20260911` | 1.0 |
| `nginx-proxy-repair-lib-nop-20260911` | 0.0 |
| `nginx-proxy-repair-lib-matrix-oracle-20260911` | 1.0 |
| `nginx-proxy-repair-lib-matrix-nop-20260911` | 0.0 |

```bash
uv run evallab run --task library/tasks/nginx-proxy-repair --agent oracle --name nginx-proxy-repair-lib-oracle-20260911 --jobs-dir runs
uv run evallab run --task library/tasks/nginx-proxy-repair --agent nop --name nginx-proxy-repair-lib-nop-20260911 --jobs-dir runs
uv run evallab matrix research/experiments/nginx-proxy-repair-local-controls.json
uv run evallab matrix research/experiments/nginx-proxy-repair-local-controls.json --reuse-existing
```

The first matrix invocation completed its oracle trial but hit a local PostgreSQL
schema-initialization deadlock during ingestion while another matrix ran. The
sequential `--reuse-existing` invocation preserved that trial, ran nop, and passed
both expectations. This was an ingestion failure, not a verifier mismatch.
Subsequent runs used `EVALLAB_DERIVED_ROOT` set to this worktree's absolute
`derived/parquet` path; the first direct oracle reported a derived-root ownership
warning without affecting its Harbor reward. Those matrix package digests include
the original added documentation, not the subsequent HTTP-probe repair.
The historical registry audit refreshed the deterministic registration inventory
without creating or promoting registry records.

Historical source-task controls passed 9/9 (zero mismatches or infrastructure
errors) in the primary checkout's ignored receipt
`runs/batch-ctl-nginx-proxy-repair-20260910-180334-dd2a/receipt.json`.
That is provenance for the source task, not a claim that all mutants were rerun
from this library package. No run directories are promoted to research evidence.

Current repair verification is recorded outside this task package in
`research/experiments/README.md` and the retained native matrix receipts, which
bind exact package, verifier and selected-solution digests. Historical oracle/nop
receipts do not qualify changed verifier bytes. An expected-zero control counts
only when setup succeeds and the real separate verifier rejects the submission.
Items have deterministic backend content: these comparisons reject the named
fabrication bypass, but do not prove that every conceivable correct-looking
response was produced by upstream forwarding.

**Limits:** These are validity controls, not model-capability evidence. No model
trials were run; difficulty is unknown. The inherited `medium` metadata is an
uncalibrated author estimate, not an empirical result. No registration, promotion,
external dataset publication, paid model call, or cloud execution is claimed.
