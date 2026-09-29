# Scout over HAR-81 + HAR-90 (local, $0, no models)

Inspect Scout as a reproducible transcript-scanning layer over 54 Harbor
trials (44 HAR-81 + 10 HAR-90): raw fragments and normalized whole-runs
side by side, plus our probe-03 capability labels surfaced inside
`scout view` with clickable message citations. No model calls, no uploads,
no trial launches. All commands run from this directory.

Pinned versions: `inspect-scout==0.5.3` (checked PyPI 2026-09-29: 0.5.3 is
still the latest release), `harbor==0.21.0` (matches the Eval Lab runs),
Python 3.12. Everything via
`uv run --no-project --python 3.12 --with inspect-scout==0.5.3 --with harbor==0.21.0 ...`
so nothing global changes. Extra packages only for reading results:
`--with pyarrow --with pandas`. Set `UV=...` prefix once:

```sh
UV="uv run --no-project --python 3.12 --with inspect-scout==0.5.3 --with harbor==0.21.0"
```

## Layout

- `data/transcripts_norm/` — 54 normalized transcripts (one per trial).
- `data/transcripts_raw/` — 71 raw transcripts across the same 54 trials
  (39 trials x 1 file, 14 x 2, 1 x 4).
- `scans/` — two current scans (see Scan). `scans_archive/` holds the two
  superseded HAR-90-only scans from the prior setup
  (`scan_id=WHnhfogjpsQQjZ9zQUA5s3`: 16 transcripts x 3 scanners = 48 rows;
  plus `scan_id=WeuebA7h4i7aMnq6d525mH`).
- `scanners.py` — 7 deterministic scanners (no model calls).
- `probe03_lookup.json` — probe-03 labels by trial (built by
  `build_probe03.py` from the read-only probe-03 `capabilities.jsonl`).
