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
`tests/`, `solution/`, `verifier/`, or `workbench/`.

Source identity must include a non-floating source reference and a declared
license. Base and verifier images must be digest-pinned. Network policy must be
explicit in `task.toml`; runtime scripts and Docker build instructions
containing network fetch/install commands fail static admission. There is no
reviewed immutable offline-package mechanism in v1, so package-manager commands
inside Dockerfiles fail closed even when their package arguments are pinned.
Scanning a Dockerfile alone is not enough, because `COPY setup.sh` plus
`RUN sh /tmp/setup.sh` hides every fetch in a file no Dockerfile line names, so
the contents of *every* regular file under `environment/` and under `tests/` are
read and scanned. A file in either that is not decodable UTF-8 is refused rather
than skipped.

### The build-context filenames v1 understands

A build context holds inert payload, which only matters because a Dockerfile
instruction copies it, and *configuration*, which Harbor, the Compose CLI, or the
Docker builder reads by name. Payload is covered by the content scan below.
Configuration is a closed namespace, and `Dockerfile` is the only member of it v1
models, so it is the whole allowlist. Reading Harbor 0.21.0, an environment
directory contributes exactly:

| Filename | Consumed by | v1 |
| --- | --- | --- |
| `Dockerfile` | `environments/definition.py` (`DOCKERFILE_NAME`), `DockerEnvironment._dockerfile_path` | modelled |
| `docker-compose.yaml` | `COMPOSE_FILE_NAME`, `_environment_docker_compose_path`, layered into `_docker_compose_paths` for `build` and `up`, reparsed by `_egress_controlled_service_names` | refused |
| `.env` | the Compose CLI, because Harbor passes `--project-directory <environment dir>` and never `--env-file` | refused |
| `.dockerignore` | Docker's builder, to drop paths from the context | refused |

The refusal is a pattern over that namespace — every `compose`/`docker-compose`
`.yaml`/`.yml`/`.json` spelling including `.override` variants, every `.env*`
spelling, and `.dockerignore` — applied in **both** build contexts and at every
depth, not a list of forbidden paths. A list of forbidden paths is what the
earlier rounds walked around: `environment/docker-compose.yaml` was refused by
exact path, while the identical two lines under `tests/` — the directory
`Trial._verifier_env_build_context` hands the separate verifier as its
environment directory — reached that image build and that `docker compose up`
unexamined. Because `_egress_controlled_service_names` excludes any service
declaring its own `network_mode` or `networks` from the egress-control rewrite
that implements `no-network`, `services: {main: {network_mode: bridge}}` there
turned a fully compliant `task.toml` into a verifier with full egress while the
workbench emitted nothing. Only a context root is consumed by Harbor 0.21.0, but
nested matches are refused too: which directory becomes an environment directory
is Harbor's choice, and the cost of a false refusal is renaming a fixture while
the cost of a miss is a false certification.

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
| Verifier build | filename allowlist plus static text scan of `tests/` | **static checks only** |
| Verifier runtime | filename allowlist plus `task.toml` declaration, checked statically | **static checks only** |

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
  `resolve_effective_verifier_env_config`. It is pinned twice:
  `test_verifier_network_resolution_is_pinned_statically` asserts the four
  expected `(baseline, phase)` pairs as literals and therefore runs everywhere,
  including CI, where no `harbor` binary exists; and
  `test_verifier_network_resolution_matches_harbor` additionally runs Harbor's own
  resolver in Harbor's own interpreter when the binary is present, and fails if
  the two stop agreeing. The declaration is only enforceable because a
  task-authored `tests/docker-compose.yaml` is refused: a service there declaring
  its own `network_mode` is excluded from Harbor's egress-control rewrite, so
  `no-network` would be declared and not applied.
