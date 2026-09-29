# Viewer trials — harbor view, evallab traj/report, Phoenix (2026-09-29)

Trials: HAR-81 `har81-l-d-a4-arvo-42496599` (long, continuation + summarization),
HAR-81 `har81-l-d-a2-arvo-18737` (short), HAR-90 `har90-mimo-0036-e` (short, passed).
Raw jobs copied to `trace-lab/viewers/harbor-raw-jobs/`; normalized jobs copied to
`trace-lab/viewers/harbor-norm-jobs/` (copies, not symlinks: harbor view's path
guard `resolved_jobs_dir in job_dir.parents` 400s symlinked job dirs with
`{"detail":"Invalid job name"}`).

Basis per claim: executed (API/CLI I ran) unless marked source-read/docs-read.

## 1. `harbor view` (harbor==0.21.0) — executed

- Raw: http://127.0.0.1:8601 — Norm: http://127.0.0.1:8602 (both running, named
  services `harbor-view-raw`, `harbor-view-norm`).
- `/api/.../trajectory` step counts (executed):
  - long: raw **61 steps / 0 with tool_calls** vs normalized **102 / 97**.
    Raw head has 61 steps + `trajectory.cont-1.json` has 42 more; harbor view reads
    the head file only — **no continuation stitching** (61 of 103 raw steps visible).
  - short: raw **118 / 0** vs normalized **118 / 117** (single file, so no
    stitching gap; only the tool-call gap).
  - har90: raw **84 / 0** vs normalized **84 / 33**.
- UI (screenshots/): raw steps render the message JSON (in-text `<tool_call>` XML
  visible as text) with observation, but **no tool-call chips**; normalized steps
  render a `bash_command keystrokes: ...` chip under each message
  (`harbor-normalized-trial-toolchip.png`, executed/observed).
- `/api/.../verifier-output` returns `{stdout, stderr, ctrf, reward, reward_details}`
  on both; har90 shows reward null + 98-char stdout + CTRF pytest payload (executed).
  Trial page has Outcome/Reward/Error/Tokens/Timing header + tabs
  Trajectory|Agent|Verifier|Artifacts|Config|Lock|Log|Exception|Analysis (observed).
- Search/filter (observed): jobs page has "Search for jobs..." + All
  agents/providers/models comboboxes; job page has task search + the same filters
  (`/api/jobs/filters`, `/api/jobs/{job}/tasks/filters` return facet counts,
  executed). No cross-trial full-text search over commands seen.
- Labels/annotations: none (no annotation affordance found).
- Recording: `/recording` returns `{"available":false,...}` on both — the viewer
  looks for OSWorld-style `agent/recording.mp4`, not Terminus `recording.cast`
  (executed; raw trial does have `recording.cast`, viewer ignores it).
- Install/startup (executed): harbor==0.21.0 `uv` env ≈ **410 MB / 201 packages**;
  `harbor view --help` warm 1.6 s; server ready in a few seconds warm (first run
  resolves env + may build frontend).

## 2. Eval Lab views (har90-modal-mimo worktree, read-only) — executed

- `traj outline|card` and `report run` need **no Postgres**, no writes (ran clean).
- Jail gotcha: paths outside the checkout are refused (`path_escapes_jail`); the
  read-only escape hatch is `EVALLAB_RUNS_ROOT=<dir>` (source-read
  `src/evallab/storage/paths.py:resolve_runs_roots`), which I pointed at my
  `viewers/` copies. No eval-lab state touched.
- `traj outline`:
  - Raw short: 118 steps, **Tools: 0**; step highlights show the in-text
    `<tool_call><function=bash>...` tags inside message text (parsed for display,
    not counted).
  - Normalized short: 118 steps, **Tools: 117** (`bash_command:116,
    mark_task_complete:1`); loop suspicion detected (`repeated_consecutive_command`).
  - Raw long: reads the continuation chain and **stitches** (source cites the
    cont file): **99 steps** (61 head + 42 cont − 4 superseded dupes), Tools: 0.
    Stitch logic: source-read `traj.py:resolve_chain`, `stitched_chain_action_steps`.
  - Normalized long: 99 steps, **129 tool calls** (`bash_command:129`; some steps
    carry several calls). Warm runtime 0.7 s.
- `traj card` (raw short → `viewers/traj-card-raw-short.md`, 87 lines): identity +
  outcome + mechanical baseline table (**Tool Calls 0 (0)**, screening ratios NULL
  with 0 calls), loop/intervention provenance, SHA-256 source citations, C0
  screening block. No tool-call recovery on raw (native `tool_calls` only).
