Status: review-wanted
Last: session bridge made usable both directions; UNIQUE (session_id) refused with a Postgres proof it would abort a multi-agent ingest; redaction-into-spans guard pinned and mutation-controlled
Next: integrator review and merge of `role/trace-join`; TRACEGRAPH follow-ups listed below need their own mission
Blockers: none

TRACE — make the existing `session_id` bridge trustworthy and usable.
Lane: Platform (`src/`, `tests/`, `sql/` schema) plus its own docs page.

Lease written: `sql/schema.sql`, `src/evallab/tracing.py`,
`tests/test_trace_join.py` (new), `docs/observability.md`, this file.
Not touched: `cli.py`, `queue.py`, `atif.py`, `facts.py`, `explorer.py`,
`dashboard/`, `policy/`.

## What was confirmed before writing anything

The premise held and the conclusion did not. Converted spans carry no
`spec_id`/`job_id`/`trial_id`, but `session.id` on the root span is the ATIF
`session_id` verbatim (`harbor_atif2otel/convert.py:212`), which is
`trajectory_documents.session_id`, which reaches `trial_id -> job_id ->
experiment_id`. Verified live, read-only, against the shared catalog:

```
session.id 01a0043a-4b83-7252-a594-fa289617124f
  -> trial  aa94250c-4f6c-4b66-bf20-1c36cd371133  event-summary__5E3btLv (codex)
  -> job    cce77192-10b9-4f82-8f29-2e0545844c68  canary-event-summary-codex-20260815
  -> experiment 01M021T5SMYY9E4EBCCMNF43A6
```

That trial is also committed evidence at
`research/evidence/runs/canary-event-summary-codex-20260815/event-summary__5E3btLv/`,
and its `result.json` id equals the catalog `trials.id`, so the committed
bundle and the catalog row are the same trial. That is what makes the new test
file a test over committed evidence rather than over a fixture.

Catalog measured at start and at finish, unchanged: **72 jobs, 23
`trajectory_documents`**, 23/23 `session_id` populated and distinct, no trial
holding more than one document.

## Uniqueness: refused, and why

**Uniqueness holds only by accident.** `proven live`.

All 23 rows are distinct, but every one of them is a flat single-agent
`agent/trajectory.json` with `embedded_path` NULL. Two mechanisms break
distinctness the moment the population widens, and the first one is fatal to a
constraint:

1. **Embedded subagents share a `session_id`.** `harbor_atif2otel/ids.py:45-55`
   says so in as many words — it scopes span seeds by `trajectory_id`
   *specifically* "to handle embedded subagents that share a session_id" — and
   `harbor_atif2otel/validate.py:67` requires `trajectory_id` on every embedded
   subagent for that reason. `evallab.atif._flatten_payloads`
   (`src/evallab/atif.py:405-415`) writes **one `trajectory_documents` row per
   flattened payload**, all under the same `trial_id`. So one multi-agent trial
   legitimately produces several rows with one `session_id`.

   Proven in a throwaway database (`trace_join_scratch`, created and dropped;
   the shared catalog was never written):

   ```
   ALTER TABLE trajectory_documents ADD CONSTRAINT td_session_unique UNIQUE (session_id);
   -- parent document                    -> INSERT 0 1
   -- its subagent document, same trial  -> ERROR:  duplicate key value violates
   --                                       unique constraint "td_session_unique"
   ```

   A `UNIQUE (session_id)` does not protect the join; it **aborts the ingest of
   the first multi-agent Codex trajectory the lab ever records**. That is
   exactly the "wrong in principle, breaks a future real ingest" case.

2. **Continuations are a false negative.** Harbor writes a resumed session as
   `<base>-cont-N` and `base_session_id` strips the suffix when seeding the
   trace (`ids.py:18-31`). Those rows are textually distinct, so they *satisfy*
   a unique constraint while still collapsing to one `trace_id`. Uniqueness on
   the raw column would advertise a one-trace-one-trial guarantee it does not
   deliver.

One mechanism I checked and **discarded** rather than report as evidence:
promotion copying a job into `research/evidence/runs/`. It cannot duplicate a
session, because `trials.id` is Harbor's own `result.json` id and
`trajectory_documents.id` is `_stable_id(trial.id, source_path,
embedded_path)`, so a copy re-ingests onto the same keys. The catalog does hold
two jobs named `event-summary-nop-evidence` and two named
`event-summary-oracle-evidence`, but their trial names differ
(`event-summary__edzDz6R` vs `event-summary__AHNxbSA`) — those are separate
runs, not a promoted copy. Recording this so the next reader does not repeat
the false lead.

**What shipped instead**: a non-unique index, idempotent per `AGENTS.md`, with
the refusal recorded at the exact line a future agent would be tempted to add
the constraint.

```sql
CREATE INDEX IF NOT EXISTS trajectory_documents_session_idx
    ON trajectory_documents (session_id);
```

Idempotence `proven live`: `sql/schema.sql` replayed twice into
`trace_join_scratch` with `ON_ERROR_STOP=1`, both passes clean, second pass
`NOTICE: relation "trajectory_documents_session_idx" already exists, skipping`.
The shared catalog was **not** migrated — it still shows `Seq Scan` for a
`session_id` filter, and will pick the index up on its next `initialize()`.

## The join, both directions

`src/evallab/tracing.py`, no new CLI surface (`cli.py` is another mission's
lease — see below).

