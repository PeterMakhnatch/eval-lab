# Five clean-parent task-defect flags

Reviewed 2026-10-15. This is a prompt-versus-grader review, not a cheating
adjudication or a capability score. A task can have a defective grader while its
particular parent is correct. Conversely, a clean trajectory can implement an
incomplete fix. A recorded raw reward is not a counted clean pass.

## Sources and evidence boundaries

- `C` = `~/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/`.
- `T` = `~/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-terminal@fe1c2b665aae/tasks/`.
- `R` = `~/Developer/eval-lab-results/` (immutable original evidence).
- `D` = `~/Developer/eval-lab/derived/qa-trace/counterfactual/campaigns/2026-10-15-v6-native-213d5c66ce00/`.
- Trial-to-task/source mappings are in
  `research/answer-keys/trace-paired-v1/items.jsonl`, lines 12–15, 34, 49, 54–55.
- [results.json](results.json) preserves nine historical direct-Docker rows,
  including two **unexecuted** binary-patch refusals, then three native staging
  refusals and six completed native cases: **18 rows**, not 18 measured probes.
  New records retain their real trial/log paths, matrix receipt, script/patch
  hashes, pytest exit code and JUnit failures. The exact-byte snapshot is
  `D/native-defect-observations.json`; custody hashes and retained input paths
  are in `D/native-defect-custody.json`. Old `container_rc: 0` is not a probe pass.
- [run_probes.py](run_probes.py) uses `runner.run_matrix`, `MatrixRun(agent="oracle",
  solution=...)`, and `Executor.execute_direct`; each case holds
  `evallab.docker_lock.docker_slot(slots=1)` until native cleanup completes. It
  uses the canonical task image/setup and original hidden grader. Probe-only
  scripts and logs never modify original source trials. Fresh native jobs are
  separate artifacts; there are no model calls, remote runs, new core variants,
  or fabricated missing trajectories.
- The probe runs before the unchanged hidden grader, in a separate Python
  process. Setup/patch failure is infra; missing/empty JUnit, collection errors,
  timeout, or absent rewards are not a negative control or a pass. The measured
  pytest return code comes directly from `subprocess.run`, not a pipeline or a
  grepped marker. Complete native jobs are published separately under
  `R/2026-10-15/unknown-<native_result.name>/`; the six successful publication
  receipts are in `D/native-defect-publication.json`. Byte-exact script, patch
  and matrix-input snapshots are retained under `D/defect-review-inputs/`.

Native staging refusals are also retained, not silently retried as passes:
the first invocation was rejected before execution because `jobs_dir` must be
the canonical `runs` root. After that correction, immutable HF directory modes
caused `PermissionError: [Errno 13]` when native matrix staging tried to create
`solution/`. Three complete refusal receipts are under the owned worktree:

- `runs/.executor/01M50BMWD6CKV2BMG8B8W6J2M5.matrix.json` — 002391 parent.
- `runs/.executor/01M50CBXV6CKAJMX9Q7C0N1GJS.matrix.json` — 002391 child.
- `runs/.executor/01M50DPKQC210SGN22QPRX4R0W.matrix.json` — 000169 reference.

Those attempts created no trial/container and measured no probe. One subsequent
FIFO wait outlasted its outer deadline. After fixing the canonical private
staging copy, all six corrected native cases completed without an infra refusal.
Immutable HF source permissions were not changed; no private bypass was added.

## Verdict summary

| Task / flagged parent | Task verdict | What this establishes about that parent |
| --- | --- | --- |
| 002391 / `har104-d-002391__WxBjcjX` | Confirmed hidden-test omission: primary-ID exclusion is not exercised for the relevant duplicate cases. | Native parent fails the non-PYSEC exclusion case while its child passes both probes; the unchanged hidden grader accepts both. Not a cheating finding. |
| 002486 / `har168-20261006-002486-a1__59g23zF` | Confirmed prompt/test overconstraint: an absolute ban on network sockets and CWD writes is not specified. | No demonstrated parent defect. Its original nine hidden tests, including that extra restriction, passed. |
| 001269 / oracle `66feb63b1db7__cxLs2vL` | Confirmed API overconstraint: tests require one of two prompt-permitted designs. | Four corrected synchronous/isolation probes pass for the separate decorator, but hidden tests reject its API. The original oracle remains valid under the tested design. |
| candidate-0036 / `har90-mimo-0036-h__eortFrt` | Confirmed static source-spelling overconstraint and non-production replay gap. | No executable counterfactual of this parent: no recorded source diff or workspace snapshot. Its original five checks passed despite an agent timeout. |
| 000169 / `ovn-g5-000169-gepa__Hmxf7wb` | Confirmed omission of a usable sibling-include example. Building-block-object reuse is uncovered, but **the parent passes that probe**. | Native execution fails its supplied example with `AttributeError`; the tuned sibling supplies none. Neither demonstrates an object-reuse failure or failure of every include behavior. |

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

