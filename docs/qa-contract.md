---
status: living
audience:
  - builder
  - operator
---

# QA contract (`evallab.qa`)

One contract for task QA, trace QA, the reward seeker and tripwire findings,
so their output is comparable and scoreable on external answer keys.
Implementation: `src/evallab/qa/` (`models`, `answer_key`, `score`, `suite`,
`cli`); tests: `tests/test_qa.py`, `tests/test_qa_suite.py`. The trace auditor
(`evallab qa trace`, `src/evallab/qa/trace.py`) is described in
`docs/harbor-view.md`. Plan of record: `research-context/FOCUS.md`.

## Finding: `qa.finding/v1`

One JSON document per finding:

- `schema`: `qa.finding/v1`
- `subject{kind: task|trace, task_id, task_digest, trial_id?}`
- `stage` (producer-defined, e.g. `S0`/`S2`/`T1`)
- `verdict: pass|fail|unknown`
- `category`: `broken|reachable|tamperable|weak|reward_hacking|false_positive|false_negative|prompt_misaligned|failure_cause`
- `severity: low|medium|high|critical` and `fault: agent|eval|platform` —
  required on `fail` findings; `pass`/`unknown` findings may leave them null
- `summary` (optional one-line human summary)
- `evidence_refs[]` (paths/URIs into retained evidence)
- `reproducer{kind: harbor_trial|command|patch, path, expected_reward}` (optional)
- `refutation{status: verified|refuted|unverified, by, reason}`
- `reviewer{agent, model, tokens, cost_usd}`
- `measurement: measured|ran_without_usable_result|not_run` — a
  `not_run` check can never carry `pass` (validated).
- `root_cause: [id, ...]` (optional, default empty) — the root causes the
  reviewer names, in the key's vocabulary (below). Findings written
  before this field existed stay valid; they simply name none.

## Answer key: `qa.answer_key/v1`

Directory under `research/answer-keys/<key_id>/`:

- `README.md` — source, licence, label provenance (required).
- `items.jsonl` — one item per line: `subject`, `expected_category`
  (required for `fail` items), `expected_verdict`, `label_source`,
  `split` (`dev`/`test`), plus optional `item_id`, `role`, `pair_ids`,
  `expected_fault`, `expected_root_cause`, `expected_root_causes`.
  Source-specific columns (`cheat_prompted`, `trial_ref`, ...) are kept,
  not rejected.
- `pairs.jsonl` (optional) — `{parent, child, category, split}` by
  `item_id`: a clean parent and its faulty child.

### Root-cause vocabulary

`evallab.qa.expected_root_causes(item)` turns each key's labels into a set
of ids; ids compare after normalization (lowercase, every run of
non-alphanumerics becomes `_`, so `Proxy-check too weak` equals
`proxy_check_too_weak`). The first non-empty source wins:

| Source field | Keys | Ids |
|---|---|---|
| `expected_root_causes` (list) | new keys (canonical) | as listed |
| `expected_mechanisms` (list) | `breaker-bench-v1` | benchflow mechanism (`conftest_hook`, `path_trojan`, `pth_injection`) or Terminal Wrench exploit class (`hollow-implementation`, ...) |
| `root_cause` (records) | `envcheck-tb4-39`, `delphik-open-defect-tb2` | each record's `id` and its `source_class` (EnvCheck: `proxy-check-too-weak`, `answer-material-readable-by-candidate`, ...; Delphik classes are free text, so exact matching rarely fires) |
| `expected_root_cause` dict | `trajectory-judge-400` | `failure_type` (`premature_stop`, `wrong_tool`, ...; step localisation is scored by `evallab qa trace score`) |
| `expected_root_cause` string | `trace-paired-v1` | `cheat_ladder: a,b` → the attack names; `upstream_fetch`, `answer_leak`, `false_claim` as is; free text after a colon (`weak_hidden_tests: ...`) → the prefix |

Never tune on the split you report; every score row names its split.

## Scoring

```bash
uv run evallab qa score --key <key_id> --findings <dir> \
  [--split dev] [--agent NAME] [--system NAME] [--status TEXT] [--notes TEXT]
```

`--findings` is scanned recursively for `*.json` and `*.jsonl`. Only
documents tagged `schema: qa.finding/v1` load (reports and manifests next
to them are skipped); a tagged document that fails validation is an error;
byte-identical copies load once. If a producer keeps several *versions* of
a finding (raw and published), point `--findings` at one of them. macOS
AppleDouble twins (`._*`, extracted from archives made on a Mac) are skipped
here and by `qa task-score`.

