---
status: living
audience:
  - analyst
  - operator
---

# QA Agent - Task: executed task/grader auditor

`evallab qa task` (`src/evallab/qa/task.py`, module `evallab.qa.task`)
implements the executed QA Task agent registered beside the shared
`evallab qa` package (`src/evallab/qa/`: `models`, `score`, `answer_key`,
`cli`). It runs:

- `S0`: neutral oracle/nop controls (×3 each), static scan, and leak inspection;
- `S1`: k=3 text-only rubric review through the subscription Codex lane
  (advisory: it scored 0.33 recall / 0.59 false alarms held-out on EnvCheck);
- `S2`: executed probes that look for weak tests —
  (a) the verifier-aware shortcut agent (PR #899: read-only `tests/` view, up
  to 3 attempts with the grader's output visible between attempts),
  (b) ref-subset (PR #892/#903: the smallest part of the reference fix that
  still passes), and (c) the verifier mutation hunt (PR #867: edit the
  reference output, grade every mutant with the task's own verifier; a
  surviving mutant becomes a candidate only if a model adjudicator reading
  `instruction.md` says the mutant is wrong);
- `S3`: fresh-container 3-control confirmation of every S2/imported candidate:
  the wrong/shortcut submission scores 1, the correct fix (oracle) scores 1,
  do-nothing (nop) scores 0.

Only S3-confirmed candidates become verified findings. MiMo semantic proposals
add a separate requirement: independent evidence that the submitted program is
actually incorrect. A self-labeled counterexample and reward 1 do not establish
that fact; their S3 replay stays `unverified` pending independent review.
Unconfirmed/refuted candidates never count as verified defects, and unmeasured
checks never count as passes.

## Stages

| Stage | What runs | Output rule |
|---|---|---|
| `S0` | Unscored static hack scan (`backend.static`); scored `oracle` ×3 and `nop` ×3 fresh-container controls (`oracle_floor=0.9`, `nop_ceiling=0.1`); scored `leak` probe executed directly in the environment image (same `LEAK_PROBE_V1` script and parser as the Harbor probe; a missing `git` with no candidate checkout is `no-repository`, not unknown) | Measured oracle `<0.9` emits a verified `broken` finding; measured nop `>0.1` emits a verified `weak` finding. A measured leak with future history appends a `reachable` Signal. GPU or multi-service Compose packages are `not_run` with the blocker reason. |
| `S1` | Complete rubric (`--rubric` or Harbor built-in) over k=3 independent text-only Codex samples; only complete samples vote; majority per `rubric:<name>` criterion | Majority fails on `DEFECT_CRITERIA` append Signals, never Findings. |
| `S2a` shortcut agent (non-MiMo) | `bundle_verifier_aware` gives the model `instruction.md`, `task.toml`, `environment/*` and `tests/*` read-only. Solutions, cheats, README and static golden directories are excluded. The existing generic prompt, submission formats, grader-output feedback and ≤3-attempt stopping rules are unchanged. | Reward 1 creates an **unverified** candidate; the existing scored `shortcut-agent` verdict applies. |
| `S2a` MiMo semantic-only | Packages with `environment/setup/setup.sh` use a separate program-correctness prompt. Only the instruction, source-code sections of `tests/test.patch`, test command, declared workdir and optional pristine base-source context are supplied. No generic reach/tamper mechanisms or runtime test output are fed back. A proposal must be a production-source patch with a quoted requirement and concrete predicted counterexample. | Every attempt retains source/context/prompt/response/candidate hashes and raw artifacts. Non-executed, rejected, failed or quota-stopped proposals have null reward, never an invented zero. Reward 1 creates an `unknown`/`unverified` candidate, not a proven wrong pass. |
| `S2b` ref-subset | `evallab.ref_subset.probe_task`: raw MiMo packages (or `--ref-patch`) use their reference patch; Harbor **separate-verifier** packages derive the reference from their own oracle and grade subsets in fresh verifier containers (#903). Shared-verifier Harbor packages are `not_run` (not applicable), never a pass. `--mutants N` also runs its line-level pass (#918): up to N one-edit mutants of the lines the reference wrote, each graded like a subset. | A measured partial-fix pass keeping ≤50% of the reference code lines (severity high/medium) creates an unverified `weak` candidate whose reproducer is `minimal.patch`. Low severity (>50% kept) is a Signal only. An exhausted grade budget is `unknown`, not a pass. A surviving line-level mutant is adjudicated against `instruction.md` exactly like an S2c survivor (check `ref-mutants`); only `wrong` ones become unverified `weak` candidates (reproducer: that mutant's patch over the base). `ref-subset` and `ref-mutants` are unscored checks. |
| `S2c` mutation hunt | `evallab.verifier_mutation.run_mutation_audit` with `--mutants N` (default 8; `0` disables) and the coverage-guided plan. Trials run through `HarborBackend.matrix_runner` (raw-docker fresh containers, same capture and coverage layout as Harbor trials) instead of Harbor jobs: ~2 s per trial instead of ~100 s. Survivors (reward kept while the file's blank control scores 0) are read by a Codex adjudicator against `instruction.md` and the diff: `wrong` (violates a quoted requirement) / `equivalent` / `unspecified`. | Only `wrong` survivors become unverified `weak` candidates (reproducer: the mutation script). Unadjudicated survivors (no lane, quota stop, more than 6) append a `weak` Signal and make the `mutation-hunt` check `unknown`. `task_broken`/`unscored` hunts are `ran_without_usable_result`. |
| `S3` | Fresh-container replay, with clean S0 controls from the same audit or one fresh oracle/nop pair when S0 did not run. Mutation scripts retain their existing oracle-channel replay. | Existing candidates become `verified`/`fail` only with replay reward 1 and clean controls. MiMo semantic proposals additionally remain `unverified`/`unknown` until independent incorrectness review; clean numerical triplets alone cannot certify them. Reward 0 with clean controls refutes a candidate. Partial, missing or failed rewards are not confirmations. |

Submission kinds from the shortcut agent are converted by
`submission_to_script`: `command` (bash run in `/app` in the agent
container), `file`/`json` (base64-written to an absolute path; JSON is
validated first), and `patch` (unified diff applied with `git apply --binary`,
a Python unified-diff fallback for minimal images without `git`/`patch`, or
`patch`).
Invalid submissions are unusable, never passes.

### Mutant adjudication batches

The mutation hunt and reference-line pass adjudicate the same first six surviving
mutants in one text-only request. The shared instruction appears once; each
independent mutation retains its original ID, metadata and bounded diff. Responses
must contain exactly one `wrong`, `equivalent` or `unspecified` entry per supplied
ID. Missing, duplicated, changed or unknown IDs, malformed entries, duplicate JSON
keys and trailing content invalidate the complete batch. No per-mutant fallback
calls are made: every affected survivor remains unadjudicated, with an `unknown`
check and advisory Signal unless another adjudicated survivor established failure.

Batch model/tokens are recorded once in
`checks[check].facts.adjudication_batch` and report totals, not copied or divided
among individual findings. S3's fresh replay and controls remain mandatory.
The existing `codex/spend.jsonl` ledger also retains the transport's source `usage`
mapping. Missing components are not synthesized; cached input is a subset of
native Codex input, and reasoning must not be added to output again. These are
reported-token diagnostics, not a subscription-quota conversion or invoice.

### Execution backend

The default `--environment docker` uses `HarborBackend.run_container_trial`,
which mirrors Harbor's separate-verifier flow.
`run_task` holds one slot of the shared host-wide Docker FIFO lock
(`evallab.docker_lock.docker_slot`, label `qa-task/<task>`) for all of a
task's container work (S0, S2, S3), so the queue is joined once per task and
not once per trial; S1 runs before it and needs no container. The backend's
per-trial, `leak`, ref-subset and mutation calls re-enter that slot
(`HarborBackend.slot` is reentrant; `docker_slot` itself is not). By default one
QA Task container runs at a time and other tabs interleave between tasks.
`HarborBackend.control` never caches an observation: oracle ×3 and nop ×3
are six independent trials, each with its own job path and raw result. S3 can
still reuse the explicitly recorded S0 controls rather than requesting new ones.

Explicit Docker task concurrency is restricted to 1–2 and retains that same
host-wide admission:

1. build (and cache per package digest) the environment image and, for
   `environment_mode = "separate"`, the `tests/` image (adding
   `--platform linux/amd64` when `Dockerfile` or `tests/wheels/*.whl` requires
   `x86_64` / `amd64`);
2. start one agent container (or, for `environment/docker-compose.yaml`
   packages, bring up the Compose stack under the same FIFO slot); run oracle
   `solution/solve.sh` (optionally replaced by a mutation/capture script), a
   shortcut script, or nothing (nop); run `[[verifier.collect]]` hooks for
   `main`; optionally copy the agent's `/logs/artifacts` out (mutation
   captures);
3. copy `main` `task.toml` artifacts out (directory artifacts keep their tree;
   dict artifacts honour `exclude`); for Compose packages with sidecar hooks or
   sidecar `artifacts = [{source, service}]`, stop `main` first so sidecar
   collection sees a quiescent state, then run sidecar `[[verifier.collect]]`
   hooks and copy sidecar artifacts;
4. remove the agent container (or tear down the Compose stack), start a fresh
   verifier container, upload the artifacts to their declared paths, run
   `tests/test.sh` with a world-writable `/logs/verifier`, read
   `/logs/verifier/reward.txt` (or `reward.json`), and copy `/logs/verifier/`
   out (coverage traces);
5. shared-mode tasks are graded in the agent container instead.
Every trial writes `agent/oracle.txt`, `verifier/test-stdout.txt` and
`result.json` under its trial directory; those are the grader outputs fed back
to the shortcut agent on the next attempt.

#### Modal: native Harbor trials, not local Docker

Real execution needs the pinned lab Harbor and its Modal extra
(`uv sync --extra laminar`); package-only `--plan` needs neither the optional
runtime nor Modal credentials.

`--environment modal` runs S0 controls/leak inspection, S2 shortcut,
reference-subset and mutation trials, and S3 confirmations on native Harbor
Modal environments. QA inherits the logical stages, patch staging and replay
rules; native `Trial.create(...).run()` owns healthchecks, execution, collect
hooks, artifact destinations/exclusions, separate/shared verifier isolation,
reward parsing and cleanup. A script-only `BaseAgent` runs the submitted script
without a model. Reference-subset uses its native Modal backend and the same
run-wide spend gate. This path never joins a Docker slot, builds/pulls/runs
local Docker, or falls back to Docker.

Every agent or verifier allocation is admitted separately, **before** native
image preparation/provider execution. The reservation includes preparation and
both possible native sandbox-creation attempts (three units per environment);
unused attempts retain their estimates. Reference-subset accounts for its own
image preparation, immutable snapshot and bounded grade attempts, included in
the plan. A separate verifier therefore needs its own reservation even after
the agent succeeds. A failed script/setup/verifier, missing/invalid reward,
budget exhaustion or package-digest change is unmeasured, not a pass or a
measured zero. Native raw `result.json` is preserved; `qa-execution.json`
records QA's usability decision and spend receipt. `agent/oracle.txt` and
`verifier/test-stdout.txt` keep the existing reproducer/report interface.

The adapter combines `LockedModalEnvironment`'s actual accepted egress-policy
evidence (`egress-lock.json`) with `RegradeModalEnvironment`'s PATH pin and
at-most-one-hour sandbox lifetime. It does not claim a public policy was
locked. Native trial cleanup runs on success and failure; failed starts also
attempt bounded cleanup. Longer requested trial timeouts do not extend the
one-hour sandbox lifetime. Script overrides and synthesized patch oracles are
staged outside the package; reports, findings and native trial config retain
the original package path/digest.

Compose packages are explicitly unsupported on this backend (no implicit
Docker-in-Docker). GPU/TPU tasks are refused before allocation because the
CPU reservation estimate is **not** qualified accelerator pricing. This is
not cloud GPU qualification. Windows and declared multi-step packages are
also unsupported by this single-step shell-control backend. Unsupported tasks
remain unmeasured with their reason.


### Workdir-bound packages (MiMo code tasks)

A package whose `task.toml` declares `[environment].workdir` and either has no
`solution/solve.sh` (raw MiMo code tasks: the oracle is a reference patch) or
declares a setup healthcheck (v6 separate-verifier MiMo tasks: own `solve.sh`, image
with no `WORKDIR`) is run through the same flow with the additions below
(`HarborBackend.patch_workdir` names them). They change nothing for any other
package, and no TB4 package declares a `workdir`.

- **Healthcheck first.** `[environment.healthcheck]` runs in the started container
  before the oracle, a shortcut, the verifier or the leak probe (`retries + 1`
  attempts, `timeout_sec` each). For MiMo it is the one-time setup that writes
  `/var/lib/mimo/base`, which `tests/test.sh` needs; without it no reward exists.
  A separate verifier completes its clean setup **before** candidate artifact
  import; its ready marker prevents later setup from deleting that candidate.
  Restore the exact package's private tests after setup/artifact import and
  before grading: setup may remove the test-image's baked `/tests` directory.
  A healthcheck that never exits 0 leaves the trial `failed` (unmeasured), never a
  pass. A package whose healthcheck waits on a service the backend does not start
  (it runs `sleep infinity`) is therefore unmeasured too.
- **Patch oracle.** `--ref-patch` (or `solution/patch.diff`) is the oracle. The
  backend stages `/solution/reference.patch` and a `solve.sh` that applies it in
  `workdir` (`git apply`, then forward-only `patch -N`; a patch neither accepts
  exits nonzero and the trial is unmeasured). The staging directory is outside the
  package, so the package digest a report binds to is untouched, and the patch
  sha256 is recorded in `facts.oracle` of every control. Without a patch the
  oracle trial fails with that reason.
- **Replay in the workdir.** An S3 `patch` reproducer applies in `workdir` for such
  packages instead of `/` or `/app`.
- **Run in the workdir.** For these packages the oracle and a shortcut run with
  `docker exec -w <workdir>`, as Harbor does. A v6 MiMo `solve.sh` finds its repository
  with `git rev-parse --show-toplevel || pwd`; in the image's default directory (`/`)
  its patch had nothing to apply to and every control came back unmeasured.
- **Embedded reference patch for v6 ref-subset.** A v6 separate-verifier MiMo
  package embeds its base64 reference patch in `solution/solve.sh`
  (`echo <b64> | base64 -d`). `HarborBackend.ref_subset` extracts it
  automatically and stages a temporary direct-grading `tests/test.sh` for the
  S2b block-drop and line-mutant pass, while S3 replays every candidate against
  the v6 package's own separate verifier.
- **Subject id.** The resolver strips the namespace from `[task].name`; `run_task`
  restores the package's own (`mimo-v2.6-rl/<id>`, `terminal-bench/<id>`) so a
  finding binds to its key. A package with no namespaced name keeps the TB2
  fallback (`terminal-bench/<id>` when the directory is the id, otherwise bare).

The S2c mutation hunt still needs an output-artifact solution; MiMo uses the
existing S2b reference-subset/ref-mutant path instead. The leak probe is unchanged:
a dangling remote HEAD can leave its history result unknown. Deterministic
`--no-model` audits retain S0, ref-subset and S3 replay without generating a
semantic proposal.

#### MiMo semantic-only S2a

`stage_s2a(report, backend, lane, config, *, run_dir, source_context=None)` is
the isolated seeker callable. `stage_s2` calls it before its unchanged S2b/S2c
work. Calling `stage_s2a` directly never imports old witnesses, extracts a
reference patch, runs reference subsets or generates reference mutants.

This path is local-Docker-only. Set `EVALLAB_DOCKER_LOCK_SLOTS=2` explicitly
before constructing/using the backend; each actual trial uses the first-party
`HarborBackend` and shared FIFO. Supply the campaign's approved, quota/spend-gated
text lane; the callable does not construct another model route.

`source_context` is a bounded mapping from safe repository-relative production
paths to pristine base-source text (160,000 characters total). One larger source
module may use that shared allowance; per-file splitting is not required.
Obtain it only from the untouched package image after its normal setup, with a
trusted read-only reader over an explicit production-path allowlist. Do not
export history, tests, fixtures, setup/grader files, solutions, reference patches
or archived wrong proposals. The reader must reject links and paths outside the
declared workdir. Existing `_ensure_images`/`_run_probe` under `backend.slot`
can perform that first-party fresh-container read; only source JSON, not setup
or grader output, belongs in the prompt. Without sufficient source, the model
can return `needs_source_context` with specific source paths; this is unmeasured,
not a fabricated patch or a pass.

Accepted responses are `candidate`, `no_candidate` or `needs_source_context`.
A candidate has `submission.kind="source_edits"` with ordered `edits`, each
containing a supplied production `path`, nonempty exact `old` text and replacement
`new` text. The old text must occur exactly once in the current file at that step.
`mimo_semantic_edit_patch(package, source_context, edits)` generates and validates
the unified diff, including EOF markers; models do not calculate hunk counts.
Unprovided/protected paths, ambiguous anchors and cancelled/no-op edits are
rejected before Docker. A concrete counterexample (`requirement`, `input`,
`expected`, `actual`) is retained for independent inspection, never treated as a
correctness judgment. Only ordinary
input/output behavior, including constant outputs and incomplete implementations,
is in scope. Reading expected-answer files or hidden data, writing rewards,
test-framework monkeypatches, introspection, process/network access and
sandbox/control-plane bypasses are not.

`grade_shortcut(..., {"kind": "semantic_patch", "content": diff}, ...)` is the
safe execution entry for an already obtained proposal, including a genuinely
batched model response. MiMo refuses the generic command/file/JSON/patch channels.
The semantic validator:

- accepts bounded, same-path text git diffs for production source; rejects
  absolute/traversal/hidden paths, tests and every path in `tests/test.patch`,
  fixtures/golden files, solutions, grader/reward/security/auth/network controls,
  runner/dependency configuration, hooks and plugins.
  Legitimate production modules such as `burnman/classes/solution.py` are allowed;
  reference-solution directories are not.
- rejects inconsistent headers, binary content, deletion, rename/copy, mode
  changes, symlink/gitlink creation, and added test-control/introspection/
  process/network code using the existing verifier guards plus the narrower
  semantic admission rules;
- applies only in the declared workdir, with runtime checks against symlinks,
  hardlinks and non-regular targets, followed by atomic `git apply --check`/
  `git apply`; there is no unsafe-path or fuzzy patch fallback. An apply failure
  aborts before grading and keeps reward null.

These are conservative admission checks, not a general security proof or a
substitute for independent source/counterexample review. Keep each
`semantic.patch`, `semantic-candidate.json` and `semantic-apply.sh` together.
The sidecar binds the original package digest and exact candidate SHA256.
`replay_reproducer` refuses changed/missing candidate custody and revalidates
the patch against the target package's workdir/protected paths before a fresh
trial. A v6 candidate transferred to released v7 is a separate transfer
measurement, not an independent v7 search. A setup failure remains unmeasured;
never substitute a different package to complete a cell.

Minimal isolated sequence, with a fresh `run_dir`, exact package/digest and the
approved `lane`/pristine `source_context` already supplied:

```python
os.environ["EVALLAB_DOCKER_LOCK_SLOTS"] = "2"
config = QATaskConfig(stages=("S2", "S3"), shortcut_attempts=1, mutants=0)
task = backend.resolve(package)
report = TaskReport(
    subject=Subject(kind="task", task_id=task.task_name, task_digest=task.package_digest),
    package=str(package), requested_stages=["S2", "S3"], started_at=utcnow(),
)
findings = stage_s2a(
    report, backend, lane, config, run_dir=run_dir, source_context=source_context,
)
stage_s3(report, findings, backend, config, run_dir=run_dir)
finalize(report, findings)
write_json(run_dir / "report.json", report.model_dump(mode="json", by_alias=True))
for finding in findings:
    write_json(run_dir / "findings" / f"{finding.id}.json",
               finding.model_dump(mode="json", by_alias=True))
```

Increase `shortcut_attempts` on the **same report** only up to three cumulative
attempts per arm. Quota admission stops do not consume a model turn; their raw
receipts are retained separately, so a later permitted continuation cannot
overwrite them. Do not wrap one physical batch response in fabricated per-item
`lane.complete` calls: retain/count the actual batch turn once and grade its
individually bound patches directly.

All admitted candidates, including reward-zero attempts, have a `reproducer`
in `report.checks["shortcut-agent"].facts["attempts"]`. Iterate those records
and call `backend.replay_reproducer(target, Reproducer.model_validate(record),
digest=exact_target_digest, run_dir=fresh_replay_dir, timeout_seconds=...)`
to measure each against its own arm or its exact partner. Persist each returned
status/facts/reason separately; transfer controls and independent incorrectness
evidence are still required before counting a wrong pass.

For a bounded actual Docker smoke, use one independently inspected production
patch from pristine source, one `grade_shortcut` trial, one fresh reproducer
replay and one fresh oracle/nop pair, with the package's bounded timeout and
FIFO slots fixed at two. No model call, reference-subset search, mutant hunt
or package mutation is needed for this smoke.


### Findings vs signals

Every `qa.finding/v1` requires a non-null `reproducer`
(`harbor_trial` | `command` | `patch`). S0 control failures are verified at
creation (the control itself is the execution). S2 candidates are created
`unverified`; only S3 can set `verified` or `refuted`. Leak detections, S1
rubric majorities, low-severity ref-subset drops and unadjudicated mutation
survivors are Signals on the report, never synthetic findings. Findings carry
an optional `root_cause` list (ids in the key's vocabulary); QA Task names none
today, so root-cause match is `n/a` (not measured), never 0.

### Measuredness and verdicts

- Check status is `measured`, `ran_without_usable_result`, or `not_run`; the
  latter two never pass.
- `quality_complete` requires at least one scored check and measured status for
  every scored check. Unscored descriptive checks normally do not block it,
  but requested S2 survivor adjudication must cover **every** `ref-mutants`/
  `mutation-hunt` survivor with a usable wrong/equivalent/unspecified decision.
  Quota-stopped, failed, missing or capped adjudications make completeness false,
  even if all numeric controls passed. Pending independent MiMo incorrectness
  review also prevents completeness. Historical raw reports are not rewritten.
  Resume admission recomputes these gates from the checks, including nested
  adjudication records, rather than trusting a stored `quality_complete` flag
  or searching only top-level reason strings. S0 reuse additionally requires
  six distinct measured job paths; duplicated/missing control custody causes
  all six controls to run fresh. An old MiMo S2a skip never satisfies the new
  semantic stage. Valid S1 checks and retained native probe reports can still
  be reused independently, without overwriting the historical source report.
- `report.verdict` is `fail` when any verified eval finding stands; otherwise
  `unknown` when measurement is incomplete, any scored check failed, any
  Signal exists, any candidate remains unverified/non-pass, or `report.error`
  is set; otherwise `pass`.
- Package bytes are re-digested after the audit; a mid-audit change sets
  `report.error` and demotes findings to `unverified`.

## CLI

```bash
# Full executed audit of one package
uv run evallab qa task --task <package-dir> \
  --rubric <benchmark>/rubrics/task-implementation.toml

# Answer-key split (packages must match pinned digests)
uv run evallab qa task --key envcheck-tb4-39 --split dev \
  --rubric <benchmark>/rubrics/task-implementation.toml

# Executed probes only, no model calls (survivors stay unadjudicated)
uv run evallab qa task --task <package-dir> --stages S0,S2,S3 --no-model \
  --mutants 4 --run-dir runs/.qa-task/manual-check

# Confirm imported candidates in S3
uv run evallab qa task --task <package-dir> --stages S2,S3 --no-model \
  --evidence <candidate-finding-or-dir>

# $0 Modal plan, including all selected tasks and conservative execution units
# No backend, Harbor/Modal runtime, Docker, quota probe or model is constructed.
uv run evallab qa task --task <package-dir> --environment modal --plan \
  --concurrency 16 --max-usd 3 --usd-per-execution 0.0025

# Capped real cloud controls; approval is recorded, not implied by --plan
uv run evallab qa task --task <package-dir> --environment modal \
  --stages S0,S3 --no-model --max-usd 3 --usd-per-execution 0.0025 \
  --approve "Authorized CPU controls, at most USD 3 in reservations"

# Digest-bound scorer (flagged vs verified); held-out rows drop items QA Task
# was tuned on, per research/answer-keys/splits/exposure.jsonl
uv run evallab qa task-score --key envcheck-tb4-39 \
  --findings runs/.qa-task/<run-id> --split heldout --exclude-exposed \
  --level verified --scoreboard research/qa-scoreboard/scoreboard.csv
```

Options: `--task`, `--key`, `--split {dev,heldout}`, `--item`,
`--packages-root`, `--run-dir`, `--stages` (default `S0,S1,S2,S3`),
`--trial-timeout` (default 1800), `--rubric`, `--model` (default
`codex/gpt-6-luna`), `--no-model`, `--mutants`, `--evidence`, `--triplet`,
`--ref-patch` (the reference patch: ref-subset's reference and, for a package
with no `solution/solve.sh`, the oracle; see "Workdir-bound packages"),
`--resume-from` (reuse digest-bound measured checks and probes from an earlier
run directory), `--environment {docker,modal}` (default Docker), `--plan`,
`--max-usd`, `--approve`, `--usd-per-execution` (positive finite; default 0.0025),
and `--concurrency` (default 1; Modal 1–16, Docker 1–2).
Run directories are fresh and immutable (an existing `--run-dir` is an error)
and must live inside the checkout. Layout: `run.json`, `summary.json`,
`tasks/<slug>/report.json`, `tasks/<slug>/s2-shortcut-a<N>/`,
`tasks/<slug>/ref_subset/`, `tasks/<slug>/mutation/`,
`tasks/<slug>/s3-replay-<suffix>/`, `tasks/<slug>/leak/`,
`findings/<sha256(finding.id)>.json`, and `codex/spend.jsonl`. Mutation trial
directories live under `runs/ec-*` (the mutation audit's own convention).

`--plan` only reads selected package/evidence metadata and prints JSON. It
creates no run directory, calls no provider or subscription model, and always
reports zero executed/reserved USD. Each selected task appears with digest,
unsupported reason, per-stage reservation upper bounds and estimated USD.
The aggregate exposes `fits_cap`, `runnable` and refusal reasons (including
missing real-run approval/cap). The bound reserves possible fresh S3 controls
and confirmations, ref-subset preparation/grades/mutants, and mutation
captures/coverage/blank controls/mutants, rather than counting only successful
trials. A shared-verifier package with neither `solution/patch.diff` nor an
explicit `--ref-patch` has no applicable ref-subset execution or confirmation
reservation; binding a reference restores both to the bound. An actual capped
run may deliberately be smaller than this full-audit
bound: allocations stop at the gate and all unfinished measurements stay
unknown. A plan is not an approval or a promise that the cap completes every
stage.

Real Modal execution requires both recorded nonempty `--approve` text and a
positive finite `--max-usd`. One thread-safe `CloudSpendGate` is shared across
all stages and tasks, with a distinct backend and model lane/scratch directory
per task. Cloud concurrency 16 means 16 independent task threads, not 16 calls
into one mutable backend. Docker's default remains sequential. Modal run
metadata, per-task reports, `cloud-spend.json` and `summary.json` include the
gate receipt (`approval`, `cap_usd`, `usd_per_execution`, `executions`,
`reserved_usd`). Per-task receipts are run-wide snapshots; the final receipt
is in the run metadata/summary. Model `cost_usd` remains separate from cloud
reservations. Reservations are conservative admission **estimates**, not
observed provider bills and not a provider billing hard kill; a failed or
short attempt retains its reservation.
The Modal rate is per **allocation/preparation unit**, not per logical trial:
each environment reserves image preparation and both possible creation attempts.
A fresh environment reserves `$0.0075`; a separate agent/verifier trial reserves
`$0.015`. Snapshot-based ref-subset grades reserve two units per clone instead.
The [reference-subset calibration](task-quality.md) describes the October 17
metered-usage cross-check and mean-based safety margin; slow or larger workloads
need an explicit higher estimate. Plan totals and cap comparisons preserve
sub-cent precision. QA Trace's `$0.01` logical-replay estimate is a different
unit and must not replace this rate or be multiplied as an allocation rate.



## Models, quota gate and spend

`CodexLane` serves S1, the S2 shortcut agent and the mutation adjudicator
through fresh text-only `CodexTransport` calls (single assistant message, JSON
only). Before **every** call it re-checks the Codex 7-day quota via
`omp usage --json --redact -p openai-codex`; an unreadable reading or
`usedFraction >= 0.60` raises `QuotaStop` and the remaining model work is
`not_run`. Calls are $0 subscription usage, logged with
`SPEND_TAG = "2026-10-15"` and the weekly used fraction to
`<run>/codex/spend.jsonl`. Model tokens count usable replies only (a lower
bound, not a bill). No paid model API is used; Modal cloud execution has its
own explicit reservation gate described above.

## Scoring (`evallab qa task-score`)

`score_findings` binds findings and reports by exact
`(subject.task_id, subject.task_digest)`; digest mismatches are excluded and
counted. Rates are over the non-excluded items of the split (not only measured
ones):

- `verified_*`: digest-bound Finding with `fault="eval"`, `verdict="fail"`,
  `refutation.status="verified"`;
- `flagged_*`: any standing unrefuted Finding or any report Signal;
- `by_label_strength`: recall on positives sliced by the key's
  `label_strength` (for `envcheck-tb4-39`: `confirmed` EnvCheck defects and
  `epoch_flagged` defects Epoch's public review reported), so the two are
  never blended;
- `category_match_recall`: verified finding category in the key's expected
  categories; `verified_/flagged_root_cause_match`: `n/a` until findings name
  root causes (see `docs/qa-contract.md`);
- `measured_fraction` / `observed_fraction`: digest-bound reports with
  `quality_complete` / any report.

`--exclude-exposed` removes reported-split items the split guard records as
`tuned` or `selected` for QA Task (`--exclude-item` adds more). Excluded items
stay in `items`, are listed in `excluded_items`, and count as unmeasured on the
scoreboard row (`n_measured <= n_items - excluded`), which is the form the
guard accepts. Dev items are never excluded; dev recall of anything tuned on
dev is circular and is labelled so.

Scoreboard rows use `system = qa-task@verified|flagged` and status
`executed S2+S3 (<n>/<scored items> measured)` when at least one scored item
was fully measured. Never tune on the split you report
(`research/answer-keys/README.md`, `research/answer-keys/splits/README.md`).

## Limits

- Shortcut search is bounded (≤3 model attempts, ≤1 graded submission per
  attempt, ≤8 ref-subset grades, ≤24 mutants of which ≤6 are adjudicated); a
  clean run is not a certificate.
- The shortcut agent sees verifier source read-only. It does not see
  `solution/`, golden file contents, or answer keys.
- The mutation hunt grades edits of the reference's captured output files, so
  it finds assertion gaps in what the verifier reads, not gaps in what the
  solution should have written elsewhere.
- GPU and multi-service Compose tasks are blocked (`not_run`) on this host;
  their items stay unmeasured, never passes.
- Mutation and adjudication need the Codex lane; with `--no-model`, survivors
  are only Signals.
- Answer-key negatives may carry weak labels, so false-alarm rates are upper
  bounds; a verified finding on a key negative is a candidate real defect (the
  Epoch relabel found 12 of them), not automatically a false alarm.
