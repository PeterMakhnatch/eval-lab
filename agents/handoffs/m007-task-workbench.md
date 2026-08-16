Status: review-wanted
Last: fourth repair round — applied the allowlist discipline outside `task.toml`: a per-context filename allowlist that refuses task-authored Compose, `.env`, and `.dockerignore` in `environment/` and `tests/` (closing the `tests/docker-compose.yaml` escape past Harbor's egress control), ~20 plain install idioms added to the build-time scan, and the Harbor drift pin split so a static pin runs with no Harbor installed; 102 tests pass, ruff green
Next: integrator re-review and merge decision on PR #49; `.github/workflows/ci.yml` recommendation in the fourth-round section is theirs to take or leave
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
result: needs_changes (23 retained task-defect diagnostics; no controls)
candidate sha256: 91d215459356f98c42b1b05304f68a12c911ab09e27fc77187928fe6d7271051
certification sha256: 2ed2cc2a4ee3e6b781b3706b688643af1976dc5dd3ee6bd6505a8a1513cf5056
second packet build: identical hashes and expected exit 1
note: refreshed at 2a6aec0 by re-running the repaired inspector over unchanged
  fixture bytes. Was 21 diagnostics / candidate bb338ae9 / certification
  01484a88 before the repair; the repaired code additionally detects
  build_network_use (environment/Dockerfile) and verifier_network_not_isolated
  (task.toml) in this deliberately bad fixture.
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

### Packet withdrawal and documentation correction (extended lease at 2a6aec0)

The Integrator granted an extended lease for exactly
`research/registration/candidates/` and `docs/task-workbench.md`, which the
repair invalidated but the previous mission could not touch. No `src/`,
`tests/`, `policy/`, or `library/` file was changed in this round; zero Python
changed and `uv run ruff check .` passes (confirmed, not assumed).

#### The false packet is withdrawn

`candidate-ee3d580b186b15e6e55a1ab9/certification.json` asserted
`certified: true`, `status: certified_for_review`, and
`check_vector.isolation: true`. It now reads `certified: false`,
`status: needs_changes`, every `check_vector` entry `false`, and carries an
explicit `withdrawal` object.

**The schema has no withdrawal state.** `Disposition` is the closed literal
`{needs_changes, controls_pending, harness_blocked, certified_for_review}` and
`CheckReport.passed` is defined as `disposition == "certified_for_review"`, so
`certified` and `status` are not independently settable in any honest way. Two
mechanisms were rejected: inventing `status: "withdrawn"` would put a value
outside the `Literal` into a data file with no code support, and deleting the
packet would destroy the evidence of the false claim. The chosen mechanism is
the least-surprising one that cannot be misread as live:

- `status` and `certified` are set to the values the repaired code *actually
  computes for these bytes*. Verified, not assumed: the exact certified bytes
  were recovered from `285b834^` (`task.toml` sha256 `31d7d7e9…`, byte-identical
  to the digest frozen in the candidate record) and re-inspected under the
  repaired code. Result: `static_passed: False` with exactly one error,
  `verifier_network_not_isolated`, which `check_candidate` maps to
  `needs_changes`. The whole `check_vector` goes false because every entry is
  gated on `verified_controls`, which requires `static_passed`.
- The `withdrawal` object records the date, the two exposures (verifier egress
  from an inherited `[environment] network_mode = "public"` that Harbor never
  covers with the overlay, and an unconstrained build network from an overlay
  that set only the Compose runtime key), the head where the contract changed
  (`2a6aec0`, fix commit `285b834`), the superseded status and check vector, and
  that re-certification requires fresh control runs. **No control runs, Harbor
  runs, or Docker builds were performed.**
- `control_bundle`, `control_plan`, and all seven `evidence/` files are retained
  byte-unchanged. They remain accurate about what was run; they are simply no
  longer accepted as certification evidence.
- `certification_id` is deliberately **not** recomputed. It is retained as the
  historical identity of the withdrawn certification; minting a fresh
  content-addressed id for a withdrawal would read as a new certification event.
  The record says so explicitly. Nothing in the code re-validates
  `certification_id`, so this breaks no invariant.
- `human_action_required` now states the packet is withdrawn and grants nothing,
  instead of inviting a reviewer to review it.

`candidate-ee3d580b…/candidate.json` was **not** edited. Its
`control_enforcement: "docker-compose main network_mode=none"` and old overlay
digest are accurate historical facts about control runs that really executed
under that overlay; rewriting them to the new string would assert that those
runs had build-network denial, which is exactly the kind of unbacked claim this
withdrawal exists to remove. Its `candidate_record_digest` seal was verified
self-consistent and is left intact.

#### Stale sibling: refreshed, deliberately

`candidate-b982b6f484cdc89e9e35d8b6/` **was** refreshed, by re-running the
repaired inspector over unchanged fixture bytes and writing the code's own
output — not by hand-patching strings. The opposite call from `ee3d580b`, on a
difference that is observable in the data:

1. Its `control_bundle` is `null`. **No control run ever executed**, so
   `control_enforcement` and `control_overlay_digest` cannot be historical facts
   about a past run. They are the workbench's forward-looking description of the
   enforcement it *would* apply — a claim about the current contract, and the
   contract changed. That is the distinguishing test between the two packets.
2. The underlying fixture bytes are unchanged since the packet was written; the
   only branch change to `cases/unpinned-dependency/` came from the original
   feature commit `5e60d54`, not from the repair. Re-inspection is therefore
   over identical inputs — a refresh, not a new run.
3. The refresh is not cosmetic. The repaired code detects two additional real
   defects the committed packet omitted: `build_network_use`
   (`environment/Dockerfile`) and `verifier_network_not_isolated` (`task.toml`),
   21 diagnostics to 23. Leaving it would leave a packet that under-reports.
4. Because the record is regenerated rather than edited, `candidate.json` stays
   digest-sealed and `certification.json`'s `candidate_record_digest` and
   `certification_id` stay self-consistent and independently reproducible by
   re-running the inspector.

Verified afterwards across both packets: no `certified: true`, no `check_vector`
entry true, both `candidate_record_digest` seals valid, and both certifications
referencing their candidate's current digest.

#### Documentation

`docs/task-workbench.md` no longer describes the overlay as forcing only
`network_mode: none`. It now separates the four phases with a table and states
the boundary for each: agent build and agent runtime denied by the overlay at
the container runtime; verifier build and verifier runtime covered **only by a
static text scan**, because Harbor discards `extra_docker_compose` for the
verifier config. The honest limitations are stated rather than smoothed over:
the verifier build has no `build.network: none` and no fail-closed rule, so a
non-UTF-8 file under `tests/` is skipped silently, unlike under `environment/`.
The four new refusals (`prebuilt_image_unsupported`, `build_context_unreadable`,
`verifier_network_not_isolated`, `verifier_phase_network_not_isolated`) are
documented, and the reference-fixture section records the withdrawal and the
schema's lack of a withdrawal state.

#### Branch file list (`git diff --name-only origin/main...HEAD`)

```text
agents/handoffs/m007-task-workbench.md
docs/task-workbench.md
research/registration/candidates/candidate-b982b6f484cdc89e9e35d8b6/candidate.json
research/registration/candidates/candidate-b982b6f484cdc89e9e35d8b6/certification.json
research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/candidate.json
research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/certification.json
research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/evidence/adversarial-empty-output.json
research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/evidence/adversarial-extra-artifact.json
research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/evidence/adversarial-wrong-value.json
research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/evidence/nop-1.json
research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/evidence/oracle-1.json
research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/evidence/oracle-2.json
research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/evidence/oracle-3.json
src/evallab/task_workbench.py
tests/fixtures/task_workbench/cases/build-context-script/environment/Dockerfile
tests/fixtures/task_workbench/cases/build-context-script/environment/setup.sh
tests/fixtures/task_workbench/cases/false-negative-verifier/controls.json
tests/fixtures/task_workbench/cases/forged-registration/task.toml
tests/fixtures/task_workbench/cases/golden-symlink/case.json
tests/fixtures/task_workbench/cases/hidden-leak/instruction.md
tests/fixtures/task_workbench/cases/interrupted-controls/controls.json
tests/fixtures/task_workbench/cases/json-copy-leak/environment/Dockerfile
tests/fixtures/task_workbench/cases/missing-files/case.json
tests/fixtures/task_workbench/cases/network-use/tests/test.sh
tests/fixtures/task_workbench/cases/networked-verifier/task.toml
tests/fixtures/task_workbench/cases/nondeterminism/controls.json
tests/fixtures/task_workbench/cases/path-escape/case.json
tests/fixtures/task_workbench/cases/permissive-verifier/controls.json
tests/fixtures/task_workbench/cases/unpinned-dependency/environment/Dockerfile
tests/fixtures/task_workbench/valid/environment/Dockerfile
tests/fixtures/task_workbench/valid/environment/input.txt
tests/fixtures/task_workbench/valid/instruction.md
tests/fixtures/task_workbench/valid/solution/solve.sh
tests/fixtures/task_workbench/valid/task.toml
tests/fixtures/task_workbench/valid/tests/Dockerfile
tests/fixtures/task_workbench/valid/tests/golden.txt
tests/fixtures/task_workbench/valid/tests/test.sh
tests/fixtures/task_workbench/valid/tests/verify.sh
tests/fixtures/task_workbench/valid/workbench/adversarial/empty-output.sh
tests/fixtures/task_workbench/valid/workbench/adversarial/extra-artifact.sh
tests/fixtures/task_workbench/valid/workbench/adversarial/wrong-value.sh
tests/test_task_workbench.py
```

42 files. `src/` and `tests/` appear only because of the earlier repair commit
`285b834`, which is under independent review; this round changed neither. The
list is byte-identical before and after this round's commit — verified, because
every file this round touched was already in it.

#### Unrelated pre-existing state, reported not fixed

`research/registration/candidates/` contains two **empty** local directories,
`candidate-13091041b42634c8c70c5912/` and `candidate-1c7115a30f231b99119da9dd/`,
both dated 2026-08-15 15:21, before this round. Git does not track empty
directories, so they are invisible to `git status` and are not part of any
commit. They look like scratch from an interrupted packet write. Left alone.

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

### Verification — withdrawal round (2a6aec0)

```text
uv run ruff check .
All checks passed!

git status --porcelain   # before commit, this round
 M docs/task-workbench.md
 M research/registration/candidates/candidate-b982b6f484cdc89e9e35d8b6/candidate.json
 M research/registration/candidates/candidate-b982b6f484cdc89e9e35d8b6/certification.json
 M research/registration/candidates/candidate-ee3d580b186b15e6e55a1ab9/certification.json
(zero .py files; agents/handoffs/m007-task-workbench.md added after)
```

Ground truth for the withdrawal, re-derived rather than assumed:

```text
# exact certified bytes recovered from 285b834^
task.toml sha256 31d7d7e9917b4702bbc92af69d69818b0beae9c022a2ce5e994ec8fdbfb3cff0
  == digests.task_toml frozen in candidate-ee3d580b's candidate.json

# repaired inspect_candidate over those exact bytes
static_passed: False
[error] verifier_network_not_isolated (task.toml)
  "the effective baseline is 'public' from [environment] (inherited)"
-> check_candidate maps any static error to needs_changes; passed == False

# post-edit sweep over research/registration/candidates/
candidate-b982b6f4: certified=False status=needs_changes check_vector all false
                    seal valid; cert digest matches candidate.json
candidate-ee3d580b: certified=False status=needs_changes check_vector all false
                    withdrawal object present
                    seal valid; cert digest matches candidate.json
ALL CLEAN: True
```

This round changed no Python, so the repair round's pytest result stands
unchanged and the suite was not re-run. No Harbor run, Docker build, container
build, model call, cloud sandbox, deploy, or publication occurred. No API-key
environment variable was introduced or read. Nothing under `policy/`,
`library/`, `src/`, or `tests/` was touched this round; `library/registry/`
still holds zero records and nothing here registers a task. The full suite,
project-wide formatters, and `scripts/premerge.sh` were left to the Integrator.
Stopping at `review-wanted`: not merged, and no new PR opened — #49 already
exists.

## Third repair round: closing the false-certification CLASS

Two successive independent reviews each found a *new* path by which a task whose
verifier or image build reaches the network still earned
`certified_for_review`. Both findings had the same shape: the workbench
re-derives Harbor's configuration resolution, and any table or path the mirror
fails to reproduce silently yields a false `isolation: true`. Enumerating what
to check cannot terminate, so this round changes the posture rather than adding
two more checks.

### The structural fix: fail closed on unrecognised configuration

`SUPPORTED_TASK_CONFIG` in `src/evallab/task_workbench.py` is now an explicit,
exhaustive allowlist of the `task.toml` tables and keys this workbench version
claims to reason about. `_validate_supported_configuration` walks the parsed
document and emits `unsupported_task_configuration` for anything outside it,
naming the exact offending path — `steps`, `verifier.collect`,
`task.authors[1].affiliation`. Arrays of tables are indexed, and an allowlisted
scalar key that arrives as a table is refused too (shape the workbench never
modelled either).

**Classification: `harness_defect`, severity `error`.** Chosen deliberately.
The existing `Classification` literal already separates "the task is wrong"
(`task_defect`) from "the tooling is wrong" (`harness_defect`), and this
diagnostic is the latter: the task is usually valid Harbor, and it is *this
workbench version* that cannot reason about it. Gating is unaffected —
`Inspection.static_passed` tests `severity == "error"` only and ignores
classification — so the refusal still blocks certification. Reporting it as a
`task_defect` would blame authors for a workbench limitation.

**Complete list of constructs v1 now refuses as unsupported** (everything
outside the allowlist; Harbor 0.21.0 field names):

- top level: `steps`, `multi_step_reward_strategy`, `solution`, `source`
- `[agent]`: `network_mode`, `allowed_hosts`, `user`
- `[verifier]`: `collect`, `env`, `user`
- `[environment]` and `[verifier.environment]`: `allow_internet` (deprecated),
  `allowed_hosts`, `env`, `workdir`, `os`, `gpus`, `gpu_types`, `tpu`,
  `healthcheck`, `mcp_servers`, `skills_dir`
- `[verifier.environment].docker_image` specifically (a prebuilt verifier image
  is never built from `tests/`, so the content scan that is the verifier image's
  only network boundary would never see it; `[environment].docker_image` stays
  allowlisted because it has its own `prebuilt_image_unsupported` refusal)
- any unknown key anywhere, at its exact dotted path

Accepted surface: `schema_version`, `artifacts`, `[task]`
(`name`/`version`/`description`/`keywords`/`[[task.authors]]` with
`name`/`email`), free-form `[metadata]`, `[agent].timeout_sec`, `[verifier]`
(`timeout_sec`/`environment_mode`/`network_mode`/`environment`), and
`[environment]`/`[verifier.environment]` limited to `network_mode`,
`build_timeout_sec`, `cpus`, `memory_mb`, `storage_mb` (+ `docker_image` on
`[environment]`).

### Harbor: mirrored, not called — and now pinned

**Route taken: mirror, because calling is not available.** Harbor 0.21.0 is
installed as a standalone `uv` tool, not as a dependency of this package —
`src/evallab/runner.py` reaches it through `shutil.which("harbor")` and a
subprocess, and `import harbor` fails inside the project venv. Adding Harbor as
a library dependency would mean editing `pyproject.toml`, outside this lease.
The resolvers themselves *are* public (`resolve_effective_verifier_env_config`,
`resolve_verifier_phase_policy`), so this is an availability limit, not an
encapsulation one.

Drift is therefore detected behaviourally.
`test_verifier_network_resolution_matches_harbor` runs a probe *inside Harbor's
own interpreter* (`Path(shutil.which("harbor")).resolve().parent / "python"`)
and compares real Harbor output against `_effective_verifier_network` over four
documents: the compliant fixture, a `[verifier.environment]` table that omits
`network_mode`, a `no-network` verifier table over a `public` environment, and a
`[verifier].network_mode` phase override. Harbor 0.21.0 returns
`no-network/no-network`, `public/public`, `no-network/no-network`,
`no-network/public`; the mirror agrees on all four.

Harbor symbols depended on (all Harbor **0.21.0**):

- `harbor.models.task.verifier_mode.resolve_effective_verifier_env_config`
- `harbor.models.task.verifier_mode.resolve_step_verifier_mode` /
  `resolve_task_verifier_mode` / `_resolve_mode` (precedence, via the above)
- `harbor.models.task.config.BaselineNetworkPolicyConfig.resolve_baseline`
  (defaults `network_mode` to `public`)
- `harbor.models.task.config.PhaseNetworkPolicyConfig.explicit_phase_policy`
- `harbor.trial.network_policy.resolve_verifier_phase_policy`
- `harbor.models.task.config.TaskConfig` (schema; its field sets were
  introspected to build the allowlist)
- observed but not mirrored: `harbor.trial.trial.Trial._separate_verifier_env`
  (`extra_docker_compose: []`) and `_verifier_env_build_context` (`tests/`)

**Caveat, stated plainly:** the drift test `pytest.skip`s when no `harbor`
binary is on PATH. In an environment without Harbor installed it does not run,
and drift there would go unnoticed until Harbor is present again.

### Instance 1 — step-level verifier tables: REFUSED, not resolved

**Decision: REFUSE `[[steps]]`.** Rationale: resolving step-first precedence
correctly would require mirroring per-step tests directories
(`_verifier_env_build_context` prefers `tests/<step>/`), per-step verifier
images, per-step artifacts and healthchecks, and `multi_step_reward_strategy` —
while the workbench's control plan, oracle repetition model, and digest scheme
are all single-verify-pass. Refusing is honest about what v1 models; resolving
would create four more mirrors to drift. The diagnostic names `steps` explicitly
and its message states the step-first precedence so the author knows why.

The danger is empirically confirmed, not assumed. Harbor's own resolver, run on
`tests/fixtures/task_workbench/cases/step-verifier-network/task.toml`, returns
`["public", "public"]` for that task's single step while every task-level table
declares `no-network` — and the pre-existing mirror still reports
`verifier_effective_baseline: "no-network"` with no diagnostic. The refusal is
the only thing between that task and a packet;
`test_step_level_verifier_network_is_refused_as_unsupported` asserts both halves
(`_codes(inspection) == {"unsupported_task_configuration"}` *and* the mirror's
blind `no-network` baseline).

### Instance 2 — verifier build context scanned with the wrong pattern

`_validate_build_context_contents` now walks both build contexts via
`_BUILD_CONTEXTS`: `environment/` (agent image) and `tests/` (separate verifier
image), each skipping its own already-scanned `Dockerfile`, each applying
`BUILD_NETWORK_PATTERN`, each fail-closed on non-UTF-8 via
`build_context_unreadable`. Previously only `tests/Dockerfile` got the build
pattern; every other file under `tests/` saw only `NETWORK_SCRIPT_PATTERN`.

**`BUILD_NETWORK_PATTERN` was not widened, because it already covers the listed
package managers** — `apk|dnf|yum|microdnf|zypper (add|install|update|upgrade)`,
`(gem|cargo) install`, `go (get|install)`, `(git|hg|svn) (clone|fetch|pull|
checkout)`, `ftp://`, `git://`, `Invoke-(WebRequest|RestMethod)`, and
`(npm|pnpm|yarn) (install|add|ci|dlx)` are all present. The round-2 finding was
about which *files* got that pattern, not about the pattern's contents. The
narrower `NETWORK_SCRIPT_PATTERN` (runtime use) was deliberately left alone: it
is a different question, and `tests/` files are still scanned by both.

The fixture pins that distinction. `cases/verifier-build-script/` has a
`tests/Dockerfile` whose lines name no network (`COPY . /tests`,
`RUN sh /tests/bootstrap.sh`) plus `tests/bootstrap.sh` containing
`apk add --no-cache jq`. The test asserts
`not NETWORK_SCRIPT_PATTERN.search(bootstrap)` — so a pass can only come from
the newly applied build pattern — and that
`_codes(inspection) == {"build_network_use"}`.

Consequence worth knowing: any file under `tests/` matching the build pattern is
now an error, including golden fixtures whose *content* happens to contain a URL
or a package-manager string. That is the fail-closed trade, and it is documented.

### Verification

```text
uv run pytest tests/test_task_workbench.py
  before this round (ea73cdd): 42 collected, 42 passed
  after  this round:           54 collected, 54 passed

negative control: new tests vs src/evallab/task_workbench.py reverted to ea73cdd
  10 of the 12 new tests FAIL, including all four adversarial/refusal cases:
    test_step_level_verifier_network_is_refused_as_unsupported
    test_verifier_build_context_script_fetch_is_rejected
    test_unscannable_verifier_build_context_file_fails_closed
    test_unsupported_task_configuration_names_the_exact_offending_path (7 params)
  the 2 that pass under ea73cdd are honest non-detectors, not weak tests:
    test_reference_fixture_stays_inside_the_supported_configuration_surface
      (a no-regression guard: the valid fixture must keep passing, and did)
    test_verifier_network_resolution_matches_harbor
      (a Harbor-drift pin; the old mirror also agreed with Harbor on the four
       step-less cases, which is correct — it was blind to steps, not wrong here)
  fixed file restored immediately; full suite re-run green afterwards

uv run ruff check src/evallab/task_workbench.py tests/test_task_workbench.py
  All checks passed!
```

`research/registration/candidates/` was **not** touched. Verified rather than
assumed: `inspect_candidate` was run over both records' frozen `task_path`s
under the ea73cdd inspector and under the repaired one, comparing
`static_passed`, the full candidate dict, and every diagnostic — the two outputs
are byte-identical. `cases/unpinned-dependency` has no `task.toml` and no
`tests/`, so neither new check can fire; `valid/` stays inside the allowlist and
its `tests/` files match no build pattern. No descriptive field changed, so no
regeneration was warranted and no `certified: true` claim was restored. The
`network_policy.verifier_build_network` string
(`"static scan of tests/ only; overlay not applied"`) was left as-is because it
remains exactly true — the change was which pattern that scan applies, not
whether a scan is the boundary.

`WORKBENCH_VERSION` stayed at `m007-v1.1`. It feeds the frozen identity digest,
so bumping it would change every `candidate_id` and orphan both committed record
directories — outside this lease. Flagging it for the Integrator as a judgement
call rather than deciding it silently.

### Does any reachable path still certify a networked verifier or build?

Honest answer: **not exhaustively ruled out, and I am not claiming closure of
the residual risk — only of the class of blind spot.** What is now true:

- Every `task.toml` construct outside the allowlist is refused, so a *new*
  Harbor table or key cannot silently change the effective policy; the failure
  mode of an unmodelled construct is refusal, not certification. This is what
  closes the class.
- Both build contexts are scanned with the build pattern and fail closed on
  undecodable files.
- The verifier runtime policy is a mirror pinned behaviourally against Harbor.

Residual exposure I can name, all of it *within* modelled constructs:

1. The verifier build and runtime are protected by **static text scanning and a
   declaration check, not container-level enforcement**. An obfuscated fetch
   (base64, string concatenation, a compiled binary that is valid UTF-8, a
   vendored client invoked by a name no pattern matches) defeats
   `BUILD_NETWORK_PATTERN`. This is inherent to the approach, was already
   documented, and is unchanged by this round.
2. The Harbor drift pin is skipped when Harbor is not installed.
3. The mirror is validated against Harbor **0.21.0** only. A future Harbor that
   changes resolution for an allowlisted construct would be caught by the drift
   test where installed; one that adds a *new* construct is caught by the
   allowlist.
4. `NETWORK_OVERLAY_CONTENT` targets the `main` service; `custom_compose_unsupported`
   refuses task-authored Compose, but the overlay's reach across Harbor's own
   Compose layering was verified by the earlier round, not re-verified here.

No Harbor run, Docker build, container build, model call, cloud sandbox, deploy,
or publication occurred; the Harbor drift probe only imports Harbor's models in
its interpreter and resolves configuration in memory. No API-key environment
variable was introduced or read. Nothing under `policy/` or `library/` was
touched; `library/registry/` still holds zero records and nothing here registers
a task. Full suite, project-wide formatters, and `scripts/premerge.sh` left to
the Integrator. Stopping at `review-wanted`: not merged, not rebased onto
`origin/main`, and no new PR opened — #49 already exists.

## Fourth repair round: the allowlist outside `task.toml`

The third review confirmed the `task.toml` allowlist holds, then showed the
class had only been confined to `task.toml`. Three defects, all fixed here.

### P0 — a task-authored Compose file under `tests/` escaped every check

`_validate_network_and_isolation` refused `environment/docker-compose.yaml` **by
exact path**. Harbor builds and runs the separate verifier environment with
`environment_dir = tests/` (`Trial._verifier_env_build_context`, trial.py:694-702,
passed at trial.py:657), and `DockerEnvironment._environment_docker_compose_path`
is `environment_dir / "docker-compose.yaml"` (docker.py:311-313), layered into
`_docker_compose_paths` (docker.py:363-364) for `docker compose build` and `up`
alike. `_egress_controlled_service_names` (docker.py:381-420) then *excludes* any
service declaring its own `network_mode` or `networks` from the sidecar-namespace
rewrite that implements `no-network` — the docstring at docker.py:346-347 states
that task-authored networking on any service, `main` included, is respected.

So a fully allowlist-compliant `task.toml` plus two lines in
`tests/docker-compose.yaml` ran the verifier with full egress and the workbench
emitted nothing: no unknown `task.toml` key, `_effective_verifier_network`
returning baseline `no-network`, and the YAML read as text matching neither
content pattern.

**Every filename Harbor consumes from an environment directory** (read from
Harbor 0.21.0 source, not inferred):

| Filename | Consumed by | Now |
| --- | --- | --- |
| `Dockerfile` | `environments/definition.py:7` `DOCKERFILE_NAME`; `has_agent_environment_definition`, `should_use_prebuilt_docker_image`, `should_upload_environment_dir`, `DockerEnvironment._dockerfile_path` (docker.py:309), and the same property on `skypilot`/`novita`/`langsmith`/`apple_container` | **modelled** (allowlisted) |
| `docker-compose.yaml` | `environments/definition.py:8` `COMPOSE_FILE_NAME`; `_environment_docker_compose_path` (docker.py:311-313) → `_docker_compose_paths` (docker.py:363-364) → `build` (docker.py:891 via 623) and `up`; reparsed by `_egress_controlled_service_names` (docker.py:392); also `novita.py:721,949`, `langsmith.py:160,261,782`, `trial.py:956` | **refused** |
| `.env` | the Compose CLI, because Harbor passes `--project-directory <environment dir>` (docker.py:620-621) and never `--env-file` (grepped: no `env-file` anywhere in Harbor), so Compose reads the project directory's `.env` and interpolates it into every `-f` document including Harbor's own `docker-compose-build.yaml` (`${CONTEXT_DIR}`) and `docker-compose-egress-control.yaml` (`${EGRESS_CONTROL_INITIAL_NETWORK_MODE}`) | **refused** |
| `.dockerignore` | Docker's builder, for the context Harbor passes as `context_dir` (docker.py:243) | **refused** |

That is the complete set. `docker-compose.override.yaml`, `compose.yaml`, and
`compose.yml` are **not** consumed by Harbor 0.21.0 — Compose's default file
discovery and override auto-loading are both bypassed by the explicit `-f` list —
and are refused anyway, on allowlist grounds.

**What is now refused**, by `_unmodelled_build_config` in
`_validate_build_context_contents`, in `environment/` *and* `tests/`, at every
depth:

- `_COMPOSE_CONFIG_NAME` = `^(?:docker-)?compose\b.*\.(?:ya?ml|json)$` →
  `custom_compose_unsupported` (reused). Covers `docker-compose.yaml|yml`,
  `compose.yaml|yml`, every `.override.` variant, and `.json` compose documents
  (Harbor writes its own overrides as `docker-compose-*.json`, so Compose accepts
  the extension). `\b` makes `composer.json` **not** match — tested.
- `_COMPOSE_ENV_CONFIG_NAME` = `^\.env(?:\..+)?$` →
  `compose_env_file_unsupported`. `.envrc` does not match — tested.
- `.dockerignore` → `build_context_ignore_unsupported`. Refused not because it
  can add egress (it cannot) but because it makes the files the workbench
  scanned differ from the files in the image, which is a claim the packet makes.

Only a context *root* is consumed by Harbor 0.21.0, so refusing at every depth is
deliberately wider than the exploit: which directory becomes an environment
directory is Harbor's choice (`_verifier_env_build_context` already picks between
`tests/` and `steps/<n>/tests`), and the cost of a false refusal is renaming a
fixture while the cost of a miss is a false green.

