# probe-03-capabilities (HAR-91)

$0 capability tagging for Harbor trials. Reuses probe-02 code by import
(`sys.path` to `../probe-02-mimo-kit`: trial discovery, reward/exception
reading, label columns) rather than forking it. Never modifies probe-02.

## Commands

```sh
cd probe-03-capabilities
uv run --no-project --with pyarrow python capabilities.py <job_dir>... \
  --out-dir <dir> [--evallab-src PATH] \
  [--treatment-key-source auto|har93:<trial_treatment.parquet>] \
  [--nop-runs-dir PATH] [--commit-equivalent SHA[,SHA...]] \
  [--equivalence-source STR] [--pass-tainted JOB... --taint-source STR] \
  [--hand-key PATH...] [--wedge-key PATH] [--sheet-picks FILE]
```

- `--nop-runs-dir`: tree holding nop/qual control trials for the R-ENV-02
  nop cross-check (matched by task_name; without it
  `nop_control_confirms: "unknown"`).
- `--commit-equivalent SHA...` + `--equivalence-source STR`: declare
  eval-lab commits whose treatment keys count as ONE treatment for
  learnability, with the source of that decision. Merged only when the
  keys differ in nothing but the commit (see Treatment keys); the merge
  and its reason are recorded per cluster.
- `--pass-tainted JOB...` + `--taint-source STR`: job-dir names whose
  scored pass a coordinator declared tainted. The row keeps reward 1.0
  (history is not rewritten) and gains `pass_tainted: {source}`; the
  trial leaves learnability attempts. An unknown job or a non-pass is an
  error. HAR-81: 2684, per Research-Harbor 10:32Z (forbidden network
  `pip install` of stevedore at head#33).
- `--hand-key PATH...`: parent-owned hand answer key(s) (JSONL, read-only;
  never tunes rules) — join on job-dir basename for the "Validation vs
  hand key" summary section plus `validation.json` (agreement counts on
  tag/attribution/stop/rule + disagreements). With several files, each
  is scored as its own set (`validation.json` `sets`: in-sample vs
  held-out) and then combined; a job keyed twice warns and the later
  file wins. Results: see Validation.
- `--wedge-key PATH`: blind WEDGE hand key (JSONL: `job`, `stretches[]`
  with start/end/turns/cause/trigger_ref/interrupt_ref/prompt_returned/
  until_run_end; read-only) — scores the wedged-terminal measurement
  into `wedge_validation.json` and a line in the WEDGE summary section.

`<job_dir>` is a trial dir, a job dir, or a folder of jobs (probe-02
discovery; shell globs accepted). Trials without `result.json` are
skipped and listed as still running.

```sh
E=~/Developer/eval-lab/.worktrees
# Pin the current pair; the capture table is its immutable sibling.
P=$(uv run --project ~/Developer/eval-lab python -c '
from pathlib import Path
from evallab.storage.paths import derived_root_from_environment
from evallab.trial_treatment import table_paths
root = Path.home() / "Developer/eval-lab"
catalog = derived_root_from_environment(root) / "external/task_catalog"
print(table_paths(catalog)["trial_treatment.parquet"])
')
uv run --no-project --with pyarrow python capabilities.py \
  $E/har90-modal-mimo/runs/har90-mimo-{0036,0036-b,0036-c,0036-d,0036-e,0036-f,0758-a,0758-b,0758-c,0758-d} \
  --out-dir har90 --treatment-key-source har93:$P \
  --wedge-key validation/har99-wedge.hand-key.jsonl
uv run --no-project --with pyarrow python capabilities.py \
  "$E/har81-dispatch-528/runs/har81-p-d-*" "$E/har81-dispatch-531/runs/har81-*" \
  --out-dir har81 --treatment-key-source har93:$P \
  --evallab-src $E/har81-dispatch-531/src --nop-runs-dir $E/mimo-ops/runs \
  --commit-equivalent 7de1ce6e,48b787b5 \
  --equivalence-source 'HAR-81 Research-Harbor decision 09:27Z' \
  --pass-tainted har81-p-d-candidate-2684-security-appsec \
  --taint-source 'HAR-81 Research-Harbor decision 10:32Z (forbidden network pip install of stevedore at head#33)' \
  --hand-key validation/har81-wave-a.hand-key.jsonl \
    validation/har81-holdout.hand-key.jsonl \
    validation/har81-holdout2.hand-key.jsonl \
    validation/har81-late.hand-key.jsonl \
  --wedge-key validation/har99-wedge.hand-key.jsonl \
  --sheet-picks for-peter/picks.txt
```

(`har81-wave-a` is the same command over the dispatch-528 glob only,
with `--evallab-src $E/har81-dispatch-528/src`, the in-sample key
`validation/har81-wave-a.hand-key.jsonl`, the same `--pass-tainted` /
`--taint-source` (2684 is a wave-A trial) and `--wedge-key`, and without
`--sheet-picks`: the picks include dispatch-531 jobs, and an unknown pick
is an error.)

