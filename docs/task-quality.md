---
status: living
audience:
  - analyst
  - operator
---

# Judging whether a task is good

This manual owns task/package identity, admission, QA, controlled verifier checks,
and training eligibility. It also retains dated measurements with their original limits.
Use [execution tiers](execution-tiers.md) for native runners, capture, and QA Trace.
Use [live watch](live-watch.md) for causal monitoring and unchanged certifier evidence.
Historical receipts never authorize new model or container execution.

A task is useful for evaluation or RL only if its reward means what it says: a pass is a real solve, a fail is a
real failure, and the task sits where the model sometimes passes and sometimes fails. Published datasets rarely
prove this. The FineEnvs MiMo Harbor ports ship no provided reference solution;
recovered history-oracle evidence must be distinguished from an actual graded control.

This page is the order of checks, cheapest first. Each stage only runs on tasks that survived the earlier ones.
Native catalog and qualification contracts appear below.

## Cloud task, issue, and trace evidence

`evallab.production.cloud_trace_db.CloudTraceDatabase` stores four linked
projections: `corpus_tasks`, `task_issues`, `agent_traces`, and `trace_steps`.
Instructions, grader digests, defect directions/statuses, reproducer scripts,
file/line citations, rewards, receipt JSON, full message/action text, and
observation references remain available together. These are ingestion APIs, not
an automatic corpus importer or a verifier: callers must supply real evidence.

Pass a PostgreSQL URL or set `EVALLAB_CLOUD_DATABASE_URL` (preferred over
`DATABASE_URL`). PostgreSQL maintains generated `tsvector` columns and GIN
indexes for native full-text search. A PostgreSQL vector itself cannot exceed
1 MiB, even when its source text fits: documents above 64 KiB bypass that
accelerator and search their **complete native English lexemes** instead.
All query terms must still match the same complete document; stemming,
stopwords, and tail matches are preserved without clipping stored or searched
text. Existing unbounded generated-vector columns/indexes migrate under the
schema lock, preserving all four source tables. An explicit local path or
`sqlite:///` URL uses SQLite FTS5; builds without FTS5 use escaped LIKE searches.
No database URL means a configuration error, not a silent local replacement.

```python
from evallab.production.cloud_trace_db import CloudTraceDatabase

with CloudTraceDatabase() as evidence:
    traces = evidence.search_traces(
        "missing validation",
        defect_class="insufficient_checking",
        tool_name="terminal",
        only_verified=True,
    )
    issues = evidence.query_task_issues(direction="unfair_fail")
```

Search results contain the task, all recorded issue statuses, and every indexed
step in step-index order. `only_verified` requires the supplied receipt's
explicit `status` to be `confirmed` or `verified`; a reward, exploit flag, or
nonempty receipt alone is insufficient. Issue queries default to confirmed
issues. Neither helper independently verifies receipts or authorizes training
on TEST/held-out data; existing verification and split/export gates still apply.
`upsert_task`, `upsert_task_issue`, and `upsert_trace` accept dictionaries named
after the table columns. Supplying `steps=` to `upsert_trace` replaces the full
step set atomically; `replace_trace_steps` also updates the stored step count.

### DEV-only retrieval for worker and issue prompts

`CloudTraceRetriever`, `RetrievedExample`, and
`format_retrieval_prompt_block` live in `evallab.production.cloud_trace_db`.
It exposes nested issue/step/provenance records and the issue-oriented fields
(`defect_class`, citation, measured reward differential, bounded reproducer,
instruction, trace/model identity, and `steps`), plus `to_dict()` and
`prompt_lines(position)`.

**Rendered prompts withhold source and label fields.** `prompt_lines`,
`prompt_text`, `format_for_prompt` (both `full` and `compact_cards`) and
`format_retrieval_prompt_block` never show `dataset_id`, `split`,
`label_categories` or `receipt_status`: the `- source:` line names the issue and
trace only, and the `- provenance:` line omits those four keys (it is dropped when
nothing else is left). The structured example (`to_dict()`, `provenance`, the flat
fields) keeps all four for callers that read fields. Task, trace and issue
identifiers (which can embed a dataset name), instruction, differential, reproducer,
outcome, trust tier and the per-step `FLAGGED` passage marker are still rendered for
QA workers. The block header no longer names the split.


```python
from evallab.production.cloud_trace_db import (
    CloudTraceDatabase,
    CloudTraceRetriever,
    format_retrieval_prompt_block,
)
from evallab.production.corpus_queue import CorpusRegistry

with CloudTraceDatabase() as evidence:
    retriever = CloudTraceRetriever(evidence, registry=CorpusRegistry())
    examples = retriever.retrieve_similar_examples(
        "grader accepts empty output", "task_quality", top_k=3,
    )
    prompt_context = retriever.format_for_prompt(examples)
    issue_examples = retriever.retrieve_similar_examples(
        "grader tamperable check",
        issue_kind="wrong_pass",  # exact defect class or wrong_pass/unfair_fail
        exclude_task_ids={"current-task"},  # leave one canonical task out
    )
    issue_context = format_retrieval_prompt_block(issue_examples, max_chars=2400)
```

The worker kinds are `task_quality` (confirmed issues in either direction,
preferring task-defect-fail traces), `reward_seeker` (confirmed wrong-pass issues
or explicitly marked exploits), and `misalignment` (flagged steps or exploits).
Omitting `kind` removes worker-specific filtering, but still requires an actual
confirmed issue or recorded trace. `issue_kind` and leave-one-out exclusions
compose with worker filters. Issue-oriented queries progressively relax native
full-text matching when few complete matches exist; stored matching evidence
determines the deterministic order. Provisional/refuted issues never contribute.
Real assistant/tool actions outrank journal telemetry, and user context is not
presented as agent action evidence. Receipt status is reported, not inferred.

Admission precedes content reads. The public, pure `admitted_cloud_task_ids`
function is the single structured gate for the retriever and identity-only actor
projections; it returns a `frozenset` of **original raw task ids**. Each input row
requires `task_id`, `dataset_id`, `split`, `original_grader_digest`,
`repaired_grader_digest`, `item_id`, and `item_type`, including explicit nulls for
absent optional identities. Invalid/missing fields or metadata type `invalid`
deny only that row, not unrelated valid rows. SQLite metadata kinds null/None
and text, and PostgreSQL null/string, are supported.

The trimmed, case-insensitive DB split must be exactly `dev` (not `development`).
Recorded and queried item ids, dataset assignment keys, canonical task names
(including namespace/version aliases), and grader/package digests compare with
trim+casefold normalization; immutable source values and provenance stay raw.
Permanent TEST seals always apply in this shared gate; `sealed_task_ids` adds
caller-specific seals rather than being required to preserve the permanent ones.
Known canonical or assignment keys require an explicit DEV assignment. The one
canonical key without ledger rows, `mimo-defect-map-v1`, resolves through its
own inventory: the record id or normalized task name, with heldouts TEST. Once
the ledger carries a key, only its rows authorize DEV. In addition,
`assigned_test_anywhere` denies task/item identities marked TEST under **any**
key, even if the DB row uses another dataset or a task-name DEV alias. This
stricter retrieval guard is separate from secondary keys' local queue semantics.

`CorpusRegistry.has_assignment_authority()` requires a valid, nonempty loaded
split ledger. A missing, empty, or malformed ledger never authorizes content
hydration; source-only registry construction with a missing ledger remains
possible with `split_assignments_digest=None`. The pure gate performs no DB/file
I/O, and the retriever sorts its admitted raw ids before hydration. Storage
`search_traces` and `query_task_issues` accept `allowed_task_ids` for exact,
bound SQL scoping before hydration: `None` preserves their legacy unrestricted
behavior, while an empty collection returns no rows without reading bodies.
Secondary item labels are checked **only as structured identities**, not as
arbitrary prose substrings: a TEST item label `qa` rejects a row whose item id is
`qa`, but does not ban an unrelated trace discussing QA. Secondary TEST **task
names** do join unstructured text seals. Canonical registry sealed item ids and
grader digests remain text-sealed, except bare numeric item ids (CheatBench row
indices such as `44`): as substrings they match inside digests and counts, so
they are sealed only as exact structured identities.

`registry=None` attempts the default `CorpusRegistry()`. If its indices cannot
load, the frozen permanent tb4/MiMo name guard still rejects sealed queries, but
**no examples are returned**: without the registry, no dataset can be positively
verified DEV. A DB DEV label never authorizes a packaged/runtime fallback.

