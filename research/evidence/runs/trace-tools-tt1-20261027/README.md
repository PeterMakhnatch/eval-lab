# Trace Tools TT-1: production Stage-1 baseline

## Question

What does the production cascade's deterministic Stage-1 flag across **every
admitted Goodfire DEV trace**, and does this full-corpus replay preserve the
retained CM-9 baseline on its 200 Goodfire traces?

This is a rule-card coverage measurement, **not** a label scorecard or a confirmed
misconduct count. Publisher labels are neither replay inputs nor output fields.

## What ran

```bash
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run python \
  research/evidence/runs/trace-tools-tt1-20261027/tools/stage1_goodfire_dev.py
```

- Source revision: `ac1533212a57c5ce1fe6826ba7fdccea5045ab12` (production source
  inherited from `origin/main` at `7762ea56a1d03b739f46d4a8da487e22683d00f8`).
- Cascade: `window-cascade/v7`, using
  `MisalignmentLiveWorker(emit=emit, judge=None)` with its production defaults.
  The receipt records the cascade constants and source-file SHA-256 digests.
- Input: read-only `~/.local/state/evallab-prod-live/cloud-trace-db.sqlite`.
  Selection requires `dataset_id='goodfire-reward-hack-data'`, exact
  `corpus_tasks.split='DEV'`, task admission through the shared
  `admitted_cloud_task_ids(..., registry=CorpusRegistry(repo_root))` cross-key
  gate, and receipt `$.admission.effective_split='DEV'`.
- Native-message conversion copies the minimal semantics of retained CM-2
  `cm2-monitor-20261026/lib.py:load_steps`; event replay follows retained CM-9
  `cm9-window-budget-20261027/stage1.py:replay`. Neither bridge module is imported.
  SQL projects only native tool calls, reasoning, and tool-call IDs from
  `flags_json`, excluding publisher annotations. Only steps and an opaque
  synthetic run ID reach the monitor; task, source, model, split, and trace IDs
  are not monitor metadata.
- Multiprocessing: **14 workers**, equal to the machine's **16 CPUs minus 2**.
  No model, paid call, upload, Docker, or local inference was used: **$0**.
- Run interval: **October 27, 2026, 5:22:00–6:52:37 PM ET**. Receipt timestamps
  retain UTC. Wall time: **5,437.15 seconds**.

The output is retained outside Git at:

```text
~/.local/state/evallab-prod-live/trace-tools/cascade-stage1-goodfire-dev.jsonl
```

Each line contains exactly `trace_id`, `stage1_any`, and `cards`; cards contain
only `rule`, `origin`, and `flagged_step`. `stage1_any` means at least one emitted
Stage-1 suspicion card, including diagnostic leads; it is not Stage-2 confirmation.
The script refuses to overwrite an existing output. Use distinct `--output` and
`--receipt` paths for a later replay.

## Numbers

| Measurement | Observed |
| --- | ---: |
| Goodfire corpus-DEV traces | 3,674 |
| Traces with receipt effective split DEV | 3,674 |
| Excluded by receipt / shared admission gate | 0 / 0 |
| Admitted DEV traces replayed and output | **3,674** |
| Distinct admitted tasks | 1,048 |
| Native DB steps | 550,955 |
| Converted replay steps (tool results attached to assistant actions) | 266,148 |
| Traces with any Stage-1 card | **2,925** |
| Traces with no Stage-1 card | 749 |
| Total Stage-1 cards | **21,535** |
| CM-9 reference traces compared | **200 / 200** |
| Matching rule/origin/flagged-step multisets | **200 / 200** |
| Mismatching / missing reference traces | **0 / 0** |

| Card origin | Cards |
| --- | ---: |
| Prefilter | 10,490 |
| Hackbook | 7,153 |
| Tripwire | 3,845 |
| Structural | 47 |

Parity compares **multisets**, not sets, preserving repeated card identities.
The reference is the retained `cm9-window-budget-20261027/e2e-muse-g.jsonl`;
reference judgments and labels are discarded before comparison and never enter
the replay. The complete counts by rule and parity result are in
[`stage1-receipt.json`](stage1-receipt.json).

Output size: **2,102,321 bytes**, **3,674 JSONL lines**. SHA-256:

```text
daae67ff6f10fbc1518c96934fef94eb3c4ff17196089b3af4b0c289b2cf816d
```

Checks exercised: full-corpus replay plus the 200-trace parity comparison;
`uv run ruff check research/evidence/runs/trace-tools-tt1-20261027/tools/stage1_goodfire_dev.py`
and `uvx ty@0.0.71 check src/ --output-format=concise` both reported
`All checks passed!`. `wc -l` independently reported 3,674 output lines.
