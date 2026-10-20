# tb4-audit-v1 — source-reported TB4 task defects

**Scorer-side answer key, not an auditor workspace.** The key preserves all **241
Preference Model catalogue entries**: 238 task-scoped findings on 60 tasks and
3 benchmark-wide findings. It merges every Epoch flag and every historical
`envcheck-tb4-39` label without treating a non-finding as a clean task. No task,
grader, submitted program or reproducer was executed to create this key; paid
cost is **$0**. These are published source claims, not 241 native confirmations
or 241 independent defect mechanisms.

## Contents and identities

- `source.json`: frozen structured source metadata, source hashes/licences,
  original auditor rows, source-preserving joins and reviewed class decisions.
  Raw PM snapshots, prose, task packages and reproducer code are not included.
- `items.jsonl`: one `qa.answer_key/v1` item per PM finding, sorted by `item_id`.
  Stable IDs hash the source section and title; an edited title changes its ID.
- `tasks.jsonl`: the complete **66-task** upstream cohort, including unmatched
  auditor flags and the six tasks with no PM finding. A missing finding or
  "No issues found" is not a certified-negative label.
- `summary.json`: split, class, status and cross-auditor arithmetic.
- `taxonomy.json`: label-free definitions of the pinned 11 task-side and 7
  run-side classes, without task examples, source links or per-finding labels.
- `pm_source.py`, `auditor_sources.py`, `import_sources.py`: static source intake.
  `build.py` reproduces registered outputs offline. `views.py` exports blind
  inputs through the existing `evallab.qa.ask.WorkspaceTools` capability.

The legacy answer-key subject schema is `task|trace`. The three entries with
`scope=benchmark` use the explicit `__benchmark__/terminal-bench-4` **context
carrier**, not a native task ID. They have no package/digest, are development
context only, and are excluded from task exports and task-level denominators.
Their original PM `task` remains `null`. Do not pass the finding key directly to
the older package-per-item `qa task` runner; use `tasks.jsonl` and the blind
export for the open-QA task runner.

Each finding retains its title, original PM type labels (including the one dual
label), port status and exact test/check symbol addresses, source attributions,
GitHub issue/PR references, PM execution claim, finding-level Harbor trial URLs,
source URL, taxonomy review and auditor match status. Task-summary Harbor links
remain on task records, not falsely attached to every finding.

## Split and source binding

The deterministic seed is **20261020**. Sort all 66 task names by
`sha256("tb4-audit-v1:20261020:<task>")`; the first **25 tasks are dev**, the
remaining **41 are test**. All findings and source annotations on a task follow
that assignment. PM reviewed/ported 65 tasks but lists 66, marking
`protein-autointerp-disulfide` not reviewed; retaining the complete upstream
cohort explains 41 rather than 40 test inputs.

There are **86 dev / 152 test task-scoped findings**, plus the 3 dev context
entries. The existing split guard registers all 241 finding items. The offline
builder also binds the full task cohort and rejects finding/task split drift.
The key does not supply a certified-clean set or justify a false-alarm rate or
an unbiased bad-data prevalence estimate.
The global guard also records **17 test finding items overlapping an older QA
Task prompt-development exposure**. This is a sealed input protocol for fresh
actors, not a claim that the cohort is unseen by every existing agent.


Original input packages are digest-bound from the retained v4.0.0 cache. Their
declared upstream commit is `452bf305c6daa62fc59061d22133a7cbc7c1572e`; cache
bytes, not a fresh Git checkout, were verified. The digest identifies the
supplied input bytes, **not a claim that every source finding was reproduced on
those bytes**. Historical EnvCheck labels retain their own v4.0.0 or
`1dcda8716784493721921c23e4bc7f7d988b4494` pins. The exported view contains only
the selected audit surfaces, so its inventory hashes are distinct from the
full original-package digest.

## Auditor agreement, not independent discovery

The three named auditors are PM's own review/execution claims, Epoch and
Tokenless. GitHub reports remain cited evidence, not an invented fourth auditor.

| Cohort / interpretation | 0 named auditors | 1 | 2 | 3+ |
|---|---:|---:|---:|---:|
| 241 findings: source-backed or undisputed PM attribution | 18 | 187 | 33 | 3 |
| 241 findings: direct source-identity links only | 20 | 198 | 20 | 3 |
| 66 tasks: co-flagged, not necessarily the same defect | 4 | 33 | 17 | 12 |

The 18 finding rows with no named-auditor attribution are GitHub-only reports,
not discarded data. An Epoch task flag does not corroborate every finding on
that task. Tokenless's inferred/unresolved matches and the disputed PM
attribution on `sglang-qwen-burst` remain explicit. Many auditors cite the same
GitHub issues; these counts do not establish independent discoveries.

Epoch's table has **30 data rows** (19 FP, 11 FN), not 31: the extra row is its
header. All 39 historical EnvCheck rows are retained: 13 confirmed, 12 later
Epoch overlays and 14 remaining weak negatives. The original sampling was 13
positive / 26 non-finding tasks. All 14 remaining non-findings have positive PM
reports here. In particular, `fin-saccr-rwa` is a PM workbook-check finding; its
old weak-negative label is superseded, not treated as certified clean or as
independent EnvCheck corroboration. The old key is unchanged.

