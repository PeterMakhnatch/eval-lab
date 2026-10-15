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

- `S0`: neutral oracle/nop controls, static scan, and leak inspection;
- `S1`: k=3 text-only rubric review through the subscription Codex lane;
- `S2`: (a) a verifier-aware shortcut agent (PR #899 method: read-only
  `tests/` view, up to 3 attempts with grader output visible between
  attempts) and (b) ref-subset reference-block probing (PR #892);
- `S3`: fresh-container 3-control confirmation of every S2/imported
  candidate: shortcut submission scores 1, correct fix (oracle) scores 1,
  do-nothing (nop) scores 0.

Only S3-confirmed candidates become verified findings. Unconfirmed or
refuted candidates never count as verified defects, and not-measured
checks never count as passes.

## Stages

| Stage | What runs | Output rule |
|---|---|---|
| `S0` | Unscored static hack scan (`backend.static`); scored `oracle` ×3 and `nop` ×3 fresh-container controls (`oracle_floor=0.9`, `nop_ceiling=0.1`); scored `leak` probe executed directly in the environment image (same `LEAK_PROBE_V1` script/parser as the Harbor probe, without a full Harbor job) | Measured oracle `<0.9` emits a verified `broken` finding; measured nop `>0.1` emits a verified `weak` finding. A measured leak with future history appends a `reachable` Signal (no synthetic finding). GPU or multi-service Compose packages are `not_run` with the blocker reason. |
| `S1` | Complete rubric (`--rubric` or Harbor built-in) over k=3 independent text-only Codex samples; only complete samples vote; majority per `rubric:<name>` criterion (scored checks; samples are unscored) | Majority fails on `DEFECT_CRITERIA` append Signals, never Findings. Signals alone do not verify a defect. |
| `S2a` | Verifier-aware shortcut agent: `bundle_verifier_aware` gives the model `instruction.md`, `task.toml`, `environment/*`, and `tests/*` read-only. `solution/`, `cheat/`, and `README.md` are excluded; static golden dirs under `tests/` (`golden-files`, `hidden_data`, `data`, `fixtures`) are listed in `omitted` with sizes, not inlined. The agent returns one JSON object per attempt (`exploit` / `no_credible_bypass` / `legit_solution` + structured submission). Each exploit submission is graded on the untouched base environment in a fresh container. Failed attempts are retried with the prior agent output and verifier stdout in the prompt (max `--shortcut-attempts`, default 3); two consecutive `no_credible_bypass` replies stop early. | A submission scoring 1.0 creates an **unverified** S2 candidate Finding (`command`/`patch` reproducer). `shortcut-agent` is a scored check: `fail` if a shortcut scored 1.0, `pass` if at least one usable reply found none, otherwise `unknown`/`not_run`. |
| `S2b` | Ref-subset (PR #892): raw MiMo packages (or `--ref-patch`) go through `evallab.ref_subset.probe_task`; Harbor packages build a reference diff from existing files under `environment/` that `solution/` modifies, and run the greedy block-dropping `Probe` only when at least 2 code blocks change (max 8 grades). Subsets are graded as file-writing shortcut scripts in fresh containers. | A measured partial-fix pass with kept fraction ≤50% creates an unverified `weak` candidate whose reproducer is the minimal patch (`ref_subset/minimal.patch`). Low-severity drops (>50% kept) are Signals only. `ref-subset` is an unscored check. |
| `S3` | Fresh-container confirmation. Baseline controls come from S0 when it ran in the same audit; otherwise S3 runs one fresh oracle and one fresh nop. Each unverified candidate (S2 or `--evidence` import) is re-executed from its reproducer on the base environment in a new container. An optional subject-bound `--triplet` is still checked numerically (`validate_triplet`) as an unscored gate. | Candidate is `verified`/`fail` iff fresh shortcut reward is exactly 1.0 **and** oracle controls pass **and** nop controls pass; `refuted`/`pass` when the replay is measured but the triplet fails; otherwise it stays `unverified`. `fresh-replay` is scored: `fail` when any candidate is confirmed, `pass` when all replays/controls are measured clean, else `unknown`. |

Submission kinds from the shortcut agent are converted by
`submission_to_script`: `command` (bash run in `/app` in the agent
container), `file`/`json` (base64-written to an absolute path; JSON is
validated first), and `patch` (unified diff applied with
`git apply`/`patch`). Invalid submissions are unusable, never passes.

### Execution backend

`HarborBackend.run_container_trial` mirrors Harbor's separate-verifier
flow with at most **1 active Docker container at a time**:

1. build (and cache per package digest) the environment image and, for
   `environment_mode = "separate"`, the `tests/` image;
2. start one agent container; run oracle `solution/solve.sh`, a shortcut
   script, or nothing (nop); run any `[[verifier.collect]]` hooks;
3. copy every `task.toml` artifact out (directory artifacts keep their
   tree, symlinks preserved; dict artifacts honour `exclude`);
4. remove the agent container, start a fresh verifier container, upload
   artifacts to their declared paths, run `tests/test.sh`, and read
   `/logs/verifier/reward.txt` (or `reward.json`);
5. shared-mode tasks are graded in the agent container instead.

Every trial writes `agent/oracle.txt`, `verifier/test-stdout.txt`, and
`result.json` under its trial directory — these are the grader outputs
fed back to the shortcut agent on the next attempt.

### Findings vs signals

Every `qa.finding/v1` requires a non-null `reproducer`
(`harbor_trial` | `command` | `patch`). S0 control failures are verified
at creation (the control itself is the execution). S2 candidates are
created `unverified`; only S3 can set `verified` or `refuted`. Leak
detections, S1 rubric majorities, and low-severity ref-subset drops are
Signals on the report, never synthetic findings.

### Measuredness and verdicts

- Check status is `measured`, `ran_without_usable_result`, or `not_run`;
  the latter two never pass.
- `quality_complete` is true iff at least one scored check exists and
  every **scored** check is `measured` (unscored descriptive checks such
  as `static`, `rubric-sample-*`, `ref-subset`, `imported-evidence`, and
  `supplied-triplet` do not block completeness).
- `report.verdict` is `fail` when any verified eval finding stands;
  otherwise `unknown` when measurement is incomplete, any scored check
  failed, any candidate remains unverified/non-pass, or `report.error`
  is set; otherwise `pass`. A fully measured clean run (S0 controls pass,
  shortcut agent finds no bypass, S3 controls clean) therefore passes.
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

# S0 controls + leak only (no model calls)
uv run evallab qa task --task <package-dir> --stages S0 --no-model \
  --run-dir runs/.qa-task/manual-s0-check

# Confirm imported candidates in S3
uv run evallab qa task --task <package-dir> --stages S2,S3 --no-model \
  --evidence <candidate-finding-or-dir>

# Digest-bound scorer (flagged vs verified; denominators are ALL items)
uv run evallab qa task-score --key envcheck-tb4-39 \
  --findings runs/.qa-task/<run-id> --split heldout \
  --level verified --scoreboard research/qa-scoreboard/scoreboard.csv
```

Options: `--task`, `--key`, `--split {dev,heldout}`, `--item`,
`--packages-root`, `--run-dir`, `--stages` (default `S0,S1,S2,S3`),
`--trial-timeout` (default 1800), `--rubric`, `--model` (default
`codex/gpt-6-luna`), `--no-model`, `--evidence`, `--triplet`, and
`--ref-patch` (reference patch for S2 ref-subset on non-MiMo packages).

Run directories are fresh and immutable (existing `--run-dir` is an
error) and must live inside the checkout. Layout: `run.json`,
`summary.json`, `tasks/<slug>/report.json`,
`tasks/<slug>/s2-shortcut-a<N>/`, `tasks/<slug>/ref_subset/`,
`tasks/<slug>/s3-replay-<suffix>/`, `tasks/<slug>/leak/`,
`findings/<sha256(finding.id)>.json`, and `codex/spend.jsonl`.

## Models and quota gate

`CodexLane` serves both S1 and S2 through fresh text-only
`CodexTransport` calls (single assistant message, JSON only). Before
every call it re-checks the Codex 7-day quota via
`omp usage --json --redact -p openai-codex`; an unreadable reading or
`usedFraction >= 0.60` raises `QuotaStop` and remaining model checks are
`not_run`. Calls are $0 subscription usage, logged with
`SPEND_TAG = "overnight-2026-10-14"` to `<run>/codex/spend.jsonl`.
Model tokens count usable replies only (a lower bound, not a bill).

## Scoring (`evallab qa task-score`)

`score_findings` binds findings/reports by exact
`(subject.task_id, subject.task_digest)`; digest mismatches are excluded
and counted. Denominators are all selected items in the split:

- `verified_*`: digest-bound Finding with `fault="eval"`,
  `verdict="fail"`, `refutation.status="verified"`;
- `flagged_*`: any standing unrefuted Finding or any report Signal;
- `category_match_recall`: verified finding category in the key's
  expected categories (per-finding root-cause IDs are not in
  `qa.finding/v1`, so the shared scoreboard `root_cause_match` stays
  `n/a`);
- `measured_fraction` / `observed_fraction`: digest-bound reports with
  `quality_complete` / any report.

Scoreboard rows use `system = qa-task@verified|flagged` and status
`executed S2+S3 (<n>/<items> measured)` when at least one selected item
was fully measured. Never tune on the split you report
(`research/answer-keys/README.md`).

## Measured results

Historical S1-only results (2026-10-14, defensive mode, nothing tuned)
remain in git history and the milestone summary; they had
`verified_recall = 0` by design because no probes executed. Executed
S2/S3 scores on `envcheck-tb4-39` are recorded in
`~/Developer/research-context/bridge/overnight-2026-10-15/qa-task.md`
and the shared scoreboard when a run completes.

## Limits

- Shortcut search is bounded (≤3 model attempts, ≤1 graded submission
  per attempt, 8 ref-subset grades); a clean run is not a certificate —
  compound or semantic wrong answers may lie outside the search.
- The shortcut agent sees verifier source read-only. It does not see
  `solution/`, golden file contents, or answer keys; grade-time
  `/tests` access is only available to candidate code that the verifier
  itself executes/imports, which is exactly the defect class S2 tests.
- GPU and multi-service Compose tasks are blocked (`not_run`) on this
  host; their items stay unmeasured, never passes.
- Answer-key negatives may carry weak labels, so flagged false-alarm
  rates are upper bounds; verified false alarms require an S3-confirmed
  shortcut on a key-negative task.
