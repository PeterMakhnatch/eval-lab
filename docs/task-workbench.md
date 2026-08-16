# Task-quality workbench

The task-quality workbench gives an author a deterministic, non-admitting
review loop for a Harbor task candidate. It inspects source bytes, performs
static safety and provenance checks, optionally runs only Harbor's free
`oracle` and `nop` controls, exercises declared invalid solutions, and writes a
review packet. It does not queue, register, freeze, publish, or change policy.

Certification means only **ready for human review**. Admission still requires a
separate, human-created record under `library/registry/`, following
[`task-registry.md`](task-registry.md).

## Candidate layout

A candidate uses the normal Harbor task package plus workbench-only adversarial
solutions:

```text
candidate/
├── task.toml
├── instruction.md
├── environment/
│   └── Dockerfile
├── solution/
│   └── solve.sh
├── tests/
│   ├── Dockerfile
│   └── test.sh
└── workbench/
    └── adversarial/
        ├── empty-output.sh
        ├── wrong-value.sh
        └── extra-artifact.sh
```

The evaluated image must not copy `solution/`, `tests/`, `verifier/`,
`workbench/`, or dot files. The verifier must run in a separate image, write a
deterministic reward under `/logs/verifier`, and keep hidden answers out of the
instructions, environment, and solution. At least three executable adversarial
solutions are required. They are substituted for the Oracle solution only in
isolated staging copies.

Both shell-form and JSON-array Docker `COPY`/`ADD` instructions are parsed.
Dynamic, wildcard, variable, escaping, or otherwise unresolved sources fail
closed. Remote `ADD` is forbidden. Candidate symlinks are forbidden rather
than followed, including links from the agent-visible build context into
`tests/`, `solution/`, `verifier/`, or `workbench/`. V1 also rejects
task-authored Docker Compose files because it cannot prove isolation for
arbitrary sidecar networking.

Source identity must include a non-floating source reference and a declared
license. Base and verifier images must be digest-pinned. Network policy must be
explicit in `task.toml`; runtime scripts and Docker build instructions
containing network fetch/install commands fail static admission. There is no
reviewed immutable offline-package mechanism in v1, so package-manager commands
inside Dockerfiles fail closed even when their package arguments are pinned.
Scanning the agent Dockerfile alone is not enough, because `COPY setup.sh` plus
`RUN sh /tmp/setup.sh` hides every fetch in a file no Dockerfile line names, so
the contents of *every* regular file under `environment/` are read and scanned.
A file there that is not decodable UTF-8 is refused rather than skipped.

### What denies the network, and where

Isolation is not uniform across the four phases, and the packet records them
separately rather than as one overlay claim. For every control the workbench
injects a fixed Docker Compose overlay on Harbor's `main` service setting both
`build: {network: none}` and `network_mode: none`. The overlay is named in the
frozen command, included in the staged-task digest, and revalidated after the
run. It reaches the agent phases only:

| Phase | Denied by | Boundary |
| --- | --- | --- |
| Agent build | overlay `build.network: none` | container runtime |
| Agent runtime | overlay `network_mode: none` | container runtime |
| Verifier build | static text scan of `tests/` | **text scan only** |
| Verifier runtime | `task.toml` declaration, checked statically | **text scan only** |

For the two agent phases, text scanning is defense in depth and the container
runtime is the safety boundary. For the two verifier phases it is the *only*
boundary, and the distinction is load-bearing:

- Harbor 0.21.0 discards `extra_docker_compose` when it builds the separate
  verifier runtime config, so the overlay never reaches the verifier container.
  Nothing at runtime stops a verifier from reaching the network. The workbench
  therefore requires `task.toml` to declare it: the effective baseline (from
  `[verifier.environment].network_mode`, or `[environment].network_mode` when
  that table is absent) and any `[verifier].network_mode` phase override must
  both be `no-network`. An absent `[verifier.environment]` does not inherit
  silently — the resolution Harbor actually performs is reproduced and checked.
  Harbor is installed as a standalone CLI, not as a library this package can
  import, so that resolution is a reproduction rather than a call to
  `resolve_effective_verifier_env_config`. It is pinned:
  `test_verifier_network_resolution_matches_harbor` runs Harbor's own resolver
  in Harbor's own interpreter and fails if the two stop agreeing.
- The verifier image build is covered by a static content scan of `tests/`, not
  by container-level enforcement. There is no `build.network: none` on that
  build, so the scan is the entire boundary. It applies the build-time network
  pattern — remote URL schemes, VCS fetches, and the online package managers
  (`apt`, `apk`, `dnf`/`yum`/`microdnf`/`zypper`, `pip`, `uv`, `npm`/`pnpm`/
  `yarn`, `gem`, `cargo`, `go`, `Invoke-WebRequest`) — to `tests/Dockerfile` and
  to every other file in the context, because `COPY . /tests` plus
  `RUN sh /tests/bootstrap.sh` hides the fetch in a file no Dockerfile line
  names. A file under `tests/` that cannot be decoded as UTF-8 is refused, not
  skipped. A text scan can still be defeated by an obfuscated fetch; it is a
  review aid, not a sandbox.

