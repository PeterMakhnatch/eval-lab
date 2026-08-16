Status: review-wanted
Last: repaired the three P1 false-certification paths found at c6c35a4; focused tests and lint green
Next: integrator re-review and merge decision; the committed candidate-ee3d580b packet is now false and needs an integrator call
Blockers: none

# M007 task-quality workbench handoff

Executing agent/model: OpenAI Codex / GPT-5. The runtime does not expose a
more specific deployment identifier, so no unobservable suffix is invented.

Branch: `role/m007-task-workbench`

Baseline: `origin/main` at `00f36ab` (`INTEGRATION: release M006 and M007`).

Boundaries observed so far:

- No live model, cloud, paid, registry, queue, policy, publication, or task-byte
  mutation has occurred.
- `library/registry/` contains only `.gitkeep`; the normative REGISTER review
  packet likewise records zero registered tasks. M007 will not manufacture a
  registration record to satisfy wording that assumes one exists.
- `event-summary` is the repository's documented candidate for initial human
  admission and has promoted oracle/nop evidence. It is the honest existing-task
  exercise target unless a human-created registry record lands before final rebase.
- The available `create-task` guidance was read. It reinforces separate verifier
  environments, absolute verifier paths, Oracle solvability checks, and keeping
  `tests/` and `solution/` out of the evaluated image.

Implementation:

- `src/evallab/task_workbench.py` provides the intentionally unregistered
  `python -m evallab.task_workbench plan|check|packet` surface. There is no
  shared CLI, queue, profile, policy, dashboard, analysis, or ACTIVE wiring.
- Inspection freezes the complete file manifest plus source, config, image,
  instruction, solution, verifier, adversarial, artifact, command, and control
  digests. Commands are stored with `$REPO`, so clone location is not identity.
- Static admission checks task schema/metadata/timeouts/artifacts, executable
  entry points, path and symlink escapes, agent/verifier isolation, pinned
  images and dependencies, JSON/shell Docker copies, explicit network policy
  and runtime network use,
  deterministic verifier constructs, reward output, hidden/golden leakage,
  adversarial coverage, source/license provenance, and forged registration
  claims.
- Static failure makes zero control calls. Admitted controls are fixed to three
  Oracle jobs, one Nop job, and every declared invalid solution; the latter run
  as Oracle only in isolated staging copies. The command fixes Docker, one
  attempt, concurrency one, and a digest-frozen Compose override that enforces
  `main.network_mode = none`. No model/cloud/paid execution is reachable.
- Complete bundles are reused idempotently. Incomplete, missing, or
  digest-invalid evidence is preserved and fails closed rather than being
  overwritten or silently retried. Outcomes are explicitly classified as task
  defect, harness defect, agent failure, or expected.
- Supplied bundle claims never certify by themselves. The assessor resolves
  exact candidate job/stage paths, recomputes both trees and reward vectors,
  checks the free agent and separate verifier, and rejects missing or changed
  evidence. Packet-local evidence records retain scrubbed raw result objects
  plus complete manifests while omitting output/log content that could leak a
  golden answer.
- Packet writes are restricted to `research/registration/candidates/`, use
  create-or-verify semantics, and cannot target registry/queue/policy/outside.
  Both packet documents say admission is false and requires a separate human
  registry record.
- `tests/test_task_workbench.py` and the fixture corpus cover the requested
  valid, missing-file, path-escape, hidden-leak, nondeterminism, permissive,
  false-negative, network, unpinned, forged-registration, and interrupted cases.

Repository-task exercise:

```text
target: library/tasks/event-summary
result: needs_changes
task defects: adversarial_cases_insufficient, base_image_unpinned,
              verifier_image_unpinned
control calls: 0
git diff --exit-code -- library/tasks/event-summary: 0
```

This is the repository's documented initial-admission candidate, not a
registered task. `library/registry/` still contains only `.gitkeep`; inventing a
record would violate the mission's central boundary.

Bad-fixture exercise:

