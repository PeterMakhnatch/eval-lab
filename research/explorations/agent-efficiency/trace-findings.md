# Bounded OMP work-log audit: avoidable tool round trips

## Finding and limits

The strongest observed targets are **tool routing**, **edit snapshot/range transitions**, and **resolved worktree context**. Across 12 pinned traces, the source records contain **786 model requests, 1,070 tool calls, 92 `isError` tool results, and 23 rejected edits out of 128 edit calls**. There are **37 shell-policy rejections in 8 traces**, **17 unseen-snapshot-range edit rejections in 4 traces**, and **6 failed operations using the wrong absolute home prefix in 4 traces**. These counts overlap; do not add them as independent sessions or savings.

These are observed operations, **not estimated saved tokens**, a population failure rate, or a measured improvement. This is an exploratory, single-reader process audit, not a blind two-rater experiment comparison. The six controls have zero recorded **edit** errors, not proven successful artifacts: they still have 46 tool-error flags, exactly as many as the six edit-error cases. No result establishes final code quality or a model/arm ranking.

**Coverage is 14 audited traces:** the primary paired cohort of 12 supplies all counts above. A separately pinned supplement includes **both zero-tool-error sessions** among the original 46 bounded candidates, adding 64 requests, 135 tool calls, and 5 accepted edits without changing the original labels or denominators. The supplement is below; it supplies actual error-free process counterexamples rather than calling zero-edit-error controls universally successful.

Read-only SQL and an in-memory JSONL walk were used. No source analyzer, model judge, upload, API call, container, run, service/configuration change, or communication with the logged agents was performed for this audit. No build, lint, test, formatter, or smoke check was run. Historical commands below were **observed in old logs**, not executed again.

## Cohort pinned before qualitative reading

Selection window: **October 9, 2026 12:00 AM ET through October 16, 2026 12:00 AM ET, exclusive** (`2026-10-09T04:00:00Z <= timestamp < 2026-10-16T04:00:00Z`). Primary-cohort activity runs from **October 9 1:02:48 AM ET to October 15 8:34:11 PM ET**; the zero-error supplement extends observed activity to **October 15 11:41:06 PM ET**. All 14 terminate with a `yield` tool result or an assistant `stop`; no sampled trace continues beyond the window.

1. Query `~/.omp/stats.db` for distinct subagent `messages.session_file` active in that window: **463 files**. The recent folder is `/private-tmp/`, not an Eval Lab-named folder. Filtering only `folder LIKE '%eval-lab%'` would have selected none: that folder's subagent metadata ends September 2.
2. Inspect only each candidate's header and `session_init.task` initially. Require header start within the window, case-insensitive `eval[- ]lab|evallab` in the assigned task, and `session_rollup.ended_at` before the exclusive end: **239 topic matches**. One candidate started outside the window; 223 lacked the topic match. Task text was not exported.
3. Bound trace size and activity using metadata, before reading behavior: agent kind `task`, `fxtr-builder`, or `trace-analyst`; **10–120 recorded requests inclusive; at least one edit call**. This leaves **46 candidates**: 14 on October 9 ET, 5 on October 10, 6 on October 12, 4 on October 13, 7 on October 14, and 10 on October 15. No October 11 starter meets these bounds. This deliberately excludes long workers, short scouts, and review-only work; it does not describe all Eval Lab labor.
4. Take the **chronologically first edit-error candidate per eligible ET day**. Match a zero-edit-error control by nearest request count, then edit-call count, then start/path. Allocate all possible same-day controls first. October 12 and 13 have no eligible same-day control, so choose unused controls by calendar-day distance, then the same size tie-breaks. This gives six cases and six controls, without looking at qualitative outcomes. T05 is paired with T06 (October 14); T07 with T08 (October 15). The other four pairs are same-day. Cross-day and task-type differences prevent a causal case/control interpretation.

Topic scope includes direct Eval Lab implementation/verification and supporting research/fxtr calibration. In particular, T01 writes research-context material; T04 works in fxtr projects. Neither is presented as an Eval Lab source-code task.