A declared `[environment].docker_image` is refused outright, because it makes
Harbor skip the reviewed `environment/Dockerfile` build entirely, so the
overlay's build-time denial would never apply to the image the agent runs.

### The task.toml surface v1 understands

Both verifier claims above rest on the workbench reproducing Harbor's
configuration resolution in `_effective_verifier_network`. Any table or key it
fails to reproduce is a silent hole: the task reaches Harbor with a policy the
packet never examined. Two review rounds each found one, so the surface is now
closed rather than open. `SUPPORTED_TASK_CONFIG` lists exactly what v1 models,
and anything else is refused with `unsupported_task_configuration`, an error
naming the offending path (`steps`, `verifier.collect`,
`task.authors[1].affiliation`).

What v1 accepts: `schema_version`, `artifacts`, `[task]` (`name`, `version`,
`description`, `keywords`, `[[task.authors]]` with `name`/`email`), the
free-form `[metadata]` table, `[agent].timeout_sec`, `[verifier]`
(`timeout_sec`, `environment_mode`, `network_mode`, `[verifier.environment]`),
and `[environment]`/`[verifier.environment]` limited to `network_mode`,
`build_timeout_sec`, `cpus`, `memory_mb`, `storage_mb`, plus `docker_image` on
`[environment]` only — where it gets its own `prebuilt_image_unsupported`
refusal.

Everything else is refused, including `[[steps]]` and
`multi_step_reward_strategy`, `[solution]`, `source`, `verifier.collect`,
`verifier.env`, `verifier.user`, `agent.network_mode`, `agent.allowed_hosts`,
`allowed_hosts`, the deprecated `allow_internet`, `mcp_servers`, `healthcheck`,
`skills_dir`, `workdir`, `env`, `os`, `gpus`, `gpu_types`, `tpu`, and
`docker_image` under `[verifier.environment]`.

`[[steps]]` is the one worth spelling out. Harbor resolves a multi-step task's
verifier step-first: `resolve_effective_verifier_env_config` returns
`steps[i].verifier.environment` before `[verifier.environment]`, and the phase
override falls back the same way. A task whose task-level tables all declare
`no-network` can therefore still run a step's verifier with full egress. v1
models a single verify pass, so it refuses `[[steps]]` instead of resolving it.

`unsupported_task_configuration` is classified `harness_defect`, not
`task_defect`: it usually means the task is fine and this workbench version is
not equipped to reason about it. It is still an error and still blocks
certification, because a false green is worse than a refusal. The fix is either
to express the task within the supported surface, or to extend
`SUPPORTED_TASK_CONFIG` together with the checks that model the new construct —
never the allowlist alone.

## Commands

Run the module from the repository root. These examples use synthetic local
source identity; use the candidate's real immutable reference and license.

```bash
python -m evallab.task_workbench plan path/to/candidate \
  --repo-root . \
  --source-uri local/my-task \
  --source-ref local/my-task@1.0.0 \
  --license MIT \
  --zone 03-synthetic
```

`plan` prints the inspection record and exact frozen control plan without
running Harbor. Commands use `$REPO` placeholders so the same source produces
the same record after a clone or directory move.

Immediately before a control run, the workbench resolves the task from the
Inspection's frozen repository-relative path, re-inspects its bytes and package
digest, and compares the entire candidate and control plan. The Harbor backend
then independently validates the candidate record, fixed control semantics,
and exact command. After constructing each isolated stage, it compares the
stage manifest with the frozen source-plus-mutation plan before invoking any
Harbor or Docker subprocess.

```bash
python -m evallab.task_workbench check path/to/candidate \
  --repo-root . \
  --source-uri local/my-task \
  --source-ref local/my-task@1.0.0 \
  --license MIT \
  --zone 03-synthetic
```

Static failure prints exact diagnostics and makes zero control calls. After
static checks pass, add `--run-controls` to run three Oracle trials, one Nop
trial, and every declared adversarial solution. The backend is fixed to local
Harbor Oracle/Nop, one attempt per job, and concurrency one. There is no model
argument or generic shell-execution surface.

An interrupted or incomplete run remains under
`runs/task-workbench/<candidate-id>/` and reports `harness_blocked`. It is never
silently replaced or retried: preserve and assess it with
`--controls path/to/controls.json`. A deliberate reattempt needs a distinct,
pinned candidate identity and therefore a new run root. A complete,
digest-valid bundle is reused without another call. Supplied JSON is not proof:
the workbench resolves each frozen job and stage under the candidate's local
run root, recomputes their tree digests and reward vector, and refuses missing,
escaping, changed, or inconsistent evidence. The retained trial and lock must
also name the candidate task and exact stage path, match Harbor's recomputed
package digest, use the planned Oracle/Nop agent and separate verifier, and
record the exact no-network Compose path and digest.

```bash
python -m evallab.task_workbench packet path/to/candidate \
  --repo-root . \
  --source-uri local/my-task \
  --source-ref local/my-task@1.0.0 \
  --license MIT \
  --zone 03-synthetic \
  --controls path/to/controls.json
```

`packet` writes only:

