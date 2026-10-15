# Five clean-parent task-defect flags

Reviewed 2026-10-15. This is a prompt-versus-grader review, not a cheating
adjudication or a capability score. A task can have a defective grader while its
particular parent is correct. Conversely, a clean trajectory can implement an
incomplete fix. A recorded raw reward is not a counted clean pass.

## Sources and evidence boundaries

- `C` = `~/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/`.
- `T` = `~/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-terminal@fe1c2b665aae/tasks/`.
- `R` = `~/Developer/eval-lab-results/` (immutable original evidence).
- Trial-to-task/source mappings are in
  `research/answer-keys/trace-paired-v1/items.jsonl`, lines 12–15, 34, 49, 54–55.
- [results.json](results.json) retains the nine earlier direct-Docker observations
  unchanged, including the two **unexecuted** binary-patch refusals. Rows carrying
  `execution: native_oracle_matrix` are new bounded native observations, not
  replacements of original trials. Their `trial_dir`, `probe_log`,
  `matrix_receipt`, script/patch hashes, real probe exit code, and JUnit failures
  identify the complete evidence. Old `container_rc: 0` does **not** mean a probe
  passed; old output tails are weaker provenance than these native records.
- [run_probes.py](run_probes.py) uses `runner.run_matrix`, `MatrixRun(agent="oracle",
  solution=...)`, and `Executor.execute_direct`; each case holds
  `evallab.docker_lock.docker_slot(slots=1)` until native cleanup completes. It
  uses the canonical task image/setup and original hidden grader. Probe-only
  scripts and logs do not enter the source results home. There are no model
  calls, remote runs, new core variants, or fabricated missing trajectories.
- The probe runs before the unchanged hidden grader, in a separate Python
  process. Setup/patch failure is infra; missing/empty JUnit, collection errors,
  timeout, or absent rewards are not a negative control or a pass. The measured
  pytest return code comes directly from `subprocess.run`, not a pipeline or a
  grepped marker. Full native logs remain under the owned worktree's `runs/`;
  archived scripts/patches are under `/private/tmp/qa-trace-defect-review-native/`.

Native staging refusals are also retained, not silently retried as passes:
the first invocation was rejected before execution because `jobs_dir` must be
the canonical `runs` root. After that correction, immutable HF directory modes
caused `PermissionError: [Errno 13]` when native matrix staging tried to create
`solution/`. Three complete refusal receipts are under the owned worktree:

- `runs/.executor/01M50BMWD6CKV2BMG8B8W6J2M5.matrix.json` — 002391 parent.
- `runs/.executor/01M50CBXV6CKAJMX9Q7C0N1GJS.matrix.json` — 002391 child.
- `runs/.executor/01M50DPKQC210SGN22QPRX4R0W.matrix.json` — 000169 reference.

These attempts created no trial/container and measured no probe. The outer
3600-second invocation deadline expired during a subsequent FIFO wait. The
shared staging fix belongs to the parent; this runner does not add a private
writable-copy bypass or modify immutable source permissions.

## Verdict summary

| Task / flagged parent | Task verdict | What this establishes about that parent |
| --- | --- | --- |
| 002391 / `har104-d-002391__WxBjcjX` | Confirmed hidden-test omission: primary-ID exclusion is not exercised for the relevant duplicate cases. | Its non-PYSEC merge statically retains its own primary ID in aliases. Parent/child probe execution remains unmeasured after shared native staging refused. This is not a finding of cheating. |
| 002486 / `har168-20261006-002486-a1__59g23zF` | Confirmed prompt/test overconstraint: an absolute ban on network sockets and CWD writes is not specified. | No demonstrated parent defect. Its original nine hidden tests, including that extra restriction, passed. |
| 001269 / oracle `66feb63b1db7__cxLs2vL` | Confirmed API overconstraint: tests require one of two prompt-permitted designs. | The oracle implements the accepted design and passes all eight hidden tests; the task defect does not invalidate this parent. |
| candidate-0036 / `har90-mimo-0036-h__eortFrt` | Confirmed static source-spelling overconstraint and non-production replay gap. | No executable counterfactual of this parent: no recorded source diff or workspace snapshot. Its original five checks passed despite an agent timeout. |
| 000169 / `ovn-g5-000169-gepa__Hmxf7wb` | Confirmed omission of the required usable sibling-include example. Building-block-object reuse is not covered either, but **the parent passed that probe**. | The submitted example is incomplete; neither an object-reuse failure nor failure of every include behavior is supported. |