The old exact-path check is **gone**, not left beside the new one — a comment at
its former site points to the general check. `environment/docker-compose.yaml` is
the one case that already failed at `1713830`, which is why it is the one
parametrised case that does not appear in the negative control below.

### P1 — `BUILD_NETWORK_PATTERN` missed plain package installs

This pattern plus the filename allowlist are the *entire* boundary on the
separate verifier image: `extra_docker_compose` is forced to `[]` for the verifier
(trial.py:648-650), so no `build.network=none` overlay reaches that build.

**Added** (each proven through a real `inspect_candidate`, not a regex probe):
`uv sync`, `uv add`, `uv lock`; `pip download`, `pip wheel`; `npm i`, `pnpm i`,
`npm/pnpm update|up|add`; `npx`; `poetry install|add|update|lock|sync`; `pipenv`;
`bundle`; `composer`; `conda`/`mamba`/`micromamba install|create|add|update|env`;
`brew install|tap|update`; `pacman -S*`/`--sync`; `dotnet
restore|add|tool|build|publish|test`; `mvn`/`mvnw`/`gradle`/`gradlew` on the tool
name alone; `cargo add|fetch|update`; `go mod download|tidy`; `gem update`;
`yarn up|upgrade`. `RUN uv sync` leads because it is this repository's own idiom.