```text
target: tests/fixtures/task_workbench/cases/unpinned-dependency
candidate: candidate-b982b6f484cdc89e9e35d8b6
result: needs_changes (21 retained task-defect diagnostics; no controls)
candidate sha256: bb338ae9e282d09af31e31b8efe1c1714dd4c09407ca6a0afda8b3a44d60a8b1
certification sha256: 01484a8852ebb5b8407cd0cc747e4784b3f80d5fb4050cf3081175e434257c23
second packet build: identical hashes and expected exit 1
```

Real free-control evidence:

```text
candidate: candidate-ee3d580b186b15e6e55a1ab9
source: local/m007-uppercase-fixture@1.0.0 (MIT, synthetic)
bundle: sha256:e6e4863cdee165d7617997279d6ae24dd87aeb2dc4b711bf91214174954916a0
oracle-1, oracle-2, oracle-3: completed, reward 1.0
nop-1: completed, reward 0.0
adversarial-empty-output: completed, reward 0.0
adversarial-extra-artifact: completed, reward 0.0
adversarial-wrong-value: completed, reward 0.0
verifier output: identical across all three Oracle runs
models/cloud/paid calls: 0
concurrency: 1
network: Docker main service forced to network_mode none for every control
status: certified_for_review (not admitted)
retained packet evidence: 7 per-control records; no outputs/logs/golden bytes
candidate sha256: 0ae5720b48d05669e3eb2f613b723047feac6a6169be5f3c5fce707f3674524c
certification sha256: 8e765efb80bf9a03f397e0564dcde5fbfa91541fc165baa5ecb7379178231ef2
second packet build: identical hashes
```

Earlier local control cycles were also retained under the ignored
`runs/task-workbench/` evidence root. The first exposed a Harbor/macOS Docker
backend limitation for `network_mode = "no-network"`; the second exposed that
the Oracle runtime requires the task image's shell contract (the initial Alpine
fixture lacked it and produced `RewardFileNotFoundError`). These were classified
as incomplete evidence rather than agent failures. A later run then correctly
caught an invalid probe the verifier could not observe: Harbor transfers only
declared artifacts, so an undeclared extra file alone was accepted. The probe
was changed to add unexpected content to the declared result as well; the final
pinned Ubuntu fixture then produced a public-network bundle. The Tasks review
correctly refused that bundle as non-portable and insufficiently isolated. The
final run above uses the injected no-network override, revalidates raw job/stage
bytes, and retains portable evidence.

Initial setup evidence:

```text
origin/main: 00f36ab INTEGRATION: release M006 and M007 (#46)
uv sync --locked: installed successfully with CPython 3.12.11
open PRs at dispatch: none
```

Local verification evidence:

```text
uv run pytest tests/test_task_workbench.py -q
............................. [100%]
29 passed

uv run ruff check .
All checks passed!

uv run pytest -q
401 passed

scripts/premerge.sh
Resolved 43 packages; audited 41 packages
All checks passed!
401 passed in 18.06s
doctor/smoke: PASS (both stores agree)
ty: 28 diagnostics; premerge green because ratchet is 28 <= 28
premerge green: Python 3.12

git fetch origin; git rebase origin/main
origin/main: aee9b81
Successfully rebased and updated role/m007-task-workbench.
validated code head before this handoff-only update: 830c5bd
```

No M007 file adds an API-key variable, model selector, queue import, registry
write, publication path, or absolute home path. Packet admission remains false.
The final self-audit also made unobserved control claims fail closed: a
`controls_pending`, interrupted, or static-failure packet cannot report Oracle,
Nop, adversarial, or verifier-determinism checks as true.

Independent Tasks review:

```text
reviewed head: e8e9005
result: not approved
blocking: caller-authored controls could certify without retained evidence;
          Docker JSON-array COPY could bypass hidden-source detection
additional: public-network safety relied on regexes; build defects could be
            mislabeled harness defects; ignored runs made evidence non-portable
```

All findings were accepted and addressed before PR: local evidence and stage
recomputation are mandatory, Docker copy parsing fails closed, controls use a
real no-network Compose override, build/task exceptions classify as task
defects, and seven portable evidence records are included without output or
verifier-log content. A fresh Tasks re-review is required on the fixed head.

