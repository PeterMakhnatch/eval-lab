# NP22: shared retriever renders no source, split or label provenance

Read-only, zero model calls, zero spend. Store: a read-only copy of the cloud trace
store (4,207,423,488 bytes, taken 2026-10-28 16:26 local), the same 44-task SB-1 cohort,
the same `FILTER`, label-key regex and cluster/anchor choice as the preregistered SB-5
audit (`research-context/bridge/2026-10-27-run-atlas/raw/sb5-leak-audit-20261028/`).
The assignments digest equals SB-5's (`sha256:7f4703f6…880f`).

| file | what |
|---|---|
| `tools/measure.py` | SB-5 modes M2/M3 (no exclusion and excluding the target) over the 44 targets; counts only |
| `tools/render.py` | renders the Task QA block, the Task QA audit bundle and the window-cascade examples from a synthetic store |
| `results/measure-before.json`, `results/measure-after.json` | counts before (source tree `98aeca5c9`, attested by `evallab_module`) and after (the same tree plus this change, measured before it was committed, so its `tree_head` still reads `98aeca5c9`) |
| `results/render-before.json`, `results/render-after.json`, `results/render.diff` | the rendered prompts before and after |

## Results

SB-5 counted struct fields. This run also counts what the rendered block shows, outside
identifiers (an id may embed a dataset name, which is a separate channel).

| 44 targets, 132 facet examples | before | after |
|---|---|---|
| examples whose rendered text shows `dataset_id`, `split`, `label_categories` or `receipt_status` | **132** | **0** |
| the same, `split`/`label_categories`/`receipt_status` alone | 0 (hidden only by the 400-character clip) | 0 (withheld by construction) |
| Atlas query with no named exclusion (M2, M3, 88 cells) | answered | **refused 88/88** |
| same-task examples (G1) | 0 | 0 |
| examples carrying a `- task asks:` line (another task's instruction) | 132 | 132 (kept for QA workers) |
| struct `instruction` / label-key `provenance` (SB-5 G2 counting) | 132 / 132 | 132 / 132 (struct kept for field readers) |
| examples whose task or trace id embeds the dataset name | 132 | **132** (not changed here) |
| M3 (cluster + anchor) | 0 eligible targets 44/44 | 44/44 |

The preregistered G2 gate (struct fields) therefore still reads FAIL: it counts the
`instruction` field and the `passage_window` key, which this change keeps on purpose.
What changed is the rendered prompt.

## Prompts still carry their evidence (`results/render.diff`)

- Task QA retrieval block: exactly two lines differ. `- source: dataset X; issue I; trace T`
  becomes `- source: issue I; trace T`, and the `- provenance:` JSON loses four keys (its
  400-character clip now reaches `step_indices`, which it used to cut off).
- Task QA audit bundle (`tq_context.assemble_audit_context`): byte-identical. It reads
  structured fields, not `prompt_lines`, and still carries `dataset_id`, `split` and
  `receipt_status` (see the receipt: a remaining channel to a non-blind worker).
- Window cascade examples (`window_cascade.retrieved_examples`): byte-identical; they read
  `trust_tier` and the step list, not `prompt_lines`.
