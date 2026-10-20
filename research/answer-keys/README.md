# Answer keys (qa.answer_key/v1)

An answer key is committed research data: a labelled set of benchmark task
packages used to score `evallab qa task` via `evallab qa task-score --key
<key_id> --findings <run_dir> [--split dev|heldout]` (or the shared
`evallab qa score --key <key_id> --findings <run_dir>/findings`). Digests
(`subject.task_digest`, computed with
`evallab.registry.task_directory_digest` on the exact labelled package dir) pin
the package bytes the label applies to; a label does not transfer to a
different checkout or commit of the same task name. In `evallab qa task-score`,
findings and task reports bind to an answer-key item only when
`(subject.task_id, subject.task_digest)` matches exactly; same-`task_id`
findings or reports (including signal-only reports) with a different digest are
excluded from hits and counted in `digest_mismatches`.

Layout: `<key_id>/{README.md, items.jsonl, build.py}`. `build.py` is
deterministic (stdlib + locked Eval Lab dependencies only) and regenerates `items.jsonl`
byte-identically from local sources; items are sorted by `item_id` and each
line is `json.dumps(item, sort_keys=True)`.

## Item schema (one JSON object per line)

```json
{"schema": "qa.answer_key/v1", "key_id": "<key_id>", "item_id": "<task_id>",
 "subject": {"kind": "task", "task_id": "<task.toml [task].name if present else <benchmark>/<dir name>>", "task_digest": "<sha256:...>"},
 "package": {"pin": "<version/commit label>", "name": "<task dir name>", "root_hint": "<~-relative path of the tasks/ dir>"},
 "expected_verdict": "fail" | "pass" | "unknown",
 "expected_category": "<primary category or null>",
 "expected_categories": ["..."],
 "root_cause": [{"id": "<source finding/defect id>", "category": "...", "summary": "<one line>", "source_class": "<raw class/type from source>"}],
 "label_source": "<short provenance string>",
 "label_strength": "confirmed" | "weak_negative" | "refuted_claim",
 "split": "dev" | "heldout"}
```

Categories (only these): `broken` (oracle/verifier/env cannot grade
correctly: rejects correct work, crashes, oracle fails, nondeterministic),
`reachable` (answer/solution/oracle material readable or fetchable by the
agent), `tamperable` (agent can write grader state/reward channel or run
code in the verifier trust domain), `weak` (verifier accepts wrong work:
insufficient checking, loopholes), `prompt_misaligned` (instruction and
tests disagree / underspecified). Negatives have `expected_verdict: "pass"`,
`expected_category: null`, `expected_categories: []`, `root_cause: []`.

`expected_verdict: "unknown"` preserves an observed task without asserting that
it is clean or defective. A measured finding on it contributes to coverage, but
not to the shared scorer's recall or false-alarm denominators. Source-derived
audits must not convert "no supported defect established" into a clean negative.

## Task-audit scoring (`evallab qa task-score`)

`evallab.qa.task.score_findings` (`evallab.qa_task.score/v1`) scores all
selected items in `--split` (denominators are **all selected items**, not only
measured ones):

- **Verified vs flagged hits**:
  - `verified_recall` (on `expected_verdict == "fail"`) and
    `verified_false_alarm_rate` (on `expected_verdict == "pass"`) require a
    `verified_hit`: at least one digest-bound `qa.finding/v1` record (with a
    required non-null `reproducer`) having `fault == "eval"`,
    `verdict == "fail"`, and `refutation.status == "verified"`.
  - `flagged_recall` and `flagged_false_alarm_rate` require a `flagged_hit`: at
    least one standing unrefuted digest-bound `Finding` (`fault == "eval"`,
    `verdict != "pass"`, `refutation.status != "refuted"`) **or** at least one
    unreproduced `Signal` in `report.signals` (from `S1` rubric review or `S0`
    leak inspection). Note that the shared `evallab qa score` command reads only
    measured `pass`/`fail` findings in `<run>/findings/` and does not inspect
    `tasks/*/report.json` `signals`.