**Deliberately left out**, with reasons:

- `apt-get download`, `apk fetch` and similar: already matched by the existing
  `apt`/`apk` alternations on their common verbs; adding rarer verbs buys little.
- `nix-env`, `guix`, `opam`, `stack`, `cabal`, `sbt`, `leiningen`, `swift package`,
  `pub get`, `hex`/`mix deps.get`, `vcpkg`, `conan`: real package managers, but
  each addition is another denylist entry, and the recommendation below is that
  enumerating them is the losing move. Named here so the gap is on the record.
- Bare `yarn` and bare `cargo`/`go`/`pip`: rejected as too false-positive-prone
  for a word-boundary text scan over arbitrary context files.
- `curl`-equivalents in other languages (`Net::HTTP`, `http.Get`, `fetch(`):
  runtime constructs; the build scan is about build commands, and obfuscation
  defeats them anyway.
- `mvn`/`gradle` are the one place I matched on the *tool* rather than a verb,
  because every default Maven/Gradle goal resolves from a remote repository
  unless `-o`/`--offline` is passed. A genuinely offline Gradle build is refused;
  that is the intended direction of error.

False-refusal edge is tested explicitly: `npm init -y`, `cargo build --offline`,
`python -m compileall`, `chmod`, and `mkdir` all still pass.

