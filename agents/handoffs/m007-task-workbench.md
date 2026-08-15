Status: building
Last: integrator repair implemented and focused tests passed (35/35)
Next: run full checks, rebase origin/main, push the repaired PR #49 head, and stop at review
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
