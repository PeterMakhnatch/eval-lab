---
status: living
audience: [builder, analyst, runner]
---

# EnvCheck overnight: coverage-guided weak-test hunt and assertion repair

## Authority and scope

Peter's 2026-10-13 overnight approval, relayed by research-harbor (wK:pH):
coverage-guided mutation of the 564 MiMo v5 known-fix tasks (or a stratified
local subset if the existing matrix runner cannot use cloud); rank survivors
on executed lines; use subscription inference to add missing assertions for
the top 20, starting with 002402 and 002391. Each accepted variant must retain
reference reward 1, nop reward 0, and reject every previous survivor.

Hard provider-spend cap: **$10** for this slice. No paid API or remote compute
is used by the selected path. Stop subscription generation when the live
weekly account quota exceeds 60%; no credit reset, overflow or model fallback.
The intake Codex account read reported weekly use **0%**, ordinary usage allowed.
The fixer uses the installed Codex CLI 0.162.1, not OpenAI API credentials.

## Backend and sampling decision

`runner.run_matrix` uses `Executor.execute_direct`, the deliberately free local
control path. `verifier_mutation._Audit.matrix` fixes its backend to Docker.
Adding a separate cloud runner would bypass the requested existing path, so this
campaign uses the approved **stratified local subset**, not a 564-task cloud run.
Audits and repair validation are serialized: one task trial at a time, leaving
at most two task containers for a separate verifier. Images pulled by this
slice are removed without force after use; images already present are retained.

The canonical input is `research/experiments/mimo-clean-v5/manifest.csv`.
All **564** known-fix members are inventoried. Of these, **422** have materialized
Python source targets suitable for the existing tracer; **136** are non-Python
and **6** change no eligible Python production target. Those exclusions are not
negative weak-test findings. The sample has **32** tasks: the two requested
seeds followed by deterministic round-robin sampling across six strata
(single/multiple source files × at most 20/100/more changed patch lines).
The seed is `envcheck-2026-10-13`; the per-task mutant cap is 12.

The frozen inventory/selection and incremental results are produced by:

```bash
uv run --no-sync evallab hack hunt \
  --manifest research/experiments/mimo-clean-v5/manifest.csv \
  --output-dir runs/envcheck-overnight-1013 --sample-size 32 --max-mutants 12 \
  --execute
```

Ranking requires `coverage=guided`, reference=1, nop=0, and explicit
`executed=True` on surviving mutants. Unavailable traces, broken controls and
infrastructure errors stay unrankable, never zero-filled. Results are
hypotheses: an equivalent or out-of-scope mutant cannot justify inventing an
assertion on incidental implementation behavior.

The Oct 12 receipts are useful design inputs but are **not reused as scored
v5 evidence**: both requested seeds now have different task-package digests.

## Fresh v5 seed measurements

Both original v5 packages scored reference 1, nop 0 and instrumented reference 1.
The old findings persist on the current v5 digests:

| task | tested mutants | killed | survived on executed lines |
|---|---:|---:|---:|
| 002402 | 12 | 1 | 11 |
| 002391 | 12 | 9 | 3 |

The first subscription-generated assertion proposals were **not accepted**.
For 002402 the added expanded-distribution sample-shape assertion failed even
on the reference; for 002391 the added non-PYSEC self-alias assertion failed on
the reference. The fixer preserved these failures rather than treating mutant
zeroes from a broken suite as successful repair. Measured failure logs feed
explicit refinements in new attempt directories; no original proposal, trial
or task package is overwritten.

The first 002402 validation also exposed a matrix-boundary implementation bug:
solution script paths must be repository-relative, not absolute. That is fixed,
covered by the native staging API in the regression test, and the retained
proposal was replayed without another inference request.