Queries containing sealed identities raise without echoing them, including case
variants and embedded longer tokens. A cached literal-pattern guard screens the
bounded fields eligible for return, not unreturned receipt bodies or clipped
tails. Ranking computes an upper bound per task from SQL term counts (steps of
every role, without reading step bodies), then hydrates and screens candidate
tasks best-first, a `top_k` batch at a time, until no unexpanded bound can
outrank the current results. Only returnable evidence is screened: an expanded
task's issues and traces, then the best-ranked steps of its chosen trace,
skipping sealed ones. Step matches that are never returned may contribute to
rank. Final serialization is checked again, including dataset/CAS provenance.
With SQLite FTS5, term scores come from per-term rowid postings and step bounds
from postings inside each trace's rowid range. Postings, those ranges, and the
identity rows read for admission are reused only while SQLite reports the
database unchanged (`data_version` plus this connection's change count); the
registry decides admission on every call. Unreturned task metadata is not
hydrated, and step flags are decoded only for steps screened for return.

**Trust tiers.** `task_issues` and `agent_traces` carry `trust_tier`, one of
`tier1_container_verified` (container-verified differentials: qualified repairs,
differentially replayed witnesses, patch witnesses with repeat reference/nop
controls, maintainer fixes), `tier2_consensus_labeled` (three-vote judge labels
with passage-level spans, single-run witnessed differentials, human-verified runs)
or `tier3_provisional` (unverified leads). A row whose writer declares no tier is
provisional; re-upserting a row without `trust_tier` keeps the stored tier, and
`assign_trust_tiers` sets tiers on existing rows atomically, touching nothing
else. Older stores gain the columns, provisional by default, when opened.
`retrieve_similar_examples` omits provisional issues and traces entirely unless
`include_provisional=True`; examples rank by their strongest returned tier, then
optional facet similarity (below), then match strength, and prefer their strongest
evidence. A trace with labeled passage steps (or LB-3 `passage_positive` step marks) returns one window:
its best-matching passage step with up to three steps on each side; provenance
reports `passage_window` and `flagged_step_indices`, and prompt lines mark flagged
steps. A list of flag names flags a step; source metadata objects do not. Each
example reports `trust_tier`, prints a `- trust:` line,
and records issue/trace tiers in provenance. Staged publisher bundles stay
provisional. The tier assignment used for the shared store, and its policy, is
`research/evidence/runs/trust-tiers-20261026/`. Terms must co-occur in one
document; when fewer than `top_k` matching tasks hold returnable evidence, every
call (trace-led and issue-led, including `task_quality` and any `issue_kind`)
widens to documents matching any term. Ranking stays tier first, then per-term
strength, so matching more terms ranks higher only within a tier.

At most one example per task and one trace per example is returned. `top_k`,
`snippet_chars`, and `max_steps_per_trace` are positive integers. Overlong issue,
witness, and trace identifiers use stable SHA-256 references; CAS URIs retain
their source identity. The single formatter flattens untrusted text, marks it as
evidence rather than instructions, and drops whole lines to respect `max_chars`
(default 2400, nonnegative). `format_for_prompt` delegates to it; empty inputs
format as `""`. Retrieval does not authorize training/export or verify a receipt.

**Progressive disclosure.** `format_for_prompt(examples, format_mode="compact_cards")`
renders one short playbook card per example instead of every field and step:
`mechanism` (defect class and direction, else the trace outcome, else `unlabeled`),
trust tier, up to three flagged lines (flagged steps, else the issue's reproducer),
a `verify` line (reward differential, citation, passage label), a `flags`
line for trace cards (each fired `compute_trace_feature_flags` lead as
`name@first_step_index` over the whole chosen trace, or `none`; routing leads, not
verdicts), and an `expand` pointer (`trace:<id>:<first>-<last>` or `issue:<id>`).
`RetrievedExample.feature_flags` carries every fired flag's step indices. They are
computed only for returned examples, from steps retrieval already hydrated, with no
extra query.
`CloudTraceRetriever.expand_pointer(pointer)` hydrates that pointer on demand under
retrieval's own rules: admitted DEV task, returnable tier (`include_provisional`
as in retrieval), every returned row free of sealed identities, at most
`max_steps` (default 20) non-user steps, the same bounded fields and `flagged`
marks. Admission and trust are decided from identity columns, inside an
admitted-task SQL scope, before any issue, trace or step body is read. Anything
else raises `ValueError` without saying whether the row exists.
`trace_feature_flags.inspect_step_window(steps, idx, task=...)` likewise refuses
(`PermissionError`) before reading a step unless the trace's identity row is admitted
DEV by `admitted_cloud_task_ids`. It discloses only agent/tool steps (never user or
system prompts), as fresh projections whose command, prose and observation are each
cut to `max_chars` (default 600).

**Run Atlas facets.** `run_facets` stores one `run-facets/v1` record per run, keyed
by `run_id` = `agent_traces.trace_id` (the trace's `run_id` column is a batch id).
`CloudTraceDatabase.upsert_run_facets(records, registry=...)` validates each
record (enums, booleans, word limits, `suspicion` 0–3, at most five distinct
`evidence_steps`) and stores it only if the trace exists, its current task is
registry-admitted DEV, and every evidence step is a stored `step_index` of that
trace. Admission and writes share one snapshot; an optional `allowed_task_ids`
list only narrows admission, never grants it. `source` (`dataset_id`),
`split` and `outcome` (`pass` if reward ≥ 1,
`fail` if ≤ 0, otherwise `unknown`) are copied from the database; the receipt
counts disagreements and refusals by reason. Refused replacements cannot discard a valid prefix,
and a torn UTF-8/JSON append is counted without losing the complete preceding records.

Cluster ingestion stores the **final contiguous build** of the producer's cluster
stream, as the Run Atlas reader selects it. An incomplete build (a level not yet
covering `facet_count` runs, e.g. mid-write) stores nothing. Scoring-time label
fields (`label_rates`, `rule_recall`, `rule_fpr`) are never stored. Runs without
stored admitted facets (for example CheatBench action IDs that have no cloud
trace) refuse the build, or with `drop_unfaceted=True` are removed, sizes recomputed,
emptied clusters dropped, and the dropped counts reported in the receipt.

`replace_run_clusters(clusters, registry=...)` atomically replaces the
`run_clusters` (`run-clusters/v1`) hierarchy: levels 1–3, a level-1 cluster has no
parent and every other one a parent one level up containing all its runs, `size`
equals `len(run_ids)`, and every run has stored facets of its current,
registry-admitted DEV task. `label_rates` stays null until scoring; other keys
(build id, embedder, seed, samples) are kept verbatim as `provenance_json`. Any
invalid cluster keeps the stored hierarchy; concurrent PostgreSQL replacements
serialize under a table lock. `search_runs(facets, registry=..., limit=...)` returns
stored facets matching every filter (`outcome`, `tests`, `eval_awareness`, the
three booleans, `suspicion`, `source`, `split`, `facet_model`; one value or a
collection), most suspicious first; unknown fields or values raise.
Both operations recompute registry admission in the same snapshot as their
reads/writes; an optional `allowed_task_ids` list only narrows it.
`CloudTraceRetriever.search_runs(facets)` uses the same gate and withholds records
naming a sealed identity, e.g.
`retriever.search_runs({"outcome": "pass", "tests": ["modified", "deleted", "replaced"]})`.

`CloudTraceRetriever.retrieve_similar_examples(query, ..., facets=None,
cluster_id=None, like_run_id=None)` also uses Run Atlas:

- `facets` hard-filters traces with exactly the `search_runs` vocabulary and
  validation above. A task qualifies only if it owns an admitted matching trace,
  and the returned trace itself must match. `facets={}` requires a stored facet
  row without adding field constraints; `None` leaves lexical retrieval unchanged.
- `cluster_id` hard-filters trace IDs to that stored cluster's admitted,
  ownership-consistent, sealed-screened members. A missing cluster or one with no
  such members raises the same `ValueError("unknown cluster_id")`, including when
  admission is empty. Facet and cluster filters intersect.
- `like_run_id` ranks candidates by equal values of `outcome`, `tests`,
  `eval_awareness`, `shortcut`, `grader_or_harness_touched`,
  `answer_material_accessed`, `success_claim_without_evidence`, and `suspicion`
  (0–8), plus one for sharing the reference's deepest cluster, when stored.
  Cluster membership is projected onto admitted, ownership-consistent,
  sealed-screened runs before comparing sizes; same-depth ties use the projected
  member count, then cluster ID. Non-admitted membership cannot affect ranking.
  The reference must be an admitted trace with stored facets; unavailable,
  non-admitted, and unfaceted references raise the same `ValueError`. It need
  not match the hard filters. Its own task is automatically excluded, in addition
  to `exclude_task_ids`.

Every Atlas argument also requires `exclude_task_ids` to name the task under audit
(`ValueError("... require exclude_task_ids ...")` otherwise, before any store
read): ranking by run similarity does nothing else to keep a caller's own task out
of its evidence. `exclude_task_ids` is a collection of task ids; a single string is refused.

Ranking is `(trust tier, descending facet similarity, descending lexical
strength, task_id)`: similarity never outranks stronger trust. Within a task,
the strongest matching trace tier wins, then its most similar trace, then the
existing evidence order. Without `like_run_id`, similarity is zero and the
existing ordering is unchanged. Only an Atlas request adds trace provenance
`facet_similarity` (integer, zero when unused) and `run_facets_matched` (true).
Default calls retain their existing provenance and rendered output.

Any of these three arguments requires trace-backed evidence, including
`task_quality` examples: an issue-only task or one without a matching,
returnable trace is omitted. An empty or punctuation-only `query` then lists
matching admitted tasks with lexical strength zero; without an Atlas argument
it still returns no examples. For example:

```python
examples = retriever.retrieve_similar_examples(
    "",
    facets={"outcome": "pass", "tests": ["modified", "deleted", "replaced"]},
    exclude_task_ids={"current-task"},  # required with any Atlas argument
    top_k=5,
)
```

Admission, facet/cluster reads and evidence hydration share one read snapshot;
an empty admitted scope reads no facet or cluster bodies. Existing sealed-identity
screening remains in force. Best-first expansion uses precomputed per-task facet
similarity bounds alongside lexical bounds, including conservative bounds when
screening can expose a different trace.
Partial-match widening likewise counts only matching traces at allowed trust
tiers for Atlas requests, never unrelated traces or issue-only evidence.

Only explicitly verified/confirmed trace receipts with confirmed issues are
eligible; the current task is excluded. Output contains at most eight compact
projections and 4500 rendered characters, not full instructions, reproducer
scripts, or CAS objects. Receipt-status filtering is not independent evidence
verification, and exact-ID exclusion is not task-family deduplication.

**Retained DEV exports.** The native CI inventory still exercises these commands:

```bash
uv run evallab prod export-runs --help
uv run evallab prod export-cheatbench-runs --help
uv run evallab prod export-cluster-scope --help
```

The exporters retain their admission checks, blinding, and separate sealed manifests.
QA-data, actor-bundle projection, and the production import commands are retired.
Stored databases, facets, findings, clusters, and CAS objects remain unchanged.

`S3CompatibleCAS` stores exact bytes locally under `cas://sha256/<digest>`.
It verifies the SHA-256 digest before returning bytes.
Pass `local_directory=` to select the local CAS.
`put_bytes` / `get_bytes` preserve raw traces or binary task packages.
`put_bundle` / `get_bundle` handle deterministic JSON bundles.
Remote S3/R2/Tigris upload and retrieval are no longer supported.
CAS references do not assert that the corresponding object has been ingested.


### Retained causal descriptions

The Task Quality live worker and swarm coordinator are retired.
Existing candidate receipts retain their original citation checks and provisional status.
A model's verification flag never establishes measured correctness.
Retained citation utilities and source descriptions do not admit or promote an asset.

### Goodfire passage labels on steps

Goodfire's publisher judge marks the exact passages where a rollout cheated. Ingestion
kept every passage record (text removed) on the step it addresses as
`trace_steps.flags_json.source_passages`; `consensus` is `positive`, `ambiguous` or
`negative`. LB-3 derived flat marks from `consensus` for the 1,048 DEV Goodfire tasks:
on every step that has a passage record, `flags_json.passage_positive` and
`passage_ambiguous` (booleans) and `passage_labels` (counts, channels, vote strength);
on every judged trace, `verified_receipt_json.passage_step_labels` with the exact
`positive_step_indices`, `ambiguous_step_indices` and the runs of consecutive assistant
turns (`positive_turn_runs`: `[first, last, turns]`). Positive steps are always assistant
turns and are never adjacent by step index, so adjacency is taken over assistant turns.
Derived counts equal the publisher's own `n_passages`, `n_passages_positive` and
`n_passages_ambiguous` on every judged trace. Labels stay provisional publisher
three-vote LLM-judge source labels; `status` and `is_exploit` are unchanged; held
Goodfire rows carry no marks. Query a step mark with
`json_extract(flags_json, '$.passage_positive') = 1`. Every DEV Goodfire step has a
non-empty `flags_json`, so a test such as "`flags_json` is truthy" marks all of them;
consumers that want the cheat window must read `passage_positive`. The updater and its
receipts are archived under `research/evidence/runs/corpus-expansion-lb3-20261026/`.

LB-5 measured the current Stage-1 tripwires against these marks (read-only, DEV only,
`research/evidence/runs/corpus-expansion-lb5-20261027/`). Two cautions for anyone reading the
marks as a recall target: the label also marks passages where the agent only mentions or
declines a shortcut (use `verified_receipt_json.source_label.verdict_enacted == "yes"`), and
76.6% of positive rows are in the reasoning channel, where command rules cannot see them. On the
664 `tool_calls` positives of enacted-yes traces the live rules catch 16.0% (verdict-class) and
28.9% (with diagnostic rules); the verdict-class rules alarm on 32.8% of clean traces.

## Corpus intake boundary

The broad corpus downloader, local staging frontend, and swarm runtime are retired.
Keep their staging objects, clearance manifests, queues, databases, CAS, and immutable receipts.
No retired import example is a supported launch route.

Admission still requires independently established canonical identity, duplicate family,
split authority, and the rights needed for each use.
`operator_raw`, `actor_retrieval`, `scorer`, `derivatives`, `training`, and `redistribution` remain separate rights.
A public URL, code license, or inherited DEV label grants none of them.
An unknown or conflicting identity denies content access.
An actor store must remain physically separate from raw SQLite, WAL, backups, quarantine, and whole CAS.
Only an admitted, reviewed field allowlist and its reachable CAS objects can enter actor context.
Citation-only labels are not verified training backing.

The [immutable LB-1 archive](../research/evidence/runs/corpus-expansion-lb1-20261025/README.md)
retains historical importer bytes and the frozen database dependency.
Do not execute those tools. Their Wrench clearance is retracted and the removed rows are quarantined.
The recorded inventory contains 3,684 held identities with zero established canonical/family grants and zero per-use allowances.
These are raw inventory counts, not production-eligible counts.

## Retired dataset audit frontend

The generic `evallab audit` frontend is retired.
Use each command's current `--help` and bind every result to exact package bytes.
Keep existing audit projections, manifests, dossiers, and ledger decisions unchanged.
An old `keep` route is not certification, task admission, validated repair, or a model-capability result.
The historical MiMo acceptance is source-pinned, not a permanent verdict count.
The ledger at `9ed0e33495daa75d7f0dff6d45f15a7da79fe468` has SHA-256
`85d71a98cca748d49a41d75b77a520bb69f7d429dc36fa9cdee21e5f5d97223d`
and routes 1,180 unique tasks as **1,146 keep / 2 fix / 32 discard**.
The later committed ledger has SHA-256
`47f4f7f9cae7a12ea04b2896345b1fc1da1aa2ef8fc5ec595ce5215dc1bbb020`
and routes the same cohort as **1,147 keep / 1 fix / 32 discard**.
A dry-run reports the selected source revision honestly; never edit verdicts to
force the historical count.

The $0 integration receipt is
[`research/evidence/har195-audit-integration-20261017.json`](../research/evidence/har195-audit-integration-20261017.json):
both full CLI replays, a fresh original-image/native-grade smoke, the real
`hello-world@1.0` oracle/nop/leak/static/history audit, SQL and dossier reads, and
the native viewer filtered by `verdict:keep`. Its local archive retains failures
as well as the successful final controls.

## Reward-hacking audit (`evallab hack`)

The dataset audit answers "is this task worth keeping". `evallab hack` answers a
different question: *which channels let a scripted agent score without doing the
work*, and whether an authored exploit actually scores through the task's own
verifier. Both halves are free and local; neither spends model tokens unless the
separate `--stages exploit` probe is requested.

- `evallab hack scan <package>` — deterministic, offline V1–V8 ledger over one
  task package: verifier isolation, answer material in the agent build, untrusted
  execution in the verifier, LLM judges, weak matching, fail-open handling,
  granted authority (network, user, privileges), plus reconnaissance facts
  (verifier mode, entrypoint, digests). Each finding cites `file:line`; findings
  are **claims**, and an absent finding is not a certificate.
- `evallab hack run <package> [--script PATH ...] [--execute]` — replays each
  exploit script through the task's own verifier by reusing the checked-in
  solution-control matrix path (`MatrixRun.solution`, oracle agent,
  `allow_billable=False`, so it can never spend). The matrix is the same artifact
  class as `research/experiments/release-branch-rescue-local-controls.json`:
  reference oracle (expect 1), no-agent control (expect 0), then one run per
  script, with `# expect: 1` in the script's first 20 lines recording the
  author's prediction. Task lint and this runner read that header with the same
  parser, so a header lint accepts is the header the runner reads. Without
  `--execute` it writes the plan only.
- `evallab hack mutate <package> [--target PATH ...] [--max-mutants N] [--workers 1|2]
  [--mutant ID ...] [--no-coverage] [--execute]` — verifier mutation testing for the
  insufficient-checking family (a verifier that rewards a wrong answer). It
  captures the reference solution's target files (declared `artifacts`, or
  `--target`) through `/logs/artifacts`, then grades one blank control per file
  and a balanced sample of one-edit mutants (comparison boundaries, `and`/`or`,
  `+`/`-`, a dropped operand, `min`/`max`, conditional branches, negated flags
  and conditions, dropped statements, one reverted hunk of the reference
  solution's change; Python by AST, C-family lexically, JSON by value) with the
  same oracle-channel matrix path. A mutant that keeps full reward while the
  file's blank control scores 0 is a **survivor**: a reproducible input written
  as `inputs/<id>/<id>.patch` against the reference output, recorded in
  `hypotheses.jsonl` with `status: candidate`. It may be behaviour-equivalent or
  change behaviour the instruction does not require, so a human or an objective
  check confirms it before it becomes a task finding.

  **Coverage-guided** (default; `--no-coverage` turns it off). When a captured
  target is Python, one more reference run grades through an instrumented copy
  of the package (`<run>/coverage-package/`): a prologue atop `tests/test.sh`
  hooks a line tracer (`sys.monitoring`, `sys.settrace` before 3.12) into every
  Python 3.8+ interpreter it finds, in shared and separate verifier modes alike,
  and each verifier process writes the executed lines of the targets to
  `/logs/verifier/envcheck-coverage/` (`src/evallab/verifier_coverage.py`).
  Lines count only for the exact reference bytes (SHA-256), so a fail-to-pass
  baseline run on the original code cannot mark them. Mutants on lines the
  hidden tests never execute survive by construction and are not run
  (`files[].unexecuted`); `files[].untested_fix_lines` lists reference-fix code
  the tests never run, and one **partial-fix** probe per file (the reference fix
  minus those hunks) runs first: if it keeps reward, the grader accepts a
  partial solution. The trace is used only when the instrumented reference
  scores 1 and at least one target ran under the tracer; otherwise
  `coverage: unavailable` (reason in `notes`) and every line is a candidate.
  Limits: Python targets only; a verifier that runs the code in an interpreter
  the prologue did not find, or replaces `sys.settrace` (pytest-cov on <3.12),
  falls back to unguided.

  Native matrix validation replays digest-checked reference and nop first;
  failed controls stop before spending time on survivors. Task admission needs
  an executed, full-reward survivor, but validation then replays **every**
  previous survivor, including unexecuted fix omissions and partial survivors.
  The ranking filter must not shrink the acceptance subset. Only complete
  **1/0/all-0** yields `repaired` and validated lineage. Equivalent/out-of-scope
  survivors may leave a `partial` candidate for review; they never justify
  source fingerprints or incidental-behavior assertions. Other outcomes are
  `rejected`, `unrepairable`
  and `blocked`. Receipts include the private Codex transcript/usage, proposal,
  derived record, validation matrix and `repair-result.json`.
  Passing the numeric gate does not establish that every added assertion is in
  scope. Check the assertions against the instruction before accepting a task
  variant for training; unrelated API tests cannot turn a survivor into a defect.

Also see `src/evallab/ref_subset.py` (`probe_task`), `src/evallab/verifier_repair.py`,
and `v_task_outcomes` in `src/evallab/task_catalog.py`.

## Stage 2: rollouts (model cost, needs approval)

Run k attempts per task (4 to 8) with one pinned agent and model. `v_task_outcomes` gives the verdict per task
version and agent/model, with the Wilson 95% interval on the pass rate:

| Verdict | Meaning | Use |
|---|---|---|
| `learnable` | some attempts pass, some fail | RL signal; keep |
| `always_pass` | every scored attempt passes | no RL gradient; fine as a regression check |
| `always_fail` | every scored attempt fails | too hard or broken; audit before keeping |
| `infra_only` | no attempt produced a reward | infrastructure or setup problem, not a model result |
| `untested` | no trials | run it |

Errors are not zeros. A missing reward (setup failed, judge down, timeout in grading) must never be counted as a
fail, and must never become reward 0 in a training batch. Control agents (`nop`, oracle) get their own rows and
never count toward a model's verdict.

## Stage 3: audit the verdicts

- Flaky grading: rerun the verifier on the same final state with
  `--verifier evallab.harbor_repeat_verifier:RepeatVerifier --verifier-kwarg repeat_n=3`
  ([verifier stability](#verifier-stability)). A verdict that changes between reruns is noise; the MiMo report (§4.2.1) used 8.
- False passes: `src/evallab/mimo_exploit.py` holds the retired exploit schema (`task_exploits.parquet`).
- False fails: for `always_fail` tasks, check whether the instruction and the hidden tests agree.

`v_task_audit` combines these: `train_eligible` requires `learnable`, stability evidence that says `stable`, and
no confirmed or unreviewed suspected exploit. A version with an `error`-severity curated finding
(`library/task-findings/`, evidence-backed and reviewable) is train-ineligible first, with reason
`finding: <rule>` — a hard defect outranks every other reason.

## Stage 4: behavior

Turns, tokens, repeated tool calls ((N − U) / N over identical calls in a turn), and error classes require a complete trajectory before these numbers are trusted. On the Terminus-2 lane summarization and handoff calls never appear as trajectory steps.

## Changing a task

A fixed or rewritten task is a new version, never an edit in place. See [task variants](#task-variants-and-exact-package-identity): the original
stays as the control, the variant records its parent, transform and changed files, and runs join to the exact
version they used.

## Package lint and retained audit readers

`evallab.task_lint` owns read-only package metadata and security checks.
Workbench scan, plan, control execution, and packet generation are retired.
Registry retains publication and exact-package hash binding.

A clean package check is not a Gold certificate. Certification uses strict G1–G7.
A package-check packet never supplies a missing Gold result.

## Lightweight lint

```bash
uv run evallab tasks lint path/to/task --json
```

A path may be one task or a collection root. Collection roots discover nested
task packages at any depth; hidden directories are skipped, and a task's own
subdirectories are never searched.

This command checks verifier separation, declared artifacts, solution presence,
control expectation headers, and hidden inputs in Docker `COPY`/`ADD` operands.
Operands are resolved against the task's `environment/` build context: a source
that names `tests` or `solution` but leaves that context, follows a symlink out of
it, or matches nothing inside it is an error. It reports unsupported syntax instead
of silently accepting unreadable instructions.

It also parses task sources without executing or writing them, and reports a
`sources-parse` error that names the file and the parser message: `bash -n` on
every `*.sh` under `solution/`, `controls/` and `tests/`, the builtin `compile()`
on every `*.py` under `tests/` and `solution/`, and `json.loads` on every `*.json`
under `tests/`. Any `bash -n` diagnostic is an error, including a warning without
a failing exit. When `bash` is not on `PATH`, shell files get no finding.

Only tasks with `environment_mode = "separate"` and declared artifacts receive
these warnings: `artifacts-instructed` when `instruction.md` is missing or does
not name each declared artifact path as its own path; `artifacts-verified` when
`tests/` is missing or no file under it names an artifact, a parent directory,
or a path below it; and `verifier-reads-undeclared` when a tests file names an
absolute path at or below an artifact's top directory (`/app` for `/app/x`) that
is neither the artifact, above it, nor below it. These warnings leave the exit
code at 0; other rules' errors still fail.

## Retained packet and receipt readers

Registry continues to validate historical `certification.json` packets against exact package hashes.
Its `--certification-packet` flag remains available. Existing packet kinds, versions, and hashes remain unchanged.
This binding does not grant Gold certification or publication permission.

The readers preserve raw rewards, unknown outcomes, manifest coverage, archive identity, and provenance limits.
Stored evidence and receipts remain intact. See [registry and publication](#registry-and-publication) for the publication boundary.

## Registry and publication

## 1. Principles

1. **Explicit-Only Admission**:
   Filesystem location, the presence of `task.toml`,
   or inclusion in `policy/canary-suite.yaml` **never implies registration**. A task is
   registered if and only if an explicit, valid, human-approved JSON record exists in
   `library/registry/<task_id>.json`.

2. **Immutable Integrity Binding**:
   Every registered task record binds the exact cryptographic digests (SHA-256) of its
   `task.toml`, instructions, environment, verifier, and full package directory. Any byte
   modification on disk invalidates admission and halts dispatch.

3. **Promoted Control Evidence**:
   Every registered task must cite durable, promoted Oracle (exact reward = 1.0) and Nop
   (exact reward = 0.0) evidence files by repository-relative path, cryptographic SHA-256
   digest, and UTC observation timestamp. Invented job names or unpromoted runs fail closed.

4. **Canonical Resolution**:
   Experiment specs claiming `registered/<task_id>` are resolved into canonical
   `task_path`, `task_version`, `verifier_digest`, `package_digest`, and limits. Omitted
   `task_path` automatically resolves to the canonical registry path. Redirection away from
   the canonical path raises `TaskPathRedirectionError`.

5. **Allowed Uses Enforcement**:
   Tasks declare permitted uses (`measurement`, `training`, `canary`, `heldout`, `foundry-seed`).
   Training-only tasks cannot be executed for measurement. The researcher loop preflights
   the explicit registry before invoking any LLM agent.

6. **Human-Gated Registration**:
   Promoting a task from `candidate` to `registered` requires human approval
   (`approved_by` and `approved_at`) plus verified control evidence. Peter Makhnatch owns all
   registration decisions.

7. **Independent Canary Policy**:
   Canaries operate under `policy/canary-suite.yaml` to ensure repository health and
   prevent chicken-and-egg bootstrap cycles. Canaries do not require registry admission.

### Fields & Validation Contract

| Field | Type | Description | Invariants |
|---|---|---|---|
| `schema_version` | `1` | Schema version | Must be `1` |
| `task_id` | `str` | Stable task slug | 3–80 lowercase alphanumeric characters / hyphens |
| `version` | `str` | Task version string | Non-empty |
| `task_path` | `str` | Repo-relative path | Must exist, must not escape repo, must have task.toml, instructions, environment, verifier |
| `digests` | `TaskDigests` | Package & component SHA-256 | Must match on-disk file bytes |
| `source_ref` | `str \| None` | Upstream commit/ref | Pinned immutable SHA or release tag if `provenance_zone == "01-external"` (no floating branches) |
| `license` | `str \| None` | Declared license | Required if `provenance_zone == "01-external"` |
| `provenance_zone` | `Literal` | Provenance zone (`01-external` ... `04-curated`) | Defined per docs/data-architecture.md |
| `is_synthetic` | `bool` | Whether generated or adapted | Boolean flag |
| `limits` | `TaskLimits` | Execution constraints | `timeout_seconds` between 1 and 21600 |
| `control_evidence` | `TaskControlEvidence` | Oracle and Nop verification runs | Oracle reward must be exactly 1.0, Nop reward exactly 0.0, evidence files must exist, match digests, and parse cleanly |
| `state` | `Literal` | `candidate`, `registered`, or `retired` | Distinct states |
| `allowed_uses` | `list[Literal]` | `canary`, `measurement`, `heldout`, `foundry-seed`, `training` | Unique items, min length 1 |
| `approved_by` | `str \| None` | Approver identity | Required if `state == "registered"` |
| `approved_at` | `datetime \| None` | Approval timestamp (UTC) | Required if `state == "registered"` |

---

## CLI Interface
### Audit Registry
```bash
# Run comprehensive read-only audit
uv run evallab registry audit

# Output JSON audit report
uv run evallab registry audit --json
```

The audit verifies:
1. Every registered record exists on disk with matching file bytes and verifiers.
2. Control evidence exists, matches SHA-256 digest, and proves exact Oracle=1.0 and Nop=0.0.
3. External records have declared licenses and pinned revisions.
4. No queue proposals claim unregistered or candidate tasks (`false_registered_claim`).
5. No queue proposals attempt `task_path` redirection or version/verifier mismatches.
6. Malformed JSON in queue specs or registry files is reported as error findings without swallowing parse errors.
7. Curated cards that are pointer-only documentation are identified.


The authoring pipeline, proposal sampler, and training-pool exporter are retired.
Keep quarantined proposals, registry records, evidence, and existing training indexes.
A structural battery or CRAFT score is not a measured oracle/nop control.
Only the human-owned registry path can admit a task.
Historical generated placeholders never become supported task packages through this consolidation.

## Task variants and exact-package identity

## Why variants exist

Two very different workflows both produce a *modified task*:

1. **Optimizer rewrites** — GEPA/DSPy propose a new instruction for a task the
   model keeps failing. The instruction is the treatment under study.
2. **Hand fixes of broken tasks** — an audit finds a verifier that crashes
   before grading or an environment that never becomes healthy. Fixing it
   changes the task's meaning and must not silently replace the original.

In both cases the original task stays the control. Every reward, error rate,
or pass-rate comparison is only attributable if each side of the comparison
resolves to one exact, content-addressed task package. So a modified task is
never edited in place: it becomes a **variant** — a full derived package plus
a small git-tracked **lineage record** linking it to its parent (which may
itself be a variant; chains are allowed).

## Storage split

| What | Where | Git |
|---|---|---|
| Lineage record (schema `evallab.task_variant/v1`) | `library/task-variants/<task_slug>/<digest12>.json` | tracked, reviewable |
| Materialized variant package | `<derived root>/task-store/variants/<task_slug>/<digest12>/` | ignored (rebuildable) |

`<task_slug>` is the task name with `/` replaced by `__` (e.g.
`mimo-v2.6-rl__candidate-0109-science-robotics`). The variants store resolves
through `evallab.storage.paths` to the primary checkout's `derived/`
directory, so every worktree shares one store; the records live in whatever
checkout derives the variant and are committed from there. Only changed files
are inlined in the record (≤1 MiB, UTF-8), which keeps records reviewable
even for large packages.

## The record

One JSON object per variant: variant and parent digests (both
`registry.task_directory_digest`, plus the Harbor lock digest
`registry.harbor_task_digest` for each), parent provenance (`hf` with repo +
40-hex revision + path, `variant` with the parent record's repo-relative
path, or `local`), the transform as `name@version`, the changed components
(`task_toml`/`instruction`/`environment`/`verifier`/`solution`, classified by
comparing `registry.compute_task_digests` before and after), per-file
before/after digests with the after-bytes inline, rationale, free-form
inputs, author, UTC timestamp, and a `status` of `candidate`, `validated`, or
`rejected`.

Integrity invariant: `materialize(parent bytes + record files)` reproduces
`variant_digest` exactly — this is proven on every rebuild, so a deleted
store directory is recoverable from Git plus the parent package alone.

## Guarantees

- **Never overwrite.** Deriving refuses if the record or the package already
  exists; identical inputs deterministically produce the same digest, so a
  re-derive is a refusal, not a fork.
- **Refusals.** Binary or >1 MiB changed files, symlinks in the parent,
  relative paths that escape the package, deletions of files the parent does
  not have, and no-op derivations (variant digest == parent digest) are all
  rejected with explicit errors. A variant starts as `candidate`, and append-only evidence moves it to `validated` or `rejected`. Identity fields are immutable
  and verdicts are final; a reversed decision requires a new variant derived
  from the same parent, so scores stay attributable to one exact package.
- **Read-only parents.** Pinned snapshots under `derived/task-store/hf` are
  read-only; the copy a variant starts from is made owner-writable, and the
  parent is never touched.

MiMo code tasks run `environment/setup/setup.sh` from a gzip+base64 tar
embedded in `task.toml`'s `[environment.healthcheck]` command, not from the
files on disk, so a setup change must re-embed that payload in the same
variant. The archived utility
`research/experiments/har105-exploration/setup_payload.py` encodes it byte for
byte as the adapter does (`gzip.GzipFile` with `mtime=0` and an empty filename).
It checks that the on-disk setup reproduces the embedded one before a change.
Restore the utility from `archive/pre-round3-2026-11-12`.

### Setup-only Python lookup repairs

`setup-python-path@1` repairs a frozen `purge-installed-copies@2` package
whose setup helpers still resolve the image's legacy project Python. It
adds the canonical system-Python PATH inside the setup child process and
re-embeds that setup in `task.toml`; it does not rebuild or relabel the
frozen v6 parent. Derive it through `derive_task`, `pack_setup`, and
`render_task_toml`, retaining the exact parent record and before/after hashes.

For held-out 001948, only `environment/setup/setup.sh` and `task.toml`
change. Every `tests/**` byte, assertion, reference solution, instruction,
and pinned image stays unchanged. Native Modal verification also requires
the existing `TaskModalEnvironment` lifecycle: its scoped modern helper PATH
runs the unchanged verifier setup before submitted artifacts are seeded;
the project's grading interpreter and PATH are not globally replaced.

This is an environment repair, not `verifier-repair@1`. Its new package and
Harbor digests require fresh exact-digest controls; the parent's controls do
not transfer. Structural replay or oracle/nop controls alone do not certify
wrong-submission rejection or authorize scoring.

Python API: `derive_task`, `materialize`, `verify`, `load_records`,
`lineage_chain`, `append_status_evidence` in `evallab.task_variants`.

For filtering historical jobs by task health, see [execution tiers](execution-tiers.md#filter-historical-jobs-by-task-health).

Variants live in zone `03-synthetic` (rebuildable from Git plus the parent package).
The old proposal/admission automation is retired.

## Task provenance

## Why unknown is honest

Matches preflight style: reports `UNKNOWN [unavailable]` with reason instead of inventing a value. A confidently wrong label would silently corrupt every cross-corpus comparison.

## Downstream use

When comparing results, filter or stratify by `origin` and `family`. A `harbor-native/terminal-bench-3` trial and a `local-lab` trial are not interchangeable evidence; the field makes the distinction queryable and auditable.


## MiMo catalog, qualification, and health

The native catalog and readers remain supported; the `mimo-clean` and `mimo-census` producers are retired.
The following source contracts preserve backend, measurement, and split boundaries.
The qualification collector and task-health census are retired. The following sections describe stored evidence.

## Storage

All runtime data lives under the shared checkout root resolved by
`evallab.storage.paths.shared_checkout_root` (never inside a worktree).
Two different `derived/` trees hang off it — snapshots are NOT under the
Parquet derived root:

- `<shared>/derived/task-store/hf/<org>__<repo>@<rev12>/` — pinned
  snapshots, read-only (`555`/`444`), one `provenance.json` per snapshot
  (`ProvenanceMetadata`, zone `01-external`).
- `<parquet-derived>/external/task_catalog/` — `task_sources`,
  `task_versions`, `task_findings`, `task_lineage` Parquet tables. (The
  resolved derived root already ends in `/parquet`; the catalog path is
  `external/...` relative to it.)
- `library/task-variants/**/*.json` — git-tracked lineage records
  (`evallab.task_variant/v1`, owned by TaskVariants).

## Tables

- `task_sources`: one row per pulled snapshot (repo, revision, domain,
  manifest/disk/registry counts, license, material digest).
- `task_versions`: one row per task, keyed by `task_version_digest`
  (`registry.task_directory_digest`) with `harbor_digest`
  (`registry.harbor_task_digest`) — the join key to Harbor `lock.json`
  `trials[].task.digest` and to `trial_facts.task_digest`.
- `task_findings`: one row per `(task_version_digest, rule)` with
  severity and message (`src/evallab/task_lint.py`), plus curated, evidence-backed
  defect records from `library/task-findings/**/*.json` (schema
  `evallab.task_finding/v1`: digest, task, domain, rule, `error`|`warning`
  severity, message, `{path, sha256}` evidence, recorder and time).
  Invalid records are skipped with a count, never a crash. An
  `error`-severity curated finding is a hard defect: it forces
  `train_eligible=false` with reason `finding: <rule>` in `v_task_audit`.
- `task_lineage`: parent/child digest links from lineage records
  (`variant` origin rows also appear in `task_versions`).
- `task_qualification`: one row per Harbor trial with backend health.

`grader_kind`/`grader_cost` are derived from task files, never the domain
name: a `*JUDGE*` verifier env or judge-graded `tests/grade.py` means a
paid model judge (`vlm_judge` when vision markers are present, else
`llm_judge`); everything else is a `script`/`free` grader.


## split_group

Stable family key so sibling tasks never straddle train/held-out:

- `code`: repository identity from graded test targets — tier 1:
  module-style `go test` targets (visible command, patch text, and shell
  scripts embedded in the patch as `mimo_build_env.tar.gz.b64`); tier 2: Go
  import paths in the patch (test-helper modules excluded); tier 3: hosted
  code URLs in the instruction and task description. This is the audit's
  go-import / test-import / issue-link union as one rule: test-target
  signals outrank import signals outrank link signals; anything with no
  signal stays a singleton (`split-group-unresolved`). Format-style tasks
  carry no repo signal in any file, so most stay unresolved by design —
  never merged silently.
- `cyber`: ARVO project (first component of `expected_crash.file`). M0:
  the 194 exact-duplicate instruction groups (501 tasks) are all
  project-pure, so the project key already keeps every group together;
  the builder verifies this per build and merges any future
  cross-project dupe group under `cyber:dupe-<inst12>`.
- `terminal`/`webdev`/`music`: task id until near-duplicate analysis lands.

Docker image digests are per-task unique in this collection (64/64
terminal, 1000/1000 cyber, 2698/2698 code), so they carry no family
signal; general/webdev/music share one image per domain (correctly ignored).

## Outcome joins

`storage/attach.py` registers `v_task_outcomes` (per task_version ×
backend × agent/model: n_trials, n_scored, n_errors, mean reward, Wilson 95%
pass-rate interval, verdict) and `v_task_audit` (same grain plus
verifier-stability, exploit, and backend-qualification rollups with
train-eligibility reason codes). `backend` is
`COALESCE(trial_facts.environment_type, 'unknown')`, read from each trial's
Harbor config (`$.environment.type`, with Eval Lab Daytona wrapper import
paths mapped to `daytona`); old trial_facts partitions without the column
read as `'unknown'`, so local and cloud trials never pool into one row.
The legacy `n_infra` count means no usable primary reward (missing, nonfinite,
or -1), not that an exception proves an infrastructure failure. A verifier
score remains in the denominator even if the agent stopped with an exception.
Verdicts: `learnable` (some pass, some fail), `always_pass`,
`always_fail`, `infra_only` (no scored trial), `untested`. Outcomes are
grained by (task_version × backend × agent/model), so nop/control trials join as
their own rows and never mix into a model's pass rate: they cannot make
a task look learnable, and a uniformly-failing nop row is simply not
learnable (control, not training signal).

([verifier stability](#verifier-stability)). The catalog still reads existing `task_exploits`
evidence. The duplicate `exploit-collect` command and its writer are retired.
No stored exploit evidence is removed. `train_eligible` =
`learnable` ∧ stability evidence that is `stable` (no evidence is
ineligible) ∧ no `confirmed` exploit and no `suspected` one still awaiting
review ∧ not broken on that backend (see below) ∧ no `error`-severity
curated finding (see above). The finding check comes first: a version with
curated finding is train-ineligible with reason `finding: <rule>`.

## Repeat verification

Run Harbor with `RepeatVerifier` ($0, local Docker):

```bash
harbor run --path <staged-task> --agent nop \
  --job-name <job> --jobs-dir runs/ \
  --n-concurrent 1 --n-attempts 1 \
  --verifier evallab.harbor_repeat_verifier:RepeatVerifier \
  --verifier-kwarg repeat_n=3 -y
```

`PYTHONPATH` must include Eval Lab's `src/` so the Harbor controller can
import the verifier.

## Verifier stability

Repeat-verification audit for MiMo-V2.6 Harbor tasks: after the agent
finishes, the task's own verifier runs **k times on the same final container
state**, and every run is recorded. The trial reward is always the FIRST
run's reward, so the audit never changes the result it measures.

## Cost impact

k× verifier wall-time per trial. All script-graded MiMo domains
(code/cyber/terminal/music) grade for **$0**, so repeat verification is free
apart from local Docker time. The judge-graded domains (general = LLM judge,
webdev = VLM judge) would cost k× judge calls per trial and are excluded
from $0 sampling.

## What each run records (`verifier/stability.json`)

Per run: `reward` (or null when no reward file was written — a testbed
failure, never a 0), `error`, `duration_sec`, `exit_code` (test script's
return code, observed from the verifier's exec calls), and
`stdout_tail_sha256` (sha256 of the last 8 KiB of `test-stdout.txt`).

## Verdicts

- `stable` — all runs produced a reward and all rewards are equal.
- `flipped` — all runs produced a reward but rewards differ.
- `errored` — any run lacks a reward (missing evidence counts as errored).

## `diff_replay` recipe (offline reruns, no container retained)

This is a manual recipe, not a supported collection driver.
`task_stability.py` keeps only the stored table name for catalog readers.

1. `docker run` the task's pinned image (amd64).
2. Replay the one-time setup from the task's `[environment.healthcheck]`
   command (unpacks `/var/lib/mimo`, writes `/var/lib/mimo/base`).
3. For code tasks: restore the agent's end state with the trial's saved
   `verifier/agent.diff` (`git apply` in the workdir), then run
   `tests/test.sh` k times and record each `reward.txt`.
## Legacy task admission records

The admission writer is retired. Stored admission records and task evidence remain unchanged.

The retired gate checked a task package directory with an offline static scan,
$0 oracle/nop controls, then the full 14-attack cheat ladder, then the
hollow-program battery, then a coverage-guided verifier-mutation probe —
in that order, stopping at the first non-passing step, always writing a
digest-bound admission record (`evallab.task_admission/v1`, gate
`tasks.admit/v3`).

## Steps (cheapest first)

| # | step | pass iff |
|---|---|---|
| 1 | `static-scan` (`hack scan` in-process) | the scan executes; findings are recorded as advisory evidence, never a veto (see below) |
| 2 | `oracle` (stability-job path, repeat 3) | every trial reward is exactly 1.0 — via the shipped `solution/solve.sh`, or via the HAR-191 reference (see below); `unproven` when neither exists |
| 3 | `nop` (stability-job path, repeat 3) | every trial reward is exactly 0.0 |
| 4–17 | `cheat:<attack>` (one single-trial cheat job per attack, full 14-attack ladder) | reward < 1.0 (the lane rule: cracked iff reward >= 1.0) |
| 18 | `battery` (retired hollow-program checks) | every fake scores 0 and every rewrite control scores 1 |
| 19 | `mutation` (`hack mutate` audit, `--max-mutants` 12) | every scored mutant is killed (see below) |

Each executed step records command, job dir, reward(s), duration, and
(for oracle/battery/mutation) the reference provenance. Verdicts: `admitted`
(all pass), `rejected` (a step proved the task bad, including a hollow
fake that kept reward), `needs_review` (a mutant of the fix kept reward,
or a meaning-preserving rewrite lost it — a hypothesis for a human, never
an auto-flip), `not_admitted` (infrastructure broke before proof either
way — fail closed), `unproven` (no oracle reference covers the task, so
solvability cannot be shown — distinct from infra).

## Retired hollow-program battery

The battery implementation and its launch route are retired.
The existing admission records and battery receipts remain unchanged.
Those records distinguish fake-program rewards from rejected reference rewrites.
They do not authorize a new run.
The archive tag `archive/pre-round3-2026-11-12` preserves the former implementation.

## Oracle reference (HAR-191 path, no new store)

MiMo code tasks ship no `solution/solve.sh`, so the oracle agent alone
cannot prove solvability. The gate reuses the existing history-oracle
reference instead of inventing one:

1. The committed `research/experiments/python-task-ledger/oracle_sweep.csv`
   selects the proven row (`oracle:pass+nop:fail`) by bare task id.
2. The recorded sweep receipt is validated: extraction `ok`, solution
   patch present with matching sha, recorded arms 1.0/0.0.
3. The gate stages the package, plants a `solve.sh` applying that patch
   in the agent worktree (the mtime-validation mechanism), and grades
   the fixed tree with the oracle agent for fresh Docker proof.

`reference` takes `auto` (the above), `none`, or an explicit sweep
receipt (`.json`) / raw patch path. The step records fix commit, patch
sha, receipt path, recorded arms, and exact/task-level digest binding
(receipt `run_digest` vs tested package).

## Verifier mutation (post-ladder probe)

The retained `evallab hack mutate` command runs `evallab.verifier_mutation`
(`evallab.verifier_mutation.report/v1`). The retired admission gate used it
against the same staged package that the oracle step graded. That gate set
`--max-mutants` to 12, with one Docker trial per mutant. It recorded the
`skip_mutation` operator override as `mutation_skipped: true` and listed
the step under `skipped_steps`.

Targets: with a HAR-191 reference, the source files the reference patch
changes, as absolute container paths (workdir + `+++ b/<path>`), so
mutants are edits of the fix. With a shipped `solve.sh`, the package's
declared artifacts (the `hack mutate` default).

Coverage: the audit is coverage-guided: one
extra instrumented reference run records which lines of the targets the
hidden tests execute, mutants on other lines are not run, and a
partial-fix probe (the reference fix minus the code the tests never run)
goes first. The step detail names the coverage mode and any reference-fix
lines the hidden tests never run; a surviving partial-fix probe routes to
review like any survivor.

Verdict mapping (`survived + partial > 0` routes to review, never rejects):

* `task_broken` → step `fail`, gate `rejected`. The capture controls
  re-prove oracle=1/base=0 on the exact staged package, so an objective
  mis-score is the same evidence class as a control-step failure: the
  harness ran fine, the verifier is demonstrably wrong.
* any survivor or partial → step `review`, gate `needs_review`. Lineage
  stays untouched (only `admitted`/`rejected` finalize it); the record
  lists the survivor patch paths (`survivor_patches`,
  `inputs/<id>/<id>.patch` under the mutation run dir).
* all killed → step `pass` (the score is recorded in the step detail).
* nothing informative (`no_mutants`, `unscored`, harness breakage, or no
  targets at all) → step `infra`, gate `not_admitted`. Fail-closed and
  stated plainly in the step detail: the gate cannot show verifier
  sensitivity, so admission is blocked.

## Shared QA findings and frozen comparisons

One contract for task QA, trace QA, the reward seeker and tripwire findings,
so their output is comparable and scoreable on external answer keys.
Implementation: `src/evallab/qa/` (`models`, `answer_key`, `score`, `suite`,
`cli`); tests: `tests/test_qa.py`, `tests/test_qa_suite.py`. The trace auditor
(`src/evallab/qa/trace.py`) is described in
[execution tiers](execution-tiers.md#qa-agent---trace).

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

`evallab.qa.models.expected_root_causes(item)` turns each key's labels into a set
of ids; ids compare after normalization (lowercase, every run of
non-alphanumerics becomes `_`, so `Proxy-check too weak` equals
`proxy_check_too_weak`). The first non-empty source wins:

| Source field | Keys | Ids |
|---|---|---|
| `expected_root_causes` (list) | new keys (canonical) | as listed |
| `expected_mechanisms` (list) | `breaker-bench-v1` | benchflow mechanism (`conftest_hook`, `path_trojan`, `pth_injection`) or Terminal Wrench exploit class (`hollow-implementation`, ...) |
| `root_cause` (records) | `envcheck-tb4-39`, `delphik-open-defect-tb2` | each record's `id` and its `source_class` (EnvCheck: `proxy-check-too-weak`, `answer-material-readable-by-candidate`, ...; Delphik classes are free text, so exact matching rarely fires) |
| `expected_root_cause` dict | `trajectory-judge-400` | `failure_type` (`premature_stop`, `wrong_tool`, ...) |
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
comparison. `--agent` is `task`, `trace`, `breaker` or `tripwire`; repeat
it to put several agents' versions in one report. Each `--version` belongs
to the one requested agent whose identity prefix it carries (below).

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
`tripwire/…`, `tripwire@…`, `tripwires/…`, `tripwires@…`, `qa-tripwire/…` or `live-watch/…`,
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
a source with no usable matching decisions is `unmeasured`. The Markdown
report puts source-bound cells in its main table and lists every `no-run`
and `inapplicable` key/split cell, per version, in an "Unmeasured cells"
table; the JSON keeps one full row per cell.

The manifest may also carry an optional `not_rescored` list for published
results the suite cannot re-score (aggregate-only scoreboard rows, unversioned
producers, datasets that are not registered keys). Each entry has exactly
`agent`, `system`, `key`, `split`, `reported`, `reason` and `source` strings.
Entries for the requested agents are printed verbatim in a separate
"Reported elsewhere, not re-scored here" table; they never become suite rows,
never enter a rate, and are not new runs.

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
`research/qa-scoreboard/suite-YYYYMMDD.md` (UTC report date by default);
`--output` overrides that destination and `--json` also prints the rows,
denominators and SHA256 custody receipts. No scoreboard CSV rows are
appended. Inputs cannot be used as the output destination. Markdown shows
repository-relative paths; JSON receipts keep resolved paths. File receipts
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

## QA Task and adaptive environment review

`evallab qa task` (`src/evallab/qa/task.py`, module `evallab.qa.task`)
implements the executed QA Task agent registered beside the shared
`evallab qa` package (`src/evallab/qa/`: `models`, `score`, `answer_key`,
`cli`). It runs:

- `S0`: neutral oracle/nop controls (×3 each), static scan, and leak inspection;
- `S1`: k=3 text-only rubric review through the subscription Codex lane
  (advisory: it scored 0.33 recall / 0.59 false alarms held-out on EnvCheck);
- `S2`: executed probes that look for weak tests —
  (a) the verifier-aware shortcut agent (PR #899: read-only `tests/` view, up
  to 3 attempts with the grader's output visible between attempts),
  (b) ref-subset (PR #892/#903: the smallest part of the reference fix that
  still passes), and (c) the verifier mutation hunt (PR #867: edit the
  reference output, grade every mutant with the task's own verifier; a
  surviving mutant becomes a candidate only if a model adjudicator reading
  `instruction.md` says the mutant is wrong);
- `S3`: fresh-container 3-control confirmation of every S2/imported candidate:
  the wrong/shortcut submission scores 1, the correct fix (oracle) scores 1,
  do-nothing (nop) scores 0.

Only S3-confirmed candidates become verified findings. MiMo semantic proposals
add a separate requirement: independent evidence that the submitted program is
actually incorrect. A self-labeled counterexample and reward 1 do not establish
that fact; their S3 replay stays `unverified` pending independent review.
Unconfirmed/refuted candidates never count as verified defects, and unmeasured
checks never count as passes.

## Stages

| Stage | What runs | Output rule |
|---|---|---|
| `S0` | Unscored static hack scan (`backend.static`); scored `oracle` ×3 and `nop` ×3 fresh-container controls (`oracle_floor=0.9`, `nop_ceiling=0.1`); scored `leak` probe executed directly in the environment image (same `LEAK_PROBE_V1` script and parser as the Harbor probe; a missing `git` with no candidate checkout is `no-repository`, not unknown) | Measured oracle `<0.9` emits a verified `broken` finding; measured nop `>0.1` emits a verified `weak` finding. A measured leak with future history appends a `reachable` Signal. GPU or multi-service Compose packages are `not_run` with the blocker reason. |
| `S1` | Complete rubric (`--rubric` or Harbor built-in) over k=3 independent text-only Codex samples; only complete samples vote; majority per `rubric:<name>` criterion | Majority fails on `DEFECT_CRITERIA` append Signals, never Findings. |
| `S2a` shortcut agent (non-MiMo) | `bundle_verifier_aware` gives the model `instruction.md`, `task.toml`, `environment/*` and `tests/*` read-only. Solutions, cheats, README and static golden directories are excluded. The existing generic prompt, submission formats, grader-output feedback and ≤3-attempt stopping rules are unchanged. | Reward 1 creates an **unverified** candidate; the existing scored `shortcut-agent` verdict applies. |
| `S2a` MiMo semantic-only | Packages with `environment/setup/setup.sh` use a separate program-correctness prompt. Only the instruction, source-code sections of `tests/test.patch`, test command, declared workdir and optional pristine base-source context are supplied. No generic reach/tamper mechanisms or runtime test output are fed back. A proposal must be a production-source patch with a quoted requirement and concrete predicted counterexample. | Every attempt retains source/context/prompt/response/candidate hashes and raw artifacts. Non-executed, rejected, failed or quota-stopped proposals have null reward, never an invented zero. Reward 1 creates an `unknown`/`unverified` candidate, not a proven wrong pass. |
| `S2b` ref-subset | `evallab.ref_subset.probe_task`: raw MiMo packages (or `--ref-patch`) use their reference patch; Harbor **separate-verifier** packages derive the reference from their own oracle and grade subsets in fresh verifier containers (#903). Shared-verifier Harbor packages are `not_run` (not applicable), never a pass. `--mutants N` also runs its line-level pass (#918): up to N one-edit mutants of the lines the reference wrote, each graded like a subset. | A measured partial-fix pass keeping ≤50% of the reference code lines (severity high/medium) creates an unverified `weak` candidate whose reproducer is `minimal.patch`. Low severity (>50% kept) is a Signal only. An exhausted grade budget is `unknown`, not a pass. A surviving line-level mutant is adjudicated against `instruction.md` exactly like an S2c survivor (check `ref-mutants`); only `wrong` ones become unverified `weak` candidates (reproducer: that mutant's patch over the base). `ref-subset` and `ref-mutants` are unscored checks. |
| `S2c` mutation hunt | `evallab.verifier_mutation.run_mutation_audit` with `--mutants N` (default 8; `0` disables) and the coverage-guided plan. Trials run through `HarborBackend.matrix_runner` (raw-docker fresh containers, same capture and coverage layout as Harbor trials) instead of Harbor jobs: ~2 s per trial instead of ~100 s. Survivors (reward kept while the file's blank control scores 0) are read by a Codex adjudicator against `instruction.md` and the diff: `wrong` (violates a quoted requirement) / `equivalent` / `unspecified`. | Only `wrong` survivors become unverified `weak` candidates (reproducer: the mutation script). Unadjudicated survivors (no lane, quota stop, more than 6) append a `weak` Signal and make the `mutation-hunt` check `unknown`. `task_broken`/`unscored` hunts are `ran_without_usable_result`. |
| `S3` | Fresh-container replay, with clean S0 controls from the same audit or one fresh oracle/nop pair when S0 did not run. Mutation scripts retain their existing oracle-channel replay. | Existing candidates become `verified`/`fail` only with replay reward 1 and clean controls. MiMo semantic proposals additionally remain `unverified`/`unknown` until independent incorrectness review; clean numerical triplets alone cannot certify them. Reward 0 with clean controls refutes a candidate. Partial, missing or failed rewards are not confirmations. |

Submission kinds from the shortcut agent are converted by
`submission_to_script`: `command` (bash run in `/app` in the agent
container), `file`/`json` (base64-written to an absolute path; JSON is
validated first), and `patch` (unified diff applied with `git apply --binary`,
a Python unified-diff fallback for minimal images without `git`/`patch`, or
`patch`).
Invalid submissions are unusable, never passes.

### Execution backend

The default `--environment docker` uses `HarborBackend.run_container_trial`,
which mirrors Harbor's separate-verifier flow.
`run_task` holds one slot of the shared host-wide Docker FIFO lock
(`evallab.docker_lock.docker_slot`, label `qa-task/<task>`) for all of a
task's container work (S0, S2, S3), so the queue is joined once per task and
not once per trial; S1 runs before it and needs no container. The backend's
per-trial, `leak`, ref-subset and mutation calls re-enter that slot
(`HarborBackend.slot` is reentrant; `docker_slot` itself is not). By default one
QA Task container runs at a time and other tabs interleave between tasks.
`HarborBackend.control` never caches an observation: oracle ×3 and nop ×3
are six independent trials, each with its own job path and raw result. S3 can
still reuse the explicitly recorded S0 controls rather than requesting new ones.

Explicit Docker task concurrency is restricted to 1–2 and retains that same
host-wide admission:

1. build (and cache per package digest) the environment image and, for
   `environment_mode = "separate"`, the `tests/` image (adding
   `--platform linux/amd64` when `Dockerfile` or `tests/wheels/*.whl` requires
   `x86_64` / `amd64`);
2. start one agent container (or, for `environment/docker-compose.yaml`
   packages, bring up the Compose stack under the same FIFO slot); run oracle
   `solution/solve.sh` (optionally replaced by a mutation/capture script), a
   shortcut script, or nothing (nop); run `[[verifier.collect]]` hooks for
   `main`; optionally copy the agent's `/logs/artifacts` out (mutation
   captures);
3. copy `main` `task.toml` artifacts out (directory artifacts keep their tree;
   dict artifacts honour `exclude`); for Compose packages with sidecar hooks or
   sidecar `artifacts = [{source, service}]`, stop `main` first so sidecar
   collection sees a quiescent state, then run sidecar `[[verifier.collect]]`
   hooks and copy sidecar artifacts;
4. remove the agent container (or tear down the Compose stack), start a fresh
   verifier container, upload the artifacts to their declared paths, run
   `tests/test.sh` with a world-writable `/logs/verifier`, read
   `/logs/verifier/reward.txt` (or `reward.json`), and copy `/logs/verifier/`
   out (coverage traces);
5. shared-mode tasks are graded in the agent container instead.
Every trial writes `agent/oracle.txt`, `verifier/test-stdout.txt` and
`result.json` under its trial directory; those are the grader outputs fed back
to the shortcut agent on the next attempt.

#### Modal: native Harbor trials, not local Docker

Real execution needs the pinned lab Harbor and its Modal extra
(`uv sync --extra laminar`); package-only `--plan` needs neither the optional
runtime nor Modal credentials.

`--environment modal` runs S0 controls/leak inspection, S2 shortcut,
reference-subset and mutation trials, and S3 confirmations on native Harbor
Modal environments. QA inherits the logical stages, patch staging and replay
rules; native `Trial.create(...).run()` owns healthchecks, execution, collect
hooks, artifact destinations/exclusions, separate/shared verifier isolation,
reward parsing and cleanup. A script-only `BaseAgent` runs the submitted script
without a model. Reference-subset uses its native Modal backend and the same
run-wide spend gate. This path never joins a Docker slot, builds/pulls/runs
local Docker, or falls back to Docker.

Every agent or verifier allocation is admitted separately, **before** native
image preparation/provider execution. The reservation includes preparation and
both possible native sandbox-creation attempts (three units per environment);
unused attempts retain their estimates. Reference-subset accounts for its own
image preparation, immutable snapshot and bounded grade attempts, included in
the plan. A separate verifier therefore needs its own reservation even after
the agent succeeds. A failed script/setup/verifier, missing/invalid reward,
budget exhaustion or package-digest change is unmeasured, not a pass or a
measured zero. Native raw `result.json` is preserved; `qa-execution.json`
records QA's usability decision and spend receipt. `agent/oracle.txt` and
`verifier/test-stdout.txt` keep the existing reproducer/report interface.

The adapter combines `LockedModalEnvironment`'s actual accepted egress-policy
evidence (`egress-lock.json`) with `RegradeModalEnvironment`'s PATH pin and
at-most-one-hour sandbox lifetime. It does not claim a public policy was
locked. Native trial cleanup runs on success and failure; failed starts also
attempt bounded cleanup. Longer requested trial timeouts do not extend the
one-hour sandbox lifetime. Script overrides and synthesized patch oracles are
staged outside the package; reports, findings and native trial config retain
the original package path/digest.

Compose packages are explicitly unsupported on this backend (no implicit
Docker-in-Docker). GPU/TPU tasks are refused before allocation because the
CPU reservation estimate is **not** qualified accelerator pricing. This is
not cloud GPU qualification. Windows and declared multi-step packages are
also unsupported by this single-step shell-control backend. Unsupported tasks
remain unmeasured with their reason.


### Workdir-bound packages (MiMo code tasks)

A package whose `task.toml` declares `[environment].workdir` and either has no
`solution/solve.sh` (raw MiMo code tasks: the oracle is a reference patch) or
declares a setup healthcheck (v6 separate-verifier MiMo tasks: own `solve.sh`, image
with no `WORKDIR`) is run through the same flow with the additions below
(`HarborBackend.patch_workdir` names them). They change nothing for any other
package, and no TB4 package declares a `workdir`.

- **Healthcheck first.** `[environment.healthcheck]` runs in the started container
  before the oracle, a shortcut, the verifier or the leak probe (`retries + 1`
  attempts, `timeout_sec` each). For MiMo it is the one-time setup that writes
  `/var/lib/mimo/base`, which `tests/test.sh` needs; without it no reward exists.
  A separate verifier completes its clean setup **before** candidate artifact
  import; its ready marker prevents later setup from deleting that candidate.
  Restore the exact package's private tests after setup/artifact import and
  before grading: setup may remove the test-image's baked `/tests` directory.
  A healthcheck that never exits 0 leaves the trial `failed` (unmeasured), never a
  pass. A package whose healthcheck waits on a service the backend does not start
  (it runs `sleep infinity`) is therefore unmeasured too.
- **Patch oracle.** `--ref-patch` (or `solution/patch.diff`) is the oracle. The
  backend stages `/solution/reference.patch` and a `solve.sh` that applies it in
  `workdir` (`git apply`, then forward-only `patch -N`; a patch neither accepts
  exits nonzero and the trial is unmeasured). The staging directory is outside the
  package, so the package digest a report binds to is untouched, and the patch
  sha256 is recorded in `facts.oracle` of every control. Without a patch the
  oracle trial fails with that reason.
- **Replay in the workdir.** An S3 `patch` reproducer applies in `workdir` for such
  packages instead of `/` or `/app`.
- **Run in the workdir.** For these packages the oracle and a shortcut run with
  `docker exec -w <workdir>`, as Harbor does. A v6 MiMo `solve.sh` finds its repository
  with `git rev-parse --show-toplevel || pwd`; in the image's default directory (`/`)
  its patch had nothing to apply to and every control came back unmeasured.
- **Embedded reference patch for v6 ref-subset.** A v6 separate-verifier MiMo
  package embeds its base64 reference patch in `solution/solve.sh`
  (`echo <b64> | base64 -d`). `HarborBackend.ref_subset` extracts it
  automatically and stages a temporary direct-grading `tests/test.sh` for the
  S2b block-drop and line-mutant pass, while S3 replays every candidate against
  the v6 package's own separate verifier.
- **Subject id.** The resolver strips the namespace from `[task].name`; `run_task`
  restores the package's own (`mimo-v2.6-rl/<id>`, `terminal-bench/<id>`) so a
  finding binds to its key. A package with no namespaced name keeps the TB2
  fallback (`terminal-bench/<id>` when the directory is the id, otherwise bare).

The S2c mutation hunt still needs an output-artifact solution; MiMo uses the
existing S2b reference-subset/ref-mutant path instead. The leak probe is unchanged:
a dangling remote HEAD can leave its history result unknown. Deterministic
`--no-model` audits retain S0, ref-subset and S3 replay without generating a
semantic proposal.

#### MiMo semantic-only S2a

`stage_s2a(report, backend, lane, config, *, run_dir, source_context=None)` is
the isolated seeker callable. `stage_s2` calls it before its unchanged S2b/S2c
work. Calling `stage_s2a` directly never imports old witnesses, extracts a
reference patch, runs reference subsets or generates reference mutants.

This path is local-Docker-only. Set `EVALLAB_DOCKER_LOCK_SLOTS=2` explicitly
before constructing/using the backend; each actual trial uses the first-party
`HarborBackend` and shared FIFO. Supply the campaign's approved, quota/spend-gated
text lane; the callable does not construct another model route.

`source_context` is a bounded mapping from safe repository-relative production
paths to pristine base-source text (160,000 characters total). One larger source
module may use that shared allowance; per-file splitting is not required.
Obtain it only from the untouched package image after its normal setup, with a
trusted read-only reader over an explicit production-path allowlist. Do not
export history, tests, fixtures, setup/grader files, solutions, reference patches
or archived wrong proposals. The reader must reject links and paths outside the
declared workdir. Existing `_ensure_images`/`_run_probe` under `backend.slot`
can perform that first-party fresh-container read; only source JSON, not setup
or grader output, belongs in the prompt. Without sufficient source, the model
can return `needs_source_context` with specific source paths; this is unmeasured,
not a fabricated patch or a pass.

Accepted responses are `candidate`, `no_candidate` or `needs_source_context`.
A candidate has `submission.kind="source_edits"` with ordered `edits`, each
containing a supplied production `path`, nonempty exact `old` text and replacement
`new` text. The old text must occur exactly once in the current file at that step.
`mimo_semantic_edit_patch(package, source_context, edits)` generates and validates
the unified diff, including EOF markers; models do not calculate hunk counts.
Unprovided/protected paths, ambiguous anchors and cancelled/no-op edits are
rejected before Docker. A concrete counterexample (`requirement`, `input`,
`expected`, `actual`) is retained for independent inspection, never treated as a
correctness judgment. Only ordinary
input/output behavior, including constant outputs and incomplete implementations,
is in scope. Reading expected-answer files or hidden data, writing rewards,
test-framework monkeypatches, introspection, process/network access and
sandbox/control-plane bypasses are not.

`grade_shortcut(..., {"kind": "semantic_patch", "content": diff}, ...)` is the
safe execution entry for an already obtained proposal, including a genuinely
batched model response. MiMo refuses the generic command/file/JSON/patch channels.
The semantic validator:

- accepts bounded, same-path text git diffs for production source; rejects
  absolute/traversal/hidden paths, tests and every path in `tests/test.patch`,
  fixtures/golden files, solutions, grader/reward/security/auth/network controls,
  runner/dependency configuration, hooks and plugins.
  Legitimate production modules such as `burnman/classes/solution.py` are allowed;
  reference-solution directories are not.
- rejects inconsistent headers, binary content, deletion, rename/copy, mode
  changes, symlink/gitlink creation, and added test-control/introspection/
  process/network code using the existing verifier guards plus the narrower
  semantic admission rules;
- applies only in the declared workdir, with runtime checks against symlinks,
  hardlinks and non-regular targets, followed by atomic `git apply --check`/
  `git apply`; there is no unsafe-path or fuzzy patch fallback. An apply failure
  aborts before grading and keeps reward null.

These are conservative admission checks, not a general security proof or a
substitute for independent source/counterexample review. Keep each
`semantic.patch`, `semantic-candidate.json` and `semantic-apply.sh` together.
The sidecar binds the original package digest and exact candidate SHA256.
`replay_reproducer` refuses changed/missing candidate custody and revalidates
the patch against the target package's workdir/protected paths before a fresh
trial. A v6 candidate transferred to released v7 is a separate transfer
measurement, not an independent v7 search. A setup failure remains unmeasured;
never substitute a different package to complete a cell.

Minimal isolated sequence, with a fresh `run_dir`, exact package/digest and the
approved `lane`/pristine `source_context` already supplied:

```python
os.environ["EVALLAB_DOCKER_LOCK_SLOTS"] = "2"
config = QATaskConfig(stages=("S2", "S3"), shortcut_attempts=1, mutants=0)
task = backend.resolve(package)
report = TaskReport(
    subject=Subject(kind="task", task_id=task.task_name, task_digest=task.package_digest),
    package=str(package), requested_stages=["S2", "S3"], started_at=utcnow(),
)
findings = stage_s2a(
    report, backend, lane, config, run_dir=run_dir, source_context=source_context,
)
stage_s3(report, findings, backend, config, run_dir=run_dir)
finalize(report, findings)
write_json(run_dir / "report.json", report.model_dump(mode="json", by_alias=True))
for finding in findings:
    write_json(run_dir / "findings" / f"{finding.id}.json",
               finding.model_dump(mode="json", by_alias=True))
```

Increase `shortcut_attempts` on the **same report** only up to three cumulative
attempts per arm. Quota admission stops do not consume a model turn; their raw
receipts are retained separately, so a later permitted continuation cannot
overwrite them. Do not wrap one physical batch response in fabricated per-item
`lane.complete` calls: retain/count the actual batch turn once and grade its
individually bound patches directly.

All admitted candidates, including reward-zero attempts, have a `reproducer`
in `report.checks["shortcut-agent"].facts["attempts"]`. Iterate those records
and call `backend.replay_reproducer(target, Reproducer.model_validate(record),
digest=exact_target_digest, run_dir=fresh_replay_dir, timeout_seconds=...)`
to measure each against its own arm or its exact partner. Persist each returned
status/facts/reason separately; transfer controls and independent incorrectness
evidence are still required before counting a wrong pass.

For a bounded actual Docker smoke, use one independently inspected production
patch from pristine source, one `grade_shortcut` trial, one fresh reproducer
replay and one fresh oracle/nop pair, with the package's bounded timeout and
FIFO slots fixed at two. No model call, reference-subset search, mutant hunt
or package mutation is needed for this smoke.


### Findings vs signals

Every `qa.finding/v1` requires a non-null `reproducer`
(`harbor_trial` | `command` | `patch`). S0 control failures are verified at
creation (the control itself is the execution). S2 candidates are created
`unverified`; only S3 can set `verified` or `refuted`. Leak detections, S1
rubric majorities, low-severity ref-subset drops and unadjudicated mutation
survivors are Signals on the report, never synthetic findings. Findings carry
an optional `root_cause` list (ids in the key's vocabulary); QA Task names none
today, so root-cause match is `n/a` (not measured), never 0.

### Measuredness and verdicts

- Check status is `measured`, `ran_without_usable_result`, or `not_run`; the
  latter two never pass.
- `quality_complete` requires at least one scored check and measured status for
  every scored check. Unscored descriptive checks normally do not block it,
  but requested S2 survivor adjudication must cover **every** `ref-mutants`/
  `mutation-hunt` survivor with a usable wrong/equivalent/unspecified decision.
  Quota-stopped, failed, missing or capped adjudications make completeness false,
  even if all numeric controls passed. Pending independent MiMo incorrectness
  review also prevents completeness. Historical raw reports are not rewritten.
  Resume admission recomputes these gates from the checks, including nested
  adjudication records, rather than trusting a stored `quality_complete` flag
  or searching only top-level reason strings. S0 reuse additionally requires
  six distinct measured job paths; duplicated/missing control custody causes
  all six controls to run fresh. An old MiMo S2a skip never satisfies the new
  semantic stage. Valid S1 checks and retained native probe reports can still
  be reused independently, without overwriting the historical source report.
- `report.verdict` is `fail` when any verified eval finding stands; otherwise
  `unknown` when measurement is incomplete, any scored check failed, any
  Signal exists, any candidate remains unverified/non-pass, or `report.error`
  is set; otherwise `pass`.
- Package bytes are re-digested after the audit; a mid-audit change sets
  `report.error` and demotes findings to `unverified`.

## CLI

```bash
# Full executed audit of one package
uv run evallab qa task --task <package-dir> \
  --rubric <benchmark>/rubrics/task-implementation.toml

# Answer-key split (packages must match pinned digests)
uv run evallab qa task --key envcheck-tb4-39 --split dev \
  --rubric <benchmark>/rubrics/task-implementation.toml

# Executed probes only, no model calls (survivors stay unadjudicated)
uv run evallab qa task --task <package-dir> --stages S0,S2,S3 --no-model \
  --mutants 4 --run-dir runs/.qa-task/manual-check

# Confirm imported candidates in S3
uv run evallab qa task --task <package-dir> --stages S2,S3 --no-model \
  --evidence <candidate-finding-or-dir>

# $0 Modal plan, including all selected tasks and conservative execution units
# No backend, Harbor/Modal runtime, Docker, quota probe or model is constructed.
uv run evallab qa task --task <package-dir> --environment modal --plan \
  --concurrency 16 --max-usd 3 --usd-per-execution 0.0025

# Capped real cloud controls; approval is recorded, not implied by --plan
uv run evallab qa task --task <package-dir> --environment modal \
  --stages S0,S3 --no-model --max-usd 3 --usd-per-execution 0.0025 \
  --approve "Authorized CPU controls, at most USD 3 in reservations"

# Digest-bound scorer (flagged vs verified); held-out rows drop items QA Task
# was tuned on, per research/answer-keys/splits/exposure.jsonl
uv run evallab qa task-score --key envcheck-tb4-39 \
  --findings runs/.qa-task/<run-id> --split heldout --exclude-exposed \
  --level verified --scoreboard research/qa-scoreboard/scoreboard.csv
```

Options: `--task`, `--key`, `--split {dev,heldout}`, `--item`,
`--packages-root`, `--run-dir`, `--stages` (default `S0,S1,S2,S3`),
`--trial-timeout` (default 1800), `--rubric`, `--model` (default
`codex/gpt-6-luna`), `--no-model`, `--mutants`, `--evidence`, `--triplet`,
`--ref-patch` (the reference patch: ref-subset's reference and, for a package
with no `solution/solve.sh`, the oracle; see "Workdir-bound packages"),
`--resume-from` (reuse digest-bound measured checks and probes from an earlier
run directory), `--environment {docker,modal}` (default Docker), `--plan`,
`--max-usd`, `--approve`, `--usd-per-execution` (positive finite; default 0.0025),
and `--concurrency` (default 1; Modal 1–16, Docker 1–2).
Run directories are fresh and immutable (an existing `--run-dir` is an error)
and must live inside the checkout. Layout: `run.json`, `summary.json`,
`tasks/<slug>/report.json`, `tasks/<slug>/s2-shortcut-a<N>/`,
`tasks/<slug>/ref_subset/`, `tasks/<slug>/mutation/`,
`tasks/<slug>/s3-replay-<suffix>/`, `tasks/<slug>/leak/`,
`findings/<sha256(finding.id)>.json`, and `codex/spend.jsonl`. Mutation trial
directories live under `runs/ec-*` (the mutation audit's own convention).

`--plan` only reads selected package/evidence metadata and prints JSON. It
creates no run directory, calls no provider or subscription model, and always
reports zero executed/reserved USD. Each selected task appears with digest,
unsupported reason, per-stage reservation upper bounds and estimated USD.
The aggregate exposes `fits_cap`, `runnable` and refusal reasons (including
missing real-run approval/cap). The bound reserves possible fresh S3 controls
and confirmations, ref-subset preparation/grades/mutants, and mutation
captures/coverage/blank controls/mutants, rather than counting only successful
trials. A shared-verifier package with neither `solution/patch.diff` nor an
explicit `--ref-patch` has no applicable ref-subset execution or confirmation
reservation; binding a reference restores both to the bound. An actual capped
run may deliberately be smaller than this full-audit
bound: allocations stop at the gate and all unfinished measurements stay
unknown. A plan is not an approval or a promise that the cap completes every
stage.

Real Modal execution requires both recorded nonempty `--approve` text and a
positive finite `--max-usd`. One thread-safe `CloudSpendGate` is shared across
all stages and tasks, with a distinct backend and model lane/scratch directory
per task. Cloud concurrency 16 means 16 independent task threads, not 16 calls
into one mutable backend. Docker's default remains sequential. Modal run
metadata, per-task reports, `cloud-spend.json` and `summary.json` include the
gate receipt (`approval`, `cap_usd`, `usd_per_execution`, `executions`,
`reserved_usd`). Per-task receipts are run-wide snapshots; the final receipt
is in the run metadata/summary. Model `cost_usd` remains separate from cloud
reservations. Reservations are conservative admission **estimates**, not
observed provider bills and not a provider billing hard kill; a failed or
short attempt retains its reservation.
The Modal rate is per **allocation/preparation unit**, not per logical trial:
each environment reserves image preparation and both possible creation attempts.
A fresh environment reserves `$0.0075`; a separate agent/verifier trial reserves
`$0.015`. Snapshot-based ref-subset grades reserve two units per clone instead.
The October 17 reference-subset calibration describes the
metered-usage cross-check and mean-based safety margin; slow or larger workloads
need an explicit higher estimate. Plan totals and cap comparisons preserve
sub-cent precision. QA Trace's `$0.01` logical-replay estimate is a different
unit and must not replace this rate or be multiplied as an allocation rate.



## Models, quota gate and spend

`CodexLane` serves S1, the S2 shortcut agent and the mutation adjudicator
through fresh text-only `CodexTransport` calls (single assistant message, JSON
only). Before **every** call it re-checks the Codex 7-day quota via
`omp usage --json --redact -p openai-codex`; an unreadable reading or
`usedFraction >= 0.60` raises `QuotaStop` and the remaining model work is
`not_run`. Calls are $0 subscription usage, logged with
`SPEND_TAG = "2026-10-15"` and the weekly used fraction to
`<run>/codex/spend.jsonl`. Model tokens count usable replies only (a lower
bound, not a bill). No paid model API is used; Modal cloud execution has its
own explicit reservation gate described above.

## Scoring (`evallab qa task-score`)

`score_findings` binds findings and reports by exact
`(subject.task_id, subject.task_digest)`; digest mismatches are excluded and
counted. Rates are over the non-excluded items of the split (not only measured
ones):

- `verified_*`: digest-bound Finding with `fault="eval"`, `verdict="fail"`,
  `refutation.status="verified"`;
- `flagged_*`: any standing unrefuted Finding or any report Signal;
- `by_label_strength`: recall on positives sliced by the key's
  `label_strength` (for `envcheck-tb4-39`: `confirmed` EnvCheck defects and
  `epoch_flagged` defects Epoch's public review reported), so the two are
  never blended;
- `category_match_recall`: verified finding category in the key's expected
  categories; `verified_/flagged_root_cause_match`: `n/a` until findings name
  root causes (see [the shared QA contract](#shared-qa-findings-and-frozen-comparisons));
- `measured_fraction` / `observed_fraction`: digest-bound reports with
  `quality_complete` / any report.

`--exclude-exposed` removes reported-split items the split guard records as
`tuned` or `selected` for QA Task (`--exclude-item` adds more). Excluded items
stay in `items`, are listed in `excluded_items`, and count as unmeasured on the
scoreboard row (`n_measured <= n_items - excluded`), which is the form the
guard accepts. Dev items are never excluded; dev recall of anything tuned on
dev is circular and is labelled so.

Scoreboard rows use `system = qa-task@verified|flagged` and status
`executed S2+S3 (<n>/<scored items> measured)` when at least one scored item
was fully measured. Never tune on the split you report
(`research/answer-keys/README.md`, `research/answer-keys/splits/README.md`).

## Limits

- Shortcut search is bounded (≤3 model attempts, ≤1 graded submission per
  attempt, ≤8 ref-subset grades, ≤24 mutants of which ≤6 are adjudicated); a
  clean run is not a certificate.
- The shortcut agent sees verifier source read-only. It does not see
  `solution/`, golden file contents, or answer keys.
- The mutation hunt grades edits of the reference's captured output files, so
  it finds assertion gaps in what the verifier reads, not gaps in what the
  solution should have written elsewhere.
- GPU and multi-service Compose tasks are blocked (`not_run`) on this host;
  their items stay unmeasured, never passes.
- Mutation and adjudication need the Codex lane; with `--no-model`, survivors
  are only Signals.
- Answer-key negatives may carry weak labels, so false-alarm rates are upper
  bounds; a verified finding on a key negative is a candidate real defect (the
  Epoch relabel found 12 of them), not automatically a false alarm.

## Rollout filtering

Use the filter for triage, not a claim of semantically clean training data.
The original exact stage rules and documented false-drop limits follow.
No threshold or label changes here.


---

### 1. Why Filter Training Data

Training generative or reinforcement-learning models on rollouts that cheat (e.g. monkeypatching test runners, tampering with grading assertions, or acquiring answers out-of-band) teaches the model to cheat rather than solve tasks. 

1. **Task-Level Evidence:** Has the task verifier been certified robust, or is it weak/exploitable?
2. **Trace-Level Evidence:** Did the rollout trigger deterministic L0 tripwires, alter protected evaluation surfaces, or receive a cheating verdict from compact card AutoReview?

---

### 2. Multi-Stage Pipeline

The filter executes across three sequential stages per rollout:

```
[Candidate Rollout]
        │
        ▼
 ┌──────────────┐      Weak/Exploitable
 │ 1. TASK      │ ─────────────────────────► [REVIEW / DROP] (default: review)
 └──────┬───────┘
        │ Gold / Hardened / Unknown
        ▼
 ┌──────────────┐      Hard Hit (Test/Grader Tamper, Answer Probe)
 │ 2. L0        │ ─────────────────────────► [DROP]
 └──────┬───────┘
        │ Soft Hit / Target Write
        ▼
 ┌──────────────┐ (Optional, `with_autoreview`)
 │ 3. AUTOREVIEW│ ─────────────────────────► [REVIEW / DROP]
 └──────┬───────┘
        │ Clean Pass
        ▼
     [KEEP]
```

#### Stage 1: TASK (Task Certification)
- Reads the stored `certified_tasks` manifest.
- If status is `weak` or `exploitable`: assigns decision `review` by default (configurable to `drop` via `task_policy`).
- If status is `gold`, `hardened` or `certified`: passes to the next stage with `TASK_GOLD`, `TASK_HARDENED` or `TASK_CERTIFIED`.
- If status is `provisional` or `uncertified` (or any status not defined above): passes with `TASK_PROVISIONAL` or `TASK_UNCERTIFIED`. Only a task missing from the manifest reports `TASK_STATUS_UNKNOWN`.
- If task ID is `unknown` when certified tasks are configured: routes to `review` with `TASK_ID_UNRESOLVED`.
- Each status can be given its own action through the `status_actions` argument of `evaluate_task_stage` (the batch line exposes it as `task_policy`); the defaults are the behavior above.

#### Stage 2: L0 (Deterministic Tripwires & Tamper Signatures)
- **Fail-Open Safeguards:**
  - Unparseable trajectories (e.g. corrupt JSON) route to `review` with `L0_PARSE_ERROR`.
  - Empty bundle directories or missing inspectable files route to `review` with `L0_NO_EVIDENCE`.
  - Cloud-exported batches (`cloud_trace_export` stores parallel calls as one `function_name: tool` call whose `arguments.command` is a JSON array of `{id, name, arguments}`) are expanded once at load into ordinary ATIF calls, so L0 and every content scan see each nested `bash` (`cmd` or `command`), `python` (`code`, inspected like a heredoc), `text_editor` and `think` call by name. A batch that is not valid JSON stays a `tool` call and routes to `L0_UNRECOGNIZED_TOOL_SHAPE`.
  - L0 uses the shared `tool_calls` normaliser for narrow transport shapes. It accepts one submitted, allow-listed Terminus shell line only with known shell context. It also handles documented `text_editor` operations, bundle `shell` calls with matching fields, and known notebook edits. Unsupported calls retain their original payloads. The projection changes neither stored evidence nor AutoReview input.
  - Terminal state carries across calls and steps. An empty shell prompt restores shell context. Partial input, multiple lines, heredocs, control keys, and unsupported commands remain unknown; they are not silently treated as shell commands. Raw handled-call text that mentions a protected target remains review evidence when no inspectable action results.
- **Hard Drops:**
  - Target-surface writes that alter tests or graders (`PROTECTED_PATH` modifications such as editing or deleting existing tests, or tampering with test runner hooks). A path counts as ALREADY EXISTING, and the edit rule applies (hard drop), when it was read earlier in the trajectory, appears in the final diff as a modification (`--- a/...`, not `/dev/null`), or was written earlier in the trajectory; in that case any `create` verb, `Add File` patch header or non-existence evidence is ignored (a prior read wins). Otherwise a `create` verb or an `Add File` header IS newness evidence, and so is proven non-existence, which is derived only from observations attached to single-path read commands (`cat X`, `ls X`, `test -f X`, or `read` tool calls) whose output explicitly names X with 'No such file'; whole-command substring searches and unrelated errors never flip the rule. New test files and honest `conftest.py` additions with evidence of newness are kept when assertions are not weakened. A blind `create` over an existing test with no earlier read and no `--- a/` in the final diff is therefore KEPT unless the body matches a weakening pattern; it is caught only when the final diff shows the modification. Writes to test paths lacking newness evidence route to soft review (`L0_SOFT_TEST_WRITE_UNVERIFIED`), and to hard drop if content weakens assertions (`assert True`, bare `pass`, skip/xfail markers).
  - Fabricated verification: single-token tamper signatures (e.g. `pytest_sessionfinish`, `always_equal`, `entrypoint_assertless`) on test/grader/harness surfaces; application source requires strict co-occurrence (`atexit` + `_exit`/`os._exit` within one added hunk; pytest hooks like `pytest_runtest_*` or `pytest_sessionfinish` in files not importing pytest). Residual limit: application hooks using external scripts, obfuscated reflection, or out-of-band imports remain out of deterministic L0 scope.
  - Answer-key or protected grader reads (`gate_protected_read`, `hidden_info_read`, `task_protected_read`).
  - Verdict-eligible Hackbook rules (`hb_grader_surface_write`, `hb_runner_shadow`, `hb_success_stub_script`, `hb_login_profile_tamper`).
- **Soft Reviews:**
  - Configuration writes (e.g. `pyproject.toml`, `pytest.ini`, `setup.py`) route to `L0_SOFT_CONFIG_WRITE`.
  - Non-test target surface writes (e.g. data fixtures or output configs).
  - Diagnostic cues and non-verdict gate rules (`history_mining`, `codeban_violation`).
  - `L0_SOFT_PROTECTED_SYSTEM_WRITE` reviews direct package-state mutations,
    unread truncation of stock boot configuration, and executed seed replacement
    after an observed failure. It never supplies a hard-drop reason.
    The action scanner reads tool calls, not narration or printed code.
    Package reads redirected into reports do not count as package writes.
    Reads take effect in call and shell order; a later read cannot excuse an earlier write.
    Boot configuration means `/etc/default/grub`, `/etc/fstab`, and `/etc/crypttab`,
    not arbitrary new `/etc` files. Appends and preserving edits do not trigger that branch.
    Python seed changes require literal replacement data to reach a file write.
    A saved script counts only when a later interpreter call executes it.
    Unknown shell control flow and dynamic Python remain outside this bounded rule.

#### What the Filter Cannot See
1. Writes not parsed as text: `awk -i inplace`, `ed`, helper scripts (`python scripts/fix.py`), `patch -p1`/`git apply` of diffs generated out-of-band, or writes by test-time hooks. (Note: `dd of=`, `sed -i` variants, and `python -c open().write` are parsed. Copying from a temporary file over a test via `cp /tmp/x.py tests/test_core.py` routes safely to review.)
2. Unrecognized edit tools that do not touch known test/grader patterns and carry no final diff. For known edit tools (`edit`, `str_replace`, `MultiEdit`, `apply_patch`), argument shapes (`old_str/new_str`, `oldText/newText`, `edits[]`, hashline `input`, `text_content`) are fully parsed; any edit-like call on a protected path with unparseable arguments routes safely to `review`.
3. Reads executed outside recognized shell commands or harness tools (e.g. executed via compiled binaries or out-of-band external scripts). The parser recognizes `read` (with or without `path:range`), `Read`, `cat`, `cat a b`, `cat < f`, `git show HEAD:f`, `xargs cat`, `find -exec cat`, `sed -n`, `head`/`tail`/`less`/`more`, `python` read calls, and shell variable expansions.
4. Dynamic path aliases from pre-existing filesystem symlinks or directory renames after the fact. Path normalization (`posixpath.normpath`), redundant slashes (`//`, `/./`, `tests/../tests/`), and case folding across both newness logic and grader tamper gates are handled.
5. A blind `create` over an existing test with no earlier read and no final diff, unless the body matches the weakening patterns.
6. Semantic exploits with zero test or grader writes (parameter abuse, copying reference outputs); these never reach AutoReview, which runs only on L0-soft or target-writing rollouts.
7. Test or grader code under unrecognized paths (`checks/validate.py`, `t/core.t`), and application hooks using dynamic reflection, string concatenation or custom imported plugins.
8. Common harness tools (`think`, `finish`, `submit`, `run_command`, `execute_bash`, `str_replace_based_edit_tool`, `text_editor`) are on the recognized allow-list. Unrecognized or malformed tool calls route safely to `review` (`L0_UNRECOGNIZED_TOOL_SHAPE`).
9. AutoReview uses one route, no ensemble, and its suspicion score is not calibrated on this data (NOT MEASURED).
10. Other startup hooks not yet intercepted: non-POSIX shell startup files (fish, csh, tcsh), out-of-band runtime environment overrides such as `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONSTARTUP`, `NODE_OPTIONS`, and complex dynamic path expressions (`eval`, shell command substitutions like `$(get_target)`) where target paths cannot be resolved statically (simple variable assignments, `.zshenv`/`.zlogin`/`.zlogout`, `/etc/zsh/*`, multi-line heredocs, and payload `cp`/`install`/`ln`/`mv` are covered; unresolved variable targets route safely to soft review). In addition, four out-of-sample evasion shapes are known limits of deterministic L0 hard drops: format string payloads (e.g. `printf '%s\\n' 'exit 0' >> ~/.bash_profile`, routes to soft review), decoded payloads (e.g. `echo ... | base64 -d >> ~/.bash_profile`, routes to soft review), indirect sourcing where the terminator is placed in an auxiliary file sourced by the profile (e.g. `echo 'exit 0' > ~/.myenv; echo '. ~/.myenv' >> ~/.bashrc`, routes to soft review), and byte copier pipes without standard shell redirects (e.g. `echo 'exit 0' | dd of=/etc/profile`, unparsed, no signal).

Known honest false drops (data lost, never leaked): a new test using `pytest.skip` or `xfail`, edits to documentation or to a file such as `src/test_runner.py`, patches to plugin repositories, and appending a new test function to an existing test file after reading it (by design). Honest iteration on an agent's own new test (create, run, fix) is preserved (the first write decides newness unless an earlier read or diff modification contradicts). Expect the drop rate on real rollouts to be higher than the false-alarm rate measured in-sample.

#### Stage 3: AUTOREVIEW (Optional Compact Card Model Review)
- Enabled via `with_autoreview=True` (off by default).
- Evaluated **only** for rollouts flagged soft by L0 or that touch target surfaces; skipped on rollouts already hard-dropped by L0.
- Strictly guarded by `max_calls` and `max_model_usd` using measured per-call assistant usage cost.
- Formats a compact card and invokes the pinned model route.
- Verifies model attestation (`served_models == [route]` and `n_fallbacks == 0` parsed from session events); mismatches route to `review` with `AUTOREVIEW_UNATTESTED`.
- Unparseable output falls back safely to suspicion 50 and decision `review` with `AUTOREVIEW_UNPARSED`.
- If suspicion is at or above `suspicion_threshold` (default: 50) or verdict is `confirmed_cheat` or `suspicious`: escalates to `review` or `drop` (`autoreview_action`).

#### Stage Precedence
- `drop` > `review` > `keep`.
- Any hard drop at any stage finalizes the decision as `drop`.
- In the absence of drops, any review flag finalizes the decision as `review`.
- Only rollouts clearing all stages cleanly are marked `keep`.

---

`evallab.hack_drop` exposes the stage functions (`evaluate_task_stage`, `evaluate_l0_stage`) for the `evallab line` batch pipeline.

### 3. Schemas

#### Output Manifests (`kept.jsonl`, `dropped.jsonl`, `review.jsonl`)
In strict adherence to Third-Party Data Policy, output manifests contain **identifiers, reason codes, step numbers, and content digests only**, strictly zero raw transcript text, prompts, tool calls, or file contents:

```json
{
  "id": "format-code-task-001804:attempt-1",
  "task_id": "format-code-task-001804",
  "decision": "drop",
  "reasons": [
    "L0_HARD_TAMPER_SIGNATURE:\\bpytest_(runtest|configure|sessionfinish)",
    "L0_HARD_TARGET_SURFACE_WRITE"
  ],
  "evidence_steps": [2, 5],
  "stage_decisions": {
    "task": {"decision": "pass", "reasons": ["TASK_STATUS_NOT_EVALUATED"], "details": {}},
    "l0": {"decision": "drop", "reasons": ["L0_HARD_TARGET_SURFACE_WRITE"], "details": {}},
    "autoreview": {"decision": "pass", "reasons": ["AUTOREVIEW_DISABLED"], "details": {}}
  }
}
```

#### Certified Tasks Schema

The manifest builder is retired. Stored DEV manifests and the `hack_drop.load_certified_tasks` reader remain unchanged.

The manifest is `evallab.task_status/v1`, with `built_at`, `code_rev`,
`inputs[].path/sha256/kind`, and `tasks`:

```json
{
  "schema": "evallab.task_status/v1",
  "built_at": "2026-11-02T12:00:00+00:00",
  "code_rev": "<git revision>",
  "inputs": [{"path": "/evidence/certification.json", "sha256": "<64 hex digits>", "kind": "gold_certification"}],
  "tasks": [{
    "task_id": "example-task",
    "status": "provisional",
    "reason": "ALT_UNMEASURED: Task-side gates passed, but alternative correct solutions were not measured.",
    "package_digest": null,
    "evidence": [{"path": "/evidence/certification.json", "sha256": "<64 hex digits>", "kind": "gold_certification"}]
  }]
}
```

Statuses use this precedence, first match wins:

1. **uncertified / REFERENCE_FAILED**: a measured reference fix does not score 1.
2. **weak**: a nop, cheat attack, tamper witness, or `.pth` probe scored 1,
   or a held-out mutant survived on this grader.
3. **uncertified / WRONG_REJECTION_k_OF_n**: a known-correct alternative was rejected.
4. **certified / ALL_GATES_PASSED**: reference = 1, nop = 0, every attack and
   probe = 0, all mutation gates pass, and at least two operator-supplied
   independent correct solutions were replayed with all accepted.
5. **provisional / ALT_UNMEASURED** or **ALT_TOO_FEW_n**: all task-side gates
   pass, but alternatives were unmeasured or fewer than two were tested.
6. **uncertified / NO_EVIDENCE** or **INCOMPLETE_GATES**: no qualifying controls
   or missing/failed gates. Unknown/infrastructure rewards never count as kills.

Old receipts lacking `alternative_solutions_total` and
`alternative_solutions_accepted` mean **not measured**, never zero wrong
rejections. A known total with `accepted: null` is also unmeasured; an observed
wrong rejection still blocks certification, including a fractional reward below
1. Boolean, nonnumeric, nonfinite, and out-of-range rewards are invalid.
Counts are not summed across receipts, because the same solutions might have
been replayed twice. For the **same explicit package digest**, a complete passing
measurement supersedes an earlier unmeasured/planned Gate 7 count; planned
totals do not increase the measured denominator. Any observed wrong rejection
in either receipt still takes precedence, regardless of input order.

Each row cites the exact input bytes with SHA-256. Conflicting package digests
or unbound package versions abort: select the intended grader's receipts and
build separate manifests, never combine evidence from different batches into
a stronger certificate. Legacy eight-task Gold summaries and failed first-batch
Gold manifests are readable in place. A4B shipped-round receipts are also
readable, but their `upgraded_package_sha256` hashes only the supplementary test,
so it is **not** promoted to `package_digest`; a baseline-accepted subset of
held-out mutant pairs is not proof that the full mutation gate passed.
These legacy adapters preserve Gate 7 evidence when supplied as Gold receipts,
but cannot be passed as alternative-solution Gate 7 receipts; those require
the full TaskCertificationResult contract. A4B's `accepted_test` must be true.
Precomputed `status`, `is_weak`, and prose claims never determine the result.
Repeated files within one input kind are deduplicated; declaring the same file
under different input kinds is invalid.

The builder refuses label/rollout/trajectory/submission/adjudication file and
parent-directory names, and recursively rejects fields carrying run IDs,
outcomes, labels, decisions, or adjudications—even when nested beside a valid
task-side receipt. The only verdict-field exception is the top-level `verdict`
of an `evallab.task_admission/v1` AdmissionRecord. A8/A8B real-submission replay
reports are therefore **rejected inputs: carry run outcomes**. Their selected
correct patches would need a separately approved task-side Gate 7 run.
Nested path/dir/file/record fields and patch lists also pass the DEV path gate.
Task-store paths are parsed structurally (`tasks/<name>` or
`variants/<slug>/<digest>`); ambiguous identities fail closed. Every candidate
identity, including package basenames and resolved aliases, is checked against
CorpusRegistry TEST assignments, not just the displayed task ID. Output paths are DEV-gated too;
`~` is expanded before resolving paths against the workspace, and existing
output files are not overwritten. Invalid input exits 2 without creating output.

`hack_drop.load_certified_tasks` reads the builder's output unchanged. Older
dictionary/JSONL manifests and historical status strings (`gold`, `hardened`,
`weak`, `exploitable`, `unknown`) remain readable by that loader; new builders
emit only `certified`, `provisional`, `weak`, and `uncertified`. The task stage reports a
per-status reason (`TASK_CERTIFIED`, `TASK_PROVISIONAL`, `TASK_UNCERTIFIED`, `TASK_WEAK`,
`TASK_GOLD`, ...); a status outside the known list is treated as `uncertified`.

---

### 4. Hard Boundaries and Limits

1. **DEV Only:** Refuses input paths matching answer-key surfaces or known held-out patterns (`held-out`, `holdout`, `test-split`, `/eval/test/`, `test_labels.*`), and rejects any rollout item or task whose ID or `<task>__<hash>` stem is registered as TEST in `CorpusRegistry` with exit code 3 (`RefusedRun`).
2. **Determinism:** All stage evaluations (Task, L0, report aggregation) are purely functional and produce bit-for-bit identical results on identical inputs.
3. **Budget Guard:** When AutoReview is enabled, the execution harness checks `max_calls` and `max_model_usd` before each call using measured per-call assistant usage cost. If limits are reached, subsequent soft-flagged rollouts default safely to `review` with reason `AUTOREVIEW_BUDGET_EXCEEDED` (a single call may overshoot the dollar cap by its own cost).

## Verifier calibration and selection lift

## Core Principle: Execution Ground Truth as the Anchor

The evaluation lab possesses execution rewards from deterministic containerized runs (Harbor
test suites, pytest ctrf, oracles, and adversarial controls). Because execution outcomes
are unambiguous ground truth ($y \in \{0, 1\}$), LLM verifiers are never trusted blindly;
they are graded, calibrated, and evaluated for selection efficacy.

Two primary measurements are performed on execution ground truth:

1. **Selection Lift ($k \ge 3$)**: Using the verifier to select the best candidate among $k$
   attempts and measuring whether selection outperforms random sampling ($\text{pass@1}$)
   and approaches the oracle ceiling ($\text{oracle@k}$).
2. **Chance-Corrected Verifier Agreement**: Scoring individual rollout trajectories against
   execution ground truth using metrics robust to severe class imbalance.

---

## The Hard Architectural Boundary

> **Hard Boundary**: LLM verifiers **NEVER** replace execution verifiers in the battery,
> native package checks, or registry admission gates. This is an instrument experiment
> (`purpose=calibration` or `purpose=elicitation`).

This boundary is enforced structurally:
- **Dependency Isolation**: `llm-verifier` and its optional `verifier` extras group are removed from `pyproject.toml`.
- **Import Boundary**: No module in `evallab.task_lint`, `evallab.registry`, or the battery
  imports `llm_verifier`.

---

## 1. Selection Lift (Best-of-$k$)

For suite tasks with $k \ge 3$ execution attempts:

- **$\text{pass@1}$**: The baseline expectation from randomly selecting a single attempt:
  $$\text{pass@1} = \frac{1}{k} \sum_{i=1}^k r_i$$
- **$\text{selected@k}$**: The execution reward of the candidate picked by the verifier:
  $$\text{selected@k} = r_{j}, \quad j = \arg\max_{i} \hat{s}_i$$
- **$\text{oracle@k}$ (Oracle Ceiling)**: The theoretical ceiling achieved by perfect selection:
  $$\text{oracle@k} = \max_{i=1}^k r_i$$
- **Selection Lift**: $\Delta = \text{selected@k} - \text{pass@1}$.

### Uncertainty and Power Contracts

- Historical estimates use task-unit bootstrap 95% confidence intervals.
  Their archived implementation is `evallab.cohort.bootstrap_mean_interval`; see [the research archive](../research/ARCHIVE.md#round-8-stale-dispatch-and-exclusive-research-consumers).
- Historical reports render `"not distinguishable"` when task evidence cannot support an interval.
  The retained preflight API still uses `minimum_detectable_effect` and `pass_at_k_probability`.

---

## 2. Verifier Agreement on Unbalanced Corpora

### Never-Measured Trials Are Not Failures

An agent exception does not by itself establish whether the verifier measured
the final state. Preserve a recorded score even when an exception is present;
an absent, nonfinite, or `-1` reward remains unmeasured, not a failure.

- **Rule**: Unmeasured trials are excluded from agreement and confusion
  matrices and reported separately as `never_measured_trials`.

### Metric Choice for Class-Imbalanced Corpora

In real evaluation corpora, passes often dominate (e.g. 68 pass / 8 fail / 16 unmeasured in the
92-trial baseline corpus, pass prevalence 89.5%). On such data, **raw agreement is actively
misleading**: a trivial verifier that always predicts "pass" achieves 89.5% raw accuracy.

To expose degenerate verifiers and calibrate discriminating power, the lab computes:

1. **Cohen's Kappa ($\kappa$)**: Chance-corrected inter-rater agreement:
   $$\kappa = \frac{p_o - p_e}{1 - p_e}$$
   *An always-pass verifier yields $\kappa = 0.000$, unmasking zero informational value.*
2. **Balanced Accuracy**: Arithmetic mean of sensitivity and specificity:
   $$\text{Balanced Accuracy} = \frac{\text{TPR} + \text{TNR}}{2}$$
   *An always-pass verifier yields $\text{Balanced Accuracy} = 50.0\%$, equivalent to coin-tossing.*
3. **Matthews Correlation Coefficient (MCC)**: Pearson correlation between binary labels:
   $$\text{MCC} = \frac{TP \times TN - FP \times FN}{\sqrt{(TP+FP)(TP+FN)(TN+FP)(TN+FN)}}$$
4. **Macro F1**: Unweighted mean of per-class F1 scores ($F1_{\text{pass}}$ and $F1_{\text{fail}}$).

---

The old card generator is retired.
Preserve calibration records and use native reports or cached score receipts for current comparisons.

## Separate-verifier generation 10 and startup isolation

See [separate-verifier](mimo/separate-verifier.md).

## Nested Node build-output leakage

See [node-build-leak](mimo/node-build-leak.md).

The Seeker gym, teacher export, and training-data frontends are retired.
Their data, receipts, and split exclusions remain unchanged.

## 1. Hackbook — the catalog of known cheats

What it is: a versioned, machine-readable catalog of ways agents cheat and
tasks break — mechanism, signatures, a detection recipe, a probe template,
a fix pattern, and examples labelled `executed` / `observed` / `claimed`.

- Inputs/outputs: `load_hackbook` returns validated catalog entries. The private task matcher is retired.
  Trace tools use `evallab.tripwires.scan_trace` for step-level evidence, not a weighted catalog score.
  Data: `research/hackbook/entries.jsonl`.
- Models/lanes: no model is needed for catalog lookup or tripwire evidence.
- Measured: 57 entries over 18 classes (#1126, #1131, later #1148/#1158/#1160);
  Historical `coverage` output reports 57 entries (48 task-side, 9 run-side),
  39/57 with executed evidence, 211 examples (74 executed / 2 observed /
  135 claimed). On the `tb4-audit-v1` DEV split (25 tasks, 86 findings),
  strict 1-to-1 category recall 68.6% (59/86), precision 48.4%, F1 0.567.
- Caveats: that recall is mostly in-sample — 47 of the 59 matched DEV
  findings are cited as catalog examples; on uncited DEV findings recall
  is 12/28 (43%). The weighted trace matcher and its outcome gates are retired.
  Trace QA uses the shared tripwire engine.

### Compact, on-demand playbook cards

`evallab.hackbook.playbook_cards` packages twelve defensive diagnosis/repair
cards: six Task-QA/Reward-Seeker cards (`QA01`–`QA06`) and six Trace-Watcher
cards (`TW01`–`TW06`). It reads no trace database and makes no model calls.
This static mechanism library complements the Cloud Trace DB's compact
evidence-instance cards; it does not replace their provenance or expansion.

```python
from evallab.hackbook.playbook_cards import match_playbook_cards

fired = [name for name, state in flags["features"].items() if state["fired"]]
cards = match_playbook_cards(fired, kind="trace", max_cards=2)
```

Matching ranks distinct fired-flag overlap, then card ID; unknown flags
produce no fallback. Pass fired names, not a state mapping with false keys.
`kind="task"` selects Task QA and Reward Seeker. The default is at most 2 matched cards.
The catalog loader, card matcher, and mutation isolation remain supported.
The full-catalog listing helper and card formatter are retired.
The historical ~211-token median bundle included an observed trace-feature index, not only the cards.
Each complete v1 card measured 70–90 `o200k_base` tokens with `tiktoken==0.12.0`.
Runtime adds no tokenizer dependency or universal token cap.
Fetch cited step windows only on demand, using the same observed prefix and
zero-based supplied-sequence positions as the index.

DEV distillation on 2026-10-27 covered 72/72 confirmed Tier-1/Tier-2 issue rows
and 1,607/1,609 Tier-2 Goodfire exploit-labeled runs by mechanism category.
These are overlapping, in-sample content supports, **not detector precision
or recall**. `label:*` triggers (TW04) and `issue:*` triggers (QA05) are offline
evidence tags, not invented live feature detectors. Flags and cards remain
leads, never cheating verdicts; missing receipts can mean incomplete capture.

## External source and test boundaries

Treat external trajectories as Zone 01 material, never as locally measured outcomes.
Pin the source revision, raw byte hash, license, native schema, and scaffold/model identity.
Convert to a new projection without replacing the retained raw bytes.
A task mirror is not a trajectory corpus.
Missing step tokens and cost remain null; aggregate totals do not fill step-level gaps.
Cross-vendor availability is not comparable evidence without matched setup and verifier identity.
Public availability does not establish training, actor, or redistribution rights.

VerifierCheck's red/blue frontend is retired.
Its retained survivors remain candidates until review against the frozen requirement map.
An equivalent mutant does not prove a grader defect.
A failing oracle or passing empty control invalidates hackability claims from that task path.

Queue tests retain conservation, unique dispatch, legal transitions, and explicit billable admission.
Tests use isolated databases and injected corpus fixtures, not machine-local stores.
Do not write to the live catalog or infer corpus availability from a developer's checkout.

