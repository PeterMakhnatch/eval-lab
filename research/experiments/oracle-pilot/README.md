# Oracle pilot: is there a gold fix to run? (Step 1 only — STOPPED)

Peter's question: "Look into the original MiMo data, is it also the same way?
You can even run the original MiMo tasks to check." The missing check is
**solvability**: does the real upstream fix pass the hidden tests in our
environment?

**Decision: stop after step 1.** The original data carries no gold fix, and the
hidden tests are synthetic — no upstream commit contains them (5/5 hand
checks). There is nothing reliable for an oracle to apply, so per the
assignment ("Do not fake an oracle") no pilot runs were launched. Spend $0.00,
no model calls, no Daytona. The plan for a true-fix pilot, if Peter still
wants it, is at the bottom; its 40-task selection is already picked
(`selection.json`, NOT RUN).

## 1. What the original data carries

Source: `XiaomiMiMo/MiMo-V2.6-RL-oss`, `code.parquet` (2,698 rows) at source
revision `639865fd3374018d6cb29b9fb82dd531406fcf5f` (the revision the FineEnvs
snapshot pins in every `task.toml` as `source_revision`). Verified 2026-10-01
with `handcheck.py` (downloads the 13 MB parquet once, parses every row).

Each row's `extra_info.instance_json` has **exactly these 8 fields, on all
2,698 rows** — no other key set exists:

| field | example (`format-code-task-001457`) |
|---|---|
| `cwd` | `/testbed` |
| `dataset_type` | `opensource-code` |
| `docker_image` | `format-code-task-001457:latest` (unqualified; digest only added by FineEnvs) |
| `instance_id` | `format-code-task-001457` |
| `problem_statement` | the issue text (GitHub issue body, `\r\n` line endings) |
| `test_command` | `bash /testbed/mimo_test_command.sh` |
| `test_patch` | unified diff: hidden tests + `mimo_test_command.sh` + `test_commands.json` |
| `verifier_timeout_sec` | `1800` |

What is **absent, for all 2,698 rows**:

- no gold patch / model patch / reference diff;
- no repo URL, no base commit (the base is captured from the image's own
  `git rev-parse HEAD` at setup time, not stored as data);
- no FAIL_TO_PASS / PASS_TO_PASS lists;
- `reward_model.ground_truth` is the empty string on **all 2,698 rows**.

This matches the training harness, not just the file: `OpenSourceCodeEnvironment`
(`XiaomiMiMo/mimoagent` @ `467f0a19016f0ac4d63b8d17a1f0da9ba07f232c`,
`src/mimoagent/environments/datasets/opensource_code.py`) requires exactly
those 8 fields (`_REQUIRED_FIELDS`), grades by reset → `git apply test_patch`
→ `test_command` exit code, and documents that the reference fix "usually
lives in a commit made after the base" — inside the training images, never in
the data. RL training never needs a gold patch (the reward comes from running
tests after the agent acts), so none was ever published. The FineEnvs Harbor
conversion (`FineEnvs/MiMo-V2.6-RL-harbor-code` @ `5746e2f0c5c61`, snapshot in
`~/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/`)
adds image digests, setup/test scripts and `task.toml` provenance, but likewise
ships no gold: each `tests/test.patch` is tests-only (plus the two harness
files), and `instruction.md` is `"Fix the following issue:\n\n"` +
`problem_statement` with `\r\n` → `\n`.

How much repo identity survives at all:

| signal | coverage |
|---|---|
| `problem_statement` contains a `github.com/<owner>/<repo>` URL (2,698 source rows) | 339 |
| `problem_statement` contains a 40-hex SHA (spot-checked: quoted commits inside issue text, not base pins) | 106 |
| pool `instruction.md` contains a repo URL (1,180 ledger tasks) | 149 |
| census `leak_issue_url` names the upstream issue (lab-matched via PyPI/GitHub, `har108-python-census/leak_check.py`) | 307 / 1,180 |
| pool `test.patch` only ADDS files (never touches an existing file) | 518 / 1,180 |

All 1,180 pool tasks are present in the 2,698 source rows (0 missing).

## 2. Can the upstream fix commit be identified reliably? No (0/5)

Method tested: "the commit after base whose diff contains the hidden test
changes". Five tasks with a known upstream repo+issue (lightest usable images,
five distinct repos), checked by hand against full upstream history cloned
2026-10-01 — every branch and tag (`git log --all -S <distinctive test
string> --`), plus every unmerged PR head (`+refs/pull/*/head`):