### P1 — the Harbor drift pin never executed in CI

`test_verifier_network_resolution_matches_harbor` ran a probe inside Harbor's own
interpreter and `pytest.skip`ped without a `harbor` binary. Harbor is not a
dependency of this package, so in CI it skipped unconditionally.

Split in two, sharing `_verifier_network_documents()`:

- `test_verifier_network_resolution_is_pinned_statically` — **runs everywhere, no
  Harbor, no subprocess.** Asserts the four `(baseline, phase)` pairs as
  literals: `("no-network","no-network")` for the compliant fixture,
  `("public","public")` for a `[verifier.environment]` that omits `network_mode`
  (no inheritance), `("no-network","no-network")` for a `no-network` verifier
  table over a `public` environment, `("no-network","public")` for a
  `[verifier].network_mode` phase override; plus the step fixture resolving to
  `("no-network","no-network")` in the mirror, which is exactly why `[[steps]]` is
  refused rather than mirrored.
- `test_verifier_network_resolution_matches_harbor` — unchanged in substance, now
  an *additional* check that skips without the binary and asserts Harbor returns
  `[["public","public"]]` for the step fixture.

Verified by running the file with a PATH containing no `harbor`:
`1 passed, 1 skipped` for `-k verifier_network_resolution`, and `98 passed,
1 skipped` for the whole file. No environment variable was invented: `.github/`
holds only `EVAL_LAB_PROFILE_DATABASE_URL` and `TY_BASELINE`, neither of which
means "CI". **Recommendation for the Integrator** (`.github/` is outside this
lease): if a hard failure on missing Harbor is wanted, gate it on GitHub's own
`CI` variable, or install Harbor as a `uv tool` step in `ci.yml` so the live
comparison runs there too. Not doing either leaves the static pin as the CI
guard, which is the defect this round was asked to fix.

