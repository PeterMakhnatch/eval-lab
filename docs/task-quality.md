---
status: living
audience:
  - analyst
  - operator
---

# Judging whether a task is good

A task is useful for evaluation or RL only if its reward means what it says: a pass is a real solve, a fail is a
real failure, and the task sits where the model sometimes passes and sometimes fails. Published datasets rarely
prove this. The FineEnvs MiMo Harbor ports ship no provided reference solution;
recovered history-oracle evidence must be distinguished from an actual graded control.

This page is the order of checks, cheapest first. Each stage only runs on tasks that survived the earlier ones.
The commands and tables are described in `docs/mimo-task-catalog.md`.

## One dataset audit command

`evallab audit <dataset-or-path>` joins the existing leak, oracle, no-agent,
static, run-history, and exploit evidence. It accepts a cached Harbor dataset,
an explicit `name@version` pin, or a real task/collection directory. MiMo ledger
and patch conventions live in the `audit_mimo` plugin, not the generic pipeline.

The default is **dry-run**: no download, container, model, queue submission, or
output publication. `--stages leak,oracle,nop,static,history,exploit` selects the
stages; `--json` includes each task's evidence, source hashes, and proposed actions.

```bash
evallab audit mimo-v2.6-rl --dry-run
evallab audit hello-world --execute --stages oracle,nop,leak,static,history \
  --environment docker \
  --derived-root "$PWD/runs/audit-hello/parquet" \
  --output-dir "$PWD/runs/audit-hello"
```

| Stage | Evidence and limits |
| --- | --- |
| `leak` | HAR-177 future-ref/unreachable-commit semantics; a solution-only native probe inspects discovered image repositories. Missing Git, absent repositories, failed commands, or incomplete output are unknown, not clean. |
| `oracle` | Recorded HAR-191 history-oracle findings or a native provided-solution control. Missing reference evidence stays unavailable; patch applicability is not a passing grade. |
| `nop` | The existing no-agent/output checks, bound to the executed package. Infrastructure failures are not scored failures. |
| `static` | Lexical flags, task lint, and the joined offline V1–V8 hack-scan ledger (schema, digests, recon, findings, limits), with input hashes and coverage. These describe files only and never predict quality or decide the verdict; absent scan findings are not a certificate. |
| `history` | One read-only native trial census, including controls and probes; recorded model-history summaries retain their original scope. |
| `exploit` | Existing HAR-161 probe evidence or an explicitly selected hosted-model probe. A negative probe is not proof of universal resistance. |

`--execute` enables selected local Docker/model-free controls and writes
projections. Remote controls and model-backed probes additionally require
`--allow-paid`; an explicit hosted `--model` is required for an exploit.
`--est-cost-usd` is the **per-task, per-stage total execution estimate**, including
infrastructure. The aggregate estimate is printed before dispatch; it is not an
infrastructure spending cap, and `--cost-limit-usd` is only the model cap.

Paid preparation submits a pinned spec but does not approve it. After separate
exact-spec approval, `--approved-spec ID` resumes only that bound task/stage;
opt-in never authorizes a queue drain or a direct paid run.

Execution emits three rebuildable projections, not a new evidence store:

- `audit.parquet` in the existing derived root, queryable as the `audit` relation
  in `evallab trials --sql`. Dataset/source revisions and both package and Harbor
  digests distinguish versions; no audit verdict rewrites a historical trial.
- The printed `task-health.json` manifest and metadata-only variants carrying
  `verdict:keep`, `verdict:fix`, `verdict:discard`, or `verdict:unknown`. Pass the
  manifest to `evallab view --task-health ... --tag verdict:keep`; include the
  report's `records` and `probe-records` with `--task-variants` for lineage.
- `dossiers.json`, indexing one HAR-186 dossier per package. `evallab task <id>`
  and its read-only Python tool automatically consult the existing audit
  projection; use `--derived-root`, `--audit-path`, and `--dataset` to select
  evidence explicitly. Multiple versions remain ambiguous rather than choosing
  an arbitrary latest row.

`keep` routes work; it is **not certification, admission, or validated repair**.
Generic keep requires digest-bound, completed oracle=1 and sound nop=0 controls;
a no-agent pass or observed future history routes to fix. Missing evidence stays
unknown. MiMo preserves its committed ledger routing, including discard
precedence. None of these control/probe trials measures model capability.

## Reward-hacking audit (`evallab hack`)

