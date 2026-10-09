---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Flight recorder prototype

The flight recorder records a local Harbor trial without installing a sensor in
its sandbox. It is opt-in and uses the existing state-journal host lifecycle seam.
It does not modify `LockedDockerEnvironment`, its validated mounts, or its
creation-time `network_mode=none` lock.

This is a bounded recorder, not proof that **every** operation is captured or that
an agent can never detect observation. The structural invisibility probes passed;
measured timing overhead and coverage limits are reported below.

## Run it

Use the worktree's project environment. The workstation's separate `harbor`
executable was 0.21.0; this worktree's `uv run --extra laminar harbor` is 0.24.0.
The older executable does not satisfy the current locked-lane environment API.

```sh
uv run --extra laminar evallab flight record \
  --task library/tasks/event-summary --agent oracle \
  --name my-flight-recording --flight-egress locked --json

uv run --extra laminar evallab flight show runs/my-flight-recording --json

uv run --extra laminar evallab flight probe \
  --flight-egress locked --out runs/my-locked-probe --json

uv run --extra laminar evallab flight probe \
  --flight-egress open --out runs/my-open-probe --json
```

`record` retains the existing direct-run control-agent policy: `oracle` or `nop`,
local Docker, no model billing. It records timelines after the trial finishes.
`show` rebuilds the timeline and prints **all** event details, including captured
model bodies; `--json` emits the summary and events. Probe output directories must
be empty so old evidence is not silently overwritten or joined to a new probe.

A trial's `flight/` directory contains:

- `events.jsonl`: cgroup-filtered kernel and optional passive-network events;
- `observer.agent.json`, `observer.verifier.json`: target identities and tier;
- `status.agent.json`, `status.verifier.json`: availability, counts, caps/errors;
- `filediff.agent.json`, `filediff.verifier.json`: external before/after snapshots;
- `timeline.jsonl`: timestamp-ordered joined events;
- `summary.json`: counts, source identity, and observer health.

The output directory is private (`0700`); full-body timelines are `0600`.
`prototype_available` means both instrumented phases successfully recorded. It
is not a completeness certification. Missing, failed, or truncated phase observers
make coverage `incomplete`; `record` then returns nonzero while retaining evidence.

## Actual mechanism

The observer image (`evallab-flight-recorder:<content hash>`, rebuilt whenever its
sources change) is built from Debian bookworm with native arm64 **bpftrace v0.17.0**,
tcpdump, and a Python standard-library decoder. It was exercised on Docker Desktop
linuxkit 7.0.12 with BTF and cgroup v2. No polling/fanotify fallback was used.
Failure to attach is reported, not replaced with pretend data.