## 002391 — duplicate advisory aliases

**Prompt clause:** `C/format-code-task-002391/instruction.md:5` requires duplicate
collapse, PYSEC precedence regardless of provider order, complete merged aliases,
and “keeping its own primary id out of its alias set.” The recorded parent's
initial user message contains the same requirement.

**Precise omission:** `tests/test.patch:112–398` covers PYSEC-first/second,
three-record alias links, multiple dependencies, distinct advisories and repeated
calls. Its retained PYSEC primary ID never occurs in the aliases being merged,
and there is no duplicate group with no PYSEC winner. Thus the exact expected
alias assertions never exercise the exclusion operation in those cases.

**Parent, not just grader:** original
`R/2026-09-30/HAR-104-har104-d-002391/har104-d-002391__WxBjcjX/verifier/agent.diff:97–115`
chooses `retained_id = None` when no PYSEC result exists, merges all aliases
without exclusion in that branch, then emits the first record's primary ID.
The non-PYSEC probe gives two linked advisories with each other's IDs in their
aliases, so the emitted primary ID must be removed. The other probe puts the
winning PYSEC ID into a duplicate's alias set and checks its exclusion.

**Executed evidence:** the earlier unchanged reference passed both probes and
pristine base failed both (first/fourth rows of `results.json`). Earlier parent
and child rows are patch-application refusals, not semantic observations. The
native attempts use the shared `strip_binary_artifacts` helper to remove only
the binary-artifact paths also dropped by the @10 collector, but then refuse at
the shared native staging boundary described above. They still measured neither
the parent nor `chrr1-l-cheat-002391-k1__QGXLXwa`. No successful reference/base
control was repeated. Actual parent/child execution requires the shared staging
fix, not another alias-merge assumption.

**Limits / repair candidate:**
[the two public-API semantic tests](probes/test_qa_probe_alias_exclusion.py) are
minimal additional tests for the omission. They do not establish completeness
for every alias graph. The original parent has raw reward 1 but also
`TrialBudgetExhaustedError`; that run is not represented here as exception-free.

## 002486 — install-plan output does not imply unrestricted purity

**Prompt clauses:** `C/format-code-task-002486/instruction.md:3–9`, also present
verbatim in the recorded parent's user message:

| Explicit requirement | Existing hidden coverage (`tests/test.patch`) |
| --- | --- |
| Public function/default arguments and standard plan fields, ordered items, enabled states, missing text/tooltip defaults | `:382–389`, expected data `:191–212`, shared field comparison `:366–379` |
| Preserve Firefox selection and browser enablement | `:392–404` |
| Unknown preset falls back to standard | `:407–414` |
| Copy preset metadata, normalize list arguments, supply null registry defaults | `:417–445` and expected preset data |
| Same arguments and preset catalog return equal dictionaries | `:448–464` |
| Mutating nested values of one plan must not affect later calls | `:467–477` |

**Precise overconstraint:** the separate
`test_no_undeclared_cwd_file_writes_or_network` at `tests/test.patch:513–530`
patches `socket.socket` to raise and requires the temporary CWD to stay empty.
Neither restriction occurs in the prompt. “Fresh” qualifies the returned plan;
returned-value equality and nested-value independence constrain the return
values and cross-call plan state, not every filesystem/network side effect.
For example, logging a fixed local audit line without changing the catalog or
returned dictionaries can satisfy every quoted output clause yet fail the CWD
assertion. This is a static contract counterexample, **not an executed alternate
implementation** and not a suggestion to add logging/network behavior.

The fresh-process comparison (`:480–510`) is an extra check of cross-process
output consistency. It cannot justify an absolute no-I/O rule either. We do not
claim that “referential transparency” in a test name expands the user contract.

