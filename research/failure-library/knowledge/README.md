# Defensive failure knowledge

This slice exports defensive **Task QA** and **Trace QA** knowledge. It does not
export Seeker packets, attack templates, exploit payloads or execution campaigns.
Historical behavior records remain in the provenance-preserving library.

## Two explicitly different knowledge modes

- **Evidence-backed DEV retrieval:** `compile_library` admits only DEV records using
  the shared CloudTraceRetriever structured gate and cross-key seals. It compiles
  `failure-card/v1` mechanisms against existing Hackbook ids. Every card has 3–10
  real admitted citations; versions, evidence references and source labels survive.
- **Curated taxonomy only:** `compile_curated` reads only the pre-existing
  `research/hackbook/taxonomy.json` fields `id`, `name`, `definition`, `fix_location`.
  It does not read library examples, transcripts, or the example-conditioned
  `mechanisms.json`. It supplies category definitions and contract-conditioned
  defensive validation cues, with **no examples, citations or support counts**.

When subject/family/template identity cannot be certified, exact-example retrieval
remains fail-closed. Generic taxonomy guidance remains available and is explicitly
marked `curated_only`, never represented as an observed finding. Manifests separate
`curated_guidance_ids` (`FG-TS..`/`FG-RS..`) from evidence-backed `card_ids` and
`example_ids`; `identity_uncertainties` makes the limitation visible.

## Usage

```sh
uv run --extra laminar evallab failures pack --for task-qa --task terminal-bench/html-js-filter \
  --exclude-id fl-215a4657118e2136 --exclude-task terminal-bench/html-js-filter \
  --top-k 2 --max-chars 12000 --output-dir /tmp/defensive-task-qa
uv run evallab failures pack --for trace-qa --curated-only \
  --task native-source/native-task --max-chars 8000 \
  --output-dir /tmp/defensive-taxonomy-trace-qa
```

Both commands print bounded Markdown. Output directories must be new.
The default Task QA baseline rubric comes from the pinned optional Harbor runtime
(`--extra laminar`); alternatively supply an existing baseline via `--rubric-base`.
`--exclude-id` accepts admitted raw ids or `COLLECTION::RAW_ID` and is repeatable.
`--exclude-task SOURCE/TASK` also handles clean controls without library records;
`excluded_tasks` preserves these explicit inputs in the manifest. Unresolved ids
block exact examples, not source-independent guidance. Conflicting global record
ids are errors rather than silent publisher selection.

Task QA exports `playbook.md` and `rubric.toml`, preserving baseline criterion names
and descriptions. Trace QA exports the existing `task_priors.json` wrapper with
`matched=false`, and distinguishes `record.kind=curated_guidance` from
`dev_analogy`. No artifact silently modifies a live agent or proves an ON/OFF gain.

## Evidence and provenance

Source tiers and status fields are **source-record assertions**, not independent
verification by this compiler. A reproducer path, publisher execution statement or
model agreement does not establish a local replay. Native oracle outcomes,
publisher reports, constructed cases and human/judge labels retain their source
context. Hackbook proposal examples are conservatively `claimed`; no V3 label is
automatically relabeled as our execution. Goodfire is never relabeled as Terminal
Wrench. Collections without an approved exact-source enum mapping are omitted from
proposals with an explicit manifest reason; their distinct provenance remains in
cards. The required Hackbook `exploit_template` field is only `applicable=false`.

Run JSONL is parsed and hashed from the same open file before any pack is published;
admission batches are bounded. A finding link transfers a TS class to a run only
when task-version digests match or source/task/repository/revision pins agree.
Conflicting known digests refuse that inference. Identity-only links remain useful
historical relationships, not proof that an older run reproduces a task defect.

## Integration checkpoint

Three subscription workers originally drafted empirical mechanism text from compact
DEV bundles. The active draft projection now contains defensive fields only. Raw
responses and previous generated artifacts are retained in local read-only custody;
drafting receipts describe this projection. These empirical drafts are **not** the
source-independent taxonomy fallback.

The integrated corrected library was rebuilt through the actual pack CLI:
**141,994 admitted DEV examples, 30 evidence-backed cards, zero missing run
collections**. Ten proposals fit the current exact-source Hackbook enum; all
attacker-template fields are disabled/empty, and other source mappings remain
explicitly omitted rather than relabelled.

`integration-smoke.json` records the exercised current exports: a known-ledger
Task subject retrieved four disjoint DEV examples, while both an explicitly
curated Trace subject and an unresolved subject used twelve taxonomy entries
with zero empirical examples. Packet hashes, native rubric loading, exclusion
seals and `matched=false` priors were checked. This proves knowledge delivery,
not model improvement. `smoke.json` remains an explicitly historical receipt of
the earlier pre-correction build, not current verification.
