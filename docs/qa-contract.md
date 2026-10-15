---
status: living
audience:
  - builder
  - operator
---

# QA contract (`evallab.qa`)

One contract for the three QA agents (task auditor, trace auditor,
reward seeker) so their output is comparable and scoreable on external
answer keys. Implementation: `src/evallab/qa/` (`models`, `answer_key`,
`score`, `cli`); tests: `tests/test_qa.py`. The trace auditor
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

## Keys on main that load with this module

| Key | Owner | Contents |
|---|---|---|
| `breaker-bench-v1` | Breaker | 626 task items, 214 pairs (benchflow 0.2.0 vs 0.2.2, Terminal Wrench holdout) |
| `trace-paired-v1` | Trace | 81 trace items, 60 pairs (own runs; CheatAgent and contamination-audit children) |
| `trajectory-judge-400` | Trace | 400 trace items, 251 pairs (upstream trajectory-judge) |
| `envcheck-tb4-39` | QA Task | 39 Terminal-Bench tasks: 13 EnvCheck grader defects (with upstream root-cause ids) + 26 weak negatives |
| `delphik-open-defect-tb2` | QA Task | 47 Terminal-Bench 2 tasks, held-out only (Delphik open-defect source-read claims) |
| `envcheck-task-audit` | Infra | the same 39 tasks as `envcheck-tb4-39` without digest pins or root-cause ids; splits copied from `envcheck-tb4-39` (#904). Prefer `envcheck-tb4-39`; this key has no labelled root causes, so root-cause match is `n/a` on it |

Split assignments, exposures and the guard live in `research/answer-keys/splits/`.
