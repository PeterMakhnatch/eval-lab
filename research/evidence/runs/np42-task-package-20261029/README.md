# NP42-CE-1: which of the 25 confirmed Task QA cases can the example compiler open, and what bytes admit the rest

Zero model calls, no paid calls, stored data only, read-only on DEV, no download, no upload, no Modal. Counts only
(`results/counts.json`, `results/store_rows.json`); no case id, task name or task text is committed. The FW-4
retest runner was not opened and `optimize retest-crosstest` was not run.

## The 25 cases

FW-4 compiler run `fw4-examples-taskqa-v3c`: 284 Task QA hard cases, 25 confirmed with strong labels (21 human, 4 executed; all positive),
refused as `source_unavailable` 15, `non_dev_input` 5, `missing_input` 5. Reproduced exactly from the same cases file on current main (scratch output dirs, since deleted).

| Question | Count |
|---|---:|
| family (`terminal-bench`, answer-key dataset `tb4-audit-v1`) | 25 of 25 |
| strictly DEV by the compiler's own gate (ledger digest `7f4703f6…`, every reference and digest check `DEV`) | 25 of 25; 0 not DEV, 0 left unread |
| distinct packages behind them | 12 |
| ref shape `task-package:<task-name>` (no `#original`/`#repaired` fragment) | 25 of 25 |
| recorded digest is a bare 64-hex string | 25 of 25 |

The compiler stops at the first refusal, so the 5 `non_dev_input` and 5 `missing_input` cases also carry a `task-package:` ref that it never reached.

## Where the bytes live, by exact digest

The digest paired with `task-package:<name>` is `evallab.registry.task_directory_digest` of the task directory (what the lab calls the task version digest;
the OQ-6 plan pins it as `pinned_native_package_digest`). **Two other functions called "package digest" do not reproduce it**: `task_import.package_digest` 0 of 12, `registry.harbor_task_digest` 0 of 12, `registry.task_directory_digest` 12 of 12.

| Store (all 12 names present in each) | Packages equal to the recorded digest | Cases |
|---|---:|---:|
| `derived/kev/raw/terminal-bench-v4.0.0/tasks/<name>` (the store the OQ-6 plan itself records; **the lane named plan-staged packages, not this one**) | 12 | 25 |
| `eval-lab-data/tb4-20261020/upstream-git-v4.0.0/terminal-bench/<name>` (git checkout of the upstream tag) | 12 | 25 |
| `eval-lab-data/tb4-20261020/upstream-hub-4.0.0/terminal-bench/<name>` | 0 | 0 |
| `derived/kev/raw/terminal-bench-tasks/tasks/<name>` | 0 | 0 |
| `derived/kev/raw/terminal-bench-1dcda8716784/tasks/<name>` | 0 | 0 |
| `eval-lab-data/open-qa-*/…/view/tasks/<name>` | 0 | 0 |
| OQ-6 staged, blinded package `inputs/packages-v3/D*` (the plan's `package`) | 0 | 0 |
| Cloud Trace DB task rows (`corpus_tasks`): 12 rows, split DEV, dataset `terminal-bench`; the local bundle holds only `metadata`, `schema`, `task_id` | no package bytes | 0 |
| Harbor task cache (`~/.cache/harbor/tasks`) | 0 of 12 names | 0 |
| `library/` | 3 name-only hits without a `task.toml` (not packages) | 0 |
| HF cache | not applicable (SWE-bench Verified only) | 0 |

**Join trap:** the names exist in six stores, but only two hold the pinned bytes. A join by name would bind the wrong bytes in four of them without any error. Only the digest decides, and it needs the right function.

Counts: resolvable now by exact digest **25 of 25** (12 of 12 packages, two stores); by a documented transform (`task_directory_digest` of `<root>/<name>`) the same 25; absent locally **0**. Public source (stored metadata, `CITATION.cff` and `LICENSE` of the store): Terminal-Bench, `github.com/harbor-framework/terminal-bench`, Apache-2.0 (the tree is labelled v4.0.0, its `CITATION.cff` says v3.0.0). No download is needed.

## The change (`failure_library/bank/examples.py`, `bank/__init__.py`)

`BankContext.task_package_roots` (env `EVALLAB_TASK_PACKAGE_ROOTS`, CLI `--task-package-root`, default empty). A `task-package:<name>` digest now binds only when some configured root holds a directory `<root>/<name>` (name must match `[A-Za-z0-9][A-Za-z0-9._-]*`; symlinked directory refused) whose
`task_directory_digest` equals the recorded digest. Same name with other bytes: `source_hash_mismatch`; no such directory: `source_unavailable`; no root configured: the old `source_unavailable: not a local byte reference`. The refusal JSON contract (`reason` and `details` in `admission.jsonl`) is unchanged.

What it does not widen: the binding only hashes. It returns no package bytes (`tests/` and `solution/` stay private), the order of checks is unchanged (the sealed-identity refusal on the input runs **before** any source is resolved), and `task-package:<name>#original|#repaired` refs are still refused.

Tests (`tests/test_issue_bank_examples.py`, 3 fail on main, 53 pass on both): exact digest admits; drifted bytes, absent package, no root and an unsafe name refuse with the stated reasons; the digest, not the name, decides between two roots; a resolvable package does not launder a sealed input (`non_dev_input`, passes on main too: it pins that the refusal stays); no package text reaches the compiled example.

## Re-run on the 25 (scratch copy; same cases file and repo root as FW-4)

| | before | after (either of the two exact stores) |
|---|---:|---:|
| admitted | 0 | **7** (7 distinct packages; all positive; 7 `task-qa-auditor-fixed-gate` demos, 0 trace demos) |
| `source_unavailable` (package) | 15 | 0 |
| `duplicate_subject` (shares its evidence unit with an admitted case) | 0 | 2 |
| `conflicting_subject_target` (one package, 6 cases whose shared evidence unit carries different kind/polarity signatures) | 0 | 6 |
| `non_dev_input` (sealed TEST identity in the input; **correct, unchanged, same 5 cases**) | 5 | 5 |
| `missing_input` (no native consumer JSON: the adjudicator's quote was not in the shown files; same 5 cases) | 5 | 5 |

Pointing the compiler at a same-named store with other bytes (upstream-hub, other KEV copies) gives 0 admitted and 15 `source_hash_mismatch`. Cross-test sets: not opened.

## Meaning

The Task QA side of FW-4 does **not** need a package download: all 25 packages are local and byte-identical to the recorded pins. The resolver was the blocker. It admits 7 of the 15 package-blocked cases; the other 8 are refused for the compiler's own reasons (2 duplicates are covered by an admitted twin, 6 conflict inside one evidence unit). The 5 sealed-identity inputs and the 5 cases with no native input stay refused, so the compiler admits 7 of 25 today; more would need native inputs for the 5, or a ruling on the 6 conflicting cases.

Reproduce (~12 s, needs the local FW-4 state named in the tool header): `tools/np42_count.py CASES BASE_ADMISSION NEW_ADMISSION OUT STORE=ROOT…`.
