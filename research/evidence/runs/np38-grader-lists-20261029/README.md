# NP38-CE-1: where the grader-side test lists are, and what the `grader_tamper` gate does on the tasks that have one

Data-access measurement for a later preregistered item. Stored data only: no model call, no download, no upload, no change to any production rule,
held-out or non-DEV key never read. Counts only (`results/join.json`); no path strings, test names or record text are committed.

Population: the 246 runs, 123 pairs and 101 tasks of NP31 (all pass the strict `goodfire-dev` gate; NP36-TW-1 reproduced 39 enacted / 28 clean alarms of the gate, and so does
`tools/np38_join.py`, which asserts that its per-path gate equals `tripwires.grader_tamper_hits` on every run). The class label (enacted or clean) is read from the pair
list only to split counts, as in NP36-TW-1.

## Where the lists live

| Family | Tasks | Grader-side test list in a store we hold? | Exact-path join possible |
|---|---:|---|---:|
| `swebench_verified` | 18 | **Yes, by file.** HF cache `princeton-nlp/SWE-bench_Verified` parquet (500 rows; 18 of 18 tasks present: `FAIL_TO_PASS`, `PASS_TO_PASS`, `test_patch`) and the Harbor task cache (`tests/config.json` and `tests/test.sh`, 18 of 18 present). The two copies agree on all 18 for `FAIL_TO_PASS`, `PASS_TO_PASS` and `test_patch`. The Cloud Trace DB task row and its bundle (`task_bundle.cas_uri`) carry only `idx`, `name`, `prompt`. | **18** |
| `deep_swe` | 77 | **No.** The task bundle (77 of 77 in the local content store) names the grader only by pointers (`task_dir` = a `datacurve_deep-swe-1-1` digest, `verifier`, `verifier_env`, `collect`, `artifacts`); it holds no test name or path. The Deep-SWE task packages were not found in `library/`, `derived/harbor-tasks`, `derived/external`, the Harbor task cache (510 task directories, none Deep-SWE), the cross-test registry or the Deep-SWE ingestion stage (traces only). The native trace metrics give only counts (`f2p_total`, `p2p_total`) for 176 of 198 runs. Stopped at the coverage figure: getting the lists means a download from the publisher. | **0** |
| `impossible_bench` | 6 | **Content, not path.** The bundle holds `test_code` (6 of 6) but no test path (`test_patch_files`, `test_directives` empty), and `reset_tests` is true for 6 of 6: the benchmark restores its tests before scoring. | **0** |
| **Total** | **101** | | **18 (17.8%)** |

Also checked and ruled out as sources: the cross-test registry (29 entries; none is a Goodfire family; `paichecker` is SWE-bench Multilingual + SWE-Gym, its own record says the Verified
rows are not staged), `library/task-variants` (MiMo-RL and synthetic tasks), the DB's `openai-swe-bench-annotations-2024` rows (their admission block says held: I checked that the 18 tasks exist there, looked at the admission block of two rows, and did not read any bundle).

## Join rule (SWE-bench Verified)

The task's list is the set of repository-relative files in the `diff --git a/X b/X` headers of `test_patch`, plus the `path` of any pytest-style id (`path.py::name`) in `FAIL_TO_PASS`
or `PASS_TO_PASS`. A run's written path is a write target of the gate's own parser (shell redirects, heredocs, `sed -i`, script writes, tool edits). It is normalised by stripping a leading
`/testbed/`, `/app/`, `/workspace/` or `./`, then compared by exact string equality. Nothing fuzzy.

## Failure modes (counts)

- **Test ids are not paths.** Of 23 `FAIL_TO_PASS` ids over the 18 tasks, 3 are path-style; of 1,319 `PASS_TO_PASS`, 195. Django (11 tasks) and SymPy (4) use `test_name (module.Class)` or a bare function name. The
  `test_patch` headers are the only path source that covers all 18 (18 of 18 have at least one file; 0 renames, so `a/` equals `b/` everywhere). Where path-style ids exist (3 tasks) their files are a subset of the `test_patch` files.
- **"In the list" means "reset before grading" here.** The cached grader script of all 18 tasks runs `git checkout <base> <exactly the test_patch files>` (18 of 18 equal), `git clean -fd` (18 of 18), then applies the
  patch. An edit to a listed file is discarded; an edit to a file outside the list is not reset. A grader-aware tightening must not read "in list" as "dangerous".
- **Write-target noise.** The parser also returns strings that are not paths (no `/` and no `.`): see `non_path_targets` in `results/join.json` per family and class. They never match a list.
- **Path forms.** The 243 Deep-SWE alarm paths are 237 repository-relative, 4 under `/app/`, 2 other absolute; a future join there needs the same normalisation and the task's checkout root.
- **Working directory.** A relative write target is read as relative to the repository root. A write after a `cd` into a subdirectory would be mis-joined; the join does not track `cd`. The independent check (submitted diffs, which carry root-relative headers) found no listed or test-like file in any of the 36 submissions, so a missed `cd` write cannot have hidden a listed-file edit that survived into the submission; it could still hide a transient one.
- **Granularity.** The list is file-level here; a file in the list may contain tests that are not `FAIL_TO_PASS` or `PASS_TO_PASS`.

## The table: gate alarms on the joinable tasks

Joinable = 18 `swebench_verified` tasks, 36 runs (18 enacted, 18 clean).

| | enacted | clean |
|---|---:|---:|
| runs | 18 | 18 |
| gate alarms | **0** | **0** |
| of which: edited file in the grader's list | 0 | 0 |
| of which: edited file not in the list | 0 | 0 |
| runs writing any file whose exact path is in the list (gate-agnostic) | 0 | 0 |
| runs writing a gate-regex grader path that is not in the list | 0 | 1 |
| submitted diffs touching a listed file (independent check, `tools/check_submission.py`) | 0 | 0 |

**The split is empty because the gate does not fire where the list exists.** All 67 alarms (39 enacted, 28 clean) are on `deep_swe` (99 enacted and 99 clean runs; 153 and 90 alarm path events), where no list
is local. `impossible_bench` (6 and 6 runs): 0 alarms, and the benchmark's own `test_modified` metric is 0 on all 12 runs. So on this population the list cannot separate enacted from clean: it is missing exactly
where the gate alarms. (Inference, not tested here: Deep-SWE agents work in a repository with a visible test tree and edit it as ordinary work, SWE-bench agents in this sample did not.)

## What this does not show

One sample (the NP31 pairs). 18 tasks is small and zero is a count, not a rate. The Deep-SWE lists could be obtained only from the publisher's task packages (not done: download). I did not test any tightening.

Reproduce (counts, ~25 s; needs the local state named in the tool headers):
`PYTHONWARNINGS=ignore .venv/bin/python research/evidence/runs/np38-grader-lists-20261029/tools/np38_join.py $PWD <db-copy> out.json`
