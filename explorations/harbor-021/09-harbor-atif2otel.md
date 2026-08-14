# harbor-atif2otel — validate + convert

## What it is

`packages/harbor-atif2otel` (and the `uvx` package of the same name) converts
an ATIF trajectory to OpenTelemetry `ResourceSpans`. `validate_trajectory()`
returns issue strings (empty = valid). `convert_trajectory()` emits a root
AGENT span plus LLM/TOOL children. A Harbor `--plugin atif2otel` can stream
or batch-write, but that is out of scope here — no backend, no OTLP.

Brief 08 is Phoenix + this converter.

Lab evidence `event-summary` oracle/nop trials have no `trajectory.json`
(oracle writes `oracle.txt`). The demo uses a real previously-run Codex
ATIF from harbor-practice, copied to
`explorations/harbor-021/fixtures/trajectory.json` and
`runs/atif-source-trial/agent/trajectory.json`.

## Demo

```bash
bash explorations/harbor-021/demos/run-atif2otel.sh
# equivalent:
uvx --from harbor-atif2otel python explorations/harbor-021/demos/run_atif2otel.py \
  --trajectory runs/atif-source-trial/agent/trajectory.json \
  --out explorations/harbor-021/captures/atif2otel/otel.json
```

Observed (2026-08-13, first run):

```
trajectory=.../runs/atif-source-trial/agent/trajectory.json
schema_version=ATIF-v1.7
agent=codex@0.146.1
n_steps=9
validate_issues=0
otel_bytes=24159
n_spans=10
span_kinds=['AGENT', 'LLM', 'TOOL']
n_root_spans=1
root_kinds=['AGENT']
root_names=['codex']
OK: valid ATIF; non-empty OTel payload with root AGENT span
```

No issues; payload is not an empty file. Transcript:
`captures/atif2otel/demo.log`. Output: `captures/atif2otel/otel.json`.

## Verdict

**Adopt first, into brief 08 (Phoenix + trace shipping).** The convert API
works locally today; `harbor-lab trace` should call `validate_trajectory` then
`convert_trajectory` / `export_trial` and only then OTLP to Phoenix. Also
usable as a `--plugin atif2otel` once the job-plugin demo is wired into the
executor (05/08). Do not add `harbor-atif2otel` to the root lockfile from
this worktree — BUILDER owns that in brief 08.