### Recommendation: should build-content checking become a `RUN` allowlist?

**Recommend it as a follow-up mission; reject it for v1.** Not implemented this
round, as instructed.

The lesson of this PR does apply — the content pattern is the last denylist left
in the isolation story, and this round added roughly twenty entries to it, which
is the same treadmill the `task.toml` surface was on. So the direction is right.

Why it is not a v1 change:

1. **The refused set is closed; the permitted set is not.** `task.toml` is a
   finite schema, so enumerating what v1 models was tractable and *complete*. A
   `RUN` line is an arbitrary shell command: an allowlist would have to model
   shell grammar (pipes, `&&`, `$(...)`, `sh -c`, heredocs, `xargs`) before it
   could decide anything. `_docker_copy_sources` already fails closed on
   unresolvable `COPY` syntax; the equivalent for `RUN` fails closed on almost
   every real Dockerfile.
2. **The false-refusal rate would be near total.** A plausible v1 allowlist
   (`mkdir`, `chmod`, `cp`, `mv`, `ln`, `echo`, `printf`, `tar`, `sed`, `python -m
   compileall`, `useradd`, `test`) refuses the first legitimate task that runs a
   project script, a compiler, or `sh -c`. The current fixture would pass; almost
   nothing else would. "Refusal is the preferred v1 outcome" is about tasks the
   workbench cannot *reason* about, not about refusing everything.