- **Category match and root-cause match**:
  - `category_match_recall` checks whether a verified finding's coarse
    `category` is in `expected_category` / `expected_categories`.
  - A finding may name `root_cause` ids (optional `qa.finding/v1` field); they
    match an item when one equals a `root_cause[].id` or `root_cause[].source_class`
    after normalization. `evallab qa task-score` reports
    `verified_root_cause_match` / `flagged_root_cause_match` and writes the
    level's value to the shared `root_cause_match` column; it is `n/a` when no
    finding names a cause (see `docs/qa-contract.md`).
- **Measuredness, coverage, and split-scoped cost**: `observed_fraction` is the
  share of selected items with a digest-bound `TaskReport`;
  `measured_fraction` is the share whose `TaskReport` has
  `quality_complete == True` (at least one scored check and all checks in
  `report.checks` `measured`). Unmeasured or `not_run` checks never count as
  `pass` (including default four-stage `S0,S1,S2,S3` runs where `S2` live
  probes and `S3` fresh replay are `not_run`). `model_tokens` and
  `cost_per_item_usd` sum only over the digest-bound reports matching the
  selected split.
- **No paired discrimination**: task answer keys contain standalone task
  packages rather than paired clean/cheat trajectories, so
  `paired_discrimination` is `n/a`.

## Split rule

Never tune on the split you report. Use `dev` items for development and
prompt/threshold tuning; report scores on `heldout` items only (the trace keys
call the reported split `test`; it means the same). Keys that are entirely
`heldout` must not be used for tuning at all.

Every key's item-level split assignment is published in `splits/assignments.csv`,
and `splits/exposure.jsonl` records which systems were tuned on which items.
`splits/guard.py --check` (run in CI by `tests/test_answer_key_splits.py`) fails on
units straddling splits, stale assignments, and scoreboard rows that score tuned
items on a reported split; see `splits/README.md`. After adding or rebuilding a key,
run `uv run python research/answer-keys/splits/guard.py`.

## Internal paired actor views

`uv run python research/answer-keys/internal_traces.py` reproduces the frozen
full/actions-only companion views described in
[`internal-views/source.json`](internal-views/source.json). This does not rebuild,
relabel, or register replacements for the original answer keys. The driver-only
custody, coverage, predictions, baseline, and leak-audit files are committed in
`internal-views/`; actor payloads remain outside Git. Loader-ready companion keys
are published under
`~/Developer/eval-lab-results/2026-10-19/internal-traces/keys/{full,actions}/<key>`.
The artifact and research-context roots can be overridden by the CLI flags.

- `trajectory-judge-400`: all **188 test items**, 138 faulty and 50 clean; the
  existing `tj_atif` adapter retains the embedded observations. These labels
  cover general actor faults, including 82 silent and 56 loud faults, not just
  wrong-but-passing submissions or reward-hacking behavior.
- `trace-paired-v1`: all **27 test slots** remain in `coverage.jsonl`; only
  **11 original actor inputs** exist (8 labelled children, 3 passing parents).
  The other **16 are unavailable**, not benign and not detector misses. No
  verifier transcript, retained diff, or fabricated empty trace substitutes for
  them. The exceptional oracle parent uses its explicitly receipted original
  one-action source, not its regrade verifier transcript. Stored terminal output
  may already be clipped.
- `mimo-v7-canonical-pass70`: all **70 accepted canonical original v6 pass
  trajectories**, labelled by the independently accepted v7 export as **6 wrong
  submissions and 64 real passes**. All arms/attempts are retained, including
  25 cells excluded only from the separate primary comparison. All 70 current
  source hashes match their sealed bundle receipts; original bytes are retained
  in the driver-only `sealed-sources/` store outside Git. This is not the older
  33-task `mimo-heldout-v6-to-v7` grader-weakness key.

Each actor view passes `blind_atif` and uses opaque filenames/session IDs.
Full means stored actor-step evidence, including prose and explicit reasoning,
not evaluator headers, identities, usage metrics, or operational duplicate text.
Actions-only removes agent prose and explicit reasoning fields while retaining
the same ordered calls, arguments, observations, and recorded user/system text.
Reasoning in code comments, tool arguments, and observations is not erased.
Never expose the source/key/coverage manifests or `sealed-sources/` to detectors.
The only baseline is the unchanged transcript-visible tripwire rule OR; its
Wilson intervals are descriptive, and wrong-submission labels do not establish
behavioral intent. No model calls, submitted-code execution, or regrading occur.

