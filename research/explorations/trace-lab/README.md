# Trace Lab

Tools for reading Harbor / Eval Lab run traces: turning raw trial folders
into whole-run transcripts, tagging them with deterministic rules, and
browsing them in Inspect Scout, Docent, or `harbor view`. Built
2026-09-26–29 for the HAR-81 / HAR-90 MiMo runs. Everything here is $0
(local code, no model calls); only Docent uploads touch the network, and
only with an explicit flag.

Related Linear cards: HAR-77 (probe-01 cheat detector), HAR-87 (probe-02
MiMo kit).

## Folders

- `normalize/` — the shared pre-processor (`harbor_normalize.py`). MiMo
  runs record no structured `tool_calls` and split long runs across
  continuation files; the normalizer stitches continuations and restores
  stock-shaped `tool_calls`, one file per trial. Run it before every tool.
- `docent/` — Docent export (`export_harbor.py`), blind-upload
  (`upload_har81_blind.py`), reading fetch/scoring (`fetch_results.py`,
  `score_reading.py`, …), plus the scored HAR-81 blind reading
  (`har81_reading_results.json`). Full story: `docent/README.md`.
- `scout/` — Inspect Scout layer: 7 deterministic scanners
  (`scanners.py`), build scripts, `scout.yaml`, view screenshots.
  Full story: `scout/README.md`.
- `viewers/` — `harbor view` fixture jobs (`harbor-norm-jobs/`, small
  normalized copies), the comparison notes (`NOTES.md`), and one citable
  traj card (`traj-card-raw-short.md`).
- `probe-01-git-peek/` — git-peek cheat detector proven on public
  SWE-bench runs (HAR-77): `detect.py --mode swe|local|atif`.
- `probe-02-mimo-kit/` — $0 behavior-metrics kit: per-trial metrics,
  report card, reading sheet (`metrics.py`, `report_card.py`,
  `reading_sheet.py`), plus a Docent exporter (HAR-87).
- `probe-03-capabilities/` — HAR-91 capability tagger (`capabilities.py`):
  five never-collapsed dimensions per trial (outcome, stop reason, first
  failure, outcome-relevant failure), blind hand keys in `validation/`.
- `2026-09-29-system-state/` — system diagrams (Excalidraw + SVG) and
  notes on how runs flow into decisions.
- `LANDSCAPE.md` — the tool landscape: which trajectory tools work with
  Harbor, the compatibility table, the rigorous-reading method.
- `docent_upload.py` — upload local Harbor runs to Docent (dry run by
  default).

## Data (local only)

Large and regenerable files live outside git at
`~/Developer/eval-lab/derived/trace-lab/`, mirroring these folder names:

- `normalized/har81/`, `normalized/har90/` — normalizer output (54 trials)
- `scout/data/`, `scout/scans/` — Scout transcript DB + scan results
- `viewers/harbor-raw-jobs/`, `viewers/screenshots/` — raw viewer copies
- `docent/har81-blind-payload.json` — the 31 MB Docent upload payload
- `2026-09-29-system-state/*.png` — full-resolution diagrams

Nothing here is precious: the normalized files regenerate with the
normalize command below, the Scout DB rebuilds with the import/scan
commands in `scout/README.md`, and the viewer copies are copies.

## Commands

From the eval-lab checkout (`cd ~/Developer/eval-lab` first, unless noted):

```sh
# Normalize a set of runs (writes into derived/trace-lab/normalized/<set>)
cd research/explorations/trace-lab
U="uv run --no-project --python 3.12 --with harbor==0.21.0 python"
$U normalize/harbor_normalize.py <runs>/har81-* \
  --out ~/Developer/eval-lab/derived/trace-lab/normalized/har81 \
  --evallab-src <worktree>/src \
  --check probe-03-capabilities/har81/capabilities.jsonl
```

```sh
# Upload runs to Docent: dry run first (converts + secret-scans, uploads nothing)
uv run --no-project --with docent python research/explorations/trace-lab/docent_upload.py
# Really upload (needs ~/.docent/docent.env, see probe-02-mimo-kit/README.md)
uv run --no-project --with docent python research/explorations/trace-lab/docent_upload.py --upload

# Score a blind reading against the hand keys (from the docent/ folder)
cd research/explorations/trace-lab/docent
python score_reading.py
```

```sh
# Open the Scout viewer (data + a scout.yaml copy live in derived)
cd ~/Developer/eval-lab/derived/trace-lab/scout
uv run --no-project --python 3.12 --with inspect-scout==0.5.3 --with harbor==0.21.0 \
  scout view --host 127.0.0.1 --port 7576 --no-browser
# then open http://127.0.0.1:7576/
```

```sh
# Open harbor view on the normalized fixtures (tracked) or raw jobs (derived)
uv run --no-project --python 3.12 --with harbor==0.21.0 harbor view \
  research/explorations/trace-lab/viewers/harbor-norm-jobs --port 8602
```

Behavior metrics on any job dir (`--help` on each script lists flags):

```sh
cd research/explorations/trace-lab/probe-02-mimo-kit
uv run --no-project python metrics.py <job_dir>... --out metrics.jsonl
uv run --no-project python report_card.py metrics.jsonl --out report_card.md
uv run --no-project python reading_sheet.py metrics.jsonl --out reading_sheet.md
```
