# Failure cards and agent packs

`failure-card/v1` compiles the Failure Library into existing Hackbook mechanisms.
`mechanisms.json` contains generic prose drafted by two subscription-model workers
over compact DEV example bundles and the existing Hackbook. `drafting.json` records
the model, input/response digests and zero metered cost. Model text never supplies
citations: the compiler joins and validates every example against the admitted
library snapshot. It makes no model calls, database writes or network requests.

## Rebuild and retrieve

```sh
# Rebuild cards, Hackbook extension proposals and the evidence census.
uv run evallab failures pack --for task-qa \
  --build-dir research/failure-library/knowledge

# All three commands print bounded Markdown; directories must not already exist.
uv run evallab failures pack --for task-qa --task terminal-bench/fix-git \
  --max-chars 8000 --output-dir /tmp/failure-task-qa
uv run evallab failures pack --for trace-qa --task terminal-bench/fix-git \
  --max-chars 8000 --output-dir /tmp/failure-trace-qa
uv run evallab failures pack --for seeker --task terminal-bench/fix-git \
  --max-chars 8000 --output-dir /tmp/failure-seeker
```

Use the actual library `source_id/task_id`; `--task-text instruction.md` optionally
adds blind instruction text to the lexical query. `--top-k` bounds similar examples.
`--exclude-id` is repeatable and excludes frozen canary finding/run ids **and their
whole subject group**, including linked runs. The loop must freeze these exclusions
before producing its ON packets. An empty successful pack has `status=empty`; a
failed build returns exit code 2 with `status=failed`, not a ready-looking artifact.

## Agent-native files

Every output has `pack.md` and `pack.manifest.json` with status, selected ids,
exclusions, library digest and each file's SHA-256/character count.

- **Task QA:** `playbook.md` and `rubric.toml`. The rubric keeps Harbor's existing
  criterion names/descriptions and appends the pack once to
  `functional_verification` guidance (or the first supplied criterion). Use
  `qa task --stages S1 --rubric .../rubric.toml`. Exporting a rubric uses the same
  optional Harbor runtime as Task QA; `--rubric-base existing.toml` supplies an
  explicit baseline instead. The Markdown has the shape of `qa.env_playbooks`.
- **Trace QA:** `task_priors.json` has the existing `task_id`, `task_digest`, `priors`
  wrapper. A prior carries `source`, `digest_expected`, `digest_found`, `matched`
  and `record`. `matched=false` and `record.kind=dev_analogy` explicitly forbid
  treating another task's evidence as an audit of this subject's version.
- **Seeker:** `hackbook.md`, the existing seeker workspace packet filename. It
  describes generic reward/intent mismatch hypotheses and counterexamples, not
  executable bypasses or instructions to modify grading infrastructure.

Agent module ownership stays unchanged. Packs are exported artifacts; creating one
does not silently modify a live agent or claim an executed ON/OFF improvement.

## Admission, grouping and evidence

- Only library `DEV` records enter knowledge. The compiler imports
  `admitted_cloud_task_ids`, the structured admission gate used by
  `CloudTraceRetriever`, including its permanent seals, digest seals and stricter
  cross-key split check. Declaring DEV cannot override another key's TEST lock.
  CheatBench/HackBench sources are refused even if mislabeled DEV.
- Every citation contains the task's upstream repository/revision, collection,
  source URL, evidence references, tier and any known upstream fix. Run labels
  remain labels, never promoted to replay-confirmed evidence. Local run bytes
  must match a committed manifest. Missing local collections are named in
  `build.json`; unmanifested run files are never read.
- The split ledger's `unit` is the family/template grouping authority. Same task
  names are excluded across aliases and revisions, and shared units are excluded
  across keys. If either subject lacks authoritative grouping, retrieval
  conservatively excludes the entire matching source/upstream repository rather
  than guessing a family from a filename. This is stricter, not a leakage bypass.
- Protected identities are also screened in prose and returned evidence text.
  Subject exclusions are applied before BM25 and card compilation, not after an
  unsafe cached card has already supplied its citations.
- BM25 ranks summaries, generic card cues and declared in-checkout task instruction
  text. It reads no reference solutions or run transcripts. Equal scores use
  stable ids. Complete blocks are admitted under `--max-chars`; citations are
  never cut mid-string.

## Card and proposal semantics

Each card extends one existing Hackbook id, with what breaks, task-file cues,
run cues, a reward/intent mismatch hypothesis, repairs, diagnostic counterexample
conditions, 3–10 real DEV example ids and prevalence by collection. Counterexample
conditions are **not** fabricated negative labels on runs: unknown is not clean.
Prevalence denominators are admitted DEV library records in that collection, not
all benchmark tasks or a measured real-world failure rate.

Nearest lexical mechanisms are selected within each finding's TS class. Sparse
sub-mechanisms fold into that class's strongest supported mechanism. Classes with
fewer than three real DEV ids are reported in `insufficient_dev_support`, not
filled with invented citations. Rebuilding after ingestion can add cards without
changing code or model prose. Evidence tiers do not change when a task is fixed.

`hackbook-proposals.jsonl` contains validated `evallab.hackbook.entry/v0`
extensions using the **same** Hackbook ids. It never edits the canonical
`research/hackbook/entries.jsonl`. The proposal drops old example lists and uses
only newly validated DEV citations. Sources outside the existing Hackbook source
enum keep their full provenance in the linked card instead of being mislabeled as
another publisher. Tripwires owns accepting these proposals.
