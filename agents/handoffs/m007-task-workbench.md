Status: ready for review
Last: deterministic workbench, retained evidence, real free controls, full suite, and premerge all passed
Next: complete independent Tasks review, push the review-only PR, and wait for exact-head GitHub CI
Blockers: repository contains zero human-registered tasks; exercise will use the documented event-summary registration candidate and state that limitation explicitly

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
  images and dependencies, explicit network policy and runtime network use,
  deterministic verifier constructs, reward output, hidden/golden leakage,
  adversarial coverage, source/license provenance, and forged registration
  claims.
- Static failure makes zero control calls. Admitted controls are fixed to three
  Oracle jobs, one Nop job, and every declared invalid solution; the latter run
  as Oracle only in isolated staging copies. The command fixes Docker, one
  attempt, and concurrency one. No model/cloud/paid execution is reachable.
- Complete bundles resume idempotently. Incomplete, missing, or digest-invalid
  evidence fails closed. Outcomes are explicitly classified as task defect,
  harness defect, agent failure, or expected.
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
candidate: candidate-c4ec5c27d830ea208cf30382
result: needs_changes (21 retained task-defect diagnostics; no controls)
candidate sha256: 3c5b69fde775df39f249675dd7f89c973fa7d0e836a3b43d5f3fa008a549a9ab
certification sha256: ed96412937151f9b55a0d64b92ac765120f44fcee25f5a941c251ca0e74d4512
second packet build: identical hashes and expected exit 1
```

Real free-control evidence:

```text
candidate: candidate-8f6a76d350ece0574a069910
source: local/m007-uppercase-fixture@1.0.0 (MIT, synthetic)
bundle: sha256:4e7c3aef2ebd374e780150a35d6fca9ddf939f3488daf4e21acafa5c0a54bd62
oracle-1, oracle-2, oracle-3: completed, reward 1.0
nop-1: completed, reward 0.0
adversarial-empty-output: completed, reward 0.0
adversarial-extra-artifact: completed, reward 0.0
adversarial-wrong-value: completed, reward 0.0
verifier output: identical across all three Oracle runs
models/cloud/paid calls: 0
concurrency: 1
status: certified_for_review (not admitted)
candidate sha256: b1b80219ee147c44a245876d9a88ff94f5cc6c061ee0db199763a9389c5207f7
certification sha256: 2f7ded1b93d663da2f47bde25230c5ccb776e8a0527befa49510d268821535ee
second packet build: identical hashes
```

Three earlier local control cycles were also retained under the ignored
`runs/task-workbench/` evidence root. The first exposed a Harbor/macOS Docker
backend limitation for `network_mode = "no-network"`; the second exposed that
the Oracle runtime requires the task image's shell contract (the initial Alpine
fixture lacked it and produced `RewardFileNotFoundError`). These were classified
as incomplete evidence rather than agent failures. A later run then correctly
caught an invalid probe the verifier could not observe: Harbor transfers only
declared artifacts, so an undeclared extra file alone was accepted. The probe
was changed to add unexpected content to the declared result as well; the final
pinned Ubuntu fixture then produced the successful bundle above.

Initial setup evidence:

```text
origin/main: 00f36ab INTEGRATION: release M006 and M007 (#46)
uv sync --locked: installed successfully with CPython 3.12.11
open PRs at dispatch: none
```

Local verification evidence:

```text
uv run pytest tests/test_task_workbench.py -q
....................... [100%]
24 passed

uv run ruff check .
All checks passed!

uv run pytest -q
395 passed

scripts/premerge.sh
Resolved 43 packages; audited 41 packages
All checks passed!
395 passed in 17.50s
doctor/smoke: PASS (both stores agree)
ty: 28 diagnostics; premerge green because ratchet is 28 <= 28
premerge green: Python 3.12

git fetch origin; git rebase origin/main
origin/main: 00f36ab
Current branch role/m007-task-workbench is up to date.
```

No M007 file adds an API-key variable, model selector, queue import, registry
write, publication path, or absolute home path. Packet admission remains false.
The final self-audit also made unobserved control claims fail closed: a
`controls_pending`, interrupted, or static-failure packet cannot report Oracle,
Nop, adversarial, or verifier-determinism checks as true.
