# Safety audit: historical runs and the repaired tool boundary

This file records what was measured before any clean run, and why the earlier labels and
line scores from this lane are withdrawn.

## 1. The original harness was not contained

Two real agent canaries used `--tools bash` only. Both read the host:

| Canary | Probe | Result |
|---|---|---|
| PATH shim | `ls /Users` | exit 0, listed host users |
| PATH shim | `cat ~/.zshrc` | exit 0, host shell config |
| PATH shim | `ls /private/tmp/ov1104` | exit 0, lane tree |
| explicit `shellPath` | same three probes | exit 0 in all three |

Cause: OMP 18.14.2 executes finite bash commands through an embedded native shell. A PATH
shim intercepts only explicit external lookups, and `shellPath` is not the executor path
for those commands.

Consequence: no historical run has valid containment evidence.

## 2. Historical side effects

Audit tool: `workers/safety-audit/audit.py`, read-only over session security metadata. It
never executes a recorded command.

| Measure | Count |
|---|---:|
| Run directories | 137 |
| Session files (all hashed) | 135 |
| Tool calls | 6,011 |
| Suspicious calls exported | 2,821 |
| Calls naming outside-workspace paths | 528 |
| Calls with observed outside-workspace access | 144 |
| Confirmed direct file-tool mutations outside `/tmp` | 0 |
| Deletion commands naming outside-`/tmp` targets | 0 |
| Explicit non-device outside-`/tmp` mutation targets | 12 (all container paths) |

The 12 non-device candidates are `/testbed`, `/io`, `/workspace`, and `/root/...` targets of
`ln -s`, `mkdir`, or redirects. All are absent on the host now. Independent re-parse of all
lane sessions found 0 `write`/`edit` targets outside `/tmp` and outside the run workspace.

Residual uncertainty: 2,446 shell calls named a mutating verb and an outside-workspace
path, and 2,466 calls have effect `unknown`. Exit status alone does not prove file state.

## 3. Quarantine

All 137 runs are quarantined. Named observed accesses include reading the hidden reference
solution (`.../mimo-v6-full-fleet/references/<task>`), this lane's `runner.py` and
`generator.py`, host site-packages, and `~/.pyenv`. Quarantined runs are never labelled,
scored, or counted in a clean denominator, and no later canary can requalify them.

## 4. The repaired boundary

- Native bash is removed: `--no-tools --trusted-extension <glue>` with `bash.enabled=false`.
- The extension registers one `bash` tool and proves, before the prompt and before every
  provider request, that the active tool list is exactly `["bash"]` and every other
  registered tool is inactive. Any non-bash tool call aborts the session.
- Each command is a fixed-argv `docker exec` into one owned container: `--network none`,
  `--read-only`, one workspace bind, tmpfs `/tmp`, `--cap-drop ALL`,
  `no-new-privileges`, private IPC/cgroup namespaces, bounded pids/memory/cpu.
- Container command environment is rebuilt with `/usr/bin/env -i`; no host variable
  reaches the container.
- The container is destroyed on timeout, cancellation, parent exit, or watchdog expiry; a
  Docker failure aborts the run instead of falling back to the host.
- Every run writes a receipt bound to the canary file hash, the transport source hash, the
  registry contents, session file hashes, and its artifacts.

Canary pass: `safety-canary/attempt10-container-v2/receipt.json` — forbidden probes return
125 / 1 / 2, workspace probe returns `/workspace`, cleanup proven.

## 5. What is still not claimed

Docker isolation is not a kernel sandbox, and the CLI still runs trusted OMP code on the
host. Inactive registered tools (`write`, goal/experiment helpers) remain present but are
never model-active and any call to them aborts the session. Image and workspace provenance
still need review per task.