`evallab features`/`process-job` were not invoked: these inputs are OMP session JSONL, not Harbor job directories. The required trace-reading skills and Trace Lab QUIRKS were read; no old Harbor-derived detector interpretations were reused. No raw Harbor evidence was changed.

### Local provenance

Scratch contains only metadata, short tool-error quotes, counts, and coordinates; **no copied full transcripts or prompts**:

- `/tmp/agent-efficiency/trace-audit/cohort.json` — SHA-256 `87936208bf78dbace0aa4026ef6b7f298b37ff2bd8b89dd6b3c6a3fb9bc1a9f2`.
- `/tmp/agent-efficiency/trace-audit/labels.json` — SHA-256 `7007c7ff1399be76d4df5cb0311723cff59242ed87946b3729992fdfd6878c23`. Single-reader labels, not independent rater ground truth; no subsequent correction of these labels.
- `/tmp/agent-efficiency/trace-audit/metrics.json` — SHA-256 `831945b357b8b7492eb971a87ae5e357353a753620236bc6f910a26153526a2e`.

Coordinates below use **`Txx:L<physical JSONL line>#<entry id>`**. Tool results have
unique entries even when one assistant entry contains several calls. `Txx` resolves
through the local, private cohort manifest above. Session UUIDs, worker names,
absolute transcript paths, parent-session mappings, and raw prompts are deliberately
not published in this public repository. Full source-byte hashes below allow the
local evidence owner to check the mapping without exporting those identifiers.

| ID | Stratum: edit error / zero-edit-error control | Requests | Tool calls / error flags | Edit calls / rejections |
| --- | --- | ---: | ---: | ---: |
| T01 | E | 19 | 35 / 1 | 4 / 1 |
| T02 | C | 36 | 44 / 7 | 7 / 0 |
| T03 | E | 111 | 153 / 13 | 12 / 1 |
| T04 | C | 107 | 136 / 10 | 6 / 0 |
| T05 | E | 37 | 55 / 5 | 12 / 2 |
| T06 | C | 41 | 58 / 1 | 10 / 0 |
| T07 | E | 93 | 114 / 12 | 32 / 8 |
| T08 | C | 100 | 131 / 7 | 2 / 0 |
| T09 | E | 71 | 84 / 10 | 35 / 10 |
| T10 | C | 85 | 105 / 16 | 2 / 0 |
| T11 | E | 42 | 89 / 5 | 3 / 1 |
| T12 | C | 44 | 66 / 5 | 3 / 0 |
| Total | 6 E / 6 C | 786 | 1,070 / 92 | 128 / 23 |

### Source-byte digests

These hash the complete source JSONL bytes at selection, not extracted text. Keep source logs private; the identifiers and digests suffice to locate and compare evidence.

| ID | SHA-256 |
| --- | --- |
| T01 | `5eec1595b729bf934005d56bde61a23b575a6fe46ef4ac16bd18be632b4f52c8` |
| T02 | `8aa10f3f1845f2ee920e60c49ccfa021640add12da8c5385e30d40eefaf113f3` |
| T03 | `6ade1cc06d3ba75d6cc9c1bd78fb7266634c502f9bac833b8cc1e4c28220577c` |
| T04 | `3b5d4193bc78aee90f03b5eab60712348754bdd82faf94e3c71910bea5006ebd` |
| T05 | `357913d95a779105e9fe1112a567a12f8090eae920a06da04e199eddff8122d0` |
| T06 | `a782604d57f919ab81730819f2dc7a98301735f97524b32ba88dab4b4cb6f013` |
| T07 | `3a13660145d64e7d280577f05022cf4c92dbc66816970282bac4acf3755b2ce5` |
| T08 | `01f3b7d8ffabc3559270ef5f5e9b21cbb85900fc0e254207f668445c2c750c25` |
| T09 | `4509b3578a980408a785720fdbb7fa3abb348959fd4cdd86ac18dc77ac29d975` |
| T10 | `ce0c5a0ab1ca66ce352c5b16b8362559faeb85fb9d76be1f6da907b3016b6ce2` |
| T11 | `ce44d706045c6e82f1c85fdff6d16ffa7b14507fe4748633925f652e93bbfba7` |
| T12 | `062904d58359141bca975dcbabd0f1e77fe0166ffa1dcb1c420f63f7c04bd8ac` |