One call appends rows to `research/qa-scoreboard/scoreboard.csv`: one per
split (`all` plus each split in the key) and category (`all` plus each
expected category). Columns: `date_utc, key_id, split, category, agent,
system, recall, false_alarm_rate, root_cause_match,
paired_discrimination, cost_per_item_usd, n_items, n_measured, n_pairs,
n_pairs_measured, status, notes`.

- *Measured*: a finding with `measurement: measured` and a `pass`/`fail`
  verdict. Unmeasured items are never passes and never misses; they
  lower `n_measured` and set `status` to `partial: X of Y items measured`.
- *Flagged*: a measured `fail` finding on the item whose refutation is not
  `refuted` (a refuted claim is not a standing flag).
- `recall` = flagged / measured positives; `false_alarm_rate` = flagged /
  measured negatives.
- `root_cause_match` = of flagged positives that carry a labelled root
  cause, the share with a `fail` finding whose `root_cause` names one of
  them. `n/a` when the findings name no root causes at all (not
  measured, never 0). Once a reviewer names causes anywhere in the run, a
  flagged positive it names none for is a miss.
- `category_match` (in `notes`, `x/y`) = of flagged positives, the share
  with a `fail` finding in the expected category. Before 2026-10-15 the
  shared scorer wrote this number into `root_cause_match`; rows from that
  scorer dated earlier carry category match in that column.