## Class review and port coverage

All 241 shared first-pass labels were reviewed against PM's narratives. The
parent independently adjudicated every changed/uncertain proposal and
spot-checked additional medium-confidence cases. **15 final classes differ
from the first pass**; original, proposed and final labels and rationales remain
separate. Every class has `evidence_status=suspected_source_claim`; no static
review upgrades a class to native-confirmed. The initial infeasible CAD label
was corrected because PM reports passing interpretations. The photonic
continuous-versus-binary score remains an instruction/grader contradiction.

PM claims execution for **137** findings: 136 tested reward hacks by its page
legend and one explicit reward-0 result. Four are explicitly untested; 100 have
no execution claim. `upstream_reward_one_claim` distinguishes reward 1 from
execution alone; none is an execution performed by this importer.

The pinned port is `a3ec9e22725b490a30b35d127bc9e7ac8a28de82`.
**218 findings have full explicit/reviewed test mappings, 7 have partial
mappings, 5 have no matching test, and 11 are not applicable** (including the
3 benchmark-wide findings). All 396 finding-to-symbol links were checked
statically against that tree. Pytest functions, grader functions and registered
check names remain distinguishable. Partial instruction-only associations do
not prove an instruction-text regression test exists. A port status of fixed
is PM's claim about its port, not proof of an upstream fix.

## Rebuild and blind export

```sh
uv run python research/answer-keys/tb4-audit-v1/build.py
uv run python research/answer-keys/tb4-audit-v1/build.py --check
uv run python research/answer-keys/splits/guard.py --check
uv run python research/answer-keys/tb4-audit-v1/build.py \
  --agent-view /private/tmp/tb4-audit-test-inputs --split test \
  --task-root /path/to/original-v4.0.0/tasks
```

`pm_source.py --help` and `import_sources.py --help` describe source refresh
from the retained private snapshots, pinned port, class review and final review
decisions. Source import parses AST/text only, verifies the source tree and
symbol locations, preserves all auditor rows and computes original-package
digests. Normal rebuilds require none of those external files or a model.

An export whitelists only task ID/input digest/package pin and original
`instruction.md`, `task.toml`, `tests/`, `solution/`, `environment/`. It excludes
top-level maintainer READMEs, cheat directories, audit prose, titles, port tests, source
labels and per-task finding counts. A new destination is required; symlinks,
changed packages and changed exported bytes are rejected. Full source snapshots,
this key, split assignments/report and bridge files must stay scorer-side.

**Required harness boundary:** provide only the exported view's inventoried
read/grep/diff capability (or an equivalent confined task runner). Do not give
the auditor a host shell, network, full repository, source caches, or the shared
raw `taxonomy.md`/YAML with task-specific examples. Use fresh actor contexts,
never a key curator's or previously label-exposed conversation. A file layout
is not an OS sandbox. Container-based checks must likewise mount only approved
task inputs, never scorer-side roots. Public/pretraining familiarity is not
ruled out by this workspace boundary. Task-level hit rate is not per-finding
recall; any per-finding scorer must match the actual finding rather than
crediting every label on a flagged task.

## Sources and licences

- [Preference Model issue catalogue](https://preferencemodel.com/blog/terminal-bench-4-is-saturated/issues/),
  checked October 16, snapshot October 20. Local copy:
  `research-context/sources/links/preferencemodel.com/2026-10-19-terminal-bench-4-every-issue-we-found-preference-model.md`.
- [PM overview](https://preferencemodel.com/blog/terminal-bench-4-is-saturated/).
  Local copy: `research-context/sources/links/preferencemodel.com/2026-10-19-terminal-bench-4-is-saturated-preference-model.md`.
- [Fixed port](https://github.com/preferencemodel/terminal-bench-4-karotte-port),
  Apache-2.0, with per-task contributor grants where present. Local copy:
  `research-context/sources/links/github.com/2026-10-19-preferencemodel-terminal-bench-4-karotte-port-terminal-bench.md`.
- [Epoch review](https://epoch.ai/benchmarks/terminal-bench-4/review), CC BY 4.0,
  attribution Epoch AI. Local copy:
  `research-context/sources/links/epoch.ai/2026-10-13-terminal-bench-4-0-0-benchmark-review-epoch-ai.md`.
- [Tokenless EnvCheck findings](https://github.com/tokenless-ai/envcheck-findings),
  commit `12d4d094ef61c4898c16d18ebcc17beafce86035`, data CC BY 4.0, code MIT;
  attribution Tokenless AI. Local copy:
  `research-context/sources/links/github.com/2026-10-09-tokenless-ai-envcheck-findings-reproducible-findings-about-g.md`.

No explicit PM prose licence was observed; this key redistributes only short
titles and factual structured metadata, not its page snapshots or port code.
Cited Harbor links are retained, but their archived JavaScript shells do not
constitute captured trial trajectories. Source hashes, acquisition scope,
licence notes and unresolved discrepancies are in `source.json`.