```text
research/registration/candidates/<candidate-id>/candidate.json
research/registration/candidates/<candidate-id>/certification.json
research/registration/candidates/<candidate-id>/evidence/<control-id>.json
```

The default output root is enforced. An output under the registry, queue,
policy directory, or outside the repository is refused. Existing identical
bytes are accepted as an idempotent rebuild; different existing bytes cause a
conflict rather than an overwrite. Each portable evidence record retains the
scrubbed raw Harbor job/trial results and full job/stage manifests needed to
rederive the score and digests. Agent logs, candidate outputs, and verifier
stdout/stderr are deliberately omitted to avoid leaking golden data; their raw
file digests remain in the manifest.

## What is checked

The candidate record freezes the task source/config/image/instruction/oracle/
verifier/adversarial/artifact digests, the complete file manifest, source
identity, license, network policy, registry observation, and every proposed
control command and digest.

Static diagnostics cover schema and metadata, required files and executable
entry points, timeout bounds, absolute artifact paths, path and symlink escape,
separate verifier isolation, hidden/golden data exposure, pinned images and
dependencies, runtime network use, nondeterministic verifier constructs,
reward output, adversarial coverage, and forged registration claims.

Five refusals exist specifically to stop the packet from claiming isolation it
cannot back:

- `unsupported_task_configuration` — the task uses a `task.toml` construct this
  workbench version does not model, so its effective network policy was never
  examined; see
  [The task.toml surface v1 understands](#the-tasktoml-surface-v1-understands);
- `prebuilt_image_unsupported` — `[environment].docker_image` is declared, so
  Harbor skips the reviewed `environment/Dockerfile` build and the overlay's
  build-time network denial never applies to the image the agent runs;
- `build_context_unreadable` — a file under `environment/` or `tests/` is not
  decodable UTF-8, so it cannot be scanned for build-time network use; it is
  refused rather than skipped;
- `verifier_network_not_isolated` — the verifier's effective *baseline* network
  is not `no-network`. Because Harbor drops the overlay for the verifier, a
  networked verifier can exfiltrate hidden inputs, and it can make
  `verifier_deterministic` an artifact of a stable remote response rather than
  of a deterministic verifier;
- `verifier_phase_network_not_isolated` — `[verifier].network_mode` reopens the
  network for the verification phase itself.

`build_network_use` and `build_context_unreadable` are enforced for both images,
but only the agent image also has container-level denial behind them. The two
`verifier_*_not_isolated` refusals are static declaration checks, not runtime
denial, and `unsupported_task_configuration` is a limitation of the workbench
rather than a finding about the task; see
[What denies the network, and where](#what-denies-the-network-and-where).

The control assessment requires:

- three Oracle rewards exactly equal to `1` with identical digests of the
  actual retained verifier output trees under each Harbor trial's `verifier/`
  directory;
- one Nop reward exactly equal to `0`;
- every declared invalid output to receive exactly `0`;
- unchanged source, image, and verifier digests across controls;
- a reconstructible staged task with the fixed no-network overlay and a local
  Harbor job whose raw result agrees with every claimed reward.

Packet check-vector booleans are gated by these recomputed evidence diagnostics.
A drifted task identity, stage, network binding, verifier mode, command, or raw
result therefore cannot leave a superficially true Oracle, Nop, adversarial,
determinism, or isolation subclaim in the packet.

Diagnostics distinguish `task_defect`, `harness_defect`, and `agent_failure`.
Oracle failures and verifier/reward failures are task defects; infrastructure,
authentication, and interruption are harness defects; a normal evaluated-agent
failure is an agent failure. Nop and adversarial rejection are expected
outcomes, not agent failures.

The certification statuses are:

- `needs_changes`: a static, isolation, verifier, or discrimination defect;
- `controls_pending`: static checks passed but controls are absent;
- `harness_blocked`: controls could not complete for infrastructure reasons;
- `certified_for_review`: every static and control requirement passed.

No status grants admission. A reviewer must compare the retained source and
control evidence, then independently create the registry record if warranted.

## Reference fixture

`tests/fixtures/task_workbench/valid/` is a small deterministic fixture with
digest-pinned agent and verifier images. The committed example packet under
`research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/`
records its real local Harbor controls. It is test evidence, not a registered or
published task.

That packet's certification is **withdrawn**. It was written when the overlay
set only `network_mode` and when the fixture declared
`[environment].network_mode = "public"`, so the agent image was built with an
unconstrained build network and the verifier container ran with full egress
while the packet asserted `check_vector.isolation: true`. Re-running the current
static inspection over the exact certified bytes fails admission with
`verifier_network_not_isolated`. The retained `control_bundle` and `evidence/`
records are kept unaltered as the factual record of what was run; they are no
longer accepted as certification evidence. Re-certification would require fresh
control runs under the current code, which have not been performed. See the
`withdrawal` object in that packet's `certification.json`.

This schema has no withdrawal disposition — `certified` is derived as
`status == "certified_for_review"`, and the four statuses above are the whole
vocabulary. A withdrawn packet is therefore recorded with the status the current
code computes for it, plus an explicit `withdrawal` object.
