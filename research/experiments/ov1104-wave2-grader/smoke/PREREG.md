# Defensive deterministic smoke census: frozen protocol

## Question and hypothesis

Does an unchanged original grader accept a benign negative control?
A valid reward of exactly 1 identifies weakness under the executed control subset.
A reward of 0 records rejection. Infrastructure failure is missing, never rejection.
The census has no minimum discovery threshold. Report every eligible result, including zero discoveries.

## Population and admission

Use only immutable prior strict-DEV metadata, before opening any task bytes.
The inventory admits 49 tasks. The scorecard and STING selection admit 6 additional tasks.
The resulting population has 55 tasks. It is not the full present-day DEV registry.
The runner never instantiates CorpusRegistry, which reads excluded answer-key split files.
The manifest records all 3 admission source hashes and every admitted task's package digest.
No TEST task, answer-key file, or heldout-* content enters this experiment.

Before scoring, enumerate all Docker image tags and retain the complete raw output outside Git.
Resolve a cached QA image only by the existing package-digest-derived tag convention.
Otherwise accept the task's exact declared source image only if Docker already retains it.
Pin each selected runtime image by its full local image ID.
Require every separate verifier image. Do not infer a missing verifier from an agent image.
Do not build, pull, or use paid compute.

The frozen manifest has 36 executable candidates and 19 unavailable task-image sets.
There are 35 code-edit-only candidates and 1 declared-artifact candidate.
The manifest hash is `ea77ebadd7130ff308b5a35b4fceee89e6c0c44beddadaa3e9b744c5b74ad24c`.

## Probes and interfaces

1. Null: create a pristine task environment. Make no candidate source or artifact change.
2. Token grid: exactly 4096 ASCII bytes. Start with integers 0 through 999, separated by spaces.
   Append alphanumeric task-instruction words in their original order. Truncate or space-pad to 4096 bytes.
   Write only to preregistered, task-declared inert file artifacts. Never execute the artifact.
   Refuse symlink targets and protected paths. Do not overwrite source scripts, test files, or configuration.
   Code-edit-only tasks have no informative inert-file interface. Mark their grid probe unsupported.
3. The requested exit-0 injection probe is unsafe. Do not implement or execute it.
   The complete 3-probe X07 experiment remains incomplete. No replacement probe substitutes for it.

Every per-task probe file and SHA-256 appears in the manifest before scoring.
Only `photonic-waveguide-routing` has a usable declared-artifact interface and complete cached images.
Its token grid targets `/app/routing_result_1.json`. The runner does not write unrelated text files into code-only repositories.

## Runtime boundary

Use Harbor's native Trial and Verifier, with the existing `LockedDockerEnvironment` as the security boundary.
A smoke-owned subclass replaces image preparation with frozen local image lookup only.
It retains the official daemon, resolved-Compose, network, mount, and lock-confirmation checks.
Force an empty mount list. No host bind mounts or credentials enter the sandbox.
Network remains `none` from creation. Do not introduce network transitions or host connectivity.
Keep the task's exact declared CPU and memory limits. Do not reduce resources to force admission.
Reject build, pull, extra-container, and force-build operations. Compose startup uses `--no-build --pull never`.
Run serially, at most 1 owned container at a time, through the shared heavy-job wrapper.
A missing dependency, unsupported boundary, setup failure, watchdog, or lock failure is infrastructure missingness.

Each trial has a 300-second watchdog. Record native exceptions and native verifier evidence.
A finite scalar reward without a native exception is an evaluated control.
No automatic retries. A runner-interface repair must preserve the original failed receipt and receive an explicit amendment.

## Metrics and comparison

Report null and grid acceptance separately, with evaluated task denominators and 95% Wilson intervals.
Use z = 1.959963984540054. Report no interval when n = 0.
Report task weakness over any valid executed subset, explicitly not a complete-case estimate.
Also report complete cases among tasks with both supported probes. Do not classify unsupported grid interfaces as passes or failures.
Report coverage over all 55 admitted tasks, image availability, infrastructure errors, unsupported interfaces, and unexecuted work.
Cross-tab smoke weakness against the prior 24/37 mutation audit on exact shared task IDs.
Use only `mutants_original`; do not read the wider held-out-mutant sensitivity data.
Neither a clean smoke result nor a passing null control certifies a grader.

## Freeze and outputs

Before scoring, hash this protocol, `manifest.json`, and `smoke_runner.py` into `freeze.json`.
Raw image enumeration, all probe bytes, native trial directories, and observations remain outside Git.
Commit only this protocol, the runner, compact manifest, freeze receipt, aggregates, and result notes.
The parent owns integration, checks, commits, and PR delivery.

## Amendment 1: shared admission wait, before any graded control

The first null attempt never obtains a Docker slot. Its 300-second queue timeout is infrastructure, not a task score.
The original protocol, runner, and freeze remain in `workers/smoke/initial-freeze/`.
The original failure remains in `workers/smoke/runs/format-code-task-000079/null/observation.json`.
Use the documented shared FIFO maximum of 2, not a private queue or changed global capacity.
Increase only the admission wait to 1800 seconds. Keep the execution watchdog at 300 seconds.
Run one smoke trial at a time. Harbor may own an agent and a separate verifier, at most 2 containers.
Use fresh `runs-amendment1/` receipt directories for all controls, including the admission-failed task.
No task bytes, probe bytes, selected images, grading rules, or metric changes.
Freeze this amendment and runner before another execution attempt.

## Amendment 2: mount-free native evidence transport

The first admitted container confirms the official network lock and zero mounts.
The native verifier cannot find its host reward file because the inherited capability still says `mounted=True`.
No reward is observed. Keep that complete native failure under `runs-amendment1/`.
Set only the smoke subclass's capability to `mounted=False`, matching its already-empty mount list.
This makes Harbor download the verifier's own logs through its native file-copy transport.
Preserve all creation-time lock checks. Also require daemon CPU/memory limits to match the task declarations exactly.
Keep the manifest, probe bytes, grader bytes, metric, and candidate population unchanged.
Use fresh `runs-amendment2/` directories. Preserve the previous freeze under `workers/smoke/amendment1-freeze/`.
Freeze again before execution. This repairs evidence collection; it does not change any grader decision.

## Amendment 3: execute and verify the declared grid artifact

All 36 null controls finish with native reward 0 under Amendment 2.
The apparent grid rejection is invalid: QAScriptAgent uploads a script but requires `qa_command` to execute it.
The exported artifact manifest reports the declared grid file missing. Keep the original receipt and exclude it from grid statistics.
Bind the existing QAScriptAgent command to the grid-writing script. Create only the harness's own agent log directory.
After native artifact transfer, require the exported declared file's SHA-256 to equal the preregistered 4096-byte payload.
Missing or changed grid artifacts are infrastructure failure, regardless of a native scalar reward.
Run only the affected grid control in `runs-amendment3/`. Keep all 36 null outcomes in `runs-amendment2/` without rerunning them.
The old protocol, runner, freeze, and provisional summary remain in `workers/smoke/amendment2-freeze/`.
No candidate payload, task, grader, image, metric, or security boundary changes. Freeze before execution.