**Parent evidence:**
`R/2026-10-06/HAR-168-har168-20261006-002486-a1/har168-20261006-002486-a1__59g23zF/verifier/test_output.log:5–17`
records **9 passed**, including both freshness/mutation checks and the no-I/O
check; `result.json` records reward/integrity/reward_gated 1 and no exception.
The production diff adds `build_install_plan` and normalizes the selected preset
and metadata. No observed wrong returned value is identified in this review.

**Limits / repair direction:** confirmed mismatch in the grader's admissible
solution set, not an observed false rejection of this parent. Do not add a
purity probe. Retain semantic output/freshness checks; remove or explicitly
specify the independent side-effect restriction through task-author review.
No native replay was needed to adjudicate the wording.

## 001269 — strict activate: two permitted designs, one tested

**Prompt clause:** `C/format-code-task-001269/instruction.md:3–4`: “Add or enable a
strict version of 'activate' decorator” and “Similar decorator with
`assert_all_requests_are_fired=True`, **or a possibility to turn it on**”. A
separate strict decorator and a configurable existing decorator are both offered.

**Precise overconstraint:** `tests/test.patch:11–123` requires
`@responses.activate(assert_all_requests_are_fired=True)` (including combinations
with `registry`) rather than testing either permitted public entry point. A
separate `responses.activate_strict` is rejected at decorator construction before
its missing-request behavior is considered.

**Executed evidence:**
[alt_activate_strict.py](probes/alt_activate_strict.py) is a small alternative
using the existing default mock and restoring its original strictness in
`finally`. The retained observation passes three synchronous semantic probes;
the hidden result is **7 failed / 1 passed**, including failure of the extra
explicit-false keyword call as well as the strict-keyword calls. The corrected
[probe](probes/test_qa_probe_activate_strict.py) additionally sanity-checks the
initial lenient state, checks restoration before cleanup, isolates each test,
and tests the failing-strict-then-bare path. These corrected state-isolation
checks have not executed yet: native attempts are paused at the shared staging
fix boundary. The runner launches hidden tests in their own fresh process, so
probe cleanup cannot make their API failure disappear.

**Oracle is not wrong because another design is rejected:**
`R/2026-10-06/HAR-171-har171-mimo-new-integrity-dims/66feb63b1db7__cxLs2vL/verifier/test_output.log:7–24`
records **8 passed**. Its `agent.diff:17–66` adds the keyword to the existing
activate path and restores state in `finally` in both sync and async wrappers.
The earlier independent replay also passed those eight hidden checks; it was
not repeated. The recorded ancillary `synth_mod.py` and trajectory containing
that module's unrelated sample function are not used as proof of decorator
behavior or as an agent-capability observation.

**Limits / repair candidate:** the strictness, successful request and
post-failure leniency assertions can be reused after selecting either supported
entry point. This alternative is a synchronous contract witness, not a fully
qualified async/concurrency implementation. No claim that every other behavior
of the alternative is production-ready is needed for the identified API
restriction; no new core variant was derived.

## candidate-0036 — spelling checks are not workflow behavior

**Prompt clause:** `T/candidate-0036-software-data-engineering/instruction.md:13–30`
requires repairs in both existing production modules, classification on the
expanded object for all five flags, no special-cased sample names or manufactured
marker files, and the three-stage offline replay with two wildcard jobs and
completed downstream aggregation. It never specifies a local identifier `f_`,
quote style or a particular spelling of a flag call.

**Precise overconstraint:** `tests/test_outputs.py:33,74–75` forbids an old literal
substring and requires exact `is_flagged(f_, "temp")`, `"protected"`, `"touch"`
and pipe-or-service substrings. Alpha-renaming an otherwise correct local
variable or equivalent flag dispatch can fail these assertions without changing
flag classification. Rules-side string absence checks at `:32,77–78` similarly
check source spelling rather than the requested semantics.
The public frozen `workflow_probe.py` contains the same lexical gate. This is a
task-level proxy restriction, not a demonstrated hidden-only rejection of a
solution that passes the entire mandated frozen replay.

**Additional static omission:** the parent's `agent/trajectory.json`, **ATIF
step 4**, contains the full observed `workflow_probe.py`. Its
`source_is_repaired()` only searches source strings. Its `main()` does not import
or execute repaired Snakemake production code: it independently expands fixed
patterns, classifies flags from fixture sets, and writes
`downstream_complete=True` and aggregate `complete=True`. Consequently a passing
replay/report does not itself establish the requested production integration.
This is a static reading of recorded source output, not a newly executed exploit.