3. **The high-value version is different work.** The honest fix for the verifier
   image is not a better text scan at all — it is *observing* the build with the
   network denied, the way the agent build already is. Harbor forces
   `extra_docker_compose: []` for the verifier, so that needs either a Harbor
   change or a workbench-side pre-build of `tests/` under `--network=none`. That
   is a container-level boundary replacing a text scan, which is worth a mission;
   a `RUN` allowlist is a worse version of the same goal.

Concrete follow-up shape, in preference order: (a) build the verifier image under
`docker build --network=none` inside the control run and bind the resulting image
digest into the packet — turns "text scan only" into a real boundary; (b) failing
that, an allowlist of *executables* (not command forms) invoked at the head of
each `RUN` word, with unresolvable shell refused — closed, but still needs shell
parsing; (c) status quo plus a documented statement that the verifier build is
not a sandbox. This PR ships (c) and says so in `docs/task-workbench.md`.

### Does any reachable path still certify a networked verifier or build?

**No, I cannot rule it out exhaustively, and I am not claiming closure.** The
class of *unexamined configuration surface* is now closed in both places it
existed — `task.toml` (third round) and build-context filenames (this round) —
but two residual paths are real and neither is a denylist gap I can fix by adding
entries:

1. **A digest-pinned base image can carry the fetch.** `tests/Dockerfile` must
   pin `FROM` by `@sha256`, and that is all the workbench knows about it. A base
   image containing `/usr/local/bin/setup` that curls, invoked as
   `RUN /usr/local/bin/setup`, matches no pattern and is refused by nothing. The
   image's *contents* are never inspected. This is the strongest remaining path
   and it defeats any text scan by construction, not by obfuscation.