The second Tasks review cleared those findings but found one additional
blocking provenance gap: the Harbor job bytes were not yet bound to the
candidate task identity. The final validator now requires the raw trial result
and lock to match the candidate task name/version, exact staged path, planned
free agent, Docker environment, separate verifier, exact Compose override path
and digest, and a Harbor-compatible package digest recomputed from the stage.
A regression copies a different task identity under the expected job path,
updates its tree digest, and confirms certification still fails.

Final independent Tasks decision:

```text
reviewed exact code head: 830c5bd5e1bdd453be3972caecfc2a4897719f02
result: APPROVED; no blocking Tasks findings
scope verified: candidate/stage/task digest, free agent, separate verifier,
                no-network overlay, portable evidence, wrong-task regression
review mode: read-only; no state changed
```

## Integrator repair round

The original worker had stopped before this repair. A sole repair writer resumed
the same worktree and accepted the integrator's five blocking findings. The
execution boundary now re-inspects the frozen candidate path and bytes before
creating runtime state; the concrete Harbor backend independently validates the
candidate record, plan semantics, command, source manifest, and staged bytes
before a subprocess can run. Static admission rejects every candidate symlink,
remote Docker `ADD`, and build-time fetch or online package-manager command.

Verifier determinism now digests the actual canonical file tree retained under
each Harbor trial's `verifier/` directory. The nondeterminism regression changes
a retained verifier stdout file and uses that production derivation; it no
longer injects a synthetic digest seed. The successful candidate packet was
mechanically regenerated from its already-retained local Oracle/Nop jobs, with
no new Harbor, Docker, model, cloud, or paid call. The intentionally failed
candidate packet was preserved unchanged.

Certification check-vector claims now require clean recomputed control evidence.
Wrong task identity, network overlay binding, or verifier isolation makes the
control subclaims and isolation false. The real module CLI `check --run-controls`
is covered through an injected `HarborControlBackend` subprocess runner, proving
the materialized paths, overlay, free agents, Docker backend, one attempt, and
concurrency one.

Focused repair evidence:

```text
.venv/bin/python -m pytest tests/test_task_workbench.py -q
...................................                                      [100%]
35 passed

.venv/bin/ruff check src/evallab/task_workbench.py tests/test_task_workbench.py
All checks passed!

git fetch origin; git rebase origin/main
Successfully rebased onto 903abe4 (INTEGRATION: add system cartographer mission)

scripts/premerge.sh
All checks passed!
407 passed in 17.96s
SMOKE PASS both-stores-agree
ty: 28 diagnostics; premerge green because ratchet is 28 <= 28
premerge green: Python 3.12
```

PR and GitHub verification:

```text
PR: https://github.com/PeterMakhnatch/eval-lab/pull/49
title: M007: add task-quality workbench
merge action: none (stopped at review as required)
lint: pass https://github.com/PeterMakhnatch/eval-lab/actions/runs/31905555156/job/95062631952
test (3.12): pass https://github.com/PeterMakhnatch/eval-lab/actions/runs/31905555156/job/95062631936
test (3.14): pass https://github.com/PeterMakhnatch/eval-lab/actions/runs/31905555156/job/95062631968
profile: pass https://github.com/PeterMakhnatch/eval-lab/actions/runs/31905555195/job/95062632150
ty: pass https://github.com/PeterMakhnatch/eval-lab/actions/runs/31905555242/job/95062632539
```

## Second repair round: three P1 false-certification paths

An independent exact-head review of `c6c35a4` returned `incorrect`. This round
repairs only those three findings. The digest/path binding, symlink containment,
determinism semantics, no-auto-registration, and no-API-key repairs were
reviewed as correct and were not touched.

### Defect 1 — build-time network denial was not enforced at the build