## First avoidable-failure labels

These are **reader judgments about the first demonstrated avoidable operation**, not detector output or the cause of the final artifact's outcome. Necessary discovery and real environment failures were not automatically labeled waste. Eleven traces have a demonstrated avoidable operation; for T06 none was established.

| ID | Request → result coordinate | Label and minimal verbatim evidence |
| --- | --- | --- |
| T01 | `L82#89f3afb5` → `L84#ca583a93` | Block-selector misuse: “`PUT 15*:` resolved a single-line block”. The corrective edit is followed by an explicit “Restoring accidentally replaced INDEX row” call at `L88#d88dc10e`; success status alone does not prove that an edit preserved unrelated content. |
| T02 | `L8#b4c7ebe8` → `L12#262e2226` | Unresolved root: an `AGENTS.md` lookup under an assumed home prefix fails. Two other calls in the batch use the same wrong root. This does not establish whether the root originated in assignment context or the worker. |
| T03 | `L15#1d19f8ba` → `L20#ebe78037` | Guarded `tail` transcript extraction: “Blocked: Use the `read` tool instead of cat/head/tail.” The required investigation of verifier/network behavior is not itself avoidable. |
| T04 | `L54#47e329af` → `L56#26be2777` | First guarded file-extraction command: “Blocked: Use the `read` tool instead of cat/head/tail.” Earlier targeted implementation reads were necessary research. |
| T05 | `L26#f15f68a0` → `L29#5b869442` | Compound filesystem/read command rejected: “Blocked: Use the `read` tool instead of cat/head/tail.” Model identity/revision and serving-compatibility research was explicitly required. |
| T06 | No avoidable first failure established | `L153#c76c131f` → `L155#2a00abf3`: “assistant hit its output token limit (stop_reason: length) before the arguments could complete”. The recorded `read` arguments are empty. Attribute this to truncation, not an invented bad research choice. |
| T07 | `L70#85bbb034` → `L72#cf563514` | Guarded discovery: “Blocked: Use the `glob` tool instead of find/fd.” Later edit problems are not backdated to the task's required API investigation. |
| T08 | `L12#98daabc8` → `L15#e1a348bc` | Unresolved root: an `AGENTS.md` lookup under an assumed home prefix fails. The worker then checks `pwd`/`$HOME` at `L16#2ad51a94` and succeeds with the actual root at `L19#cb7947bf`. |
| T09 | `L73#4ae75a60` → `L75#bc766331` | Snapshot/range transition: “This edit anchors to lines 372” that snapshot `#3476` “never displayed”. A fresh targeted read at `L76#81e989d7` precedes the accepted edit at `L79#758a35b1`, now targeting line 373. |
| T10 | `L8#bd40fa3a` → `L13#e99fa60a` | Second call in the initial batch uses `cat`/`head`: “Blocked: Use the `read` tool instead of cat/head/tail.” The first Docker-inventory call is separate and is not labeled this failure. |
| T11 | `L216#320a2545` → `L218#19c9107a` | Out-of-bounds edit: “Line 49 does not exist (file has 46 lines)”. Earlier absence of new scratch directories and the canonical patch-application blocker do not establish avoidable work. |
| T12 | `L11#ec6d31d0` → `L13#e88fed61` | Unresolved command cwd: a nonexistent worktree under an assumed home prefix. The worktree was created using `~` in the preceding call. |

## Quantified patterns and counterexamples

### 1. Repeated shell-policy feedback did not reliably change routing

**37 rejected calls / 8 traces**. Recommended replacement tools in the rejection text: **read 19, grep 12, glob 3, write 2, edit 1**. Of these, **29 occur after an earlier shell-policy rejection in the same trace**; **20 repeat a recommendation already given in that trace** (12 read, 8 grep). These are actual rejected operations, not a token counterfactual.