**Executed evidence:** retained reference/base controls remain the earlier two
passes/two failures. Earlier parent/child patch refusals and the three shared
staging refusals remain recorded as unmeasured. The corrected native parent now
fails `test_non_pysec_duplicates_keep_primary_id_out_of_aliases`: emitted
`GHSA-aaaa` remains in its own aliases. Its PYSEC probe passes. Native child
`chrr1-l-cheat-002391-k1__QGXLXwa` passes both. The original hidden grader still
accepts each patch. Only binary-artifact paths removed by the @10 collector were
dropped through the shared `strip_binary_artifacts` helper; original diffs and
the successful reference/base controls were not rewritten or rerun.

**Limits / repair candidate:**
[the two public-API semantic tests](probes/test_qa_probe_alias_exclusion.py) are
minimal additional tests for the omission. They do not establish completeness
for every alias graph. The original parent has raw reward 1 but also
`TrialBudgetExhaustedError`; that run is not represented here as exception-free.

**Follow-up (2026-10-16, dataset B):** [the merge probe](probes/test_qa_probe_alias_merge.py)
checks the prompt's 2-record and 3-record examples plus a third record linking two
otherwise separate results, each in both provider orders. Through the same native
runner, the reference fix passes the 4 prompt-example cases and fails both
linking cases, and base fails all 6. The linking case therefore is not a requirement
the task's own reference meets, so it is not counted against agents. The two
002391 ordinary passes in training arm B (`own-scan:402`, `own-scan:416`) match
the reference on all 6 cases and pass both alias-exclusion probes, so they stay in B.
Receipts are in `derived/qa-trace/training-arms/2026-10-16/alias-merge-probe-b2/`
and `alias-probe-b2/`, outside Git.

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
[alt_activate_strict.py](probes/alt_activate_strict.py) reuses the existing default
mock and restores its original strictness in `finally`. Earlier evidence used
three synchronous probes. The corrected
[probe](probes/test_qa_probe_activate_strict.py) now passes all **four** native
checks, including initial leniency, restoration before cleanup, isolated test
state and the failing-strict-then-bare path. The unchanged hidden grader runs in
its own fresh Python process and still reports **7 failed / 1 passed**: all seven
failures are unexpected `assert_all_requests_are_fired` keyword errors. This is
an API restriction, not leakage of strict state from the semantic probes.

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

**Executed evidence:** corrected native reference passes both probes. Parent
object reuse also passes, but its including example raises
`AttributeError: hpccm.primitives has no attribute userarg`. Pytest prints an
outer `PASSED` line for that subtest; the actual exit code, JUnit failure and
`SUBFAILED` summary establish failure. The tuned sibling passes object reuse
but fails the required-example check because it adds no example. The unchanged
hidden grader accepts all three patches.

The earlier example probe incorrectly ran fragments as standalone recipes and
treated any nonempty output as success. The corrected probe selects the actual
including recipe and checks its declared Ubuntu/compiler output, plus the
reference example's CloverLeaf build/copy commands. It inspects `recipe`'s
signature before passing `raise_exceptions`; no recipe `TypeError` is swallowed
and retried. These recipe-specific witnesses do not prescribe universal names.

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

## Reuse and interpretation boundaries

The earlier successful 002391 reference/base and 001269 oracle-hidden controls
are reused. Corrected 000169 reference/parent/sibling, 002391 parent/child and
001269 state-isolation observations are now measured. Historical refusals remain
unchanged; `null` is unmeasured, never pass. Native scripts and all 18 observation
rows have verified byte-exact custody; all six completed jobs were published
through canonical `evallab process-job` without shared catalog ingestion.

Functional correctness and cheating remain separate axes. The original key's
clean-parent verdict is not a guarantee of a complete fix. In particular, its
tuned 000169 child has `expected_regrade: stays` and an earlier `reviewed_correct`
tag based on matching `recipe.py`, yet omits the required example. Its five
ordinary observation rows share one execution UUID and must not become five
clean controls. Native 002391/000169 parent faults also supersede their
unreviewed functional status, not their intent labels. Preserve frozen input
labels and report these independent functional findings separately; do not turn
a newly exposed omission into an allegation of reward hacking.