| task | upstream issue | hidden test file | distinctive string | commits containing it |
|---|---|---|---|---|
| `format-code-task-001647` | [joke2k/django-environ#173](https://github.com/joke2k/django-environ/issues/173) | `environ/test.py` (modifies existing) | `test_custom_db_scheme_no_engine_arg` | **none** |
| `format-code-task-002408` | [python-hyper/h2#510](https://github.com/python-hyper/h2/issues/510) | `test/test_invalid_headers.py` | `TestPseudoHeadersWrongDirection` | **none** (file existed historically with different content) |
| `format-code-task-000803` | [didix21/mdutils#42](https://github.com/didix21/mdutils/issues/42) | `tests/test_tools/test_table_default_align.py` (new) | `test_table_default_align` | **none** |
| `format-code-task-001870` | [matthewwithanm/python-markdownify#170](https://github.com/matthewwithanm/python-markdownify/issues/170) | `tests/test_empty_line_optimization.py` (new) | `empty_line_optimization` | **none** |
| `format-code-task-002470` | [r1chardj0n3s/parse#88](https://github.com/r1chardj0n3s/parse/issues/88) | `test_parse.py` at repo root (upstream has `tests/test_parse.py`) | `test_sign_plus_issue_reproduction` | **none** |

The hidden tests are **synthesized by Xiaomi's pipeline** (note
`test_sign_plus_issue_reproduction` — a repro-test name; every patch also
carries generated `test_commands.json` + `mimo_test_command.sh`). The real
upstream fixes exist but are *different changes with different tests*:

- django-environ#173 → fix `ef89960` ("Fix db_url_config… (#174)") touches
  `environ/environ.py` + `environ/test.py`, but its added test (`CUSTOM_BACKEND`
  via env var) is not the hidden test (`Env.db_url_config(url)` unbound call).
- mdutils#42 → fix PR #45 (commits `e957f34`/`c11571e`, merge `e47d6ce`)
  touches `mdutils/tools/Table.py` + `tests/test_tools/test_table.py`, while the
  hidden tests live in a new file `tests/test_tools/test_table_default_align.py`
  that exists nowhere upstream.

So the proposed identification rule fails 5/5, including on patches that modify
existing test files: the method cannot work when the test text was never
committed upstream. And with no gold patch in the data, no repo URL on ~3/4 of
the pool, and no base commit outside the images, there is no second reliable
route to a per-task gold fix. Running the *true* upstream fix as an oracle
remains possible for the ~307-issue subset, but that answers a different
question (does the true fix satisfy synthetic tests?) and needs its own design
— see plan.

## 3. Limits

- 5 hand checks, all Python, all from the `leak_issue_url` subset; the other
  ~875 pool tasks have no confirmed repo, so the check cannot even start there.
- Clones reflect upstream at 2026-10-01; a test added upstream *after* this
  date would read as synthetic. Against this: all five issues are years old and
  closed, and the hidden tests match the issue era's file layout, not today's
  (e.g. `environ/test.py`, since moved to `tests/`).
- GitHub API calls were unauthenticated metadata reads (timelines, commit
  search, file fetches). No model calls, no Daytona, no writes outside this
  directory and `/tmp` (parquet + clones, re-downloadable; not evidence).
- Pool ⊆ source verified by instance_id join (1,180/1,180).

## 4. Files

- `handcheck.py` — reproduces §1–§2; writes `handcheck_results.json`.
  `uv run python research/experiments/oracle-pilot/handcheck.py`
- `handcheck_results.json` — machine-readable numbers + the 5-task table.
- `selection.py` / `selection.json` — the NOT-RUN 40-task pilot selection
  (plan artifact): train split only, seed `oracle-pilot:1`, lightest
  `image_mib` first with seeded tiebreak; 15 usable / 10 review-checker-only /
  5 review-hand-or-grader_suspect / 10 discarded, each with the ledger `run`
  package (original / leak-closed / repair) and `run_digest` to run.

## 5. Plan (if Peter wants the true-fix pilot anyway)

1. For each selected task with a `leak_issue_url`, resolve the closing commit
   via the issue timeline (as §2 did for ef89960 / PR #45); tasks without one
   stay out — expect well under 40 to qualify.
2. Confirm the image base equals the fix commit's parent (`git rev-parse HEAD`
   in a Daytona probe vs the GitHub parent SHA); mismatch means Xiaomi rebuilt
   the tree and the diff may not apply — record, don't force.
3. Build the oracle variant with existing machinery (copy, don't edit:
   `har113-variants/` variant specs + `har115-census/specs.py`, `outcomes.py`,
   `probe_solve.sh`): same image/setup as the ledger's `run_digest` package,
   plus a `solution/solve.sh` (Harbor `oracle` agent entry point) applying only
   the fix commit's **non-test** diff, then the unchanged `test.sh`.
4. Grade once per task on Daytona (cap $1.00, no model calls); also record the
   repair-variant grading where one exists. Per task: oracle reward, test
   counts/failing ids if parseable, failure class (patch-no-apply /
   tests-fail / env-error), ledger status, checker label, hand labels
   (`har112/pool_labels.jsonl`, `har112/hand_{a,b,adj}/`, `har111/census_labels.jsonl`).
5. Cross-tab oracle pass rate by ledger status and by checker label, plus
   checker-broken vs oracle-fail agreement. A true fix failing synthetic tests
   would show up as oracle-fail on `usable` tasks — that is the solvability
   signal Peter asked for.

## Spend

`keys run -- uv run evallab spend day --date 2026-10-01`: $0.00 Daytona for
this ticket (no sandboxes launched). No model spend (no LLM calls of any kind).