## Keys and provenance caveats

- [`tb4-audit-v1`](tb4-audit-v1/README.md): **241 PM finding records** (238
  task-scoped + 3 benchmark-wide), all 30 Epoch flags and 39 historical EnvCheck
  rows; 66-task cohort, seeded 25 dev / 41 test. **10 dev-split witness
  programs** replayed (`reward=1.0` upstream TB4 on Modal, `reward=0.0` on the
  fixed port; $0.79 finalized Modal / $0.92 incl. 2 aborted attempts); the
  remaining 231 findings stay source-reported, and there are no certified-clean
  negatives. Reviewed taxonomy, explicit port-test mappings, scorer-side labels
  and witnesses, whitelisted blind task exports, and split-guard registration.
- [`cheatbench-actions-v1`](cheatbench-actions-v1/README.md): **300 external
  traces**, 75 positive + 75 negative in each dev/test split; benchmark-family
  holdout, blinded full normalized ATIF and assistant-prose-free actions view.
  MIT source, pinned raw hashes, exhaustive label-leak and mutation audits, $0
  fixed-tripwire baseline with Wilson intervals. **Already present in detectors-v1
  calibration:** not a fresh unseen dataset for systems tuned on that corpus.
- [`hacktrace-act-v1`](hacktrace-act-v1/README.md): **300 external trajectories**,
  75 ACT-positive + 75 negative in each upstream problem-disjoint split.
  CC BY 4.0 source; explicit file-submission blocks are decoded into ordered
  writes, not invented native shell actions. Full and actions-only ATIF, source
  pins, exhaustive leakage audits and the same $0 frozen tripwire baseline.
  ACT negatives can be wrong submissions; they are not correctness certificates.
  Both external keys have `subject.task_digest: null` because they do not provide
  digest-bound runnable task packages; source and trajectory byte hashes are
  separate manifest fields.
- [`mimo-v7-canonical-pass70`](mimo-v7-canonical-pass70/README.md): **70 MiMo v6
  passes** (27 tasks), all `test`: 6 `wrong_pass` (`expected_verdict: fail`) and 64
  `real_pass`, labelled by the accepted v7 regrade (`regrade-v7.json`, pinned in
  `internal-views/source.json`). `trial_ref`/`actions_only_ref` are the hash-bound
  internal views above; `build.py` re-derives the manifest and checks every hash.
  Grading-correctness labels, not behavioural intent; the trajectories were already
  packaged for qa-trace-6 held-out v7 runs.
- [`mimo-fresh-window3`](mimo-fresh-window3/README.md): **52 fresh window-3 MiMo
  rollouts**, all `test`. 49 come from the CheatBench cheat-set (4 `wrong_pass`, 14
  `real_pass`, 31 `not_a_pass`; v6/v7 rewards) and 3 are native PD-1 `real_pass`
  cells. Blinded full and actions-only views; label sources are pinned in
  `source.json` and the 8 excluded cells are listed in `coverage.jsonl`. Prompted
  population; wrong passes are rare.

- `envcheck-tb4-39`: **39 items** on historical Terminal-Bench 4 checkouts (`v4.0.0`
  and `1dcda8716784`).
  - **13 `confirmed` defect items**, aggregating 24 EnvCheck finding rows from
    `post/2026-10-grader-defects`.
  - **12 `epoch_flagged` defect items** (2026-10-15): former weak negatives that
    Epoch's TB4 review links to a public issue.
  - **14 `weak_negative` items.**
  - The split is stratified on the original EnvCheck labels and locked: 4 dev + 9
    heldout defects, 9 dev + 17 heldout former negatives.
  - This is not 50 tasks as in early planning notes. It is the only key for these
    39 tasks: the undigested `envcheck-task-audit` copy was retired on 2026-10-15.
