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

Only S3-confirmed candidates become verified findings. Unconfirmed or refuted
candidates never count as verified defects, and not-measured checks never
count as passes.

## Stages

| Stage | What runs | Output rule |
|---|---|---|
| `S0` | Unscored static hack scan (`backend.static`); scored `oracle` ×3 and `nop` ×3 fresh-container controls (`oracle_floor=0.9`, `nop_ceiling=0.1`); scored `leak` probe executed directly in the environment image (same `LEAK_PROBE_V1` script and parser as the Harbor probe; a missing `git` with no candidate checkout is `no-repository`, not unknown) | Measured oracle `<0.9` emits a verified `broken` finding; measured nop `>0.1` emits a verified `weak` finding. A measured leak with future history appends a `reachable` Signal. GPU or multi-service Compose packages are `not_run` with the blocker reason. |
| `S1` | Complete rubric (`--rubric` or Harbor built-in) over k=3 independent text-only Codex samples; only complete samples vote; majority per `rubric:<name>` criterion | Majority fails on `DEFECT_CRITERIA` append Signals, never Findings. |
| `S2a` shortcut agent | `bundle_verifier_aware` gives the model `instruction.md`, `task.toml`, `environment/*` and `tests/*` read-only. `solution/`, `cheat/` and `README.md` are excluded; static golden dirs under `tests/` (`golden-files`, `hidden_data`, `data`, `fixtures`) are listed in `omitted` with sizes. The prompt names three generic defect classes (`reachable`, `tamperable`, `weak`), not task-specific mechanisms. One JSON object per attempt (`exploit` / `no_credible_bypass` / `legit_solution` + submission). Each exploit is graded on the untouched base in a fresh container; failed attempts are retried with the previous agent output and verifier stdout in the prompt (max 3). Two consecutive `no_credible_bypass` replies stop early. | A submission scoring 1.0 creates an **unverified** candidate Finding (`command`/`patch` reproducer). `shortcut-agent` is a scored check: `fail` if a shortcut scored 1.0, `pass` if at least one usable reply found none, else `unknown`/`not_run`. |
| `S2b` ref-subset | `evallab.ref_subset.probe_task`: raw MiMo packages (or `--ref-patch`) use their reference patch; Harbor **separate-verifier** packages derive the reference from their own oracle and grade subsets in fresh verifier containers (#903). Shared-verifier Harbor packages are `not_run` (not applicable), never a pass. `--mutants N` also runs its line-level pass (#918): up to N one-edit mutants of the lines the reference wrote, each graded like a subset. | A measured partial-fix pass keeping ≤50% of the reference code lines (severity high/medium) creates an unverified `weak` candidate whose reproducer is `minimal.patch`. Low severity (>50% kept) is a Signal only. An exhausted grade budget is `unknown`, not a pass. A surviving line-level mutant is adjudicated against `instruction.md` exactly like an S2c survivor (check `ref-mutants`); only `wrong` ones become unverified `weak` candidates (reproducer: that mutant's patch over the base). `ref-subset` and `ref-mutants` are unscored checks. |
| `S2c` mutation hunt | `evallab.verifier_mutation.run_mutation_audit` with `--mutants N` (default 8; `0` disables) and the coverage-guided plan. Trials run through `HarborBackend.matrix_runner` (raw-docker fresh containers, same capture and coverage layout as Harbor trials) instead of Harbor jobs: ~2 s per trial instead of ~100 s. Survivors (reward kept while the file's blank control scores 0) are read by a Codex adjudicator against `instruction.md` and the diff: `wrong` (violates a quoted requirement) / `equivalent` / `unspecified`. | Only `wrong` survivors become unverified `weak` candidates (reproducer: the mutation script). Unadjudicated survivors (no lane, quota stop, more than 6) append a `weak` Signal and make the `mutation-hunt` check `unknown`. `task_broken`/`unscored` hunts are `ran_without_usable_result`. |
| `S3` | Fresh-container confirmation. Baseline controls come from S0 when it ran in the same audit; otherwise S3 runs one fresh oracle and one fresh nop. Each unverified candidate (S2 or `--evidence` import) is re-executed from its reproducer on the base environment in a new container; mutation scripts replay as oracle-channel overrides with the task's `solution/` staged. An optional subject-bound `--triplet` is checked numerically (`validate_triplet`) as an unscored gate. | `verified`/`fail` iff fresh reward is 1.0 **and** oracle and nop controls are clean. A task whose controls are not clean leaves every candidate `unverified`. Reward in (0, 1) stays `unverified`. Reward 0 with clean controls is `refuted`/`pass`. `fresh-replay` is scored: `fail` when any candidate is confirmed, `pass` when every replay and control is measured clean, else `unknown`. |

Submission kinds from the shortcut agent are converted by
`submission_to_script`: `command` (bash run in `/app` in the agent
container), `file`/`json` (base64-written to an absolute path; JSON is
validated first), and `patch` (unified diff applied with `git apply --binary`,
a Python unified-diff fallback for minimal images without `git`/`patch`, or
`patch`).
Invalid submissions are unusable, never passes.

### Execution backend

`HarborBackend.run_container_trial` mirrors Harbor's separate-verifier flow.
`run_task` holds one slot of the shared host-wide Docker FIFO lock
(`evallab.docker_lock.docker_slot`, label `qa-task/<task>`) for all of a
task's container work (S0, S2, S3), so the queue is joined once per task and
not once per trial; S1 runs before it and needs no container. The backend's
per-trial, `leak`, ref-subset and mutation calls re-enter that slot
(`HarborBackend.slot` is reentrant; `docker_slot` itself is not). At most one
QA Task container runs at a time and other tabs interleave between tasks:

1. build (and cache per package digest) the environment image and, for
   `environment_mode = "separate"`, the `tests/` image;
2. start one agent container; run oracle `solution/solve.sh` (optionally
   replaced by a mutation/capture script), a shortcut script, or nothing
   (nop); run any `[[verifier.collect]]` hooks; optionally copy the agent's
   `/logs/artifacts` out (mutation captures);
3. copy every `task.toml` artifact out (directory artifacts keep their tree;
   dict artifacts honour `exclude`);
4. remove the agent container, start a fresh verifier container, upload the
   artifacts to their declared paths, run `tests/test.sh`, read
   `/logs/verifier/reward.txt` (or `reward.json`), and copy `/logs/verifier/`
   out (coverage traces);
5. shared-mode tasks are graded in the agent container instead.

Every trial writes `agent/oracle.txt`, `verifier/test-stdout.txt` and
`result.json` under its trial directory; those are the grader outputs fed back
to the shortcut agent on the next attempt.

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
  `/var/lib/mimo/base`, which `tests/test.sh` needs; without it no reward exists. A
  healthcheck that never exits 0 leaves the trial `failed` (unmeasured), never a
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
- **Subject id.** The resolver strips the namespace from `[task].name`; `run_task`
  restores the package's own (`mimo-v2.6-rl/<id>`, `terminal-bench/<id>`) so a
  finding binds to its key. A package with no namespaced name keeps the TB2
  fallback (`terminal-bench/<id>` when the directory is the id, otherwise bare).

Not covered: the S2a shortcut agent's submission formats assume `/app` and the S2c
mutation hunt needs `solution/solve.sh`; neither is validated on patch-oracle
packages. The leak probe is unchanged and often stays `unknown` on raw MiMo images: a
dangling `refs/remotes/origin/HEAD` makes `git fsck` exit 2, so the unreachable-commit
count is unknown even though the base commit and refs are read (measured on
`format-code-task-001809`); the v6 package of `format-code-task-000657` measured `pass`.
Use `--no-model` for a deterministic audit (S0 controls, leak, ref-subset, S3 replay of
an `--evidence` candidate or of its own ref-subset
finding).

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
- `quality_complete` is true iff at least one scored check exists and every
  **scored** check is `measured` (unscored descriptive checks such as
  `static`, `rubric-sample-*`, `rubric:*` when `S2`/`S3` run, `ref-subset`,
  `ref-mutants`, `mutation-hunt`, `imported-evidence` and `supplied-triplet`
  do not block completeness).
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
and `--ref-patch` (the reference patch: ref-subset's reference and, for a package
with no `solution/solve.sh`, the oracle; see "Workdir-bound packages").

Run directories are fresh and immutable (an existing `--run-dir` is an error)
and must live inside the checkout. Layout: `run.json`, `summary.json`,
`tasks/<slug>/report.json`, `tasks/<slug>/s2-shortcut-a<N>/`,
`tasks/<slug>/ref_subset/`, `tasks/<slug>/mutation/`,
`tasks/<slug>/s3-replay-<suffix>/`, `tasks/<slug>/leak/`,
`findings/<sha256(finding.id)>.json`, and `codex/spend.jsonl`. Mutation trial
directories live under `runs/ec-*` (the mutation audit's own convention).

## Models, quota gate and spend

`CodexLane` serves S1, the S2 shortcut agent and the mutation adjudicator
through fresh text-only `CodexTransport` calls (single assistant message, JSON
only). Before **every** call it re-checks the Codex 7-day quota via
`omp usage --json --redact -p openai-codex`; an unreadable reading or
`usedFraction >= 0.60` raises `QuotaStop` and the remaining model work is
`not_run`. Calls are $0 subscription usage, logged with
`SPEND_TAG = "2026-10-15"` and the weekly used fraction to
`<run>/codex/spend.jsonl`. Model tokens count usable replies only (a lower
bound, not a bill). No paid API is used.

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