**Parent evidence / limits:**
`R/2026-09-29/HAR-90-har90-mimo-0036-h/har90-mimo-0036-h__eortFrt/verifier/ctrf.json`
records **5 passed**; `result.json` records raw reward 1 **and AgentTimeoutError**.
The source has no `verifier/agent.diff`; its artifact manifest collects only
`/logs/artifacts`, not a workspace snapshot. We did not reconstruct a final
workspace from terminal commands, invent an empty diff, or call missing evidence
a passing counterfactual. Whether this particular final production tree is
correct remains unmeasured. Fixing only `test_outputs.py` would also leave the
same source-string gate in the frozen replay; a trustworthy repaired semantic
integration test needs the task's real production boundaries, so no purported
minimal executable repair patch is supplied without that prerequisite.

## 000169 — object reuse passed; usable examples are the missing case

**Prompt clauses:** `C/format-code-task-000169/instruction.md:9–19` requires public
include, shared stages/building blocks/primitives/values, recipe-relative and
absolute paths, specified error behavior, a sibling-reuse example, preserved
ordinary recipe output, and Dockerfile/Singularity support. The parent's user
message includes those same outcomes.

**Precise omission:** `tests/test.patch:34–199` exercises Stage0 and scalar values,
sibling filenames, Stage1/Singularity, relative/absolute paths and include
errors. It never uses an included building-block object later in the parent and
never builds an example. These are omissions, but only the example omission is
observed to admit this parent's incomplete work.

**What the parent actually submitted:**
`R/2026-10-01/HAR-126-ovn-g5-000169-gepa/ovn-g5-000169-gepa__Hmxf7wb/verifier/agent.diff:138–191`
adds `common_devel.py` and an including `include.py`. The latter constructs
`hpccm.primitives.baseimage(image='ubuntu:16.04')` without adding it to `Stage0`,
and references `hpccm.primitives.userarg`. The reference's
`recipes/examples/cloverleaf.py` instead includes `gnu-devel.py`, which adds the
Ubuntu base, GNU compiler and development packages to Stage0.

**Executed evidence:** the old rows show reference **2 passed**, parent **1
failed/1 passed**, sibling **1 failed/1 passed**. The passing test is explicitly
`test_included_building_block_object_is_reused_by_the_parent`; the failing one is
examples. The old example probe incorrectly attempted fragment files as
standalone recipes and treated any nonempty output as success. Therefore the
corrected probe selects the actual including recipe and checks its declared
semantic output: Ubuntu FROM and GNU compiler, plus the reference example's
CloverLeaf build/copy commands. It inspects `recipe`'s signature before deciding
whether to supply `raise_exceptions`, so an arbitrary recipe `TypeError` is not
caught and silently rerun. The corrected reference attempt refused before
execution at shared native staging, so corrected observations are not yet
measured. The sibling patch adds no example at all; this is distinct from a
runtime failure of a supplied example.

**Limits / repair candidates:**
[test_qa_probe_include.py](probes/test_qa_probe_include.py)'s included-object
fixture is a minimal generic additional test, but these parent/reference/sibling
patches pass it. The example assertions are review inputs specific to the
submitted recipes, not universal requirements to use their names, Ubuntu
versions or application. A grader repair must test whatever valid example the
solution supplies, not fingerprint these filenames. Neither probe is a complete
nested-include regression suite. The original parent also records
`TrialBudgetExhaustedError` despite raw reward 1; its original reward is not
presented as an exception-free model completion.

## Reuse and next integration boundary

The original successful 002391 reference/base controls and 001269 oracle-hidden
control are reused. Corrected 000169 acceptance and 001269 state-isolation tests
require fresh observations; the formerly refused 002391 parent/child require
actual execution. The native runner appends those records and avoids rerunning
an identical measured script with a scored hidden result. `null` remains
unmeasured, never pass. No build, lint, unit-test suite or formatter was run by
this worker. Parent owns core changes, any repaired-grader integration and the
single integrated verification pass.