- Category rows hold the items labelled with the category plus both
  members of every pair of that category, so per-category paired
  discrimination keeps every pair (same denominators as #894).
- `paired_discrimination` = mean over pairs with both sides measured of
  `flag(child) − flag(parent)`.
- `cost_per_item_usd` = reviewer cost of matched findings / measured
  items. Findings on subjects outside the key are counted in `notes`.

## Frozen version comparisons: `qa suite`

```bash
uv run evallab qa suite --agent trace \
  --version qa-trace/7 --version qa-trace/8 \
  --runs /private/tmp/retained-trace-runs.json --date 2026-10-19 \
  --output /private/tmp/qa-trace-comparison.md --json
```

This command only reads surviving evidence. It runs no model, solver, task,
repair loop or benchmark, and its **new re-score cost is $0**, not a claim
about historical model cost. One `--version` is valid; repeat it for a
comparison. `--agent` is `task`, `trace`, `breaker` or `tripwire`.

The optional `--runs` JSON manifest has exactly this shape (additional
fields are rejected):

```json
{
  "runs": [
    {
      "agent": "trace",
      "version": "qa-trace/8",
      "key": "trace-paired-v1",
      "findings": "raw/qa-trace-8/findings",
      "run": "retained-run-id",
      "notes": "Optional evidence metadata, not scoring authority"
    }
  ]
}
```

`agent`, `version`, `key` and `findings` are required. `run` and `notes` are
optional strings. `findings` is an existing directory of raw
`qa.finding/v1` JSON/JSONL documents, or a JSONL file containing only those
records. Relative paths resolve from the **manifest's directory**, not the
shell's directory or repository root. Directories may contain ancillary
JSON reports; malformed tagged findings are errors. AppleDouble `._*` files
are ignored. Duplicate JSON fields, duplicate `(agent, version, key)`
bindings, conflicting finding ids, unknown keys and invalid sources fail
without replacing the previous report. One explicit binding replaces
local discovery for that version/key; it does not pool alternative runs.
The same source can bind more than one key, with exact subject filtering.

Without a binding, discovery is limited to a registered key's
`findings*.jsonl` and `findings/`. A filename or manifest label is **not**
producer proof. Supported version identities are `qa-task/…` or
`qa-task@…`; `qa-trace/…` or `qa-trace@…`; `qa-breaker/…`,
`qa-breaker@…`, `breaker/…`, `breaker@…` or `cheat-ladder-…`; and
`tripwire/…`, `tripwire@…`, `qa-tripwire/…` or `live-watch/…`,
respectively. They must match raw `reviewer.agent` exactly. For a trace
pipeline such as `qa-trace/8`, raw `qa-trace/8/T0` through `/T3` also
prove that pipeline when the suffix equals the finding's `stage`. A
stage name itself is not a suite version. `cheat-ladder-3.0.0` and
`cheat-ladder-v3.0.0` are distinct identities. Generic `QA Task`,
`Traces` or `TA monitor` records remain unavailable for a selected version;
custom decision files and scoreboard aggregates are not raw findings.
An explicit source with a different/unversioned producer is an error,
never relabelled. Task evidence additionally requires the same task id
**and package digest**; trace evidence requires the same task id and trial
id. Missing identities and same-name/different-digest findings cannot
measure a key item.

Every registered direct-child answer key is loaded, including mixed keys,
and every native split gets one `category=all` row per requested version.
There is no pooled `all` split or hardcoded key list. Keys, pairs,
assignments and exposures remain frozen: the existing split guard checks
the lock files, pair/task grouping and exposure acknowledgements before
publication. Claimed exclusions are also checked against the actual
measured item ids, not just aggregate coverage. Rows with no subjects of
the agent's kind are `inapplicable`; no version-proved source is `no-run`;
a source with no usable matching decisions is `unmeasured`.

The suite calls the shared scorer, preserving refutation, root-cause and
paired-discrimination semantics. Rates show their labelled **measured**
denominators; unknown labels never become negatives. Unknown decisions,
explicit not-run, unusable runs and missing evidence have separate item-level
counts. With several stages, usable measured evidence takes precedence, then
unknown decisions, unusable runs, explicit not-run and missing evidence.
Only complete measured pairs enter paired discrimination; no measured
pairs is `n/a`. Overlapping keys are not independent replications, and
exposed or source-derived labels are not a new blinded/generalization
result. A scoped nonflag does not certify a clean task or trace.

Recorded reviewer cost includes only matched findings for this
version/key/split and distinguishes explicit recorded `$0`, missing cost
fields, and the known subtotal of partially recorded charges. Per-measured
item cost is available only when all matched measured findings record
cost; unusable/unknown decision charges remain in recorded totals.
These are raw finding charges, **not total historical audit/model/solver
cost**. JSON exposes the nullable suite cost fields and deliberately
omits the legacy scorer's default-zero `cost_per_item_usd`.

The deterministic Markdown report atomically replaces
`research/qa-scoreboard/suite-<date>.md` (UTC report date by default);
`--output` overrides that destination and `--json` also prints the rows,
denominators and SHA256 custody receipts. No scoreboard CSV rows are
appended. Inputs cannot be used as the output destination. File receipts
hash original bytes; directory receipts hash the sorted relative-path /
file-byte-hash inventory of files containing findings. Frozen key README,
item, pair, assignment, exposure and manifest hashes accompany the comparison.
JSON also lists every bound source path (including unrequested versions),
which is reserved against accidental output overwrite but is not thereby
scored or certified.
See `research/qa-scoreboard/README.md` for a retained Breaker example.

## Keys on main that load with this module

| Key | Owner | Contents |
|---|---|---|
| `breaker-bench-v1` | Breaker | 626 task items, 214 pairs (benchflow 0.2.0 vs 0.2.2, Terminal Wrench holdout) |
| `trace-paired-v1` | Trace | 81 trace items, 60 pairs (own runs; CheatAgent and contamination-audit children) |
| `trajectory-judge-400` | Trace | 400 trace items, 251 pairs (upstream trajectory-judge) |
| `envcheck-tb4-39` | QA Task | 39 Terminal-Bench tasks: 13 EnvCheck grader defects (with upstream root-cause ids) + 12 `epoch_flagged` defects (Epoch TB4 review, one public issue each) + 14 weak negatives |
| `delphik-open-defect-tb2` | QA Task | 47 Terminal-Bench 2 tasks, held-out only (Delphik open-defect source-read claims) |
| `executed-outcomes-v1` | QA Task | 56 MiMo/TB task packages labelled by an executed check (ref-subset, #867 hunt/repair, grader probes, #899 replays) |
| `mimo-weak-tests-v1` | QA Task + Trace | 49 items: 16 weak MiMo packages (3 partial-fix-pass packages, 13 ref-subset confirmed), 10 refuted flags, 16 frozen clean controls; 4 partial-fix passes + 3 clean parents as traces |

`envcheck-task-audit` (an undigested copy of the same 39 tasks) was retired on
2026-10-15 so one split lock (`envcheck-tb4-39`) governs those tasks.

Split assignments, exposures and the guard live in `research/answer-keys/splits/`.