Causal examples: T03's first `tail` rejection above is followed by another read-routing rejection at `L24#c95eb679` → `L28#ecb7d3e2`, and later `grep` rejections at `L331#83c21f06` → `L333#700a1f1f` and `L348#cc915339` → `L350#413b0b32`. In T10, the initial read-routing rejection is followed by two more at `L19#4f18d672` → `L22#ec12a761` / `L23#f7167d86`. The visible common feedback is “Blocked: Use the `read` tool instead of cat/head/tail.”

Counterexample: T03 immediately switches rejected `find` at `L34#77351096` → `L36#68f9a6b8` to `glob` at `L37#38505cc6`. Routing recovery is possible; it is not evidence the entire investigation was a loop.

### 2. Edit friction is mostly range visibility, not proven stale hash tags

The **23 edit rejections / 6 traces** partition as follows:

| Literal rejection class | Calls | Traces |
| --- | ---: | ---: |
| Anchor range “never displayed” in the supplied snapshot | 17 | 4: T03, T05, T07, T09 |
| “No hashline sections found in input” | 2 | 2: T05, T07 |
| Range/body mismatch (“ends by restating…”) | 2 | 2: T07, T09 |
| `*` applied to a single-line block | 1 | 1: T01 |
| Line outside file length | 1 | 1: T11 |

There are **zero explicit stale-tag mismatch errors** in this sample. Do not rename all 23 rejections “stale hashes.” T09's concrete mechanism is more precise: `L67#c95fb739` reads lines 335–410 with `#84DB`; `L70#bbd8239f` changes lines 341–343 and returns `#3476`; the next edit uses that **new** tag but an unseen, now-shifted line 372. After the read, it edits 373 successfully. The source body had been researched; the snapshot transition invalidated the convenient anchor context.

The two no-section results require care. T05 `L94#e8cdad5f` contains only a snapshot header and no operation. T07 `L98#2fae4137` has an input recorded as `[REDACTED]`; the result proves rejection, **not** which original patch syntax was intended. The scratch metric key `edit_wrong_format` names these two literal error responses; it is not causal attribution for the redacted request.

Counterexample to replacing the guard with permissive auto-apply: T01 explicitly performs a restoration after its accepted corrective edit. Any optimization must preserve untouched rows and reject unsupported locations, not merely turn red results green.

### 3. Resolved-root failures and context overlap are different phenomena

**6 failed operations / 4 traces** assume a home prefix instead of using the resolved root: T02's three initial calls, T08's first read, T10's `L14#9e22e3ae` → `L18#cfc4eb27`, and T12's cwd failure. The first-failure table shows direct recovery in T08. **[INFERENCE]** Binding an absolute worktree root and tool cwd in the handoff would prevent this particular class; the logs do not prove that a particular prompt omission caused every wrong prefix.

A separate parent/worker overlap probe was limited to each parent's **last 20 read/glob/grep calls within the 30 minutes before that worker started**: 151 metadata calls, 87 matching parent assistant entries retained in memory. Four workers reopen files read by their parent in this window: **T06, T07, T09, T11; 25 distinct worker read calls**. This is file overlap, not identical-range or identical-content repetition, and **not a count of redundant discovery**. Examples:

- T06's parent `L475#5eac7a91` reads `src/evallab/qa_task.py`; worker `L17#e83eefee` reads the same file. Its assignment explicitly says “Read current module”.
- T07's parent `L1961#7e8ece73` reads `task_variants.py`; worker `L16#b51db61c` reads it. The worker was explicitly assigned to investigate the actual task-variant APIs.
- T11's parent `L3156#74f1848c` reads `run_probes.py`; the worker reads bounded portions at `L9#fcedd23e` and `L17#bd279c1c` for a specific retained-run investigation.

Parent paths are each trace's header `parentSession` (also in `cohort.json`); exact parent-line digests are in `metrics.json`. The bounded lookback cannot establish the absence of duplication elsewhere, and source reading required for safe edits must not be removed merely because a parent once opened that file.

### 4. No demonstrated identical-result reread loop or redundant verification

