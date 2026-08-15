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
For every control, the workbench also injects a fixed Docker Compose overlay
that forces Harbor's `main` service to
`network_mode: none`. The overlay is named in the frozen command, included in
the staged-task digest, and revalidated after the run. Text scanning is defense
in depth, not the network safety boundary.

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