**Was:** `NETWORK_OVERLAY_CONTENT` set `services.main.network_mode: none`. That
is a Compose *runtime* key. Harbor 0.21.0 builds the agent image with
`docker compose ... build` against its bundled `docker-compose-build.yaml`,
which declares `build.context` and no `build.network`, so nothing constrained
the build network. `_validate_build_network`'s regex over Dockerfile logical
lines was the only barrier.

**Now:** the overlay declares both keys:

```yaml
services:
  main:
    build:
      network: none
    network_mode: none
```

**Build-time network denial is ENFORCED, not reported-as-unverified.** Basis,
read in the installed Harbor 0.21.0 under
`~/.local/share/uv/tools/harbor/lib/python3.12/site-packages/harbor`:

- `environments/docker/docker.py:366` — `_docker_compose_paths` appends
  `extra_docker_compose_paths`, so the workbench overlay is in the path list.
- `environments/docker/docker.py:623` — every path in that list becomes a `-f`
  argument.
- `environments/docker/docker.py:891` — `start()` runs
  `_run_docker_compose_command(["build"])`, so the overlay is part of the build
  invocation, not only `up`.
- Live merge check (`docker compose config`, Compose v5.1.3) of Harbor's
  `docker-compose-build.yaml` with the new overlay resolves to
  `build: {context: …, dockerfile: Dockerfile, network: none}` — the mapping
  merges rather than replacing the context.

Enforcement scope, stated exactly:

- Agent image build: enforced by `build.network=none`.
- Agent runtime: enforced by `network_mode=none`.
- Verifier image build: **not** overlay-covered (Harbor discards
  `extra_docker_compose` for the verifier), so it remains static-scan-only over
  `tests/`. The packet now says this in
  `network_policy.verifier_build_network` rather than implying overlay coverage.

Capability label: the overlay's build-network denial is **fixture-proven only**
end to end — the compose merge is `proven live`, but no Docker build was run,
since this mission forbids Docker builds. `network_policy.control_enforcement`
no longer claims more than the code applies.

Related guard added in the same commit: `[environment].docker_image` is now
refused (`prebuilt_image_unsupported`). Harbor's
`should_use_prebuilt_docker_image` returns true whenever that key is set, which
skips the reviewed `environment/Dockerfile` build entirely — the build-network
claim would be vacuous and the reviewed image would never be the image run.

Proven by: `test_network_overlay_denies_the_build_network_not_only_the_runtime`
(parses the overlay and asserts `services.main.build.network == "none"`), and
`test_prebuilt_docker_image_bypassing_the_reviewed_build_is_rejected`.

### Defect 2 — `environment/` file contents were never scanned

**Was:** `_validate_network_and_isolation` content-scanned only `tests/`,
`verifier/`, and `solution/`. `_validate_build_network` read only
`environment/Dockerfile` and `tests/Dockerfile`. An `environment/Dockerfile`
with `COPY setup.sh /tmp/setup.sh` then `RUN sh /tmp/setup.sh` matched no
pattern, `setup.sh` passed the COPY source checks, and its bytes were never
read.

**Now:** `_validate_build_context_contents` reads every regular file under
`environment/` and applies `BUILD_NETWORK_PATTERN`. `environment/Dockerfile` is
skipped there because `_validate_dockerfile` already scans it with Dockerfile
line semantics (continuations joined, comments and `FROM` refs exempt). A file
that cannot be decoded as UTF-8 is refused (`build_context_unreadable`) instead
of skipped, matching the fail-closed posture of `_docker_copy_sources`.

Proven by: `test_build_context_script_bypassing_the_dockerfile_is_rejected`,
which uses the new fixture
`tests/fixtures/task_workbench/cases/build-context-script/` reproducing exactly
the COPY-then-RUN-script bypass. The test asserts the Dockerfile itself contains
no `curl` and no `https://`, so the refusal can only come from reading
`environment/setup.sh`. `test_unscannable_build_context_file_fails_closed`
covers the undecodable case.

### Defect 3 — a networked verifier environment was accepted

**Was:** `network_mode = "public"` for the verifier produced no diagnostic, and
an absent `[verifier.environment]` silently inherited `[environment]`.