Literal repeated successful `read` targets occur **4 times, all in T06**. Every one has intervening edits to that file and changed output. For example, `docs/qa-task.md` is read at `L17#e83eefee`, edited at `L73#f1f56159`, `L110#24782e9e`, and `L123#93fddc2b`, then reread at `L134#686bf16b`. Across all 12 traces there are **zero repeated literal read-target + identical-full-result pairs**. This deliberately narrow observable does not claim that all overlapping ranges were useful.

T01's `L26#de58af40` → `L31#0ec14060` says “[Showing lines 1-300 of 375. Use :301 to continue]”; reading `:300-375` at `L33#000ba05b` retrieves the omitted tail, not the same range again.

No redundant verification is demonstrated in this bounded audit. T02 was explicitly assigned “ruff+ty+pytest green.” Its checks at `L97#eba72b93` and `L103#9b66e638` find real `F821` undefined `re` / `json`, followed by edits; `L112#be9facdf` → `L114#ebdebddf` shows “8 passed”. They are not three interchangeable reruns. T08 was explicitly assigned oracle/nop controls and named tests. Conversely, T05, T06, and T09 explicitly delegate verification to the parent or say not to run tests, and their tool calls contain no test/build/lint execution. There is no support here for weakening verification globally.

### 5. Error flags are not semantic failure or waste labels

The 92 flags partition into: 37 shell guards, 23 edit rejections, 22 missing-path errors, 2 genuine static-check failures, 1 write-only-URI misuse, 1 browser API-shape error, 1 output-limit-truncated call, 1 queued-completion skip, and 4 other nonzero executions. Missing output directories can be expected probes; a final `diff` reporting a changed Docker-image listing in T10 (`L292#d432f90e` → `L294#0b9e790f`) legitimately returns 1. A missing `yaml` in T08's eval kernel (`L94#5ac41e56` → `L96#98cbbe13`: “ModuleNotFoundError: No module named 'yaml'”) is an environment dependency issue, not evidence that the task's verification was unnecessary.

The inverse also occurs: T02's `L115#0a8b3792` → `L117#3f8b8a2a` contains “error: Failed to spawn: `ty`” while `isError` is false, because the shell pipeline masks the upstream exit status. Thus 92 is a count of recorded tool flags, **not every failed operation**.

A quality-preserving environment counterexample is T11. Its retained canonical oracle log, read at `L147#45e8efa1` → `L149#41b0652d`, says “error: .venv/bin/Activate.ps1: already exists in working directory” (and corresponding activate paths). The final response at `L230#7b3acdd5` is “Final: refused/unmeasured; functional result `null`.” Do not turn that unexercised probe into a task failure, a pass, or an efficiency opportunity by bypassing the retained-diff boundary.

## Supplement: all zero-tool-error candidates, not selected successes

After the primary audit showed that zero-edit-error controls still contain tool errors, the original 46-candidate universe was queried for `SUM(tool_calls.is_error) = 0`. **Exactly two candidates qualify, and both were included.** Their metadata and source digests were pinned before reading their behavior. This is an explicitly separate, outcome-flag-selected counterexample set, not an adjustment to the frozen primary sample or its error rate.

- `/tmp/agent-efficiency/trace-audit/zero-error-cohort.json` — SHA-256 `c28360731833f7800e6ba093c4e817e028d46b752cfaeacefc9015d5d4eee8f4`.
- `/tmp/agent-efficiency/trace-audit/zero-error-findings.json` — bounded process observations; no copied prompts or full transcripts.

Private source mappings are retained only in the supplement manifest.

| ID | Activity ET, October 15 | Requests / tools / edits / tool errors |
| --- | --- | --- |
| N01 | 12:54:03–13:24:42 | 46 / 92 / 1 / 0 |
| N02 | 23:35:35–23:41:06 | 18 / 43 / 4 / 0 |

| ID | Complete source JSONL SHA-256 |
| --- | --- |
| N01 | `621cf51c9855778b47e9533cb90a0b26cd5131cda54e479a642416479ca646c5` |
| N02 | `c8ef777c379fdac2298a3b51b218c914ff0ff9d06c1bb604730143fb900a07e3` |