- `delphik-open-defect-tb2` — **47 items**, all `heldout`, on Terminal-Bench
  2.0 at pinned commit `f5b891cb4f7c20e306f9d05887628b43af740f43`: **23
  archival defect claims** from Delphik's source-read audit ledger
  (`label_strength: "confirmed"` in the schema, representing archival ledger
  status rather than live re-verification in Eval Lab) and **24 negatives**
  (**23 `weak_negative`** sampled 1:1 from the 60 unflagged TB 2.0 tasks + **1
  `refuted_claim`** `torch-pipeline-parallelism`; not 29 defects or 545
  negatives from early cross-benchmark notes). Several Delphik defect entries
  reflect historical external/network drift (for example, `build-pov-ray`
  issue-76 recorded a `povray.org` HTTP 403 download block that upstream later
  restored).
- `executed-outcomes-v1`: **56 items** on 46 tasks, mostly MiMo `format-code`.
  - Each label rests on an executed check: a ref-subset subset grade plus triage,
    a #867 mutant killed by repaired tests, a re-run grader attack, or a #899
    replayed exploit.
  - Items carry `selected_by`, and `splits/exposure.jsonl` marks each source
    tool's own recall on them as circular.
  - It contradicts two weak negatives: envcheck-tb4-39 `fin-saccr-rwa` and
    Delphik `fix-git`.
  - See `executed-outcomes-v1/README.md`.
- `mimo-weak-tests-v1`: **49 items**. It is the MiMo weak-test seed key for the loop
  find weak tests → repair the tests → regrade old passes.
  - **Weak task packages (16):**
    - 3 packages where an honest partial fix was rewarded: 001809, 001985 and 002486;
    - 13 ref-subset weak tests confirmed by the refutation read.
  - **Negatives:** 10 refuted ref-subset flags and 16 frozen clean controls.
  - **Traces:** the 4 partial-fix passes (`expected_regrade: flips`) and their 3 clean
    parents.
  - **Caveat:** ref-subset selected most of these items, so ref-subset's own score
    on them is circular.
  - See `mimo-weak-tests-v1/README.md`.
- `mimo-weak-tests-v2`: the fixed 567-task MiMo primary-probe atlas, with a
  **positive-only, package-digest-bound** weak-test key and executable witnesses.
  - Block omissions and line survivors are labels only after instruction-level
    adjudication; unresolved, unreviewed, and unprobed cases stay explicit.
  - Recorded controls are not fresh replay or repaired-package certification.
  - Selection is source-derived and circular for the selecting tool; existing
    split assignments and the historical v1 key are preserved.
  - See `mimo-weak-tests-v2/README.md` and its deterministic `summary.json`.
- `mimo-weak-tests-v2.1`: the complete versioned 567-task atlas after adjudicating
  **all 112 archived unreviewed line survivors across 22 tasks**.
  - Preserves the immutable v2 publication, its accepted witnesses and historical
    splits; exports additional positives only after prompt-bound source review.
  - No unreviewed survivors remain; adjudicated uncertainty and other coverage
    gaps remain explicit rather than becoming clean negatives.
  - Whole-patch witnesses are portable within the repository. Archived controls
    remain distinct from fresh execution or repaired-package certification.
  - Consume this provenance-rich key with the shared `evallab qa score`; the
    separate `qa task-score` local model rejects the extra provenance columns.
  - See `mimo-weak-tests-v2.1/README.md` and its deterministic `summary.json`.
- `mimo-heldout-v6-to-v7`: retrospective audit of the frozen 33 MiMo evaluation
  task IDs, with original-audit package bindings and selected-v7 replay
  diagnostics kept separate. Supported semantic weaknesses, unresolved claims,
  equivalent submissions, and incomplete scientific qualification are distinct;
  the key does not release v7 scoring.
- `mimo-stratified-sample100`: seeded, language-stratified sample from the
  previously measured non-held-out MiMo probe pool. The strict weak-test
  fraction and historical atlas agreement are descriptive source
  reconciliation, not an independent detector benchmark or an unbiased estimate
  over all MiMo tasks. Nonpositive atlas entries remain unknown, not clean.
