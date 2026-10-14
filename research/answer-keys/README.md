# Answer keys (qa.answer_key/v1)

An answer key is committed research data: a labelled set of benchmark task
packages used to score `evallab qa task` (`evallab qa score --key <key_id>
--findings <run_dir> [--split dev|heldout]`). Digests (`subject.task_digest`,
computed with `evallab.registry.task_directory_digest` on the exact labelled
package dir) pin the package bytes the label applies to; a label does not
transfer to a different checkout or commit of the same task name. Findings and
task reports bind to an answer-key item only when
`(subject.task_id, subject.task_digest)` matches exactly; same-`task_id`
findings or reports (including signal-only reports) with a different digest are
excluded from hits and counted in `digest_mismatches`.

Layout: `<key_id>/{README.md, items.jsonl, build.py}`. `build.py` is
deterministic (stdlib + evallab only) and regenerates `items.jsonl`
byte-identically from local sources; items are sorted by `item_id` and each
line is `json.dumps(item, sort_keys=True)`.

## Item schema (one JSON object per line)

```json
{"schema": "qa.answer_key/v1", "key_id": "<key_id>", "item_id": "<task_id>",
 "subject": {"kind": "task", "task_id": "<task.toml [task].name if present else <benchmark>/<dir name>>", "task_digest": "<sha256:...>"},
 "package": {"pin": "<version/commit label>", "name": "<task dir name>", "root_hint": "<~-relative path of the tasks/ dir>"},
 "expected_verdict": "fail" | "pass",
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

## Scoring contract (`evallab.qa_task.score/v1`)

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
    leak inspection).
- **Category match vs root-cause match**:
  - `category_match_recall` checks whether a verified finding's coarse
    `category` is in `expected_category` / `expected_categories`.
  - `root_cause_alignment_fraction` reports the share of positive items that
    have at least one verified finding with a non-null `root_cause_id`.
  - `root_cause_match_recall` checks whether a verified finding's explicit
    `root_cause_id` matches an entry `id` in `item.root_cause`, and is `null`
    when no verified finding on a positive item carries a non-null
    `root_cause_id`. **Coarse category agreement is not a root-cause match.**
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
  packages rather than paired clean/cheat trajectories, so `score_findings`
  does not compute or claim paired discrimination.

## Split rule

Never tune on the split you report. Use `dev` items for development and
prompt/threshold tuning; report scores on `heldout` items only. Keys that
are entirely `heldout` must not be used for tuning at all.

## Keys and provenance caveats

- `envcheck-tb4-39` — **39 items** on historical Terminal-Bench 4 checkouts
  (`v4.0.0` and `1dcda8716784`): **13 defect items** (`label_strength:
  "confirmed"`, aggregating 24 EnvCheck finding rows from
  `post/2026-10-grader-defects`) and **26 `weak_negative` items** (2 per defect
  in that pin, stratified `dev`/`heldout` split: 4 dev + 9 heldout defects, 9
  dev + 17 heldout weak negatives; not 50 tasks as in early planning notes).
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
