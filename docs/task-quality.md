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
| `static` | Lexical flags and task lint, with input hashes and coverage. These describe files only and never predict quality or decide the verdict. |
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

Receipts land in `runs/.reward-hack/<slug>-<ulid>/` (`hack-report.json`,
`hack-report.md`, `matrix.json`, staged script copies with digests); the matrix
receipt and invocation log stay under `runs/.executor/`. Verdicts are
`hackable` / `partially_hackable` / `not_demonstrated` / `task_broken` /
`unscored` / `planned`. `task_broken` (a no-agent pass, or a failing reference
solution) outranks hackability and must be fixed before quoting any number from
that run path. A resisted script is a negative result for that channel on that
exact revision, never a certificate of robustness.

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