- The verifier image build is covered by the filename allowlist above plus a
  static content scan of `tests/`, not by container-level enforcement. There is no
  `build.network: none` on that build, so those two checks are the entire
  boundary. The scan applies the build-time network pattern — remote URL schemes,
  VCS fetches, and the plain install idioms of every ecosystem's package manager
  (`apt`, `apk`, `dnf`/`yum`/`microdnf`/`zypper`, `pacman`, `brew`,
  `conda`/`mamba`, `pip`, `uv` including `uv sync`/`uv add`/`uv lock`, `poetry`,
  `pipenv`, `bundle`, `composer`, `npm`/`pnpm`/`yarn` including `npm i`, `npx`,
  `uvx`, `gem`, `cargo`, `go`, `dotnet`, and any `mvn`/`gradle` invocation since
  every default goal resolves remotely) — to `tests/Dockerfile` and to every other
  file in the context, because `COPY . /tests` plus `RUN sh /tests/bootstrap.sh`
  hides the fetch in a file no Dockerfile line names. A file under `tests/` that
  cannot be decoded as UTF-8 is refused, not skipped. This part is still a
  denylist and inherits a denylist's weakness: an obfuscated fetch, or a package
  manager nobody enumerated, defeats it. It is a review aid, not a sandbox. The
  filename allowlist beside it is closed; the content pattern is not.

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
naming the offending path (`steps`, `environment.healthcheck`,
`task.authors[1].affiliation`). A handful of keys are admitted for one value
only, and the same error names them when they carry any other value.

What v1 accepts: `schema_version`, `artifacts`, `[task]` (`name`, `version`,
`description`, `keywords`, `[[task.authors]]` with `name`/`email`), the
free-form `[metadata]` table, `[agent].timeout_sec`, `[verifier]`
(`timeout_sec`, `environment_mode`, `network_mode`, `[verifier.environment]`),
and `[environment]`/`[verifier.environment]` limited to `network_mode`,
`build_timeout_sec`, `cpus`, `memory_mb`, `storage_mb`, plus `docker_image` on
`[environment]` only — where it gets its own `prebuilt_image_unsupported`
refusal.

#### Keys admitted for one value only

Seven further keys are admitted, but each for exactly one value: the inert one.
M009 finding F-06 showed the surface above had been drawn from what the test
fixtures declare rather than from what `library/tasks/` declares, so the
workbench could not certify a single one of the four tasks this repository runs
— `library/tasks/event-summary` alone tripped three refusals. Every real
occurrence of these keys in the task library is empty or the schema default,
which Harbor folds away, so admitting the inert value costs nothing while
admitting the key outright would reopen the hole the closed surface exists to
shut: an inert `mcp_servers = []` and a populated one are the same key.

`_MODELLED_CONSTRUCT_VALUES` is that model, one entry per key per side, and each
entry carries the Harbor 0.21.0 source line behind its refusal.

| Key | Accepted value | Any other value is refused because |
|---|---|---|
| `os` (both tables) | `"linux"` | It is the `TaskOS` default. `os = "windows"` makes Harbor raise rather than enforce isolation: `DockerEnvironment` rejects Windows containers whenever egress control is required (`environments/docker/docker.py:218-222`), which is every `network_mode` other than `public` (`:265-275`). It also switches the file-transfer/exec platform and the artifact convention source. |
| `gpus` (both tables) | `0` | Harbor folds `0` to the same value as omission (`environments/base.py:367-369`). A nonzero request cannot run under the local controls at all — `DockerEnvironment` leaves `EnvironmentCapabilities.gpus` at `False`, so Harbor raises (`environments/base.py:745-750`) — and the GPU-capable providers it steers to are cloud environments v1 does not model. |
| `mcp_servers` (both tables) | `[]` | Harbor merges every declared server into the agent's constructor kwargs with no network-policy filter (`trial/trial.py:829-837`), and `MCPServerConfig` admits both a remote `url` and a `command` the stdio transport spawns (`models/task/config.py:616-636`). That is an egress and exec path beside the `network_mode` the workbench reasons about. |
| `env` (`[environment]`, `[verifier.environment]`, `[verifier]`, `[solution]`) | empty table | `resolve_env_vars` substitutes `${VAR}` from the *host* environment at runtime and raises when it is unset (`utils/env.py:94-130`), so a populated table makes the container a function of the workstation and is the documented API-key channel (`verifier/verifier.py:166-171` warns about exactly that; `trial/trial.py:778-813` scrubs the resolved values afterwards). |
| `verifier.collect` | `[]` | Each hook is a shell command run with `service_exec` inside a compose service after the agent phase, best-effort, a nonzero exit only logged (`trial/trial.py:999-1029`) — an unscanned command under the agent environment's network policy, in a service the build-context scan never sees. |