- **Trial -> trace.** `trace_identity(trajectory)` /
  `trace_identity_for_trial(trial_dir)` return `session_id`,
  `base_session_id`, `trajectory_id`, `trace_id`, `root_span_id`. They call
  `atif2otel`'s own seed functions rather than reimplementing them, and a test
  asserts the result equals the ids in the converted payload, so the two cannot
  drift.
- **Trace -> research graph.** `session_lookup_sql(placeholder)` is the join
  text; `resolve_session(session_id, fetch=...)` runs it through a
  caller-supplied cursor. `placeholder` exists so the same SQL runs under
  psycopg (`%s`) and under DuckDB (`?`) in tests — the test exercises the real
  join text, not a paraphrase.
- It returns a `SessionResolution`, deliberately a set. `.trial` gives the one
  trial or raises `TraceError`. Several documents for one trial (subagent
  fan-out) is a normal answer; several *trials* is refused by name. That is the
  fan-out protection the unique constraint was supposed to give, implemented
  where it is actually true.

Phoenix stays derived and disposable: nothing added here reads Phoenix, writes
Phoenix, or treats a resolution as evidence. No POST was made; the only
conversion command run was
`evallab trace research/evidence/runs/.../event-summary__5E3btLv --dry-run`
(`traced 1  skipped 0  failed 0`, `spans=12 root=codex kinds=AGENT,LLM,TOOL`).

## The redaction guard

`input.value` on the root span is the `<<evallab-redacted: N bytes,
sha256:...>>` marker verbatim — confirmed, and now pinned over **every**
committed Codex bundle, not just one.

One correction to the brief's framing, found while writing it: the marker does
**not** always arrive as a standalone attribute value. On LLM spans the
converter folds the step into a JSON-encoded message list, so the marker
reaches the payload embedded in a larger string
(`[{"role": "user", "content": "<<evallab-redacted: 710 bytes, ...>>"}]`). A
test asserting equality against whole attribute values fails on real evidence.
The guard therefore matches markers as substrings against a pinned shape and
asserts two things: every marker on a span is a whole, unaltered marker from
the source document, and no string holds a marker prefix that no complete
marker accounts for. The second half catches a marker sliced by `atif2otel`'s
attribute truncation — which would still read as redacted while having lost the
digest.

`proven` that the guard is not vacuous, two ways:

- In-suite mutation control
  (`test_spans_would_leak_if_promotion_stopped_redacting`): the same document
  with markers replaced by plaintext puts the plaintext in `input.value`, and
  the payload then carries zero markers.
- Directly, running the guard's own assertions over both documents:

  ```
  as promoted          GUARD PASSES  (markers on spans: 3)
  redaction bypassed   GUARD FAILS   -> redaction did not reach the spans at all
  ```

The plaintext itself is genuinely absent from the repository, so it cannot be
searched for; the mutation control is the only honest way to demonstrate
sensitivity, which is why it is written that way.

## Verification

- `uv run pytest` — 583 passed (18 new in `tests/test_trace_join.py`).
- `uv run ruff check .` — All checks passed.
- `uvx ty@0.0.71 check src/ --output-format=concise` — 28 diagnostics, under
  the 33 ratchet, none in the new code (the two `tracing.py` entries are the
  pre-existing optional `litellm`/`dspy` imports).
- Shared catalog re-checked at finish: 72 jobs, 23 `trajectory_documents`.
- No paid agent invoked. No Phoenix POST. No `docker compose` command. No
  LaunchAgent touched.

## What a future TRACEGRAPH mission still needs

Recorded here rather than half-built, in lease order.

1. **No command exposes any of this.** `cli.py` is leased exclusively to
   GateAuthorization in this batch, so nothing was added to it. The natural
   surface is `evallab trace --identity <trial>` (print the trace id / session
   id for a trial) and `evallab trace --resolve <session-id>` (print
   experiment/job/trial). Both are thin wrappers over
   `trace_identity_for_trial` and `resolve_session`; the integrator should
   sequence them after the `cli.py` lease clears. Note the open `cli.py`
   command-registry question on the board — these two commands are exactly the
   kind of addition that question is about.
2. **`resolve_session` has no production caller.** It takes an injected
   `fetch`, and nothing in `src/` wires it to `evallab.database`. Deliberate:
   the connection-owning module is not in this lease and a caller with no
   command behind it would be dead code. One function in a leased module plus
   the CLI hop in (1) closes it.
3. **The live catalog has no session index yet.** It ships in `sql/schema.sql`
   and lands on the next `initialize()`; nobody re-migrated the shared catalog
   from a worktree. Worth confirming during the next reindex.
4. **`base_session_id` has no catalog column.** Continuation grouping — "show
   me every document in this trace" — currently requires stripping `-cont-N` in
   the query. If continued sessions ever appear in real runs, a stored
   `base_session_id` (or a functional index) is the cheap fix. No continuation
   exists in the 23 rows today, so this is `designed`, not needed yet.
5. **The reverse direction is untested against Postgres.** The join runs live
   (result quoted above) but the automated test runs it under DuckDB to stay
   deterministic per `agents/CHECKS.md`. A Postgres-backed integration test
   belongs with M014's CI work, not here.
6. **Nothing verifies a shipped trace.** `--dry-run` proves conversion; nobody
   has confirmed that a trace, once in Phoenix, is findable by `session.id`
   from the UI or API. That needs a Phoenix POST, which this mission was
   forbidden to make and which should stay an integrator acceptance step.