The dataset audit answers "is this task worth keeping". `evallab hack` answers a
different question: *which channels let a scripted agent score without doing the
work*, and whether an authored exploit actually scores through the task's own
verifier. Both halves are free and local; neither spends model tokens unless the
separate `--stages exploit` probe is requested.

- `evallab hack scan <package>` — deterministic, offline V1–V8 ledger over one
  task package: verifier isolation, answer material in the agent build, untrusted
  execution in the verifier, LLM judges, weak matching, fail-open handling,
  granted authority (network, user, privileges), plus reconnaissance facts
  (verifier mode, entrypoint, digests). Each finding cites `file:line`; findings
  are **claims**, and an absent finding is not a certificate.
- `evallab hack run <package> [--script PATH ...] [--execute]` — replays each
  exploit script through the task's own verifier by reusing the checked-in
  solution-control matrix path (`MatrixRun.solution`, oracle agent,
  `allow_billable=False`, so it can never spend). The matrix is the same artifact
  class as `research/experiments/release-branch-rescue-local-controls.json`:
  reference oracle (expect 1), no-agent control (expect 0), then one run per
  script, with `# expect: 1` on the script's first lines recording the author's
  prediction. Without `--execute` it writes the plan only.
- `evallab hack mutate <package> [--target PATH ...] [--max-mutants N] [--workers 1|2]
  [--mutant ID ...] [--no-coverage] [--execute]` — verifier mutation testing for the
  insufficient-checking family (a verifier that rewards a wrong answer). It
  captures the reference solution's target files (declared `artifacts`, or
  `--target`) through `/logs/artifacts`, then grades one blank control per file
  and a balanced sample of one-edit mutants (comparison boundaries, `and`/`or`,
  `+`/`-`, a dropped operand, `min`/`max`, conditional branches, negated flags
  and conditions, dropped statements, one reverted hunk of the reference
  solution's change; Python by AST, C-family lexically, JSON by value) with the
  same oracle-channel matrix path. A mutant that keeps full reward while the
  file's blank control scores 0 is a **survivor**: a reproducible input written
  as `inputs/<id>/<id>.patch` against the reference output, recorded in
  `hypotheses.jsonl` with `status: candidate`. It may be behaviour-equivalent or
  change behaviour the instruction does not require, so a human or an objective
  check confirms it before it becomes a task finding.

  **Coverage-guided** (default; `--no-coverage` turns it off). When a captured
  target is Python, one more reference run grades through an instrumented copy
  of the package (`<run>/coverage-package/`): a prologue atop `tests/test.sh`
  hooks a line tracer (`sys.monitoring`, `sys.settrace` before 3.12) into every
  Python 3.8+ interpreter it finds, in shared and separate verifier modes alike,
  and each verifier process writes the executed lines of the targets to
  `/logs/verifier/envcheck-coverage/` (`src/evallab/verifier_coverage.py`).
  Lines count only for the exact reference bytes (SHA-256), so a fail-to-pass
  baseline run on the original code cannot mark them. Mutants on lines the
  hidden tests never execute survive by construction and are not run
  (`files[].unexecuted`); `files[].untested_fix_lines` lists reference-fix code
  the tests never run, and one **partial-fix** probe per file (the reference fix
  minus those hunks) runs first: if it keeps reward, the grader accepts a
  partial solution. The trace is used only when the instrumented reference
  scores 1 and at least one target ran under the tracer; otherwise
  `coverage: unavailable` (reason in `notes`) and every line is a candidate.
  Limits: Python targets only; a verifier that runs the code in an interpreter
  the prologue did not find, or replaces `sys.settrace` (pytest-cov on <3.12),
  falls back to unguided.
- `evallab hack battery <package> [--target PATH ...] [--execute]` — the
  hollow-program battery (Bohrin battery 2, `src/evallab/verifier_battery.py`):
  after the reference solution, every function it changed in each target Python
  file is replaced by five fakes that do no work (each must score 0) and three
  meaning-preserving rewrites (comments dropped, `ast.unparse` layout, renamed
  locals; each must score 1, like the oracle). Verdicts `clean` /
  `fake_rewarded` / `control_rejected` / `no_probes` / `task_broken` /
  `unscored`; receipts in `runs/.envcheck/battery-<slug>-<nonce>/`. `tasks admit`
  runs it as the `battery` step.
- `evallab hack hunt --manifest <manifest.csv> --output-dir <runs/dir>
  [--sample-size 32] [--max-mutants 12] [--execute]` — inventory all known-fix
  tasks, then run a deterministic, stratified local Python subset. The two seed
  cases are 002402 and 002391; the remaining sample balances single/multiple
  source files and small/medium/large reference patches. `hunt-plan.json`
  freezes the manifest digest and selection; `hunt-results.json` ranks only
  survivors explicitly traced as executed, with passing reference/nop controls.
  Unsupported languages, missing coverage and infrastructure failures stay
  unrankable, not zero-filled. Execution is serial. For a hard two-container
  ceiling, set `EVALLAB_STATE_JOURNAL=off`: native Docker's agent plus egress
  sidecar consume two slots, and the optional state observer would add a third.
  This path does not dispatch cloud compute or estimate fleet-wide prevalence
  from its sample.
- `evallab hack repair <mutation-report.json> --model gpt-6.1-sol
  [--execute] [--output-dir <runs/dir>] [--json]` — propose missing semantic
  assertions through the installed ChatGPT-subscription Codex CLI. Preview
  writes bounded context and the prompt without a model call. Execution checks
  live account-wide weekly usage before proposing and refuses unavailable
  quota, non-subscription auth or consumption above 60%; it never substitutes
  a model or automatically retries a spent/ambiguous attempt. For an explicit
  refinement, use `--feedback <prior-repair-result.json>` and a new output
  directory: the next proposal receives the retained verifier failures as
  evidence, rather than rerunning the model blindly.
  Optional model metadata may be null: recorded identity comes from the
  caller-selected subscription route, not the proposal's self-description.
  A null command is allowed only for an explicit not-repairable response.
  Malformed or unsafe proposals are `blocked`, not proof that the task is
  unrepairable. A CLI reconnect notice can be followed by one valid completed
  text-only turn; its original transcript and usage remain authoritative.
  Failed, incomplete, rerouted, or multiple turns still fail closed.

  The proposal appends new test-file diffs to `tests/test.patch` and wraps
  `tests/test_command.sh`, preserving the original suite's exit status.
  MiMo v5's pristine and candidate phases both receive the additional tests.
  Variants use the existing `tasks derive` implementation and lineage store;
  original task packages, production source and solutions remain unchanged.
  Native matrix validation replays digest-checked reference and nop first;
  failed controls stop before spending time on survivors. Task admission needs
  an executed, full-reward survivor, but validation then replays **every**
  previous survivor, including unexecuted fix omissions and partial survivors.
  The ranking filter must not shrink the acceptance subset. Only complete
  **1/0/all-0** yields `repaired` and validated lineage. Equivalent/out-of-scope
  survivors may leave a `partial` candidate for review; they never justify
  source fingerprints or incidental-behavior assertions. Other outcomes are
  `rejected`, `unrepairable`
  and `blocked`. Receipts include the private Codex transcript/usage, proposal,
  derived record, validation matrix and `repair-result.json`.
  Passing the numeric gate does not establish that every added assertion is in
  scope. Check the assertions against the instruction before accepting a task
  variant for training; unrelated API tests cannot turn a survivor into a defect.
- `evallab hack ref-subset run <package> --patch <reference.patch> --output-dir DIR`
  and `... sweep --index research/experiments/mimo-reference-fixes/index.csv
  --tasks-root <raw MiMo tasks> --output-dir DIR [--lanes 1|2]` — the
  reference-subset weak-test probe (`src/evallab/ref_subset.py`, QA Task S2).
  - **Blocks.** The reference fix is split into change blocks: contiguous edited
    lines per file, computed on the base and fixed contents. Docs, comment-only
    blocks and Python blocks that leave the docstring-free AST unchanged do not
    count as code.
  - **Grading.** Each grade puts a subset of the blocks on the base checkout and
    runs the task's own `tests/test.sh`. Grades use local Docker with no network,
    one fresh container per grade from a snapshot taken after the task's setup.
  - **Separate-verifier tasks.** `run <harbor task>` without `--patch` probes a
    Harbor task whose `[verifier] environment_mode = "separate"` grades
    `artifacts`, e.g. Terminal-Bench 4. The task's own oracle (`solution/solve.sh`)
    runs once in the environment image; its artifact diff becomes the reference
    (`reference.patch`, paths relative to `/`). Each grade is a fresh container
    of the `tests/` image that receives only the artifacts, as Harbor uploads them.
    These containers keep Docker's default network, because oracles and verifiers
    install packages. GPU tasks are `unavailable`, and the built images are
    removed afterwards.
  - **Minimisation.** A greedy pass first checks the baselines (full reference 1,
    base 0). It then drops the docs and comment blocks, then each file, then
    each block, largest first. It ends with a confirmation grade.
  - **Outputs** in `DIR/<task>/`:
    - `report.json` (`evallab.ref_subset.report/v1`);
    - `finding.json` (`qa.finding/v1`, category `weak`);
    - `minimal.patch`, the reproducer: `git apply` it on base and grade, expected reward 1;
    - `dropped.patch`, the reference behaviour no hidden test needs.
  - **Statuses:** `measured`, `budget_exhausted`, `baseline_mismatch`, `flaky`,
    `not_applicable` (fewer than two code blocks) and `unavailable`.
  - **Severity** comes from the kept share of reference code lines: at most 25% is
    `high`, at most 50% is `medium`, anything else dropped is `low`.
  - **Caveat.** A dropped block is a hypothesis: it can be optional behaviour,
    so read `dropped.patch` against the instruction.
  - **Trace check.** `... trace <trial>... --reports DIR` is the matching QA
    Trace T0 check. A passing trial whose `verifier/agent.diff` leaves every
    untested reference block alone is a suspected partial fix
    (`false_positive`, unverified).
- `evallab hack ref-repair <ref-subset report dir> [--execute]
  [--feedback <prior repair-result.json>]` repairs a weak verifier with
  instruction-scoped test additions. Intake verifies the task/package and
  reference digests and replays the probe's **cumulative** greedy removals,
  rather than substituting independent one-block deletions.
  - `--partial-fix LABEL=agent.diff` adds an observed real partial submission;
    repeat it for every old pass. For report-less tasks, supply `--package`
    and `--reference-patch`. These inputs assert provenance, not correctness:
    the reference must still pass every control.
  - The subscription proposer shares `hack repair`'s schema, fingerprint bans,
    and live 60% quota gate. Tool-bearing, failed and incomplete model turns
    are rejected; text-only preambles do not invalidate a completed proposal.
  - `--proposal proposal.json --execute` grades retained or reviewed test
    proposals with **no model call**. Its receipt hashes the input proposal;
    it does not manufacture a quota reading or repeat token usage.
  - Test-only variants append new tests and preserve the original command's
    exit status. Bare `python`/`python3` use `/opt/test_venv/bin/python3` when
    available; explicit interpreter paths remain explicit.
  - Acceptance requires full reference **3/3**, nop **0/3**, and every
    expected shortcut exactly once with reward 0. Missing, duplicate,
    substituted or unscored checks cannot certify a repair.
  - `repaired` is the measured gate result; instruction-scope review is still
    required. Declared equivalent/out-of-scope survivors produce `partial`,
    never `repaired`. Other outcomes are `rejected`, `unrepairable`, `blocked`.
    Unsupported patch paths, file-mode changes and symlinks are blocked,
    not silently converted into a weaker submission.
  - Receipts (result, expected shortcut IDs, proposal, quota when a model is
    called, usage/proposal provenance) land in `--output-dir`. Attempts are
    not overwritten; failed lineage updates remain explicit in the result.

Receipts land in `runs/.reward-hack/<slug>-<ulid>/` (`hack-report.json`,
`hack-report.md`, `matrix.json`, staged script copies with digests); the matrix
receipt and invocation log stay under `runs/.executor/`. Verdicts are
`hackable` / `partially_hackable` / `not_demonstrated` / `task_broken` /
`unscored` / `planned`. `task_broken` (a no-agent pass, or a failing reference
solution) outranks hackability and must be fixed before quoting any number from
that run path. A resisted script is a negative result for that channel on that
exact revision, never a certificate of robustness. `hack mutate` receipts land in
`runs/.envcheck/<slug>-<nonce>/` (`mutation-report.{json,md}`, `hypotheses.jsonl`,
`inputs/`, `capture/`, `coverage-package/`, staged scripts and matrices); verdicts
are `survivors_found` / `no_survivors` / `no_mutants` / `task_broken` / `unscored` /
`planned`, with a mutation score of killed over informative scored mutants and
`coverage` `guided` / `unavailable` / `disabled`.

The authoring procedure — legitimacy rules, channel order, honesty requirements —
is `.omp/skills/reward-hacking-audit/SKILL.md`. Only the replay adapter is
Harbor-specific; the ledger and rules port to other harnesses unchanged.


## Stage 0: read the files (free, no Docker)

`evallab tasks catalog build` records one row per task version in `task_versions` and one row per failed rule in
`task_findings` (`src/evallab/task_lint.py`).

| Question | Finding rule | Why it matters |
|---|---|---|
| Are these the bytes we think? | `mimo-manifest-digest-mismatch` | A silent upstream change invalidates every earlier result |
| Can anyone solve it? | `mimo-no-oracle` | Without an oracle, "no model passes" cannot separate hard from broken |
| Can the agent see the answer? | `mimo-answer-leak`, `mimo-git-history-readable` | A pass may be copying, not solving |
| Can the agent change the grader? | `mimo-verifier-not-isolated`, `mimo-terminal-hook-planting`, `mimo-testmain-plantable`, `mimo-conftest-plantable` | A root agent in the grading container can forge a pass |
| Can the agent fetch the fix? | `mimo-network-public` | Public network plus root means no network guarantee |
| Does grading cost money or depend on a third party? | `mimo-paid-judge`, `mimo-verify-network-dep` | Judges add cost and nondeterminism; grading downloads can fail |
| Will another runner set the task up? | `mimo-setup-healthcheck-only` | Runners that skip Harbor's healthcheck (e.g. Tinker's `harbor_rl` recipe) never run setup, so every attempt is ungradable |
| Do siblings straddle splits? | `split_group`, `split-group-unresolved` | A train/held-out split by task id leaks near-identical tasks |