- `report run`: **replays in-text `<tool_call>` tags**, so raw short reports
  "118 steps, **117 tool calls**, 2 errors, 96 repeated actions" with per-step
  slowest-exec table, token/time/verifier sections — identical numbers on the
  normalized copy. Best raw-data UX of the three for "what did it run".
- Labels: none of the three attach/display probe-03 labels; card cites file SHAs
  but no label field.

## 3. Phoenix 20.16.0 (local, `uv run --with arize-phoenix`) — executed

- Server: http://127.0.0.1:6006 (named service `phoenix-viewer`).
- Uploaded via bundled `phoenix.client.helpers.atif.upload_atif_trajectories_as_spans`
  (docs-read: helper docstring; supports ATIF v1.0–v1.7, one AGENT root per
  trajectory, `iteration N` CHAIN spans, `metadata.atif.step_id`).
  - `tracelab-raw`: OK, 522 spans = 3 AGENT + 260 CHAIN + 259 LLM + **0 TOOL**.
  - `tracelab-normalized`: **rejected as-is** — `ValueError: steps[62]: tool_calls
    must be a list` (normalizer emits explicit `tool_calls: null`; raw files omit
    the key). Uploaded after in-memory null→[] (no files changed): 887 spans,
    **289 TOOL** (`bash_command:288, mark_task_complete:1`). Per-trace CHAIN
    counts: 98 / 117 / 83 ≈ agent-step counts. **Interop note for the normalizer:
    prefer `[]` over `null`, or Phoenix needs a shim.**
- Span reads via API work (executed): filtered TOOL query, AGENT spans per trace.
- Annotations (executed): `add_span_annotation` round-trips
  (`probe-03:outcome_relevant_failure`, label `completion/unclear` + rule/refs in
  metadata, id `U3BhbkFubm90YXRpb246MQ==`, readable via
  `get_span_annotations_dataframe`). **But: with ANY span annotation present
  (full or minimal label-only), the `spanAnnotationSummaries { count }` resolver
  errors** (`an unexpected error occurred` at that span's edge — reproduced via
  GraphQL), which is the query the trace-detail UI issues, so the annotated
  trace's detail page shows Phoenix's "Something went wrong". Deleting the
  annotation (GraphQL `deleteSpanAnnotations`) restores the query. Annotations are
  API-possible, UI-fragile on Phoenix 20.16.0 — I left the projects
  annotation-free so Peter sees working pages.
- Trace detail renders the span tree (AGENT terminus-2 → `iteration N` → LLM +
  `bash_command` TOOL spans, executed/observed via DOM after an in-app table-row
  click). Two quirks: (a) navigating to a trace URL directly (goto/address bar)
  leaves the traces list mounted — the detail loader only fires on the in-app
  link click (with `selectedSpanNodeId`); (b) see annotation bug above.
  `phoenix-trace-detail.pdf` captured the list (background-tab print), so visual
  proof of the waterfall is still missing; the span-tree text + span API counts
  are the executed evidence.
- Install/startup (executed): `arize-phoenix==20.16.0` env ≈ **688 MB** (largest
  of the three); first download ~36 s; server 127.0.0.1:6006 answered 200 after
  ~20–40 s cold start.

## Verdicts (plain language)

- **harbor view** is the trial drill-down: reward/error/tokens/timing header,
  full step text, verifier output, job→task→trial navigation with facet filters.
  Good for "open this trial and read it". Annoyances: no continuation stitching
  (long MiMo runs show head only), zero tool-call visibility on raw_content runs,
  symlink-unfriendly path guard, no labels. Keep the **normalized** instance
  (8602) as the daily driver.
- **evallab `report run`** is the best raw-data reader: stitches continuations and
  replays in-text tool calls, so it sees 117/117 commands where harbor view and
  outline/card see 0. `outline` is the 1-second skim; `card` is the citable
  record (SHAs, C0 screening). Annoyances: path jail needs `EVALLAB_RUNS_ROOT`
  for outside dirs; outline/card count only native `tool_calls` (blind on raw).
- **Phoenix** is the span-tree overview: per-step latency shapes, TOOL-span
  counts, cross-trial tables/charts. Raw imports lose everything tool-shaped
  (0 TOOL spans); normalized lights up. Annoyances: heaviest install (688 MB),
  strictest importer (rejects `tool_calls: null`), annotations API-work but crash
  the trace-detail summary query on 20.16.0 — labels live in probe-03 JSON, not
  Phoenix, for now.