**Now:** `_effective_verifier_network` resolves the verifier's exposure the way
Harbor resolves it, verified against the installed source:

- `models/task/verifier_mode.py:60-64` —
  `resolve_effective_verifier_env_config` uses `task.verifier.environment` when
  present, otherwise a deep copy of `task.environment`.
- `models/task/config.py:249-252` — `EnvironmentConfig.network_mode` defaults to
  `public`, so a `[verifier.environment]` table that omits the key is public and
  does **not** inherit the other table's value.
- `trial/network_policy.py:121-131` — `[verifier].network_mode` is a *phase*
  override applied during `verify()`; the container still starts at the
  baseline.
- `trial/trial.py:648-650` — `extra_docker_compose` is emptied for the verifier
  runtime config, which is why the workbench overlay cannot rescue this.

`verifier_network_not_isolated` is an ERROR when the effective baseline is not
`no-network`, naming the resolved value and whether it came from
`[verifier.environment]` or from `[environment] (inherited)`.
`verifier_phase_network_not_isolated` is an ERROR when an explicit
`[verifier].network_mode` reopens the network for the verification phase. Both
codes also appear in the certification `isolation` vector.

`tests/fixtures/task_workbench/valid/task.toml` declared
`[environment] network_mode = "public"` with no `[verifier.environment]`; it now
declares `no-network`, so the valid case is genuinely isolated.

Proven by: `test_networked_verifier_environment_is_rejected` (new fixture
`cases/networked-verifier/`, the exact inherited-public shape),
`test_verifier_environment_table_does_not_inherit_a_no_network_default`, and
`test_verifier_phase_override_cannot_reopen_the_network`.

### Committed packet that is now false — integrator decision required

`research/registration/candidates/` is outside this task's lease, so nothing
there was edited.

- `research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/certification.json`
  reports `certified: true`, `status: certified_for_review`, and
  `check_vector.isolation: true` for `tests/fixtures/task_workbench/valid`.
  Those control runs used a verifier container with full egress (the fixture
  declared `[environment] network_mode = "public"` with no
  `[verifier.environment]`), and their agent image was built with an
  unconstrained build network. Both claims are false; under the repaired code
  the same candidate would fail static admission. The candidate record also
  pins `control_enforcement: "docker-compose main network_mode=none"` and the
  old overlay digest, neither of which the code produces any more.
- `research/registration/candidates/candidate-b982b6f484cdc89e9e35d8b6/certification.json`
  is `certified: false` / `needs_changes`, so it asserts no false trust, but its
  candidate record carries the same stale `control_enforcement` string and
  overlay digest.

Recommendation for the Integrator: regenerating the `ee3d580b` packet is not
possible without new control runs, and its retained evidence describes runs that
no longer satisfy the isolation contract. Withdrawing or superseding the packet
is the honest option; this worker did not touch it.

`docs/task-workbench.md` (also outside this lease) still describes the overlay
as forcing only `network_mode: none` and does not mention build-context content
scanning or the verifier-isolation requirement. It is stale, not wrong about
anything else. Reported, not edited.

### Verification

```text
uv run pytest tests/test_task_workbench.py -q
..........................................                               [100%]
42 passed

uv run ruff check src/evallab/task_workbench.py tests/test_task_workbench.py
All checks passed!
```

Negative control: with `src/evallab/task_workbench.py` reverted to `HEAD` and
the new tests and fixtures kept, all seven new tests fail
(`build_context_unreadable` absent, `verifier_network_not_isolated` absent,
`prebuilt_image_unsupported` absent, `KeyError: 'build'` on the overlay). The
pre-fix file was restored immediately afterwards.

No Harbor run, Docker build, model call, cloud sandbox, or publication occurred.
No API-key environment variable was introduced or read. Nothing under `policy/`
was touched. No path in this diff registers a task or grants promotion powers.
The full suite, project-wide formatters, and `scripts/premerge.sh` were left to
the Integrator per this mission's constraints; this repair invalidates the
previous green CI on PR #49, as expected.