## Stage 1: controls (free, local Docker)

- `nop` agent: reward must be 0 and the verifier must finish. A pass is a free reward; a verifier error means
  setup or grading is broken.
- Oracle: only where a provided or recovered reference exists; require a completed, bound verifier result.
- Record both per backend: `evallab tasks qualify-collect` writes one
  `task_qualification` row per trial (reasons, status, Daytona cost estimate)
  and `catalog export-broken` publishes the per-backend broken list —
  full rules in `docs/mimo-task-catalog.md` ("Backend qualification").
  In particular a `grader_broken` trial means the grader's own pytest
  collection failed under a control agent (root-cause line in `grader_error`;
  guarded so a module the agent is supposed to create does not flag), and a
  setup-phase disk-capacity failure is `backend_quota` (inconclusive), never
  a task defect.

## Stage 2: rollouts (model cost, needs approval)

Run k attempts per task (4 to 8) with one pinned agent and model. `v_task_outcomes` gives the verdict per task
version and agent/model, with the Wilson 95% interval on the pass rate:

| Verdict | Meaning | Use |
|---|---|---|
| `learnable` | some attempts pass, some fail | RL signal; keep |
| `always_pass` | every scored attempt passes | no RL gradient; fine as a regression check |
| `always_fail` | every scored attempt fails | too hard or broken; audit before keeping |
| `infra_only` | no attempt produced a reward | infrastructure or setup problem, not a model result |
| `untested` | no trials | run it |