2. **Obfuscation inside the context.** base64, string concatenation, a vendored
   HTTP client invoked under an innocuous name, a UTF-8-decodable script that
   assembles a URL at build time. Documented before, unchanged.

What *is* now true and verified from Harbor's source: with no task-authored
Compose file in `tests/` (refused) and a `no-network` declaration (checked
against a statically pinned mirror), the verifier **container** does get
container-level enforcement — `_egress_controlled_service_names` returns
`[main]` when there are no task compose paths (docker.py:387-388), so `main`
joins the egress sidecar namespace, and if egress control cannot be enabled at
all, `capabilities.disable_internet` is `False` and Harbor's
`validate_network_policy_support` raises rather than running with egress. The
verifier **build** has no such backstop. That asymmetry is now stated in
`docs/task-workbench.md` rather than flattened into one "text scan only" claim.

`research/registration/candidates/` was **not** touched, and this was checked
rather than assumed: neither record's frozen `task_path`
(`tests/fixtures/task_workbench/valid`, `cases/unpinned-dependency`) contains any
file matching the three new filename patterns, and neither contains any of the
added install idioms, so the repaired inspector computes exactly what is already
recorded. `WORKBENCH_VERSION` stayed at `m007-v1.1` for the same identity-digest
reason as the previous round.