`--with pyarrow` is needed for the HAR-93 parquet joins (treatment key +
capture coverage). Outputs in `--out-dir`: `capabilities.jsonl` (one row
per trial), `summary.md` (one page), `reading_sheet.md` (~20 traces
balanced across outcome tag x attribution: round-robin over strata,
distinct tasks first; or exactly the `--sheet-picks` list, an unknown
pick being an error), with probe-02's label columns exactly plus proposed
tag/attribution/evidence), `validation.json` (only with `--hand-key`:
agreement counts + disagreements) and `wedge_validation.json` (only with
`--wedge-key`).

## Five dimensions (never collapsed)

Per trial: **verifier outcome** (`scored`, `reward`; a verifier reward
after `AgentTimeoutError` is still scored), **stop reason**
(`exception_info` / agent metadata / summarization markers / natural
`n_episodes`, token-attributed share), **first failure** (`step_id`, tag,
attribution, rule id, `recovered` = a later accepted turn exists), and
**outcome_relevant_failure** (the failure that explains the verifier
outcome / stop). Anything absent is null/`unknown`, never 0.
`first_failure` is null (not R-UNC-01) when the execution is mechanically
clean and the outcome explains the trial (contract, planning, false-claim
and pass trials) — loops belong to the outcome, not the first failure.

## Historical-truth rule

Replaying old messages through today's normalizer recovers what the model
PROPOSED. It is never what the harness accepted/executed at the time.
Acceptance/execution is inferred only from that step's recorded
observation, otherwise `unknown`.

## Harness stand-ins (STANDIN)

An agent-source message exactly `Technical difficulties. Please continue
with the task.` is Harbor's stand-in reply, not model output (Infra
HAR-81 audit). Stand-ins are classified `harness_standin` (source
harness), excluded from model-behavior counts (accepted/rejected counts,
rejection causes, repeated-message loops), reported per document as
`reconstruction.harness_standins` (`count`/`first`/`last`), and cited as
the overflow/end-of-run pattern (0758-c cont-31 #184-213: 30; 0758-b
cont-11 #761: 1). Coverage keeps both counts: `agent_turns_read` (all
agent-source steps) and `model_turns_excluding_standins` (what the rules
run on).

## Trajectory assembly (ASM-*)

Steps are assembled as head + each `trajectory.cont-N.json` in numeric
order, dropping any leading run of a cont file identical
(source+message) to the already-assembled sequence (positional
prefix-drop). Never concatenated blindly, never summed.

- ASM-DUP `duplicate`: cont adds nothing (0036-f: cont-1 == head).
- ASM-SUPER `cumulative_superset`: cont extends head (0758-c: head
  prefix + new steps = 213).
- ASM-NEW `new_session`: cont has a different `session_id` (0758-d: only
  the system prompt is shared; 56 head + 178 cont agent steps = 234).
- ASM-HEADLESS `head_missing`: no head FILE, but a lone cumulative cont
  may still cover all episodes (0758-b: cont-11 starts at the system
  prompt, 761/762 episodes; noted `episodes covered N via cumulative
  <doc>` — the head file is missing, the episodes are not).
- `single_head`: no cont files.

ASM-MISSING-RECON: when HAR-93's capture table lists continuations as
`missing` (0758-c 1-30, 0758-b 1-10), the coverage note quotes Data's
table verbatim and reconciles: the ledger's cont-N numbering counts
split attempts, so missing numbers need not correspond to
`trajectory.cont-N.json` files on disk — see `coverage.cont_files`
for which episodes were actually read.

## Tokens (TOK-*)

- TOK-LAST: totals come from the LAST document's `final_metrics`
  (cumulative) compared against `result.json` input/output totals.
- TOK-LOOP: per-step `metrics` sums are used ONLY to cost a loop or a
  completion-claim regime (e.g. 0036-e steps 35-84: 1,865,875 prompt +
  23,450 completion), never for trial totals.
- TOK-NEVER-SUM: documents are never added together (breaks ASM-NEW,
  where steps are new but tokens are cumulative).
- TOK-SHARE: capture coverage shows BOTH "episodes by count" and the
  "token-attributed share" (`trajectory_input_tokens /
  proxy_input_tokens` from HAR-93). Step count matching `n_episodes` is
  NOT full coverage when tokens are unattributed (0758-c: 21.1% share,
  20,637,372 unattributed input tokens; 0758-b: 86.0%, 3,646,823).

## Observation rules (H-ACC-*)

- H-ACC-TERM-TRUE: observation carries terminal output (`New Terminal
  Output:` / `Current Terminal Screen:`) => step was NOT rejected.
  `true` means "not rejected"; keystrokes were sent only if the turn
  carried calls (a call-less prose turn with terminal echo is usually
  stale drain from an earlier turn).
- H-ACC-PARSEERR-FALSE: parse/validation error notice (`Previous response
  had parsing errors` / `ERROR:`) with no terminal echo => rejected
  before anything was sent.
- H-ACC-UNKNOWN: no observation, empty results, or another harness
  version's format.

## Shape rules (SHAPE-*)

- SHAPE-XML `native_xml`: has a `<function=...>` opener and the
  normalizer recovers >=1 call.