Errors are not zeros. A missing reward (setup failed, judge down, timeout in grading) must never be counted as a
fail, and must never become reward 0 in a training batch. Control agents (`nop`, oracle) get their own rows and
never count toward a model's verdict.

## Stage 3: audit the verdicts

- Flaky grading: rerun the verifier on the same final state with
  `--verifier evallab.harbor_repeat_verifier:RepeatVerifier --verifier-kwarg repeat_n=3`
  (`docs/task-stability.md`). A verdict that changes between reruns is noise; the MiMo report (§4.2.1) used 8.
- False passes: `src/evallab/mimo_exploit.py` marks a trial `suspected` when its trace reads hidden files, edits
  tests, `/etc/hosts` or reward files, or fetches the fix. A reviewer confirms with `confirm_exploit`. The hack
  probe (`research/experiments/mimo-hack-probe/probe.md`) deliberately asks a model to cheat, to find these holes
  before training does.
- False fails: for `always_fail` tasks, check whether the instruction and the hidden tests agree.

`v_task_audit` combines these: `train_eligible` requires `learnable`, stability evidence that says `stable`, and
no confirmed or unreviewed suspected exploit. A version with an `error`-severity curated finding
(`library/task-findings/`, evidence-backed and reviewable) is train-ineligible first, with reason
`finding: <rule>` — a hard defect outranks every other reason. `evallab tasks catalog export-eligible` also drops held-out tasks and
writes the list with its sha256, plus the digests of the tables it was built from.

## Stage 4: behavior

Turns, tokens, repeated tool calls ((N − U) / N over identical calls in a turn), and error classes, from
`evallab report run`. The independent model capture (`docs/model-capture.md`) shows whether the harness trace is
complete before any of these numbers are trusted. On the Terminus-2 lane it is not: summarization and handoff
calls never appear as trajectory steps.

## Changing a task

A fixed or rewritten task is a new version, never an edit in place. See `docs/task-variants.md`: the original
stays as the control, the variant records its parent, transform and changed files, and runs join to the exact
version they used.