### Verification

```text
uv run pytest tests/test_task_workbench.py
  before this round: 54 passed
  after  this round: 102 passed   (48 new)
  with no `harbor` on PATH:       98 passed, 1 skipped
    the 1 skip is the live Harbor comparison; the static pin runs

negative control: new tests vs src/evallab/task_workbench.py reverted to 1713830
  37 failed, 62 passed  (run at 99 collected tests; the 3
  test_lookalike_filenames_are_not_refused params were added after it and are
  false-refusal guards that pass in both directions, so they carry no signal here)
  every failure is a new test:
    test_task_authored_compose_under_tests_escapes_nothing
    test_compose_filenames_are_refused_in_every_build_context (8 of 9 params)
    test_other_interpreted_build_context_files_are_refused (5 params)
    test_verifier_build_uv_sync_is_rejected
    test_plain_install_idioms_are_rejected_in_the_verifier_build (22 of 22 params)
  the params that pass under 1713830 are honest non-detectors, not weak tests:
    ...[environment/docker-compose.yaml] — already refused by the old exact-path
      check; it is the one case that was never broken
    test_offline_build_commands_are_not_refused,
      test_reference_fixture_has_no_interpreted_build_context_configuration
      (false-refusal guards: they must pass both before and after)
    test_verifier_network_resolution_is_pinned_statically — pins the mirror,
      which this round did not change; its defect was that it never RAN in CI,
      and the proof of that fix is the PATH-cleared run above, not a revert
  fixed file restored immediately; full suite re-run green afterwards

uv run ruff check src/evallab/task_workbench.py tests/test_task_workbench.py
  All checks passed!

smoke test (real CLI, not a test file):
  python -m evallab.task_workbench check over a copy of the valid fixture plus
  tests/docker-compose.yaml → disposition "needs_changes", one diagnostic,
  code custom_compose_unsupported, path tests/docker-compose.yaml, message
  naming "the separate verifier image"
  same fixture with that file removed → disposition "controls_pending",
  diagnostics []
```

New fixtures: `cases/verifier-compose-escape/tests/docker-compose.yaml` (the
two-line adversarial escape) and `cases/verifier-uv-sync/tests/Dockerfile`
(`RUN uv sync --frozen`).

No Harbor run, Docker build, model call, cloud sandbox, deploy, or publication
occurred. No API-key environment variable was introduced or read. Nothing under
`policy/`, `library/`, or `.github/` was touched. Full suite, project-wide
formatters, and `scripts/premerge.sh` left to the Integrator. Stopping at
`review-wanted`: not merged, not rebased onto `origin/main`, no new PR.