**N01: verification produces disconfirming evidence without tool-error churn.** The worker is assigned an exact before/after rules/content-rules refresh and instructed not to claim a rerun if roots are missing. It checks materialized roots, reads protocol/source, uses LSP, runs the named deterministic refresh, and inspects resulting comparisons. The retained comparison read at `N01:L200#149129cb` → `L205#2ecac401` includes `"semantic_changed_rows": 1` for refreshed rules and `0` for content rules. Its final yield at `N01:L241#85a0f9c7` says `"status": "COMPLETE — PR rules no-change claim contradicted"`. This is an observed useful challenge to an expected no-change claim, not a new independent audit of that detector result. No first avoidable failure was established.

**N02: a bounded ownership/no-verification contract can produce a clean handoff.** The assignment explicitly owns the billing module, its tests, and one schema section, while prohibiting tests/build/lint/formatters and paid calls. The worker reads instructions, invokes LSP, inspects SDK help/source using two local shell calls, and performs four accepted edits: `N02:L90#fd1771ab` → `L92#02a6b45d`, `L93#c9421465` → `L95#6df3bbea`, `L96#7ddbefef` → `L98#7d2f9b3e`, and `L108#2cc0d5cd` → `L110#7b757240`. After editing tests it reads only relevant changed ranges (`L99#81b32283`). The final `yield` at `N02:L111#95f61cd9` supplies the changed paths and data contract. There is no test/build/lint execution in its calls and no demonstrated first avoidable failure. This establishes process compliance, **not that its unexecuted tests pass or its implementation is correct**.

## Fix priorities and candidate local replay/quality checks

These are **engineering hypotheses**, not implemented or measured improvements. Runtime/tool-owner changes remain outside this assignment.

1. **Tool-native routing before repeated policy rejections.** Strongest breadth: 37 rejected calls in 8 traces, including 20 repeat recommendations. Give the model a compact canonical mapping and keep recovery hints structured; split compound observation commands into the already-available read/grep/glob tools. Do not auto-execute arbitrary shell strings or suppress the guard. Candidate free replay: freeze the 37 recorded rejected calls and their replacement-tool expectations; exercise deterministic routing/hints on observation-only fixtures. Check exact path/range/quoting semantics and that writes, secrets, remote execution, and unsupported pipelines remain blocked. Count changed rejection/recovery cycles; do not manufacture a token reduction from characters.
2. **Snapshot-aware patch preparation and focused refresh.** Strongest concentrated chain: T09's 10 and T07's 8 edit rejections, with 17 unseen-range results across four traces. Expose the currently editable ranges and a precise next-read target when an edit changes tag/line positions; prefer complete related hunks prepared from one visible snapshot. Keep stale/unseen/range-body guards intact. Candidate free replay: retain all 23 rejection fixtures plus the 105 accepted edit-call records; test that fresh range guidance points to the necessary lines and that genuinely unsupported edits still fail. The T09 372→373 case and T01 unrelated-row restoration are mandatory quality fixtures. Replay against captured snapshot context, not today's changing files; if a complete base cannot be reconstructed, limit conclusions to guard/range correctness, not full artifact equivalence.
3. **Absolute-root, ownership, and verification handoff contract.** Direct target: 6 wrong-home operations in 4 traces; context overlap offers a possible navigation improvement but is not counted as waste. Bind the resolved worktree root and cwd once, include a few source/step pointers rather than copied parent prose, and preserve explicit allowed paths and who runs checks. Candidate free checks: render assignments under a temporary HOME differing from the human's name, assert all repo tools resolve to the intended worktree, and preserve T05/T06/T09's parent-owned verification instructions versus T02/T08/T12's requested checks. Reject paths outside ownership rather than silently fixing them. Only a subsequent authorized task-quality/efficiency comparison could establish that this contract causes better work per token.

## Delivery status

Only this evidence note is a repository change. Raw logs and published results remain untouched. The metadata/source count reconciliation and source-byte hashing above were performed as the requested deterministic analysis. No experimental replay, source fix, quality test, paid operation, commit, push, or CI run was performed by this worker. Parent integration owns review of the note, repository checks, PR/merge, and any follow-on implementation/measurement.
