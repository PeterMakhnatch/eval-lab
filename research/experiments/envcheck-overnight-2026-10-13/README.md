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
Audits and repair validation are serialized: one task trial at a time. Serial
dispatch alone was insufficient: retained Docker events exposed a **three-container
peak** (agent, egress sidecar, and optional state-journal observer). Dispatch was
stopped on discovery; the observer is disabled for resumed runs using
`EVALLAB_STATE_JOURNAL=off`. The grader and coverage trace are unchanged. A
continuous owned-container event monitor now interrupts dispatch above two.
The interrupted audit reuses the original digest-checked native matrix receipts;
11 scored mutants were retained without rerunning them, and one interrupted
mutant remains explicitly unscored. Images pulled by this slice are removed
without force after use; images already present are retained.

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
EVALLAB_STATE_JOURNAL=off uv run --no-sync evallab hack hunt \
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
The old findings persist on the current v5 digests. After one measured-feedback
refinement per seed, both derived variants retain reference 1 and nop 0:

| task | original traced survivors / 12 mutants | previous survivors killed by new assertions | outcome | derived variant |
|---|---:|---:|---|---|
| 002402 | 11 | 11 / 11 | **repaired**, validated | `bd5b2efb9a4e` |
| 002391 | 3 | 2 / 3 | **partial**, candidate | `879d41b6ac07` |

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

002402 now checks promoted Normal and Dirichlet batch/event shapes and
expanded-distribution means, variances, log probabilities and base sampling.
Every previous survivor scores 0. Its final receipt is
`runs/envcheck-overnight-1013/repairs/002402-refine1/repair-result.json`.

002391 now rejects both functional alias/deduplication defects
(`a68a99652d`, `8bd7075fd8`). The sole remaining survivor, `a8d7478b61`,
changes dataclass mutability rather than the requested alias behavior. The
model explicitly identified it as out of scope; no incidental-immutability
assertion was added merely to satisfy the count. Therefore this variant is
**not claimed to meet the all-survivors-killed acceptance criterion**.
Receipt: `runs/envcheck-overnight-1013/repairs/002391-refine1/repair-result.json`.

## Variant custody and spend

`seed-variants.json` indexes the four seed-attempt lineage records, including
the two rejected first proposals. The repository ignores newly generated
`library/task-variants/*/*.json`; these records are preserved byte-for-byte
with SHA-256 verification outside Git at
`~/Developer/eval-lab-results/2026-10-13/envcheck-overnight/lineage/`.
The materialized packages remain in the canonical shared
`derived/task-store/variants/` store; no source task was edited or registered.

Seed proposal usage is in `seed-proposal-usage.json`: four completed ordinary
ChatGPT-subscription calls, 98,604 input and 6,270 output tokens; shared weekly
usage observed before those calls was 4–6%, not attributable solely to this lane.
The existing Eval Lab `mimo-census record-spend` ledger records **$0 incremental
provider spend** for both seed batches in `spend.jsonl`. The binding approval is
this slice's $10 cap, not the census command's unrelated default slice ceiling.