`[solution]` is admitted as a table because `SolutionConfig` carries exactly one
field, `env` (`models/task/config.py:335-336`); naming it closes the table.

A key admitted for one value is decided by that value and never descended into,
so an allowlisted parent cannot shelter an unmodelled child.

Everything else is still refused, including `[[steps]]` and
`multi_step_reward_strategy`, `source`, `verifier.user`, `agent.network_mode`,
`agent.allowed_hosts`, `allowed_hosts`, `healthcheck`, `skills_dir`, `workdir`,
`gpu_types`, `tpu`, and `docker_image` under `[verifier.environment]`.

#### `allow_internet` is still refused, deliberately

The deprecated `[environment].allow_internet` alias remains outside the surface
even though `library/tasks/query-optimize` declares it. Harbor folds it into
`network_mode` in a model validator, but only when `network_mode` is absent from
`model_fields_set` *and* `allowed_hosts` is `None`
(`models/task/config.py:885-892`). Mirroring that three-way interaction would
put a second, weaker network resolver beside `_effective_verifier_network`,
which is exactly the shape of the two holes the earlier review rounds found. A
task states its policy with an explicit `network_mode` instead. This is the one
`unsupported_task_configuration` still reachable from the current task library,
and it is a refusal on the merits rather than an oversight.

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
never the allowlist alone. F-06 is the worked example of the second path, and of
why the rule is binding: the keys it admitted each arrived with a
`_MODELLED_CONSTRUCT_VALUES` entry, and
`test_every_key_admitted_for_one_value_arrives_with_that_value_model` fails if a
later key is added without one, or if a rename leaves an existing model
unreachable from the allowlist and therefore silently not running.

Widening the surface does not mean a task certifies. After F-06 the four in-repo
packages carry no `harness_defect` for a construct the workbench decided to
model, and they still fail static acceptance on genuine task findings —
`adversarial_cases_insufficient`, `base_image_unpinned`, `source_ref_unpinned`,
`verifier_image_unpinned`, `verifier_network_not_isolated` and the rest. Those
are findings about the tasks, and the workbench is correct to keep reporting
them.

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

Eight refusals exist specifically to stop the packet from claiming isolation it
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
- `custom_compose_unsupported` — a build context contains a Compose file. Harbor
  layers an environment directory's Compose file into `docker compose build` and
  `up` alike, and excludes any service declaring its own `network_mode` or
  `networks` from the egress control that implements `no-network`, so v1 cannot
  prove isolation for task-authored Compose services; refused in `environment/`
  and in `tests/`, which is what Harbor hands the separate verifier;
- `compose_env_file_unsupported` — a build context contains `.env`. Harbor runs
  `docker compose --project-directory` on that directory and never passes
  `--env-file`, so Compose interpolates the file into every Compose document,
  Harbor's own included;
- `build_context_ignore_unsupported` — a build context contains `.dockerignore`,
  so the files the workbench scanned are not the files that reach the image;
- `verifier_network_not_isolated` — the verifier's effective *baseline* network
  is not `no-network`. Because Harbor drops the overlay for the verifier, a
  networked verifier can exfiltrate hidden inputs, and it can make
  `verifier_deterministic` an artifact of a stable remote response rather than
  of a deterministic verifier;
- `verifier_phase_network_not_isolated` — `[verifier].network_mode` reopens the
  network for the verification phase itself.

`build_network_use`, `build_context_unreadable`, and the three filename refusals
are enforced for both images, but only the agent image also has container-level
denial behind them. The two `verifier_*_not_isolated` refusals are static
declaration checks, not runtime denial, and `unsupported_task_configuration` is a
limitation of the workbench rather than a finding about the task; see
[What denies the network, and where](#what-denies-the-network-and-where) and
[The build-context filenames v1 understands](#the-build-context-filenames-v1-understands).

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

`tests/fixtures/task_workbench/cases/inert-surface/task.toml` is the same
fixture plus every construct F-06 admitted, each carrying the inert value the
real packages declare. It must certify with zero diagnostics, and its resolved
network policy must be identical to the reference document's: that pair of
assertions is what makes "the inert declaration is equivalent to omitting the
key" a checked claim rather than a comment.