- `validation/wedge_stuck.json` — 54 trial-level stuck-terminal cases from
  the blind har99-wedge hand key (built by `build_wedge_validation.py`
  after the normalized import; ids are that DB's transcript ids).
- `raw_trials.py`, `import_raw54.sh`, `build_probe03.py`,
  `build_wedge_validation.py` — reproducibility scripts ($0, local).
- `screenshots/` — view captures (see View).

## Import

Normalized (2 commands; only `agent/trajectory.json` carries
`schema_version`, so each root yields exactly one transcript per trial):

```sh
echo "1" | $UV scout import atif -T ./data/transcripts_norm \
  -P path=../normalized/har81
echo "1" | $UV scout import atif -T ./data/transcripts_norm \
  -P path=../normalized/har90
```

Raw (one import per trial dir; the loop answers the `1 = add` prompt):

```sh
./import_raw54.sh   # trial list from raw_trials.py (manifests -> worktrees)
```

Do not point one import at a `runs/` root: besides sweeping
`.executor/*.state.json` noise it pulls trials outside our 54 (the HAR-90
worktree also holds `har90-mimo-0036-g/h` and `har90-mimo-0758-e/f`, which
have no probe-03 labels or normalized counterparts). First attempt did
exactly this (116 dirs -> 58 trials); the DB was rebuilt to the exact 54.

## Raw vs normalized (executed numbers)

| | raw DB | normalized DB |
|---|---|---|
| trials | 54 | 54 |
| transcripts | 71 (39x1, 14x2, 1x4 files) | 54 (one per trial) |
| messages | 15,287 | 11,733 |
| structured `tool_calls` | **0** | **4,236** (3,767 msgs) |
| scan rows (7 scanners) | 852, 0 errors | 648, 0 errors |
| `restored_tool_calls` grep | bash 0, complete 0 | bash **4,186**, complete **50** (= 4,236) |
| `native_tool_call_markup` | 7,712 / 7,712 | 5,943 / 5,943 |
| `harness_parse_errors` `no_valid_json` | 6,340 | 3,301 |

Raw is inflated by double-counting: the 0036-f duplicate cont, the 0758-c
superset head+cont, and summarization files that import as transcripts when
nothing references them. Raw `tool_calls` are empty even on HAR-81 (its
executed calls live in `extra.step_layers`, which Scout drops) — the
`restored_tool_calls` gap (0 vs 4,236) is the visibility comparison.

How the ATIF import maps (source-read in
`inspect_scout/sources/_atif/*.py`, verified against our DBs):

- One trajectory file = one transcript. `transcript_id` =
  `<session_id>-<file stem>`; continuations share `source_id` (the session)
  but stay separate transcripts. Normalized files stitch continuations, so
  each trial is one transcript.
- Each step yields its own message (system/user/assistant by `source`) plus
  one tool message per `observation.results` entry; `tool_calls` become
  structured calls on the assistant message and render in text as
  `Tool Call: <fn>`. System steps with `extra.context_management` yield no
  message (walk predicts stored counts exactly, e.g. 206/206 on stitched
  trial har81-p-d-arvo-41330).
- **`extra` is dropped**: every imported message has `metadata: None`; only
  `schema_version` / `continued_trajectory_ref` reach transcript metadata.
  So `extra.trace_lab.ref` does NOT survive — probe-03 step refs are mapped
  by re-walking the source file in `scanners.py` (`_ref_index_map`), keyed
  by `trace_lab.ref` on normalized files and `<filename>#<step_id>` (with
  `head` aliasing `trajectory.json`) on raw fragments. Raw refs from other
  fragments land in `metadata.unmapped_refs` (20 of 71 raw outcome rows).

## Scan (zero model calls — no `--model` flag anywhere)

```sh
$UV scout scan scanners.py -V probe03_wedge:validation/wedge_stuck.json --display plain
# normalized -> scans/scan_id=CTd7CcyhnaGtchCBzaPsHV (54 transcripts, 378 inputs, 648 rows, 0 errors)
$UV scout scan scanners.py -T ./data/transcripts_raw --scans ./scans --display plain
# raw -> scans/scan_id=WTbJxoT5BZu3JYcS6Qynnq (71 transcripts, 497 inputs, 852 rows, 0 errors)
```

Read results with (`scan_results_df`, lowercase):

```python
from inspect_scout import scan_results_df
r = scan_results_df("scans/scan_id=CTd7CcyhnaGtchCBzaPsHV")
r.scanners["probe03_wedge"]  # value bool + validation_* columns
```

The scans list "Results" column counts positive-valued results only
(norm 362 = 41+34+98+72+54+54+9; raw 367).

## Validation (executed)

`probe03_wedge` (trial stuck-terminal True when probe-03 records stretches)
against the 54 blind hand-key cases: **53/54 valid (98.1%)** — precision
8/9 = 88.9%, recall 8/8 = 100%, F1 94.1%. The single miss is a false
positive on `har81-p-d-format-code-task-00152__P9EP83h` (two 3-turn pager
stretches with prompt returned; hand key says not stuck). Note the
in-sample caveat: probe-03 scored its wedge measurement against this same
key, so this validates the Scout plumbing + lookup fidelity, not the rule.
Oddity (observed, unexplained): the scan page shows "accuracy 98.9%" while
the stored entries give 53/54 = 98.1%; precision/recall/F1 match exactly.

## View (live, port 7576)

```sh
$UV scout view --host 127.0.0.1 --port 7576 --no-browser
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7576/  # 200
```

URL: `http://127.0.0.1:7576/` (service `scout-view`, left running; project
`harbor-trace-lab` points at the normalized DB + `scans/`). Screenshots in
`screenshots/`: `01-scans-list`, `02-scan-results` (per-scanner positives +
wedge validation metrics), `03-wedge-results`, `04-wedge-transcript-cite`
(pager 96-turn wedge result with `head#15 ([M28]); head#16 ([M30]);
head#111 ([M220])` citation links), `05-cited-message` (transcript jumped
to cited M30, less-pager screen visible), `06-validation` (54 cases).

Browser note: screenshots work from a managed tab here (an earlier note
said they error out through the user-Chrome relay; not the case now). The
relay browser is shared with sibling trials — coordinate before driving it.

## Scanners (`scanners.py`)

Text family: `repeated_assistant_message` (custom longest identical
assistant run; cites 1-based over all messages), `harness_parse_errors`
(4 labels, occurrence counts), `native_tool_call_markup` (2 labels,
assistant-only cites), `restored_tool_calls` (2 labels on Scout's
`Tool Call: <fn>` rendering — the raw/normalized visibility probe).

Probe-03 family (lookup in `probe03_lookup.json`, cites via the step-order
walk): `probe03_outcome` (value `tag/attribution/rule`, explanation carries
evidence cites + stop reason), `probe03_first_failure` (value rule or
`none`), `probe03_wedge` (value bool, validated). Explanations embed
`[Mn]` cites (`head#16 ([M30])`), which is what `scout view` turns into
clickable message links — references alone do not render as links.

## Verdict

- How it works: local Parquet DB of transcripts + Python scanners per
  transcript (grep or custom, sync/async) whose `Result`s (value,
  explanation, message references, metadata) are stored as Parquet and
  browsed in `scout view`; validation sets score scanner values against
  labels with P/R/F1 in the UI and CLI. No server, no account, no model
  needed for any of this.
- Good for us: whole-run normalized transcripts with real tool calls;
  deterministic label layer with exact citations that survive into the UI;
  validation UX works out of the box (metrics inline per scanner + a
  validation-set page); scans are seconds (54 trials x 7 scanners ~8 s);
  zero spend.
- Annoyed: `extra` silently dropped (our ref mapping is a re-walk
  workaround); one file = one transcript so raw continuations fragment
  (by design — normalization is the fix); UI "Results"/"accuracy" counts
  use their own definitions (positives only; 98.9 vs stored 98.1);
  `task_id`/scores empty (no eval-log join); result detail embeds the whole
  transcript (82k-char snapshots, virtualized list); shared relay browser
  needs coordination for screenshots.
- Can our analysis live in its UI? Yes for the label layer: outcome /
  first-failure / wedge render with working citation jumps and validation
  metrics. Deeper reading (cross-trial comparisons, prevalence) stays in
  DataFrames/CLI — Scout View is per-result browsing, not analytics.
- Time taken: ~1.5 h active (imports ~5 min for 54+54, scans ~10 s each,
  most of the rest interactive screenshot-driving and one cite-linkify
  rescan). Credits: analysts are deterministic code; $0 spent.