- SHAPE-XML-BROKEN `unparseable`: native surface but nothing recovers.
- SHAPE-JSON `terminus_json`: brace-led object that strict-parses or
  normalizes (includes the wrapper-tail shape).
- SHAPE-JSON-BROKEN `unparseable`: brace-led but neither parses.
- SHAPE-PROSE `prose`: natural language, no markup.

## Rejection causes (per rejected turn, deterministic)

`classify_rejection_cause`, parent-verified, order matters:

1. today's normalizer accepts the message => `today_normalizer_accepts`
2. no `<function=` => `json_invalid` if the message is `{`-led else
   `prose_no_call`
3. function not in {exec, exec_command, bash} =>
   `unknown_function:<name>`
4. `<parameter=X>` outside {command, keystrokes, duration} =>
   `extra_param:<X>`
5. `{`-led => `hybrid_json_in_markup`
6. else `other_native`

Plus `harness_standin` for stand-in messages (see STANDIN). Counts and
first step per cause, per document, are reported in
`reconstruction.rejection_causes`.

## Failure rules (first firing rule wins per failure object)

Each trial gets TWO failure objects: `first_failure` (earliest firing,
includes `recovered`) and `outcome_relevant_failure` (explains the
verifier outcome / stop). Priority reflects outcome-explanatoriness, not
chronology. Tags are HAR-91's set plus `context` and `unclear`.

- R-ENV-01 `environment`/`unclear`: no model turns (infra-only, e.g. 401
  `AuthenticationError` with `n_episodes` 1). Nothing to attribute
  (0036-c, 0758-a).