The external observer is **not** `--privileged`. It starts from `--cap-drop=ALL` and
adds back only what was proven necessary on this kernel: `SYS_ADMIN + SYS_PTRACE +
BPF + PERFMON`, plus `--security-opt=no-new-privileges=true`, `--memory=1g` and
`--pids-limit=256` (it shares the machine with other trials' containers) and the
label `evallab.flight=1`. It uses host PID/cgroup namespaces. The locked tier runs
with `--network=none` (no sniffer runs there). The open tier uses `--network=host`
solely to see the trial's own bridge and additionally holds `NET_RAW`, plus
`SETUID/SETGID/CHOWN`, which exist only because tcpdump insists on dropping to its own
user after opening the capture. `SYS_ADMIN` plus host PID is still a strongly
privileged container: this reduces the blast radius of an observer compromise, it
does not make the observer unprivileged.
It writes only its own host output directory. Its additional tracing mounts
exist only in the observer. The target gets no observer prompt, tool, environment
variable, file, mount, process, proxy setting, CA, route, or firewall rule.

Empirical bpftrace 0.17 limits found by breaking them: `BPFTRACE_STRLEN` past
200 refuses to start; STRLEN=200 with any DNS `buf()` window overflows the
512-byte BPF stack in `sys_enter_sendto`. The working maximum is STRLEN=160
with a 64-byte DNS window (up from v0's 128/128). Fields at the 160-byte
limit are flagged `path_truncated`, payloads past 64 bytes `payload_truncated`;
complete names remain in file snapshots. `@dns`/`@dnsbuf`/`@dnsfd` maps are
capped by `BPFTRACE_MAP_KEYS_MAX=1024`; overflow warnings land in the bounded
errors list and degrade the phase to partial.

Per-run framing integrity: the observer generates a random 32-hex-char nonce
per observer start, passes it as host-only `FLIGHT_NONCE` env, and the
recorder renders it into `out/probes.<phase>.bt` (host-only output dir, never
a sandbox-visible mount, never logged). Every probe line starts with the
nonce; userspace rejects nonce-less lines and enforces exact per-kind arity.
bpftrace's own `Attaching N probes...` chatter is ignored uncounted; anything
else without the nonce counts as `tamper_suspect`, wrong-arity as
`malformed_events`, both visible in status and timeline coverage.

1. Resolve the **exact** trial session's Docker Compose `main` service, including
   the separately created verifier service. Do not enumerate another container's
   processes or inspect its filesystem.
2. Read only the target's `/proc/<pid>/cgroup`; resolve that cgroup's inode.
3. Every eBPF event probe predicates on `cgroup == <target inode>`. Userspace repeats
   the equality check before `/proc` enrichment. The observer's own processes and
   other trial containers are not recorded.
4. Attach after environment creation and before agent setup. For a separate
   verifier, attach after its creation and before artifact upload/test commands.
   The shared-verifier hook attaches to the same target for that phase.
5. Trace scheduler exec/exit, `openat`/`openat2`, scalar read/write entry/exit,
   connect entry/exit, sendto entry/exit, and DNS-socket recvfrom. Record host PID,
   TID, UID, comm, exec parent PID/arguments, pathname/access flags, fd/byte counts,
   syscall result/errno, DNS names/answers, and destination metadata.
6. Enrich arguments and fd-relative paths from the **event's own** pinned `/proc`
   directory, rechecking its cgroup before reading argv/fd links. This excludes
   foreign data if a host PID has been reused. Cache observed exec arguments/
   parent identity for exit rows. Enrichment remains best-effort when a process
   disappears before its proc entry can be read.
7. Snapshot `/app`, `/tmp`, `/tests`, and `/logs/verifier` through a pinned,
   container-identity-validated `/proc/<pid>/root` descriptor. Directory traversal
   and file opens use descriptor-relative `O_NOFOLLOW`; even an intermediate
   sandbox symlink cannot redirect the privileged observer to a peer filesystem.
   File and directory reads use `O_NOATIME`, never target writes.

Kernel timestamps are converted to wall time using the observer's startup clock
calibration. Every timeline row carries trial/job/task identities and a timestamp.
When a source lacks a timestamp, a trial-finish timestamp is labeled as that
fallback source rather than presented as the actual operation time.

### Egress tiers

**Locked:** leave `LockedDockerEnvironment`'s `network_mode=none` unchanged.
Connections and DNS sends that fail before any packet leaves still appear from
syscall tracepoints. Enforcement is `network_mode_none`.

**Open:** use the trial's ordinary private Docker bridge. A **passive** observer
listens on that exact bridge with a libpcap own-trial IP filter, then repeats the
IP filter in its metadata parser. This is not an inline router or enforcing
firewall. It changes no target routes, DNS, proxy environment, TLS trust, or
visible gateway. Enforcement is `none`.

The packet stream is consumed in memory and never written as pcap. Exported
fields are DNS names/answers, TCP/UDP five-tuples, timestamps, bytes/packets,
SYN/SYN-ACK/established/reset/pending-timeout outcomes, and TLS ClientHello SNI.
No HTTP/application body or TLS plaintext is exported; there is no TLS MITM.
Immediate-mode capture and a one-second shutdown drain preserve short flows.

**Docker embedded DNS was empirically different from bridge traffic:** the open
probe queried `example.com` at `127.0.0.11`. No DNS packet appeared on the trial's
bridge. The cgroup-filtered sendto/recvfrom probes recorded the question and a
61-byte answer containing `104.20.23.154` and `172.66.147.243`. The passive bridge
observer separately recorded a TLS flow to `1.1.1.1:443`, 15 packets / 4,015 bytes,
`established`, SNI `cloudflare-dns.com`. The complete open probe captured 17
packets and exported 3 metadata-only flows, with no reported drops.

### Full-body model and trajectory plane

Reuse the existing **host-side** `evallab capture serve` tap; do not instrument
agents or change their visible provider/proxy configuration. `flight record`
enables the existing host capture-route-token chaining and binds a supplied
`--capture-dir` for the job, restoring its host environment afterwards. A running
loopback tap and the existing provider secret-proxy upstream setup are still
required to generate model bodies: setting `EVALLAB_MODEL_CAPTURE=1` alone does
not start a server or intercept direct-to-vendor traffic.

The timeline consumes either that job's durable capture binding or an explicitly
supplied capture directory. Attribution considers **all trials in the job**,
using existing route-token/session/conversation chaining, not overlapping clocks
alone. Each attributed call is copied into the per-trial timeline with its full
redacted request/response record, including returned reasoning and SSE text.
Existing tap secrets redaction and body caps apply. No hidden reasoning that an
upstream API does not return can be observed.

A real loopback HTTP integration fixture starts the existing tap, forwards a
request to a local HTTP server (no LLM), and proves request body, response
`reasoning_content`, secret redaction, route attribution, and per-trial joining.
Harbor ATIF continuations and tool-call arguments are joined without the old
excerpt truncation; a 12,000-character continuation fixture is retained intact.
Oracle/nop do not make model calls or emit an ATIF model trajectory, so those
planes are explicitly `expected_empty`, not claimed as exercised model inference.

## Local smoke evidence

All runs used local Docker and free local/public-network probes. No hosted model,
judge, GPU, cloud sandbox, or paid computation ran; cost was $0.

### Real locked trial

Reran at **6:50 PM ET on October 8, 2026** under the hardened observer
(image `evallab-flight-recorder:b520393fa344af65`):

- Task: `local-lab/event-summary` (`library/tasks/event-summary`).
- Agent: `oracle`; Harbor 0.24.0; separate verifier; reward **1.0**.
- Job: `eaeadbe6-33d7-4b1a-ab81-efa78ffbc433`.
- Trial: `e310d467-1bae-46a0-9da4-ed50eff82627`.
- Timeline:
  `runs/flight-caps-01/flight-caps-01__eMxPUU3/flight/timeline.jsonl`
  (**1,395 rows**, identifiers on every row).
- Both phase observers: available, no errors/drops/caps; agent **426** events,
  verifier **925** events.

### Hardened invisibility probes (caps, no --privileged)

- `runs/flight-proof/locked-caps/`: **passed, 825 events, status available**,
  zero tamper/malformed/errors. All seven checks green.
- `runs/flight-proof/open-caps/`: **passed, 843 events, status available**,
  3 egress flows / 15 packets, kernel DNS + `connect 1.1.1.1:443 in_progress`
  + TLS SNI preserved. All seven checks green.

### Adversarial probe (E)

Driver retained at `runs/flight-proof/adversarial-01/driver.py`; evidence in
the same directory. A locked container under the observer ran: an exec'd file
whose name contains `\nE\t` + a forged kernel row (executed, rc 0), a
tab-variant filename, a 3000-deep `mkdir` chain (ENAMETOOLONG at depth 2043),
200 files with 200-char names sharing a 130-char prefix, 200 DNS-port
sendto calls, a background file writer growing through the after-snapshot,
and a child-cgroup move attempt (refused: read-only `/sys/fs/cgroup`).

- **878 rows, 0 forged.** 12 `tamper_suspect` + 10 `malformed_events` counted.
- Status **partial** with explicit reasons (depth-cap `snapshot_status`,
  framing counters); `filediff.agent.json` + `status.agent.json` written;
  observer survived.
- 200 `path_truncated` rows (160-byte window); 200 `udp_send` rows
  `failure/ENETUNREACH`; `hash_truncated: 1` (grow.bin past the 8 MiB cap);
  `child_cgroup_unobserved: false` (no child cgroup was created).

### Exec-flood probe (found by independent re-review)

The observer kept every exec's argv (up to 64 KiB) for exit-join enrichment and never
freed it, so an agent looping `exec` with a ~60 KB argument could grow the observer
until its 1 GiB limit killed it and the phase's kernel evidence was lost. Exec
enrichment is now a summary (16 args x 200 chars), freed at process exit, hard-capped
at 8,192 live entries (`process_table_dropped` in the status, which makes the phase
partial); the unmatched-start table is capped at 16,384. Bytes still buffered at
shutdown count as `malformed_events`. Live check: 6,000 execs with a 60 KB argv
recorded 6,002 exec rows, status `available`, observer peak 79 MiB.

### Scripted-cheater demo (F)

Harness: `research/experiments/flight-scripted-cheater/` (stub + driver +
README). A REAL `mimoagent` Harbor trial on `library/tasks/event-summary`
under the LOCKED recorder, driven by the stub through the full-body tap
(`terminus-2` is refused under a creation-time docker lock by standing
validation — it needs in-container installation — so the sanctioned
docker-locked model agent runs instead; direct `harbor run` is used because
the evallab direct path is control-only and the queue path needs a human
spend approval, while this run provably spends $0).
Timeline: `runs/flight-cheater-demo/event-summary__DTEQjgh/flight/timeline.jsonl`
(**1,827 rows**, ids on every row): 6 `model_call` + 6 `model_reasoning`
rows interleave with 8 trajectory steps, 5 tool calls, 45 execs, 715 opens,
`connect 8.8.8.8:80 failure/ENETUNREACH`, the `/app/conftest.py` plant in the
file diff, and the verifier verdict. Honest findings: the sandbox has no
`git`/`curl` binaries and no `.git` or `/tests` mounts, so those recon
attempts fail visibly (exec + `command not found` observations); the cheat
never solves the task (reward 0.0) and the planted conftest coincides with
`output_hygiene` 0.0.

| Plane | Rows / actual evidence |
| --- | --- |
| Kernel | **1,352**: 32 exec, 72 exit, 584 opens, 509 reads, 139 writes, 16 connect attempts |
| File diffs | **24** added/removed/modified category records across roots/phases; these include empty categories, not 24 changed files |
| Phase | **8** lifecycle timestamps |
| Verifier | **5** verdict/output records; grader loads include `/tests/test.sh`, `/tests/verify.py`, `/tests/fixtures/events.jsonl` |
| Model | 1 `expected_empty` verdict; **0 model calls**, as required for oracle |
| Trajectory | 1 `expected_empty` verdict; **0 agent tool/ATIF calls**, as expected for oracle |
| Egress packets | **0** under locked networking; failed attempts are kernel events |

Arguments were recovered for **19/32** execs; guarded enrichment yielded no argv
for the other 13. Proc enrichment is best-effort, especially for fast exits.
Missing argv is not reconstructed or silently attributed to a reused PID.
The trial's 16 connect attempts were
failed Unix-socket/NSS lookups, not invented external network activity. The
locked probe separately proved external TCP and DNS attempts returning
`-101 / ENETUNREACH` with no outgoing network required.

### Seven invisibility checks

Full raw dumps and comparisons are retained at:

- `runs/flight-proof/locked-isolated/probe.json` (**824 kernel events**);
- `runs/flight-proof/open-isolated/probe.json` (**844 kernel + 3 egress events**).

The same container was warmed, sampled unmonitored, then sampled with an external
observer. Its probe script and prebuilt image (including network tools and
recorder binaries) were present before **both** samples; attachment did not inject
or run an observer inside the target. All seven checks passed their comparison.

| Check | Locked | Open |
| --- | --- | --- |
| Visible process comm/cmdlines | Raw identical; no observer process | Raw identical; no observer process |
| Environment | Raw identical, including hostname | Raw identical, including hostname |
| Mount list | Raw identical | Raw identical |
| `/proc/self/mountinfo` | Raw identical | Raw identical |
| Visible inotify instances | Raw identical; no extra sensor | Raw identical; no extra sensor |
| DNS/config + network structure | Raw identical | Same DNS/hosts/NSS, routes, interfaces, neighbor MAC/IP, iptables result and first-hop IP; timing-only raw difference below |
| Timing + first-connect reachability | Same failed-connect outcome; measured overhead below | Same connected outcome; measured overhead below |

**Every observed open structural-dump difference:** traceroute hop-one latency
was **0.063 ms unmonitored vs 0.142 ms monitored**. Hop-one/gateway remained
`192.168.192.1`; neighbor MAC remained `f2:d9:ff:3c:fc:7f`.
`resolv.conf` retained `nameserver 127.0.0.11`. Inside-visible `iptables -S` failed
with the same ordinary permissions message in both samples. No structural
change was observed. Traceroute milliseconds and neighbor reachability timers
are treated as noise, but raw differences are retained, never hidden.

Measured timing (200 open/read/close operations):

- Locked: **10.534125 ms -> 22.463625 ms**, +11.929500 ms, **2.132462x**.
  First failed TCP connect: **1.115375 ms -> 1.256417 ms**.
- Open: **10.481208 ms -> 21.498500 ms**, +11.017292 ms, **2.051147x**.
  First successful TCP connect: **18.139750 ms -> 25.352292 ms**.

The timing gate is a disclosed 5x bound plus unchanged connect reachability. A
single passing sample is **not** statistical proof that observation is
undetectable. The overhead is real, not described as identical timing.

## Coverage limits versus “everything”

- Scalar `read`/`write` are traced; mmap page reads, pread/pwrite, readv/writev,
  io_uring, sendmsg/sendmmsg, recvmsg, and unimplemented syscall families are not.
- File opens are attempts with flags, not successful-open verdicts. FD/path and
  argv enrichment race short-lived processes; relative/unlinked paths may be
  unresolved. Normal child processes sharing the target cgroup are included;
  processes found in descendant cgroups at observer start/shutdown set
  `child_cgroup_unobserved` and degrade the phase to partial. Child mount/net
  namespaces can make path/IP interpretation ambiguous.
- TCP syscall `EINPROGRESS` is recorded as pending, not failure. Packet outcomes
  improve open-tier knowledge; an observer stopped before timeout can leave a
  SYN pending. IPv6 destination decoding, extension/fragment handling, QUIC/ECH,
  multi-record or gapped ClientHello reconstruction are incomplete.
- DNS metadata comes from ordinary IPv4 DNS sockets using intercepted calls.
  DNS-over-HTTPS/TLS, untraced send/recv variants and messages exceeding the
  64-byte bpftrace buffer are not fully decoded (flagged `payload_truncated`).
- There is no generic HTTPS plaintext capture. Only the separately configured
  model tap has application bodies, with the existing 4 MiB body cap and
  redaction. Reasoning not returned by a provider is unavailable.
- Snapshots cap at 50,000 files/root and hash files only up to 8 MiB. Large files
  use metadata; concurrent writes are not an atomic filesystem snapshot. The
  event cap is 500,000/phase and overflow/decode errors degrade status explicitly.
- Separate verifier startup is captured before tests/artifact upload, not the
  container's first init instructions. Observers detach at phase boundaries,
  before final container teardown. Multi-step/restarted agent environments and
  additional task service containers are not a complete mission-wide sensor.
- [INFERENCE] Existing filesystem watches could notice external snapshot opens;
  `O_NOATIME` does not suppress fsnotify. Kernel-resource/timing effects are not
  eliminated by this probe (locked 10.5→22.5 ms, open 10.5→21.5 ms per 200
  reads; connect outcomes unchanged). The inotify check compares instances,
  not arbitrary watcher output. Literal zero-information invisibility is
  **not established**.
- Trajectory/tool rows without provider timestamps carry the labeled
  trial-finish fallback timestamp, so cross-plane time order is exact for
  kernel/model/verifier rows and approximate for trajectory rows.

## Behavioral checks

`tests/test_flight_recorder.py` (now **55 tests**) exercises full-body
multi-plane joining, `model_reasoning` rows, stub-scripted cheat joins,
fail-open lifecycle (lazy import, guarded awaits, per-phase unavailable,
hook restore, `verifier_unobserved`), nonce framing/tamper counters,
snapshot depth/hash/cmdline bounds, gateway caps, timeline presence/corrupt
caps, CLI races, observer tier/capability command shape, truncation flags,
child-cgroup detection, plus the earlier model control, cgroup/decoy,
pcap, DNS, coverage, privacy, CLI display, and invisibility checks.
These are behavioral fixtures, not source-text assertions.

```sh
uv run --extra laminar pytest tests/test_flight_recorder.py -q
uv run --extra laminar ruff check src/evallab/flight src/evallab/cli.py \
  src/evallab/harbor_state_journal.py tests/test_flight_recorder.py
uv run --extra laminar --with ty ty check src/evallab/flight \
  src/evallab/cli.py src/evallab/harbor_state_journal.py
```

No commits, PRs, shared-service changes, deployment, LaunchAgents, or other
worktree edits are part of this prototype.

Observed fixture/check result: **55 passed** (two existing `semantic_facts`
`construct`-field warnings); touched-file Ruff and ty checks passed.