- R-TOOL-00 `tool_use`/`unclear`: accepted Terminus JSON whose commands
  actually CONCATENATED — gate A-CONCAT-EXEC requires the newline warning
  AND two proposed commands fused on one prompt-echo line in the
  observation (0036 head#2: `.../snakemakecat /app/workflow_probe.py`;
  0036-b head#2). The warning alone does not fire (0036-e/0036-f/0758-d
  early steps ran commands on separate prompt lines). Transient:
  `recovered=true` when a later accepted turn exists. As the OUTCOME it
  fires on an identical loop whose turns (>= half) each send one command
  WITHOUT a trailing newline and the echo shows that command fused onto
  its previous copy (`head -40grep -rn ...`), so nothing ever executes
  (0036 head#17-121, 105 turns; the model even writes "The commands are
  being concatenated"). Attribution stays `unclear` per A-CONCAT-UNCLEAR,
  so this beats R-REC-01 (model).
- R-TOOL-01 `tool_use`/`harness`: first rejected turn with cause
  `today_normalizer_accepts` — the output was valid in its native
  format and the harness rejected it before the normalizer existed
  (0036-b head#3 184x, 0036-d head#2 529x, 0758-b cont-11#2 760x, 0758-c
  cont-31#6 178x). Harness gap, since fixed by `mimo_tool_calls.py`
  (#512). A rejected turn is OUTCOME-relevant (any R-TOOL-0x) only as a
  rejection storm (>= 10 rejected turns) or when no accepted turn follows
  it; a lone recovered rejection is `first_failure` only (0036 head#7: 1
  rejection; 1048 head#39: 1 malformed `duration`).
- R-TOOL-02 `tool_use`/`model`: rejected turn with cause
  `unknown_function:*` (0036-f head#27 `<function=keystrokes>`,
  head#29+ `<function=write>`: 2 + 6 rejections, recovered=true) — the
  model emitted a surface the tool set does not have (A-NON-NATIVE).
- R-TOOL-03 `tool_use`/`harness`: rejected turn with cause
  `extra_param:*` where the SAME native signature was accepted >=10x in
  earlier documents and rejected >=10x in this document after a
  summarization/new-session boundary (0758-d: head 56/56 accepted, then
  cont-1#5 177/177 `extra_param:description`). Mechanism: the
  normalizer allowlist (`mimo_tool_calls.py:154`, args <=
  {command|keystrokes, duration}) rejects the extra `description` param
  while stock feedback only says "No valid JSON found", never naming
  the parameter, so the model could not self-correct. The 176 identical
  retries (cont-1#6-#181) are a SECONDARY `error_recovery`/`unclear`
  pattern (uninformative feedback), not the model's failure. Requires an
  engineering flag.
- R-COMP-01 `completion`/`harness`: trailing rejected-prose run whose
  cause is `prose_no_call` after a completion claim — Terminus has no
  prose completion path, so the final answer is re-prompted as a parse
  error until the timeout (0036-e head#35-84, 50 turns, loop cost
  1,865,875 prompt + 23,450 completion; scored 1.0 despite the
  `AgentTimeoutError`).
- R-COMP-02 `completion`/`model`: false completion claim on a limit stop
  with reward < 1.0. A claim, in every branch, is a CC-CLAIM message OR a
  turn the harness recorded as `task_complete` (a3-000240 head#35 "The
  implementation is complete"; a4-1634 head#21 "The repair is complete").
  Three branches: (a) trailing identical CC-CLAIM run — cites the FIRST
  claim (COMP-CLAIM-ONSET: the earliest claim step whose window to the
  loop end stays >=50% claims — 0036-f head#44 "The task is complete...",
  verifier 4/5 at that point) and then the identical loop (0036-f
  head#55-172, final variant #173); (b) trailing claim REGIME
  (alternating prose-completion/call confirmation cycle, never 10
  consecutive identical messages); (c) ANY claim plus a contradicting
  verifier (failing ctrf tests, or mocha-style numbered `N) ...` failures
  in test-stdout.txt — 000434 "The fix is complete" at head#31, then a
  verification loop to the ceiling while tests fail; a4-1634 head#21,
  then edits until the timeout, never confirmed). Grader notes ride along
  on every R-COMP-02 branch, including the harness-confirmed one
  (`source_text_assertion`: the failing verifier test asserts a literal
  source string — `read_text()` + `in source` (0036-f), or a failing
  `assert "<literal>" in <name>` where <name> was read from a file
  (all four candidate-1634 trials pin `def atomic(self,
  transaction_type=None, **kwargs):`) — plus whether that literal appears
  in instruction.md; grader audit flag, NOT R-ENV-02, never a tag or
  learnability change).
- R-COMP-03 `completion`/`model-or-unclear`: the submit-contract rule
  (HAR-81 cyber/arvo). Gate (a): harness-confirmed `task_complete` with
  reward 0 — contradicted by failing tests => `model` (1634: broke a
  pinned signature, 5/6 passing); deliverable-only mismatch =>
  `model`/`unclear` by contract engagement. Gate (b): reward 0 with
  contract ENGAGEMENT but no submission — the ceiling trials that
  explore without ever submitting (no claim required). Engagement =
  `contract_seen` (an observation carries the submit.sh header /
  "submit PoC"; `obs_content` joins ALL result parts), `attempted_repro`
  (executed keystrokes invoke `binary/run.sh` or a fuzz binary WITH a
  path-like input arg — reads/views like `cat run.sh` and flag-only
  `-help=1` runs do not count), `poc_reproduced` (sanitizer SUMMARY at
  or after the first attempt). Attribution: reproduced-and-not-submitted
  => `model`; attempted-but-not-reproduced => `model`; seen-only =>
  `model`; submitted-but-unscored => `unclear`. Gate (c): the
  submit-contract verifier (`verifier/result.json` `submitted: false`)
  scores 0 and the model NEVER engaged (no repro attempt, submit.sh never
  opened, no confirmed completion) => `unclear`, because the instruction
  never states the deliverable (57589: source reading plus a 61x
  identical grep to the ceiling). Submission (`submit.sh <arg>`
  execution) excludes reads/views (`cat submit.sh`). The verifier's
  named deliverable vs the instruction is checked separately
  (`deliverable_not_in_instruction` grader note, never a tag).
- R-COMP-04 `completion`/... (SECONDARY ONLY, never outcome): the
  confirmation_with_call loop — after the harness "Are you sure ...?"
  prompt the model answers claim-plus-tool-call, which the harness
  executes instead of confirming, repeating (000434 secondary
  head#32-104). `recorded` detector (prompt in an observation) or
  `inferred` (prompt text on disk).
- R-CTX-01 `context`/`harness`: summarization livelock. trial.log
  repeats >=2 "Context length exceeded. Using fallback summarization"
  cycles, each unwinding to the SAME state ("full summary failed ->
  short summary succeeded -> Even fallback chat failed"); metadata
  `summarization_count` confirms (0758-c: lines 192-437, 31 attempts;
  0758-b: lines 776-800, 11 attempts). Fires as the OUTCOME over any
  other rule except R-COMP-01 and R-ENV-*: a rejection loop inside the
  livelock (0758-b/c R-TOOL-01) stays `first_failure`, because steps
  after the first cycle were produced under degraded context and are
  NOT attributed to the model (including stand-ins). Research-Harbor
  ruling (HAR-91 05:11, verified 05:43), authoritative over "missing
  continuations" readings of the same logs. Requires an engineering flag
  (old config, max_tokens 8192).
- R-PLAN-01 `planning`/`model`: ceiling/timeout stop, not a pass, no
  R-ENV/R-COMP rule fired (by position: rejections return above), and
  ZERO executed file-changing ops on task paths — "no task edit before
  budget ran out" (1271, 001645, 002537). Writes-only accounting
  (`_task_targets`): redirect targets, `sed -i` files, and files a
  heredoc script writes (`open(p, 'w')` / `Path(p).write_text`, with
  `p = '...'` resolved in the same body: a4-1634 head#10; a reused name
  resolves to its latest assignment before the write, so a4-1789
  head#28's one `p` writes tester.py, issue.py and trojansource.py); read-only
  prints (`sed -n ...p FILE`) never count; a heredoc body's `>` is
  script content, not a shell redirection; /tmp, /dev/null, fd targets
  excluded. Identical loops
  stay secondary R-REC-01. The outcome tag records WHAT failed
  deterministically; the WHY (wrong localization, API break) lives in
  `mechanism`, hand-only. Evidence for this absence claim is the loop
  span when there is one, else the scanned span (first and last agent
  step, also in `scanned_step_refs`).
- R-REC-01 `error_recovery`/`model`: longest identical run (>=10) with
  no change of approach after errors; usually a SECONDARY pattern.
- R-ENV-02 `environment`/`harness`: suspect grader — the trial's OWN
  verifier fails at setup/collection/import rather than on model
  behavior, cross-checked against the same-task nop/qual control
  (`--nop-runs-dir`). Path (a): test-stdout.txt shows `ERROR at setup
  of ...` / `ERROR collecting` / missing-module errors. It fires even
  when other tests ran (1789: 4 of 7 ERROR at setup, 3 ran), because a
  correct fix would still fail those tests, so the trial carries no
  capability signal. Path (b): the verifier ran NO tests and only the
  anti-hack guard rejects, and the nop control fails on an import
  (1702: `anti_hack_guard: REJECT protected_file_mutated`, nop 6/6
  `ModuleNotFoundError: tqdm`). Never fires on the nop alone: when the
  trial's own verifier shows no such error, the outcome stays with the
  model (1634: nop fails on `collections.Callable`, the model fixed it,
  5/6 pass). Fires first AND as outcome. Fires nowhere on HAR-90.
  Path (b) also records `guard_mutation_steps`: the executed model steps
  whose keystrokes write the guarded file (path-suffix match, since the
  model edits relative to a `cd`). All three HAR-81 guard rejects were
  earned this way (1702 head#15/16/19 stubbing `tools/run_fixture.py`;
  a2-1789 head#22 hard-coding the version in `bandit/__init__.py`;
  a4-1789 head#28 changing the `Issue` constructor in
  `bandit/core/issue.py`). This is measurement only: the tag stays
  R-ENV-02 because the nop fails at import with no edit at all. It still
  means none of these trials is usable as a training example.
- R-TOOL-01U `tool_use`/`unclear`: rejected turn whose cause is not
  covered above (json_invalid, hybrid_json_in_markup, other_native) —
  attribution unclear. Defined; fires nowhere on HAR-90.
- R-UNC-01 `unclear`/`unclear`: nothing fired; needs hand reading. On a
  ceiling/timeout stop, the evidence ref is the progress pointer's
  `last_file_changing_step`.

## Attribution rules (A-*)

- A-NATIVE-HARNESS: rejected turn the normalizer recovers => `harness`
  (R-TOOL-01).
- A-PROSE-HARNESS: a prose final answer rejected as a parse error is a
  harness gap — Terminus has no prose completion path => `harness`
  (R-COMP-01).
- A-NON-NATIVE (was A-NON-NATIVE-MODEL): the model emitted a call
  surface the tool set/normalizer declines (`unknown_function:*`) =>
  `model` (R-TOOL-02).
- A-EXTRA-PARAM-SESSION: `extra_param:*` after a session boundary where
  the same signature was accepted before => harness-side (R-TOOL-03);
  the same cause WITHOUT the session evidence stays `model` (R-TOOL-02).
- A-STANDIN-HARNESS: the stand-in message is Harbor's reply, not model
  output => excluded from model attribution entirely (STANDIN).
- A-FALSE-CLAIM: the model claims completion the verifier contradicts
  while the harness executes faithfully => `model` (R-COMP-02).
- A-NO-ADAPT: identical repetition with no change after errors =>
  `model` (R-REC-01).
- A-CONCAT-UNCLEAR + A-CONCAT-EXEC: Terminus's JSON protocol requires
  trailing newlines and warns when missing, but sends keystrokes
  verbatim, so commands can concatenate and produce garbage. The trace
  alone cannot separate "model ignored the protocol + warnings" from
  "harness sent known-bad input verbatim" (the later Eval Lab fix chose
  to handle this harness-side via `executed_keystrokes`), so attribution
  is `unclear` — and the tag only fires when the concatenation actually
  EXECUTED (two proposed commands fused on one prompt-echo line), not on
  the warning alone (R-TOOL-00). Warning-only turns are recorded as
  `accepted_with_newline_warnings` counts and never fire a tag
  (0036-e/0036-f/0758-d early steps).
  output to judge).

## Completion-claim test (CC-CLAIM)

Claim-matching message: `/the (task|fix) is (verified and |confirmed
)?complete/i` (0036-f "The task is complete...", 000434 "The fix is
complete." / "The fix is verified and complete."). `task_complete`
acceptances recorded by the harness are tracked separately
(`completion_refs`); R-COMP-02 counts either as a claim (other wordings,
e.g. "The repair is complete", reach it through the harness record).

## Normalizer fallback (FB-*)
`--evallab-src` (default: the verified HAR-90 worktree `src`) provides
`normalize_mimo_tool_calls`; commit + sha256 of `mimo_tool_calls.py` are
recorded in every row. If the import fails, FB-XML-PARAM extracts
`<parameter=command|keystrokes>` calls via explicit regex, FB-DEFER
declines wrapped whole objects (needs the real normalizer), and the row
records `mode: fallback-explicit-rules`.

## Treatment keys

Priority, first present wins; `treatment.key_source` records which:

1. `infra-dispatch`: Infra's pinned dispatch key for the worktree the
   trial ran from, `<worktree>/derived/<lane>/treatment-key.json`
   (exactly one such file). HAR-81: `0d4b7a40` on 7de1ce6e
   (har81-dispatch-528, wave A) and `04b4e45c` on 48b787b5
   (har81-dispatch-531, the last 3 + wave B). `receipt pair` verified
   the key per job. This is the key Research-Harbor's equivalence ruling
   names, and it covers every trial of its worktree.
2. `har93`: `--treatment-key-source har93:<trial_treatment.parquet>`
   joins `setup_key` on `trial_name` (Data's HAR-93, #519 / 792d459d).
   HAR-93 covers HAR-90 and HAR-81 wave A only, and keys HAR-81 per
   domain, so it would split attempt 1 from attempts 2–4. The HAR-93 row
   (`complete` / `unknown_fields`) stays attached as a cross-check
   wherever it exists. HAR-90 setup_key groups: 478c43c9 {0036};
   643cf741 {0036-b}; df176e73 {0036-c, 0036-d, 0758-a, 0758-b};
   a2b8ed45 {0036-e, 0758-c}; 7929e9ee {0036-f, 0758-d} (0036 and 0036-b
   have dirty trees: unknown model_revision/serving/parser_digest).
3. `infra-pin:<field>`: a pin field in the job lab-metadata.json (none in
   current HAR-81/HAR-90 metadata).
4. `auto-*`: config-derived; any such row makes learnability NOT
   COMPUTED (config.json lacks the parser/normalizer version).

Declared equivalence (`--commit-equivalent`): two dispatch keys merge
iff every field matches except `eval_lab_commit` and both commits are in
the declared set (HAR-81: the two files differ only there). Two HAR-93
rows merge iff every column matches except
repository_commit/produced_at/field_sources, same commit rule. Both
parquet sha256s (treatment + capture) are in `summary.md` provenance.

Capture coverage joins `trial_capture.parquet` on `trial_name`:
`continuations_missing`, `proxy_input_tokens`,
`trajectory_input_tokens`, `input_tokens_unattributed`, plus the derived
`token_attributed_share` (see TOK-SHARE).

## Learnability

Per (task, treatment key or declared-equivalent keys). Excluded from
attempts and listed per cluster: no-signal trials (zero model turns),
suspect-grader trials (outcome R-ENV-02: a correct fix still scores
0, so the trial says nothing about capability) and declared
`pass_tainted` passes. `learnable` = 2+ scored attempts with at least
one pass and one fail; `all-pass` / `all-fail` = 2+ scored attempts,
one outcome; `grader-suspect` / `pass-tainted` / `no-signal` = every
trial excluded; otherwise `unknown`. Never across keys without a
recorded equivalence. HAR-90: one attempt per setup_key, so every cell
is `unknown` (the coordinator ruling also treats those runs as
engineering smoke runs under mixed treatments).

## Pass caveats and loop cost

- `pass_caveats` (R-NONE-01 rows only), each a reason the pass is a weak
  SFT sample as-is: `env_remediated` (the model `pip install`ed a module
  the same-task nop control fails on: 2684 stevedore at head#33),
  `parse_errors: N` (N >= 2 harness-rejected turns), `loop_heavy` (>= 25%
  of per-step prompt tokens inside loops: a2-001520, 91%, 75 identical
  completion claims after the fix).
- LOOP-COST (`loop_cost` per row, `summary.md` table): per-step
  `metrics.prompt_tokens` / `completion_tokens` inside every identical
  run (>= 10 byte-identical consecutive messages) plus the R-COMP-04
  confirmation span, steps counted once where they overlap. The share
  divides by the per-step sum over all model steps (same source).
  `result_input_tokens` (proxy total, includes summarization calls) rides
  along. Steps without metrics (summarization hand-off turns, cont-N#3)
  are counted in `steps_without_metrics`, never as 0.
- HANDSHAKE (`completion_handshake` per row, `summary.md` table): the
  harness asks "Are you sure you want to mark the task as complete? ...
  include "task_complete": true in your JSON response again." MiMo
  writes native tool calls, not Terminus JSON; per the recorded
  step_layers a native turn confirms only as `prose_completion` (no tool
  call: 000240 head#20-21). A claim plus a tool call is `calls`, so the
  call runs and the episode continues. Recorded per row: first prompt
  ref, prompt count, `confirmed` (stop = task_complete_confirmed), turns
  and per-step prompt tokens after the first prompt, and
  `echo_task_complete_turns` (the model running `echo "task_complete"`
  to follow the JSON wording: a2-arvo-18737 95x). Measurement only; it
  changes no tag.
- WEDGE (`wedge` per row, `summary.md` table; HAR-99): a stretch of
  executed turns whose keystrokes landed while the shell was NOT at a
  prompt, so they never ran as shell commands. The terminal state after
  each executed turn is read from the last non-empty line of its screen
  (ANSI stripped, the harness's "Are you sure...?" question cut):
  `prompt` (`user@host:path#`/`$` at line end, unanchored, so arvo's
  `agent@<uuid>:~/src/zstd$` and output-then-prompt both count),
  `pager` (`(END)`, a lone `:`, `--More--`, less's "Pattern not found
  (press RETURN)"), `continuation` (`> ` from an unclosed quote or
  heredoc), `interactive` (`>>>`, `(Pdb)`, `[y/N]`, password, inquirer
  `? Question`),
  `unsubmitted` (the turn typed without Enter: the shell sits at a prompt
  with a pending line, the R-TOOL-00 case, never a wedge), else
  `no_prompt` (a program reading stdin, or a submitted command still
  running). A turn is wedged when the previous executed turn left a
  non-prompt screen AND its own command does not appear right after a
  shell prompt on its screen (line wraps removed): a slow command that
  finished before the next turn is not a wedge. Rejected and empty
  (wait) turns send nothing, so they neither open nor close a stretch.
  Reported when a stretch has >= 3 turns, with trigger step and command,
  cause, start/end refs, `interrupt_ref` (first C-c, C-d, q, Escape or
  `:q` in the stretch), `prompt_returned`, `until_run_end`, and per-step
  prompt tokens inside. HAR-81 reads the harness-recorded executed
  keystrokes; HAR-90 uses normalizer replay (`keystroke_source`). An
  uninterrupted stretch adds the SECONDARY R-REC-02
  `error_recovery`/`model` note (`secondary_wedge`): the harness sent
  exactly what the model typed, and getting the shell back is the
  model's recovery to make. It never changes the outcome tag.

## Validation

- In-sample: `validation/har81-wave-a.hand-key.jsonl` (17 wave-A trials,
  parent hand reads; rules were developed against these): 17/17 on tag,
  attribution, stop and rule.
- Held-out: `validation/har81-holdout.hand-key.jsonl` (11 trials read
  blind by three readers who never saw tagger output). First scoring:
  8/11 on tag, attribution and rule; 11/11 on stop. The three
  disagreements:
  1. `p-d-arvo-57589`: tagger R-PLAN-01, hand R-COMP-03/unclear. Gap: no
     cyber rule for a model that never engaged the submit contract. Fix:
     R-COMP-03 gate (c).
  2. `p-d-candidate-1048`: tagger R-TOOL-01U from ONE recovered
     malformed turn (head#39), hand R-PLAN-01. Gap: any rejection counted
     as the outcome. Fix: rejections are outcome-relevant only as a storm
     (>= 10) or when nothing is accepted afterwards (see R-TOOL-01). This
     also moves HAR-90 0036 (one rejection at head#7) to its real
     outcome, R-TOOL-00 unclear (105 unsubmitted keystroke turns).
  3. `l-d-a3-candidate-1789`: tagger R-ENV-02, hand R-PLAN-01. Caused by
     the reader contract's wording ("no tests run"), not the tagger: the
     trial's own verifier shows 4/7 `ERROR at setup` and the nop control
     matches. Label kept as written; not fixed.

  After the two fixes: 10/11 (only #3 remains). That score is NOT blind
  for 57589 and 1048; the first-scoring 8/11 is the honest held-out
  number. Both fixes are general rules (no trial names), re-verified
  17/17 in-sample.
- Held-out 2: `validation/har81-holdout2.hand-key.jsonl` (the remaining
  14 HAR-81 trials, read blind by three readers given the rule
  definitions but no tagger output). First scoring: 14/14 on tag,
  attribution, stop and rule. Blind for 13 of 14: the claim definition
  (harness-recorded `task_complete` counts as a claim) was changed with
  a4-candidate-1634 in view before this key existed.
- Late: `validation/har81-late.hand-key.jsonl` (the two a4 format-code
  trials that finished after held-out 2; one blind reader, no rule
  change afterwards): 2/2. The reader returned the a3 job name for the
  a4-001520 read; corrected by the parent from the cited refs
  (`parent_note` in the row). Each row's `parent_note` adds what the
  reader missed: both terminals were wedged (see WEDGE validation).
- All 44 HAR-81 trials are keyed: combined 43/44 on tag, attribution
  and rule (the one miss is held-out #3), 44/44 on stop.
- What this measures: whether the tagger applies the written rules the
  way a careful reader does. It does not show the rules are the right
  taxonomy; readers were given the same rule definitions.
- Not scored: step refs and `first_failure`. Of 13 trials where both
  sides cite an outcome step, 9 match exactly. The 4 misses: one reader
  counted list positions (0-based) instead of `step_id` (a2-1634 and
  a4-1634, off by one); claim onset (a2-000240: hand cites the first
  CC-CLAIM text at head#17, the tagger the harness-recorded claim at
  head#16); p-d-1702 (tagger cites the first environment error at
  head#2, hand head#12). `first_failure` presence agrees on 33/42:
  readers null it when the outcome explains the trial (e.g. R-ENV-02),
  while the tagger reports the earliest environment error.

### WEDGE validation (HAR-99)

- Key: `validation/har99-wedge.hand-key.jsonl`, all 54 trials (44 HAR-81
  and 10 HAR-90), read blind by four readers (12 format-code, 16 arvo,
  16 candidate, 10 HAR-90) who got the definition (>= 3 consecutive agent
  turns whose keystrokes land while the shell is not at a prompt; causes
  pager / continuation / stdin_read / interactive_prompt /
  running_process) and never saw detector output. Hand: 8 wedged
  trials, 46 negatives.
- First scoring: the detector's first run was frozen before any key was
  scored (`validation/har99-wedge.frozen-f24e16dd.jsonl`, capabilities.py
  sha256 f24e16dd). Trials 51/54 (0 misses, 3 false alarms). All 8 hand
  stretches matched: start exact 6/8 (8/8 within 3 turns), end exact 6/8
  (8/8 within 1), interrupt 8/8, prompt back 8/8, cause 7/8.
- The 3 false alarms: `a4-arvo-18737` (slow `find /`, 3 turns) and
  `a4-candidate-1789` (slow `pip install`, 5 turns): the command was
  still running when the screen was captured, but it finished before the
  next turn, which landed at a prompt. `p-d-format-code-001520`: see
  below.
- Two changes after scoring, applied to every trial (NOT blind; no
  unkeyed trials are left to hold out): (1) a turn whose own command
  appears right after a shell prompt on its screen landed at a prompt,
  whatever the previous screen said; (2) inquirer questions (`? Location
  of ...`) are `interactive` (p-d-000434's dredd init questionnaire; the
  reader said interactive_prompt, the first run said no_prompt). After:
  trials 53/54, stretches 8/8, start exact 7/8, end exact 7/8, cause
  8/8, interrupt and prompt back 8/8.
- Remaining disagreements, left as they are rather than tuned to the key:
  - `p-d-format-code-001520` (trial level): two pager episodes of 3
    turns each (cont-1#7-9, #27-29). The `q` that quit less (cont-1#9,
    #29) landed in the pager, so the definition counts it; the reader
    counted only the swallowed turns (2 each). Both episodes have an
    interrupt, so neither adds the R-REC-02 secondary.
  - `p-d-arvo-42496599` start (head#12 vs head#15): the reader split at
    the empty wait head#14 and dropped the 2-turn piece before it; the
    detector skips turns that send nothing.
  - `p-d-arvo-42514310` end (head#19 vs head#18): at head#19 the queued
    commands flushed and the prompt came back; the detector counts that
    turn, the reader did not.
- HAR-90: 10/10, no wedges. 0036's 115 keystroke turns without Enter are
  `unsubmitted` (the shell sits at a prompt with a pending line: the
  R-TOOL-00 mechanism), which the reader also ruled not a wedge.

## Known limits (honest)

- WEDGE is measurement plus a secondary note; it changes no outcome tag.
  The outcome tag says WHAT failed (a3-001520 R-COMP-02, a4-001520
  R-UNC-01, a4-000240 R-PLAN-01); the wedge says WHY. Promoting it to an
  outcome would need a precedence decision and a held-out set, since all
  54 available trials were used to fix the rule.
- WEDGE reads the screen captured right after each turn. A slow command
  can look wedged in one capture; the landed-at-prompt check clears it
  only when the next turn's command is visible right after a prompt.
  Commands shorter than 4 characters cannot be checked that way.
- The `no_prompt` cause does not separate a program reading stdin from
  one still running (the readers did, from context). Across all 54
  trials no C-c, C-d or Escape was ever sent; `q` appears only in
  p-d-001520.
- The harness did not treat identical native shapes identically across
  runs: 0758-d head bash/XML turns executed while 0758-b's same-shape
  turns were all rejected (761/761, `No valid JSON found`). The later
  runs (0036-f, 0758-d) show warnings/acceptance patterns consistent
  with partial normalizer handling (`keystrokes`/`write`/`description`
  variants still rejected). The why is out of scope; acceptance here is
  strictly per-step observation text.
- `result.json` `n_episodes` counts inconsistently across trials (0758-c:
  213 total steps incl. user; 0758-d: 234 agent steps). Coverage reports
  raw `assembled-agent/n_episodes` pairs AND the token-attributed share
  without forcing equality.
- 0758-b head is missing, so pre-cont-11 context is unknown; its episode
  coverage is complete via the cumulative cont-11 but token coverage is
  86.0% (3.6M input tokens unattributed, conts 1-10 never materialized).
- 0758-c: conts 1-30 never materialized (context-exceeded split failures
  in trial.log); trajectory covers ~5.5M of ~26.2M ledger input tokens
  (21.1% share). Its step-complete coverage must NOT be read as full
  capture.
- R-CTX-01 boundary (when it fires) is the first trailing identical-run
  start (no timestamped step<->log-line mapping exists); trial.log
  remains the authority for cycle counts and line ranges.
- `reading_sheet.md` keeps probe-02's six label columns exactly and
  appends the proposal columns (first + outcome failures with evidence).
